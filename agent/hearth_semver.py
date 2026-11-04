#!/usr/bin/env python3
"""hearth semver: the version ordering the updater can be trusted with.

hearth_update.py refuses a release that is not strictly newer than the
installed build, because a validly signed OLD release is an attack and not
an edge case: whoever can serve files can serve yesterday's genuinely
signed build with yesterday's known vulnerability, and the signature on it
is real. That refusal is only as good as the comparison underneath it, and
the comparison is the part that is easy to get wrong.

Compared as strings, "0.10.0" sorts BELOW "0.9.0", because "1" is less than
"9" at the third character. Every consequence of that is bad, in both
directions. A feed offering 0.10.0 to an installed 0.9.0 looks like a
rollback and gets refused, so the user is silently stranded on the
vulnerable build while the updater reports that all is well. And an
attacker offering the old 0.9.0 to an installed 0.10.0 looks like an
upgrade and gets accepted. One character of lexicographic ordering is the
whole of the downgrade protection.

So the ordering lives here, on its own, with a self-test that asserts the
exact cases a string comparison gets wrong.

What this module does
---------------------
Full semver 2.0.0 ordering: MAJOR.MINOR.PATCH, an optional prerelease and
optional build metadata, with the rules that actually decide outcomes:

  * BUILD METADATA IS IGNORED for ordering. 1.0.0+a and 1.0.0+b are the
    same version. Treating build metadata as significant would let whoever
    writes a manifest mint an unbounded supply of "newer" 1.0.0s.
  * A PRERELEASE SORTS BEFORE ITS RELEASE: 1.0.0-rc.1 is older than 1.0.0.
    Somebody on 1.0.0 is never walked back onto a release candidate.
  * NUMERIC PRERELEASE IDENTIFIERS COMPARE NUMERICALLY while alphanumeric
    ones compare as ASCII, so 1.0.0-alpha.2 is older than 1.0.0-alpha.10,
    which is the other ordering the obvious string compare gets backwards.
  * A NUMERIC IDENTIFIER ALWAYS SORTS BELOW AN ALPHANUMERIC ONE, and a
    prerelease sorts below one carrying the same identifiers plus more.

A leading "v" is tolerated, because tags get written both ways and a tag
that differs from a manifest by one character must not change a security
decision.

What this module deliberately does NOT do
-----------------------------------------
  * IT DOES NOT GUESS. parse() raises on anything it cannot read honestly:
    an empty string, "1.0", "1.0.0.0", a negative component, a leading zero
    in a numeric component, a non-numeric major. There is no lenient mode,
    no coercion of "1.0" into "1.0.0", no best effort.
  * IT DOES NOT READ OR WRITE ANYTHING. No network, no files, no
    environment, no clock. Two strings in, a verdict out, the same answer
    every time. That is what makes it testable to the point of boredom, and
    it is why the self-test runs fully offline.
  * IT DOES NOT DECIDE THE REST OF THE UPDATE. Signatures, the manifest
    expiry, the persisted downgrade floor, the channel, the artifact digest:
    all of that stays in hearth_update.py. This module answers exactly one
    question, which is which of two version strings is older.
  * IT DOES NOT IMPORT ANYTHING OUTSIDE THE STANDARD LIBRARY. Not
    `packaging`: that is a dependency, which is a supply chain, taken on
    for an ordering that fits on a page. A module whose whole premise is
    that code arriving from elsewhere has to earn its way in does not get
    to be the place where a convenience import is fine.

Why fail-closed is the only safe default for an updater
-------------------------------------------------------
An unparseable version is not a formatting problem, it is a missing fact.
The question being asked is "is this candidate strictly newer than what is
installed", and when one side cannot be read there is no answer to it.
There is only a guess, and a guess here installs code.

Guessing has a direction and both directions lose. Guess "newer" and an
attacker writes a version string the parser does not understand and has
whatever they like accepted. Guess "older" quietly and the user stops
receiving security updates while the interface says they are current. So
parse() raises, compare() lets that propagate, and eligible() turns the
failure into an explicit refusal carrying a reason a human can act on.
Refusing to update is a visible and recoverable state: somebody can fetch
an installer by hand. Applying the wrong build is neither.

Standard library only. Offline. No side effects.
"""

import argparse
import contextlib
import io
import sys

#: Exactly the characters semver 2.0.0 allows in a prerelease or build
#: identifier. Checked by membership rather than with str.isalnum(), which
#: is true for non-ASCII digits and letters too.
_IDENT_CHARS = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-")
_DIGITS = frozenset("0123456789")

_CORE_FIELDS = ("major", "minor", "patch")

#: A numeric component longer than this is not a version, it is somebody
#: seeing what happens. Bounded here so nothing downstream has to wonder.
MAX_COMPONENT_DIGITS = 9

#: How a release and a prerelease are kept apart inside the comparable
#: tuple: the release gets the higher rank, so 1.0.0 sorts above every
#: 1.0.0-anything without the two prerelease lists ever being compared
#: against each other's absence.
_RELEASE_RANK = 1
_PRERELEASE_RANK = 0


class VersionError(ValueError):
    """A version string this module refuses to interpret.

    A ValueError because that is what callers already catch around a parse,
    and its own class because the updater has to be able to tell "I could
    not read this" apart from "I read this and it is older".
    """


def _is_number(text):
    return bool(text) and all(ch in _DIGITS for ch in text)


def _number(text, field, raw):
    """One core component as an int, or raise with the reason."""
    if not text:
        raise VersionError("{!r}: the {} is empty".format(raw, field))
    if not _is_number(text):
        raise VersionError(
            "{!r}: the {} {!r} is not a decimal number".format(
                raw, field, text))
    if len(text) > 1 and text[0] == "0":
        raise VersionError(
            "{!r}: the {} {!r} has a leading zero, so two different strings "
            "would mean one version".format(raw, field, text))
    if len(text) > MAX_COMPONENT_DIGITS:
        raise VersionError(
            "{!r}: the {} has more than {} digits".format(
                raw, field, MAX_COMPONENT_DIGITS))
    return int(text)


def _check_identifiers(text, kind, raw, numeric_rules):
    """Validate a dot-separated prerelease or build metadata string."""
    for ident in text.split("."):
        if not ident:
            raise VersionError(
                "{!r}: the {} has an empty identifier".format(raw, kind))
        for ch in ident:
            if ch not in _IDENT_CHARS:
                raise VersionError(
                    "{!r}: {!r} is not allowed in a {} identifier".format(
                        raw, ch, kind))
        if (numeric_rules and _is_number(ident) and len(ident) > 1
                and ident[0] == "0"):
            raise VersionError(
                "{!r}: the {} identifier {!r} has a leading zero".format(
                    raw, kind, ident))
    return True


def _identifier_key(ident):
    """One prerelease identifier as a comparable triple.

    Numeric identifiers compare numerically and alphanumeric ones compare
    as ASCII, and a numeric one always sorts below an alphanumeric one. A
    triple with the class first does all three at once, and keeps an int
    from ever being compared against a str.
    """
    if _is_number(ident):
        return (0, int(ident), "")
    return (1, 0, ident)


def parse(text):
    """`text` as a comparable tuple, or raise VersionError.

    The tuple is (major, minor, patch, rank, prerelease identifiers), where
    rank separates a release from its prereleases and the identifiers are
    the triples described in _identifier_key. Build metadata is validated
    and then dropped, because it carries no ordering. Plain tuple
    comparison on the result is the full semver 2.0.0 order, which also
    means a list of versions can be sorted with key=parse.
    """
    if not isinstance(text, str):
        raise VersionError("a version must be a string, got {!r}".format(text))
    raw = text.strip()
    if not raw:
        raise VersionError("a version is required, and this one is empty")

    body = raw[1:] if raw[0] in ("v", "V") else raw
    if not body:
        raise VersionError("{!r} is a prefix, not a version".format(raw))

    # Build metadata first: it is allowed to contain hyphens, so splitting
    # it off before the prerelease is what keeps the two from mixing.
    if "+" in body:
        body, build = body.split("+", 1)
        _check_identifiers(build, "build metadata", raw, numeric_rules=False)

    prerelease = None
    if "-" in body:
        body, prerelease = body.split("-", 1)
        _check_identifiers(prerelease, "prerelease", raw, numeric_rules=True)

    parts = body.split(".")
    if len(parts) != 3:
        raise VersionError(
            "{!r} is not MAJOR.MINOR.PATCH: it has {} dotted component(s), "
            "not 3".format(raw, len(parts)))
    numbers = tuple(_number(parts[i], _CORE_FIELDS[i], raw) for i in range(3))

    if prerelease is None:
        return numbers + (_RELEASE_RANK, ())
    identifiers = tuple(_identifier_key(i) for i in prerelease.split("."))
    return numbers + (_PRERELEASE_RANK, identifiers)


def compare(a, b):
    """-1 when `a` is older than `b`, 0 when they are the same version, 1
    when `a` is newer.

    Raises VersionError when either side is unreadable, rather than ranking
    a version nobody could read. eligible() is where that becomes a
    refusal with a reason.
    """
    key_a = parse(a)
    key_b = parse(b)
    if key_a < key_b:
        return -1
    return 0 if key_a == key_b else 1


def newer(a, b):
    """True when `a` is strictly newer than `b`. Raises on unreadable input."""
    return compare(a, b) > 0


def eligible(installed, candidate, min_version=None):
    """Whether `candidate` may be applied over `installed`.

    Returns a verdict dict: `allowed` (bool), `code` (a stable token for
    callers to branch on), `reason` (a sentence for a person), and the three
    inputs as they were given. It never raises: an unreadable version on
    either side, or an unreadable floor, is a refusal with a reason,
    because this is the decision that installs code and a guess at it has
    no safe direction.

    Refused: a candidate older than what is installed (a rollback, however
    validly it is signed), a candidate equal to what is installed (there is
    nothing to apply, and build metadata does not make a new version), and a
    candidate whose stated `min_version` is above what is installed, which
    is the release saying it cannot be applied over this build directly.
    `min_version=None` means the candidate states no floor.
    """
    verdict = {
        "allowed": False,
        "code": "",
        "reason": "",
        "installed": installed,
        "candidate": candidate,
        "min_version": min_version,
    }

    try:
        installed_key = parse(installed)
    except VersionError as exc:
        verdict["code"] = "installed-unparseable"
        verdict["reason"] = (
            "Hearth cannot tell which version it is running ({}), so it will "
            "not apply an update over it.".format(exc))
        return verdict

    try:
        candidate_key = parse(candidate)
    except VersionError as exc:
        verdict["code"] = "candidate-unparseable"
        verdict["reason"] = (
            "the offered release does not state a version this build can "
            "read ({}), and an unreadable version is refused rather than "
            "guessed at.".format(exc))
        return verdict

    if candidate_key < installed_key:
        verdict["code"] = "downgrade"
        verdict["reason"] = (
            "the offered release {} is older than the installed {}. That is "
            "a rollback, not an update, and it is refused.".format(
                candidate, installed))
        return verdict

    if candidate_key == installed_key:
        verdict["code"] = "already-installed"
        verdict["reason"] = (
            "the offered release {} is the version already installed ({}), "
            "so there is nothing to apply.".format(candidate, installed))
        return verdict

    if min_version is not None:
        try:
            floor_key = parse(min_version)
        except VersionError as exc:
            verdict["code"] = "min-version-unparseable"
            verdict["reason"] = (
                "the offered release states a minimum version this build "
                "cannot read ({}), so it is refused.".format(exc))
            return verdict
        if installed_key < floor_key:
            verdict["code"] = "below-minimum"
            verdict["reason"] = (
                "{} can only be installed over {} or newer, and this is {}. "
                "Install a fresh download instead.".format(
                    candidate, min_version, installed))
            return verdict

    verdict["allowed"] = True
    verdict["code"] = "newer"
    verdict["reason"] = "{} is newer than the installed {}.".format(
        candidate, installed)
    return verdict


def _self_test():
    # The case that is the entire reason this module exists: compared as
    # strings, the newer version loses.
    assert "0.10.0" < "0.9.0", "as strings, 0.10.0 sorts below 0.9.0"
    assert compare("0.9.0", "0.10.0") == -1, "0.9.0 < 0.10.0"
    assert compare("0.10.0", "0.10.1") == -1, "0.10.0 < 0.10.1"
    assert compare("0.9.0", "0.10.1") == -1, "0.9.0 < 0.10.1"
    assert newer("0.10.0", "0.9.0"), "0.10.0 is newer than 0.9.0"
    assert not newer("0.9.0", "0.10.0"), "0.9.0 is not newer than 0.10.0"
    assert compare("1.9.0", "1.10.0") == -1, "the same trap one level up"
    assert compare("9.0.0", "10.0.0") == -1, "and in the major"

    # Core ordering, and equality with itself.
    assert compare("1.2.3", "1.2.3") == 0
    assert compare("0.0.1", "0.1.0") == -1
    assert compare("1.2.3", "1.3.0") == -1
    assert compare("1.3.0", "2.0.0") == -1
    assert compare("2.0.0", "1.9.9") == 1
    assert not newer("1.2.3", "1.2.3")

    # Build metadata is ignored for ordering, entirely.
    assert compare("1.0.0+build.1", "1.0.0+build.2") == 0
    assert compare("1.0.0", "1.0.0+20260101") == 0
    assert compare("1.0.0+zzz", "1.0.0") == 0
    assert compare("1.0.0-rc.1+a", "1.0.0-rc.1+b") == 0
    assert parse("1.0.0+zzz") == parse("1.0.0")
    assert compare("1.0.0+zzz", "1.0.1+aaa") == -1
    assert not newer("1.0.0+rebuild", "1.0.0")

    # A prerelease sorts before its release.
    assert compare("1.0.0-rc.1", "1.0.0") == -1
    assert compare("1.0.0", "1.0.0-rc.1") == 1
    assert compare("1.0.0-rc.1", "0.9.9") == 1

    # Numeric prerelease identifiers compare numerically; alphanumeric ones
    # compare as ASCII; a numeric one always sorts below an alphanumeric one.
    assert compare("1.0.0-alpha.2", "1.0.0-alpha.10") == -1
    assert compare("1.0.0-2", "1.0.0-10") == -1
    assert compare("1.0.0-1", "1.0.0-alpha") == -1
    assert compare("1.0.0-alpha", "1.0.0-1") == 1
    assert compare("1.0.0-Z", "1.0.0-a") == -1, "ASCII, so Z is below a"
    assert compare("1.0.0-alpha", "1.0.0-alpha.1") == -1, "fewer sorts lower"

    # The ordering chain from the semver 2.0.0 specification itself.
    chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta",
             "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1",
             "1.0.0"]
    for low, high in zip(chain, chain[1:]):
        assert compare(low, high) == -1, (low, high)
        assert compare(high, low) == 1, (high, low)
        assert newer(high, low), (high, low)

    # A leading v is tolerated and changes nothing.
    assert parse("v1.2.3") == parse("1.2.3")
    assert parse("V1.2.3") == parse("1.2.3")
    assert compare("v0.9.0", "0.10.0") == -1
    assert compare("V1.0.0-rc.1", "v1.0.0") == -1
    assert compare("v1.0.0", "1.0.0") == 0

    # parse() returns something sortable, and the order is total.
    ordered = ["0.0.1", "0.1.0", "0.9.0", "0.10.0", "0.10.1",
               "1.0.0-alpha", "1.0.0-alpha.2", "1.0.0-alpha.10",
               "1.0.0-rc.1", "1.0.0", "1.0.1", "1.9.0", "1.10.0", "2.0.0"]
    for i, low in enumerate(ordered):
        for j, high in enumerate(ordered):
            want = (i > j) - (i < j)
            assert compare(low, high) == want, (low, high, want)
    assert isinstance(parse("1.0.0"), tuple)
    assert sorted(reversed(ordered), key=parse) == ordered

    # Refused outright rather than guessed at.
    refused = (
        "",                 # nothing at all
        "   ",              # nothing at all, with whitespace
        "1.0",              # too few components
        "1",                # too few components
        "1.0.0.0",          # too many components
        "1.2.-3",           # a negative component
        "-1.0.0",           # a negative component
        "1.01.0",           # a leading zero
        "01.0.0",           # a leading zero
        "1.0.00",           # a leading zero
        "x.0.0",            # a non-numeric major
        "latest",           # a non-numeric major
        "1.y.0",            # a non-numeric minor
        "v",                # a prefix and nothing else
        "1.0.0-",           # an empty prerelease
        "1.0.0-01",         # a leading zero in a prerelease identifier
        "1.0.0-rc..1",      # an empty prerelease identifier
        "1.0.0+",           # empty build metadata
        "1.0.0+build+2",    # a stray plus in build metadata
        "1.0.0-rc!1",       # a character semver does not allow
        "1.0.0-rc 1",       # a character semver does not allow
        "1.0.0_1",          # a character semver does not allow
        "1 0 0",            # not dotted at all
        "1.0.0000000000",   # an absurdly long component
        None,               # not a string
        100,                # not a string
        (1, 0, 0),          # not a string
    )
    for bad in refused:
        try:
            parse(bad)
        except VersionError as exc:
            assert str(exc), "a refusal must say why: {!r}".format(bad)
        else:
            raise AssertionError("parse({!r}) should have refused".format(bad))
    assert issubclass(VersionError, ValueError)
    # compare() and newer() let the refusal through rather than ranking a
    # version nobody could read.
    for args in (("1.0", "1.0.0"), ("1.0.0", "1.0")):
        try:
            compare(*args)
        except VersionError:
            pass
        else:
            raise AssertionError("compare{!r} should have refused".format(args))

    # eligible(): the updater's actual question.
    good = eligible("0.9.0", "0.10.0")
    assert good["allowed"] is True, good
    assert good["code"] == "newer", good
    assert good["installed"] == "0.9.0" and good["candidate"] == "0.10.0"

    roll = eligible("0.10.0", "0.9.0")
    assert roll["allowed"] is False and roll["code"] == "downgrade", roll
    assert "0.9.0" in roll["reason"] and "0.10.0" in roll["reason"], roll

    same = eligible("1.0.0", "1.0.0")
    assert same["allowed"] is False, same
    assert same["code"] == "already-installed", same
    # build metadata does not make a new version, so neither does it make an
    # eligible one
    assert eligible("1.0.0", "1.0.0+rebuild.2")["code"] == "already-installed"
    # and a release candidate is never offered over its release
    assert eligible("1.0.0", "1.0.0-rc.2")["code"] == "downgrade"
    assert eligible("1.0.0-rc.1", "1.0.0")["allowed"] is True
    assert eligible("1.0.0-rc.1", "1.0.0-rc.2")["allowed"] is True

    # min_version is the floor the candidate states for the build it is
    # applied over, not a floor on the candidate itself.
    low = eligible("1.0.0", "3.0.0", min_version="2.0.0")
    assert low["allowed"] is False and low["code"] == "below-minimum", low
    assert "2.0.0" in low["reason"], low
    assert eligible("2.0.0", "3.0.0", min_version="2.0.0")["allowed"] is True
    assert eligible("2.1.0", "3.0.0", min_version="2.0.0")["allowed"] is True
    assert eligible("1.0.0", "3.0.0", min_version=None)["allowed"] is True
    assert eligible("1.0.0", "3.0.0")["allowed"] is True
    assert eligible("v2.0.0", "v3.0.0", min_version="v2.0.0")["allowed"] is True
    # a prerelease of the floor is below the floor, so it is refused too
    assert eligible("2.0.0-rc.1", "3.0.0",
                    min_version="2.0.0")["code"] == "below-minimum"

    # Fail closed, on either side and on the floor, and never by raising.
    assert eligible("", "1.0.0")["code"] == "installed-unparseable"
    assert eligible("1.0.0", "two")["code"] == "candidate-unparseable"
    assert eligible("1.0.0", "2.0.0",
                    min_version="1.0")["code"] == "min-version-unparseable"
    junk = (None, "", "   ", "1.0", "1.0.0.0", 7, "latest", "0.9")
    for item in junk:
        verdicts = [eligible(item, item),
                    eligible("1.0.0", item),
                    eligible(item, "2.0.0")]
        # None is the one value that is not junk in the floor: it is how a
        # release says it states no minimum at all.
        if item is not None:
            verdicts.append(eligible("1.0.0", "2.0.0", min_version=item))
        for verdict in verdicts:
            assert verdict["allowed"] is False, (item, verdict)
            assert verdict["code"], (item, verdict)
            assert verdict["reason"], (item, verdict)

    # Every verdict says something, and carries the inputs it is about.
    for verdict in (good, roll, same, low):
        assert verdict["reason"].endswith("."), verdict
        assert set(verdict) == {"allowed", "code", "reason", "installed",
                                "candidate", "min_version"}, verdict

    # The command line agrees with the library, including its exit codes,
    # which are what a caller in a shell script actually branches on. Its
    # output is captured rather than printed so that the one line this
    # self-test emits stays the one line it promises.
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        codes = [main(["--compare", "0.9.0", "0.10.0"]),
                 main(["--compare", "1.0", "1.0.0"]),
                 main(["--eligible", "0.9.0", "0.10.0"]),
                 main(["--eligible", "0.10.0", "0.9.0"]),
                 main(["--eligible", "1.0.0", "1.0.0"]),
                 main(["--eligible", "1.0.0", "3.0.0",
                       "--min-version", "2.0.0"]),
                 main(["--eligible", "2.0.0", "3.0.0",
                       "--min-version", "2.0.0"])]
    assert codes == [0, 2, 0, 1, 1, 1, 0], codes
    printed = quiet.getvalue().splitlines()
    assert printed[0] == "-1", printed
    assert printed[2].startswith("allowed: "), printed
    assert printed[3].startswith("refused: "), printed

    print("hearth-semver self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-semver",
        description="semver 2.0.0 ordering for Hearth's updater")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"),
                   help="print -1, 0 or 1 for A against B")
    p.add_argument("--eligible", nargs=2, metavar=("INSTALLED", "CANDIDATE"),
                   help="decide whether CANDIDATE may be applied over "
                        "INSTALLED; exits nonzero when it may not")
    p.add_argument("--min-version", default=None,
                   help="the candidate's stated minimum installed version")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.compare:
        try:
            print(compare(a.compare[0], a.compare[1]))
        except VersionError as exc:
            print("refused: {}".format(exc), file=sys.stderr)
            return 2
        return 0
    if a.eligible:
        verdict = eligible(a.eligible[0], a.eligible[1], a.min_version)
        print("{}: {}".format("allowed" if verdict["allowed"] else "refused",
                              verdict["reason"]))
        return 0 if verdict["allowed"] else 1
    p.error("nothing to do (use --self-test, --compare or --eligible)")


if __name__ == "__main__":
    sys.exit(main())
