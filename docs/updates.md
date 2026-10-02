# Updating Hearth on Windows

How Hearth finds a new version, what it verifies before it installs one, and
how a release is published so that installed copies can find it. The feed is
this repository's
[GitHub releases page](https://github.com/EricFinland/hearth-windows/releases):
every release carries a signed `manifest-stable.json` next to its installer,
and Hearth checks for it once each time it starts.

**Updating, for people using Hearth:** when a new version is out, a banner
says so at the top of the window. Click **Install now**. Hearth downloads it,
checks it, asks once more with the version and its fingerprint in front of
you, then closes, updates and opens again. Your models, chats and settings are
kept. If a turn, the work loop or a swarm is still running (in any chat),
that last question says so first, because installing stops that work
partway. **Later** hides the banner until the next launch; the Updates panel in
the sidebar always has the full picture and a **Check for updates** button.

Hearth 0.1.1 and older cannot do this: they were built with a placeholder feed
and a key that has since been retired. Anyone on 0.1.1 installs the next
version by hand once (download it from the releases page and run it), and
every update after that happens inside the app.

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

1. **Fetch the manifest.** One HTTPS GET of
   `<feed>latest/download/manifest-<channel>.json`, capped at 64 KiB, with no
   query string and no header that identifies anybody (the User-Agent is the
   fixed string `hearth-updater`). GitHub answers with redirects to the
   versioned release and then to its asset host; those are followed at most
   five times, only over https on the default port, and only to `github.com`,
   `release-assets.githubusercontent.com` (where GitHub sends asset downloads
   today) or `objects.githubusercontent.com` (where it sent them until 2025).
   That list limits where a request can go. It is not part of deciding whether
   anything is trustworthy, which is still steps 2 to 7. A trust file without
   a `layout` uses the older directory layout, `<feed><channel>/manifest.json`,
   and redirects only within its own origin.
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
6. **Download and hash.** The installer is fetched from the path the signed
   manifest names, `download/v<version>/<file>`, never through `latest/`, so a
   release published halfway through an update cannot change which file
   arrives. Bytes stream to a `.part` file and are hashed as they
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
asked. Only a yes spawns the installer, detached, with `/S /UPDATE /R`: silent
(the user has just been asked), as an update over the existing install (no
shortcuts re-created, no WebView2 bootstrap, and the uninstaller's optional
"delete app data" step never runs), and relaunching Hearth when it is done.
These are the switches `tauri-plugin-updater` passes to the same installer
template. Hearth quits immediately afterwards so the sidecar, and with it
`llama-server` and its VRAM, is gone before the files are replaced.

Nothing the user made is in the way of the installer. The program lives in
`%LOCALAPPDATA%\Programs\Hearth` (see `desktop/tauri/installer-hooks.nsi`);
models, chats, checkpoints, settings and the updater's own state live in
`%LOCALAPPDATA%\Hearth`, which neither the installer nor the uninstaller
touches. The only folder Tauri's template can ever delete is the WebView2
profile under the bundle id, `com.hearthlocal.hearth`, and only when somebody
uninstalls through the GUI and ticks the box.

The renderer cannot name a file and cannot pass a path. `window.hearth.installUpdate()`
is a request with no arguments.

## The default, and why

| | default | can be changed |
| --- | --- | --- |
| check for updates | **yes**, once per launch (a page reload inside one run does not check again for 6 hours) | yes, the checkbox in the Updates panel |
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
race, so neither happens without an explicit action. **Install now** in the
banner is that action: it downloads, verifies and hands over to the shell's
dialog in one go.

The setting is stored as `auto_check` in `%LOCALAPPDATA%\Hearth\update\state.json`,
beside the downgrade floor. Every change to that file goes through one lock
and a uniquely named temporary file, so turning the check off while a check is
writing cannot lose either change; losing the floor that way would quietly
reopen the rollback it exists to close.

**When a check fails.** A launch check that cannot reach GitHub (offline, a
captive portal, a firewall) says so in grey in the Updates panel, "Could not
reach GitHub to check for updates. Hearth will try again next launch.", and
shows no banner. So does a newest release that was published without a signed
manifest. Clicking **Check for updates** gives the precise reason. None of
these is ever shown as "up to date", because a check that did not happen is not
evidence of anything. A refused signature, a rollback or a hash mismatch is
different: it is shown as a warning, in full, because it means something is
wrong rather than missing.

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
  [packaging-windows.md](packaging-windows.md).

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

## The feed: GitHub Releases

`release/trust.json` pins the feed at
`https://github.com/EricFinland/hearth-windows/releases/` with
`"layout": "github-releases"`. A GitHub release's assets share one flat
namespace, so the feed is two files per release:

| asset | fetched as |
| --- | --- |
| `manifest-stable.json` | `releases/latest/download/manifest-stable.json` |
| `Hearth-Setup-<version>.exe` | `releases/download/v<version>/Hearth-Setup-<version>.exe`, the path inside the signed manifest |

`latest/download/` always means the newest *published* release. A draft is
not served through it until it is published, so a release created as a draft
is invisible to installed copies until somebody clicks **Publish**.

## Publishing a release

Pushing a version tag runs `.github/workflows/release.yml`, which builds the
installer, writes the release notes and, in the step **Sign the update
manifest**, signs `manifest-stable.json` and attaches it to the release next
to the installer. Nothing else is needed per release.

### The signing secret

The step reads the repository secret **`HEARTH_UPDATE_SIGNING_KEY`**. It holds
exactly the 64 hexadecimal characters of the `private_seed` value from a key
file written by `release_manifest.py keygen`: no quotes, no JSON, no other
fields. To set it, open the repository's **Settings**, **Secrets and
variables**, **Actions**, **New repository secret**, name it
`HEARTH_UPDATE_SIGNING_KEY`, and paste the value.

In the workflow the seed is written to a temporary file only the runner's user
can read, passed to the signer as a path (never as a command-line argument,
which every process can see), and shredded on every exit path. Nothing echoes
it, and GitHub masks it in logs as well. The signer derives the public key
from the seed and looks it up in `release/trust.json`: a secret that does not
belong to an **active** key there fails the job with a message that says so,
rather than publishing a manifest every client would refuse.

Without the secret, the release still publishes, with a warning on the run
that in-app updates are disabled for that release. Installed copies then find
no manifest in the newest release, say so calmly in the Updates panel, and
keep checking on each launch.

### Release notes in the app

The banner and the Updates panel show a short note from the signed manifest
(at most 4000 characters). By default it is one line, "Hearth X.Y.Z. What
changed:" and a link to the release page. To say more, commit
`release/notes/v<version>.txt` before tagging; the workflow uses it instead.

### Expiry, and re-signing

Each manifest is valid for 180 days from its release. That bound is what turns
a frozen feed (somebody replaying an old manifest forever) into a visible
failure. It also means that if there is no new release within 180 days, every
install's check starts failing, calmly ("the newest update information ...
expired"), until there is. Before that happens, either publish a release or
re-sign the current one with a fresh date and replace the asset:

    python scripts/release_manifest.py sign \
      --installer Hearth-Setup-<version>.exe --version <version> \
      --seed-file <file holding the seed> --out <folder holding the installer>
    gh release upload v<version> <folder>/manifest-stable.json --clobber

The installer itself is unchanged, so its hash is unchanged, and nobody who
already has that version is offered anything.

### Trying a release before it is public

`scripts/release_manifest.py serve` answers the way GitHub does, redirects
included, so the shipped client can be pointed at a folder of release assets
on this machine:

    python scripts/release_manifest.py sign --installer build/dist/Hearth-Setup-<v>.exe \
      --version <v> --key <key file> --out build/feed
    python scripts/release_manifest.py serve --feed build/feed --port 8799
    set HEARTH_UPDATE_FEED=http://127.0.0.1:8799/
    "%LOCALAPPDATA%\Programs\Hearth\Hearth.exe"

Plain http is accepted only for a loopback address given through
`HEARTH_UPDATE_FEED`; the signature is checked exactly as it is against GitHub.
To check a downloaded set of assets with a client that is not the one that
made them:

    python scripts/release_manifest.py verify --feed <folder> --installed 0.1.1

### The keys, and rotating them

The active key is `hearth-release-2026-10`. Its private seed was generated with
`keygen` into a folder outside every checkout and outside anything that syncs
to a cloud service, and it exists in exactly two places: that file, and the
GitHub secret. `hearth-release-2026-08`, the key 0.1.1 shipped with, is marked
`revoked`: its seed sat in a cloud-synced folder, so it cannot be treated as
private. It never signed a published release.

On Windows, the key file gets the permissions of the folder it is in (the
`0600` mode `keygen` asks for has no effect there). Restricting that folder to
your own account, for example with `icacls <folder> /inheritance:r /grant:r
"%USERNAME%:(OI)(CI)F"`, is worth doing once.

To rotate:

1. `python scripts/release_manifest.py keygen --key-id hearth-release-<date> --out <private folder>\hearth-release-<date>.key`.
   It adds the new public key to `release/trust.json` as `active`; commit that.
2. Ship one release still signed with the old key. Installed copies only trust
   the keys they were built with, so the new key has to reach them inside a
   release the old key vouches for.
3. Replace the `HEARTH_UPDATE_SIGNING_KEY` secret with the new seed.
4. Once nobody is running a build older than step 2, mark the old key
   `revoked` in `release/trust.json`.

If a seed may have leaked, do not wait for step 4: revoke it at once and sign
with the new key. Installs that only know the leaked key stop accepting
updates, and their owners install the next version by hand once, which is the
price of not trusting a key somebody else may hold.

## Where things live

| | |
| --- | --- |
| `release/trust.json` | the pinned public keys, the feed and its layout. Committed, and shipped. |
| `release/keys/*.key` | where `keygen` writes by default. Gitignored; the real key lives outside every checkout. |
| `HEARTH_UPDATE_SIGNING_KEY` | the repository secret holding the active key's seed, for the release workflow. |
| `.github/workflows/release.yml` | builds, signs `manifest-stable.json` and publishes the release. |
| `agent/hearth_ed25519.py` | Ed25519, standard library only. Checked against the RFC 8032 vectors and against OpenSSL. |
| `agent/hearth_update.py` | fetch, verify, refuse, stage. Cannot execute anything. |
| `scripts/release_manifest.py` | the operator's tool. Not shipped. |
| `desktop/tauri/src/update.rs` | the only code that runs an installer. |
| `desktop/ui/js/update.js` | the Updates panel. |
| `desktop/ui/js/update-banner.js` | the "Hearth X.Y.Z is available" banner. |
| `GET /update`, `POST /update`, `GET /update/events` | the sidecar's surface. |
| `%LOCALAPPDATA%\Hearth\update\` | the persisted floor, the auto-check setting, and staged installers. |

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
acceptable because of where signing happens: on a GitHub-hosted Actions
runner, a fresh virtual machine that runs one job, signs one manifest a handful
of times a year, and is then destroyed, with no other tenant on it to time
anything. If signing ever moves to a self-hosted or otherwise shared runner it
must move to a constant-time implementation at the same time.

Correctness is asserted three ways: the RFC 8032 §7.1 test vectors (bytes
produced by other people's implementations, so passing them means agreeing with
OpenSSL and libsodium rather than with itself), a mutation battery that flips
every byte of every signature, key and message in turn, and a cross-check
against OpenSSL 3.5.6 and pyca/cryptography over 200 random keypairs in which
the derived public keys and the signatures were byte-identical in both
directions.
