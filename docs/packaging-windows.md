# Packaging Hearth for Windows

How the installer is built, what is inside it, and what it does and does not
do yet. Written against the first packaged build, version 0.1.0.

## Build it

From a clean checkout, on Windows, with Node and Python installed:

    python scripts/build_windows.py

That is the whole command. It produces:

    build/dist/Hearth-Setup-0.1.0.exe

The build machine needs Node.js (for electron-builder), Python 3.11 or newer
(to run the build and vendor scripts), and a network connection the first
time. The machine Hearth is installed on needs nothing at all.

Useful variations:

    python scripts/build_windows.py --skip-build   stage and verify, do not package
    python scripts/build_windows.py --dir          unpacked app, no installer
    python scripts/build_windows.py --offline      use only already-fetched archives

## What is in the installer

| Component | Size | Where it comes from |
| --- | ---: | --- |
| Electron runtime (Chromium, Node, V8) | 364.3 MB | npm, `electron` |
| llama.cpp engine (`llama-server.exe` + backend DLLs) | 45.0 MB | `scripts/vendor_llama.py`, pinned release b10105 |
| CPython 3.12.10, embeddable | 21.8 MB | `scripts/vendor_python.py`, pinned python.org release |
| `agent/` modules | 1.6 MB | the checkout |
| Python sidecar (`desktop/server/`) | 0.5 MB | the checkout |
| User interface (`desktop/ui/`) | 0.2 MB | the checkout |
| **Installed on disk** | **433.4 MB** | |
| **Installer (compressed)** | **122.7 MB** | |

Electron is 84 percent of the download. That is the price of this milestone,
and it is the single biggest reason to finish the Tauri port: the same app
with a WebView2 shell would be roughly 15 MB, because the browser is already
on the machine.

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
      Hearth.exe                          the Electron shell
      resources\app.asar                  main.js, preload.js, origin.js, sidecar.js
      resources\python\                   the vendored interpreter
      resources\hearth\agent\             unchanged
      resources\hearth\desktop\server\    unchanged
      resources\hearth\ui\                unchanged
      resources\hearth\vendor\llama\      llama-server.exe and its backends
      resources\hearth\vendor\llama_manifest.json    the pin
      resources\hearth\scripts\vendor_llama.py       the fetcher

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
`bundled`, and computes a GPU fetch plan from the staged manifest for a
pretend NVIDIA card, so a payload that would leave every user stuck on the
CPU build fails the build instead of shipping.

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

    sidecar stdout  ->  Electron main process  ->  IPC  ->  renderer

The renderer asks for it with `window.hearth.handshake()`, a
`contextBridge` function exposed by `preload.js`. `contextIsolation` is on,
`nodeIntegration` is off, `sandbox` is on, and the bridge exposes exactly
three names: `shell`, `handshake()` and `pickFolder()`. No Node, no
filesystem, no `ipcRenderer` itself. The main process checks that a handshake
request came from its own window, loaded from its own origin, before
answering.

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
exact origin, and a route allowlist. `desktop/shell/origin.js` explains the
whole design.

## Nothing is left running

The sidecar can be holding a `llama-server.exe` with several gigabytes of
VRAM. Three mechanisms, each covering a different failure:

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
   libuv, which is what Electron's `spawn` uses, does not do it, and the
   sidecar dies within two seconds there. The heartbeat means the guarantee
   does not rest on that.

Measured on the installed build, with a model loaded and `llama-server`
resident, for each of three kill paths: everything gone within 2 seconds.

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

## Versioning

The desktop app carries its own version line, starting at 0.1.0. That is
separate from the `CHANGELOG.md` line for the Linux daemon, which is at
1.6.0. They are the same repository and not the same artifact.

## Development

Run the shell against the working tree, no packaging step:

    python scripts/vendor_llama.py vendor
    python scripts/vendor_python.py vendor
    cd desktop/shell && npm install && npm start

It uses the vendored interpreter and the checkout's own `agent/`,
`desktop/server/` and `desktop/ui/`, so the only difference from a packaged
run is where the files are. `HEARTH_DEVTOOLS=1` enables the developer tools.

The older browser-based dev host still works and is still useful for testing
the UI in isolation:

    node desktop/ui/dev-host.mjs --open

It is not a shipping path, for the token reason above.

## What a Tauri port would have to replace

Three files, and nothing above them:

- `desktop/shell/sidecar.js` - start the interpreter, read the handshake,
  hold stdin open, heartbeat.
- `desktop/shell/origin.js` - serve `desktop/ui/` and forward the sidecar's
  routes on one origin. Tauri does not need this at all: the Rust side owns
  the sidecar and the webview reaches it without a browser origin.
- `desktop/shell/main.js` and `preload.js` - the window, and a bridge
  exposing `shell`, `handshake()` and `pickFolder()`.

`desktop/ui/` does not know which shell it is running under. It asks
`globalThis.hearth` for a handshake and a folder picker and uses nothing else
from its host, so a Tauri build exposes the same three names with the same
shapes and no interface code changes. The interface has no npm dependencies
and no build step, and the payload staged into the installer is a verbatim
copy of the checkout.
