# Hearth for Windows

This page assumes no Linux knowledge. If you already run the NixOS system,
this is not for you; see the [main README](../README.md#the-nixos-system)
instead.

## What Hearth is

Hearth is a desktop application. The pitch is simple: a hosted AI assistant
gives you a chat window backed by a model running on someone else's
servers, billed per token. Hearth gives you the same kind of chat window,
backed by a model running on your own GPU, for free, because you already
own the hardware. A "shop" inside Hearth is meant to make picking and
downloading a model as easy as picking an app, rather than something that
requires knowing what a GGUF or a quantization level is.

**Hearth Code** is the agentic coding surface inside Hearth: the part that
reads your repository, proposes edits, runs shell commands, and can be
handed a task to work through on its own while you do something else. Two
names, used consistently: Hearth is the application, Hearth Code is the
coding agent inside it. Everything below about permission modes, undo, and
containment is really about Hearth Code, since that's the surface with the
file and shell access.

## Where this actually stands today

Read this section before the rest, because it changes how to read
everything else on this page.

**Built, and self-tested on Windows:**

- Per-platform data locations and Windows long-path handling
  (`agent/hearth_paths.py`).
- The workspace boundary that every file tool goes through: rejects path
  traversal, drive-qualified absolute paths, reserved device names (`NUL`,
  `CON`, `COM1`, ...), alternate data streams, trailing dots and spaces, and
  wildcards, and prunes NTFS junctions out of directory walks so a
  reparse-point escape can't slip past it (`agent/hearth_contain.py`).
- Contained subprocess execution: UTF-8 output capture that can't silently
  turn into "no output" on a decode error, a timeout that kills the whole
  process tree instead of just the shell, and long commands spilled to a
  script file instead of truncated at Windows' command-line limit
  (`agent/hearth_proc.py`).
- Hardware detection: GPU, VRAM, and system RAM, plus the KV-cache fit
  calculator the model shop's verdicts are built on (`agent/hearth_hw.py`).
- The model shop's catalog and fit logic (`agent/hearth_shop.py`) - the
  data and arithmetic, not a UI yet.
- Git-backed checkpoint and undo (`agent/hearth_checkpoint.py`).
- The permission engine: `plan`, `edit`, `auto`, `bypass`, plus a
  capability manifest that hard-caps what tools a run may use, in every
  mode including `bypass` (`agent/permissions.py`).
- A Windows tool manifest of exactly ten tools (reading, writing, editing,
  listing, searching, and replacing in files; running shell commands; git
  status and diff). The NixOS-only tools still exist in the codebase but
  are never offered on Windows.
- The sidecar HTTP layer (`desktop/server/`): a standard-library-only HTTP
  server wiring the permission engine, the tool layer, and checkpoint/undo
  to real routes (`/session`, `/prompt`, `/events`, `/approve`, `/cancel`,
  `/restore`, `/models`, `/checkpoints`). Every route but `/healthz` requires
  a bearer token plus `Host`/`Origin` validation, and the server binds
  `127.0.0.1` on an ephemeral port only. Self-tested, and exercised end to
  end against a real Ollama by `scripts/e2e_live.py`: 16 of 16 steps pass,
  including a real tool call, the permission gate firing, an approval
  resolved over HTTP, a byte-exact workspace change, an automatic pre-turn
  checkpoint, and a byte-exact restore.

**Not built yet.** There is no Tauri desktop shell, no UI, no installer, no
code signing, and nothing published anywhere. The model shop described below
has no interface yet: it exists as data and logic today, callable, tested,
and correct, but with nothing to click. There is no cloud API key support.
**There is no download.** Everything on this page describes an engine that
works when driven directly, not a finished application.

## What you need

Once there is something to run, it needs [Ollama](https://ollama.com)
installed with at least one model already pulled. Hearth does not fetch or
manage models yet; that is the model shop's job, and it isn't built. Until
then, whatever model you choose in Ollama is the trust decision, made
outside Hearth.

## How a model, and its context length, get chosen for your hardware

This is the part most local-model tools get wrong, so it's worth explaining
properly. See [docs/model-shop.md](model-shop.md) for the full write-up;
this is the short version.

Model weights are not the whole memory story. Every token of context an
attention model holds onto costs a fixed number of bytes, and that cost
scales linearly with how much context you ask it to remember. A model that
comfortably fits a short conversation can blow past your VRAM the moment
you paste in a few files and let an agent loop run for a while. Hearth's
fit calculator (`hearth_hw.fits()`) always computes weights *plus* KV cache,
never weights alone.

That math also drives context length automatically. Ollama, left to its own
defaults, picks a context size from your detected VRAM: 4096 tokens under
23GiB, 32768 at 23GiB and up. That default is often far too tight for an
agent that pastes file contents and tool output into every turn. Hearth
instead walks a ladder of context sizes (4096, 8192, 16384, 32768, 65536)
and picks the largest one that actually fits your GPU with real headroom to
spare.

Concretely, on a 6GB RTX 2060: Ollama's own default for that card is 4096
tokens. Hearth selects 16384 instead, verified end to end with the model
actually loading at `context_length=16384`. Doing the arithmetic on a 7B
Q4 coding model at that context leaves about 0.80GB of headroom on that
card; the same model at 32768 tokens does not fit at all. That is the
difference the KV-cache math makes over just picking "the biggest number
that sounds safe."

One honesty note that matters: on Windows, VRAM is read through
`nvidia-smi` when it's available, which is precise. When it isn't,
detection falls back to PowerShell or `wmic`, both of which read
`Win32_VideoController.AdapterRAM`, a signed 32-bit field. That field wraps
above roughly 4GB, so a 24GB card can report a small or even negative
number through that path. Every reading from the fallback path is marked
approximate, and the shop's verdicts are deliberately softened when they're
built on an approximate reading rather than a precise one - a confident
"this runs great" is never shown on a guessed number.

## What the permission modes mean

Hearth Code never runs unattended by default. Four modes, in
`agent/permissions.py`:

| Mode | What runs automatically | What needs your approval |
| --- | --- | --- |
| `plan` | Reads only | Everything else is refused outright, not just gated. The agent can look, not touch. |
| `edit` | Reads | Every file write and every dangerous action (shell commands, network calls) - shown to you first. **This is the desktop default.** |
| `auto` | Reads and file edits | Dangerous actions (shell, network) still stop and ask, unless the specific command is on a pre-approved allowlist. |
| `bypass` | Everything | Nothing. No prompts at all. |

Underneath the mode, every run also carries a capability manifest: a fixed
list of tools that run is allowed to touch at all, checked before the mode
logic runs and enforced in every mode including `bypass`. A run scoped to
fewer tools can't be talked into reaching for one outside that list no
matter what it reads or what mode it's in.

"`plan` refuses everything but reads" is a claim about the permission
engine, and the permission engine is only half of what has to be true for
it to hold. The other half is that every tool classified `safe` in
`agent/permissions.py`'s `RISK` table actually is read-only in what it
does - the engine trusts that classification, it doesn't independently
verify it. That trust broke once already on this branch: `git_diff` was
classified `safe`, but its `path` argument was interpolated into a shell
command string, so a crafted path reached a real shell and `plan` mode
allowed arbitrary command execution. The fix was to run git through argv
with no shell involved at all (`agent/hearth_tools.py`'s `_run_git_argv`),
and a property test now asserts that no tool classified `safe` can reach a
shell (`agent/hearth_tools.py`). The permission table below is accurate
today because of that test, not just because of the mode logic - if a
future `safe`-classified tool shells out again, that's the invariant to
check first.

Read [docs/limitations.md](limitations.md) before treating `auto` or
`bypass` as safe defaults for `run_command` specifically - the short version
is that approving a command by reading its text is not a real security
control on Windows.

## How undo works

Local models get things wrong often enough that cheap, reliable recovery is
a headline feature, not a nicety. Hearth's undo is git-backed: every
workspace gets its own hidden git store, owned by Hearth, pointed at your
workspace as its worktree, entirely separate from any `.git` you already
have there. A checkpoint is a plain `git add -A` and commit against that
shadow store, not a diff of whatever an edit tool happened to touch.

That distinction matters in practice: because a checkpoint captures the
whole tree, undo reverts damage done by shell commands too, not just by the
file-edit tools. A `rm`, a build script that rewrites a config file, a
command Hearth Code ran that clobbered something you didn't expect - all of
it is captured and revertible the same way, because capture is a whole-tree
snapshot, not an interception of specific write calls. That's a genuine
advantage over undo systems that only track their own edits.

Your own git state is never touched. The shadow store's index, working
tree pointer, and history are entirely separate files from your real
`.git`; your commits, branches, staged changes, and stashes are exactly as
you left them whether or not the workspace is a git repository at all.

A few things undo does not cover, on purpose: files matching common secret
patterns (like `.env`) are excluded from the shadow store's own capture, so
they are not restorable through undo; and a nested repository inside your
workspace is tracked as a pointer, not its actual content, so its own
history is what protects it, not Hearth's.

## Read this next

- [docs/model-shop.md](model-shop.md) - the model shop's honesty
  properties in full: fit verdicts, KV-cache math, and why there's no
  predicted tokens-per-second number.
- [docs/limitations.md](limitations.md) - what Hearth Code cannot
  protect you from yet, stated plainly. Read this before pointing it at
  anything you'd mind losing.
- [docs/security/windows-threat-model.md](security/windows-threat-model.md) -
  the full threat model this page's safety claims are drawn from.
