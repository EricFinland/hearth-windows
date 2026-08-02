# Licensing

Hearth is Apache-2.0. That is the whole of the interesting part for anyone
reading the source. The rest of this document is about the part that actually
constrains shipping: Hearth redistributes an Electron runtime, a CPython
interpreter and a llama.cpp inference engine, and every one of those carries
licence terms that require their notices to travel with the binary.

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
| Electron version | `desktop/shell/package.json` |
| the Chromium, Node and V8 inventory, plus a copyleft scan | `desktop/shell/node_modules/electron/dist/LICENSES.chromium.html` |
| the licence texts Hearth has to supply | `vendor/licenses/` |

It refuses to run when a source is missing rather than emitting a notices file
with holes in it, because a file that looks complete and is not is worse than no
file.

### vendor/licenses/

The llama.cpp Windows release archive contains **no licence file of any kind**.
Its build embeds licence strings into the executables through a CMake helper but
exposes no flag that prints them, so a user who installed Hearth would receive
MIT-licensed software with no MIT notice anywhere on their disk. That is a
compliance gap that only Hearth can close.

`vendor/licenses/` holds those texts, taken once from the pinned upstream
sources, committed, and recorded in `MANIFEST.json` with the URL each came from
and the sha256 of the bytes written. The generator verifies every digest on
every run, so a licence text cannot be quietly edited.

This directory is committed on purpose, unlike the rest of `vendor/`. The
binaries under `vendor/llama/` and `vendor/python/` are reproducible from their
manifests and are gitignored. Licence texts are not reproducible from anything
in the repository, and fetching them at build time would make a network failure
into a compliance failure.

## Copyleft, and where it actually bites

Two components inside the Electron runtime are LGPL:

- **FFmpeg** (`ffmpeg.dll`), LGPL-2.1-or-later. Electron builds it without
  `--enable-gpl`, so the GPL-only parts are absent.
- **Blink**, which descends from WebKit and KHTML, is LGPL-2.0-or-later and
  LGPL-2.1-or-later in part, statically linked into `Hearth.exe`.

Neither is combined with Hearth's own code at the source level. Hearth is Python
and JavaScript running on top of a runtime it did not build, so the aggregate is
an application plus a separately licensed runtime, not a derivative work of
either library. Apache-2.0 for Hearth's own code is unaffected.

The redistribution obligations are real regardless, and
`THIRD-PARTY-NOTICES.md` spells out how each is met: the notice, the licence
texts (which ship in `LICENSES.chromium.html`), the source availability, and the
right to relink. The one that needs a human to keep a promise is source
availability: anyone entitled to the corresponding source under the LGPL can ask
through the contact route in `SECURITY.md` and must be given it.

The generator records which of the components in `LICENSES.chromium.html` grant
a GNU licence for themselves. That list is a tripwire, not an analysis: when an
Electron bump adds a genuinely new copyleft dependency, the list changes,
`--check` goes red, and somebody looks. Without it, a new GPL dependency would
arrive silently inside a 20 MB HTML file that nobody reads.

## Where the notices have to land in an install

`LICENSE.electron.txt` and `LICENSES.chromium.html` already reach the
application root: electron-builder copies both out of the Electron runtime and
renames the first. CPython's `LICENSE.txt` already reaches
`resources\python\LICENSE.txt`, because the whole vendored interpreter directory
is copied verbatim. Those three cover Electron, Chromium, Node, V8 and every
component CPython bundles.

**Nothing else does.** Hearth's own licence, the notices file, and the llama.cpp
licence texts are not in the installer today.

### Required build change

`scripts/build_windows.py`, in `stage()`, after the payload trees are copied and
before `verify_stage()` runs, must copy four things into `build/stage/hearth`:

    LICENSE                  ->  build/stage/hearth/LICENSE
    NOTICE                   ->  build/stage/hearth/NOTICE
    THIRD-PARTY-NOTICES.md   ->  build/stage/hearth/THIRD-PARTY-NOTICES.md
    vendor/licenses/         ->  build/stage/hearth/vendor/licenses/

which lands them at, respectively:

    %LOCALAPPDATA%\Programs\Hearth\resources\hearth\LICENSE
    %LOCALAPPDATA%\Programs\Hearth\resources\hearth\NOTICE
    %LOCALAPPDATA%\Programs\Hearth\resources\hearth\THIRD-PARTY-NOTICES.md
    %LOCALAPPDATA%\Programs\Hearth\resources\hearth\vendor\licenses\*

`vendor/licenses/` has to travel because `THIRD-PARTY-NOTICES.md` quotes those
texts inline; shipping the directory as well costs about 20 KB and means the
texts exist as their own files rather than only as quotations.

`verify_stage()` should then assert the four are present, for the same reason it
already asserts the update trust anchor is present: a build that silently drops
them ships an installer that is out of compliance and looks identical to one
that is not.

The generator should also be run as a gate. Immediately after the two vendor
steps, and before staging:

    python scripts/third_party_notices.py --check

fails the build when the notices no longer describe what is about to be
packaged, which is exactly the moment a version bump would otherwise slip
through.

### Optional, and better if the shell owner wants it

Putting the notices at the **application root**, next to
`LICENSE.electron.txt`, is more discoverable than burying them under
`resources\hearth`. That needs an `extraFiles` entry in
`desktop/shell/package.json` rather than a change to the build script:

```json
"extraFiles": [
  { "from": "../../LICENSE", "to": "LICENSE.hearth.txt" },
  { "from": "../../NOTICE", "to": "NOTICE.txt" },
  { "from": "../../THIRD-PARTY-NOTICES.md", "to": "THIRD-PARTY-NOTICES.md" }
]
```

Separately, `desktop/shell/package.json` still declares `"license": "MIT"`. That
field ends up in the packaged `app.asar` and is now wrong; it should read
`"Apache-2.0"`.

## What is not redistributed

`desktop/shell/node_modules` is packaging tooling. `desktop/shell/package.json`
lists exactly five files under `files`, and none of them is a dependency, so
electron-builder and its two hundred transitive packages never enter the asar or
the resources directory. `site/node_modules` builds the documentation site.
Neither needs a notice.

The GPU engines, the CUDA runtime and the model weights are fetched after
installation, at the user's request, and are listed in the notices file under a
heading that says so. Two of them are not open source: the NVIDIA CUDA
redistributables and parts of the Intel and AMD runtimes. The default first-run
GPU fetch is Vulkan, which pulls nothing proprietary.

## SignPath Foundation readiness

SignPath Foundation signs open-source projects for free, which is the route out
of the SmartScreen warning described in
[packaging-windows.md](packaging-windows.md). Their published conditions and
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
| Product name and version metadata enforced | met, electron-builder sets both |
| A page headed "Code signing policy" on the project site | not yet |
| Privacy statement, or a statement that nothing is transferred | not yet, and Hearth is well placed to make the strong version of this claim |
| Uninstall facility | met, NSIS registers one |

Two things deserve attention before applying rather than after:

**Proprietary components.** The conditions say a project "may not contain any
proprietary, non open-source component," with a carve-out for system libraries.
The installer contains `vcruntime140.dll` and `vcruntime140_1.dll` from the
Microsoft Visual C++ runtime, redistributed by python.org inside the embeddable
package, and `dxil.dll` and `d3dcompiler_47.dll` from the Electron runtime.
These are Microsoft system libraries in the ordinary sense of the term and are
the obvious intended carve-out, but it is worth asking rather than assuming. The
NVIDIA CUDA redistributables are unambiguously proprietary and are **not** in
the installer; they are fetched later, only on explicit request. Keeping it that
way keeps the signed package clean.

**GitHub-hosted runners.** For the free tier, every job in the workflow leading
to the signing request must run on GitHub-hosted agents. The Windows build
currently runs locally. Moving it to `windows-latest` is the real work item, and
it is worth doing on its own merits: it makes the build reproducible by someone
other than its author, which is also what origin verification checks.

Note also that the certificate is issued to SignPath Foundation, so the
publisher shown by SmartScreen and UAC is "SignPath Foundation", not "Hearth"
and not "Eric Catalano".
