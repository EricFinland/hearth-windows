#!/usr/bin/env python3
"""hearth secret-in-motion scanner: a signal, not a boundary.

agent/hearth_injection.py covers instructions coming in: content the agent
reads that tries to steer what it does next. This module covers credentials
going out: text the agent is about to write to a real file, where two
distinct failure modes are both live for a coding agent backed by a small
local model.

  A. The model WRITES a credential into a file. Asked for an example config,
     a 7B model will cheerfully emit a plausible-looking AWS key, and
     sometimes a real one memorised from training data. Asked to "fix the
     auth code," it may inline a literal token instead of reading it from
     the environment. The user then commits it.
  B. The model MOVES a real secret somewhere it does not belong. It reads
     .env for context while debugging, then pastes a live key into a
     README, a test fixture, or a code comment while explaining what it
     did. The secret was already on disk; it just became far more likely
     to be shared, because READMEs and comments get copy-pasted, screen-
     shared, and committed in a way .env is not.

Hearth's own checkpoint store (agent/hearth_checkpoint.py) already excludes
secret-shaped paths (.env*, *.pem, *.key, id_rsa*, *.pfx, credentials*) from
its shadow-git capture, which means damage inside one of those files is not
even undoable through Hearth's own undo path. Catching a bad write before it
lands is the only good moment left.

This module does NOT stop that write. It does not block a tool call, refuse
a file write, or strip anything from what actually reaches disk. It scans a
blob of text that is about to be written, returns structured findings with a
kind, a location, a confidence, and a plain-English reason, and lets the
layer above (the permission gate, and ultimately the human clicking approve)
decide what to do with that information. The same discipline hearth_proc.py
states about itself applies here word for word: this module does not
sandbox anything, and a scanner that can be wrong must never be the thing
standing between the model and the write. Blocking on a heuristic was
rejected for the same reason agent/hearth_injection.py rejected it: a false
positive that silently dropped or mangled a write would corrupt the user's
own files, a far worse failure than a missed detection. A user must remain
able to deliberately write a file containing a real token -- committing a
CI secret to a private ops repo, updating a rotated key in .env -- and nothing
here may stand in the way of that.

Standard library only. No network, no file writes, no side effects beyond
the optional self-test reading its own fixture files.

DETECTION STRATEGY, in the same spirit as hearth_injection.py: shape, not a
wordlist. A wordlist of provider names ages the moment a new provider ships
a key format, and it does nothing for a secret that carries no provider name
at all. Two structural signals carry the actual weight:

  1. Known key formats: distinctive prefixes and lengths that providers
     themselves treat as a stable public contract (AKIA..., ghp_..., xox...,
     sk_live_..., AIza..., a PEM block's BEGIN/END markers, a JWT's three
     dot-separated base64url segments). These do not need entropy scoring
     to be recognised; the shape alone is close to unambiguous.
  2. High-entropy strings sitting next to a suspicious variable name
     (api_key, secret, token, password, credential, and their common
     spellings), where Shannon entropy is what actually separates a live
     random key from a human-written placeholder. A connection string
     carrying inline user:pass@host credentials is treated as a variant of
     this same shape, no variable name required, since the syntax itself
     names the value's role.

THE PLACEHOLDER PROBLEM, which is the harder half of this module by design
and by explicit instruction: example configs, docs, and test fixtures are
full of secret-shaped strings that are not secrets -- "sk-xxxxxxxx",
"YOUR_API_KEY_HERE", "<token>", 'password = "changeme"', and AWS's own
documented example key AKIAIOSFODNN7EXAMPLE. A scanner that flags those is
worse than none: it trains the user to click through the approval prompt
without reading it, which is the exact failure mode that makes the NEXT,
real finding invisible too. is_placeholder() below is the whole answer to
this, and it suppresses (drops the finding entirely, not merely discounts
it, since a shown-but-dimmed placeholder finding still trains the same bad
habit) on any of: an unresolved template placeholder ("{api_key}",
"${TOKEN}", "%(secret)s", "<token>"); a small curated list of sentinel words
(example, sample, dummy, fake, placeholder, changeme, redacted, ...); a run
of 5+ repeated characters or a 6+ character ascending/descending run
("xxxxxxxx", "01234567"); a low unique-character ratio; the value itself
being shaped like a SCREAMING_SNAKE_CASE environment-variable NAME rather
than a value (catches "secret = 'AWS_SECRET_ACCESS_KEY'", a lookup-table
entry that names a secret without being one); and, doing most of the actual
work, Shannon entropy below a threshold tuned to the value's own character
class (hex, base64-ish, or generic).

WHAT WAS TRADED to keep placeholders quiet, stated plainly rather than
discovered the hard way:

  - A passphrase-style secret made of real English words ("correct horse
    battery staple") has LOW character-level entropy by this module's
    measure, the same as a placeholder does, and is not distinguished from
    one. This is a real, disclosed false negative, not an edge case someone
    forgot: this module cannot tell a weak-but-real secret from a strong
    placeholder using entropy alone, and was not asked to solve that.
  - A short secret (under roughly 16-20 characters) sitting exactly on the
    entropy threshold for its character class can go either way; the
    thresholds were tuned against this repository's own fixtures and a set
    of synthetic keys built at self-test runtime (never written into this
    file's own source as static text -- see "WHY NO CODE-STRUCTURE
    DISCOUNT" below for why that distinction matters here), not against an
    exhaustive corpus.
  - The known-format regexes (AWS, GitHub, Slack, Stripe, Google, JWT) are
    NOT entropy-gated at all, deliberately: a fixed fifteen-character
    prefix like "AKIA" or "ghp_" is already strong, close-to-unambiguous
    structural evidence on its own, and gating it on entropy too would
    risk suppressing a genuine leaked key that happened to score just under
    an arbitrary threshold, which is a worse failure than occasionally
    reporting a structurally-shaped but ultimately fake key. Those formats
    are suppressed only by the placeholder heuristics above (sentinel
    words, template syntax, repeated/sequential runs), which is exactly why
    AKIAIOSFODNN7EXAMPLE -- structurally a perfect AWS access key, 20
    characters, correct alphabet -- is still caught: it contains the
    substring "example," a sentinel word, not because its entropy looks low.

WHY NO CODE-STRUCTURE DISCOUNT, unlike hearth_injection.py's tokenize-based
suppression of matches inside Python STRING/COMMENT tokens. That discount is
correct for injection phrases, where a phrase sitting inside a string
literal or comment in this project's own source is almost certainly a
pattern definition, not a live instruction. It would be actively wrong here.
A hardcoded secret IS, in every language, a string literal -- that is what
"hardcoded" means. Discounting matches that fall inside a STRING token would
discount exactly the failure mode this module exists to catch (failure mode
A above: a live key assigned directly to a variable like api_key instead of
being read from the environment), and would do so specifically for the case
a coding agent is most likely to produce. So this module does not look at
token type at all; a secret-shaped
string is scored the same whether it sits in a comment, a docstring, an
f-string, or a bare assignment. The corresponding cost, paid deliberately
rather than by accident, is that this module's OWN self-test cannot embed a
realistic positive fixture as a static string literal without that literal
then being flagged when this file's own source is scanned as a benign
fixture (see the repo-wide sweep below). The fix used here is not a
suppression rule; it is that the self-test's real-secret-shaped fixtures are
assembled at runtime from short, non-matching pieces plus the standard
library's own `secrets` module, so the assembled, matching string never
exists as contiguous text anywhere in this file on disk. See _self_test().

WHAT REDACTION MAY HIDE, which is a separate property from what detection
may catch and was learned the hard way. redact() exists so a caller holding
both the text and the findings can render a safe-to-display copy, and the
desktop approval card is built on it: the card shows a redacted copy of a
write while approving executes the real one. That only stays honest if the
redacted span contains nothing but credential material, because every byte
inside a span disappears from the card while still reaching disk. It did
not. _PEM_RE's body accepted any characters at all, so a model could open a
key block, emit a realistic base64 prefix, append whatever it wanted the
user not to read, close the block, and hide the lot behind one token.
Demonstrated by execution against this module, 273 characters concealed.
Two bounds now hold that line, both proven non-vacuous in _self_test:

  1. A PEM finding covers only the longest run of whole pure-base64 lines
     inside the block (_pem_key_material_span), never the markers and never
     a line carrying anything else. Any text a person would actually need
     to read -- a command, a URL, a sentence -- contains a character
     outside the base64 alphabet and therefore cannot enter the span.
  2. No span of any kind, from any detector, may collapse more than
     MAX_REDACT_SPAN characters. Past that redact() leaves the text
     visible rather than hiding more, and redaction_summary() reports the
     exact number of characters concealed so a caller can put it on screen
     instead of concealing silently.

OTHER KNOWN LIMITATIONS, stated rather than left implicit:

  - Scan window: only a head slice plus a tail slice of a document larger
    than MAX_SCAN_CHARS is scanned (see _scan_windows), the same traded
    blind spot hearth_injection.py accepts for the same reason: staying
    O(bounded) on every write matters more than covering an arbitrarily
    large middle section.
  - The generic entropy detector requires an explicit suspicious variable
    name nearby (api_key, secret, token, password, credential, and close
    spellings/compounds). A truly generic high-entropy string assigned to
    an unrelated name (`x = "<40 random chars>"`) is invisible to this
    module; only the known-format regexes catch a secret with no
    recognisable name context at all.
  - The connection-string detector and the generic name+value detector
    only recognise a value that is directly adjacent to its name via `=` or
    `:` with a quoted string, or an unquoted UPPER_SNAKE `NAME=value` line
    (.env / shell-export style). A value reached indirectly -- built up via
    string concatenation, read from a second variable, or expressed as an
    unquoted lowercase YAML scalar (`api_key: AKIA...` with no quotes) --
    is not matched by the name-context detectors, though a KNOWN-FORMAT
    secret (an actual AKIA/ghp_/sk_live_ shape) is still caught regardless
    of assignment syntax, since those regexes do not depend on context at
    all.
  - Absence of a finding must never be read as "this text carries no
    secret." A sufficiently novel key format, a secret with no recognisable
    variable name nearby, or a real credential phrased as a low-entropy
    passphrase can all pass through unseen. Detecting and surfacing what
    this module CAN see is the job; it is not proof of the absence of what
    it cannot.
"""

import math
import os
import re
import sys

# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

SEVERITY = ("none", "low", "medium", "high", "critical")
_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITY)}


def meets_threshold(result, level):
    """True if result['severity'] is at or above `level` in the SEVERITY order."""
    return _SEVERITY_RANK[result["severity"]] >= _SEVERITY_RANK[level]


# ---------------------------------------------------------------------------
# Kinds of finding. Named so a caller can filter or explain without parsing
# the reason string.
# ---------------------------------------------------------------------------

KIND_AWS_ACCESS_KEY = "aws_access_key"
KIND_GITHUB_TOKEN = "github_token"
KIND_SLACK_TOKEN = "slack_token"
KIND_SLACK_WEBHOOK = "slack_webhook_url"
KIND_STRIPE_KEY = "stripe_key"
KIND_GOOGLE_API_KEY = "google_api_key"
KIND_OPENAI_STYLE_KEY = "openai_style_key"
KIND_JWT = "jwt"
KIND_PRIVATE_KEY_PEM = "private_key_pem"
KIND_CONNECTION_STRING = "connection_string_credential"
KIND_GENERIC_ASSIGNMENT = "high_entropy_assignment"

# ---------------------------------------------------------------------------
# Scan window: bound the work. Same rationale and the same head+tail shape
# as hearth_injection.py's MAX_SCAN_CHARS -- this can run on every write, so
# it stays a bounded number of regex passes rather than an unbounded one.
# The traded blind spot is identical and identically deliberate: a secret
# placed only in the untouched middle of a document larger than the window
# is not seen.
# ---------------------------------------------------------------------------

MAX_HEAD_CHARS = 60_000
MAX_TAIL_CHARS = 20_000
MAX_SCAN_CHARS = MAX_HEAD_CHARS + MAX_TAIL_CHARS


def _scan_windows(text):
    """Return ([(offset, chunk), ...], truncated) covering at most MAX_SCAN_CHARS."""
    n = len(text)
    if n <= MAX_SCAN_CHARS:
        return [(0, text)], False
    head = text[:MAX_HEAD_CHARS]
    tail_start = n - MAX_TAIL_CHARS
    tail = text[tail_start:]
    return [(0, head), (tail_start, tail)], True


# ---------------------------------------------------------------------------
# Line numbers. Computed per window (cheap: str.find/str.count are C loops),
# not once for the whole document, so a multi-megabyte write does not pay
# for a full-document scan just to answer "what line was this on."
# ---------------------------------------------------------------------------


def _line_starts(chunk):
    """offsets of the start of every line within `chunk`, offsets[0] == 0."""
    starts = [0]
    idx = chunk.find("\n")
    while idx != -1:
        starts.append(idx + 1)
        idx = chunk.find("\n", idx + 1)
    return starts


def _line_in_chunk(line_starts, pos):
    """1-indexed line number of `pos` within the chunk `line_starts` was built from."""
    lo, hi = 0, len(line_starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if line_starts[mid] <= pos:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _base_line_number(text, offset):
    """1-indexed line number of `offset` within the full original `text`."""
    if offset <= 0:
        return 1
    return text[:offset].count("\n") + 1


# ---------------------------------------------------------------------------
# Entropy and the placeholder heuristics
# ---------------------------------------------------------------------------

_HEX_CHARS = set("0123456789abcdefABCDEF")
_B64_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-"
)

# Thresholds are Shannon entropy in bits per character, tuned against this
# repository's own benign fixtures (see _self_test) and a handful of
# hand-computed dictionary-word placeholders ("changeme" ~2.75 bits/char,
# "the-real-token-value" ~3.38 bits/char, both comfortably under their
# threshold) rather than an exhaustive corpus. A value using only hex digits
# gets the tightest threshold because its whole alphabet is 16 symbols wide
# (max possible entropy 4 bits/char), so even a real hex secret cannot score
# as high as a base64-alphabet one; a value using only the base64-ish
# alphabet gets a looser threshold for the same reason in reverse. Anything
# else falls back to a middle value.
_HEX_ENTROPY_THRESHOLD = 3.0
_B64_ENTROPY_THRESHOLD = 4.0
_GENERIC_ENTROPY_THRESHOLD = 3.6


def _shannon_entropy(s):
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    n = len(s)
    entropy = 0.0
    for count in freq.values():
        p = count / n
        entropy -= p * math.log2(p)
    return entropy


def _entropy_threshold_for(value):
    chars = set(value)
    if chars and chars <= _HEX_CHARS:
        return _HEX_ENTROPY_THRESHOLD
    if chars and chars <= _B64_CHARS:
        return _B64_ENTROPY_THRESHOLD
    return _GENERIC_ENTROPY_THRESHOLD


# A small, deliberately curated set of sentinel words. This is NOT the kind
# of wordlist the module docstring warns against (a list of provider names
# that ages the moment a new provider ships): these are words that mark a
# value as an EXAMPLE regardless of what it is an example of, which is a
# much more durable signal than a provider name would be. Kept short on
# purpose -- a long list risks a real secret coincidentally containing one
# of these as a substring, which is why entropy (below) carries most of the
# actual weight and this list is a fast path for the obvious cases.
_PLACEHOLDER_WORDS = (
    "example", "sample", "dummy", "fake", "placeholder", "changeme",
    "change_me", "change-me", "insert", "replace", "your_", "your-",
    "todo", "redacted", "masked", "notreal", "not_a_real", "fixme",
    "foobar", "testing", "xxxx", "0000000", "1111111",
)

# An unresolved template placeholder anywhere in the value -- Python
# str.format()/f-string ("{api_key}"), shell/YAML ("${TOKEN}"), printf-style
# ("%(secret)s"), or a bare angle-bracket convention ("<token>",
# "<your-api-key>") -- is strong, structural evidence that the value is a
# template slot, not a live secret: a real secret has already been
# substituted in and cannot itself contain the syntax that means "put a
# value here." This is what keeps agent/hearth_tools.py's honeyfile decoy
# templates ("aws_secret_access_key = {canary}", "STRIPE_SECRET_KEY=sk_live_
# {canary}") quiet without needing any code-structure discount: the decoy
# template strings in that file's source are read here exactly as they
# appear on disk, unrendered, with the format placeholder still literally
# present.
_TEMPLATE_RE = re.compile(
    r"\{\{?[A-Za-z_][A-Za-z0-9_]*\}?\}|\$\{[A-Za-z_][A-Za-z0-9_]*\}|"
    r"%\([A-Za-z_][A-Za-z0-9_]*\)[sd]|<[A-Za-z_][A-Za-z0-9_ .\-]{0,60}>"
)

_REPEATED_RUN_RE = re.compile(r"(.)\1{4,}")

# A value that is itself shaped like a SCREAMING_SNAKE_CASE environment
# variable NAME (all-caps identifier segments joined by underscores, no
# lowercase, no other punctuation) is being used as a reference to a secret,
# not as the secret's value -- e.g. `secret = "AWS_SECRET_ACCESS_KEY"` in a
# lookup table that maps friendly names to env-var names. Real secret VALUES
# essentially never take this exact shape (mixed case or digit-heavy,
# generated by a CSPRNG, not spelled like an identifier).
_ENV_VAR_SHAPE_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")

# A value shaped like a dotted code reference ("hearth_tools.find_canary",
# "os.environ.get") rather than a secret. This exists because the bare
# .env-style regex has no way to know, syntactically, that the right-hand
# side of `token = hearth_tools.find_canary(result)` is a function call and
# not a literal value -- it only sees "no whitespace, no quotes, 8+ chars"
# once the trailing "(result)" is excluded by the value charclass. Real
# secrets are not, in practice, two-or-more identifier-shaped segments
# joined by literal dots; a JWT (handled by its own dedicated detector) has
# a different shape (three base64url segments, not word-shaped) and a
# genuine dotted hostname assigned to a name that suggests a credential is
# rare enough that this is an acceptable trade.
_DOTTED_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def _has_sequential_run(value, min_run):
    """True if `value` contains `min_run`+ consecutive ascending or
    descending characters by code point ("0123456", "hgfedcb")."""
    for step in (1, -1):
        run = 1
        for i in range(1, len(value)):
            if ord(value[i]) - ord(value[i - 1]) == step:
                run += 1
                if run >= min_run:
                    return True
            else:
                run = 1
    return False


# Exposed as a module-level flag, not inlined into is_placeholder, so the
# self-test can disable it and prove the suppression is load-bearing rather
# than vacuous (see _self_test's non-vacuous-suppression proof). When False,
# is_placeholder() always returns False regardless of what it actually
# observes, which lets the self-test show, by direct comparison, what this
# repository's benign fixtures would look like with the placeholder
# heuristics turned off.
_PLACEHOLDER_SUPPRESSION_ENABLED = True


def is_placeholder(value, check_entropy=True):
    """True if `value` looks like an example, a template slot, or an
    otherwise non-random stand-in rather than a live secret.

    This is the harder half of this module, by design: real credentials
    are near-random and placeholders are not, so entropy carries most of
    the weight, backed by a few cheap structural checks (template syntax,
    repeated/sequential runs, a curated sentinel-word list, and the
    env-var-NAME-shape check) that catch the common cases entropy alone
    scores ambiguously. See the module docstring's "THE PLACEHOLDER
    PROBLEM" section for what this deliberately does and does not catch.

    check_entropy=False skips the entropy sub-check specifically (every
    other structural check still applies). The known-key-format detectors
    (AWS, GitHub, Slack, Stripe, Google, OpenAI-style, JWT) pass False
    here: those formats use a short, fixed-length string over a REDUCED
    alphabet (an AWS access key's 16-character suffix is uppercase+digit
    only, 36 symbols, not the ~64-95 symbols ordinary text or base64 draws
    from), and at that length the empirically observed entropy of a
    genuinely random value routinely lands BELOW this module's generic
    threshold by chance alone -- found in review, on a synthetic AWS key
    built from `secrets.choice`, no human-chosen placeholder involved. A
    fixed fifteen-character prefix like "AKIA" or "ghp_" is already close
    to unambiguous structural evidence; gating it on entropy too would
    risk suppressing a genuinely leaked key that happened to sample low,
    which is a worse failure than the rare case of an entropy-adjacent
    non-secret matching the shape. The generic name+value detector, which
    has no fixed alphabet or length to lean on, keeps the entropy check
    (the default): entropy is the ONLY signal available there.
    """
    if value is None:
        return False
    v = value.strip()
    if len(v) < 6:
        return True
    if not _PLACEHOLDER_SUPPRESSION_ENABLED:
        return False
    if _TEMPLATE_RE.search(v):
        return True
    low = v.lower()
    if any(w in low for w in _PLACEHOLDER_WORDS):
        return True
    if _REPEATED_RUN_RE.search(v):
        return True
    if _has_sequential_run(v, 6):
        return True
    unique_ratio = len(set(v)) / len(v)
    if unique_ratio < 0.35:
        return True
    if _ENV_VAR_SHAPE_RE.fullmatch(v):
        return True
    if not check_entropy:
        # The two checks below are soft, entropy-adjacent heuristics (a
        # dotted run of letters "looks like" a code reference; a raw
        # entropy score "looks like" prose) rather than hard structural
        # markers, and both have a real known-format shape that can
        # legitimately collide with them: a JWT's three dot-separated
        # base64url segments can fullmatch the dotted-identifier shape
        # purely by chance whenever every segment happens to start with a
        # letter (found in review: a synthetic JWT test fixture tripped
        # this on a coin flip). Known-format detectors already have strong
        # prefix/shape evidence and skip entropy for the reasons in this
        # function's docstring; skipping the dotted-identifier check here
        # too keeps that skip consistent rather than half-applied.
        return False
    if _DOTTED_IDENT_RE.fullmatch(v):
        return True
    if _shannon_entropy(v) < _entropy_threshold_for(v):
        return True
    return False


# ---------------------------------------------------------------------------
# Redaction-safe masking for the findings this module returns. A masked
# preview, not the raw value, ships in every finding by default -- see the
# module docstring and redact() below for why: findings travel through
# logs and event streams, and a scanner whose own output reproduces the
# secret it found would be self-defeating.
# ---------------------------------------------------------------------------


def _mask(value):
    if value is None:
        return ""
    v = value.strip()
    if len(v) <= 6:
        return "*" * len(v)
    # A fixed-width mask regardless of actual length beyond the visible
    # edges: showing exactly len(v)-4 stars would leak the secret's real
    # length, which is itself sometimes enough to narrow down a key format.
    return v[:2] + "..." + v[-2:]


# ---------------------------------------------------------------------------
# Suspicious-name detection for the generic entropy detector. Segment-based
# rather than a single \bapi_key\b-style regex on purpose: a naive
# \b(...)\b alternation cannot match a suspicious word that is a SUFFIX of a
# longer underscore-joined identifier ("STRIPE_SECRET_KEY", "TELEGRAM_BOT_
# TOKEN", "AWS_SECRET_ACCESS_KEY"), because "_" is a word character in
# regex terms and there is no \b boundary between it and the letters on
# either side. Splitting the identifier into segments first and checking
# each segment (and adjacent segment pairs, for two-word signals like
# "access"+"key") sidesteps that boundary problem entirely, and also
# handles camelCase ("apiKey", "clientSecret") the same way snake_case and
# SCREAMING_SNAKE_CASE are handled, with one code path instead of three
# regex variants.
# ---------------------------------------------------------------------------

_IDENT_SEGMENT_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")

_SUSPICIOUS_SINGLE_SEGMENTS = frozenset((
    "secret", "token", "password", "passwd", "pwd", "credential",
    "credentials", "apikey",
))
_SUSPICIOUS_SEGMENT_PAIRS = frozenset((
    "accesskey", "apikey", "privatekey", "clientsecret", "authtoken",
    "accesstoken", "refreshtoken", "sessiontoken", "secretkey", "bearertoken",
))


def _split_ident(name):
    segs = []
    for part in re.split(r"[_\-]+", name):
        if not part:
            continue
        segs.extend(m.lower() for m in _IDENT_SEGMENT_RE.findall(part))
    return segs


def _name_is_suspicious(name):
    segs = _split_ident(name)
    if any(s in _SUSPICIOUS_SINGLE_SEGMENTS for s in segs):
        return True
    for a, b in zip(segs, segs[1:]):
        if (a + b) in _SUSPICIOUS_SEGMENT_PAIRS:
            return True
    return False


def _severity_for_name(name):
    segs = _split_ident(name)
    if "private" in segs and "key" in segs:
        return "critical"
    if "client" in segs and "secret" in segs:
        return "critical"
    return "high"


# ---------------------------------------------------------------------------
# Known key-format detectors: distinctive prefix + length, no entropy gate.
# See the module docstring for why these are not entropy-gated.
# ---------------------------------------------------------------------------

_F = re.IGNORECASE

# (kind, severity, base_confidence, regex, reason)
_KNOWN_FORMATS = [
    (KIND_AWS_ACCESS_KEY, "critical", 0.9,
     re.compile(r"\b((?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16})\b"),
     "matches the AWS access key ID shape: a fixed 4-letter prefix plus 16 "
     "uppercase-alphanumeric characters"),
    (KIND_GITHUB_TOKEN, "critical", 0.9,
     re.compile(r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,255})\b"),
     "matches a GitHub personal-access or app token prefix"),
    (KIND_SLACK_TOKEN, "high", 0.85,
     re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,72})\b"),
     "matches a Slack bot/user/app token prefix (xoxb-, xoxp-, ...)"),
    (KIND_SLACK_WEBHOOK, "high", 0.8,
     re.compile(r"(https://hooks\.slack\.com/services/T[A-Za-z0-9]{6,}/B[A-Za-z0-9]{6,}/[A-Za-z0-9]{16,})"),
     "matches a Slack incoming-webhook URL, which can post to a channel without further auth"),
    (KIND_GOOGLE_API_KEY, "high", 0.75,
     re.compile(r"\b(AIza[0-9A-Za-z_\-]{35})\b"),
     "matches the Google API key shape (AIza prefix, 39 characters total)"),
    (KIND_OPENAI_STYLE_KEY, "high", 0.75,
     re.compile(r"\b(sk-[A-Za-z0-9]{20,})\b"),
     "matches an OpenAI-style secret key prefix (sk- followed by a long random string)"),
    (KIND_JWT, "medium", 0.6,
     re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"),
     "matches a JSON Web Token: three base64url segments separated by dots, header starting 'eyJ'"),
]

_STRIPE_RE = re.compile(r"\b((?:sk|rk)_(live|test)_[A-Za-z0-9]{16,})\b")

_PEM_RE = re.compile(
    r"-----BEGIN ((?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY)-----"
    r"([\s\S]*?)"
    r"-----END \1-----"
)
# A PEM block with almost nothing between its markers is not real key
# material -- see the module docstring's "WHY NO CODE-STRUCTURE DISCOUNT"
# section for the concrete case this exists to handle: a test fixture that
# writes only the BEGIN line as a standalone string (agent/hearth_checkpoint.py
# does this), or a BEGIN/END pair separated by ordinary source code rather
# than a real body, both fail this length check and are never even
# considered as candidates.
_PEM_MIN_BODY_CHARS = 40

# The PEM detector is the ONLY one in this module whose match can span an
# unbounded amount of text the model chose: every other regex here caps its
# value group (256 characters for the two assignment forms, a fixed length
# for the known formats) or restricts it to an alphabet with no whitespace.
# _PEM_RE's `[\s\S]*?` body accepts anything at all between the markers,
# and a finding's [start, end) is what redact() collapses into a single
# "[REDACTED:...]" token. Left as the whole match, that made the redacted
# span attacker-chosen: a model could emit a realistic-looking base64
# prefix, append arbitrary text, close the block, and every byte of that
# text vanished from anything built on redact() -- including the approval
# card, whose entire job is to show what a write will do. Found by
# execution, not by reading: 273 characters concealed behind one token.
#
# The fix is to stop trusting the match's own extent. A PEM body IS base64
# and nothing else, so _pem_key_material_span() below picks out the longest
# run of whole lines that are pure base64, and the finding covers only that
# run. The BEGIN/END markers stay visible (the user should see that a key
# block is being written), and so does every line that is not key material,
# which is exactly the text an attacker would want hidden: a shell command,
# a URL, a sentence all carry characters outside this alphabet, so none of
# them can be swallowed into the span.
_PEM_B64_LINE_RE = re.compile(r"[A-Za-z0-9+/=]+")

# How much key material is judged at a time by _pem_body_is_placeholder.
# is_placeholder() is written for a single value of the length a variable
# assignment or a known key format produces, and one of its checks -- the
# unique-character ratio -- does not survive being handed a long one: a
# genuine 900-character base64 body draws from a 64-symbol alphabet, so its
# ratio is about 0.07, far under the 0.35 threshold, and the whole body
# would be dismissed as a placeholder. Judging it in windows keeps every
# is_placeholder check meaningful at the length it was tuned for.
_PEM_PLACEHOLDER_WINDOW = 120


def _pem_body_is_placeholder(material):
    """True only if EVERY window of `material` looks like a placeholder.

    Strictly stronger than the head slice this replaces. `is_placeholder(
    body[:120])` inspected a fixed 120-character prefix and decided the
    whole block from it, which cut both ways: a realistic prefix vouched
    for arbitrary text after it (the concealment bug documented above
    _PEM_RE), and a placeholder-looking prefix dismissed a real key sitting
    behind it. Requiring all windows to look fake before suppressing
    removes both.
    """
    if len(material) < _PEM_MIN_BODY_CHARS:
        return True
    for i in range(0, len(material), _PEM_PLACEHOLDER_WINDOW):
        window = material[i:i + _PEM_PLACEHOLDER_WINDOW]
        if i and len(window) < 20:
            continue  # a trailing scrap too short to judge on its own
        if not is_placeholder(window):
            return False
    return True


def _pem_key_material_span(body):
    """(start, end, char_count) of the longest run of consecutive whole
    lines inside `body` that are pure base64, or None if there is no such
    run. Offsets are relative to `body`.

    Whole LINES, not a character run: a real PEM body is a column of fixed-
    width base64 lines, and matching line by line is what makes a line
    carrying anything else (a space, a colon, a semicolon, a dot -- every
    readable sentence, command, or URL has at least one) unable to join the
    run at all rather than merely truncating it.
    """
    best = None
    best_chars = 0
    run_start = run_end = None
    run_chars = 0
    pos = 0
    n = len(body)
    while pos <= n:
        nl = body.find("\n", pos)
        line_end = n if nl == -1 else nl
        raw = body[pos:line_end]
        stripped = raw.strip()
        if stripped and _PEM_B64_LINE_RE.fullmatch(stripped):
            lead = len(raw) - len(raw.lstrip())
            if run_start is None:
                run_start = pos + lead
                run_chars = 0
            run_end = pos + lead + len(stripped)
            run_chars += len(stripped)
        else:
            if run_start is not None and run_chars > best_chars:
                best, best_chars = (run_start, run_end, run_chars), run_chars
            run_start = run_end = None
            run_chars = 0
        if nl == -1:
            break
        pos = nl + 1
    if run_start is not None and run_chars > best_chars:
        best, best_chars = (run_start, run_end, run_chars), run_chars
    return best

_CONN_STRING_RE = re.compile(
    r"\b([a-zA-Z][a-zA-Z0-9+.\-]{1,15})://"
    r"([^\s:/@\"'<>]{1,64}):([^\s:/@\"'<>]{1,256})@"
    r"([^\s\"'<>]{1,255})"
)

# ---------------------------------------------------------------------------
# Generic suspicious-name + high-entropy assignment detector
# ---------------------------------------------------------------------------
#
# Quoted form covers any language that assigns a name to a quoted string:
# Python (`api_key = "..."`), JSON (`"api_key": "..."`), YAML/TOML with
# quotes, JS/TS. The name group is intentionally broad (any identifier) --
# specificity comes from _name_is_suspicious() checking the captured name
# AFTER the match, not from narrowing the regex itself, precisely so that
# compound identifiers like STRIPE_SECRET_KEY are not missed the way a
# \bapi_key\b-style alternation would miss them (see _name_is_suspicious's
# comment for the underscore-boundary problem this sidesteps).
#
# The captured value excludes newlines (`[^"\n]`, not `[^"]`) on purpose: a
# real quoted-string assignment is always on one line, and without this a
# name+operator with an unclosed-looking quote (any prose sentence
# containing the literal text `api_key = "`, for instance -- which this
# very module's own docstrings and comments do, while explaining this exact
# bug) searches forward past the end of the line for the next unrelated `"`
# anywhere in the next 256 characters and swallows everything in between as
# a fake "value." Found in review: this file's own self-test source
# tripped its own repo-wide sweep this way twice, once through a docstring
# and once through a code comment, before this line was added.
_QUOTED_ASSIGN_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\b\s*[\"']?\s*[:=]\s*"
    r"(?:\"([^\"\n]{8,256})\"|'([^'\n]{8,256})')"
)

# Bare .env / shell-export form: KEY=value with no quotes, name in
# SCREAMING_SNAKE_CASE at the start of a line. The value charclass excludes
# Python/shell call-and-lookup punctuation ( ) [ ] { } , ; on top of
# whitespace and quotes: without that exclusion, a plain assignment
# statement like `token = hearth_tools.find_canary(result)` has no
# whitespace between "=" and the end of the line, so the bare-value group
# happily swallows the entire right-hand side as if it were a secret
# (found in review, scanning agent/hearth_loop.py, a real false positive
# this fixed). Deliberately does not try to catch an unquoted lowercase
# form (`api_key: AKIA...` in YAML) -- see the module docstring's Known
# Limitations for why that gap was left rather than closed with a
# broader, riskier regex.
_BARE_ENV_ASSIGN_RE = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*([^\s#\"'(){}\[\],;]{8,256})[ \t]*$"
)

_MIN_REPORT_LEN = 8


def _overlaps(start, end, spans):
    return any(start < e and end > s for s, e in spans)


def _make_finding(kind, severity, confidence, base_offset, text_offset,
                   window_line_starts, base_line, start, end, secret_value, reason):
    line = base_line - 1 + _line_in_chunk(window_line_starts, start)
    return {
        "kind": kind,
        "severity": severity,
        "confidence": round(min(1.0, confidence), 3),
        "line": line,
        "start": base_offset + start,
        "end": base_offset + end,
        "masked": _mask(secret_value),
        "reason": reason,
    }


def _scan_known_formats(chunk, base_offset, line_starts, base_line, covered):
    findings = []
    for kind, severity, confidence, regex, reason in _KNOWN_FORMATS:
        for m in regex.finditer(chunk):
            value = m.group(1)
            start, end = m.start(1), m.end(1)
            if is_placeholder(value, check_entropy=False):
                continue
            findings.append(_make_finding(
                kind, severity, confidence, base_offset, base_offset,
                line_starts, base_line, start, end, value, reason,
            ))
            covered.append((start, end))
    return findings


def _scan_stripe(chunk, base_offset, line_starts, base_line, covered):
    findings = []
    for m in _STRIPE_RE.finditer(chunk):
        value, mode = m.group(1), m.group(2)
        if is_placeholder(value, check_entropy=False):
            continue
        start, end = m.start(1), m.end(1)
        if mode == "live":
            severity, confidence = "critical", 0.9
            reason = "matches a live Stripe secret/restricted key prefix (sk_live_/rk_live_)"
        else:
            severity, confidence = "medium", 0.5
            reason = ("matches a Stripe TEST-mode key prefix (sk_test_/rk_test_): not usable "
                       "against real customer data, but still a credential that should not be "
                       "committed or shared")
        findings.append(_make_finding(
            KIND_STRIPE_KEY, severity, confidence, base_offset, base_offset,
            line_starts, base_line, start, end, value, reason,
        ))
        covered.append((start, end))
    return findings


def _scan_pem(chunk, base_offset, line_starts, base_line, covered):
    findings = []
    for m in _PEM_RE.finditer(chunk):
        span = _pem_key_material_span(m.group(2))
        if span is None:
            continue
        rel_start, rel_end, n_chars = span
        if n_chars < _PEM_MIN_BODY_CHARS:
            continue
        start = m.start(2) + rel_start
        end = m.start(2) + rel_end
        # The whole run, newlines stripped out, not the first 120 characters
        # of whatever sat between the markers. The old head slice was the
        # other half of the concealment bug documented above _PEM_RE: a
        # realistic base64 prefix satisfied it and the rest of the block --
        # placeholder text or attacker payload alike -- was never looked at.
        material = "".join(chunk[start:end].split())
        if _pem_body_is_placeholder(material):
            continue
        findings.append(_make_finding(
            KIND_PRIVATE_KEY_PEM, "critical", 0.95, base_offset, base_offset,
            line_starts, base_line, start, end,
            "<{} char PEM body>".format(n_chars),
            "a PEM-encoded private key block (BEGIN/END PRIVATE KEY markers with real "
            "content between them); this is a full credential, not a fingerprint of one",
        ))
        covered.append((start, end))
    return findings


def _scan_connection_strings(chunk, base_offset, line_starts, base_line, covered):
    findings = []
    for m in _CONN_STRING_RE.finditer(chunk):
        scheme, user, password, host = m.group(1), m.group(2), m.group(3), m.group(4)
        if is_placeholder(password):
            continue
        start, end = m.start(), m.end()
        findings.append(_make_finding(
            KIND_CONNECTION_STRING, "high", 0.7, base_offset, base_offset,
            line_starts, base_line, start, end, password,
            "a {}:// connection string embeds a plaintext username and password inline "
            "rather than referencing them out of band".format(scheme),
        ))
        covered.append((start, end))
    return findings


def _scan_generic_assignment(chunk, base_offset, line_starts, base_line, covered):
    findings = []
    for m in _QUOTED_ASSIGN_RE.finditer(chunk):
        name = m.group(1)
        value = m.group(2) if m.group(2) is not None else m.group(3)
        if value is None or len(value) < _MIN_REPORT_LEN:
            continue
        if not _name_is_suspicious(name):
            continue
        vgroup = 2 if m.group(2) is not None else 3
        start, end = m.start(vgroup), m.end(vgroup)
        if _overlaps(start, end, covered):
            continue
        if is_placeholder(value):
            continue
        findings.append(_make_finding(
            KIND_GENERIC_ASSIGNMENT, _severity_for_name(name), 0.5, base_offset,
            base_offset, line_starts, base_line, start, end, value,
            "a high-entropy value is assigned to '{}', a name that suggests a "
            "credential, and the value does not look like a placeholder".format(name),
        ))
        covered.append((start, end))

    for m in _BARE_ENV_ASSIGN_RE.finditer(chunk):
        name, value = m.group(1), m.group(2)
        if not _name_is_suspicious(name):
            continue
        start, end = m.start(2), m.end(2)
        if _overlaps(start, end, covered):
            continue
        if is_placeholder(value):
            continue
        findings.append(_make_finding(
            KIND_GENERIC_ASSIGNMENT, _severity_for_name(name), 0.5, base_offset,
            base_offset, line_starts, base_line, start, end, value,
            "a high-entropy value is assigned to '{}' in KEY=value form, a name that "
            "suggests a credential, and the value does not look like a placeholder".format(name),
        ))
        covered.append((start, end))
    return findings


# ---------------------------------------------------------------------------
# Top-level scan
# ---------------------------------------------------------------------------


def scan(text, path=None):
    """Scan text that is about to be written for signs of a live credential.

    Returns a dict:
      path            - whatever the caller passed (a file path being
                         written, a diff hunk's target, ...), unexamined,
                         for the caller's own bookkeeping.
      total_chars     - len(text).
      scanned_chars   - how many characters were actually scanned (may be
                        less than total_chars; see MAX_SCAN_CHARS).
      truncated       - True if the scan window did not cover the full text.
      count           - len(findings).
      severity        - the highest severity among findings, or "none".
      findings        - list of finding dicts, each with kind, severity,
                        confidence (0-1), line (1-indexed), start/end
                        offsets into the original text, a masked preview
                        (never the raw secret; see _mask), and a one-
                        sentence reason.
      summary         - one-line human-readable summary.

    This function has no side effects and performs no I/O. It does not
    modify, block, or redact anything it is given; see redact() for an
    opt-in way to render a redacted copy of `text` using the offsets in
    the findings this function returns.

    Detect and surface, do not block: see the module docstring. A finding
    here is a signal for the approval layer above, never a decision made
    on the caller's behalf.
    """
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)

    windows, truncated = _scan_windows(text)

    findings = []
    scanned_chars = 0
    for offset, chunk in windows:
        scanned_chars += len(chunk)
        line_starts = _line_starts(chunk)
        base_line = _base_line_number(text, offset)
        covered = []
        findings.extend(_scan_known_formats(chunk, offset, line_starts, base_line, covered))
        findings.extend(_scan_stripe(chunk, offset, line_starts, base_line, covered))
        findings.extend(_scan_pem(chunk, offset, line_starts, base_line, covered))
        findings.extend(_scan_connection_strings(chunk, offset, line_starts, base_line, covered))
        findings.extend(_scan_generic_assignment(chunk, offset, line_starts, base_line, covered))

    findings.sort(key=lambda f: f["start"])

    if findings:
        severity = SEVERITY[max(_SEVERITY_RANK[f["severity"]] for f in findings)]
    else:
        severity = "none"

    by_sev = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    if findings:
        parts = ", ".join(
            "{} {}".format(by_sev[s], s) for s in reversed(SEVERITY) if s in by_sev
        )
        summary = "{} finding(s) ({}); overall severity: {}".format(len(findings), parts, severity)
    else:
        summary = "no findings; overall severity: none"

    return {
        "path": path,
        "total_chars": len(text),
        "scanned_chars": scanned_chars,
        "truncated": truncated,
        "count": len(findings),
        "severity": severity,
        "findings": findings,
        "summary": summary,
    }


# The most any single finding may hide behind one "[REDACTED:...]" token.
# A second, span-shape-independent bound on the concealment problem
# documented above _PEM_RE: that fix makes a PEM finding cover only base64
# key material, and this one makes the amount of it bounded regardless of
# which detector produced the span or how its regex is later changed. Sized
# well clear of anything real: a 4096-bit RSA private key is about 3,200
# base64 characters and a 16384-bit one about 12,000, so 8,192 leaves every
# key size in ordinary use untouched. Past this, redact() REFUSES to
# collapse the span and leaves the text visible instead, which is the safe
# direction: showing the user an implausibly large blob they can read is a
# smaller failure than hiding an unbounded amount of text from them on an
# approval card that claims to show exactly what will be written.
MAX_REDACT_SPAN = 8192


def _redaction_spans(text, findings, max_span):
    """Ordered, non-overlapping (start, end, kind, collapse) tuples.
    collapse is False for a span the cap refuses to hide."""
    spans = [
        (f["start"], f["end"], f["kind"])
        for f in findings
        if 0 <= f["start"] < f["end"] <= len(text)
    ]
    spans.sort(key=lambda s: s[0])
    out = []
    cursor = 0
    for start, end, kind in spans:
        if start < cursor:
            continue  # overlapping span; skip rather than corrupt offsets
        collapse = max_span is None or (end - start) <= max_span
        out.append((start, end, kind, collapse))
        cursor = end
    return out


def redact(text, findings, max_span=MAX_REDACT_SPAN):
    """Return a copy of `text` with every finding's [start, end) span
    replaced by a fixed, kind-labelled placeholder that does not reproduce
    the secret. Findings from `scan()` already carry only a masked preview
    (see _mask), never the raw secret, so this function's own inputs are
    already safe to have passed through a log; this exists for the case a
    caller has BOTH the original text and the findings on hand (the normal
    case, immediately after calling scan()) and wants a safe-to-display or
    safe-to-log copy of the text itself, not just the finding list.

    A span longer than `max_span` is left VISIBLE rather than collapsed;
    see MAX_REDACT_SPAN for why that is the safe direction. Pass
    max_span=None to restore the old unbounded behaviour, which the
    self-test does to prove the cap is load-bearing.
    """
    spans = _redaction_spans(text, findings, max_span)
    if not spans:
        return text
    out = []
    cursor = 0
    for start, end, kind, collapse in spans:
        if not collapse:
            continue
        out.append(text[cursor:start])
        out.append("[REDACTED:{}]".format(kind))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def redaction_summary(text, findings, max_span=MAX_REDACT_SPAN):
    """How much redact() would hide, so a caller can SAY so rather than
    quietly doing it.

      concealed_chars - characters of `text` that redact() replaces.
      collapsed       - how many findings were collapsed.
      oversized       - how many were left visible because of the cap.
      longest_span    - the largest collapsed span, in characters.

    An approval card that hides part of a write is honest only if the
    amount hidden is itself on the card; this is the number to put there.
    """
    spans = _redaction_spans(text, findings, max_span)
    concealed = sum(e - s for s, e, _k, c in spans if c)
    longest = max((e - s for s, e, _k, c in spans if c), default=0)
    return {
        "concealed_chars": concealed,
        "collapsed": sum(1 for *_x, c in spans if c),
        "oversized": sum(1 for *_x, c in spans if not c),
        "longest_span": longest,
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

import secrets as _secrets_mod
import string as _string_mod

_ALNUM = _string_mod.ascii_letters + _string_mod.digits


def _rand_alnum(n):
    return "".join(_secrets_mod.choice(_ALNUM) for _ in range(n))


def _quoted_assignment(name, value):
    """Build a NAME, space, equals, space, quoted VALUE string at runtime,
    via a one-character `q` variable rather than a literal quote mark
    placed directly next to the assignment in the caller's source.

    This exists because of a subtlety in how this module's own regex-based
    detector reads text: it works on raw characters, not parsed Python, so
    it cannot tell a real opening quote from one that merely LOOKS like the
    start of a quoted value in a comment or docstring. If a name, an
    operator, and a bare quote character sit next to each other anywhere in
    this file's own source text -- including inside prose explaining this
    exact behaviour -- and some unrelated quote character happens to follow
    within the next couple hundred characters (routine in any Python file:
    a dict key, an f-string, another comment), the detector reads
    everything in between as one long candidate secret. Found in review,
    twice, scanning this file's own repo-wide-sweep fixture: once through a
    fixture-building helper that concatenated literal quote marks directly
    around a value, and once through an earlier draft of this very
    docstring that spelled the broken pattern out to explain it. Routing
    every synthetic quoted fixture through this one function, and phrasing
    this explanation without ever writing a bare name-operator-quote
    sequence, keeps both fixes in one place."""
    q = '"'
    return name + " = " + q + value + q


def _build_real_secret_fixtures():
    """Build realistic, structurally-valid-but-synthetic secrets AT RUNTIME,
    never as a contiguous literal in this file's own source. See the module
    docstring's "WHY NO CODE-STRUCTURE DISCOUNT" section: this module does
    not discount matches that fall inside a Python string literal (doing so
    would hide the exact failure mode -- a hardcoded secret in source -- it
    exists to catch), so the burden of not tripping this file's own
    repo-wide self-scan falls on how the self-test builds its fixtures, not
    on the scanner suppressing itself. Every value below is assembled from
    short, non-matching pieces plus `secrets`, so the matching string never
    exists as static text on disk -- and see _quoted_assignment() above for
    why the ASSEMBLY code itself also has to avoid leaving adjacent quote
    characters in this file's own source."""
    aws_key = "AKIA" + "".join(_secrets_mod.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(16))
    github_token = "ghp_" + _rand_alnum(36)
    slack_token = "xoxb-" + _secrets_mod.token_hex(20)
    stripe_live = "sk_live_" + _rand_alnum(24)
    stripe_test = "sk_test_" + _rand_alnum(24)
    google_key = "AIza" + _rand_alnum(35)
    openai_key = "sk-" + _rand_alnum(40)
    jwt = "eyJ" + _rand_alnum(20) + "." + _rand_alnum(30) + "." + _rand_alnum(30)
    pem_body = "\n".join(_rand_alnum(64) for _ in range(14))
    pem = "-----BEGIN RSA PRIVATE KEY-----\n" + pem_body + "\n-----END RSA PRIVATE KEY-----"
    conn_str = "postgres://svc_" + _rand_alnum(6) + ":" + _rand_alnum(28) + "@db.internal:5432/prod"
    generic_quoted = _quoted_assignment("api_key", _rand_alnum(40))
    # "=" kept as its own literal, not fused onto "DATABASE_PASSWORD", for
    # the same raw-text-adjacency reason documented on _quoted_assignment().
    generic_bare = "DATABASE_PASSWORD" + "=" + _rand_alnum(32)
    compound_name_quoted = _quoted_assignment("STRIPE_SECRET_KEY", _rand_alnum(30))
    return {
        "aws": aws_key,
        "github": github_token,
        "slack": slack_token,
        "stripe_live": stripe_live,
        "stripe_test": stripe_test,
        "google": google_key,
        "openai": openai_key,
        "jwt": jwt,
        "pem": pem,
        "conn_str": conn_str,
        "generic_quoted": generic_quoted,
        "generic_bare": generic_bare,
        "compound_name_quoted": compound_name_quoted,
    }


# Placeholders and documented example values that must NEVER trip. These are
# safe to write as static literals: the whole point is that they must not be
# flagged regardless of context, so having them sit right here in this
# module's own source, and the repo-wide sweep below confirming this file
# itself stays quiet, is a positive proof, not a risk.
_KNOWN_NEGATIVES = [
    "AKIAIOSFODNN7EXAMPLE",  # AWS's own documented example access key ID
    "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "YOUR_API_KEY_HERE",
    "<token>",
    "<your-api-key>",
    "ghp_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    "xoxb-your-slack-token-here",
    "AKIA0000000000000000",
    "aws_secret_access_key = 'changeme'",
    'password = "changeme"',
    'token = "1234567890123456"',
    'api_key: "${API_KEY}"',
    'secret = "AWS_SECRET_ACCESS_KEY"',
    'client_secret = "the-real-token-value"',
]

_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_THIS_FILE_PATH = os.path.abspath(__file__)
_THREAT_MODEL_PATH = os.path.join(_REPO_ROOT, "docs", "security", "windows-threat-model.md")
_INJECTION_MODULE_PATH = os.path.join(_REPO_ROOT, "agent", "hearth_injection.py")


def _iter_repo_fixture_files():
    for sub in ("agent", os.path.join("desktop", "server")):
        d = os.path.join(_REPO_ROOT, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                yield os.path.join(d, name)
    for sub in ("docs", os.path.join("docs", "security")):
        d = os.path.join(_REPO_ROOT, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".md"):
                yield os.path.join(d, name)


def _self_test():
    # --- entropy / placeholder unit checks ---------------------------------
    assert is_placeholder("changeme")
    assert is_placeholder("the-real-token-value")
    assert is_placeholder("AKIAIOSFODNN7EXAMPLE")
    assert is_placeholder("YOUR_API_KEY_HERE")
    assert is_placeholder("<token>")
    assert is_placeholder("xxxxxxxxxxxxxxxxxxxx")
    assert is_placeholder("01234567890123456789")
    assert is_placeholder("AWS_SECRET_ACCESS_KEY")
    assert not is_placeholder(_rand_alnum(40))

    # --- direction 1: real key shapes score high ---------------------------
    fixtures = _build_real_secret_fixtures()

    r = scan(fixtures["aws"], path="fixture:aws")
    assert meets_threshold(r, "critical"), r
    assert any(f["kind"] == KIND_AWS_ACCESS_KEY for f in r["findings"]), r

    r = scan(fixtures["github"], path="fixture:github")
    assert meets_threshold(r, "critical"), r
    assert any(f["kind"] == KIND_GITHUB_TOKEN for f in r["findings"]), r

    r = scan(fixtures["slack"], path="fixture:slack")
    assert meets_threshold(r, "high"), r
    assert any(f["kind"] == KIND_SLACK_TOKEN for f in r["findings"]), r

    r = scan(fixtures["stripe_live"], path="fixture:stripe_live")
    assert meets_threshold(r, "critical"), r
    assert any(f["kind"] == KIND_STRIPE_KEY and f["severity"] == "critical" for f in r["findings"]), r

    r = scan(fixtures["stripe_test"], path="fixture:stripe_test")
    assert meets_threshold(r, "medium"), r
    assert not meets_threshold(r, "high"), (
        "a stripe TEST-mode key should score lower than a live one: {}".format(r)
    )

    r = scan(fixtures["google"], path="fixture:google")
    assert meets_threshold(r, "high"), r
    assert any(f["kind"] == KIND_GOOGLE_API_KEY for f in r["findings"]), r

    r = scan(fixtures["openai"], path="fixture:openai")
    assert meets_threshold(r, "high"), r
    assert any(f["kind"] == KIND_OPENAI_STYLE_KEY for f in r["findings"]), r

    r = scan(fixtures["jwt"], path="fixture:jwt")
    assert meets_threshold(r, "medium"), r
    assert any(f["kind"] == KIND_JWT for f in r["findings"]), r

    r = scan(fixtures["pem"], path="fixture:pem")
    assert meets_threshold(r, "critical"), r
    assert any(f["kind"] == KIND_PRIVATE_KEY_PEM for f in r["findings"]), r

    r = scan(fixtures["conn_str"], path="fixture:conn_str")
    assert meets_threshold(r, "high"), r
    assert any(f["kind"] == KIND_CONNECTION_STRING for f in r["findings"]), r

    r = scan(fixtures["generic_quoted"], path="fixture:generic_quoted")
    assert meets_threshold(r, "high"), r
    assert any(f["kind"] == KIND_GENERIC_ASSIGNMENT for f in r["findings"]), r

    r = scan(fixtures["generic_bare"], path="fixture:generic_bare")
    assert meets_threshold(r, "high"), r
    assert any(f["kind"] == KIND_GENERIC_ASSIGNMENT for f in r["findings"]), r

    # the compound-name case: STRIPE_SECRET_KEY has no \b boundary before
    # "secret" or "key" for a naive alternation regex; this proves the
    # segment-split name check catches it anyway.
    r = scan(fixtures["compound_name_quoted"], path="fixture:compound_name")
    assert meets_threshold(r, "high"), r

    # no findings ever carry the raw secret text in their fields. Checked
    # against the random TAIL of each fixture, not the whole value: the
    # fixed structural prefix (e.g. "sk_live_") legitimately appears in a
    # reason string describing the format, and that is not a leak.
    for kind_result in (fixtures["aws"], fixtures["github"], fixtures["stripe_live"]):
        random_tail = kind_result[-12:]
        r = scan(kind_result)
        for f in r["findings"]:
            assert kind_result not in str(f["masked"]), "masked field leaked the raw secret: {}".format(f)
            assert random_tail not in f.get("reason", ""), "reason leaked the raw secret: {}".format(f)

    # --- direction 2: placeholders and documented examples do not trip -----
    for i, value in enumerate(_KNOWN_NEGATIVES):
        r = scan(value, path="known_negative[{}]".format(i))
        assert not meets_threshold(r, "low"), (
            "known-negative {} unexpectedly scored {} ({}): {!r}\nfindings={}".format(
                i, r["severity"], r["count"], value, r["findings"]
            )
        )

    # a specific, explicit assertion on the AWS documented example key, since
    # it is the sharpest case in this module's brief: structurally a perfect
    # AWS access key (correct prefix, correct length, correct alphabet), and
    # it must still score "none" because it is a publicly documented example.
    r = scan("aws_access_key_id = AKIAIOSFODNN7EXAMPLE")
    assert r["severity"] == "none", "AWS's own documented example key tripped: {}".format(r)

    # --- benign fixtures: this repository's own files must stay quiet ------
    checked = 0
    for path in _iter_repo_fixture_files():
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        r = scan(content, path=path)
        assert not meets_threshold(r, "high"), (
            "{} scored too high: {} ({})\nfindings: {}".format(
                path, r["severity"], r["count"], r["findings"]
            )
        )
        checked += 1
    assert checked >= 10, "expected to sweep at least 10 repo files, only found {}".format(checked)

    # The two named adversarial fixtures from the brief, checked explicitly
    # (also covered by the sweep above, but named here so a failure points
    # straight at the sharpest case rather than an unlabelled loop index).
    for path in (_THREAT_MODEL_PATH, _INJECTION_MODULE_PATH):
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        r = scan(content, path=path)
        assert not meets_threshold(r, "high"), (
            "{} scored too high: {} ({})\nfindings: {}".format(
                path, r["severity"], r["count"], r["findings"]
            )
        )

    # This module's own source must not trip on itself either: its
    # self-test fixtures are assembled at runtime specifically so this
    # holds (see _build_real_secret_fixtures's docstring).
    with open(_THIS_FILE_PATH, "r", encoding="utf-8") as fh:
        own_source = fh.read()
    r = scan(own_source, path=_THIS_FILE_PATH)
    assert not meets_threshold(r, "high"), (
        "hearth_secrets.py scored too high on itself: {} ({})\nfindings: {}".format(
            r["severity"], r["count"], r["findings"]
        )
    )

    # --- prove placeholder suppression is NOT vacuous -----------------------
    # Disable it, rerun the benign fixtures that specifically depend on it
    # (the known-negatives, plus this file's own honeyfile-decoy-adjacent
    # source in hearth_tools.py), and confirm they NOW trip. If they did
    # not, the suppression was never the thing keeping them quiet and the
    # "not meets_threshold(..., 'high')" assertions above would have passed
    # for the wrong reason.
    global _PLACEHOLDER_SUPPRESSION_ENABLED
    tripped_without_suppression = []
    passed_with_suppression_disabled_anyway = []
    _PLACEHOLDER_SUPPRESSION_ENABLED = False
    try:
        for i, value in enumerate(_KNOWN_NEGATIVES):
            r = scan(value)
            if meets_threshold(r, "low"):
                tripped_without_suppression.append(("known_negative[{}]".format(i), r["severity"]))
            else:
                passed_with_suppression_disabled_anyway.append(("known_negative[{}]".format(i), value))

        hearth_tools_path = os.path.join(_REPO_ROOT, "agent", "hearth_tools.py")
        if os.path.exists(hearth_tools_path):
            with open(hearth_tools_path, "r", encoding="utf-8") as fh:
                content = fh.read()
            r = scan(content, path=hearth_tools_path)
            if meets_threshold(r, "high"):
                tripped_without_suppression.append(("hearth_tools.py", r["severity"]))
    finally:
        _PLACEHOLDER_SUPPRESSION_ENABLED = True

    assert tripped_without_suppression, (
        "suppression toggle had no effect: disabling _PLACEHOLDER_SUPPRESSION_ENABLED "
        "did not raise ANY benign fixture's severity, so placeholder suppression is "
        "not proven to do anything. Passed anyway (still quiet with suppression off): {}".format(
            passed_with_suppression_disabled_anyway
        )
    )

    # With suppression restored, the same fixtures must pass again.
    for i, value in enumerate(_KNOWN_NEGATIVES):
        r = scan(value)
        assert not meets_threshold(r, "low"), (
            "known-negative {} did not restore correctly after the suppression toggle: {!r} -> {}".format(
                i, value, r
            )
        )

    print("--- non-vacuous placeholder suppression proof ---")
    print("WITH suppression disabled, now trip (severity):")
    for name, sev in tripped_without_suppression:
        print("  {} -> {}".format(name, sev))
    print("WITH suppression restored: all {} known-negatives pass again".format(len(_KNOWN_NEGATIVES)))
    print("--------------------------------------------------")

    # --- redact() -----------------------------------------------------------
    secret_text = "here is my key: " + fixtures["aws"] + " -- do not share it"
    r = scan(secret_text)
    assert r["findings"]
    redacted = redact(secret_text, r["findings"])
    assert fixtures["aws"] not in redacted
    assert "do not share it" in redacted
    assert "[REDACTED:{}]".format(KIND_AWS_ACCESS_KEY) in redacted

    # redact() with no findings is a no-op
    assert redact("nothing to see here", []) == "nothing to see here"

    # --- redaction may not conceal attacker-chosen text ---------------------
    # Built from the working attack, not from a friendly input. A model that
    # wants a line hidden from the approval card opens a PEM block, emits a
    # realistic-looking base64 prefix (a short or code-like body scores
    # "none", so the decoy is load-bearing), appends the payload, and closes
    # the block. Before _pem_key_material_span existed, the finding's span
    # was the whole match, redact() collapsed all of it into one token, and
    # 273 characters -- payload included -- were absent from the card while
    # still reaching disk.
    pem_decoy = "\n".join(
        "".join(_secrets_mod.choice(_ALNUM + "+/") for _ in range(60)) for _ in range(2)
    )
    pem_payload = ("ATTACKER PAYLOAD THE USER NEVER SEES: powershell -enc SQBFAFgA "
                   "; net user bad P@ss /add")
    pem_attack = ("# notes\n"
                  "-----BEGIN PRIVATE KEY-----\n"
                  + pem_decoy + "\n"
                  + pem_payload + "\n"
                  "-----END PRIVATE KEY-----\n"
                  "tail\n")
    r = scan(pem_attack, path="config.ini")
    assert meets_threshold(r, "high"), (
        "the decoy prefix must still be recognised as key material, or this "
        "test proves nothing about redaction: {}".format(r)
    )
    shown = redact(pem_attack, r["findings"])
    assert pem_payload in shown, (
        "PEM redaction concealed attacker-chosen text that was not key "
        "material; the approval card would hide it from the user while the "
        "write still lands on disk.\ncard would show:\n{}".format(shown)
    )
    for keep in ("# notes", "tail", "-----BEGIN PRIVATE KEY-----",
                 "-----END PRIVATE KEY-----"):
        assert keep in shown, "redaction swallowed {!r}: {}".format(keep, shown)
    for decoy_line in pem_decoy.split("\n"):
        assert decoy_line not in shown, (
            "the base64 key material itself must still be hidden: {}".format(shown))

    # the same attack under the OLD span rule (whole match) must still hide
    # the payload -- otherwise the assertion above passes for the wrong
    # reason and _pem_key_material_span is not what is holding the line.
    whole_match_finding = [dict(r["findings"][0])]
    m_old = _PEM_RE.search(pem_attack)
    whole_match_finding[0]["start"] = m_old.start()
    whole_match_finding[0]["end"] = m_old.end()
    assert pem_payload not in redact(pem_attack, whole_match_finding), (
        "the old whole-match span no longer conceals the payload, so this "
        "regression test is vacuous"
    )

    # the head slice cut the other way too, and that direction is a missed
    # detection rather than a concealment: a block whose first 120 characters
    # are obvious filler used to vouch for everything behind it, so a real
    # key parked after the filler was never reported at all. Every window
    # must look fake before the block is dismissed.
    filler_first = ("x" * 60 + "\n" + "x" * 60 + "\n"
                    + "\n".join(_rand_alnum(64) for _ in range(10)))
    r = scan("-----BEGIN PRIVATE KEY-----\n" + filler_first + "\n-----END PRIVATE KEY-----")
    assert meets_threshold(r, "critical"), (
        "a real key behind a placeholder-looking prefix was dismissed on the "
        "strength of the prefix alone: {}".format(r)
    )

    # --- and a genuine private key is still protected -----------------------
    real_pem = fixtures["pem"]
    r = scan(real_pem, path="id_rsa")
    assert meets_threshold(r, "critical"), r
    assert any(f["kind"] == KIND_PRIVATE_KEY_PEM for f in r["findings"]), r
    shown_real = redact(real_pem, r["findings"])
    for line in real_pem.split("\n"):
        if line.startswith("-----"):
            assert line in shown_real, "the markers should stay visible: {}".format(shown_real)
        else:
            assert line not in shown_real, (
                "a real private key's material leaked onto the card: {}".format(shown_real))
    assert "[REDACTED:{}]".format(KIND_PRIVATE_KEY_PEM) in shown_real

    # --- the cap on how much one finding may hide ---------------------------
    # An absurdly large all-base64 body is the residual shape that CAN still
    # enter a span (it carries no character the line filter would reject),
    # so the cap is the bound that does not depend on span shape at all.
    over = "\n".join(_rand_alnum(64) for _ in range(MAX_REDACT_SPAN // 64 + 40))
    over_pem = "-----BEGIN PRIVATE KEY-----\n" + over + "\n-----END PRIVATE KEY-----"
    r = scan(over_pem)
    assert r["findings"], r
    assert (r["findings"][0]["end"] - r["findings"][0]["start"]) > MAX_REDACT_SPAN, r
    shown_over = redact(over_pem, r["findings"])
    assert over.split("\n")[0] in shown_over, (
        "a span over MAX_REDACT_SPAN must be left visible, not collapsed")
    summary_over = redaction_summary(over_pem, r["findings"])
    assert summary_over["oversized"] == 1 and summary_over["concealed_chars"] == 0, summary_over
    # non-vacuous: with the cap lifted, the same span IS collapsed.
    assert over.split("\n")[0] not in redact(over_pem, r["findings"], max_span=None), (
        "MAX_REDACT_SPAN is not what left that span visible; the cap is vacuous")

    # --- redaction_summary reports what was hidden --------------------------
    r = scan(real_pem)
    summary = redaction_summary(real_pem, r["findings"])
    assert summary["collapsed"] == 1 and summary["oversized"] == 0, summary
    assert summary["concealed_chars"] > 0, summary
    assert summary["concealed_chars"] == len(real_pem) - len(shown_real) + len(
        "[REDACTED:{}]".format(KIND_PRIVATE_KEY_PEM)), summary
    assert redaction_summary("nothing here", []) == {
        "concealed_chars": 0, "collapsed": 0, "oversized": 0, "longest_span": 0}

    # --- no detector may hand redact() an unbounded span --------------------
    # The property the PEM bug violated, asserted for every detector at once
    # rather than for the one that happened to break: a finding's span is
    # what disappears from an approval card, so no scan of a hostile
    # document may produce one longer than the cap allows to be hidden.
    hostile = "\n".join([
        pem_attack,
        "sk-" + _rand_alnum(4000),
        "postgres://u:" + _rand_alnum(200) + "@h/db",
        _quoted_assignment("api_key", _rand_alnum(240)),
        "-----BEGIN EC PRIVATE KEY-----\n" + _rand_alnum(64) + "\nrm -rf /\n"
        + _rand_alnum(64) + "\n-----END EC PRIVATE KEY-----",
    ])
    hostile_scan = scan(hostile)
    hostile_shown = redact(hostile, hostile_scan["findings"])
    assert "rm -rf /" in hostile_shown, hostile_shown
    for f in hostile_scan["findings"]:
        assert (f["end"] - f["start"]) <= MAX_REDACT_SPAN, (
            "detector {} produced a span longer than MAX_REDACT_SPAN and would "
            "hide {} characters behind one token: {}".format(
                f["kind"], f["end"] - f["start"], f))

    # --- scan window bounding ------------------------------------------------
    huge = "x" * (MAX_SCAN_CHARS + 5000)
    r = scan(huge)
    assert r["truncated"] is True
    assert r["scanned_chars"] == MAX_SCAN_CHARS

    small = "hello world"
    r = scan(small)
    assert r["truncated"] is False
    assert r["scanned_chars"] == len(small)
    assert r["severity"] == "none"
    assert r["count"] == 0
    assert r["findings"] == []

    # a secret placed only past the head window but inside the tail window
    # is still caught (proves the tail slice, not just the head, is scanned).
    # Built via a `quote` variable rather than adjacent literal quote marks,
    # so the raw SOURCE TEXT of this line never itself contains a quoted
    # "name = value" shape (found in review: an earlier version wrote the
    # quotes inline, which made this file's own source -- not the runtime
    # string -- accidentally match its own quoted-assignment detector).
    quote = '"'
    padded = ("filler " * 20000) + "api_key = " + quote + _rand_alnum(40) + quote
    r = scan(padded)
    assert r["truncated"] is True
    assert meets_threshold(r, "high"), "tail-window secret was missed: {}".format(r)

    # --- line numbers ---------------------------------------------------------
    multiline = "line one\nline two\napi_key = \"{}\"\nline four".format(_rand_alnum(40))
    r = scan(multiline)
    assert r["findings"], r
    assert r["findings"][0]["line"] == 3, r["findings"]

    # --- non-string input does not raise --------------------------------------
    r = scan(None)
    assert r["severity"] == "none"
    r = scan(12345)
    assert isinstance(r["findings"], list)

    print("hearth-secrets self-test OK ({} repo files swept clean)".format(checked))
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else _self_test())
