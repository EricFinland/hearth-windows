---
title: MCP servers
description: Connecting Model Context Protocol servers, and the gate every tool call passes through.
---

Hearth can drive an external [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio and offer its tools to the model alongside the built-in ten.
The client is `agent/hearth_mcp.py`. Standard library only, like everything
else in `agent/`.

There is no MCP server configured by default. Without a config file Hearth
behaves exactly as it did before this existed: no subprocess, no extra tools,
not even an import.

You can manage servers from the **Tools** tab in the app, or by editing the
config file by hand. Both change the same file. See
[Managing servers from the app](#managing-servers-from-the-app).

## Managing servers from the app

The Tools tab, next to Chat and Model shop, lists every server in the config
file as a card:

* a status: running, starting, ready (enabled, and starts with the next turn
  that needs tools), disabled, could not start (with the server's error), or
  ignored (with the reason the loader skips a hand-edited entry);
* an on/off switch;
* the command and each argument as a separate pill, so an argument that
  contains a space cannot pass for two;
* the environment variables, with anything that looks like a credential
  hidden (see below);
* once a turn has started the server, its tools, each with the risk class it
  landed in.

**Test** starts a separate copy of the server, does the handshake, asks for
its tools, and stops it again. It shows the server's name and version, how
long the handshake took, the server's description of itself, and every tool
with its risk class, so you can see what a server would expose before any
model sees it. Each step has a 15 second limit. Test never touches the copy a
turn is using, and a disabled server cannot be tested, because testing runs
it: switch it on first.

**Add** and **Edit** open a form with a name, a command, one row per
argument and one row per environment variable. What the form accepts is
stricter than what the file accepts, because a value arriving over the
sidecar's HTTP connection is not the same as one you typed into the file:

* The name becomes part of every tool name (`mcp__<name>__<tool>`), so it is
  letters, digits, `-` and `_`, starting with a letter or digit, without `__`.
* The command is one program: a name on your `PATH` like `npx`, or a full
  path. It is never a command line. Hearth does not split it into arguments
  and never runs it through a shell, so `npx -y some-server` is refused with
  a message to put the arguments in their own rows. Relative paths, network
  paths, quotes, shell characters and unexpanded `%VARIABLES%` are refused
  too.
* Arguments and values cannot contain line breaks or control characters, and
  two variable names that differ only by case are refused, because Windows
  treats them as one.

On Windows, `npx` is a batch script (`npx.cmd`), which Windows will not start
directly. Use `cmd` as the command, with `/c`, `npx`, `-y` and the package as
the first arguments. The card says so when it spots this.

### Adding a server asks first

Adding a server lets a program run on your computer with your permissions.
Before a server is added, or before a change to what it runs (command,
arguments, environment, working directory) is saved, Hearth shows a dialog
that says so in those words, shows exactly what will run, names the
environment variables it will set, and lists any warnings: for example that
`cmd` or `powershell` reads its arguments as a command line, or that a
variable like `NODE_OPTIONS` changes which code a program loads. A new server
is saved switched off unless you tick **Enable it now** in that dialog.

This is enforced by the sidecar, not only by the page: a save without the
explicit acknowledgement is refused. That does not make the sidecar's token
less powerful, since whoever holds it can still choose what Hearth runs. It
makes using it that way deliberate and visible.

### Credentials stay hidden

An environment variable whose name contains `key`, `token`, `secret`,
`pass`, `auth`, `cred`, `session` or `cookie`, or whose value Hearth's secret
scanner recognises, is shown as at most its first two characters and its
length. Arguments are treated the same way: `--api-key=...`, the value after
`--token`, or anything the scanner flags. The full value never leaves the
sidecar. When you save without touching a hidden value, the page asks the
sidecar to keep the stored one; to change it, press **Replace** and type the
new value.

### A change takes effect at once

Hearth keeps MCP servers running between turns. When you switch a server off,
remove it, or change one that is on, Hearth stops its running MCP servers and
forgets their tools, and the next turn that needs tools starts them again
from the file as it now stands. A server you switched off is never started
again. Because that would cut off a tool call in the middle, such a change is
refused while a turn is running (or while a cancelled turn's tool call is
still finishing); the panel says so, and you can try again when the turn ends
or after pressing Stop. Changes to a server that is off before and after are
always allowed.

### How the file is written

The panel writes the same file in the same format. Writes are atomic (a
temporary file, then a rename), UTF-8 without a byte-order mark, readable and
writable by your user only where the file system has permissions, and keep
everything the panel does not edit: other top-level keys, and each server's
`risk`, `cwd`, `timeout` and anything else you put there. A file that cannot
be parsed is reported and never overwritten, so a typo cannot cost you your
hand edits.

## Where config lives

| Platform | Path |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Hearth\mcp.json` |
| Linux | `$XDG_DATA_HOME/hearth/mcp.json`, or `/var/lib/hearth/mcp.json` when the daemon owns that directory |

That is `hearth_paths.data_dir()`, the same per-user directory that already
holds the audit database and the checkpoints. `HEARTH_MCP_CONFIG` overrides it.

```json
{
  "servers": {
    "roblox": {
      "command": "C:\\Users\\you\\AppData\\Local\\Roblox\\Versions\\version-xxxxxxxx\\StudioMCP.exe",
      "args": [],
      "env": {},
      "timeout": 120,
      "enabled": true,
      "risk": { "screen_capture": "dangerous" }
    }
  }
}
```

* `command` and `args` are what Hearth runs. Required.
* `env` is added to the minimal child environment `hearth_proc.child_env`
  builds. Hearth's own environment (audit database path, tokens, spend caps)
  is not inherited.
* `timeout` bounds a single tool call, in seconds.
* `risk` may make a tool more restricted than Hearth worked out on its own.
  It can never make one less restricted. See below.

Run `python agent/hearth_mcp.py --live` to connect to everything in the file
and print each server's handshake, its tools, their schemas, and the risk class
each one landed in. Do that before letting a model near a new server. The
**Test** button in the Tools tab does the same for one server, using the same
code.

The file may be saved with or without a byte-order mark (Notepad adds one);
Hearth reads both.

## Why that location, and what it does not protect

This file names an executable. Whoever can write it chooses what code Hearth
runs the next time it starts a server. So the location is a security decision:

* On Windows, `%LOCALAPPDATA%` is ACL'd to the user by the OS.
* On Linux, the XDG data directory is mode 0700 by convention, and
  `hearth_mcp` refuses outright to read a config file that is group- or
  world-writable.
* It is not inside any workspace, so `write_file`, `edit_file` and
  `replace_in_files`, which are contained to the workspace, cannot reach it.

Three things it does not protect against, stated plainly rather than left to
be discovered:

1. **`run_command` is not sandboxed against writing this file.** At any level
   below `workspace` there is no write boundary at all, and at `workspace` the
   boundary is on the workspace, not a deny list for the rest of the user
   profile. An agent allowed to run shell commands can rewrite this file and
   choose what Hearth launches next. That is not a new capability, since it
   already had arbitrary code execution, but this file is a control on
   everybody else, not on the agent.
2. **`HEARTH_MCP_CONFIG` moves the file.** Anything that can set Hearth's
   environment can already choose its Python path, so this adds no exposure,
   but it is a knob and it is worth knowing about.
3. **The Tools tab can write it.** Anything holding the sidecar's bearer
   token can add a server through the same routes the tab uses. The sidecar
   insists on the explicit acknowledgement described above and starts new
   servers switched off, which makes that a deliberate act rather than a
   side effect, but the token is still enough. The token lives only in the
   shell's memory and is regenerated on every start.

## Tool names

Every MCP tool is offered to the model as `mcp__<server>__<tool>`, for example
`mcp__roblox__get_studio_state`. No built-in uses that prefix, and a self-test
asserts it against the built-in list, so an MCP server cannot shadow
`read_file` no matter what it calls its tools.

The capability manifest works on those names like any other, so a run can be
given exactly the MCP tools it needs and nothing else. The manifest is a hard
cap in every mode, `bypass` included.

## Risk classes

MCP tools carry optional annotations. Hearth derives a risk class from them:

> `dangerous`, unless the server says read-only **and** non-destructive **and**
> closed-world, in which case `safe`.

Nothing is ever derived as `edit`, because `edit` is the class `auto` mode runs
without asking, and a tool that mutates a live game scene must not run unasked.
A tool with no annotations is `dangerous`. A tool Hearth has never seen is
unknown to `permissions.py`, which already fails closed to `dangerous`.

The `risk` map in config is clamped: it may move a tool from `safe` to `edit`
or `dangerous`, and a request to move one the other way is ignored.

Those annotations come from the server, so a hostile server could claim
everything is read-only. That is worth being clear about. A configured MCP
server is an executable Hearth starts, so it already has whatever the user has,
and no risk table can claw that back; the config file is the boundary, not the
annotation. What the derivation genuinely buys is protection from a confused or
jailbroken model quietly triggering side effects the server itself describes as
side effects.

## Tool results are untrusted

A tool result is text produced by another process. It reaches the model, and
through the UI it reaches a person. Hearth returns MCP results as ordinary
tool-result strings, so they travel exactly the path every other tool result
travels: the sidecar's prompt-injection scan, the flight recorder, the
transcript, and `neutralize` in `desktop/ui/js/dom.js` before anything is
displayed. Nothing in the MCP path is treated as instructions and nothing
shortcuts that handling.

## Process hygiene

An MCP server left running after Hearth exits is the same bug as an orphaned
shell command, so it gets the same fix. On Windows the child is assigned to the
process-wide `KILL_ON_JOB_CLOSE` Job object from `hearth_sandbox`, the one
`llama-server` already uses: when Hearth dies for any reason, including a crash
or a task-kill, Windows terminates everything still in the Job. On Linux the
child gets its own session plus `PR_SET_PDEATHSIG`.

Ordinary shutdown is still polite first: close the child's stdin, wait, then
tree-kill through `hearth_proc`. The Job is a backstop, not a strategy. The
module's self-test proves it by starting a server from a helper process,
hard-killing that helper with no tree walk, and asserting the server is gone.

## Bounds

Every wait is bounded, so no server can hang Hearth:

| | |
| --- | --- |
| handshake | 30s |
| `tools/list` | 60s |
| one tool call | `timeout` from config, default 120s |
| polite shutdown before a tree-kill | 5s |
| the Tools tab's **Test** | 15s for the handshake, 15s for `tools/list` |
| longest single output line kept | 4 MiB, then discarded to the next newline |
| unparseable lines tolerated | 200 |
| result text kept | 8000 characters, then truncated with a note |

A server that dies, floods stdout without ever ending a line, answers an id
nobody sent, or simply never replies produces an error inside those bounds
rather than a stuck loop.
