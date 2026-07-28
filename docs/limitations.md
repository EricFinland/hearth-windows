# Limitations

This is the page most worth reading before you point Hearth Code at
anything you'd mind losing. Direct, on purpose: the goal is that you finish
this page knowing precisely what Hearth does and does not protect you
from, not reassured by adjectives. The full detail behind everything here
is in [docs/security/windows-threat-model.md](security/windows-threat-model.md);
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

## `run_command` is not contained

Say this plainly, because it's the single biggest gap in the Windows
build: **`run_command` runs through a real shell, with the workspace only
as its working directory.** A working directory is not a security
boundary. `cd ..` gets out of it. An absolute path gets out of it. A
command that never touches the filesystem at all - a network call, a
registry edit - was never inside it to begin with.

The workspace boundary that *does* exist (`agent/hearth_contain.py`'s
`safe_join`) protects the file tools: read, write, edit, search, list,
replace. It does not protect `run_command`, because a shell command isn't a
path Hearth can validate before it runs. On NixOS, this same gap sat behind
systemd sandboxing and a kernel-level nftables egress wall, so even an
unconstrained shell command couldn't reach outside its sandbox or phone
home to an unapproved host. **On Windows, none of that exists.** There is
no mandatory access control, no syscall filtering, no kernel-level egress
wall standing behind `run_command`. It runs as an ordinary process with
your own full account privileges, minus a reduced environment (see below).

An AppContainer or restricted-token sandbox is the real fix for this, and
it is not built yet. Until it is, treat any `run_command` call that runs in
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
your real files. There is no equivalent backstop today. The workspace
boundary (`safe_join`, and the reparse-point pruning that closes the
NTFS-junction hole it originally had) is the one piece of containment that
has been rebuilt for Windows specifically and tested against a real
escape. It is currently the strongest boundary in the system, and it is
also the *only* boundary in that spot - there is no second layer behind
it the way there was on NixOS.

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
call is allowed to proceed - see above.

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
the untouched middle of a very large document is not seen. Both stayed
quiet on this repository's own files during testing (48 files each) and
both caught real adversarial fixtures at high confidence, but "caught
these" is not the same claim as "catches everything," and this page will
not pretend otherwise.

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

See [docs/security/windows-threat-model.md](security/windows-threat-model.md)
for the full threat model, including what's explicitly out of scope
(an attacker who already has code execution as you, physical access,
vulnerabilities in Ollama or Windows itself) and the realistic attacker
ranking this page's priorities are drawn from.
