#!/usr/bin/env python3
"""Generate THIRD-PARTY-NOTICES.md from what is actually vendored.

    python scripts/third_party_notices.py            write the file
    python scripts/third_party_notices.py --check    fail if it is stale
    python scripts/third_party_notices.py --self-test

Hearth is Apache-2.0, but the thing a user installs is mostly other
people's software: a Tauri shell statically linked from a couple of hundred
Rust crates, a CPython interpreter and a llama.cpp inference engine, each
of which carries its own dependency tree. Every one of those licences
requires that its notice travel with the binary. This script exists so that
the notice file is DERIVED rather than remembered, because a hand-written
inventory is wrong the first time a version is bumped and nobody notices
until somebody complains.

What is derived, and from what
------------------------------
  llama.cpp version, commit, variant, archive digest
                          vendor/llama_manifest.json
  the actual engine files that will be copied into the installer
                          vendor/llama/ on this machine
  CPython version and archive digest
                          vendor/python_manifest.json
  that CPython's own licence file is present and will be shipped
                          vendor/python/LICENSE.txt
  Hearth's version, every Rust crate linked into the shell executable, its
  version, its SPDX expression, the licence files it ships, the copyright
  notices in them, and a scan for crates that grant a copyleft licence
                          cargo metadata, run in desktop/tauri/
  where the notices land in an installed application, and whether the
  installer redistributes the WebView2 runtime or fetches Microsoft's
  bootstrapper
                          desktop/tauri/tauri.conf.json
  the licence texts for components whose upstream release archive ships
  none, together with the URL each was taken from and its digest
                          vendor/licenses/MANIFEST.json

What is asserted rather than derived is the COMPONENTS table below: the
human-readable description of each component and the SPDX name of its
licence. A licence identifier cannot be computed from a DLL. Each entry
records the evidence it rests on so the assertion can be rechecked. The
same goes for LICENCE_TEXTS, which says which crate each reproduced licence
text was taken from; the bytes themselves are checked against that crate's
own file on every run.

Refusing to run is the point
----------------------------
If vendor/llama or vendor/python is absent, or if cargo cannot resolve the
crate graph, this script fails instead of emitting a notice file with holes
in it. Run scripts/build_windows.py --skip-build first, or install a Rust
toolchain. A notices file generated from a half-populated tree is worse
than no notices file, because it looks complete.

Standard library only, and no network. cargo is a subprocess, not an
import, and it reads a Cargo.lock that is committed. Every byte this script
reads is already on disk; vendor/licenses/ was fetched once, is committed,
and is verified against its recorded digests on every run.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(REPO_ROOT, "THIRD-PARTY-NOTICES.md")

LLAMA_MANIFEST = os.path.join(REPO_ROOT, "vendor", "llama_manifest.json")
PYTHON_MANIFEST = os.path.join(REPO_ROOT, "vendor", "python_manifest.json")
LICENSES_DIR = os.path.join(REPO_ROOT, "vendor", "licenses")
LICENSES_MANIFEST = os.path.join(LICENSES_DIR, "MANIFEST.json")
LLAMA_DIR = os.path.join(REPO_ROOT, "vendor", "llama")
PYTHON_DIR = os.path.join(REPO_ROOT, "vendor", "python")
TAURI_DIR = os.path.join(REPO_ROOT, "desktop", "tauri")
TAURI_CONF = os.path.join(TAURI_DIR, "tauri.conf.json")

#: The only target Hearth ships. cargo metadata is asked to resolve for it
#: explicitly, so that a crate pulled in only by a unix cfg does not appear
#: in an inventory of a Windows installer.
CARGO_TARGET = "x86_64-pc-windows-msvc"

#: The canonical Apache-2.0 text, so LICENSE can be proven unmodified rather
#: than eyeballed. Verified on 2026-08-02 against
#: https://www.apache.org/licenses/LICENSE-2.0.txt (11358 bytes, LF endings).
APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
APACHE_2_0_BYTES = 11358

#: Where the payload lands once the Tauri bundler and NSIS have run. Under
#: Tauri the resources sit BESIDE the executable rather than under a
#: resources\ subdirectory, so hearth\ and python\ are at the application
#: root. The exact destinations are read out of tauri.conf.json rather than
#: repeated here; see resource_layout().
INSTALL_ROOT = r"%LOCALAPPDATA%\Programs\Hearth"


class Missing(SystemExit):
    """A source this file must be derived from is not on disk."""


def _require(path, why):
    if not os.path.exists(path):
        raise Missing(
            "cannot generate the notices: {} is missing.\n{}".format(
                os.path.relpath(path, REPO_ROOT), why))
    return path


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _read_json(path):
    return json.loads(_read(path))


def _read_licence_file(path):
    """Read a licence file that somebody else wrote, strictly.

    errors="replace" here would put U+FFFD in the middle of a copyright
    notice and ship it. If a crate's licence file is not UTF-8 the right
    answer is to stop and look at it.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return raw.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise SystemExit(
            "{} is not valid UTF-8 ({}). A copyright notice cannot be read "
            "out of it safely, so it is not read out of it at all.".format(
                path, exc))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# The asserted half: what each redistributed component is and under what
# licence. Every row names the evidence behind its licence field, because
# "MIT" is a claim about a binary and a claim about a binary needs a source.
# --------------------------------------------------------------------------

class Component(object):
    def __init__(self, name, licence, where, evidence, text_file=None):
        self.name = name
        self.licence = licence
        #: Path inside the installed application, relative to INSTALL_ROOT.
        self.where = where
        self.evidence = evidence
        #: Filename in vendor/licenses/, when Hearth has to supply the text.
        self.text_file = text_file


#: Shipped inside the installer. Ordered by which tree they arrive in.
COMPONENTS = (
    # ---- the inference engine -------------------------------------------
    Component(
        "llama.cpp and ggml", "MIT",
        r"hearth\vendor\llama\*.exe, *.dll",
        "vendor/llama_manifest.json pins release b10105 at commit "
        "e6dd0e29a6751d4859abaa8899959f5ddf756f4e; that commit's LICENSE is "
        "MIT and is reproduced here because the Windows release archive "
        "contains no licence file at all",
        text_file="llama.cpp.txt"),
    Component(
        "cpp-httplib", "MIT",
        r"hearth\vendor\llama\llama-server.exe (statically linked)",
        "vendored at vendor/cpp-httplib in the pinned llama.cpp commit and "
        "linked into llama-server",
        text_file="cpp-httplib.txt"),
    Component(
        "nlohmann/json", "MIT",
        r"hearth\vendor\llama\llama-server.exe (statically linked)",
        "vendored at vendor/nlohmann in the pinned llama.cpp commit",
        text_file="nlohmann-json.txt"),
    Component(
        "stb_image", "MIT or public domain, at your option",
        r"hearth\vendor\llama\mtmd.dll (statically linked)",
        "vendored at vendor/stb in the pinned llama.cpp commit; the licence "
        "block is carried at the end of stb_image.h",
        text_file="stb_image.txt"),
    Component(
        "sheredom/subprocess.h", "Unlicense (public domain)",
        r"hearth\vendor\llama\*.exe (statically linked)",
        "vendored at vendor/sheredom in the pinned llama.cpp commit",
        text_file="subprocess.h.txt"),
    Component(
        "miniaudio", "public domain or MIT-0, at your option",
        r"hearth\vendor\llama\*.exe (statically linked)",
        "vendored at vendor/miniaudio in the pinned llama.cpp commit",
        text_file="miniaudio.txt"),
    Component(
        "big-integer", "Unlicense (public domain)",
        r"hearth\vendor\llama\llama-server.exe (embedded web UI)",
        "vendored at tools/ui/src/lib/vendors in the pinned llama.cpp "
        "commit; llama-server embeds the built web UI in the executable",
        text_file="big-integer.txt"),
    Component(
        "decimal.js", "MIT",
        r"hearth\vendor\llama\llama-server.exe (embedded web UI)",
        "vendored at tools/ui/src/lib/vendors in the pinned llama.cpp commit",
        text_file="decimal.js.txt"),
    Component(
        "nerdamer-prime", "MIT",
        r"hearth\vendor\llama\llama-server.exe (embedded web UI)",
        "vendored at tools/ui/src/lib/vendors in the pinned llama.cpp commit",
        text_file="nerdamer-prime.txt"),
    Component(
        "LLVM OpenMP runtime (libomp)",
        "University of Illinois/NCSA Open Source License and MIT, dual",
        r"hearth\vendor\llama\libomp140.x86_64.dll",
        "the DLL's own version resource reads \"LLVM* OpenMP* Performance "
        "Library version 5.0.20140926\", company LLVM. That predates LLVM's "
        "2019 relicensing, so the NCSA and MIT dual licence applies rather "
        "than Apache-2.0 with the LLVM exception. It reaches Hearth inside "
        "the llama.cpp Windows release archive, having originated in the "
        "Microsoft Visual C++ toolset's redistributable OpenMP runtime",
        text_file="llvm-openmp.txt"),

    # ---- the interpreter -------------------------------------------------
    Component(
        "CPython (Windows embeddable package)",
        "Python Software Foundation License Version 2",
        r"python\*",
        "vendor/python_manifest.json pins 3.12.10; the distribution's own "
        "LICENSE.txt travels inside the archive and is copied into the "
        "installer unchanged",
        text_file=None),
    Component(
        "OpenSSL", "Apache-2.0",
        r"python\libcrypto-3.dll, libssl-3.dll",
        "the DLLs' version resources read \"The OpenSSL Toolkit\" 3.0.16; "
        "OpenSSL 3.x is Apache-2.0. Bundled by python.org inside the "
        "embeddable package, and covered by the licence text that package "
        "carries",
        text_file=None),
    Component(
        "SQLite", "public domain",
        r"python\sqlite3.dll",
        "the DLL's version resource reads SQLite3 3.49.1.0. SQLite is "
        "dedicated to the public domain and asks for no attribution; it is "
        "listed for completeness",
        text_file=None),
    Component(
        "libffi", "MIT",
        r"python\libffi-8.dll",
        "bundled by python.org inside the embeddable package and covered by "
        "the licence text that package carries",
        text_file=None),
    Component(
        "expat, zlib, bzip2, xz/liblzma, libmpdec, Unicode data",
        "MIT, zlib, BSD-like, 0BSD and the Unicode licence",
        r"python\python312.zip, *.pyd",
        "compiled into CPython; each licence is reproduced in full in the "
        "distribution's own LICENSE.txt, which ships",
        text_file=None),
    Component(
        "Microsoft Visual C++ runtime",
        "Microsoft Distributable Code terms",
        r"python\vcruntime140.dll, vcruntime140_1.dll",
        "the DLLs' version resources read Microsoft Visual Studio "
        "14.42.34438.0. Not open source. Redistributed by python.org as part "
        "of the embeddable package under Microsoft's redistribution terms "
        "for the Visual C++ runtime, which permit shipping it with an "
        "application",
        text_file=None),

    # ---- the shell -------------------------------------------------------
    Component(
        "Rust crates linked into the shell executable",
        "MIT or Apache-2.0 for almost all of them; every crate and its SPDX "
        "expression is inventoried below",
        r"Hearth.exe (statically linked)",
        "derived by walking the resolve graph that `cargo metadata "
        "--locked --filter-platform " + CARGO_TARGET + "` reports for "
        "desktop/tauri, following normal dependency edges only. Nothing is "
        "remembered; the inventory is rebuilt from Cargo.lock every run",
        text_file=None),
    Component(
        "The Rust standard library", "MIT OR Apache-2.0",
        r"Hearth.exe (statically linked)",
        "rust-lang/rust declares `MIT OR Apache-2.0` and its distributed "
        "toolchain carries both texts under share/doc/rust/licenses. std is "
        "linked into every Rust binary and does not appear in cargo "
        "metadata, because it is not a package in the dependency graph",
        text_file=None),
)

#: Fetched onto the user's machine AFTER installation, so not part of the
#: installer and not covered by the notices that ship inside it. Recorded
#: because a user is entitled to know what a click will download, and
#: because two of these are not open source.
POST_INSTALL = (
    ("llama.cpp GPU engines (Vulkan, CUDA, ROCm, SYCL, OpenVINO)", "MIT",
     "agent/hearth_engine.py fetches the variant chosen for the detected "
     "GPU from the release pinned in vendor/llama_manifest.json"),
    ("NVIDIA CUDA runtime libraries", "NVIDIA CUDA Toolkit EULA, proprietary",
     "only when a user explicitly selects a CUDA engine through "
     "HEARTH_GPU_ENGINE; the cudart-* archives in the manifest are NVIDIA "
     "redistributables and are not open source. The default first-run GPU "
     "fetch is Vulkan, which pulls nothing proprietary"),
    ("AMD ROCm and Intel oneAPI or OpenVINO runtimes",
     "vendor terms, partly proprietary",
     "only when a user explicitly selects the matching engine"),
    ("Language model weights", "whatever the model publisher chose",
     "agent/hearth_hf.py and agent/hearth_shop.py download models the user "
     "picks. Hearth ships no weights and takes no position on their terms"),
)

#: Present in the repository, used to BUILD the installer, and not
#: redistributed. cargo's build-dependencies are the Rust equivalent of
#: electron-builder and its two hundred transitive packages: they run on
#: the build machine, they produce bytes, and none of them is linked into
#: the executable. That is why the inventory follows normal dependency
#: edges only, and why the build-only crates are named below rather than
#: silently dropped.
NOT_REDISTRIBUTED = (
    ("desktop/tauri build-dependencies", "tauri-build and the crates it "
     "pulls in run at compile time to generate the asset embedding and the "
     "Windows resource block. They are listed by name below, derived from "
     "the same resolve graph, so that dropping one from the inventory is a "
     "decision this file records rather than an omission"),
    ("site/node_modules", "Astro and Starlight, used to build the "
     "documentation site"),
)


# --------------------------------------------------------------------------
# Licence texts that the crate inventory needs.
#
# Reproducing every crate's licence file would produce a document nobody
# reads: 233 crates share about a dozen distinct texts, and the MIT text
# alone is byte-identical across 35 of them. So the inventory table below is
# complete and the texts are deduplicated: one copy of each distinct
# licence, taken from a crate that actually ships it.
#
# The crate each text came from is asserted here. Everything else is
# checked: the crate has to still be in the inventory, its file on disk has
# to still hash to the digest recorded in vendor/licenses/MANIFEST.json, and
# the committed copy has to hash to the same thing. A text that upstream
# changes fails the run rather than drifting.
# --------------------------------------------------------------------------

class LicenceText(object):
    def __init__(self, spdx, text_file, crate, filename):
        self.spdx = spdx
        self.text_file = text_file
        self.crate = crate
        self.filename = filename


LICENCE_TEXTS = (
    LicenceText("MIT", "licence-MIT.txt", "serde", "LICENSE-MIT"),
    LicenceText("MPL-2.0", "licence-MPL-2.0.txt", "cssparser", "LICENSE"),
    LicenceText("Unicode-3.0", "licence-Unicode-3.0.txt",
                "icu_collections", "LICENSE"),
    LicenceText("Zlib", "licence-Zlib.txt", "foldhash", "LICENSE"),
    LicenceText("Unlicense", "licence-Unlicense.txt",
                "aho-corasick", "UNLICENSE"),
    LicenceText("0BSD", "licence-0BSD.txt", "adler2", "LICENSE-0BSD"),
    LicenceText("CC0-1.0", "licence-CC0-1.0.txt", "dunce", "LICENSE"),
)

#: SPDX identifiers that appear in the crate inventory and deliberately have
#: no text under vendor/licenses/. Anything else without a text fails the
#: run, which is what makes the list above complete rather than merely long.
ATOMS_WITHOUT_TEXT = {
    "Apache-2.0":
        "the complete Apache License 2.0 is Hearth's own `LICENSE`, which "
        "ships at the application root and again inside the payload. A "
        "second copy under `vendor/licenses/` would be a second source of "
        "truth that could drift from the one a user actually reads",
    "MIT-0":
        "offered by one crate, `dunce`, alongside CC0-1.0 and Apache-2.0. "
        "It ships the CC0-1.0 text and no MIT-0 text, so CC0-1.0 is the "
        "option taken and that is the text reproduced",
}


# --------------------------------------------------------------------------
# The derived half.
# --------------------------------------------------------------------------

#: SPDX identifiers that must stop the build. Hearth redistributes a single
#: statically linked executable, so a GPL, LGPL, AGPL or SSPL crate anywhere
#: in the graph is a licensing decision, not a dependency bump, and it has
#: to be made by a person.
_FATAL_COPYLEFT = re.compile(r"^(?:AGPL|LGPL|GPL|SSPL)\b", re.I)

#: Reported, not fatal. MPL-2.0 is file-level weak copyleft and is genuinely
#: present; see the notices file for what it obliges and how that is met.
_REPORTED_COPYLEFT = re.compile(r"^MPL\b", re.I)

#: Splits an SPDX expression into its identifiers. crates.io has three
#: spellings of the same dual licence in this tree alone ("MIT OR
#: Apache-2.0", "MIT/Apache-2.0", "Apache-2.0 / MIT"), plus one compound
#: ("(MIT OR Apache-2.0) AND Unicode-3.0"), so the split has to handle all
#: of them rather than the tidy one.
_SPDX_SPLIT = re.compile(r"\s+(?:OR|AND|WITH)\s+|[()/]|\s+")

#: Files a crate might ship its licence in. The underscore matters: tauri
#: names them LICENSE_MIT and LICENSE_APACHE-2.0, and a pattern that only
#: allowed a hyphen would report seven Tauri crates as shipping no licence
#: at all.
_LICENCE_FILENAME = re.compile(
    r"^(LICEN[SC]E|COPYING|UNLICENSE|NOTICE)([-._].*)?$", re.I)

#: A copyright NOTICE, as opposed to the word "Copyright" inside a licence's
#: own prose. Requiring (c), the symbol or a year after it drops "Copyright
#: License. Subject to the terms and conditions of" out of every Apache
#: text and "Copyright and Related Rights" out of the CC0 one.
_COPYRIGHT = re.compile(
    r"^[ \t*#/]*(Copyright\s+(?:\(c\)|\(C\)|©|[0-9]).*?)[ \t]*$", re.M)

#: The Apache appendix's fill-in-the-blank line is not a notice.
_COPYRIGHT_PLACEHOLDER = re.compile(
    r"\[yyyy\]|\[name of copyright owner\]", re.I)


def spdx_atoms(expression):
    return sorted({
        tok for tok in _SPDX_SPLIT.split(expression or "")
        if tok and tok.upper() not in ("OR", "AND", "WITH")})


def cargo_metadata():
    """Resolve desktop/tauri's dependency graph, for Windows, from the lock.

    --locked so the answer is Cargo.lock's and not whatever cargo felt like
    resolving today; --filter-platform so a crate reachable only through a
    unix cfg does not appear in the inventory of a Windows installer.
    """
    _require(os.path.join(TAURI_DIR, "Cargo.lock"),
             "The crate inventory is derived from the lock file, which is "
             "committed. Without it there is nothing to enumerate.")
    argv = ["cargo", "metadata", "--format-version", "1", "--locked",
            "--filter-platform", CARGO_TARGET]
    try:
        done = subprocess.run(argv, cwd=TAURI_DIR, capture_output=True,
                              shell=(os.name == "nt"))
    except OSError as exc:
        raise Missing(
            "cannot generate the notices: cargo did not run ({}).\n"
            "The Rust crate inventory is derived from `{}`, so a Rust "
            "toolchain has to be installed. See docs/packaging-windows.md."
            .format(exc, " ".join(argv)))
    if done.returncode != 0:
        raise SystemExit(
            "`{}` failed in desktop/tauri:\n{}\nThe inventory cannot be "
            "guessed; fix the resolve and run this again.".format(
                " ".join(argv), done.stderr.decode("utf-8", "replace").strip()))
    # cargo writes a BOM on some Windows shells and json.loads will not eat it.
    return json.loads(done.stdout.decode("utf-8-sig"))


def linked_crates(meta):
    """Every crate that ends up inside Hearth.exe, and nothing else.

    Walks the resolve graph from the root package following only edges whose
    dep_kinds include a null kind, which is cargo's spelling of "normal
    dependency". Build-dependencies run on the build machine and
    dev-dependencies run in tests; neither is redistributed, exactly as
    electron-builder never entered the asar.
    """
    packages = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    root = meta["resolve"]["root"]
    if root is None:
        raise SystemExit(
            "cargo metadata reports no root package for desktop/tauri. The "
            "inventory is the root's normal dependency closure, so without "
            "a root there is nothing to walk.")

    seen, stack = set(), [root]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for dep in nodes[current]["deps"]:
            if any(kind.get("kind") is None
                   for kind in dep.get("dep_kinds", ())):
                stack.append(dep["pkg"])
    seen.discard(root)

    crates = []
    for pkg_id in seen:
        pkg = packages[pkg_id]
        licence = pkg.get("license")
        if not licence:
            raise SystemExit(
                "{} {} declares no licence at all. A crate with no licence "
                "field cannot be redistributed on the strength of a guess."
                .format(pkg["name"], pkg["version"]))
        crates.append(describe_crate(pkg, licence))
    crates.sort(key=lambda c: (c["name"].lower(), c["version"]))

    build_only = sorted(
        {p["name"] for p in packages.values()}
        - {c["name"] for c in crates}
        - {packages[root]["name"]},
        key=str.lower)
    return packages[root], crates, build_only


def describe_crate(pkg, licence):
    """Read a crate's own licence files out of the registry source cache.

    cargo unpacks each .crate archive next to the manifest it reports, so
    the licence files a crate ships are right there. Most carry one; the
    ones that carry none are named in the notices file rather than papered
    over.
    """
    directory = os.path.dirname(pkg["manifest_path"])
    files, notices, digests = [], [], set()
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not _LICENCE_FILENAME.match(name) or not os.path.isfile(path):
            continue
        files.append(name)
        body = _read_licence_file(path)
        digests.add(sha256_text(body))
        # No tidying of what is found. A copyright notice is somebody's
        # legal name and a range of years; trimming a trailing full stop
        # off "Unicode, Inc." would turn it into a different company.
        for found in _COPYRIGHT.findall(body):
            found = found.strip()
            if _COPYRIGHT_PLACEHOLDER.search(found) or found in notices:
                continue
            notices.append(found)
    return {
        "name": pkg["name"],
        "version": pkg["version"],
        "licence": licence,
        "atoms": spdx_atoms(licence),
        "dir": directory,
        "files": files,
        "digests": digests,
        "notices": notices,
    }


def classify_copyleft(crates):
    """Fail on GNU or SSPL, report MPL, say nothing about the rest.

    The Electron shell derived this from Chromium's own LICENSES.chromium
    .html. There is no such file for a Rust binary, and there does not need
    to be: cargo states every crate's licence in machine-readable form, so
    the scan is over the SPDX expressions themselves and it is exact rather
    than a text search that could miss a phrasing.
    """
    fatal, reported = [], []
    for crate in crates:
        for atom in crate["atoms"]:
            if _FATAL_COPYLEFT.match(atom):
                fatal.append("{} {} ({})".format(
                    crate["name"], crate["version"], crate["licence"]))
                break
        else:
            if any(_REPORTED_COPYLEFT.match(a) for a in crate["atoms"]):
                reported.append(crate)
    if fatal:
        raise SystemExit(
            "a GNU or SSPL licensed crate is linked into the shell:\n  "
            + "\n  ".join(fatal)
            + "\nHearth ships one statically linked executable, so this is a "
              "licensing decision and not a dependency bump. Remove the "
              "crate or decide, deliberately, to comply.")
    return reported


def verify_vendored_licences():
    """Check vendor/licenses/ against the digests recorded when it was made."""
    manifest = _read_json(_require(
        LICENSES_MANIFEST,
        "vendor/licenses/ holds the licence texts for components whose "
        "upstream release archive ships none, and one copy of each distinct "
        "licence the Rust crates use. It is committed; if it is gone, "
        "restore it from git rather than regenerating it."))
    texts = {}
    for name, entry in sorted(manifest["files"].items()):
        path = os.path.join(LICENSES_DIR, name)
        _require(path, "vendor/licenses/MANIFEST.json lists it.")
        body = _read(path)
        digest = sha256_text(body)
        if digest != entry["sha256"]:
            raise SystemExit(
                "vendor/licences/{} does not match its recorded digest.\n"
                "  recorded {}\n  actual   {}\n"
                "A licence text must not be edited. Restore it from git."
                .format(name, entry["sha256"], digest))
        texts[name] = body
    return manifest, texts


def verify_crate_licence_texts(crates, manifest):
    """Prove each reproduced licence text is still the crate's own bytes.

    The mapping from a licence to the crate it was taken from is asserted.
    That the committed copy is that crate's file, today, is not: it is
    rechecked here against the registry, so an upstream edit to a licence
    text turns into a failed run instead of a quietly stale quotation.
    """
    by_name = {}
    for crate in crates:
        by_name.setdefault(crate["name"], crate)

    taken = []
    for text in LICENCE_TEXTS:
        crate = by_name.get(text.crate)
        if crate is None:
            raise SystemExit(
                "vendor/licenses/{} was taken from the {} crate, which is no "
                "longer linked into the shell. Retake the {} text from a "
                "crate that is, and update LICENCE_TEXTS and MANIFEST.json."
                .format(text.text_file, text.crate, text.spdx))
        path = os.path.join(crate["dir"], text.filename)
        if not os.path.isfile(path):
            raise SystemExit(
                "{} {} no longer ships {}, so the {} text can no longer be "
                "traced to it. Retake it from a crate that does, and update "
                "LICENCE_TEXTS and MANIFEST.json.".format(
                    crate["name"], crate["version"], text.filename,
                    text.spdx))
        digest = sha256_text(_read_licence_file(path))
        recorded = manifest["files"][text.text_file]["sha256"]
        if digest != recorded:
            raise SystemExit(
                "{} {} now ships a different {}:\n  vendored {}\n  upstream "
                "{}\nRetake vendor/licenses/{} from it and record the new "
                "digest.".format(crate["name"], crate["version"],
                                 text.filename, recorded, digest,
                                 text.text_file))
        # How many of the crates ship exactly these bytes. It is the
        # measure of what deduplicating the texts actually saved, and it
        # is a count rather than a claim.
        shared = sum(1 for c in crates if digest in c["digests"])
        taken.append((text, crate, shared))
    return taken


def check_every_licence_has_a_text(crates):
    """No crate may be shipped under a licence whose text is nowhere."""
    have = {t.spdx for t in LICENCE_TEXTS} | set(ATOMS_WITHOUT_TEXT)
    missing = {}
    for crate in crates:
        for atom in crate["atoms"]:
            if atom not in have:
                missing.setdefault(atom, []).append(
                    "{} {}".format(crate["name"], crate["version"]))
    if missing:
        raise SystemExit(
            "a crate is linked under a licence whose text this repository "
            "does not carry:\n  " + "\n  ".join(
                "{}: {}".format(atom, ", ".join(sorted(who)))
                for atom, who in sorted(missing.items()))
            + "\nAdd the text to vendor/licenses/ and an entry to "
              "LICENCE_TEXTS, or record in ATOMS_WITHOUT_TEXT why none is "
              "needed.")


def resource_layout():
    """Where the bundler puts things, read out of tauri.conf.json.

    Under Tauri the resources land beside the executable rather than under
    a resources\\ subdirectory, which is the opposite of where
    electron-builder put them. Reading the map rather than restating it
    means a change to the bundle configuration changes this file, and
    --check goes red, instead of the notices quietly naming paths that no
    longer exist.
    """
    conf = _read_json(_require(
        TAURI_CONF, "It is committed, and it is what decides where the "
                    "licence files land in an install."))
    resources = conf.get("bundle", {}).get("resources", {})
    destinations = sorted(resources.values())
    for needed in ("hearth", "python"):
        if needed not in destinations:
            raise SystemExit(
                "tauri.conf.json no longer copies anything to {}\\, but the "
                "component table says the payload lands there. One of the "
                "two is wrong.".format(needed))
    webview = conf.get("bundle", {}).get("windows", {}).get(
        "webviewInstallMode", {})
    return {
        "version": conf["version"],
        "resources": resources,
        "root_files": sorted(
            dest for dest in destinations if "." in dest),
        "webview_mode": webview.get("type", "(unset)"),
    }


def verify_own_licence():
    """Prove LICENSE is the canonical Apache-2.0 text, byte for byte."""
    path = _require(os.path.join(REPO_ROOT, "LICENSE"),
                    "Hearth's own licence must exist before it can claim one.")
    size = os.path.getsize(path)
    digest = sha256_file(path)
    if digest != APACHE_2_0_SHA256 or size != APACHE_2_0_BYTES:
        raise SystemExit(
            "LICENSE is not the canonical Apache-2.0 text.\n"
            "  expected {} bytes, sha256 {}\n"
            "  actual   {} bytes, sha256 {}\n"
            "The licence text is not a place for edits; the copyright line "
            "belongs in NOTICE.".format(
                APACHE_2_0_BYTES, APACHE_2_0_SHA256, size, digest))
    return digest


def gather():
    """Read every source. Fails loudly rather than emitting a partial file."""
    llama = _read_json(_require(LLAMA_MANIFEST, "It is committed."))
    python = _read_json(_require(PYTHON_MANIFEST, "It is committed."))

    _require(LLAMA_DIR,
             "Run: python scripts/build_windows.py --skip-build\n"
             "The notices must describe the engine that will actually ship, "
             "so the engine has to be on disk.")
    _require(os.path.join(PYTHON_DIR, "LICENSE.txt"),
             "Run: python scripts/build_windows.py --skip-build\n"
             "CPython's own licence file is what satisfies the PSF licence "
             "in the installed application, so its presence is checked here "
             "rather than assumed.")

    layout = resource_layout()
    root, crates, build_only = linked_crates(cargo_metadata())
    if root["version"] != layout["version"]:
        raise SystemExit(
            "desktop/tauri/Cargo.toml says version {} and tauri.conf.json "
            "says {}. The installer would carry one and the notices the "
            "other.".format(root["version"], layout["version"]))
    check_every_licence_has_a_text(crates)
    mpl = classify_copyleft(crates)

    engine_files = sorted(
        n for n in os.listdir(LLAMA_DIR)
        if n.lower().endswith((".exe", ".dll")))
    licences_manifest, licence_texts = verify_vendored_licences()

    spread = {}
    for crate in crates:
        spread[crate["licence"]] = spread.get(crate["licence"], 0) + 1

    return {
        "apache_sha256": verify_own_licence(),
        "llama": llama,
        "python": python,
        "python_licence_sha256": sha256_file(
            os.path.join(PYTHON_DIR, "LICENSE.txt")),
        "app_version": root["version"],
        "shell_licence": root["license"],
        "layout": layout,
        "crates": crates,
        "build_only": build_only,
        # Apache-2.0 section 4(d) makes a crate's NOTICE file, if it has
        # one, travel with every redistribution. Checked rather than
        # assumed, because the answer is only "none" until it is not.
        "notice_files": [
            "{} {} ({})".format(c["name"], c["version"], name)
            for c in crates for name in c["files"]
            if name.upper().startswith("NOTICE")],
        "spread": sorted(spread.items(), key=lambda kv: (-kv[1], kv[0])),
        "mpl": mpl,
        "inventory_sha256": sha256_text("\n".join(
            "{} {} {}".format(c["name"], c["version"], c["licence"])
            for c in crates) + "\n"),
        "engine_files": engine_files,
        "licences_manifest": licences_manifest,
        "licence_texts": licence_texts,
        "crate_texts": verify_crate_licence_texts(crates, licences_manifest),
    }


# --------------------------------------------------------------------------
# Rendering.
# --------------------------------------------------------------------------

def _table(rows, headers):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |",
           "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    for row in rows:
        out.append("| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(row)) + " |")
    return "\n".join(out)


def _wrapped(names, width=74):
    """Lay a long list of crate names out as prose that a diff can read."""
    lines, current = [], ""
    for name in names:
        candidate = name if not current else current + ", " + name
        if len(candidate) > width and current:
            lines.append(current + ",")
            current = name
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def render(facts):
    llama = facts["llama"]
    python = facts["python"]
    layout = facts["layout"]
    crates = facts["crates"]
    variant = llama["variants"][llama["bundled_variant"]]

    out = []
    w = out.append

    w("# Third-party notices")
    w("")
    w("Hearth is licensed under the Apache License, Version 2.0. See LICENSE")
    w("and NOTICE. Everything below is somebody else's work, redistributed")
    w("under its own terms.")
    w("")
    w("This file is generated by `scripts/third_party_notices.py` from the")
    w("vendored trees and from `cargo metadata`, not written by hand.")
    w("Regenerate it after any version bump; `--check` fails the build when it")
    w("is stale. The receipt at the end records the digests it was derived")
    w("from.")
    w("")
    w("An installed application carries it, and the two licences it refers")
    w("to, at the application root, put there by")
    w("`desktop/tauri/tauri.conf.json`:")
    w("")
    for name in layout["root_files"]:
        w("    {}\\{}".format(INSTALL_ROOT, name))
    w("")
    w("and again inside `{}\\hearth\\`, where".format(INSTALL_ROOT))
    w("`scripts/build_windows.py` stages them next to the code they describe,")
    w("together with `vendor\\licenses\\`. Tauri puts resources beside the")
    w("executable rather than under a `resources\\` directory, so none of")
    w("those paths has `resources\\` in it.")
    w("")

    w("## Versions this file describes")
    w("")
    tauri_crate = next(
        (c for c in crates if c["name"] == "tauri"), None)
    rows = [["Hearth", facts["app_version"], facts["shell_licence"]]]
    if tauri_crate:
        rows.append(["Tauri", tauri_crate["version"], tauri_crate["licence"]])
    rows.extend([
        ["llama.cpp", "{} ({})".format(llama["release_tag"],
                                       llama["bundled_variant"]), "MIT"],
        ["CPython", python["version"] + " (windows-embeddable)",
         "PSF License 2.0"],
    ])
    w(_table(rows, ["Component", "Version", "Licence"]))
    w("")
    w("llama.cpp is pinned at commit `{}`.".format(llama["commit"]))
    w("The bundled archive is `{}`,".format(variant["asset"]))
    w("sha256 `{}`.".format(variant["sha256"]))
    w("The CPython archive is `{}`,".format(python["artifact"]["asset"]))
    w("sha256 `{}`.".format(python["artifact"]["sha256"]))
    w("")

    w("## What the installer contains")
    w("")
    w("Paths are relative to the install directory, `{}`.".format(INSTALL_ROOT))
    w("")
    w(_table([[c.name, c.licence, "`{}`".format(c.where)] for c in COMPONENTS],
             ["Component", "Licence", "Where it lands"]))
    w("")
    w("The engine directory carries {} executables and libraries:".format(
        len(facts["engine_files"])))
    w("")
    for name in facts["engine_files"]:
        w("- `{}`".format(name))
    w("")
    w("There is no Chromium, no Node.js and no V8 in an install. The shell is")
    w("a single Rust executable that asks Windows for a webview; the runtime")
    w("that renders the UI is Microsoft's and is not redistributed, which is")
    w("its own heading below.")
    w("")

    w("## The Rust crates linked into Hearth.exe")
    w("")
    w("`cargo metadata --locked --filter-platform {}`,".format(CARGO_TARGET))
    w("run in `desktop/tauri`, resolves the graph that `Cargo.lock` pins. The")
    w("inventory below is the root package's closure over normal dependency")
    w("edges only. Build-dependencies are excluded because they run on the")
    w("build machine and no byte of them enters the executable; they are named")
    w("further down so that the exclusion is on the record.")
    w("")
    w("**{} crates** are linked in, under these licence expressions:".format(
        len(crates)))
    w("")
    w(_table([[expr, str(count)] for expr, count in facts["spread"]],
             ["SPDX expression", "Crates"]))
    w("")
    w("The spelling varies because crates.io records whatever the crate")
    w("author wrote: `MIT OR Apache-2.0`, `MIT/Apache-2.0` and")
    w("`Apache-2.0 / MIT` are the same offer written three ways. The table is")
    w("not normalised, because normalising it would mean this file asserting")
    w("something upstream did not say.")
    w("")
    w("### Inventory")
    w("")
    w("Every crate, its version, its SPDX expression, and the licence files it")
    w("ships in its own published archive.")
    w("")
    w(_table([[c["name"], c["version"], c["licence"],
               ", ".join(c["files"]) or "(none)"] for c in crates],
             ["Crate", "Version", "Licence", "Licence files it ships"]))
    w("")

    w("### Copyright notices")
    w("")
    w("MIT and the BSD-style licences require the copyright notice to travel")
    w("with the software, and that notice differs per crate. These are read")
    w("out of the licence files each crate ships. Nothing here is written by")
    w("hand, and where a crate ships no notice this file says so rather than")
    w("inventing one.")
    w("")
    for crate in crates:
        if not crate["notices"]:
            continue
        if len(crate["notices"]) == 1:
            w("- **{} {}** {}".format(
                crate["name"], crate["version"], crate["notices"][0]))
        else:
            w("- **{} {}**".format(crate["name"], crate["version"]))
            for notice in crate["notices"]:
                w("  - {}".format(notice))
    w("")
    silent = ["{} {}".format(c["name"], c["version"])
              for c in crates if c["files"] and not c["notices"]]
    w("Of the crates above, {} ship a licence file that carries no copyright".format(
        len(silent)))
    w("notice at all. The MIT text several of them ship omits the notice line")
    w("entirely. That is upstream's choice, and this file does not fill it in:")
    w("")
    for line in _wrapped(silent):
        w("    " + line)
    w("")
    bare = ["{} {} ({})".format(c["name"], c["version"], c["licence"])
            for c in crates if not c["files"]]
    w("Another {} ship no licence file of any kind. The SPDX expression in".format(
        len(bare)))
    w("their `Cargo.toml` is the whole of what upstream states, it is in the")
    w("table above, and the text of every licence it names is either")
    w("reproduced at the end of this file or, for Apache-2.0, is `LICENSE`")
    w("itself:")
    w("")
    for line in _wrapped(bare):
        w("    " + line)
    w("")
    if facts["notice_files"]:
        w("These crates ship a NOTICE file, whose contents section 4(d) of")
        w("Apache-2.0 requires to travel with every redistribution:")
        w("")
        for line in _wrapped(facts["notice_files"]):
            w("    " + line)
    else:
        w("None of them ships a NOTICE file. Section 4(d) of Apache-2.0 would")
        w("require the contents of one to be carried along with the binary, so")
        w("the generator looks for one on every run and this sentence changes")
        w("if one ever appears.")
    w("")

    w("## Copyleft, and what it obliges")
    w("")
    w("Nothing in the installer is GPL, LGPL, AGPL or SSPL. The generator")
    w("splits every crate's SPDX expression into identifiers and fails the run")
    w("outright if one of those four appears, so a dependency bump that pulls")
    w("in a GNU-licensed crate stops the build rather than shipping. That")
    w("check is exact rather than a search for a phrasing: cargo states every")
    w("crate's licence in machine-readable form, so there is nothing to miss.")
    w("")
    w("Something genuinely different is present, though, and it is worth")
    w("being exact about. There are {} crates under MPL-2.0:".format(
        len(facts["mpl"])))
    w("")
    for crate in facts["mpl"]:
        w("- **{} {}** ({})".format(
            crate["name"], crate["version"], crate["licence"]))
    w("")
    w("The Mozilla Public License 2.0 is weak copyleft at file granularity.")
    w("It reaches the files that are already under it and nothing else:")
    w("section 3.3 says outright that a Larger Work may be distributed under")
    w("other terms, so Hearth's own Apache-2.0 code and the other crates in")
    w("the executable are unaffected. What it does oblige when a binary is")
    w("handed to someone, and how each obligation is met:")
    w("")
    w("1. **Tell recipients how to get the Source Code Form, and let them have it.**")
    w("   Section 3.2(a), at no more than the cost of distribution.")
    w("   Hearth links these crates exactly as published, with no")
    w("   patches, so the Source Code Form is the published crate itself and")
    w("   naming the exact version is what makes it obtainable. crates.io")
    w("   serves the archive for a name and a version indefinitely, and a")
    w("   version that is yanked stays downloadable:")
    w("")
    for crate in facts["mpl"]:
        w("   - <https://crates.io/api/v1/crates/{}/{}/download>".format(
            crate["name"], crate["version"]))
    w("")
    w("2. **Do not use the executable's own licence to cut those rights down.**")
    w("   Section 3.2(b). Hearth's Apache-2.0 grant covers Hearth's code. It")
    w("   says nothing about these files and takes nothing away from anyone's")
    w("   rights in them.")
    w("3. **Leave the notices in the covered source alone.** Section 3.4. The")
    w("   crates are linked as published and nothing is modified, so there are")
    w("   no Modifications and no altered notices. `selectors` is the one that")
    w("   ships no LICENSE file of its own; its MPL header is in its source")
    w("   files, which is what section 3.4 is about.")
    w("")
    w("Section 3.2 does not literally require the licence text to travel with")
    w("an executable, the way section 3.1 does for source. It ships anyway:")
    w("`vendor/licenses/licence-MPL-2.0.txt` is inside the application and is")
    w("reproduced at the end of this file, because a recipient who has to go")
    w("looking for the terms has been given the letter of the licence and not")
    w("the point of it.")
    w("")
    w("Two things this rests on that are worth stating rather than assuming.")
    w("If Hearth ever vendors a patched copy of one of these crates, the")
    w("patched files are Modifications, they have to be published under")
    w("MPL-2.0, and this section has to say where; nothing in the build does")
    w("that today, and the checksums cargo verifies against `Cargo.lock` are")
    w("what make that checkable rather than a promise. And the source links")
    w("above depend on crates.io continuing to serve those archives. If that")
    w("ever stops, the obligation falls back on Hearth: anyone entitled to the")
    w("Source Code Form may ask through the contact route in `SECURITY.md` and")
    w("will be given it.")
    w("")

    w("## The WebView2 runtime, which is not Hearth's to license")
    w("")
    w("The user interface is HTML rendered by the Microsoft Edge WebView2")
    w("Runtime. It is Microsoft's software, it is already present on the")
    w("machine, and **the installer does not redistribute it**. There is no")
    w("copy of it inside `Hearth-Setup-<version>.exe` and no line of it in")
    w("this file's inventory, because Hearth has no licence to grant over it.")
    w("")
    w("`desktop/tauri/tauri.conf.json` sets `webviewInstallMode` to")
    w("`{}`. If the runtime is missing, the".format(layout["webview_mode"]))
    w("installer downloads Microsoft's own bootstrapper from Microsoft and")
    w("runs it, and Microsoft's terms apply to what that installs. That is a")
    w("network fetch a user should know about, which is why it is written")
    w("here rather than left to be discovered.")
    w("")

    w("## Downloaded after installation, and therefore not covered above")
    w("")
    w("Hearth fetches some things on the user's machine, after install, at the")
    w("user's request. They are not in the installer and this file is not")
    w("their notice; they are listed so that nobody has to guess.")
    w("")
    w(_table([[n, l, why] for n, l, why in POST_INSTALL],
             ["What", "Licence", "When it is fetched"]))
    w("")

    w("## In the repository, but not redistributed")
    w("")
    for path, why in NOT_REDISTRIBUTED:
        w("- **`{}`** {}.".format(path, why))
    w("")
    w("The {} build-only crates, derived from the same resolve graph as the".format(
        len(facts["build_only"])))
    w("inventory above and excluded from it because no edge to them is a")
    w("normal dependency:")
    w("")
    for line in _wrapped(facts["build_only"]):
        w("    " + line)
    w("")

    w("## Licence texts carried by the components themselves")
    w("")
    w("These arrive with their own licence file and are shipped unmodified.")
    w("Nothing below is reproduced in this document, because a copy would be")
    w("a second source of truth that could drift from the one that ships.")
    w("")
    w("- **CPython** `python\\LICENSE.txt`, sha256")
    w("  `{}`. It covers the PSF".format(facts["python_licence_sha256"]))
    w("  License and every licence CPython's own bundled components carry.")
    w("- **Hearth, and every crate that offers Apache-2.0** `LICENSE.hearth.txt`")
    w("  at the application root, and `hearth\\LICENSE`. Both are the canonical")
    w("  Apache License 2.0, byte for byte; see the receipt.")
    w("")

    w("## Licence texts Hearth has to supply")
    w("")
    w("The llama.cpp Windows release archive contains no licence file. Its")
    w("build embeds licence strings into the executables but exposes no way to")
    w("print them, so a user who installs Hearth would otherwise receive MIT")
    w("software with no MIT notice. The texts below were taken once from the")
    w("pinned upstream sources, are committed under `vendor/licenses/`, and are")
    w("verified against their recorded digests every time this file is")
    w("generated.")
    w("")
    for c in COMPONENTS:
        if not c.text_file:
            continue
        entry = facts["licences_manifest"]["files"][c.text_file]
        w("### {}".format(c.name))
        w("")
        w("Source: <{}>".format(entry["source"]))
        if entry.get("extracted"):
            w("")
            w("Extracted from the {}.".format(entry["extracted"]))
        w("")
        w("```")
        w(facts["licence_texts"][c.text_file].rstrip("\n"))
        w("```")
        w("")

    w("## Licence texts for the Rust crates")
    w("")
    w("Those {} crates share {} distinct licences, and mostly share the same".format(
        len(crates), len(LICENCE_TEXTS) + len(ATOMS_WITHOUT_TEXT)))
    w("bytes of them: each text below records how many of the crates ship a")
    w("copy that is byte-identical to it. Reproducing every crate's own copy")
    w("would produce a document nobody reads and would say the same thing")
    w("dozens of times over. So the inventory above is the part that has to be")
    w("complete, and each distinct licence appears once here.")
    w("")
    w("Each text was taken from a crate that actually ships it. The crate is")
    w("named, and the generator re-reads that crate's file on every run and")
    w("fails if the bytes have changed, so these are quotations that cannot go")
    w("stale silently. Where a text carries a copyright line it is that")
    w("crate's; every other crate's notice is in the list above.")
    w("")
    w("Not all of them are reproduced here:")
    w("")
    for spdx, why in sorted(ATOMS_WITHOUT_TEXT.items()):
        w("- **{}**, {}.".format(spdx, why))
    w("")
    for text, crate, shared in facts["crate_texts"]:
        entry = facts["licences_manifest"]["files"][text.text_file]
        w("### {}".format(text.spdx))
        w("")
        w("Taken from `{}` in the `{}` crate, version {},".format(
            text.filename, crate["name"], crate["version"]))
        w("published at <{}>.".format(entry["source"]))
        if shared == 1:
            w("It is the only crate in the inventory that ships this text.")
        else:
            w("Byte-identical to the copy shipped by {} of the crates in the"
              " inventory.".format(shared))
        w("")
        w("```")
        w(facts["licence_texts"][text.text_file].rstrip("\n"))
        w("```")
        w("")

    w("## Receipt")
    w("")
    w("Generated by `scripts/third_party_notices.py` on {}.".format(
        datetime.date.today().isoformat()))
    w("")
    w(_table([
        ["LICENSE (Apache-2.0)", facts["apache_sha256"]],
        ["the crate inventory, {} crates".format(len(crates)),
         facts["inventory_sha256"]],
        ["python/LICENSE.txt", facts["python_licence_sha256"]],
        [variant["asset"], variant["sha256"]],
        [python["artifact"]["asset"], python["artifact"]["sha256"]],
    ], ["File", "sha256"]))
    w("")
    w("The crate inventory digest is over one `name version licence` line per")
    w("crate, in the order printed above. It changes when the graph changes,")
    w("which is the point: `--check` then fails and the inventory is")
    w("regenerated rather than assumed.")
    w("")
    w("The Apache-2.0 digest above is the canonical one published at")
    w("<https://www.apache.org/licenses/LICENSE-2.0.txt>. `LICENSE` is that")
    w("file byte for byte; the copyright line lives in `NOTICE`, which is")
    w("where section 4(d) of the licence puts it.")
    return "\n".join(out).rstrip("\n") + "\n"


# --------------------------------------------------------------------------

def generate():
    return render(gather())


def _stable(text):
    """Drop the generation date so --check does not fail once a day."""
    return re.sub(r"^Generated by .* on \d{4}-\d\d-\d\d\.$", "Generated.",
                  text, flags=re.M)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="third_party_notices.py",
        description="Generate THIRD-PARTY-NOTICES.md from what is vendored.")
    p.add_argument("--check", action="store_true",
                   help="exit non-zero if the committed file is out of date")
    p.add_argument("--self-test", action="store_true",
                   help="run the internal checks and exit")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return _self_test()

    fresh = generate()
    if args.check:
        if not os.path.exists(OUTPUT_PATH):
            print("THIRD-PARTY-NOTICES.md does not exist. Run this script.")
            return 1
        if _stable(_read(OUTPUT_PATH)) != _stable(fresh):
            print("THIRD-PARTY-NOTICES.md is out of date. Regenerate it:\n"
                  "    python scripts/third_party_notices.py")
            return 1
        print("THIRD-PARTY-NOTICES.md is current.")
        return 0

    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(fresh)
    print("wrote {} ({} bytes)".format(
        os.path.relpath(OUTPUT_PATH, REPO_ROOT), len(fresh.encode("utf-8"))))
    return 0


def _self_test():
    """Checks that hold without needing the vendored trees or a toolchain.

    The vendor directories are gitignored and cargo may not be installed, so
    a clean checkout cannot run the generator. These are the parts that must
    still be provable there: the licence texts are intact, Hearth's own
    licence is canonical, the SPDX splitter and the copyleft detector are
    not vacuous, the copyright reader tells a notice from prose, and the
    table renders.
    """
    failures = []
    total = [0]

    def check(name, ok, detail=""):
        total[0] += 1
        print("  {} {}{}".format("ok  " if ok else "FAIL", name,
                                 "" if ok else "  <- " + detail))
        if not ok:
            failures.append(name)

    print("third_party_notices self-test")

    try:
        digest = verify_own_licence()
        check("LICENSE is the canonical Apache-2.0 text",
              digest == APACHE_2_0_SHA256)
    except SystemExit as exc:
        check("LICENSE is the canonical Apache-2.0 text", False, str(exc))

    try:
        manifest, texts = verify_vendored_licences()
        check("vendor/licenses/ matches its recorded digests", True)
        wanted = {c.text_file for c in COMPONENTS if c.text_file}
        wanted |= {t.text_file for t in LICENCE_TEXTS}
        check("every component and licence that needs a text has one",
              wanted <= set(texts),
              "missing: " + ", ".join(sorted(wanted - set(texts))))
        orphans = sorted(set(texts) - wanted)
        check("no licence text is vendored without something that uses it",
              not orphans, "orphans: " + ", ".join(orphans))
    except SystemExit as exc:
        check("vendor/licenses/ matches its recorded digests", False, str(exc))

    check("no licence is both reproduced and declared unnecessary",
          not ({t.spdx for t in LICENCE_TEXTS} & set(ATOMS_WITHOUT_TEXT)))

    # crates.io records the same dual licence three ways in this tree alone,
    # so a splitter that only understood " OR " would silently miss two
    # thirds of the Apache-2.0 offers.
    check("SPDX splitter handles OR",
          spdx_atoms("MIT OR Apache-2.0") == ["Apache-2.0", "MIT"])
    check("SPDX splitter handles the slash spellings",
          spdx_atoms("MIT/Apache-2.0") == ["Apache-2.0", "MIT"]
          and spdx_atoms("Apache-2.0 / MIT") == ["Apache-2.0", "MIT"])
    check("SPDX splitter handles a compound expression",
          spdx_atoms("(MIT OR Apache-2.0) AND Unicode-3.0")
          == ["Apache-2.0", "MIT", "Unicode-3.0"])

    # Both directions matter. A detector that fired on anything containing
    # "GPL" would flag Apache-2.0 WITH LLVM-exception and be ignored; one
    # that fired on nothing would be worse.
    fatal = ["GPL-3.0-or-later", "LGPL-2.1-only", "AGPL-3.0", "SSPL-1.0"]
    check("copyleft detector fires on every GNU or SSPL identifier",
          all(_FATAL_COPYLEFT.match(a) for a in fatal))
    quiet = ["MIT", "Apache-2.0", "MPL-2.0", "Unicode-3.0", "Zlib",
             "LLVM-exception", "0BSD", "Unlicense", "CC0-1.0"]
    check("copyleft detector stays quiet on everything else",
          not any(_FATAL_COPYLEFT.match(a) for a in quiet))
    check("MPL is reported rather than fatal",
          bool(_REPORTED_COPYLEFT.match("MPL-2.0"))
          and not _FATAL_COPYLEFT.match("MPL-2.0"))

    # The Apache text says "Copyright License. Subject to the terms and
    # conditions of" and "Copyright [yyyy] [name of copyright owner]", and
    # the CC0 text says "Copyright and Related Rights". None of the three is
    # a notice, and reprinting them as one would be worse than printing
    # nothing.
    notice = "Copyright (c) 2015 Andrew Gallant\n"
    prose = ("  3. Grant of Copyright License. Subject to the terms and\n"
             "Copyright [yyyy] [name of copyright owner]\n"
             "Copyright and Related Rights include, but are not limited to\n")
    found = [m for m in _COPYRIGHT.findall(notice + prose)
             if not _COPYRIGHT_PLACEHOLDER.search(m)]
    check("copyright reader takes the notice and leaves the prose",
          found == ["Copyright (c) 2015 Andrew Gallant"], repr(found))

    check("licence filenames with an underscore are found",
          bool(_LICENCE_FILENAME.match("LICENSE_APACHE-2.0"))
          and bool(_LICENCE_FILENAME.match("UNLICENSE"))
          and not _LICENCE_FILENAME.match("LICENSES.md.bak.rs"))

    rendered = _table([["a", "bb"], ["ccc", "d"]], ["h1", "h2"])
    check("table renders with aligned columns",
          rendered.count("\n") == 3 and rendered.startswith("| h1  | h2 |"),
          repr(rendered))

    check("NOTICE exists", os.path.isfile(os.path.join(REPO_ROOT, "NOTICE")))

    # The generator must refuse rather than improvise when a source is gone.
    try:
        _require(os.path.join(REPO_ROOT, "no-such-file"), "because.")
        check("a missing source is fatal", False, "it returned instead")
    except Missing:
        check("a missing source is fatal", True)

    print("{} checks, {} failed".format(total[0], len(failures))
          if failures else "all checks green")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
