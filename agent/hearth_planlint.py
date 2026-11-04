#!/usr/bin/env python3
"""hearth planlint: static preflight for a proposed tool-call plan.

A local 7B model does not emit tool calls the way a frontier model does. It
emits them nearly right: the required argument whose name it half remembered,
the timeout passed as "120" instead of 120, the path it reached for outside
the workspace, the same read repeated until the context fills, the write to a
file it never read. Executing a plan one call at a time finds each of those
faults only after every earlier call has already run, so the cheap mistake at
step 6 is discovered on the far side of the destructive one at step 4.

`lint(plan, tools, workspace)` reads the whole plan first and reports what is
wrong with it before anything runs. It is a static check: nothing here opens a
file, resolves a path on disk, spawns a process or touches the network, and the
same plan always produces the same findings in the same order. That is the
point. A preflight that needs the filesystem to be in a particular state is
not a preflight.

Checks, with the severity each one carries:

  error    unknown tool name, a required argument missing, an argument of the
           wrong type, a path argument that is absolute or contains ".." while
           a workspace is set, the same tool-and-arguments repeated more than
           twice (a loop the model is stuck in), a step that is not a usable
           {"tool", "args"} object, and an empty plan.
  warning  an argument the tool does not declare, a write to a path the plan
           never read first, and a plan longer than MAX_PLAN_STEPS.

`fatal(findings)` is True when any finding is an error. Warnings are advisory
on purpose: overwriting a file the plan never read is usually a bug but is
exactly right when the file is new, and a long plan is a smell rather than a
fault. Severity is split this way so a caller can block on `fatal` and still
log the rest.

What this deliberately does NOT do:

  It does not check the filesystem. Whether a path exists, is writable, or is
  the file the model meant is not knowable here and is the executor's job.
  It does not run, simulate or reorder a plan, and it never edits one. A
  linter that silently rewrites the model's intent hides the model's real
  failure rate, which is the number worth watching.
  It does not reason about what a command does. "run_command" with
  "rm -rf /" is a well-formed call and passes; containment is hearth_contain
  and hearth_sandbox, not a string check here.
  It does not score, rank or judge the plan's approach. Only its mechanics.
  It does not warn about anything it cannot state plainly. A linter that
  complains about a legitimate plan gets switched off within a week, so a
  clean plan must return an empty list, and the self-test asserts that first.

Registry shapes accepted for `tools`, since callers have two in hand:

  the compact form this module documents,
      {"name": "read_file", "args": {"path": {"required": True,
                                              "type": "str"}}}
  the JSON-schema form hearth_tools.TOOLS already uses,
      {"name": "read_file", "parameters": {"type": "object", "properties":
          {"path": {"type": "string"}}, "required": ["path"]}}
  and a bare name, as in hearth_tools.WINDOWS_TOOLS, which establishes that
  the tool exists and leaves its arguments unchecked rather than guessed.

Plan steps are accepted as {"tool": ..., "args": ...} and as the
{"name": ..., "arguments": ...} shape hearth_loop parses off the wire, so a
plan can be linted exactly as the model produced it. Standard library only.
"""

import argparse
import json
import os
import re
import sys

ERROR = "error"
WARNING = "warning"

# A plan longer than this is nearly always the model having lost the thread
# rather than a genuinely long task. Advisory, not a hard stop.
MAX_PLAN_STEPS = 40

# Two identical calls can be legitimate (read, edit, read back). A third is
# the model going in circles.
MAX_REPEATS = 2

# Tools that change a file at their "path" argument, and the tools that count
# as having read it first. Deliberately narrow: only a real content read
# satisfies the dependency, and only a real content write demands it.
WRITE_TOOLS = ("write_file", "edit_file", "append_file")
READ_TOOLS = ("read_file",)

# Argument names whose value is a workspace path. Matched exactly or by
# suffix, so "path", "src_path" and "output_file" all count while "glob",
# "find" and "query" do not.
PATH_ARGS = ("path", "file", "filename", "dir", "directory", "dest",
             "destination", "source", "src", "target", "output", "cwd")
PATH_SUFFIXES = ("_path", "_file", "_dir", "_filename")

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")

_TYPE_WORDS = {
    "str": "a string",
    "string": "a string",
    "int": "an integer",
    "integer": "an integer",
    "bool": "a boolean",
    "boolean": "a boolean",
    "float": "a number",
    "number": "a number",
}


def _type_ok(value, declared):
    """True when value matches the declared type name. An unrecognised type
    name passes: this module will not invent a rule the registry did not
    state. bool is checked before int on purpose, because in Python True is
    an int and a model that sends true where a count belongs has made a
    mistake worth reporting."""
    name = (declared or "").strip().lower()
    if name in ("str", "string"):
        return isinstance(value, str)
    if name in ("int", "integer"):
        return isinstance(value, int) and not isinstance(value, bool)
    if name in ("bool", "boolean"):
        return isinstance(value, bool)
    if name in ("float", "number"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _type_word(declared):
    """The declared type as a readable noun phrase for a finding sentence."""
    return _TYPE_WORDS.get((declared or "").strip().lower(),
                           "of type {}".format(declared))


def _schema_from_parameters(params):
    """Flatten a JSON-schema parameters block into the compact arg form."""
    props = params.get("properties") or {}
    required = params.get("required") or []
    out = {}
    for arg, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        out[arg] = {"required": arg in required,
                    "type": spec.get("type", "")}
    return out


def _tool_table(tools):
    """Build {name: argspec-or-None} from any accepted registry shape. A
    value of None means the tool is known but its arguments are not declared,
    so argument checks are skipped for it rather than guessed at."""
    table = {}
    for entry in (tools or []):
        if isinstance(entry, str):
            table.setdefault(entry, None)
            continue
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        if isinstance(entry.get("args"), dict):
            spec = {}
            for arg, rule in entry["args"].items():
                rule = rule if isinstance(rule, dict) else {}
                spec[arg] = {"required": bool(rule.get("required")),
                             "type": rule.get("type", "")}
            table[name] = spec
        elif isinstance(entry.get("parameters"), dict):
            table[name] = _schema_from_parameters(entry["parameters"])
        else:
            table[name] = None
    return table


def _read_step(entry):
    """Normalise one plan step to (tool_name, args). Returns (None, reason)
    when the step is not usable at all, so a malformed step becomes a finding
    instead of an exception."""
    if not isinstance(entry, dict):
        return None, "this step is {} rather than an object".format(
            type(entry).__name__)
    name = entry.get("tool", entry.get("name"))
    if not isinstance(name, str) or not name.strip():
        return None, "this step names no tool"
    args = entry.get("args")
    if args is None:
        args = entry.get("arguments")
    if args is None:
        args = {}
    if isinstance(args, str):
        # Some wire formats hand the arguments over as a JSON string.
        try:
            args = json.loads(args)
        except ValueError:
            return None, "the arguments of this step are not valid JSON"
    if not isinstance(args, dict):
        return None, "the arguments of this step are not an object"
    return name.strip(), args


def _is_path_arg(arg):
    """True when an argument name denotes a workspace path."""
    low = arg.lower()
    return low in PATH_ARGS or low.endswith(PATH_SUFFIXES)


def _path_fault(value):
    """Describe how a path argument escapes the workspace, or return "" when
    it does not. Purely textual: no path on disk is consulted."""
    if not isinstance(value, str) or not value:
        return ""
    text = value.strip()
    if _DRIVE.match(text):
        return "is an absolute Windows path"
    if text.startswith("\\\\"):
        return "is a UNC network path"
    if text[:1] in ("/", "\\"):
        return "is an absolute path"
    parts = text.replace("\\", "/").split("/")
    if ".." in parts:
        return "climbs out of the workspace with '..'"
    if text.startswith("~"):
        return "points at a home directory outside the workspace"
    return ""


def _norm_path(value):
    """A comparable form of a path argument, for matching a write against an
    earlier read. Separators unified and case folded, because hearth's own
    target is Windows, where the two spellings are the same file."""
    text = str(value).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/").lower()


def _finding(severity, index, code, message):
    return {"severity": severity, "index": index, "code": code,
            "message": message}


def lint(plan, tools, workspace=""):
    """Check a proposed plan and return a list of findings.

    plan       list of {"tool": name, "args": {...}} steps (the
               {"name", "arguments"} spelling is accepted too).
    tools      registry, in any of the shapes the module docstring lists.
    workspace  when set, path arguments are required to stay inside it. The
               value itself is never resolved or touched; it only switches
               the path checks on.

    Findings are dicts of severity, index (the 0-based step, or -1 for a
    finding about the plan as a whole), a short stable code, and one plain
    sentence. Errors come before warnings, each group in plan order. A plan
    with nothing wrong with it returns []."""
    out = []
    table = _tool_table(tools)

    if not isinstance(plan, (list, tuple)):
        return [_finding(ERROR, -1, "bad_plan",
                         "The plan is not a list of steps.")]
    if not plan:
        return [_finding(ERROR, -1, "empty_plan",
                         "The plan is empty, so there is nothing to run.")]
    if len(plan) > MAX_PLAN_STEPS:
        out.append(_finding(
            WARNING, -1, "too_long",
            "The plan has {} steps, past the sensible cap of {}, which "
            "usually means the model lost the thread.".format(
                len(plan), MAX_PLAN_STEPS)))

    seen_counts = {}
    read_paths = set()

    for i, entry in enumerate(plan):
        name, args = _read_step(entry)
        if name is None:
            out.append(_finding(ERROR, i, "bad_step",
                                args[0].upper() + args[1:] + "."))
            continue

        known = name in table
        if not known:
            out.append(_finding(
                ERROR, i, "unknown_tool",
                "There is no tool called '{}' in the registry.".format(name)))

        spec = table.get(name)
        if spec is not None:
            for arg, rule in sorted(spec.items()):
                if rule.get("required") and arg not in args:
                    out.append(_finding(
                        ERROR, i, "missing_arg",
                        "Tool '{}' needs the argument '{}' and this call "
                        "leaves it out.".format(name, arg)))
            for arg in sorted(args):
                if arg not in spec:
                    out.append(_finding(
                        WARNING, i, "extra_arg",
                        "Tool '{}' does not take an argument called '{}', "
                        "so it will be ignored.".format(name, arg)))
                    continue
                declared = spec[arg].get("type", "")
                if not _type_ok(args[arg], declared):
                    out.append(_finding(
                        ERROR, i, "bad_type",
                        "Argument '{}' of '{}' should be {} but this call "
                        "passes {}.".format(
                            arg, name, _type_word(declared),
                            json.dumps(args[arg], default=str))))

        if workspace:
            for arg in sorted(args):
                if not _is_path_arg(arg):
                    continue
                fault = _path_fault(args[arg])
                if fault:
                    out.append(_finding(
                        ERROR, i, "path_escape",
                        "The '{}' argument of '{}' {}: {}.".format(
                            arg, name, fault,
                            json.dumps(args[arg], default=str))))

        # Loop detection. Reported once, on the step that first exceeds the
        # allowance, so a call repeated nine times is one finding and not six.
        try:
            key = (name, json.dumps(args, sort_keys=True, default=str))
        except (TypeError, ValueError):
            key = (name, repr(sorted(args.items())))
        seen_counts[key] = seen_counts.get(key, 0) + 1
        if seen_counts[key] == MAX_REPEATS + 1:
            out.append(_finding(
                ERROR, i, "loop",
                "'{}' is called with these exact arguments more than {} "
                "times, which is a loop rather than a plan.".format(
                    name, MAX_REPEATS)))

        # Write-before-read. Only a real content read of the same path
        # satisfies it, and the finding is a warning because writing a file
        # that does not exist yet is legitimate.
        path = args.get("path")
        if isinstance(path, str) and path:
            if name in READ_TOOLS:
                read_paths.add(_norm_path(path))
            elif name in WRITE_TOOLS and _norm_path(path) not in read_paths:
                out.append(_finding(
                    WARNING, i, "write_before_read",
                    "'{}' writes to {} which no earlier step in the plan "
                    "read.".format(name, json.dumps(path))))

    order = {ERROR: 0, WARNING: 1}
    return sorted(out, key=lambda f: (order.get(f["severity"], 2),
                                      f["index"]))


def fatal(findings):
    """True when any finding is an error, meaning the plan must not run."""
    return any(f.get("severity") == ERROR for f in (findings or []))


def format_findings(findings):
    """Render findings as lines grouped by severity. Returns a list of lines,
    empty when there is nothing to report."""
    lines = []
    for severity in (ERROR, WARNING):
        group = [f for f in findings if f.get("severity") == severity]
        if not group:
            continue
        lines.append("{}s ({}):".format(severity, len(group)))
        for f in group:
            where = "plan" if f.get("index", -1) < 0 else "step {}".format(
                f["index"])
            lines.append("  {}: {}".format(where, f.get("message", "")))
    return lines


def _live_tools():
    """The running hearth registry, for a CLI invocation that did not pass
    one. Imported lazily and best-effort: the import pulls in containment and
    sandbox modules, and the self-test must not depend on any of that."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import hearth_tools
        return list(hearth_tools.all_tools())
    except Exception:  # noqa: BLE001 - no registry is a CLI message, not a crash
        return []


def _load_json(path):
    """Read JSON from a file, or from stdin when path is missing or "-"."""
    if not path or path == "-":
        return json.loads(sys.stdin.read())
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _as_plan(obj):
    """Accept a bare list of steps, or an object with a "plan" key."""
    if isinstance(obj, dict):
        for key in ("plan", "steps", "calls", "tool_calls"):
            if isinstance(obj.get(key), list):
                return obj[key]
    return obj


def _self_test():
    tools = [
        {"name": "read_file",
         "args": {"path": {"required": True, "type": "str"}}},
        {"name": "write_file",
         "args": {"path": {"required": True, "type": "str"},
                  "content": {"required": True, "type": "str"}}},
        {"name": "edit_file",
         "args": {"path": {"required": True, "type": "str"},
                  "find": {"required": True, "type": "str"},
                  "replace": {"required": False, "type": "str"},
                  "all": {"required": False, "type": "bool"}}},
        {"name": "run_command",
         "args": {"command": {"required": True, "type": "str"},
                  "timeout": {"required": False, "type": "int"}}},
    ]

    def codes(findings):
        return [f["code"] for f in findings]

    # A legitimate plan must come back completely clean. This is the first
    # assertion because it is the one that keeps the linter usable.
    good = [
        {"tool": "read_file", "args": {"path": "src/app.py"}},
        {"tool": "edit_file", "args": {"path": "src/app.py",
                                       "find": "old", "replace": "new"}},
        {"tool": "run_command", "args": {"command": "pytest -q",
                                         "timeout": 300}},
        {"tool": "read_file", "args": {"path": "src/app.py"}},
    ]
    assert lint(good, tools, workspace="/w") == [], lint(good, tools, "/w")
    assert not fatal(lint(good, tools, workspace="/w"))
    assert format_findings([]) == []

    # The same plan with no workspace set is still clean, and linting is
    # deterministic.
    assert lint(good, tools) == []
    assert lint(good, tools, "/w") == lint(good, tools, "/w")

    # Empty and non-list plans.
    f = lint([], tools)
    assert codes(f) == ["empty_plan"], f
    assert f[0]["index"] == -1 and f[0]["severity"] == ERROR, f
    assert fatal(f)
    assert codes(lint("nope", tools)) == ["bad_plan"]

    # Unknown tool.
    f = lint([{"tool": "frobnicate", "args": {}}], tools)
    assert codes(f) == ["unknown_tool"], f
    assert "frobnicate" in f[0]["message"], f

    # Missing required argument. Both missing ones are reported, in a stable
    # alphabetical order, and the known-but-wrong call is still checked.
    f = lint([{"tool": "write_file", "args": {}}], tools)
    assert codes(f) == ["missing_arg", "missing_arg"], f
    assert "'content'" in f[0]["message"], f
    assert "'path'" in f[1]["message"], f

    # Wrong types, including the bool/int traps.
    f = lint([{"tool": "run_command",
               "args": {"command": "ls", "timeout": "120"}}], tools)
    assert codes(f) == ["bad_type"], f
    assert "an integer" in f[0]["message"], f
    f = lint([{"tool": "run_command",
               "args": {"command": "ls", "timeout": True}}], tools)
    assert codes(f) == ["bad_type"], f
    f = lint([{"tool": "read_file", "args": {"path": "a"}},
              {"tool": "edit_file", "args": {"path": "a", "find": "x",
                                             "all": 1}}], tools)
    assert codes(f) == ["bad_type"], f
    assert "a boolean" in f[0]["message"], f
    f = lint([{"tool": "read_file", "args": {"path": 7}}], tools)
    assert codes(f) == ["bad_type"], f
    # An integer where a number belongs is fine; so is an absent optional.
    assert lint([{"tool": "run_command",
                  "args": {"command": "ls"}}], tools) == []

    # An unexpected extra argument warns and does not block the plan.
    f = lint([{"tool": "read_file",
               "args": {"path": "a.py", "encoding": "utf-8"}}], tools)
    assert codes(f) == ["extra_arg"], f
    assert f[0]["severity"] == WARNING, f
    assert not fatal(f)

    # Path escapes, only once a workspace is set.
    escapes = ["/etc/passwd", "C:\\Windows\\win.ini", "../../secrets.txt",
               "src/../../etc/shadow", "\\\\server\\share\\x",
               "~/.ssh/id_rsa", "\\etc\\passwd"]
    for bad in escapes:
        plan = [{"tool": "read_file", "args": {"path": bad}}]
        f = lint(plan, tools, workspace="/w")
        assert codes(f) == ["path_escape"], (bad, f)
        assert lint(plan, tools) == [], bad
    # Ordinary relative paths, including ones that merely contain two dots,
    # are left alone.
    for ok in ["src/app.py", "./src/app.py", "a..b/c.txt", "notes/v1.2.md",
               "deep/nested/dir/file.txt"]:
        plan = [{"tool": "read_file", "args": {"path": ok}}]
        assert lint(plan, tools, workspace="/w") == [], ok

    # Loop detection: twice is allowed, three times is a finding, and it is
    # reported once no matter how long the loop runs.
    call = {"tool": "read_file", "args": {"path": "a.py"}}
    assert lint([dict(call), dict(call)], tools) == []
    f = lint([dict(call), dict(call), dict(call)], tools)
    assert codes(f) == ["loop"], f
    assert f[0]["index"] == 2, f
    f = lint([dict(call)] * 9, tools)
    assert codes(f) == ["loop"], f
    assert f[0]["index"] == 2, f
    # Differing arguments are not a loop, and key order does not matter.
    varied = [{"tool": "read_file", "args": {"path": "a.py"}},
              {"tool": "read_file", "args": {"path": "b.py"}},
              {"tool": "read_file", "args": {"path": "c.py"}}]
    assert lint(varied, tools) == []
    same = [{"tool": "edit_file", "args": {"path": "a", "find": "x"}},
            {"tool": "edit_file", "args": {"find": "x", "path": "a"}},
            {"tool": "edit_file", "args": {"path": "a", "find": "x"}}]
    assert "loop" in codes(lint(same, tools)), lint(same, tools)

    # Write before read.
    f = lint([{"tool": "write_file", "args": {"path": "out.txt",
                                              "content": "hi"}}], tools)
    assert codes(f) == ["write_before_read"], f
    assert f[0]["severity"] == WARNING, f
    assert lint([{"tool": "read_file", "args": {"path": "out.txt"}},
                 {"tool": "write_file", "args": {"path": "out.txt",
                                                 "content": "hi"}}],
                tools) == []
    # Reading a different file does not license the write.
    f = lint([{"tool": "read_file", "args": {"path": "other.txt"}},
              {"tool": "write_file", "args": {"path": "out.txt",
                                              "content": "hi"}}], tools)
    assert codes(f) == ["write_before_read"], f
    # Reading afterwards is too late.
    f = lint([{"tool": "write_file", "args": {"path": "out.txt",
                                              "content": "hi"}},
              {"tool": "read_file", "args": {"path": "out.txt"}}], tools)
    assert codes(f) == ["write_before_read"], f
    # Separator and case spellings of the same file match.
    assert lint([{"tool": "read_file", "args": {"path": "Src/App.py"}},
                 {"tool": "edit_file", "args": {"path": "src\\app.py",
                                                "find": "x"}}], tools) == []

    # Plan length cap: a long but otherwise valid plan warns about the plan
    # itself rather than any one step, and does not block.
    long_plan = [{"tool": "read_file", "args": {"path": "f{}.py".format(n)}}
                 for n in range(MAX_PLAN_STEPS + 1)]
    f = lint(long_plan, tools, workspace="/w")
    assert codes(f) == ["too_long"], f
    assert f[0]["index"] == -1 and f[0]["severity"] == WARNING, f
    assert not fatal(f)
    assert lint(long_plan[:MAX_PLAN_STEPS], tools) == []

    # Malformed steps become findings, never exceptions.
    f = lint(["read_file", 7, None, {}, {"tool": ""},
              {"tool": "read_file", "args": "not json"},
              {"tool": "read_file", "args": 5}], tools)
    assert codes(f) == ["bad_step"] * 7, f
    assert all(c[0].isupper() and c.endswith(".")
               for c in [x["message"] for x in f]), f

    # The wire shape hearth_loop parses, and arguments arriving as a JSON
    # string, lint the same as the documented shape.
    wire = [{"name": "read_file", "arguments": {"path": "a.py"}},
            {"name": "edit_file", "arguments": '{"path": "a.py", '
                                               '"find": "x"}'}]
    assert lint(wire, tools, workspace="/w") == []
    assert codes(lint([{"name": "read_file",
                        "arguments": {"path": "/etc/passwd"}}],
                      tools, workspace="/w")) == ["path_escape"]

    # The JSON-schema registry shape gives identical findings to the compact
    # one, so a caller can pass hearth_tools.TOOLS straight through.
    schema_tools = [
        {"name": "read_file", "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]}},
        {"name": "write_file", "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "content": {"type": "string"}},
            "required": ["path", "content"]}},
        {"name": "edit_file", "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "find": {"type": "string"},
                           "replace": {"type": "string"},
                           "all": {"type": "boolean"}},
            "required": ["path", "find"]}},
        {"name": "run_command", "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"},
                           "timeout": {"type": "integer"}},
            "required": ["command"]}},
    ]
    assert lint(good, schema_tools, workspace="/w") == []
    mixed = [{"tool": "write_file", "args": {"content": "x"}},
             {"tool": "run_command", "args": {"command": "ls",
                                              "timeout": "9"}}]
    assert lint(mixed, schema_tools) == lint(mixed, tools), (
        lint(mixed, schema_tools), lint(mixed, tools))

    # A registry of bare names establishes existence and nothing more: the
    # unknown name is still caught, declared-argument checks are skipped
    # rather than guessed, and path and loop checks still apply.
    names = ["read_file", "write_file", "run_command"]
    assert lint([{"tool": "read_file", "args": {"whatever": 1}}], names) == []
    assert codes(lint([{"tool": "edit_file", "args": {}}], names)) == [
        "unknown_tool"]
    assert codes(lint([{"tool": "read_file", "args": {"path": "/etc/x"}}],
                      names, workspace="/w")) == ["path_escape"]

    # An unrecognised declared type is not second-guessed.
    odd = [{"name": "odd", "args": {"blob": {"required": True,
                                             "type": "array"}}}]
    assert lint([{"tool": "odd", "args": {"blob": [1, 2]}}], odd) == []

    # Severity ordering: errors first, each group in plan order.
    messy = [{"tool": "read_file", "args": {"path": "a.py", "extra": 1}},
             {"tool": "write_file", "args": {"path": "b.py"}},
             {"tool": "nope", "args": {}}]
    f = lint(messy, tools)
    assert codes(f) == ["missing_arg", "unknown_tool", "extra_arg",
                        "write_before_read"], codes(f)
    assert [x["index"] for x in f] == [1, 2, 0, 1], f
    assert fatal(f)

    # Every finding carries the three documented fields and one sentence.
    for x in f:
        assert set(x) == {"severity", "index", "code", "message"}, x
        assert x["severity"] in (ERROR, WARNING), x
        assert isinstance(x["index"], int), x
        assert x["message"].endswith("."), x

    # Rendering groups by severity and labels a plan-level finding.
    lines = format_findings(f)
    assert lines[0] == "errors (2):", lines
    assert lines[3] == "warnings (2):", lines
    assert all(line.startswith("  step ") for line in lines[1:3] + lines[4:6])
    assert format_findings(lint([], tools))[1].startswith("  plan: "), (
        format_findings(lint([], tools)))

    # fatal() on odd input.
    assert fatal([]) is False
    assert fatal(None) is False
    assert fatal([{"severity": WARNING}]) is False
    assert fatal([{"severity": WARNING}, {"severity": ERROR}]) is True

    # An empty registry makes every tool unknown but still does not raise.
    assert codes(lint(good, [])) == ["unknown_tool"] * 4
    assert codes(lint(good, None)) == ["unknown_tool"] * 4

    print("hearth-planlint self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-planlint",
        description="Preflight a proposed tool-call plan before it runs.")
    p.add_argument("plan", nargs="?",
                   help="JSON plan file, or - for stdin (the default)")
    p.add_argument("--tools", help="JSON tool registry file; without it the "
                                   "live hearth registry is used")
    p.add_argument("--workspace", default="",
                   help="workspace root; enables the path checks")
    p.add_argument("--json", action="store_true",
                   help="print findings as JSON instead of lines")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()

    try:
        plan = _as_plan(_load_json(a.plan))
    except (OSError, ValueError) as exc:
        sys.stderr.write("cannot read plan: {}\n".format(exc))
        return 2
    if a.tools:
        try:
            tools = _as_plan(_load_json(a.tools))
        except (OSError, ValueError) as exc:
            sys.stderr.write("cannot read tools: {}\n".format(exc))
            return 2
    else:
        tools = _live_tools()
        if not tools:
            sys.stderr.write("no tool registry available; pass --tools\n")
            return 2

    findings = lint(plan, tools, workspace=a.workspace)
    if a.json:
        print(json.dumps(findings, indent=2))
    elif not findings:
        print("plan looks clean ({} step(s))".format(
            len(plan) if isinstance(plan, (list, tuple)) else 0))
    else:
        for line in format_findings(findings):
            print(line)
    return 1 if fatal(findings) else 0


if __name__ == "__main__":
    sys.exit(main())
