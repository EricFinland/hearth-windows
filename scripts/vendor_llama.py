#!/usr/bin/env python3
r"""Vendor llama.cpp's llama-server into Hearth, from a pinned release.

Hearth's promise is that the user installs exactly one thing. hearth_llama
already knows how to find, launch and supervise `llama-server`; what it
never had was a binary to find. This script is the other half: it fetches
one official llama.cpp release build and puts it where
hearth_llama.find_server() already looks (`vendor/llama/`, the last entry in
hearth_llama.BUNDLED_SUBDIRS), so no discovery code had to change.

## This is a supply chain, so it is treated as one

Five rules, each of which exists because the obvious shortcut is unsound:

  1. The release tag is PINNED, in vendor/llama_manifest.json, and is never
     "latest". llama.cpp cuts a tagged release per merged pull request, so
     "latest" means a different binary every hour and a build nobody can
     reason about after the fact.

  2. The SHA-256 is pinned in the repository as data, and the check is
     against that pinned value. A checksum fetched in the same operation as
     the artifact proves only that the file matches itself: whoever can
     serve you the bytes can serve you the hash of those bytes. The pinned
     hashes here were produced by `pin` (see below), which downloads the
     asset, hashes the bytes locally, and requires that hash to equal the
     digest GitHub's API publishes for the asset. Those are two independent
     responses from two different hosts, and both must agree before a hash
     is written into the manifest.

     Not every variant in the manifest was downloaded. Each one records
     `sha256_source`: "downloaded_and_hashed" means the bytes were fetched
     and hashed on the machine that pinned them and matched the API digest;
     "github_api_digest" means only GitHub's published digest was recorded,
     which is weaker and is labelled as such rather than implied to be the
     same thing.

  3. Downloads come only from github.com/ggml-org/llama.cpp/releases, over
     https, with the exact pinned tag in the path. No mirrors, no
     third-party rehosts, no user-supplied URL. GitHub redirects release
     assets to release-assets.githubusercontent.com, so that host is
     allowed for redirects only, and every other redirect target is
     refused rather than followed.

  4. NOTHING DOWNLOADED IS EVER EXECUTED BY THIS SCRIPT. Not to read a
     version, not to test it. This module imports no process-spawning and
     no foreign-function machinery at all, and _self_test asserts that by
     scanning its own source for the names that would allow it, so the
     property survives a later edit that forgets why it mattered.
     Verification therefore always precedes anything that treats the file
     as code, because there is nothing here that treats it as code.
     Running the binary is hearth_llama's job, after this has put a
     verified file on disk.

  5. The archive is verified BEFORE it is opened. A zip is parsed by code,
     and unpacking an unverified archive is running attacker input through
     a parser. Extraction additionally refuses absolute paths, parent
     traversal, drive letters and symlinks, and caps total uncompressed
     size, because a matching hash proves the bytes came from the pinned
     release and nothing more.

## Which variant Hearth ships

llama.cpp publishes eleven Windows artifacts per release (see the manifest
for the full pinned list at b10105). Sizes, measured from the release
metadata rather than assumed:

    win-cpu-x64                18.2 MB   works on any x64 Windows
    win-cpu-arm64              12.1 MB   Windows on ARM
    win-vulkan-x64             33.5 MB   NVIDIA, AMD and Intel GPUs
    win-cuda-13.3-x64         146.3 MB   NVIDIA, plus 391 MB of cudart
    win-cuda-12.4-x64         250.2 MB   NVIDIA, plus 391 MB of cudart
    win-hip-radeon-x64        320.9 MB   AMD only
    win-sycl-x64              119.7 MB   Intel only
    win-openvino-x64           80.4 MB   Intel only
    win-opencl-adreno-arm64    12.8 MB   Qualcomm Adreno only

Hearth bundles win-cpu-x64 and fetches a GPU build later. The reasoning:

  * A CPU build is the only one that cannot fail to start. The Vulkan build
    links vulkan-1.dll, the CUDA build links the CUDA runtime; on a machine
    without the matching driver those are load-time failures of the
    executable itself, which hearth_llama would report as a broken install
    rather than as slow inference. Shipping the floor means the engine
    always runs, and "your GPU is not being used" is a message rather than
    a crash.
  * CUDA is the fastest path on the NVIDIA cards most Hearth users have,
    and it is also 537 MB with its cudart companion. That is thirty times
    the CPU build, downloaded by every user including the ones with no
    NVIDIA card at all. It does not belong in an installer.
  * CUDA is also not one choice. The 12.4 build does not cover Blackwell
    (RTX 50-series), which needs a newer toolkit; the 13.3 build does.
    Picking between them requires knowing the card, which an installer
    does not know until it runs on the machine.
  * Vulkan at 33.5 MB is the best value for a first-run GPU fetch: one
    artifact covers NVIDIA, AMD and Intel, and it is smaller than any
    vendor-specific build. It is slower than CUDA on NVIDIA, so CUDA stays
    available as an explicit upgrade for people who want it.
  * Shipping every variant would be roughly 1 GB of installer to deliver
    one working binary.

So the recommendation a future installer should implement is: bundle
win-cpu-x64, and on first run detect the GPU (hearth_hw.gpus() already
does) and fetch one further variant, all of them already pinned here:

    NVIDIA, Blackwell or newer -> win-cuda-13.3-x64 + its cudart
    NVIDIA, older              -> win-cuda-13.3-x64 or win-vulkan-x64
    AMD or Intel               -> win-vulkan-x64
    no GPU, or fetch failed    -> keep the bundled CPU build

Every one of those fetches goes through this same script and the same
pinned hashes, so the first-run path is not a second, weaker supply chain.

## Layout

    vendor/llama_manifest.json   committed. The pin: tag, assets, hashes.
    vendor/llama/                gitignored. The extracted binaries.
    vendor/llama/.vendored.json  gitignored. What is actually installed.
    vendor/cache/                gitignored. Downloaded archives.

vendor/llama/ is chosen because hearth_llama.BUNDLED_SUBDIRS already lists
"vendor/llama", so this required no change to the discovery code.

## Constraints

Python standard library only. Every network call is bounded by a timeout.
Nothing writes outside the repository's vendor/ directory. The default
self-test needs no network and no binary; the tests that need either are
behind --live.
"""

import argparse
import hashlib
import io
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

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Where the pin lives. Committed; this is the trust anchor.
MANIFEST_PATH = os.path.join(REPO_ROOT, "vendor", "llama_manifest.json")

#: Override for the install root, so the self-test never writes to the real
#: vendor tree and a packager can stage into a build directory.
ENV_VENDOR_DIR = "HEARTH_VENDOR_DIR"

#: Extracted binaries. The name matters: hearth_llama.BUNDLED_SUBDIRS
#: already contains "vendor/llama", so find_server() looks here unaided.
VENDOR_SUBDIR = os.path.join("vendor", "llama")
CACHE_SUBDIR = os.path.join("vendor", "cache")

#: Written into the install directory to record what is there. Read back to
#: make a second run a no-op.
RECEIPT_NAME = ".vendored.json"

SERVER_EXE = "llama-server.exe"

#: The only repository releases are fetched from, and the only hosts a
#: redirect may land on. github.com issues a 302 to
#: release-assets.githubusercontent.com for every release asset (verified
#: live); objects.githubusercontent.com is the older host and is kept
#: because GitHub has used both.
UPSTREAM_OWNER = "ggml-org"
UPSTREAM_REPO = "llama.cpp"
DOWNLOAD_HOST = "github.com"
API_HOST = "api.github.com"
REDIRECT_HOSTS = ("github.com", "release-assets.githubusercontent.com",
                  "objects.githubusercontent.com")

DOWNLOAD_TIMEOUT = 120
API_TIMEOUT = 30
CHUNK_BYTES = 1024 * 1024

#: Cap on total uncompressed bytes from one archive. The largest pinned
#: Windows artifact expands to roughly 700 MB, so 2 GiB leaves headroom
#: while still stopping a zip bomb well short of filling a disk.
MAX_UNCOMPRESSED_BYTES = 2 * 1024 ** 3

#: A sha256 as it appears in the manifest.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Release tags are llama.cpp build tags: a "b" and a build number.
_TAG_RE = re.compile(r"^b\d+$")

#: How a hash in the manifest was established. See the module docstring.
SRC_HASHED = "downloaded_and_hashed"
SRC_API = "github_api_digest"

#: Members kept from an archive by default. Hearth runs llama-server and
#: nothing else, but the server loads its backends as DLLs at runtime (the
#: ggml-cpu-*.dll set is a per-ISA dispatch table, not dead weight), so
#: every DLL is kept and the other standalone tools are dropped. Pass --all
#: to keep the archive whole.
def _is_server_member(name):
    """True when an archive member belongs in a server-only install."""
    base = os.path.basename(name).lower()
    if base.endswith(".dll"):
        return True
    return base == SERVER_EXE.lower()


#: The Windows artifacts of a llama.cpp release, classified. This table is
#: code rather than manifest data because it is a judgement about what each
#: build is for; the manifest holds the mechanical facts (asset name, size,
#: hash) that `pin` reads off a specific release. `match` is applied to the
#: asset name with {tag} substituted, because some artifacts carry an
#: upstream toolkit version in the filename that moves between releases.
VARIANT_TABLE = {
    "win-cpu-x64": {
        "match": r"^llama-{tag}-bin-win-cpu-x64\.zip$",
        "os": "windows", "arch": "x86_64", "backend": "cpu",
        "gpu_vendors": [], "requires": [],
        "note": "Runs on any x64 Windows. No GPU offload. Hearth's bundled floor.",
    },
    "win-cpu-arm64": {
        "match": r"^llama-{tag}-bin-win-cpu-arm64\.zip$",
        "os": "windows", "arch": "arm64", "backend": "cpu",
        "gpu_vendors": [], "requires": [],
        "note": "Windows on ARM. No GPU offload.",
    },
    "win-vulkan-x64": {
        "match": r"^llama-{tag}-bin-win-vulkan-x64\.zip$",
        "os": "windows", "arch": "x86_64", "backend": "vulkan",
        "gpu_vendors": ["nvidia", "amd", "intel"], "requires": [],
        "note": ("One artifact covering every desktop GPU vendor. Needs a "
                 "Vulkan-capable driver (vulkan-1.dll). Recommended first-run "
                 "GPU fetch."),
    },
    "win-cuda-13.3-x64": {
        "match": r"^llama-{tag}-bin-win-cuda-13\.3-x64\.zip$",
        "os": "windows", "arch": "x86_64", "backend": "cuda",
        "gpu_vendors": ["nvidia"], "requires": ["cudart-cuda-13.3-x64"],
        "note": ("Fastest on NVIDIA. Covers Blackwell (RTX 50-series). Needs "
                 "the matching cudart archive unless the CUDA 13 runtime is "
                 "already installed."),
    },
    "win-cuda-12.4-x64": {
        "match": r"^llama-{tag}-bin-win-cuda-12\.4-x64\.zip$",
        "os": "windows", "arch": "x86_64", "backend": "cuda",
        "gpu_vendors": ["nvidia"], "requires": ["cudart-cuda-12.4-x64"],
        "note": ("For NVIDIA drivers too old for CUDA 13. Built against a "
                 "toolkit that predates Blackwell, so RTX 50-series cards "
                 "want the 13.3 build instead."),
    },
    "win-hip-radeon-x64": {
        "match": r"^llama-{tag}-bin-win-hip-radeon-x64\.zip$",
        "os": "windows", "arch": "x86_64", "backend": "rocm",
        "gpu_vendors": ["amd"], "requires": [],
        "note": "AMD only, and larger than the Vulkan build that also covers AMD.",
    },
    "win-sycl-x64": {
        "match": r"^llama-{tag}-bin-win-sycl-x64\.zip$",
        "os": "windows", "arch": "x86_64", "backend": "sycl",
        "gpu_vendors": ["intel"], "requires": [],
        "note": "Intel GPUs. Needs the Intel oneAPI runtime.",
    },
    "win-openvino-x64": {
        "match": r"^llama-{tag}-bin-win-openvino-.*-x64\.zip$",
        "os": "windows", "arch": "x86_64", "backend": "openvino",
        "gpu_vendors": ["intel"], "requires": [],
        "note": "Intel only. The filename carries an OpenVINO toolkit version.",
    },
    "win-opencl-adreno-arm64": {
        "match": r"^llama-{tag}-bin-win-opencl-adreno-arm64\.zip$",
        "os": "windows", "arch": "arm64", "backend": "opencl",
        "gpu_vendors": ["qualcomm"], "requires": [],
        "note": "Qualcomm Adreno on Windows on ARM.",
    },
    "cudart-cuda-13.3-x64": {
        "match": r"^cudart-llama-bin-win-cuda-13\.3-x64\.zip$",
        "os": "windows", "arch": "x86_64", "backend": "runtime",
        "gpu_vendors": ["nvidia"], "requires": [],
        "note": ("The CUDA 13 runtime DLLs, not an engine. Extracted beside "
                 "win-cuda-13.3-x64."),
    },
    "cudart-cuda-12.4-x64": {
        "match": r"^cudart-llama-bin-win-cuda-12\.4-x64\.zip$",
        "os": "windows", "arch": "x86_64", "backend": "runtime",
        "gpu_vendors": ["nvidia"], "requires": [],
        "note": ("The CUDA 12 runtime DLLs, not an engine. Extracted beside "
                 "win-cuda-12.4-x64."),
    },
}

#: Carried into a regenerated manifest so `pin` never silently drops the
#: reasoning. Overridden by whatever the existing manifest already says.
DEFAULT_POLICY = {
    "bundled_variant": "win-cpu-x64",
    "bundled_reason": (
        "A CPU build is the only Windows artifact that cannot fail to start: "
        "the GPU builds link vendor runtimes that are absent on machines "
        "without the matching driver, and a missing DLL is a launch failure "
        "rather than slow inference. It is also the smallest x64 artifact."),
    "first_run_gpu_fetch": {
        "nvidia_blackwell_or_newer": "win-cuda-13.3-x64",
        "nvidia_older": "win-cuda-13.3-x64",
        "amd": "win-vulkan-x64",
        "intel": "win-vulkan-x64",
        "other_or_none": None,
    },
    "first_run_reason": (
        "Vulkan is 33.5 MB and covers every desktop GPU vendor, so it is the "
        "cheapest fetch that turns on offload. CUDA is faster on NVIDIA but "
        "is 537 MB with its cudart companion, which is why it is fetched on "
        "demand for NVIDIA hardware rather than bundled for everyone."),
}


class VendorError(RuntimeError):
    """Anything this script refuses to paper over: a hash that does not
    match, an unexpected download host, an archive that tries to escape its
    destination."""


class ChecksumError(VendorError):
    """The bytes on disk are not the bytes that were pinned.

    Its own class because it is the one failure that means the supply chain
    was violated rather than merely inconvenienced, and a caller may want to
    treat it differently from a timeout.
    """


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def vendor_root(env=None):
    """The directory vendor/llama and vendor/cache live under."""
    env = os.environ if env is None else env
    return env.get(ENV_VENDOR_DIR) or REPO_ROOT


def install_dir(env=None):
    """Where the extracted binaries go, and where find_server() looks."""
    return os.path.join(vendor_root(env), VENDOR_SUBDIR)


def cache_dir(env=None):
    """Where downloaded archives are kept, so a re-run does not refetch."""
    return os.path.join(vendor_root(env), CACHE_SUBDIR)


def receipt_path(env=None):
    """The record of what is currently installed."""
    return os.path.join(install_dir(env), RECEIPT_NAME)


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def load_manifest(path=None):
    """The pinned release, read from vendor/llama_manifest.json.

    Validated on load rather than trusted: a manifest with a malformed tag,
    a hash that is not 64 hex characters, or a default variant that is not
    in its own variant table is a broken trust anchor, and failing here is
    better than failing after a download.
    """
    path = path or MANIFEST_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise VendorError("cannot read the pin manifest at {}: {}".format(path, exc)) from exc
    except ValueError as exc:
        raise VendorError("the pin manifest at {} is not valid JSON: {}".format(
            path, exc)) from exc
    validate_manifest(data)
    return data


def validate_manifest(data):
    """Raise VendorError unless `data` is a usable pin. Returns None."""
    if not isinstance(data, dict):
        raise VendorError("the pin manifest must be a JSON object")
    tag = data.get("release_tag")
    if not isinstance(tag, str) or not _TAG_RE.match(tag):
        raise VendorError("release_tag must look like 'b10105', got {!r}".format(tag))
    variants = data.get("variants")
    if not isinstance(variants, dict) or not variants:
        raise VendorError("the pin manifest lists no variants")
    for name, entry in variants.items():
        if not isinstance(entry, dict):
            raise VendorError("variant {!r} is not an object".format(name))
        for key in ("asset", "sha256", "size_bytes", "sha256_source"):
            if key not in entry:
                raise VendorError("variant {!r} is missing {!r}".format(name, key))
        if not _SHA256_RE.match(str(entry["sha256"])):
            raise VendorError("variant {!r} has a sha256 that is not 64 hex characters: "
                              "{!r}".format(name, entry["sha256"]))
        if not isinstance(entry["size_bytes"], int) or entry["size_bytes"] <= 0:
            raise VendorError("variant {!r} has a non-positive size_bytes".format(name))
        if entry["sha256_source"] not in (SRC_HASHED, SRC_API):
            raise VendorError("variant {!r} has an unknown sha256_source {!r}".format(
                name, entry["sha256_source"]))
        if tag not in entry["asset"] and not entry["asset"].startswith("cudart-"):
            raise VendorError("variant {!r} names asset {!r}, which does not carry the "
                              "pinned tag {}".format(name, entry["asset"], tag))
    default = data.get("bundled_variant") or (data.get("policy") or {}).get("bundled_variant")
    if default and default not in variants:
        raise VendorError("bundled_variant {!r} is not in the variant table".format(default))


def bundled_variant(manifest):
    """The variant Hearth ships, per the manifest's own policy."""
    return (manifest.get("bundled_variant")
            or (manifest.get("policy") or {}).get("bundled_variant")
            or DEFAULT_POLICY["bundled_variant"])


def variant_entry(manifest, name):
    """One variant's pinned facts, or a VendorError naming what is available."""
    variants = manifest["variants"]
    if name not in variants:
        raise VendorError("no pinned variant {!r}; the manifest has: {}".format(
            name, ", ".join(sorted(variants))))
    return variants[name]


def asset_url(manifest, entry):
    """The one URL an asset may be fetched from.

    Built from the pinned tag and asset name rather than read out of the
    manifest, so a manifest that has been tampered with to point somewhere
    else cannot redirect a download: the host and path shape are fixed in
    code here and checked again in check_url().
    """
    return "https://{}/{}/{}/releases/download/{}/{}".format(
        DOWNLOAD_HOST, UPSTREAM_OWNER, UPSTREAM_REPO,
        manifest["release_tag"], entry["asset"])


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def check_url(url, tag):
    """Raise VendorError unless `url` is an official release asset URL.

    Checked: https, exactly github.com, and a path of exactly
    /ggml-org/llama.cpp/releases/download/<tag>/<asset> with no traversal
    and no extra segments. This is the rule that says "no mirrors", and it
    is enforced on the URL that is actually opened rather than on the one
    the manifest suggested.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise VendorError("refusing a non-https download URL: {}".format(url))
    if parts.netloc != DOWNLOAD_HOST:
        raise VendorError("refusing a download from {!r}; llama.cpp binaries are taken "
                          "only from {}".format(parts.netloc, DOWNLOAD_HOST))
    prefix = "/{}/{}/releases/download/{}/".format(UPSTREAM_OWNER, UPSTREAM_REPO, tag)
    if not parts.path.startswith(prefix):
        raise VendorError("refusing {!r}: not an asset of the pinned release {}".format(
            parts.path, tag))
    rest = parts.path[len(prefix):]
    if not rest or "/" in rest or ".." in rest:
        raise VendorError("refusing a malformed asset path: {!r}".format(parts.path))
    return True


class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to GitHub's own release asset hosts.

    urllib follows redirects by default and would happily follow one to any
    host at all, which would make the "official releases only" rule
    decorative: an attacker who could inject a 302 would choose the bytes.
    A refused redirect raises, and the download fails loudly.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.netloc not in REDIRECT_HOSTS:
            raise VendorError(
                "refusing a redirect to {!r}; release assets may only redirect "
                "to {}".format(newurl.split("?")[0], ", ".join(REDIRECT_HOSTS)))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    """A urllib opener with the redirect allowlist and certificate checking.

    ssl.create_default_context() is passed explicitly so that verification
    is on regardless of how the interpreter was configured.
    """
    return urllib.request.build_opener(
        _PinnedRedirectHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def sha256_file(path):
    """The SHA-256 of a file on disk, read in bounded chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path, expected_sha256, expected_size=None):
    """Raise ChecksumError unless `path` is exactly the pinned artifact.

    Size is checked first because it is free and because a length mismatch
    is a clearer message than a hash mismatch, but the hash is what decides:
    a truncated file with the right length is still refused.
    """
    try:
        actual_size = os.path.getsize(path)
    except OSError as exc:
        raise ChecksumError("cannot read {} to verify it: {}".format(path, exc)) from exc
    if expected_size is not None and actual_size != expected_size:
        raise ChecksumError(
            "{} is {} bytes but the pin says {}; refusing it".format(
                path, actual_size, expected_size))
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ChecksumError(
            "SHA-256 mismatch for {}\n  pinned:   {}\n  on disk:  {}\n"
            "The bytes are not the ones this repository pinned. Nothing has been "
            "extracted and nothing has been run.".format(path, expected_sha256, actual))
    return actual


def download(url, dest, expected_sha256, expected_size, tag, on_progress=None,
             timeout=DOWNLOAD_TIMEOUT, opener=None):
    """Fetch one pinned asset to `dest`, verified before it lands there.

    Bytes go to `dest`.part and are hashed as they stream. The file is
    renamed into place only after the hash matches the pinned value; a
    mismatch deletes the partial, because a file that failed its hash is
    not something to resume from or keep around. The read is capped at the
    pinned size, so a response that keeps sending is cut off rather than
    filling the disk.

    Returns the verified sha256. Raises ChecksumError on a mismatch and
    VendorError on anything else.
    """
    check_url(url, tag)
    opener = opener or _opener()
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    digest = hashlib.sha256()
    done = 0
    limit = expected_size + 1  # one byte past the pin is already too many

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
                        "refusing it".format(os.path.basename(dest), expected_size))
                fh.write(chunk)
                digest.update(chunk)
                if on_progress is not None:
                    on_progress(done, expected_size)
    except VendorError:
        _unlink(part)
        raise
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        _unlink(part)
        raise VendorError("download of {} failed: {}".format(url, exc)) from exc

    actual = digest.hexdigest()
    if done != expected_size or actual != expected_sha256:
        _unlink(part)
        raise ChecksumError(
            "SHA-256 mismatch for {}\n  pinned:   {} ({} bytes)\n  received: {} ({} bytes)\n"
            "The download has been deleted. Nothing has been extracted and nothing "
            "has been run.".format(os.path.basename(dest), expected_sha256,
                                   expected_size, actual, done))
    os.replace(part, dest)
    return actual


def _unlink(path):
    """Delete `path` if it exists. Never raises."""
    try:
        os.unlink(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def safe_members(zf, keep=None):
    """The members of `zf` that may be written, refusing anything unsafe.

    Refused, each with its own message: absolute paths, parent traversal,
    Windows drive letters and UNC prefixes, backslash separators (a zip
    path separator is "/" by spec, so a backslash is either a literal
    filename character or an attempt to sidestep a forward-slash check),
    symlinks, and a total uncompressed size over MAX_UNCOMPRESSED_BYTES.

    A verified hash proves the archive is the one that was pinned. It does
    not prove the archive is well behaved, and upstream's build could
    change shape without anybody here noticing, so the check stays.
    """
    total = 0
    out = []
    for info in zf.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        if name.startswith("/") or name.startswith("\\"):
            raise VendorError("archive member {!r} is an absolute path".format(name))
        if "\\" in name:
            raise VendorError("archive member {!r} contains a backslash".format(name))
        if re.match(r"^[A-Za-z]:", name):
            raise VendorError("archive member {!r} carries a drive letter".format(name))
        parts = name.split("/")
        if ".." in parts or "." in parts:
            raise VendorError("archive member {!r} tries to traverse out of the "
                              "destination".format(name))
        # The high 16 bits of external_attr are the POSIX mode when the
        # archive was made on a POSIX system; 0xA000 is S_IFLNK.
        if (info.external_attr >> 16) & 0xF000 == 0xA000:
            raise VendorError("archive member {!r} is a symlink".format(name))
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise VendorError(
                "archive expands to more than {} bytes; refusing it".format(
                    MAX_UNCOMPRESSED_BYTES))
        if keep is not None and not keep(name):
            continue
        out.append(info)
    return out


def extract(archive, dest, keep=None):
    """Unpack a VERIFIED archive into `dest`, flattening it.

    llama.cpp's Windows archives are flat: every binary sits at the archive
    root with no directory prefix (checked against the real b10105 CPU and
    Vulkan archives, 51 and 52 members, no directories). Members are
    written by basename so that an upstream change to a nested layout still
    lands llama-server.exe where find_server() looks, and safe_members has
    already refused anything that could escape.

    The caller must have verified the archive first. This function does not
    check hashes and does not run anything.

    Returns the sorted list of filenames written.
    """
    os.makedirs(dest, exist_ok=True)
    written = []
    with zipfile.ZipFile(archive) as zf:
        members = safe_members(zf, keep=keep)
        for info in members:
            base = os.path.basename(info.filename)
            if not base:
                continue
            if base in written:
                raise VendorError(
                    "archive holds two members that flatten to {!r}; refusing to let "
                    "one overwrite the other".format(base))
            target = os.path.join(dest, base)
            with zf.open(info) as src, open(target, "wb") as fh:
                shutil.copyfileobj(src, fh, CHUNK_BYTES)
            written.append(base)
    return sorted(written)


# --------------------------------------------------------------------------
# Install state
# --------------------------------------------------------------------------

def read_receipt(env=None):
    """What a previous run installed, or None when nothing is installed."""
    try:
        with open(receipt_path(env), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_receipt(manifest, variant, entry, files, env=None):
    """Record what is installed, so the next run can be a no-op."""
    data = {
        "release_tag": manifest["release_tag"],
        "variant": variant,
        "asset": entry["asset"],
        "sha256": entry["sha256"],
        "backend": (VARIANT_TABLE.get(variant) or {}).get("backend"),
        "vendored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }
    with open(receipt_path(env), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return data


def status(manifest=None, variant=None, env=None):
    """What is installed, and whether it is what the pin says it should be.

    Returns a dict, never raises:

        {"installed": bool, "matches_pin": bool, "server": str|None,
         "receipt": dict|None, "variant": str, "release_tag": str,
         "reason": str}

    "installed" means llama-server.exe is present. "matches_pin" additionally
    means the receipt records the same release tag, variant and hash the
    manifest currently pins, which is what makes a re-run a no-op and what
    makes a changed pin actually re-vendor.
    """
    try:
        manifest = manifest or load_manifest()
    except VendorError as exc:
        return {"installed": False, "matches_pin": False, "server": None,
                "receipt": None, "variant": variant, "release_tag": None,
                "reason": str(exc)}
    variant = variant or bundled_variant(manifest)
    server = os.path.join(install_dir(env), SERVER_EXE)
    receipt = read_receipt(env)
    out = {"installed": os.path.isfile(server), "matches_pin": False,
           "server": server if os.path.isfile(server) else None,
           "receipt": receipt, "variant": variant,
           "release_tag": manifest["release_tag"], "reason": ""}
    if not out["installed"]:
        out["reason"] = "no {} under {}".format(SERVER_EXE, install_dir(env))
        return out
    if receipt is None:
        out["reason"] = ("{} is present but there is no {} recording where it came "
                         "from".format(SERVER_EXE, RECEIPT_NAME))
        return out
    try:
        entry = variant_entry(manifest, variant)
    except VendorError as exc:
        out["reason"] = str(exc)
        return out
    same = (receipt.get("release_tag") == manifest["release_tag"]
            and receipt.get("variant") == variant
            and receipt.get("sha256") == entry["sha256"])
    out["matches_pin"] = same
    out["reason"] = ("installed from {} {} and it matches the pin".format(
        receipt.get("release_tag"), receipt.get("variant")) if same else
        "installed from {} {} but the pin now says {} {}".format(
            receipt.get("release_tag"), receipt.get("variant"),
            manifest["release_tag"], variant))
    return out


# --------------------------------------------------------------------------
# The operation
# --------------------------------------------------------------------------

def vendor(variant=None, manifest=None, force=False, keep_all=False, offline=False,
           on_progress=None, log=None, env=None):
    """Put the pinned llama-server where hearth_llama will find it.

    Idempotent: when llama-server.exe is already installed from this exact
    release, variant and hash, nothing is downloaded and nothing is
    extracted. Offline-friendly for the same reason, and additionally
    because a previously downloaded archive in vendor/cache is re-verified
    against the pin and reused rather than refetched. `offline=True` refuses
    to open the network at all, so a build that must not reach out fails
    loudly instead of quietly succeeding on a machine that happens to have
    connectivity.

    Order of operations, which is the security property: the archive is
    downloaded, then verified against the pinned hash, and only then opened.
    Nothing downloaded is ever executed by this function.

    Returns a dict describing what happened. Raises ChecksumError when the
    bytes do not match the pin, and VendorError for everything else.
    """
    log = log or (lambda _msg: None)
    manifest = manifest or load_manifest()
    variant = variant or bundled_variant(manifest)
    entry = variant_entry(manifest, variant)

    state = status(manifest, variant, env=env)
    if state["matches_pin"] and not force:
        log("already vendored: {} {} at {}".format(
            manifest["release_tag"], variant, state["server"]))
        return {"action": "already-present", "variant": variant,
                "release_tag": manifest["release_tag"], "server": state["server"],
                "sha256": entry["sha256"], "files": (state["receipt"] or {}).get("files", []),
                "downloaded": False}

    archive = os.path.join(cache_dir(env), entry["asset"])
    downloaded = False
    if os.path.isfile(archive):
        try:
            verify_file(archive, entry["sha256"], entry["size_bytes"])
            log("reusing verified archive {}".format(archive))
        except ChecksumError as exc:
            # A cached file that fails the pin is not a cache, it is a
            # problem. Delete it and refetch rather than trusting it.
            log("cached archive failed verification, discarding it: {}".format(exc))
            _unlink(archive)

    if not os.path.isfile(archive):
        if offline:
            raise VendorError(
                "offline mode: {} is not in {} and this run may not download "
                "it".format(entry["asset"], cache_dir(env)))
        url = asset_url(manifest, entry)
        log("downloading {} ({:,} bytes) from {}".format(entry["asset"],
                                                         entry["size_bytes"], url))
        os.makedirs(cache_dir(env), exist_ok=True)
        download(url, archive, entry["sha256"], entry["size_bytes"],
                 manifest["release_tag"], on_progress=on_progress)
        downloaded = True

    # Verified above, in both the cached and the freshly downloaded case.
    # Only now is the archive opened.
    verify_file(archive, entry["sha256"], entry["size_bytes"])
    log("sha256 verified: {}".format(entry["sha256"]))

    dest = install_dir(env)
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    files = extract(archive, dest, keep=None if keep_all else _is_server_member)

    server = os.path.join(dest, SERVER_EXE)
    if not os.path.isfile(server):
        raise VendorError(
            "{} extracted {} files but none of them is {}; the upstream archive "
            "layout has changed".format(entry["asset"], len(files), SERVER_EXE))
    write_receipt(manifest, variant, entry, files, env=env)
    log("installed {} files to {}".format(len(files), dest))
    return {"action": "installed", "variant": variant,
            "release_tag": manifest["release_tag"], "server": server,
            "sha256": entry["sha256"], "files": files, "downloaded": downloaded}


# --------------------------------------------------------------------------
# Pinning a release
# --------------------------------------------------------------------------

def fetch_release(tag, timeout=API_TIMEOUT, opener=None):
    """A release's asset metadata from GitHub's API. Network required.

    Used only by `pin`. The API response is metadata, not the artifact: the
    digests it carries are recorded as a cross-check against the hash
    computed locally from the downloaded bytes, never as a substitute for
    it.
    """
    if not _TAG_RE.match(tag or ""):
        raise VendorError("{!r} is not a llama.cpp release tag".format(tag))
    url = "https://{}/repos/{}/{}/releases/tags/{}".format(
        API_HOST, UPSTREAM_OWNER, UPSTREAM_REPO, tag)
    opener = opener or _opener()
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "hearth-vendor-llama",
    })
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise VendorError("could not read release {} from GitHub: {}".format(tag, exc)) from exc


def _api_digest(asset):
    """The bare sha256 hex from an asset's "digest" field, or None."""
    raw = (asset or {}).get("digest") or ""
    if raw.startswith("sha256:") and _SHA256_RE.match(raw[7:]):
        return raw[7:]
    return None


def build_manifest(tag, release, hash_variants=(), previous=None, log=None, env=None):
    """A manifest for `tag` from GitHub's release metadata.

    Every variant in VARIANT_TABLE that the release actually publishes is
    recorded; ones it does not publish are simply absent, which is why the
    variant list in the manifest is what upstream really shipped rather than
    what anybody assumed.

    `hash_variants` names the variants to download and hash locally. For
    each of those the locally computed hash MUST equal the digest GitHub's
    API published, and the pin is refused if they disagree: those are two
    responses from two different hosts, and a disagreement means one of them
    is wrong. Variants not in that list record the API digest and say so in
    sha256_source, which is a weaker claim and is labelled as one.

    `previous` is an existing manifest whose policy prose is carried
    forward, so re-pinning never silently drops the reasoning.
    """
    log = log or (lambda _msg: None)
    assets = {a.get("name"): a for a in (release.get("assets") or []) if a.get("name")}
    variants = {}
    for name, spec in sorted(VARIANT_TABLE.items()):
        pattern = re.compile(spec["match"].replace("{tag}", re.escape(tag)))
        found = sorted(n for n in assets if pattern.match(n))
        if not found:
            log("release {} publishes no asset for variant {}".format(tag, name))
            continue
        if len(found) > 1:
            raise VendorError("variant {} matches {} assets in {}: {}".format(
                name, len(found), tag, ", ".join(found)))
        asset = assets[found[0]]
        digest = _api_digest(asset)
        if not digest:
            raise VendorError("GitHub published no sha256 digest for {}".format(found[0]))
        entry = {
            "asset": found[0],
            "size_bytes": int(asset.get("size") or 0),
            "sha256": digest,
            "sha256_source": SRC_API,
            "os": spec["os"], "arch": spec["arch"], "backend": spec["backend"],
            "gpu_vendors": list(spec["gpu_vendors"]),
            "requires": list(spec["requires"]),
            "note": spec["note"],
        }
        variants[name] = entry

    for name in hash_variants:
        if name not in variants:
            raise VendorError("cannot hash {!r}: release {} does not publish it".format(
                name, tag))
        entry = variants[name]
        archive = os.path.join(cache_dir(env), entry["asset"])
        url = "https://{}/{}/{}/releases/download/{}/{}".format(
            DOWNLOAD_HOST, UPSTREAM_OWNER, UPSTREAM_REPO, tag, entry["asset"])
        if os.path.isfile(archive) and os.path.getsize(archive) == entry["size_bytes"]:
            local = sha256_file(archive)
        else:
            os.makedirs(cache_dir(env), exist_ok=True)
            log("downloading {} to hash it locally".format(entry["asset"]))
            local = download(url, archive, entry["sha256"], entry["size_bytes"], tag)
        if local != entry["sha256"]:
            raise VendorError(
                "refusing to pin {}: the bytes hash to {} but GitHub's API publishes "
                "{}. Two independent responses disagree about the same artifact and "
                "neither can be trusted.".format(entry["asset"], local, entry["sha256"]))
        entry["sha256_source"] = SRC_HASHED
        log("{}: {} confirmed by download and by the API digest".format(
            entry["asset"], local))

    policy = dict(DEFAULT_POLICY)
    if previous and isinstance(previous.get("policy"), dict):
        policy = previous["policy"]
    if policy["bundled_variant"] not in variants:
        raise VendorError("the policy bundles {!r}, which release {} does not "
                          "publish".format(policy["bundled_variant"], tag))

    manifest = {
        "schema": 1,
        "project": "llama.cpp",
        "upstream": "https://github.com/{}/{}".format(UPSTREAM_OWNER, UPSTREAM_REPO),
        "release_tag": tag,
        "build_number": int(tag[1:]),
        "commit": release.get("target_commitish"),
        "released_at": release.get("published_at"),
        "pinned_at": time.strftime("%Y-%m-%d", time.gmtime()),
        "download_prefix": "https://{}/{}/{}/releases/download/{}/".format(
            DOWNLOAD_HOST, UPSTREAM_OWNER, UPSTREAM_REPO, tag),
        "bundled_variant": policy["bundled_variant"],
        "policy": policy,
        "sha256_source_meanings": {
            SRC_HASHED: ("the asset was downloaded and hashed on the machine that "
                         "pinned it, and that hash equalled the digest GitHub's API "
                         "published for it"),
            SRC_API: ("only the digest GitHub's API published was recorded; the bytes "
                      "were not fetched and hashed at pin time"),
        },
        "variants": variants,
    }
    validate_manifest(manifest)
    return manifest


def write_manifest(manifest, path=None):
    """Write a validated manifest to disk. Returns the path."""
    path = path or MANIFEST_PATH
    validate_manifest(manifest)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _progress_printer():
    """A progress callback that rewrites one line, or nothing when stdout is
    not a terminal (a build log does not want ten thousand carriage
    returns)."""
    if not sys.stdout.isatty():
        return None
    state = {"last": 0.0}

    def emit(done, total):
        now = time.monotonic()
        if now - state["last"] < 0.2 and done < total:
            return
        state["last"] = now
        pct = (done / total * 100) if total else 0
        sys.stdout.write("\r  {:5.1f}%  {:,} / {:,} bytes".format(pct, done, total))
        sys.stdout.flush()
        if done >= total:
            sys.stdout.write("\n")
    return emit


def _build_parser():
    p = argparse.ArgumentParser(
        prog="vendor_llama",
        description="Fetch the pinned llama.cpp llama-server into vendor/llama.")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--live", action="store_true",
                   help="also run the self-tests that need the network")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="command")

    inst = sub.add_parser("install", help="vendor the pinned binary (default)")
    inst.add_argument("--variant", default=None,
                      help="which pinned variant to install (default: the bundled one)")
    inst.add_argument("--force", action="store_true",
                      help="reinstall even when the pin already matches")
    inst.add_argument("--all", action="store_true", dest="keep_all",
                      help="extract every archive member, not just the server set")
    inst.add_argument("--offline", action="store_true",
                      help="fail rather than download; use only a cached archive")

    st = sub.add_parser("status", help="report what is installed")
    st.add_argument("--variant", default=None)

    sub.add_parser("variants", help="list the pinned variants and what they are for")

    pin = sub.add_parser("pin", help="regenerate the manifest for a release tag")
    pin.add_argument("tag", help="a llama.cpp release tag, for example b10105")
    pin.add_argument("--hash", default="",
                     help="comma-separated variants to download and hash locally")
    pin.add_argument("--out", default=None)
    return p


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test(live=args.live)
        print("vendor_llama self-test: ok")
        return 0

    try:
        if args.command == "variants":
            manifest = load_manifest()
            if args.json:
                print(json.dumps(manifest["variants"], indent=2, sort_keys=True))
                return 0
            print("llama.cpp {} (build {}), pinned {}".format(
                manifest["release_tag"], manifest["build_number"], manifest["pinned_at"]))
            for name in sorted(manifest["variants"]):
                e = manifest["variants"][name]
                print("  {:<24} {:>12,} B  {:<8} {}{}".format(
                    name, e["size_bytes"], e["backend"],
                    "[bundled] " if name == bundled_variant(manifest) else "",
                    e["note"]))
                print("  {:<24} sha256 {} ({})".format("", e["sha256"], e["sha256_source"]))
            return 0

        if args.command == "status":
            info = status(variant=getattr(args, "variant", None))
            if args.json:
                print(json.dumps(info, indent=2, sort_keys=True))
            else:
                print("{}: {}".format(
                    "ok" if info["matches_pin"] else "not vendored", info["reason"]))
            return 0 if info["matches_pin"] else 1

        if args.command == "pin":
            release = fetch_release(args.tag)
            wanted = [v for v in (args.hash or "").split(",") if v.strip()]
            previous = None
            try:
                previous = load_manifest(args.out)
            except VendorError:
                pass
            manifest = build_manifest(args.tag, release, hash_variants=wanted,
                                      previous=previous, log=lambda m: print(m))
            path = write_manifest(manifest, args.out)
            print("wrote {} ({} variants)".format(path, len(manifest["variants"])))
            return 0

        result = vendor(
            variant=getattr(args, "variant", None),
            force=getattr(args, "force", False),
            keep_all=getattr(args, "keep_all", False),
            offline=getattr(args, "offline", False),
            on_progress=_progress_printer(),
            log=lambda m: print(m),
        )
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print("{}: {} {} -> {}".format(result["action"], result["release_tag"],
                                           result["variant"], result["server"]))
        return 0
    except ChecksumError as exc:
        print("CHECKSUM FAILURE: {}".format(exc), file=sys.stderr)
        return 2
    except VendorError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

#: Source tokens that would mean this script can run what it downloads. The
#: check below is a real guarantee rather than a comment: rule 4 in the
#: module docstring is only worth writing down if something enforces it.
_EXECUTION_TOKENS = ("import subprocess", "os.system", "os.popen", "os.exec",
                     "os.spawn", "import ctypes", "runpy", "eval(", "exec(")


def _make_zip(path, members, external_attr=None):
    """A zip built from {name: bytes}, for the extraction tests.

    The name is assigned after the ZipInfo is constructed on purpose:
    ZipInfo's constructor rewrites os.sep to "/", which on Windows would
    quietly turn the backslash test case into a legitimate nested path and
    make that test pass without testing anything.
    """
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            if external_attr is not None:
                info.external_attr = external_attr
            zf.writestr(info, data)
    return path


class _StubZip:
    """An object with just enough of ZipFile for safe_members, so a member
    of a declared size can be tested without writing that many bytes."""

    def __init__(self, infos):
        self._infos = infos

    def infolist(self):
        return self._infos


class _FakeResponse:
    """A minimal stand-in for a urllib response, for the download tests."""

    def __init__(self, data):
        self._buf = io.BytesIO(data)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._buf.close()
        return False


class _FakeOpener:
    """An opener that serves fixed bytes, so the download path can be tested
    without a network and with deliberately wrong bytes."""

    def __init__(self, data):
        self.data = data
        self.opened = []

    def open(self, url, timeout=None):
        self.opened.append(url)
        return _FakeResponse(self.data)


def _self_test(live=False):
    tmp = tempfile.mkdtemp(prefix="hearth-vendor-test-")
    try:
        # -- nothing here can execute what it downloads --------------------
        # Scanning the source is the point: the guarantee has to survive an
        # edit by somebody who did not read the docstring.
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
            source = fh.read()
        body = source.split('_EXECUTION_TOKENS = (', 1)[0]
        for token in _EXECUTION_TOKENS:
            assert token not in body, (
                "vendor_llama must never be able to run what it downloads, but its "
                "source contains {!r}".format(token))
        assert "subprocess" not in globals(), "subprocess must not be imported here"

        # -- the committed manifest is a usable pin ------------------------
        manifest = load_manifest()
        assert _TAG_RE.match(manifest["release_tag"]), manifest["release_tag"]
        assert manifest["variants"], "the pin lists no variants"
        bundled = bundled_variant(manifest)
        assert bundled in manifest["variants"], bundled
        entry = variant_entry(manifest, bundled)
        assert entry["os"] == "windows" and entry["arch"] == "x86_64", entry
        # The bundled variant is the one Hearth actually ships, so its hash
        # must be the strong kind, not a digest somebody read off an API.
        assert entry["sha256_source"] == SRC_HASHED, (
            "the bundled variant's hash must have been verified by download, not "
            "only read from the API: {}".format(entry["sha256_source"]))
        for name, e in manifest["variants"].items():
            assert _SHA256_RE.match(e["sha256"]), name
            assert manifest["release_tag"] in e["asset"] or e["asset"].startswith("cudart-"), name
            for req in e.get("requires", []):
                assert req in manifest["variants"], (
                    "{} requires {}, which is not pinned".format(name, req))

        # -- a manifest that is not a usable pin is refused ----------------
        for broken in (
            {"release_tag": "latest", "variants": {"x": {}}},
            {"release_tag": "b1", "variants": {}},
            {"release_tag": "b1", "variants": {"x": {"asset": "llama-b1-x.zip",
                                                     "sha256": "nothex",
                                                     "size_bytes": 1,
                                                     "sha256_source": SRC_HASHED}}},
            {"release_tag": "b1", "variants": {"x": {"asset": "llama-b1-x.zip",
                                                     "sha256": "a" * 64,
                                                     "size_bytes": 0,
                                                     "sha256_source": SRC_HASHED}}},
            {"release_tag": "b1", "variants": {"x": {"asset": "llama-b1-x.zip",
                                                     "sha256": "a" * 64,
                                                     "size_bytes": 1,
                                                     "sha256_source": "trust me"}}},
            # The asset does not carry the pinned tag, so the manifest is
            # pointing at some other release's file.
            {"release_tag": "b2", "variants": {"x": {"asset": "llama-b1-x.zip",
                                                     "sha256": "a" * 64,
                                                     "size_bytes": 1,
                                                     "sha256_source": SRC_HASHED}}},
        ):
            try:
                validate_manifest(broken)
                raise AssertionError("a broken manifest must be refused: {}".format(broken))
            except VendorError:
                pass

        # -- only official release URLs are accepted -----------------------
        tag = manifest["release_tag"]
        good = asset_url(manifest, entry)
        assert check_url(good, tag)
        for bad in (
            good.replace("https://", "http://"),
            good.replace("github.com", "github.com.evil.example"),
            good.replace("github.com", "raw.githubusercontent.com"),
            good.replace("/ggml-org/", "/someone-else/"),
            "https://github.com/ggml-org/llama.cpp/releases/download/b1/x.zip",
            good.replace(entry["asset"], "../../../etc/passwd"),
            good.replace(entry["asset"], "sub/dir/" + entry["asset"]),
        ):
            try:
                check_url(bad, tag)
                raise AssertionError("must refuse {}".format(bad))
            except VendorError:
                pass

        # -- redirects off GitHub's own hosts are refused ------------------
        handler = _PinnedRedirectHandler()
        for bad in ("https://evil.example/x.zip", "http://github.com/x.zip"):
            try:
                handler.redirect_request(None, None, 302, "Found", {}, bad)
                raise AssertionError("must refuse a redirect to {}".format(bad))
            except VendorError:
                pass

        # -- MUTATION: corrupt bytes fail verification ---------------------
        payload = b"hearth" * 1000
        good_hash = hashlib.sha256(payload).hexdigest()
        blob = os.path.join(tmp, "blob.bin")
        with open(blob, "wb") as fh:
            fh.write(payload)
        assert verify_file(blob, good_hash, len(payload)) == good_hash
        # One flipped byte, same length: the size check cannot catch this and
        # the hash must.
        mutated = bytearray(payload)
        mutated[17] ^= 0x01
        with open(blob, "wb") as fh:
            fh.write(bytes(mutated))
        try:
            verify_file(blob, good_hash, len(payload))
            raise AssertionError("a corrupted file must fail verification")
        except ChecksumError:
            pass
        # A truncated file fails too, on length before hash.
        with open(blob, "wb") as fh:
            fh.write(payload[:-1])
        try:
            verify_file(blob, good_hash, len(payload))
            raise AssertionError("a truncated file must fail verification")
        except ChecksumError:
            pass

        # -- MUTATION: a wrong pinned hash refuses good bytes --------------
        with open(blob, "wb") as fh:
            fh.write(payload)
        try:
            verify_file(blob, "0" * 64, len(payload))
            raise AssertionError("a wrong pin must refuse the file")
        except ChecksumError:
            pass

        # -- the download path verifies before it lands the file -----------
        dest = os.path.join(tmp, "cache", "asset.zip")
        url = "https://github.com/ggml-org/llama.cpp/releases/download/{}/x.zip".format(tag)
        opener = _FakeOpener(payload)
        assert download(url, dest, good_hash, len(payload), tag, opener=opener) == good_hash
        assert os.path.isfile(dest)
        # Wrong bytes: nothing is left behind, not even the .part.
        _unlink(dest)
        opener = _FakeOpener(bytes(mutated))
        try:
            download(url, dest, good_hash, len(payload), tag, opener=opener)
            raise AssertionError("a download whose bytes do not match must fail")
        except ChecksumError:
            pass
        assert not os.path.exists(dest), "a failed download must not land the file"
        assert not os.path.exists(dest + ".part"), "a failed download must not keep the part"
        # A response longer than the pin is cut off rather than written out.
        opener = _FakeOpener(payload + b"extra")
        try:
            download(url, dest, good_hash, len(payload), tag, opener=opener)
            raise AssertionError("an over-long response must be refused")
        except VendorError:
            pass
        assert not os.path.exists(dest + ".part")

        # -- archives are opened only after verification --------------------
        # Proven by construction: vendor() calls verify_file before extract,
        # and extract is the only caller of zipfile here. The ordering is
        # asserted below through vendor() itself with a poisoned cache.

        # -- extraction refuses paths that escape --------------------------
        # The backslash case is driven through safe_members rather than
        # through a real archive, because CPython's ZipFile rewrites os.sep
        # to "/" when it reads the central directory: on Windows a
        # backslash member is normalised into a nested path before any of
        # this code sees it, so an end-to-end test of that case would pass
        # without testing anything. The check still matters on POSIX, where
        # no such rewrite happens and a backslash is a literal character in
        # the name.
        back = zipfile.ZipInfo("placeholder")
        back.filename = "a\\b.txt"
        try:
            safe_members(_StubZip([back]))
            raise AssertionError("must refuse a backslash in an archive member")
        except VendorError:
            pass

        for bad_name in ("../escape.txt", "/abs.txt", "C:/abs.txt",
                         "sub/../../escape.txt"):
            z = _make_zip(os.path.join(tmp, "bad.zip"), {bad_name: b"x"})
            try:
                extract(z, os.path.join(tmp, "out"))
                raise AssertionError("must refuse archive member {!r}".format(bad_name))
            except VendorError:
                pass
        # A symlink member is refused even with a harmless name.
        z = _make_zip(os.path.join(tmp, "link.zip"), {"link": b"/etc/passwd"},
                      external_attr=(0xA1FF << 16))
        try:
            extract(z, os.path.join(tmp, "out"))
            raise AssertionError("must refuse a symlink member")
        except VendorError:
            pass

        # -- extraction keeps the server set and flattens ------------------
        z = _make_zip(os.path.join(tmp, "ok.zip"), {
            SERVER_EXE: b"MZ-not-really",
            "ggml-base.dll": b"dll",
            "llama-tts.exe": b"other tool",
            "nested/ggml-cpu-haswell.dll": b"dll",
        })
        got = extract(z, os.path.join(tmp, "out"), keep=_is_server_member)
        assert got == sorted(["ggml-base.dll", "ggml-cpu-haswell.dll", SERVER_EXE]), got
        assert not os.path.exists(os.path.join(tmp, "out", "llama-tts.exe"))
        assert os.path.isfile(os.path.join(tmp, "out", SERVER_EXE))
        got_all = extract(z, os.path.join(tmp, "out2"))
        assert "llama-tts.exe" in got_all, got_all

        # -- a zip bomb is refused -----------------------------------------
        # safe_members is driven directly here: writing a member that really
        # declares two gigabytes would mean producing two gigabytes, and the
        # size cap is a decision about the declared size anyway.
        big = zipfile.ZipInfo("big.bin")
        big.file_size = MAX_UNCOMPRESSED_BYTES + 1
        try:
            safe_members(_StubZip([big]))
            raise AssertionError("an over-large archive must be refused")
        except VendorError:
            pass
        # Two members that are individually fine but together are not.
        half = MAX_UNCOMPRESSED_BYTES // 2 + 1
        pair = []
        for n in ("a.bin", "b.bin"):
            i = zipfile.ZipInfo(n)
            i.file_size = half
            pair.append(i)
        try:
            safe_members(_StubZip(pair))
            raise AssertionError("a cumulative overflow must be refused")
        except VendorError:
            pass

        # -- end to end against a fake pin, offline ------------------------
        # A full vendor() run with a manifest whose one variant is a zip
        # this test built, exercising install, idempotency, a changed pin,
        # and a poisoned cache.
        fake_root = os.path.join(tmp, "root")
        fenv = {ENV_VENDOR_DIR: fake_root}
        os.makedirs(os.path.join(fake_root, CACHE_SUBDIR))
        fake_zip = os.path.join(fake_root, CACHE_SUBDIR, "llama-b1-bin-win-cpu-x64.zip")
        _make_zip(fake_zip, {SERVER_EXE: b"MZ", "ggml.dll": b"d", "llama-tts.exe": b"t"})
        fake_manifest = {
            "release_tag": "b1",
            "bundled_variant": "win-cpu-x64",
            "variants": {"win-cpu-x64": {
                "asset": "llama-b1-bin-win-cpu-x64.zip",
                "sha256": sha256_file(fake_zip),
                "size_bytes": os.path.getsize(fake_zip),
                "sha256_source": SRC_HASHED,
                "os": "windows", "arch": "x86_64", "backend": "cpu",
                "gpu_vendors": [], "requires": [], "note": "test",
            }},
        }
        validate_manifest(fake_manifest)
        st = status(fake_manifest, env=fenv)
        assert not st["installed"] and not st["matches_pin"], st

        res = vendor(manifest=fake_manifest, offline=True, env=fenv)
        assert res["action"] == "installed", res
        assert os.path.isfile(os.path.join(fake_root, VENDOR_SUBDIR, SERVER_EXE))
        assert "llama-tts.exe" not in res["files"], res["files"]

        st = status(fake_manifest, env=fenv)
        assert st["installed"] and st["matches_pin"], st

        # Idempotent: a second run downloads nothing and changes nothing.
        res2 = vendor(manifest=fake_manifest, offline=True, env=fenv)
        assert res2["action"] == "already-present", res2
        assert res2["downloaded"] is False

        # A changed pin is NOT already-present, which is what makes bumping
        # the manifest actually re-vendor.
        moved = json.loads(json.dumps(fake_manifest))
        moved["release_tag"] = "b2"
        moved["variants"]["win-cpu-x64"]["asset"] = "llama-b2-bin-win-cpu-x64.zip"
        validate_manifest(moved)
        st = status(moved, env=fenv)
        assert st["installed"] and not st["matches_pin"], st
        try:
            vendor(manifest=moved, offline=True, env=fenv)
            raise AssertionError("offline mode must refuse to fetch a missing archive")
        except VendorError:
            pass
        # The old install survived a failed re-vendor.
        assert os.path.isfile(os.path.join(fake_root, VENDOR_SUBDIR, SERVER_EXE))

        # MUTATION: poison the cached archive. vendor() must refuse to
        # extract it, which also proves verification happens before the
        # archive is opened: the poisoned zip is a perfectly valid zip, so
        # anything that opened it first would have succeeded.
        poisoned = json.loads(json.dumps(fake_manifest))
        poisoned["variants"]["win-cpu-x64"]["sha256"] = "b" * 64
        validate_manifest(poisoned)
        try:
            vendor(manifest=poisoned, offline=True, force=True, env=fenv)
            raise AssertionError("a cached archive that fails the pin must not be used")
        except VendorError:
            pass

        # -- live: the real pinned asset ----------------------------------
        if live:
            live_env = {ENV_VENDOR_DIR: os.path.join(tmp, "live")}
            got = vendor(manifest=manifest, env=live_env)
            assert got["action"] == "installed", got
            assert os.path.isfile(got["server"]), got
            assert sha256_file(os.path.join(
                cache_dir(live_env), entry["asset"])) == entry["sha256"]
            # MUTATION against the real asset: corrupt one byte of the
            # cached archive and confirm the next run refuses it.
            cached = os.path.join(cache_dir(live_env), entry["asset"])
            with open(cached, "r+b") as fh:
                fh.seek(1024)
                b = fh.read(1)
                fh.seek(1024)
                fh.write(bytes([b[0] ^ 0xFF]))
            try:
                verify_file(cached, entry["sha256"], entry["size_bytes"])
                raise AssertionError("a corrupted real asset must fail verification")
            except ChecksumError:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
