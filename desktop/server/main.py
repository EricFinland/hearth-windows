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
Stop when the shell that started us does: python main.py --watch-parent
Run the self-test instead of serving:     python main.py --self-test

Why --watch-parent exists
-------------------------
This process can be holding a llama-server child with several gigabytes of
VRAM. That child is inside a kill-on-close Windows Job Object owned by this
process (hearth_llama._win_job), so it dies whenever this process dies, by
any means, including a hard kill. The missing link was this process itself:
orphan it and it survives happily, keeping the model resident forever, and
the user's only clue is a fan and a missing 6 GB.

--watch-parent closes that link, using the sidecar's own stdin as a liveness
handle rather than a parent pid (Windows recycles pids, and killing a
healthy sidecar because an unrelated process inherited a number is worse
than the problem) or any platform-specific process handle. Two signals, and
either one shuts the sidecar down: the shell's end of the pipe closing, and
a shell that had been heartbeating going silent. watch_parent's own
docstring says why one signal was not enough, with the measurement that
showed it.

Without the flag, stdin is untouched and nothing about the old behaviour
changes, so a run from a terminal, or from scripts/e2e_live.py, is
unaffected.

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
import threading
import time
from http.server import ThreadingHTTPServer

# This directory has to be importable before the three imports below, and
# there is exactly one way to guarantee that from inside the file itself.
# Running `python main.py` from a checkout gets it for free, because CPython
# prepends a script's own directory to sys.path -- but not when the
# interpreter is in isolated mode, which is the mode the embeddable
# distribution the packaged app ships runs in (verified: a script run by the
# stock embeddable cannot import its own siblings). Doing it here rather
# than in the interpreter's path configuration means the sidecar can be
# started from any directory, by any interpreter, in any layout, which is
# what keeps the packaging layer from having a say in whether the imports
# work. engine.py does the same thing for the agent/ directory next door.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import app as app_mod  # noqa: E402
import engine as engine_mod
import loop_engine as loop_mod
import session_state
import swarm_engine as swarm_mod


def _default_persist_hook(session):
    session_state.save(session_state.snapshot(session))


def restore_engine_factory(persisted, default_factory, state):
    """The engine factory session_state.restore_session should use for THIS
    persisted session.

    A session that was running a work loop has to come back as one. Restoring
    it with the process-wide chat factory would not merely lose a setting: a
    chat engine has no pending_run(), so the loop's journal would sit on disk
    with nothing in the application able to see it, and the user would be
    told nothing at all about the run that was in flight when the machine
    went down.

    The persisted config is re-validated through parse_loop_config, and a
    config that does not pass is replaced by the defaults rather than
    honoured or refused. That is the same suspicion RESTORABLE_MODES applies
    to the persisted mode, for the same reason: this file lives in the data
    directory the agent's own write_file can reach, so it may say what a
    session WAS and never what a session is allowed to be. Falling back to
    the defaults (rather than dropping the session) keeps the run visible,
    which is the point, while denying the file any say in its bounds.

    A swarm is rebuilt on exactly the same terms, and its ROLES are never
    taken from the file: build_swarm_engine derives them from this build's own
    DEFAULT_ROLES, narrowed by the (re-validated) tool manifest. A state file
    that named its own roles could name a second writing role, and the
    single-writer property that keeps two agents from corrupting one
    workspace would become something a file on disk could switch off.
    """
    if not isinstance(persisted, dict):
        return default_factory
    kind = persisted.get("engine_kind")
    if kind not in ("loop", "swarm"):
        return default_factory

    # (module, its mode set, its config parser, its error, its builder, its gauge)
    spec = {
        "loop": (loop_mod.LOOP_MODES, loop_mod.parse_loop_config,
                 loop_mod.ConfigError, loop_mod.build_loop_engine,
                 state.get_loop_status, "work loop"),
        "swarm": (swarm_mod.SWARM_MODES, swarm_mod.parse_swarm_config,
                  swarm_mod.ConfigError, swarm_mod.build_swarm_engine,
                  state.get_swarm_status, "swarm"),
    }[kind]
    modes, parse, config_error, build, status_of, label = spec

    if persisted.get("mode") not in modes:
        # It cannot run in this mode, so do not rebuild one that would refuse
        # its own first prompt. The chat engine restores normally.
        return default_factory
    try:
        config = parse(persisted.get("engine_config"))
    except config_error as exc:
        print("[hearth-main] persisted {} config is not acceptable ({}); "
              "restoring the run with default ceilings instead".format(label, exc),
              file=sys.stderr)
        config = parse({})
    return lambda: build(config, status=status_of())


def start_engine_acquisition(state, stream=None):
    """Kick off the GPU engine fetch, in the background, at startup.

    Hearth's installer bundles llama.cpp's CPU build because it is the only
    Windows artifact that cannot fail to start. The right GPU build is
    fetched afterwards, and this is where "afterwards" begins: as soon as
    the sidecar is up, and on a thread, so the user is chatting on the CPU
    engine within seconds while 33 MB arrives behind them. The swap takes
    effect the next time a model server starts.

    Idempotent and unconditional to call: an installation that already has
    a verified engine settles into "active" without touching the network, a
    machine with no GPU settles into "skipped", and a machine with no
    network settles into "failed" with a message. None of those is a reason
    not to run, and none of them stops the sidecar from serving.

    Never raises. A sidecar that would not start because engine acquisition
    misbehaved would be a strictly worse product than one running on the
    CPU, which is what it is doing anyway while this runs.
    """
    stream = stream or sys.stderr
    try:
        state.get_engine().start()
    except Exception as exc:  # noqa: BLE001 - see the docstring: nothing here
        # is worth failing a launch over.
        print("[hearth-main] could not start GPU engine acquisition: {}: {}".format(
            type(exc).__name__, exc), file=stream, flush=True)
        return False
    return True


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
                restored = session_state.restore_session(
                    persisted, restore_engine_factory(persisted, engine_factory, state))
            except Exception as exc:  # noqa: BLE001 - a broken restore must not crash startup
                restored = None
                print("[hearth-main] failed to restore persisted session state: {}: {}; "
                      "starting with no session".format(type(exc).__name__, exc), file=sys.stderr)
            if restored is not None:
                state.set_restored_session(restored)
                # Put any inherited unfinished run on the gauge NOW, so the
                # first GET /loop a restarted app makes already carries it.
                # Without this the run is only discovered when the user
                # happens to submit something, which is exactly the moment
                # they would already have destroyed the chance to resume it.
                publish = getattr(restored.engine, "publish_pending", None)
                if callable(publish):
                    try:
                        publish()
                    except Exception as exc:  # noqa: BLE001 - never crash startup
                        print("[hearth-main] could not read the work loop journal: "
                              "{}: {}".format(type(exc).__name__, exc), file=sys.stderr)

    return server, state


def print_handshake(state, stream=None):
    stream = stream or sys.stdout
    line = json.dumps({"port": state.port, "token": state.token, "pid": os.getpid()})
    stream.write(line + "\n")
    stream.flush()


#: How long a shutdown triggered by a dead parent is allowed to take before
#: the process leaves anyway. serve_forever's loop exits within one poll
#: interval, so this is generous; the point is that no wedged worker thread
#: can turn "the shell died" into "the model stays resident".
PARENT_EXIT_GRACE = 5.0


#: How long a heartbeating shell may go quiet before it is presumed dead.
#: Only enforced once a first heartbeat has arrived -- see watch_parent.
PARENT_HEARTBEAT_TIMEOUT = 60.0

#: How often the deadline above is checked.
PARENT_HEARTBEAT_POLL = 2.0


def _private_stdin():
    """Take the shell's pipe off file descriptor 0 and return it.

    Descriptor 0 is duplicated to a private descriptor for the watcher, and
    0 itself is repointed at the null device. Every process the sidecar
    starts afterwards -- git, llama-server, a shell tool -- therefore
    inherits the null device as its standard input, and cannot see or touch
    the channel the shell uses to say it is still alive.

    This is not tidiness. Leaving the pipe on descriptor 0 was measured to
    make the first turn take over two minutes instead of five seconds: every
    checkpoint git invocation in hearth_checkpoint inherited the pipe,
    subprocess's reader threads then sat until their timeouts expired, and
    init_store makes several such calls in a row. A thread dump taken during
    the stall showed exactly that, and the same turn with the sidecar's
    stdin untouched completed in five seconds. Beyond the speed, an
    inherited pipe is a correctness problem in its own right: a child that
    reads standard input would eat the shell's heartbeats and eventually
    convince the sidecar its shell had died.

    Falls back to whatever sys.stdin offers if descriptor 0 cannot be
    manipulated (no handle at all, a closed descriptor). The watcher treats
    an unreadable stream as a dead shell, which is the safe direction.
    """
    try:
        private_fd = os.dup(0)
    except OSError:
        return getattr(sys.stdin, "buffer", sys.stdin)
    try:
        null_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(null_fd, 0)
        finally:
            os.close(null_fd)
    except OSError:
        # Descriptor 0 could not be replaced. The watcher still works off
        # its private copy; children just keep inheriting the pipe.
        pass
    return os.fdopen(private_fd, "rb", buffering=0)


def watch_parent(on_gone, stream=None, heartbeat_timeout=PARENT_HEARTBEAT_TIMEOUT,
                 poll=PARENT_HEARTBEAT_POLL, clock=time.monotonic):
    """Call `on_gone` when the shell that started us stops being there.

    Two independent signals, because one of them turned out not to be
    universally reliable.

    1. END OF STREAM. The shell holds our stdin open for as long as it
       lives; when it stops living the OS closes its end and the read below
       returns EOF. This is the fast path and it fires within a second.

       It depends on the shell being the ONLY holder of the write end of
       that pipe, and that is not something the sidecar can enforce.
       Measured on Windows: a parent using CPython's own subprocess module
       leaks an inheritable copy of the write handle into the child, so
       killing that parent produces no EOF at all and the sidecar lives
       forever. A parent using Node (libuv), which is what Hearth's shell
       is, does not, and the sidecar dies within two seconds. Relying on
       the shell's process library to get handle inheritance right is not
       a guarantee, and the thing at stake is several gigabytes of VRAM.

    2. HEARTBEAT SILENCE. A shell may also write a byte periodically. Once
       one has arrived, the sidecar requires another within
       `heartbeat_timeout` and shuts down if none comes. This needs no EOF,
       no parent pid (which Windows recycles, so polling one risks killing
       a healthy sidecar because an unrelated process inherited the
       number), and no platform-specific process handle.

       Enforcement starts only after the first heartbeat, so a caller that
       never sends one -- a terminal, a test harness, scripts/e2e_live.py --
       keeps exactly the old EOF-only behaviour and is never shut down for
       being quiet.

    Bytes are read and discarded. The sidecar has no stdin protocol and
    never will: everything it accepts arrives over the authenticated
    loopback HTTP API. Returns the reader thread, for tests.

    An unreadable stdin (no handle at all, already closed) counts as "the
    shell is gone", which is the safe direction: a sidecar with no shell to
    talk to has nothing to serve.
    """
    stream = _private_stdin() if stream is None else stream
    fired = threading.Event()
    beat = {"seen": False, "at": clock()}

    def fire():
        if fired.is_set():
            return
        fired.set()
        on_gone()

    def read_loop():
        try:
            while True:
                chunk = stream.read(1)
                if not chunk:
                    break
                beat["seen"] = True
                beat["at"] = clock()
        except Exception:  # noqa: BLE001 - any read failure means the pipe is gone
            pass
        fire()

    def deadline_loop():
        while not fired.is_set():
            time.sleep(poll)
            if beat["seen"] and clock() - beat["at"] > heartbeat_timeout:
                fire()
                return

    reader = threading.Thread(target=read_loop, name="hearth-parent-watch", daemon=True)
    reader.start()
    threading.Thread(target=deadline_loop, name="hearth-parent-heartbeat", daemon=True).start()
    return reader



def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    server, state = make_server()

    if "--watch-parent" in argv:
        def _parent_gone():
            print("[hearth-main] the shell closed our stdin; shutting down",
                  file=sys.stderr, flush=True)
            # A hard exit after the grace period, in case shutdown() is
            # blocked behind a request that will not finish. Terminating
            # this process is what releases the job object holding
            # llama-server, so "leave anyway" is strictly better than "hang
            # forever with the model loaded".
            timer = threading.Timer(PARENT_EXIT_GRACE, lambda: os._exit(0))
            timer.daemon = True
            timer.start()
            server.shutdown()

        watch_parent(_parent_gone)

    print_handshake(state)
    # After the handshake, so the shell is already talking to us and the UI
    # can watch GET /engine/events from the very first frame.
    start_engine_acquisition(state)
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


def _self_test_restore_engine_factory():
    """restore_engine_factory rebuilds the RIGHT kind of engine, and believes
    the state file about nothing that grants authority.

    Untested until a swarm engine turned this function from a single `if` into
    a dispatch. A session that comes back as the wrong kind is not a lost
    setting: a chat engine has no pending_run(), so an unfinished run's
    journal sits on disk with nothing in the application able to see it, and
    the user is told nothing at all about what was in flight.
    """
    import app as app_mod  # noqa: PLC0415

    state = app_mod.SidecarState("t")
    sentinel = object()

    def default():
        return sentinel

    def kind_of(persisted):
        got = restore_engine_factory(persisted, default, state)()
        return "default" if got is sentinel else getattr(got, "ENGINE_KIND", "chat")

    # The three engine kinds land on the three factories.
    assert kind_of({"engine_kind": "chat", "mode": "auto"}) == "default"
    assert kind_of({"engine_kind": "loop", "mode": "auto"}) == "loop"
    assert kind_of({"engine_kind": "swarm", "mode": "auto"}) == "swarm"
    assert kind_of({"engine_kind": "nonsense", "mode": "auto"}) == "default"
    assert kind_of(None) == "default"
    assert kind_of("not a dict") == "default"

    # A mode the engine may not run in restores as a chat rather than
    # rebuilding an engine that would refuse its own first prompt. bypass is
    # the one that matters: it must never come back as a loop or a swarm.
    for kind in ("loop", "swarm"):
        for mode in ("bypass", "edit", None, "nonsense"):
            assert kind_of({"engine_kind": kind, "mode": mode}) == "default", (kind, mode)

    # A config the parser refuses falls back to the DEFAULTS rather than being
    # honoured, because this file is writable by the agent's own write_file.
    for kind, ceiling_key in (("loop", "max_turns"), ("swarm", "max_cycles")):
        eng = restore_engine_factory(
            {"engine_kind": kind, "mode": "auto",
             "engine_config": {"ceilings": {ceiling_key: -1}}}, default, state)()
        assert getattr(eng, "ENGINE_KIND", None) == kind, kind
        got = eng.manifest()["ceilings"][ceiling_key]
        assert got > 0, ("a state file must not be able to hand its successor an "
                         "unbounded run", kind, got)

    # A swarm rebuilt from a state file gets THIS BUILD's roles, with exactly
    # one writer, whatever the file says. Roles are not a persisted field at
    # all, and adding one must not silently start working.
    eng = restore_engine_factory(
        {"engine_kind": "swarm", "mode": "auto",
         "engine_config": {"roles": [{"name": "evil", "writes": True}]}},
        default, state)()
    roles = eng.manifest()["roles"]
    assert [r["name"] for r in roles] == ["planner", "implementer", "reviewer"], roles
    assert sum(1 for r in roles if r["writes"]) == 1, roles


def _self_test_body():
    import io
    import http.client
    import threading
    import time
    import urllib.request

    _self_test_restore_engine_factory()

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
        on an approval that this test deliberately never resolves.

        server_b below (the "after the restart" server) uses make_server()'s
        own default engine_factory -- the real engine_mod.RealEngine, not
        this fake -- to prove the persisted state is genuinely
        engine-agnostic JSON, not tied to whatever engine object happened to
        produce it. That only still works now that session_state.py
        validates a persisted conversation's first message against
        whichever engine restores it (see its own Finding 1 fix): this
        engine's own opening system message must therefore actually match
        what RealEngine generates for the session's mode ("edit", the
        default -- see the POST /session body below), not an arbitrary
        placeholder string, or the real restart path would (correctly)
        discard the conversation as unverifiable."""

        def __init__(self):
            self._messages = None

        def get_state(self):
            if self._messages is None:
                return None
            return {"messages": self._messages, "turn_starts": [0]}

        def load_state(self, state):
            self._messages = state.get("messages")

        def expected_system_prompt(self, mode):
            return engine_mod._system_prompt(mode)

        def run(self, ctx):
            self._messages = self._messages or [
                {"role": "system", "content": engine_mod._system_prompt(ctx.mode)}]
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

    # === --watch-parent: the sidecar must not outlive the shell ===========
    # This is the thing standing between a Task-Manager kill of the shell
    # and a llama-server left holding several GB of VRAM until the machine
    # reboots, so it gets a real pipe rather than a mock.
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    fired = threading.Event()
    watcher = watch_parent(fired.set, stream=reader)

    # A parent that has never heartbeated and is merely quiet must NOT
    # trigger it. This is what keeps a terminal run, or e2e_live.py, from
    # being shut down for saying nothing: silence only becomes evidence
    # once the shell has shown it does heartbeat.
    assert not fired.wait(0.3), "the watcher fired while the parent was still alive"

    # Traffic does not trigger it either: there is no stdin protocol, bytes
    # are read and dropped.
    os.write(write_fd, b"noise the sidecar must ignore\n")
    assert not fired.wait(0.3), "the watcher fired on stdin traffic rather than on EOF"

    os.close(write_fd)
    assert fired.wait(5), "closing the parent's end of stdin did not wake the watcher"
    watcher.join(timeout=5)
    assert not watcher.is_alive()
    reader.close()

    # A stdin that cannot be read at all (no handle, already closed) counts
    # as "the parent is gone" rather than as "carry on serving".
    class _DeadStream:
        def read(self, _n):
            raise OSError("the handle is closed")

    fired2 = threading.Event()
    watch_parent(fired2.set, stream=_DeadStream()).join(timeout=5)
    assert fired2.is_set(), "an unreadable stdin must be treated as a dead parent"

    # === descriptor 0 is taken away from every child ======================
    # Leaving the shell's pipe on descriptor 0 was measured to turn a
    # five-second first turn into a two-minute one, because every git
    # invocation hearth_checkpoint makes inherited it and subprocess's
    # reader threads then sat until their timeouts expired. A child must
    # inherit the null device instead, and this is the check that says so.
    r3, w3 = os.pipe()
    saved_stdin = os.dup(0)
    try:
        os.dup2(r3, 0)
        os.close(r3)
        private = _private_stdin()
        # Descriptor 0 no longer refers to the pipe: writing to the pipe
        # must not be readable through descriptor 0, and reading descriptor
        # 0 must give EOF the way the null device does.
        os.write(w3, b"heartbeat\n")
        assert os.read(0, 32) == b"", \
            "descriptor 0 must be the null device after the watcher takes its copy"
        # The watcher's private copy still sees the shell.
        assert private.read(1) == b"h", "the watcher lost the shell's channel"
        private.close()
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(w3)

    # === the second net: a heartbeating shell that goes silent ============
    # A parent can die WITHOUT the pipe ever reporting EOF -- measured on
    # Windows with a CPython subprocess parent, which leaks an inheritable
    # copy of the write handle into the child, so the child holds its own
    # stdin open forever. Nothing here can stop a shell doing that, so the
    # sidecar must not depend on EOF alone. Once a heartbeat has been seen,
    # silence past the deadline means gone.
    r2, w2 = os.pipe()
    reader2 = os.fdopen(r2, "rb", buffering=0)
    fired3 = threading.Event()
    watch_parent(fired3.set, stream=reader2, heartbeat_timeout=0.6, poll=0.05)
    # No heartbeat yet: the deadline must not apply, however long we wait.
    assert not fired3.wait(1.5), \
        "silence from a parent that never heartbeated must not shut the sidecar down"
    os.write(w2, b"\n")            # the shell proves it heartbeats
    time.sleep(0.2)
    os.write(w2, b"\n")            # and keeps its promise once
    assert not fired3.wait(0.3), "a heartbeat inside the deadline must keep the sidecar up"
    # Now it stops. The pipe stays OPEN the whole time, so EOF never comes
    # and only the deadline can catch this.
    assert fired3.wait(5), "a heartbeating shell that went silent was not noticed"
    os.close(w2)
    reader2.close()

    # === GPU engine acquisition is started, and cannot break the launch ===
    class _Acq:
        def __init__(self, boom=False):
            self.started = 0
            self._boom = boom

        def start(self, force=False):  # noqa: ARG002 - signature parity
            self.started += 1
            if self._boom:
                raise RuntimeError("no data directory")
            return {"state": "planning"}

    good = _Acq()
    state_eng = app_mod.SidecarState("tok", engine_acquirer=good)
    assert start_engine_acquisition(state_eng) is True
    assert good.started == 1, good.started

    # An acquirer that throws is reported and swallowed. A sidecar that
    # refused to start because a 33 MB optional download misbehaved would
    # be strictly worse than one running on the CPU build it already has.
    noisy = io.StringIO()
    bad = _Acq(boom=True)
    state_bad = app_mod.SidecarState("tok", engine_acquirer=bad)
    assert start_engine_acquisition(state_bad, stream=noisy) is False
    assert "no data directory" in noisy.getvalue(), noisy.getvalue()

    print("hearth-desktop-main self-test OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
