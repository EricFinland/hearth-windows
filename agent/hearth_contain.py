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

A leading path separator is treated as workspace-root-relative, not as a
filesystem-absolute escape: "/etc/passwd" resolves to "<root>/etc/passwd",
never to the real /etc/passwd. This is deliberate, not an oversight. It
matches the original _safe_join, which every existing hearth agent already
depends on, and containment is still enforced after the strip: a path that
climbs back out with ".." from that root-relative position is still refused.
On Windows a drive-qualified path such as "C:\\Windows\\win.ini" is not
touched by this stripping (it has no leading separator) and is refused on its
own terms.

Standard library only.
"""

import os
import re
import sys

import hearth_paths


# The one authoritative skip-directory list. hearth_tools and hearth_project
# used to each carry their own divergent copy (one missing .venv entirely),
# which meant replace_in_files could walk into .venv and rewrite
# site-packages -- content the checkpoint store never captures (see
# hearth_checkpoint's _BUILTIN_EXCLUDES), so that damage was not undoable.
# Every walking tool now prunes with this set (hearth_checkpoint.sub_repos
# already did); a caller that genuinely needs extra entries unions them on
# top of this base rather than redefining it (see hearth_project.SKIP_DIRS).
SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", "result", ".direnv", ".mypy_cache", ".venv"})

_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM{}".format(i) for i in range(1, 10)}
    | {"LPT{}".format(i) for i in range(1, 10)}
)

_ILLEGAL = set('<>"|?*')


# --------------------------------------------------------------------------
# .hearthignore: gitignore-flavoured patterns that additionally scope a run's
# workspace, on top of safe_join. This is a NARROWING-ONLY filter, never a
# second way to resolve a path. Every function below only ever classifies a
# path that safe_join (or an os.walk rooted at one) has already produced --
# there is no code path here from a pattern string to a filesystem location,
# only from an already-resolved, already-contained location to a yes/no
# visibility bit. That is what makes a hostile pattern -- a leading '!', a
# '..', a drive letter -- structurally unable to widen access: it has
# nothing to widen. See is_ignored's docstring for the precise guarantee,
# and the module self-test for a hostile-file regression proof.
# --------------------------------------------------------------------------

class _Rule:
    """One compiled .hearthignore pattern."""
    __slots__ = ("regex", "negate", "dir_only")

    def __init__(self, regex, negate, dir_only):
        self.regex = regex
        self.negate = negate
        self.dir_only = dir_only


def _segment_to_regex(seg):
    """Translate one path segment (no '/' in it) into a regex fragment.

    Supports '*' (any run of non-separator characters), '?' (exactly one),
    and '[seq]' / '[!seq]' character classes; everything else is literal.
    Hand-rolled rather than built on fnmatch.translate, whose exact wrapper
    format is not part of its documented contract and has changed across
    Python versions; the matching semantics here are the familiar ones.
    """
    out = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and seg[j] in ("!", "^"):
                j += 1
            if j < n and seg[j] == "]":
                j += 1
            while j < n and seg[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))  # unterminated '[' -> literal
                i += 1
            else:
                inner = seg[i + 1:j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


# Every interior '**' (strictly between the first and last segment)
# compiles to its own "/(?:.*/)?" alternation -- an unbounded, unanchored
# quantifier that overlaps itself on non-matching input ('.' matches '/'
# too). A single one of these is cheap; several chained back to back is
# classic catastrophic backtracking, since the regex engine tries every way
# of splitting the input among them before concluding there is no match.
# Measured on one is_ignored() call against a non-matching path, pattern
# '(**/a)' repeated n times plus '/zzz': n=13 took 0.83s, n=14 took 3.34s,
# n=15 took 13.8s, n=16 took 55.4s -- roughly 4x per added interior '**',
# and unbounded as n grows, on a pattern any attacker-controlled
# .hearthignore content can write (search_files and replace_in_files call
# is_ignored per file and per directory, on a cancellable worker thread
# that a hang leaves spinning forever even after the caller gives up -- see
# is_workspace_busy in session.py). No real workspace-scoping pattern needs
# more than a couple of these; MAX_INTERIOR_DOUBLE_STAR caps it at a number
# comfortably above any legitimate use and comfortably below where
# backtracking becomes measurable at all -- see the self-test for the
# actual worst-case timing at the cap.
MAX_INTERIOR_DOUBLE_STAR = 2


class PatternTooComplex(ValueError):
    """Raised by _pattern_to_regex when a pattern's interior '**' count
    exceeds MAX_INTERIOR_DOUBLE_STAR. A distinct exception from ordinary
    re.error so _parse_hearthignore can report this case with its own,
    clearer message -- a user who wrote an over-complex pattern deserves to
    know it was rejected, not have it silently fold into "matches nothing",
    the fate of an ordinary malformed pattern."""


def _pattern_to_regex(body, anchored, flags):
    """Translate a gitignore-style pattern body (negation and the
    directory-only trailing slash already stripped by the caller) into a
    compiled, fully-anchored regex matched against a '/'-separated,
    workspace-relative path with no leading slash.

    '**' is only special as its own path segment: 'a/**/b' matches zero or
    more whole directories between a and b, '**/x' matches x at any depth,
    and 'x/**' matches x and everything under it. '**' fused into a larger
    segment (e.g. 'a**b') is not expanded; it falls back to two ordinary
    '*'s, the same degrade-quietly behaviour a real gitignore gives that
    construct.

    Raises PatternTooComplex if the pattern has more than
    MAX_INTERIOR_DOUBLE_STAR interior '**' segments -- see that constant's
    own comment for why this bound exists at all.
    """
    raw_segs = body.split("/")
    segs = []
    for s in raw_segs:
        if s == "**" and segs and segs[-1] == "**":
            continue  # collapse a run of consecutive '**' into one
        segs.append(s)
    n = len(segs)
    interior_double_star = sum(
        1 for i, seg in enumerate(segs) if seg == "**" and 0 < i < n - 1)
    if interior_double_star > MAX_INTERIOR_DOUBLE_STAR:
        raise PatternTooComplex(
            "pattern has {} interior '**' segments (max {}): {!r}".format(
                interior_double_star, MAX_INTERIOR_DOUBLE_STAR, body))
    parts = []
    for i, seg in enumerate(segs):
        if seg == "**":
            if n == 1:
                parts.append(".*")
            elif i == 0:
                parts.append("(?:.*/)?")
            elif i == n - 1:
                parts.append("(?:/.*)?")
            else:
                parts.append("/(?:.*/)?")
        else:
            if parts and segs[i - 1] != "**":
                parts.append("/")
            parts.append(_segment_to_regex(seg))
    body_re = "".join(parts)
    prefix = r"\A" if anchored else r"\A(?:.*/)?"
    return re.compile(prefix + body_re + r"\Z", flags)


def _parse_hearthignore(lines):
    """Parse .hearthignore lines into a list of _Rule, in file order (a
    later rule takes precedence over an earlier one for a given path,
    exactly like gitignore's own last-match-wins rule).

    Supported: blank lines and full-line '#' comments; '\\#' and '\\!' to
    match a literal leading '#' or '!'; a leading '!' to negate a pattern
    (un-ignore a path an earlier or later rule in the same file also
    matches -- this can only re-include a path already inside the
    workspace; see IgnoreSpec.is_ignored); a trailing '/' for a
    directory-only pattern; '*', '?', '[seq]'/'[!seq]' globbing; and '**'
    as a whole path segment for "zero or more directories" ('**/x', 'x/**',
    'a/**/b'). A pattern containing '/' anywhere but its very end is
    anchored to the workspace root; a pattern with no interior '/' matches
    at any depth.

    Deliberately not supported: nested per-directory .hearthignore files
    (only a single workspace-root file is ever read); any backslash escape
    other than a leading '\\#' or '\\!'; trailing-whitespace escaping; and
    '**' fused into a larger segment. An unsupported construct degrades to
    a literal or a partial match rather than raising, the same way an
    unmatched gitignore pattern quietly matches nothing.
    """
    flags = re.IGNORECASE if hearth_paths.is_windows() else 0
    rules = []
    for raw in lines:
        line = raw.rstrip("\r\n").rstrip()
        if not line or line.startswith("#"):
            continue
        negate = False
        if line.startswith("\\#") or line.startswith("\\!"):
            line = line[1:]
        elif line.startswith("!"):
            negate = True
            line = line[1:]
        if not line:
            continue
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        if not line:
            continue
        try:
            regex = _pattern_to_regex(line, anchored, flags)
        except PatternTooComplex as exc:
            print("[hearth-contain] .hearthignore pattern rejected (too complex, "
                  "would risk catastrophic regex backtracking): {}".format(exc),
                  file=sys.stderr)
            continue  # reported clearly, not silently dropped -- see PatternTooComplex
        except re.error:
            continue  # a malformed pattern matches nothing, rather than crashing
        rules.append(_Rule(regex, negate, dir_only))
    return rules


class IgnoreSpec:
    """Compiled .hearthignore rules for one workspace, in file order."""
    __slots__ = ("rules",)

    def __init__(self, rules):
        self.rules = rules

    def _match_one(self, relpath, is_dir):
        result = False
        for rule in self.rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(relpath):
                result = not rule.negate
        return result

    def is_ignored(self, relpath, is_dir=False):
        """True if `relpath` (workspace-relative, either slash style, no
        leading separator) -- or any directory above it -- is ignored.

        Every ancestor is checked, not just the leaf, so a negation
        pattern cannot re-include a file merely by naming it once its
        parent directory is already ignored (the same restriction
        gitignore itself applies). This is also why a pruning walk gets
        the restriction for free without doing anything special: once a
        directory is dropped from `dirs`, os.walk never redescends into it
        to ask about a file inside; this method exists for the tools that
        check one path directly, without a walk, and must reach the same
        answer.
        """
        if not self.rules:
            return False
        parts = [p for p in relpath.replace("\\", "/").split("/") if p and p != "."]
        for i in range(len(parts)):
            seg = "/".join(parts[:i + 1])
            seg_is_dir = is_dir if i == len(parts) - 1 else True
            if self._match_one(seg, seg_is_dir):
                return True
        return False


_EMPTY_IGNORE = IgnoreSpec(())
_IGNORE_CACHE = {}  # realpath(root) -> (mtime_or_None, IgnoreSpec)


def load_ignore(root):
    """Load and compile `.hearthignore` at the workspace root, once per
    session: cached by root and by the file's own mtime, so a long-running
    agent that never touches it pays the parse cost exactly once, while an
    agent (or a user, between runs) that does edit it sees the change on
    the next call rather than a stale compiled copy.

    Returns an IgnoreSpec whose is_ignored() always returns False when no
    `.hearthignore` exists at the workspace root -- absence changes
    nothing. Nested per-directory ignore files are not read; a single
    workspace-root file is the whole feature.
    """
    root_real = os.path.realpath(root)
    path = os.path.join(root_real, ".hearthignore")
    try:
        mtime = os.stat(hearth_paths.long_path(path)).st_mtime
    except OSError:
        mtime = None
    cached = _IGNORE_CACHE.get(root_real)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    if mtime is None:
        spec = _EMPTY_IGNORE
    else:
        try:
            with open(hearth_paths.long_path(path), "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        spec = IgnoreSpec(_parse_hearthignore(lines)) if lines else _EMPTY_IGNORE
    _IGNORE_CACHE[root_real] = (mtime, spec)
    return spec


def is_ignored(root, full, is_dir=None):
    """True if `full` -- an absolute path already produced by safe_join (or
    discovered inside a walk rooted at one) -- is excluded by the
    workspace's `.hearthignore`.

    This is the only entry point a tool should call, and it must always be
    called AFTER safe_join has already approved the path, never in place
    of it: is_ignored never resolves or joins anything itself, it only
    classifies a location that is already known to be inside the
    workspace. That ordering is what makes a hostile `.hearthignore`
    structurally unable to grant access -- there is no route through this
    function from a pattern string to an escaping filesystem location, only
    from an already-contained one to a yes/no visibility bit.
    """
    root_real = os.path.realpath(root)
    full_real = os.path.realpath(full)
    if full_real == root_real:
        return False  # the workspace root itself can never be ignored
    rel = os.path.relpath(full_real, root_real)
    if is_dir is None:
        try:
            is_dir = os.path.isdir(hearth_paths.long_path(full_real))
        except OSError:
            is_dir = False
    return load_ignore(root_real).is_ignored(rel, is_dir=is_dir)


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


def is_rooted(path):
    r"""True when `path` names a place on the filesystem rather than a place
    inside a workspace.

    This classifies; safe_join above resolves. The two give different answers
    for the same string on purpose, and they are not in tension:

      safe_join answers "can this be made safe", for a path a TOOL asked for
      mid-run. A leading separator is workspace-root-relative there, so
      "/src/main.py" lands inside the workspace instead of being refused.
      That is the documented ruling at the top of this module and every agent
      depends on it.

      is_rooted answers "did the person mean what this will do", for a path a
      HUMAN typed into a config that the server accepts over HTTP. Somebody
      who writes "/etc/passwd" or "C:\build\out.txt" into a required-artifacts
      list means a location on their disk. Quietly rereading that as
      "<workspace>/etc/passwd" is contained, and still wrong: the run would
      then wait forever on a file the user never asked for, never sees, and
      cannot find in the error message. Refusing it says so, at the only
      moment anyone is still in a position to fix the typo.

    Deliberately does not use os.path.isabs, which answers differently
    depending on the interpreter: ntpath.isabs stopped treating a single
    leading slash as absolute in Python 3.13, so a validation rule written on
    it refused "/etc/passwd" under a 3.12 sidecar and accepted it under a 3.13
    one, on the same machine, with the same config. A boundary that moves when
    the runtime is upgraded is not a boundary. This answers the same on every
    platform and every version.
    """
    text = (path or "").replace("\\", "/")
    if text.startswith("/"):
        return True  # POSIX absolute, and UNC "//host/share"
    # A drive qualifier, absolute ("C:/x") or drive-relative ("C:x"). Both name
    # a place on a specific volume; neither is inside a workspace.
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


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


def prune(dirpath, dirs, skip_names=(), root=None, ignore_spec=None):
    """Filter a walk's directory list in place. Drops skip-listed names
    (case-insensitively, because NTFS is case-insensitive and ".GIT" is ".git")
    and every reparse point.

    When `root` and `ignore_spec` are both given, also drops any directory
    excluded by `.hearthignore` (computed relative to `root`). The
    exclusion happens here, before os.walk descends -- not by filtering
    results afterward -- so a caller passing both never opens, reads, or
    rewrites a single file underneath an ignored directory, and never pays
    to walk it either.

    Must be called in every os.walk loop that touches workspace content.
    """
    skip = {s.lower() for s in skip_names}
    kept = []
    for d in dirs:
        if d.lower() in skip:
            continue
        full_d = os.path.join(dirpath, d)
        if is_reparse(full_d):
            continue
        if ignore_spec is not None and root is not None:
            rel = os.path.relpath(full_d, root)
            if ignore_spec.is_ignored(rel, is_dir=True):
                continue
        kept.append(d)
    dirs[:] = kept
    return dirs


def _self_test():
    import contextlib
    import io
    import shutil
    import tempfile
    import time

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

        # is_rooted classifies what safe_join resolves, and must answer the
        # same on every platform and every Python version - see its docstring
        # for the os.path.isabs change that made this its own function.
        for rooted in ("/etc/passwd", "/", "\\windows\\x", "C:\\Windows\\win.ini",
                       "c:/w/x", "C:x", "//host/share/x", "\\\\host\\share\\x"):
            assert is_rooted(rooted) is True, rooted
        for relative in ("out/report.txt", "a.txt", "", "sub/deep/f",
                         "../escape", "a/../../b", "CC:/not-a-drive", "1:/x"):
            assert is_rooted(relative) is False, relative

        # Traversal is refused.
        for bad in ("../evil.txt", "../../evil.txt", "sub/../../evil.txt", "sub/deep/../../../evil.txt"):
            try:
                safe_join(root, bad)
                raise AssertionError("traversal not caught: {}".format(bad))
            except ValueError:
                pass

        # A sibling whose name merely starts with the workspace name is outside it.
        # A startswith(root + os.sep) test would wrongly accept this; commonpath does not.
        sibling = root + "-evil"
        os.makedirs(sibling, exist_ok=True)
        try:
            safe_join(root, os.path.join("..", os.path.basename(sibling), "x.txt"))
            raise AssertionError("sibling-prefix escape not caught")
        except ValueError:
            pass
        finally:
            shutil.rmtree(sibling, ignore_errors=True)

        # A leading slash is workspace-root-relative on every platform, not a
        # filesystem-absolute escape. This is deliberate (see module docstring)
        # and matches the original _safe_join that hearth's agents depend on.
        if hearth_paths.is_windows():
            # A drive-qualified path has no leading separator, so it is not
            # touched by the leading-slash strip, and must still be refused
            # on its own terms. C:\Windows\win.ini is caught by the
            # alternate-data-stream check on "C:" before containment even
            # runs, so exercise _within directly too, with no colon involved,
            # to prove the containment check itself also fails closed here.
            try:
                safe_join(root, "C:\\Windows\\win.ini")
                raise AssertionError("drive-qualified escape not caught")
            except ValueError:
                pass
            assert not _within(os.path.join(os.path.dirname(root), "win.ini"), root)
        else:
            assert safe_join(root, "/etc/passwd") == os.path.realpath(os.path.join(root, "etc", "passwd"))

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

    # --- .hearthignore -----------------------------------------------------

    # Absence changes nothing: no file at all means never ignored.
    ig_none = os.path.realpath(tempfile.mkdtemp(prefix="hearth-ignore-none-"))
    try:
        os.makedirs(os.path.join(ig_none, "sub"))
        with open(os.path.join(ig_none, "sub", "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        spec0 = load_ignore(ig_none)
        assert not spec0.rules, "no .hearthignore must produce an empty spec"
        assert not is_ignored(ig_none, os.path.join(ig_none, "sub", "f.txt"))
        assert not is_ignored(ig_none, os.path.join(ig_none, "sub"))
    finally:
        shutil.rmtree(ig_none, ignore_errors=True)

    # Parsing/matching: comments, blank lines, directory-only, negation,
    # anchoring, and '**'.
    ig_root = os.path.realpath(tempfile.mkdtemp(prefix="hearth-ignore-parse-"))
    try:
        os.makedirs(os.path.join(ig_root, "vendor", "deep"))
        os.makedirs(os.path.join(ig_root, "build"))
        os.makedirs(os.path.join(ig_root, "src", "vendor"))
        os.makedirs(os.path.join(ig_root, "logs"))
        for rel in ("vendor/a.txt", "vendor/deep/b.txt", "build/out.bin",
                    "src/vendor/c.txt", "src/keep.py", "logs/app.log",
                    "logs/important.log", "top.log"):
            full = os.path.join(ig_root, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write("x")
        with open(os.path.join(ig_root, ".hearthignore"), "w", encoding="utf-8") as fh:
            fh.write(
                "# a comment, and a blank line follow\n"
                "\n"
                "vendor/\n"          # dir-only, unanchored: matches at any depth
                "/build\n"           # anchored: only the top-level 'build'
                "*.log\n"            # unanchored file pattern: any depth
                "!important.log\n"   # narrows the ignore set back down
            )
        spec = load_ignore(ig_root)
        assert len(spec.rules) == 4, [r.regex.pattern for r in spec.rules]

        def _ig(rel, is_dir=False):
            return is_ignored(ig_root, os.path.join(ig_root, *rel.split("/")), is_dir=is_dir)

        assert _ig("vendor", is_dir=True), "unanchored dir-only pattern must match at the top level"
        assert _ig("vendor/a.txt"), "a file under an ignored directory is ignored too"
        assert _ig("src/vendor", is_dir=True), "unanchored dir-only pattern must match at any depth"
        assert _ig("src/vendor/c.txt")
        assert not _ig("src/keep.py"), "an unrelated file must not be ignored"
        assert _ig("build", is_dir=True), "anchored pattern matches the top-level name"
        assert _ig("build/out.bin")
        assert _ig("logs/app.log"), "unanchored file pattern matches at any depth"
        assert _ig("top.log")
        assert not _ig("logs/important.log"), "a later negation narrows the ignore set"

        # A directory-only pattern must not match a same-named FILE.
        with open(os.path.join(ig_root, "vendor_file_placeholder"), "w", encoding="utf-8") as fh:
            fh.write("x")
        assert not _ig("vendor_file_placeholder"), "dir-only pattern must not match a differently-named file"

        # Negating a file inside a still-ignored directory does not surface
        # it: this is the "cannot re-include inside an ignored parent"
        # restriction gitignore itself applies, and it is what a pruning
        # walk enforces for free (see the walk test in hearth_tools).
        with open(os.path.join(ig_root, ".hearthignore"), "a", encoding="utf-8") as fh:
            fh.write("!vendor/a.txt\n")
        _IGNORE_CACHE.pop(os.path.realpath(ig_root), None)
        assert _ig("vendor/a.txt"), \
            "negating a file cannot re-include it while its parent directory is still ignored"
    finally:
        shutil.rmtree(ig_root, ignore_errors=True)

    # '**' matching: leading, trailing, and interior.
    ig_star = os.path.realpath(tempfile.mkdtemp(prefix="hearth-ignore-star-"))
    try:
        with open(os.path.join(ig_star, ".hearthignore"), "w", encoding="utf-8") as fh:
            fh.write("**/cache\na/**/z\ndata/**\n")
        s = load_ignore(ig_star)

        def _sig(rel, is_dir=False):
            return is_ignored(ig_star, os.path.join(ig_star, *rel.split("/")), is_dir=is_dir)

        assert _sig("cache", is_dir=True), "'**/x' must match at the root too"
        assert _sig("p/q/cache", is_dir=True), "'**/x' must match at any depth"
        assert _sig("a/z"), "'a/**/z' must match with zero directories in between"
        assert _sig("a/mid/z")
        assert _sig("a/mid1/mid2/z")
        assert not _sig("a/z_other"), "'a/**/z' must not match an unrelated sibling"
        assert _sig("data", is_dir=True), "'x/**' matches x itself"
        assert _sig("data/anything/deep")
    finally:
        shutil.rmtree(ig_star, ignore_errors=True)

    # === Finding 2: catastrophic regex backtracking from unbounded ========
    # === interior '**' segments is capped, not merely observed ============
    def _hostile_double_star_pattern(n):
        """The exact shape the security review measured: '(**/a)' repeated
        n times plus a trailing '/zzz' that never matches. Produces
        n - 1 interior '**' segments (the first '**' is the pattern's
        leading segment, not interior)."""
        return "/".join(["**", "a"] * n) + "/zzz"

    # THE PIN, part 1 (non-vacuousness): prove the repro really is
    # catastrophic when nothing bounds it, by temporarily lifting the cap
    # and timing a real match -- if this stops being slow, the fix below
    # is not actually preventing anything, it would just be asserting its
    # own existence.
    global MAX_INTERIOR_DOUBLE_STAR
    old_cap = MAX_INTERIOR_DOUBLE_STAR
    MAX_INTERIOR_DOUBLE_STAR = 10_000
    try:
        hostile_n = 11  # 10 interior '**' segments, far past the real cap of 2
        hostile_body = _hostile_double_star_pattern(hostile_n)
        hostile_rx = _pattern_to_regex(hostile_body, True, 0)
        hostile_path = "a/" * (3 * hostile_n) + "notzzz"  # close enough to match to force exhaustive backtracking
        t0 = time.monotonic()
        hostile_rx.match(hostile_path)
        uncapped_seconds = time.monotonic() - t0
    finally:
        MAX_INTERIOR_DOUBLE_STAR = old_cap
    assert uncapped_seconds > 1.0, (
        "non-vacuousness check itself is broken: an uncapped pattern with 10 interior "
        "'**' segments was expected to take multiple seconds to fail against a mere "
        "33-segment non-matching path, but took only {:.3f}s -- either this no longer "
        "reproduces catastrophic backtracking, or the harness changed under it, and the "
        "PASS below would not be proving what it claims to prove".format(uncapped_seconds)
    )

    # THE PIN, part 2: with the real cap in place, _pattern_to_regex refuses
    # the same pattern outright -- PatternTooComplex, not a hang.
    assert MAX_INTERIOR_DOUBLE_STAR == 2, "cap must already be restored here"
    try:
        _pattern_to_regex(hostile_body, True, 0)
        raise AssertionError("an over-complex pattern must raise PatternTooComplex, not compile")
    except PatternTooComplex:
        pass

    # THE PIN, part 3: the real, end-to-end path (a hostile .hearthignore on
    # disk, loaded via load_ignore, queried via is_ignored -- the exact
    # call chain search_files/replace_in_files drive per file and per
    # directory) rejects the pattern at PARSE time and returns fast, rather
    # than compiling it and hanging on the first non-matching query. Also
    # proves the rejection is reported, not silently dropped: the pattern
    # never becomes a rule (it does not shadow the other, well-formed rule
    # in the same file), and a clear message reaches stderr.
    redos_root = os.path.realpath(tempfile.mkdtemp(prefix="hearth-ignore-redos-"))
    try:
        with open(os.path.join(redos_root, ".hearthignore"), "w", encoding="utf-8") as fh:
            fh.write(hostile_body + "\n" + "plainly-ignored.txt\n")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            redos_spec = load_ignore(redos_root)
        stderr_text = buf.getvalue()
        assert "too complex" in stderr_text, \
            "a rejected pattern must be reported clearly, not silently dropped: {!r}".format(stderr_text)
        assert hostile_body in stderr_text, \
            "the rejection message should name the actual rejected pattern: {!r}".format(stderr_text)
        # The other, well-formed rule in the same file is unaffected.
        assert len(redos_spec.rules) == 1, [r.regex.pattern for r in redos_spec.rules]

        deep_path = os.path.join(redos_root, *(["a"] * (3 * hostile_n) + ["notzzz"]))
        t0 = time.monotonic()
        result = is_ignored(redos_root, deep_path)
        capped_seconds = time.monotonic() - t0
        assert result is False, "the rejected hostile pattern must never take effect as a rule"
        assert capped_seconds < 1.0, \
            "is_ignored() against the exact previously-catastrophic input must stay fast " \
            "once the hostile pattern is rejected at parse time: took {:.3f}s".format(capped_seconds)
    finally:
        shutil.rmtree(redos_root, ignore_errors=True)

    # A legitimate pattern AT the cap (2 interior '**') still compiles and
    # matches correctly, and stays fast even against a long non-matching
    # path -- the cap must not make ordinary .hearthignore patterns
    # unusable, only reject the pathological ones past it.
    at_cap_body = _hostile_double_star_pattern(3)  # 2 interior '**' segments, exactly at the cap
    at_cap_rx = _pattern_to_regex(at_cap_body, True, 0)  # must not raise
    at_cap_matching_path = "/".join(["x", "a"] * 3 + ["zzz"])
    assert at_cap_rx.match(at_cap_matching_path), "a pattern at the cap must still match correctly"
    long_non_matching = "a/" * 200 + "notzzz"
    t0 = time.monotonic()
    at_cap_rx.match(long_non_matching)
    at_cap_seconds = time.monotonic() - t0
    assert at_cap_seconds < 2.0, \
        "a pattern at the cap must stay fast even against a long non-matching path: " \
        "took {:.3f}s".format(at_cap_seconds)

    # --- The hard rule: a hostile .hearthignore cannot widen safe_join. ---
    # These patterns are exactly the shapes that would matter if the ignore
    # check were ever wired into path RESOLUTION instead of applied as a
    # post-containment filter: a negation of a traversal, a negation of a
    # drive-qualified Windows path, a bare traversal pattern, and a
    # negate-everything catch-all. None of them are ever consulted by
    # safe_join at all -- it does not know .hearthignore exists -- so this
    # is a regression guard against that ever changing by accident.
    hostile_root = os.path.realpath(tempfile.mkdtemp(prefix="hearth-ignore-hostile-"))
    try:
        with open(os.path.join(hostile_root, ".hearthignore"), "w", encoding="utf-8") as fh:
            fh.write(
                "!/../../etc/passwd\n"
                "!C:\\Windows\\*\n"
                "../../*\n"
                "!*\n"
                "!**\n"
            )
        loaded = load_ignore(hostile_root)  # must not raise
        assert isinstance(loaded, IgnoreSpec)
        for bad in ("../../etc/passwd", "../evil.txt", "../../../evil.txt",
                    "sub/../../../evil.txt"):
            try:
                safe_join(hostile_root, bad)
                raise AssertionError("hostile .hearthignore widened safe_join: {}".format(bad))
            except ValueError:
                pass
        if hearth_paths.is_windows():
            try:
                safe_join(hostile_root, "C:\\Windows\\win.ini")
                raise AssertionError("hostile .hearthignore widened safe_join (drive path)")
            except ValueError:
                pass
        # And ordinary containment inside the workspace is unaffected by the
        # hostile file's presence.
        with open(os.path.join(hostile_root, "ok.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        assert safe_join(hostile_root, "ok.txt") == os.path.realpath(os.path.join(hostile_root, "ok.txt"))
    finally:
        shutil.rmtree(hostile_root, ignore_errors=True)

    # Caching: compiled once, reused until the file's mtime changes.
    cache_root = os.path.realpath(tempfile.mkdtemp(prefix="hearth-ignore-cache-"))
    try:
        with open(os.path.join(cache_root, ".hearthignore"), "w", encoding="utf-8") as fh:
            fh.write("a.txt\n")
        first = load_ignore(cache_root)
        second = load_ignore(cache_root)
        assert first is second, "load_ignore must reuse the cached spec when the file is unchanged"
        import time as _t
        _t.sleep(0.05)
        with open(os.path.join(cache_root, ".hearthignore"), "w", encoding="utf-8") as fh:
            fh.write("b.txt\n")
        os.utime(os.path.join(cache_root, ".hearthignore"), None)
        third = load_ignore(cache_root)
        assert third is not first, "an mtime change must invalidate the cache"
    finally:
        _IGNORE_CACHE.pop(cache_root, None)
        shutil.rmtree(cache_root, ignore_errors=True)

    # prune() drops an ignored directory before a walk ever descends into it.
    prune_root = os.path.realpath(tempfile.mkdtemp(prefix="hearth-ignore-prune-"))
    try:
        with open(os.path.join(prune_root, ".hearthignore"), "w", encoding="utf-8") as fh:
            fh.write("secret/\n")
        pspec = load_ignore(prune_root)
        dlist = ["secret", "public"]
        prune(prune_root, dlist, SKIP_DIRS, root=prune_root, ignore_spec=pspec)
        assert dlist == ["public"], dlist
        # Backward compatibility: the 3-positional-argument call (every
        # existing caller) still works with no ignore filtering applied.
        dlist2 = ["secret", ".git"]
        prune(prune_root, dlist2, SKIP_DIRS)
        assert dlist2 == ["secret"], dlist2
    finally:
        shutil.rmtree(prune_root, ignore_errors=True)

    print("hearth-contain self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
