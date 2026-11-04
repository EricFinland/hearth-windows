#!/usr/bin/env python3
"""hearth path patterns: allow and deny lists for paths inside a workspace.

WHY THIS EXISTS, AND WHY IT IS NOT hearth_contain
-------------------------------------------------
hearth_contain answers one question: is this path inside the run's
workspace. That is a boundary, it touches the filesystem (realpath,
junction detection, reserved device names), and nothing above it may be
trusted to re-answer it. This module answers the next question down:
given a path that containment has ALREADY approved, may this tool touch
it. "Keep the agent out of secrets/ and out of *.pem, but let it rewrite
anything under src/" is a policy question, not a containment question.

This module COMPLEMENTS hearth_contain and is NOT A SUBSTITUTE FOR IT.
A pattern matcher is not a boundary. It is a string classifier: it never
calls realpath, never follows a symlink or a junction, never opens
anything, and therefore cannot know that "src/vendor" is a junction
pointing at C:\\Windows. The only correct order is safe_join first (which
resolves and contains), this matcher second (which classifies the result).
Calling this INSTEAD of safe_join would be a security bug, because every
guarantee here is about the shape of a string and none of it is about the
filesystem. Same relationship, and the same ordering rule, as
hearth_contain.is_ignored: resolve, then classify.

It is also deliberately a separate module from the .hearthignore support
already inside hearth_contain. That one is narrowing-only policy attached
to a workspace on disk (one file, at the root, written by the user). This
one is an in-process policy object a caller constructs per tool or per
run, with two lists, no file, and an allow list .hearthignore has no
concept of.

THE NORMALISATION (this is the whole security story)
----------------------------------------------------
Every trick against a path deny list is a spelling trick: the pattern
says secrets/* and the caller says ./secrets/key, or secrets//key, or
a/../secrets/key, or secrets\\key, or "secrets ./key". So matching never
happens on the caller's string. It happens on a normalised, '/'-separated,
root-relative form, built like this:

  1. A UNC path ("//host/share/x", "\\\\host\\share\\x") or a
     drive-qualified path ("C:/x", "C:\\x", "C:x") is refused outright.
     Those name a place on a volume, not a place inside a workspace, so
     there is no honest relative form to match and guessing one would be
     how a deny rule gets dodged. Verdict: not allowed, decided_by
     "not-relative". (This mirrors hearth_contain.is_rooted, and for the
     same reason.)
  2. Backslashes are read as separators, on every platform, because a
     model prompted for Windows emits them on any host.
  3. A leading separator is stripped: "/secrets/key" is root-relative,
     exactly as hearth_contain.safe_join treats it. It is not an escape
     and it is not a filesystem-absolute path.
  4. Empty segments collapse ("secrets//key" -> "secrets/key") and "."
     segments are dropped ("./secrets/key" -> "secrets/key").
  5. ".." is resolved lexically against the segments to its left
     ("a/../secrets/key" -> "secrets/key"). A ".." that would climb above
     the root (".." on its own, "a/../..") is refused: not allowed,
     decided_by "not-relative". Fail closed, because a path that is not
     inside the root cannot be classified by root-relative patterns, and
     because containment (not this module) is what should have caught it.
  6. Trailing dots and spaces are stripped from each segment, because
     Windows strips them when it opens the file: "secrets ./key",
     "secrets./key" and "secrets/key." all address what "secrets/key"
     addresses.

Steps 2 and 6 would, on POSIX, change the meaning of a legal filename (a
backslash is an ordinary character there, and "secrets " is a real
directory distinct from "secrets"). Rather than trade one hole for
another, both readings are kept as CANDIDATES on non-Windows hosts, and
the verdict combines them the only safe way round: a path is denied if
ANY candidate is denied, and allowed only if EVERY candidate is allowed.
On Windows there is only ever one candidate, because there the stripping
is simply what the OS does.

VERDICTS
--------
match() returns a dict, never a bare bool, because "denied" without
"denied by which rule" is unusable in a log and unfixable by a user:

  {"input": the caller's string,
   "path": the normalised form (None when it has none),
   "allowed": True/False,
   "rule": the exact pattern string that decided, or None,
   "decided_by": "deny" | "allow" | "allow-empty" | "no-allow-match"
                 | "not-relative",
   "reason": one line, safe to log}

DENY ALWAYS WINS. The deny list is consulted first and a hit returns
immediately, so no ordering of the two lists, and no clever allow
pattern, can out-vote a deny rule. An empty allow list means
allow-everything-not-denied; an empty deny list denies nothing. Both
lists, empty, allow everything: a Matcher with no policy is not a
silently closed door.

Ancestors are checked, not just the leaf, so denying "secrets/" or
"secrets/**" denies everything beneath it without the pattern having to
enumerate depths, and allowing "src/" allows its contents. Note the
gitignore-faithful consequence: "secrets/*" denies the CONTENTS of
secrets but not the directory entry itself. Deny "secrets/**" or
"secrets/" when you mean the directory too; the self-test pins this
behaviour so it cannot drift silently.

MALFORMED PATTERNS RAISE AT CONSTRUCTION
----------------------------------------
PatternError (a ValueError) is raised by the constructor, not at match
time and never swallowed. A deny rule that quietly matches nothing is the
worst available failure mode here: the run looks protected and is not.
hearth_contain's .hearthignore parser deliberately degrades quietly
instead, because there a bad line in a user's file must not break a run
and the file can only ever narrow access. The trade is the other way
round for a policy object built in code: wrong patterns must be loud.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
No filesystem access of any kind, so match() works on paths that do not
exist and the self-test needs no temp directory. No containment check
(hearth_contain). No symlink, junction, reserved-device-name or
alternate-data-stream handling (hearth_contain). No gitignore '!'
negation: an exemption inside a deny list makes "deny wins" unprovable,
and two explicit lists say the same thing legibly, so a leading '!' is a
PatternError. No per-directory pattern files, no comments and no blank
lines (this is a list in code, not a file). No '**' fused into a larger
segment ('a**b' raises rather than quietly degrading). No case folding
beyond re.IGNORECASE, so exotic Unicode casing is not guaranteed; do not
rely on case-insensitive matching as a security property.

Standard library only.
"""

import argparse
import os
import re
import sys


# Each interior '**' (strictly between the first and last segment) compiles
# to its own unbounded "/(?:.*/)?" alternation, and a chain of them is
# classic catastrophic backtracking on non-matching input. hearth_contain
# measured it: roughly 4x per added interior '**', 13.8s at n=15 for a
# single match call. The same cap applies here, with the same comfortable
# margin above any legitimate pattern, and here it RAISES rather than
# warning, because a pattern in code is a programming error.
MAX_INTERIOR_DOUBLE_STAR = 2

# A pattern longer than this is not a pattern anyone wrote on purpose.
MAX_PATTERN_LENGTH = 1024


class PatternError(ValueError):
    """A pattern that cannot be compiled, raised by Matcher's constructor.

    A ValueError subclass so callers that already catch ValueError around
    hearth_contain.safe_join keep working, and a named class so a config
    loader can report "your rule is wrong" rather than "something failed".
    """


def default_case_sensitive():
    """The platform's path case sensitivity, as this module assumes it.

    False on Windows (NTFS is case-insensitive, so ".GIT" is ".git" and a
    case-sensitive deny rule there would be dodgeable by typing SECRETS/).
    True everywhere else. macOS is usually case-insensitive in practice
    but reports os.name == "posix"; a caller who knows better passes
    case_sensitive explicitly rather than having it guessed.
    """
    return os.name != "nt"


class _Rule:
    """One compiled pattern, kept next to the exact string it came from so
    a verdict can name the rule the user wrote, not a regex."""
    __slots__ = ("pattern", "regex", "dir_only")

    def __init__(self, pattern, regex, dir_only):
        self.pattern = pattern
        self.regex = regex
        self.dir_only = dir_only


def _segment_to_regex(seg, pattern):
    """Translate one pattern segment (no '/' in it) to a regex fragment.

    '*' is any run of non-separator characters, '?' is exactly one, and
    '[seq]' / '[!seq]' are character classes; everything else is literal.
    Hand-rolled rather than built on fnmatch.translate, whose wrapper
    format is not part of its documented contract and has changed between
    Python versions, while the semantics below are the familiar ones.

    Raises PatternError for an unterminated class, and for a class that
    contains '/', which would otherwise let a single segment's pattern
    reach across a separator and quietly break the one invariant the
    caller is relying on ('*' and '?' never cross a separator).
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
                raise PatternError(
                    "unterminated character class in {!r}".format(pattern))
            inner = seg[i + 1:j]
            if not inner or inner in ("!", "^"):
                raise PatternError(
                    "empty character class in {!r}".format(pattern))
            if "/" in inner:
                raise PatternError(
                    "character class may not contain '/' in {!r}: a class is "
                    "matched inside one path segment".format(pattern))
            if inner.startswith("!"):
                inner = "^" + inner[1:]
            out.append("[" + inner + "]")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _compile(pattern, case_sensitive):
    """Compile one gitignore-flavoured pattern into a _Rule.

    Supported: '*' (never crosses a separator), '**' as a whole segment
    (crosses separators: '**/x' at any depth, 'x/**' for x and everything
    under it, 'a/**/b' for zero or more directories between), '?',
    '[seq]' / '[!seq]', a leading '/' anchoring to the workspace root, a
    trailing '/' for directories only, and gitignore's own anchoring rule:
    a pattern containing '/' anywhere but its very end is anchored to the
    root, and a pattern with no interior '/' matches at any depth.

    Everything else raises PatternError. See the module docstring for why
    this is loud rather than quiet.
    """
    if not isinstance(pattern, str):
        raise PatternError("pattern must be a string, got {!r}".format(
            type(pattern).__name__))
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise PatternError("pattern longer than {} characters".format(
            MAX_PATTERN_LENGTH))
    if not pattern:
        raise PatternError("empty pattern")
    if pattern != pattern.strip():
        raise PatternError(
            "pattern has leading or trailing whitespace: {!r}".format(pattern))
    if not pattern.strip():
        raise PatternError("whitespace-only pattern")
    for ch in pattern:
        if ord(ch) < 32:
            raise PatternError(
                "control character in pattern: {!r}".format(pattern))
    if "\\" in pattern:
        # Not a separator and not an escape: patterns are written with '/'
        # on every platform. Accepting '\\' here would mean guessing which
        # of the two it was meant to be, on a string that decides access.
        raise PatternError(
            "backslash in pattern {!r}: write path patterns with '/' on "
            "every platform".format(pattern))
    if pattern.startswith("!"):
        raise PatternError(
            "negation is not supported: {!r}. Use the allow list instead; "
            "deny always wins here".format(pattern))
    if pattern.startswith("#"):
        raise PatternError(
            "comments are not supported in a pattern list: {!r}".format(
                pattern))
    if pattern.startswith("//"):
        raise PatternError("UNC-style pattern is not supported: {!r}".format(
            pattern))
    if len(pattern) >= 2 and pattern[1] == ":" and pattern[0].isalpha():
        raise PatternError(
            "drive-qualified pattern is not supported: {!r}. Patterns are "
            "workspace-relative".format(pattern))

    dir_only = pattern.endswith("/")
    body = pattern[:-1] if dir_only else pattern
    anchored = "/" in body
    body = body.lstrip("/")
    if not body:
        raise PatternError("pattern has no body: {!r}".format(pattern))

    segs = body.split("/")
    for seg in segs:
        if not seg:
            raise PatternError(
                "empty path segment in {!r} (an unintended '//')".format(
                    pattern))
        if seg in (".", ".."):
            raise PatternError(
                "'.' and '..' are not allowed in a pattern: {!r}".format(
                    pattern))
        if "**" in seg and seg != "**":
            raise PatternError(
                "'**' must be a whole path segment in {!r}; 'a**b' is "
                "ambiguous".format(pattern))

    n = len(segs)
    interior = sum(1 for i, s in enumerate(segs)
                   if s == "**" and 0 < i < n - 1)
    if interior > MAX_INTERIOR_DOUBLE_STAR:
        raise PatternError(
            "pattern {!r} has {} interior '**' segments (max {}): this "
            "risks catastrophic regex backtracking".format(
                pattern, interior, MAX_INTERIOR_DOUBLE_STAR))

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
            parts.append(_segment_to_regex(seg, pattern))

    prefix = r"\A" if anchored else r"\A(?:.*/)?"
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(prefix + "".join(parts) + r"\Z", flags)
    except re.error as exc:  # defensive: every known cause is caught above
        raise PatternError("cannot compile {!r}: {}".format(pattern, exc))
    return _Rule(pattern, regex, dir_only)


def _segments(text, strip_tail):
    """Split one '/'-separated reading into normalised segments.

    Returns None when the path climbs above the root, which the caller
    turns into a refusal rather than a guess. See the module docstring,
    steps 4 to 6, for what is dropped and why. '.' and '..' are decided
    BEFORE the trailing dot/space strip, because "..".rstrip(" .") is the
    empty string and a traversal that normalised itself out of existence
    would be the single worst bug this module could have.
    """
    out = []
    for raw in text.split("/"):
        if not raw or raw == ".":
            continue
        if raw == "..":
            if not out:
                return None
            out.pop()
            continue
        seg = raw.rstrip(" .") if strip_tail else raw
        if not seg:
            continue  # a segment of nothing but dots and spaces
        out.append(seg)
    return out


def _normalise(path):
    """Normalise a caller's path string for matching.

    Returns (candidates, trailing_sep, problem). `candidates` is a tuple
    of normalised, '/'-separated, root-relative strings (more than one
    only on non-Windows hosts, where a backslash and a trailing space are
    legal filename characters and both readings must be judged: see the
    module docstring). `problem` is None, or a one-line reason the path
    has no root-relative form at all.
    """
    if not isinstance(path, str):
        return (), False, "path must be a string, got {}".format(
            type(path).__name__)
    probe = path.replace("\\", "/")
    if probe.startswith("//") and probe.strip("/"):
        return (), False, (
            "UNC path is not workspace-relative: {!r}".format(path))
    if len(probe) >= 2 and probe[1] == ":" and probe[0].isalpha():
        return (), False, (
            "drive-qualified path is not workspace-relative: {!r}".format(
                path))

    trailing = path.endswith("/") or path.endswith("\\")
    readings = [probe]
    if os.name != "nt" and "\\" in path:
        readings.append(path)  # backslash as an ordinary filename character
    strip_variants = (True,) if os.name == "nt" else (True, False)

    cands = []
    for reading in readings:
        for strip_tail in strip_variants:
            segs = _segments(reading, strip_tail)
            if segs is None:
                return (), False, (
                    "path climbs above the workspace root: {!r}".format(path))
            rel = "/".join(segs)
            if rel not in cands:
                cands.append(rel)
    return tuple(cands), trailing, None


def _first_hit(rules, rel, is_dir):
    """The first rule matching `rel` or any directory above it, else None.

    Ancestors are walked shallowest first, so a rule on a directory wins
    over a rule on a leaf inside it, and rules are tried in list order at
    each depth. Every ancestor is treated as a directory; only the leaf
    uses the caller's `is_dir`.
    """
    if not rules or not rel:
        return None
    segs = rel.split("/")
    last = len(segs) - 1
    for i in range(len(segs)):
        sub = "/".join(segs[:i + 1])
        sub_is_dir = is_dir if i == last else True
        for rule in rules:
            if rule.dir_only and not sub_is_dir:
                continue
            if rule.regex.match(sub):
                return rule
    return None


class Matcher:
    """A compiled allow/deny policy for workspace-relative paths.

    Matcher(allow=None, deny=None, case_sensitive=None). Both lists are
    sequences of pattern strings; a single string is rejected rather than
    iterated character by character, because Matcher(deny="secrets/*")
    would otherwise compile seven one-character rules and protect
    nothing. case_sensitive defaults to the platform (see
    default_case_sensitive) and is an explicit flag so a caller, and the
    self-test, can pin either behaviour without depending on the host.

    Construction raises PatternError on any malformed pattern. Matching
    never raises and never touches the filesystem.
    """
    __slots__ = ("allow", "deny", "case_sensitive")

    def __init__(self, allow=None, deny=None, case_sensitive=None):
        if case_sensitive is None:
            case_sensitive = default_case_sensitive()
        self.case_sensitive = bool(case_sensitive)
        self.allow = self._compile_list(allow, "allow", self.case_sensitive)
        self.deny = self._compile_list(deny, "deny", self.case_sensitive)

    @staticmethod
    def _compile_list(patterns, name, case_sensitive):
        if patterns is None:
            return ()
        if isinstance(patterns, str):
            raise PatternError(
                "{} must be a list of patterns, not a single string "
                "({!r})".format(name, patterns))
        return tuple(_compile(p, case_sensitive) for p in patterns)

    def match(self, path, is_dir=False):
        """Judge one path. Returns the verdict dict described in the
        module docstring. Deny is consulted first and wins outright.

        `is_dir` tells a trailing-slash (directory-only) rule what this
        path is, since nothing here looks at the filesystem to find out. A
        path written with a trailing separator counts as a directory too.
        """
        cands, trailing, problem = _normalise(path)
        if problem is not None:
            return {"input": path, "path": None, "allowed": False,
                    "rule": None, "decided_by": "not-relative",
                    "reason": problem}
        is_dir = bool(is_dir) or trailing
        shown = cands[0] if cands else ""

        # Deny first, and return on the first hit: nothing in the allow
        # list can reach past this point.
        for rel in cands:
            rule = _first_hit(self.deny, rel, is_dir)
            if rule is not None:
                return {"input": path, "path": rel, "allowed": False,
                        "rule": rule.pattern, "decided_by": "deny",
                        "reason": "denied by {!r}".format(rule.pattern)}

        if not self.allow:
            return {"input": path, "path": shown, "allowed": True,
                    "rule": None, "decided_by": "allow-empty",
                    "reason": "no deny rule matched and the allow list is "
                              "empty"}

        # Every candidate reading must be allowed, not just one.
        hit = None
        for rel in cands:
            rule = _first_hit(self.allow, rel, is_dir)
            if rule is None:
                return {"input": path, "path": rel, "allowed": False,
                        "rule": None, "decided_by": "no-allow-match",
                        "reason": "no allow rule matched {!r}".format(rel)}
            if hit is None:
                hit = rule
        return {"input": path, "path": shown, "allowed": True,
                "rule": hit.pattern, "decided_by": "allow",
                "reason": "allowed by {!r}".format(hit.pattern)}

    def allowed(self, path, is_dir=False):
        """match()["allowed"], for callers that only need the bit."""
        return self.match(path, is_dir=is_dir)["allowed"]


def _self_test():
    """Offline, deterministic, and pure string logic: not one path below
    exists on disk, and none needs to. Every hostile spelling this module
    was written for is pinned here."""

    # --- the shape of a verdict ------------------------------------------
    m = Matcher(deny=["secrets/*"], case_sensitive=True)
    v = m.match("secrets/key")
    assert set(v) == {"input", "path", "allowed", "rule", "decided_by",
                      "reason"}, sorted(v)
    assert v["allowed"] is False, v
    assert v["rule"] == "secrets/*", v  # the pattern as written, not a regex
    assert v["decided_by"] == "deny", v
    assert v["path"] == "secrets/key", v
    assert v["input"] == "secrets/key", v
    assert issubclass(PatternError, ValueError)

    # --- THE HOSTILE SPELLINGS -------------------------------------------
    # Every one of these addresses what 'secrets/*' protects, and every
    # one of them must be denied by that rule. This is the list the module
    # exists for.
    tricks = [
        "secrets/key",            # the plain case
        "./secrets/key",          # a leading '.' segment
        ".//secrets///key",       # and empty segments with it
        "secrets//key",           # a doubled separator
        "a/../secrets/key",       # a traversal that lands back inside
        "a/b/../../secrets/key",  # two of them
        "secrets/./key",          # an interior '.' segment
        "secrets\\key",           # Windows separators on any host
        "/secrets/key",           # leading separator: root-relative
        "\\secrets\\key",         # both at once
        "secrets./key",           # trailing dot on a Windows directory
        "secrets /key",           # trailing space on a Windows directory
        "secrets/key.",           # trailing dot on a Windows filename
        "secrets/key ",           # trailing space on a Windows filename
        "secrets/. ./key",        # a segment of nothing but dots and spaces
        "./a/./../secrets/key.",  # several tricks stacked
    ]
    for t in tricks:
        got = m.match(t)
        assert got["allowed"] is False, (t, got)
        assert got["rule"] == "secrets/*", (t, got)

    # A very long path is matched, not truncated and not refused: one
    # enormous segment, and a deeply nested one that is caught by its
    # ancestor rather than its leaf.
    long_leaf = "secrets/" + ("x" * 8000)
    assert m.match(long_leaf)["rule"] == "secrets/*", "long leaf segment"
    deep = "secrets/" + "/".join(["d{}".format(i) for i in range(300)])
    assert m.match(deep)["rule"] == "secrets/*", "deep path, ancestor hit"
    assert m.match("./" * 500 + "secrets/key")["allowed"] is False

    # Paths with no root-relative form at all fail closed, and say so.
    for bad in ("..", "../x", "a/../..", "a/../../b", "/..",
                "C:\\secrets\\key", "C:/secrets/key", "C:secrets",
                "c:/x", "//host/share/secrets/key",
                "\\\\host\\share\\secrets\\key", 17, None):
        got = m.match(bad)
        assert got["allowed"] is False, (bad, got)
        assert got["decided_by"] == "not-relative", (bad, got)
        assert got["path"] is None, (bad, got)
        assert got["rule"] is None, (bad, got)
    # A bare ".." is refused even with no rules at all: it is not a path
    # this layer can classify, whatever the policy says.
    assert Matcher().match("..")["decided_by"] == "not-relative"
    # "CC:/x" and "1:/x" are not drive qualifiers, they are filenames.
    assert Matcher().match("CC:/x")["allowed"] is True
    assert Matcher().match("1:/x")["allowed"] is True

    # Near misses are NOT denied: the rule must be specific, or it is
    # protecting by accident.
    for ok in ("secretsx/key", "notsecrets/key", "src/secrets.md",
               "sub/secrets/key"):
        assert m.match(ok)["allowed"] is True, ok
    # Faithful gitignore semantics, pinned because it is a footgun:
    # 'secrets/*' covers the CONTENTS of secrets, not the entry itself.
    assert m.match("secrets", is_dir=True)["allowed"] is True
    assert Matcher(deny=["secrets/**"]).match("secrets", is_dir=True)[
        "allowed"] is False
    assert Matcher(deny=["secrets/"]).match("secrets", is_dir=True)[
        "allowed"] is False

    # --- case sensitivity, both settings, on any host --------------------
    cs = Matcher(deny=["secrets/*"], case_sensitive=True)
    ci = Matcher(deny=["secrets/*"], case_sensitive=False)
    assert cs.match("SECRETS/key")["allowed"] is True, "sensitive: no match"
    assert cs.match("secrets/KEY")["allowed"] is False
    for spelling in ("SECRETS/key", "Secrets/Key", "secREts/key",
                     ".\\SECRETS\\KEY."):
        assert ci.match(spelling)["allowed"] is False, spelling
    assert Matcher(deny=["*.PEM"], case_sensitive=False).match(
        "k/id.pem")["allowed"] is False
    assert Matcher(deny=["*.PEM"], case_sensitive=True).match(
        "k/id.pem")["allowed"] is True
    # The default follows the platform, and is stated rather than implied.
    assert Matcher(deny=["x"]).case_sensitive is (os.name != "nt")
    assert default_case_sensitive() is (os.name != "nt")

    # --- deny wins, and the empty-list rules -----------------------------
    both = Matcher(allow=["**"], deny=["secrets/**"])
    got = both.match("secrets/key")
    assert got["allowed"] is False and got["rule"] == "secrets/**", got
    assert both.match("src/a.py")["allowed"] is True
    # Order of the lists cannot change the outcome.
    assert Matcher(allow=["secrets/key"], deny=["secrets/key"]).match(
        "secrets/key")["decided_by"] == "deny"
    assert Matcher(allow=["secrets/*", "**"], deny=["*.pem"]).match(
        "secrets/id.pem")["decided_by"] == "deny"
    # Empty allow list: everything not denied is allowed.
    only_deny = Matcher(deny=["secrets/*"])
    got = only_deny.match("src/main.py")
    assert got["allowed"] is True and got["decided_by"] == "allow-empty", got
    assert got["rule"] is None, got
    # Empty deny list: nothing is denied.
    only_allow = Matcher(allow=["src/**"])
    assert only_allow.match("src/deep/a.py")["decided_by"] == "allow"
    got = only_allow.match("etc/passwd")
    assert got["allowed"] is False and got["decided_by"] == "no-allow-match"
    assert got["rule"] is None, got
    # No policy at all is an open door, not a silently closed one.
    wide = Matcher()
    assert wide.match("anything/at/all")["allowed"] is True
    assert wide.match("")["allowed"] is True
    assert wide.allowed("x/y") is True and only_allow.allowed("x/y") is False
    # With an allow list, the root itself matches nothing and is refused.
    assert only_allow.match(".")["decided_by"] == "no-allow-match"

    # --- gitignore-flavoured pattern semantics ---------------------------
    # '*' does not cross a separator (checked where no ancestor rule can
    # mask the result).
    star = Matcher(deny=["x/*/y"])
    assert star.match("x/m/y")["allowed"] is False
    assert star.match("x/m/n/y")["allowed"] is True, "'*' crossed a separator"
    # '**' does cross separators, in every position.
    dstar = Matcher(deny=["x/**/y"])
    for p in ("x/y", "x/m/y", "x/m/n/o/y"):
        assert dstar.match(p)["allowed"] is False, p
    assert dstar.match("x/m/y_other")["allowed"] is True
    assert Matcher(deny=["**/cache"]).match("a/b/cache", is_dir=True)[
        "allowed"] is False
    assert Matcher(deny=["**/cache"]).match("cache", is_dir=True)[
        "allowed"] is False
    assert Matcher(deny=["data/**"]).match("data/a/b/c")["allowed"] is False
    assert Matcher(deny=["**"]).match("a/b/c")["allowed"] is False
    # '?' is exactly one non-separator character.
    q = Matcher(deny=["/a?.txt"])
    assert q.match("ab.txt")["allowed"] is False
    assert q.match("abc.txt")["allowed"] is True
    assert q.match("a/.txt")["allowed"] is True, "'?' crossed a separator"
    # Character classes, plain and negated.
    cls = Matcher(deny=["/log[0-9].txt"], case_sensitive=True)
    assert cls.match("log7.txt")["allowed"] is False
    assert cls.match("logx.txt")["allowed"] is True
    neg = Matcher(deny=["/log[!0-9].txt"], case_sensitive=True)
    assert neg.match("logx.txt")["allowed"] is False
    assert neg.match("log7.txt")["allowed"] is True
    # A leading '/' anchors to the root; no interior '/' matches any depth.
    anchored = Matcher(deny=["/build"])
    assert anchored.match("build", is_dir=True)["allowed"] is False
    assert anchored.match("sub/build", is_dir=True)["allowed"] is True
    floating = Matcher(deny=["build"])
    assert floating.match("sub/deep/build", is_dir=True)["allowed"] is False
    assert Matcher(deny=["*.pem"]).match("a/b/c/id.pem")["allowed"] is False
    assert Matcher(deny=["/*.pem"]).match("a/id.pem")["allowed"] is True
    # A trailing '/' matches directories only, and nothing here consults
    # the filesystem to decide which a path is.
    dironly = Matcher(deny=["build/"])
    assert dironly.match("build", is_dir=True)["allowed"] is False
    assert dironly.match("build", is_dir=False)["allowed"] is True
    assert dironly.match("build/")["allowed"] is False, "trailing separator"
    assert dironly.match("build\\")["allowed"] is False
    assert dironly.match("build/out.o")["allowed"] is False, "ancestor is dir"
    assert dironly.match("buildfile")["allowed"] is True
    # Allowing a directory allows what is under it.
    assert Matcher(allow=["src/"]).match("src/deep/a.py")["allowed"] is True

    # --- malformed patterns raise at CONSTRUCTION ------------------------
    # A deny rule that quietly matched nothing would be the worst failure
    # this module could have, so none of these may construct.
    for bad in ("", "   ", " a", "a ", "\t", "/", "//", "///", "!secrets",
                "!", "#comment", "secrets\\key", "\\", "a//b", "a/b//c",
                "a/../b", "a/./b", "..", ".", "a[b", "a[]b", "a[!]b",
                "a[/]b", "a[a/b]c", "a**b", "**a", "a**", "C:/x", "c:x",
                "//host/share", "a\x00b", "a\nb", "a\x01b",
                "**/a/**/b/**/c/**/d", "x" * (MAX_PATTERN_LENGTH + 1)):
        for kwargs in ({"deny": [bad]}, {"allow": [bad]}):
            try:
                Matcher(**kwargs)
                raise AssertionError(
                    "malformed pattern accepted: {!r}".format(bad))
            except PatternError:
                pass
    for bad_type in (None, 1, b"x", ("a",), object()):
        if bad_type is None:
            continue  # None means "no list", which is legitimate
        try:
            Matcher(deny=[bad_type])
            raise AssertionError("non-string pattern accepted")
        except PatternError:
            pass
    # A bare string instead of a list would compile one rule per
    # character and protect nothing, so it is refused outright.
    for kwargs in ({"deny": "secrets/*"}, {"allow": "src/**"}):
        try:
            Matcher(**kwargs)
            raise AssertionError("a bare string was accepted as a list")
        except PatternError:
            pass
    # The interior '**' cap: at the cap compiles, past it raises.
    at_cap = Matcher(deny=["a/**/b/**/c"])
    assert at_cap.match("a/1/b/2/c")["allowed"] is False
    assert at_cap.match("a/b/c")["allowed"] is False
    try:
        Matcher(deny=["a/**/b/**/c/**/d"])
        raise AssertionError("the interior '**' cap did not fire")
    except PatternError as exc:
        assert "interior" in str(exc), str(exc)
    # And the exotic-but-legal patterns must NOT raise.
    ok_patterns = ["*", "**", "?", "a/**", "**/a", "a/**/b", "/a", "a/",
                   "[!0-9]*.log", "a?b", "secrets/*", "**/*.pem", "a/b/c",
                   "[abc]x", "x" * MAX_PATTERN_LENGTH]
    legal = Matcher(deny=ok_patterns, case_sensitive=True)
    assert len(legal.deny) == len(ok_patterns)
    # Non-vacuousness: every one of those rules can actually fire, so
    # "compiles" is not being mistaken for "works".
    for rule in legal.deny:
        assert rule.regex.match(
            rule.pattern.strip("/").replace("**", "zz").replace("*", "z")
            .replace("?", "z").replace("[!0-9]", "z").replace("[abc]", "a")
        ) is not None or rule.pattern in ("**/a", "a/**/b"), rule.pattern
    assert Matcher(deny=["**/a"]).match("q/w/a")["allowed"] is False
    assert Matcher(deny=["a/**/b"]).match("a/q/b")["allowed"] is False

    # --- a realistic policy, end to end ---------------------------------
    policy = Matcher(
        allow=["src/**", "tests/**", "*.md"],
        deny=["**/secrets/**", "*.pem", "**/.env", ".git/"],
        case_sensitive=False)
    for allowed_path in ("src/app/main.py", "tests/test_x.py", "README.md",
                         "src/a/b/c/deep.py", "./src/../src/main.py"):
        assert policy.match(allowed_path)["allowed"] is True, allowed_path
    for denied_path in ("src/secrets/key", "src/certs/id.pem", "src/.env",
                        "src\\secrets\\key", "src/secrets./key",
                        "./src/a/../secrets/key", ".git/config",
                        "etc/passwd", "build/out.o", "/src/secrets/key"):
        assert policy.match(denied_path)["allowed"] is False, denied_path
    # A denied path names its rule, so an operator log can say why.
    assert policy.match("src/certs/id.pem")["rule"] == "*.pem"
    assert policy.match("src/secrets/key")["rule"] == "**/secrets/**"

    print("hearth-pathmatch self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-pathmatch",
        description="Judge paths against allow/deny patterns. Exits nonzero "
                    "if any path is denied.")
    p.add_argument("--allow", action="append", default=[], metavar="PATTERN",
                   help="allow pattern (repeatable); empty means allow "
                        "everything not denied")
    p.add_argument("--deny", action="append", default=[], metavar="PATTERN",
                   help="deny pattern (repeatable); deny always wins")
    p.add_argument("--path", action="append", default=[], metavar="PATH",
                   help="path to judge (repeatable)")
    p.add_argument("--dir", action="store_true",
                   help="treat each path as a directory (for 'name/' rules)")
    p.add_argument("--case-sensitive", dest="case", action="store_true",
                   default=None, help="force case-sensitive matching")
    p.add_argument("--case-insensitive", dest="case", action="store_false",
                   help="force case-insensitive matching")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.path:
        p.error("nothing to do (use --self-test or --path)")
    try:
        matcher = Matcher(allow=a.allow, deny=a.deny, case_sensitive=a.case)
    except PatternError as exc:
        print("[hearth-pathmatch] bad pattern: {}".format(exc),
              file=sys.stderr)
        return 2
    worst = 0
    for raw in a.path:
        v = matcher.match(raw, is_dir=a.dir)
        print("{} {}: {}".format("allow" if v["allowed"] else "DENY",
                                 raw, v["reason"]))
        if not v["allowed"]:
            worst = 1
    return worst


if __name__ == "__main__":
    sys.exit(main())
