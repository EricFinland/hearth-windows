#!/usr/bin/env python3
r"""Build the Hearth Windows installer from a clean checkout.

    python scripts/build_windows.py

That is the whole command. It fetches and verifies everything the installer
needs, regenerates and checks the third-party notices, stages the payload,
compiles the shell, runs the bundler, reads the result back out of the
executable, and prints a size breakdown. From a fresh clone on a machine
with Rust and Python it needs nothing else.

What it needs on the build machine
----------------------------------
    Rust (cargo) 1.82+   to compile the shell
    cargo-tauri          the bundler; `cargo install tauri-cli --locked`
    Python 3.11+         to run this script and the two vendor scripts
    network              first run only, to fetch llama-server and CPython;
                         --offline afterwards

Nothing on the TARGET machine except the WebView2 runtime, which every
supported Windows already ships and which the installer fetches if a machine
somehow does not have it. That is the point of the exercise: the installer
carries its own Python and its own inference engine, and borrows the browser
engine that is already there.

Node is no longer required, on either machine. The shell was Electron until
this port, and an Electron runtime is 364 MB of Chromium, Node and V8 next
to a 70 MB payload -- 84% of an install, to draw a window and start a
subprocess. See docs/packaging-windows.md for the measurements.

The steps, and why each one is here
-----------------------------------
  1. vendor llama-server   scripts/vendor_llama.py, pinned and checksummed
                           against vendor/llama_manifest.json. The binary is
                           gitignored, so a clean checkout does not have it
                           and the build has to fetch it rather than assume
                           it is lying around.
  2. vendor CPython        scripts/vendor_python.py, same discipline, from
                           vendor/python_manifest.json. See that file for
                           why the embeddable package rather than a freezer.
  3. check the notices     scripts/third_party_notices.py --check, run here
                           because this is the moment the versions it
                           describes are on disk and about to be packaged.
  4. stage the payload     build/stage/, laid out so that
                           agent/hearth_llama.app_root() finds
                           vendor/llama/llama-server.exe with no
                           packaging-specific code in the agent, and
                           carrying the licence texts the installed
                           application has to be able to show.
  5. build                 cargo tauri build, from desktop/tauri/. Compiles
                           the shell, links desktop/ui/ into it, and produces
                           an NSIS installer, which is copied to
                           build/dist/Hearth-Setup-<version>.exe
  6. verify the binary     scripts/verify_binary.py, which reads back out of
                           the shipped executable the things step 5 asked
                           for. Replaces desktop/shell/verify-fuses.js.

Nothing under build/ is committed, and neither is anything under vendor/
except the two manifests and vendor/licenses/.

The installer is UNSIGNED. See docs/packaging-windows.md for exactly what a
user sees the first time they run it, which is a full-screen SmartScreen
warning, and why that is not something a build flag can fix.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import vendor_llama  # noqa: E402
import vendor_python  # noqa: E402
import verify_binary  # noqa: E402

BUILD_DIR = os.path.join(REPO_ROOT, "build")
STAGE_DIR = os.path.join(BUILD_DIR, "stage")
DIST_DIR = os.path.join(BUILD_DIR, "dist")
TAURI_DIR = os.path.join(REPO_ROOT, "desktop", "tauri")
TAURI_CONFIG = os.path.join(TAURI_DIR, "tauri.conf.json")

#: Out of the crate directory, so a `cargo clean` in a checkout does not have
#: to know about it and nothing a build writes lands under desktop/.
#: desktop/tauri/tauri.conf.json's frontendDist points into build/ for the
#: same reason.
CARGO_TARGET_DIR = os.path.join(BUILD_DIR, "cargo-target")
RELEASE_DIR = os.path.join(CARGO_TARGET_DIR, "release")
BUNDLE_DIR = os.path.join(RELEASE_DIR, "bundle", "nsis")

#: The UI tree that gets linked INTO the executable. Assembled by
#: desktop/tauri/build.rs and named by frontendDist in tauri.conf.json; the
#: three of them have to agree.
UI_EMBED = os.path.join(BUILD_DIR, "ui-embed")

#: Copied into the payload verbatim. Source path relative to the repo root,
#: destination relative to build/stage/hearth.
#:
#: desktop/ui is deliberately NOT here. It used to be, because Electron's
#: main process read index.html off disk; the Tauri shell has it linked into
#: the executable instead, and staging a second copy next to the executable
#: would put the application's own code back on disk where anything running
#: as the user could rewrite it. scripts/verify_binary.py fails the build if
#: it reappears.
PAYLOAD_TREES = (
    ("agent", "agent"),
    (os.path.join("desktop", "server"), os.path.join("desktop", "server")),
)

#: The licence texts the installed application has to carry, copied into the
#: payload so they land inside the install rather than only in the repo.
#: See docs/licensing.md. verify_stage() refuses to build without them.
LICENCE_FILES = ("LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md")
LICENCE_TREE = os.path.join("vendor", "licenses")

#: Never staged. __pycache__ is build residue from whatever interpreter last
#: ran here and would ship stale bytecode compiled by a different Python.
#: dev-host.mjs and the xss-check pages are development tools: the shell
#: replaces the first and the second is a test harness, and neither belongs
#: in an installed application. Kept in step with excluded() in
#: desktop/tauri/build.rs, which applies the same rule to the UI tree that
#: gets linked into the executable.
EXCLUDE_NAMES = frozenset({"__pycache__", "dev-host.mjs"})
EXCLUDE_PREFIXES = ("xss-check.",)


def _excluded(name):
    return name in EXCLUDE_NAMES or name.startswith(EXCLUDE_PREFIXES)


def _ignore(_dir, names):
    return [n for n in names if _excluded(n)]


def dir_size(path):
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not _excluded(d)]
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def mb(n):
    return "{:7.1f} MB".format(n / 1e6)


def app_version():
    """The version this build is, read from the one place that decides it.

    tauri-build compiles this value into the executable's resource block and
    app.package_info().version reads it back, which is what the updater's
    downgrade check compares against. There is no second copy to drift.
    """
    with open(TAURI_CONFIG, "r", encoding="utf-8") as fh:
        return json.load(fh)["version"]


def run(argv, cwd, label, env=None):
    """Run a build command, streaming its output. Raises on failure."""
    print("\n$ {}  (in {})".format(" ".join(argv), os.path.relpath(cwd, REPO_ROOT) or "."))
    started = time.monotonic()
    # shell=True on Windows so cargo's shims resolve the same way they do at
    # a prompt.
    completed = subprocess.run(argv, cwd=cwd, shell=(os.name == "nt"), env=env)
    if completed.returncode != 0:
        raise SystemExit("{} failed with exit code {}".format(label, completed.returncode))
    print("  {} took {:.1f}s".format(label, time.monotonic() - started))


def tool_version(argv):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, shell=(os.name == "nt"))
    except OSError:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def preflight():
    cargo = tool_version(["cargo", "--version"])
    tauri = tool_version(["cargo", "tauri", "--version"])
    missing = []
    if not cargo:
        missing.append("cargo (install Rust from https://rustup.rs)")
    if not tauri:
        missing.append("cargo-tauri (cargo install tauri-cli --locked)")
    if missing:
        raise SystemExit(
            "missing build tools:\n  {}\nNothing else is needed; the installer "
            "ships its own Python and borrows the WebView2 runtime Windows "
            "already has.".format("\n  ".join(missing)))
    print("{}   cargo-tauri {}   python {}".format(
        cargo, tauri, sys.version.split()[0]))
    if os.name != "nt":
        print("warning: this produces a Windows installer and has only been run on "
              "Windows. The vendored binaries are Windows x64, the shell links "
              "against WebView2, and nothing here is tested off Windows.")


def check_notices():
    """Fail the build when THIRD-PARTY-NOTICES.md no longer describes what is
    about to be packaged.

    Run here, between vendoring and staging, because this is the one moment
    when the versions the notices claim are on disk and have not yet been
    wrapped in an installer. A version bump that slips past this ships an
    installer whose compliance paperwork is about a different program.
    See docs/licensing.md.
    """
    print("\n== checking third-party notices ==")
    out = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "third_party_notices.py"),
                          "--check"], capture_output=True, text=True, cwd=REPO_ROOT)
    sys.stdout.write(out.stdout)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        raise SystemExit(
            "THIRD-PARTY-NOTICES.md is stale. Regenerate it with:\n"
            "    python scripts/third_party_notices.py")


def stage(offline=False):
    """Fetch, verify and lay out everything the installer will carry."""
    print("\n== vendoring llama-server ==")
    llama = vendor_llama.vendor(offline=offline, log=lambda m: print("  " + m))
    print("\n== vendoring CPython ==")
    python = vendor_python.vendor(offline=offline, log=lambda m: print("  " + m))

    check_notices()

    print("\n== staging the payload ==")
    vendor_python._rmtree_with_retries(STAGE_DIR)
    payload = os.path.join(STAGE_DIR, "hearth")
    os.makedirs(payload)

    for src_rel, dest_rel in PAYLOAD_TREES:
        src = os.path.join(REPO_ROOT, src_rel)
        dest = os.path.join(payload, dest_rel)
        if not os.path.isdir(src):
            raise SystemExit("expected {} to exist in the checkout".format(src_rel))
        shutil.copytree(src, dest, ignore=_ignore)
        print("  {:<24} -> hearth/{}".format(src_rel, dest_rel))

    # The engine lands at hearth/vendor/llama because that is the last entry
    # in hearth_llama.BUNDLED_SUBDIRS, so find_server() locates it with no
    # packaging-aware code in the agent at all.
    llama_dest = os.path.join(payload, "vendor", "llama")
    shutil.copytree(vendor_llama.install_dir(), llama_dest)
    print("  vendor/llama             -> hearth/vendor/llama")

    # The supply chain ships WITH the application, because acquiring the GPU
    # engine happens on the user's machine after installation and has to use
    # exactly this code path and exactly these pinned hashes. Shipping only
    # the manifest, or only the fetcher, would leave agent/hearth_engine.py
    # unable to do the one thing it exists for. The layout is not arbitrary:
    # vendor_llama resolves its manifest as ../vendor/llama_manifest.json
    # relative to itself, and hearth_engine imports it from
    # hearth_llama.app_root()/scripts, so both files must land exactly here.
    os.makedirs(os.path.join(payload, "scripts"), exist_ok=True)
    shutil.copy2(os.path.join(SCRIPTS_DIR, "vendor_llama.py"),
                 os.path.join(payload, "scripts", "vendor_llama.py"))
    shutil.copy2(vendor_llama.MANIFEST_PATH,
                 os.path.join(payload, "vendor", "llama_manifest.json"))
    print("  scripts/vendor_llama.py  -> hearth/scripts/vendor_llama.py")
    print("  vendor/llama_manifest    -> hearth/vendor/llama_manifest.json")

    # The licence texts. Every one of the licences Hearth redistributes under
    # requires that its notice travel with the binary, and a notice that only
    # exists in a git repository has not travelled anywhere. These land beside
    # the code they describe, inside the install. The bundler additionally
    # puts the first three at the application root, where somebody looking for
    # them would look first; this copy is the one that is guaranteed to be
    # next to the thing it is about. See docs/licensing.md.
    for name in LICENCE_FILES:
        src = os.path.join(REPO_ROOT, name)
        if not os.path.isfile(src):
            raise SystemExit(
                "{} is missing from the checkout. The installer may not ship "
                "without it; see docs/licensing.md.".format(name))
        shutil.copy2(src, os.path.join(payload, name))
        print("  {:<24} -> hearth/{}".format(name, name))
    shutil.copytree(os.path.join(REPO_ROOT, LICENCE_TREE),
                    os.path.join(payload, LICENCE_TREE), ignore=_ignore)
    print("  vendor/licenses          -> hearth/vendor/licenses")

    # The update trust anchor. release/trust.json holds the Ed25519 PUBLIC key
    # that agent/hearth_update.py checks every release manifest against, and it
    # has to travel inside the application: a key fetched at update time from
    # the same place as the update is not a key, it is a suggestion. This is
    # the single most important file in the payload after the code itself,
    # which is why verify_stage() below refuses to build without it.
    #
    # version.json is written FROM desktop/tauri/tauri.conf.json rather than
    # kept as a second committed copy, so the two cannot drift and the updater
    # cannot be told it is running a version that was never built. The shell
    # additionally passes its own version down in HEARTH_APP_VERSION, which the
    # updater prefers because it comes out of the executable's resource block
    # rather than off disk; this file is the fallback for a payload run without
    # the shell.
    release_dest = os.path.join(payload, "release")
    os.makedirs(release_dest, exist_ok=True)
    trust_src = os.path.join(REPO_ROOT, "release", "trust.json")
    if not os.path.isfile(trust_src):
        raise SystemExit(
            "release/trust.json is missing. Without it the installer ships an "
            "application that cannot verify its own updates. Generate a signing "
            "key with:\n    python scripts/release_manifest.py keygen --key-id <id>")
    shutil.copy2(trust_src, os.path.join(release_dest, "trust.json"))
    version = app_version()
    with open(os.path.join(release_dest, "version.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": version, "app_id": "com.hearthlocal.hearth"},
                  fh, indent=2, sort_keys=True)
        fh.write("\n")
    print("  release/trust.json       -> hearth/release/trust.json")
    print("  version {:<16} -> hearth/release/version.json".format(version))

    shutil.copytree(vendor_python.install_dir(), os.path.join(STAGE_DIR, "python"))
    print("  vendor/python            -> python")

    # A staged tree that cannot import itself produces an installer that
    # fails on the user's machine with a traceback nobody will see, so check
    # here instead. This runs the STAGED interpreter against the STAGED
    # payload, in the same relative layout the installer ships.
    verify_stage()

    return {
        "llama_variant": llama.get("variant"),
        "python_version": python["pinned_version"],
        "version": version,
    }


def verify_stage():
    """Prove the staged payload works before wrapping it in an installer.

    Five checks, each covering a failure that would otherwise only appear
    on a user's machine, at launch, as a dialog with a traceback in it --
    or, for the last one, never appear at all.
    """
    exe = os.path.join(STAGE_DIR, "python", "python.exe")
    server_dir = os.path.join(STAGE_DIR, "hearth", "desktop", "server")
    agent_dir = os.path.join(STAGE_DIR, "hearth", "agent")
    # PYTHONDONTWRITEBYTECODE so these checks do not leave __pycache__
    # directories in the tree they are checking, which would then be packaged.
    #
    # HEARTH_ENGINE_DIR points at a directory that does not exist, because
    # find_server() now prefers a GPU engine hearth_engine fetched, and a
    # build machine with a GPU has one. Without this the "the bundled engine
    # is in the payload" check below is answered by the BUILD MACHINE's own
    # fetched engine and would pass on an installer that shipped no engine
    # at all. Caught exactly that way, once.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               HEARTH_ENGINE_DIR=os.path.join(BUILD_DIR, "no-such-engines"))

    # 1. The staged interpreter can run the staged sidecar. This is the real
    #    entry point, run the real way, so it also covers the import
    #    bootstrap at the top of main.py -- the thing that makes an isolated
    #    interpreter able to import the sidecar's own siblings at all.
    out = subprocess.run([exe, "main.py", "--self-test"], capture_output=True,
                         text=True, env=env, cwd=server_dir)
    if out.returncode != 0:
        raise SystemExit(
            "the staged sidecar does not run under the staged interpreter:\n{}\n{}".format(
                out.stdout.strip(), out.stderr.strip()))

    # 2. The bundled engine is where the agent looks for it. This depends
    #    entirely on the relative layout staged above: hearth_llama.app_root()
    #    resolves to the parent of the agent directory, and vendor/llama is
    #    the last entry in its BUNDLED_SUBDIRS. Rearranging the payload
    #    without knowing that ships an installer with no engine in it.
    probe = (
        "import sys, json; sys.path.insert(0, sys.argv[1]);"
        "import hearth_llama;"
        "print(json.dumps({'python': sys.version.split()[0],"
        "'llama': hearth_llama.find_server()['source']}))"
    )
    out = subprocess.run([exe, "-c", probe, agent_dir], capture_output=True,
                         text=True, env=env, cwd=server_dir)
    if out.returncode != 0:
        raise SystemExit("the staged agent modules do not import:\n{}".format(
            out.stderr.strip()))
    result = json.loads(out.stdout.strip())
    if result["llama"] != "bundled":
        raise SystemExit(
            "the staged payload found llama-server via {!r} rather than as a "
            "bundled binary; the installer would ship without an engine".format(
                result["llama"]))
    # 3. The GPU engine fetch can actually run from the staged layout. It
    #    needs THREE things that only exist because stage() put them there:
    #    scripts/vendor_llama.py, vendor/llama_manifest.json, and the two
    #    resolving to each other. Getting this wrong ships an installer
    #    whose users are permanently stuck on the CPU build with no error
    #    anybody would ever see, which is exactly the outcome fetching an
    #    engine exists to prevent. A plan is computed rather than a fetch
    #    performed: this asks "could it", on a build machine that may have
    #    no GPU and no network.
    probe = (
        "import sys, json; sys.path.insert(0, sys.argv[1]);"
        "import hearth_engine;"
        "m = hearth_engine._vendor_llama().load_manifest();"
        "plan = hearth_engine.choose_variant("
        "    m, gpus=[{'name': 'test', 'vendor': 'nvidia', 'vram_bytes': 1}],"
        "    nvidia=[], system='Windows', machine='AMD64', env={});"
        "print(json.dumps({'tag': m['release_tag'], 'variant': plan['variant'],"
        " 'reason': plan['reason']}))"
    )
    out = subprocess.run([exe, "-c", probe, agent_dir], capture_output=True,
                         text=True, env=env, cwd=server_dir)
    if out.returncode != 0:
        raise SystemExit(
            "the staged payload cannot run the GPU engine fetch; it would ship "
            "with every user pinned to the CPU build:\n{}".format(out.stderr.strip()))
    engine = json.loads(out.stdout.strip())
    if not engine["variant"]:
        raise SystemExit(
            "the staged payload would fetch NO GPU engine for an NVIDIA card: "
            "{}".format(engine["reason"]))

    # 4. The updater can read its own trust anchor from the staged layout, and
    #    reports the version this build actually is. Getting this wrong ships
    #    an application that cannot verify an update -- which, unlike a missing
    #    GPU engine, is not a performance problem: it is a build with no way to
    #    deliver a security fix, and no user would ever see an error saying so.
    #    The `configured` assertion is the other half: nothing has been
    #    published, so a build whose pinned feed points at a resolvable host is
    #    a build that was configured by accident.
    probe = (
        "import sys, json; sys.path.insert(0, sys.argv[1]);"
        "import hearth_update as u;"
        "t = u.load_trust();"
        "print(json.dumps({'version': u.current_version({}),"
        " 'keys': [k['key_id'] for k in t['keys'] if k['status'] == 'active'],"
        " 'configured': u.configured(t, {}), 'feed': t['feed']}))"
    )
    out = subprocess.run([exe, "-c", probe, agent_dir], capture_output=True,
                         text=True, env=env, cwd=server_dir)
    if out.returncode != 0:
        raise SystemExit(
            "the staged payload cannot read its update trust anchor; it would "
            "ship unable to verify any update at all:\n{}".format(out.stderr.strip()))
    updater = json.loads(out.stdout.strip())
    if not updater["keys"]:
        raise SystemExit("the staged release/trust.json carries no active signing key")
    if not updater["version"]:
        raise SystemExit(
            "the staged payload cannot tell which version it is; the updater "
            "would refuse to run at all")
    if updater["configured"]:
        print("  NOTE: this build's update feed is {}, a resolvable host. Make "
              "sure that is deliberate.".format(updater["feed"]))

    # 5. The licence texts are in the payload. A build that silently drops
    #    them ships an installer that is out of compliance and looks
    #    identical to one that is not, which is the whole reason this is a
    #    build gate rather than a checklist. See docs/licensing.md.
    payload = os.path.join(STAGE_DIR, "hearth")
    for name in LICENCE_FILES:
        if not os.path.isfile(os.path.join(payload, name)):
            raise SystemExit(
                "{} did not reach the payload. Hearth may not be distributed "
                "without it; see docs/licensing.md.".format(name))
    licences = os.path.join(payload, LICENCE_TREE)
    texts = [n for n in os.listdir(licences)] if os.path.isdir(licences) else []
    if "MANIFEST.json" not in texts or len(texts) < 2:
        raise SystemExit(
            "hearth/vendor/licenses is empty or has no MANIFEST.json. "
            "THIRD-PARTY-NOTICES.md quotes those texts, so the installed "
            "application has to carry them; see docs/licensing.md.")

    print("  staged payload verified: sidecar self-test green under python {}, "
          "bundled engine found, GPU fetch would install {} from {}".format(
              result["python"], engine["variant"], engine["tag"]))
    print("  update trust anchor: version {}, active key(s) {}, feed {}{}".format(
        updater["version"], ", ".join(updater["keys"]), updater["feed"],
        "" if updater["configured"] else "  (unresolvable: nothing published)"))
    print("  licence texts staged: {}, and {} file(s) under vendor/licenses".format(
        ", ".join(LICENCE_FILES), len(texts)))


def cargo_env():
    """Keep every byte cargo writes under build/.

    A checkout should be able to run this and then `git status` and see
    nothing, which a target/ directory inside desktop/tauri/ would break.
    """
    return dict(os.environ, CARGO_TARGET_DIR=CARGO_TARGET_DIR)


def build_installer(unpacked_only=False):
    version = app_version()

    # cargo-tauri refuses to start when frontendDist does not exist, and
    # frontendDist is assembled by desktop/tauri/build.rs, which only runs
    # once cargo-tauri has started. The directory is created here to break
    # that circle; build.rs empties and refills it a moment later, so what
    # ends up linked into the executable is still build.rs's answer and there
    # is no second copy of the exclusion rules to keep in step.
    os.makedirs(UI_EMBED, exist_ok=True)

    print("\n== compiling and bundling the shell ==")
    argv = ["cargo", "tauri", "build"]
    argv += ["--no-bundle"] if unpacked_only else ["--bundles", "nsis"]
    run(argv, TAURI_DIR, "cargo tauri build", env=cargo_env())
    verify_built(RELEASE_DIR)

    if unpacked_only:
        return version

    # The bundler names its output Hearth_<version>_x64-setup.exe. Everything
    # downstream -- scripts/release_manifest.py, the update feed, the artifact
    # name agent/hearth_update.py will accept -- was written around
    # Hearth-Setup-<version>.exe, and a rename here is cheaper than a new
    # convention everywhere else.
    os.makedirs(DIST_DIR, exist_ok=True)
    produced = [n for n in os.listdir(BUNDLE_DIR) if n.lower().endswith(".exe")] \
        if os.path.isdir(BUNDLE_DIR) else []
    if not produced:
        raise SystemExit("the bundler produced no installer in {}".format(BUNDLE_DIR))
    newest = max(produced, key=lambda n: os.path.getmtime(os.path.join(BUNDLE_DIR, n)))
    target = os.path.join(DIST_DIR, "Hearth-Setup-{}.exe".format(version))
    shutil.copy2(os.path.join(BUNDLE_DIR, newest), target)
    print("  {} -> {}".format(newest, os.path.relpath(target, REPO_ROOT)))
    return version


def verify_built(release_dir):
    """Read the built application back and refuse a build that is not what
    was asked for.

    The successor to desktop/shell/verify-fuses.js. See scripts/verify_binary.py
    for what carries over from the seven Electron fuses, what does not, and
    why the one check with no Electron ancestor -- WebView2's environment --
    is the one that matters most here.
    """
    print("\n== verifying the built shell ==")
    try:
        notes = verify_binary.verify(release_dir)
    except verify_binary.Complaint as err:
        raise SystemExit("verify_binary: {}".format(err))
    for note in notes:
        print("  {}".format(note))


def report(info, unpacked_only=False):
    print("\n== size breakdown ==")
    print("  bundled: CPython {}, llama.cpp {}".format(
        info["python_version"], info["llama_variant"] or "(see the receipt)"))
    rows = [
        ("CPython (embeddable)", os.path.join(STAGE_DIR, "python")),
        ("llama.cpp engine", os.path.join(STAGE_DIR, "hearth", "vendor", "llama")),
        ("agent modules", os.path.join(STAGE_DIR, "hearth", "agent")),
        ("sidecar", os.path.join(STAGE_DIR, "hearth", "desktop", "server")),
        ("licence texts", os.path.join(STAGE_DIR, "hearth", "vendor", "licenses")),
    ]
    payload_total = sum(dir_size(p) for _, p in rows)
    for label, path in rows:
        print("  {:<28}{}".format(label, mb(dir_size(path))))
    print("  {:<28}{}".format("payload total", mb(payload_total)))

    exe = os.path.join(RELEASE_DIR, "hearth.exe")
    if os.path.isfile(exe):
        print("  {:<28}{}   (UI linked in; WebView2 is the OS's)".format(
            "shell executable", mb(os.path.getsize(exe))))
        print("  {:<28}{}".format(
            "installed, on disk", mb(payload_total + os.path.getsize(exe))))

    if unpacked_only:
        print("\ncompiled but not bundled. Executable in {}".format(
            os.path.relpath(RELEASE_DIR, REPO_ROOT)))
        return
    installers = sorted(
        (os.path.join(DIST_DIR, n) for n in os.listdir(DIST_DIR)
         if n.lower().endswith(".exe")),
        key=os.path.getmtime, reverse=True) if os.path.isdir(DIST_DIR) else []
    if installers:
        print("\n  {:<28}{}   {}".format(
            "INSTALLER", mb(os.path.getsize(installers[0])),
            os.path.relpath(installers[0], REPO_ROOT)))
    print("\nThe installer is unsigned. The first person to run it gets a "
          "SmartScreen warning\nthat hides the Run button behind \"More info\". "
          "See docs/packaging-windows.md.")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="build_windows.py",
        description="Build the Hearth Windows installer from a clean checkout.")
    p.add_argument("--offline", action="store_true",
                   help="use only already-downloaded archives; never fetch")
    p.add_argument("--dir", dest="unpacked_only", action="store_true",
                   help="compile the shell but do not bundle an installer")
    p.add_argument("--skip-build", action="store_true",
                   help="stage and verify the payload, then stop")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    started = time.monotonic()
    preflight()
    info = stage(offline=args.offline)
    if args.skip_build:
        print("\nstaged only, as asked. Payload in {}".format(
            os.path.relpath(STAGE_DIR, REPO_ROOT)))
        return 0
    build_installer(unpacked_only=args.unpacked_only)
    report(info, unpacked_only=args.unpacked_only)
    print("\ntotal build time {:.0f}s".format(time.monotonic() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
