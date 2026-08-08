# Getting started

This page takes you from nothing to a working Hearth, in order, with no steps
assumed. The other pages in `docs/` are reference: they answer "how does X
work" well and "what do I do first" badly. This one is the walkthrough.

## Read this first: there is no download yet

Hearth has never been released. There is no installer to download, no release
page, and no signed binary anywhere. `release/trust.json` points at
`releases.hearth.invalid`, which is a reserved name that cannot resolve, on
purpose: an updater pointed at a real host that nobody controls is worse than
one pointed at a host that cannot exist.

So the only way to run Hearth today is to build it. That is what this page
walks through. It takes about twenty minutes, most of it waiting.

If you were looking for a finished product to install and use, this is not
that yet, and [docs/limitations.md](limitations.md) is the honest page about
what else is missing.

## What you need

**A Windows machine.** Windows 10 or 11. Hearth's engine also runs on Linux,
but the shell, the installer and the packaging are Windows-only.

**About 8 GB of RAM free**, more if you want a larger model. Hearth measures
your hardware and tells you which models actually fit rather than letting you
pick one that will swap to disk. See
[docs/model-shop.md](model-shop.md) for how that arithmetic works.

**A GPU helps and is not required.** Hearth detects NVIDIA VRAM and uses it if
present. Without one, models run on the CPU, slower.

You do **not** need Ollama. Earlier versions required it. Hearth now bundles
llama.cpp's `llama-server` and drives it itself, so a fresh install has an
inference engine in it. Ollama still works if you have it and prefer it, and
`agent/hearth_backend.py` will use it, but nothing requires it.

You do **not** need Node. The shell was Electron until the Tauri port and is
now Rust.

## Step 1: install the build tools

Three things, once.

**Rust.** Install from [rustup.rs](https://rustup.rs). Hearth needs 1.82 or
newer. On Windows, rustup will offer to install the Microsoft C++ build tools
if you do not have them; say yes. Rust cannot link a Windows executable
without them, and this is the step people most often skip and then get a
confusing linker error from.

**The Tauri bundler.** This compiles from source and takes a few minutes:

```bash
cargo install tauri-cli --locked
```

**Python 3.11 or newer.** From [python.org](https://python.org) or the
Microsoft Store. Tick "Add python.exe to PATH" if the installer offers it.

Check all three answer:

```bash
cargo --version
cargo tauri --version
python --version
```

## Step 2: get the code

```bash
git clone https://github.com/EricFinland/hearth-windows.git
cd hearth-windows
```

## Step 3: build it

```bash
python scripts/build_windows.py
```

One command, and it does everything: downloads and checksums a pinned
`llama-server` and a pinned CPython, regenerates and verifies the third-party
notices, stages the payload, proves the staged payload actually runs under the
staged interpreter, compiles the shell, and produces an installer. It then
reads the hardening back out of the executable it just built rather than
trusting that it asked for it.

The first run needs a network connection to fetch the engine and the
interpreter. After that, `--offline` works.

Expect ten to twenty minutes, nearly all of it Rust compiling dependencies for
the first time. Later builds are much faster.

When it finishes you have:

```
build/dist/Hearth-Setup-0.1.0.exe
```

That is a real installer, about 20 MB. You can run it, or move to step 4 and
skip installing entirely.

### If you only want to run it, not package it

You do not have to build an installer to use Hearth. Fetch the two vendored
pieces and run the shell straight from the source tree:

```bash
python scripts/vendor_llama.py vendor
python scripts/vendor_python.py vendor
cd desktop/tauri
cargo run --release
```

This is the faster loop if you are changing code.

## Step 4: run it

Run the installer, or run `cargo run --release` as above.

**Windows will warn you.** A full-screen blue SmartScreen panel saying the
publisher is unknown. That is correct and expected: the installer is not code
signed, because a certificate costs money and the free path for open-source
projects (SignPath Foundation) requires a public repository with a green CI
build first. Click "More info", then "Run anyway".

You should be suspicious of that instruction from a stranger on the internet.
[docs/code-signing-policy.md](code-signing-policy.md) explains exactly what
signing would and would not prove, and
[docs/updates.md](updates.md) explains the Ed25519 key that protects updates
whether or not the installer is ever signed.

Hearth installs per-user, into `%LOCALAPPDATA%`. It does not ask for
administrator rights and does not write outside your own profile.

## Step 5: get a model

Open the model shop in the app.

Hearth lists real models from Hugging Face and, for each quantisation, tells
you whether it fits *your* machine: not a generic recommendation, but a
calculation against the RAM and VRAM it measured, including the KV cache at
the context length you would actually use. A model that will not fit is marked
as not fitting rather than offered and left to fail later.

Pick one that fits and download it. Progress is real: byte counts that never
move backwards, a disk-space check before it starts, and cancellation that
works.

If you are unsure, start with a 7B model at Q4. It fits comfortably in 8 GB
and is good enough to judge whether any of this is useful to you.

## Step 6: your first task

Point Hearth at a folder and ask it to do something small.

The folder you choose is the boundary. Every file tool goes through a
containment check against it: path traversal, absolute paths, reserved device
names like `NUL` and `COM1`, alternate data streams and NTFS junctions are all
rejected, so an agent that gets confused or is talked into trying cannot reach
outside the directory you named.

Start in `edit` mode, which is the default. Reads happen automatically; every
file write and every shell command stops and shows you what it wants to do
before it does it.

A good first task is something you can check by eye:

> Read every Python file in this folder and write a README.md that lists what
> each one does, in one sentence each.

Watch what it asks for. That is the part worth paying attention to, more than
the output.

## The four permission modes

| Mode | Runs automatically | Asks you |
| --- | --- | --- |
| `plan` | Reads only | Nothing else is allowed at all. The agent can look, not touch. |
| `edit` | Reads | Every file write, every shell command, every network call. **The default.** |
| `auto` | Reads and file edits | Shell and network still stop, unless pre-approved. |
| `bypass` | Everything | Nothing. No prompts. |

Underneath all four, each run carries a capability manifest: a fixed list of
tools it may touch at all, enforced before the mode logic and in every mode
including `bypass`. A run scoped to fewer tools cannot be talked into reaching
for one outside that list.

[docs/windows.md](windows.md) covers the modes in full, including a case where
this exact trust broke and how it was fixed.

## When something goes wrong

**A linker error from `cargo`.** The Microsoft C++ build tools are missing.
Re-run the rustup installer and accept them.

**`cargo tauri` is not recognised.** `cargo install tauri-cli --locked` did not
finish, or `~/.cargo/bin` is not on your PATH. Open a new terminal first.

**The build fails on a checksum.** That is the vendoring step refusing a file
that does not match `vendor/llama_manifest.json` or
`vendor/python_manifest.json`. It is supposed to do that. Do not work around
it; open an issue.

**The model is very slow.** Check that it fits. The shop's verdict is the
thing to trust here, not the model's popularity.

**Something else.** Hearth writes to `%LOCALAPPDATA%\Hearth`. Open an issue and
include what is in there, minus anything sensitive: those paths can contain the
names of files you opened.

## Where to go next

- **[docs/windows.md](windows.md)**: the full reference. What Hearth is, what
  it does, what each permission mode means, `.hearthignore`, how undo works,
  what survives a restart.
- **[docs/limitations.md](limitations.md)**: the honest page. Read it before
  trusting Hearth with anything that matters.
- **[docs/security/windows-threat-model.md](security/windows-threat-model.md)**:
  what the boundaries are, and what they are not.
- **[docs/model-shop.md](model-shop.md)**: how the fit calculation works.
- **[docs/agent-swarm.md](agent-swarm.md)**: running several agents at once.
- **[docs/privacy.md](privacy.md)**: every destination Hearth contacts, and
  why.
- **[CONTRIBUTING.md](../CONTRIBUTING.md)**: if you want to change something.
