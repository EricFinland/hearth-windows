#!/usr/bin/env python3
"""Generate THIRD-PARTY-NOTICES.md from what is actually vendored.

    python scripts/third_party_notices.py            write the file
    python scripts/third_party_notices.py --check    fail if it is stale
    python scripts/third_party_notices.py --self-test

Hearth is Apache-2.0, but the thing a user installs is mostly other
people's software: an Electron runtime, a CPython interpreter and a
llama.cpp inference engine, each of which carries its own dependency tree.
Every one of those licences requires that its notice travel with the
binary. This script exists so that the notice file is DERIVED rather than
remembered, because a hand-written inventory is wrong the first time a
version is bumped and nobody notices until somebody complains.

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
  Electron version        desktop/shell/package.json
  the Chromium/Node/V8 component inventory, its size, its digest, and a
  scan for components that grant a GNU licence
                          desktop/shell/node_modules/electron/dist/
                          LICENSES.chromium.html
  the licence texts for components whose upstream release archive ships
  none, together with the URL each was taken from and its digest
                          vendor/licenses/MANIFEST.json

What is asserted rather than derived is the COMPONENTS table below: the
human-readable description of each component and the SPDX name of its
licence. A licence identifier cannot be computed from a DLL. Each entry
records the evidence it rests on so the assertion can be rechecked.

Refusing to run is the point
----------------------------
If vendor/llama, vendor/python or the Electron runtime is absent this
script fails instead of emitting a notice file with holes in it. Run
scripts/build_windows.py --skip-build first, or npm install in
desktop/shell. A notices file generated from a half-populated tree is
worse than no notices file, because it looks complete.

Standard library only, and no network. Every byte it reads is already on
disk; vendor/licenses/ was fetched once, is committed, and is verified
against its recorded digests on every run.
"""

import argparse
import datetime
import hashlib
import html
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(REPO_ROOT, "THIRD-PARTY-NOTICES.md")

LLAMA_MANIFEST = os.path.join(REPO_ROOT, "vendor", "llama_manifest.json")
PYTHON_MANIFEST = os.path.join(REPO_ROOT, "vendor", "python_manifest.json")
LICENSES_DIR = os.path.join(REPO_ROOT, "vendor", "licenses")
LICENSES_MANIFEST = os.path.join(LICENSES_DIR, "MANIFEST.json")
LLAMA_DIR = os.path.join(REPO_ROOT, "vendor", "llama")
PYTHON_DIR = os.path.join(REPO_ROOT, "vendor", "python")
SHELL_PACKAGE = os.path.join(REPO_ROOT, "desktop", "shell", "package.json")
ELECTRON_DIST = os.path.join(REPO_ROOT, "desktop", "shell", "node_modules",
                             "electron", "dist")
CHROMIUM_NOTICES = os.path.join(ELECTRON_DIST, "LICENSES.chromium.html")

#: The canonical Apache-2.0 text, so LICENSE can be proven unmodified rather
#: than eyeballed. Verified on 2026-08-02 against
#: https://www.apache.org/licenses/LICENSE-2.0.txt (11358 bytes, LF endings)
#: and against 288 independent copies inside LICENSES.chromium.html.
APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
APACHE_2_0_BYTES = 11358

#: Where the payload lands once electron-builder has run. hearth/ is
#: build/stage/hearth copied in as an extraResource; the two files at the
#: application root are placed there by electron-builder itself, which
#: renames Electron's own LICENSE on the way in.
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


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------
# The asserted half: what each redistributed component is and under what
# licence. Every row names the evidence behind its licence field, because
# "MIT" is a claim about a binary and a claim about a binary needs a source.
# --------------------------------------------------------------------------

class Component(object):
    def __init__(self, name, licence, where, evidence, text_file=None,
                 copyleft=False):
        self.name = name
        self.licence = licence
        #: Path inside the installed application, relative to INSTALL_ROOT.
        self.where = where
        self.evidence = evidence
        #: Filename in vendor/licenses/, when Hearth has to supply the text.
        self.text_file = text_file
        self.copyleft = copyleft


#: Shipped inside the installer. Ordered by which tree they arrive in.
COMPONENTS = (
    # ---- the inference engine -------------------------------------------
    Component(
        "llama.cpp and ggml", "MIT",
        r"resources\hearth\vendor\llama\*.exe, *.dll",
        "vendor/llama_manifest.json pins release b10105 at commit "
        "e6dd0e29a6751d4859abaa8899959f5ddf756f4e; that commit's LICENSE is "
        "MIT and is reproduced here because the Windows release archive "
        "contains no licence file at all",
        text_file="llama.cpp.txt"),
    Component(
        "cpp-httplib", "MIT",
        r"resources\hearth\vendor\llama\llama-server.exe (statically linked)",
        "vendored at vendor/cpp-httplib in the pinned llama.cpp commit and "
        "linked into llama-server",
        text_file="cpp-httplib.txt"),
    Component(
        "nlohmann/json", "MIT",
        r"resources\hearth\vendor\llama\llama-server.exe (statically linked)",
        "vendored at vendor/nlohmann in the pinned llama.cpp commit",
        text_file="nlohmann-json.txt"),
    Component(
        "stb_image", "MIT or public domain, at your option",
        r"resources\hearth\vendor\llama\mtmd.dll (statically linked)",
        "vendored at vendor/stb in the pinned llama.cpp commit; the licence "
        "block is carried at the end of stb_image.h",
        text_file="stb_image.txt"),
    Component(
        "sheredom/subprocess.h", "Unlicense (public domain)",
        r"resources\hearth\vendor\llama\*.exe (statically linked)",
        "vendored at vendor/sheredom in the pinned llama.cpp commit",
        text_file="subprocess.h.txt"),
    Component(
        "miniaudio", "public domain or MIT-0, at your option",
        r"resources\hearth\vendor\llama\*.exe (statically linked)",
        "vendored at vendor/miniaudio in the pinned llama.cpp commit",
        text_file="miniaudio.txt"),
    Component(
        "big-integer", "Unlicense (public domain)",
        r"resources\hearth\vendor\llama\llama-server.exe (embedded web UI)",
        "vendored at tools/ui/src/lib/vendors in the pinned llama.cpp "
        "commit; llama-server embeds the built web UI in the executable",
        text_file="big-integer.txt"),
    Component(
        "decimal.js", "MIT",
        r"resources\hearth\vendor\llama\llama-server.exe (embedded web UI)",
        "vendored at tools/ui/src/lib/vendors in the pinned llama.cpp commit",
        text_file="decimal.js.txt"),
    Component(
        "nerdamer-prime", "MIT",
        r"resources\hearth\vendor\llama\llama-server.exe (embedded web UI)",
        "vendored at tools/ui/src/lib/vendors in the pinned llama.cpp commit",
        text_file="nerdamer-prime.txt"),
    Component(
        "LLVM OpenMP runtime (libomp)",
        "University of Illinois/NCSA Open Source License and MIT, dual",
        r"resources\hearth\vendor\llama\libomp140.x86_64.dll",
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
        r"resources\python\*",
        "vendor/python_manifest.json pins 3.12.10; the distribution's own "
        "LICENSE.txt travels inside the archive and is copied into the "
        "installer unchanged",
        text_file=None),
    Component(
        "OpenSSL", "Apache-2.0",
        r"resources\python\libcrypto-3.dll, libssl-3.dll",
        "the DLLs' version resources read \"The OpenSSL Toolkit\" 3.0.16; "
        "OpenSSL 3.x is Apache-2.0. Bundled by python.org inside the "
        "embeddable package, and covered by the licence text that package "
        "carries",
        text_file=None),
    Component(
        "SQLite", "public domain",
        r"resources\python\sqlite3.dll",
        "the DLL's version resource reads SQLite3 3.49.1.0. SQLite is "
        "dedicated to the public domain and asks for no attribution; it is "
        "listed for completeness",
        text_file=None),
    Component(
        "libffi", "MIT",
        r"resources\python\libffi-8.dll",
        "bundled by python.org inside the embeddable package and covered by "
        "the licence text that package carries",
        text_file=None),
    Component(
        "expat, zlib, bzip2, xz/liblzma, libmpdec, Unicode data",
        "MIT, zlib, BSD-like, 0BSD and the Unicode licence",
        r"resources\python\python312.zip, *.pyd",
        "compiled into CPython; each licence is reproduced in full in the "
        "distribution's own LICENSE.txt, which ships",
        text_file=None),
    Component(
        "Microsoft Visual C++ runtime",
        "Microsoft Distributable Code terms",
        r"resources\python\vcruntime140.dll, vcruntime140_1.dll",
        "the DLLs' version resources read Microsoft Visual Studio "
        "14.42.34438.0. Not open source. Redistributed by python.org as part "
        "of the embeddable package under Microsoft's redistribution terms "
        "for the Visual C++ runtime, which permit shipping it with an "
        "application",
        text_file=None),

    # ---- the shell -------------------------------------------------------
    Component(
        "Electron", "MIT",
        r"Hearth.exe, LICENSE.electron.txt, and the runtime beside it",
        "desktop/shell/package.json pins the version; electron-builder "
        "copies Electron's own LICENSE to the application root and renames "
        "it LICENSE.electron.txt",
        text_file=None),
    Component(
        "Chromium, Node.js, V8 and their dependency tree",
        "BSD-3-Clause and several hundred others, enumerated in "
        "LICENSES.chromium.html",
        r"Hearth.exe, LICENSES.chromium.html, *.dll, *.pak, locales\*",
        "Chromium generates LICENSES.chromium.html from the metadata in its "
        "own source tree; Electron ships it and electron-builder copies it "
        "to the application root. It is the authoritative inventory and is "
        "not restated here",
        text_file=None),
    Component(
        "FFmpeg", "LGPL-2.1-or-later",
        r"ffmpeg.dll",
        "Electron ships a build configured without --enable-gpl, so the GPL "
        "parts of FFmpeg are absent and LGPL-2.1-or-later applies. The full "
        "LGPL-2.1 text is inside LICENSES.chromium.html under the ffmpeg "
        "entry",
        text_file=None, copyleft=True),
    Component(
        "Blink, derived from WebKit and KHTML",
        "LGPL-2.0-or-later and LGPL-2.1-or-later in part, BSD-3-Clause for "
        "the rest",
        r"Hearth.exe (statically linked)",
        "the WebKit entry in LICENSES.chromium.html carries the full GNU "
        "Library General Public License version 2 and the full GNU Lesser "
        "General Public License version 2.1",
        text_file=None, copyleft=True),
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
#: redistributed. electron-builder, its 200-odd transitive dependencies and
#: the docs site are all in this category: desktop/shell/package.json lists
#: exactly five files under "files", and node_modules is not among them.
NOT_REDISTRIBUTED = (
    ("desktop/shell/node_modules", "electron-builder and its dependency "
     "tree. Packaging tooling; nothing from it enters the asar or the "
     "resources directory"),
    ("site/node_modules", "Astro and Starlight, used to build the "
     "documentation site"),
)


# --------------------------------------------------------------------------
# The derived half.
# --------------------------------------------------------------------------

#: Phrasings by which a component's licence text grants a GNU licence FOR
#: THAT COMPONENT, as opposed to merely naming one. Chromium's tree is full
#: of the latter: MPL tri-licence headers offer the GPL as an alternative,
#: LLVM's licence file lists third parties, and several files note that they
#: are "GPL-compatible". Matching those would bury the two entries that
#: actually carry obligations.
_GNU_GRANT = re.compile(
    r"(is free software[;,:]? you can redistribute it and/or modify"
    r"|under the terms of the GNU (Lesser |Library )?General Public License"
    r"|licensed under the GNU (Lesser |Library )?General Public"
    r"|are under the GNU Lesser General Public License"
    r"|GNU (Lesser |Library )?General Public License [Vv]ersion [0-9.]+"
    r"\s*\n?\s*or later)")

_PRODUCT = re.compile(r'<div class="product">(.*?)</div>\s*</div>', re.S)
_TITLE = re.compile(r'<span class="title">(.*?)</span>', re.S)
_LICENCE = re.compile(r'<div class="license">\s*<pre>(.*?)</pre>', re.S)


def scan_chromium_notices(path):
    """Inventory LICENSES.chromium.html and flag its copyleft components.

    The count and the flagged list are written into the notices file as a
    tripwire. An Electron bump that adds a copyleft component changes the
    list, --check goes red, and a human looks at it. Without that, a new
    GPL dependency would arrive silently inside a 20 MB HTML file that
    nobody reads.
    """
    raw = _read(path)
    flagged = []
    products = 0
    for block in _PRODUCT.findall(raw):
        products += 1
        title = _TITLE.search(block)
        title = html.unescape(title.group(1)).strip() if title else "(untitled)"
        body = _LICENCE.search(block)
        if body and _GNU_GRANT.search(html.unescape(body.group(1))):
            flagged.append(title)
    return {
        "path": path,
        "products": products,
        "flagged": sorted(flagged, key=str.lower),
        "sha256": sha256_file(path),
        "bytes": os.path.getsize(path),
    }


def verify_vendored_licences():
    """Check vendor/licenses/ against the digests recorded when it was made."""
    manifest = _read_json(_require(
        LICENSES_MANIFEST,
        "vendor/licenses/ holds the licence texts for components whose "
        "upstream release archive ships none. It is committed; if it is "
        "gone, restore it from git rather than regenerating it."))
    texts = {}
    for name, entry in sorted(manifest["files"].items()):
        path = os.path.join(LICENSES_DIR, name)
        _require(path, "vendor/licenses/MANIFEST.json lists it.")
        body = _read(path)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if digest != entry["sha256"]:
            raise SystemExit(
                "vendor/licences/{} does not match its recorded digest.\n"
                "  recorded {}\n  actual   {}\n"
                "A licence text must not be edited. Restore it from git."
                .format(name, entry["sha256"], digest))
        texts[name] = body
    return manifest, texts


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
    shell = _read_json(_require(SHELL_PACKAGE, "It is committed."))

    _require(LLAMA_DIR,
             "Run: python scripts/build_windows.py --skip-build\n"
             "The notices must describe the engine that will actually ship, "
             "so the engine has to be on disk.")
    _require(os.path.join(PYTHON_DIR, "LICENSE.txt"),
             "Run: python scripts/build_windows.py --skip-build\n"
             "CPython's own licence file is what satisfies the PSF licence "
             "in the installed application, so its presence is checked here "
             "rather than assumed.")
    _require(CHROMIUM_NOTICES,
             "Run: npm install in desktop/shell\n"
             "LICENSES.chromium.html is the authoritative inventory for the "
             "Chromium, Node and V8 trees. It cannot be summarised from "
             "memory.")

    engine_files = sorted(
        n for n in os.listdir(LLAMA_DIR)
        if n.lower().endswith((".exe", ".dll")))
    licences_manifest, licence_texts = verify_vendored_licences()

    return {
        "apache_sha256": verify_own_licence(),
        "llama": llama,
        "python": python,
        "python_licence_sha256": sha256_file(
            os.path.join(PYTHON_DIR, "LICENSE.txt")),
        "electron_version": shell["devDependencies"]["electron"],
        "app_version": shell["version"],
        "engine_files": engine_files,
        "chromium": scan_chromium_notices(CHROMIUM_NOTICES),
        "licences_manifest": licences_manifest,
        "licence_texts": licence_texts,
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


def render(facts):
    llama = facts["llama"]
    python = facts["python"]
    chromium = facts["chromium"]
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
    w("vendored trees themselves, not written by hand. Regenerate it after any")
    w("version bump; `--check` fails the build when it is stale. The receipt at")
    w("the end records the digests it was derived from.")
    w("")
    w("A copy of this file ships inside the installed application at")
    w("`{}\\resources\\hearth\\THIRD-PARTY-NOTICES.md`,".format(INSTALL_ROOT))
    w("together with `LICENSE` and `NOTICE`.")
    w("")

    w("## Versions this file describes")
    w("")
    w(_table([
        ["Hearth", facts["app_version"], "Apache-2.0"],
        ["Electron", facts["electron_version"], "MIT"],
        ["llama.cpp", "{} ({})".format(llama["release_tag"],
                                       llama["bundled_variant"]), "MIT"],
        ["CPython", python["version"] + " (windows-embeddable)",
         "PSF License 2.0"],
    ], ["Component", "Version", "Licence"]))
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
    rows = []
    for c in COMPONENTS:
        rows.append([c.name, c.licence, "`{}`".format(c.where)])
    w(_table(rows, ["Component", "Licence", "Where it lands"]))
    w("")
    w("The engine directory carries {} executables and libraries:".format(
        len(facts["engine_files"])))
    w("")
    for name in facts["engine_files"]:
        w("- `{}`".format(name))
    w("")

    w("## Copyleft components, and what they oblige")
    w("")
    w("Two components of the Electron runtime are licensed under the GNU")
    w("Lesser General Public License. Neither is combined with Hearth's own")
    w("code at the source level: Hearth is Python and JavaScript running on")
    w("top of a runtime it did not build, and the LGPL applies to that")
    w("runtime, not to the application. Redistributing it still carries")
    w("obligations, and they are met as follows.")
    w("")
    for c in COMPONENTS:
        if not c.copyleft:
            continue
        w("### {}".format(c.name))
        w("")
        w("- **Licence.** {}.".format(c.licence))
        w("- **Where.** `{}`.".format(c.where))
        w("- **Evidence.** {}.".format(c.evidence))
        w("")
    w("Obligations, and how each is discharged:")
    w("")
    w("1. **Notice that the library is used.** This file, shipped inside the")
    w("   application.")
    w("2. **A copy of the licence.** The complete GNU Lesser General Public")
    w("   License version 2.1 and GNU Library General Public License version 2")
    w("   texts ship at the application root in `LICENSES.chromium.html`,")
    w("   under the `ffmpeg` and `WebKit` entries.")
    w("3. **The corresponding source.** Both components are built from public")
    w("   sources at a version this file names. Electron {}".format(
        facts["electron_version"]))
    w("   is published at <https://github.com/electron/electron>, and its")
    w("   Chromium revision, including the FFmpeg and Blink trees, is")
    w("   reachable from that tag. Anyone entitled to the source under the")
    w("   LGPL may also request it through the contact route in SECURITY.md")
    w("   and it will be provided.")
    w("4. **The right to relink against a modified library.** `ffmpeg.dll` is")
    w("   a separate dynamic library in the install directory. Replacing it")
    w("   with a compatible build requires no cooperation from Hearth, which")
    w("   is what section 6 of the LGPL asks for. Blink is statically linked")
    w("   into the Electron executable; the remedy there is the same one")
    w("   every Chromium-derived product relies on, which is that the")
    w("   complete source of that executable is public and rebuildable.")
    w("")
    w("A scan of `LICENSES.chromium.html` flags {} of its {} components as".format(
        len(chromium["flagged"]), chromium["products"]))
    w("granting a GNU licence for themselves. Most are Android-only bundles")
    w("that a Windows build does not contain, or aggregate licence files that")
    w("quote the GPL on behalf of a sub-dependency. The list is recorded here")
    w("so that a future Electron bump which adds a genuinely new copyleft")
    w("component shows up as a diff rather than as nothing at all.")
    w("")
    for title in chromium["flagged"]:
        w("- {}".format(title))
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

    w("## Licence texts carried by the components themselves")
    w("")
    w("These arrive with their own licence file and are shipped unmodified.")
    w("Nothing below is reproduced in this document, because a copy would be")
    w("a second source of truth that could drift from the one that ships.")
    w("")
    w("- **CPython** `resources\\python\\LICENSE.txt`, sha256")
    w("  `{}`. It covers the PSF".format(facts["python_licence_sha256"]))
    w("  License and every licence CPython's own bundled components carry.")
    w("- **Electron** `LICENSE.electron.txt` at the application root.")
    w("- **Chromium, Node.js, V8 and their dependencies**")
    w("  `LICENSES.chromium.html` at the application root, {} components,".format(
        chromium["products"]))
    w("  {:.1f} MB, sha256".format(chromium["bytes"] / 1e6))
    w("  `{}`.".format(chromium["sha256"]))
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

    w("## Receipt")
    w("")
    w("Generated by `scripts/third_party_notices.py` on {}.".format(
        datetime.date.today().isoformat()))
    w("")
    w(_table([
        ["LICENSE (Apache-2.0)", facts["apache_sha256"]],
        ["LICENSES.chromium.html", chromium["sha256"]],
        ["resources/python/LICENSE.txt", facts["python_licence_sha256"]],
        [llama["variants"][llama["bundled_variant"]]["asset"],
         variant["sha256"]],
        [python["artifact"]["asset"], python["artifact"]["sha256"]],
    ], ["File", "sha256"]))
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
    """Checks that hold without needing the vendored trees on disk.

    The vendor directories are gitignored, so a clean checkout cannot run
    the generator. These are the parts that must still be provable there:
    the licence texts are intact, Hearth's own licence is canonical, the
    copyleft detector is not vacuous, and the table renders.
    """
    failures = []

    def check(name, ok, detail=""):
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
        check("every component that needs a supplied text has one",
              all(c.text_file in texts for c in COMPONENTS if c.text_file),
              "missing: " + ", ".join(
                  c.text_file for c in COMPONENTS
                  if c.text_file and c.text_file not in texts))
        orphans = sorted(set(texts) - {c.text_file for c in COMPONENTS})
        check("no licence text is vendored without a component that uses it",
              not orphans, "orphans: " + ", ".join(orphans))
    except SystemExit as exc:
        check("vendor/licenses/ matches its recorded digests", False, str(exc))

    # The copyleft detector has to fire on a real grant and stay quiet on a
    # mere mention. Both directions matter: a detector that matched every
    # occurrence of "GPL" would flag a third of the Chromium tree and be
    # ignored, and one that matched nothing would be worse.
    grants = "This library is free software; you can redistribute it and/or modify"
    mention = ("Historically, most, but not all, Python releases have also "
               "been GPL-compatible. See the GNU General Public License FAQ.")
    check("copyleft detector fires on a grant", bool(_GNU_GRANT.search(grants)))
    check("copyleft detector ignores a mention",
          not _GNU_GRANT.search(mention))

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

    print("{} checks, {} failed".format(
        7 + 2, len(failures)) if failures else "all checks green")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
