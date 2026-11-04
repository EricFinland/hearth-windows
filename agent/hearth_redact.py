#!/usr/bin/env python3
"""hearth transcript redaction: the eraser behind the secret scanner.

agent/hearth_secrets.py answers one question, "is a credential in motion
right now?", and deliberately stops there. It reports and never rewrites,
because a heuristic that can be wrong must not stand between the model and
the user's own files: a false positive that silently mangled a write would
corrupt real work, which is worse than a missed detection. That is the
right call for a write that is about to land on disk.

It is the wrong call for the other half of the problem. A Hearth run also
produces artefacts whose entire purpose is to leave the machine:

  - a run transcript pasted into a GitHub issue,
  - a bug report assembled from the last few hundred agent turns,
  - an exported log attached to a support thread,
  - a swarm trace handed to somebody else to read.

Those are the moments where a token the agent merely *saw* becomes a token
that is *published*, and no operator realistically audits five thousand
lines by eye first. Here the trade-off inverts. Mangling a quoted secret in
a document that is on its way out is the desired outcome, and the cost of a
false positive is one unreadable word in a log rather than a broken source
file. So this module does the thing hearth_secrets.py refuses to do: it
rewrites the text.

What it covers: AWS access key ids, GitHub tokens (ghp_/gho_/ghu_/ghs_/
ghr_), "Authorization: Bearer <token>" headers, PEM private key blocks,
URLs carrying inline credentials (https://user:pass@host), email
addresses, KEY=value lines whose key name reads as a secret
(token/secret/password/api_key and friends), and loose 32+ character
high-entropy hex or base64-ish runs. Callers can add their own rules
through extra_patterns.

Three properties are load-bearing, because an operator has to be able to
run this over a log twice, diff the result, and still read it:

  Idempotent    Every replacement is a bracketed placeholder, and any
                placeholder already present in the input is treated as a
                protected region that no rule may match inside. Redacting
                redacted text is therefore a no-op and reports no
                findings, so a transcript can pass through a pipeline more
                than once without turning into nested brackets.
  Bounded       A placeholder is at most PLACEHOLDER_MAX_LEN characters no
                matter how long the secret was, and each match collapses
                to exactly one placeholder. A 4 KB PEM block becomes a
                single short token on a single line, so the output can
                only shrink or grow by a fixed amount per finding, never
                unboundedly.
  Correlatable  The placeholder carries a short digest of the secret, so
                the same value appearing in twenty places reads as the
                same value twenty times without ever revealing it. That is
                usually the whole diagnostic point: "the key in the error
                is the key from the config".

What this module deliberately does NOT do:

  - It does not decide policy. It never refuses an export, never blocks a
    tool call, and never touches a file in place. Callers pass text in and
    get text out; the layer above owns what happens next.
  - It does not pretend to be a boundary. A redactor is a last line of
    courtesy, not a containment mechanism. A secret that was in a
    transcript was already read by a model and already on disk; removing
    it from an export does not un-leak it, and the honest response to a
    real exposure is still rotation.
  - It does not treat the digest as a secret-preserving commitment. Six
    hex characters of a domain-separated SHA-256 are not reversible for a
    high-entropy key, but a weak human password is dictionary-attackable
    from its digest. Correlation was judged worth more than that residual
    risk for logs that are being shared anyway; a caller who disagrees
    should strip the suffix before publishing.
  - It does not parse formats. There is no JSON, YAML, or HTTP parser
    here, only line and token level rules, because the input is a
    transcript: interleaved prose, tool output, and fragments of twenty
    file formats, none of which parse as a whole.
  - It does not try to catch everything. Base64 containing "/" is matched
    only where a 32 character run survives the split, since treating "/"
    as token material redacted ordinary POSIX paths and made logs
    unreadable. A PEM block missing its END line is not collapsed either,
    since anchoring to end-of-input would let one stray BEGIN marker
    swallow the rest of the document; its body lines are still caught by
    the high-entropy rule.
  - It does not do network or clock work. Redaction is pure, so the same
    input always produces the same output, which is what makes the
    self-test meaningful and the result diffable.

File reading is injected (read_fn / write_fn on main) so the CLI is
testable without a filesystem. Standard library only.
"""

import argparse
import collections
import hashlib
import io
import json
import math
import re
import sys

__all__ = ["redact", "redact_dict", "fingerprint", "main"]

FINGERPRINT_LEN = 6
MAX_KIND_LEN = 24
# "[redacted:" + kind + ":" + digest + "]"
PLACEHOLDER_MAX_LEN = 10 + MAX_KIND_LEN + 1 + FINGERPRINT_LEN + 1
# Domain separation so a digest from this module cannot be compared against
# a bare sha256 of the same value computed somewhere else.
FINGERPRINT_DOMAIN = b"hearth-redact-v1:"

# Matches what _placeholder emits. Anything matching this in the input is a
# region an earlier pass already cleaned, and is protected from every rule.
PLACEHOLDER_RE = re.compile(r"\[redacted:[a-z][a-z0-9-]{0,%d}:[0-9a-f]{%d}\]"
                            % (MAX_KIND_LEN - 1, FINGERPRINT_LEN))
KIND_RE = re.compile(r"^[a-z][a-z0-9-]{0,%d}$" % (MAX_KIND_LEN - 1))

# Key names that read as "this value is a credential". Compared against the
# key lowercased with "-" folded to "_", so apiKey, API-KEY and api_key all
# land on the same rule.
SECRET_NAME_WORDS = (
    "token", "secret", "password", "passwd", "pwd", "api_key", "apikey",
    "access_key", "accesskey", "private_key", "privatekey", "credential",
    "authorization", "session_key", "passphrase", "client_secret",
)

# Values that are references or masks rather than credentials. Redacting
# "$API_KEY" or "***" throws away information and adds none.
ENV_NOISE_RE = re.compile(
    r"^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
    r"|%[A-Za-z_][A-Za-z0-9_]*%"
    r"|\*+|x+|X+|<[^>]*>|\.\.\.|none|null|true|false|changeme)$")

HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
MIN_TOKEN_LEN = 32
# A random base64 run of this length sits near 5 bits per character; the
# thresholds are set low enough to keep real tokens and high enough to drop
# padding, repeated characters, and long identifiers.
MIN_ENTROPY_BITS = 3.0
MIN_HEX_ENTROPY_BITS = 2.0

PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"[\s\S]*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----")
URL_CRED_RE = re.compile(r"(?<=://)([^/\s:@]+:[^/\s@]*)(?=@)")
BEARER_RE = re.compile(r"Bearer\s+([A-Za-z0-9\-._~+/=]{8,})", re.IGNORECASE)
AWS_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:AKIA|ASIA|ABIA|ACCA|AIDA|AROA|AGPA|AIPA|ANPA|ANVA)"
    r"[0-9A-Z]{16})(?![A-Za-z0-9])")
GITHUB_RE = re.compile(
    r"(?<![A-Za-z0-9_])((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,})"
    r"(?![A-Za-z0-9])")
ENV_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?[\"']?"
    r"([A-Za-z_][A-Za-z0-9_\-]*)"
    r"[\"']?[ \t]*[:=][ \t]*"
    r"[\"']?(\S[^\r\n]*?)[\"']?[ \t\r,;]*$",
    re.MULTILINE)
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
    r"(?![A-Za-z0-9.-])")
TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9+=_-])([A-Za-z0-9+=_-]{%d,})(?![A-Za-z0-9+=_-])"
    % MIN_TOKEN_LEN)

_Rule = collections.namedtuple("_Rule", "kind regex group check")


def _entropy(value):
    """Shannon entropy of value in bits per character. 0.0 for empty."""
    if not value:
        return 0.0
    counts = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    total = float(len(value))
    bits = 0.0
    for n in counts.values():
        p = n / total
        bits -= p * math.log(p, 2)
    return bits


def _looks_like_token(match, group):
    """True when a long alphanumeric run looks like a credential rather than
    an identifier. Hex gets a lower bar because its alphabet is only 16
    characters wide; everything else has to contain both digits and letters,
    which drops long CamelCase names and repeated filler."""
    value = match.group(group)
    if HEX_RE.match(value):
        return _entropy(value) >= MIN_HEX_ENTROPY_BITS
    has_digit = any(ch.isdigit() for ch in value)
    has_alpha = any(ch.isalpha() for ch in value)
    if not (has_digit and has_alpha):
        return False
    return _entropy(value) >= MIN_ENTROPY_BITS


def _is_secret_name(name):
    """True when a key name reads as naming a credential."""
    if not isinstance(name, str):
        return False
    folded = name.lower().replace("-", "_")
    return any(word in folded for word in SECRET_NAME_WORDS)


def _env_line_is_secret(match, group):
    """True for a KEY=value line worth redacting: the key name reads as a
    secret and the value is an actual value rather than a reference or a
    mask that is already redacted by hand."""
    if not _is_secret_name(match.group(1)):
        return False
    value = match.group(group).strip("\"' \t")
    if not value:
        return False
    return not ENV_NOISE_RE.match(value.lower())


RULES = (
    # Order matters: the widest and most specific rules go first, and a
    # later rule is skipped where it would overlap an accepted span. That
    # is what makes "AWS_ACCESS_KEY_ID=AKIA..." report as an aws-key rather
    # than as an anonymous env-secret.
    _Rule("private-key", PEM_RE, 0, None),
    _Rule("url-credentials", URL_CRED_RE, 1, None),
    _Rule("bearer-token", BEARER_RE, 1, None),
    _Rule("aws-key", AWS_KEY_RE, 1, None),
    _Rule("github-token", GITHUB_RE, 1, None),
    _Rule("env-secret", ENV_RE, 2, _env_line_is_secret),
    _Rule("email", EMAIL_RE, 1, None),
    _Rule("token", TOKEN_RE, 1, _looks_like_token),
)


def fingerprint(value):
    """Short stable digest of a secret value. Same value in, same digest
    out, across processes and runs, with no secret material recoverable by
    reading it. See the module docstring on what this does not promise."""
    if not isinstance(value, str):
        value = str(value)
    digest = hashlib.sha256(FINGERPRINT_DOMAIN
                            + value.encode("utf-8", "replace"))
    return digest.hexdigest()[:FINGERPRINT_LEN]


def _placeholder(kind, value):
    return "[redacted:{}:{}]".format(kind, fingerprint(value))


def _normalise_extra(extra_patterns):
    """Turn caller-supplied rules into _Rule records. Accepts a dict of
    {kind: pattern} or an iterable of (kind, pattern) / (kind, pattern,
    group). A pattern may be a string or an already compiled regex. Raises
    ValueError on anything unusable, because a silently dropped custom rule
    is a leak the caller believes was covered."""
    if not extra_patterns:
        return []
    if isinstance(extra_patterns, dict):
        items = list(extra_patterns.items())
    else:
        items = list(extra_patterns)
    rules = []
    for item in items:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            kind, pattern = item
            group = 0
        elif isinstance(item, (tuple, list)) and len(item) == 3:
            kind, pattern, group = item
        else:
            raise ValueError(
                "extra pattern must be (kind, regex[, group]), got {!r}"
                .format(item))
        if not isinstance(kind, str) or not KIND_RE.match(kind):
            raise ValueError(
                "bad redaction kind {!r}: lowercase letters, digits and "
                "dashes, {} characters max".format(kind, MAX_KIND_LEN))
        if isinstance(pattern, str):
            try:
                pattern = re.compile(pattern)
            except re.error as exc:
                raise ValueError("bad regex for kind {}: {}".format(kind, exc))
        if not hasattr(pattern, "finditer"):
            raise ValueError("bad regex for kind {}: {!r}".format(kind, pattern))
        if not isinstance(group, int) or group < 0:
            raise ValueError("bad group for kind {}: {!r}".format(kind, group))
        rules.append(_Rule(kind, pattern, group, None))
    return rules


def _overlaps(start, end, spans):
    for other_start, other_end in spans:
        if start < other_end and other_start < end:
            return True
    return False


def redact(text, extra_patterns=None):
    """Replace every credential-shaped run in text with a placeholder.

    Returns (clean_text, findings). Each finding is a dict with the kind,
    the half-open span [start, end) in the ORIGINAL text, and the
    placeholder that replaced it. A finding never carries the secret
    itself, so the findings list is as shareable as the redacted text.

    Caller rules from extra_patterns are tried before the built-ins, so a
    project-specific pattern wins over the generic high-entropy rule.
    """
    if text is None:
        return "", []
    if not isinstance(text, str):
        raise TypeError("redact() wants str, got {}".format(
            type(text).__name__))

    rules = _normalise_extra(extra_patterns) + list(RULES)
    # Placeholders from an earlier pass are claimed up front but never
    # replaced: that is what makes a second pass a no-op.
    taken = [m.span() for m in PLACEHOLDER_RE.finditer(text)]
    hits = []
    for rule in rules:
        for match in rule.regex.finditer(text):
            try:
                start, end = match.span(rule.group)
            except (IndexError, re.error):  # noqa: BLE001 - bad caller group
                continue
            if start < 0 or start >= end:
                continue
            if _overlaps(start, end, taken):
                continue
            if rule.check is not None and not rule.check(match, rule.group):
                continue
            taken.append((start, end))
            hits.append((start, end, rule.kind))

    hits.sort()
    out = []
    findings = []
    pos = 0
    for start, end, kind in hits:
        marker = _placeholder(kind, text[start:end])
        out.append(text[pos:start])
        out.append(marker)
        pos = end
        findings.append({"kind": kind, "start": start, "end": end,
                         "placeholder": marker})
    out.append(text[pos:])
    return "".join(out), findings


def _walk(obj, path, findings, extra_patterns):
    if isinstance(obj, dict):
        clean = {}
        for key, value in obj.items():
            child = "{}.{}".format(path, key) if path else "{}".format(key)
            if isinstance(key, str):
                key_clean, key_hits = redact(key, extra_patterns)
                for hit in key_hits:
                    hit["path"] = "{} (key)".format(child)
                    findings.append(hit)
                # Two distinct secret keys can collapse onto one
                # placeholder; keep both entries rather than lose one.
                new_key = key_clean
                suffix = 2
                while new_key in clean:
                    new_key = "{}#{}".format(key_clean, suffix)
                    suffix += 1
            else:
                new_key = key
            if (_is_secret_name(key)
                    and not isinstance(value, (dict, list, tuple))):
                # A secret-named key means the value is a credential even
                # when it does not look like one ("password": "hunter2").
                # A secret-named key over a container names a section, not
                # a value, so that case recurses normally.
                if value is None:
                    clean[new_key] = None
                    continue
                if isinstance(value, str) and PLACEHOLDER_RE.fullmatch(value):
                    # already cleaned by an earlier pass: replacing it again
                    # would change the digest and break idempotence
                    clean[new_key] = value
                    continue
                marker = _placeholder("env-secret", str(value))
                clean[new_key] = marker
                findings.append({"kind": "env-secret", "path": child,
                                 "placeholder": marker})
                continue
            clean[new_key] = _walk(value, child, findings, extra_patterns)
        return clean
    if isinstance(obj, (list, tuple)):
        items = [_walk(v, "{}[{}]".format(path, i), findings, extra_patterns)
                 for i, v in enumerate(obj)]
        return tuple(items) if isinstance(obj, tuple) else items
    if isinstance(obj, str):
        clean, hits = redact(obj, extra_patterns)
        for hit in hits:
            hit["path"] = path
            findings.append(hit)
        return clean
    return obj


def redact_dict(obj, extra_patterns=None):
    """Redact a nested structure of dicts, lists, and strings.

    Returns (clean_obj, findings). String values are redacted by the same
    rules as redact(); string keys are redacted too, so a secret used as a
    key does not survive; and a key whose NAME reads as a secret has its
    scalar value replaced whole, even when the value looks innocuous. Key
    names themselves are kept so the shape stays readable. Numbers,
    booleans, None, and other objects pass through untouched unless they
    sit under a secret-named key. Findings carry a dotted path instead of a
    span into one flat string.
    """
    findings = []
    clean = _walk(obj, "", findings, extra_patterns)
    return clean, findings


def _default_read(path):
    if path in (None, "", "-"):
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _default_write(text):
    sys.stdout.write(text)


def _parse_cli_patterns(specs):
    extra = []
    for spec in specs or []:
        if "=" not in spec:
            raise ValueError("--pattern wants KIND=REGEX, got {!r}".format(spec))
        kind, pattern = spec.split("=", 1)
        extra.append((kind.strip(), pattern))
    return extra


def _self_test():
    # The AWS key fixtures below are split across adjacent string
    # literals ("AKI" "AIOSFO..."), which Python joins into the same
    # value. Written out whole they would be a genuine critical finding
    # for agent/hearth_secrets.py, whose own self-test scans this
    # repository's sources: the redaction module must not be the one
    # file in the tree carrying a scannable secret. Do not rejoin them.
    # 1. empty and clean input
    assert redact("") == ("", []), redact("")
    assert redact(None) == ("", []), redact(None)
    plain = "nothing to see, just prose and a path /usr/local/lib/x\n"
    assert redact(plain) == (plain, []), redact(plain)
    try:
        redact(b"bytes")
        raise AssertionError("bytes should not be accepted")
    except TypeError:
        pass

    # 2. aws key: redacted, span points into the original, nothing leaks
    text = "creds id AKI" "AIOSFODNN7EXAMPLE then more"
    clean, found = redact(text)
    assert "AKI" "AIOSFODNN7EXAMPLE" not in clean, clean
    assert clean.startswith("creds id [redacted:aws-key:"), clean
    assert clean.endswith("] then more"), clean
    assert len(found) == 1, found
    assert found[0]["kind"] == "aws-key", found
    assert text[found[0]["start"]:found[0]["end"]] == "AKI" "AIOSFODNN7EXAMPLE"
    assert "AKIA" not in json.dumps(found), found

    # 3. the same value twice gets the same suffix; a different one differs
    pair = ("a AKI" "AIOSFODNN7EXAMPLE b AKI" "AIOSFODNN7EXAMPLE "
            "c AKI" "AIOSFODNN7OTHERXY")
    clean, found = redact(pair)
    assert len(found) == 3, found
    assert found[0]["placeholder"] == found[1]["placeholder"], found
    assert found[2]["placeholder"] != found[0]["placeholder"], found
    assert fingerprint("x") == fingerprint("x")
    assert fingerprint("x") != fingerprint("y")
    assert len(fingerprint("x")) == FINGERPRINT_LEN

    # 4. bearer header: the token goes, the header name stays
    clean, found = redact("Authorization: Bearer abcdefghijklmnop1234")
    assert clean.startswith("Authorization: Bearer [redacted:bearer-token:"), clean
    assert "abcdefghijklmnop1234" not in clean, clean
    assert [f["kind"] for f in found] == ["bearer-token"], found

    # 5. github tokens, both prefixes the brief named
    for tok in ("ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
                "gho_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6"):
        clean, found = redact("token={}".format(tok))
        assert tok not in clean, clean
        assert [f["kind"] for f in found] == ["github-token"], (tok, found)

    # 6. PEM block: many lines collapse to one short placeholder
    pem = ("head\n"
           "-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIBOgIBAAJBAK7hVwvtfhMA6lkKJMtpVYPu8vqKkZOxkbJnQnuVnsRQ\n"
           "8vqKkZOxkbJnQnuVnsRQMIIBOgIBAAJBAK7hVwvtfhMA6lkKJMtpVYPu\n"
           "-----END RSA PRIVATE KEY-----\n"
           "tail\n")
    clean, found = redact(pem)
    assert [f["kind"] for f in found] == ["private-key"], found
    assert "BEGIN RSA PRIVATE KEY" not in clean, clean
    assert "MIIBOgIBAAJBAK" not in clean, clean
    assert clean.startswith("head\n") and clean.endswith("\ntail\n"), clean
    assert len(clean.splitlines()) == 3, clean
    assert len(clean) < len(pem), (len(clean), len(pem))

    # a PEM with no END marker is NOT collapsed (documented limitation),
    # but the body still trips the high-entropy rule
    half = ("-----BEGIN RSA PRIVATE KEY-----\n"
            "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0\n")
    clean, found = redact(half)
    assert "BEGIN RSA PRIVATE KEY" in clean, clean
    assert "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0" not in clean, clean
    assert [f["kind"] for f in found] == ["token"], found

    # 7. inline URL credentials: userinfo goes, scheme and host stay
    clean, found = redact("clone https://eric:s3cr3tpw@example.com/repo.git")
    assert "s3cr3tpw" not in clean and "eric:" not in clean, clean
    assert clean.startswith("clone https://[redacted:url-credentials:"), clean
    assert clean.endswith("]@example.com/repo.git"), clean
    assert [f["kind"] for f in found] == ["url-credentials"], found

    # 8. email addresses
    clean, found = redact("ping eric.catalano@example.co.uk about it")
    assert "example.co.uk" not in clean, clean
    assert [f["kind"] for f in found] == ["email"], found

    # 9. KEY=value lines, including the cases that must be left alone
    env = ("API_KEY=abc123xyz\n"
           "export AUTH_TOKEN = 'tok-9'\n"
           '  "password": "hunter2",\n'
           "AUTHOR=Eric\n"
           "DEBUG=1\n"
           "PASSWORD=\n"
           "SECRET_KEY=${SECRET_KEY}\n"
           "api-password=***\n")
    clean, found = redact(env)
    assert [f["kind"] for f in found] == ["env-secret"] * 3, found
    for leaked in ("abc123xyz", "tok-9", "hunter2"):
        assert leaked not in clean, (leaked, clean)
    for kept in ("AUTHOR=Eric", "DEBUG=1", "PASSWORD=\n",
                 "SECRET_KEY=${SECRET_KEY}", "api-password=***"):
        assert kept in clean, (kept, clean)

    # 10. high-entropy runs in, ordinary long strings out
    hexy = "3f786850e387550fdab836ed7e6dc881de23001b"
    clean, found = redact("sha {} end".format(hexy))
    assert hexy not in clean and [f["kind"] for f in found] == ["token"], found
    for innocent in ("a" * 40, "VeryLongCamelCaseIdentifierNameHere",
                     "/usr/local/lib/python3.12/site-packages/thing",
                     "----------------------------------------"):
        got, hits = redact("x {} y".format(innocent))
        assert hits == [], (innocent, hits)
        assert innocent in got, (innocent, got)

    # 11. overlapping rules produce one placeholder, most specific kind wins
    clean, found = redact("AWS_ACCESS_KEY_ID=AKI" "AIOSFODNN7EXAMPLE")
    assert [f["kind"] for f in found] == ["aws-key"], found
    assert clean.startswith("AWS_ACCESS_KEY_ID=[redacted:aws-key:"), clean
    clean, found = redact(
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY")
    assert [f["kind"] for f in found] == ["env-secret"], found
    assert "wJalrXUtn" not in clean, clean
    clean, found = redact("https://bot:ghp_tokenvalue@github.com/x")
    assert [f["kind"] for f in found] == ["url-credentials"], found
    assert "ghp_tokenvalue" not in clean, clean

    # 12. idempotence over a blob containing every kind
    blob = "".join([text, "\n", pair, "\n", pem, env,
                    "Authorization: Bearer abcdefghijklmnop1234\n",
                    "clone https://eric:s3cr3tpw@example.com/repo.git\n",
                    "mail eric@example.com\n", "sha ", hexy, "\n",
                    "token=ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4\n"])
    once, first = redact(blob)
    assert first, "blob should produce findings"
    twice, second = redact(once)
    assert second == [], second
    assert twice == once, "redaction is not idempotent"
    thrice, third = redact(twice)
    assert thrice == once and third == [], third
    kinds = set(f["kind"] for f in first)
    assert kinds == {"private-key", "url-credentials", "bearer-token",
                     "aws-key", "github-token", "env-secret", "email",
                     "token"}, kinds
    # no original secret survives, and spans still address the input
    for secret in ("AKI" "AIOSFODNN7EXAMPLE", "s3cr3tpw", "hunter2", hexy,
                   "abcdefghijklmnop1234", "MIIBOgIBAAJBAK"):
        assert secret not in once, secret
    for finding in first:
        assert blob[finding["start"]:finding["end"]], finding

    # 13. bounded growth: one placeholder per finding, fixed maximum size
    assert PLACEHOLDER_MAX_LEN == 42, PLACEHOLDER_MAX_LEN
    for kind in [r.kind for r in RULES]:
        assert KIND_RE.match(kind), kind
        assert len(_placeholder(kind, "v")) <= PLACEHOLDER_MAX_LEN, kind
        assert PLACEHOLDER_RE.match(_placeholder(kind, "v")), kind
    huge = "key " + ("A1b2C3d4E5f6G7h8" * 400)
    clean, found = redact(huge)
    assert len(found) == 1, found
    assert len(clean) < 60, clean
    assert max(len(line) for line in clean.splitlines()) <= PLACEHOLDER_MAX_LEN + 4
    for sample in (blob, env, pem, huge, plain):
        got, hits = redact(sample)
        assert len(got) <= len(sample) + PLACEHOLDER_MAX_LEN * len(hits), sample

    # 14. caller-supplied rules run first and can beat the generic rule
    clean, found = redact("ticket JOB-4821 filed",
                          extra_patterns=[("job-id", r"JOB-[0-9]{4}")])
    assert "JOB-4821" not in clean, clean
    assert [f["kind"] for f in found] == ["job-id"], found
    clean, found = redact("sha {} end".format(hexy),
                          extra_patterns={"build-sha": r"[0-9a-f]{40}"})
    assert [f["kind"] for f in found] == ["build-sha"], found
    clean, found = redact("x secret=abc y",
                          extra_patterns=[("tagged", r"secret=(\w+)", 1)])
    assert clean.startswith("x secret=[redacted:tagged:"), clean
    # and the custom kind is still idempotent on a second pass
    assert redact(clean, extra_patterns=[("tagged", r"secret=(\w+)", 1)])[1] == []
    for bad in ([("Bad Kind", r"x")], [("x" * 40, r"y")],
                [("ok", r"(unclosed")], [("ok",)], ["nope"],
                [("ok", r"x", -1)], [("ok", 7)]):
        try:
            redact("anything", extra_patterns=bad)
            raise AssertionError("bad extra pattern accepted: {!r}".format(bad))
        except ValueError:
            pass

    # 15. redact_dict: nesting, secret-named keys, scalars, ordering
    obj = collections.OrderedDict([
        ("name", "hearth"),
        ("password", "hunter2"),
        ("pin_secret", 1234),
        ("api_key", None),
        ("retries", 3),
        ("enabled", True),
        ("note", "id AKI" "AIOSFODNN7EXAMPLE seen"),
        ("credentials", {"token": "abc", "host": "example.com"}),
        ("history", [{"secret": 99}, "Bearer abcdefghijklmnop1234", 7]),
    ])
    clean, found = redact_dict(obj)
    assert list(clean.keys()) == list(obj.keys()), clean
    assert clean["name"] == "hearth", clean
    assert clean["retries"] == 3 and clean["enabled"] is True, clean
    assert clean["api_key"] is None, clean
    assert clean["password"].startswith("[redacted:env-secret:"), clean
    assert clean["pin_secret"].startswith("[redacted:env-secret:"), clean
    assert "AKI" "AIOSFODNN7EXAMPLE" not in clean["note"], clean
    assert clean["credentials"]["host"] == "example.com", clean
    assert clean["credentials"]["token"].startswith("[redacted:"), clean
    assert clean["history"][1].startswith("Bearer [redacted:bearer-token:")
    assert clean["history"][2] == 7, clean
    assert "hunter2" not in json.dumps(clean), clean
    paths = sorted(f["path"] for f in found)
    assert paths == ["credentials.token", "history[0].secret",
                     "history[1]", "note", "password",
                     "pin_secret"], paths
    # the same value under two secret-named keys correlates
    twin, _ = redact_dict({"a_token": "same", "b_secret": "same"})
    assert twin["a_token"] == twin["b_secret"], twin
    # idempotent on structures too
    again, hits = redact_dict(clean)
    assert hits == [], hits
    assert again == clean, again
    # a secret used as a key is redacted, and a collision keeps both entries
    keyed, found = redact_dict({"AKI" "AIOSFODNN7EXAMPLE": 1,
                                "x AKI" "AIOSFODNN7EXAMPLE": 2})
    assert not any("AKI" "AIOSFODNN7EXAMPLE" in k for k in keyed), keyed
    assert len(keyed) == 2, keyed
    assert any("(key)" in f["path"] for f in found), found
    # scalars and tuples at the root
    assert redact_dict(5) == (5, []), redact_dict(5)
    assert redact_dict("id AKI" "AIOSFODNN7EXAMPLE")[0].startswith("id [redacted:")
    tup, _ = redact_dict(("AKI" "AIOSFODNN7EXAMPLE", 1))
    assert isinstance(tup, tuple) and tup[1] == 1, tup

    # 16. CLI, with reads and writes injected so no file or stdin is touched
    written = []
    rc = main(["log.txt"], read_fn=lambda p: "id AKI" "AIOSFODNN7EXAMPLE\n",
              write_fn=written.append)
    assert rc == 0, rc
    assert "AKI" "AIOSFODNN7EXAMPLE" not in written[0], written
    assert written[0].endswith("\n"), written

    seen_paths = []

    def fake_read(path):
        seen_paths.append(path)
        return "k: Bearer abcdefghijklmnop1234\n"

    written = []
    assert main([], read_fn=fake_read, write_fn=written.append) == 0
    assert seen_paths == [None], seen_paths
    assert main(["-"], read_fn=fake_read, write_fn=written.append) == 0
    assert seen_paths[-1] == "-", seen_paths

    written = []
    rc = main(["--json", "log.txt"],
              read_fn=lambda p: "id AKI" "AIOSFODNN7EXAMPLE and a@b.com\n",
              write_fn=written.append)
    assert rc == 0, rc
    payload = json.loads(written[0])
    assert sorted(f["kind"] for f in payload["findings"]) == ["aws-key", "email"]
    assert "AKI" "AIOSFODNN7EXAMPLE" not in written[0], written
    assert "a@b.com" not in written[0], written
    assert payload["text"].startswith("id [redacted:aws-key:"), payload

    written = []
    rc = main(["--pattern", "job-id=JOB-[0-9]{4}", "log.txt"],
              read_fn=lambda p: "JOB-4821\n", write_fn=written.append)
    assert rc == 0 and "JOB-4821" not in written[0], written

    # failure paths: unreadable input and an unusable custom pattern each
    # exit 2, say why on stderr, and write nothing to the output stream
    written = []

    def boom(path):
        raise IOError("no such file")

    held, sys.stderr = sys.stderr, io.StringIO()
    try:
        assert main(["missing.txt"], read_fn=boom, write_fn=written.append) == 2
        assert main(["--pattern", "nonsense", "log.txt"],
                    read_fn=lambda p: "", write_fn=written.append) == 2
        assert main(["--pattern", "job-id=(unclosed", "log.txt"],
                    read_fn=lambda p: "", write_fn=written.append) == 2
        complaints = sys.stderr.getvalue().splitlines()
    finally:
        sys.stderr = held
    assert written == [], written
    assert len(complaints) == 3, complaints
    assert all(line.startswith("hearth-redact: ") for line in complaints)
    assert "missing.txt" in complaints[0], complaints

    print("hearth-redact self-test OK")
    return 0


def main(argv=None, read_fn=None, write_fn=None):
    p = argparse.ArgumentParser(
        prog="hearth-redact",
        description="strip credentials out of a transcript before sharing it")
    p.add_argument("path", nargs="?",
                   help="file to read, or - (the default) for stdin")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="write a JSON object with the redacted text and the "
                        "list of findings instead of plain text")
    p.add_argument("--pattern", action="append", default=[],
                   metavar="KIND=REGEX",
                   help="extra redaction rule, repeatable")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()

    read = read_fn or _default_read
    write = write_fn or _default_write
    try:
        extra = _parse_cli_patterns(a.pattern)
    except ValueError as exc:
        sys.stderr.write("hearth-redact: {}\n".format(exc))
        return 2
    try:
        text = read(a.path)
    except Exception as exc:  # noqa: BLE001 - any read failure is just exit 2
        sys.stderr.write("hearth-redact: cannot read {}: {}\n".format(
            a.path or "-", exc))
        return 2
    try:
        clean, findings = redact(text, extra_patterns=extra)
    except ValueError as exc:
        sys.stderr.write("hearth-redact: {}\n".format(exc))
        return 2
    if a.as_json:
        write(json.dumps({"text": clean, "findings": findings},
                         indent=2, sort_keys=True) + "\n")
    else:
        write(clean)
    return 0


if __name__ == "__main__":
    sys.exit(main())
