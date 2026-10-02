#!/usr/bin/env python3
r"""hearth desktop sidecar MCP manager: the app's way to add, edit, enable,
disable, remove and test the MCP servers in mcp.json, so nobody has to
hand-edit a JSON file to give the model a new tool.

The file is the one agent/hearth_mcp.py already reads
(%LOCALAPPDATA%\Hearth\mcp.json, or HEARTH_MCP_CONFIG), in the format it
already reads. Nothing here is a second source of truth: a hand edit and a
panel edit land in the same place, and hearth_mcp.check_server is the one
judge of whether an entry is runnable.

What this module adds on top of hearth_mcp, and why each piece exists:

  1. STRICTER INPUT THAN THE FILE ALLOWS. hearth_mcp checks types, because a
     hand-written file is its author's business. A value arriving over HTTP
     is not, so every field the panel can write is checked here before it
     reaches the file:
       - the server key must already be a valid tool-name part (what
         hearth_mcp.sanitize would leave alone), short, and free of "__",
         which is the separator split_tool_name relies on;
       - the command is ONE program: a bare name like `npx` or a full path.
         Never a command line. Nothing here splits a string into arguments
         or hands one to a shell, so `npx -y some-server` is refused with a
         message saying to put the arguments in their own rows. Relative
         paths (which depend on where Hearth happens to be running from),
         network and device paths, quotes, shell metacharacters, and
         unexpanded %VARIABLES% are refused too;
       - each argument and environment value is bounded and free of control
         characters; environment names are identifiers, and two names that
         differ only by case are refused, because Windows treats them as
         one variable.

  2. AN EXPLICIT ACKNOWLEDGEMENT. Saving an MCP server is choosing an
     executable Hearth will launch with the user's permissions. Over this
     transport that turns a bearer token into "choose what Hearth runs",
     the same class of exposure app.py's docstring gives for refusing
     bypass mode. So any save that adds a server, or changes what would
     run (command, arguments, environment, working directory), is refused
     with 400 and `needs_acknowledge` unless the body carries
     "acknowledge": "runs-program". The UI sends that only from a dialog
     that says in plain words that the program will run on this computer.
     A new server starts disabled unless that same dialog was ticked.

  3. SECRETS ARE NOT ECHOED. An environment value whose name looks like a
     credential (key, token, secret, pass, auth, cred, session, cookie), or
     whose value hearth_secrets.scan recognises, is listed masked: at most
     its first two characters and its length. Arguments are treated the
     same way (`--api-key=...`, the value after `--token`, or anything the
     scanner flags). To save without retyping, the UI sends a keep sentinel
     ({"keep": true} for an env value, {"keep": <index>} for an argument)
     and the stored value is substituted here. The mask itself is never
     written to the file and the full value never leaves this process.

  4. A CHANGE TAKES EFFECT, OR IS REFUSED. hearth_mcp builds its registry
     from the file once and keeps the servers running. Without the call to
     hearth_mcp.invalidate() below, disabling a server in the panel would
     leave the old process running and its tools callable until Hearth
     restarted: "disabled" would be a label, not a fact. Invalidating while
     a turn is running would kill a tool call mid-flight instead, so a
     change that touches an enabled server is refused with 409 while the
     session is busy. A change to a server that is disabled before and
     after cannot affect anything live, so it is allowed at any time.

  5. TEST NEVER TOUCHES THE LIVE REGISTRY. POST /mcp/test runs
     hearth_mcp.probe_server on a private client with short timeouts, on
     its own thread, and the result is read back from GET /mcp. A disabled
     server is refused rather than started. The work happens off the
     request thread because a page has six connections to its origin and
     already holds five open for streams: a request that sat for thirty
     seconds waiting on a slow server would stall every other request the
     page makes.

Writes are atomic (temp file in the same directory, then os.replace), UTF-8
without a byte-order mark, mode 0600 where modes exist, and preserve every
key the panel does not edit: unknown top-level keys, and per-server `risk`,
`cwd`, `timeout` and anything else. Reads tolerate a byte-order mark, since
Notepad writes one. Every read-modify-write holds one module-level lock. A
file that cannot be parsed is reported and never overwritten: it may hold
hours of someone's hand edits.

Standard library only.
"""

import json
import os
import re
import shutil
import sys
import threading
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))  # desktop/server -> desktop -> repo root
_AGENT_DIR = os.path.join(_REPO_ROOT, "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

import hearth_mcp  # noqa: E402
import hearth_paths  # noqa: E402
import hearth_secrets  # noqa: E402


# The exact string a save must carry when it adds a server or changes what
# would run. A fixed phrase rather than `true`, so it cannot be satisfied by
# a client that sets every boolean it sees.
ACKNOWLEDGE = "runs-program"

# The Test button's bounds. Shorter than hearth_mcp's own (30s handshake,
# 60s tools/list), because a person is watching a spinner, and a server that
# needs longer than this to say hello is worth hearing about anyway.
TEST_START_TIMEOUT = 15
TEST_LIST_TIMEOUT = 15
MAX_CONCURRENT_TESTS = 3

MAX_SERVERS = 64
MAX_KEY_LEN = 48
MAX_COMMAND_LEN = 1024
MAX_ARGS = 64
MAX_ARG_LEN = 4096
MAX_ENV = 64
MAX_ENV_KEY_LEN = 128
MAX_ENV_VALUE_LEN = 8192
MAX_FILE_BYTES = 4 * 1024 * 1024

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_BARE_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SECRET_NAME_RE = re.compile(r"(?i)key|token|secret|pass|auth|cred|session|cookie")
_FLAG_RE = re.compile(r"^--?([A-Za-z0-9][A-Za-z0-9_.-]*)(?:=(.*))?$", re.S)
# Characters that cannot appear in a Windows file name and only make sense in
# a command line: a command containing one is a command line in disguise.
_COMMAND_FORBIDDEN = set('"<>|*?')

# Programs whose arguments are themselves a command line. Not refused (on
# Windows `cmd /c npx ...` is the ordinary way to run an npm-installed
# server), but named in a warning so the confirmation says what it means.
_SHELLS = {"cmd", "powershell", "pwsh", "bash", "sh", "zsh", "wsl"}
# Environment variables that change which code a program loads. Not refused
# either; named, because the confirmation dialog is where that should be said.
_LOADER_ENV = {"NODE_OPTIONS", "NODE_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
               "DYLD_INSERT_LIBRARIES", "PYTHONSTARTUP", "PYTHONPATH",
               "PYTHONHOME", "COMSPEC", "PATH", "PATHEXT"}

BUSY_MESSAGE = ("a turn is running, and this change would stop a server it may be "
                "in the middle of using; try again when the turn has finished")

_config_lock = threading.Lock()
_tests_lock = threading.Lock()
_tests = {}   # raw key -> {"token", "state", "started_at", "fingerprint", "result"}


class AdminError(Exception):
    """A refusal with the HTTP status app.py should send. `extra` rides along
    in the JSON body, e.g. needs_acknowledge or the field that was wrong."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra

    def body(self):
        out = dict(self.extra)
        out["error"] = str(self)
        return out


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _text(value, field, limit, what):
    """`value` as a string that is safe to store, or AdminError(400)."""
    if not isinstance(value, str):
        raise AdminError(400, "{} must be text".format(what), field=field)
    if len(value) > limit:
        raise AdminError(400, "{} is longer than {} characters".format(what, limit), field=field)
    if _CONTROL_RE.search(value):
        raise AdminError(400, "{} contains a line break or control character".format(what),
                         field=field)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise AdminError(400, "{} is not valid text".format(what), field=field)
    return value


def validate_key(key):
    """A new server's key. It becomes the middle of every tool name the server
    contributes (mcp__<key>__<tool>), so it must already be one: anything
    hearth_mcp.sanitize would rewrite is refused rather than rewritten,
    because two keys that sanitise to the same name would silently shadow
    each other. "__" is refused because it is the separator."""
    if not isinstance(key, str) or not key:
        raise AdminError(400, "a server needs a name", field="key")
    if len(key) > MAX_KEY_LEN:
        raise AdminError(400, "the name is longer than {} characters".format(MAX_KEY_LEN),
                         field="key")
    if not _KEY_RE.match(key) or hearth_mcp.sanitize(key) != key:
        raise AdminError(400, "use letters, digits, '-' and '_' only, starting with a "
                              "letter or digit", field="key")
    if "__" in key:
        raise AdminError(400, "the name cannot contain '__', which separates the server "
                              "from the tool in a tool name", field="key")
    return key


def _lookup_key(key):
    """An existing server's key, which may be anything a hand-edited file
    holds. Only bounded, never reshaped: it is looked up, not stored."""
    if not isinstance(key, str) or not key or len(key) > 512:
        raise AdminError(400, "key is required", field="key")
    return key


def validate_command(command):
    """One program: a bare name found on PATH, or a full path. Never a
    command line, and never something that needs a shell to mean anything."""
    command = _text(command, "command", MAX_COMMAND_LEN, "the command")
    if not command:
        raise AdminError(400, "a server needs a command", field="command")
    if command != command.strip():
        raise AdminError(400, "remove the spaces around the command", field="command")
    if command.startswith(("\\\\", "//")):
        raise AdminError(400, "network and device paths are not accepted; copy the "
                              "program to this computer and give its full path",
                         field="command")
    if any(ch in _COMMAND_FORBIDDEN for ch in command):
        raise AdminError(400, "the command contains quotes or shell characters; give one "
                              "program, with no quotes, and put its arguments in Args",
                         field="command")
    if "%" in command or command.startswith(("~", "$")):
        raise AdminError(400, "environment variables and ~ are not expanded here; give "
                              "the full path", field="command")
    if _DRIVE_PATH_RE.match(command):
        return command
    if command.startswith("/") and not hearth_paths.is_windows():
        return command
    if _BARE_COMMAND_RE.match(command):
        return command
    first = command.split()[0] if command.split() else command
    if (" " in command and (_BARE_COMMAND_RE.match(first) or _DRIVE_PATH_RE.match(first))
            and not _DRIVE_PATH_RE.match(command)):
        raise AdminError(400, "the command is one program, not a command line: put "
                              "everything after {!r} in Args, one per row".format(first),
                         field="command")
    raise AdminError(400, "give a program name (like npx or node) or the program's "
                          "full path; a relative path depends on where Hearth is "
                          "running from", field="command")


def _resolve_args(value, old_args):
    """The argument list to store. Each item is a string, or {"keep": i} for
    "the stored argument at index i, unchanged", which is how a masked
    argument is saved without the page ever holding it."""
    if value is None:
        value = []
    if not isinstance(value, list):
        raise AdminError(400, "args must be a list, one argument per item", field="args")
    if len(value) > MAX_ARGS:
        raise AdminError(400, "more than {} arguments".format(MAX_ARGS), field="args")
    out = []
    for i, item in enumerate(value):
        if isinstance(item, dict):
            idx = item.get("keep")
            if (set(item) != {"keep"} or isinstance(idx, bool) or not isinstance(idx, int)
                    or not 0 <= idx < len(old_args)):
                raise AdminError(400, "argument {} refers to a saved value that does not "
                                      "exist; type it again".format(i + 1), field="args")
            item = old_args[idx]
        out.append(_text(item, "args", MAX_ARG_LEN, "argument {}".format(i + 1)))
    return out


def _resolve_env(value, old_env):
    """The environment to store, {name: value}. A value may be {"keep": true}
    for "the stored value of this same name, unchanged"."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise AdminError(400, "env must be an object of name to value", field="env")
    if len(value) > MAX_ENV:
        raise AdminError(400, "more than {} environment variables".format(MAX_ENV), field="env")
    out = {}
    seen = {}
    for name, item in value.items():
        if not isinstance(name, str) or not name or len(name) > MAX_ENV_KEY_LEN \
                or not _ENV_KEY_RE.match(name):
            raise AdminError(400, "{!r} is not a valid environment variable name: use "
                                  "letters, digits and '_', not starting with a "
                                  "digit".format(str(name)[:60]), field="env")
        folded = name.upper()
        if folded in seen:
            raise AdminError(400, "{} and {} are the same variable on Windows; keep "
                                  "one".format(seen[folded], name), field="env")
        seen[folded] = name
        if isinstance(item, dict):
            if item != {"keep": True} or not isinstance(old_env.get(name), str):
                raise AdminError(400, "there is no saved value of {} to keep; type it "
                                      "again".format(name), field="env")
            item = old_env[name]
        out[name] = _text(item, "env", MAX_ENV_VALUE_LEN, "the value of {}".format(name))
    return out


def warnings_for(command, args, env):
    """Plain-language notes for the confirmation dialog and the card. None of
    these refuse anything; each names a way the entry means more than it
    looks like it does."""
    out = []
    if not isinstance(command, str):
        return out
    base = re.split(r"[\\/]", command)[-1].lower()
    stem = base[:-4] if base.endswith(".exe") else base
    if stem in _SHELLS:
        out.append("{} is a command shell: its arguments are a command line, and "
                   "whatever they say is what runs.".format(base))
    if base.endswith((".bat", ".cmd")):
        out.append("{} is a batch script: Windows runs it through cmd.exe, which "
                   "reads its arguments as a command line.".format(base))
    for name in (env or {}):
        if str(name).upper() in _LOADER_ENV:
            out.append("{} changes which code the program loads.".format(name))
    if hearth_paths.is_windows() and _BARE_COMMAND_RE.match(command) and "." not in command:
        found = shutil.which(command)
        if found is None:
            out.append("{} was not found on PATH, so it will not start. Give its full "
                       "path instead.".format(command))
        elif found.lower().endswith((".cmd", ".bat")):
            out.append("On this computer {} is a batch script ({}), which Windows will "
                       "not start directly. Use cmd as the command, with /c and {} as "
                       "the first arguments.".format(command, os.path.basename(found),
                                                     command))
    return out


# --------------------------------------------------------------------------
# Masking
# --------------------------------------------------------------------------

def _scanner_flags(value):
    try:
        return hearth_secrets.scan(value)["count"] > 0
    except Exception:  # noqa: BLE001 - a scanner bug must fail toward masking
        return True


def is_secret_env(name, value):
    return bool(_SECRET_NAME_RE.search(str(name))) or _scanner_flags(str(value))


def secret_arg_indexes(args):
    """Which arguments to mask: `--api-key=...`, the value after `--token`,
    and anything hearth_secrets recognises on its own."""
    out = set()
    for i, arg in enumerate(args):
        m = _FLAG_RE.match(arg)
        if m and _SECRET_NAME_RE.search(m.group(1)):
            if m.group(2):
                out.add(i)
            elif i + 1 < len(args) and not args[i + 1].startswith("-"):
                out.add(i + 1)
        if _scanner_flags(arg):
            out.add(i)
    return out


def _mask(value):
    """What a masked value looks like on the wire: never the value. Two
    leading characters are shown only for a long value, where they identify
    the kind of token without giving any of its entropy away."""
    return {"masked": True, "hint": value[:2] if len(value) >= 16 else "",
            "length": len(value)}


def _env_view(env):
    out = []
    for name, value in env.items():
        row = {"name": name}
        if is_secret_env(name, value):
            row.update(_mask(value))
        else:
            row.update(masked=False, value=value)
        out.append(row)
    return out


def _args_view(args):
    secret = secret_arg_indexes(args)
    out = []
    for i, arg in enumerate(args):
        if i in secret:
            item = _mask(arg)
            item["index"] = i
            out.append(item)
        else:
            out.append(arg)
    return out


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------

def _read_document(path):
    """(document, problem). A missing file is an empty document. Anything that
    cannot be read as an object with a `servers` object is a problem string,
    and the caller must not write over it."""
    if not os.path.exists(path):
        return {"servers": {}}, None
    insecure = hearth_mcp._insecure_mode(path)
    if insecure:
        return None, "Hearth refuses to use the MCP config because {}".format(insecure)
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None, "{} is larger than {} bytes".format(path, MAX_FILE_BYTES)
        with open(path, "r", encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "{} could not be read: {}".format(path, exc)
    if not isinstance(doc, dict):
        return None, "{} is not a JSON object".format(path)
    if "servers" not in doc:
        doc["servers"] = {}
    if not isinstance(doc["servers"], dict):
        return None, "'servers' in {} is not an object".format(path)
    return doc, None


def _retry_replace(tmp, full, attempts=5, delay=0.05):
    """os.replace, retried briefly through the sharing violations Windows
    raises while an editor, OneDrive or antivirus has the file open. The same
    policy session_state._retry_replace applies, kept local because that one
    belongs to another module's private surface."""
    last = None
    for i in range(attempts):
        try:
            os.replace(tmp, full)
            return
        except PermissionError as exc:
            last = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32):
                raise
            last = exc
        if i < attempts - 1:
            time.sleep(delay * (i + 1))
    raise last


def _write_document(path, doc):
    """Atomic, UTF-8 without a byte-order mark, owner-only where modes exist.

    0600 rather than session_state's 0666-minus-umask: hearth_mcp refuses to
    read a group- or world-writable config, so a umask of 002 would otherwise
    make the panel write a file the loader then ignores. It also keeps the
    environment values, which may be credentials, readable by the owner only.
    """
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, ".hearth-tmp-mcp-{}".format(uuid.uuid4().hex))
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _retry_replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_for_write():
    path = hearth_mcp.config_path()
    doc, problem = _read_document(path)
    if problem:
        raise AdminError(409, problem + ". Fix it by hand or move it aside: Hearth will "
                                        "not overwrite a file it cannot read.")
    return path, doc


def _find(servers, key):
    if key not in servers:
        raise AdminError(404, "no MCP server called {!r}".format(key), field="key")
    return servers[key]


def _runs(entry):
    """What would actually be launched. Two entries with the same value here
    run the same program the same way."""
    if entry is None:
        return None
    return (entry["command"], list(entry["args"]), dict(entry["env"]), entry["cwd"])


def _is_live(entry):
    return entry is not None and entry["enabled"]


# --------------------------------------------------------------------------
# Operations (the routes)
# --------------------------------------------------------------------------

def list_servers():
    """GET /mcp. Never starts anything: status comes from hearth_mcp.status(),
    which only reports on the registry a turn has already built."""
    path = hearth_mcp.config_path()
    with _config_lock:
        doc, problem = _read_document(path)
    live = hearth_mcp.status()
    servers = []
    names = set()
    for key, raw in ((doc or {}).get("servers") or {}).items():
        servers.append(_view(key, raw, live, names))
    with _tests_lock:
        running = sum(1 for t in _tests.values() if t["state"] == "running")
    return {
        "config_path": path,
        "exists": os.path.exists(path),
        "problem": problem,
        "servers": servers,
        "registry": {"loaded": live["loaded"], "busy": live["busy"]},
        "tests_running": running,
        "acknowledge": ACKNOWLEDGE,
        "limits": {"args": MAX_ARGS, "env": MAX_ENV, "key": MAX_KEY_LEN,
                   "test_seconds": TEST_START_TIMEOUT + TEST_LIST_TIMEOUT},
    }


def _view(key, raw, live, names):
    entry, why = hearth_mcp.check_server(key, raw)
    name = hearth_mcp.sanitize(key)
    if entry is not None and name in names:
        entry, why = None, ("another server already uses the name {}; this one is "
                            "ignored".format(name))
    names.add(name)
    view = {"key": key, "name": name, "prefix": hearth_mcp.PREFIX + name + "__",
            "valid": entry is not None, "problem": why, "error": None,
            "tool_count": None, "tools": None, "guarded": None, "test": None}
    if entry is None:
        raw = raw if isinstance(raw, dict) else {}
        command = raw.get("command")
        view.update(command=command if isinstance(command, str) else None,
                    args=[], env=[], enabled=raw.get("enabled", True) is not False,
                    cwd=None, timeout=None, risk_overrides=0, warnings=[],
                    status="invalid")
        return view
    view.update(command=entry["command"], args=_args_view(entry["args"]),
                env=_env_view(entry["env"]), enabled=entry["enabled"],
                cwd=entry["cwd"], timeout=entry["timeout"],
                risk_overrides=len(entry["risk"]),
                warnings=warnings_for(entry["command"], entry["args"], entry["env"]))
    if not entry["enabled"]:
        view["status"] = "disabled"
    else:
        srv = live["servers"].get(name)
        if srv is not None:
            view.update(status=srv["state"], error=srv["error"], guarded=srv["guarded"],
                        tool_count=srv["tool_count"], tools=srv["tools"])
        elif live["busy"] and not live["loaded"]:
            view["status"] = "starting"
        else:
            view["status"] = "idle"
    view["test"] = _test_view(key, _runs(entry))
    return view


def save(body, busy=None):
    """POST /mcp/save: add a server ({"create": true}) or edit one.

    Fields the panel edits: command, args, env, enabled. Everything else in
    the stored entry is preserved. See the module docstring for the
    acknowledgement and the busy refusal."""
    if not isinstance(body, dict):
        raise AdminError(400, "invalid_json")
    create = body.get("create") is True
    key = validate_key(body.get("key")) if create else _lookup_key(body.get("key"))
    command = validate_command(body.get("command"))
    enabled = body.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise AdminError(400, "enabled must be true or false", field="enabled")
    with _config_lock:
        path, doc = _load_for_write()
        servers = doc["servers"]
        if create:
            if key in servers or any(hearth_mcp.sanitize(k) == key for k in servers):
                raise AdminError(409, "a server called {} already exists".format(key),
                                 field="key")
            if len(servers) >= MAX_SERVERS:
                raise AdminError(400, "there are already {} servers".format(MAX_SERVERS))
            old_raw = None
        else:
            old_raw = _find(servers, key)
        old = old_raw if isinstance(old_raw, dict) else {}
        old_args = old.get("args") if isinstance(old.get("args"), list) else []
        old_env = old.get("env") if isinstance(old.get("env"), dict) else {}
        args = _resolve_args(body.get("args"), old_args)
        env = _resolve_env(body.get("env"), old_env)

        new_raw = dict(old)
        new_raw["command"] = command
        new_raw["args"] = args
        new_raw["env"] = env
        if create:
            # Off unless the confirmation dialog said otherwise: adding a
            # server and starting it are two decisions, and only the second
            # one launches anything.
            new_raw["enabled"] = enabled is True
        elif enabled is not None:
            new_raw["enabled"] = enabled
        new_entry, why = hearth_mcp.check_server(key, new_raw)
        if new_entry is None:
            raise AdminError(400, "the saved entry would still be ignored: {}. Fix that "
                                  "part of mcp.json by hand.".format(why))
        old_entry = hearth_mcp.check_server(key, old_raw)[0] if old_raw is not None else None
        warnings = warnings_for(command, args, env)

        if old_raw is not None and new_raw == old_raw:
            return {"changed": False, "restarted": False, "warnings": warnings}
        if _runs(new_entry) != _runs(old_entry) and body.get("acknowledge") != ACKNOWLEDGE:
            raise AdminError(
                400, "saving this lets {} run on this computer; it needs an explicit "
                     "acknowledgement".format(command),
                needs_acknowledge=True, acknowledge=ACKNOWLEDGE, create=create,
                command=command, args=_args_view(args), env_names=list(env),
                warnings=warnings)
        live = _is_live(old_entry) or _is_live(new_entry)
        if live and busy is not None and busy():
            raise AdminError(409, BUSY_MESSAGE)
        servers[key] = new_raw
        _write_document(path, doc)
        _forget_test(key)
    if live:
        hearth_mcp.invalidate()
    return {"changed": True, "restarted": live, "warnings": warnings}


def toggle(body, busy=None):
    """POST /mcp/toggle {key, enabled}. Enabling never starts the server here:
    like every other server, it starts with the next turn that needs tools."""
    if not isinstance(body, dict):
        raise AdminError(400, "invalid_json")
    key = _lookup_key(body.get("key"))
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise AdminError(400, "enabled must be true or false", field="enabled")
    with _config_lock:
        path, doc = _load_for_write()
        raw = _find(doc["servers"], key)
        entry, why = hearth_mcp.check_server(key, raw)
        if entry is None:
            raise AdminError(409, "this entry is ignored ({}); edit it first".format(why))
        if entry["enabled"] == enabled:
            return {"changed": False, "restarted": False}
        if busy is not None and busy():
            raise AdminError(409, BUSY_MESSAGE)
        raw["enabled"] = enabled
        _write_document(path, doc)
        if not enabled:
            _forget_test(key)
    hearth_mcp.invalidate()
    return {"changed": True, "restarted": True}


def remove(body, busy=None):
    """POST /mcp/remove {key}."""
    if not isinstance(body, dict):
        raise AdminError(400, "invalid_json")
    key = _lookup_key(body.get("key"))
    with _config_lock:
        path, doc = _load_for_write()
        raw = _find(doc["servers"], key)
        live = _is_live(hearth_mcp.check_server(key, raw)[0])
        if live and busy is not None and busy():
            raise AdminError(409, BUSY_MESSAGE)
        del doc["servers"][key]
        _write_document(path, doc)
        _forget_test(key)
    if live:
        hearth_mcp.invalidate()
    return {"removed": True, "restarted": live}


# --------------------------------------------------------------------------
# Test
# --------------------------------------------------------------------------

def _forget_test(key):
    with _tests_lock:
        _tests.pop(key, None)


def _test_view(key, runs):
    """The last test of `key`, if it tested what the entry runs NOW. A result
    for a command that has since been edited answers a question nobody is
    asking any more, so it is not shown."""
    with _tests_lock:
        t = _tests.get(key)
        if t is None or t["fingerprint"] != runs:
            return None
        out = {"state": t["state"], "started_at": t["started_at"]}
        if t["result"] is not None:
            out.update(t["result"])
        return out


def start_test(body):
    """POST /mcp/test {key}. Returns at once; the result arrives on GET /mcp."""
    if not isinstance(body, dict):
        raise AdminError(400, "invalid_json")
    key = _lookup_key(body.get("key"))
    with _config_lock:
        doc, problem = _read_document(hearth_mcp.config_path())
        if problem:
            raise AdminError(409, problem)
        raw = _find(doc["servers"], key)
    entry, why = hearth_mcp.check_server(key, raw)
    if entry is None:
        raise AdminError(400, "this entry is ignored: {}".format(why))
    if not entry["enabled"]:
        # A disabled server is never launched, and a test launches it. Enabling
        # is the deliberate act; testing should not be a way around it.
        raise AdminError(409, "this server is disabled; enable it before testing it")
    runs = _runs(entry)
    with _tests_lock:
        current = _tests.get(key)
        if current is not None and current["state"] == "running" \
                and current["fingerprint"] == runs:
            return {"test": {"state": "running", "started_at": current["started_at"]}}
        if sum(1 for t in _tests.values() if t["state"] == "running") >= MAX_CONCURRENT_TESTS:
            raise AdminError(429, "{} tests are already running; wait for one to "
                                  "finish".format(MAX_CONCURRENT_TESTS))
        token = uuid.uuid4().hex
        started = time.time()
        _tests[key] = {"token": token, "state": "running", "started_at": started,
                       "fingerprint": runs, "result": None}
    threading.Thread(target=_run_test, args=(key, entry, token), daemon=True,
                     name="mcp-test-{}".format(entry["key"])).start()
    return {"test": {"state": "running", "started_at": started}}


def _run_test(key, entry, token):
    try:
        report = hearth_mcp.probe_server(entry, start_timeout=TEST_START_TIMEOUT,
                                         list_timeout=TEST_LIST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - a probe bug must end the spinner
        report = {"ok": False, "error": "the test itself failed: {}".format(exc)}
    with _tests_lock:
        t = _tests.get(key)
        if t is None or t["token"] != token:
            return   # the server was edited or removed meanwhile
        t["state"] = "ok" if report.get("ok") else "failed"
        t["result"] = dict(report, finished_at=time.time())


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _self_test():
    import tempfile

    scratch = tempfile.mkdtemp(prefix="hearth-mcp-admin-")
    saved_env = {k: os.environ.get(k) for k in ("HEARTH_MCP_CONFIG", "HEARTH_DATA_DIR")}
    cfg = os.path.join(scratch, "mcp.json")
    os.environ["HEARTH_MCP_CONFIG"] = cfg
    os.environ["HEARTH_DATA_DIR"] = os.path.join(scratch, "data")
    saved_risk = dict(hearth_mcp.permissions.RISK)
    global TEST_START_TIMEOUT, TEST_LIST_TIMEOUT
    saved_timeouts = (TEST_START_TIMEOUT, TEST_LIST_TIMEOUT)

    def refused(fn, *a, status=400, needle=None, **kw):
        try:
            fn(*a, **kw)
        except AdminError as exc:
            assert exc.status == status, (exc.status, str(exc), a)
            if needle:
                assert needle in str(exc), (needle, str(exc))
            return exc
        raise AssertionError("expected a {} refusal for {!r}".format(status, a))

    def file_text():
        with open(cfg, "rb") as fh:
            return fh.read().decode("utf-8")

    def wait_test(key, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = [s for s in list_servers()["servers"] if s["key"] == key][0]
            if view["test"] and view["test"]["state"] != "running":
                return view["test"]
            time.sleep(0.1)
        raise AssertionError("the test of {} never finished".format(key))

    try:
        server_py = os.path.join(scratch, "fake_server.py")
        with open(server_py, "w", encoding="utf-8") as fh:
            fh.write(hearth_mcp._FAKE_SERVER)
        exe = sys.executable
        ack = {"acknowledge": ACKNOWLEDGE}

        # === validation: what the panel may write ===========================
        for good in ("roblox", "fs-1", "A_b", "x" * MAX_KEY_LEN):
            assert validate_key(good) == good
        for bad in ("", "a b", "a__b", "x" * (MAX_KEY_LEN + 1), "-x", "_x", "a.b",
                    "caf\u00e9", 5, None):
            refused(validate_key, bad)

        for good in ("npx", "node.exe", "uvx", "python3.12", exe):
            assert validate_command(good) == good, good
        cases = {
            "npx -y @scope/server": "Args",
            "": "needs a command",
            " npx": "spaces around",
            "a\nb": "control character",
            "a\x00b": "control character",
            '"C:\\Program Files\\x.exe"': "quotes",
            "x|y": "quotes or shell",
            "..\\x.exe": "full path",
            "./x": "full path",
            "bin/x": "full path",
            "\\\\server\\share\\x.exe": "network",
            "//server/share/x": "network",
            "%APPDATA%\\x.exe": "not expanded",
            "~/x": "not expanded",
            "x" * (MAX_COMMAND_LEN + 1): "longer than",
        }
        for bad, needle in cases.items():
            refused(validate_command, bad, needle=needle)
        # a full path may contain spaces: that is a path, not a command line
        assert validate_command("C:\\Program Files\\nodejs\\node.exe")

        assert _resolve_args(["-y", "pkg"], []) == ["-y", "pkg"]
        assert _resolve_args(None, []) == []
        refused(_resolve_args, "-y pkg", [], needle="list")
        refused(_resolve_args, [1], [], needle="text")
        refused(_resolve_args, ["a\nb"], [])
        refused(_resolve_args, ["x"] * (MAX_ARGS + 1), [])
        refused(_resolve_args, ["x" * (MAX_ARG_LEN + 1)], [])
        refused(_resolve_args, [{"keep": 3}], ["a"], needle="does not exist")
        refused(_resolve_args, [{"keep": True}], ["a"])
        assert _resolve_args([{"keep": 0}, "b"], ["kept"]) == ["kept", "b"]

        assert _resolve_env({"A": "1", "_B2": ""}, {}) == {"A": "1", "_B2": ""}
        for bad_name in ("1A", "A-B", "A=B", "", "A B"):
            refused(_resolve_env, {bad_name: "x"}, {})
        refused(_resolve_env, {"A": 3}, {})
        refused(_resolve_env, {"A": "x\ny"}, {})
        refused(_resolve_env, {"Path": "a", "PATH": "b"}, {}, needle="same variable")
        refused(_resolve_env, {"A": {"keep": True}}, {}, needle="no saved value")
        refused(_resolve_env, ["A=1"], {})
        assert _resolve_env({"A": {"keep": True}}, {"A": "old"}) == {"A": "old"}

        w = warnings_for("cmd", ["/c", "npx"], {"NODE_OPTIONS": "--x"})
        assert any("command shell" in x for x in w) and any("NODE_OPTIONS" in x for x in w), w
        assert any("batch script" in x for x in warnings_for("C:\\x\\run.cmd", [], {}))
        assert warnings_for(exe, [], {"LOG_LEVEL": "debug"}) == []

        # === masking ======================================================
        real_token = "ghp_" + "pLIix6MEOLeMa61EqJomTEI1JEzO3joOj37H"
        aws = "AKIA" + "PLIIX6MEOLEMA61E"
        assert is_secret_env("GITHUB_TOKEN", "x") and is_secret_env("API_KEY", "x")
        assert is_secret_env("ANYTHING", real_token), "the scanner must catch a value too"
        assert not is_secret_env("LOG_LEVEL", "debug")
        assert secret_arg_indexes(["--api-key=abc", "--token", "abc", "--verbose", "x",
                                   aws]) == {0, 2, 5}
        masked = _mask(real_token)
        assert masked == {"masked": True, "hint": "gh", "length": len(real_token)}
        assert _mask("short")["hint"] == "", "a short secret must not show any of itself"

        # === an existing hand-written file: BOM, unknown keys, extra fields ==
        with open(cfg, "wb") as fh:
            fh.write(b"\xef\xbb\xbf" + json.dumps({
                "note": "kept by hand",
                "servers": {
                    "hand": {"command": exe, "args": [server_py, "ok", "--token", real_token],
                             "env": {"GITHUB_TOKEN": real_token, "LOG_LEVEL": "debug",
                                     "PLAIN": aws},
                             "enabled": False, "cwd": scratch, "timeout": 7,
                             "risk": {"peek": "dangerous"}, "custom": [1, 2]},
                    "Broken Entry": {"args": "not a list"},
                }}).encode("utf-8"))
        listing = list_servers()
        assert listing["problem"] is None and listing["exists"] is True, listing
        out = json.dumps(listing)
        assert real_token not in out and aws not in out, \
            "a secret must never be returned in full"
        hand = [s for s in listing["servers"] if s["key"] == "hand"][0]
        assert hand["status"] == "disabled" and hand["valid"] is True, hand
        env_rows = {r["name"]: r for r in hand["env"]}
        assert env_rows["GITHUB_TOKEN"]["masked"] and env_rows["PLAIN"]["masked"]
        assert env_rows["LOG_LEVEL"] == {"name": "LOG_LEVEL", "masked": False,
                                         "value": "debug"}
        assert hand["args"][:3] == [server_py, "ok", "--token"]
        assert hand["args"][3]["masked"] is True and hand["args"][3]["index"] == 3
        assert hand["cwd"] == scratch and hand["timeout"] == 7 and hand["risk_overrides"] == 1
        broken = [s for s in listing["servers"] if s["key"] == "Broken Entry"][0]
        assert broken["valid"] is False and "command" in broken["problem"], broken
        assert broken["status"] == "invalid" and broken["name"] == "Broken_Entry"

        # an unchanged save, sending keep sentinels for every masked value,
        # changes nothing, needs no acknowledgement, and writes nothing
        before = file_text()
        res = save({"key": "hand", "command": exe,
                    "args": [server_py, "ok", "--token", {"keep": 3}],
                    "env": {"GITHUB_TOKEN": {"keep": True}, "LOG_LEVEL": "debug",
                            "PLAIN": {"keep": True}}})
        assert res["changed"] is False, res
        assert file_text() == before, "a no-op save must not rewrite the file"

        # an edit of what runs needs the acknowledgement, and without it the
        # file is untouched
        edit = {"key": "hand", "command": exe,
                "args": [server_py, "ok", "--token", {"keep": 3}],
                "env": {"GITHUB_TOKEN": {"keep": True}, "LOG_LEVEL": "info",
                        "PLAIN": {"keep": True}}}
        exc = refused(save, edit, needle="acknowledgement")
        assert exc.extra["needs_acknowledge"] is True and exc.extra["command"] == exe
        assert exc.extra["env_names"] == ["GITHUB_TOKEN", "LOG_LEVEL", "PLAIN"],             "the dialog names every variable it will set, and only names them"
        assert real_token not in json.dumps(exc.body()), "the refusal must not echo a secret"
        assert file_text() == before
        res = save(dict(edit, **ack))
        assert res == {"changed": True, "restarted": False, "warnings": []}, res
        text = file_text()
        assert not text.startswith("\ufeff"), "the panel must write UTF-8 without a BOM"
        doc = json.loads(text)
        assert doc["note"] == "kept by hand", "unknown top-level keys must survive"
        h = doc["servers"]["hand"]
        assert h["env"] == {"GITHUB_TOKEN": real_token, "LOG_LEVEL": "info", "PLAIN": aws}, \
            "keep sentinels must restore the stored values"
        assert h["args"][3] == real_token
        assert (h["cwd"], h["timeout"], h["risk"], h["custom"]) == \
            (scratch, 7, {"peek": "dangerous"}, [1, 2]), "fields the panel does not edit survive"
        assert h["enabled"] is False
        assert "Broken Entry" in doc["servers"], "an entry the panel cannot read is kept"
        assert '"masked"' not in text and '"keep"' not in text, \
            "a mask or a sentinel must never be written to the file"
        assert not [n for n in os.listdir(scratch) if n.startswith(".hearth-tmp")], \
            "the atomic write must not leave its temp file behind"
        refused(save, dict(edit, env={"GITHUB_TOKEN": {"keep": True},
                                      "NEW_KEY": {"keep": True}}, **ack),
                needle="no saved value")

        # === adding a server ===============================================
        new = {"create": True, "key": "fake", "command": exe, "args": [server_py, "ok"],
               "env": {}, "enabled": True}
        exc = refused(save, new, needle="acknowledgement")
        assert exc.extra["create"] is True
        assert "fake" not in json.loads(file_text())["servers"]
        refused(save, dict(new, key="hand", **ack), status=409, needle="already exists")
        refused(save, dict(new, key="Broken_Entry", **ack), status=409,
                needle="already exists")
        refused(save, dict(new, command="npx -y x", **ack), needle="Args")
        refused(save, dict(new, enabled="yes", **ack), needle="true or false")
        res = save(dict(new, enabled=False, **ack))
        assert res["changed"] is True and res["restarted"] is False, res
        assert json.loads(file_text())["servers"]["fake"]["enabled"] is False
        refused(save, {"key": "ghost", "command": exe}, status=404)

        # === Test ==========================================================
        refused(start_test, {"key": "fake"}, status=409, needle="disabled")
        refused(start_test, {"key": "ghost"}, status=404)
        refused(start_test, {"key": "Broken Entry"}, needle="ignored")

        # === toggle, and the registry really changing ======================
        refused(toggle, {"key": "fake", "enabled": "on"}, needle="true or false")
        refused(toggle, {"key": "ghost", "enabled": True}, status=404)
        refused(toggle, {"key": "Broken Entry", "enabled": True}, status=409)
        # enabling is refused while a turn is busy, and writes nothing
        before = file_text()
        refused(toggle, {"key": "fake", "enabled": True}, busy=lambda: True, status=409)
        assert file_text() == before
        assert toggle({"key": "fake", "enabled": True}, busy=lambda: False)["changed"] is True
        assert toggle({"key": "fake", "enabled": True})["changed"] is False

        # a turn would now start it: prove that, then prove disabling stops it
        names = sorted(d["name"] for d in hearth_mcp.descriptors())
        assert names == ["mcp__fake__peek", "mcp__fake__plain", "mcp__fake__poke",
                         "mcp__fake__surf"], names
        fake = [s for s in list_servers()["servers"] if s["key"] == "fake"][0]
        assert fake["status"] == "running" and fake["tool_count"] == 4, fake
        assert {t["name"]: t["risk"] for t in fake["tools"]}["peek"] == "safe", fake
        live_client = hearth_mcp.registry().clients["fake"]
        assert live_client.alive

        # editing an enabled server while busy is refused too
        refused(save, dict(new, create=False, args=[server_py, "notify"], **ack),
                busy=lambda: True, status=409)
        # ... but adding a DISABLED server touches nothing live, so it is fine
        save({"create": True, "key": "later", "command": exe, "args": [server_py, "ok"],
              "acknowledge": ACKNOWLEDGE}, busy=lambda: True)
        assert live_client.alive, "adding a disabled server must not restart anything"

        assert toggle({"key": "fake", "enabled": False})["restarted"] is True
        assert not live_client.alive, "disabling must stop the running server"
        assert hearth_mcp.status()["servers"] == {}
        assert not [n for n in hearth_mcp.permissions.RISK if n.startswith("mcp__")], \
            "a disabled server's tools must be forgotten by permissions"
        assert hearth_mcp.descriptors() == [], \
            "a server disabled from the panel must never be launched again"
        assert hearth_mcp.status()["servers"] == {}

        # === Test, for real, against the fake server =======================
        toggle({"key": "fake", "enabled": True})
        assert start_test({"key": "fake"})["test"]["state"] == "running"
        again = start_test({"key": "fake"})
        assert again["test"]["state"] == "running", "a second press must not start a second probe"
        result = wait_test("fake")
        assert result["state"] == "ok" and result["ok"] is True, result
        assert result["tool_count"] == 4
        risks = {t["name"]: t["risk"] for t in result["tools"]}
        assert risks == {"peek": "safe", "poke": "dangerous", "surf": "dangerous",
                         "plain": "dangerous"}, risks
        assert hearth_mcp.status()["servers"] == {}, \
            "a test must not start or swap the live registry"
        # editing what the server runs drops the stale result
        save(dict(new, create=False, args=[server_py, "notify"], **ack))
        fake = [s for s in list_servers()["servers"] if s["key"] == "fake"][0]
        assert fake["test"] is None, "a result for an edited command must not be shown"

        # a server that never answers fails inside the Test bound
        TEST_START_TIMEOUT, TEST_LIST_TIMEOUT = 2, 2
        save({"create": True, "key": "mute", "command": exe, "args": [server_py, "silent"],
              "enabled": True, "acknowledge": ACKNOWLEDGE})
        t0 = time.monotonic()
        start_test({"key": "mute"})
        result = wait_test("mute")
        assert result["state"] == "failed" and "did not answer" in result["error"], result
        assert time.monotonic() - t0 < 30, "a silent server overran the Test bound"

        # === remove ========================================================
        refused(remove, {"key": "ghost"}, status=404)
        refused(remove, {"key": "fake"}, busy=lambda: True, status=409)
        assert remove({"key": "later"}, busy=lambda: True)["restarted"] is False, \
            "removing a disabled server needs no idle session"
        assert remove({"key": "fake"})["removed"] is True
        assert remove({"key": "Broken Entry"})["removed"] is True
        doc = json.loads(file_text())
        assert sorted(doc["servers"]) == ["hand", "mute"], doc["servers"]
        assert doc["note"] == "kept by hand"

        # === a file Hearth cannot read is reported, never overwritten ======
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write('{"servers": {"x": ')
        listing = list_servers()
        assert listing["problem"] and listing["servers"] == [], listing
        refused(save, dict(new, **ack), status=409, needle="will not overwrite")
        refused(toggle, {"key": "x", "enabled": True}, status=409)
        refused(remove, {"key": "x"}, status=409)
        assert file_text() == '{"servers": {"x": ', "a broken file must be left alone"

        if not hearth_paths.is_windows():
            os.remove(cfg)
            save(dict(new, **ack))
            assert os.stat(cfg).st_mode & 0o777 == 0o600, oct(os.stat(cfg).st_mode)
            os.chmod(cfg, 0o666)
            assert "refuses" in list_servers()["problem"]
            refused(save, dict(new, key="other", **ack), status=409)

        # a missing file lists as empty and is created by the first save
        os.remove(cfg)
        assert list_servers()["servers"] == [] and list_servers()["exists"] is False
        save(dict(new, **ack))
        assert sorted(json.loads(file_text())["servers"]) == ["fake"]
    finally:
        TEST_START_TIMEOUT, TEST_LIST_TIMEOUT = saved_timeouts
        hearth_mcp.invalidate()
        hearth_mcp.permissions.RISK.clear()
        hearth_mcp.permissions.RISK.update(saved_risk)
        with _tests_lock:
            _tests.clear()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Give any probe thread still stopping its server a moment, so the
        # scratch directory is not in use when it is removed.
        time.sleep(0.5)
        shutil.rmtree(scratch, ignore_errors=True)

    print("hearth-desktop-mcp-admin self-test OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    print("usage: mcp_admin.py --self-test")
    sys.exit(2)
