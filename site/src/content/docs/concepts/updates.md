---
title: Signed updates
description: The Ed25519 trust anchor, what it protects, and what it deliberately does not.
---

How Hearth decides whether to replace itself, what it verifies before it does,
and what an operator would have to do to publish a release. Nothing has been
published. There is no release server, no download page and no remote; this
document describes a mechanism, not an operation.

## The short version

Every release is described by a small JSON manifest signed with an Ed25519 key
whose public half is committed to this repository, in `release/trust.json`, and
copied into the installer. Hearth accepts an update only when an active key
from that file signed the manifest, and the installer's SHA-256 matches what
the manifest says. A release host cannot push code to Hearth users, because it
cannot produce a signature.

That is the whole of the trust model, and it is deliberately independent of
code signing, which does not exist yet.

## Why not an off-the-shelf updater

Every desktop toolchain ships one. electron-builder had `electron-updater`,
with `latest.yml` and publish providers; Tauri has `tauri-plugin-updater`,
with its own signing key and its own endpoint list. Both were read and
neither is used. The Electron one is set out below because it is the one that
was evaluated in depth, and the Tauri plugin lands in the same place for the
first reason and adds a second: it is a plugin, and a plugin is a thing the
renderer can be granted permission to call. The whole design here is that the
code which decides whether bytes are trustworthy is not the code that can act
on it, and a capability entry away from being callable from the page is too
close.

electron-updater was rejected for reasons specific to where Hearth actually
is:

* **Its integrity check is self-referential on an unsigned app.** On Windows it
  compares the downloaded installer against a `sha512` it read out of
  `latest.yml` moments earlier, fetched from the same host over the same
  connection. Against a compromised or substituted release host that is not a
  check: whoever serves the bytes serves the hash of those bytes. Its real
  defence is `verifyUpdateCodeSignature`, which shells out to PowerShell's
  `Get-AuthenticodeSignature` and compares the publisher name. Hearth has no
  certificate, so there is nothing to compare against, and the updater degrades
  to "the host said so" without saying that it has.
* **Code signing would not fully fix it.** Authenticode proves the installer
  was signed by a holder of the certificate. It says nothing about *which*
  signed installer, so it does not stop a rollback to an older, still validly
  signed build with a known bug in it.
* **Our own key protects users today.** It works on an unsigned build, it keeps
  working after a certificate is bought, and it does not depend on a purchase
  landing.

None of that is a criticism of `electron-updater` on a signed app with a
trusted host. It is a statement that Hearth is neither of those yet.

## What is checked, in order

`agent/hearth_update.py` does all of it, and can do nothing else: the module
imports no process spawning, no foreign-function machinery, no dynamic
evaluation, and its own self-test scans its source to keep it that way.

1. **Fetch the manifest.** One HTTPS GET of `<feed>/<channel>/manifest.json`,
   capped at 64 KiB, redirects allowed only within the feed's own origin.
2. **Verify the signature**, over the canonical serialization of the manifest's
   `signed` block, against an *active* key in the shipped `release/trust.json`.
   Nothing inside the block is inspected before this passes. A revoked key, an
   unknown key id, a missing signature list and an edited byte are all refused
   with the same result: nothing is downloaded and nothing changes.
3. **Check it is about this application and this channel.** `app_id` and
   `channel` are inside the signed block, so a real, signed manifest for the
   beta channel cannot be served to stable users.
4. **Check freshness.** Manifests carry `expires_at`, and an expired one is
   refused. This is the freeze attack: an attacker who can only *withhold*
   traffic replays the newest manifest they have forever, and version
   comparison never notices, because that manifest really is the newest one it
   has seen. An expiry turns silence into a visible failure. An operator cannot
   mint an eternal manifest either: more than 400 days of validity is refused.
5. **Refuse downgrades.** The offered version must be strictly greater than the
   installed one, and at or above a floor kept in the user's data directory
   that only ever moves up. The floor is raised when a manifest *verifies*, not
   when an update installs, so a user who declines 0.2.0 still cannot be walked
   back to 0.1.0. A manifest whose release date is older than the newest one
   already seen is refused too.
6. **Download and hash.** Bytes stream to a `.part` file and are hashed as they
   arrive, capped at exactly the signed size plus one byte. Free disk space is
   checked first. A mismatch, a short read, an over-long response, a full disk
   or a cancellation all delete the partial file; nothing outside the staging
   directory is written in any of those paths.
7. **Verify again on disk**, immediately before the file is handed on. The
   first check is over a stream; the second is over the file that will actually
   be opened.
8. **Stop.** `hearth_update.py` never runs anything.

## Who runs the installer

The desktop shell (`desktop/tauri/src/update.rs`), and only after three checks
of its own:

* the path must be inside the staging directory, which the shell derives itself
  from the same rule `agent/hearth_paths.py` uses rather than believing the path
  it was handed;
* the size and SHA-256 are **recomputed** from the file on disk. The sidecar
  already verified it, but a verified installer then sits on disk for as long as
  the user takes to click, and any process running as that user can overwrite it
  in that window. Hashing immediately before the spawn narrows the window to one
  syscall;
* the version must be strictly greater than the running one, which comes out
  of the executable's own resource block, written there by `tauri-build` at
  compile time. Under Electron that value came out of the asar and a fuse made
  the asar tamper-evident; this is the stronger form of the same answer,
  because there is no separate archive beside the binary to edit at all.
  Downgrade protection that only lived in the sidecar could be undone by
  editing a file next to it.

Then the user is shown the version and the full SHA-256 in a native dialog and
asked. Only a yes spawns the installer, detached, with NSIS's `/S`; Hearth quits
immediately afterwards so the sidecar, and with it `llama-server` and its VRAM,
is gone before the files are replaced.

The renderer cannot name a file and cannot pass a path. `window.hearth.installUpdate()`
is a request with no arguments.

## The default, and why

| | default | can be changed |
| --- | --- | --- |
| check for updates | **yes**, once per launch and at most every 6 hours | yes, a checkbox |
| download automatically | **no** | no |
| install automatically | **no** | no |

Hearth runs code on the user's machine. Silently replacing it while somebody is
using it is exactly the capability an attacker who compromised the release key
would want, and requiring a click bounds that blast radius by human attention
rather than by a timer. Being told about a security fix is the entire value of
an updater, so the check is on by default; it is one GET of a small signed JSON
document to a pinned host, with no identifier of any kind attached, and it can
be turned off. Downloading 117 MB unprompted onto a metered connection is rude,
and a staged installer sitting on disk is one more thing for a local attacker to
race, so neither happens without an explicit action.

## What protection exists, before and after code signing

**Today, unsigned:**

* A release host cannot push code to Hearth users. Neither can anyone who
  obtains a valid TLS certificate for it, or who controls DNS for it, or who is
  in the middle of the connection. They can withhold updates, and the manifest
  expiry bounds how long that goes unnoticed.
* A rollback to an older, genuinely signed release is refused.
* The installer's bytes are pinned by a hash inside a signed document, so a
  swapped artifact is refused even though the manifest is real.
* Windows itself performs no check. The user sees the same full-screen
  SmartScreen warning the first install produced. See
  [packaging-windows.md](/hearth-windows/reference/packaging/).

**After a certificate is in place,** everything above still holds, and:

* Windows verifies the installer's Authenticode signature when it runs, so a
  file swapped on disk after Hearth verified it is caught by the OS as well as
  by the shell's re-hash.
* SmartScreen stops warning once the certificate has reputation.

Neither replaces the other. The release key protects the *decision* to update;
Authenticode protects the *execution*. They fail differently and that is the
point of having both.

One thing that must not be forgotten when the certificate arrives:
`scripts/verify_binary.py` sets out what a build must not have in it before it
is signed. Under Electron that was seven fuses, five of which restrained a
JavaScript runtime this binary no longer contains. What remains is smaller and
still real: no inspector compiled in, no interpretable code on disk beside the
executable, and the code that disowns WebView2's environment present in the
shipped bytes. It is a hard build failure and it applies to every build the
updater ships, not only the first one.

## Nothing has been published

`release/trust.json` pins the feed at `releases.hearth.invalid`. `.invalid` is
reserved by RFC 2606 and can never resolve, so a shipped Hearth cannot fetch an
update from anywhere at all. The Updates panel says exactly that: *"This build
of Hearth carries no release feed, so it cannot check for updates. Nothing has
been published yet."* It does not say "up to date", because "we did not look"
and "we looked and there is nothing" are different facts, and reporting the
first as the second is the most common way an updater lies to people.

## What an operator would have to do to publish

None of this has been done. In order:

1. **Get a real signing key onto a machine that is not the release host.**
   The key currently in `release/trust.json` was generated on a development
   machine; its private half has never left that machine, and it should be
   replaced before anything is published:

       python scripts/release_manifest.py keygen --key-id hearth-release-<date>

   That writes `release/keys/<id>.key` (mode 0600, and `release/keys/` is
   gitignored) and adds the public half to `release/trust.json`. Move the key
   file to removable media or a password manager. It is never printed and never
   needed again until the next release.

2. **Decide on a host and put it in `release/trust.json`** as `feed`, ending in
   a slash. It must be https. Static file hosting is all that is required; the
   feed has no server-side logic.

3. **Rebuild.** `python scripts/build_windows.py`. The trust file is copied into
   the payload, and the build fails if it is missing or carries no active key.
   Every user who is to receive updates has to be running a build that carries
   the key, which is why key rotation means *ship first, sign later*.

4. **Sign the installer into a feed directory:**

       python scripts/release_manifest.py sign \
         --installer build/dist/Hearth-Setup-0.1.1.exe \
         --version 0.1.1 --key release/keys/<id>.key --out build/feed

   That reads the size and SHA-256 off the file (never from an argument),
   builds the signed block, signs it, lays out

       build/feed/stable/manifest.json
       build/feed/stable/0.1.1/Hearth-Setup-0.1.1.exe
       build/feed/index.html          a plain download page

   and then re-checks the result with the *client's* own verifier.

5. **Test it locally before it exists in public:**

       python scripts/release_manifest.py serve --feed build/feed
       HEARTH_UPDATE_FEED=http://127.0.0.1:<port>/ "%LOCALAPPDATA%\Programs\Hearth\Hearth.exe"

6. **Upload the feed directory** to the host from step 2, verbatim. Upload the
   installer before the manifest: a manifest that names an artifact which is not
   there yet is a broken feed, and the other order is never broken.

7. **Verify from outside**, with a client that is not the one that made it:

       python scripts/release_manifest.py verify --feed <a fresh download> --installed 0.1.0

`scripts/release_manifest.py` uploads nothing, has no credentials for any host,
and its `serve` binds loopback. Publishing is a deliberate, separate, human act.
It is also not staged into the installer, so a compromised Hearth install
contains no signing code and no path to a key.

## Where things live

| | |
| --- | --- |
| `release/trust.json` | the pinned public key and feed. Committed, and shipped. |
| `release/keys/*.key` | private signing seeds. Gitignored, and must not be on the release host. |
| `agent/hearth_ed25519.py` | Ed25519, standard library only. Checked against the RFC 8032 vectors and against OpenSSL. |
| `agent/hearth_update.py` | fetch, verify, refuse, stage. Cannot execute anything. |
| `scripts/release_manifest.py` | the operator's tool. Not shipped. |
| `desktop/tauri/src/update.rs` | the only code that runs an installer. |
| `desktop/ui/js/update.js` | the Updates panel. |
| `GET /update`, `POST /update`, `GET /update/events` | the sidecar's surface. |
| `%LOCALAPPDATA%\Hearth\update\` | the persisted floor, the settings, and staged installers. |

## Why Ed25519 is written out by hand

Python's standard library ships no asymmetric cryptography, and `agent/`,
`desktop/server/` and `scripts/` are standard library only. That rule is why a
clean checkout builds on a machine with nothing installed and why the shipped
payload has no third-party code in it. So the primitive is written against
RFC 8032, in `agent/hearth_ed25519.py`, using `hashlib.sha512` and Python
integers.

Verification involves no secret, so a pure-Python verifier is exactly as safe as
a C one and only slower, by about ten milliseconds per signature, once per
update check. **Signing** is different: Python's integer arithmetic is not
constant time, and signing multiplies the base point by a secret scalar. That is
acceptable because signing happens on the operator's own machine, offline, a
handful of times a year, with nobody measuring. If signing ever moves onto a
shared or network-facing machine it must move to a real implementation at the
same time.

Correctness is asserted three ways: the RFC 8032 §7.1 test vectors (bytes
produced by other people's implementations, so passing them means agreeing with
OpenSSL and libsodium rather than with itself), a mutation battery that flips
every byte of every signature, key and message in turn, and a cross-check
against OpenSSL 3.5.6 and pyca/cryptography over 200 random keypairs in which
the derived public keys and the signatures were byte-identical in both
directions.
