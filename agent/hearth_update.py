#!/usr/bin/env python3
r"""Hearth's updater: fetch, verify, refuse, stage. Never execute.

An auto-updater is the highest-value target a desktop application has. It is
a mechanism that downloads code and runs it with the user's privileges, on a
schedule, without asking. Get it wrong and the product ships a backdoor into
every install that is more reliable than any exploit, because it is supposed
to work.

So this is built to the same rules as scripts/vendor_llama.py, which is this
project's other supply chain, and the rules are restated here rather than
referenced because they are the whole design.

  1. THE TRUST ANCHOR IS IN THE APPLICATION, NOT ON THE WIRE. Every release is
     described by a manifest signed with an Ed25519 key whose PUBLIC half is
     committed to this repository (release/trust.json) and shipped inside the
     installed application. The private half never touches the release host.
     A release host that is compromised, substituted, or simply not ours can
     serve any bytes it likes and none of them will be accepted, because it
     cannot produce a signature. TLS is still used, and TLS is still not the
     check: TLS proves you reached a host, and the host is exactly the thing
     that gets taken over.

     This is the difference between this and a sha512 in a latest.yml. A hash
     fetched over the same connection as the artifact proves only that the
     file matches itself: whoever serves you the bytes serves you the hash of
     those bytes. See the "why not electron-updater" section below.

  2. VERIFICATION HAPPENS BEFORE ANYTHING IS TREATED AS CODE, and a failure
     leaves the existing install untouched. The manifest's signature is
     checked before a single artifact byte is requested. The artifact's
     SHA-256 is checked, against the size and digest in the SIGNED manifest,
     while it streams and again on disk, before it is renamed out of its
     .part file. Nothing is ever moved into place unverified, and nothing is
     ever run from here at all.

  3. NOTHING IN THIS MODULE CAN EXECUTE ANYTHING. No process spawning, no
     foreign-function machinery, no dynamic evaluation, no module runner.
     _self_test asserts it by scanning this file's own source for the names
     that would allow any of those, so the property survives an edit by
     somebody who did not read this paragraph. Launching the verified installer is the
     desktop shell's job (desktop/shell/main.js), which re-hashes the file
     immediately before spawning it -- see "the handoff" below.

  4. DOWNGRADE IS AN ATTACK, NOT AN EDGE CASE. An attacker who can serve
     files can serve a genuinely, validly signed OLD release with a known
     vulnerability. Signature checking alone does not stop that: the
     signature is real. Three things do, all of them in evaluate():
     the version must be strictly greater than what is installed, it must
     be at or above a floor persisted in the user's data directory that only
     ever moves up, and the manifest must not have expired. The last one is
     the freeze attack: without an expiry, an attacker who can only withhold
     traffic pins a user on today's version forever by replaying today's
     manifest, and no amount of version comparison notices.

  5. EVERY RESPONSE IS BOUNDED. The manifest read is capped, the artifact
     read is capped at exactly the signed size plus one byte, free disk space
     is checked before a download starts, and the release notes are length
     limited. A response that keeps sending is cut off rather than allowed to
     fill a disk.

Why not electron-builder's updater
----------------------------------
electron-builder ships electron-updater, with latest.yml and publish
providers, and it is the obvious choice. It was read rather than assumed, and
it is not used here, for three reasons that are specific rather than
stylistic.

  * ITS INTEGRITY CHECK IS SELF-REFERENTIAL WHEN THE APP IS UNSIGNED. On
    Windows, electron-updater compares the downloaded installer against a
    sha512 that it read out of latest.yml -- which it fetched from the same
    host, over the same connection, moments earlier. Against a compromised or
    substituted release host that is not a check at all. Its real defence is
    verifyUpdateCodeSignature, which shells out to PowerShell's
    Get-AuthenticodeSignature and compares the publisher name. Hearth has no
    certificate (see docs/packaging-windows.md), so that check has nothing to
    compare against. An updater that silently degrades to "the host said so"
    on an unsigned app is precisely the thing this project was told not to
    build.

  * SIGNING WOULD NOT FULLY FIX IT EITHER. Authenticode proves the installer
    was signed by a holder of the certificate. It says nothing about WHICH
    signed installer, so it does not stop a rollback to an older, still
    validly signed build. Downgrade protection has to come from a signed
    statement about versions, which is what the manifest here is.

  * THE PROTECTION IT DOES OFFER ARRIVES ONLY WITH A PURCHASE. A signature
    over the artifact with our own key protects users today, on an unsigned
    build, and keeps protecting them after a certificate is bought. It is
    strictly more coverage for strictly less money, and it does not depend on
    a procurement decision landing.

None of that is a criticism of electron-updater on a signed app with a
trusted host. It is a statement that Hearth is neither of those yet, and that
the smaller mechanism here covers the case Hearth is actually in. When a
certificate does arrive, Authenticode becomes a SECOND, independent check
that Windows itself enforces at launch, on top of this one. The two are
complementary and this module does not become redundant: it is what stops a
rollback and what stops a host from choosing which signed build you get.

The handoff, and why the shell re-hashes
----------------------------------------
This module ends by putting a verified file on disk and writing a receipt
next to it. It does not launch it, because a module that decides whether
bytes are trustworthy must not also be able to run them.

The shell reads the receipt over the authenticated loopback API and then does
three things of its own before spawning anything: it re-computes the file's
SHA-256, it refuses any path outside the staging directory, and it refuses a
version that is not greater than the version compiled into its own asar
(which EnableEmbeddedAsarIntegrityValidation makes tamper-evident). The
re-hash is not ceremony. A verified installer sits on disk for as long as the
user takes to click, and any other process running as that user can overwrite
it in that window. Hashing immediately before the spawn closes that window to
the width of a single syscall.

What a user actually gets, before and after code signing
--------------------------------------------------------
BEFORE (today, unsigned):
  * A release host cannot push code to Hearth users. It can withhold updates,
    and the expiry bounds how long that goes unnoticed.
  * A network attacker with a valid certificate for the release host cannot
    push code either.
  * A rollback to an older signed release is refused.
  * The user still sees a SmartScreen warning when the installer runs, exactly
    as they did for the first install, and Windows itself performs no check.
AFTER a certificate is in place, everything above still holds, and:
  * Windows verifies the installer's Authenticode signature at launch, so a
    file swapped on disk after this module verified it is caught by the OS as
    well as by the shell's re-hash.
  * SmartScreen stops warning once the certificate has reputation.
Neither of those replaces the other. The key protects the update decision;
Authenticode protects the execution.

The default, and why
--------------------
Checking is automatic. Downloading and installing are not, and there is no
setting that makes them automatic. Hearth is a tool that runs code on the
user's machine; silently replacing it while somebody is using it is exactly
the capability an attacker who compromises the release key would want, and
requiring a click bounds that blast radius by human attention rather than by
a timer. The check itself is one HTTPS GET of a small signed JSON document to
a pinned host, with no identifier of any kind attached, and being told about a
security fix is the entire value of an updater, so the check is on by default
and can be turned off. Downloading 117 MB unprompted onto a metered
connection is rude, and a staged installer sitting on disk is one more thing
for a local attacker to race, so neither happens without an explicit action.

Nothing has been published
--------------------------
There is no release server. The feed in release/trust.json points at
`releases.hearth.invalid`, which is an RFC 2606 reserved name that can never
resolve, so a shipped Hearth cannot fetch an update from anywhere at all
until an operator puts a real host in the trust file and rebuilds. The UI says
so in those words rather than pretending to be up to date. docs/updates.md has
the steps an operator would follow to actually publish; none of them have been
performed.

Standard library only. Every network call is bounded by a timeout.
"""

import argparse
import datetime
import errno
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hearth_ed25519  # noqa: E402
import hearth_paths  # noqa: E402

# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

#: The installed application root. agent/ sits directly under it in both a
#: checkout and the packaged payload (see desktop/shell/main.js's layout
#: note), so this resolves the same way in both.
ENV_APP_ROOT = "HEARTH_APP_ROOT"

#: The running application's version, handed down by the shell, which reads
#: it from the asar. Authoritative when present precisely because the asar is
#: integrity-checked and a file in the payload is not.
ENV_VERSION = "HEARTH_APP_VERSION"

#: Point the updater at a different feed. For the local test harness, and for
#: an operator staging a release before it is announced. NOT a trust boundary:
#: the signature is checked against the pinned key whatever the feed says, so
#: the worst an attacker who can set this achieves is denial of service, and
#: anybody who can set your environment already has your session.
ENV_FEED = "HEARTH_UPDATE_FEED"

ENV_CHANNEL = "HEARTH_UPDATE_CHANNEL"

TRUST_SUBPATH = os.path.join("release", "trust.json")
VERSION_SUBPATH = os.path.join("release", "version.json")

#: Everything this module writes lives under here, inside the user's data
#: directory. Never inside the install directory: the install directory is
#: what the installer replaces, and a staging area that the installer deletes
#: halfway through is its own kind of bug.
UPDATE_DIRNAME = "update"
STATE_NAME = "state.json"
STAGED_DIRNAME = "staged"
RECEIPT_NAME = "receipt.json"

MANIFEST_NAME = "manifest.json"

#: Reserved by RFC 2606 and guaranteed never to resolve. The shipped default,
#: because nothing has been published and a default that quietly pointed at a
#: real host would be a decision nobody made.
UNCONFIGURED_HOST_SUFFIX = ".invalid"

MANIFEST_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 300
CHUNK_BYTES = 1024 * 1024

#: A release manifest is a few hundred bytes. 64 KiB is four hundred times
#: that and still nothing, and it means a hostile feed cannot stream JSON at
#: the parser forever.
MAX_MANIFEST_BYTES = 64 * 1024

#: The installer is 117 MB today. 1 GiB is the ceiling a manifest may claim;
#: anything above it is refused before a byte is fetched, so a signed-but-
#: wrong size cannot be used to fill a disk.
MAX_ARTIFACT_BYTES = 1024 ** 3

#: Free space required beyond the artifact itself, because the installer has
#: to unpack roughly its own size again.
DISK_HEADROOM_BYTES = 400 * 1024 ** 2

#: Release notes are attacker-influenceable text (a release host cannot forge
#: them, but whoever holds the key writes them and the UI must survive either
#: way). Bounded here; neutralized at the point of rendering by
#: desktop/ui/js/dom.js.
MAX_NOTES_CHARS = 4000

#: An operator cannot mint a manifest that is valid forever: the expiry has
#: to be a real deadline or the freeze attack is back.
MAX_VALIDITY_DAYS = 400

#: How long a check result is considered fresh, so a page reload does not
#: re-open the network.
CHECK_INTERVAL_SECONDS = 6 * 3600

SNAPSHOT_WAIT_SECONDS = 25

_VERSION_RE = re.compile(r"^(\d{1,6})\.(\d{1,6})\.(\d{1,6})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.exe$")
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

STATE_UNCONFIGURED = "unconfigured"
STATE_IDLE = "idle"
STATE_CHECKING = "checking"
STATE_UP_TO_DATE = "up-to-date"
STATE_AVAILABLE = "available"
STATE_DOWNLOADING = "downloading"
STATE_READY = "ready"
STATE_FAILED = "failed"


class UpdateError(RuntimeError):
    """Anything this module refuses to paper over."""


class SignatureError(UpdateError):
    """The manifest is not signed by a key this application trusts.

    Its own class because it is the failure that means somebody is trying,
    rather than that something is broken. Nothing downstream of it runs.
    """


class DowngradeError(UpdateError):
    """A validly signed release that is older than what is installed, or older
    than a release this installation has already seen. The signature is real;
    the offer is a rollback."""


class ChecksumError(UpdateError):
    """The artifact's bytes are not the bytes the signed manifest describes."""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def app_root(env=None):
    """The installed application root: the parent of this agent directory."""
    env = os.environ if env is None else env
    override = env.get(ENV_APP_ROOT)
    if override:
        return override
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def trust_path(env=None):
    return os.path.join(app_root(env), TRUST_SUBPATH)


def update_dir(env=None):
    """Where state and staged installers live, under the user's data dir."""
    env = os.environ if env is None else env
    override = env.get("HEARTH_UPDATE_DIR")
    if override:
        return override
    prev = os.environ.get("HEARTH_DATA_DIR")
    if env is not os.environ and env.get("HEARTH_DATA_DIR"):
        # A caller passing its own environment expects that environment's
        # data directory, and hearth_paths reads os.environ. Swap, ask, and
        # put it back, rather than duplicating the platform logic here and
        # having the two drift.
        os.environ["HEARTH_DATA_DIR"] = env["HEARTH_DATA_DIR"]
        try:
            base = hearth_paths.data_dir()
        finally:
            if prev is None:
                os.environ.pop("HEARTH_DATA_DIR", None)
            else:
                os.environ["HEARTH_DATA_DIR"] = prev
    else:
        base = hearth_paths.data_dir()
    return os.path.join(base, UPDATE_DIRNAME)


def state_path(env=None):
    return os.path.join(update_dir(env), STATE_NAME)


def staging_dir(env=None):
    return os.path.join(update_dir(env), STAGED_DIRNAME)


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

def parse_version(text):
    """A version as a comparable tuple, or None when it is not one.

    Deliberately strict: exactly three dotted decimal components, nothing
    else. No prerelease tags, no build metadata, no "v" prefix. The comparison
    that downgrade protection rests on has to be a TOTAL ORDER with no
    surprises, and every subtlety semver has ("1.0.0-rc1" sorts below
    "1.0.0", but where does "1.0.0-rc10" go) is a subtlety an attacker gets to
    choose the answer to. A release train that needs prereleases can add them
    here, once, with tests, rather than inheriting them by accident.
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def current_version(env=None):
    """The version of the running application, or None when it is unknown.

    Three sources, most trustworthy first:

      1. HEARTH_APP_VERSION, set by the shell from app.getVersion(). That
         value comes out of the asar, whose contents Electron's embedded
         integrity validation makes tamper-evident.
      2. release/version.json in the payload, written at build time. A plain
         file in the install directory, so a local attacker who can write
         there can lower it -- which is exactly why the persisted floor below
         exists and why the shell checks the version again before spawning.
      3. desktop/shell/package.json, so a development checkout that was never
         packaged still reports the truth.

    None means "unknown", and unknown disables updating rather than defaulting
    to 0.0.0. A default of zero would make every release look like an upgrade,
    which is the wrong way for this to fail.
    """
    env = os.environ if env is None else env
    raw = (env.get(ENV_VERSION) or "").strip()
    if parse_version(raw):
        return raw
    for path, key in ((os.path.join(app_root(env), VERSION_SUBPATH), "version"),
                      (os.path.join(app_root(env), "desktop", "shell",
                                    "package.json"), "version")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        candidate = (data or {}).get(key)
        if parse_version(candidate):
            return candidate.strip()
    return None


# --------------------------------------------------------------------------
# Canonical serialization
# --------------------------------------------------------------------------

def canonical_bytes(value):
    """The bytes a signature is made over: one JSON object, one encoding.

    A signature is over bytes, and a manifest arrives as text that a parser
    has already had opinions about (whitespace, key order, duplicate keys,
    unicode escapes). Signing the raw response would be simplest but makes the
    manifest unreadable and unformattable; signing a re-serialization means
    producer and verifier must agree on exactly one encoding. This is that
    encoding: sorted keys, no whitespace, UTF-8, no NaN.

    Note what this gets for free. A duplicate key in the transmitted JSON
    cannot smuggle anything: Python keeps the last one, the canonical form is
    built from the parsed object, and the object that is verified is the same
    object that is then acted on. There is no second parse to disagree with
    the first.

    Floats are refused outright. A release manifest has no use for one, and
    the shortest-round-trip text form of a float is not something two
    languages can be relied on to agree about.
    """
    _reject_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _reject_floats(value, path="signed"):
    if isinstance(value, float):
        raise UpdateError("{} is a float; a release manifest may not contain "
                          "one".format(path))
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise UpdateError("{} has a non-string key".format(path))
            _reject_floats(item, "{}.{}".format(path, key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_floats(item, "{}[{}]".format(path, index))


# --------------------------------------------------------------------------
# The trust anchor
# --------------------------------------------------------------------------

def load_trust(path=None, env=None):
    """The pinned public keys and feed, read from release/trust.json.

    Validated on load rather than trusted. A trust file with a malformed key,
    an unknown algorithm or a non-https feed is a broken anchor, and failing
    here is better than failing after a download -- or worse, not failing.
    """
    path = path or trust_path(env)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise UpdateError("cannot read the update trust file at {}: {}".format(
            path, exc)) from exc
    except ValueError as exc:
        raise UpdateError("the update trust file at {} is not valid JSON: {}".format(
            path, exc)) from exc
    validate_trust(data)
    return data


def validate_trust(data):
    """Raise UpdateError unless `data` is a usable trust anchor."""
    if not isinstance(data, dict):
        raise UpdateError("the trust file must be a JSON object")
    if data.get("schema") != 1:
        raise UpdateError("unsupported trust file schema {!r}".format(data.get("schema")))
    if not isinstance(data.get("app_id"), str) or not data["app_id"]:
        raise UpdateError("the trust file names no app_id")
    feed = data.get("feed")
    if not isinstance(feed, str) or not feed:
        raise UpdateError("the trust file names no feed")
    parts = urllib.parse.urlsplit(feed)
    if parts.scheme != "https" or not parts.netloc:
        raise UpdateError("the trust file's feed must be an https URL, got {!r}".format(feed))
    if not feed.endswith("/"):
        raise UpdateError("the trust file's feed must end with '/', got {!r}".format(feed))
    channels = data.get("channels")
    if not isinstance(channels, list) or not channels:
        raise UpdateError("the trust file lists no channels")
    for name in channels:
        if not isinstance(name, str) or not _CHANNEL_RE.match(name):
            raise UpdateError("{!r} is not a usable channel name".format(name))
    if data.get("default_channel") not in channels:
        raise UpdateError("default_channel {!r} is not one of the listed channels".format(
            data.get("default_channel")))
    keys = data.get("keys")
    if not isinstance(keys, list) or not keys:
        raise UpdateError("the trust file carries no signing keys")
    seen = set()
    for entry in keys:
        if not isinstance(entry, dict):
            raise UpdateError("a key entry is not an object")
        key_id = entry.get("key_id")
        if not isinstance(key_id, str) or not _KEY_ID_RE.match(key_id):
            raise UpdateError("{!r} is not a usable key_id".format(key_id))
        if key_id in seen:
            raise UpdateError("key_id {!r} appears twice".format(key_id))
        seen.add(key_id)
        if entry.get("algorithm") != "ed25519":
            raise UpdateError("key {}: unsupported algorithm {!r}".format(
                key_id, entry.get("algorithm")))
        try:
            hearth_ed25519.from_hex(entry.get("public_key"), 32)
        except hearth_ed25519.SignatureError as exc:
            raise UpdateError("key {}: {}".format(key_id, exc)) from exc
        if entry.get("status") not in ("active", "revoked"):
            raise UpdateError("key {}: status must be 'active' or 'revoked', got "
                              "{!r}".format(key_id, entry.get("status")))
    if not any(e.get("status") == "active" for e in keys):
        raise UpdateError("the trust file has no active signing key")
    return True


def feed_base(trust, env=None):
    """The feed this run will use: the environment override, else the pin."""
    env = os.environ if env is None else env
    override = (env.get(ENV_FEED) or "").strip()
    base = override or trust["feed"]
    if not base.endswith("/"):
        base += "/"
    parts = urllib.parse.urlsplit(base)
    if parts.scheme == "http":
        # Loopback only, and only from an explicit override. This exists for
        # the local test harness. It is not a weakening: the signature is
        # checked identically whatever the transport, and an attacker who can
        # both set your environment and bind your loopback already owns the
        # session.
        host = parts.hostname or ""
        if not override or host not in ("127.0.0.1", "::1", "localhost"):
            raise UpdateError("refusing a plain-http update feed: {}".format(base))
    elif parts.scheme != "https":
        raise UpdateError("refusing an update feed that is not https: {}".format(base))
    return base


def configured(trust, env=None):
    """False when this build has no real release feed, which is the shipped
    state: the pinned host is an RFC 2606 .invalid name that cannot resolve.

    Reported honestly rather than presented as "up to date", because "we did
    not look" and "we looked and there is nothing" are different facts.
    """
    try:
        base = feed_base(trust, env)
    except UpdateError:
        return False
    host = urllib.parse.urlsplit(base).hostname or ""
    return not host.endswith(UNCONFIGURED_HOST_SUFFIX)


def channel_for(trust, env=None):
    env = os.environ if env is None else env
    wanted = (env.get(ENV_CHANNEL) or "").strip()
    if wanted and wanted in trust["channels"]:
        return wanted
    return trust["default_channel"]


# --------------------------------------------------------------------------
# Signature verification
# --------------------------------------------------------------------------

def verify_document(document, trust):
    """The `signed` block of a manifest, once a trusted key has vouched for it.

    Raises SignatureError and returns nothing useful on any failure, so there
    is no shape of this function's result that means "unverified but here it
    is anyway". Returns (signed, key_id).

    Order matters and is asserted by the self-test: NOTHING about the contents
    of `signed` is inspected here. Whether the version is newer, whether the
    artifact is plausible, whether the channel matches -- all of that happens
    in evaluate(), after this. A verifier that peeked at fields first would be
    making decisions on unauthenticated data.
    """
    if not isinstance(document, dict):
        raise SignatureError("the release manifest is not a JSON object")
    signed = document.get("signed")
    if not isinstance(signed, dict):
        raise SignatureError("the release manifest carries no signed block")
    signatures = document.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        raise SignatureError("the release manifest is not signed")

    payload = canonical_bytes(signed)
    keys = {e["key_id"]: e for e in trust["keys"]}
    problems = []
    for entry in signatures:
        if not isinstance(entry, dict):
            problems.append("a signature entry is not an object")
            continue
        key_id = entry.get("key_id")
        known = keys.get(key_id) if isinstance(key_id, str) else None
        if known is None:
            problems.append("signed by {!r}, which this build does not trust".format(key_id))
            continue
        if known.get("status") != "active":
            problems.append("key {} is {}".format(key_id, known.get("status")))
            continue
        if entry.get("algorithm") != known["algorithm"]:
            problems.append("key {}: signature claims algorithm {!r}".format(
                key_id, entry.get("algorithm")))
            continue
        try:
            raw = hearth_ed25519.from_hex(entry.get("signature"),
                                          hearth_ed25519.SIGNATURE_BYTES)
            public = hearth_ed25519.from_hex(known["public_key"], 32)
        except hearth_ed25519.SignatureError as exc:
            problems.append("key {}: {}".format(key_id, exc))
            continue
        if hearth_ed25519.verify(public, payload, raw):
            return signed, key_id
        problems.append("key {}: the signature does not match these bytes".format(key_id))

    raise SignatureError(
        "no trusted key signed this release manifest ({}). Nothing has been "
        "downloaded and nothing has been changed.".format("; ".join(problems)))


# --------------------------------------------------------------------------
# What a verified manifest is allowed to say
# --------------------------------------------------------------------------

def _parse_time(text, field):
    if not isinstance(text, str):
        raise UpdateError("{} is missing".format(field))
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError as exc:
        raise UpdateError("{} is not an ISO-8601 UTC timestamp like "
                          "2026-08-02T12:00:00Z: {!r}".format(field, text)) from exc


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def validate_signed(signed, trust, channel):
    """Structural checks on a manifest whose signature already verified.

    Separate from evaluate() because these are statements about SHAPE (is
    there an artifact, is its size a positive integer) rather than about this
    particular installation, and because the shape checks have to pass before
    the installation checks can be phrased at all.
    """
    if signed.get("schema") != 1:
        raise UpdateError("unsupported release manifest schema {!r}".format(
            signed.get("schema")))
    if signed.get("app_id") != trust["app_id"]:
        raise UpdateError(
            "this manifest is for {!r}, but this application is {!r}".format(
                signed.get("app_id"), trust["app_id"]))
    if signed.get("channel") != channel:
        # A signed manifest for another channel is a real document with a real
        # signature. Serving the beta manifest to stable users would be a
        # downgrade-shaped attack in the other direction, so the channel is
        # bound into what was signed and checked against what was asked for.
        raise UpdateError(
            "this manifest is for the {!r} channel, but this installation "
            "follows {!r}".format(signed.get("channel"), channel))
    version = parse_version(signed.get("version"))
    if version is None:
        raise UpdateError("the manifest's version {!r} is not MAJOR.MINOR.PATCH".format(
            signed.get("version")))
    minimum = signed.get("minimum_version", "0.0.0")
    if parse_version(minimum) is None:
        raise UpdateError("minimum_version {!r} is not MAJOR.MINOR.PATCH".format(minimum))
    released_at = _parse_time(signed.get("released_at"), "released_at")
    expires_at = _parse_time(signed.get("expires_at"), "expires_at")
    if expires_at <= released_at:
        raise UpdateError("the manifest expires before it was released")
    if expires_at - released_at > datetime.timedelta(days=MAX_VALIDITY_DAYS):
        raise UpdateError(
            "the manifest is valid for more than {} days; an expiry that far "
            "out is not an expiry".format(MAX_VALIDITY_DAYS))
    notes = signed.get("notes", "")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES_CHARS:
        raise UpdateError("release notes must be a string of at most {} "
                          "characters".format(MAX_NOTES_CHARS))
    artifact = signed.get("artifact")
    if not isinstance(artifact, dict):
        raise UpdateError("the manifest describes no artifact")
    name = artifact.get("name")
    if not isinstance(name, str) or not _ARTIFACT_NAME_RE.match(name):
        raise UpdateError("the artifact name {!r} is not a plain .exe filename".format(name))
    path = artifact.get("path")
    _check_relative_path(path)
    size = artifact.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise UpdateError("the artifact size must be a positive integer, got {!r}".format(size))
    if size > MAX_ARTIFACT_BYTES:
        raise UpdateError("the artifact claims {:,} bytes, above the {:,} byte "
                          "ceiling; refusing it".format(size, MAX_ARTIFACT_BYTES))
    digest = artifact.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
        raise UpdateError("the artifact's sha256 is not 64 hex characters")
    return True


def _check_relative_path(path):
    """Raise unless `path` is a safe relative feed path.

    Refused: absolute paths, schemes, drive letters, backslashes, parent
    traversal, empty segments, query strings and fragments. This runs on a
    SIGNED value, which is the point: the whole design says a signature is
    what makes bytes trustworthy, and it would be strange to then let a signed
    string decide what host to talk to or what file to open. Signing key
    compromise should cost an attacker a bad release, not arbitrary URL
    construction.
    """
    if not isinstance(path, str) or not path:
        raise UpdateError("the artifact names no path")
    if "://" in path or path.startswith("/") or path.startswith("\\"):
        raise UpdateError("the artifact path {!r} is not relative".format(path))
    if "\\" in path or re.match(r"^[A-Za-z]:", path):
        raise UpdateError("the artifact path {!r} is not a feed path".format(path))
    if "?" in path or "#" in path:
        raise UpdateError("the artifact path {!r} carries a query or fragment".format(path))
    segments = path.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise UpdateError("the artifact path {!r} has an empty or traversing "
                          "segment".format(path))
    for seg in segments:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", seg):
            raise UpdateError("the artifact path segment {!r} is not a plain "
                              "name".format(seg))
    return True


def evaluate(signed, trust, channel, installed, floor=None, now=None):
    """What this installation should do about a verified manifest.

    Returns {"action": "update"|"current", ...}. Raises DowngradeError when the
    manifest is a rollback and UpdateError when it is unusable.

    `installed` is the running version string; `floor` is the persisted
    high-water mark. Both are compared, and the floor is the one that survives
    an attacker who can edit the version file in the install directory: it
    lives in the user's data directory, it only ever moves up, and it is
    written after a manifest verifies rather than after an install succeeds,
    so a user who declines an update still cannot be walked backwards.
    """
    validate_signed(signed, trust, channel)
    now = now or _now()

    expires_at = _parse_time(signed["expires_at"], "expires_at")
    if now > expires_at:
        # The freeze attack: an attacker who can only WITHHOLD traffic replays
        # the newest manifest they have forever, and version comparison never
        # notices because that manifest really is the newest one it has seen.
        # An expiry turns silence into a visible failure.
        raise UpdateError(
            "this release manifest expired on {}. Either the release feed is "
            "stale or something is holding back newer ones; Hearth will not "
            "act on it.".format(signed["expires_at"]))

    current = parse_version(installed)
    if current is None:
        raise UpdateError(
            "Hearth cannot tell which version it is running, so it will not "
            "apply an update. Reinstalling from a fresh download fixes this.")
    offered = parse_version(signed["version"])

    floor_version = parse_version((floor or {}).get("version") or "") or current
    if offered < floor_version:
        raise DowngradeError(
            "the release feed is offering {}, which is older than {} -- a "
            "version this installation has already seen. That is a rollback, "
            "not an update, and it is refused.".format(
                signed["version"], _format(floor_version)))
    floor_released = (floor or {}).get("released_at")
    if floor_released:
        try:
            previous = _parse_time(floor_released, "floor released_at")
        except UpdateError:
            previous = None
        if previous is not None and _parse_time(
                signed["released_at"], "released_at") < previous:
            raise DowngradeError(
                "this manifest was released on {}, before the newest one this "
                "installation has already seen ({}). Refusing it.".format(
                    signed["released_at"], floor_released))

    if offered <= current:
        return {"action": "current", "version": signed["version"],
                "installed": installed}

    required = parse_version(signed.get("minimum_version", "0.0.0"))
    if current < required:
        raise UpdateError(
            "{} can only be installed over {} or newer, and this is {}. "
            "Download a fresh installer instead.".format(
                signed["version"], signed["minimum_version"], installed))

    artifact = signed["artifact"]
    return {
        "action": "update",
        "version": signed["version"],
        "installed": installed,
        "released_at": signed["released_at"],
        "notes": signed.get("notes", ""),
        "name": artifact["name"],
        "path": artifact["path"],
        "size_bytes": artifact["size_bytes"],
        "sha256": artifact["sha256"],
    }


def _format(version_tuple):
    return ".".join(str(part) for part in version_tuple)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects only within the feed's own origin.

    urllib follows redirects by default and would follow one to any host at
    all. The signature check means a redirect cannot get code accepted, but a
    redirect off-origin is still a request this application did not intend to
    make, to a host it was not told about, and refusing it costs nothing.
    """

    def __init__(self, allowed):
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if (parts.scheme, parts.netloc) != self.allowed:
            raise UpdateError(
                "refusing a redirect to {!r}; the update feed may only redirect "
                "within {}://{}".format(newurl.split("?")[0], *self.allowed))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(base):
    parts = urllib.parse.urlsplit(base)
    return urllib.request.build_opener(
        _PinnedRedirectHandler((parts.scheme, parts.netloc)),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def check_url(url, base):
    """Raise UpdateError unless `url` sits inside the feed.

    Enforced on the URL that is actually opened rather than on the one that
    was suggested, so a manifest that has been tampered with to point
    somewhere else cannot redirect a download. Belt and braces on top of the
    signature: this is what stops a bad URL before verification, and the
    signature is what stops bad bytes after it.
    """
    parts = urllib.parse.urlsplit(url)
    base_parts = urllib.parse.urlsplit(base)
    if parts.scheme != base_parts.scheme or parts.netloc != base_parts.netloc:
        raise UpdateError("refusing {!r}: not on the update feed {}://{}".format(
            url, base_parts.scheme, base_parts.netloc))
    if not parts.path.startswith(base_parts.path):
        raise UpdateError("refusing {!r}: outside the feed path {!r}".format(
            url, base_parts.path))
    if ".." in parts.path.split("/"):
        raise UpdateError("refusing a traversing feed path: {!r}".format(parts.path))
    return True


def manifest_url(base, channel):
    return "{}{}/{}".format(base, channel, MANIFEST_NAME)


def artifact_url(base, path):
    _check_relative_path(path)
    return base + path


def fetch_manifest(base, channel, timeout=MANIFEST_TIMEOUT, opener=None):
    """The raw manifest document, parsed but NOT verified. Bounded read.

    Returns the decoded JSON object. Everything that happens to it afterwards
    happens in verify_document, and nothing at all happens to it before.
    """
    url = manifest_url(base, channel)
    check_url(url, base)
    opener = opener or _opener(base)
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "hearth-updater",
        "Cache-Control": "no-cache",
    })
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_MANIFEST_BYTES + 1)
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise UpdateError("could not reach the update feed: {}".format(exc)) from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("the release manifest is larger than {:,} bytes; "
                          "refusing it".format(MAX_MANIFEST_BYTES))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("the release manifest is not valid JSON: {}".format(exc)) from exc


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path, expected_sha256, expected_size):
    """Raise ChecksumError unless `path` is exactly the signed artifact."""
    try:
        actual_size = os.path.getsize(path)
    except OSError as exc:
        raise ChecksumError("cannot read {} to verify it: {}".format(path, exc)) from exc
    if actual_size != expected_size:
        raise ChecksumError("{} is {:,} bytes but the signed manifest says {:,}; "
                            "refusing it".format(path, actual_size, expected_size))
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ChecksumError(
            "SHA-256 mismatch for {}\n  signed:   {}\n  on disk:  {}\n"
            "These are not the bytes the release manifest describes. Nothing has "
            "been installed and the copy of Hearth you are running is "
            "untouched.".format(os.path.basename(path), expected_sha256, actual))
    return actual


def download(url, dest, expected_sha256, expected_size, base, on_progress=None,
             timeout=DOWNLOAD_TIMEOUT, opener=None, cancelled=None):
    """Fetch the artifact to `dest`, verified before it lands there.

    Bytes go to `dest`.part and are hashed as they stream. The file is renamed
    into place only after the hash matches the value from the SIGNED manifest.
    A mismatch, a short read, an over-long response, a full disk or a
    cancellation all delete the partial: a file that failed its check is not
    something to resume from, and leaving one behind is how a later run ends
    up trusting it.

    The existing installation is untouched in every one of those paths,
    because nothing outside `dest` is written at all.
    """
    check_url(url, base)
    opener = opener or _opener(base)
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    digest = hashlib.sha256()
    done = 0
    limit = expected_size + 1  # one byte past the signed size is already too many

    try:
        with opener.open(url, timeout=timeout) as response, open(part, "wb") as fh:
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateError("the download was cancelled")
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise UpdateError(
                        "the response for {} is longer than the signed {:,} bytes; "
                        "refusing it".format(os.path.basename(dest), expected_size))
                fh.write(chunk)
                digest.update(chunk)
                if on_progress is not None:
                    on_progress(done, expected_size)
    except UpdateError:
        _unlink(part)
        raise
    except OSError as exc:
        _unlink(part)
        if exc.errno in (errno.ENOSPC, errno.EDQUOT) or "space" in str(exc).lower():
            raise UpdateError(
                "there is not enough free disk space to download the update. "
                "Nothing has been changed.") from exc
        raise UpdateError("downloading the update failed: {}".format(exc)) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        _unlink(part)
        raise UpdateError("downloading the update failed: {}".format(exc)) from exc

    actual = digest.hexdigest()
    if done != expected_size or actual != expected_sha256:
        _unlink(part)
        raise ChecksumError(
            "SHA-256 mismatch for {}\n  signed:   {} ({:,} bytes)\n"
            "  received: {} ({:,} bytes)\nThe download has been deleted, nothing "
            "has been installed, and the copy of Hearth you are running is "
            "untouched.".format(os.path.basename(dest), expected_sha256,
                                expected_size, actual, done))
    os.replace(part, dest)
    return actual


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Persisted state
# --------------------------------------------------------------------------

def read_state(env=None):
    try:
        with open(state_path(env), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(data, env=None):
    """Write the state file, atomically.

    A torn state file would be read back as {} by read_state, which silently
    resets the downgrade floor. That is the one piece of state here whose loss
    is a security regression rather than an inconvenience, so it is written to
    a temporary file in the same directory and renamed over the old one.
    """
    path = state_path(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def read_floor(env=None):
    state = read_state(env)
    floor = state.get("floor")
    return floor if isinstance(floor, dict) else {}


def raise_floor(version, released_at, env=None):
    """Move the downgrade floor up. Never down; that is the entire point.

    Called after a manifest VERIFIES, not after an update installs. A user who
    is offered 0.2.0 and declines it has still learned that 0.2.0 exists, and
    an attacker must not be able to walk them back to 0.1.0 by waiting for
    them to say no.
    """
    state = read_state(env)
    floor = state.get("floor") if isinstance(state.get("floor"), dict) else {}
    current = parse_version(floor.get("version") or "")
    offered = parse_version(version)
    if offered is None:
        return floor
    if current is not None and offered <= current:
        return floor
    floor = {"version": version, "released_at": released_at,
             "recorded_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ")}
    state["floor"] = floor
    write_state(state, env)
    return floor


# --------------------------------------------------------------------------
# Staging
# --------------------------------------------------------------------------

def staged_receipt(env=None):
    """The verified installer waiting to be run, or None.

    Re-verified on every read, not merely remembered. A receipt is a claim
    about a file, and the file sits on disk for as long as the user takes to
    click; anything else on the machine running as that user could have
    replaced it in the meantime. A receipt whose file no longer matches is
    deleted rather than reported.
    """
    root = staging_dir(env)
    if not os.path.isdir(root):
        return None
    best = None
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, RECEIPT_NAME)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        installer = os.path.join(root, name, data.get("name") or "")
        if not os.path.isfile(installer):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            continue
        try:
            verify_file(installer, data.get("sha256"), data.get("size_bytes"))
        except (ChecksumError, TypeError):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            continue
        version = parse_version(data.get("version") or "")
        if version is None:
            continue
        data = dict(data)
        data["path"] = installer
        if best is None or version > parse_version(best["version"]):
            best = data
    return best


def clear_staged(env=None):
    shutil.rmtree(staging_dir(env), ignore_errors=True)


def prune_staged(installed, env=None):
    """Delete staged installers at or below `installed`. Returns how many.

    An update that succeeds leaves its own 123 MB installer sitting in the
    staging directory, because the thing that would have cleaned it up is the
    process the installer just replaced. Two reasons that matters, and only
    one of them is disk space: a staged installer whose version is the version
    already running would otherwise be offered as "ready to install", and a
    button that the shell is guaranteed to refuse ("that is a downgrade, not
    an update") is worse than no button. Pruning on every status read is what
    makes the first launch after an update look like a normal launch.
    """
    root = staging_dir(env)
    current = parse_version(installed or "")
    if current is None or not os.path.isdir(root):
        return 0
    removed = 0
    for name in os.listdir(root):
        version = parse_version(name)
        if version is not None and version <= current:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            removed += 1
    return removed


def stage(plan, base, env=None, on_progress=None, opener=None, disk_fn=None,
          cancelled=None):
    """Download, verify and record one release. Returns the receipt.

    Order of operations, which is the security property:

        free space checked -> bytes streamed to a .part file and hashed as
        they arrive -> hash compared to the SIGNED manifest -> renamed into
        place -> verified AGAIN on disk -> receipt written.

    The second verification is not redundant with the first. The first is over
    a stream; the second is over the file that will actually be opened, and
    adjacency to the thing being trusted is what survives somebody reordering
    this function later.
    """
    root = os.path.join(staging_dir(env), plan["version"])
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    disk_fn = disk_fn or (lambda path: shutil.disk_usage(path).free)
    try:
        free = disk_fn(root)
    except OSError:
        free = None
    needed = plan["size_bytes"] + DISK_HEADROOM_BYTES
    if free is not None and free < needed:
        shutil.rmtree(root, ignore_errors=True)
        raise UpdateError(
            "the update needs about {:,} MB free and there is {:,} MB. Nothing "
            "has been downloaded.".format(needed // 1000000, free // 1000000))

    dest = os.path.join(root, plan["name"])
    try:
        download(artifact_url(base, plan["path"]), dest, plan["sha256"],
                 plan["size_bytes"], base, on_progress=on_progress,
                 opener=opener, cancelled=cancelled)
        verify_file(dest, plan["sha256"], plan["size_bytes"])
    except Exception:
        # A failed stage leaves nothing behind. There is no half-downloaded
        # installer for a later run to find and no directory for
        # staged_receipt to have an opinion about.
        shutil.rmtree(root, ignore_errors=True)
        raise

    receipt = {
        "version": plan["version"],
        "name": plan["name"],
        "sha256": plan["sha256"],
        "size_bytes": plan["size_bytes"],
        "released_at": plan.get("released_at"),
        "signed_by": plan.get("signed_by"),
        "staged_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(os.path.join(root, RECEIPT_NAME), "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
        fh.write("\n")
    out = dict(receipt)
    out["path"] = dest
    return out


# --------------------------------------------------------------------------
# The updater
# --------------------------------------------------------------------------

class Updater:
    """The check and the download, behind a versioned snapshot.

    One instance per process, the same shape as hearth_engine.Acquirer and
    the download queue: `check()` and `download()` return immediately and the
    work happens on a thread, and progress is published as a WHOLE snapshot
    rather than as a stream of deltas, because a client that reconnects wants
    the current state and not a narrative it has to replay.

    Every dependency a test needs to replace is an argument. The default
    self-test drives a complete check, a complete download, a tampered
    artifact, a wrong key, a rollback, an expiry and an interrupted transfer,
    with no network and no installer.
    """

    def __init__(self, trust=None, env=None, opener_fn=None, disk_fn=None,
                 version_fn=None, now_fn=None):
        self._env = env if env is not None else os.environ
        self._trust_override = trust
        self._opener_fn = opener_fn
        self._disk_fn = disk_fn
        self._version_fn = version_fn or (lambda: current_version(self._env))
        self._now_fn = now_fn or _now
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._thread = None
        self._cancel = threading.Event()
        self._version = 0
        self._plan = None
        self._snap = {
            "state": STATE_IDLE,
            "message": "",
            "error": None,
            "available": None,
            "bytes_done": 0,
            "bytes_total": 0,
            "last_check_at": None,
            "signed_by": None,
            "checked": False,
        }

    # -- snapshot ----------------------------------------------------------

    def _set(self, **fields):
        with self._cond:
            self._snap.update(fields)
            self._version += 1
            self._cond.notify_all()

    def _trust(self):
        if self._trust_override is not None:
            return self._trust_override
        return load_trust(env=self._env)

    def _public(self):
        data = dict(self._snap)
        data["version"] = self._version
        data["current_version"] = self._version_fn()
        data["running"] = bool(self._thread and self._thread.is_alive())
        try:
            trust = self._trust()
            data["channel"] = channel_for(trust, self._env)
            data["configured"] = configured(trust, self._env)
            data["feed"] = feed_base(trust, self._env) if data["configured"] else None
            data["trust_error"] = None
        except UpdateError as exc:
            data["channel"] = None
            data["configured"] = False
            data["feed"] = None
            data["trust_error"] = str(exc)
        state = read_state(self._env)
        data["auto_check"] = state.get("auto_check", True) is not False
        data["floor"] = read_floor(self._env) or None
        try:
            # An applied update leaves its own installer behind: the process
            # that would have tidied up is the one the installer replaced. The
            # first status read after a restart is where that gets noticed.
            prune_staged(data["current_version"], self._env)
            data["staged"] = staged_receipt(self._env)
        except OSError:
            data["staged"] = None
        if data["staged"] and data["state"] in (STATE_IDLE, STATE_UP_TO_DATE):
            data["state"] = STATE_READY
        elif data["staged"] is None and data["state"] == STATE_READY:
            # The staged installer was here a moment ago and is not here now.
            # staged_receipt() re-verifies on every read and deletes a file
            # that no longer matches its receipt, so the usual reason is that
            # something on this machine rewrote it. Saying "ready to install"
            # over a button that cannot work would be the wrong end of an
            # honest failure; say what happened and offer the check again.
            data["state"] = STATE_FAILED
            data["error"] = data["error"] or (
                "The downloaded installer is no longer the one Hearth verified, "
                "so it has been deleted and nothing has been installed. Check "
                "for updates again.")
            data["message"] = data["error"]
        if not data["configured"] and data["state"] in (STATE_IDLE, STATE_UP_TO_DATE):
            data["state"] = STATE_UNCONFIGURED
            data["message"] = data["message"] or (
                "This build of Hearth carries no release feed, so it cannot "
                "check for updates. Nothing has been published yet.")
        return data

    def snapshot(self):
        with self._cond:
            return self._public()

    def snapshot_after(self, version, timeout=None):
        deadline = time.monotonic() + (SNAPSHOT_WAIT_SECONDS if timeout is None
                                       else timeout)
        with self._cond:
            while self._version <= version:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._public()

    # -- settings ----------------------------------------------------------

    def set_auto_check(self, enabled):
        state = read_state(self._env)
        state["auto_check"] = bool(enabled)
        write_state(state, self._env)
        self._set()
        return self.snapshot()

    # -- driving -----------------------------------------------------------

    def _busy(self):
        return self._thread is not None and self._thread.is_alive()

    def _spawn(self, target, name, **kwargs):
        with self._cond:
            if self._busy():
                return self._public()
            self._cancel.clear()
            self._thread = threading.Thread(target=target, kwargs=kwargs,
                                            name=name, daemon=True)
            self._thread.start()
            return self._public()

    def check(self, force=False):
        """Ask the feed what the newest release is. Returns at once."""
        return self._spawn(self._guarded, "hearth-update-check",
                           fn=self.check_once, force=force)

    def download(self):
        """Fetch and verify the release the last check found. Returns at once."""
        return self._spawn(self._guarded, "hearth-update-download",
                           fn=self.download_once)

    def cancel(self):
        self._cancel.set()
        return self.snapshot()

    def join(self, timeout=None):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.snapshot()

    def _guarded(self, fn, **kwargs):
        try:
            fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a background thread that
            # raised would leave the UI showing "checking" forever. Every
            # failure has to become a state somebody can read.
            self._fail("the update check failed unexpectedly: {}".format(exc))

    def _fail(self, message, state=STATE_FAILED):
        self._set(state=state, error=message, message=message)
        return self.snapshot()

    # -- the work ----------------------------------------------------------

    def check_once(self, force=False):
        """One check, synchronously. Never raises: every failure is a state."""
        try:
            trust = self._trust()
        except UpdateError as exc:
            return self._fail("Hearth cannot read its own update trust file, so "
                              "it will not check for updates: {}".format(exc))
        if not configured(trust, self._env):
            self._set(state=STATE_UNCONFIGURED, error=None, available=None,
                      message="This build of Hearth carries no release feed, so "
                              "it cannot check for updates. Nothing has been "
                              "published yet.")
            return self.snapshot()

        installed = self._version_fn()
        if parse_version(installed or "") is None:
            return self._fail(
                "Hearth cannot tell which version it is running, so it will not "
                "check for or apply updates.")

        state = read_state(self._env)
        last = state.get("last_check_at")
        if not force and last and self._snap.get("checked"):
            try:
                age = (self._now_fn() - _parse_time(last, "last_check_at")).total_seconds()
            except UpdateError:
                age = None
            if age is not None and 0 <= age < CHECK_INTERVAL_SECONDS:
                return self.snapshot()

        channel = channel_for(trust, self._env)
        base = feed_base(trust, self._env)
        self._set(state=STATE_CHECKING, error=None,
                  message="Checking for updates", bytes_done=0, bytes_total=0)

        opener = self._opener_fn(base) if self._opener_fn else None
        try:
            document = fetch_manifest(base, channel, opener=opener)
        except UpdateError as exc:
            # An unreachable feed is not an update problem; say so plainly
            # rather than implying the installation is up to date.
            return self._fail(
                "Hearth could not check for updates: {} You are still running "
                "{}.".format(exc, installed))

        try:
            signed, key_id = verify_document(document, trust)
        except SignatureError as exc:
            return self._fail(
                "Refusing this update: {} ".format(exc) +
                "Hearth is unchanged.")
        except UpdateError as exc:
            return self._fail("Refusing this update: {}".format(exc))

        floor = read_floor(self._env)
        try:
            plan = evaluate(signed, trust, channel, installed, floor=floor,
                            now=self._now_fn())
        except DowngradeError as exc:
            return self._fail("Refusing this update: {}".format(exc))
        except UpdateError as exc:
            return self._fail("Refusing this update: {}".format(exc))

        # Only now, with a verified and accepted manifest, is anything
        # persisted. A manifest that failed any check above moves nothing.
        state = read_state(self._env)
        state["last_check_at"] = self._now_fn().strftime("%Y-%m-%dT%H:%M:%SZ")
        write_state(state, self._env)
        raise_floor(signed["version"], signed["released_at"], self._env)

        if plan["action"] == "current":
            with self._cond:
                self._plan = None
            self._set(state=STATE_UP_TO_DATE, error=None, available=None,
                      checked=True, signed_by=key_id,
                      last_check_at=state["last_check_at"],
                      message="Hearth {} is the newest release.".format(installed))
            return self.snapshot()

        plan["signed_by"] = key_id
        with self._cond:
            self._plan = plan
        self._set(state=STATE_AVAILABLE, error=None, checked=True,
                  signed_by=key_id, last_check_at=state["last_check_at"],
                  available={"version": plan["version"],
                             "released_at": plan["released_at"],
                             "notes": plan["notes"],
                             "size_bytes": plan["size_bytes"],
                             "name": plan["name"]},
                  message="Hearth {} is available. You are running {}.".format(
                      plan["version"], installed))
        return self.snapshot()

    def download_once(self):
        """Fetch and verify the available release. Never raises."""
        with self._cond:
            plan = self._plan
        if not plan:
            return self._fail("There is no verified update to download. Check "
                              "for updates first.")
        try:
            trust = self._trust()
            base = feed_base(trust, self._env)
        except UpdateError as exc:
            return self._fail(str(exc))

        self._set(state=STATE_DOWNLOADING, error=None, bytes_done=0,
                  bytes_total=plan["size_bytes"],
                  message="Downloading Hearth {}".format(plan["version"]))

        last = [0.0]

        def progress(done, total):
            now = time.monotonic()
            if now - last[0] < 0.2 and done < total:
                return
            last[0] = now
            self._set(bytes_done=done, bytes_total=total)

        opener = self._opener_fn(base) if self._opener_fn else None
        try:
            receipt = stage(plan, base, env=self._env, on_progress=progress,
                            opener=opener, disk_fn=self._disk_fn,
                            cancelled=self._cancel.is_set)
        except ChecksumError as exc:
            return self._fail("Refusing this update: {}".format(exc))
        except UpdateError as exc:
            return self._fail(str(exc))

        self._set(state=STATE_READY, error=None,
                  bytes_done=plan["size_bytes"], bytes_total=plan["size_bytes"],
                  message="Hearth {} is verified and ready to install.".format(
                      receipt["version"]))
        return self.snapshot()

    def dismiss(self):
        """Throw away a staged installer and go back to idle.

        The floor is NOT lowered: declining an update is not forgetting that it
        exists, and forgetting would reopen the rollback the floor closes.
        """
        clear_staged(self._env)
        with self._cond:
            self._plan = None
        self._set(state=STATE_IDLE, error=None, available=None,
                  bytes_done=0, bytes_total=0, message="")
        return self.snapshot()


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

#: Source tokens that would mean this module can run what it downloads. Rule 3
#: in the module docstring is only worth writing down if something enforces it.
_EXECUTION_TOKENS = ("import subprocess", "os.system", "os.popen", "os.exec",
                     "os.spawn", "import ctypes", "runpy", "eval(", "exec(")


class _FakeResponse:
    def __init__(self, data, chunks=None):
        self._buf = io.BytesIO(data)
        self._chunks = chunks

    def read(self, n=-1):
        if self._chunks is not None:
            n = min(n if n and n > 0 else self._chunks, self._chunks)
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._buf.close()
        return False


class _FakeFeed:
    """A feed in a dictionary. url -> bytes, or a callable raising."""

    def __init__(self, files):
        self.files = files
        self.opened = []

    def open(self, target, timeout=None):
        url = target if isinstance(target, str) else target.full_url
        self.opened.append(url)
        body = self.files.get(url)
        if body is None:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        if callable(body):
            return body()
        return _FakeResponse(body)


def _self_test():
    tmp = tempfile.mkdtemp(prefix="hearth-update-test-")
    try:
        # -- nothing here can execute what it downloads --------------------
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
            source = fh.read()
        body = source.split("_EXECUTION_TOKENS = (", 1)[0]
        for token in _EXECUTION_TOKENS:
            assert token not in body, (
                "hearth_update must never be able to run what it downloads, but "
                "its source contains {!r}".format(token))
        assert "subprocess" not in globals(), "subprocess must not be imported here"

        # -- the committed trust file is a usable anchor -------------------
        real_trust = load_trust()
        assert real_trust["app_id"] == "com.hearthlocal.hearth", real_trust["app_id"]
        assert any(k["status"] == "active" for k in real_trust["keys"])
        # Shipped, nothing is published: the pinned feed must be a name that
        # cannot resolve, so a build that was never configured cannot talk to
        # anything at all.
        assert not configured(real_trust, {}), (
            "the committed trust file points at a real feed; nothing has been "
            "published and the shipped default must not pretend otherwise")

        # -- a broken trust file is refused --------------------------------
        good_key = {"key_id": "k1", "algorithm": "ed25519",
                    "public_key": "a" * 64, "status": "active"}
        base_trust = {"schema": 1, "app_id": "x", "feed": "https://h/u/",
                      "channels": ["stable"], "default_channel": "stable",
                      "keys": [dict(good_key)]}
        assert validate_trust(json.loads(json.dumps(base_trust)))
        for mutate in (
            lambda t: t.update(schema=2),
            lambda t: t.update(app_id=""),
            lambda t: t.update(feed="http://h/u/"),
            lambda t: t.update(feed="https://h/u"),          # no trailing slash
            lambda t: t.update(channels=[]),
            lambda t: t.update(default_channel="beta"),
            lambda t: t.update(keys=[]),
            lambda t: t["keys"][0].update(algorithm="rsa"),
            lambda t: t["keys"][0].update(public_key="nothex"),
            lambda t: t["keys"][0].update(public_key="ab"),
            lambda t: t["keys"][0].update(status="maybe"),
            lambda t: t.update(keys=[dict(good_key), dict(good_key)]),
            lambda t: t.update(keys=[dict(good_key, status="revoked")]),
        ):
            broken = json.loads(json.dumps(base_trust))
            mutate(broken)
            try:
                validate_trust(broken)
                raise AssertionError("a broken trust file must be refused: {}".format(broken))
            except UpdateError:
                pass

        # -- versions ------------------------------------------------------
        assert parse_version("0.1.0") == (0, 1, 0)
        assert parse_version("10.2.30") == (10, 2, 30)
        assert parse_version(" 1.2.3 ") == (1, 2, 3)
        for bad in ("1.2", "1.2.3.4", "v1.2.3", "1.2.3-rc1", "1.2.x", "", None, 1.2):
            assert parse_version(bad) is None, bad
        assert parse_version("0.2.0") > parse_version("0.1.9")
        assert parse_version("0.10.0") > parse_version("0.9.0"), "must not compare as strings"

        # -- canonical bytes -----------------------------------------------
        assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
        # Non-ASCII is written as UTF-8, not as a \u escape, so producer and
        # verifier cannot disagree about which escaping to use.
        assert canonical_bytes({"a": "\u00e9"}) == b'{"a":"\xc3\xa9"}'
        # Key order in the source text cannot change the signed bytes, which
        # is what lets a manifest be pretty-printed and still verify.
        one = json.loads('{"z":1,"a":{"y":2,"b":3}}')
        two = json.loads('{"a":{"b":3,"y":2},"z":1}')
        assert canonical_bytes(one) == canonical_bytes(two)
        # A duplicate key cannot smuggle a second value past the signature:
        # the object that is verified IS the object that is used.
        dup = json.loads('{"version":"9.9.9","version":"0.0.1"}')
        assert dup == {"version": "0.0.1"}
        assert canonical_bytes(dup) == b'{"version":"0.0.1"}'
        try:
            canonical_bytes({"a": 1.5})
            raise AssertionError("a float must be refused")
        except UpdateError:
            pass

        # -- a signed manifest, and everything that must not verify --------
        seed = hashlib.sha256(b"hearth-update-test-key").digest()
        public = hearth_ed25519.public_key(seed)
        other_seed = hashlib.sha256(b"an attacker's key").digest()
        other_public = hearth_ed25519.public_key(other_seed)
        assert other_public != public

        trust = {"schema": 1, "app_id": "com.example.app",
                 "feed": "https://updates.example.com/u/",
                 "channels": ["stable", "beta"], "default_channel": "stable",
                 "keys": [{"key_id": "release-1", "algorithm": "ed25519",
                           "public_key": hearth_ed25519.to_hex(public),
                           "status": "active"},
                          {"key_id": "retired-0", "algorithm": "ed25519",
                           "public_key": hearth_ed25519.to_hex(other_public),
                           "status": "revoked"}]}
        assert validate_trust(trust)

        installer = b"MZ" + b"this is not really an installer" * 500
        artifact_sha = hashlib.sha256(installer).hexdigest()

        def make_signed(**overrides):
            signed = {
                "schema": 1,
                "app_id": "com.example.app",
                "channel": "stable",
                "version": "0.2.0",
                "released_at": "2026-08-01T00:00:00Z",
                "expires_at": "2026-09-01T00:00:00Z",
                "minimum_version": "0.1.0",
                "notes": "Fixes a thing.",
                "artifact": {"name": "Hearth-Setup-0.2.0.exe",
                             "path": "stable/0.2.0/Hearth-Setup-0.2.0.exe",
                             "size_bytes": len(installer),
                             "sha256": artifact_sha},
            }
            signed.update(overrides)
            return signed

        def sign_doc(signed, key_seed=seed, key_id="release-1", algorithm="ed25519"):
            signature = hearth_ed25519.sign(key_seed, canonical_bytes(signed))
            return {"signed": signed,
                    "signatures": [{"key_id": key_id, "algorithm": algorithm,
                                    "signature": hearth_ed25519.to_hex(signature)}]}

        good = sign_doc(make_signed())
        signed, key_id = verify_document(good, trust)
        assert key_id == "release-1"
        assert signed["version"] == "0.2.0"

        # MUTATION: one character of the signed block changed. The signature is
        # over the canonical form of the whole block, so ANY edit breaks it.
        for mutate in (
            lambda d: d["signed"].update(version="9.9.9"),
            lambda d: d["signed"].update(channel="beta"),
            lambda d: d["signed"]["artifact"].update(sha256="0" * 64),
            lambda d: d["signed"]["artifact"].update(size_bytes=1),
            lambda d: d["signed"].update(notes="Fixes a thing!"),
            lambda d: d["signed"].update(extra_field="added"),
            lambda d: d["signed"].pop("minimum_version"),
        ):
            tampered = json.loads(json.dumps(good))
            mutate(tampered)
            try:
                verify_document(tampered, trust)
                raise AssertionError("a tampered manifest must not verify: {}".format(
                    tampered["signed"]))
            except SignatureError:
                pass

        # MUTATION: signed with a key this build does not trust at all.
        stranger_seed = hashlib.sha256(b"nobody's key").digest()
        forged = sign_doc(make_signed(), key_seed=stranger_seed, key_id="release-1")
        try:
            verify_document(forged, trust)
            raise AssertionError("a manifest signed by the wrong key must not verify")
        except SignatureError as exc:
            assert "does not match" in str(exc), str(exc)

        # ... and with a key that is IN the trust file but revoked.
        revoked = sign_doc(make_signed(), key_seed=other_seed, key_id="retired-0")
        try:
            verify_document(revoked, trust)
            raise AssertionError("a revoked key must not verify")
        except SignatureError as exc:
            assert "revoked" in str(exc), str(exc)

        # ... and with a key id nobody has heard of.
        unknown = sign_doc(make_signed(), key_id="release-99")
        try:
            verify_document(unknown, trust)
            raise AssertionError("an unknown key id must not verify")
        except SignatureError:
            pass

        # ... and with no signature at all, which is the shape a naive
        # "verify if present" implementation lets through.
        for shape in ({"signed": make_signed()},
                      {"signed": make_signed(), "signatures": []},
                      {"signed": make_signed(), "signatures": "yes"},
                      {"signatures": good["signatures"]},
                      "not an object"):
            try:
                verify_document(shape, trust)
                raise AssertionError("must refuse {!r}".format(shape))
            except SignatureError:
                pass

        # A signature entry that names ed25519 but carries a different
        # algorithm string, and one whose hex is not hex.
        for mutate in (lambda d: d["signatures"][0].update(algorithm="ed448"),
                       lambda d: d["signatures"][0].update(signature="zz"),
                       lambda d: d["signatures"][0].update(signature="ab" * 32)):
            broken = json.loads(json.dumps(good))
            mutate(broken)
            try:
                verify_document(broken, trust)
                raise AssertionError("must refuse a broken signature entry")
            except SignatureError:
                pass

        # -- what a verified manifest is allowed to say --------------------
        assert validate_signed(make_signed(), trust, "stable")
        for overrides, fragment in (
            ({"schema": 2}, "schema"),
            ({"app_id": "com.other.app"}, "this application"),
            ({"channel": "beta"}, "channel"),
            ({"version": "1.0"}, "MAJOR.MINOR.PATCH"),
            ({"minimum_version": "x"}, "minimum_version"),
            ({"released_at": "yesterday"}, "released_at"),
            ({"expires_at": "2026-07-01T00:00:00Z"}, "expires before"),
            ({"expires_at": "2030-09-01T00:00:00Z"}, "valid for more than"),
            ({"notes": "x" * (MAX_NOTES_CHARS + 1)}, "release notes"),
            ({"artifact": {}}, "artifact name"),
        ):
            try:
                validate_signed(make_signed(**overrides), trust, "stable")
                raise AssertionError("must refuse {}".format(overrides))
            except UpdateError as exc:
                assert fragment in str(exc), (overrides, str(exc))

        # A signed artifact path is still not allowed to be a URL, escape the
        # feed, or name a Windows path. Key compromise buys a bad release, not
        # arbitrary URL construction.
        for bad_path in ("https://evil.example/x.exe", "/etc/passwd", "../../x.exe",
                         "stable/../../x.exe", "C:/x.exe", "stable\\x.exe",
                         "stable//x.exe", "stable/x.exe?a=b", ""):
            try:
                validate_signed(make_signed(artifact={
                    "name": "Hearth-Setup-0.2.0.exe", "path": bad_path,
                    "size_bytes": 10, "sha256": "a" * 64}), trust, "stable")
                raise AssertionError("must refuse artifact path {!r}".format(bad_path))
            except UpdateError:
                pass
        for bad_name in ("../x.exe", "x.dll", "x", "a/b.exe", "x.exe.txt"):
            try:
                validate_signed(make_signed(artifact={
                    "name": bad_name, "path": "stable/x.exe",
                    "size_bytes": 10, "sha256": "a" * 64}), trust, "stable")
                raise AssertionError("must refuse artifact name {!r}".format(bad_name))
            except UpdateError:
                pass
        # A size above the ceiling is refused before anything is fetched.
        try:
            validate_signed(make_signed(artifact={
                "name": "x.exe", "path": "stable/x.exe",
                "size_bytes": MAX_ARTIFACT_BYTES + 1, "sha256": "a" * 64}),
                trust, "stable")
            raise AssertionError("an oversized artifact must be refused")
        except UpdateError:
            pass

        # -- downgrade protection ------------------------------------------
        now = datetime.datetime(2026, 8, 2, tzinfo=datetime.timezone.utc)
        plan = evaluate(make_signed(), trust, "stable", "0.1.0", now=now)
        assert plan["action"] == "update" and plan["version"] == "0.2.0", plan

        # The same version is not an update.
        assert evaluate(make_signed(version="0.1.0", minimum_version="0.0.0"),
                        trust, "stable", "0.1.0", now=now)["action"] == "current"

        # A validly signed OLDER release is a rollback and is refused. This is
        # the attack signature checking alone does not stop: the signature on
        # this manifest is genuine.
        older = make_signed(version="0.0.9", minimum_version="0.0.0")
        assert verify_document(sign_doc(older), trust)[1] == "release-1"
        try:
            evaluate(older, trust, "stable", "0.1.0", now=now)
            raise AssertionError("a rollback below the installed version must be refused")
        except DowngradeError:
            pass

        # ... and refused even when the INSTALLED version was lowered, because
        # the floor lives in the user's data directory and only moves up.
        try:
            evaluate(older, trust, "stable", "0.0.1",
                     floor={"version": "0.2.0",
                            "released_at": "2026-08-01T00:00:00Z"}, now=now)
            raise AssertionError("a rollback below the floor must be refused")
        except DowngradeError as exc:
            assert "rollback" in str(exc), str(exc)

        # A newer version number carrying an OLDER release date is refused too:
        # replaying an old manifest with the version field bumped is not
        # possible (the signature covers it) but an attacker who holds two real
        # manifests must not be able to present the older one as the newer.
        try:
            evaluate(make_signed(version="0.3.0",
                                 released_at="2026-07-01T00:00:00Z",
                                 expires_at="2026-07-20T00:00:00Z"),
                     trust, "stable", "0.1.0",
                     floor={"version": "0.1.0",
                            "released_at": "2026-08-01T00:00:00Z"}, now=now)
            raise AssertionError("a manifest older than the floor's release date "
                                 "must be refused")
        except (DowngradeError, UpdateError):
            pass

        # An expired manifest is refused: the freeze attack.
        try:
            evaluate(make_signed(), trust, "stable", "0.1.0",
                     now=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc))
            raise AssertionError("an expired manifest must be refused")
        except UpdateError as exc:
            assert "expired" in str(exc), str(exc)

        # minimum_version: an update that cannot be installed over what is here.
        try:
            evaluate(make_signed(minimum_version="0.1.5"), trust, "stable", "0.1.0",
                     now=now)
            raise AssertionError("an update below its own minimum must be refused")
        except UpdateError as exc:
            assert "0.1.5" in str(exc), str(exc)

        # An unknown installed version disables updating rather than defaulting
        # to zero, which would make every release look like an upgrade.
        try:
            evaluate(make_signed(), trust, "stable", None, now=now)
            raise AssertionError("an unknown installed version must refuse")
        except UpdateError as exc:
            assert "which version" in str(exc), str(exc)

        # -- feed URLs -----------------------------------------------------
        base = "https://updates.example.com/u/"
        assert check_url(base + "stable/manifest.json", base)
        for bad in ("http://updates.example.com/u/stable/manifest.json",
                    "https://evil.example/u/stable/manifest.json",
                    "https://updates.example.com/elsewhere/manifest.json",
                    "https://updates.example.com/u/../secret"):
            try:
                check_url(bad, base)
                raise AssertionError("must refuse {}".format(bad))
            except UpdateError:
                pass
        handler = _PinnedRedirectHandler(("https", "updates.example.com"))
        for bad in ("https://evil.example/x", "http://updates.example.com/x"):
            try:
                handler.redirect_request(None, None, 302, "Found", {}, bad)
                raise AssertionError("must refuse a redirect to {}".format(bad))
            except UpdateError:
                pass

        # feed_base: an http override is loopback-only, and a pinned http feed
        # is refused outright.
        assert feed_base(trust, {}) == base
        assert feed_base(trust, {ENV_FEED: "http://127.0.0.1:9/u/"}) == "http://127.0.0.1:9/u/"
        assert feed_base(trust, {ENV_FEED: "https://other.example/u"}).endswith("/")
        for bad in ("http://evil.example/u/", "ftp://x/u/"):
            try:
                feed_base(trust, {ENV_FEED: bad})
                raise AssertionError("must refuse feed {}".format(bad))
            except UpdateError:
                pass

        # -- end to end, against a fake feed --------------------------------
        home = os.path.join(tmp, "data")
        env = {"HEARTH_DATA_DIR": home, ENV_VERSION: "0.1.0"}
        manifest_bytes = json.dumps(good, indent=2).encode("utf-8")
        art_url = base + "stable/0.2.0/Hearth-Setup-0.2.0.exe"
        feed = _FakeFeed({base + "stable/manifest.json": manifest_bytes,
                          art_url: installer})

        updater = Updater(trust=trust, env=env, opener_fn=lambda _b: feed,
                          disk_fn=lambda _p: 10 ** 12,
                          now_fn=lambda: now)
        snap = updater.snapshot()
        assert snap["state"] == STATE_IDLE, snap
        assert snap["current_version"] == "0.1.0"
        assert snap["configured"] is True

        snap = updater.check_once()
        assert snap["state"] == STATE_AVAILABLE, snap
        assert snap["available"]["version"] == "0.2.0", snap
        assert snap["signed_by"] == "release-1"
        assert read_floor(env)["version"] == "0.2.0", read_floor(env)

        snap = updater.download_once()
        assert snap["state"] == STATE_READY, snap
        receipt = staged_receipt(env)
        assert receipt and receipt["version"] == "0.2.0", receipt
        assert os.path.isfile(receipt["path"]), receipt
        with open(receipt["path"], "rb") as fh:
            assert fh.read() == installer
        assert receipt["sha256"] == artifact_sha

        # -- MUTATION: one byte of the artifact flipped ---------------------
        # The manifest is untouched and its signature is perfect. The bytes are
        # not the signed bytes, so nothing is staged and the previous staged
        # install is gone rather than silently kept.
        clear_staged(env)
        corrupted = bytearray(installer)
        corrupted[17] ^= 0x01
        bad_feed = _FakeFeed({base + "stable/manifest.json": manifest_bytes,
                              art_url: bytes(corrupted)})
        bad_updater = Updater(trust=trust, env=env, opener_fn=lambda _b: bad_feed,
                              disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
        bad_updater.check_once()
        snap = bad_updater.download_once()
        assert snap["state"] == STATE_FAILED, snap
        assert "SHA-256 mismatch" in snap["error"], snap["error"]
        assert "untouched" in snap["error"], snap["error"]
        assert staged_receipt(env) is None, "a failed download must stage nothing"
        assert not os.path.exists(os.path.join(staging_dir(env), "0.2.0")), (
            "a failed download must leave no directory behind")

        # A truncated artifact fails on length, and an over-long response is
        # cut off rather than written out.
        for payload in (installer[:-1], installer + b"extra"):
            feed_x = _FakeFeed({base + "stable/manifest.json": manifest_bytes,
                                art_url: payload})
            u = Updater(trust=trust, env=env, opener_fn=lambda _b, f=feed_x: f,
                        disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
            u.check_once()
            assert u.download_once()["state"] == STATE_FAILED
            assert staged_receipt(env) is None

        # -- MUTATION: the whole manifest re-signed with the wrong key ------
        forged_bytes = json.dumps(sign_doc(make_signed(), key_seed=stranger_seed),
                                  indent=2).encode("utf-8")
        forged_feed = _FakeFeed({base + "stable/manifest.json": forged_bytes,
                                 art_url: installer})
        u = Updater(trust=trust, env=env, opener_fn=lambda _b: forged_feed,
                    disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
        snap = u.check_once()
        assert snap["state"] == STATE_FAILED, snap
        assert "no trusted key" in snap["error"], snap["error"]
        assert snap["available"] is None
        # The artifact was never requested. Verification precedes the fetch.
        assert art_url not in forged_feed.opened, forged_feed.opened

        # -- MUTATION: a validly signed older release ----------------------
        old_bytes = json.dumps(sign_doc(make_signed(
            version="0.1.9", minimum_version="0.0.0",
            artifact={"name": "Hearth-Setup-0.1.9.exe",
                      "path": "stable/0.1.9/Hearth-Setup-0.1.9.exe",
                      "size_bytes": len(installer), "sha256": artifact_sha},
        ))).encode("utf-8")
        old_feed = _FakeFeed({base + "stable/manifest.json": old_bytes})
        u = Updater(trust=trust, env=env, opener_fn=lambda _b: old_feed,
                    disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
        snap = u.check_once()
        assert snap["state"] == STATE_FAILED, snap
        assert "rollback" in snap["error"], snap["error"]
        # The floor did not move down.
        assert read_floor(env)["version"] == "0.2.0", read_floor(env)

        # -- an interrupted transfer leaves a working install ---------------
        class _Interrupted(_FakeResponse):
            def read(self, n=-1):
                data = super().read(n)
                if not data:
                    raise TimeoutError("the connection dropped")
                return data[: len(data) // 2] or data

        half = {base + "stable/manifest.json": manifest_bytes,
                art_url: lambda: _Interrupted(installer[: len(installer) // 3])}
        cut_feed = _FakeFeed(half)
        u = Updater(trust=trust, env=env, opener_fn=lambda _b: cut_feed,
                    disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
        u.check_once()
        snap = u.download_once()
        assert snap["state"] == STATE_FAILED, snap
        assert staged_receipt(env) is None
        assert not os.path.exists(os.path.join(staging_dir(env), "0.2.0"))
        # ... and a retry over a healthy feed still works, so the failure was
        # not sticky.
        u2 = Updater(trust=trust, env=env, opener_fn=lambda _b: feed,
                     disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
        u2.check_once()
        assert u2.download_once()["state"] == STATE_READY
        assert staged_receipt(env)["version"] == "0.2.0"

        # -- a full disk is refused before anything is downloaded -----------
        clear_staged(env)
        u3 = Updater(trust=trust, env=env, opener_fn=lambda _b: feed,
                     disk_fn=lambda _p: 1000, now_fn=lambda: now)
        u3.check_once()
        snap = u3.download_once()
        assert snap["state"] == STATE_FAILED, snap
        assert "free disk space" in snap["error"] or "free" in snap["error"], snap["error"]
        assert staged_receipt(env) is None

        # -- a staged installer that is tampered with afterwards ------------
        # The receipt is a claim about a file, and the file sits on disk for as
        # long as the user takes to click. Anything running as that user can
        # replace it, so the receipt is re-verified on every read.
        u2b = Updater(trust=trust, env=env, opener_fn=lambda _b: feed,
                      disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
        u2b.check_once()
        u2b.download_once()
        staged = staged_receipt(env)
        assert staged is not None
        with open(staged["path"], "r+b") as fh:
            fh.seek(5)
            fh.write(b"\x00")
        assert staged_receipt(env) is None, (
            "a staged installer that no longer matches its receipt must not be "
            "offered")
        assert not os.path.exists(os.path.dirname(staged["path"]))
        # ... and the snapshot stops saying "ready to install" over a button
        # that cannot work. A stale success is its own kind of lie.
        snap = u2b.snapshot()
        assert snap["state"] == STATE_FAILED, snap
        assert "no longer the one Hearth verified" in snap["error"], snap["error"]

        # -- a staged installer is dropped once it IS the running version ---
        # The shape of a successful update: 0.2.0 was staged, the installer
        # ran, and the app that comes back is 0.2.0 with 0.2.0 still sitting
        # in its staging directory. Offering it again would produce a button
        # the shell is guaranteed to refuse.
        clear_staged(env)
        u_applied = Updater(trust=trust, env=env, opener_fn=lambda _b: feed,
                            disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
        u_applied.check_once(force=True)
        u_applied.download_once()
        assert staged_receipt(env)["version"] == "0.2.0"
        after = Updater(trust=trust, env=dict(env, **{ENV_VERSION: "0.2.0"}),
                        opener_fn=lambda _b: feed, disk_fn=lambda _p: 10 ** 12,
                        now_fn=lambda: now)
        snap = after.snapshot()
        assert snap["staged"] is None, snap["staged"]
        assert snap["state"] != STATE_READY, snap
        assert not os.path.exists(os.path.join(staging_dir(env), "0.2.0")), (
            "an applied update must not leave its own installer on disk")

        # -- up to date ----------------------------------------------------
        clear_staged(env)
        current_bytes = json.dumps(sign_doc(make_signed(
            version="0.3.0", minimum_version="0.0.0",
            released_at="2026-08-02T00:00:00Z",
            expires_at="2026-09-02T00:00:00Z",
            artifact={"name": "Hearth-Setup-0.3.0.exe",
                      "path": "stable/0.3.0/Hearth-Setup-0.3.0.exe",
                      "size_bytes": len(installer), "sha256": artifact_sha},
        ))).encode("utf-8")
        newer_feed = _FakeFeed({base + "stable/manifest.json": current_bytes})
        # Its own data directory: this run raises the floor to 0.3.0, and the
        # sections below are about an installation that has never seen it.
        env_new = {"HEARTH_DATA_DIR": os.path.join(tmp, "data-current"),
                   ENV_VERSION: "0.3.0"}
        u4 = Updater(trust=trust, env=env_new, opener_fn=lambda _b: newer_feed,
                     disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
        snap = u4.check_once()
        assert snap["state"] == STATE_UP_TO_DATE, snap
        assert snap["available"] is None

        # -- an unreachable feed is honest ---------------------------------
        empty_feed = _FakeFeed({})
        u5 = Updater(trust=trust, env=env, opener_fn=lambda _b: empty_feed,
                     disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
        snap = u5.check_once()
        assert snap["state"] == STATE_FAILED, snap
        assert "could not check for updates" in snap["error"], snap["error"]
        assert snap["available"] is None
        assert "0.1.0" in snap["error"], "an honest failure names what is running"

        # -- an oversized manifest is cut off ------------------------------
        huge = _FakeFeed({base + "stable/manifest.json":
                          b"{" + b" " * (MAX_MANIFEST_BYTES + 10)})
        try:
            fetch_manifest(base, "stable", opener=huge)
            raise AssertionError("an oversized manifest must be refused")
        except UpdateError as exc:
            assert "larger than" in str(exc), str(exc)

        # -- an unconfigured build says so ---------------------------------
        blind = dict(trust, feed="https://releases.hearth.invalid/updates/")
        u6 = Updater(trust=blind, env=env, opener_fn=lambda _b: feed,
                     now_fn=lambda: now)
        snap = u6.check_once()
        assert snap["state"] == STATE_UNCONFIGURED, snap
        assert "nothing has been published" in snap["message"].lower(), snap["message"]
        assert not feed.opened or base + "stable/manifest.json" in feed.opened

        # -- the threaded surface -------------------------------------------
        clear_staged(env)
        u7 = Updater(trust=trust, env=env, opener_fn=lambda _b: feed,
                     disk_fn=lambda _p: 10 ** 12, now_fn=lambda: now)
        first = u7.snapshot()["version"]
        u7.check(force=True)
        snap = u7.join(timeout=20)
        assert snap["state"] == STATE_AVAILABLE, snap
        assert snap["version"] > first
        # snapshot_after blocks until something changes and returns unchanged
        # on timeout, so an SSE loop can tell the two apart by version alone.
        assert u7.snapshot_after(snap["version"], timeout=0.05)["version"] == snap["version"]
        u7.download()
        snap = u7.join(timeout=30)
        assert snap["state"] == STATE_READY, snap
        assert snap["staged"]["version"] == "0.2.0", snap

        # dismiss throws the file away but NOT the floor.
        snap = u7.dismiss()
        assert snap["state"] == STATE_IDLE, snap
        assert staged_receipt(env) is None
        assert read_floor(env)["version"] == "0.2.0", (
            "declining an update must not forget that it exists")

        # -- auto_check is a persisted setting -----------------------------
        assert u7.snapshot()["auto_check"] is True
        u7.set_auto_check(False)
        assert u7.snapshot()["auto_check"] is False
        assert read_state(env)["auto_check"] is False
        u7.set_auto_check(True)
        assert u7.snapshot()["auto_check"] is True

        # -- the floor is atomic and monotonic ------------------------------
        raise_floor("0.5.0", "2026-09-01T00:00:00Z", env)
        assert read_floor(env)["version"] == "0.5.0"
        raise_floor("0.4.0", "2026-08-01T00:00:00Z", env)
        assert read_floor(env)["version"] == "0.5.0", "the floor must never fall"
        raise_floor("not a version", "2026-08-01T00:00:00Z", env)
        assert read_floor(env)["version"] == "0.5.0"

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("hearth-update self-test OK")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hearth_update",
        description="Check for, verify and stage a Hearth update. Never runs one.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--check", action="store_true",
                        help="check the configured feed once and print the result")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return _self_test()
    if args.check:
        updater = Updater()
        snap = updater.check_once()
        if args.json:
            print(json.dumps(snap, indent=2, sort_keys=True, default=str))
        else:
            print("{}: {}".format(snap["state"], snap["message"] or snap["error"] or ""))
        return 0 if snap["state"] not in (STATE_FAILED,) else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
