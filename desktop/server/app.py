#!/usr/bin/env python3
"""hearth desktop sidecar HTTP routing.

Endpoints:
  GET  /healthz   no auth; liveness only. Never leaks the token, the
                  workspace path, or any other state -- this is the one
                  route reachable by any local process or web page without a
                  bearer token, so its response must stay boring on purpose.
  POST /session   create or replace the live session: {"workspace", "model",
                  "mode"?}. Replacing drops the previous session's event log
                  and any turn in flight for it.

                  SECURITY: "mode" accepts "plan", "edit", or "auto" only.
                  "bypass" is rejected with 400 -- permissions.decide still
                  implements it (the Linux CLI can still use it directly),
                  but it is not settable over this HTTP transport. In bypass
                  mode, permissions.decide allows every manifest tool with no
                  approval gate, and run_command is not contained by the
                  workspace at all (the workspace is only the subprocess cwd,
                  which is not a security boundary): one authenticated
                  request would otherwise turn a bearer token into
                  unconfirmed arbitrary code execution anywhere the OS user
                  can reach. Nothing in the product needs that reachable
                  remotely yet; a future desktop shell can grant it through a
                  deliberate local action instead of an HTTP field.

                  SECURITY: "workspace" is caller-controlled and NOT
                  restricted to any particular root -- there is no "projects
                  root" UI concept yet to restrict it to. Whoever holds the
                  bearer token can point a session at any directory the OS
                  user can reach. File tools (read_file, write_file, etc.)
                  are contained to that workspace via
                  hearth_contain.safe_join; run_command is NOT contained by
                  it in any mode -- the workspace is only the subprocess's
                  cwd, and `cd ..` (or an absolute path) reaches anywhere the
                  OS user can. This is an accepted, documented exposure, not
                  an oversight: closing it needs a workspace allowlist tied
                  to a real UI concept, which does not exist yet.

                  Replacing a session whose old session is still busy (a
                  turn running, or a previous turn's cancelled-but-abandoned
                  worker still executing -- see Session.is_workspace_busy)
                  AND targets the exact same workspace directory is refused
                  with 409, so a still-writing abandoned worker from the old
                  session can never race the new session's own turns in the
                  same directory. A different workspace is always accepted
                  immediately; the old session (and its abandoned worker, if
                  any) simply keeps running against its own, different,
                  workspace path, unaffected.
  GET  /session   the current session's state, or 404 if none exists yet.
  POST /prompt    submit a user turn: {"message"}. Returns {"turn_id"}
                  immediately; the turn runs on a background thread.
  GET  /events    a Server-Sent Events stream of turn events (token deltas,
                  tool calls, approval requests, completion, error). Accepts
                  ?since=<event id> or a Last-Event-ID header to resume.
  POST /approve   resolve a pending approval: {"id", "decision": "allow"|
                  "deny"}.
  POST /cancel    interrupt the running turn, if any.
  GET  /models    what Ollama has pulled locally (best-effort; [] if Ollama
                  is unreachable), plus the shop's catalog with fit verdicts
                  for this machine, so a UI can show what will actually run.
  GET  /checkpoints  the current session's checkpoint history, newest first.
  POST /restore   revert the current session's workspace to a prior
                  checkpoint: {"checkpoint_id"}. The response is whatever
                  hearth_checkpoint.restore() reports, unmodified -- in
                  particular "skipped_gitlinks" is always passed through, so
                  a UI can never present a partial restore as a complete one.

                  Refused with 409 whenever Session.is_workspace_busy() is
                  true: not only while a turn is actively running, but also
                  while a previous turn's cancelled-but-abandoned worker
                  (see engine.py's _run_cancellable) is still executing.
                  Cancellation flips `status` back to idle within about a
                  second while that abandoned call can keep writing to the
                  workspace for as long as 1800s (run_command) behind it; a
                  restore that only checked `status` could run `git
                  read-tree -u --reset` while that worker was still mutating
                  the same files underneath it.

Every route except GET /healthz requires, in this order: a valid Host header
(the DNS-rebinding defence), a valid Origin header when one is present, and a
valid bearer token. See auth.py for why and how; this module only wires
those checks into the request lifecycle.

Standard library only: http.server + ThreadingHTTPServer, no framework.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import engine as engine_mod
import session as session_mod


MAX_SSE_CONNECTIONS = 16  # bound on concurrent GET /events streams; see acquire_sse_slot


class WorkspaceBusyError(RuntimeError):
    """Raised by SidecarState.create_session when the requested workspace is
    the same directory as the session being replaced, and that session is
    still busy (Session.is_workspace_busy(): a turn running, or a previous
    turn's cancelled-but-abandoned worker still executing). Replacing blind
    here would let the old session's abandoned worker race the new
    session's own writes in the same directory -- the same hazard POST
    /restore has, just via a different route. _post_session in
    SidecarHandler turns this into a 409."""


class SidecarState:
    """Shared state behind every request. One instance per server process;
    ThreadingHTTPServer runs each request on its own thread, so mutation of
    `session` goes through a lock rather than relying on request isolation.
    """

    def __init__(self, token, engine_factory=None, port=0, models_fetcher=None,
                 max_sse_connections=None):
        self.token = token
        self.port = port  # main.py sets this to the real bound port after listen
        self.engine_factory = engine_factory or (lambda: session_mod.NullEngine())
        # Best-effort GET /models data source. Injectable so tests never need
        # a live Ollama; defaults to the real one for production use.
        self.models_fetcher = models_fetcher or engine_mod.list_installed_models
        self._lock = threading.Lock()
        self.session = None
        # ThreadingHTTPServer spawns one thread per connection, and GET
        # /events holds its connection open for as long as the client keeps
        # reading (or forever, if it never reads). A client opening many and
        # reading none would accumulate threads without bound, so concurrent
        # SSE streams are capped; see acquire_sse_slot/release_sse_slot.
        self.max_sse_connections = max_sse_connections or MAX_SSE_CONNECTIONS
        self._sse_count = 0

    def get_session(self):
        with self._lock:
            return self.session

    def acquire_sse_slot(self):
        """True and reserves a slot if under max_sse_connections, else False
        (no slot reserved). Always paired with release_sse_slot in a
        finally, from _get_events."""
        with self._lock:
            if self._sse_count >= self.max_sse_connections:
                return False
            self._sse_count += 1
            return True

    def release_sse_slot(self):
        with self._lock:
            self._sse_count = max(0, self._sse_count - 1)

    def create_session(self, workspace, model, mode):
        """Build a fresh Session and make it the live one. If a turn is still
        running on the session being replaced, cancel it first -- otherwise
        its background thread would keep running with nothing left able to
        reach it (POST /cancel and GET /events only ever see the current
        `self.session`), silently orphaning it.

        But if the new workspace is the exact same directory as the one
        being replaced, and that old session is still busy (running, or a
        cancelled turn's abandoned worker still executing against it),
        replacing it is refused with WorkspaceBusyError instead: cancelling
        and moving on would leave the abandoned worker free to race the new
        session's own turns in that same directory. A different workspace
        is always safe to replace immediately -- the old session's abandoned
        worker, if any, keeps running against its own, different, path."""
        with self._lock:
            old = self.session
        if old is not None:
            try:
                same_workspace = os.path.realpath(workspace) == os.path.realpath(old.workspace)
            except (OSError, ValueError):
                same_workspace = False
            if same_workspace and old.is_workspace_busy():
                raise WorkspaceBusyError(
                    "cannot replace session: this workspace has running or abandoned "
                    "work in progress; wait for it to finish and try again")
            old.cancel()
        s = session_mod.Session(workspace, model, mode, engine=self.engine_factory())
        with self._lock:
            self.session = s
        return s


class SidecarHandler(BaseHTTPRequestHandler):
    """Set `state = <SidecarState instance>` on a subclass before handing it
    to ThreadingHTTPServer; see make_handler()."""

    state = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep stdout clean: the handshake line is the only thing main.py prints there

    # ---- request helpers ----

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _authorized(self):
        """The full security gate for every route except /healthz. Writes
        the error response itself on failure; callers do
        `if not self._authorized(): return`."""
        if not auth.check_host(self.headers.get("Host"), self.state.port):
            self._send_json(403, {"error": "forbidden_host"})
            return False
        if not auth.check_origin(self.headers.get("Origin"), self.state.port):
            self._send_json(403, {"error": "forbidden_origin"})
            return False
        if not auth.check_bearer(self.headers.get("Authorization"), self.state.token):
            self._send_json(401, {"error": "unauthorized"})
            return False
        return True

    def _path(self):
        return self.path.split("?", 1)[0]

    def _query(self):
        parts = self.path.split("?", 1)
        if len(parts) < 2:
            return {}
        out = {}
        for kv in parts[1].split("&"):
            if not kv:
                continue
            k, _, v = kv.partition("=")
            out[k] = v
        return out

    # ---- routing ----

    def do_GET(self):
        path = self._path()
        if path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        if not self._authorized():
            return
        if path == "/session":
            self._get_session()
        elif path == "/events":
            self._get_events()
        elif path == "/models":
            self._get_models()
        elif path == "/checkpoints":
            self._get_checkpoints()
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        if not self._authorized():
            return
        path = self._path()
        if path == "/session":
            self._post_session()
        elif path == "/prompt":
            self._post_prompt()
        elif path == "/approve":
            self._post_approve()
        elif path == "/cancel":
            self._post_cancel()
        elif path == "/restore":
            self._post_restore()
        else:
            self._send_json(404, {"error": "not_found"})

    # ---- handlers ----

    def _get_session(self):
        s = self.state.get_session()
        if s is None:
            self._send_json(404, {"error": "no_session"})
            return
        self._send_json(200, s.to_dict())

    def _post_session(self):
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "invalid_json"})
            return
        workspace = body.get("workspace")
        model = body.get("model")
        mode = body.get("mode") or session_mod.DEFAULT_MODE
        if not isinstance(workspace, str) or not workspace:
            self._send_json(400, {"error": "workspace is required"})
            return
        if not isinstance(model, str) or not model:
            self._send_json(400, {"error": "model is required"})
            return
        # "bypass" is a settled product decision, not an oversight: see the
        # module docstring. permissions.py keeps supporting it for the Linux
        # CLI; this transport simply never accepts it as an input.
        if mode == "bypass":
            self._send_json(400, {"error": "mode 'bypass' cannot be set over HTTP"})
            return
        try:
            s = self.state.create_session(workspace, model, mode)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except WorkspaceBusyError as exc:
            self._send_json(409, {"error": str(exc)})
            return
        self._send_json(200, s.to_dict())

    def _post_prompt(self):
        s = self.state.get_session()
        if s is None:
            self._send_json(400, {"error": "no_session"})
            return
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "invalid_json"})
            return
        message = body.get("message")
        if not isinstance(message, str) or not message:
            self._send_json(400, {"error": "message is required"})
            return
        try:
            turn_id = s.submit_prompt(message)
        except RuntimeError as exc:
            self._send_json(409, {"error": str(exc)})
            return
        self._send_json(200, {"turn_id": turn_id})

    def _post_approve(self):
        s = self.state.get_session()
        if s is None:
            self._send_json(400, {"error": "no_session"})
            return
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "invalid_json"})
            return
        approval_id = body.get("id")
        decision = body.get("decision")
        if not isinstance(approval_id, str) or decision not in ("allow", "deny"):
            self._send_json(400, {"error": "id and decision (allow|deny) are required"})
            return
        ok = s.resolve_approval(approval_id, decision == "allow")
        if not ok:
            self._send_json(404, {"error": "unknown_or_already_resolved_approval"})
            return
        self._send_json(200, {"resolved": True})

    def _post_cancel(self):
        s = self.state.get_session()
        if s is None:
            self._send_json(400, {"error": "no_session"})
            return
        cancelled = s.cancel()
        self._send_json(200, {"cancelled": cancelled})

    def _get_events(self):
        s = self.state.get_session()
        if s is None:
            self._send_json(404, {"error": "no_session"})
            return
        q = self._query()
        since = 0
        if "since" in q:
            try:
                since = int(q["since"])
            except ValueError:
                since = 0
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id:
            try:
                since = max(since, int(last_event_id))
            except ValueError:
                pass

        # Bound concurrent SSE connections: ThreadingHTTPServer spawns one
        # thread per connection and this handler holds its thread for as
        # long as the client keeps the connection open (or forever, if it
        # never reads), so a client opening many and reading none would
        # otherwise accumulate threads without limit.
        if not self.state.acquire_sse_slot():
            self._send_json(429, {"error": "too_many_event_streams"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    events = s.events_after(since, timeout=15)
                    if not events:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    for ev in events:
                        since = ev["id"]
                        self._write_sse(ev)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                return
        finally:
            self.state.release_sse_slot()

    def _get_models(self):
        """Best-effort: an unreachable Ollama must not take the whole route
        down, since the shop's catalog verdicts (computed purely from local
        hardware, no Ollama involved) are still useful on their own."""
        try:
            installed = self.state.models_fetcher()
        except Exception:  # noqa: BLE001 - a fetcher bug must not break this route
            installed = []
        try:
            catalog = engine_mod.hearth_shop.catalog_with_verdicts()
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": "catalog_failed: {}".format(exc)})
            return
        self._send_json(200, {"installed": installed, "catalog": catalog})

    def _get_checkpoints(self):
        s = self.state.get_session()
        if s is None:
            self._send_json(404, {"error": "no_session"})
            return
        try:
            checkpoints = engine_mod.hearth_checkpoint.list_checkpoints(s.workspace)
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": "checkpoint_list_failed: {}".format(exc)})
            return
        self._send_json(200, {"checkpoints": checkpoints})

    def _post_restore(self):
        s = self.state.get_session()
        if s is None:
            self._send_json(400, {"error": "no_session"})
            return
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "invalid_json"})
            return
        checkpoint_id = body.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            self._send_json(400, {"error": "checkpoint_id is required"})
            return
        if s.is_workspace_busy():
            self._send_json(409, {
                "error": "cannot restore while the workspace has running or abandoned work "
                         "in progress; wait for it to finish and try again"})
            return
        try:
            result = engine_mod.hearth_checkpoint.restore(s.workspace, checkpoint_id)
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": "restore_failed: {}".format(exc)})
            return
        # Pass the module's own result straight through, unmodified: it
        # already reports "skipped_gitlinks" and, on failure, an "error" key.
        # This handler must never add a false note of completeness on top of
        # what hearth_checkpoint itself is willing to claim.
        if "error" in result:
            self._send_json(409, result)
            return
        self._send_json(200, result)

    def _write_sse(self, ev):
        payload = json.dumps({"turn_id": ev["turn_id"], "kind": ev["kind"],
                              "data": ev["data"], "ts": ev["ts"]})
        chunk = "id: {}\nevent: {}\ndata: {}\n\n".format(ev["id"], ev["kind"], payload)
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()


def make_handler(state):
    """Bind `state` to a fresh SidecarHandler subclass. ThreadingHTTPServer
    instantiates the handler class itself per request, so the state has to
    reach it as a class attribute rather than through __init__."""
    return type("BoundSidecarHandler", (SidecarHandler,), {"state": state})


def _self_test():
    import http.client
    import time
    import urllib.request

    class _FakeEngine:
        """Drives one scripted turn: a delta, a gated tool call, then done.
        Stands in for the real engine (hearth_loop + permissions.decide)
        this seam is built for; see the session.py module docstring."""

        def run(self, ctx):
            ctx.emit("delta", {"text": "thinking"})
            decision = ctx.request_approval("write_file", {"path": "note.txt"})
            ctx.emit("tool_call", {"tool": "write_file", "decision": decision})
            ctx.emit("done", {})

    class _StubbornEngine:
        """Loops until cancelled, so /cancel has something real to interrupt."""

        def run(self, ctx):
            while not ctx.cancelled():
                time.sleep(0.02)
            ctx.emit("cancelled", {})

    def _start(token="the-real-token-value", engine_factory=None, models_fetcher=None):
        state = SidecarState(token, engine_factory=engine_factory, models_fetcher=models_fetcher)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        state.port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        return server, state

    def _raw_request(port, method, path, headers=None, body=None):
        """Full control over headers (including Host), for exercising the
        Host-header check the way http.client and urllib would otherwise
        paper over by always sending a correct Host automatically."""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data
        finally:
            conn.close()

    server, state = _start(engine_factory=lambda: _FakeEngine())
    port = state.port
    token = state.token
    try:
        # === the four non-negotiable security assertions ===

        # 1. GET /healthz needs no auth, and leaks nothing.
        with urllib.request.urlopen("http://127.0.0.1:{}/healthz".format(port), timeout=5) as r:
            assert r.status == 200
            body = json.loads(r.read().decode())
        assert body == {"ok": True}, body
        raw_text = json.dumps(body)
        assert token not in raw_text
        assert "workspace" not in raw_text

        # 2. No bearer token at all -> 401.
        status, _ = _raw_request(port, "GET", "/session",
                                  headers={"Host": "127.0.0.1:{}".format(port)})
        assert status == 401, status

        # 3. Wrong bearer token -> 401.
        status, _ = _raw_request(port, "GET", "/session", headers={
            "Host": "127.0.0.1:{}".format(port),
            "Authorization": "Bearer not-the-token",
        })
        assert status == 401, status

        # 4. Bad Host header (the DNS-rebinding case: an attacker-controlled
        #    hostname that still resolves to 127.0.0.1 on the wire) -> 403,
        #    even with a correct bearer token.
        status, _ = _raw_request(port, "GET", "/session", headers={
            "Host": "evil.example.com:{}".format(port),
            "Authorization": "Bearer " + token,
        })
        assert status == 403, status

        # 5. Correct Host + correct token -> succeeds (no session yet -> 404,
        #    which proves the request passed the security gate and reached
        #    the route handler, as opposed to being rejected upstream).
        status, _ = _raw_request(port, "GET", "/session", headers={
            "Host": "127.0.0.1:{}".format(port),
            "Authorization": "Bearer " + token,
        })
        assert status == 404, status  # no session created yet

        auth_headers = {
            "Host": "127.0.0.1:{}".format(port),
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }

        # A bad Origin is rejected even with a good Host and token.
        bad_origin_headers = dict(auth_headers, Origin="http://evil.example.com:{}".format(port))
        status, _ = _raw_request(port, "GET", "/session", headers=bad_origin_headers)
        assert status == 403, status

        # A matching Origin is accepted.
        good_origin_headers = dict(auth_headers, Origin="http://127.0.0.1:{}".format(port))
        status, _ = _raw_request(port, "GET", "/session", headers=good_origin_headers)
        assert status == 404, status  # still no session; proves it passed the gate

        # === session lifecycle ===

        status, data = _raw_request(port, "POST", "/session", headers=auth_headers,
                                    body=json.dumps({"workspace": "/tmp/ws", "model": "qwen2.5-coder"}))
        assert status == 200, (status, data)
        created = json.loads(data)
        assert created == {"workspace": "/tmp/ws", "model": "qwen2.5-coder", "mode": "edit",
                           "status": "idle", "turn_id": None}, created

        status, data = _raw_request(port, "GET", "/session", headers=auth_headers)
        assert status == 200
        assert json.loads(data)["workspace"] == "/tmp/ws"

        # missing workspace/model -> 400
        status, data = _raw_request(port, "POST", "/session", headers=auth_headers,
                                    body=json.dumps({"model": "m"}))
        assert status == 400, (status, data)

        # === prompt -> events -> approve -> done, over real SSE ===

        status, data = _raw_request(port, "POST", "/prompt", headers=auth_headers,
                                    body=json.dumps({"message": "please write a file"}))
        assert status == 200, (status, data)
        turn_id = json.loads(data)["turn_id"]
        assert turn_id

        req = urllib.request.Request(
            "http://127.0.0.1:{}/events".format(port),
            headers={"Authorization": "Bearer " + token, "Host": "127.0.0.1:{}".format(port)},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        seen_kinds = []
        approval_id = None
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                line = resp.readline().decode("utf-8", "replace")
                if not line:
                    break
                if line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                    seen_kinds.append(payload["kind"])
                    if payload["kind"] == "approval_request":
                        approval_id = payload["data"]["id"]
                        break
        finally:
            pass
        assert approval_id, "approval_request never arrived over SSE: {}".format(seen_kinds)

        # resolve it via POST /approve
        status, data = _raw_request(port, "POST", "/approve", headers=auth_headers,
                                    body=json.dumps({"id": approval_id, "decision": "allow"}))
        assert status == 200, (status, data)

        # resolving again is a 404 (already resolved), not a silent success
        status, data = _raw_request(port, "POST", "/approve", headers=auth_headers,
                                    body=json.dumps({"id": approval_id, "decision": "allow"}))
        assert status == 404, (status, data)

        # keep reading SSE until "done" arrives
        while time.monotonic() < deadline:
            line = resp.readline().decode("utf-8", "replace")
            if not line:
                break
            if line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                seen_kinds.append(payload["kind"])
                if payload["kind"] == "done":
                    break
        resp.close()
        assert "tool_call" in seen_kinds and "done" in seen_kinds, seen_kinds

        # === cancel ===

        server2, state2 = _start(engine_factory=lambda: _StubbornEngine())
        try:
            port2, token2 = state2.port, state2.token
            headers2 = {"Host": "127.0.0.1:{}".format(port2), "Authorization": "Bearer " + token2,
                       "Content-Type": "application/json"}
            status, _ = _raw_request(port2, "POST", "/session", headers=headers2,
                                     body=json.dumps({"workspace": "/tmp/ws2", "model": "m"}))
            assert status == 200
            status, data = _raw_request(port2, "POST", "/prompt", headers=headers2,
                                        body=json.dumps({"message": "hang forever"}))
            assert status == 200, data
            time.sleep(0.1)  # let the stubborn engine actually start looping

            # POST /restore must refuse while a turn is running rather than
            # race a live turn's own file writes.
            status, data = _raw_request(port2, "POST", "/restore", headers=headers2,
                                        body=json.dumps({"checkpoint_id": "deadbeef"}))
            assert status == 409, (status, data)
            assert "running" in json.loads(data)["error"], data

            status, data = _raw_request(port2, "POST", "/cancel", headers=headers2)
            assert status == 200 and json.loads(data)["cancelled"] is True, (status, data)
            # cancelling again once idle reports nothing to cancel
            deadline2 = time.monotonic() + 5
            sess = state2.get_session()
            while sess.to_dict()["status"] != "idle" and time.monotonic() < deadline2:
                time.sleep(0.02)
            status, data = _raw_request(port2, "POST", "/cancel", headers=headers2)
            assert status == 200 and json.loads(data)["cancelled"] is False, (status, data)

            # replacing a session with a turn still running must not orphan
            # the old turn's background thread: nothing reachable over HTTP
            # after the replacement could ever cancel it otherwise, since
            # POST /cancel and GET /events only ever see the current session.
            status, data = _raw_request(port2, "POST", "/prompt", headers=headers2,
                                        body=json.dumps({"message": "hang forever again"}))
            assert status == 200, data
            time.sleep(0.1)
            old_session = state2.get_session()
            assert old_session.to_dict()["status"] == "running"
            status, data = _raw_request(port2, "POST", "/session", headers=headers2,
                                        body=json.dumps({"workspace": "/tmp/ws2b", "model": "m"}))
            assert status == 200, data
            deadline3 = time.monotonic() + 5
            while old_session.to_dict()["status"] != "idle" and time.monotonic() < deadline3:
                time.sleep(0.02)
            assert old_session.to_dict()["status"] == "idle", \
                "replacing the session must cancel the old one's running turn, not orphan it"
        finally:
            server2.shutdown()
            server2.server_close()

        # === Finding 3: "bypass" is not settable over HTTP =====================
        # === plan / edit / auto are; the default stays edit ====================

        server_modes, state_modes = _start(engine_factory=lambda: _FakeEngine())
        try:
            port_m, token_m = state_modes.port, state_modes.token
            headers_m = {"Host": "127.0.0.1:{}".format(port_m), "Authorization": "Bearer " + token_m,
                        "Content-Type": "application/json"}

            status, data = _raw_request(port_m, "POST", "/session", headers=headers_m,
                                        body=json.dumps({"workspace": "/tmp/ws-bypass",
                                                         "model": "m", "mode": "bypass"}))
            assert status == 400, (status, data)
            assert "bypass" in json.loads(data)["error"], data
            # the rejected request must never have taken effect
            status, data = _raw_request(port_m, "GET", "/session", headers=headers_m)
            assert status == 404, (status, data)  # still no session at all

            for allowed_mode in ("plan", "edit", "auto"):
                status, data = _raw_request(port_m, "POST", "/session", headers=headers_m,
                                            body=json.dumps({"workspace": "/tmp/ws-" + allowed_mode,
                                                             "model": "m", "mode": allowed_mode}))
                assert status == 200, (allowed_mode, status, data)
                assert json.loads(data)["mode"] == allowed_mode, (allowed_mode, data)

            # omitting mode entirely still defaults to edit
            status, data = _raw_request(port_m, "POST", "/session", headers=headers_m,
                                        body=json.dumps({"workspace": "/tmp/ws-default", "model": "m"}))
            assert status == 200, (status, data)
            assert json.loads(data)["mode"] == "edit", data
        finally:
            server_modes.shutdown()
            server_modes.server_close()

        # === Finding 1, the actual pin: cancel a turn whose tool call is =====
        # === still running (via the REAL RealEngine, not a cooperative fake =
        # === that checks ctx.cancelled() itself), confirm status reports ====
        # === idle, then confirm POST /restore does NOT proceed as though ====
        # === the workspace were quiet =========================================

        def _f1_checkpoint(workspace, label=None, timestamp=None):
            return {"id": "cp-f1", "label": label, "file_count": 0, "sub_repos": [], "warning": None}

        f1_tool_release = threading.Event()

        def _f1_slow_tool(name, args, workspace):
            f1_tool_release.wait(timeout=10)
            return "finished after being abandoned"

        def _f1_chat(messages):
            # mode "auto" + auto_allow=("sleepforever",) reaches an "allow"
            # verdict for run_command without a gate -- this deliberately
            # exercises the actually-dangerous case the review named
            # (run_command, unbounded up to 1800s), while going through the
            # real POST /session HTTP endpoint (which can no longer be asked
            # for "bypass" at all, per Finding 3).
            return ({"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "run_command", "arguments": {"command": "sleepforever --now"}}}]}, 1, 1)

        f1_engine = engine_mod.RealEngine(chat_fn=_f1_chat, execute_tool_fn=_f1_slow_tool,
                                          checkpoint_fn=_f1_checkpoint, auto_allow=("sleepforever",))
        server_f1, state_f1 = _start(engine_factory=lambda: f1_engine)
        import shutil as _shutil_f1
        import tempfile as _tempfile_f1
        ws_f1 = _tempfile_f1.mkdtemp(prefix="hearth-app-f1-")
        try:
            port_f1 = state_f1.port
            headers_f1 = {"Host": "127.0.0.1:{}".format(port_f1),
                         "Authorization": "Bearer " + state_f1.token,
                         "Content-Type": "application/json"}
            status, data = _raw_request(port_f1, "POST", "/session", headers=headers_f1,
                                        body=json.dumps({"workspace": ws_f1, "model": "m", "mode": "auto"}))
            assert status == 200, (status, data)
            status, data = _raw_request(port_f1, "POST", "/prompt", headers=headers_f1,
                                        body=json.dumps({"message": "run something that hangs"}))
            assert status == 200, (status, data)

            sess_f1 = state_f1.get_session()
            deadline_f1 = time.monotonic() + 5
            while sess_f1.to_dict()["status"] != "running" and time.monotonic() < deadline_f1:
                time.sleep(0.01)
            time.sleep(0.1)  # let the worker thread actually enter _f1_slow_tool's wait

            status, data = _raw_request(port_f1, "POST", "/cancel", headers=headers_f1)
            assert status == 200 and json.loads(data)["cancelled"] is True, (status, data)

            deadline_f1 = time.monotonic() + 5
            while sess_f1.to_dict()["status"] != "idle" and time.monotonic() < deadline_f1:
                time.sleep(0.01)
            assert sess_f1.to_dict()["status"] == "idle", "turn never reported idle after cancel"

            # THE PIN. status reads idle, but the tool call _f1_slow_tool is
            # still blocked (f1_tool_release has not been set) -- a restore
            # here would run git read-tree -u --reset while that abandoned
            # call could still be writing into the same workspace.
            status, data = _raw_request(port_f1, "POST", "/restore", headers=headers_f1,
                                        body=json.dumps({"checkpoint_id": "cp-f1"}))
            assert status == 409, \
                (status, data, "restore must refuse while an abandoned worker is still live, "
                               "even though status already reads idle")
            err_msg = json.loads(data)["error"]
            assert "abandoned" in err_msg or "progress" in err_msg, data

            # release the abandoned worker and confirm the hazard actually
            # clears -- this is not a permanent wedge, only a live one.
            f1_tool_release.set()
            deadline_f1 = time.monotonic() + 5
            while sess_f1.is_workspace_busy() and time.monotonic() < deadline_f1:
                time.sleep(0.01)
            assert not sess_f1.is_workspace_busy(), "abandoned worker never reported finished"
        finally:
            server_f1.shutdown()
            server_f1.server_close()
            _shutil_f1.rmtree(ws_f1, ignore_errors=True)

        # === Finding 1, session-replacement side: replacing a session that ==
        # === targets the SAME workspace while it is busy (running or has ===
        # === an abandoned worker) is refused with 409, not silently allowed =

        f2_tool_release = threading.Event()

        def _f2_slow_tool(name, args, workspace):
            f2_tool_release.wait(timeout=10)
            return "finished"

        def _f2_chat(messages):
            return ({"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "run_command", "arguments": {"command": "sleepforever --now"}}}]}, 1, 1)

        f2_engine = engine_mod.RealEngine(chat_fn=_f2_chat, execute_tool_fn=_f2_slow_tool,
                                          checkpoint_fn=_f1_checkpoint, auto_allow=("sleepforever",))
        server_f2, state_f2 = _start(engine_factory=lambda: f2_engine)
        ws_f2 = _tempfile_f1.mkdtemp(prefix="hearth-app-f2-")
        ws_f2_other = _tempfile_f1.mkdtemp(prefix="hearth-app-f2-other-")
        try:
            port_f2 = state_f2.port
            headers_f2 = {"Host": "127.0.0.1:{}".format(port_f2),
                         "Authorization": "Bearer " + state_f2.token,
                         "Content-Type": "application/json"}
            status, data = _raw_request(port_f2, "POST", "/session", headers=headers_f2,
                                        body=json.dumps({"workspace": ws_f2, "model": "m", "mode": "auto"}))
            assert status == 200, (status, data)
            status, data = _raw_request(port_f2, "POST", "/prompt", headers=headers_f2,
                                        body=json.dumps({"message": "run something that hangs"}))
            assert status == 200, (status, data)
            sess_f2 = state_f2.get_session()
            deadline_f2 = time.monotonic() + 5
            while sess_f2.to_dict()["status"] != "running" and time.monotonic() < deadline_f2:
                time.sleep(0.01)
            time.sleep(0.1)
            status, data = _raw_request(port_f2, "POST", "/cancel", headers=headers_f2)
            assert status == 200, (status, data)
            deadline_f2 = time.monotonic() + 5
            while sess_f2.to_dict()["status"] != "idle" and time.monotonic() < deadline_f2:
                time.sleep(0.01)
            assert sess_f2.is_workspace_busy() is True, "abandoned worker should still be live here"

            # same workspace, still busy -> refused
            status, data = _raw_request(port_f2, "POST", "/session", headers=headers_f2,
                                        body=json.dumps({"workspace": ws_f2, "model": "m2"}))
            assert status == 409, \
                (status, data, "replacing a session with the SAME workspace while it is busy "
                               "must be refused, not silently allowed")
            # it must genuinely not have replaced anything
            assert state_f2.get_session() is sess_f2, "a refused replacement must not swap the session"

            # a DIFFERENT workspace is always safe to replace immediately,
            # even while the old session is still busy: the abandoned worker
            # keeps running against its own, different, directory.
            status, data = _raw_request(port_f2, "POST", "/session", headers=headers_f2,
                                        body=json.dumps({"workspace": ws_f2_other, "model": "m2"}))
            assert status == 200, (status, data, "a different workspace must not be blocked by "
                                                  "another workspace's abandoned worker")
            assert state_f2.get_session() is not sess_f2

            f2_tool_release.set()  # let the abandoned worker finish, don't leak the thread
        finally:
            server_f2.shutdown()
            server_f2.server_close()
            _shutil_f1.rmtree(ws_f2, ignore_errors=True)
            _shutil_f1.rmtree(ws_f2_other, ignore_errors=True)

        # === minor: GET /events is bounded to state.max_sse_connections; ====
        # === a client opening more than that gets 429, not an accumulating ==
        # === pile of server threads =========================================

        server_sse, state_sse = _start(engine_factory=lambda: _FakeEngine())
        state_sse.max_sse_connections = 2
        try:
            port_sse = state_sse.port
            status, _ = _raw_request(port_sse, "POST", "/session", headers={
                "Host": "127.0.0.1:{}".format(port_sse),
                "Authorization": "Bearer " + state_sse.token,
                "Content-Type": "application/json",
            }, body=json.dumps({"workspace": "/tmp/ws-sse", "model": "m"}))
            assert status == 200, status

            def _open_sse():
                req = urllib.request.Request(
                    "http://127.0.0.1:{}/events".format(port_sse),
                    headers={"Authorization": "Bearer " + state_sse.token,
                            "Host": "127.0.0.1:{}".format(port_sse)})
                return urllib.request.urlopen(req, timeout=10)

            conn1 = _open_sse()
            conn2 = _open_sse()
            deadline_sse = time.monotonic() + 5
            while state_sse._sse_count < 2 and time.monotonic() < deadline_sse:
                time.sleep(0.01)
            assert state_sse._sse_count == 2, "both real connections should have acquired a slot"
            try:
                # a third concurrent stream must be refused, not accepted
                # and left to accumulate a thread forever
                status3, data3 = _raw_request(port_sse, "GET", "/events", headers={
                    "Host": "127.0.0.1:{}".format(port_sse),
                    "Authorization": "Bearer " + state_sse.token,
                })
                assert status3 == 429, (status3, data3)
            finally:
                conn1.close()
                conn2.close()

            # The bound is not a permanent lockout: releasing a slot (exactly
            # what a finished connection's own `finally` does in _get_events)
            # frees it up for the next one. Simulated directly here rather
            # than waiting on the ~15s keep-alive tick the server would
            # otherwise need to notice these particular sockets closed --
            # the mechanism under test is the counter, not socket teardown
            # latency.
            state_sse.release_sse_slot()
            state_sse.release_sse_slot()
            assert state_sse._sse_count == 0
            conn3 = _open_sse()
            conn3.close()
            state_sse.release_sse_slot()
        finally:
            server_sse.shutdown()
            server_sse.server_close()

        # === GET /models: works with an injected fetcher, no live Ollama ===
        # === required, and the catalog survives a broken fetcher too ======

        def _fake_models_fetcher():
            return [{"name": "test-model:1b", "size_bytes": 42, "modified_at": "x", "digest": "y"}]

        server3, state3 = _start(engine_factory=lambda: _FakeEngine(),
                                 models_fetcher=_fake_models_fetcher)
        try:
            port3 = state3.port
            headers3 = {"Host": "127.0.0.1:{}".format(port3),
                       "Authorization": "Bearer " + state3.token}
            status, data = _raw_request(port3, "GET", "/models", headers=headers3)
            assert status == 200, (status, data)
            body = json.loads(data)
            assert body["installed"] == [_fake_models_fetcher()[0]], body
            assert isinstance(body["catalog"], list) and len(body["catalog"]) >= 6, body
            assert all("verdict" in e and "id" in e for e in body["catalog"]), body
        finally:
            server3.shutdown()
            server3.server_close()

        def _broken_fetcher():
            raise RuntimeError("ollama unreachable")

        server3b, state3b = _start(engine_factory=lambda: _FakeEngine(),
                                   models_fetcher=_broken_fetcher)
        try:
            port3b = state3b.port
            headers3b = {"Host": "127.0.0.1:{}".format(port3b),
                        "Authorization": "Bearer " + state3b.token}
            status, data = _raw_request(port3b, "GET", "/models", headers=headers3b)
            assert status == 200, (status, data)  # a broken fetcher must not take the route down
            body = json.loads(data)
            assert body["installed"] == [], body
            assert isinstance(body["catalog"], list) and body["catalog"], body
        finally:
            server3b.shutdown()
            server3b.server_close()

        # === GET /checkpoints and POST /restore, end to end over HTTP ======

        import shutil as _shutil
        import tempfile as _tempfile

        ws_dir = _tempfile.mkdtemp(prefix="hearth-app-selftest-")
        try:
            status, data = _raw_request(port, "POST", "/session", headers=auth_headers,
                                        body=json.dumps({"workspace": ws_dir, "model": "m"}))
            assert status == 200, (status, data)

            # No checkpoint store yet: an empty history, not an error.
            status, data = _raw_request(port, "GET", "/checkpoints", headers=auth_headers)
            assert status == 200, (status, data)
            assert json.loads(data)["checkpoints"] == [], data

            # missing checkpoint_id -> 400
            status, data = _raw_request(port, "POST", "/restore", headers=auth_headers,
                                        body=json.dumps({}))
            assert status == 400, (status, data)

            # an unknown checkpoint id -> 409, and the error response still
            # carries "skipped_gitlinks" (empty here), never a bare "error"
            # someone could mistake for a partial-success shape.
            status, data = _raw_request(port, "POST", "/restore", headers=auth_headers,
                                        body=json.dumps({"checkpoint_id": "d" * 40}))
            assert status == 409, (status, data)
            body = json.loads(data)
            assert "error" in body and "skipped_gitlinks" in body, body

            if engine_mod.hearth_checkpoint.is_git_available():
                a_path = os.path.join(ws_dir, "a.txt")
                with open(a_path, "w") as fh:
                    fh.write("version one")
                cp = engine_mod.hearth_checkpoint.checkpoint(ws_dir, label="selftest")

                status, data = _raw_request(port, "GET", "/checkpoints", headers=auth_headers)
                assert status == 200, (status, data)
                listed = json.loads(data)["checkpoints"]
                assert any(c["id"] == cp["id"] and c["label"] == "selftest" for c in listed), listed

                with open(a_path, "w") as fh:
                    fh.write("DESTROYED")
                status, data = _raw_request(port, "POST", "/restore", headers=auth_headers,
                                            body=json.dumps({"checkpoint_id": cp["id"]}))
                assert status == 200, (status, data)
                body = json.loads(data)
                assert "error" not in body, body
                assert "skipped_gitlinks" in body, \
                    "skipped_gitlinks must always be present, even when empty"
                assert body["skipped_gitlinks"] == [], body
                with open(a_path) as fh:
                    restored_text = fh.read()
                assert restored_text == "version one", \
                    "POST /restore over HTTP did not actually restore the file"
        finally:
            _shutil.rmtree(ws_dir, ignore_errors=True)

        # === malformed JSON body is rejected, not a 500 ===
        status, data = _raw_request(port, "POST", "/session", headers=auth_headers, body="{not json")
        assert status == 400, (status, data)

        # === unknown route -> 404, not a crash ===
        status, _ = _raw_request(port, "GET", "/nope", headers=auth_headers)
        assert status == 404

    finally:
        server.shutdown()
        server.server_close()

    print("hearth-desktop-app self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
