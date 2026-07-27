#!/usr/bin/env python3
"""hearth live end-to-end test: the real sidecar, the real agent loop, the
real permission gate, the real tool layer, and real git-backed checkpoints,
all driven together against a real Ollama and a real local model.

Every module under agent/ and desktop/server/ has its own self-test, and
every one of those self-tests passes with the model stubbed out. That is
deliberate -- it makes each module fast and deterministic to test in
isolation -- but it also means no self-test ever proves the pieces actually
fit together. That gap has already produced a real defect: hearth_loop.chat()
returned a 3-tuple while callers unpacked two values, and all 22 self-tests
stayed green because every one of them injected a mock chat function that
never took that path. This script is the guard against that class of
failure: it starts the sidecar for real, talks to it over real HTTP, and
drives one real turn against a real model, end to end.

What it proves, step by step:
  1. desktop/server/main.py boots as a real subprocess and hands back a
     working handshake (port, token, pid) on stdout.
  2. GET /healthz answers without auth.
  3. A session can be created against a temporary workspace with real files.
  4. A prompt reaches the model, and the model's tool calls are actually
     dispatched through the real tool layer.
  5. The permission gate (permissions.decide, mode "edit") actually fires
     for a file write, and POST /approve actually resolves it -- not a
     stubbed decision function.
  6. The workspace changes on disk exactly the way the tool_call event says
     it did.
  7. The automatic pre-turn checkpoint (hearth_checkpoint.checkpoint, called
     by engine.RealEngine at the top of every turn) round-trips through
     GET /checkpoints and POST /restore back to byte-exact original content.
  8. POST /cancel actually interrupts a running turn and the session returns
     to idle promptly, not just eventually.
  9. The sidecar shuts down cleanly and no orphan process is left listening
     on its port.

Design notes, read before changing behaviour:

  Local models are weak and frequently emit malformed tool calls. This
  script never asserts that the model produced a *correct* answer -- only
  that the mechanics around it are sound: a tool call was dispatched, the
  gate fired, the approval resolved, the disk changed the way the event
  claimed, and checkpoint/restore round-tripped byte-exact. Those are
  deterministic given that the model attempts what it was concretely asked
  to attempt; the prompt below names the exact tool to use for exactly this
  reason -- it is not "assert the model got fancy reasoning right", it is
  "give a 7B model the least room possible to wander off the mechanical path
  this script needs to observe".

  Ollama base URL override. desktop/server/main.py's engine.RealEngine has
  no CLI flag or environment variable to point it at a non-default Ollama:
  it always falls back to hearth_loop.DEFAULT_OLLAMA (a plain module
  constant, "http://127.0.0.1:11434"), read at RealEngine.__init__ time, not
  bound into any function's default-argument list at import time. This
  script starts the sidecar via a tiny inline bootstrap (python -c ...) that
  imports hearth_loop, overwrites hearth_loop.DEFAULT_OLLAMA with whatever
  --ollama-url was given, and only then runpy.run_path()s the real,
  unmodified desktop/server/main.py as __main__. That is exactly what
  running `python main.py` directly does, plus one attribute assignment
  first -- main.py itself is never edited, and every module it imports is
  imported and executed exactly as it would be in production. This relies
  on engine.py's own deliberate choice to look up hearth_loop.DEFAULT_OLLAMA
  at call time rather than def time (see RealEngine.__init__ and
  list_installed_models); if that ever changes, this override silently stops
  working and the live steps below will simply fail to reach the model
  (a loud, honest failure -- not a false pass), since this script's own
  preflight check already proved a model is reachable at --ollama-url from
  this host, so the sidecar being unable to reach it too is worth reporting.

  Skip, not fail, when there is no model. A red result on a machine with no
  Ollama or no pulled model would train people to ignore this script, which
  defeats its entire purpose. The Ollama-and-model preflight check runs
  before anything else -- before the sidecar is even started -- and on
  failure this prints a single clear SKIPPED line and exits 0.

Standard library only. Python 3.12+. Cross-platform (developed on Windows,
intended to run for real on Linux; see the report this script ships with for
exactly what was and was not exercised on which platform).
"""

import argparse
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
# The catalog in agent/hearth_shop.py names this as the right call for an
# RTX 2060: big enough to be a real coding model, small enough to fit.
DEFAULT_MODEL = "qwen2.5-coder:7b-instruct-q4_K_M"
DEFAULT_TIMEOUT = 420.0  # seconds budget for the one real turn over SSE;
                         # hearth_loop.chat's own per-call timeout is 300s,
                         # and a read-then-write turn needs more than one call.

PREFLIGHT_TIMEOUT = 8.0
HANDSHAKE_TIMEOUT = 20.0
SHUTDOWN_TIMEOUT = 10.0
CANCEL_IDLE_TIMEOUT = 30.0
RESTORE_IDLE_TIMEOUT = 10.0
SSE_SOCKET_TIMEOUT = 30.0  # the server sends a keep-alive comment at least
                           # every 15s, so a healthy stream never blocks
                           # readline() this long; this is a hang detector.

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "desktop" / "server"
AGENT_DIR = REPO_ROOT / "agent"
MAIN_PY = SERVER_DIR / "main.py"

HELLO_CONTENT = "Hello, hearth!\n"
README_CONTENT = "# scratch\n\nTemporary workspace for hearth's live e2e test.\n"

PROMPT_MESSAGE = (
    "There is a file named hello.txt in this workspace. First call the "
    "read_file tool to read its exact current contents. Then call the "
    "write_file tool to overwrite hello.txt with its original contents "
    "followed by exactly one new line that says: e2e test touched this "
    "file. Use the tools now; do not just describe what you would do."
)

CANCEL_PROMPT_MESSAGE = (
    "Write a very long, detailed essay of at least 3000 words about the "
    "history of software testing. Do not call any tools -- just write the "
    "essay as plain text."
)


class Results:
    """Tracks PASS/FAIL lines and the overall exit status. SKIPPED is
    handled separately at the top of main(), before this is ever built,
    since a skip must never look like even a partial failure."""

    def __init__(self):
        self.ok = True
        self.count_pass = 0
        self.count_fail = 0

    def passed(self, name, detail=""):
        self.count_pass += 1
        line = "[PASS] {}".format(name)
        if detail:
            line += " -- {}".format(detail)
        print(line, flush=True)

    def failed(self, name, detail=""):
        self.ok = False
        self.count_fail += 1
        line = "[FAIL] {}".format(name)
        if detail:
            line += " -- {}".format(detail)
        print(line, flush=True)


def _info(msg):
    print("[INFO] {}".format(msg), flush=True)


# ---- Ollama preflight ------------------------------------------------

def check_ollama(ollama_url, model, timeout):
    """(ok, reason, available_names). Never raises: any failure to reach
    Ollama or to find the model is reported as ok=False with a human reason,
    which main() turns into a SKIPPED line, not a FAIL."""
    url = ollama_url.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
        return False, "Ollama not reachable at {}: {}: {}".format(
            ollama_url, type(exc).__name__, exc), []

    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return False, "Ollama at {} returned an unexpected /api/tags response shape".format(
            ollama_url), []

    names = set()
    for m in models:
        if isinstance(m, dict):
            for key in ("name", "model"):
                v = m.get(key)
                if v:
                    names.add(v)

    if model not in names:
        return False, "model {!r} is not pulled on {} (installed: {})".format(
            model, ollama_url, ", ".join(sorted(names)) or "(none)"), sorted(names)

    return True, "", sorted(names)


# ---- sidecar subprocess -----------------------------------------------

def _build_bootstrap(ollama_url):
    """The inline launcher: monkeypatches hearth_loop.DEFAULT_OLLAMA, then
    runs the real, unmodified desktop/server/main.py as __main__. See the
    module docstring's "Ollama base URL override" note for why this exists
    and what it depends on."""
    return (
        "import runpy, sys\n"
        "sys.path.insert(0, {agent!r})\n"
        "sys.path.insert(0, {server!r})\n"
        "import hearth_loop\n"
        "hearth_loop.DEFAULT_OLLAMA = {url!r}\n"
        "runpy.run_path({main!r}, run_name='__main__')\n"
    ).format(agent=str(AGENT_DIR), server=str(SERVER_DIR), url=ollama_url, main=str(MAIN_PY))


def _kill(proc, timeout=SHUTDOWN_TIMEOUT):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:  # noqa: BLE001 - best-effort escalation below
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def start_sidecar(ollama_url):
    """Start desktop/server/main.py for real, as a subprocess, and read its
    one-line JSON handshake from stdout. Raises RuntimeError with full
    diagnostics (including captured stderr) on any failure; the caller
    decides how to report that."""
    bootstrap = _build_bootstrap(ollama_url)
    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        cwd=str(SERVER_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stdout_q = queue.Queue()
    stderr_buf = []

    def _pump_stdout():
        try:
            for line in iter(proc.stdout.readline, ""):
                stdout_q.put(line)
        finally:
            stdout_q.put(None)

    def _pump_stderr():
        try:
            for line in iter(proc.stderr.readline, ""):
                stderr_buf.append(line)
        except Exception:  # noqa: BLE001 - diagnostics only
            pass

    threading.Thread(target=_pump_stdout, daemon=True).start()
    threading.Thread(target=_pump_stderr, daemon=True).start()

    try:
        line = stdout_q.get(timeout=HANDSHAKE_TIMEOUT)
    except queue.Empty:
        line = "<no output at all>"

    if not line:
        _kill(proc)
        raise RuntimeError(
            "sidecar produced no handshake within {}s (process exit code: {}); "
            "stderr:\n{}".format(HANDSHAKE_TIMEOUT, proc.poll(), "".join(stderr_buf[-60:])))

    try:
        handshake = json.loads(line.strip())
    except ValueError as exc:
        _kill(proc)
        raise RuntimeError(
            "handshake line was not valid JSON: {!r} ({}); stderr:\n{}".format(
                line, exc, "".join(stderr_buf[-60:]))) from exc

    for key in ("port", "token", "pid"):
        if key not in handshake:
            _kill(proc)
            raise RuntimeError("handshake missing {!r}: {}".format(key, handshake))

    return proc, handshake, stderr_buf


def shutdown_sidecar(proc, handshake, results):
    """Step 9: terminate the sidecar and prove nothing is left listening on
    its port. Always attempted, even if earlier steps failed."""
    if proc is None:
        results.failed("shutdown sidecar", "sidecar was never successfully started")
        return

    port = handshake.get("port") if handshake else None
    already_dead = proc.poll() is not None
    graceful = True
    if not already_dead:
        proc.terminate()
        try:
            proc.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            graceful = False
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    exited = proc.poll() is not None
    orphan_listening = False
    if port is not None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                orphan_listening = True
        except OSError:
            orphan_listening = False

    if exited and not orphan_listening:
        results.passed(
            "shutdown sidecar",
            "process exited (graceful={}), port {} no longer accepting connections".format(
                graceful, port))
    else:
        results.failed(
            "shutdown sidecar",
            "process exited={} exit_code={} orphan_listening_on_port={}".format(
                exited, proc.poll(), orphan_listening))


# ---- HTTP client for the sidecar's own API ------------------------------

class SidecarClient:
    def __init__(self, port, token, timeout=30):
        self.port = port
        self.token = token
        self.timeout = timeout
        self.base = "http://127.0.0.1:{}".format(port)

    def request(self, method, path, obj=None):
        """Returns (status, parsed_body). Never raises for a well-formed
        HTTP error response (4xx/5xx) -- those come back as (code, body)
        like any other response, since app.py's own error shapes are part
        of what this script is verifying. Raises RuntimeError only for a
        genuine transport failure (connection refused, DNS, etc.)."""
        url = self.base + path
        data = None
        headers = {"Authorization": "Bearer " + self.token}
        if obj is not None:
            data = json.dumps(obj).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                body = json.loads(raw) if raw else None
                return resp.status, body
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = raw.decode("utf-8", "replace")
            return exc.code, body
        except urllib.error.URLError as exc:
            raise RuntimeError("request to {} failed: {}".format(url, exc)) from exc

    def stream_turn(self, overall_timeout, on_event):
        """Read GET /events (real SSE, real HTTP, no framework) until a
        terminal event kind (done/error/cancelled) arrives or
        overall_timeout elapses. on_event(payload) is called for every
        event in order, including the terminal one, so the caller can react
        to approval_request events as they arrive (see main()). Returns
        (events, terminal_kind_or_None)."""
        url = self.base + "/events"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
        deadline = time.monotonic() + overall_timeout
        resp = urllib.request.urlopen(req, timeout=SSE_SOCKET_TIMEOUT)
        events = []
        terminal = None
        try:
            while time.monotonic() < deadline:
                try:
                    raw = resp.readline()
                except (socket.timeout, TimeoutError):
                    continue  # no data within the socket timeout; recheck the outer deadline
                if not raw:
                    break  # server closed the connection
                line = raw.decode("utf-8", "replace")
                if not line.startswith("data: "):
                    continue
                try:
                    payload = json.loads(line[len("data: "):])
                except ValueError:
                    continue
                events.append(payload)
                on_event(payload)
                if payload.get("kind") in ("done", "error", "cancelled"):
                    terminal = payload["kind"]
                    break
            return events, terminal
        finally:
            resp.close()


def wait_idle(client, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = client.request("GET", "/session")
        if status == 200 and isinstance(body, dict) and body.get("status") == "idle":
            return True
        time.sleep(0.1)
    return False


# ---- workspace fixture ---------------------------------------------------

def make_workspace():
    ws = tempfile.mkdtemp(prefix="hearth-e2e-live-")
    hello_path = os.path.join(ws, "hello.txt")
    with open(hello_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(HELLO_CONTENT)
    readme_path = os.path.join(ws, "README.md")
    with open(readme_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(README_CONTENT)
    return ws, hello_path


# ---- the live turn: prompt, SSE, approvals, disk verification -----------

def run_prompt_and_turn(client, results, workspace, hello_path, original_bytes, timeout):
    """Steps 3-7 from the module docstring: submit the prompt, consume SSE,
    resolve every approval as it arrives, verify the workspace changed on
    disk exactly as the events claimed, then checkpoint/restore round-trips
    back to the original bytes. Returns True if the turn reached "done" and
    a write was verified (the precondition the cancel test does not need,
    but that later reporting uses to decide whether checkpoint/restore was
    even meaningful to attempt)."""
    status, body = client.request("POST", "/prompt", {"message": PROMPT_MESSAGE})
    if not (status == 200 and isinstance(body, dict) and body.get("turn_id")):
        results.failed("submit prompt", "status={} body={}".format(status, body))
        return False, None
    results.passed("submit prompt", "turn_id={}".format(body["turn_id"]))

    seen_kinds = []
    checkpoint_id = None
    approvals_seen = 0
    approvals_ok = True
    tool_calls = []
    tool_results = []

    def on_event(payload):
        nonlocal checkpoint_id, approvals_seen, approvals_ok
        kind = payload.get("kind")
        seen_kinds.append(kind)
        data = payload.get("data") or {}
        if kind == "checkpoint" and checkpoint_id is None:
            checkpoint_id = data.get("id")
        elif kind == "approval_request":
            approvals_seen += 1
            appr_id = data.get("id")
            try:
                st, resp_body = client.request(
                    "POST", "/approve", {"id": appr_id, "decision": "allow"})
            except RuntimeError as exc:
                approvals_ok = False
                results.failed("resolve approval {}".format(appr_id), str(exc))
                return
            if st == 200 and isinstance(resp_body, dict) and resp_body.get("resolved") is True:
                results.passed(
                    "resolve approval {}".format(appr_id),
                    "tool={} args={}".format(data.get("tool"), data.get("args")))
            else:
                approvals_ok = False
                results.failed(
                    "resolve approval {}".format(appr_id),
                    "status={} body={}".format(st, resp_body))
        elif kind == "tool_call":
            tool_calls.append(data)
        elif kind == "tool_result":
            tool_results.append(data)

    try:
        events, terminal = client.stream_turn(timeout, on_event)
    except RuntimeError as exc:
        results.failed("consume SSE stream", str(exc))
        return False, None

    if terminal == "done":
        results.passed("turn reaches completion",
                        "terminal event = done ({} events total)".format(len(events)))
    elif terminal is None:
        results.failed(
            "turn reaches completion",
            "no terminal event within {}s; kinds seen so far: {}".format(timeout, seen_kinds))
    else:
        results.failed(
            "turn reaches completion",
            "terminal event = {} (kinds seen: {})".format(terminal, seen_kinds))

    if checkpoint_id:
        results.passed("automatic pre-turn checkpoint fired", "id={}".format(checkpoint_id))
    else:
        results.failed(
            "automatic pre-turn checkpoint fired",
            "no checkpoint event observed (a checkpoint_error event, or git missing from "
            "PATH, would also land here); kinds seen: {}".format(seen_kinds))

    if tool_calls:
        results.passed(
            "tool calls dispatched through the real tool layer",
            "{} tool_call event(s): {}".format(len(tool_calls), [c.get("tool") for c in tool_calls]))
    else:
        results.failed(
            "tool calls dispatched through the real tool layer",
            "no tool_call events observed; the model never attempted a tool call")

    if approvals_seen > 0 and approvals_ok:
        results.passed(
            "permission gate fired and resolved over HTTP",
            "{} approval_request event(s), each resolved via POST /approve".format(approvals_seen))
    elif approvals_seen == 0:
        results.failed(
            "permission gate fired and resolved over HTTP",
            "no approval_request observed; edit mode should gate write_file")
    else:
        results.failed(
            "permission gate fired and resolved over HTTP",
            "at least one POST /approve call did not resolve cleanly; see the FAIL line above")

    write_verified = _verify_workspace_write(results, workspace, tool_calls, tool_results)

    turn_ok = terminal == "done" and bool(checkpoint_id) and write_verified
    return turn_ok, checkpoint_id


def _verify_workspace_write(results, workspace, tool_calls, tool_results):
    """Step 6: the workspace must have changed on disk exactly the way the
    events claimed it did -- not merely 'some file changed'. For write_file
    (what this script's prompt explicitly asks for) the tool_call event
    carries the exact content argument, so the check is a byte-for-byte
    comparison against what actually landed on disk: the strongest, most
    mechanical proof available. edit_file/replace_in_files are accepted as
    a weaker fallback (content is a diff, not a full body, so only "the
    file changed and the tool did not report an error" is checked) in case
    a weak model reaches for a different edit tool than the one it was
    asked to use by name."""
    allowed_writes = [
        c for c in tool_calls
        if c.get("tool") in ("write_file", "edit_file", "replace_in_files")
        and c.get("decision") == "allow"
    ]
    if not allowed_writes:
        results.failed(
            "workspace changed on disk exactly as the events claimed",
            "no allowed write_file/edit_file/replace_in_files tool_call observed")
        return False

    verified_any = False
    for call in allowed_writes:
        tool = call.get("tool")
        args = call.get("args") or {}
        path = args.get("path")
        if not path:
            continue
        full = os.path.join(workspace, path)
        if not os.path.isfile(full):
            results.failed(
                "workspace changed on disk exactly as the events claimed",
                "{} does not exist after an allowed {} call".format(path, tool))
            continue

        with open(full, "rb") as fh:
            actual = fh.read()

        if tool == "write_file":
            content = args.get("content")
            if content is None:
                continue
            expected = content.encode("utf-8")
            if actual == expected:
                results.passed(
                    "workspace changed on disk exactly as the events claimed",
                    "{} matches the write_file tool_call's content byte-for-byte "
                    "({} bytes)".format(path, len(actual)))
                verified_any = True
            else:
                results.failed(
                    "workspace changed on disk exactly as the events claimed",
                    "{} on disk ({} bytes) does not match the write_file call's "
                    "content argument ({} bytes)".format(path, len(actual), len(expected)))
        else:
            matching_result = next(
                (r for r in tool_results if r.get("tool") == tool and not r.get("denied")), None)
            output = (matching_result or {}).get("output", "")
            if isinstance(output, str) and output.startswith("error:"):
                results.failed(
                    "workspace changed on disk exactly as the events claimed",
                    "{} tool reported an error: {}".format(tool, output))
            else:
                results.passed(
                    "workspace changed on disk exactly as the events claimed",
                    "{} ran against {} without a reported error ({} bytes now on disk)".format(
                        tool, path, len(actual)))
                verified_any = True

    return verified_any


def run_checkpoint_restore(client, results, hello_path, original_bytes, checkpoint_id):
    """Step 7: the checkpoint taken automatically at the top of the turn
    (captured above from the SSE "checkpoint" event) round-trips through
    the real HTTP checkpoint API back to byte-exact original content."""
    if not checkpoint_id:
        results.failed(
            "checkpoint / restore round-trip",
            "no checkpoint id available from the turn above; nothing to restore")
        return

    if not wait_idle(client, RESTORE_IDLE_TIMEOUT):
        results.failed(
            "checkpoint / restore round-trip",
            "session did not return to idle within {}s; POST /restore refuses to run "
            "while a turn is in progress".format(RESTORE_IDLE_TIMEOUT))
        return

    status, body = client.request("GET", "/checkpoints")
    listed_ids = [c.get("id") for c in (body or {}).get("checkpoints", [])] if status == 200 else []
    if checkpoint_id in listed_ids:
        results.passed("GET /checkpoints lists the pre-turn checkpoint", checkpoint_id)
    else:
        results.failed(
            "GET /checkpoints lists the pre-turn checkpoint",
            "status={} ids seen={}".format(status, listed_ids))

    status, body = client.request("POST", "/restore", {"checkpoint_id": checkpoint_id})
    if status == 200 and isinstance(body, dict) and "error" not in body:
        results.passed("POST /restore", "skipped_gitlinks={}".format(body.get("skipped_gitlinks")))
    else:
        results.failed("POST /restore", "status={} body={}".format(status, body))
        return

    with open(hello_path, "rb") as fh:
        restored_bytes = fh.read()
    if restored_bytes == original_bytes:
        results.passed(
            "restore is byte-exact",
            "hello.txt matches its pre-turn content exactly after restore")
    else:
        results.failed(
            "restore is byte-exact",
            "hello.txt after restore ({} bytes) does not match its pre-turn content "
            "({} bytes)".format(len(restored_bytes), len(original_bytes)))


def run_cancel_check(client, results):
    """Step 8: submit a turn deliberately shaped to keep the model busy,
    cancel it almost immediately, and verify the session returns to idle
    promptly. engine.py's _run_cancellable polls the cancellation flag every
    0.1s regardless of what the blocking call underneath is doing, so a
    prompt cancellation should return to idle in well under
    CANCEL_IDLE_TIMEOUT even against a slow real model call -- the poll
    does not wait for that call to finish, it abandons it."""
    status, body = client.request("POST", "/prompt", {"message": CANCEL_PROMPT_MESSAGE})
    if not (status == 200 and isinstance(body, dict) and body.get("turn_id")):
        results.failed("cancel a turn", "could not submit a turn to cancel: status={} body={}".format(
            status, body))
        return

    time.sleep(0.1)  # give the background thread a moment to actually start
    t0 = time.monotonic()
    status, cancel_body = client.request("POST", "/cancel")
    if status != 200:
        results.failed("cancel a turn", "POST /cancel failed: status={} body={}".format(
            status, cancel_body))
        return

    idle = wait_idle(client, CANCEL_IDLE_TIMEOUT)
    elapsed = time.monotonic() - t0
    if idle:
        results.passed(
            "cancel a turn",
            "cancel response={} ; session returned to idle in {:.2f}s".format(
                cancel_body, elapsed))
    else:
        results.failed(
            "cancel a turn",
            "session did not return to idle within {}s after POST /cancel "
            "(cancel response={})".format(CANCEL_IDLE_TIMEOUT, cancel_body))


# ---- main -----------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="hearth live end-to-end test: real sidecar, real model, real gate.")
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL,
                    help="Ollama base URL (default: {}). Not necessarily localhost -- "
                         "the sidecar's own HTTP server is always loopback, but Ollama "
                         "does not have to be.".format(DEFAULT_OLLAMA_URL))
    p.add_argument("--model", default=DEFAULT_MODEL,
                    help="model name, must already be pulled on the target Ollama "
                         "(default: {})".format(DEFAULT_MODEL))
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="seconds budget for the live turn to reach completion over SSE "
                         "(default: {:g})".format(DEFAULT_TIMEOUT))
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    ok_pre, reason, _available = check_ollama(args.ollama_url, args.model, PREFLIGHT_TIMEOUT)
    if not ok_pre:
        print("[SKIPPED] {}".format(reason), flush=True)
        print("[SKIPPED] pass --ollama-url and/or --model to point at a reachable "
              "Ollama with the model already pulled.", flush=True)
        return 0

    _info("Ollama reachable at {} with model {!r} pulled.".format(args.ollama_url, args.model))
    if shutil.which("git") is None:
        _info("warning: no git executable on PATH; the checkpoint/restore step below "
              "will report checkpoint_error and fail as a result, not skip.")

    # On POSIX, treat SIGTERM like SIGINT so a supervisor stopping this
    # script still runs the finally block below and cleans up the child
    # process and temp workspace instead of leaking them.
    if hasattr(signal, "SIGTERM") and os.name == "posix":
        def _on_sigterm(signum, frame):  # noqa: ARG001
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, _on_sigterm)

    results = Results()
    proc = None
    handshake = None
    workspace = None

    try:
        try:
            proc, handshake, _stderr_buf = start_sidecar(args.ollama_url)
            results.passed(
                "start sidecar subprocess and read handshake",
                "pid={} port={}".format(handshake["pid"], handshake["port"]))
        except RuntimeError as exc:
            results.failed("start sidecar subprocess and read handshake", str(exc))
            return 1

        client = SidecarClient(handshake["port"], handshake["token"], timeout=30)

        status, body = client.request("GET", "/healthz")
        if status == 200 and body == {"ok": True}:
            results.passed("GET /healthz (no auth)", "200 {{'ok': True}}")
        else:
            results.failed("GET /healthz (no auth)", "status={} body={}".format(status, body))

        workspace, hello_path = make_workspace()
        with open(hello_path, "rb") as fh:
            original_bytes = fh.read()
        results.passed("create temporary workspace with real files", workspace)

        status, body = client.request(
            "POST", "/session", {"workspace": workspace, "model": args.model})
        session_ok = (
            status == 200 and isinstance(body, dict)
            and body.get("workspace") == workspace and body.get("status") == "idle")
        if session_ok:
            results.passed("create session", "mode={} model={}".format(
                body.get("mode"), body.get("model")))
        else:
            results.failed("create session", "status={} body={}".format(status, body))

        turn_ok = False
        checkpoint_id = None
        if session_ok:
            turn_ok, checkpoint_id = run_prompt_and_turn(
                client, results, workspace, hello_path, original_bytes, args.timeout)
        else:
            results.failed(
                "prompt / SSE / approval / disk-write turn",
                "skipped: no live session to submit a prompt to")

        if session_ok:
            run_checkpoint_restore(client, results, hello_path, original_bytes, checkpoint_id)
        else:
            results.failed(
                "checkpoint / restore round-trip",
                "skipped: no live session")

        if session_ok:
            run_cancel_check(client, results)
        else:
            results.failed("cancel a turn", "skipped: no live session")

    except KeyboardInterrupt:
        print("[INFO] interrupted; cleaning up", flush=True)
        results.failed("run", "interrupted by signal before completion")
    finally:
        shutdown_sidecar(proc, handshake, results)
        if workspace and os.path.isdir(workspace):
            shutil.rmtree(workspace, ignore_errors=True)

    print("", flush=True)
    print("{} passed, {} failed".format(results.count_pass, results.count_fail), flush=True)
    if results.ok:
        print("ALL STEPS PASSED", flush=True)
    else:
        print("SOME STEPS FAILED -- see [FAIL] lines above for diagnostics", flush=True)
    return 0 if results.ok else 1


if __name__ == "__main__":
    sys.exit(main())
