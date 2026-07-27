#!/usr/bin/env python3
r"""hearth path resolution: where hearth keeps its data on each platform.

Linux keeps the historical /var/lib/hearth layout. Windows uses
%LOCALAPPDATA%\Hearth. Every location is overridable by environment variable so
a test or a redirected run never writes to the real data directory.

Standard library only. This module knows nothing about workspaces or security;
path containment lives in hearth_contain.
"""

import os
import sys


MAX_COMFORTABLE_PATH = 255


def is_windows():
    return os.name == "nt"


def data_dir():
    """The root of hearth's writable state."""
    env = os.environ.get("HEARTH_DATA_DIR")
    if env:
        return env
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "Hearth")
    return "/var/lib/hearth"


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
    old = {k: os.environ.get(k) for k in ("HEARTH_DATA_DIR", "HEARTH_DB", "LOCALAPPDATA")}
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

        # No environment override: the platform default applies.
        os.environ.pop("HEARTH_DATA_DIR", None)
        if is_windows():
            assert data_dir().endswith("Hearth"), data_dir()
        else:
            assert data_dir() == "/var/lib/hearth", data_dir()

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
