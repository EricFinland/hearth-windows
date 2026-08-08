---
title: Licensing
description: Apache-2.0, the vendored components, and how third-party notices are generated.
---

Hearth is Apache-2.0. That is the whole of the interesting part for anyone
reading the source. The rest of this document is about the part that actually
constrains shipping: Hearth redistributes a Tauri shell statically linked from a
couple of hundred Rust crates, a CPython interpreter and a llama.cpp inference
engine, and every one of those carries licence terms that require their notices
to travel with the binary.

## The three files at the repository root

    LICENSE                  the Apache License, Version 2.0, verbatim
    NOTICE                   the copyright line and the trademark reservation
    THIRD-PARTY-NOTICES.md   generated, never edited by hand

`LICENSE` is byte-identical to the canonical text at
<https://www.apache.org/licenses/LICENSE-2.0.txt>: 11358 bytes, LF endings,
sha256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`.
`scripts/third_party_notices.py --self-test` asserts that digest, so an edit to
the licence text fails a gate rather than sitting there unnoticed. The copyright
line is not in `LICENSE`, because putting it there would mean modifying the
licence to state it. It is in `NOTICE`, which is what section 4(d) of the
licence is for.

### Why Apache-2.0 and not MIT

Hearth started under MIT. Three things pushed it:

1. **The patent grant.** Section 3 grants a patent licence from every
   contributor and terminates it for anyone who sues over the software. MIT
   says nothing about patents at all.
2. **Trademark.** Section 6 explicitly withholds trademark rights. MIT is
   silent, which leaves the question of whether a fork may call itself Hearth to
   be argued rather than read.
3. **SignPath Foundation** requires an OSI-approved licence with no commercial
   dual-licensing. Apache-2.0 qualifies, and so did MIT, so this one is not the
   deciding factor; it is the reason the question came up.

Relicensing was possible because the copyright is held by one person and the
project had never been published, so there were no external contributions to
obtain permission for.

`desktop/tauri/Cargo.toml` declares `license = "Apache-2.0"`, which is what ends
up in the crate metadata and, through `tauri-winres`, in the executable's own
version resource.

### Why there are no per-file licence headers

Apache's own guidance suggests attaching the boilerplate notice to each source
file. Hearth does not, deliberately:

- There is one copyright holder and no external contributions, so a per-file
  header would resolve no ambiguity that exists.
- Hearth ships as a packaged application, not as files copied into other
  people's codebases, which is the situation per-file headers are designed for.
- The modules in `agent/` and `desktop/server/` open with long docstrings that
  are the primary documentation of the design. Eleven lines of boilerplate above
  each of them costs real readability for no legal gain.

If that calculus ever changes, the cheap version is a single
`# SPDX-License-Identifier: Apache-2.0` line per file rather than the full
boilerplate. It is a one-command change and nothing here depends on it not
happening.

## THIRD-PARTY-NOTICES.md is generated

    python scripts/third_party_notices.py            write it
    python scripts/third_party_notices.py --check    fail if it is stale
    python scripts/third_party_notices.py --self-test

A hand-maintained inventory is wrong the first time a version is bumped. The
generator derives everything it can from the vendored trees on disk:

| Field | Derived from |
|-------|--------------|
| llama.cpp version, commit, variant, digest | `vendor/llama_manifest.json` |
| the engine files that will ship | `vendor/llama/` |
| CPython version and digest | `vendor/python_manifest.json` |
| CPython's own licence file, and that it ships | `vendor/python/LICENSE.txt` |
| Hearth's version, every Rust crate in the executable, its SPDX expression, the licence files it ships, the copyright notices in them, and a copyleft scan | `cargo metadata --locked --filter-platform x86_64-pc-windows-msvc`, run in `desktop/tauri` |
| where the notices land in an install, and whether WebView2 is redistributed | `desktop/tauri/tauri.conf.json` |
| the licence texts Hearth has to supply | `vendor/licenses/` |

It refuses to run when a source is missing rather than emitting a notices file
with holes in it, because a file that looks complete and is not is worse than no
file.

### The crate inventory

`cargo metadata` is a subprocess, not an import, so the "standard library only"
rule in `scripts/` still holds. The inventory is the root package's closure over
**normal dependency edges only**, walked from `resolve.root`. Build-dependencies
are excluded on purpose and named in the notices file so the exclusion is on the
record: they run on the build machine and no byte of them enters the executable,
exactly as electron-builder and its two hundred transitive packages never
entered the asar.

`--locked` means the answer is `Cargo.lock`'s rather than whatever cargo felt
like resolving today. `--filter-platform` means a crate reachable only through a
unix `cfg` does not appear in the inventory of a Windows installer.

Each crate's `.crate` archive is unpacked into the cargo registry source cache
beside the `Cargo.toml` that `cargo metadata` reports, so the licence files a
crate publishes are right there to read. The generator lists them, reads the
copyright notices out of them, and says plainly which crates ship none rather
than inventing a notice for them.

### vendor/licenses/

Two different problems land in the same directory.

The llama.cpp Windows release archive contains **no licence file of any kind**.
Its build embeds licence strings into the executables through a CMake helper but
exposes no flag that prints them, so a user who installed Hearth would receive
MIT-licensed software with no MIT notice anywhere on their disk. That is a
compliance gap that only Hearth can close.

The Rust crates have the opposite problem: too many copies. Reproducing every
crate's own licence file would produce a document nobody reads and would say the
same thing dozens of times over, because the crates share a handful of texts
byte for byte. So the notices file carries a complete inventory table and one
copy of each distinct licence, taken from a crate that actually ships it. The
mapping from a licence to the crate it came from is asserted in
`LICENCE_TEXTS`; everything else is checked. The crate has to still be in the
inventory, its file on disk has to still hash to the recorded digest, and the
committed copy has to hash to the same thing, so an upstream edit to a licence
text fails the run instead of drifting.

Two licences named by the crates are deliberately not reproduced there.
Apache-2.0 is `LICENSE` itself, which already ships twice in an install; a
second copy would be a second source of truth. MIT-0 is offered by one crate,
`dunce`, which ships the CC0-1.0 text and no MIT-0 text, so CC0-1.0 is the
option taken. Any other identifier that turns up without a text fails the run,
which is what keeps the set complete rather than merely long.

`MANIFEST.json` records the URL each text came from and the sha256 of the bytes
written. The generator verifies every digest on every run, so a licence text
cannot be quietly edited.

This directory is committed on purpose, unlike the rest of `vendor/`. The
binaries under `vendor/llama/` and `vendor/python/` are reproducible from their
manifests and are gitignored. Licence texts are not reproducible from anything
in the repository, and fetching them at build time would make a network failure
into a compliance failure.

## Copyleft, and where it actually bites

Nothing in the installer is GPL, LGPL, AGPL or SSPL. The Electron shell had two
LGPL components, FFmpeg and Blink, and the notices file had to explain how four
separate obligations were met for them. The Tauri shell has neither, because it
has no browser engine in it: the renderer is Microsoft's WebView2, already on
the machine and not redistributed.

The tripwire that was a text search over Chromium's `LICENSES.chromium.html` is
now a split of every crate's SPDX expression into identifiers. GPL, LGPL, AGPL
and SSPL fail the run outright, so a dependency bump that pulls one in stops the
build rather than shipping. This check is exact rather than a search for a
phrasing that could be worded a new way.

Five crates are MPL-2.0: `cssparser`, `cssparser-macros`, `selectors`,
`dtoa-short` and `option-ext`. That is reported, not fatal, because it is real
and it is different from the rest. MPL-2.0 is weak copyleft at file granularity:
section 3.3 says outright that a Larger Work may be distributed under other
terms, so Hearth's own Apache-2.0 code and the other crates are unaffected, and
the obligation that does bite is section 3.2(a), which is source availability
for those specific files. Hearth links the crates exactly as published, so their
Source Code Form is the published crate, and naming the exact version is what
makes it obtainable: crates.io serves the archive for a name and a version
indefinitely, and a yanked version stays downloadable. `THIRD-PARTY-NOTICES.md`
names all five with versions and links.

Two things there need a human rather than a script. If Hearth ever vendors a
patched copy of one of those crates, the patched files are Modifications and
have to be published under MPL-2.0. And if crates.io ever stops serving those
archives, the obligation falls back on Hearth to supply the source on request,
through the contact route in `SECURITY.md`.

## Where the notices land in an install

Under Tauri the resources sit **beside** the executable rather than under a
`resources\` subdirectory, which is the opposite of where electron-builder put
them. Two mechanisms put the licence files there, and both are deliberate.

`desktop/tauri/tauri.conf.json` lists three of them under `bundle.resources`,
which lands them at the application root, where somebody looking for them would
look first:

    %LOCALAPPDATA%\Programs\Hearth\LICENSE.hearth.txt
    %LOCALAPPDATA%\Programs\Hearth\NOTICE.txt
    %LOCALAPPDATA%\Programs\Hearth\THIRD-PARTY-NOTICES.md

`scripts/build_windows.py`, in `stage()`, copies the same three plus the licence
texts into the payload, which lands them next to the code they describe:

    %LOCALAPPDATA%\Programs\Hearth\hearth\LICENSE
    %LOCALAPPDATA%\Programs\Hearth\hearth\NOTICE
    %LOCALAPPDATA%\Programs\Hearth\hearth\THIRD-PARTY-NOTICES.md
    %LOCALAPPDATA%\Programs\Hearth\hearth\vendor\licenses\*

`vendor/licenses/` travels because `THIRD-PARTY-NOTICES.md` quotes those texts
inline; shipping the directory as well costs about 60 KB and means the texts
exist as their own files rather than only as quotations.

CPython's own `LICENSE.txt` reaches `%LOCALAPPDATA%\Programs\Hearth\python\`,
because the whole vendored interpreter directory is copied verbatim. It covers
the PSF licence and every licence CPython's bundled components carry.
`LICENSE.electron.txt` and `LICENSES.chromium.html` used to sit at the
application root and no longer exist, because nothing they described is in the
installer any more.

`verify_stage()` asserts that the three files and a non-empty `vendor/licenses/`
are present, for the same reason it already asserts the update trust anchor is:
a build that silently drops them ships an installer that is out of compliance
and looks identical to one that is not.

The generator is a build gate too. `scripts/build_windows.py` runs

    python scripts/third_party_notices.py --check

between vendoring and staging, because that is the one moment when the versions
the notices claim are on disk and have not yet been wrapped in an installer. A
version bump that slips past it ships an installer whose compliance paperwork is
about a different program.

## What is not redistributed

**The Microsoft Edge WebView2 Runtime.** It renders the entire user interface
and it is not Hearth's to license. It is already present on supported Windows
installs, and `tauri.conf.json` sets `webviewInstallMode` to
`downloadBootstrapper`, so if it is missing the installer downloads Microsoft's
own bootstrapper from Microsoft and runs it. Nothing of WebView2 is inside the
installer, and Microsoft's terms apply to what that bootstrapper installs. The
notices file says so under its own heading, because a network fetch during
install is something a user is entitled to know about.

**cargo's build-dependencies.** `tauri-build` and the crates it pulls in run at
compile time to generate the asset embedding and the Windows resource block.
None of them is linked into the executable. They are named in the notices file
so that excluding them is a decision on the record rather than an omission.
`site/node_modules` builds the documentation site. Neither needs a notice.

The GPU engines, the CUDA runtime and the model weights are fetched after
installation, at the user's request, and are listed in the notices file under a
heading that says so. Two of them are not open source: the NVIDIA CUDA
redistributables and parts of the Intel and AMD runtimes. The default first-run
GPU fetch is Vulkan, which pulls nothing proprietary.

## SignPath Foundation readiness

SignPath Foundation signs open-source projects for free, which is the route out
of the SmartScreen warning described in
[packaging-windows.md](/hearth-windows/reference/packaging/). Their published conditions and
where Hearth stands:

| Requirement | Status |
|-------------|--------|
| OSI-approved licence, no commercial dual-licensing | met, Apache-2.0 |
| Public repository | not yet, the repository is private |
| Actively maintained | met |
| Already released in the form to be signed | not yet, nothing is published |
| Functionality described on the download page | met by README, needs a release page |
| No malware, no security-circumvention features | needs a judgement call, see below |
| Builds from a trusted CI, on GitHub-hosted runners | not yet, builds are local |
| Origin verification enabled, restricted to release branches | not yet |
| Every signing request approved by a human | process, not code |
| MFA on SignPath and on the source repository | outside this repository |
| Product name and version metadata enforced | met, `tauri.conf.json` sets both and `tauri-winres` writes them into the executable |
| A page headed "Code signing policy" on the project site | not yet |
| Privacy statement, or a statement that nothing is transferred | not yet, and Hearth is well placed to make the strong version of this claim |
| Uninstall facility | met, NSIS registers one |

Two things deserve attention before applying rather than after:

**Proprietary components.** The conditions say a project "may not contain any
proprietary, non open-source component," with a carve-out for system libraries.
The move off Electron shrank this to one item: `vcruntime140.dll` and
`vcruntime140_1.dll` from the Microsoft Visual C++ runtime, redistributed by
python.org inside the embeddable package. `dxil.dll` and `d3dcompiler_47.dll`
came from the Electron runtime and are gone. These are Microsoft system
libraries in the ordinary sense of the term and are the obvious intended
carve-out, but it is worth asking rather than assuming. WebView2 is proprietary
and is **not** in the installer at all. The NVIDIA CUDA redistributables are
unambiguously proprietary and are also not in the installer; they are fetched
later, only on explicit request. Keeping it that way keeps the signed package
clean.

**GitHub-hosted runners.** For the free tier, every job in the workflow leading
to the signing request must run on GitHub-hosted agents. The Windows build
currently runs locally. Moving it to `windows-latest` is the real work item, and
it is worth doing on its own merits: it makes the build reproducible by someone
other than its author, which is also what origin verification checks.

Note also that the certificate is issued to SignPath Foundation, so the
publisher shown by SmartScreen and UAC is "SignPath Foundation", not "Hearth"
and not "Eric Catalano".
