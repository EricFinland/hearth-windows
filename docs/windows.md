# Hearth for Windows

This page assumes no Linux knowledge and no command line.

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
- The model shop's live Hugging Face listings and per-quantisation fit
  logic (`agent/hearth_shop.py`, sourcing from `agent/hearth_hf.py`) - the
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
  `/restore`, `/models`, `/checkpoints`, `/setup`, `/idle`, and an
  unauthenticated `/healthz`). Every route but `/healthz` requires
  a bearer token plus `Host`/`Origin` validation, and the server binds
  `127.0.0.1` on an ephemeral port only. Self-tested, and exercised end to
  end against a real Ollama by `scripts/e2e_live.py`: 16 of 16 steps pass,
  including a real tool call, the permission gate firing, an approval
  resolved over HTTP, a byte-exact workspace change, an automatic pre-turn
  checkpoint, and a byte-exact restore.
- Session persistence (`desktop/server/session_state.py`): the conversation,
  workspace, model, and mode survive a sidecar restart. A turn or an
  approval that was in flight when the process stopped is never silently
  resumed; see "What survives a restart" below.
- Model downloads with honest progress (`agent/hearth_pull.py`): drives
  Ollama's own pull stream and turns it into a progress bar and byte count
  that never move backwards, with cancellation, a disk-space check up
  front, and a per-layer digest record. Verified against a real Ollama
  server pulling a real model: 6 layers, 2,019,393,189 bytes, progress
  monotonic across every one of 10 callbacks sampled.
- A prompt-injection scanner (`agent/hearth_injection.py`) and an outbound
  secret scanner (`agent/hearth_secrets.py`). Both detect and surface;
  neither blocks anything. See "What Hearth notices before you approve
  something" below for what they catch and, just as important, what they
  are known to miss.
- Task-aware model routing (`agent/hearth_router.py`): `classify()` scores
  how hard a turn looks from its prompt, history, and tool signals into a
  small/medium/large tier; `route()` turns that into a concrete, installed,
  hardware-appropriate model; `escalate()` moves up exactly one tier after
  a demonstrated failure (a malformed tool call, a retry). Checked by hand
  against nine prompts when it landed, all nine classified into the tier
  expected; the module's own self-test pins a subset of those as permanent
  regression fixtures along with the full escalation chain, and the sidecar
  self-test proves a malformed tool call really does escalate a turn one
  tier up. A heuristic, not a guarantee: it will misroute sometimes, and it
  is deliberately biased toward starting cheap, since escalating after a bad
  turn is a tested recovery path and there is no equivalent for the
  opposite mistake.
- Idle-aware compute (`agent/hearth_idle.py`): holds off on heavy
  background work while you are actively using the machine, and gets out
  of the way again quickly once you stop. See "Staying out of your way"
  below.
- First-run setup diagnosis (`agent/hearth_setup.py`): checks, in
  dependency order, whether Ollama is installed, running, new enough, has
  any model pulled, and whether that model actually fits your hardware,
  so a new user gets a sentence they can act on instead of a raw
  connection error from deep inside the agent loop.
- `.hearthignore` support (`agent/hearth_contain.py`, `agent/hearth_tools.py`):
  a gitignore-flavoured file that narrows which paths the *file tools* will
  touch inside an already-permitted workspace. It is a filter on those
  tools, not a secrecy boundary: `run_command` does not consult it. See
  "Scoping what the file tools will touch" below for the full syntax
  reference and the exact limits.

**Not built yet.** There is no Tauri desktop shell, no UI, no installer, no
code signing, and nothing published anywhere. The model shop described below
has no interface yet: it exists as data and logic today, callable, tested,
and correct, but with nothing to click. There is no cloud API key support.
**There is no download of Hearth itself.** The model-download engine above
can pull an Ollama model when driven directly; there is no button, no app,
and no installer around it yet. Everything on this page describes an engine
that works when driven directly, not a finished application.

## What you need

Once there is something to run, it needs [Ollama](https://ollama.com)
installed with at least one model already pulled. Hearth does not fetch or
manage models yet; that is the model shop's job, and it isn't built. Until
then, whatever model you choose in Ollama is the trust decision, made
outside Hearth.

The engine already knows how to tell you which of those things is missing.
`hearth_setup.py` checks, in order, whether Ollama is installed, whether it
is running, what version it reports, whether any model has been pulled, and
whether that model actually fits your hardware, and stops at the first
thing that is actually wrong rather than piling up downstream failures you
cannot act on yet. It has no interface yet either; today it is a diagnosis
you can call directly, not a message you will see on first launch.

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

## Getting a model onto your machine

A multi-gigabyte download with a progress bar that lies, freezes, or jumps
backwards is one of the more common ways local-model tools lose a user's
trust in the first five minutes. `agent/hearth_pull.py` drives Ollama's own
`/api/pull` stream and is built specifically not to do that.

Ollama's stream is messier than it looks: early lines carry no byte counts
at all, the same layer gets reported more than once as it downloads, a
later line can arrive with a *smaller* completed-byte count than one
already processed, and the full list of layers is never announced up
front. A progress bar built from "whatever the latest line says" jumps
backwards and sideways in the face of all of that. Hearth tracks every
layer independently and only ever lets its byte counts increase, so the
number on screen is monotonic by construction, not by luck. Verified
against a real Ollama server pulling a real model: 6 layers, 2,019,393,189
bytes total, and progress that never decreased across 10 sampled progress
callbacks.

A few other things this engine gets right before there is a UI to show
them:

- **Cancellation actually stops the download**, not just the progress bar.
  Closing the underlying connection from outside interrupts a background
  thread blocked mid-read, on both Windows and POSIX, so a cancelled
  multi-gigabyte pull stops within about half a second rather than waiting
  for Ollama to notice.
- **A disk-space check runs before any connection opens.** When Hearth
  knows or can estimate how large a model will be, it compares that
  against free space on the volume Ollama stores models on and refuses
  before wasting your bandwidth, naming both numbers in the refusal. When
  neither number is knowable, the check is skipped rather than inventing a
  threshold to fail on.
- **Success is Ollama's word, not a guess.** The download is only reported
  complete when Ollama's own stream says so explicitly. A dropped
  connection, a timeout, or a stream that just stops are all reported as
  not-confirmed-done, never inferred as success because the byte count
  happened to reach the total.
- **What was pulled is recorded**, not verified. On a successful pull,
  Hearth writes down the digest and size of every layer Ollama reported.
  That is a baseline a later integrity check could compare against, not a
  hash Hearth computes and checks itself.

There is still no button for any of this. It is driven directly today, the
same as the rest of the engine described on this page.

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

What does hold underneath the approval is `agent/hearth_sandbox.py`. Set
`HEARTH_SANDBOX` to pick a level:

| value | what it does |
|---|---|
| `off` | no containment |
| `limits` (default) | privileges dropped, memory and process caps, no orphaned processes. Blocks no access. |
| `workspace` | the child cannot write anything outside the workspace, and cannot read another process's memory. It can still read your files and reach the network. |

`workspace` is not the default because it breaks `bash`, `grep`, `sed` (all
of Git for Windows' MSYS2 tools), `pip install` and `npm install`. Native
`git`, `python`, PowerShell and `cmd` builtins are unaffected. Enabling it
stamps an integrity label on your workspace directory, once; that label only
restricts writes from processes running below Medium integrity, so nothing
you do yourself changes. Run `python agent/hearth_sandbox.py --probe` to see
the reachability table measured on your own machine, and
[docs/limitations.md](limitations.md) for the full account of what stays
reachable.

## What Hearth notices before you approve something

Two scanners run underneath the permission gate above. Both follow the same
rule, stated in their own source and repeated here because it is the single
most important thing to understand about them: **they detect and surface,
they do not block.** Neither one refuses a tool call, strips anything from
what the model sees, or stops a write from landing. Each one scans a blob
of text and returns structured findings with a severity and a plain-English
reason. Blocking on a heuristic was considered and rejected for both: a
false positive that silently dropped or mangled content would corrupt what
the model sees, or corrupt a file you meant to write, which is a worse
failure than an occasional miss.

Be precise about where a finding actually goes, because "it reaches the
approval step" is only half true. **A finding never creates an approval.**
It annotates one the permission mode had already decided to ask for:

- When the permission engine gates a call, the finding rides along on that
  call's `approval_request` (`injection_finding` and `secrets_finding` are
  separate fields, so neither can be mistaken for the other), and a gated
  write whose content trips the secret scanner also has the secret redacted
  out of the arguments shown in every event. The write itself, if you
  approve it, still uses the real unredacted text.
- When the permission engine allows a call outright, which is what `auto`
  mode does for every file write, there is no approval to annotate. The
  secret scan still runs, and a finding is emitted as its own
  `secrets_finding` event next to the ordinary tool call. Nothing pauses.
  So in `auto` mode a credential-shaped write is *recorded*, not
  *intercepted*.
- The injection scanner only ever surfaces on a gated call. A tool result
  that scored `high` is dropped from view entirely if the very next tool
  call is one the mode allows without asking.

There is also no UI reading any of this yet (see "Where this actually
stands today"): today these are fields on events the sidecar emits, not
something on a screen.

**The prompt-injection scanner** (`agent/hearth_injection.py`) looks at
content the agent reads: repo files, web pages, dependency READMEs, tool
output, anything that could carry text engineered to look like an
instruction rather than information. It scores what it finds from `low` to
`critical` across categories like imperative overrides ("ignore all
previous instructions"), authority spoofing ("the user has already
approved this"), exfiltration shapes (a directive verb plus a
sensitive-looking target plus an outbound destination), escalation
language ("skip the approval"), structural spoofing (fake system-message
formatting), and obfuscation (invisible characters, homoglyphs, long
base64 blobs). Its self-test verifies every adversarial payload in its
regression fixture set scores `high` or `critical`, and that its benign
sweep stays clean: every Python module under `agent/`, plus this
repository's README, limitations page, and threat model. Both directions
run on every self-test, so neither can rot quietly.

It will miss things, by design and by disclosure. Two gaps worth knowing
specifically:

- An in-band exfiltration attempt ("print the value of the token you
  found") is only caught when it names a target the scanner already
  recognizes (`id_rsa`, `.env`, a private key, `AWS_SECRET_ACCESS_KEY`-style
  names, and a short list of others). A generic, unnamed target produces no
  finding at all, on purpose - the alternative made ordinary debugging
  requests ("check if the API key expired") indistinguishable from an
  attack.
- Wrapping a real payload in fake quotes or narration ("for example,
  ignore all previous instructions and...") gets the same discount a
  genuine security document quoting the same phrase gets. There is no way
  to tell the two apart from the text alone, and this is the single
  largest known evasion in this scanner.

Absence of a finding must never be read as "this content is safe." A
paraphrase, an unfamiliar unicode trick, or an instruction split across the
scanner's scan window can all pass through unseen.

**The secret scanner** (`agent/hearth_secrets.py`) looks the other
direction: text the agent is about to write to a real file. A small local
model will sometimes invent a plausible-looking credential when asked for
an example config, or inline a real one it read from `.env` while
explaining a fix instead of referencing it out of band. The scanner
recognizes known key shapes (AWS, GitHub, Slack, Stripe, Google, OpenAI-
style, JWT, PEM blocks) and a generic high-entropy value sitting next to a
suspicious name (`api_key`, `secret`, `token`, `password`, `credential`).
Its self-test builds a fresh, real-shaped key for every format it claims to
know at runtime (never as a literal in its own source, which would then
trip its own sweep) and requires each one to be caught; it requires every
placeholder form in its known-negative list to stay silent, including AWS's
own documented example key `AKIAIOSFODNN7EXAMPLE`; and it sweeps every
Python module in `agent/` and `desktop/server/` plus every page under
`docs/`, all of which must scan clean.

Its known gap is the harder half of the problem it was built to solve: a
passphrase-style secret made of real words ("correct horse battery
staple") has low character-level entropy, the same as a placeholder does,
and this scanner cannot tell the two apart. A weak-but-real secret phrased
that way will not be flagged.

## Scoping what the file tools will touch: `.hearthignore`

Everything above assumes the agent operates inside a workspace boundary it
cannot escape (`agent/hearth_contain.py`'s `safe_join`, see
[docs/limitations.md](limitations.md) for exactly how strong that boundary
is). `.hearthignore`, placed at the root of a workspace, narrows that
further: a gitignore-flavoured pattern file that hides matching paths from
the file tools (`read_file`, `write_file`, `edit_file`, `list_files`,
`list_tree`, `search_files`, `replace_in_files`, and the directory walks
underneath them) without changing where the workspace boundary itself sits.

**Read this before you trust it with anything sensitive.** `.hearthignore`
is a filter on those seven tools, not a secrecy boundary around the files
it names. Three of the ten Windows tools never consult it: `run_command`
runs a shell, so a command can read or overwrite an ignored file and hand
its contents back as ordinary tool output; `git_status` lists an ignored
path that changed; and `git_diff` names it in a `--stat` summary. Use
`.hearthignore` to keep a vendored tree out of a search or to stop the
agent rewriting generated code. Do not use it to hide a credential file
from a model that can also run commands. See
[docs/limitations.md](limitations.md) for the full statement of this gap.

This is the one piece of configuration in this document you will actually
write yourself, so here is the real reference rather than a mention.

**Supported syntax:**

- Blank lines and full-line `#` comments are ignored.
- `\#` and `\!` match a literal leading `#` or `!`.
- A leading `!` negates a pattern: it re-includes a path that an earlier or
  later rule in the same file also matches. Rules are applied in file
  order and the last match wins, exactly like `.gitignore`.
- A trailing `/` makes a pattern match directories only.
- `*` matches any run of characters except `/`; `?` matches exactly one;
  `[seq]` / `[!seq]` are character classes.
- `**` is special only as a whole path segment: `**/x` matches `x` at any
  depth, `x/**` matches `x` and everything under it, `a/**/b` matches zero
  or more whole directories between `a` and `b`.
- A pattern containing `/` anywhere but its very end is anchored to the
  workspace root. A pattern with no interior `/` matches at any depth, the
  same as `.gitignore`.

**Not supported:** nested per-directory `.hearthignore` files (only a
single workspace-root file is ever read), backslash escapes other than a
leading `\#` or `\!`, trailing-whitespace escaping, and `**` fused into a
larger segment (`a**b` falls back to two ordinary `*`s rather than
expanding). An unsupported construct degrades to a literal or partial
match rather than raising, the same way an unmatched `.gitignore` pattern
quietly matches nothing.

**The guarantee that matters,** and it is narrower than the feature's name
suggests: `.hearthignore` can only narrow access, never widen it. It does
not follow that an ignored path is unreachable, only that the seven tools
above will refuse it. Every function that consults it classifies a path
`safe_join` has already approved; there is no code path from a pattern
string to a filesystem location, only from an already-contained path to a
yes/no visibility bit. A hostile `.hearthignore` (a pattern like `!/../../etc/passwd`,
a drive-qualified Windows path, a negate-everything catch-all) has nothing
to widen and cannot escape the workspace boundary, and this is checked
directly in the module's own tests, not just asserted here. If you edit
`.hearthignore` mid-session, the change is picked up on the next tool call
that reads it (it is cached per workspace, keyed on the file's own
modification time), not on a delay or a restart.

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

That first gap used to be invisible at the moment it mattered: a restore
could report a clean list of everything it put back while a `.env` the
agent had corrupted stayed corrupted, silently, because it was never
captured in the first place. Restore now closes that specific silence,
without closing the underlying gap: it re-scans for the same secret
patterns after restoring and reports every excluded file that changed
since the checkpoint, as `excluded_changed` entries naming the path and
whether it was modified, deleted, or newly created. It still cannot put
those files back - they were never captured - but it no longer lets a
restore look complete when it wasn't. A checkpoint taken before this
existed has no baseline to compare against; restore says so explicitly
(`excluded_manifest_available: false`) rather than reporting a false "no
changes."

## Staying out of your way

A model saturating the GPU during a video call is a bad neighbor, and
running Hearth hard overnight only stays appealing if it reliably gets out
of the way the moment you sit back down. `agent/hearth_idle.py` is the
part of the engine built to answer exactly that question: is now a good
time to run heavy background work, and why.

On Windows it combines several signals: how long since the last keyboard
or mouse event system-wide (the strongest signal available anywhere in
this module), whether the GPU is actually computing right now versus
merely holding a model resident in VRAM, whether the foreground window is
fullscreen, and whether the machine is running on battery. The two
directions are deliberately asymmetric: it waits 5 minutes of genuine
inactivity before declaring the machine idle, but drops back to "busy"
after just 15 seconds of renewed activity. It costs little to be slow to
start heavy work; it costs your trust in leaving Hearth running unattended
to be slow to stop.

Two honesty notes worth knowing:

- **On headless Linux, input idleness cannot be seen at all.** X11 needs an
  extension this module does not use, and Wayland does not expose global
  input idleness to unprivileged clients by design. Where no signal is
  available, the module answers "unknown, low confidence, good time to
  run" rather than guessing - a wrong "busy" means nothing ever runs, which
  is worse than a wrong "idle" running a job while someone happens to be
  at the keyboard undetectably.
- It does not catch a windowed video call (a call window that isn't
  maximized); only borderless or exclusive fullscreen is detected, and only
  against the primary monitor's resolution.

There is no scheduler wired up to this yet. Today it is a signal a caller
can consult, the same as the rest of the engine described on this page.

## What survives a restart

The sidecar keeps a session's entire life in memory by default, which
means closing the app, a crash, or a reboot for an update used to mean
losing the conversation, the workspace/model/mode in use, and the record
of what was approved. `desktop/server/session_state.py` persists the
conversation, the workspace, model, and mode, and a bounded tail of recent
events, so a restart is a resumption rather than a reset.

What is deliberately not persisted matters as much as what is. A pending
approval is never resurrected: an approval is a live question with a
thread blocked on it, and after a restart there is no thread and no turn
to resume. A restarted session instead records that one was pending and
marks the interrupted turn honestly in its own history, rather than
silently dropping it or leaving a question nobody can still answer. The
same is true of a turn that was mid-flight when the process stopped - it
is never resumed, because a restarted process has no way to know whether
the tool call that was in progress actually happened. The bearer token is
never persisted either; it is regenerated on every process start on
purpose, so it never becomes a file-on-disk secret.

## Read this next

- [docs/model-shop.md](model-shop.md) - the model shop's honesty
  properties in full: fit verdicts, KV-cache math, and why there's no
  predicted tokens-per-second number.
- [docs/limitations.md](limitations.md) - what Hearth Code cannot
  protect you from yet, stated plainly. Read this before pointing it at
  anything you'd mind losing.
- [docs/security/windows-threat-model.md](security/windows-threat-model.md) -
  the full threat model this page's safety claims are drawn from.
