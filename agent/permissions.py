#!/usr/bin/env python3
"""hearth permission engine: decide whether an agent may run a tool given the
current permission mode. Pure and I/O-free so it is trivially testable and shared
by every drive path (interactive sessions and background workers).

Permission modes:
  plan   - read-only; the agent may look but change nothing, then must produce a plan.
  edit   - reads run automatically; every file change and every dangerous action
           is shown to the user for approval. The desktop default.
  auto   - safe reads and file edits run automatically; dangerous actions are gated
           (the user must approve each one).
  bypass - everything runs, no prompts.

Decision values:
  "allow" - run the tool now
  "gate"  - pause and ask the user to approve or deny
  "deny"  - refuse outright (and tell the model why)
"""

import sys

MODES = ("plan", "edit", "auto", "bypass")

# Risk class per tool: "safe" (reads), "edit" (file writes), "dangerous"
# (shell, network, sudo). Unknown tools are treated as dangerous (fail closed).
#
# Tools from configured MCP servers are added to this dict at run time by
# hearth_mcp.Registry, under names prefixed "mcp__". They are written in here
# rather than resolved through a callback so `decide` below stays the pure,
# I/O-free function this module's docstring promises. The consequence of the
# ordering is the safe one: an MCP tool that has not been registered yet is
# simply absent, and risk_of already fails closed on absence, so a decision
# taken before registration can only ever be too strict.
RISK = {
    "read_file": "safe",
    "list_files": "safe",
    "list_tree": "safe",
    "search_files": "safe",
    "edit_file": "edit",
    "current_generation": "safe",
    "list_generations": "safe",
    "system_health": "safe",
    "read_self_config": "safe",
    # git_status/git_diff are genuinely read-only ONLY because hearth_tools
    # runs them via an argv list straight to subprocess.run, with no shell
    # in between. That was not always true: a prior version built a shell
    # command STRING for these two tools with only space-aware quoting on
    # git_diff's model-supplied 'path' argument, which let a value like
    # 'x&whoami' run a second, unexamined command with no approval prompt in
    # every mode (including 'plan', which every doc describes as read-only)
    # because "safe" here is exactly what grants that free pass. Re-verify
    # the argv-only property in hearth_tools.py before ever touching this
    # value; see the property test there over every RISK=="safe" entry in
    # WINDOWS_TOOLS, which exists specifically to catch this again.
    "git_status": "safe",
    "git_diff": "safe",
    "write_file": "edit",
    "run_command": "dangerous",
    "http_request": "dangerous",
    "web_search": "dangerous",
    "web_fetch": "dangerous",
    "nix_check": "safe",
    "write_self_config": "edit",
    "remember": "safe",
    "recall": "safe",
    "kb_search": "safe",
    "kb_add": "edit",
    "replace_in_files": "edit",
    "fetch_to_kb": "dangerous",
    "index_dir": "edit",
}


def risk_of(tool):
    return RISK.get(tool, "dangerous")


def _command_head(args):
    """The executable name at the head of a shell command, without directory or
    extension.

    A naive cmd.split()[0] breaks on Windows in two ways: a quoted path
    containing a space yields '"C:\\Program', and a full path yields something
    that never matches a bare allowlist entry like 'git'.

    This is allowlist matching for convenience, not a security control. String
    inspection of a shell command cannot be one: cmd.exe strips carets, so
    's^e^t' runs 'set', and %VAR:~n,m% substring expansion assembles an
    executable name that never appears in the approved string.

    Quoting the path is not a mitigation against this limitation. Shell
    metacharacters like &&, ;, |, &, backticks, and $() can chain an
    unexamined payload after an approved executable name. Reliable containment
    belongs at the execution layer, not string inspection: that layer is
    agent/hearth_sandbox.py, and how much it actually contains depends on the
    level the user chose. Nothing here should get more permissive because that
    module exists -- at the default level it stops resource exhaustion and
    orphans, not access, and even at its strongest level a command can still
    read every file the user can read and reach the network. An allowlist
    entry is still a decision to trust the head of a string.
    """
    cmd = ((args or {}).get("command") or "").strip()
    if not cmd:
        return ""
    if cmd[0] in "\"'":
        quote = cmd[0]
        end = cmd.find(quote, 1)
        head = cmd[1:end] if end > 0 else cmd[1:]
    else:
        # Handle backslash-escaped spaces before splitting, as they appear in
        # POSIX shell paths (e.g. /opt/my\ tool/bin/git).
        placeholder = "__HEARTH_ESCAPED_SPACE__"
        cmd_for_split = cmd.replace("\\ ", placeholder)
        head = cmd_for_split.split()[0]
        head = head.replace(placeholder, " ")
    head = head.replace("\\", "/").rsplit("/", 1)[-1]
    if head.lower().endswith((".exe", ".cmd", ".bat", ".com", ".ps1")):
        head = head.rsplit(".", 1)[0]
    return head


def decide(mode, tool, args=None, auto_allow=(), allowed_tools=None):
    """Return 'allow' | 'gate' | 'deny' for (mode, tool, args).

    auto_allow is an optional collection of command heads (for example
    {'git', 'ls'}) that run automatically even in auto mode. Empty by default.
    auto_allow only applies to run_command command heads; it does not affect other dangerous tools such as http_request.

    allowed_tools is the run's capability manifest: None means no manifest
    (every registered tool is a candidate, mode logic decides); a collection
    means the run is hard-capped to exactly those tools. A tool outside the
    manifest is denied in EVERY mode, including bypass. An empty manifest
    denies everything.
    """
    if allowed_tools is not None and tool not in allowed_tools:
        return "deny"  # the manifest is a hard cap, checked before mode logic
    if mode not in MODES:
        return "gate"  # invalid modes fail safe by gating
    risk = risk_of(tool)
    if mode == "bypass":
        return "allow"
    if mode == "plan":
        return "allow" if risk == "safe" else "deny"
    if mode == "edit":
        # Reads run freely; anything that changes state or leaves the box is
        # shown to the user first.
        return "allow" if risk == "safe" else "gate"
    # auto
    if risk in ("safe", "edit"):
        return "allow"
    if tool == "run_command" and _command_head(args) in set(auto_allow):
        return "allow"
    return "gate"


def _self_test():
    # bypass: everything allowed
    for t in ("read_file", "write_file", "run_command", "http_request", "mystery"):
        assert decide("bypass", t) == "allow", t
    # plan: only safe reads, everything else denied
    assert decide("plan", "read_file") == "allow"
    assert decide("plan", "list_files") == "allow"
    assert decide("plan", "write_file") == "deny"
    assert decide("plan", "run_command") == "deny"
    assert decide("plan", "http_request") == "deny"
    # auto: safe and edit allowed, dangerous gated
    assert decide("auto", "read_file") == "allow"
    assert decide("auto", "write_file") == "allow"
    assert decide("auto", "run_command") == "gate"
    assert decide("auto", "http_request") == "gate"
    # auto + allowlist: a whitelisted command head runs automatically
    assert decide("auto", "run_command", {"command": "git status"}, auto_allow={"git"}) == "allow"
    assert decide("auto", "run_command", {"command": "rm -rf /"}, auto_allow={"git"}) == "gate"
    # unknown tool fails closed (dangerous)
    assert risk_of("mystery") == "dangerous"
    assert decide("auto", "mystery") == "gate"
    # unknown mode -> gate (safest)
    assert decide("yolo", "read_file") == "gate"
    assert risk_of("web_search") == "dangerous", "web_search should be dangerous"
    assert risk_of("web_fetch") == "dangerous", "web_fetch should be dangerous"
    assert decide("auto", "web_search") == "gate"
    assert decide("bypass", "web_fetch") == "allow"
    assert decide("plan", "web_search") == "deny"
    assert risk_of("nix_check") == "safe"
    assert risk_of("write_self_config") == "edit"
    assert decide("plan", "write_self_config") == "deny"  # plan mode changes nothing
    assert decide("bypass", "write_self_config") == "allow"
    # current_generation is read-only introspection: safe in every mode.
    assert risk_of("current_generation") == "safe"
    assert decide("plan", "current_generation") == "allow"
    assert decide("auto", "current_generation") == "allow"
    for t in ("list_generations", "system_health", "read_self_config", "git_status", "git_diff"):
        assert risk_of(t) == "safe", t
        assert decide("plan", t) == "allow", t
    assert risk_of("remember") == "safe" and risk_of("recall") == "safe"
    assert risk_of("list_tree") == "safe" and risk_of("search_files") == "safe"
    assert risk_of("edit_file") == "edit"
    assert decide("plan", "search_files") == "allow" and decide("plan", "edit_file") == "deny"
    assert decide("auto", "edit_file") == "allow"
    assert risk_of("kb_search") == "safe" and risk_of("kb_add") == "edit"
    assert decide("plan", "kb_search") == "allow" and decide("plan", "kb_add") == "deny"
    assert risk_of("replace_in_files") == "edit" and risk_of("fetch_to_kb") == "dangerous"
    assert decide("auto", "replace_in_files") == "allow" and decide("auto", "fetch_to_kb") == "gate"
    assert decide("plan", "recall") == "allow"
    # capability manifest: a hard cap in every mode, including bypass
    assert decide("bypass", "run_command", allowed_tools={"read_file"}) == "deny"
    assert decide("auto", "read_file", allowed_tools={"read_file"}) == "allow"
    assert decide("auto", "write_file", allowed_tools={"read_file", "write_file"}) == "allow"
    assert decide("plan", "write_file", allowed_tools={"write_file"}) == "deny"  # mode still applies inside the manifest
    assert decide("bypass", "web_fetch", allowed_tools=set()) == "deny"  # empty manifest denies everything
    assert decide("bypass", "web_fetch", allowed_tools=None) == "allow"  # None = no manifest (back-compat)
    assert decide("auto", "run_command", {"command": "git status"}, auto_allow={"git"}, allowed_tools={"run_command"}) == "allow"
    # _command_head: quoted paths, escaped spaces, and bare names all extract
    # the executable name correctly for allowlist matching. Note that this does
    # not mitigate shell-metacharacter chaining: an approved head can still
    # carry an unexamined payload via &&, ;, |, &, backticks, or $().
    assert _command_head({"command": "/opt/my\\ tool/bin/git status"}) == "git"
    assert _command_head({"command": "/usr/bin/git status"}) == "git"
    assert _command_head({"command": r"C:\tools\rg.exe -n foo"}) == "rg"
    assert _command_head({"command": '"C:\\Program Files\\Git\\bin\\git.exe" status'}) == "git"
    assert _command_head({"command": "'/usr/bin/git' status"}) == "git"
    assert _command_head({"command": ""}) == ""
    assert decide("auto", "run_command",
                  {"command": '"C:\\Program Files\\Git\\bin\\git.exe" status'},
                  auto_allow={"git"}) == "allow"
    # 'edit' mode: reads run, writes are gated, dangerous stays gated. This is
    # the mode the desktop UI defaults to, so a weak local model cannot rewrite
    # a file without the user seeing a diff first.
    assert "edit" in MODES
    assert decide("edit", "read_file") == "allow"
    assert decide("edit", "list_tree") == "allow"
    assert decide("edit", "write_file") == "gate"
    assert decide("edit", "edit_file") == "gate"
    assert decide("edit", "replace_in_files") == "gate"
    assert decide("edit", "run_command") == "gate"
    assert decide("edit", "mystery") == "gate"
    assert decide("edit", "write_file", allowed_tools={"read_file"}) == "deny"

    # --- MCP tools ------------------------------------------------------
    # An MCP tool nobody registered is an unknown tool, and unknown is
    # dangerous. This is what makes the registration ordering safe.
    assert risk_of("mcp__roblox__execute_luau") == "dangerous"
    assert decide("plan", "mcp__roblox__execute_luau") == "deny"
    assert decide("auto", "mcp__roblox__execute_luau") == "gate"
    assert decide("edit", "mcp__roblox__execute_luau") == "gate"
    # auto_allow is a run_command allowlist and never reaches another tool, so
    # it cannot be used to wave an MCP tool through.
    assert decide("auto", "mcp__roblox__execute_luau",
                  {"command": "git status"}, auto_allow={"git"}) == "gate"
    # The manifest allows and denies MCP tools individually, in every mode,
    # bypass included: there is no mode that reaches a tool the manifest
    # left out.
    _mcp_manifest = {"read_file", "mcp__roblox__get_studio_state"}
    assert decide("auto", "mcp__roblox__get_studio_state",
                  allowed_tools=_mcp_manifest) == "gate"  # unregistered: still dangerous
    assert decide("auto", "mcp__roblox__execute_luau",
                  allowed_tools=_mcp_manifest) == "deny"
    assert decide("bypass", "mcp__roblox__execute_luau",
                  allowed_tools=_mcp_manifest) == "deny"
    # Once hearth_mcp registers a genuinely read-only tool, it reads like any
    # other read. Registered here directly rather than by importing hearth_mcp,
    # so this module keeps no dependency on it.
    RISK["mcp__roblox__get_studio_state"] = "safe"
    try:
        assert decide("plan", "mcp__roblox__get_studio_state") == "allow"
        assert decide("auto", "mcp__roblox__get_studio_state") == "allow"
        assert decide("auto", "mcp__roblox__get_studio_state",
                      allowed_tools=_mcp_manifest) == "allow"
        assert decide("plan", "mcp__roblox__get_studio_state",
                      allowed_tools={"read_file"}) == "deny"
    finally:
        RISK.pop("mcp__roblox__get_studio_state", None)

    print("hearth-permissions self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
