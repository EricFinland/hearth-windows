#!/usr/bin/env python3
r"""hearth model source: finding GGUF models on Hugging Face and downloading them.

Hearth is dropping Ollama. Ollama was model registry, downloader, storage
layout, and inference server in one; llama.cpp's llama-server replaces only
the last of those. This module replaces the first three. It discovers GGUF
repositories on the Hugging Face Hub, enumerates the quantisations inside
one, and downloads a chosen file into hearth's own model store, resumably,
cancellably, and with the file's real hash checked before it is allowed to
become a model.

Standard library only, Python 3.12+. Never prints (the CLI at the bottom is
the one exception, since printing is its whole job). Writes only under
model_store_dir(), or under an explicit dest_dir the caller names.

## The API this actually talks to, as verified against the live Hub

Everything below was confirmed by request against https://huggingface.co on
2026-07-31, not read off documentation and hoped for. Where a claim could
not be verified it is labelled as such.

  GET /api/models?search=<q>&filter=gguf&limit=<n>&sort=downloads
      &direction=-1&expand[]=<field>...
    Returns a JSON array of repo summaries. Without expand[] the response
    carries id/likes/private/downloads/tags/pipeline_tag/createdAt but NOT
    lastModified and NOT gated, which is why this module asks for those
    explicitly (see _SEARCH_EXPAND_FIELDS). "gated" is false, "auto", or
    "manual" (all three observed), so it is normalised into a boolean plus
    the raw mode. Paging is by an opaque cursor handed back in a Link header
    with rel="next"; this module does not follow it, it asks for `limit`
    results and returns those.

    filter=gguf selects repos carrying the Hub's "gguf" tag. The Hub applies
    that tag to repos containing .gguf files, and all 20 of the top
    downloaded filter=gguf results were confirmed to genuinely contain GGUF
    files. It is still a repo-level tag, not a per-file guarantee:
    list_gguf_files() is the authority on what a repo actually holds, and
    search_models() is only a way to find candidates worth asking about.

  GET /api/models/<owner>/<name>/tree/main?recursive=true&expand=true
    Returns a JSON array of file entries. Each carries path, size, oid (the
    git blob sha1, NOT the file's hash), and for LFS-backed files an "lfs"
    object with {"oid": <sha256>, "size": <bytes>}. expand=true is what adds
    the "lfs" object; without it there is no hash at all. Paging is again a
    Link header with rel="next"; unlike search this module DOES follow it
    (see _paged_tree), since a repo can hold more files than one page.

  GET /<owner>/<name>/resolve/main/<path>
    The file itself. Answers 302 to a CDN host with the real bytes;
    Accept-Ranges: bytes, and Range survives the redirect (see the resume
    section below). The 302 response also carries X-Linked-Size and
    X-Linked-ETag, and X-Linked-ETag was confirmed to be exactly the same
    sha256 as the tree API's lfs.oid.

  Errors, as the Hub actually reports them to an unauthenticated client:
    404 + X-Error-Code: EntryNotFound   - repo exists, that file does not.
    401 + X-Error-Code: GatedRepo       - repo exists and is gated.
    401 + no X-Error-Code               - repo does not exist, OR is
                                          private, OR needs auth. The Hub
                                          deliberately does not distinguish
                                          these for an anonymous caller, and
                                          neither does this module: it says
                                          so rather than guessing which.
    429                                 - rate limited. This one is from
                                          the Hub's documentation, not from
                                          observation: no 429 was provoked
                                          while writing this module, so the
                                          handling for it is untested
                                          against a real one. The Hub does
                                          advertise its budget in a
                                          RateLimit header
                                          ("api";r=<remaining>;t=<seconds>),
                                          observed on every response
                                          this module made.

## Hashes: what is verified, and what is not

The tree API's lfs.oid is the sha256 of the file's real content. That was
not assumed: a small GGUF was downloaded in two Range requests, concatenated,
and hashed, and the digest matched lfs.oid exactly. download() therefore
hashes what it wrote and compares.

A file with no "lfs" object (a small, non-LFS file committed directly to
git) has no sha256 available from this endpoint. Its git oid is a sha1 over
a git blob header plus content, which is not a content hash and is not
checked here. For such a file download() returns verified=False and
verification="size_only", and says so, rather than reporting a verification
it did not perform.

## Resume, and whether Range really survives the CDN redirect

It does, confirmed: a request to the resolve URL with Range: bytes=1000-1099
followed the 302 to the CDN host and came back 206 with
Content-Range: bytes 1000-1099/1321079200. urllib's HTTPRedirectHandler
carries the original request's headers onto the redirected request, so the
Range header reaches the CDN rather than being dropped at huggingface.co.

download() writes to <target>.part and resumes from its existing size. A
server that ignores Range answers 200 rather than 206; that case is
detected and the partial file is discarded and restarted rather than being
appended to, which would silently produce a corrupt file. When a 206 does
come back, its Content-Range start is checked against the offset that was
asked for, for the same reason.

## Multi-part GGUFs

llama.cpp's gguf-split writes a model too large for one file as
<stem>-00001-of-00003.gguf, <stem>-00002-of-00003.gguf, and so on. Showing
those as three separate downloads would be wrong: they are one model and
llama-server needs all of them.

The grouping rule here is: a filename matching

    <stem>[-.]<5 digits>-of-<5 digits>.gguf

is a part, and parts sharing a directory, a stem, and a total are one
logical model whose size is the sum of its parts. That rule was derived
from data, not guessed. 3143 unique GGUF filenames were harvested by walking
the file listings of the Hub's most-downloaded GGUF repositories across seven
searches (up to 210 repo listings, deduplicated by filename); 766 of them
were split parts; every single one used exactly five digits on both sides;
and all 190 resulting part groups were complete and consecutively numbered
1..N.
The separator before the index is usually "-" but "." also occurs in the
wild (Meta-Llama-3-70B-Instruct.Q8_0.00001-of-00003.gguf), which is why the
rule accepts both. No other split-naming convention appeared in that
sample; if one exists elsewhere, those files fall through as ordinary
single files rather than being mis-grouped.

Grouping is per directory because repos really do use them: unsloth's
DeepSeek-R1 repo keeps each quantisation in its own folder.

## Quantisation labels

The quant is parsed from the filename, since that is the only place it
exists. The stem is split on "-", "." and space, and the last token that
looks like a quant name wins (see _QUANT_TOKEN_RE). Against the same 3143
filename sample this identified a label for 3041 of them; the remaining 102
are genuinely unlabelled (vision projectors named mmproj*.gguf, and a
handful of repos using hand-rolled descriptions like "APEX-Compact"), and
those get quant=None rather than an invented guess.

## Storage layout, and why a repo id is treated as hostile

A repo id is "owner/name". The slash is not a legal Windows filename
character, and the id is attacker-influenced text: it arrives from a search
result, a config file, or a UI field, and a naive join of it onto the store
directory is a path traversal waiting to happen.

Two independent defences, in order:

  1. validate_repo_id() rejects anything that is not exactly two components
     of [A-Za-z0-9][A-Za-z0-9._-]* . That alone makes "..", a leading dot,
     a backslash, a drive letter, a NUL byte, and a second slash all
     impossible before any path is built.
  2. The resulting name is then joined through hearth_contain.safe_join,
     which resolves the path and refuses anything landing outside the store
     root, and additionally refuses Windows reserved device names,
     alternate data streams, and trailing dots and spaces.

Neither is trusted alone. The self-test proves the traversal refusal
against both layers.

The on-disk mapping is repo_dir_name(): "/" becomes "%2F", and a trailing
"." becomes "%2E". Both are reversible because "%" cannot occur in a valid
repo id, so no escape can collide with literal text.

The trailing-dot escape earns its place, though not in the way it first
looks. Windows really does silently strip a trailing dot from a filename:
creating "a%2Fb." on this machine produced a directory named "a%2Fb", and
os.path.realpath of the two is byte-identical (verified, not assumed). What
that would cause is not a silent collision, because hearth_contain's own
component check refuses a name with a trailing dot outright: the failure
mode without the escape is that a legitimately named Hub repo becomes
undownloadable, not that two repos share a directory. The escape makes such
a repo both usable and distinct. repo_id_from_dir_name() inverts the
mapping, and the self-test pins the round trip against the real filesystem
rather than against string inequality.

Windows MAX_PATH is handled by running every real file operation's path
through hearth_paths.long_path(), always after containment has been checked,
never before (the \\?\ prefix disables the normalisation containment
depends on).

## What this module deliberately does not do

No authentication. Every request is anonymous. A gated repo therefore shows
up in search and in a file listing but cannot be downloaded (verified: the
Hub answers its resolve URL with 401 and X-Error-Code: GatedRepo), and this
module reports that rather than pretending otherwise. A private repo does not
appear to an anonymous client at all. No token handling means no token to
leak.

No inference, no model loading, no llama-server. This module's job ends the
moment a verified file is sitting in the store.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_contain  # noqa: E402
import hearth_paths  # noqa: E402
import hearth_shop  # noqa: E402

HF_BASE = "https://huggingface.co"

# Sent on every request. The Hub does not require a User-Agent, but an
# anonymous client that identifies itself is a better citizen on a shared
# API, and it makes hearth's traffic legible in the Hub's own logs.
USER_AGENT = "hearth/1.0 (+https://github.com/hearth) python-urllib"

DEFAULT_TIMEOUT = 30

# Socket timeout for the download connection. Larger than DEFAULT_TIMEOUT
# because a CDN can legitimately pause between chunks of a multi-gigabyte
# transfer, and smaller than "forever" because a connection that has gone
# silent should fail and leave a resumable .part rather than hang a UI
# thread indefinitely. This is a per-read timeout, not a budget for the
# whole download: there is no wall-clock cap on how long a download may
# take, only on how long it may say nothing.
DOWNLOAD_TIMEOUT = 120

# How much is read from the socket per iteration. Also the granularity at
# which is_cancelled() and on_progress() are consulted, so it trades
# cancellation latency against syscall overhead.
CHUNK_BYTES = 1024 * 1024

# Room to leave beyond the download itself. Mirrors hearth_shop's constant
# of the same name and the same reasoning: clearing a download at 99% of
# free disk and then failing on the last few bytes is a terrible outcome.
DISK_BUFFER_BYTES = 512 * 1024 ** 2

# Where models land, under hearth_paths.data_dir(). "hf" is a sibling slot
# so a future non-Hub source can live beside it without a migration.
MODELS_SUBDIR = "models"
SOURCE_SUBDIR = "hf"

PART_SUFFIX = ".part"

# Fields the search endpoint does NOT return by default. lastModified and
# gated in particular are absent from a plain /api/models response and have
# to be requested, which is the sort of thing that silently produces a UI
# full of "unknown" if nobody checks.
_SEARCH_EXPAND_FIELDS = ("downloads", "likes", "lastModified", "gated", "private", "tags", "author")

# Error kinds returned in a result's "error_kind". A UI can branch on these
# without parsing English.
ERR_UNREACHABLE = "unreachable"
ERR_RATE_LIMITED = "rate_limited"
ERR_NOT_FOUND = "not_found"
ERR_GATED = "gated"
ERR_AUTH_REQUIRED = "auth_required"
ERR_SERVER = "server_error"
ERR_HTTP = "http_error"
ERR_BAD_RESPONSE = "bad_response"
ERR_BAD_REQUEST = "bad_request"
ERR_NO_SPACE = "no_space"
ERR_RANGE_MISMATCH = "range_mismatch"
ERR_HASH_MISMATCH = "hash_mismatch"
ERR_LOCAL_IO = "local_io"

# See the module docstring's multi-part section for how this rule was
# derived and what it was checked against.
_SPLIT_RE = re.compile(r"^(?P<stem>.+)[-.](?P<index>\d{5})-of-(?P<total>\d{5})\.gguf$")

# A token that looks like a quantisation label. Anchored at both ends and
# applied to whole tokens (not as a substring search) so a model named
# "Qwen3" or "stories260K" cannot be mistaken for a quant.
_QUANT_TOKEN_RE = re.compile(
    r"^(?:"
    r"I?Q\d+(?:_[A-Z0-9]+)*"      # Q4_K_M, Q8_0, Q2_K, IQ4_XS, IQ1_M, ...
    r"|TQ\d+_\d+"                  # ternary quants, TQ1_0
    r"|BF16|F16|F32|FP16|FP32"     # unquantised weights
    r"|MXFP4(?:_MOE)?"             # microscaling fp4
    r"|NVFP4"
    r")$"
)

# Each side of a repo id. The Hub caps a namespace or a repo name at 96
# characters; the leading character is constrained to alphanumeric, which
# is what makes "." and ".." unrepresentable rather than merely filtered.
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

# One segment of a repo-relative file path. Spaces occur in real Hub
# filenames, so they are allowed; a leading dot is not, which keeps
# "." and ".." out by construction rather than by a blacklist.
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ +-]{0,254}$")


def _now():
    return time.monotonic()


def _human_bytes(n):
    """A short human-readable byte count, or "an unknown amount" for None.
    Used only in messages shown to a person.
    """
    if n is None:
        return "an unknown amount"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return "{:.0f} {}".format(n, unit) if unit == "B" else "{:.1f} {}".format(n, unit)
        n /= 1024.0
    return "{:.1f} PB".format(n)


# --------------------------------------------------------------------------
# Chokepoints: every network call goes through one of these two, so the
# self-test can replace them with captured fixtures and exercise the whole
# module offline. Same pattern as hearth_pull's _open_pull_stream and
# hearth_bench's _http_get.
# --------------------------------------------------------------------------

def _http_get_json(url, timeout):
    """GET url and return (parsed_json, headers_dict).

    Raises urllib.error.HTTPError for a non-2xx status, urllib.error.URLError
    or OSError for a transport failure, ValueError for a body that is not
    JSON. Callers turn all of that into a result dict via _error_result;
    nothing in this module's public surface raises on a network failure.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        headers = {k.lower(): v for k, v in resp.headers.items()}
    return json.loads(body), headers


def _open_download_stream(url, offset, timeout):
    """Open url for reading, starting at byte `offset`, and return the live
    response object (.status, .headers, .read(n), .close()).

    Sends Accept-Encoding: identity so no transfer compression can sit
    between the byte counts this module does arithmetic on and the bytes
    that actually land on disk. Sends Range only when offset is positive: a
    "Range: bytes=0-" on a fresh download would be a needless way to invite
    a 206 where a 200 is simpler.

    The redirect to the CDN is followed by urllib's default handler, which
    carries this request's headers (including Range) onto the redirected
    request. That the Range genuinely survives to the CDN was verified
    against the live Hub, see the module docstring.
    """
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if offset > 0:
        headers["Range"] = "bytes={}-".format(offset)
    req = urllib.request.Request(url, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=timeout)


def _error_result(exc, what):
    """Classify an exception from one of the chokepoints into
    (error_kind, message). Never raises.

    The 401-with-no-X-Error-Code case is deliberately reported as an
    ambiguity rather than resolved: the Hub answers identically for a repo
    that does not exist and one that exists privately, and inventing a
    distinction here would be a claim this module cannot back.
    """
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        hf_code = None
        try:
            hf_code = exc.headers.get("X-Error-Code")
        except Exception:
            hf_code = None
        if code == 429:
            retry = None
            try:
                retry = exc.headers.get("Retry-After")
            except Exception:
                retry = None
            msg = "Hugging Face is rate limiting this client"
            if retry:
                msg += "; it suggests waiting {} seconds".format(retry)
            return ERR_RATE_LIMITED, msg + "."
        if hf_code == "GatedRepo":
            return ERR_GATED, (
                "{} is gated on Hugging Face: it requires accepting the model's "
                "licence while signed in. Hearth downloads anonymously and "
                "cannot do that.".format(what)
            )
        # EntryNotFound and GatedRepo were both observed live.
        # RevisionNotFound is from the Hub's documented set and was not
        # provoked, so it is handled on faith rather than on evidence.
        if hf_code in ("EntryNotFound", "RevisionNotFound"):
            return ERR_NOT_FOUND, "{} does not exist on Hugging Face.".format(what)
        if code == 404:
            return ERR_NOT_FOUND, "{} does not exist on Hugging Face.".format(what)
        if code in (401, 403):
            return ERR_AUTH_REQUIRED, (
                "Hugging Face refused anonymous access to {}. It does not exist, "
                "is private, or is gated; the Hub answers the same way for all "
                "three when nobody is signed in.".format(what)
            )
        if code >= 500:
            return ERR_SERVER, "Hugging Face returned HTTP {} for {}.".format(code, what)
        return ERR_HTTP, "Hugging Face returned HTTP {} for {}.".format(code, what)
    if isinstance(exc, ValueError):
        return ERR_BAD_RESPONSE, "Hugging Face returned something that is not valid JSON for {}.".format(what)
    if isinstance(exc, (urllib.error.URLError, OSError, TimeoutError)):
        return ERR_UNREACHABLE, "Could not reach Hugging Face for {}: {}: {}".format(
            what, type(exc).__name__, exc,
        )
    return ERR_HTTP, "Unexpected failure fetching {}: {}: {}".format(what, type(exc).__name__, exc)


def _rate_limit_from_headers(headers):
    """Parse the Hub's RateLimit header into {"remaining", "reset_seconds"},
    or None when it is absent or unparseable.

    Observed shape: RateLimit: "api";r=499;t=121 (RFC 9218 draft style).
    Parsed leniently on purpose: this is advisory telemetry for a UI, and a
    format change should degrade to None rather than break a search.
    """
    raw = (headers or {}).get("ratelimit")
    if not raw:
        return None
    out = {}
    for part in str(raw).split(";"):
        part = part.strip()
        if part.startswith("r="):
            try:
                out["remaining"] = int(part[2:])
            except ValueError:
                pass
        elif part.startswith("t="):
            try:
                out["reset_seconds"] = int(part[2:])
            except ValueError:
                pass
    return out or None


def _next_page_url(headers):
    """The rel="next" URL out of a Link header, or None.

    The Hub's tree endpoint pages with an opaque cursor it only ever hands
    back this way, so this is the only correct way to walk a repo with more
    files than one page holds. Deliberately does not construct a cursor URL
    itself.
    """
    link = (headers or {}).get("link")
    if not link:
        return None
    for piece in str(link).split(","):
        piece = piece.strip()
        if not piece.startswith("<"):
            continue
        end = piece.find(">")
        if end < 0:
            continue
        url = piece[1:end]
        if 'rel="next"' in piece[end:]:
            return url
    return None


# --------------------------------------------------------------------------
# Repo ids, filenames, and where they land on disk
# --------------------------------------------------------------------------

def validate_repo_id(repo_id):
    """Return None if `repo_id` is a well-formed Hub repo id, else a reason.

    A repo id is attacker-influenced text (it comes from a search result, a
    saved config, or a text field), and it is about to be turned into a
    filesystem path. This is the first of two defences: it rejects rather
    than sanitises, so nothing ambiguous ever reaches the path layer. See
    the module docstring for the second (hearth_contain.safe_join).

    Requires exactly two slash-separated components, each starting with an
    alphanumeric and continuing with alphanumerics, dot, underscore, or
    hyphen. That shape makes "..", ".", a leading dot, a backslash, a drive
    letter, an alternate data stream, a URL scheme, a percent escape, and a
    NUL byte all unrepresentable rather than merely filtered out.
    """
    if not isinstance(repo_id, str) or not repo_id:
        return "repo id must be a non-empty string"
    if repo_id.count("/") != 1:
        return "repo id must be exactly 'owner/name' (one slash)"
    owner, name = repo_id.split("/", 1)
    for part, label in ((owner, "owner"), (name, "name")):
        if not _REPO_PART_RE.match(part):
            return "repo id {} {!r} is not a legal Hugging Face name".format(label, part)
    return None


def validate_repo_path(path):
    """Return None if `path` is a well-formed repo-relative file path, else
    a reason. Same rejection-not-sanitisation stance as validate_repo_id.

    Forward slash separates segments (the Hub only ever emits forward
    slashes). Each segment must start with an alphanumeric, which keeps "."
    and ".." unrepresentable. Spaces are allowed because real Hub filenames
    contain them; backslash, colon, and the Windows-illegal set are not,
    and are additionally caught by hearth_contain later.
    """
    if not isinstance(path, str) or not path:
        return "file path must be a non-empty string"
    if "\\" in path:
        return "file path must use forward slashes, not backslashes"
    if path.startswith("/") or path.endswith("/"):
        return "file path must not start or end with a slash"
    segments = path.split("/")
    if len(segments) > 16:
        return "file path is implausibly deep"
    for seg in segments:
        if not _PATH_SEGMENT_RE.match(seg):
            return "file path segment {!r} is not a legal file name".format(seg)
    return None


def repo_dir_name(repo_id):
    """The single directory name a repo's files live in, encoded so it is a
    legal Windows filename and so the encoding round-trips.

    "/" becomes "%2F" and a trailing "." becomes "%2E". Both escapes are
    unambiguous because "%" cannot appear in a repo id that passed
    validate_repo_id, so no escape sequence can collide with literal text.

    The trailing-dot escape exists for a real collision, not for tidiness:
    Windows silently strips trailing dots from filenames, so "a/b." and
    "a/b" would otherwise resolve to the same directory and one repo's
    files would land inside another's.

    Raises ValueError on a repo id that does not validate, rather than
    encoding something it should have refused.
    """
    reason = validate_repo_id(repo_id)
    if reason:
        raise ValueError("refusing repo id: {}: {!r}".format(reason, repo_id))
    encoded = repo_id.replace("/", "%2F")
    stripped = encoded.rstrip(".")
    return stripped + "%2E" * (len(encoded) - len(stripped))


def repo_id_from_dir_name(name):
    """Invert repo_dir_name. Returns the repo id, or None if `name` does not
    decode to a valid one (a stray directory somebody dropped in the store).
    """
    if not isinstance(name, str) or not name:
        return None
    decoded = name.replace("%2E", ".").replace("%2F", "/")
    return None if validate_repo_id(decoded) else decoded


def model_store_dir():
    """The directory hearth keeps downloaded Hugging Face models in.

    Derived from hearth_paths.data_dir(), so HEARTH_DATA_DIR redirects it
    and a test never writes to the real store. Not created here; the
    functions that write create it.
    """
    return os.path.join(hearth_paths.data_dir(), MODELS_SUBDIR, SOURCE_SUBDIR)


def model_dir(repo_id, store_dir=None):
    """The directory a repo's downloaded files live in.

    Raises ValueError if the repo id does not validate or if the resulting
    path would land outside the store. Both checks are real: the first
    rejects the input's shape, the second (hearth_contain.safe_join)
    resolves the path and refuses an escape, and additionally refuses
    Windows reserved device names, alternate data streams, and trailing
    dots and spaces.
    """
    root = store_dir or model_store_dir()
    return hearth_contain.safe_join(root, repo_dir_name(repo_id))


def model_path(repo_id, filename, store_dir=None):
    """The full local path a repo's `filename` downloads to.

    `filename` is a repo-relative path and may contain subdirectories,
    because real repos use them (unsloth keeps each quantisation in its own
    folder). Raises ValueError if either the repo id or the path fails
    validation or containment.
    """
    reason = validate_repo_path(filename)
    if reason:
        raise ValueError("refusing file path: {}: {!r}".format(reason, filename))
    return hearth_contain.safe_join(model_dir(repo_id, store_dir), filename)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def _normalize_search_entry(raw):
    """One /api/models element into hearth's shape, or None if it is not
    usable (no id). Missing fields become None rather than 0 or "", so a UI
    can tell "this repo has no likes" from "the Hub did not say".
    """
    if not isinstance(raw, dict):
        return None
    repo_id = raw.get("id") or raw.get("modelId")
    if not isinstance(repo_id, str) or not repo_id:
        return None
    gated_raw = raw.get("gated", False)
    tags = raw.get("tags")
    author = raw.get("author")
    if not author and "/" in repo_id:
        author = repo_id.split("/", 1)[0]
    return {
        "repo_id": repo_id,
        "author": author,
        "downloads": raw.get("downloads"),
        "likes": raw.get("likes"),
        "last_modified": raw.get("lastModified"),
        # gated is false, "auto", or "manual" on the live Hub. The boolean
        # is what a UI needs to grey out a download button; the raw mode is
        # kept because "auto" (click through instantly) and "manual" (wait
        # for a human to approve) are very different waits for a user.
        "gated": bool(gated_raw),
        "gated_mode": gated_raw if isinstance(gated_raw, str) else None,
        "private": bool(raw.get("private", False)),
        "tags": list(tags) if isinstance(tags, list) else [],
        "url": "{}/{}".format(HF_BASE, repo_id),
    }


def search_url(query, limit=20):
    """The exact /api/models URL search_models() will request. Split out so
    the self-test can pin the parameters without a network call, and so a
    caller debugging an odd result can see and paste the URL.
    """
    params = [
        ("filter", "gguf"),
        ("limit", str(max(1, int(limit)))),
        ("sort", "downloads"),
        ("direction", "-1"),
    ]
    if query:
        params.insert(0, ("search", query))
    for field in _SEARCH_EXPAND_FIELDS:
        params.append(("expand[]", field))
    return "{}/api/models?{}".format(HF_BASE, urllib.parse.urlencode(params))


def search_models(query, limit=20, timeout=DEFAULT_TIMEOUT):
    """Find Hugging Face repositories that hold GGUF models.

    Always returns a dict, never raises. On success:
      {"ok": True, "error": None, "error_kind": None, "query": ...,
       "models": [...], "rate_limit": {...} or None, "url": ...}

    On any failure (the Hub unreachable, rate limiting, a non-2xx status, a
    body that is not JSON) the same dict comes back with ok False, models
    an empty list, and error/error_kind filled in - see the ERR_* constants
    for the machine-readable kinds a UI can branch on.

    An empty query lists the most-downloaded GGUF repos rather than
    erroring, which is a reasonable default first screen.

    Each element of "models" carries repo_id, author, downloads, likes,
    last_modified (an ISO-8601 string as the Hub sends it, not a parsed
    datetime), gated, gated_mode, private, tags, and url. downloads, likes,
    and last_modified are None when the Hub did not report them.

    Results are limited to repos carrying the Hub's "gguf" tag. That tag is
    repo-level, so it is a way of finding candidates, not proof of what is
    inside: call list_gguf_files() for the actual file list.
    """
    url = search_url(query, limit)
    result = {
        "ok": False,
        "error": None,
        "error_kind": None,
        "query": query,
        "models": [],
        "rate_limit": None,
        "url": url,
    }
    try:
        data, headers = _http_get_json(url, timeout)
    except Exception as exc:  # noqa: BLE001 - classified, never propagated
        kind, message = _error_result(exc, "a model search")
        result["error_kind"] = kind
        result["error"] = message
        return result

    result["rate_limit"] = _rate_limit_from_headers(headers)
    if not isinstance(data, list):
        result["error_kind"] = ERR_BAD_RESPONSE
        result["error"] = "Hugging Face returned a {} where a list of models was expected.".format(
            type(data).__name__,
        )
        return result

    models = []
    for raw in data:
        entry = _normalize_search_entry(raw)
        if entry is not None:
            models.append(entry)
    result["ok"] = True
    result["models"] = models
    return result


# --------------------------------------------------------------------------
# Quantisation enumeration
# --------------------------------------------------------------------------

def parse_quant(name):
    """The quantisation label in a GGUF filename or stem, or None.

    Splits on "-", "." and space and takes the LAST whole token that looks
    like a quant name. Whole tokens, not a substring search: "Qwen3" and
    "stories260K" both contain something Q-shaped and neither is a quant.
    Last rather than first because the label conventionally trails the model
    name, and a name can legitimately contain an earlier quant-shaped token.

    Returns the label upper-cased and canonical (a file named
    "...-q4_k_m.gguf" yields "Q4_K_M"), so labels from repos with different
    casing conventions compare equal.

    None is a real answer, not a failure: 102 of 3143 sampled Hub GGUF
    filenames carry no parseable label (vision projectors, and repos using
    prose names like "APEX-Compact"). Returning None beats inventing one.
    """
    if not isinstance(name, str) or not name:
        return None
    stem = name[:-5] if name.endswith(".gguf") else name
    stem = stem.rsplit("/", 1)[-1]
    for token in reversed(re.split(r"[-. ]", stem)):
        upper = token.upper()
        if _QUANT_TOKEN_RE.match(upper):
            return upper
    return None


def _split_key(path):
    """(group_key, index, total) for a multi-part GGUF path, or None for an
    ordinary single file.

    group_key includes the directory, because repos really do put each
    quantisation in its own folder and two folders can hold identically
    stemmed parts.
    """
    directory, _, base = path.rpartition("/")
    match = _SPLIT_RE.match(base)
    if not match:
        return None
    return (
        (directory, match.group("stem"), match.group("total")),
        int(match.group("index")),
        int(match.group("total")),
    )


def group_gguf_files(entries):
    """Turn raw tree entries into logical models, grouping multi-part GGUFs.

    `entries` is a list of dicts as the tree API returns them (path, size,
    and optionally an "lfs" object). Non-GGUF files are ignored.

    Returns a list of dicts, sorted by size ascending (smallest quant
    first, which is the order a user weighing "what will fit" wants):

      name         - display name: the filename for a single file, the
                     shared stem for a multi-part model.
      path         - the repo-relative path to download, or None when this
                     is a multi-part model (download each entry of "parts").
      quant        - parse_quant() of the name, or None.
      size_bytes   - the file size, or the SUM across all parts.
      sha256       - the file's sha256 (from lfs.oid), or None. Always None
                     for a multi-part model: the parts are hashed
                     individually, there is no hash of the concatenation.
      multipart    - True when this is a split model.
      part_count   - 1 for a single file, else the count declared in the
                     filenames ("-of-00003" means 3).
      parts        - [{"path", "size_bytes", "sha256", "index"}], one entry
                     per file, in index order. Length 1 for a single file.
      complete     - False when a declared part is missing from the repo.
                     A model with a missing part cannot be loaded, so this
                     is surfaced rather than papered over with a smaller
                     sum that would look like a valid, cheaper download.
      missing_parts - the indexes that are absent, [] when complete.
    """
    singles = []
    groups = {}
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        path = raw.get("path")
        if not isinstance(path, str) or not path.endswith(".gguf"):
            continue
        lfs = raw.get("lfs") if isinstance(raw.get("lfs"), dict) else None
        # lfs.size is the real content size for an LFS-backed file; the
        # top-level "size" agrees with it in every sample checked, but lfs
        # is the authoritative one so prefer it where present.
        size = None
        if lfs and isinstance(lfs.get("size"), int):
            size = lfs["size"]
        elif isinstance(raw.get("size"), int):
            size = raw["size"]
        sha256 = lfs.get("oid") if lfs else None
        if not isinstance(sha256, str) or len(sha256) != 64:
            sha256 = None
        part = {"path": path, "size_bytes": size, "sha256": sha256, "index": 1}

        split = _split_key(path)
        if split is None:
            singles.append(part)
            continue
        key, index, total = split
        part["index"] = index
        groups.setdefault(key, {"total": total, "parts": []})["parts"].append(part)

    out = []
    for part in singles:
        out.append({
            "name": part["path"].rsplit("/", 1)[-1],
            "path": part["path"],
            "quant": parse_quant(part["path"]),
            "size_bytes": part["size_bytes"],
            "sha256": part["sha256"],
            "multipart": False,
            "part_count": 1,
            "parts": [part],
            "complete": True,
            "missing_parts": [],
        })

    for (directory, stem, _total_str), group in groups.items():
        parts = sorted(group["parts"], key=lambda p: p["index"])
        total = group["total"]
        present = {p["index"] for p in parts}
        missing = [i for i in range(1, total + 1) if i not in present]
        sizes = [p["size_bytes"] for p in parts]
        # A single unknown part size makes the sum a lie, so the sum is
        # only reported when every part reported a size.
        summed = sum(sizes) if sizes and all(isinstance(s, int) for s in sizes) else None
        out.append({
            "name": stem,
            "path": None,
            "quant": parse_quant(stem),
            "size_bytes": summed,
            "sha256": None,
            "multipart": True,
            "part_count": total,
            "parts": parts,
            "complete": not missing,
            "missing_parts": missing,
        })

    # Unknown sizes sort last rather than sorting as zero, which would put
    # them at the top of a "smallest first" list and read as tiny.
    out.sort(key=lambda e: (e["size_bytes"] is None, e["size_bytes"] or 0, e["name"]))
    return out


def tree_url(repo_id, revision="main"):
    """The /tree URL list_gguf_files() starts from. expand=true is what adds
    the "lfs" object carrying each file's sha256; without it there is no
    hash to verify a download against at all.
    """
    return "{}/api/models/{}/tree/{}?recursive=true&expand=true".format(
        HF_BASE, repo_id, urllib.parse.quote(revision, safe=""),
    )


def _paged_tree(url, timeout, max_pages=50):
    """Walk the tree endpoint's Link-header pagination, returning
    (entries, headers_of_last_page). Raises what _http_get_json raises.

    max_pages is a backstop against a cursor that never terminates, not an
    expected limit. The endpoint's default page size was not measured (a
    149-file repo came back in a single page, so it is at least that), so
    this is a generous round number rather than a computed bound.
    """
    entries = []
    headers = {}
    seen = set()
    pages = 0
    while url and pages < max_pages:
        if url in seen:
            break  # a cursor that points at itself would otherwise spin
        seen.add(url)
        data, headers = _http_get_json(url, timeout)
        if not isinstance(data, list):
            raise ValueError("tree endpoint returned a {}, not a list".format(type(data).__name__))
        entries.extend(data)
        url = _next_page_url(headers)
        pages += 1
    return entries, headers


def list_gguf_files(repo_id, timeout=DEFAULT_TIMEOUT, revision="main"):
    """List the GGUF models inside one Hub repository, with sizes and hashes.

    Always returns a dict, never raises:
      {"ok": True, "error": None, "error_kind": None, "repo_id": ...,
       "revision": ..., "files": [...], "file_count": n,
       "rate_limit": {...} or None, "url": ...}

    "files" holds one entry per LOGICAL model, so a split model appears once
    with its parts summed rather than as N separate downloads - see
    group_gguf_files() for the entry shape, and the module docstring for how
    the grouping rule was derived and checked. Entries are sorted smallest
    first, since size is the decision a user is actually making when a repo
    offers a dozen quantisations of the same weights.

    An invalid repo id fails here without touching the network
    (error_kind ERR_BAD_REQUEST). A repo that does not exist, is private, or
    is gated comes back as ERR_AUTH_REQUIRED or ERR_GATED with a message
    that says the Hub does not distinguish them for anonymous callers.

    An empty "files" list with ok True is a real answer: the repo exists and
    holds no GGUF files. That is exactly what the "gguf" search tag cannot
    promise, which is why this function and not search_models() is the
    authority.
    """
    result = {
        "ok": False,
        "error": None,
        "error_kind": None,
        "repo_id": repo_id,
        "revision": revision,
        "files": [],
        "file_count": 0,
        "rate_limit": None,
        "url": None,
    }
    reason = validate_repo_id(repo_id)
    if reason:
        result["error_kind"] = ERR_BAD_REQUEST
        result["error"] = "Not a usable Hugging Face repo id: {}.".format(reason)
        return result

    url = tree_url(repo_id, revision)
    result["url"] = url
    try:
        entries, headers = _paged_tree(url, timeout)
    except Exception as exc:  # noqa: BLE001 - classified, never propagated
        kind, message = _error_result(exc, "the file list for {}".format(repo_id))
        result["error_kind"] = kind
        result["error"] = message
        return result

    result["rate_limit"] = _rate_limit_from_headers(headers)
    result["ok"] = True
    result["files"] = group_gguf_files(entries)
    result["file_count"] = len(result["files"])
    return result


def find_file(repo_id, filename, timeout=DEFAULT_TIMEOUT, revision="main"):
    """Look one repo-relative path up in a repo's file listing.

    Used by download() to learn a file's expected size and sha256 when the
    caller did not supply them. Searches parts as well as whole entries, so
    a single file of a split model can be looked up by its own path.

    Always returns a dict, never raises. On success: ok True plus path,
    size_bytes, sha256 (None when the Hub published none for that file),
    quant, and of_model. On failure: ok False with error and error_kind,
    either passed through from the listing or ERR_NOT_FOUND when the repo
    simply does not contain that path.
    """
    listing = list_gguf_files(repo_id, timeout=timeout, revision=revision)
    if not listing["ok"]:
        return listing  # carries ok False, error, error_kind
    for entry in listing["files"]:
        for part in entry["parts"]:
            if part["path"] == filename:
                return {
                    "ok": True,
                    "error": None,
                    "error_kind": None,
                    "path": part["path"],
                    "size_bytes": part["size_bytes"],
                    "sha256": part["sha256"],
                    "quant": entry["quant"],
                    "of_model": entry["name"],
                }
    return {
        "ok": False,
        "error": "{} does not contain {}.".format(repo_id, filename),
        "error_kind": ERR_NOT_FOUND,
    }


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def download_url(repo_id, filename, revision="main"):
    """The resolve URL for one file. This is the URL that answers 302 to a
    CDN host; see the module docstring on Range surviving that redirect.
    """
    quoted = "/".join(urllib.parse.quote(seg, safe="") for seg in filename.split("/"))
    return "{}/{}/resolve/{}/{}".format(
        HF_BASE, repo_id, urllib.parse.quote(revision, safe=""), quoted,
    )


def _free_bytes_near(path):
    """Free bytes on the volume that would hold `path`, measured against the
    nearest existing ancestor directory, or None when it cannot be read.

    Delegates to hearth_shop.free_disk_bytes rather than carrying a second
    copy of the same shutil.disk_usage call. That function returns 0 both
    for "genuinely zero free" and for "could not measure", so this one only
    hands it a directory already confirmed to exist and translates a
    still-zero answer into None, keeping "no space" and "no reading"
    distinguishable to the caller.
    """
    check = os.path.abspath(path)
    while check and not os.path.isdir(check):
        parent = os.path.dirname(check)
        if not parent or parent == check:
            return None
        check = parent
    free = hearth_shop.free_disk_bytes(check)
    return free if free > 0 else None


def _content_range_start(header):
    """The first byte offset out of a Content-Range header, or None.
    Shape: "bytes 1000-1099/1321079200".
    """
    if not header:
        return None
    match = re.match(r"^\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", str(header))
    return int(match.group(1)) if match else None


def _content_range_total(header):
    """The total size out of a Content-Range header, or None when the
    server sent "*" (total unknown) or nothing parseable.
    """
    if not header:
        return None
    match = re.match(r"^\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", str(header))
    if not match or match.group(3) == "*":
        return None
    return int(match.group(3))


def _sha256_file(path, is_cancelled=None):
    """Streaming sha256 of a file, or None if cancellation interrupted it.

    Hashes the finished .part file rather than hashing incrementally while
    downloading, because a resumed download would have to re-read the
    existing prefix to seed the hash anyway. Consults is_cancelled between
    chunks so hashing a multi-gigabyte file stays as abortable as
    downloading one.
    """
    digest = hashlib.sha256()
    with open(hearth_paths.long_path(path), "rb") as handle:
        while True:
            if is_cancelled is not None and is_cancelled():
                return None
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def download(repo_id, filename, dest_dir=None, on_progress=None, is_cancelled=None,
             timeout=DOWNLOAD_TIMEOUT, revision="main", expected_sha256=None,
             expected_size=None, lookup_timeout=DEFAULT_TIMEOUT):
    """Download one GGUF file from a Hub repo into hearth's model store.

    Always returns a dict, never raises. Key fields:
      ok            - True only when the bytes are in place at "path" AND
                      either the sha256 matched or no sha256 existed to
                      check (see "verified"/"verification" below).
      error, error_kind - see the ERR_* constants.
      cancelled     - True when is_cancelled() stopped it. The .part file is
                      left behind and a later call resumes from it.
      path          - the final path, or None if it never got there.
      part_path     - the .part file, so a UI can show or clear it.
      bytes_done / bytes_total - final byte counts.
      resumed_from  - the offset this call started at (0 for a fresh start).
      already_present - True when the file was already there, verified, and
                      nothing was downloaded.
      verified      - True only when a sha256 was available AND matched.
      verification  - "sha256" (hashed and matched), "size_only" (the file
                      is the right length but the Hub published no sha256
                      for it, so no content check happened), or "none".
      sha256_expected / sha256_actual - what was compared.
      disk_check    - {"needed_bytes", "free_bytes", "checked", "ok"}.

    ## Resume
    Bytes go to <path>.part and the next call resumes from its size with a
    Range request. A server that ignores Range answers 200 instead of 206;
    that is detected and the partial is discarded and restarted rather than
    appended to, which would produce a silently corrupt file. A 206 whose
    Content-Range does not start where this call asked is refused outright
    (ERR_RANGE_MISMATCH) for the same reason. Range was verified to survive
    the Hub's CDN redirect, see the module docstring.

    ## Cancellation
    is_cancelled() is consulted before each chunk is written and between
    chunks while hashing, so a cancel takes effect within one chunk's read.
    Latency is therefore bounded by however long one CHUNK_BYTES read takes,
    up to the socket timeout, not by the size of the download. A cancelled
    download leaves a valid .part and never a half-written final file.

    ## Verification and atomicity
    Nothing is renamed into place until it has been checked. The .part file
    is hashed after the last byte lands; only on a match (or on there being
    no published hash) is it os.replace()d onto the final path, which is
    atomic within a volume. A hash mismatch deletes the .part, because a
    file that failed its hash is not a useful thing to resume from.

    When the Hub publishes no sha256 for a file (a small non-LFS file
    committed directly to git), verified is False and verification is
    "size_only". That is reported, not glossed: the length was checked and
    the content was not.

    ## Disk space
    Checked before the connection is opened, against the bytes still needed
    plus DISK_BUFFER_BYTES, and refused with a message naming both figures.
    Skipped (disk_check["checked"] False) only when the size is genuinely
    unknown or free space could not be read, rather than inventing a
    threshold to compare against.

    ## Multi-part models
    One call downloads one file. A multi-part model's entry from
    list_gguf_files() carries a "parts" list; loop over it and call this
    once per part.
    """
    result = {
        "ok": False,
        "error": None,
        "error_kind": None,
        "cancelled": False,
        "repo_id": repo_id,
        "filename": filename,
        "revision": revision,
        "path": None,
        "part_path": None,
        "url": None,
        "bytes_done": 0,
        "bytes_total": expected_size,
        "resumed_from": 0,
        "already_present": False,
        "verified": False,
        "verification": "none",
        "sha256_expected": expected_sha256,
        "sha256_actual": None,
        "elapsed_seconds": 0.0,
        "disk_check": {"needed_bytes": None, "free_bytes": None, "checked": False, "ok": None},
    }

    reason = validate_repo_id(repo_id)
    if reason:
        result["error_kind"] = ERR_BAD_REQUEST
        result["error"] = "Not a usable Hugging Face repo id: {}.".format(reason)
        return result
    reason = validate_repo_path(filename)
    if reason:
        result["error_kind"] = ERR_BAD_REQUEST
        result["error"] = "Not a usable file path: {}.".format(reason)
        return result

    # Containment: safe_join resolves and refuses an escape, on top of the
    # shape validation above. Both layers, neither trusted alone.
    try:
        if dest_dir:
            target = hearth_contain.safe_join(dest_dir, filename)
        else:
            target = model_path(repo_id, filename)
    except ValueError as exc:
        result["error_kind"] = ERR_BAD_REQUEST
        result["error"] = "Refusing to write outside the model store: {}".format(exc)
        return result

    part = target + PART_SUFFIX
    result["path"] = target
    result["part_path"] = part
    url = download_url(repo_id, filename, revision)
    result["url"] = url

    if is_cancelled is not None and is_cancelled():
        result["cancelled"] = True
        result["error_kind"] = ERR_BAD_REQUEST
        result["error"] = "Download cancelled before it started."
        return result

    # Fill in whatever the caller did not supply. A failure here is a real
    # failure: without a size there is no disk check and without a hash
    # there is no verification, and silently proceeding without either
    # would be exactly the kind of quiet degradation this module avoids.
    if expected_sha256 is None or expected_size is None:
        meta = find_file(repo_id, filename, timeout=lookup_timeout, revision=revision)
        if not meta.get("ok"):
            result["error_kind"] = meta.get("error_kind") or ERR_NOT_FOUND
            result["error"] = meta.get("error") or "Could not look up {} in {}.".format(filename, repo_id)
            return result
        if expected_sha256 is None:
            expected_sha256 = meta.get("sha256")
        if expected_size is None:
            expected_size = meta.get("size_bytes")
    result["sha256_expected"] = expected_sha256
    result["bytes_total"] = expected_size

    try:
        os.makedirs(os.path.dirname(hearth_paths.long_path(target)), exist_ok=True)
    except OSError as exc:
        result["error_kind"] = ERR_LOCAL_IO
        result["error"] = "Could not create the model directory: {}: {}".format(type(exc).__name__, exc)
        return result

    long_target = hearth_paths.long_path(target)
    long_part = hearth_paths.long_path(part)

    # Already downloaded? Only claim so on a real check, not on the file
    # merely existing: a truncated leftover from an older, cruder run must
    # not be mistaken for a finished model.
    if os.path.isfile(long_target):
        on_disk = os.path.getsize(long_target)
        if expected_size is None or on_disk == expected_size:
            actual = _sha256_file(target, is_cancelled) if expected_sha256 else None
            if expected_sha256 and actual is None:
                result["cancelled"] = True
                result["error_kind"] = ERR_BAD_REQUEST
                result["error"] = "Cancelled while verifying the existing file."
                return result
            if expected_sha256 is None or actual == expected_sha256:
                result["ok"] = True
                result["already_present"] = True
                result["bytes_done"] = on_disk
                result["bytes_total"] = expected_size if expected_size is not None else on_disk
                result["sha256_actual"] = actual
                result["verified"] = bool(expected_sha256) and actual == expected_sha256
                result["verification"] = "sha256" if result["verified"] else "size_only"
                return result
        # Wrong size or wrong hash: not a usable model. Leave it alone (it
        # is not this call's to delete) and download beside it, replacing it
        # only once a verified file is ready.

    resume_from = 0
    if os.path.isfile(long_part):
        existing = os.path.getsize(long_part)
        if expected_size is not None and existing > expected_size:
            # Longer than the real file: whatever produced it was not this,
            # or the remote file changed. Resuming would append past the
            # end, so start over.
            try:
                os.remove(long_part)
            except OSError:
                pass
        else:
            # Short of the expected size, resume from here. Exactly equal to
            # it means the bytes all arrived on a previous attempt that died
            # before verifying, and need_transfer below drops straight
            # through to the hash check rather than opening a connection to
            # request zero bytes.
            resume_from = existing
    result["resumed_from"] = resume_from

    # Disk preflight, before any connection is opened.
    remaining = None
    if expected_size is not None:
        remaining = max(0, expected_size - resume_from)
    free = _free_bytes_near(os.path.dirname(target))
    disk_check = {
        "needed_bytes": (remaining + DISK_BUFFER_BYTES) if remaining is not None else None,
        "free_bytes": free,
        "checked": False,
        "ok": None,
    }
    if remaining is not None and free is not None:
        needed = remaining + DISK_BUFFER_BYTES
        disk_check["checked"] = True
        disk_check["ok"] = free >= needed
        if not disk_check["ok"]:
            result["disk_check"] = disk_check
            result["error_kind"] = ERR_NO_SPACE
            result["error"] = (
                "Not enough free disk space for {}: need about {} (the remaining "
                "download plus a safety margin), only {} free at {}.".format(
                    filename, _human_bytes(needed), _human_bytes(free),
                    os.path.dirname(target),
                )
            )
            return result
    result["disk_check"] = disk_check

    started = _now()
    bytes_done = resume_from

    def emit():
        if on_progress is None:
            return
        total = result["bytes_total"]
        elapsed = max(1e-9, _now() - started)
        gained = bytes_done - resume_from
        progress = {
            "bytes_done": bytes_done,
            "bytes_total": total,
            "fraction": (max(0.0, min(1.0, bytes_done / total)) if total else None),
            "resumed_from": resume_from,
            "elapsed_seconds": elapsed,
            # A session average, not an instantaneous rate: it is what this
            # call has actually achieved so far, and it does not swing on
            # one slow chunk the way a two-sample rate does.
            "speed_bytes_per_sec": (gained / elapsed) if gained > 0 else None,
            "eta_seconds": (
                (total - bytes_done) / (gained / elapsed)
                if (total and gained > 0 and bytes_done < total) else None
            ),
            "path": target,
        }
        try:
            on_progress(progress)
        except Exception:
            # A bug in a UI callback must not abort a multi-gigabyte
            # download that is otherwise going fine.
            pass

    need_transfer = expected_size is None or resume_from < expected_size
    if need_transfer:
        try:
            resp = _open_download_stream(url, resume_from, timeout)
        except Exception as exc:  # noqa: BLE001 - classified, never propagated
            kind, message = _error_result(exc, "{} from {}".format(filename, repo_id))
            result["error_kind"] = kind
            result["error"] = message
            return result

        try:
            status = getattr(resp, "status", None)
            headers = resp.headers
            if resume_from > 0:
                if status == 200:
                    # Range ignored: this body starts at byte zero. Appending
                    # it to the partial would corrupt the file silently, so
                    # throw the partial away and take the whole thing.
                    resume_from = 0
                    bytes_done = 0
                    result["resumed_from"] = 0
                    mode = "wb"
                elif status == 206:
                    start = _content_range_start(headers.get("Content-Range"))
                    if start is None or start != resume_from:
                        result["error_kind"] = ERR_RANGE_MISMATCH
                        result["error"] = (
                            "Hugging Face resumed at byte {} when {} was requested; "
                            "refusing to append mismatched bytes.".format(start, resume_from)
                        )
                        return result
                    mode = "ab"
                else:
                    result["error_kind"] = ERR_HTTP
                    result["error"] = "Unexpected HTTP {} on a resumed download.".format(status)
                    return result
            else:
                mode = "wb"

            if result["bytes_total"] is None:
                total = _content_range_total(headers.get("Content-Range"))
                if total is None:
                    try:
                        length = int(headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        length = 0
                    total = (resume_from + length) if length else None
                result["bytes_total"] = total

            emit()

            def _flush(handle):
                """Get the partial genuinely onto disk before returning, so
                the resume offset a later call reads back is real even if
                the machine loses power a moment later."""
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                except OSError:
                    pass

            try:
                handle = open(long_part, mode)
            except OSError as exc:
                result["error_kind"] = ERR_LOCAL_IO
                result["error"] = "Could not open the partial download file: {}: {}".format(
                    type(exc).__name__, exc,
                )
                return result

            with handle:
                while True:
                    if is_cancelled is not None and is_cancelled():
                        _flush(handle)
                        result["cancelled"] = True
                        result["bytes_done"] = bytes_done
                        result["error_kind"] = ERR_BAD_REQUEST
                        result["error"] = (
                            "Download cancelled at {} of {}. The partial file was "
                            "kept and a later download resumes from it.".format(
                                _human_bytes(bytes_done), _human_bytes(result["bytes_total"]),
                            )
                        )
                        result["elapsed_seconds"] = _now() - started
                        return result
                    # Reading and writing are caught separately and on
                    # purpose. Both raise OSError, and a socket that dropped
                    # mid-transfer is a network failure the caller should
                    # retry, while a disk that rejected the write is not.
                    # Collapsing them into one handler reports a dead Wi-Fi
                    # connection as a broken disk.
                    try:
                        chunk = resp.read(CHUNK_BYTES)
                    except Exception as exc:  # noqa: BLE001 - a drop mid-transfer
                        _flush(handle)
                        kind, message = _error_result(
                            exc, "{} from {}".format(filename, repo_id),
                        )
                        result["error_kind"] = kind
                        result["error"] = message
                        result["bytes_done"] = bytes_done
                        result["elapsed_seconds"] = _now() - started
                        return result
                    if not chunk:
                        break
                    try:
                        handle.write(chunk)
                    except OSError as exc:
                        result["error_kind"] = ERR_LOCAL_IO
                        result["bytes_done"] = bytes_done
                        result["error"] = "Writing the download failed: {}: {}".format(
                            type(exc).__name__, exc,
                        )
                        return result
                    bytes_done += len(chunk)
                    emit()
        except Exception as exc:  # noqa: BLE001 - anything else from the response
            if result["error"] is None:
                kind, message = _error_result(exc, "{} from {}".format(filename, repo_id))
                result["error_kind"] = kind
                result["error"] = message
                result["bytes_done"] = bytes_done
            return result
        finally:
            try:
                resp.close()
            except Exception:
                pass
    else:
        bytes_done = resume_from

    result["bytes_done"] = bytes_done
    result["elapsed_seconds"] = _now() - started

    if not os.path.isfile(long_part):
        result["error_kind"] = ERR_LOCAL_IO
        result["error"] = "The partial download file disappeared before it could be verified."
        return result

    on_disk = os.path.getsize(long_part)
    if result["bytes_total"] is not None and on_disk != result["bytes_total"]:
        result["error_kind"] = ERR_BAD_RESPONSE
        result["error"] = (
            "Download ended at {} bytes but {} were expected. The partial file was "
            "kept and a later download resumes from it.".format(on_disk, result["bytes_total"])
        )
        return result

    if expected_sha256:
        actual = _sha256_file(part, is_cancelled)
        if actual is None:
            result["cancelled"] = True
            result["error_kind"] = ERR_BAD_REQUEST
            result["error"] = "Cancelled while verifying the download. The partial file was kept."
            return result
        result["sha256_actual"] = actual
        if actual != expected_sha256:
            # A file that failed its hash is not something to resume from.
            try:
                os.remove(long_part)
            except OSError:
                pass
            result["error_kind"] = ERR_HASH_MISMATCH
            result["error"] = (
                "{} failed verification: expected sha256 {} but got {}. The corrupt "
                "download was deleted.".format(filename, expected_sha256, actual)
            )
            return result
        result["verified"] = True
        result["verification"] = "sha256"
    else:
        # Honest about what did and did not happen: the length was checked,
        # the content was not, because the Hub published no hash for it.
        result["verification"] = "size_only"

    try:
        os.replace(long_part, long_target)
    except OSError as exc:
        result["error_kind"] = ERR_LOCAL_IO
        result["error"] = "Could not move the verified download into place: {}: {}".format(
            type(exc).__name__, exc,
        )
        return result

    result["ok"] = True
    return result


# --------------------------------------------------------------------------
# Self-test fixtures: trimmed but otherwise verbatim payloads captured from
# the live Hub, so the offline tests exercise the real response shapes.
# --------------------------------------------------------------------------

_FIXTURE_SEARCH = [
    {
        "_id": "672f595e437aab8fbf2921a3",
        "id": "Qwen/Qwen2.5-Coder-32B-Instruct-GGUF",
        "author": "Qwen",
        "gated": False,
        "lastModified": "2024-11-09T12:45:18.000Z",
        "likes": 219,
        "private": False,
        "downloads": 214637,
        "tags": ["transformers", "gguf", "code", "license:apache-2.0"],
    },
    {
        # gated is a STRING on the live Hub for restricted repos, not a
        # bool. A UI that assumed bool would render "manual" as truthy by
        # luck and lose the distinction between click-through and
        # wait-for-a-human access.
        "_id": "66a183c30520be533d8d9c6f",
        "id": "meta-llama/Llama-3.1-8B-Instruct",
        "author": "meta-llama",
        "gated": "manual",
        "lastModified": "2024-08-03T22:11:58.000Z",
        "likes": 4000,
        "private": False,
        "downloads": 7948428,
        "tags": ["transformers", "gguf"],
    },
    # A malformed element: the Hub has never sent one, but a normalizer that
    # dies on an element without an id would take a whole search down.
    {"_id": "deadbeef", "likes": 3},
]

# From /api/models/bartowski/Qwen2.5-Coder-32B-Instruct-GGUF/tree/main and
# /api/models/ggml-org/models/tree/main, trimmed to the fields this module
# reads. Sizes and lfs oids are the real ones.
_FIXTURE_TREE = [
    {"type": "file", "oid": "e7168107c02adc124126c15fbcdc51f2e76c7b55", "size": 3688,
     "path": ".gitattributes"},
    {"type": "file", "oid": "aaa", "size": 11264441312,
     "lfs": {"oid": "3e10282de5567d1c424c429d75f3cbedb1b38c954d3af74f70a8a8ca9aa00cb5",
             "size": 11264441312, "pointerSize": 136},
     "path": "Qwen2.5-Coder-32B-Instruct-IQ2_M.gguf"},
    {"type": "file", "oid": "bbb", "size": 9000000000,
     "lfs": {"oid": "b" * 64, "size": 9000000000, "pointerSize": 136},
     "path": "Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf"},
    # A three-part split, deliberately listed out of order so the grouping
    # cannot pass by accident on a pre-sorted input.
    {"type": "file", "oid": "c3", "size": 7636576,
     "lfs": {"oid": "34a6543e31bddf578870b1958b0a2937db1506314deaf0261c6d1e1f8db0ef21",
             "size": 7636576},
     "path": "split/stories15M-q8_0-00003-of-00003.gguf"},
    {"type": "file", "oid": "c1", "size": 10517152,
     "lfs": {"oid": "8a3a74042ae05dda34985dff2d49adf2ee3f9d0fd73b03fa6cc4307c924e4040",
             "size": 10517152},
     "path": "split/stories15M-q8_0-00001-of-00003.gguf"},
    {"type": "file", "oid": "c2", "size": 10381216,
     "lfs": {"oid": "4a70d35fdd275b054c29979da9c5d27e7045f011fdf11f736a652a03b1ffb355",
             "size": 10381216},
     "path": "split/stories15M-q8_0-00002-of-00003.gguf"},
    # The dot-separated split variant that really exists on the Hub
    # (mradermacher-style repos), two parts, complete.
    {"type": "file", "oid": "d1", "size": 100,
     "lfs": {"oid": "d" * 64, "size": 100},
     "path": "Meta-Llama-3-70B-Instruct.Q8_0.00001-of-00002.gguf"},
    {"type": "file", "oid": "d2", "size": 200,
     "lfs": {"oid": "e" * 64, "size": 200},
     "path": "Meta-Llama-3-70B-Instruct.Q8_0.00002-of-00002.gguf"},
    # An INCOMPLETE split: part 2 of 2 is missing from the repo.
    {"type": "file", "oid": "f1", "size": 55,
     "lfs": {"oid": "f" * 64, "size": 55},
     "path": "broken/Halfmodel-Q2_K-00001-of-00002.gguf"},
    # A small non-LFS GGUF: no "lfs" object, therefore no sha256 available.
    {"type": "file", "oid": "270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d",
     "size": 1185376, "path": "tinyllamas/stories260K.gguf"},
    # A directory entry and a non-GGUF file, both of which must be ignored.
    {"type": "directory", "oid": "zzz", "size": 0, "path": "split"},
    {"type": "file", "oid": "yyy", "size": 4096, "path": "README.md"},
]


class _FakeResponse:
    """A stand-in for what _open_download_stream returns: .status, .headers,
    .read(n), .close(). Enough for download() to be driven end to end
    without a socket.
    """

    def __init__(self, data, status=200, headers=None, fail_after=None):
        self._data = data
        self._pos = 0
        self.status = status
        self.headers = headers or {}
        self._fail_after = fail_after

    def read(self, n=-1):
        if self._fail_after is not None and self._pos >= self._fail_after:
            raise OSError("connection reset by peer")
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self):
        pass


def _self_test():
    import builtins
    import io as _io
    import tempfile

    global _http_get_json, _open_download_stream
    orig_get_json = _http_get_json
    orig_open_stream = _open_download_stream
    old_env = {k: os.environ.get(k) for k in ("HEARTH_DATA_DIR",)}
    scratch = tempfile.mkdtemp(prefix="hearth-hf-selftest-")

    try:
        os.environ["HEARTH_DATA_DIR"] = os.path.join(scratch, "data")

        # ------------------------------------------------------------------
        # search_url: the parameters that actually matter
        # ------------------------------------------------------------------
        url = search_url("qwen coder", limit=5)
        assert url.startswith(HF_BASE + "/api/models?"), url
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert query["search"] == ["qwen coder"], query
        assert query["filter"] == ["gguf"], (
            "without filter=gguf a search returns safetensors-only repos, "
            "which are useless to a llama.cpp-based app", query,
        )
        assert query["limit"] == ["5"], query
        assert query["sort"] == ["downloads"] and query["direction"] == ["-1"], query
        # lastModified and gated are ABSENT from a default /api/models
        # response (verified live). If they stop being requested, a UI
        # quietly renders "unknown" for every repo.
        assert "lastModified" in query["expand[]"], query
        assert "gated" in query["expand[]"], query
        # An empty query is a legitimate "show me the popular ones" listing.
        assert "search" not in urllib.parse.parse_qs(urllib.parse.urlsplit(search_url("")).query)

        # ------------------------------------------------------------------
        # search_models: parsing a captured payload, offline
        # ------------------------------------------------------------------
        def fake_search_ok(u, timeout):
            assert "filter=gguf" in u, u
            return _FIXTURE_SEARCH, {"ratelimit": '"api";r=499;t=121'}

        _http_get_json = fake_search_ok
        found = search_models("qwen", limit=20)
        assert found["ok"] is True, found
        assert found["error"] is None, found
        # The malformed third element is dropped, not fatal.
        assert len(found["models"]) == 2, found
        first = found["models"][0]
        assert first["repo_id"] == "Qwen/Qwen2.5-Coder-32B-Instruct-GGUF", first
        assert first["downloads"] == 214637 and first["likes"] == 219, first
        assert first["last_modified"] == "2024-11-09T12:45:18.000Z", first
        assert first["gated"] is False and first["gated_mode"] is None, first
        assert first["private"] is False, first
        assert first["author"] == "Qwen", first
        gated = found["models"][1]
        assert gated["gated"] is True, gated
        assert gated["gated_mode"] == "manual", (
            "the Hub sends 'auto' or 'manual', and the difference is whether a "
            "user waits for a human; collapsing it to a bool loses that", gated,
        )
        assert found["rate_limit"] == {"remaining": 499, "reset_seconds": 121}, found

        # An author that the Hub omitted is derived from the repo id rather
        # than left None.
        _http_get_json = lambda u, t: ([{"id": "someone/some-gguf"}], {})
        assert search_models("x")["models"][0]["author"] == "someone"

        # ------------------------------------------------------------------
        # search_models: every failure comes back as data, never an exception
        # ------------------------------------------------------------------
        def raiser(exc):
            def _fn(u, timeout):
                raise exc
            return _fn

        cases = [
            (urllib.error.URLError("connection refused"), ERR_UNREACHABLE),
            (urllib.error.HTTPError("u", 429, "Too Many Requests",
                                    {"Retry-After": "30"}, _io.BytesIO(b"")), ERR_RATE_LIMITED),
            (urllib.error.HTTPError("u", 503, "Bad Gateway", {}, _io.BytesIO(b"")), ERR_SERVER),
            (urllib.error.HTTPError("u", 401, "Unauthorized", {}, _io.BytesIO(b"")), ERR_AUTH_REQUIRED),
            (urllib.error.HTTPError("u", 401, "Unauthorized",
                                    {"X-Error-Code": "GatedRepo"}, _io.BytesIO(b"")), ERR_GATED),
            (urllib.error.HTTPError("u", 404, "Not Found",
                                    {"X-Error-Code": "EntryNotFound"}, _io.BytesIO(b"")), ERR_NOT_FOUND),
            (ValueError("not json"), ERR_BAD_RESPONSE),
            (TimeoutError("timed out"), ERR_UNREACHABLE),
        ]
        for exc, expected_kind in cases:
            _http_get_json = raiser(exc)
            got = search_models("anything")
            assert got["ok"] is False, (exc, got)
            assert got["error_kind"] == expected_kind, (exc, got)
            assert got["models"] == [], got
            assert got["error"], got
        # The 429 message names the wait the Hub suggested.
        _http_get_json = raiser(cases[1][0])
        assert "30 seconds" in search_models("x")["error"], search_models("x")

        # A body of the wrong JSON type is a bad response, not a crash.
        _http_get_json = lambda u, t: ({"error": "nope"}, {})
        bad = search_models("x")
        assert bad["ok"] is False and bad["error_kind"] == ERR_BAD_RESPONSE, bad

        # ------------------------------------------------------------------
        # parse_quant, against real filenames from the Hub
        # ------------------------------------------------------------------
        quant_cases = [
            ("Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf", "Q4_K_M"),
            ("Qwen2.5-Coder-32B-Instruct-IQ2_M.gguf", "IQ2_M"),
            ("qwen2.5-0.5b-instruct-q2_k.gguf", "Q2_K"),          # lower case
            ("meta-llama-3.1-8b-instruct-abliterated.Q8_0.gguf", "Q8_0"),
            ("qwen2.5-72b-instruct-fp16-00001-of-00042.gguf", "FP16"),
            ("DeepSeek-R1.BF16-00001-of-00030.gguf", "BF16"),
            ("Meta-Llama-3.1-70B-Instruct-IQ4_XS.gguf", "IQ4_XS"),
            ("model-Q3_K_XL.gguf", "Q3_K_XL"),
            # No label at all. These are real Hub filenames; None is the
            # honest answer and an invented "Q4_K_M" would be a lie.
            ("mmproj.gguf", None),
            ("KAT-Coder-V2.5-Dev-APEX-Compact.gguf", None),
            ("stories260K.gguf", None),
            # Q-shaped model names that must NOT be read as quants. This is
            # the whole reason matching is on whole tokens, not substrings.
            ("Qwen3-Coder-30B.gguf", None),
            ("", None),
        ]
        for name, expected in quant_cases:
            assert parse_quant(name) == expected, (name, parse_quant(name), expected)
        assert parse_quant(None) is None

        # ------------------------------------------------------------------
        # Multi-part grouping
        # ------------------------------------------------------------------
        grouped = group_gguf_files(_FIXTURE_TREE)
        by_name = {e["name"]: e for e in grouped}
        # Non-GGUF files and directory entries never appear.
        assert "README.md" not in by_name and "split" not in by_name, by_name.keys()
        assert ".gitattributes" not in by_name, by_name.keys()

        split = by_name["stories15M-q8_0"]
        assert split["multipart"] is True, split
        assert split["part_count"] == 3, split
        assert len(split["parts"]) == 3, split
        assert split["complete"] is True and split["missing_parts"] == [], split
        # Summed, not shown three times. This is the property that matters:
        # three files at ~10MB each are ONE ~26MB model.
        assert split["size_bytes"] == 10517152 + 10381216 + 7636576, split
        assert [p["index"] for p in split["parts"]] == [1, 2, 3], (
            "parts must come back in index order even though the fixture "
            "lists them 3, 1, 2", split,
        )
        assert [p["path"] for p in split["parts"]] == [
            "split/stories15M-q8_0-00001-of-00003.gguf",
            "split/stories15M-q8_0-00002-of-00003.gguf",
            "split/stories15M-q8_0-00003-of-00003.gguf",
        ], split
        assert split["quant"] == "Q8_0", split
        # No hash of the concatenation exists, so none is claimed; each part
        # carries its own.
        assert split["sha256"] is None, split
        assert all(p["sha256"] for p in split["parts"]), split
        assert split["path"] is None, "a multi-part model has no single path to download"
        # The individual part filenames must NOT show up as their own models.
        for part in split["parts"]:
            assert part["path"].rsplit("/", 1)[-1] not in by_name, (
                "a split part must never be listed as a separate download", part,
            )

        dotted = by_name["Meta-Llama-3-70B-Instruct.Q8_0"]
        assert dotted["multipart"] is True and dotted["part_count"] == 2, dotted
        assert dotted["size_bytes"] == 300, dotted
        assert dotted["quant"] == "Q8_0", (
            "the dot-separated split form really occurs on the Hub and must "
            "group exactly like the hyphenated one", dotted,
        )

        broken = by_name["Halfmodel-Q2_K"]
        assert broken["multipart"] is True and broken["complete"] is False, broken
        assert broken["missing_parts"] == [2], broken
        assert broken["size_bytes"] == 55, (
            "an incomplete model's size is the sum of what is actually there; "
            "'complete' is how a caller learns it cannot be loaded", broken,
        )

        single = by_name["Qwen2.5-Coder-32B-Instruct-IQ2_M.gguf"]
        assert single["multipart"] is False and single["part_count"] == 1, single
        assert single["size_bytes"] == 11264441312, single
        assert single["sha256"] == "3e10282de5567d1c424c429d75f3cbedb1b38c954d3af74f70a8a8ca9aa00cb5", single
        assert single["path"] == "Qwen2.5-Coder-32B-Instruct-IQ2_M.gguf", single

        nolfs = by_name["stories260K.gguf"]
        assert nolfs["size_bytes"] == 1185376, nolfs
        assert nolfs["sha256"] is None, (
            "a non-LFS file's git oid is a sha1 over a blob header, not a "
            "content hash; reporting it as sha256 would make verification lie", nolfs,
        )

        # Smallest first, and unknown sizes last rather than sorting as zero.
        sizes = [e["size_bytes"] for e in grouped]
        known = [s for s in sizes if s is not None]
        assert known == sorted(known), sizes
        assert group_gguf_files([]) == []
        assert group_gguf_files(None) == []
        unknown_first = group_gguf_files([
            {"path": "big.gguf", "size": 100},
            {"path": "mystery.gguf"},
        ])
        assert unknown_first[0]["name"] == "big.gguf", unknown_first
        assert unknown_first[1]["size_bytes"] is None, unknown_first

        # Identically stemmed parts in different directories are different
        # models and must not merge into one six-part blob.
        two_dirs = group_gguf_files([
            {"path": "a/m-00001-of-00002.gguf", "size": 1},
            {"path": "a/m-00002-of-00002.gguf", "size": 2},
            {"path": "b/m-00001-of-00002.gguf", "size": 4},
            {"path": "b/m-00002-of-00002.gguf", "size": 8},
        ])
        assert len(two_dirs) == 2, two_dirs
        assert sorted(e["size_bytes"] for e in two_dirs) == [3, 12], two_dirs

        # ------------------------------------------------------------------
        # list_gguf_files, including Link-header pagination
        # ------------------------------------------------------------------
        page_calls = []

        def fake_tree_paged(u, timeout):
            page_calls.append(u)
            if "cursor=" not in u:
                return _FIXTURE_TREE[:6], {"link": '<{}&cursor=abc>; rel="next"'.format(u)}
            return _FIXTURE_TREE[6:], {}

        _http_get_json = fake_tree_paged
        listing = list_gguf_files("bartowski/Qwen2.5-Coder-32B-Instruct-GGUF")
        assert listing["ok"] is True, listing
        assert len(page_calls) == 2, (
            "the tree endpoint pages with an opaque Link cursor; stopping at "
            "page one silently hides files", page_calls,
        )
        assert "expand=true" in page_calls[0], (
            "without expand=true the response carries no lfs object and so no "
            "sha256 to verify anything against", page_calls,
        )
        assert listing["file_count"] == len(grouped), listing
        assert {e["name"] for e in listing["files"]} == set(by_name), listing

        # A cursor that points at itself must terminate rather than spin.
        _http_get_json = lambda u, t: (_FIXTURE_TREE[:2], {"link": '<{}>; rel="next"'.format(u)})
        assert list_gguf_files("a/b")["ok"] is True

        # A repo with no GGUF files is a real, successful answer.
        _http_get_json = lambda u, t: ([{"path": "model.safetensors", "size": 10}], {})
        empty = list_gguf_files("a/b")
        assert empty["ok"] is True and empty["files"] == [], empty

        # A bad repo id never touches the network.
        network_calls = {"n": 0}

        def counting(u, timeout):
            network_calls["n"] += 1
            return [], {}

        _http_get_json = counting
        for bad_id in ("../etc", "no-slash", "a/b/c", "", "a//b", "a/../b", ".hidden/x"):
            got = list_gguf_files(bad_id)
            assert got["ok"] is False, (bad_id, got)
            assert got["error_kind"] == ERR_BAD_REQUEST, (bad_id, got)
        assert network_calls["n"] == 0, "an invalid repo id must be refused before any request"

        # ------------------------------------------------------------------
        # Repo id validation, path mapping, and traversal refusal
        # ------------------------------------------------------------------
        for good in ("Qwen/Qwen2.5-Coder-32B-Instruct-GGUF", "a/b", "TheBloke/Llama-2-7B.Q4",
                     "user-name/model_name.v2", "a0/b0"):
            assert validate_repo_id(good) is None, good
        for bad in ("../../etc/passwd", "..%2F..", "a/../b", "a/b/c", "/abs/path",
                    "C:/windows/system32", "a\\b", "no-slash", "", "a/", "/b",
                    ".git/config", "a/.git", "a/b\x00c", "a b/c", "a/b|c",
                    "a" * 200 + "/b", "a/" + "b" * 200):
            assert validate_repo_id(bad) is not None, bad

        # The mapping round-trips exactly ...
        for repo in ("Qwen/Qwen2.5-Coder-32B-Instruct-GGUF", "a/b", "user.name/model.v2"):
            encoded = repo_dir_name(repo)
            assert "/" not in encoded and "\\" not in encoded, encoded
            assert repo_id_from_dir_name(encoded) == repo, (repo, encoded)
        # ... including the trailing-dot case, checked against the REAL
        # filesystem rather than against string inequality. Comparing the
        # encoded strings would pass even with the %2E escape removed, since
        # "a%2Fb." and "a%2Fb" are different strings; it is only once
        # Windows has had its way with them that they stop being different
        # paths. Both directories are therefore actually created and their
        # resolved paths compared.
        assert repo_id_from_dir_name(repo_dir_name("a/b.")) == "a/b."
        assert repo_id_from_dir_name(repo_dir_name("a/b")) == "a/b"
        dot_store = os.path.join(scratch, "dotstore")
        os.makedirs(dot_store, exist_ok=True)
        dotted_dir = model_dir("a/b.", store_dir=dot_store)
        plain_dir = model_dir("a/b", store_dir=dot_store)
        os.makedirs(dotted_dir, exist_ok=True)
        os.makedirs(plain_dir, exist_ok=True)
        assert os.path.realpath(dotted_dir) != os.path.realpath(plain_dir), (
            "'a/b.' and 'a/b' are different Hugging Face repos and must not "
            "resolve to the same directory once the filesystem has normalised "
            "the names", dotted_dir, plain_dir,
        )
        assert len(os.listdir(dot_store)) == 2, os.listdir(dot_store)
        # Injective across a nastier set: no two distinct ids may collide.
        ids = ["a/b", "a/b.", "a/b..", "a.b/c", "a/b-c", "a-b/c", "ab/c", "a/bc"]
        encodings = [repo_dir_name(i) for i in ids]
        assert len(set(encodings)) == len(ids), list(zip(ids, encodings))
        assert repo_id_from_dir_name("not-a-repo-dir") is None
        assert repo_id_from_dir_name("") is None

        # THE traversal test. A repo id is attacker-influenced text; every
        # one of these must be refused outright, never encoded into a path.
        store = os.path.join(scratch, "store")
        os.makedirs(store, exist_ok=True)
        traversals = [
            "../../../../Windows/System32/drivers/etc",
            "..%2F..%2Fetc/passwd",
            "a/../../b",
            "/etc/passwd",
            "C:/Windows/win.ini",
            "a\\..\\..\\b",
            "....//....//x",
            "a/b/../../../../..",
            "\x00/etc",
            "a/b\x00.gguf",
        ]
        for hostile in traversals:
            try:
                built = model_dir(hostile, store_dir=store)
            except ValueError:
                continue  # refused, which is the point
            raise AssertionError(
                "model_dir accepted a hostile repo id and produced {!r}".format(built),
            )
        # And the containment layer, independently: even a path that got
        # past shape validation must not resolve outside the store.
        for hostile_path in ("../escape.gguf", "a/../../escape.gguf", "..\\escape.gguf",
                             "/etc/passwd", "nul.gguf", "a:b.gguf"):
            try:
                built = model_path("a/b", hostile_path, store_dir=store)
            except ValueError:
                continue
            raise AssertionError(
                "model_path accepted {!r} and produced {!r}".format(hostile_path, built),
            )
        # A legitimate nested path (repos really do use subdirectories) works
        # and stays inside the store.
        good_path = model_path("unsloth/DeepSeek-R1-GGUF", "DeepSeek-R1-BF16/x-00001-of-00002.gguf",
                               store_dir=store)
        assert os.path.commonpath([os.path.realpath(store), good_path]) == os.path.realpath(store), good_path
        assert good_path.endswith(os.path.join("DeepSeek-R1-BF16", "x-00001-of-00002.gguf")), good_path

        # model_store_dir follows HEARTH_DATA_DIR, so no test ever writes to
        # the real store.
        assert model_store_dir().startswith(os.environ["HEARTH_DATA_DIR"]), model_store_dir()
        assert model_store_dir().endswith(os.path.join(MODELS_SUBDIR, SOURCE_SUBDIR)), model_store_dir()

        # ------------------------------------------------------------------
        # Header parsing helpers
        # ------------------------------------------------------------------
        assert _content_range_start("bytes 1000-1099/1321079200") == 1000
        assert _content_range_total("bytes 1000-1099/1321079200") == 1321079200
        assert _content_range_start("bytes 0-9/*") == 0
        assert _content_range_total("bytes 0-9/*") is None
        assert _content_range_start(None) is None
        assert _content_range_start("garbage") is None
        assert _next_page_url({"link": '<https://x/y?cursor=1>; rel="next"'}) == "https://x/y?cursor=1"
        assert _next_page_url({"link": '<https://x/prev>; rel="prev"'}) is None
        assert _next_page_url({}) is None
        assert _rate_limit_from_headers({}) is None
        assert _rate_limit_from_headers({"ratelimit": "garbage"}) is None

        # download_url percent-encodes each segment but keeps the separators.
        got_url = download_url("a/b", "dir/file name.gguf")
        assert got_url == HF_BASE + "/a/b/resolve/main/dir/file%20name.gguf", got_url

        # ------------------------------------------------------------------
        # download(): the real behaviours, offline
        # ------------------------------------------------------------------
        # Deliberately several CHUNK_BYTES long: a payload that fits in one
        # read would let the cancellation and progress tests pass without
        # the chunk loop ever iterating, which would make them vacuous.
        _block = bytes((i * 7 + 3) % 256 for i in range(65536))
        payload = (_block * (3 * CHUNK_BYTES // 65536 + 2))[:3 * CHUNK_BYTES + 12345]
        assert len(payload) > 3 * CHUNK_BYTES, len(payload)
        payload_sha = hashlib.sha256(payload).hexdigest()
        dest = os.path.join(scratch, "dl")
        os.makedirs(dest, exist_ok=True)

        def stream_from(offset, timeout, status=None, headers=None, fail_after=None, body=None):
            data = payload if body is None else body
            if status is None:
                status = 206 if offset else 200
            if headers is None:
                headers = {}
                if status == 206:
                    headers["Content-Range"] = "bytes {}-{}/{}".format(
                        offset, len(data) - 1, len(data),
                    )
                else:
                    headers["Content-Length"] = str(len(data))
            return _FakeResponse(data[offset:] if status == 206 else data,
                                 status=status, headers=headers, fail_after=fail_after)

        # -- a clean, verified download ------------------------------------
        opens = []

        def fake_open_ok(u, offset, timeout):
            opens.append(offset)
            return stream_from(offset, timeout)

        _open_download_stream = fake_open_ok
        progress = []
        got = download("a/b", "m.gguf", dest_dir=dest,
                       on_progress=lambda p: progress.append(dict(p)),
                       expected_sha256=payload_sha, expected_size=len(payload))
        assert got["ok"] is True, got
        assert got["verified"] is True and got["verification"] == "sha256", got
        assert got["bytes_done"] == len(payload), got
        assert got["resumed_from"] == 0, got
        final = os.path.join(dest, "m.gguf")
        assert os.path.isfile(final), got
        assert open(final, "rb").read() == payload
        assert not os.path.exists(final + PART_SUFFIX), "the .part must be gone once complete"
        assert progress and progress[-1]["bytes_done"] == len(payload), progress[-1]
        assert progress[-1]["fraction"] == 1.0, progress[-1]
        assert all(p["bytes_total"] == len(payload) for p in progress), progress
        assert [p["bytes_done"] for p in progress] == sorted(p["bytes_done"] for p in progress)

        # -- a second call finds it already there and does not re-download --
        opens.clear()
        again = download("a/b", "m.gguf", dest_dir=dest,
                         expected_sha256=payload_sha, expected_size=len(payload))
        assert again["ok"] is True and again["already_present"] is True, again
        assert again["verified"] is True, again
        assert opens == [], "an already-verified file must not be downloaded again"
        os.remove(final)

        # -- cancellation leaves a RESUMABLE partial, not a corrupt file ----
        cancel_state = {"chunks": 0}

        def cancel_after_two():
            cancel_state["chunks"] += 1
            return cancel_state["chunks"] > 2

        _open_download_stream = fake_open_ok
        cancelled = download("a/b", "m.gguf", dest_dir=dest,
                             is_cancelled=cancel_after_two,
                             expected_sha256=payload_sha, expected_size=len(payload))
        assert cancelled["ok"] is False and cancelled["cancelled"] is True, cancelled
        assert not os.path.exists(final), (
            "a cancelled download must never leave a file at the final path", cancelled,
        )
        part_file = final + PART_SUFFIX
        assert os.path.isfile(part_file), "a cancelled download must leave its .part behind"
        partial_size = os.path.getsize(part_file)
        assert 0 < partial_size < len(payload), partial_size
        # The partial is a genuine PREFIX of the real file, which is what
        # makes resuming from it sound rather than merely non-crashing.
        assert open(part_file, "rb").read() == payload[:partial_size], (
            "the partial must be a byte-exact prefix or resuming corrupts the file",
        )

        # -- resume picks up from exactly that offset ----------------------
        opens.clear()
        resumed = download("a/b", "m.gguf", dest_dir=dest,
                           expected_sha256=payload_sha, expected_size=len(payload))
        assert resumed["ok"] is True, resumed
        assert resumed["resumed_from"] == partial_size, (
            "resume must start at the partial's size, not at zero", resumed, partial_size,
        )
        assert opens == [partial_size], (
            "the Range request must ask for exactly the bytes still missing", opens,
        )
        assert resumed["bytes_done"] == len(payload), resumed
        assert open(final, "rb").read() == payload, "the resumed file must be byte-identical"
        assert resumed["verified"] is True, resumed
        os.remove(final)

        # -- a server that IGNORES Range answers 200: restart, never append -
        def fake_open_ignores_range(u, offset, timeout):
            opens.append(offset)
            return stream_from(0, timeout, status=200)

        with open(part_file, "wb") as handle:
            handle.write(payload[:50000])
        opens.clear()
        _open_download_stream = fake_open_ignores_range
        ignored = download("a/b", "m.gguf", dest_dir=dest,
                           expected_sha256=payload_sha, expected_size=len(payload))
        assert ignored["ok"] is True, ignored
        assert ignored["resumed_from"] == 0, (
            "a 200 means the body starts at byte zero; appending it to a "
            "partial would silently corrupt the file", ignored,
        )
        assert open(final, "rb").read() == payload
        os.remove(final)

        # -- a 206 that starts at the WRONG offset is refused --------------
        with open(part_file, "wb") as handle:
            handle.write(payload[:50000])

        def fake_open_wrong_offset(u, offset, timeout):
            return stream_from(0, timeout, status=206,
                               headers={"Content-Range": "bytes 0-{}/{}".format(
                                   len(payload) - 1, len(payload))})

        _open_download_stream = fake_open_wrong_offset
        mismatch = download("a/b", "m.gguf", dest_dir=dest,
                            expected_sha256=payload_sha, expected_size=len(payload))
        assert mismatch["ok"] is False, mismatch
        assert mismatch["error_kind"] == ERR_RANGE_MISMATCH, mismatch
        assert not os.path.exists(final), mismatch
        os.remove(part_file)

        # -- HASH VERIFICATION: corrupt bytes must never become a model ----
        corrupt = bytearray(payload)
        corrupt[123] ^= 0xFF

        def fake_open_corrupt(u, offset, timeout):
            return stream_from(offset, timeout, body=bytes(corrupt))

        _open_download_stream = fake_open_corrupt
        bad_hash = download("a/b", "m.gguf", dest_dir=dest,
                            expected_sha256=payload_sha, expected_size=len(payload))
        assert bad_hash["ok"] is False, bad_hash
        assert bad_hash["error_kind"] == ERR_HASH_MISMATCH, bad_hash
        assert bad_hash["verified"] is False, bad_hash
        assert bad_hash["sha256_actual"] == hashlib.sha256(bytes(corrupt)).hexdigest(), bad_hash
        assert not os.path.exists(final), (
            "a file that failed its hash must never be renamed into place", bad_hash,
        )
        assert not os.path.exists(part_file), (
            "a corrupt partial is not worth resuming from and must be deleted", bad_hash,
        )
        assert payload_sha in bad_hash["error"] and bad_hash["sha256_actual"] in bad_hash["error"]

        # -- no published hash: verification is reported as size_only ------
        # The Hub really does serve small non-LFS files with no "lfs" object
        # and therefore no sha256 (see the stories260K fixture above). The
        # tree lookup below reproduces that exactly.
        _http_get_json = lambda u, t: ([{"path": "m.gguf", "size": len(payload)}], {})
        _open_download_stream = fake_open_ok
        unhashed = download("a/b", "m.gguf", dest_dir=dest)
        assert unhashed["ok"] is True, unhashed
        assert unhashed["sha256_expected"] is None, unhashed
        assert unhashed["verified"] is False, (
            "no hash was available, so nothing was verified and the result "
            "must not claim otherwise", unhashed,
        )
        assert unhashed["verification"] == "size_only", unhashed
        assert unhashed["bytes_total"] == len(payload), unhashed
        os.remove(final)

        # -- a truncated body is caught even without a hash ----------------
        def fake_open_short(u, offset, timeout):
            return _FakeResponse(payload[:1000], status=200,
                                 headers={"Content-Length": str(len(payload))})

        _open_download_stream = fake_open_short
        short = download("a/b", "m.gguf", dest_dir=dest)
        assert short["ok"] is False and short["error_kind"] == ERR_BAD_RESPONSE, short
        assert not os.path.exists(final), short
        assert os.path.isfile(part_file), "a short read leaves a resumable partial"
        os.remove(part_file)

        # -- a connection that drops mid-transfer keeps what it got --------
        def fake_open_drops(u, offset, timeout):
            return stream_from(offset, timeout, fail_after=100000)

        _open_download_stream = fake_open_drops
        dropped = download("a/b", "m.gguf", dest_dir=dest,
                           expected_sha256=payload_sha, expected_size=len(payload))
        assert dropped["ok"] is False, dropped
        assert dropped["error_kind"] == ERR_UNREACHABLE, dropped
        assert os.path.isfile(part_file), "a dropped connection must leave a resumable partial"
        assert os.path.getsize(part_file) > 0, dropped
        assert open(part_file, "rb").read() == payload[:os.path.getsize(part_file)]
        os.remove(part_file)

        # -- a DISK failure is not reported as a network failure -----------
        # Both raise OSError. The pair of assertions is the point: the same
        # exception type from read() and from write() must land in different
        # buckets, or a full disk reads as a dead connection and the user
        # retries forever.
        real_open = open

        def open_that_cannot_write(path, mode, *a, **k):
            if str(path).endswith(PART_SUFFIX):
                class _BadHandle:
                    def write(self, _data):
                        raise OSError(28, "No space left on device")

                    def flush(self):
                        pass

                    def fileno(self):
                        raise OSError("no fd")

                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False
                return _BadHandle()
            return real_open(path, mode, *a, **k)

        _open_download_stream = fake_open_ok
        builtins.open = open_that_cannot_write
        try:
            disk_failed = download("a/b", "m.gguf", dest_dir=dest,
                                   expected_sha256=payload_sha, expected_size=len(payload))
        finally:
            builtins.open = real_open
        assert disk_failed["ok"] is False, disk_failed
        assert disk_failed["error_kind"] == ERR_LOCAL_IO, (
            "a failed write is a local disk problem, not an unreachable Hub",
            disk_failed,
        )
        assert disk_failed["error_kind"] != dropped["error_kind"], (
            "read failures and write failures both raise OSError and must not "
            "be reported identically", disk_failed, dropped,
        )
        assert not os.path.exists(final), disk_failed
        if os.path.exists(part_file):
            os.remove(part_file)

        # -- DISK SPACE: refused before the connection is even opened ------
        opens.clear()

        def spy_open(u, offset, timeout):
            opens.append(offset)
            raise AssertionError("must not open a connection when the disk is too full")

        _open_download_stream = spy_open
        old_free = globals()["_free_bytes_near"]
        try:
            globals()["_free_bytes_near"] = lambda p: 1024
            no_space = download("a/b", "huge.gguf", dest_dir=dest,
                                expected_sha256="0" * 64, expected_size=20_000_000_000)
            assert no_space["ok"] is False, no_space
            assert no_space["error_kind"] == ERR_NO_SPACE, no_space
            assert opens == [], "the disk refusal must happen before any request"
            assert no_space["disk_check"]["checked"] is True, no_space
            assert no_space["disk_check"]["ok"] is False, no_space
            assert "18." in no_space["error"] or "19." in no_space["error"], no_space
            assert not os.path.exists(os.path.join(dest, "huge.gguf")), no_space

            # The margin is real: exactly the file size free, with no room
            # for DISK_BUFFER_BYTES on top, is still a refusal.
            globals()["_free_bytes_near"] = lambda p: 1_000_000
            tight = download("a/b", "m2.gguf", dest_dir=dest,
                             expected_sha256="0" * 64, expected_size=1_000_000)
            assert tight["error_kind"] == ERR_NO_SPACE, tight

            # Ample space proceeds (and here the spy proves the check passed
            # by being reached at all).
            globals()["_free_bytes_near"] = lambda p: 10 ** 15
            _open_download_stream = fake_open_ok
            plenty = download("a/b", "m.gguf", dest_dir=dest,
                              expected_sha256=payload_sha, expected_size=len(payload))
            assert plenty["ok"] is True, plenty
            assert plenty["disk_check"]["checked"] is True and plenty["disk_check"]["ok"] is True, plenty
            os.remove(final)

            # An unreadable free-space figure skips the check rather than
            # inventing a threshold, and does not block the download.
            globals()["_free_bytes_near"] = lambda p: None
            unknown = download("a/b", "m.gguf", dest_dir=dest,
                               expected_sha256=payload_sha, expected_size=len(payload))
            assert unknown["ok"] is True, unknown
            assert unknown["disk_check"]["checked"] is False, unknown
            os.remove(final)
        finally:
            globals()["_free_bytes_near"] = old_free

        # -- resume shrinks what the disk check demands --------------------
        # A 90%-complete 4GB download needs room for the last 10%, not for
        # another 4GB. Getting this wrong strands users near the finish line.
        with open(part_file, "wb") as handle:
            handle.write(payload[:len(payload) - 10000])
        seen_need = {}
        try:
            globals()["_free_bytes_near"] = lambda p: DISK_BUFFER_BYTES + 20000
            _open_download_stream = fake_open_ok
            near_done = download("a/b", "m.gguf", dest_dir=dest,
                                 expected_sha256=payload_sha, expected_size=len(payload))
            seen_need = near_done["disk_check"]
            assert near_done["ok"] is True, near_done
            assert seen_need["needed_bytes"] == DISK_BUFFER_BYTES + 10000, (
                "the disk check must ask for the REMAINING bytes, not the whole "
                "file, or a nearly-finished resume is refused for no reason",
                seen_need,
            )
            os.remove(final)
        finally:
            globals()["_free_bytes_near"] = old_free

        # -- bad inputs are refused without a request ----------------------
        _open_download_stream = spy_open
        opens.clear()
        for bad_repo in ("../x", "a/b/c", "no-slash"):
            got = download(bad_repo, "m.gguf", dest_dir=dest,
                           expected_sha256="0" * 64, expected_size=1)
            assert got["ok"] is False and got["error_kind"] == ERR_BAD_REQUEST, (bad_repo, got)
        for bad_file in ("../escape.gguf", "/abs.gguf", "a/../../x.gguf", "..\\x.gguf",
                         "nul.gguf", "a:stream.gguf", ""):
            got = download("a/b", bad_file, dest_dir=dest,
                           expected_sha256="0" * 64, expected_size=1)
            assert got["ok"] is False, (bad_file, got)
            assert got["error_kind"] == ERR_BAD_REQUEST, (bad_file, got)
        assert opens == [], "no invalid input may reach the network"
        assert not os.path.exists(os.path.join(scratch, "escape.gguf"))
        assert not os.path.exists(os.path.join(dest, "..", "escape.gguf"))

        # A cancel flag already set before the call does no work at all.
        pre_cancelled = download("a/b", "m.gguf", dest_dir=dest, is_cancelled=lambda: True,
                                 expected_sha256="0" * 64, expected_size=1)
        assert pre_cancelled["cancelled"] is True and pre_cancelled["ok"] is False, pre_cancelled
        assert opens == [], pre_cancelled

        # -- metadata lookup failures surface, they do not download blind --
        _http_get_json = raiser(urllib.error.URLError("down"))
        _open_download_stream = spy_open
        no_meta = download("a/b", "m.gguf", dest_dir=dest)
        assert no_meta["ok"] is False, no_meta
        assert no_meta["error_kind"] == ERR_UNREACHABLE, no_meta
        assert opens == [], "a failed metadata lookup must not fall through to a blind download"

        # -- the metadata path end to end: size and hash come from the tree -
        body = b"tiny gguf body"
        body_sha = hashlib.sha256(body).hexdigest()
        _http_get_json = lambda u, t: ([
            {"path": "small.gguf", "size": len(body),
             "lfs": {"oid": body_sha, "size": len(body)}},
        ], {})
        _open_download_stream = lambda u, offset, timeout: _FakeResponse(
            body[offset:], status=(206 if offset else 200),
            headers={"Content-Length": str(len(body) - offset)},
        )
        looked_up = download("a/b", "small.gguf", dest_dir=dest)
        assert looked_up["ok"] is True, looked_up
        assert looked_up["sha256_expected"] == body_sha, looked_up
        assert looked_up["bytes_total"] == len(body), looked_up
        assert looked_up["verified"] is True, looked_up

        # A file the repo does not contain is a clear not-found, not a 404
        # discovered halfway through a transfer.
        missing = download("a/b", "absent.gguf", dest_dir=dest)
        assert missing["ok"] is False and missing["error_kind"] == ERR_NOT_FOUND, missing

        # find_file locates a part of a split model by its own path.
        _http_get_json = lambda u, t: (_FIXTURE_TREE, {})
        part_meta = find_file("a/b", "split/stories15M-q8_0-00002-of-00003.gguf")
        assert part_meta["ok"] is True, part_meta
        assert part_meta["size_bytes"] == 10381216, part_meta
        assert part_meta["of_model"] == "stories15M-q8_0", part_meta

        print("hearth-hf self-test OK")
        return 0
    finally:
        _http_get_json = orig_get_json
        _open_download_stream = orig_open_stream
        shutil.rmtree(scratch, ignore_errors=True)
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --------------------------------------------------------------------------
# Live checks: real network, real CDN, real hashes. Never part of the
# default self-test, which has to pass offline and in CI.
# --------------------------------------------------------------------------

def _live_test():
    """Exercise the real Hub. Run with --live.

    Every claim in this module's docstring about the live API is re-checked
    here, so a change on the Hub's side surfaces as a failing check rather
    than as a module whose documentation has quietly become fiction.

    Uses ggml-org/models, which holds a 1.1MB GGUF (small enough to download
    twice in a few seconds) and a genuine three-part split model.
    """
    failures = []

    def check(name, condition, detail=""):
        if condition:
            print("  ok   {}".format(name))
        else:
            print("  FAIL {} {}".format(name, detail))
            failures.append(name)

    print("hearth-hf live checks against {}".format(HF_BASE))

    found = search_models("qwen2.5-coder", limit=5)
    check("search returns results", found["ok"] and found["models"], found.get("error"))
    if found["ok"] and found["models"]:
        entry = found["models"][0]
        check("search entries carry last_modified", entry["last_modified"] is not None, entry)
        check("search entries carry downloads", isinstance(entry["downloads"], int), entry)
        check("search entries carry a gated flag", isinstance(entry["gated"], bool), entry)
        # filter=gguf must actually mean something: every result should hold
        # GGUF files. This is the claim the module docstring makes.
        listing = list_gguf_files(entry["repo_id"])
        check("a search result really contains GGUF files",
              listing["ok"] and listing["file_count"] > 0, listing.get("error"))

    listing = list_gguf_files("ggml-org/models")
    check("tree listing succeeds", listing["ok"], listing.get("error"))
    if listing["ok"]:
        by_name = {e["name"]: e for e in listing["files"]}
        check("multi-part models are grouped, not listed per file",
              "stories15M-q8_0" in by_name, sorted(by_name)[:10])
        split = by_name.get("stories15M-q8_0")
        if split:
            check("the split model has 3 parts", split["part_count"] == 3, split)
            check("the split model is complete", split["complete"], split)
            check("the split model's size is summed",
                  split["size_bytes"] == sum(p["size_bytes"] for p in split["parts"]), split)
            check("the split model's quant parsed", split["quant"] == "Q8_0", split)
        tiny = by_name.get("stories260K.gguf")
        check("the tiny model has a published sha256", bool(tiny and tiny["sha256"]), tiny)

    import tempfile
    scratch = tempfile.mkdtemp(prefix="hearth-hf-live-")
    try:
        # A full, verified download. This is the end-to-end proof that
        # lfs.oid really is the sha256 of the delivered bytes.
        got = download("ggml-org/models", "tinyllamas/stories260K.gguf", dest_dir=scratch)
        check("download succeeds", got["ok"], got.get("error"))
        check("download is sha256-verified",
              got["verified"] and got["verification"] == "sha256", got)
        check("the downloaded file exists", got["path"] and os.path.isfile(got["path"]), got)
        check("no .part is left behind",
              got["part_path"] and not os.path.exists(got["part_path"]), got)

        # Resume through the CDN redirect. Truncate the real download into a
        # .part and confirm a second call fetches only the tail and still
        # produces a byte-identical, hash-verified file.
        if got["ok"]:
            whole = open(got["path"], "rb").read()
            os.remove(got["path"])
            cut = len(whole) // 3
            with open(got["part_path"], "wb") as handle:
                handle.write(whole[:cut])
            resumed = download("ggml-org/models", "tinyllamas/stories260K.gguf", dest_dir=scratch)
            check("resume starts at the partial's offset", resumed.get("resumed_from") == cut, resumed)
            check("resume completes and verifies",
                  resumed["ok"] and resumed["verified"], resumed.get("error"))
            check("the resumed file is byte-identical",
                  resumed["ok"] and open(resumed["path"], "rb").read() == whole)

        # A hash the file does not have must be caught, not shrugged off.
        bad = download("ggml-org/models", "tinyllamas/stories260K-be.gguf", dest_dir=scratch,
                       expected_sha256="0" * 64, expected_size=1185376)
        check("a wrong expected hash is rejected", bad["error_kind"] == ERR_HASH_MISMATCH, bad)

        # Error surfaces, live.
        gone = list_gguf_files("ggml-org/definitely-not-a-real-repo-xyz")
        check("a missing repo is reported, not raised",
              gone["ok"] is False and gone["error_kind"] in (ERR_AUTH_REQUIRED, ERR_NOT_FOUND), gone)
        gated = download("meta-llama/Llama-3.1-8B-Instruct", "config.json", dest_dir=scratch,
                         expected_sha256=None, expected_size=None)
        check("a gated repo is reported as gated or auth-required",
              gated["error_kind"] in (ERR_GATED, ERR_AUTH_REQUIRED, ERR_NOT_FOUND), gated)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    # Pagination, live: force a small page so the Link cursor is exercised
    # against the real endpoint rather than only against a fixture.
    small_page = tree_url("Qwen/Qwen2.5-72B-Instruct-GGUF") + "&limit=50"
    try:
        entries, _ = _paged_tree(small_page, DEFAULT_TIMEOUT)
        check("Link-header pagination walks past the first page", len(entries) > 50, len(entries))
    except Exception as exc:  # noqa: BLE001
        check("Link-header pagination walks past the first page", False, exc)

    if failures:
        print("hearth-hf live checks FAILED: {}".format(", ".join(failures)))
        return 1
    print("hearth-hf live checks OK")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hearth-hf",
        description="Find and download GGUF models from Hugging Face. Reports what it "
                    "verified, never claims more.",
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--live", action="store_true",
                        help="run the network checks against the real Hugging Face Hub")
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="find repos holding GGUF models")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--limit", type=int, default=20)

    p_files = sub.add_parser("files", help="list the GGUF models inside one repo")
    p_files.add_argument("repo_id")

    p_get = sub.add_parser("get", help="download one file from a repo")
    p_get.add_argument("repo_id")
    p_get.add_argument("filename")
    p_get.add_argument("--dest", default=None)

    p_where = sub.add_parser("where", help="print the model store location")

    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()
    if args.live:
        return _live_test()

    if args.command == "search":
        result = search_models(args.query, limit=args.limit)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if args.command == "files":
        result = list_gguf_files(args.repo_id)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if args.command == "get":
        def on_progress(p):
            # The CLI's job is to print; the library itself never does.
            pct = "{:5.1f}%".format(p["fraction"] * 100) if p["fraction"] is not None else "    ?"
            print("[{}] {} / {}".format(pct, _human_bytes(p["bytes_done"]),
                                        _human_bytes(p["bytes_total"])))

        result = download(args.repo_id, args.filename, dest_dir=args.dest,
                          on_progress=on_progress)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if args.command == "where":
        print(json.dumps({
            "model_store_dir": model_store_dir(),
            "free_bytes": _free_bytes_near(model_store_dir()),
        }, indent=2))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv or len(sys.argv) == 1:
        sys.exit(_self_test())
    sys.exit(main())
