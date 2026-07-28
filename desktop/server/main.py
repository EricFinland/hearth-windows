#!/usr/bin/env python3
"""hearth desktop sidecar entrypoint.

Binds 127.0.0.1 on an ephemeral port (port 0, then reads back the real port
the OS chose), generates a bearer token with secrets.token_urlsafe(32), and
prints the handshake as exactly one line of JSON on stdout, then flushes:

  {"port": <int>, "token": "<str>", "pid": <int>}

Tauri (or a test harness) reads that one line to learn how to reach the
server. The token is never passed as a command-line argument -- any local
process can read another process's argv (on Windows via a handle to the
process, on Linux via /proc/<pid>/cmdline) -- and it is never printed again
after the handshake line. Never bind 0.0.0.0, and never a fixed port: a
fixed port lets a leftover orphan process from a previous run answer a new
UI's requests as if it were the current session.

Run with no arguments to actually serve:  python main.py
Run the self-test instead of serving:     python main.py --self-test

Restart survival: make_server() also wires up session_state.py so a Session
is durable across a process restart -- see session.py's and
session_state.py's own module docstrings for what is persisted, what
deliberately is not, and why. Two things happen here, in this order, every
time a server is built (unless a caller opts out, e.g. a test that does not
want its own session state on disk):

  1. A real persist hook (session_state.save(session_state.snapshot(...)))
     is handed to SidecarState, which is what actually makes any of this
     durable going forward -- see app.py's SidecarState._persist_if_current
     for why the hook is threaded through app.py rather than called
     directly here.
  2. Before this function returns -- so before the server has served a
     single request -- whatever session_state.load() finds on disk from a
     PRIOR process is rebuilt via session_state.restore_session() and
     adopted as the live session (SidecarState.set_restored_session). A
     turn that was still running when that prior process stopped is never
     resumed; it is marked interrupted in the rebuilt session's own event
     log (see Session.record_restart_interruption). A corrupt or missing
     state file is not an error here: session_state.load() already
     degrades to None on its own, which this function treats exactly like
     "nothing to restore" -- the server still starts, just with no
     inherited session, the same as any other first run.
"""

import json
import os
import secrets
import sys
from http.server import ThreadingHTTPServer

import app as app_mod
import engine as engine_mod
import session_state


def _default_persist_hook(session):
    session_state.save(session_state.snapshot(session))


def make_server(engine_factory=None, host="127.0.0.1", port=0, token=None,
                models_fetcher=None, persist_hook=True, restore_state=True,
                state_loader=None):
    """Build and bind a (server, state) pair. Defaults to an ephemeral port,
    a freshly generated token, and the real agent engine (engine.RealEngine);
    all overridable for tests. Does not start serving -- call
    server.serve_forever() (or use run_until_stop).

    persist_hook: True (the default) wires up the real
    session_state.save/snapshot pair; a callable overrides it (tests use
    this to record calls instead of touching disk); None disables
    persistence entirely (nothing is ever written, and restore_state below
    is skipped) -- used by tests that want a plain in-memory server with no
    session-state side effects at all.

    restore_state: whether to look for and adopt a prior process's
    persisted session before returning. state_loader overrides
    session_state.load itself (tests point this at a fake or a
    pre-populated scratch file); the default reads the real on-disk
    location session_state.state_path() resolves via hearth_paths."""
    token = token or secrets.token_urlsafe(32)
    engine_factory = engine_factory or (lambda: engine_mod.RealEngine())
    if persist_hook is True:
        persist_hook = _default_persist_hook
    elif persist_hook is False:
        persist_hook = None
    state = app_mod.SidecarState(token, engine_factory=engine_factory,
                                 models_fetcher=models_fetcher, persist_hook=persist_hook)
    server = ThreadingHTTPServer((host, port), app_mod.make_handler(state))
    state.port = server.server_address[1]

    if restore_state and persist_hook is not None:
        loader = state_loader or session_state.load
        persisted = None
        try:
            persisted = loader()
        except Exception as exc:  # noqa: BLE001 - a broken loader must not crash startup
            print("[hearth-main] failed to read persisted session state: {}: {}; "
                  "starting with no session".format(type(exc).__name__, exc), file=sys.stderr)
        if persisted:
            try:
                restored = session_state.restore_session(persisted, engine_factory)
            except Exception as exc:  # noqa: BLE001 - a broken restore must not crash startup
                restored = None
                print("[hearth-main] failed to restore persisted session state: {}: {}; "
                      "starting with no session".format(type(exc).__name__, exc), file=sys.stderr)
            if restored is not None:
                state.set_restored_session(restored)

    return server, state


def print_handshake(state, stream=None):
    stream = stream or sys.stdout
    line = json.dumps({"port": state.port, "token": state.token, "pid": os.getpid()})
    stream.write(line + "\n")
    stream.flush()


def main(argv=None):
    server, state = make_server()
    print_handshake(state)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


def _self_test():
    import io
    import shutil
    import tempfile
    import threading
    import time
    import urllib.request

    old_data_dir = os.environ.get("HEARTH_DATA_DIR")
    scratch = tempfile.mkdtemp(prefix="hearth-main-selftest-")
    os.environ["HEARTH_DATA_DIR"] = scratch
    try:
        _self_test_body()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if old_data_dir is None:
            os.environ.pop("HEARTH_DATA_DIR", None)
        else:
            os.environ["HEARTH_DATA_DIR"] = old_data_dir
    return 0


def _self_test_body():
    import io
    import http.client
    import threading
    import time
    import urllib.request

    # Binds 127.0.0.1 on an ephemeral (non-zero, non-fixed) port.
    server, state = make_server()
    try:
        host, port = server.server_address
        assert host == "127.0.0.1", host
        assert port != 0, "server_address must report the real ephemeral port, not 0"
        assert state.port == port

        # A second server picks a different ephemeral port -- proof neither
        # is hardcoded, which matters because a fixed port lets an orphaned
        # process from a previous run answer a new UI's requests.
        server2, state2 = make_server()
        try:
            assert state2.port != 0
            assert state2.port != state.port, "two independent servers must not collide on a fixed port"
            assert state2.token != state.token, "each process gets its own token"
        finally:
            # server2.serve_forever() was never started, so shutdown() would
            # block forever waiting on an internal Event that only gets set
            # from inside serve_forever's loop. server_close() alone is
            # correct here; it just releases the listening socket.
            server2.server_close()

        # Token shape: url-safe, long enough that secrets.token_urlsafe(32) is
        # plausibly what produced it (32 random bytes -> 43 base64url chars).
        assert len(state.token) >= 40, state.token
        assert all(c.isalnum() or c in "-_" for c in state.token), state.token

        # The handshake is exactly one line of JSON, flushed, on stdout --
        # not stderr, not split across lines, not carrying extra prose that
        # would break a caller doing readline() + json.loads().
        buf = io.StringIO()
        print_handshake(state, stream=buf)
        text = buf.getvalue()
        assert text.count("\n") == 1 and text.endswith("\n"), repr(text)
        handshake = json.loads(text.strip())
        assert set(handshake.keys()) == {"port", "token", "pid"}, handshake
        assert handshake["port"] == state.port
        assert handshake["token"] == state.token
        assert handshake["pid"] == os.getpid()

        # The server the handshake describes actually answers.
        t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:{}/healthz".format(state.port), timeout=5
            ) as r:
                assert r.status == 200
                assert json.loads(r.read().decode()) == {"ok": True}
        finally:
            server.shutdown()
            t.join(timeout=5)
    finally:
        try:
            server.server_close()
        except OSError:
            pass

    # === restart survival, end to end: a real server, a real turn parked ===
    # === on a real pending approval, torn down WITHOUT a graceful ==========
    # === shutdown path (only server_close(), never server.shutdown() -- ===
    # === the closest a self-test can get to "the process just died"), =====
    # === then a brand new make_server() against the SAME HEARTH_DATA_DIR ===
    # === (this function's caller already isolated that to a scratch dir) ==
    # === picks the session back up. ==========================================
    def _http(port, token, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            headers = {"Host": "127.0.0.1:{}".format(port), "Authorization": "Bearer " + token,
                      "Content-Type": "application/json"}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    class _GateEngine:
        """A minimal engine matching RealEngine's get_state()/load_state()
        shape closely enough to exercise the real restart path end to end,
        without needing a live Ollama: one user turn, one tool call gated
        on an approval that this test deliberately never resolves."""

        def __init__(self):
            self._messages = None

        def get_state(self):
            if self._messages is None:
                return None
            return {"messages": self._messages, "turn_starts": [0]}

        def load_state(self, state):
            self._messages = state.get("messages")

        def run(self, ctx):
            self._messages = self._messages or [{"role": "system", "content": "sys"}]
            self._messages.append({"role": "user", "content": ctx.message})
            ctx.request_approval("write_file", {"path": "f.txt", "content": "body"})
            # deliberately never reached in this test: the approval above
            # blocks forever (nobody resolves it), standing in for the
            # process dying while a real tool call is still gated.

    server_a, state_a = make_server(engine_factory=lambda: _GateEngine())
    ta = threading.Thread(target=server_a.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    ta.start()
    try:
        status, data = _http(state_a.port, state_a.token, "POST", "/session",
                             body=json.dumps({"workspace": "/tmp/ws-restart-e2e", "model": "m"}))
        assert status == 200, (status, data)
        status, data = _http(state_a.port, state_a.token, "POST", "/prompt",
                             body=json.dumps({"message": "write f.txt for me"}))
        assert status == 200, (status, data)
        sess_a = state_a.get_session()
        deadline = time.monotonic() + 5
        while not sess_a.pending_approvals() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sess_a.pending_approvals(), "approval never arrived before the simulated crash"
    finally:
        # Deliberately no "flush state on the way out" step, no clean
        # session teardown -- just stop serving and close up, the same way
        # a real crash would leave things: whatever was already written to
        # disk by the persist_hook calls that already fired (the turn-start
        # write, then the approval-gate write) is all that gets to survive.
        server_a.shutdown()
        server_a.server_close()
        ta.join(timeout=5)

    server_b, state_b = make_server()
    tb = threading.Thread(target=server_b.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    tb.start()
    try:
        assert state_b.token != state_a.token, "a restart must still mint a brand new token"
        status, data = _http(state_b.port, state_b.token, "GET", "/session")
        assert status == 200, (status, data)
        restored_dict = json.loads(data)
        assert restored_dict["workspace"] == "/tmp/ws-restart-e2e", restored_dict
        assert restored_dict["status"] == "idle", \
            "a restored session must never look like it is still running a turn: {}".format(restored_dict)
        assert restored_dict["turn_id"] is None, restored_dict

        # The conversation survived.
        sess_b = state_b.get_session()
        restored_state = sess_b.engine.get_state()
        assert restored_state is not None
        assert any(m.get("content") == "write f.txt for me" for m in restored_state["messages"]), \
            restored_state

        # The abandoned approval is visibly marked, not silently dropped --
        # and, critically, not resolvable: the old approval id (whatever it
        # was) is gone, and nothing pending exists for a client to answer.
        # GET /events streams the restored history immediately (since=0
        # default) followed by a keep-alive wait for anything new, so read
        # directly off the SSE connection rather than urlopen'ing the whole
        # (open-ended) response.
        conn = http.client.HTTPConnection("127.0.0.1", state_b.port, timeout=5)
        conn.request("GET", "/events", headers={
            "Host": "127.0.0.1:{}".format(state_b.port), "Authorization": "Bearer " + state_b.token})
        resp = conn.getresponse()
        seen_kinds = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            line = resp.readline().decode("utf-8", "replace")
            if not line:
                break
            if line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                seen_kinds.append(payload["kind"])
                if payload["kind"] == "turn_interrupted":
                    break
        conn.close()
        assert "approval_abandoned" in seen_kinds, seen_kinds
        assert "turn_interrupted" in seen_kinds, seen_kinds

        status, data = _http(state_b.port, state_b.token, "POST", "/approve",
                             body=json.dumps({"id": "appr-anything-at-all", "decision": "allow"}))
        assert status == 404, "nothing must be resolvable as an approval after a restart: {}".format(
            (status, data))

        # The restored session is not wedged: a brand new prompt works.
        status, data = _http(state_b.port, state_b.token, "POST", "/prompt",
                             body=json.dumps({"message": "try again"}))
        assert status == 200, (status, data)
    finally:
        server_b.shutdown()
        server_b.server_close()
        tb.join(timeout=5)

    print("hearth-desktop-main self-test OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
