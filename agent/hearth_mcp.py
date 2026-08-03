#!/usr/bin/env python3
r"""hearth MCP client: drive an external Model Context Protocol server over
stdio and surface its tools as ordinary hearth tools.

An MCP server is a program hearth starts and talks JSON-RPC 2.0 to over the
child's stdin/stdout, one JSON object per line. The server answers `initialize`,
lists its tools with JSON schemas, and runs them on request. Roblox Studio ships
one (StudioMCP.exe), which is why this module exists, but nothing here is
Roblox-specific.

Four things this module is careful about, because each is a way an external
process can hurt the agent that trusts it:

  1. It cannot hang hearth. Every wait is bounded. A server that never replies,
     replies to the wrong id, dies mid-request, or emits an unterminated line
     forever produces an error inside a known deadline, never a blocked loop.
     The reader works on raw bytes with a hard per-line ceiling, so a server
     that floods stdout without a newline is discarded rather than buffered
     until memory runs out.

  2. It cannot outlive hearth. The child is assigned to the process-wide
     KILL_ON_JOB_CLOSE Job object from hearth_sandbox on Windows, and to its
     own session with PR_SET_PDEATHSIG on Linux, the same machinery
     hearth_llama uses for llama-server, for the same reason. A crashed or
     task-killed hearth leaves no MCP server behind. Ordinary shutdown still
     does the polite thing first (close stdin, wait, then tree-kill through
     hearth_proc), because the Job is a backstop, not a strategy.

  3. Its tools cannot impersonate hearth's own. Every MCP tool is advertised
     under `mcp__<server>__<tool>`, and a self-test asserts no built-in tool
     name can ever collide with that prefix.

  4. Its output is untrusted. Tool results are text from outside hearth that
     reaches the model and, through the UI, a person. They are returned as
     ordinary tool-result strings so they travel the exact path every other
     tool result travels: the sidecar's prompt-injection scan, the transcript,
     and `neutralize` in desktop/ui/js/dom.js before anything is displayed.
     Nothing here is treated as instructions, and nothing here shortcuts that
     path.

Risk, and why annotations do not simply decide it
-------------------------------------------------
MCP tools carry optional `annotations` (readOnlyHint, destructiveHint,
openWorldHint). This module derives a hearth risk class from them:

    dangerous, unless the server says read-only AND non-destructive AND
    closed-world, in which case safe.

Nothing is ever derived as "edit", because "edit" is the class `auto` mode runs
without asking, and a tool that mutates a live game scene must not run
unasked. A tool this module has never registered is unknown to permissions.py
and therefore already dangerous, the fail-closed default, unchanged.

Those annotations come from the server, so a hostile server could claim
everything is read-only. That is worth stating plainly rather than defending
against here: a configured MCP server is an executable hearth starts, so it
already has whatever the user has, and no risk table can claw that back. The
boundary that matters is the config file (see `config_path()` below), not the
annotation. What the derivation genuinely protects against is a confused or
jailbroken *model* quietly triggering side effects the server itself describes
as side effects. To make that useful in the other direction, config may
*tighten* any tool's risk and may never loosen it: `risk` entries are clamped
to at-least-as-restrictive-as-derived.

Standard library only.
"""

import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hearth_paths  # noqa: E402
import hearth_proc  # noqa: E402
import permissions  # noqa: E402

try:
    import hearth_sandbox  # noqa: E402
except Exception:  # noqa: BLE001 - the Job backstop is optional, not required
    hearth_sandbox = None


# Every hearth tool backed by an MCP server carries this prefix. It exists so
# an MCP server can never shadow a built-in: see _self_test, which asserts no
# name in hearth_tools starts with it.
PREFIX = "mcp__"

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "hearth"
CLIENT_VERSION = "1"

# Every bound in this module, in one place, so the "cannot hang" claim is
# checkable by reading a dozen lines rather than the whole file.
START_TIMEOUT = 30      # seconds to complete the initialize handshake
CALL_TIMEOUT = 120      # default seconds for one tools/call
LIST_TIMEOUT = 60       # seconds for tools/list
STOP_TIMEOUT = 5        # seconds to wait for a polite exit before tree-killing
MAX_LINE_BYTES = 4 * 1024 * 1024   # a single stdout line longer than this is dropped
MAX_PENDING_LINES = 4096           # reader backlog before we stop queueing
MAX_BAD_LINES = 200                # malformed lines tolerated before giving up
MAX_RESULT_CHARS = 8000            # a tool result is truncated to this, with a note

_RISK_ORDER = {"safe": 0, "edit": 1, "dangerous": 2}


class MCPError(RuntimeError):
    """Anything that went wrong talking to an MCP server. Never a hang."""


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

def sanitize(part):
    """Reduce `part` to the characters a tool name may contain.

    Model APIs accept `[A-Za-z0-9_-]` for function names; a server key or tool
    name carrying anything else would produce a spec the model cannot call.
    Everything outside the set becomes '_'.
    """
    out = []
    for ch in str(part or ""):
        out.append(ch if (ch.isalnum() and ch.isascii()) or ch in "_-" else "_")
    return "".join(out)


def tool_name(server, tool):
    """The hearth-side name for `tool` on `server`."""
    return "{}{}__{}".format(PREFIX, sanitize(server), sanitize(tool))


def split_tool_name(full):
    """(server, tool) for a hearth-side MCP name, or (None, None).

    The first '__' after the prefix is the split. A server key containing '__'
    would make that ambiguous, which is why `Registry` rejects a key whose
    sanitised form collides with another (see load_config).
    """
    if not isinstance(full, str) or not full.startswith(PREFIX):
        return None, None
    rest = full[len(PREFIX):]
    if "__" not in rest:
        return None, None
    server, tool = rest.split("__", 1)
    if not server or not tool:
        return None, None
    return server, tool


def is_mcp_tool(name):
    return split_tool_name(name)[0] is not None


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------

def derive_risk(tool_def):
    """The hearth risk class implied by an MCP tool definition's annotations.

    "safe" only when the server describes the tool as read-only, not
    destructive, and not reaching the outside world. Everything else, including
    a tool with no annotations at all, is "dangerous". Never "edit": see the
    module docstring.
    """
    ann = (tool_def or {}).get("annotations")
    if not isinstance(ann, dict):
        return "dangerous"
    if ann.get("readOnlyHint") is not True:
        return "dangerous"
    if ann.get("destructiveHint") is True:
        return "dangerous"
    if ann.get("openWorldHint") is True:
        return "dangerous"
    return "safe"


def clamp_risk(derived, requested):
    """`requested` if it is at least as restrictive as `derived`, else `derived`.

    Config may tighten a tool and may never loosen it, so a config file an
    attacker edits cannot turn a scene-mutating tool into a "safe" read that
    runs with no prompt in every mode.
    """
    if requested not in _RISK_ORDER:
        return derived
    return requested if _RISK_ORDER[requested] >= _RISK_ORDER[derived] else derived


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def config_path():
    r"""Where the MCP server list lives.

    `%LOCALAPPDATA%\Hearth\mcp.json` on Windows, `<data_dir>/mcp.json`
    elsewhere: the same per-user directory that already holds the audit
    database and the checkpoints, resolved by hearth_paths.data_dir().

    That location is the security decision, not an incidental one. This file
    names an executable and its arguments, so whoever can write it can run
    arbitrary code as the user the next time hearth starts a server. The
    per-user data directory is the strongest place hearth can put it without
    asking for privileges it does not have: on Windows %LOCALAPPDATA% is
    ACL'd to the user; on Linux the XDG data directory is mode 0700 by
    convention and this module refuses to read a group- or world-writable
    file outright.

    Two honest limitations:

      * It is NOT out of reach of the agent itself. Hearth's `write_file` is
        contained to the workspace and cannot touch it, but `run_command` is
        not sandboxed against writing here at any level below `workspace`,
        and even at `workspace` the containment is a write boundary on the
        workspace, not a deny list for the rest of the profile. An agent
        allowed to run shell commands can write this file and thereby choose
        what executable hearth launches next. That is not a new capability,
        it already has arbitrary code execution, but it does mean this file
        is not a control on the agent. It is a control on everyone else.

      * HEARTH_MCP_CONFIG overrides the path, for tests and for an operator
        who keeps configuration elsewhere. Anything that can set hearth's
        environment can already choose its Python path, so this adds no
        exposure, but it is a knob and it is named here rather than hidden.
    """
    env = os.environ.get("HEARTH_MCP_CONFIG")
    if env:
        return env
    return os.path.join(hearth_paths.data_dir(), "mcp.json")


def _insecure_mode(path):
    """A reason string when `path` is writable by someone other than its owner.

    POSIX only. Windows has no cheap stdlib ACL read, and %LOCALAPPDATA% is
    already per-user ACL'd by the OS, so there is nothing honest to check
    there; returning None on Windows says that rather than pretending.
    """
    if hearth_paths.is_windows():
        return None
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if mode & 0o022:
        return "{} is group- or world-writable (mode {:o})".format(path, mode & 0o777)
    return None


def _clean_server(key, raw):
    """A validated server entry, or None with a reason on stderr.

    Rejects rather than coerces: a config that does not say exactly what to run
    should not be guessed at, because the guess is a process launch.
    """
    def bad(why):
        print("[hearth-mcp] ignoring server {!r}: {}".format(key, why), file=sys.stderr)
        return None

    if not isinstance(raw, dict):
        return bad("entry is not an object")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        return bad("missing a string 'command'")
    args = raw.get("args", [])
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        return bad("'args' must be a list of strings")
    env = raw.get("env", {})
    if not isinstance(env, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        return bad("'env' must be an object of string to string")
    risk = raw.get("risk", {})
    if not isinstance(risk, dict):
        return bad("'risk' must be an object of tool name to risk class")
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return bad("'cwd' must be a string")
    try:
        timeout = float(raw.get("timeout", CALL_TIMEOUT))
    except (TypeError, ValueError):
        return bad("'timeout' must be a number")
    if timeout <= 0:
        return bad("'timeout' must be positive")
    return {
        "key": sanitize(key),
        "command": command,
        "args": list(args),
        "env": dict(env),
        "cwd": cwd,
        "enabled": raw.get("enabled", True) is not False,
        "timeout": timeout,
        "risk": {str(k): str(v) for k, v in risk.items()},
    }


def load_config(path=None):
    """The configured servers as {key: entry}. A missing or broken config
    yields {} and a note on stderr, never an exception: MCP is optional, and a
    typo in an optional file must not stop hearth from starting."""
    path = path or config_path()
    if not os.path.exists(path):
        return {}
    insecure = _insecure_mode(path)
    if insecure:
        print("[hearth-mcp] refusing to read MCP config: {}".format(insecure),
              file=sys.stderr)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print("[hearth-mcp] could not read {}: {}".format(path, exc), file=sys.stderr)
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        print("[hearth-mcp] {} has no 'servers' object".format(path), file=sys.stderr)
        return {}
    out = {}
    for key, raw in servers.items():
        entry = _clean_server(key, raw)
        if entry is None:
            continue
        if entry["key"] in out:
            print("[hearth-mcp] duplicate server key {!r} after sanitising; "
                  "ignoring the later one".format(entry["key"]), file=sys.stderr)
            continue
        out[entry["key"]] = entry
    return out


# --------------------------------------------------------------------------
# Process guarding: reuse, do not reinvent
# --------------------------------------------------------------------------

def _assign_to_outer_job(proc):
    """Put `proc` in hearth's process-wide KILL_ON_JOB_CLOSE Job. True if it took.

    This is the same guarantee hearth_llama gives llama-server and
    hearth_sandbox gives every contained command: the Job handle is owned by
    this Python process and never closed, so when hearth dies for any reason
    Windows terminates everything still in it. An MCP server left running after
    hearth exits is the same bug, so it gets the same fix rather than a
    parallel one.

    Assigning straight after Popen is sufficient here for the same reason it is
    for llama-server: a stdio MCP server does its work after the handshake,
    which cannot have happened in the microseconds before assignment, so there
    is no grandchild to escape through the gap.
    """
    if not hearth_paths.is_windows() or hearth_sandbox is None:
        return False
    try:
        import ctypes
        handle = hearth_sandbox.outer_job()
        if not handle:
            return False
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        return bool(k.AssignProcessToJobObject(
            ctypes.c_void_p(handle), ctypes.c_void_p(int(proc._handle))))
    except Exception:  # noqa: BLE001 - the Job is a backstop; failing to get it
        # must not stop the server from starting, only be reported.
        return False


def _posix_die_with_parent():
    """preexec_fn asking the kernel to SIGKILL this child when hearth dies.
    Linux only, never raises (an exception in preexec_fn fails the spawn)."""
    try:
        import ctypes
        # PR_SET_PDEATHSIG (1), SIGKILL (9)
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 9, 0, 0, 0)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------

class MCPClient(object):
    """One stdio MCP server, spawned and spoken to over JSON-RPC 2.0.

    Concurrent `call_tool` on one instance is not supported beyond the id
    bookkeeping: `Registry` serialises calls per server with a lock, which is
    what the agent loop needs (one tool at a time) and keeps this class small.
    """

    def __init__(self, key, command, args=(), env=None, cwd=None,
                 timeout=CALL_TIMEOUT):
        self.key = key
        self.command = command
        self.args = list(args or ())
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = timeout
        self.proc = None
        self.guarded = False
        self.server_info = {}
        self.capabilities = {}
        self.instructions = ""
        self.notifications = []
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending = {}          # id -> [Event, message_or_None]
        self._pending_lock = threading.Lock()
        self._stderr = []
        self._bad_lines = 0
        self.overflows = 0
        self.dropped = 0

    # -- lifecycle ---------------------------------------------------------

    def _spawn(self):
        argv = [self.command] + self.args
        env = hearth_proc.child_env(self.env)
        kw = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": env,
            "cwd": self.cwd or None,
            "bufsize": 0,   # raw bytes: this module does its own line framing
        }
        if hearth_paths.is_windows():
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
            if sys.platform.startswith("linux"):
                kw["preexec_fn"] = _posix_die_with_parent
        try:
            self.proc = subprocess.Popen(argv, **kw)
        except OSError as exc:
            raise MCPError("could not start MCP server {!r}: {}".format(self.key, exc))
        self.guarded = _assign_to_outer_job(self.proc)
        threading.Thread(target=self._read_stdout, daemon=True,
                         name="mcp-{}-out".format(self.key)).start()
        threading.Thread(target=self._read_stderr, daemon=True,
                         name="mcp-{}-err".format(self.key)).start()

    def start(self):
        """Spawn and complete the initialize handshake. Returns the server's own
        description of itself."""
        if self.proc is not None:
            raise MCPError("client for {!r} already started".format(self.key))
        self._spawn()
        try:
            result = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            }, timeout=START_TIMEOUT)
        except MCPError:
            self.stop()
            raise
        if not isinstance(result, dict):
            self.stop()
            raise MCPError("MCP server {!r} answered initialize with {}".format(
                self.key, type(result).__name__))
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = result.get("instructions") or ""
        self._notify("notifications/initialized")
        return result

    def stop(self):
        """Shut the server down. Polite first, forceful second, always bounded.

        Closing stdin is how the MCP spec says to ask a stdio server to exit. A
        server that ignores it gets its whole tree killed through hearth_proc,
        the same path a timed-out shell command takes. The Job object would get
        it anyway when hearth exits; this is so a long-lived hearth does not
        accumulate servers.
        """
        proc = self.proc
        if proc is None:
            return
        self._fail_pending("MCP server {!r} stopped".format(self.key))
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            hearth_proc._kill_tree(proc)
            try:
                proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except OSError:
                pass
        self.proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.stop()
        return False

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # -- transport ---------------------------------------------------------

    def _read_stdout(self):
        """Frame the child's stdout into JSON-RPC messages, on raw bytes.

        Deliberately not `for line in proc.stdout`: a server that writes
        megabytes without a newline would make that buffer without limit, and
        "the MCP server made hearth run out of memory" is a hang by another
        name. Bytes accumulate to MAX_LINE_BYTES and are then discarded up to
        the next newline, which loses one message and keeps the process alive.
        """
        stream = self.proc.stdout
        buf = bytearray()
        skipping = False
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                if skipping:
                    nl = chunk.find(b"\n")
                    if nl < 0:
                        continue
                    skipping = False
                    chunk = chunk[nl + 1:]
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[:nl + 1]
                    self._on_line(line)
                if len(buf) > MAX_LINE_BYTES:
                    self.overflows += 1
                    buf.clear()
                    skipping = True
        except (OSError, ValueError):
            pass
        finally:
            self._fail_pending("MCP server {!r} closed its output".format(self.key))

    def _read_stderr(self):
        """Keep the last few stderr lines so an error can say what the server
        complained about. Bounded: a chatty server must not grow this forever."""
        try:
            for raw in iter(lambda: self.proc.stderr.readline(), b""):
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if text:
                    self._stderr.append(text)
                    if len(self._stderr) > 50:
                        del self._stderr[:-50]
        except (OSError, ValueError, AttributeError):
            pass

    def _on_line(self, raw):
        if not raw.strip():
            return
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            self._bad_lines += 1
            if self._bad_lines == MAX_BAD_LINES:
                self._fail_pending("MCP server {!r} sent {} unparseable lines".format(
                    self.key, MAX_BAD_LINES))
            return
        if not isinstance(msg, dict):
            self._bad_lines += 1
            return
        rid = msg.get("id")
        if rid is None:
            # A notification, or a request FROM the server. Servers may ask the
            # client to sample a model or list roots; hearth advertises no such
            # capability, so there is nothing to answer, and recording it is
            # the correct behaviour rather than replying to something we never
            # promised to support.
            if len(self.notifications) < MAX_PENDING_LINES:
                self.notifications.append(msg)
            else:
                self.dropped += 1
            return
        with self._pending_lock:
            slot = self._pending.get(rid)
        if slot is None:
            # A reply to a request that already timed out, or an id we never
            # sent. Dropped on purpose: matching it to anything would let a
            # server answer a question hearth is no longer asking.
            self.dropped += 1
            return
        slot[1] = msg
        slot[0].set()

    def _fail_pending(self, why):
        with self._pending_lock:
            slots = list(self._pending.values())
        for slot in slots:
            if slot[1] is None:
                slot[1] = {"error": {"code": -32000, "message": why}}
            slot[0].set()

    def _notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _send(self, obj):
        proc = self.proc
        if proc is None or proc.stdin is None or proc.stdin.closed:
            raise MCPError("MCP server {!r} is not running".format(self.key))
        payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MCPError("MCP server {!r} would not accept a request: {}{}".format(
                self.key, exc, self._stderr_note()))

    def _stderr_note(self):
        if not self._stderr:
            return ""
        return " (stderr: {})".format(" | ".join(self._stderr[-3:])[:500])

    def _request(self, method, params=None, timeout=None):
        """Send one request and return its `result`. Raises MCPError on any
        failure, including the deadline passing. Never blocks past `timeout`."""
        timeout = self.timeout if timeout is None else timeout
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
        slot = [threading.Event(), None]
        with self._pending_lock:
            self._pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params or {}})
            if not slot[0].wait(timeout):
                raise MCPError(
                    "MCP server {!r} did not answer {} within {:g}s{}".format(
                        self.key, method, timeout, self._stderr_note()))
        finally:
            with self._pending_lock:
                self._pending.pop(rid, None)
        msg = slot[1] or {}
        if msg.get("error") is not None:
            err = msg["error"]
            detail = err.get("message") if isinstance(err, dict) else err
            raise MCPError("MCP server {!r} refused {}: {}{}".format(
                self.key, method, detail, self._stderr_note()))
        if "result" not in msg:
            raise MCPError("MCP server {!r} answered {} with no result{}".format(
                self.key, method, self._stderr_note()))
        return msg["result"]

    # -- protocol ----------------------------------------------------------

    def list_tools(self):
        """Every tool the server offers, following `nextCursor` pagination.

        The page loop is bounded: a server that returns the same cursor forever
        would otherwise be an infinite loop wearing a protocol's clothes.
        """
        tools = []
        cursor = None
        seen_cursors = set()
        for _ in range(50):
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params, timeout=LIST_TIMEOUT)
            if not isinstance(result, dict):
                raise MCPError("MCP server {!r} sent a malformed tools/list".format(self.key))
            page = result.get("tools")
            if not isinstance(page, list):
                raise MCPError("MCP server {!r} sent tools/list with no tool array".format(
                    self.key))
            for item in page:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    tools.append(item)
            cursor = result.get("nextCursor")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        return tools

    def call_tool(self, tool, arguments=None, timeout=None):
        """Run one tool. Returns the raw MCP result dict."""
        return self._request("tools/call",
                             {"name": tool, "arguments": arguments or {}},
                             timeout=timeout)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

def result_text(result):
    """An MCP tools/call result as the plain string hearth's tool layer returns.

    Existing tools return text and signal failure with a leading "error: ", so
    this flattens content blocks the same way rather than inventing a second
    result shape the loop would have to learn. Non-text blocks are named, not
    inlined: an image is not something the text transcript can carry, and
    pretending otherwise would put base64 in the model's context.
    """
    if not isinstance(result, dict):
        return "error: MCP server returned {}".format(type(result).__name__)
    parts = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "resource":
            res = block.get("resource") or {}
            if isinstance(res, dict) and isinstance(res.get("text"), str):
                parts.append(res["text"])
            else:
                parts.append("[resource {}]".format(
                    (res or {}).get("uri", "?") if isinstance(res, dict) else "?"))
        else:
            parts.append("[{} content omitted]".format(kind or "unknown"))
    text = "\n".join(p for p in parts if p)
    if not text:
        structured = result.get("structuredContent")
        if structured is not None:
            try:
                text = json.dumps(structured, ensure_ascii=False)
            except (TypeError, ValueError):
                text = ""
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n... (truncated at {} characters)".format(
            MAX_RESULT_CHARS)
    if result.get("isError"):
        return "error: " + (text or "the MCP tool reported a failure")
    return text or "(no output)"


# --------------------------------------------------------------------------
# The registry: config -> hearth tools
# --------------------------------------------------------------------------

class Registry(object):
    """Every configured MCP server, and the hearth tools they add.

    Servers are started lazily on the first `descriptors()` call and kept
    running, because the handshake plus tools/list costs a second or two and an
    agent loop calls tools repeatedly. `close()` stops them; the Job object
    covers the case where nobody calls it.
    """

    def __init__(self, config=None):
        self.config = load_config() if config is None else config
        self.clients = {}
        self.tools = {}            # hearth name -> {"server","tool","def","risk"}
        self.errors = {}           # server key -> why it is unavailable
        self._lock = threading.RLock()
        self._loaded = False

    def _start_server(self, key, entry):
        client = MCPClient(key, entry["command"], entry["args"], entry["env"],
                           entry["cwd"], entry["timeout"])
        client.start()
        self.clients[key] = client
        for tool_def in client.list_tools():
            raw_name = tool_def.get("name")
            full = tool_name(key, raw_name)
            derived = derive_risk(tool_def)
            risk = clamp_risk(derived, entry["risk"].get(raw_name))
            self.tools[full] = {"server": key, "tool": raw_name,
                                "def": tool_def, "risk": risk}

    def load(self, force=False):
        """Connect to every enabled server and collect its tools.

        One server failing does not stop the others: a Studio that is not
        running should cost the run that server's tools, not every server's.
        """
        with self._lock:
            if self._loaded and not force:
                return self.tools
            self.tools = {}
            self.errors = {}
            for key, entry in sorted(self.config.items()):
                if not entry["enabled"]:
                    continue
                try:
                    if key not in self.clients:
                        self._start_server(key, entry)
                except MCPError as exc:
                    self.errors[key] = str(exc)
                    print("[hearth-mcp] {}".format(exc), file=sys.stderr)
            self._loaded = True
            self._register_risks()
            return self.tools

    def _register_risks(self):
        """Teach permissions.py what each MCP tool costs.

        Written into permissions.RISK rather than looked up through a hook so
        `permissions.decide` stays the pure, I/O-free function its docstring
        promises. A tool that has not been registered is simply unknown to
        permissions.risk_of, which already fails closed to "dangerous", so the
        ordering here can only ever be too strict, never too permissive.
        """
        for full, meta in self.tools.items():
            permissions.RISK[full] = meta["risk"]

    def descriptors(self):
        """The MCP tools in hearth_tools' TOOLS shape, ready to append."""
        self.load()
        out = []
        for full, meta in sorted(self.tools.items()):
            tool_def = meta["def"]
            schema = tool_def.get("inputSchema")
            if not isinstance(schema, dict) or schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}
            desc = (tool_def.get("description") or tool_def.get("title")
                    or "MCP tool {} on server {}".format(meta["tool"], meta["server"]))
            out.append({
                "name": full,
                "description": _one_paragraph(desc),
                "parameters": schema,
                "fn": _make_caller(self, full),
                "mcp": True,
            })
        return out

    def call(self, full, args):
        """Run one MCP tool by its hearth name. Returns a result string.

        Errors come back as "error: ..." text rather than exceptions, the same
        convention hearth_tools.execute_tool uses, so a server going away
        during a run reads to the model as a failed tool and not as a crash.
        """
        self.load()
        with self._lock:
            meta = self.tools.get(full)
        if meta is None:
            return "error: unknown MCP tool {}".format(full)
        client = self.clients.get(meta["server"])
        if client is None or not client.alive:
            return "error: MCP server {!r} is not running".format(meta["server"])
        entry = self.config.get(meta["server"]) or {}
        try:
            with self._lock:
                result = client.call_tool(meta["tool"], args,
                                          timeout=entry.get("timeout", CALL_TIMEOUT))
        except MCPError as exc:
            return "error: {}".format(exc)
        return result_text(result)

    def close(self):
        with self._lock:
            for client in list(self.clients.values()):
                try:
                    client.stop()
                except Exception:  # noqa: BLE001 - shutdown must not raise
                    pass
            self.clients = {}
            self._loaded = False


def _one_paragraph(text, limit=600):
    """A tool description short enough to spend on a small model's context.

    Studio's own descriptions run to a thousand characters of examples; a
    handful of those is most of an 8k window before the conversation starts.
    """
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _make_caller(reg, full):
    def call_it(args, _workspace):
        return reg.call(full, args or {})
    return call_it


_registry = None
_registry_lock = threading.Lock()


def registry():
    """The process-wide registry, built from config on first use."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = Registry()
        return _registry


def descriptors():
    """Every configured MCP tool, in hearth_tools' TOOLS shape.

    Returns [] and costs nothing when no MCP config exists, which is the
    default: hearth without an mcp.json behaves exactly as it did before this
    module existed, down to never starting a subprocess.
    """
    if not load_config():
        return []
    try:
        return registry().descriptors()
    except Exception as exc:  # noqa: BLE001 - MCP is optional; its failure must
        # never take the tool layer with it.
        print("[hearth-mcp] could not load MCP tools: {}".format(exc), file=sys.stderr)
        return []


def call(full, args):
    """Run a configured MCP tool by hearth name."""
    return registry().call(full, args or {})


def shutdown():
    """Stop every running MCP server. Safe to call more than once."""
    global _registry
    with _registry_lock:
        if _registry is not None:
            _registry.close()
            _registry = None


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

# A minimal MCP server, written to a temp file and run with this interpreter.
# `mode` selects the misbehaviour under test. Deliberately hand-rolled rather
# than mocked: the failures this module exists to survive (a dead pipe, an
# unterminated flood, a reply to an id nobody asked about) only happen to a
# real process on a real pipe.
_FAKE_SERVER = r'''
import json, os, sys, time

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"
PIDFILE = sys.argv[2] if len(sys.argv) > 2 else ""
if PIDFILE:
    with open(PIDFILE, "w") as fh:
        fh.write(str(os.getpid()))

if MODE == "die":
    sys.exit(7)
if MODE == "flood":
    # One line that never ends, forever, as fast as the pipe allows.
    while True:
        sys.stdout.write("x" * 65536)
        sys.stdout.flush()

def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

TOOLS = [
    {"name": "peek", "description": "read something",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}},
                     "required": ["q"]},
     "annotations": {"readOnlyHint": True, "destructiveHint": False,
                     "openWorldHint": False}},
    {"name": "poke", "description": "change something",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
    {"name": "surf", "description": "reach the network",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "openWorldHint": True}},
    {"name": "plain", "description": "no annotations at all",
     "inputSchema": {"type": "object", "properties": {}}},
]

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        msg = json.loads(raw)
    except ValueError:
        continue
    method = msg.get("method")
    rid = msg.get("id")
    if rid is None:
        continue
    if method == "initialize":
        if MODE == "silent":
            continue
        if MODE == "garbage":
            sys.stdout.write("this is not json at all\n")
            sys.stdout.write("{\"partial\": \n")
            sys.stdout.flush()
            out({"jsonrpc": "2.0", "id": 999, "result": {"stray": True}})
        if MODE == "notify":
            out({"jsonrpc": "2.0", "method": "notifications/message",
                 "params": {"level": "info", "data": "hello"}})
        out({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "fake", "version": "0.0.1"},
            "instructions": "a fake server"}})
    elif method == "tools/list":
        out({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        args = (msg.get("params") or {}).get("arguments") or {}
        if MODE == "hang" and name == "peek":
            continue
        if name == "peek":
            out({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": "saw " + str(args.get("q"))}]}})
        elif name == "poke":
            out({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "nope"}], "isError": True}})
        else:
            out({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": "no such tool " + str(name)}})
    else:
        out({"jsonrpc": "2.0", "id": rid, "error": {
            "code": -32601, "message": "unknown method"}})

if MODE == "sticky":
    # stdin has ended, and this server does not care. A well-behaved stdio
    # server exits here; the orphan test needs one that does not, so that what
    # kills it can only be the Job object or PDEATHSIG.
    while True:
        time.sleep(1)
'''

# A helper that starts an MCP server and then does nothing, so the orphan test
# can hard-kill it and see whether the server survives.
_ORPHAN_HELPER = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
import hearth_mcp
c = hearth_mcp.MCPClient("orphan", sys.executable,
                         [sys.argv[2], "sticky", sys.argv[3]], timeout=20)
c.start()
sys.stdout.write("guarded=%s\n" % c.guarded)
sys.stdout.flush()
while True:
    time.sleep(0.5)
'''


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _pid_alive(pid):
    if hearth_paths.is_windows():
        out = subprocess.run(["tasklist", "/FI", "PID eq {}".format(pid), "/NH"],
                             capture_output=True, text=True, timeout=30).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _hard_kill(pid):
    """Kill just this process, NOT its tree: the orphan test's whole point is
    that nothing walks down to the grandchild for us."""
    if hearth_paths.is_windows():
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=30)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _self_test():
    import shutil
    import tempfile

    # --- names ----------------------------------------------------------
    assert sanitize("Roblox Studio!") == "Roblox_Studio_"
    assert tool_name("roblox", "get_studio_state") == "mcp__roblox__get_studio_state"
    assert split_tool_name("mcp__roblox__get_studio_state") == ("roblox", "get_studio_state")
    assert split_tool_name("mcp__roblox__a__b") == ("roblox", "a__b")
    assert split_tool_name("read_file") == (None, None)
    assert split_tool_name("mcp__nounderscores") == (None, None)
    assert split_tool_name(None) == (None, None)
    assert is_mcp_tool("mcp__x__y") and not is_mcp_tool("run_command")

    # An MCP server must never be able to answer to a built-in tool's name.
    # Checked against hearth_tools itself rather than a copied list, so adding
    # a built-in that starts with "mcp__" fails here instead of in production.
    import hearth_tools
    for t in hearth_tools.TOOLS:
        assert not t["name"].startswith(PREFIX), t["name"]
    assert all(not n.startswith(PREFIX) for n in hearth_tools.WINDOWS_TOOLS)
    assert all(not n.startswith(PREFIX) for n in permissions.RISK), \
        "a built-in risk entry must not use the MCP prefix"

    # --- risk derivation ------------------------------------------------
    ro = {"annotations": {"readOnlyHint": True, "destructiveHint": False,
                          "openWorldHint": False}}
    assert derive_risk(ro) == "safe"
    assert derive_risk({"annotations": {"readOnlyHint": True, "destructiveHint": True}}) == "dangerous"
    assert derive_risk({"annotations": {"readOnlyHint": True, "openWorldHint": True}}) == "dangerous"
    assert derive_risk({"annotations": {"readOnlyHint": False}}) == "dangerous"
    assert derive_risk({}) == "dangerous", "no annotations must fail closed"
    assert derive_risk(None) == "dangerous"
    # Nothing is ever derived as "edit": that is the class auto mode runs
    # without asking, and no external tool earns it automatically.
    for case in (ro, {}, {"annotations": {"readOnlyHint": True}}):
        assert derive_risk(case) in ("safe", "dangerous")

    # config may tighten, never loosen
    assert clamp_risk("safe", "dangerous") == "dangerous"
    assert clamp_risk("safe", "edit") == "edit"
    assert clamp_risk("dangerous", "safe") == "dangerous", "config must not loosen risk"
    assert clamp_risk("dangerous", "edit") == "dangerous", "config must not loosen risk"
    assert clamp_risk("safe", "nonsense") == "safe"
    assert clamp_risk("safe", None) == "safe"

    ws = tempfile.mkdtemp(prefix="hearth-mcp-")
    old_cfg = os.environ.get("HEARTH_MCP_CONFIG")
    try:
        server_py = _write(os.path.join(ws, "fake_server.py"), _FAKE_SERVER)

        # --- config validation ------------------------------------------
        cfg_path = os.path.join(ws, "mcp.json")
        os.environ["HEARTH_MCP_CONFIG"] = cfg_path
        assert config_path() == cfg_path
        assert load_config() == {}, "a missing config file must be empty, not an error"

        _write(cfg_path, "{ not json")
        assert load_config() == {}, "a broken config must not raise"

        _write(cfg_path, json.dumps({"servers": {
            "good": {"command": "x", "args": ["a"], "risk": {"t": "dangerous"}},
            "nocommand": {"args": []},
            "badargs": {"command": "x", "args": [1, 2]},
            "badenv": {"command": "x", "env": {"A": 3}},
            "badtimeout": {"command": "x", "timeout": "soon"},
            "negtimeout": {"command": "x", "timeout": -1},
            "notobject": ["x"],
            "off": {"command": "x", "enabled": False},
        }}))
        cfg = load_config()
        assert sorted(cfg) == ["good", "off"], sorted(cfg)
        assert cfg["good"]["args"] == ["a"] and cfg["good"]["enabled"] is True
        assert cfg["off"]["enabled"] is False
        assert cfg["good"]["timeout"] == CALL_TIMEOUT

        if not hearth_paths.is_windows():
            os.chmod(cfg_path, 0o666)
            assert load_config() == {}, "a world-writable config must be refused"
            os.chmod(cfg_path, 0o600)
            assert load_config(), "a 0600 config must be accepted again"

        # --- a real handshake against a real (fake) server ---------------
        with MCPClient("fake", sys.executable, [server_py, "notify"], timeout=20) as c:
            assert c.server_info.get("name") == "fake", c.server_info
            assert c.server_info.get("version") == "0.0.1"
            assert c.capabilities.get("tools", {}).get("listChanged") is True
            assert c.instructions == "a fake server"
            assert c.alive
            tools = c.list_tools()
            assert [t["name"] for t in tools] == ["peek", "poke", "surf", "plain"], tools
            # a notification arriving before the reply is recorded, not confused
            # for a response
            assert any(n.get("method") == "notifications/message"
                       for n in c.notifications), c.notifications
            r = c.call_tool("peek", {"q": "hello"})
            assert result_text(r) == "saw hello", r
            r = c.call_tool("poke", {})
            assert result_text(r) == "error: nope", r
            try:
                c.call_tool("missing", {})
            except MCPError as exc:
                assert "no such tool" in str(exc), exc
            else:
                raise AssertionError("a JSON-RPC error must raise MCPError")
        assert not c.alive, "stop() must leave no server running"

        # --- results ------------------------------------------------------
        assert result_text({"content": [{"type": "image", "data": "AAAA"}]}) == \
            "[image content omitted]", "binary content must not reach the transcript"
        assert result_text({"content": []}) == "(no output)"
        assert result_text("nonsense").startswith("error: ")
        assert result_text({"structuredContent": {"a": 1}}) == '{"a": 1}'
        big = result_text({"content": [{"type": "text", "text": "z" * (MAX_RESULT_CHARS * 2)}]})
        assert len(big) < MAX_RESULT_CHARS + 100 and "truncated" in big

        # --- a server that never answers times out, and does not hang -----
        t0 = time.monotonic()
        c2 = MCPClient("silent", sys.executable, [server_py, "silent"], timeout=2)
        try:
            c2.start()
        except MCPError as exc:
            assert "did not answer" in str(exc), exc
        else:
            raise AssertionError("a silent server must fail the handshake")
        elapsed = time.monotonic() - t0
        assert elapsed < 45, "a silent server blocked for {:.1f}s".format(elapsed)
        assert not c2.alive, "a failed start must not leave the server running"

        # --- a server that hangs on one call, having answered the rest ----
        c3 = MCPClient("hang", sys.executable, [server_py, "hang"], timeout=2)
        c3.start()
        try:
            t0 = time.monotonic()
            try:
                c3.call_tool("peek", {"q": "x"})
            except MCPError as exc:
                assert "did not answer" in str(exc), exc
            else:
                raise AssertionError("a hanging tool call must time out")
            elapsed = time.monotonic() - t0
            assert elapsed < 30, "a hanging call blocked for {:.1f}s".format(elapsed)
            # the client is still usable afterwards: one dead call does not
            # poison the connection
            assert result_text(c3.call_tool("poke", {})) == "error: nope"
        finally:
            c3.stop()

        # --- a server that dies immediately -------------------------------
        t0 = time.monotonic()
        c4 = MCPClient("dead", sys.executable, [server_py, "die"], timeout=10)
        try:
            c4.start()
        except MCPError as exc:
            assert "closed its output" in str(exc) or "not running" in str(exc), exc
        else:
            raise AssertionError("a server that exits must fail the handshake")
        elapsed = time.monotonic() - t0
        assert elapsed < 30, "a dead server took {:.1f}s to fail".format(elapsed)

        # --- malformed JSON, and a reply to an id nobody sent --------------
        c5 = MCPClient("garbage", sys.executable, [server_py, "garbage"], timeout=15)
        c5.start()
        try:
            assert c5.server_info.get("name") == "fake", \
                "unparseable lines before the reply must not break the handshake"
            assert c5.dropped >= 1, "a reply to an unknown id must be dropped, not matched"
            assert result_text(c5.call_tool("peek", {"q": "still here"})) == "saw still here"
        finally:
            c5.stop()

        # --- a server that floods stdout without ever ending a line -------
        # The bound is memory, not time: the reader must discard rather than
        # buffer. The handshake still fails, promptly, instead of the process
        # growing until it dies.
        t0 = time.monotonic()
        c6 = MCPClient("flood", sys.executable, [server_py, "flood"], timeout=3)
        try:
            c6.start()
        except MCPError:
            pass
        else:
            raise AssertionError("a flooding server cannot have completed a handshake")
        elapsed = time.monotonic() - t0
        assert elapsed < 45, "a flooding server blocked for {:.1f}s".format(elapsed)
        assert not c6.alive, "the flooding server must be gone after a failed start"

        # --- ORPHAN PREVENTION -------------------------------------------
        # test name: mcp-orphan-guard
        # A hearth that is hard-killed (no clean shutdown, no tree walk) must
        # not leave its MCP server running. On Windows that is the
        # KILL_ON_JOB_CLOSE Job from hearth_sandbox; on Linux PR_SET_PDEATHSIG.
        # The helper is killed with taskkill WITHOUT /T (or a bare SIGKILL) so
        # nothing walks down to the grandchild on our behalf, and the server is
        # run in "sticky" mode so it ignores the stdin EOF that a well-behaved
        # stdio server would exit on. Both matter: without the sticky mode this
        # test passes with the Job object removed, because closing the pipe was
        # doing the work, and it would then be proving nothing about the guard
        # it is named for. If the server dies here, it is because the guard
        # worked.
        agent_dir = os.path.dirname(os.path.abspath(__file__))
        helper = _write(os.path.join(ws, "orphan_helper.py"), _ORPHAN_HELPER)
        pidfile = os.path.join(ws, "server.pid")
        h = subprocess.Popen([sys.executable, helper, agent_dir, server_py, pidfile],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        try:
            deadline = time.monotonic() + 60
            server_pid = None
            while time.monotonic() < deadline:
                if os.path.exists(pidfile):
                    try:
                        server_pid = int(open(pidfile).read().strip())
                        break
                    except ValueError:
                        pass
                if h.poll() is not None:
                    raise AssertionError("orphan helper exited: {}".format(
                        h.communicate()[1][:500]))
                time.sleep(0.2)
            assert server_pid, "the orphan helper never started an MCP server"
            # Let the helper finish its handshake so the guard is in place.
            time.sleep(2.0)
            assert _pid_alive(server_pid), "the fake server should be running"
            _hard_kill(h.pid)
            gone_deadline = time.monotonic() + 30
            while time.monotonic() < gone_deadline and _pid_alive(server_pid):
                time.sleep(0.3)
            assert not _pid_alive(server_pid), (
                "MCP server {} outlived a hard-killed hearth: the "
                "KILL_ON_JOB_CLOSE guard did not engage".format(server_pid))
        finally:
            _hard_kill(h.pid)
            try:
                sp = int(open(pidfile).read().strip()) if os.path.exists(pidfile) else 0
                if sp and _pid_alive(sp):
                    _hard_kill(sp)
            except (OSError, ValueError):
                pass

        # --- PERMISSION GATING -------------------------------------------
        # test name: mcp-permission-gating
        # The registry is what teaches permissions.py an MCP tool's risk, so
        # the gating is tested through a real registry over a real server
        # rather than by poking permissions.RISK directly.
        _write(cfg_path, json.dumps({"servers": {"fake": {
            "command": sys.executable, "args": [server_py, "ok"],
            "timeout": 20,
            # tighten one tool the server itself calls read-only, and try to
            # loosen one it calls destructive: only the tightening may land.
            "risk": {"peek": "dangerous", "poke": "safe"}}}}))
        saved_risk = dict(permissions.RISK)
        reg = Registry()
        try:
            descs = reg.descriptors()
            names = [d["name"] for d in descs]
            assert names == ["mcp__fake__peek", "mcp__fake__plain",
                             "mcp__fake__poke", "mcp__fake__surf"], names
            for d in descs:
                assert d["parameters"]["type"] == "object", d["name"]
                assert d["description"] and len(d["description"]) <= 603
                assert callable(d["fn"])
            # the schema the model sees is the server's real one
            peek = [d for d in descs if d["name"] == "mcp__fake__peek"][0]
            assert peek["parameters"]["required"] == ["q"], peek["parameters"]

            # risk, as registered
            assert permissions.risk_of("mcp__fake__surf") == "dangerous", \
                "an open-world tool must be dangerous"
            assert permissions.risk_of("mcp__fake__poke") == "dangerous", \
                "a destructive tool must be dangerous even when config asks for safe"
            assert permissions.risk_of("mcp__fake__peek") == "dangerous", \
                "config tightening must be honoured"
            assert permissions.risk_of("mcp__fake__plain") == "dangerous", \
                "an unannotated tool must fail closed"
            # an MCP tool nobody registered is dangerous, because unknown is
            # dangerous: the ordering between registration and a decision can
            # only ever be too strict.
            assert permissions.risk_of("mcp__fake__never_registered") == "dangerous"

            # ... and how that risk decides
            assert permissions.decide("plan", "mcp__fake__poke") == "deny"
            assert permissions.decide("edit", "mcp__fake__poke") == "gate"
            assert permissions.decide("auto", "mcp__fake__poke") == "gate", \
                "a scene-mutating MCP tool must never run unasked in auto mode"
            assert permissions.decide("auto", "mcp__fake__surf") == "gate"

            # the capability manifest can allow or deny MCP tools one by one,
            # and it caps every mode including bypass, so bypass stays
            # unreachable as a way around the manifest.
            manifest = {"read_file", "mcp__fake__surf"}
            assert permissions.decide("auto", "mcp__fake__surf",
                                      allowed_tools=manifest) == "gate"
            assert permissions.decide("auto", "mcp__fake__poke",
                                      allowed_tools=manifest) == "deny"
            assert permissions.decide("bypass", "mcp__fake__poke",
                                      allowed_tools=manifest) == "deny", \
                "bypass must not escape the capability manifest"
            assert permissions.decide("bypass", "mcp__fake__poke",
                                      allowed_tools=set()) == "deny"

            # a genuinely read-only tool derives safe, once nothing tightens it
            entry = dict(load_config()["fake"], risk={})
            reg2 = Registry({"fake2": dict(entry, key="fake2")})
            try:
                reg2.descriptors()
                assert permissions.risk_of("mcp__fake2__peek") == "safe"
                assert permissions.decide("plan", "mcp__fake2__peek") == "allow"
                assert permissions.decide("auto", "mcp__fake2__peek") == "allow"
            finally:
                reg2.close()

            # and the tool actually runs through the registry
            assert reg.call("mcp__fake__peek", {"q": "live"}) == "saw live"
            assert reg.call("mcp__nope__nope", {}).startswith("error: unknown MCP tool")
            # descriptors carry a callable with hearth_tools' (args, workspace)
            # signature, which is how execute_tool reaches them
            assert peek["fn"]({"q": "via fn"}, ws) == "saw via fn"
        finally:
            permissions.RISK.clear()
            permissions.RISK.update(saved_risk)
            reg.close()

        # a server whose command does not exist costs that server, not the run
        reg3 = Registry({"broken": {"key": "broken", "command": os.path.join(ws, "nope.exe"),
                                    "args": [], "env": {}, "cwd": None,
                                    "enabled": True, "timeout": 5, "risk": {}}})
        try:
            assert reg3.descriptors() == []
            assert "broken" in reg3.errors
            assert reg3.call("mcp__broken__x", {}).startswith("error: ")
        finally:
            reg3.close()

        # no config at all: no tools, no subprocess, no cost
        os.environ["HEARTH_MCP_CONFIG"] = os.path.join(ws, "absent.json")
        shutdown()
        assert descriptors() == []
    finally:
        shutdown()
        if old_cfg is None:
            os.environ.pop("HEARTH_MCP_CONFIG", None)
        else:
            os.environ["HEARTH_MCP_CONFIG"] = old_cfg
        shutil.rmtree(ws, ignore_errors=True)

    print("hearth-mcp self-test OK")
    return 0


def _live_report():
    """Connect to every configured server and print what it offers.

    The operator-facing half of this module: it is how you find out what a
    server actually exposes, and what risk class each tool lands in, before
    letting a model near it.
    """
    cfg = load_config()
    if not cfg:
        print("no MCP servers configured; see {}".format(config_path()))
        return 1
    rc = 0
    for key, entry in sorted(cfg.items()):
        print("=" * 70)
        print("server {!r}: {} {}".format(key, entry["command"], " ".join(entry["args"])))
        if not entry["enabled"]:
            print("  (disabled)")
            continue
        client = MCPClient(key, entry["command"], entry["args"], entry["env"],
                           entry["cwd"], entry["timeout"])
        try:
            info = client.start()
            print("  initialize: protocol={} name={} version={}".format(
                info.get("protocolVersion"),
                client.server_info.get("name"), client.server_info.get("version")))
            print("  capabilities: {}".format(json.dumps(client.capabilities)))
            if client.instructions:
                print("  instructions: {}".format(client.instructions))
            print("  orphan guard engaged: {}".format(client.guarded))
            tools = client.list_tools()
            print("  {} tools:".format(len(tools)))
            for t in tools:
                derived = derive_risk(t)
                final = clamp_risk(derived, entry["risk"].get(t["name"]))
                props = list((t.get("inputSchema") or {}).get("properties") or {})
                req = (t.get("inputSchema") or {}).get("required") or []
                print("    {:<28} risk={:<9} required={} optional={}".format(
                    t["name"], final, req, [p for p in props if p not in req]))
        except MCPError as exc:
            print("  FAILED: {}".format(exc))
            rc = 1
        finally:
            client.stop()
    return rc


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return _self_test()
    if "--live" in argv:
        return _live_report()
    print("usage: hearth_mcp.py [--self-test | --live]")
    print("  --live connects to every server in {} and prints its tools".format(
        config_path()))
    return 2


if __name__ == "__main__":
    sys.exit(main())
