#!/usr/bin/env python3
r"""Build the Hearth Windows installer from a clean checkout.

    python scripts/build_windows.py

That is the whole command. It fetches and verifies everything the installer
needs, stages the payload, installs the packaging toolchain, runs
electron-builder, and prints a size breakdown. From a fresh clone on a
machine with Node and Python it needs nothing else.

What it needs on the build machine
----------------------------------
    Node.js and npm      only to run electron-builder and to be the shell
    Python 3.11+         to run this script and the two vendor scripts
    network              first run only, to fetch llama-server, CPython and
                         the Electron runtime; --offline afterwards

Nothing on the TARGET machine. That is the point of the exercise: the
installer carries its own Python, its own inference engine and its own
browser runtime.

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
  3. stage the payload     build/stage/, laid out so that
                           agent/hearth_llama.app_root() finds
                           vendor/llama/llama-server.exe with no
                           packaging-specific code in the agent.
  4. npm install           the packaging layer, and only the packaging
                           layer: electron and electron-builder. desktop/ui/
                           has no dependencies and no build step, and step 3
                           copies it verbatim.
  5. electron-builder      produces build/dist/Hearth-Setup-<version>.exe

Nothing under build/ is committed, and neither is anything under vendor/
except the two manifests.

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

BUILD_DIR = os.path.join(REPO_ROOT, "build")
STAGE_DIR = os.path.join(BUILD_DIR, "stage")
DIST_DIR = os.path.join(BUILD_DIR, "dist")
SHELL_DIR = os.path.join(REPO_ROOT, "desktop", "shell")

#: Copied into the payload verbatim. Source path relative to the repo root,
#: destination relative to build/stage/hearth.
PAYLOAD_TREES = (
    ("agent", "agent"),
    (os.path.join("desktop", "server"), os.path.join("desktop", "server")),
    (os.path.join("desktop", "ui"), "ui"),
)

#: Never staged. __pycache__ is build residue from whatever interpreter last
#: ran here and would ship stale bytecode compiled by a different Python.
#: dev-host.mjs and the xss-check pages are development tools: the shell
#: replaces the first and the second is a test harness, and neither belongs
#: in an installed application.
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


def run(argv, cwd, label):
    """Run a build command, streaming its output. Raises on failure."""
    print("\n$ {}  (in {})".format(" ".join(argv), os.path.relpath(cwd, REPO_ROOT) or "."))
    started = time.monotonic()
    # shell=True on Windows so npm/npx resolve through their .cmd shims.
    completed = subprocess.run(argv, cwd=cwd, shell=(os.name == "nt"))
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
    node = tool_version(["node", "--version"])
    npm = tool_version(["npm", "--version"])
    missing = []
    if not node:
        missing.append("node")
    if not npm:
        missing.append("npm")
    if missing:
        raise SystemExit(
            "missing build tools: {}\nInstall Node.js (which brings npm) and try "
            "again. Nothing else is needed; the installer ships its own "
            "Python.".format(", ".join(missing)))
    print("node {}   npm {}   python {}".format(node, npm, sys.version.split()[0]))
    if os.name != "nt":
        print("warning: this produces a Windows installer and has only been run on "
              "Windows. electron-builder can cross-build, but the vendored binaries "
              "are Windows x64 and nothing here is tested off Windows.")


def stage(offline=False):
    """Fetch, verify and lay out everything the installer will carry."""
    print("\n== vendoring llama-server ==")
    llama = vendor_llama.vendor(offline=offline, log=lambda m: print("  " + m))
    print("\n== vendoring CPython ==")
    python = vendor_python.vendor(offline=offline, log=lambda m: print("  " + m))

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
    }


def verify_stage():
    """Prove the staged payload works before wrapping it in an installer.

    Two checks, each covering a failure that would otherwise only appear on
    a user's machine, at launch, as a dialog with a traceback in it.
    """
    exe = os.path.join(STAGE_DIR, "python", "python.exe")
    server_dir = os.path.join(STAGE_DIR, "hearth", "desktop", "server")
    agent_dir = os.path.join(STAGE_DIR, "hearth", "agent")
    # PYTHONDONTWRITEBYTECODE so these checks do not leave __pycache__
    # directories in the tree they are checking, which would then be packaged.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

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
    print("  staged payload verified: sidecar self-test green under python {}, "
          "bundled engine found".format(result["python"]))


def build_installer(unpacked_only=False):
    print("\n== installing the packaging toolchain ==")
    lock = os.path.join(SHELL_DIR, "package-lock.json")
    install = ["npm", "ci"] if os.path.isfile(lock) else ["npm", "install"]
    run(install + ["--no-audit", "--no-fund"], SHELL_DIR, "npm install")

    # electron's own binary arrives through a postinstall script, and a npm
    # configured with ignore-scripts, or a postinstall that was skipped
    # because the package was already in the cache, leaves the package
    # present and the runtime absent. Observed on this machine. Checking is
    # cheaper than the confusing electron-builder failure it otherwise
    # causes an hour later.
    runtime = os.path.join(SHELL_DIR, "node_modules", "electron", "dist", "electron.exe")
    if not os.path.isfile(runtime):
        print("  electron's runtime is missing; fetching it")
        run(["node", os.path.join("node_modules", "electron", "install.js")],
            SHELL_DIR, "electron runtime download")
        if not os.path.isfile(runtime):
            raise SystemExit("electron's runtime is still missing at {}".format(runtime))

    print("\n== packaging ==")
    run(["npm", "run", "pack" if unpacked_only else "dist"], SHELL_DIR, "electron-builder")
    verify_fuses()


def verify_fuses():
    """Read the Electron fuse wire back out of the packed executable.

    The fuses are requested in desktop/shell/package.json and applied by
    electron-builder deep inside packaging, by rewriting a sentinel in the
    executable after the runtime is copied. A silent no-op there -- a
    renamed option, a skipped step -- produces a build that looks entirely
    normal and ships an executable that runs arbitrary JavaScript out of an
    ELECTRON_RUN_AS_NODE environment variable. See desktop/shell/verify-fuses.js
    for why that must never be signed. Checking here is the only place the
    result of the request can actually be observed.
    """
    print("\n== verifying electron fuses ==")
    exe = os.path.join(DIST_DIR, "win-unpacked", "Hearth.exe")
    if not os.path.isfile(exe):
        raise SystemExit("expected a packed executable at {}".format(exe))
    run(["node", "verify-fuses.js", exe], SHELL_DIR, "fuse verification")


def report(info, unpacked_only=False):
    print("\n== size breakdown ==")
    print("  bundled: CPython {}, llama.cpp {}".format(
        info["python_version"], info["llama_variant"] or "(see the receipt)"))
    rows = [
        ("CPython (embeddable)", os.path.join(STAGE_DIR, "python")),
        ("llama.cpp engine", os.path.join(STAGE_DIR, "hearth", "vendor", "llama")),
        ("agent modules", os.path.join(STAGE_DIR, "hearth", "agent")),
        ("sidecar", os.path.join(STAGE_DIR, "hearth", "desktop", "server")),
        ("user interface", os.path.join(STAGE_DIR, "hearth", "ui")),
    ]
    unpacked = os.path.join(DIST_DIR, "win-unpacked")
    payload_total = sum(dir_size(p) for _, p in rows)
    for label, path in rows:
        print("  {:<28}{}".format(label, mb(dir_size(path))))
    print("  {:<28}{}".format("payload total", mb(payload_total)))
    if os.path.isdir(unpacked):
        total = dir_size(unpacked)
        print("  {:<28}{}   (Chromium, Node, V8)".format(
            "Electron runtime", mb(max(0, total - payload_total))))
        print("  {:<28}{}".format("installed, on disk", mb(total)))

    if unpacked_only:
        print("\nunpacked build in {}".format(os.path.relpath(unpacked, REPO_ROOT)))
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
                   help="produce the unpacked app but not the installer")
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
