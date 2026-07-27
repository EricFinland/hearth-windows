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
"""

import json
import os
import secrets
import sys
from http.server import ThreadingHTTPServer

import app as app_mod


def make_server(engine_factory=None, host="127.0.0.1", port=0, token=None):
    """Build and bind a (server, state) pair. Defaults to an ephemeral port
    and a freshly generated token; both are overridable for tests. Does not
    start serving -- call server.serve_forever() (or use run_until_stop)."""
    token = token or secrets.token_urlsafe(32)
    state = app_mod.SidecarState(token, engine_factory=engine_factory)
    server = ThreadingHTTPServer((host, port), app_mod.make_handler(state))
    state.port = server.server_address[1]
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

    print("hearth-desktop-main self-test OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
