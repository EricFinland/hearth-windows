# MCP servers

Hearth can drive an external [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio and offer its tools to the model alongside the built-in ten.
The client is `agent/hearth_mcp.py`. Standard library only, like everything
else in `agent/`.

There is no MCP server configured by default. Without a config file Hearth
behaves exactly as it did before this existed: no subprocess, no extra tools,
not even an import.

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
each one landed in. Do that before letting a model near a new server.

## Why that location, and what it does not protect

This file names an executable. Whoever can write it chooses what code Hearth
runs the next time it starts a server. So the location is a security decision:

* On Windows, `%LOCALAPPDATA%` is ACL'd to the user by the OS.
* On Linux, the XDG data directory is mode 0700 by convention, and
  `hearth_mcp` refuses outright to read a config file that is group- or
  world-writable.
* It is not inside any workspace, so `write_file`, `edit_file` and
  `replace_in_files`, which are contained to the workspace, cannot reach it.

Two things it does not protect against, stated plainly rather than left to be
discovered:

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
| longest single output line kept | 4 MiB, then discarded to the next newline |
| unparseable lines tolerated | 200 |
| result text kept | 8000 characters, then truncated with a note |

A server that dies, floods stdout without ever ending a line, answers an id
nobody sent, or simply never replies produces an error inside those bounds
rather than a stuck loop.
