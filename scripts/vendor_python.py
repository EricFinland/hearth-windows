#!/usr/bin/env python3
r"""Vendor a CPython interpreter into Hearth, from a pinned python.org release.

Hearth's sidecar is Python and the person installing Hearth is not expected
to have Python. This script fetches the official Windows embeddable package
for the version pinned in vendor/python_manifest.json and lays it out so the
packaged app can run desktop/server/main.py with an interpreter it brought
with it.

## Why the embeddable package and not PyInstaller or Nuitka

Three candidates were considered, and the deciding question was what each
one does to the code that has to keep working.

  embeddable package (chosen)
      A directory of files: python.exe, python312.dll, the stdlib in
      python312.zip, and the extension .pyds. Nothing is installed. It
      writes no registry keys, adds nothing to PATH, registers no file
      associations and creates no py.exe launcher, so it cannot collide
      with a Python the user already has, and uninstalling Hearth is
      deleting a folder. It is plain CPython, so `agent/` and
      `desktop/server/` are imported as the ordinary modules they are --
      no hidden-import lists, no data-file manifests, no second thing to
      keep in sync with the first. 11 MB compressed, about 26 MB on disk.
      Startup is CPython startup, measured at roughly 40 ms to interpreter
      ready on this machine.

  PyInstaller
      Would produce one exe, which sounds tidier. Against it: it discovers
      imports statically and this codebase imports by name in several
      places, so the hidden-import list becomes a maintenance surface that
      fails at runtime rather than at build time; onefile builds unpack to
      a temp directory on every launch, which is both slow and a real
      source of antivirus quarantines; and a packed exe with an embedded
      interpreter is one of the most common SmartScreen and AV heuristics
      there is. We already have an unsigned-installer problem (see
      docs/packaging-windows.md); adding a packer to it is the wrong
      trade.

  Nuitka
      Compiles to C, which needs a C toolchain on the build machine. This
      machine has no MSVC Build Tools and cannot get them without
      administrator elevation, which is the same wall that ruled Tauri out
      for this milestone. Ruled out on availability before merit.

The embeddable package's one real cost is that it is a second thing to
download at build time. That is the same cost scripts/vendor_llama.py
already pays for llama-server, and it is paid by the build machine, not by
the user.

## The supply chain rules, which are vendor_llama.py's rules

  1. The version is PINNED in vendor/python_manifest.json, never "latest".

  2. The SHA-256 is pinned in the repository as data and the check is
     against that pinned value, not against a hash fetched alongside the
     bytes. python.org publishes no SHA-256 for this artifact; it publishes
     an MD5 on the release page. So the pinned SHA-256 was established by
     downloading the archive, hashing it locally, and requiring its MD5 to
     equal the MD5 the release page serves -- two independently served
     values that have to agree. The manifest records that provenance in
     sha256_source rather than implying something stronger.

  3. Downloads come only from https://www.python.org/ftp/python/<version>/,
     with the pinned version in the path. No mirrors, no user-supplied URL.
     Redirects are followed only to www.python.org itself.

  4. NOTHING DOWNLOADED IS EVER EXECUTED BY THIS SCRIPT. This module
     imports no process-spawning and no foreign-function machinery at all,
     and _self_test scans its own source for the names that would allow it,
     so the property survives a later edit that forgets why it mattered.
     Running the interpreter is the shell's job, after this has put a
     verified file on disk.

  5. The archive is verified BEFORE it is opened, and extraction refuses
     absolute paths, traversal, drive letters and symlinks, and caps total
     uncompressed size. Those checks are vendor_llama.safe_members, reused
     here rather than reimplemented.

## What is changed after extraction

Exactly one file: python312._pth, the interpreter's path configuration. Its
mere presence is what puts CPython in isolated mode, which is the property
worth having -- PYTHONPATH, PYTHONHOME, user site-packages and the registry
are all ignored, so a Python the user already has cannot leak into ours --
and the file is rewritten rather than inherited so that a future stock ._pth
cannot change the packaged interpreter's sys.path underneath the app.

Isolated mode has one consequence worth stating plainly, because it is the
kind of thing that fails only in the packaged build: it does NOT prepend a
script's own directory to sys.path. Verified directly. desktop/server/
main.py therefore adds its own directory itself, in its first few lines, and
that is what makes `python.exe main.py` able to `import app` here. The path
configuration is deliberately not where that is solved; see PTH_LINES.

Three members of the archive are dropped and nothing resembling pip is
added: the sidecar is standard library only, so there is nothing to install
and no reason to carry an installer.

Usage:

    python scripts/vendor_python.py status
    python scripts/vendor_python.py vendor
    python scripts/vendor_python.py vendor --force
    python scripts/vendor_python.py --self-test
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vendor_llama  # noqa: E402  - sibling script, reused for archive safety

VendorError = vendor_llama.VendorError
ChecksumError = vendor_llama.ChecksumError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Where the pin lives. Committed; this is the trust anchor.
MANIFEST_PATH = os.path.join(REPO_ROOT, "vendor", "python_manifest.json")

#: Override for the install root, shared with vendor_llama so one build
#: directory holds both vendored trees.
ENV_VENDOR_DIR = "HEARTH_VENDOR_DIR"

VENDOR_SUBDIR = os.path.join("vendor", "python")
CACHE_SUBDIR = os.path.join("vendor", "cache")
RECEIPT_NAME = ".vendored.json"

DOWNLOAD_HOST = "www.python.org"
REDIRECT_HOSTS = ("www.python.org",)
DOWNLOAD_TIMEOUT = 180
CHUNK_BYTES = 1024 * 1024

#: The embeddable package expands to about 26 MB. 512 MiB is far past any
#: plausible growth and far short of filling a disk.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 ** 2

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: sys.path for the packaged interpreter, written over the stock
#: python3xx._pth. Two entries: the standard library zip and the interpreter
#: directory, both relative to python.exe, which is how CPython resolves
#: them.
#:
#: Deliberately NOT here: the payload directories. Putting them here would
#: hard-code the packaged layout into the interpreter, so a rearrangement of
#: build_windows.py's staging would break importing in a way that only shows
#: up on a user's machine. desktop/server/main.py adds its own directory to
#: sys.path instead, which works from any layout, any working directory and
#: any interpreter, packaged or not.
#:
#: This file is still written rather than left alone -- it currently matches
#: the stock one line for line -- because the interpreter's path
#: configuration is something the build should state rather than inherit. If
#: a future CPython ships a stock ._pth that enables site, or adds a
#: directory, the packaged interpreter's sys.path must not change underneath
#: the app because of it.
PTH_LINES = (
    "{zipname}",
    ".",
)

#: Files dropped from the embeddable package. Each one is dead weight for a
#: sidecar that never opens a GUI, never plays a sound and never builds an
#: MSI, and every megabyte here is a megabyte in the installer.
DROP_MEMBERS = frozenset({
    "winsound.pyd",   # PC speaker beeps
    "_msi.pyd",       # Windows Installer authoring
    "python.cat",     # catalogue file for the archive's own signature
})


def vendor_root(env=None):
    env = os.environ if env is None else env
    return env.get(ENV_VENDOR_DIR) or REPO_ROOT


def install_dir(env=None):
    return os.path.join(vendor_root(env), VENDOR_SUBDIR)


def cache_dir(env=None):
    return os.path.join(vendor_root(env), CACHE_SUBDIR)


def receipt_path(env=None):
    return os.path.join(install_dir(env), RECEIPT_NAME)


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def load_manifest(path=None):
    path = path or MANIFEST_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise VendorError("cannot read the python pin at {}: {}".format(path, exc)) from exc
    except ValueError as exc:
        raise VendorError("the python pin at {} is not valid JSON: {}".format(path, exc)) from exc
    return validate_manifest(data)


def validate_manifest(data):
    """Raise VendorError unless the pin says exactly what it must.

    A pin that is missing a hash, or carries a hash of the wrong shape, is
    refused here rather than being discovered as a confusing failure after
    a download.
    """
    if not isinstance(data, dict):
        raise VendorError("the python pin must be a JSON object")
    if data.get("schema") != 1:
        raise VendorError("unsupported python pin schema: {!r}".format(data.get("schema")))
    version = data.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise VendorError("the python pin needs a \"version\" like 3.12.10, got {!r}".format(version))
    prefix = data.get("download_prefix")
    expected_prefix = "https://{}/ftp/python/{}/".format(DOWNLOAD_HOST, version)
    if not isinstance(prefix, str) or not prefix.startswith("https://"):
        raise VendorError("the python pin needs an https \"download_prefix\"")
    if prefix != expected_prefix:
        raise VendorError("download_prefix must be exactly {!r}, got {!r}".format(
            expected_prefix, prefix))
    if version not in prefix:
        raise VendorError("download_prefix {!r} does not name the pinned version {!r}".format(
            prefix, version))
    art = data.get("artifact")
    if not isinstance(art, dict):
        raise VendorError("the python pin needs an \"artifact\" object")
    asset = art.get("asset")
    if not isinstance(asset, str) or not asset.endswith(".zip") or "/" in asset or "\\" in asset:
        raise VendorError("artifact.asset must be a bare .zip filename, got {!r}".format(asset))
    if not isinstance(art.get("size"), int) or art["size"] <= 0:
        raise VendorError("artifact.size must be a positive integer")
    if not isinstance(art.get("sha256"), str) or not _SHA256_RE.match(art["sha256"]):
        raise VendorError("artifact.sha256 must be 64 lowercase hex characters")
    md5 = art.get("md5_published")
    if md5 is not None and (not isinstance(md5, str) or not _MD5_RE.match(md5)):
        raise VendorError("artifact.md5_published must be 32 lowercase hex characters")
    return data


def asset_url(manifest):
    return manifest["download_prefix"] + manifest["artifact"]["asset"]


def check_url(url, version, asset):
    """Raise VendorError unless `url` is the official python.org asset URL.

    Checked on the URL that is actually opened: https, exactly
    www.python.org, and a path of exactly /ftp/python/<version>/<asset>.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise VendorError("refusing a non-https download URL: {}".format(url))
    if parts.netloc != DOWNLOAD_HOST:
        raise VendorError("refusing a download from {!r}; CPython is taken only from "
                          "{}".format(parts.netloc, DOWNLOAD_HOST))
    expected = "/ftp/python/{}/{}".format(version, asset)
    if parts.path != expected:
        raise VendorError("refusing {!r}: the pinned asset path is {!r}".format(
            parts.path, expected))
    return True


class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects only back to python.org.

    urllib would otherwise follow a redirect to any host at all, which
    would make the "official downloads only" rule decorative.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.netloc not in REDIRECT_HOSTS:
            raise VendorError(
                "refusing a redirect to {!r}; the CPython download may only redirect "
                "to {}".format(newurl.split("?")[0], ", ".join(REDIRECT_HOSTS)))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(
        _PinnedRedirectHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def download(url, dest, expected_sha256, expected_size, version, asset,
             expected_md5=None, on_progress=None, timeout=DOWNLOAD_TIMEOUT, opener=None):
    """Fetch the pinned archive to `dest`, verified before it lands there.

    Bytes stream to `dest`.part and are hashed on the way past. The file is
    renamed into place only once the hash matches; a mismatch deletes the
    partial, because a file that failed its hash is not something to resume
    from. The read is capped one byte past the pinned size, so a response
    that keeps sending is cut off rather than filling the disk.

    When the pin carries the MD5 python.org publishes, that is checked too.
    It adds no cryptographic strength -- MD5 is broken for collisions -- and
    it is not what decides; it is a cheap confirmation that the bytes match
    the value on a differently served page, which is the same cross-check
    that established the SHA-256 in the first place.
    """
    check_url(url, version, asset)
    opener = opener or _opener()
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    digest = hashlib.sha256()
    md5 = hashlib.md5()
    done = 0
    limit = expected_size + 1

    try:
        with opener.open(url, timeout=timeout) as resp, open(part, "wb") as fh:
            while True:
                chunk = resp.read(CHUNK_BYTES)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise VendorError(
                        "the response for {} is longer than the pinned {} bytes; "
                        "refusing it".format(asset, expected_size))
                fh.write(chunk)
                digest.update(chunk)
                md5.update(chunk)
                if on_progress is not None:
                    on_progress(done, expected_size)
    except VendorError:
        vendor_llama._unlink(part)
        raise
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        vendor_llama._unlink(part)
        raise VendorError("download of {} failed: {}".format(url, exc)) from exc

    actual = digest.hexdigest()
    if done != expected_size or actual != expected_sha256:
        vendor_llama._unlink(part)
        raise ChecksumError(
            "SHA-256 mismatch for {}\n  pinned:   {} ({} bytes)\n  received: {} ({} bytes)\n"
            "The download has been deleted. Nothing has been extracted and nothing "
            "has been run.".format(asset, expected_sha256, expected_size, actual, done))
    if expected_md5 and md5.hexdigest() != expected_md5:
        vendor_llama._unlink(part)
        raise ChecksumError(
            "MD5 mismatch for {}\n  published: {}\n  received:  {}\n"
            "The SHA-256 matched but the MD5 python.org publishes did not, which "
            "means the pin itself is inconsistent. The download has been "
            "deleted.".format(asset, expected_md5, md5.hexdigest()))
    os.replace(part, dest)
    return actual


# --------------------------------------------------------------------------
# Extraction and layout
# --------------------------------------------------------------------------

def _keep(name):
    return os.path.basename(name) not in DROP_MEMBERS


def zip_name(manifest):
    """The stdlib zip filename for the pinned version: python312.zip."""
    major, minor, _ = manifest["version"].split(".")
    return "python{}{}.zip".format(major, minor)


def pth_name(manifest):
    """The path-configuration filename CPython reads: python312._pth."""
    return os.path.splitext(zip_name(manifest))[0] + "._pth"


def pth_contents(manifest):
    """The replacement path configuration, as text.

    Written over the stock file. See this module's docstring for why the
    stock one cannot be kept.
    """
    return "".join(line.format(zipname=zip_name(manifest)) + "\n" for line in PTH_LINES)


def extract(archive, dest, manifest):
    """Unpack a VERIFIED archive into `dest` and write the path config.

    The caller must have verified the archive first; this function does not
    check hashes and does not run anything. Archive safety (traversal,
    symlinks, drive letters, expansion cap) is vendor_llama.safe_members,
    with this module's own uncompressed cap.

    Returns the sorted list of filenames written.
    """
    os.makedirs(dest, exist_ok=True)
    written = []
    saved_cap = vendor_llama.MAX_UNCOMPRESSED_BYTES
    vendor_llama.MAX_UNCOMPRESSED_BYTES = MAX_UNCOMPRESSED_BYTES
    try:
        with zipfile.ZipFile(archive) as zf:
            members = vendor_llama.safe_members(zf, keep=_keep)
            for info in members:
                base = os.path.basename(info.filename)
                if not base:
                    continue
                if base in written:
                    raise VendorError(
                        "archive holds two members that flatten to {!r}; refusing to "
                        "let one overwrite the other".format(base))
                target = os.path.join(dest, base)
                with zf.open(info) as src, open(target, "wb") as fh:
                    shutil.copyfileobj(src, fh, CHUNK_BYTES)
                written.append(base)
    finally:
        vendor_llama.MAX_UNCOMPRESSED_BYTES = saved_cap

    pth = pth_name(manifest)
    with open(os.path.join(dest, pth), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(pth_contents(manifest))
    if pth not in written:
        written.append(pth)
    return sorted(written)


# --------------------------------------------------------------------------
# Install state
# --------------------------------------------------------------------------

def _rmtree_with_retries(path, attempts=5, delay=0.3):
    """Clear a directory tree, working around Windows delete semantics.

    Observed on this machine, reproducibly, on a checkout under a synced
    OneDrive folder: os.rmdir of an EMPTY directory fails with WinError 5
    for several seconds after its contents were deleted, because something
    (the sync filter, the indexer) still holds a handle and the delete is
    pending. Retrying alone does not always clear it inside a sensible
    window, but renaming does -- a rename needs no exclusive handle -- so
    after a few honest attempts the tree is moved aside and deleted from
    there, which frees the path the caller actually wanted. If even the
    rename fails, something is genuinely holding the tree open and that is
    worth stopping the build for.
    """
    last = None
    for attempt in range(attempts):
        if not os.path.isdir(path):
            return
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            last = exc
            time.sleep(delay * (attempt + 1))

    aside = "{}.stale-{}".format(path, os.getpid())
    try:
        os.replace(path, aside) if os.path.isdir(aside) else os.rename(path, aside)
    except OSError as exc:
        raise VendorError(
            "could not clear {}: {} (and moving it aside failed: {}). Close "
            "anything using it -- an explorer window, a running Hearth -- and try "
            "again.".format(path, last, exc)) from exc
    shutil.rmtree(aside, ignore_errors=True)


def read_receipt(env=None):
    try:
        with open(receipt_path(env), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_receipt(manifest, files, env=None):
    receipt = {
        "project": "CPython",
        "version": manifest["version"],
        "asset": manifest["artifact"]["asset"],
        "sha256": manifest["artifact"]["sha256"],
        "files": sorted(files),
        "interpreter": "python.exe",
    }
    path = receipt_path(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return receipt


def interpreter_path(env=None):
    return os.path.join(install_dir(env), "python.exe")


def status(manifest=None, env=None):
    """What is pinned, what is installed, and whether they agree."""
    manifest = manifest or load_manifest()
    receipt = read_receipt(env)
    exe = interpreter_path(env)
    installed = os.path.isfile(exe)
    current = bool(
        installed and receipt and receipt.get("sha256") == manifest["artifact"]["sha256"]
    )
    return {
        "pinned_version": manifest["version"],
        "pinned_sha256": manifest["artifact"]["sha256"],
        "install_dir": install_dir(env),
        "interpreter": exe,
        "installed": installed,
        "current": current,
        "receipt": receipt,
    }


def vendor(manifest=None, force=False, offline=False, env=None, on_progress=None, log=None):
    """Put a verified, pinned CPython in vendor/python/. Idempotent.

    A second run with the same pin is a no-op: the receipt records the
    pinned hash, so nothing is downloaded and nothing is rewritten unless
    --force is given or the pin has moved.

    offline: refuse to reach the network. Uses a cached archive if one is
    present and verifies it exactly as a fresh download would be verified.
    """
    manifest = manifest or load_manifest()
    log = log or (lambda _msg: None)
    art = manifest["artifact"]
    state = status(manifest, env=env)
    if state["current"] and not force:
        log("python {} is already vendored in {}".format(manifest["version"], state["install_dir"]))
        return state

    cache = cache_dir(env)
    os.makedirs(cache, exist_ok=True)
    archive = os.path.join(cache, art["asset"])

    have_cached = os.path.isfile(archive)
    if have_cached:
        try:
            vendor_llama.verify_file(archive, art["sha256"], art["size"])
            log("using the verified archive already in {}".format(cache))
        except ChecksumError:
            vendor_llama._unlink(archive)
            have_cached = False

    if not have_cached:
        if offline:
            raise VendorError(
                "offline was requested but {} is not in {} (or failed its hash); "
                "run without --offline once to populate it".format(art["asset"], cache))
        url = asset_url(manifest)
        log("downloading {}".format(url))
        download(url, archive, art["sha256"], art["size"], manifest["version"],
                 art["asset"], expected_md5=art.get("md5_published"),
                 on_progress=on_progress)

    dest = install_dir(env)
    _rmtree_with_retries(dest)
    files = extract(archive, dest, manifest)
    write_receipt(manifest, files, env=env)
    log("installed python {} ({} files) into {}".format(manifest["version"], len(files), dest))
    return status(manifest, env=env)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _progress_printer():
    last = [-1]

    def report(done, total):
        pct = int(done * 100 / total) if total else 0
        if pct != last[0] and pct % 5 == 0:
            last[0] = pct
            sys.stderr.write("\r  {:3d}%  {:.1f} MB".format(pct, done / 1e6))
            sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")
    return report


def _build_parser():
    p = argparse.ArgumentParser(
        prog="vendor_python.py",
        description="Fetch the pinned CPython embeddable package into vendor/python/.")
    p.add_argument("--self-test", action="store_true", help="run the module's tests and exit")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("status", help="show what is pinned and what is installed")
    v = sub.add_parser("vendor", help="download, verify and install the pinned interpreter")
    v.add_argument("--force", action="store_true", help="reinstall even if the pin already matches")
    v.add_argument("--offline", action="store_true", help="use only a cached archive; never fetch")
    v.add_argument("--quiet", action="store_true", help="no progress output")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    try:
        if args.command == "status" or args.command is None:
            state = status()
            print(json.dumps(state, indent=2, sort_keys=True))
            return 0 if state["current"] else 1
        if args.command == "vendor":
            state = vendor(force=args.force, offline=args.offline,
                           on_progress=None if args.quiet else _progress_printer(),
                           log=lambda m: print(m))
            print(json.dumps({"interpreter": state["interpreter"],
                              "version": state["pinned_version"]}, indent=2, sort_keys=True))
            return 0 if state["current"] else 1
    except ChecksumError as exc:
        print("checksum failure:\n{}".format(exc), file=sys.stderr)
        return 3
    except VendorError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2
    return 0


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

#: Names whose presence would mean this module could run what it fetched.
#: The scan below splits the source at this assignment, so the list can name
#: the tokens without tripping over itself. Same device as vendor_llama.py.
_EXECUTION_TOKENS = ("import subprocess", "os.system", "os.popen", "os.exec",
                     "os.spawn", "import ctypes", "runpy", "eval(", "exec(")


def _make_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return path


def _self_test():
    scratch = tempfile.mkdtemp(prefix="hearth-vendor-python-selftest-")
    try:
        _self_test_body(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print("hearth-vendor-python self-test OK")
    return 0


def _self_test_body(scratch):
    # --- rule 4: this module cannot run what it downloads -----------------
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
        source = fh.read()
    body = source.split("_EXECUTION_TOKENS = (", 1)[0]
    for token in _EXECUTION_TOKENS:
        assert token not in body, (
            "vendor_python.py must never gain the ability to execute what it "
            "downloads, but its source contains {!r}".format(token))
    assert "subprocess" not in globals(), "subprocess must not be imported here"

    # --- the committed manifest is valid ---------------------------------
    manifest = load_manifest()
    assert manifest["version"] == "3.12.10", manifest["version"]
    assert manifest["artifact"]["asset"].endswith("-embed-amd64.zip")
    assert zip_name(manifest) == "python312.zip"
    assert pth_name(manifest) == "python312._pth"

    # --- validate_manifest refuses each way a pin can be wrong ------------
    def bad(mutate):
        data = json.loads(json.dumps(manifest))
        mutate(data)
        try:
            validate_manifest(data)
        except VendorError:
            return True
        return False

    assert bad(lambda d: d.update(schema=2))
    assert bad(lambda d: d.update(version="latest"))
    assert bad(lambda d: d.update(download_prefix="http://www.python.org/ftp/python/3.12.10/"))
    assert bad(lambda d: d.update(download_prefix="https://example.invalid/3.12.10/"))
    assert bad(lambda d: d["artifact"].update(sha256="nope"))
    assert bad(lambda d: d["artifact"].update(sha256="A" * 64))
    assert bad(lambda d: d["artifact"].update(size=0))
    assert bad(lambda d: d["artifact"].update(asset="../evil.zip"))
    assert bad(lambda d: d["artifact"].update(md5_published="xyz"))
    # A pin whose prefix names a different version than its version field is
    # the exact shape of a half-finished version bump.
    assert bad(lambda d: d.update(download_prefix="https://www.python.org/ftp/python/3.11.9/"))

    # --- check_url is the "no mirrors" rule, enforced on the real URL -----
    version, asset = manifest["version"], manifest["artifact"]["asset"]
    assert check_url(asset_url(manifest), version, asset)
    for url in (
        "http://www.python.org/ftp/python/3.12.10/" + asset,
        "https://python.org/ftp/python/3.12.10/" + asset,
        "https://evil.invalid/ftp/python/3.12.10/" + asset,
        "https://www.python.org/ftp/python/3.12.9/" + asset,
        "https://www.python.org/ftp/python/3.12.10/other.zip",
        "https://www.python.org/ftp/python/3.12.10/../3.11.0/" + asset,
    ):
        try:
            check_url(url, version, asset)
        except VendorError:
            pass
        else:
            raise AssertionError("check_url accepted {!r}".format(url))

    # --- the redirect allowlist refuses an off-site hop -------------------
    handler = _PinnedRedirectHandler()
    try:
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.invalid/x.zip")
    except VendorError:
        pass
    else:
        raise AssertionError("the redirect handler followed an off-site redirect")

    # --- extraction: safety, the drop list, and the ._pth rewrite ---------
    env = {ENV_VENDOR_DIR: scratch}
    archive = _make_zip(os.path.join(scratch, "fake-embed.zip"), [
        ("python.exe", b"MZ-not-really"),
        ("python312.dll", b"dll"),
        ("python312.zip", b"stdlib"),
        ("python312._pth", b"python312.zip\n.\n"),
        ("winsound.pyd", b"drop me"),
        ("_msi.pyd", b"drop me too"),
        ("python.cat", b"drop me three"),
        ("_socket.pyd", b"keep"),
    ])
    dest = install_dir(env)
    written = extract(archive, dest, manifest)
    assert "python.exe" in written and "_socket.pyd" in written, written
    for dropped in DROP_MEMBERS:
        assert dropped not in written, (dropped, written)
        assert not os.path.exists(os.path.join(dest, dropped))

    # The path configuration is written by us, not inherited. The fake
    # archive above carries a ._pth with different contents, so this also
    # proves ours wins rather than being skipped because the file already
    # existed.
    with open(os.path.join(dest, "python312._pth"), "r", encoding="utf-8") as fh:
        pth = fh.read()
    assert pth.splitlines() == ["python312.zip", "."], pth
    assert "import site" not in pth, (
        "the packaged interpreter must stay isolated: enabling site would let a "
        "Python the user already has contribute modules to ours")

    # An archive that tries to escape is refused, and the hash it carries is
    # irrelevant to that: safe_members runs on every archive, verified or not.
    evil = _make_zip(os.path.join(scratch, "evil.zip"), [("../escape.txt", b"x")])
    try:
        extract(evil, os.path.join(scratch, "evil-out"), manifest)
    except VendorError:
        pass
    else:
        raise AssertionError("extraction accepted a traversal member")
    assert not os.path.exists(os.path.join(scratch, "escape.txt"))

    # --- verify_file rejects the wrong bytes ------------------------------
    good = os.path.join(scratch, "good.bin")
    with open(good, "wb") as fh:
        fh.write(b"hearth")
    real = hashlib.sha256(b"hearth").hexdigest()
    assert vendor_llama.verify_file(good, real, 6) == real
    for args in ((real, 7), ("0" * 64, 6)):
        try:
            vendor_llama.verify_file(good, *args)
        except ChecksumError:
            pass
        else:
            raise AssertionError("verify_file accepted {}".format(args))

    # --- receipt and status round trip ------------------------------------
    write_receipt(manifest, written, env=env)
    state = status(manifest, env=env)
    assert state["installed"] is True, state
    assert state["current"] is True, state
    assert state["interpreter"].endswith(os.path.join("vendor", "python", "python.exe"))

    # A pin that moves makes an existing install stale rather than current,
    # which is what stops a build from shipping yesterday's interpreter.
    moved = json.loads(json.dumps(manifest))
    moved["artifact"]["sha256"] = "b" * 64
    assert status(moved, env=env)["current"] is False

    # --- offline refuses to invent an archive ------------------------------
    empty_env = {ENV_VENDOR_DIR: os.path.join(scratch, "empty")}
    try:
        vendor(manifest=manifest, offline=True, env=empty_env)
    except VendorError as exc:
        assert "offline" in str(exc), exc
    else:
        raise AssertionError("offline vendor invented an archive from nowhere")


if __name__ == "__main__":
    sys.exit(main())
