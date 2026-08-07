# Security Policy

Hearth runs local language models and an autonomous coding agent on your own
Windows machine. This document explains what it defends, what it does not,
and how to report a problem.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Open a private report through GitHub Security Advisories: the **Security** tab
on this repository, then **Report a vulnerability**. That channel is private
between you and the maintainer until an advisory is published.

You will get an acknowledgement. Once a fix is out, the advisory will credit
you unless you ask otherwise.

If a report concerns the release signing key or the update channel, say so in
the first line. Those are the two places where a problem reaches every install
at once.

## Supported versions

Hearth is in development and nothing has been published. There is no released
version to support yet. Security fixes land on `main`. When releases begin,
this section will name the supported line.

## What Hearth defends

The full analysis is [docs/security/windows-threat-model.md](docs/security/windows-threat-model.md).
The short version, and the honest one:

- **Tool calls are contained to a workspace.** `agent/hearth_contain.py` is the
  single boundary every file tool resolves through. It rejects paths outside
  the workspace root, refuses to follow symbolic links and NTFS junctions out
  of it, and normalises Windows path forms (short names, alternate data
  streams, device paths, case variants) before deciding. `agent/hearth_proc.py`
  runs commands as child processes with a bounded environment and a tree kill.
- **Commands are gated, not free.** `agent/permissions.py` decides every tool
  call against the active mode (`plan`, `edit`, `auto`, `bypass`). In the modes
  that ask, a human sees the exact command or the exact write before it
  happens.
- **Content that tries to steer the model is flagged.**
  `agent/hearth_injection.py` scans what the agent reads (files, tool output,
  fetched pages) for instructions aimed at the model rather than at the user,
  and surfaces them for approval instead of acting on them silently.
- **Credentials on the way out are flagged.** `agent/hearth_secrets.py` scans
  text the agent is about to write to disk for key shapes and high-entropy
  values sitting next to credential-shaped names.
- **Every turn is undoable.** `agent/hearth_checkpoint.py` keeps a shadow git
  store outside the workspace, so a turn can be reverted even in a directory
  that is not itself a repository.
- **The sidecar is not reachable from anywhere else.**
  `desktop/server/auth.py` binds to loopback and requires a per-launch bearer
  token handed to the shell over its own stdout. Nothing on the network can
  talk to it.
- **Updates are verified against a key, not a host.**
  `agent/hearth_update.py` accepts a release manifest only when it carries a
  valid Ed25519 signature from a key listed in `release/trust.json`, which
  ships inside the installer. A compromised download host cannot make Hearth
  install anything. See [docs/updates.md](docs/updates.md).

## Sharp edges to know about

These are stated plainly because a security document that only lists defences
is marketing.

- **There is no OS sandbox.** Hearth runs as an ordinary user process with the
  user's own privileges. There is no mandatory access control, no syscall
  filtering, and no kernel-level egress wall. Containment is enforced by
  Hearth's own code, in Hearth's own process. A bug in that code is a
  containment failure, and a command the agent runs is outside it entirely:
  a contained subprocess is still a subprocess with the user's rights.
- **`.hearthignore` is a scope control, not a secrecy boundary.** It narrows
  which paths the file tools will touch. It does not stop a shell command from
  reading a file, and it is not a permission system.
- **The scanners are signals, not boundaries.** The injection scanner and the
  secret scanner both report; neither blocks. That is deliberate and the
  reasoning is written at the top of each module: a heuristic that silently
  mangled a real write would corrupt the user's own files, which is worse than
  a missed detection.
- **The installer is unsigned.** Until code signing is in place, a downloaded
  installer triggers a full-screen SmartScreen warning, and there is no way for
  a user to tell a real Hearth installer from a forged one by inspection. This
  is why nothing is published. See
  [docs/code-signing-policy.md](docs/code-signing-policy.md).
- **Local model quality is a real limit.** A small local model is more easily
  talked into something than a large hosted one. The permission modes exist
  because the model's judgement is not the thing standing between an
  instruction and your files.

[docs/limitations.md](docs/limitations.md) is the longer version of this
section and the page worth reading before pointing Hearth Code at anything you
would mind losing.
