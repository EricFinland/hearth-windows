#!/usr/bin/env python3
r"""hearth path resolution: where hearth keeps its data on each platform.

Linux prefers the historical /var/lib/hearth layout, the one the NixOS
daemon deployment runs as, but that directory is root-owned on a host set up
for the daemon. The desktop sidecar runs as the ordinary logged-in user and
cannot write there, so when /var/lib/hearth exists but this process cannot
actually write into it, we fall back to the conventional per-user location:
$XDG_DATA_HOME/hearth, or ~/.local/share/hearth when XDG_DATA_HOME is unset.

Windows uses %LOCALAPPDATA%\Hearth, which is already user-writable and needs
no fallback. Every location is overridable by environment variable so a test
or a redirected run never writes to the real data directory.

Standard library only. This module knows nothing about workspaces or security;
path containment lives in hearth_contain.
"""

import os
import shutil
import sys
import tempfile


MAX_COMFORTABLE_PATH = 255

# The daemon's historical system-wide data directory on Linux. A plain module
# constant (rather than a literal buried in data_dir()) so a test can point
# it at a scratch directory without touching the real filesystem path.
_LINUX_SYSTEM_DATA_DIR = "/var/lib/hearth"

# Per-process cache of writability probes, keyed by path, so a run with many
# db_path()/checkpoints_dir() calls only touches the filesystem once. Also
# tracks whether the one-line fallback notice has already been printed.
_writability_cache = {}
_fallback_notice_shown = False


def is_windows():
    return os.name == "nt"


def _probe_writable(path):
    """Return True if this process can actually create and write into `path`.

    Deliberately does the real operation instead of os.access(): a directory
    can exist with mode bits that look writable while still rejecting this
    process (a different owner, an ACL, a read-only bind mount), and that
    mismatch is exactly the shape of the bug this function exists to catch
    (a root-owned /var/lib/hearth that a non-root sidecar cannot use).
    os.access is documented as unsuitable for this: it does not always
    reflect the outcome of a real open()/mkdir() call. So we attempt the
    genuine operation -- create the directory if missing, write a small
    probe file into it, then remove the probe file -- and treat any OSError
    as "not usable". The result is cached per path for the life of the
    process.
    """
    if path in _writability_cache:
        return _writability_cache[path]
    result = False
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".hearth-write-probe-{}".format(os.getpid()))
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
        result = True
    except OSError:
        result = False
    _writability_cache[path] = result
    return result


def _linux_user_data_dir():
    """The XDG per-user fallback: $XDG_DATA_HOME/hearth, or ~/.local/share/hearth."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "hearth")


def data_dir():
    """The root of hearth's writable state."""
    env = os.environ.get("HEARTH_DATA_DIR")
    if env:
        return env
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "Hearth")
    if _probe_writable(_LINUX_SYSTEM_DATA_DIR):
        return _LINUX_SYSTEM_DATA_DIR
    fallback = _linux_user_data_dir()
    global _fallback_notice_shown
    if not _fallback_notice_shown:
        print(
            "[hearth-paths] {} is not writable by this process, using {} instead".format(
                _LINUX_SYSTEM_DATA_DIR, fallback
            ),
            file=sys.stderr,
        )
        _fallback_notice_shown = True
    return fallback


def db_path():
    """The audit database. HEARTH_DB overrides the derived location."""
    return os.environ.get("HEARTH_DB") or os.path.join(data_dir(), "runs", "audit.db")


def logs_dir():
    return os.path.join(data_dir(), "logs")


def checkpoints_dir():
    return os.path.join(data_dir(), "checkpoints")


def long_path(p):
    """Prefix a Windows path with \\\\?\\ so it survives the 260-character MAX_PATH
    limit. A no-op off Windows, on short paths, and on already-prefixed paths.

    Call this only AFTER containment has been validated. The prefix disables
    Windows path normalisation, so a prefixed path passed to a containment check
    would skip the very normalisation the check depends on.
    """
    if not is_windows() or not p or p.startswith("\\\\?\\"):
        return p
    ap = os.path.abspath(p)
    if len(ap) <= MAX_COMFORTABLE_PATH:
        return p
    if ap.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def _self_test():
    # Declared up front: Python requires `global` to precede any use (even a
    # read) of these names within this function, and both are read early
    # (the /var/lib/hearth-or-fallback assertion below) and reassigned later
    # (the monkeypatched fallback-wiring test).
    global _LINUX_SYSTEM_DATA_DIR, _fallback_notice_shown
    old = {k: os.environ.get(k) for k in ("HEARTH_DATA_DIR", "HEARTH_DB", "LOCALAPPDATA", "XDG_DATA_HOME")}
    try:
        os.environ["HEARTH_DATA_DIR"] = os.path.join("X:", "hearth-test") if is_windows() else "/tmp/hearth-test"
        os.environ.pop("HEARTH_DB", None)
        base = data_dir()
        assert db_path() == os.path.join(base, "runs", "audit.db"), db_path()
        assert logs_dir() == os.path.join(base, "logs")
        assert checkpoints_dir() == os.path.join(base, "checkpoints")

        # HEARTH_DB wins over the derived location.
        os.environ["HEARTH_DB"] = os.path.join(base, "custom.db")
        assert db_path() == os.path.join(base, "custom.db")
        os.environ.pop("HEARTH_DB", None)

        # No environment override: the platform default applies. On Linux
        # that is /var/lib/hearth when this process can actually write it,
        # and the XDG per-user fallback otherwise -- either is a correct
        # "platform default" outcome, so accept both rather than assuming
        # this process owns /var/lib/hearth on whatever host runs the test.
        os.environ.pop("HEARTH_DATA_DIR", None)
        if is_windows():
            assert data_dir().endswith("Hearth"), data_dir()
        else:
            result = data_dir()
            assert result == _LINUX_SYSTEM_DATA_DIR or result == _linux_user_data_dir(), result

        # long_path is a no-op for short paths everywhere.
        short = os.path.join(base, "a.txt")
        assert long_path(short) == short
        # ... and idempotent on an already-prefixed path.
        assert long_path("\\\\?\\C:\\x") == "\\\\?\\C:\\x"
        assert long_path("") == ""

        if is_windows():
            deep = "C:\\" + "\\".join("d" * 20 for _ in range(20)) + "\\f.txt"
            assert len(deep) > 255
            assert long_path(deep) == "\\\\?\\" + os.path.abspath(deep)
            unc = "\\\\server\\share\\" + "\\".join("d" * 20 for _ in range(20))
            assert long_path(unc).startswith("\\\\?\\UNC\\"), long_path(unc)
        else:
            # No prefixing off Windows, however long the path.
            deep = "/" + "/".join("d" * 20 for _ in range(20))
            assert long_path(deep) == deep

        # --- _probe_writable: prove it against a directory that genuinely
        # EXISTS and genuinely cannot be written to. This is the crux of the
        # live bug (a root-owned /var/lib/hearth that the sidecar's user
        # cannot write into), so os.access-style "looks writable" is not
        # good enough -- the probe has to attempt the real thing.
        scratch = tempfile.mkdtemp(prefix="hearth-paths-selftest-")
        try:
            writable = os.path.join(scratch, "writable")
            _writability_cache.clear()
            assert _probe_writable(writable) is True, "a fresh, owned directory must probe writable"

            if is_windows():
                # A drive letter that does not exist: creating anything
                # under it fails regardless of the running user's privileges,
                # so this is honest on any account, including an admin one.
                unwritable = "Z:\\hearth-selftest-does-not-exist\\deeper"
            else:
                # An existing directory with its write bit stripped -- an
                # existing-but-unwritable directory, not a missing one,
                # which is what actually broke on the live host. (Running
                # as root defeats chmod; skip the strict check there since
                # root can write through any permission bits.)
                unwritable = os.path.join(scratch, "locked")
                os.makedirs(unwritable)
                os.chmod(unwritable, 0o500)
            is_root = (not is_windows()) and hasattr(os, "geteuid") and os.geteuid() == 0
            if not is_root:
                _writability_cache.clear()
                assert _probe_writable(unwritable) is False, "an unwritable existing directory must probe unwritable"

            # --- data_dir()'s Linux fallback wiring, exercised even when
            # this self-test itself runs on Windows, by forcing the Linux
            # branch and pointing the "system" directory at our scratch
            # fixtures instead of the real /var/lib/hearth.
            orig_system_dir = _LINUX_SYSTEM_DATA_DIR
            orig_is_windows = is_windows
            orig_notice_shown = _fallback_notice_shown
            g = globals()
            try:
                os.environ.pop("HEARTH_DATA_DIR", None)
                os.environ.pop("HEARTH_DB", None)
                g["is_windows"] = lambda: False  # force the Linux branch

                # System dir usable -> returned unchanged, untouched.
                g["_LINUX_SYSTEM_DATA_DIR"] = writable
                _writability_cache.clear()
                _fallback_notice_shown = False
                assert data_dir() == writable, data_dir()

                if not is_root:
                    # System dir exists but is not writable -> falls back to
                    # the XDG per-user location, and that fallback is itself
                    # genuinely writable.
                    g["_LINUX_SYSTEM_DATA_DIR"] = unwritable
                    _writability_cache.clear()
                    _fallback_notice_shown = False
                    xdg_scratch = os.path.join(scratch, "xdg")
                    os.environ["XDG_DATA_HOME"] = xdg_scratch
                    fell_back = data_dir()
                    assert fell_back == os.path.join(xdg_scratch, "hearth"), fell_back
                    _writability_cache.clear()
                    assert _probe_writable(fell_back) is True, fell_back
                    os.environ.pop("XDG_DATA_HOME", None)

                    # The stderr notice fires exactly once per process, not
                    # once per call.
                    assert _fallback_notice_shown is True
                    _writability_cache.clear()
                    data_dir()  # second call while still unwritable
                    assert _fallback_notice_shown is True

                    # HEARTH_DATA_DIR still overrides even while the system
                    # dir is (simulated) unwritable.
                    override = os.path.join(scratch, "explicit-override")
                    os.environ["HEARTH_DATA_DIR"] = override
                    assert data_dir() == override, data_dir()
                    os.environ.pop("HEARTH_DATA_DIR", None)
            finally:
                g["is_windows"] = orig_is_windows
                g["_LINUX_SYSTEM_DATA_DIR"] = orig_system_dir
                _fallback_notice_shown = orig_notice_shown
                _writability_cache.clear()
        finally:
            if not is_windows():
                try:
                    os.chmod(unwritable, 0o700)
                except OSError:
                    pass
            shutil.rmtree(scratch, ignore_errors=True)
            _writability_cache.clear()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("hearth-paths self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
