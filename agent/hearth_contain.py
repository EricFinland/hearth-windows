#!/usr/bin/env python3
"""hearth workspace containment: the single place that decides whether a path is
inside the run's workspace.

This is a security boundary. On NixOS it was defence in depth on top of the
systemd sandbox and the nftables egress wall. On Windows there is no such
backstop, so this module is the boundary rather than a second layer behind one.

Three Windows-specific holes are closed here that a POSIX-shaped check misses:
reserved device names (NUL, CON, COM1) which swallow writes silently, alternate
data streams (a.txt:hidden) which are invisible to directory listings and to
git, and reparse points (junctions) which os.path.islink does not detect.

Standard library only.
"""

import os
import sys

import hearth_paths


SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", "result", ".direnv", ".mypy_cache", ".venv"})

_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM{}".format(i) for i in range(1, 10)}
    | {"LPT{}".format(i) for i in range(1, 10)}
)

_ILLEGAL = set('<>"|?*')


def _components(rel):
    """Split a workspace-relative path into components on both separator styles.

    Backslash is a legal filename character on POSIX, so it is only treated as a
    separator on Windows. A model trained on Unix will emit forward slashes and a
    model prompted for Windows will emit backslashes; both must be understood on
    Windows, and only forward slash on Linux.
    """
    if hearth_paths.is_windows():
        return [c for c in rel.replace("\\", "/").split("/") if c]
    return [c for c in rel.split("/") if c]


def _reject(comp):
    """Return a reason string if this path component is unacceptable, else None."""
    stem = comp.split(".")[0].upper()
    if stem in _RESERVED:
        return "reserved device name ({})".format(comp)
    if ":" in comp:
        return "alternate data stream ({})".format(comp)
    if comp != comp.rstrip(" ."):
        return "trailing dot or space ({!r})".format(comp)
    for ch in comp:
        if ch in _ILLEGAL:
            return "illegal character {!r}".format(ch)
        if ord(ch) < 32:
            return "control character in name"
    return None


def _within(full, root):
    """True if `full` is `root` or lives underneath it.

    Uses commonpath rather than a startswith test on root + os.sep. At a drive
    root that concatenation produces "C:\\\\", which matches nothing and fails
    closed, and startswith would also accept a sibling like /workspace-evil.
    """
    f = os.path.normcase(full)
    r = os.path.normcase(root)
    if f == r:
        return True
    try:
        return os.path.commonpath([f, r]) == r
    except ValueError:
        return False  # different drives, or mixed absolute and relative


def safe_join(root, path):
    """Resolve `path` inside `root`, raising ValueError on any escape.

    The single containment check for every file tool. Returns a real, absolute
    path with symlinks and 8.3 short names resolved.
    """
    rel = path or ""
    rel = rel.lstrip("/\\") if hearth_paths.is_windows() else rel.lstrip("/")
    for comp in _components(rel):
        if comp in (".", ".."):
            continue  # traversal is caught by the containment check below
        reason = _reject(comp)
        if reason:
            raise ValueError("refusing path: {}: {}".format(reason, path))
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_real, rel))
    if not _within(full, root_real):
        raise ValueError("path escapes workspace: {}".format(path))
    return full


def is_reparse(path):
    """True for symlinks and for NTFS junctions.

    os.path.islink returns False for a junction, which is why os.walk descends
    into one. os.path.isjunction exists in Python 3.12 and later.
    """
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction and isjunction(path))
    except OSError:
        return True  # unreadable: treat as unsafe and skip


def prune(dirpath, dirs, skip_names=()):
    """Filter a walk's directory list in place. Drops skip-listed names
    (case-insensitively, because NTFS is case-insensitive and ".GIT" is ".git")
    and every reparse point.

    Must be called in every os.walk loop that touches workspace content.
    """
    skip = {s.lower() for s in skip_names}
    dirs[:] = [
        d for d in dirs
        if d.lower() not in skip and not is_reparse(os.path.join(dirpath, d))
    ]
    return dirs


def _self_test():
    import shutil
    import tempfile

    root = os.path.realpath(tempfile.mkdtemp(prefix="hearth-contain-"))
    try:
        os.makedirs(os.path.join(root, "sub", "deep"))
        with open(os.path.join(root, "sub", "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")

        # Ordinary paths resolve inside the root.
        assert safe_join(root, "sub/a.txt") == os.path.realpath(os.path.join(root, "sub", "a.txt"))
        assert safe_join(root, ".") == root
        assert safe_join(root, "") == root
        # A leading slash is treated as workspace-relative, not filesystem-absolute.
        assert safe_join(root, "/sub/a.txt") == os.path.realpath(os.path.join(root, "sub", "a.txt"))

        # Traversal is refused.
        for bad in ("../evil.txt", "../../evil.txt", "sub/../../evil.txt", "sub/deep/../../../evil.txt"):
            try:
                safe_join(root, bad)
                raise AssertionError("traversal not caught: {}".format(bad))
            except ValueError:
                pass

        # Absolute paths outside the root are refused.
        outside = "C:\\Windows\\win.ini" if hearth_paths.is_windows() else "/etc/passwd"
        try:
            safe_join(root, outside)
            raise AssertionError("absolute escape not caught")
        except ValueError:
            pass

        # Reserved device names are refused, with or without an extension.
        for bad in ("NUL", "nul", "CON", "com1", "LPT9", "NUL.txt", "sub/NUL.tar.gz", "aux.log"):
            try:
                safe_join(root, bad)
                raise AssertionError("reserved name not caught: {}".format(bad))
            except ValueError:
                pass
        # A name that merely contains a reserved word is fine.
        assert safe_join(root, "console.txt")
        assert safe_join(root, "sub/nullable.py")

        # Alternate data streams are refused.
        for bad in ("a.txt:hidden", "sub/a.txt:$DATA"):
            try:
                safe_join(root, bad)
                raise AssertionError("stream not caught: {}".format(bad))
            except ValueError:
                pass

        # Trailing dots and spaces are refused (Windows silently strips them,
        # so "a.txt " and "a.txt" would collide).
        for bad in ("a.txt ", "a.txt.", "sub /x"):
            try:
                safe_join(root, bad)
                raise AssertionError("trailing whitespace not caught: {}".format(bad))
            except ValueError:
                pass

        # Wildcards and control characters are refused.
        for bad in ("a*.txt", "a?.txt", 'a".txt', "a|b", "a<b", "a>b", "a\x01b"):
            try:
                safe_join(root, bad)
                raise AssertionError("illegal char not caught: {!r}".format(bad))
            except ValueError:
                pass

        # A workspace at a drive root must not fail closed.
        # (root + os.sep produces "C:\\\\" and matches nothing; commonpath does not.)
        assert _within(os.path.join("C:", os.sep, "x"), "C:" + os.sep) or not hearth_paths.is_windows()

        # is_reparse: a plain directory is not a reparse point.
        assert not is_reparse(os.path.join(root, "sub"))

        # prune drops skip-listed directories case-insensitively.
        dirs = ["src", ".git", ".GIT", "Node_Modules", "node_modules", "keep"]
        prune(root, dirs, SKIP_DIRS)
        assert dirs == ["src", "keep"], dirs

        # prune drops reparse points. Junctions need no admin rights on Windows,
        # which is exactly why the walking tools must prune them.
        if hearth_paths.is_windows():
            target = os.path.realpath(tempfile.mkdtemp(prefix="hearth-outside-"))
            try:
                link = os.path.join(root, "escape")
                rc = os.system('mklink /J "{}" "{}" >nul 2>&1'.format(link, target))
                if rc == 0:
                    assert is_reparse(link), "junction not detected"
                    d2 = ["escape", "sub"]
                    prune(root, d2, SKIP_DIRS)
                    assert d2 == ["sub"], d2
            finally:
                shutil.rmtree(target, ignore_errors=True)
        else:
            link = os.path.join(root, "escape")
            os.symlink("/tmp", link)
            assert is_reparse(link)
            d2 = ["escape", "sub"]
            prune(root, d2, SKIP_DIRS)
            assert d2 == ["sub"], d2
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("hearth-contain self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
