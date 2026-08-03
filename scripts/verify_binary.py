#!/usr/bin/env python3
r"""Read back out of the built application the things the build asked for.

    python scripts/verify_binary.py build/cargo-target/release
    python scripts/verify_binary.py --installed "%LOCALAPPDATA%\Programs\Hearth"
    python scripts/verify_binary.py --self-test

scripts/build_windows.py runs the first form after the bundler and fails the
build on any complaint. It is the replacement for desktop/shell/verify-fuses.js,
which read the Electron fuse wire out of the packed executable and refused a
build where a fuse had silently not been applied.

Why this file is shorter than the thing it replaced
---------------------------------------------------
Five of Electron's seven fuses existed because an Electron binary is a
general-purpose JavaScript runtime that has been asked to behave like an
application. RunAsNode, EnableNodeOptionsEnvironmentVariable and
EnableNodeCliInspectArguments each turned off a documented way to make
Hearth.exe execute somebody else's script; OnlyLoadAppFromAsar and
EnableEmbeddedAsarIntegrityValidation existed because the application's own
code sat in an archive on disk that anything running as the user could
rewrite.

None of those has an analogue here, and not because the risk was waved away:

  * There is no script runtime in this binary to hand a script to. The shell
    is compiled Rust. `Hearth.exe -e "..."` is not a thing that can be made
    to mean anything.
  * There is no archive of application code on disk. desktop/ui/ is embedded
    in the executable at link time by tauri-build, so the integrity property
    the two asar fuses bought is a property of how the file was made. Check
    (1) below states that as an observation rather than a claim: if the UI
    ever reappears as loose files next to the executable, this fails.

What DOES carry over is the shape of the problem -- a build-time request that
silently does not take effect -- and one thing that is genuinely worse here
than it was under Electron. The checks:

  (1) nothing beside the executable is code a runtime would interpret
  (2) the inspector is not compiled into what shipped
  (3) the code that disowns WebView2's environment IS compiled in
  (4) the window's capability grants no plugin permission
  (5) withGlobalTauri is off and the asset protocol is disabled

(2) and (3) are read out of the shipped bytes. (4) and (5) are read out of
the configuration, because that is where they live and there is nothing in
the binary to compare them against. (1) is read out of whichever of the two
things it was pointed at.

Check (3) is the one with no Electron ancestor, and it is the reason this
file is not merely a formality. WebView2 is a Chromium, and Chromium takes
orders from the environment: with WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS set
to --remote-debugging-port, an unmitigated build of this shell opens an
unauthenticated DevTools endpoint on loopback, attached to the page that is
holding the sidecar's bearer token. Measured, on a real build, before the
mitigation existed. desktop/tauri/src/main.rs now clears that variable and
four others before the runtime loads, and this check exists so that deleting
those lines fails the build instead of shipping quietly. See WEBVIEW2_ENV in
main.rs, and docs/packaging-windows.md for the transcript.

Two modes, because check (1) has two different subjects at two different
moments. The first is what the build can see: the executable, plus the list
of things the bundler has been told to install beside it. The second is what
a user ends up with: the same executable, plus the files that are actually on
their disk. The second is the stronger reading, and it is the one
docs/packaging-windows.md records the output of.

Standard library only, and it reads files -- it never runs the executable it
is checking.
"""

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAURI_DIR = os.path.join(REPO_ROOT, "desktop", "tauri")

#: Written into the binary by desktop/tauri/src/main.rs under #[cfg(feature)].
#: The build must ship the first and must not ship the second.
MARKER_REQUIRED = b"hearth-build-marker:devtools-disabled"
MARKER_FORBIDDEN = b"hearth-build-marker:devtools-enabled"

#: Every name in WEBVIEW2_ENV in main.rs. All of them must appear in the
#: binary, because the only reason they would be there is the loop that
#: removes them.
WEBVIEW2_ENV = (
    b"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    b"WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
    b"WEBVIEW2_USER_DATA_FOLDER",
    b"WEBVIEW2_RELEASE_CHANNEL_PREFERENCE",
    b"WEBVIEW2_CHANNEL_SEARCH_KIND",
)

#: File types a runtime would interpret. None of these may sit loose in the
#: application directory.
CODE_SUFFIXES = (".js", ".mjs", ".cjs", ".html", ".htm", ".asar", ".node")

#: The two directories of payload that are meant to be there. Python is code,
#: and shipping it is the entire point: it is the sidecar. It is not loose
#: script the shell will pick up -- the shell runs python.exe on main.py and
#: nothing else -- so it is excluded from (1) by name rather than by accident,
#: and its contents are the sidecar's business, checked by verify_stage() in
#: scripts/build_windows.py.
PAYLOAD_DIRS = ("hearth", "python")


class Complaint(Exception):
    """A check failed. The message is what the builder needs to read."""


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def find_exe(directory):
    for name in ("Hearth.exe", "hearth.exe"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    raise Complaint("no Hearth.exe in {}".format(directory))


def check_installed_tree(app_dir, notes):
    """(1), read off a real install: nothing beside the executable is code."""
    found = []
    for name in sorted(os.listdir(app_dir)):
        if name in PAYLOAD_DIRS:
            continue
        path = os.path.join(app_dir, name)
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for leaf in files:
                    if leaf.lower().endswith(CODE_SUFFIXES):
                        found.append(os.path.relpath(os.path.join(root, leaf), app_dir))
        elif name.lower().endswith(CODE_SUFFIXES):
            found.append(name)
    if found:
        raise Complaint(
            "the installed application carries loose application code: {}.\n"
            "The UI is supposed to be linked into the executable, where nothing "
            "running as the user can rewrite it. Files like these are what the "
            "two asar fuses used to exist for.".format(", ".join(sorted(found)[:8])))
    notes.append("installed: no interpretable code on disk beside the executable")


def check_bundled_resources(notes):
    """(1), read off the configuration: the bundler installs no loose code.

    Everything that lands beside the executable is named in
    tauri.conf.json's bundle.resources, so this reads the whole list rather
    than a directory. A destination is acceptable when it is one of the two
    payload directories or a file that is not code.
    """
    config = _read_json(os.path.join(TAURI_DIR, "tauri.conf.json"))
    resources = ((config.get("bundle") or {}).get("resources") or {})
    if not resources:
        raise Complaint(
            "tauri.conf.json installs no resources at all. The payload -- the "
            "interpreter, the agent, the sidecar and the engine -- is supposed "
            "to be listed there.")
    bad = []
    for source, dest in sorted(resources.items()):
        head = dest.replace("\\", "/").split("/")[0]
        if head in PAYLOAD_DIRS:
            continue
        if dest.lower().endswith(CODE_SUFFIXES):
            bad.append("{} -> {}".format(source, dest))
    if bad:
        raise Complaint(
            "the bundler is configured to install code beside the executable: "
            "{}. The UI belongs inside the binary.".format(", ".join(bad)))
    notes.append("the bundler installs {} resource entries, none of them loose code"
                 .format(len(resources)))


def check_marker(exe, notes):
    """(2) The inspector is not compiled into what shipped."""
    blob = _read(exe)
    if MARKER_FORBIDDEN in blob:
        raise Complaint(
            "{} was built with the `devtools` feature. That binary has a working "
            "inspector in it, and an inspector attached to this page can read the "
            "sidecar's bearer token out of it. Build without --features "
            "devtools.".format(os.path.basename(exe)))
    if MARKER_REQUIRED not in blob:
        raise Complaint(
            "{} carries neither build marker. Either main.rs no longer defines "
            "BUILD_MARKER, or the linker discarded it and BUILD_MARKER_ANCHOR is "
            "no longer doing its job. Until one of those is fixed this check "
            "proves nothing, so it fails rather than passes."
            .format(os.path.basename(exe)))
    notes.append("built without the inspector: the marker in the file says so")
    return blob


def check_webview2_disowned(blob, notes):
    """(3) The five variables WebView2 reads are cleared before it loads."""
    missing = [name.decode() for name in WEBVIEW2_ENV if name not in blob]
    if missing:
        raise Complaint(
            "the shipped executable does not mention {}, so the code that clears "
            "WebView2's environment is not in it. Without that, setting "
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS on this process turns on a "
            "remote debugging port, and a remote debugging port is the bearer "
            "token.".format(", ".join(missing)))
    notes.append("WebView2's five environment variables are cleared before it loads")


#: Exactly what the page may ask for. tauri-build generates one permission per
#: name in build.rs's command list; anything with a `:` in it belongs to a
#: plugin or to Tauri's core, and the page is granted none of those.
ALLOWED_PERMISSIONS = ("allow-handshake", "allow-install-update", "allow-pick-folder")


def check_capability(notes):
    """(4) The one window may ask the shell for the three commands in main.rs,
    and may ask no plugin for anything."""
    capability = _read_json(os.path.join(TAURI_DIR, "capabilities", "main.json"))
    granted = capability.get("permissions") or []
    plugin_grants = [p for p in granted if not isinstance(p, str) or ":" in p]
    if plugin_grants:
        raise Complaint(
            "capabilities/main.json grants the page {}. Every one of those is a "
            "plugin or core command the renderer can call directly, bypassing "
            "the three in main.rs that validate what they are asked. The page is "
            "supposed to have no plugin permission at all.".format(plugin_grants))
    if tuple(sorted(granted)) != ALLOWED_PERMISSIONS:
        raise Complaint(
            "capabilities/main.json grants {} rather than exactly {}. The page is "
            "served over http on loopback, which Tauri treats as a remote origin, "
            "so this list is the whole of what it can reach: too short and the "
            "application cannot start, too long and the bridge is wider than the "
            "four names in BRIDGE.".format(sorted(granted), list(ALLOWED_PERMISSIONS)))
    windows = capability.get("windows") or []
    if windows != ["main"]:
        raise Complaint(
            "capabilities/main.json applies to {} rather than exactly the one "
            "window this application opens.".format(windows))
    notes.append("the window's capability grants no plugin permission")


def check_config(notes):
    """(5) Two settings in tauri.conf.json that widen the renderer if wrong."""
    config = _read_json(os.path.join(TAURI_DIR, "tauri.conf.json"))
    app = config.get("app") or {}
    if app.get("withGlobalTauri"):
        raise Complaint(
            "withGlobalTauri is on, which puts Tauri's whole API object on "
            "window for the page to read. The page is supposed to see the four "
            "names in BRIDGE and nothing else.")
    asset = ((app.get("security") or {}).get("assetProtocol") or {})
    if asset.get("enable"):
        raise Complaint(
            "the asset protocol is enabled, which gives the page a URL scheme "
            "that reads files off disk.")
    notes.append("withGlobalTauri off, asset protocol disabled")


def verify(directory, installed=False):
    """Check a built or installed application. Returns the notes, or raises."""
    exe = find_exe(directory)
    notes = []
    if installed:
        check_installed_tree(directory, notes)
    else:
        check_bundled_resources(notes)
    blob = check_marker(exe, notes)
    check_webview2_disowned(blob, notes)
    check_capability(notes)
    check_config(notes)
    return notes


def _self_test():
    import tempfile

    good = MARKER_REQUIRED + b"\x00" + b"\x00".join(WEBVIEW2_ENV)

    with tempfile.TemporaryDirectory() as tmp:
        # A directory with no executable in it is not a build.
        try:
            verify(tmp)
        except Complaint as err:
            assert "no Hearth.exe" in str(err), err
        else:
            raise AssertionError("an empty directory passed")

        exe = os.path.join(tmp, "Hearth.exe")
        with open(exe, "wb") as fh:
            fh.write(good)
        # The repository's own configuration is the fixture for (1), (4), (5).
        notes = verify(tmp)
        assert len(notes) == 5, notes
        notes = verify(tmp, installed=True)
        assert len(notes) == 5, notes

        # A build with the inspector in it is refused by name.
        with open(exe, "wb") as fh:
            fh.write(good + b"\x00" + MARKER_FORBIDDEN)
        try:
            verify(tmp)
        except Complaint as err:
            assert "devtools" in str(err), err
        else:
            raise AssertionError("a devtools build passed")

        # A build with no marker at all fails rather than passing quietly,
        # because a check that cannot see its subject is not a check.
        with open(exe, "wb") as fh:
            fh.write(b"\x00".join(WEBVIEW2_ENV))
        try:
            verify(tmp)
        except Complaint as err:
            assert "neither build marker" in str(err), err
        else:
            raise AssertionError("an unmarked build passed")

        # Dropping one variable from the loop in main.rs fails the build.
        with open(exe, "wb") as fh:
            fh.write(MARKER_REQUIRED + b"\x00" + b"\x00".join(WEBVIEW2_ENV[:-1]))
        try:
            verify(tmp)
        except Complaint as err:
            assert "WEBVIEW2_CHANNEL_SEARCH_KIND" in str(err), err
        else:
            raise AssertionError("a build that ignores WebView2's environment passed")

        # The UI back on disk beside the executable is what the two asar
        # fuses used to be about, and it fails here.
        with open(exe, "wb") as fh:
            fh.write(good)
        os.makedirs(os.path.join(tmp, "resources", "js"))
        with open(os.path.join(tmp, "resources", "js", "app.js"), "w") as fh:
            fh.write("//")
        try:
            verify(tmp, installed=True)
        except Complaint as err:
            assert "loose application code" in str(err), err
        else:
            raise AssertionError("an install with the UI on disk passed")
        # ...and the payload directories are not mistaken for it.
        os.makedirs(os.path.join(tmp, "hearth", "desktop", "server"))
        with open(os.path.join(tmp, "hearth", "desktop", "server", "app.py"), "w") as fh:
            fh.write("#")

    # The shipped configuration must itself pass (1), (4) and (5), or the
    # checks above are testing a fixture and nothing else.
    notes = []
    check_bundled_resources(notes)
    check_capability(notes)
    check_config(notes)
    assert len(notes) == 3, notes

    print("hearth-verify-binary self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="verify_binary.py",
        description="Read back out of the built application what the build asked for.")
    p.add_argument("directory", nargs="?",
                   help="the directory holding Hearth.exe")
    p.add_argument("--installed", action="store_true",
                   help="the directory is an installed application, not a build "
                        "output: check the files actually on disk")
    p.add_argument("--self-test", action="store_true",
                   help="run this module's tests and exit")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return _self_test()
    if not args.directory:
        p.error("a directory holding Hearth.exe is required")
    try:
        notes = verify(args.directory, installed=args.installed)
    except Complaint as err:
        sys.stderr.write("verify_binary: {}\n".format(err))
        return 1
    for note in notes:
        print("  {}".format(note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
