---
title: Limitations
description: The honest page. Read this before trusting Hearth with anything that matters.
---

This is the page most worth reading before you point Hearth Code at
anything you'd mind losing. Direct, on purpose: the goal is that you finish
this page knowing precisely what Hearth does and does not protect you
from, not reassured by adjectives. The full detail behind everything here
is in [docs/security/windows-threat-model.md](/hearth-windows/reference/threat-model/);
this is the plain-language version.

## Local models are the honest ceiling

Hearth runs whatever model you point it at, on your own hardware. That
model is very likely weaker than a large hosted model. It will misread
instructions, produce worse code, and get confused on long or ambiguous
tasks more often than a frontier hosted model would. Nothing about running
locally changes that; local model quality is the honest ceiling on what
Hearth Code can do, not something the product works around.

One concrete symptom: local models regularly fail to emit a proper
structured tool call and instead put something call-shaped in plain text
content - sometimes with a trailing comma, sometimes with other JSON that
doesn't quite parse. This isn't rare enough to treat as an edge case:
the agent loop carries a dedicated fallback parser
(`hearth_loop.parse_content_tool_calls`) specifically to recover tool calls
a local model wrote as text instead of using the API's structured field,
tolerant of the kind of malformed JSON that shows up in practice. There's
also a small library of hints appended to tool results for the mistakes
local models make repeatedly - using a Python package that was never
installed, running a command that isn't on PATH - so a weak model has a
chance to self-correct instead of looping on the same error. None of this
makes a small local model as capable as a large hosted one. It means the
gap is a known, designed-for reality, not a surprise you'll discover the
hard way.

## `run_command` is contained, but only against writes, and only if you ask

Say this plainly, because it used to be the single biggest gap in the
Windows build and it is now a smaller one rather than a closed one.
**`run_command` runs through a real shell, with the workspace only as its
working directory.** A working directory is not a security boundary. `cd ..`
gets out of it. An absolute path gets out of it. A command that never
touches the filesystem at all - a network call, a registry edit - was never
inside it to begin with.

`agent/hearth_sandbox.py` is what now stands behind that shell on Windows.
It has three levels, and the difference between them matters more than the
fact that it exists.

| what a command can reach | `off` | `limits` (default) | `workspace` |
|---|---|---|---|
| read files outside the workspace | yes | yes | **yes** |
| write to `%USERPROFILE%` | yes | yes | no |
| open a network connection | yes | yes | **yes** |
| spawn a child process | yes | yes | yes |
| read another process's memory | yes | yes | no |
| write inside the workspace | yes | yes | yes |

That table is produced by running real commands, not by reading flags:
`python agent/hearth_sandbox.py --probe` regenerates it on your machine.

**`limits` is the default and blocks nothing in that table.** It is honest
about that. What it does is drop every removable privilege from the child's
token, mark the Administrators groups deny-only, cap the command's total
committed memory and its process count through a Job object, deny it the
clipboard and the ability to reboot the machine, hand it exactly three
inherited handles, and place it in a second Job marked kill-on-close so no
command outlives Hearth. On a normal, non-elevated Windows account the
privilege drop changes almost nothing, because a standard user's token has
almost nothing to drop. It is genuinely useful against a runaway build and
against orphaned processes, and it is real containment only if Hearth is
run elevated. It costs nothing: no development command behaves differently
under it.

**`workspace` is the level that is actually a boundary, and it is opt-in.**
Set `HEARTH_SANDBOX=workspace`. The child then runs at a Windows integrity
level one step below Medium, and Hearth labels your workspace to match, so
the kernel's mandatory integrity policy refuses every write the command
makes to anything else: your home directory, your other repositories, the
registry under `HKCU`, Hearth's own audit database. Reading another
process's memory fails too.

Two things it does **not** do, and you should assume an attacker knows both:

- **It does not stop reads.** A command at this level still reads
  `%USERPROFILE%\.ssh\id_rsa`, `.aws\credentials`, your browser profile,
  and every other file your account can read. This is measured and asserted
  in the module's own test, not assumed.
- **It does not stop the network.** There is no egress wall on Windows.
  Combined with the point above, everything readable remains exfiltratable.

So `workspace` protects the *integrity* of your machine, not the
*confidentiality* of it. If that distinction is not enough for what you're
about to do, the answer is a separate Windows account or a VM, not a
setting in Hearth.

It also breaks real tools, which is why it is not the default. Measured on
Windows 11:

| command | `off` | `limits` | `workspace` |
|---|---|---|---|
| `git status`, `git commit` | works | works | works |
| `python -c`, writing files with Python | works | works | works |
| `dir`, writing inside the workspace | works | works | works |
| PowerShell | works | works | works |
| `bash`, `grep`, `sed` (Git for Windows) | works | works | **breaks** |
| `pip download` / `pip install` | works | works | **breaks** |
| `npm install` | works | works | **breaks** |

Every MSYS2/Cygwin binary dies at startup with
`fatal error - NtCreateDirectoryObject(\BaseNamedObjects\msys-2.0...)`,
because it needs a shared kernel object it can no longer create. That takes
out all of Git for Windows' `usr\bin`, which is exactly the set of tools a
Unix-trained model reaches for. `npm install` fails with `EPERM` on its
cache under `%LOCALAPPDATA%`. Native `git.exe` is unaffected.

Turning the level on also **modifies your workspace**: Hearth stamps an
integrity label on the directory and everything under it (about four
seconds for a 14,000-entry repository, once). That label only restricts
*writes from below Medium*; your own editor, your own shell and Hearth's
file tools are unaffected, and no other sandboxed process on the machine
gains access, because the label sits above the Low level that browser and
document sandboxes run at. `hearth_sandbox.unlabel_tree()` removes it.

Rejected alternatives, with the reason rather than a shrug: a token with
*restricting* SIDs (the Chromium renderer approach) and an AppContainer
both leave the child unable to run `git` or `python` at all, because a
developer's toolchain lives inside the user profile whose ACLs name only
the user's own SID. Windows Sandbox is a virtual machine per command. All
three were tried or costed before `workspace` was chosen; the detail is in
`agent/hearth_sandbox.py`'s docstring.

Until you turn `workspace` on, treat any `run_command` call that runs in
`bypass` mode, or that you approve in `auto` or `edit` mode, as equivalent
to handing the model a shell with your own privileges. Because that is
exactly what it is.

## Approving a command by reading it is not a security control

Hearth shows you the command before it runs in `edit` and `auto` mode, and
you can decline it. That's a real UI safeguard against an obviously bad
command, and it's worth having. It is not a security boundary, and the
reason is specific, not hand-wavy:

- **`cmd.exe` strips carets.** `s^e^t` executes `set`. A command can be
  visually mangled with characters that get silently removed before
  execution, so what you approved and what actually runs are not
  guaranteed to be the same text.
- **Substring expansion can assemble an executable name that was never in
  the string you looked at.** `%VAR:~n,m%`-style expansion can build up a
  command from pieces, none of which individually look like the thing that
  ends up running.
- **Shell metacharacters chain an unexamined payload onto an innocuous
  head.** `&&`, `;`, `|`, backticks, `$()` - a command whose visible first
  word looks completely safe can have a second command riding along after
  it that you never separately evaluated.

The convenience allowlist in `agent/permissions.py` (matching a command's
head against a set of pre-approved names like `git`, so `auto` mode doesn't
stop and ask for routine commands) is explicitly documented in that module
as exactly that: a convenience, not a security boundary. String inspection
of a shell command cannot be a security control, for the reasons above, and
this document says so plainly instead of leaving it to be rediscovered
later.

## What used to be a backstop, and isn't here

On the NixOS system, an agent's shell access sits inside a hardened
systemd unit: `ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, an
empty capability set, a syscall allow-list, a per-run private temp, and a
kernel-level nftables wall scoped to a run's declared allowed hosts. If a
run tries to escape its tool-layer allowlist by shelling out to `curl`
directly, the wall drops the packet at the kernel regardless of what the
process thinks it's doing.

The Windows build runs as a normal user-mode process on a machine full of
your real files. There is no equivalent backstop today. Two pieces of
containment have been rebuilt for Windows specifically: the workspace
boundary (`safe_join`, and the reparse-point pruning that closes the
NTFS-junction hole it originally had), which is enforced and tested against
a real escape, and the `run_command` sandbox above, which is enforced by
the kernel's integrity policy but only for writes and only when you enable
it. Neither has a second layer behind it the way it did on NixOS, and
neither replaces the nftables egress wall: nothing on Windows stops a
command from reaching the network.

## What is actually contained

To be fair about what does work: every file tool (read, write, edit,
search, list, replace) goes through `safe_join`, and that function rejects
directory traversal (`../`), drive-qualified absolute paths
(`C:\Windows\win.ini`), reserved Windows device names (`NUL`, `CON`,
`COM1`...), alternate data streams (`file.txt:hidden`), trailing dots and
spaces (which Windows silently strips, creating collisions), and wildcard
characters. A bare leading slash or a UNC-style path is neutralized to a
workspace-relative location rather than being allowed to reach the real
target. Directory walks prune NTFS junctions before descending into them,
because `os.path.islink` returns `False` for a junction and a naive walk
would otherwise follow it straight out of the workspace - a real bug found
and fixed on this branch, not a hypothetical.

The permission engine (`agent/permissions.py`) is a real boundary for tool
*dispatch*: whether a call is allowed to happen at all, gated for approval,
or denied outright depends on the current mode and, underneath that, on a
hard-capped capability manifest that applies in every mode including
`bypass`. What it cannot do is contain what happens once a `run_command`
call is allowed to proceed - that is the sandbox's job, and only to the
extent described above.

The sandbox level is deliberately not something the model can choose. It
comes from `HEARTH_SANDBOX` in Hearth's own environment; the arguments of a
`run_command` call are ignored entirely for this purpose, so a prompt
injection cannot talk the agent into asking for a weaker sandbox. There is
a test for exactly that in `agent/hearth_tools.py`.

## `.hearthignore` is a tool-level filter, not a secrecy boundary

This one is worth stating flatly, because the name invites the wrong
reading. `.hearthignore` narrows which paths the *file tools* will touch
inside an already-permitted workspace. Seven of the ten Windows tools call
`hearth_contain.is_ignored()` after `safe_join` has already approved a path,
and refuse an ignored one: `read_file`, `write_file`, `edit_file`,
`list_files`, `list_tree`, `search_files`, and `replace_in_files`. That is
the entire feature.

The other three never consult it at all:

- **`run_command` is not `.hearthignore`-aware.** `tool_run_command` in
  `agent/hearth_tools.py` hands the command string to `hearth_proc` and
  never looks at the ignore list. A shell command can read, print, copy, or
  overwrite any ignored file in the workspace, and its contents come back
  as ordinary tool output. This is the same gap as the section above: a
  shell command is not a path Hearth can classify before it runs.
- **`git_status` lists ignored paths** like any other changed file. It
  leaks the path name, not the contents.
- **`git_diff` runs `git diff --stat`**, so an ignored path that changed
  shows up in the summary. Again a name, not a file body.

So `.hearthignore` is genuinely useful for keeping a vendored directory out
of a search, or keeping the agent from rewriting generated code it should
leave alone. It is not a way to keep a credential file secret from a model
that can also run shell commands. If something must stay unreadable, it has
to sit outside the workspace, not behind an ignore rule.

What it *does* guarantee is narrowing-only: it can never widen access,
because every function that consults it classifies a path `safe_join`
already approved, and no pattern string ever reaches path resolution. A
hostile `.hearthignore` has nothing to widen. That guarantee is real and
tested; it is just a smaller claim than "the agent cannot see this file."

## Two scanners that detect and surface, and nothing more

`agent/hearth_injection.py` (prompt injection in content the agent reads)
and `agent/hearth_secrets.py` (credentials about to be written to a file)
both say the same thing about themselves, in their own docstrings, and it
is repeated here because it is the point most likely to be misread as
stronger than it is: **they are a signal, not a boundary.** Neither one
blocks a tool call, refuses a write, or strips anything from what actually
reaches the model or the disk. A finding is something the permission gate
and the human clicking approve get to see; absence of a finding is never
proof that content is safe.

Each has a specific, disclosed gap worth naming rather than leaving
implicit:

- The **injection scanner** can be evaded. Wrapping a real payload in fake
  quotes or narrative framing ("for example, ignore all previous
  instructions and...") gets the same discount a genuine security document
  quoting the same phrase gets - there is no way to tell the two apart from
  the text alone, and it is the single largest known evasion in this
  scanner.
- The **secret scanner** misses passphrase-style secrets. A real credential
  made of ordinary words ("correct horse battery staple") has the same low
  character-level entropy a placeholder does, and entropy is the signal
  this scanner leans on hardest. A weak-but-real secret phrased that way
  passes through unflagged.

Both scanners are also bounded to a head-and-tail scan window on anything
larger than about 60-80K characters; a payload or a secret placed only in
the untouched middle of a very large document is not seen. Each one sweeps
this repository's own files as a benign fixture set on every self-test run
and stays quiet on all of them (the injection scanner over every module in
`agent/` plus the README, this page, and the threat model; the secret
scanner over every module in `agent/` and `desktop/server/` plus every page
under `docs/`), and both catch real adversarial fixtures at high
confidence. "Caught these" is not the same claim as "catches everything,"
and this page will not pretend otherwise.

## Idle detection has a blind spot on headless Linux

`agent/hearth_idle.py` decides whether it's a good time to run heavy
background work. Its strongest signal, how long since the last keyboard or
mouse event, is Windows-only: it reads through `GetLastInputInfo`, which
has no headless-Linux equivalent. X11 needs an extension this module
doesn't use, and Wayland does not expose global input idleness to
unprivileged clients at all, by design. Where no signal is available at
all, the module defaults to "good time to run" rather than guessing busy -
the same asymmetry as everywhere else in this document: a wrong "always
busy" silently disables a feature, a wrong "idle" merely means a
background job ran while a human happened to be present undetectably.

## Undo still cannot restore what it never captured

Files matching common secret patterns (`.env`, `*.pem`, `id_rsa*`, and
similar) are excluded from the checkpoint store's own capture, on purpose,
so undo has never been able to restore damage inside one of them. That gap
is unchanged. What changed is that it no longer hides: restore now
re-scans for the same excluded files after putting everything else back,
and reports every one that was modified, deleted, or created since the
checkpoint, under `excluded_changed`, instead of letting a restore report
a clean list while a corrupted `.env` stays corrupted. Naming a gap is not
the same as closing it - if `excluded_changed` names a file, that file is
not back to what it was, and nothing currently in Hearth can put it there.

## Read this before you flip to `bypass`

`bypass` mode exists to let you skip every prompt. If you choose a model
you don't fully trust and run it in `bypass`, Hearth is not going to save
you from that choice - the permission engine documents this plainly
("everything runs, no prompts") rather than hiding it. The same is true,
more quietly, of any `auto`-mode command that lands on the convenience
allowlist: it runs without a prompt because its head matched a known-safe
name, not because anything deeper verified it was safe.

See [docs/security/windows-threat-model.md](/hearth-windows/reference/threat-model/)
for the full threat model, including what's explicitly out of scope
(an attacker who already has code execution as you, physical access,
vulnerabilities in Ollama or Windows itself) and the realistic attacker
ranking this page's priorities are drawn from.
