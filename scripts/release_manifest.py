#!/usr/bin/env python3
r"""Make, sign and check a Hearth release feed. The operator's half of the updater.

    python scripts/release_manifest.py keygen --key-id hearth-release-2026-10 \
                                             --out <a folder outside every checkout>/<id>.key
    python scripts/release_manifest.py sign  --installer build/dist/Hearth-Setup-0.2.0.exe \
                                             --version 0.2.0 --key <id>.key --out build/feed
    python scripts/release_manifest.py sign  ... --seed-file <file holding only the hex seed>
    python scripts/release_manifest.py verify --feed build/feed --installed 0.1.0
    python scripts/release_manifest.py serve  --feed build/feed --port 8799

The shipped application (agent/hearth_update.py) is the consumer. This is the
producer, and it deliberately does not ship: it is not staged into the
installer, so a compromised Hearth install contains no signing code and no
path to a key. See build_windows.py's stage(), which copies exactly one
script.

THE LAYOUT
----------
The layout follows release/trust.json. With "layout": "github-releases" (the
shipped one), `sign` writes manifest-<channel>.json beside the installer, which
are exactly the files to attach to a GitHub release, and the signed artifact
path is download/v<version>/<installer>. `serve` then answers the way GitHub
does, redirects included, so the shipped client can be pointed at it with
HEARTH_UPDATE_FEED and driven end to end before anything is public. Without a
layout, the older directory layout (<channel>/manifest.json and
<channel>/<version>/<installer>, plus a plain index.html) is written and served
as plain files.

In CI, .github/workflows/release.yml runs `sign --seed-file` with the seed from
the HEARTH_UPDATE_SIGNING_KEY secret written to a private temporary file. The
seed is read from a file, never from an argument or this process's
environment: arguments are visible to every process on the machine, and both
end up in logs.

WHAT IS SIGNED, AND WHAT IS NOT
-------------------------------
The signature is over the canonical serialization of the manifest's `signed`
block, which names the version, the channel, the release and expiry dates, the
minimum version an update may be applied over, the release notes, and the
artifact's name, feed path, exact size and SHA-256. Everything a client acts on
is inside that block. The installer itself is NOT signed by this tool: it does
not need to be, because its SHA-256 is inside the signed block, and a hash in a
signed document is a signature over the file by another name. That is what
makes this work on an unsigned build with no certificate.

THE KEY
-------
Ed25519, 32 bytes of seed, written to a file this tool creates with 0600 where
the platform honours it (Windows does not: there the file inherits its
folder's permissions, which is one more reason the folder must be private and
must not be synced anywhere). `*.key` is in .gitignore, so a key file cannot be
committed by accident, but that is a safety net rather than a policy. The
public half goes in release/trust.json, is committed, and ships inside the
application. Rotating a key means adding the new one to trust.json as `active`,
shipping a build that carries it, and only then signing with it; the old key
stays until nobody is running a build that predates the new one, and is then
marked `revoked` rather than deleted so an old signature stops verifying
rather than becoming unattributable.

WHAT THIS TOOL DOES NOT DO
--------------------------
It does not upload anything, does not talk to any remote host, and has no
credentials for one. `serve` binds 127.0.0.1 and exists so the whole chain can
be exercised end to end on one machine. Publishing is the release workflow's
job, started by pushing a tag; docs/updates.md describes it.

Python standard library only.
"""

import argparse
import datetime
import functools
import hashlib
import html
import http.server
import json
import os
import re
import sys
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "agent"))

import hearth_ed25519 as ed  # noqa: E402
import hearth_update as hu  # noqa: E402

TRUST_PATH = os.path.join(REPO_ROOT, "release", "trust.json")
KEYS_DIR = os.path.join(REPO_ROOT, "release", "keys")

#: How long a manifest is valid for. Short enough that a feed nobody is
#: maintaining stops being trusted, long enough that a release cadence of a
#: few months does not strand anybody. hearth_update refuses anything above
#: MAX_VALIDITY_DAYS whatever is passed here. Once the newest manifest
#: expires, every install's check fails (calmly) until a release or a
#: re-signed manifest replaces it; docs/updates.md says how.
DEFAULT_VALID_DAYS = 120

_SEED_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def trust_layout(trust_path=TRUST_PATH):
    """The feed layout the given trust file declares."""
    data = _read_json(trust_path) or {}
    return hu.layout_for(data)


# --------------------------------------------------------------------------
# keygen
# --------------------------------------------------------------------------

def keygen(key_id, out=None, trust_path=TRUST_PATH, add_to_trust=True, seed=None):
    """Make a signing key. Returns {"key_id", "public_key", "path"}.

    The seed comes from os.urandom, which is the platform CSPRNG. It is
    written to exactly one file and never printed: a key that scrolls past in
    a terminal is a key in a scrollback buffer, in a screen recording, and in
    whatever the terminal syncs to.
    """
    if not hu._KEY_ID_RE.match(key_id or ""):
        raise SystemExit("a key id must be letters, digits, dot, dash or "
                         "underscore: {!r}".format(key_id))
    seed = seed or os.urandom(32)
    public = ed.public_key(seed)
    path = out or os.path.join(KEYS_DIR, key_id + ".key")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        raise SystemExit("{} already exists; refusing to overwrite a signing "
                         "key".format(path))
    # Created with the mode set at open time rather than chmod'ed afterwards,
    # so there is no window where the file exists and is world readable.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        json.dump({"key_id": key_id, "algorithm": "ed25519",
                   "private_seed": ed.to_hex(seed),
                   "public_key": ed.to_hex(public),
                   "created_at": _stamp(_now())}, fh, indent=2, sort_keys=True)
        fh.write("\n")

    if add_to_trust:
        trust = _read_json(trust_path) or {
            "schema": 1, "app_id": "com.hearthlocal.hearth",
            "feed": "https://releases.hearth.invalid/updates/",
            "channels": ["stable"], "default_channel": "stable", "keys": []}
        trust["keys"] = [k for k in trust.get("keys", []) if k.get("key_id") != key_id]
        trust["keys"].append({
            "key_id": key_id, "algorithm": "ed25519",
            "public_key": ed.to_hex(public), "status": "active",
            "added_at": _stamp(_now()),
        })
        hu.validate_trust(trust)
        _write_json(trust_path, trust)
    return {"key_id": key_id, "public_key": ed.to_hex(public), "path": path}


def load_key(path):
    data = _read_json(path)
    if not isinstance(data, dict):
        raise SystemExit("{} is not a key file".format(path))
    seed = ed.from_hex(data.get("private_seed"), 32)
    key_id = data.get("key_id")
    if not hu._KEY_ID_RE.match(key_id or ""):
        raise SystemExit("{} carries no usable key id".format(path))
    if ed.to_hex(ed.public_key(seed)) != data.get("public_key"):
        raise SystemExit("{}: the private seed does not match the public key "
                         "recorded beside it".format(path))
    return key_id, seed


def load_seed_file(path, trust_path=TRUST_PATH, key_id=None):
    """A bare hex seed from a file, matched to a key in the trust file.

    This is the CI path: the HEARTH_UPDATE_SIGNING_KEY secret holds only the
    64 hex characters of `private_seed`, which is the smallest thing that can
    be pasted into a secret without also pasting a format. The key id is not
    taken on trust from anywhere: the public key is derived from the seed and
    looked up in release/trust.json, so a secret that does not belong to an
    ACTIVE key the shipped builds trust fails here, before anything is
    signed, rather than producing a manifest every client refuses.

    No message from this function ever includes what the file contains.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read(4096).strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit("cannot read the seed file: {}".format(
            exc.__class__.__name__)) from None
    if not _SEED_RE.match(text):
        raise SystemExit(
            "the seed file does not hold exactly 64 hex characters (the "
            "private_seed value from a key file made by keygen); refusing it")
    seed = bytes.fromhex(text)
    public = ed.to_hex(ed.public_key(seed))
    trust = hu.load_trust(trust_path)
    match = [k for k in trust["keys"] if k.get("public_key") == public]
    if not match:
        raise SystemExit(
            "this seed's public key ({}) is not in {}. Sign with a key the "
            "shipped builds trust, or add its public key there first.".format(
                public, trust_path))
    entry = match[0]
    if entry.get("status") != "active":
        raise SystemExit("this seed belongs to key {}, which is {} in {}; refusing "
                         "to sign with it".format(entry["key_id"], entry.get("status"),
                                                  trust_path))
    if key_id and key_id != entry["key_id"]:
        raise SystemExit("this seed belongs to key {}, not {}".format(
            entry["key_id"], key_id))
    return entry["key_id"], seed


# --------------------------------------------------------------------------
# sign
# --------------------------------------------------------------------------

def build_signed(version, channel, artifact_path, notes="", released_at=None,
                 valid_days=DEFAULT_VALID_DAYS, minimum_version="0.0.0",
                 app_id="com.hearthlocal.hearth", layout=hu.LAYOUT_DIRECTORY):
    """The block that gets signed, built from the installer on disk.

    The size and hash are read off the FILE, never taken from an argument.
    An operator who could type a hash could type the wrong one, and the whole
    chain rests on this number being the number of the bytes that will
    actually be served.
    """
    if hu.parse_version(version) is None:
        raise SystemExit("--version must be MAJOR.MINOR.PATCH, got {!r}".format(version))
    if layout not in hu.LAYOUTS:
        raise SystemExit("unknown layout {!r}".format(layout))
    if not os.path.isfile(artifact_path):
        raise SystemExit("no installer at {}".format(artifact_path))
    name = os.path.basename(artifact_path)
    size = os.path.getsize(artifact_path)
    digest = hu.sha256_file(artifact_path)
    released = released_at or _now()
    if layout == hu.LAYOUT_GITHUB:
        path = hu.github_artifact_path(version, name)
    else:
        path = "{}/{}/{}".format(channel, version, name)
    signed = {
        "schema": 1,
        "app_id": app_id,
        "channel": channel,
        "version": version,
        "released_at": _stamp(released),
        "expires_at": _stamp(released + datetime.timedelta(days=valid_days)),
        "minimum_version": minimum_version,
        "notes": notes or "",
        "artifact": {
            "name": name,
            "path": path,
            "size_bytes": size,
            "sha256": digest,
        },
    }
    return signed


def sign_document(signed, key_id, seed):
    """Wrap a signed block with its signature. The signature is over
    hearth_update.canonical_bytes(signed), which is the same function the
    client verifies with -- one implementation, not two that must agree."""
    signature = ed.sign(seed, hu.canonical_bytes(signed))
    return {
        "signed": signed,
        "signatures": [{"key_id": key_id, "algorithm": "ed25519",
                        "signature": ed.to_hex(signature)}],
    }


def stage_feed(document, artifact_path, out_dir, layout=hu.LAYOUT_DIRECTORY):
    """Lay out a feed an operator can publish verbatim. Returns the manifest path.

    Directory layout, for a static host:

        <out>/<channel>/manifest.json
        <out>/<channel>/<version>/<installer>
        <out>/index.html          a plain download page, local artifact only

    GitHub Releases layout, the release assets themselves (one flat folder):

        <out>/manifest-<channel>.json
        <out>/<installer>         copied only when it is not already there
    """
    signed = document["signed"]
    channel = signed["channel"]
    rel = signed["artifact"]["path"]
    hu._check_relative_path(rel)
    if layout == hu.LAYOUT_GITHUB:
        expected = hu.github_artifact_path(signed["version"], signed["artifact"]["name"])
        if rel != expected:
            raise SystemExit("a GitHub release serves its installer at {}, but this "
                             "manifest names {}".format(expected, rel))
        target = os.path.join(out_dir, signed["artifact"]["name"])
        manifest_path = os.path.join(out_dir, hu.flat_manifest_name(channel))
    else:
        target = os.path.join(out_dir, *rel.split("/"))
        manifest_path = os.path.join(out_dir, channel, hu.MANIFEST_NAME)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    if os.path.abspath(target) != os.path.abspath(artifact_path):
        with open(artifact_path, "rb") as src, open(target, "wb") as dst:
            for chunk in iter(functools.partial(src.read, 1024 * 1024), b""):
                dst.write(chunk)
    _write_json(manifest_path, document)
    if layout != hu.LAYOUT_GITHUB:
        # A GitHub release page already is the download page, with the hash
        # in its notes; a second one beside it would only disagree with it.
        _write_download_page(out_dir, document)
    return manifest_path


def _write_download_page(out_dir, document):
    """A plain download page for the feed root.

    Every value that reaches it is escaped, and it carries no script and no
    external reference. It exists so a person can check a download by hand
    against the same hash the updater checks against, and so "publish" is a
    single directory upload rather than a page somebody has to write.
    """
    signed = document["signed"]
    artifact = signed["artifact"]
    esc = html.escape
    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hearth {version} for Windows</title>
<style>
 body {{ font: 16px/1.6 system-ui, sans-serif; max-width: 44rem; margin: 4rem auto;
        padding: 0 1.5rem; color: #241d18; background: #faf7f3; }}
 code {{ font: 13px/1.5 ui-monospace, monospace; word-break: break-all; }}
 .hash {{ background: #efe9e2; padding: .6rem .8rem; border-radius: .4rem; display: block; }}
 a.dl {{ display: inline-block; background: #b4441f; color: #fff; text-decoration: none;
        padding: .7rem 1.2rem; border-radius: .4rem; font-weight: 600; }}
 .warn {{ border-left: 3px solid #b4441f; padding-left: 1rem; }}
</style></head><body>
<h1>Hearth {version}</h1>
<p>Released {released}. Windows 10 and 11, 64-bit. Installs for the current
user; no administrator rights are required.</p>
<p><a class="dl" href="{href}">Download {name}</a> ({size} MB)</p>
<h2>Verify what you downloaded</h2>
<p>SHA-256:</p>
<code class="hash">{sha}</code>
<p>In PowerShell:</p>
<code class="hash">Get-FileHash -Algorithm SHA256 .\\{name}</code>
<h2>Release notes</h2>
<pre>{notes}</pre>
<p class="warn">This build is not code-signed, so Windows SmartScreen will warn
about it. Hearth checks its own updates against a signing key built into the
application, which is a different and stronger check than the one SmartScreen
is making.</p>
</body></html>
""".format(version=esc(signed["version"]),
           released=esc(signed["released_at"]),
           href=esc("/".join(artifact["path"].split("/"))),
           name=esc(artifact["name"]),
           size="{:.0f}".format(artifact["size_bytes"] / 1e6),
           sha=esc(artifact["sha256"]),
           notes=esc(signed.get("notes") or "(none)"))
    path = os.path.join(out_dir, "index.html")
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def verify_feed(feed_dir, trust_path=TRUST_PATH, installed=None, channel=None,
                layout=None):
    """Check a staged feed the way a client would. Returns a report dict.

    Reads the manifest and the artifact off disk and runs them through the
    CLIENT's own verify_document, validate_signed and verify_file, so a feed
    that passes here is a feed the shipped application accepts. Using the
    producer's own idea of correctness here would prove nothing.
    """
    trust = hu.load_trust(trust_path)
    channel = channel or trust["default_channel"]
    layout = layout or hu.layout_for(trust)
    if layout == hu.LAYOUT_GITHUB:
        manifest_path = os.path.join(feed_dir, hu.flat_manifest_name(channel))
    else:
        manifest_path = os.path.join(feed_dir, channel, hu.MANIFEST_NAME)
    document = _read_json(manifest_path)
    if document is None:
        raise SystemExit("no manifest at {}".format(manifest_path))
    signed, key_id = hu.verify_document(document, trust)
    hu.validate_signed(signed, trust, channel)
    if layout == hu.LAYOUT_GITHUB:
        expected = hu.github_artifact_path(signed["version"], signed["artifact"]["name"])
        if signed["artifact"]["path"] != expected:
            # GitHub serves an asset only under its own release's tag. A path
            # naming any other tag is a manifest whose installer 404s.
            raise hu.UpdateError("the signed artifact path is {} but a GitHub release "
                                 "serves it at {}".format(signed["artifact"]["path"],
                                                          expected))
        artifact = os.path.join(feed_dir, signed["artifact"]["name"])
    else:
        artifact = os.path.join(feed_dir, *signed["artifact"]["path"].split("/"))
    hu.verify_file(artifact, signed["artifact"]["sha256"],
                   signed["artifact"]["size_bytes"])
    report = {"manifest": manifest_path, "signed_by": key_id,
              "version": signed["version"], "channel": channel, "layout": layout,
              "artifact": artifact, "sha256": signed["artifact"]["sha256"],
              "size_bytes": signed["artifact"]["size_bytes"],
              "expires_at": signed["expires_at"]}
    if installed:
        plan = hu.evaluate(signed, trust, channel, installed)
        report["action"] = plan["action"]
    return report


# --------------------------------------------------------------------------
# serve (local testing only)
# --------------------------------------------------------------------------

class _FeedHandler(http.server.SimpleHTTPRequestHandler):
    """Static files with no directory listing and no logging noise."""

    def log_message(self, fmt, *args):
        sys.stderr.write("  feed: " + (fmt % args) + "\n")

    def list_directory(self, path):
        self.send_error(404, "not found")
        return None


class _GitHubFeedHandler(_FeedHandler):
    """A flat release folder, answered the way GitHub answers release assets.

        /latest/download/<name>      302 -> /download/v<newest>/<name>
        /download/v<version>/<name>  302 -> /asset/<name>?token=...
        /asset/<name>                the file

    The second redirect stands in for GitHub's hop to its asset host, query
    token and all, so the client's redirect handling and its habit of keeping
    tokens out of messages are exercised rather than assumed. Anything else is
    a 404, like a release asset that does not exist.
    """

    def _versions(self):
        found = set()
        for name in os.listdir(self.directory):
            if name.startswith("manifest-") and name.endswith(".json"):
                doc = _read_json(os.path.join(self.directory, name)) or {}
                version = ((doc.get("signed") or {}).get("version"))
                if hu.parse_version(version or ""):
                    found.add(version)
        return found

    def _route(self):
        path = urllib.parse.urlsplit(self.path).path
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["latest", "download"]:
            versions = self._versions()
            if not versions:
                return ("missing", None)
            newest = max(versions, key=hu.parse_version)
            return ("redirect", "/download/v{}/{}".format(newest, parts[2]))
        if len(parts) == 3 and parts[0] == "download" and parts[1].startswith("v"):
            if parts[1][1:] in self._versions():
                return ("redirect", "/asset/{}?token=local-test-token".format(parts[2]))
            return ("missing", None)
        if len(parts) == 2 and parts[0] == "asset" and "/" not in parts[1]:
            if os.path.isfile(os.path.join(self.directory, parts[1])):
                return ("file", "/" + parts[1])
        return ("missing", None)

    def _answer(self, head_only):
        kind, where = self._route()
        if kind == "redirect":
            self.send_response(302)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if kind == "file":
            self.path = where
            return super().do_HEAD() if head_only else super().do_GET()
        self.send_error(404, "not found")

    def do_GET(self):  # noqa: N802 - the stdlib's name
        self._answer(False)

    def do_HEAD(self):  # noqa: N802
        self._answer(True)


def detect_layout(feed_dir):
    """Which layout a staged folder is in, from what is in it."""
    try:
        names = os.listdir(feed_dir)
    except OSError:
        return hu.LAYOUT_DIRECTORY
    if any(n.startswith("manifest-") and n.endswith(".json") for n in names):
        return hu.LAYOUT_GITHUB
    return hu.LAYOUT_DIRECTORY


def serve(feed_dir, port=0, host="127.0.0.1", layout=None):
    """Serve a staged feed on loopback. Returns the running server.

    Loopback only, always. This is a test harness for the update path, not a
    way to distribute anything.
    """
    layout = layout or detect_layout(feed_dir)
    cls = _GitHubFeedHandler if layout == hu.LAYOUT_GITHUB else _FeedHandler
    handler = functools.partial(cls, directory=os.path.abspath(feed_dir))
    server = http.server.ThreadingHTTPServer((host, port), handler)
    return server


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_parser():
    p = argparse.ArgumentParser(
        prog="release_manifest",
        description="Make, sign and check a Hearth release feed.")
    p.add_argument("--self-test", action="store_true")
    sub = p.add_subparsers(dest="command")

    kg = sub.add_parser("keygen", help="make an Ed25519 release signing key")
    kg.add_argument("--key-id", required=True)
    kg.add_argument("--out", default=None)
    kg.add_argument("--no-trust", action="store_true",
                    help="do not add the public key to release/trust.json")

    sg = sub.add_parser("sign", help="sign an installer into a feed")
    sg.add_argument("--installer", required=True)
    sg.add_argument("--version", required=True)
    which = sg.add_mutually_exclusive_group(required=True)
    which.add_argument("--key", help="a key file written by keygen")
    which.add_argument("--seed-file",
                       help="a file holding only the 64-hex-character private seed "
                            "(how CI passes the HEARTH_UPDATE_SIGNING_KEY secret)")
    sg.add_argument("--key-id", default=None,
                    help="with --seed-file: refuse unless the seed is this key")
    sg.add_argument("--out", required=True, help="the feed folder to write")
    sg.add_argument("--trust", default=TRUST_PATH)
    sg.add_argument("--layout", choices=hu.LAYOUTS, default=None,
                    help="default: the layout release/trust.json declares")
    sg.add_argument("--channel", default="stable")
    sg.add_argument("--notes", default="")
    sg.add_argument("--notes-file", default=None)
    sg.add_argument("--minimum-version", default="0.0.0")
    sg.add_argument("--valid-days", type=int, default=DEFAULT_VALID_DAYS)
    sg.add_argument("--released-at", default=None,
                    help="ISO-8601 UTC, for reproducing an existing manifest")

    vf = sub.add_parser("verify", help="check a feed the way a client would")
    vf.add_argument("--feed", required=True)
    vf.add_argument("--trust", default=TRUST_PATH)
    vf.add_argument("--channel", default=None)
    vf.add_argument("--installed", default=None)
    vf.add_argument("--layout", choices=hu.LAYOUTS, default=None)

    sv = sub.add_parser("serve", help="serve a feed on loopback, for testing")
    sv.add_argument("--feed", required=True)
    sv.add_argument("--port", type=int, default=0)
    sv.add_argument("--layout", choices=hu.LAYOUTS, default=None)
    return p


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return _self_test()

    if args.command == "keygen":
        result = keygen(args.key_id, out=args.out, add_to_trust=not args.no_trust)
        print("wrote {}".format(result["path"]))
        print("key id      {}".format(result["key_id"]))
        print("public key  {}".format(result["public_key"]))
        if not args.no_trust:
            print("added to    {}".format(TRUST_PATH))
        print("\nThe private seed is in that file and nowhere else. Keep that "
              "folder private and\nout of anything that syncs. It is not printed "
              "here on purpose.")
        return 0

    if args.command == "sign":
        notes = args.notes
        if args.notes_file:
            with open(args.notes_file, "r", encoding="utf-8") as fh:
                notes = fh.read()
        released = None
        if args.released_at:
            released = hu._parse_time(args.released_at, "--released-at")
        if args.seed_file:
            key_id, seed = load_seed_file(args.seed_file, trust_path=args.trust,
                                          key_id=args.key_id)
        else:
            key_id, seed = load_key(args.key)
        layout = args.layout or trust_layout(args.trust)
        signed = build_signed(args.version, args.channel, args.installer,
                              notes=notes, released_at=released,
                              valid_days=args.valid_days,
                              minimum_version=args.minimum_version,
                              layout=layout)
        document = sign_document(signed, key_id, seed)
        manifest = stage_feed(document, args.installer, args.out, layout=layout)
        print("signed {} {} with {}".format(args.channel, args.version, key_id))
        print("  layout   {}".format(layout))
        print("  sha256   {}".format(signed["artifact"]["sha256"]))
        print("  size     {:,} bytes".format(signed["artifact"]["size_bytes"]))
        print("  path     {}".format(signed["artifact"]["path"]))
        print("  expires  {}".format(signed["expires_at"]))
        print("  manifest {}".format(manifest))
        report = verify_feed(args.out, trust_path=args.trust, channel=args.channel,
                             layout=layout)
        print("\nre-checked with the client's own verifier: signed by {}, "
              "artifact hash matches".format(report["signed_by"]))
        return 0

    if args.command == "verify":
        try:
            report = verify_feed(args.feed, trust_path=args.trust,
                                 installed=args.installed, channel=args.channel,
                                 layout=args.layout)
        except hu.UpdateError as exc:
            print("REFUSED: {}".format(exc), file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if args.command == "serve":
        server = serve(args.feed, port=args.port, layout=args.layout)
        host, port = server.server_address[:2]
        print("serving {} at http://{}:{}/".format(os.path.abspath(args.feed), host, port))
        print("point the app at it with HEARTH_UPDATE_FEED=http://{}:{}/".format(host, port))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    parser.print_help()
    return 0


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _self_test():
    import shutil
    import tempfile
    import threading
    import urllib.error
    import urllib.request

    tmp = tempfile.mkdtemp(prefix="hearth-release-test-")
    saved_env = {k: os.environ.get(k) for k in ("HEARTH_DATA_DIR",)}
    try:
        os.environ["HEARTH_DATA_DIR"] = os.path.join(tmp, "hearth-data")
        # -- the committed trust file is valid and points at the real feed ---
        trust = hu.load_trust(TRUST_PATH)
        assert any(k["status"] == "active" for k in trust["keys"])
        assert hu.configured(trust, {}), "release/trust.json must name a real feed"
        assert hu.layout_for(trust) == hu.LAYOUT_GITHUB, trust.get("layout")
        assert trust_layout(TRUST_PATH) == hu.LAYOUT_GITHUB
        # The 2026-08 seed sat in a cloud-synced folder; it must stay revoked.
        assert {k["key_id"]: k["status"] for k in trust["keys"]}.get(
            "hearth-release-2026-08") == "revoked"

        # -- keygen ---------------------------------------------------------
        trust_copy = os.path.join(tmp, "trust.json")
        _write_json(trust_copy, {"schema": 1, "app_id": "com.hearthlocal.hearth",
                                 "feed": "https://releases.hearth.invalid/updates/",
                                 "channels": ["stable"], "default_channel": "stable",
                                 "keys": []})
        key_path = os.path.join(tmp, "test.key")
        made = keygen("test-key", out=key_path, trust_path=trust_copy)
        assert os.path.isfile(key_path)
        key_id, seed = load_key(key_path)
        assert key_id == "test-key"
        assert ed.to_hex(ed.public_key(seed)) == made["public_key"]
        local_trust = _read_json(trust_copy)
        assert hu.validate_trust(local_trust)
        assert local_trust["keys"][0]["public_key"] == made["public_key"]
        # A second keygen to the same path refuses rather than overwriting a
        # signing key, which would silently invalidate every past release.
        try:
            keygen("test-key", out=key_path, trust_path=trust_copy)
            raise AssertionError("keygen must not overwrite an existing key")
        except SystemExit:
            pass
        # A key file whose seed and public key disagree is refused.
        tampered_key = os.path.join(tmp, "tampered.key")
        data = _read_json(key_path)
        data["public_key"] = "0" * 64
        _write_json(tampered_key, data)
        try:
            load_key(tampered_key)
            raise AssertionError("a key file that contradicts itself must be refused")
        except SystemExit:
            pass

        # -- sign, stage, verify (directory layout) --------------------------
        installer = os.path.join(tmp, "Hearth-Setup-0.2.0.exe")
        payload = b"MZ" + os.urandom(4096)
        with open(installer, "wb") as fh:
            fh.write(payload)
        signed = build_signed("0.2.0", "stable", installer, notes="Test release.")
        assert signed["artifact"]["sha256"] == hashlib.sha256(payload).hexdigest()
        assert signed["artifact"]["size_bytes"] == len(payload)
        assert signed["artifact"]["path"] == "stable/0.2.0/Hearth-Setup-0.2.0.exe"
        document = sign_document(signed, key_id, seed)
        feed_dir = os.path.join(tmp, "feed")
        manifest_path = stage_feed(document, installer, feed_dir)
        assert os.path.isfile(manifest_path)
        assert os.path.isfile(os.path.join(feed_dir, "stable", "0.2.0",
                                           "Hearth-Setup-0.2.0.exe"))
        assert os.path.isfile(os.path.join(feed_dir, "index.html"))
        assert detect_layout(feed_dir) == hu.LAYOUT_DIRECTORY

        report = verify_feed(feed_dir, trust_path=trust_copy, installed="0.1.0")
        assert report["signed_by"] == "test-key", report
        assert report["action"] == "update", report

        # -- MUTATION: one byte of the served installer ----------------------
        # The manifest still verifies; the artifact does not, and the client's
        # own verify_file is what says so.
        target = os.path.join(feed_dir, "stable", "0.2.0", "Hearth-Setup-0.2.0.exe")
        with open(target, "r+b") as fh:
            fh.seek(100)
            fh.write(bytes([fh.read(1)[0] ^ 0xFF]))
        try:
            verify_feed(feed_dir, trust_path=trust_copy)
            raise AssertionError("a tampered artifact must fail the feed check")
        except hu.ChecksumError:
            pass
        with open(target, "wb") as fh:
            fh.write(payload)
        assert verify_feed(feed_dir, trust_path=trust_copy)["version"] == "0.2.0"

        # -- MUTATION: the manifest re-signed with a key nobody trusts -------
        other = keygen("other-key", out=os.path.join(tmp, "other.key"),
                       add_to_trust=False)
        _other_id, other_seed = load_key(os.path.join(tmp, "other.key"))
        forged = sign_document(signed, "test-key", other_seed)
        _write_json(manifest_path, forged)
        try:
            verify_feed(feed_dir, trust_path=trust_copy)
            raise AssertionError("a manifest signed by an untrusted key must be "
                                 "refused")
        except hu.SignatureError:
            pass
        assert other["public_key"] != made["public_key"]
        _write_json(manifest_path, document)

        # -- MUTATION: the manifest edited after signing ---------------------
        edited = json.loads(json.dumps(document))
        edited["signed"]["version"] = "9.9.9"
        _write_json(manifest_path, edited)
        try:
            verify_feed(feed_dir, trust_path=trust_copy)
            raise AssertionError("an edited manifest must be refused")
        except hu.SignatureError:
            pass
        _write_json(manifest_path, document)

        # -- the download page escapes what it prints ------------------------
        hostile = build_signed("0.2.0", "stable", installer,
                               notes='</pre><img src=x onerror=alert(1)>')
        page = _write_download_page(os.path.join(tmp, "page"),
                                    sign_document(hostile, key_id, seed))
        with open(page, "r", encoding="utf-8") as fh:
            text = fh.read()
        assert "<img src=x" not in text, "the download page must escape release notes"
        assert "&lt;img src=x" in text, text[-600:]

        # -- serve (directory layout), and a real client fetch over loopback --
        server = serve(feed_dir, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            base = "http://127.0.0.1:{}/".format(port)
            got = hu.fetch_manifest(base, "stable")
            fetched_signed, fetched_key = hu.verify_document(got, local_trust)
            assert fetched_key == "test-key"
            assert fetched_signed["version"] == "0.2.0"
            # Directory listings are off: a feed is a set of known paths, not
            # a browsable directory.
            try:
                with urllib.request.urlopen(base + "stable/", timeout=5) as resp:
                    raise AssertionError("directory listing must be refused, got "
                                         "{}".format(resp.status))
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, exc.code
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        # -- build_signed refuses what it should -----------------------------
        for version in ("0.2", "v0.2.0", "0.2.0-rc1"):
            try:
                build_signed(version, "stable", installer)
                raise AssertionError("must refuse version {!r}".format(version))
            except SystemExit:
                pass
        try:
            build_signed("0.2.0", "stable", os.path.join(tmp, "nope.exe"))
            raise AssertionError("must refuse a missing installer")
        except SystemExit:
            pass

        # -- the GitHub Releases layout --------------------------------------
        gh_trust = os.path.join(tmp, "gh-trust.json")
        gh_doc = dict(_read_json(trust_copy), layout=hu.LAYOUT_GITHUB,
                      feed="https://github.com/o/r/releases/")
        _write_json(gh_trust, gh_doc)
        assert hu.validate_trust(gh_doc)
        assert trust_layout(gh_trust) == hu.LAYOUT_GITHUB

        # The CI path: the secret holds only the hex seed, and the key id is
        # found by deriving the public key, never taken from the caller.
        seed_file = os.path.join(tmp, "seed.txt")
        with open(seed_file, "w", encoding="utf-8") as fh:
            fh.write(ed.to_hex(seed) + "\n")
        assert load_seed_file(seed_file, trust_path=gh_trust) == ("test-key", seed)
        assert load_seed_file(seed_file, trust_path=gh_trust, key_id="test-key")[0] == "test-key"
        for bad_trust_change, why in (
                (lambda t: t.update(keys=[dict(t["keys"][0], status="revoked"),
                                          {"key_id": "spare", "algorithm": "ed25519",
                                           "public_key": other["public_key"],
                                           "status": "active"}]), "revoked"),
                (lambda t: t.update(keys=[{"key_id": "spare", "algorithm": "ed25519",
                                           "public_key": other["public_key"],
                                           "status": "active"}]), "not in")):
            changed = json.loads(json.dumps(gh_doc))
            bad_trust_change(changed)
            bad_path = os.path.join(tmp, "bad-trust.json")
            _write_json(bad_path, changed)
            try:
                load_seed_file(seed_file, trust_path=bad_path)
                raise AssertionError("a seed whose key is {} must be refused".format(why))
            except SystemExit as exc:
                assert why in str(exc), str(exc)
        try:
            load_seed_file(seed_file, trust_path=gh_trust, key_id="someone-else")
            raise AssertionError("a seed that is not the named key must be refused")
        except SystemExit:
            pass
        # A malformed secret is refused, and the refusal never repeats it.
        for junk in ("not hex at all", ed.to_hex(seed)[:-2], "0x" + ed.to_hex(seed),
                     json.dumps({"private_seed": ed.to_hex(seed)})):
            with open(seed_file, "w", encoding="utf-8") as fh:
                fh.write(junk)
            try:
                load_seed_file(seed_file, trust_path=gh_trust)
                raise AssertionError("must refuse seed file {!r}".format(junk[:8]))
            except SystemExit as exc:
                assert ed.to_hex(seed)[:16] not in str(exc), "a refusal printed the seed"
        os.remove(seed_file)

        gh_signed = build_signed("0.2.0", "stable", installer, notes="GitHub release.",
                                 layout=hu.LAYOUT_GITHUB)
        assert gh_signed["artifact"]["path"] == "download/v0.2.0/Hearth-Setup-0.2.0.exe", (
            gh_signed["artifact"]["path"])
        gh_document = sign_document(gh_signed, key_id, seed)
        assets = os.path.join(tmp, "assets")
        gh_manifest = stage_feed(gh_document, installer, assets, layout=hu.LAYOUT_GITHUB)
        # Release assets are flat: exactly the manifest and the installer,
        # no folders, no download page.
        assert sorted(os.listdir(assets)) == ["Hearth-Setup-0.2.0.exe",
                                              "manifest-stable.json"], os.listdir(assets)
        assert gh_manifest == os.path.join(assets, "manifest-stable.json")
        assert detect_layout(assets) == hu.LAYOUT_GITHUB
        report = verify_feed(assets, trust_path=gh_trust, installed="0.1.0")
        assert report["layout"] == hu.LAYOUT_GITHUB and report["action"] == "update", report
        # Signing in place (the CI case: --out is the folder the installer is
        # already in) does not copy the installer onto itself.
        in_place = os.path.join(tmp, "in-place")
        os.makedirs(in_place)
        shutil.copy2(installer, in_place)
        stage_feed(gh_document, os.path.join(in_place, "Hearth-Setup-0.2.0.exe"),
                   in_place, layout=hu.LAYOUT_GITHUB)
        assert verify_feed(in_place, trust_path=gh_trust)["version"] == "0.2.0"
        # A GitHub manifest whose path names some other tag would 404; it is
        # refused here rather than discovered by every client.
        wrong = json.loads(json.dumps(gh_signed))
        wrong["artifact"]["path"] = "download/v0.1.9/Hearth-Setup-0.2.0.exe"
        _write_json(gh_manifest, sign_document(wrong, key_id, seed))
        try:
            verify_feed(assets, trust_path=gh_trust)
            raise AssertionError("a GitHub manifest naming another tag must be refused")
        except hu.UpdateError as exc:
            assert "serves it at" in str(exc), str(exc)
        try:
            stage_feed(sign_document(wrong, key_id, seed), installer, assets,
                       layout=hu.LAYOUT_GITHUB)
            raise AssertionError("stage_feed must refuse a mismatched GitHub path")
        except SystemExit:
            pass
        _write_json(gh_manifest, gh_document)

        # -- end to end: the shipped client against a GitHub-shaped loopback --
        # The real Updater, the real urllib opener and redirect handler, the
        # feed reached through HEARTH_UPDATE_FEED exactly as an operator would
        # test a release before publishing it. check -> download -> verify ->
        # a staged receipt the shell would accept.
        server = serve(assets, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            base = "http://127.0.0.1:{}/".format(port)
            # latest/ redirects to the versioned path, which redirects to the
            # asset, the way GitHub does.
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                opener.open(base + "latest/download/manifest-stable.json", timeout=5)
                raise AssertionError("latest/download must redirect")
            except urllib.error.HTTPError as exc:
                assert exc.code == 302, exc.code
                assert exc.headers["Location"] == "/download/v0.2.0/manifest-stable.json"
            try:
                urllib.request.urlopen(base + "download/v0.1.0/manifest-stable.json",
                                       timeout=5)
                raise AssertionError("an unknown tag must 404")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, exc.code

            data_dir = os.path.join(tmp, "client-data")
            env = {"HEARTH_DATA_DIR": data_dir, hu.ENV_VERSION: "0.1.0",
                   hu.ENV_FEED: base}
            updater = hu.Updater(trust=_read_json(gh_trust), env=env,
                                 disk_fn=lambda _p: 10 ** 12)
            snap = updater.check_once(force=True)
            assert snap["state"] == hu.STATE_AVAILABLE, snap
            assert snap["available"]["version"] == "0.2.0", snap
            snap = updater.download_once()
            assert snap["state"] == hu.STATE_READY, snap
            staged = snap["staged"]
            # What desktop/tauri/src/update.rs verify() checks, in the same
            # order: inside the staging root it derives itself, an .exe that
            # exists, a version newer than the running one, and a size and
            # SHA-256 recomputed from the file on disk.
            root = os.path.realpath(os.path.join(data_dir, "update", "staged"))
            real = os.path.realpath(staged["path"])
            assert real.lower().startswith(root.lower() + os.sep), (real, root)
            assert real.lower().endswith(".exe") and os.path.isfile(real)
            assert hu.parse_version(staged["version"]) > hu.parse_version("0.1.0")
            assert os.path.getsize(real) == staged["size_bytes"] == len(payload)
            assert hu.sha256_file(real) == staged["sha256"] == hashlib.sha256(payload).hexdigest()
            assert staged["signed_by"] == "test-key", staged
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)

    print("release-manifest self-test OK")
    return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """For the self-test: see a redirect instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    sys.exit(main())
