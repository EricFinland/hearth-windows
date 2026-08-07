# Packaging Hearth for Windows

How the installer is built, what is inside it, and what it does and does not
do yet. Written against version 0.1.0, and rewritten when the shell stopped
being Electron.

## Build it

From a clean checkout, on Windows, with Rust and Python installed:

    python scripts/build_windows.py

That is the whole command. It produces:

    build/dist/Hearth-Setup-0.1.0.exe

The build machine needs cargo (from <https://rustup.rs>), `cargo-tauri`
(`cargo install tauri-cli --locked`), Python 3.11 or newer to run the build
and vendor scripts, and a network connection the first time. Node is no
longer needed anywhere. The machine Hearth is installed on needs the WebView2
runtime, which every supported Windows already ships; the installer fetches
Microsoft's bootstrapper if a machine somehow does not have it.

Useful variations:

    python scripts/build_windows.py --skip-build   stage and verify, do not package
    python scripts/build_windows.py --dir          compile the shell, no installer
    python scripts/build_windows.py --offline      use only already-fetched archives

## What is in the installer

| Component | Size | Where it comes from |
| --- | ---: | --- |
| llama.cpp engine (`llama-server.exe` + backend DLLs) | 45.0 MB | `scripts/vendor_llama.py`, pinned release b10105 |
| CPython 3.12.10, embeddable | 21.8 MB | `scripts/vendor_python.py`, pinned python.org release |
| `Hearth.exe` (the shell, with `desktop/ui/` linked in) | 4.3 MB | `desktop/tauri/`, cargo |
| `agent/` modules | 2.3 MB | the checkout |
| Python sidecar (`desktop/server/`) | 0.7 MB | the checkout |
| Licence texts (`LICENSE`, `NOTICE`, notices, `vendor/licenses/`) | 0.1 MB | the checkout |
| **Installed on disk** | **74.7 MB** | 144 files |
| **Installer (compressed)** | **20.5 MB** | |

### What that replaced

The same application, same payload, with an Electron shell, measured on the
same machine on the same day:

| | Electron | Tauri | |
| --- | ---: | ---: | --- |
| Installer | 122.98 MB | 20.51 MB | 83.3 percent smaller |
| Installed on disk | 434.57 MB | 74.66 MB | 82.8 percent smaller |
| The shell itself | 364.4 MB | 4.3 MB | Chromium, Node and V8, versus a Rust binary |
| Files installed | 204 | 144 | |

The payload did not change: the engine, the interpreter, the agent and the
sidecar are byte for byte what they were. The whole difference is that
WebView2 is already on the machine and Chromium was not. Electron was 84
percent of the old download; the browser engine is now 0 percent of the new
one.

Neither vendored binary is committed. Both are fetched at build time from a
pinned release and checked against a SHA-256 recorded in the repository, in
`vendor/llama_manifest.json` and `vendor/python_manifest.json`. Those two
files are the trust anchors; read the module docstrings in the two vendor
scripts for the rules they enforce.

## How Python ships

The official **Windows embeddable package**, not a freezer. Reasons, in the
order they mattered:

1. `agent/` and `desktop/server/` are imported as the ordinary modules they
   are. No hidden-import lists, no data-file manifests, nothing that fails at
   runtime because a static import scanner missed a name.
2. It installs nothing. No registry keys, no PATH entry, no file
   associations, no `py.exe` launcher, so it cannot collide with a Python the
   user already has, and uninstalling is deleting a folder.
3. Startup is plain CPython startup.
4. No packer, and therefore one fewer antivirus and SmartScreen heuristic to
   trip. The installer is already unsigned; adding a packed interpreter on
   top of that is the wrong trade.

PyInstaller and Nuitka were both considered and rejected; `scripts/
vendor_python.py`'s module docstring records the reasoning in full, including
that Nuitka needs a C toolchain this machine cannot install without
administrator elevation.

The interpreter runs in isolated mode, so `PYTHONPATH`, `PYTHONHOME`, user
site-packages and the registry are all ignored. Isolated mode also does not
put a script's own directory on `sys.path`, which is why
`desktop/server/main.py` puts it there itself in its first few lines rather
than the packaging layer encoding the layout into the interpreter.

Nothing in `agent/` or `desktop/server/` gained a dependency. Both are still
standard library only.

## Layout of an install

    %LOCALAPPDATA%\Programs\Hearth\
      Hearth.exe                          the shell, with desktop/ui/ linked into it
      uninstall.exe
      LICENSE.hearth.txt  NOTICE.txt  THIRD-PARTY-NOTICES.md
      python\                             the vendored interpreter
      hearth\agent\                       unchanged
      hearth\desktop\server\              unchanged
      hearth\vendor\llama\                llama-server.exe and its backends
      hearth\vendor\llama_manifest.json   the pin
      hearth\vendor\licenses\             the texts the notices quote
      hearth\scripts\vendor_llama.py      the fetcher
      hearth\release\trust.json           the update signing key
      hearth\release\version.json         what this build is
      hearth\LICENSE  hearth\NOTICE  hearth\THIRD-PARTY-NOTICES.md

Two things about that listing are worth reading twice.

There is no `resources\` level any more, because Tauri resolves resources
relative to the executable rather than to a subdirectory. And **there is no
`ui\`**. The interface is linked into `Hearth.exe` by `tauri-build` at compile
time, so the application's own code is not a file on disk that anything
running as the user can rewrite. That is the property Electron needed two
fuses and a build gate for, and here it is a consequence of how the binary was
made. `scripts/verify_binary.py --installed` walks a real install and fails if
any interpretable code reappears beside the executable.

The install path is set by `desktop/tauri/installer-hooks.nsi` rather than
left at Tauri's default. Tauri's default for a per-user install is
`%LOCALAPPDATA%\<productName>`, which for this application is
`%LOCALAPPDATA%\Hearth`, and that is already Hearth's **data** directory:
`agent/hearth_paths.py` resolves to exactly the same path, and it holds
checkpoints, model weights, fetched GPU engines and staged updates. Installing
on top of it puts an uninstaller in the same folder as the user's work. The
hook moves it to `%LOCALAPPDATA%\Programs\Hearth`, which is where the Electron
build installed, and the two directories are separate again.

Running the application adds `__pycache__` directories under `hearth\agent\`
and `hearth\desktop\server\`, which is why a used install measures a few
megabytes more than a fresh one.

The payload layout is not arbitrary. `agent/hearth_llama.app_root()` resolves
to the parent of the `agent` directory and then looks for `vendor/llama`
beneath it, so staging the payload this way makes `find_server()` locate the
bundled engine with no packaging-aware code in the agent at all. The last two
entries are there for the same kind of reason: `vendor_llama` resolves its
manifest as `../vendor/llama_manifest.json` relative to itself, and
`agent/hearth_engine.py` imports it from `app_root()/scripts`, so the GPU
engine fetch works from an install only if both land exactly there.

The build verifies all of this before packaging. It runs the staged sidecar's
own self-test with the staged interpreter, asserts the engine is found as
`bundled`, computes a GPU fetch plan from the staged manifest for a pretend
NVIDIA card, reads the staged update trust anchor back to confirm it carries
an active signing key and a version, and checks that `LICENSE`, `NOTICE`,
`THIRD-PARTY-NOTICES.md` and `vendor/licenses/` all reached the payload. A
payload that would leave every user stuck on the CPU build, unable to verify
an update at all, or out of licence compliance, fails the build instead of
shipping. Before any of that, `python scripts/third_party_notices.py --check`
runs as a gate, so a version bump that leaves the notices describing a
different program stops the build at the moment it happens. Afterwards,
`scripts/verify_binary.py` reads the built executable back.

`release/trust.json` holds the Ed25519 **public** key every release manifest
is checked against, and `release/version.json` is generated from
`desktop/tauri/tauri.conf.json` at stage time so the two cannot drift. The
shell also passes its own version down in `HEARTH_APP_VERSION`, and that one
comes out of the executable's resource block, written by `tauri-build` from
the same file. See [updates.md](updates.md) for the whole update mechanism.

## The GPU engine, which is not in the installer

Hearth bundles llama.cpp's **CPU x64** build and fetches a GPU build after
installation. Both halves of that are deliberate.

The CPU build is bundled because it is the only Windows artifact that cannot
fail to start. Every GPU build links a vendor runtime that is absent on a
machine without the matching driver, and on Windows a missing DLL is a
load-time failure of the executable, not slow inference. Shipping the floor
means the engine always runs.

It is not what anybody should be running. Measured on an RTX 5080 with the
pinned build 10105, Qwen2.5-7B-Instruct Q4_K_M:

| build | generation | prompt processing |
| --- | ---: | ---: |
| CPU x64 | 13.8 tok/s | 185 tok/s |
| Vulkan x64 | 169.2 tok/s | 8436 tok/s |
| CUDA 13.3 x64 | 169.2 tok/s | 8861 tok/s |

So on first launch the sidecar starts `agent/hearth_engine.py` on a
background thread. It detects the GPU, reads the fetch policy out of
`vendor/llama_manifest.json`, downloads the matching variant through
`scripts/vendor_llama.py` against the pinned SHA-256, runs the new binary to
confirm it works on this machine, and only then writes the pointer that makes
it the engine `find_server()` returns. Hearth is usable on the CPU build the
whole time; the swap takes effect at the next model load. Progress is on
`GET /engine` and `GET /engine/events`, and in the Backend panel.

Vulkan is what NVIDIA gets too, and the table above is why: 5 to 7 per cent
of prompt-processing throughput is not worth 504 MB of extra download, and
Vulkan covers AMD and Intel from the same 33 MB artifact. CUDA, ROCm, SYCL
and OpenVINO stay pinned and can be requested with `HEARTH_GPU_ENGINE`; for
CUDA the choice between the 12.4 and 13.3 builds is made from the card's
compute capability and the driver's CUDA ceiling, and refused outright when
neither build covers the hardware.

Nothing about this can leave a broken install:

* no network, a failed download, or a checksum mismatch leaves the CPU build
  active and records the reason;
* a build that downloads correctly but does not run here is deleted, never
  activated, and remembered as failed for this hardware so the next launch
  does not repeat it (the Backend panel's "Try again" clears that);
* an engine that stops working later, after a removed driver or a swapped
  card, is demoted at launch by `hearth_llama.start()`, which retries on the
  bundled build inside the same call;
* `HEARTH_GPU_ENGINE=off` skips the whole thing.

The Backend panel reports whichever of those is true, and never claims GPU
acceleration that the running engine does not actually have.

## How the bearer token reaches the interface

The sidecar prints one line of JSON on stdout at startup with its port and a
fresh bearer token. That line is the only place the token is ever emitted.

    sidecar stdout  ->  the shell process  ->  Tauri IPC  ->  renderer

The renderer asks for it with `window.hearth.handshake()`. That object is
installed by an initialization script in `desktop/tauri/src/main.rs`, is
frozen, is defined non-writable and non-configurable, and holds exactly four
names: `shell`, `handshake()`, `pickFolder()` and `installUpdate()`. Under it
sits Tauri's `invoke`, and what `invoke` can reach is decided by
`desktop/tauri/capabilities/main.json`, which names three commands and grants
no plugin permission at all. The Rust side then checks that every one of those
calls came from its own window at its own origin before answering.
`installUpdate()` takes no arguments on purpose: the page cannot name a file,
and the shell reads the verified receipt from the sidecar itself, re-hashes it
and asks the user. See [updates.md](updates.md).

The capability is not a formality. The page is served over http on loopback,
which Tauri treats as a **remote** origin, and a remote origin reaches exactly
what a capability spells out. With the permission list left empty, `handshake`
itself was refused and the application could not start.

Measured in the running application, with the page asked what it can see:

* the token appears in none of `localStorage`, `sessionStorage`,
  `document.cookie`, `location.href`, `location.search`, `location.hash`,
  `document.title`, `document.referrer`, `window.name`, the serialized DOM,
  the history state, or any of the 22 resource URLs the page requested.
  `localStorage` and `sessionStorage` are empty, there are no cookies and no
  IndexedDB databases.
* `require`, `process`, `Buffer`, `__dirname`, `module`, `exports` and
  `global` are all undefined, and `window.__TAURI__` does not exist.
* `window.hearth` is frozen: assigning to `handshake` is ignored,
  `Object.defineProperty` on `window.hearth` throws, and `delete` does
  nothing.
* every plugin command tried by hand is refused by the access-control list
  before it runs, including `plugin:fs|read_text_file`,
  `plugin:fs|write_text_file`, `plugin:shell|execute`,
  `plugin:dialog|open`, `plugin:http|fetch` and
  `plugin:webview|create_webview_window`. The dialog plugin is compiled in
  and used by the shell; the page still cannot call it.
* an offsite `fetch`, a fetch to a different loopback port, an inline
  `<script>`, an offsite `<script src>` and `window.open` are all blocked, the
  last returning null.
* `/dev-host.mjs`, `/xss-check.html`, a path that never existed, and three
  spellings of directory traversal (plain, percent-encoded and backslashed)
  all return 404. A sidecar route with no token returns 401, and so does one
  with the wrong token.

### The environment variable that is Tauri's version of a fuse

There is no Node runtime in this binary, so `ELECTRON_RUN_AS_NODE`,
`NODE_OPTIONS` and `--inspect` have no analogue. But WebView2 is a Chromium,
and Chromium takes orders from the environment. With

    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333

set on the process, an early build of this shell opened an unauthenticated
DevTools endpoint on loopback, attached to the page holding the bearer token.
That is not a hypothesis: it was run, the port listened, `/json/list` returned
the Hearth page, and a script attached to it could read the token out of the
running application. The same variable also accepts
`--disable-web-security`, which retires the Content-Security-Policy and the
same-origin rule the loopback origin exists to enforce, and
`WEBVIEW2_BROWSER_EXECUTABLE_FOLDER` loads the browser binaries from a folder
of somebody else's choosing.

`main.rs` now removes those and three related variables at the top of `main`,
before the runtime is loaded and before any webview exists. Re-run against the
fixed build with the identical environment: the window opens normally and
nothing is listening on the port. `scripts/verify_binary.py` fails the build if
the code that does the removing is no longer in the shipped executable.

**No HTTP route returns the token, in the packaged app or anywhere reachable
from it.** `desktop/ui/dev-host.mjs` does serve it at `GET
/__hearth/handshake`, to any process on loopback that asks; that is why it is
a development tool and says so in its own header comment. The packaged shell
has no such route. `desktop/ui/js/api.js` prefers the bridge whenever one
exists and only falls back to the HTTP route under the dev host.

The shell does serve the UI on a loopback origin that also forwards the
sidecar's routes, because the sidecar refuses a page served from any other
origin (`Host` and `Origin` checks, no CORS, no static file serving). That
proxy grants no authority of its own: it mints no credentials, injects none,
forwards `Authorization` exactly as the page sent it, and refuses anything
but `/healthz` that arrives without one. It also adds three checks the dev
host does not have: loopback only, an `Origin` that must be absent or this
exact origin, and a route allowlist. `desktop/tauri/src/origin.rs` explains
the whole design, and `desktop/server/app.py`'s self-test diffs the allowlist
in it against the router and against the two copies in `desktop/ui/`, so a
route added in one place and missed in another fails a test instead of 404ing
only in the packaged application.

One thing the Rust origin has to do that the JavaScript one did not: Tauri's
asset resolver is written for single-page applications and falls back, in
order, to `<path>.html`, `<path>/index.html` and finally to `index.html`, so
an origin built on it alone answers 200 for every URL ever typed. It also
percent-decodes a second time, after the origin has already decoded once. The
shell takes the set of asset keys the binary actually carries at startup and
answers only those, which makes a 404 a 404 again and closes the double
decode. Measured: without it, `GET /not-a-route` returned `index.html` with
status 200.

## The six-connections problem, which is now a ten-connections problem

The page holds long-lived SSE streams for events, downloads, the engine, the
loop and the swarm. Under Electron a sixth permanent stream took the last
socket Chromium allows per origin and the renderer could make no requests at
all, which is why the updater panel polls instead of streaming.

Measured in WebView2, by opening one more stream at a time and timing a plain
`GET /healthz` after each, while counting the established sockets the WebView2
process holds to the origin:

| sockets held | `GET /healthz` |
| ---: | --- |
| 4 (the application at rest) | 200 in 8 ms |
| 5 | 200 in 9 ms |
| 6 | 200 in 8 ms |
| 7 | 200 in 10 ms |
| 8 | 200 in 11 ms |
| 9 | 200 in 8 ms |
| 10 | no answer in 8 seconds |

**The limit is ten, not six.** At ten held sockets the page cannot reach its
own origin at all, and releasing the streams restores it immediately (200 in
9 ms). So there are six spare sockets rather than one, and the updater panel
could go back to a stream. That change has not been made here: this port set
out to change the shell and nothing else, and `desktop/ui/` was not touched.
The measurement is recorded so the decision is a decision rather than a
guess.

## Nothing is left running

The sidecar can be holding a `llama-server.exe` with several gigabytes of
VRAM. Four mechanisms, each covering a different failure:

1. `llama-server` is inside a kill-on-close Windows Job Object owned by the
   Python process (`agent/hearth_llama._win_job`). When Python dies by any
   means, including a hard kill, the kernel kills what is in the job. The
   shell starts Python directly and does not get between them.
2. Python is started with `--watch-parent` and a piped stdin the shell holds
   open. Any way the shell stops existing closes the write end, Python reads
   EOF and exits, which triggers (1).
3. The shell also writes a newline down that pipe every 15 seconds, and the
   sidecar shuts down if 60 seconds pass with no beat. This exists because
   (2) is a guarantee about handle ownership that the sidecar cannot enforce:
   a parent that leaks an inheritable copy of the write handle into the child
   produces no EOF at all, ever. That is not hypothetical. It is what
   CPython's own `subprocess` module does on Windows, and it was found by
   running this exact sidecar under a Python parent instead of a Node one.
   Neither libuv, which is what Electron's `spawn` used, nor Rust's
   `std::process` does it, and the sidecar dies within two seconds under
   either. The heartbeat means the guarantee does not rest on that.
4. A **second** kill-on-close Job Object, created by the shell, holding the
   Python process. This is new in the Tauri shell and it closes the last gap:
   (3) is a sixty second timeout, so a hard kill of the shell used to leave
   the sidecar and its model resident for up to a minute. With this, the
   kernel kills it in the same breath as the shell. Job membership is
   inherited, so `llama-server` is inside this job too and is covered twice
   over. Nesting has been legal since Windows 8; if the assignment fails
   anyway the heartbeat is still there, so it is an improvement that cannot
   become a regression.

Measured on the installed build, for each of three kill paths: a normal quit
(`WM_CLOSE` to the window), a Task Manager style `taskkill /F` on the shell
alone with no `/T`, and a `TerminateProcess` crash with no chance to run any
shutdown code. In every case `tasklist` showed `Hearth.exe`, `python.exe` and
`llama-server.exe` all gone within seconds, with no survivors.

## SmartScreen: what a user actually sees

**The installer is not signed.** There is no code-signing certificate for
this project yet. Do not describe this build as ready to hand to anyone
without saying what follows.

Downloading and running `Hearth-Setup-0.1.0.exe` on a machine that has not
seen it before produces a blue full-screen dialog:

> **Windows protected your PC**
> Microsoft Defender SmartScreen prevented an unrecognised app from starting.
> Running this app might put your PC at risk.

The only visible button is **Don't run**. Running it requires clicking the
small **More info** link, which reveals the app name and an unknown
publisher, and then **Run anyway**. Browsers add their own layer: Edge and
Chrome will usually warn that the file "isn't commonly downloaded" and may
require an explicit "Keep" before the file is even saved.

Most people stop at the blue screen. That is what it is designed to do.

A code-signing certificate removes the unknown-publisher line and, once the
signed binary has built reputation (or immediately, with an EV certificate),
removes the warning. It is a purchase and an identity-verification process
with a lead time measured in days to weeks. Nothing in this build can
substitute for it, and no build flag turns it off.

Until then, this installer is for people who have been told directly what to
expect.

Hearth's own updater does not depend on any of this. It verifies each release
against an Ed25519 key built into the application, which protects users today,
on an unsigned build, and keeps protecting them after a certificate is bought.
[updates.md](updates.md) sets out exactly what each of the two covers.

## Versioning

The desktop app carries its own version line, starting at 0.1.0. It is
declared in `desktop/shell/package.json` and is what electron-builder stamps
into the installer name and the uninstall entry.

## Development

Run the shell against the working tree, no packaging step:

    python scripts/vendor_llama.py vendor
    python scripts/vendor_python.py vendor
    cd desktop/tauri && cargo run --release

It uses the vendored interpreter and the checkout's own `agent/`,
`desktop/server/` and `desktop/ui/`, so the only difference from a packaged
run is where the files are: `resolve_layout` in `main.rs` looks for the
payload beside the executable first and falls back to the checkout.
`cargo run --release --features devtools` compiles the inspector in; a release
build has no inspector at all, by any key combination or environment variable,
because the code is not there.

The older browser-based dev host still works and is still useful for testing
the UI in isolation:

    node desktop/ui/dev-host.mjs --open

It is the one remaining thing in this repository that wants Node, it is not a
shipping path, and it says so in its own header for the token reason above.

## What the port replaced

`desktop/shell/` is gone. `desktop/tauri/` is what ships, and it is the only
shell: there is no second half-working one to choose between.

| was | is |
| --- | --- |
| `desktop/shell/sidecar.js` | `desktop/tauri/src/sidecar.rs` |
| `desktop/shell/origin.js` | `desktop/tauri/src/origin.rs` |
| `desktop/shell/main.js`, `preload.js` | `desktop/tauri/src/main.rs` |
| the update half of `main.js` | `desktop/tauri/src/update.rs` |
| `desktop/shell/verify-fuses.js` | `scripts/verify_binary.py` |
| `desktop/shell/package.json` (electron-builder) | `desktop/tauri/tauri.conf.json`, `Cargo.toml`, `installer-hooks.nsi` |

**`desktop/ui/` did not change.** Not one file. It asks `globalThis.hearth`
for a handshake, a folder picker and an update, and uses nothing else from its
host, so the same four names with the same shapes were all the new shell had
to provide. That was the bet the previous milestone made when it kept the
interface dependency-free and shell-agnostic, and it paid: the port is one new
directory, one deleted directory, and a build script.

Two things did not survive as they were, and both are improvements:

* The seven Electron fuses have no equivalent, because five of them existed to
  restrain a JavaScript runtime that is no longer in the binary and two
  existed to make an on-disk code archive tamper-evident when there is no
  longer an on-disk code archive. `scripts/verify_binary.py` explains what
  carries over and checks five things that are real, including the WebView2
  environment above, rather than passing silently.
* The route allowlist is still duplicated three ways, but the third copy is
  Rust now. `app.py`'s self-test reads all three and still fails on drift.
