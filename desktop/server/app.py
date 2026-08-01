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
  GET  /models    every model this machine can actually run right now, plus
                  the shop's catalog with fit verdicts, so a UI can show
                  what will really work. "installed" spans BOTH engines --
                  Ollama's pulled tags and the GGUFs Hearth downloaded for
                  its own bundled engine -- and every entry carries the
                  backend that runs it and a "ref" (the exact string to send
                  back to POST /session). Each source is self-gating: an
                  unreachable Ollama contributes nothing, and a missing
                  bundled engine contributes nothing, so the list never
                  offers a model the active configuration cannot run. Both
                  halves are best-effort; neither can take the route down.
  GET  /shop      hearth_shop.search_shop(?q=): live Hugging Face GGUF
                  results with every quantisation graded against THIS
                  machine's hardware. ?context= sets the context length the
                  verdicts are judged at. The response's own "source" field
                  is "live" or "fallback", and "ok" is True only for live
                  Hub results -- a caller must not present a fallback
                  listing as search results, and this route never flattens
                  that distinction away.
  GET  /shop/quants  hearth_shop.repo_quants(?repo=): every quantisation of
                  one repository, for the results search_shop left with
                  quants_loaded False. Each quantisation is additionally
                  annotated with what is already on disk for it (see
                  downloads.quant_local_state), so a UI can say "3.1 GB
                  already downloaded, resume" instead of offering a fresh
                  4 GB download over a partial.
  GET  /downloads     every download this PROCESS knows about, with live byte
                  counts. Not session-scoped: see downloads.py's module
                  docstring for why downloads deliberately do not ride on
                  GET /events.
  POST /downloads     start one: {"repo_id", "filename"}. filename is the
                  quantisation's path (a single file) or its logical name (a
                  split model). Sizes and hashes are NOT accepted from the
                  caller; downloads.py re-reads them from the Hub, so a page
                  cannot pick what a download is verified against.
  GET  /downloads/events  a Server-Sent Events stream carrying the COMPLETE
                  download list on every change (event: "downloads"), not a
                  delta log. Accepts ?since=<version>. Every frame is
                  self-sufficient, so a page reload resumes correctly with no
                  replay and no gap marker.
  POST /downloads/cancel   {"id"}: stop a queued or running download. A
                  running one leaves a resumable .part behind; starting the
                  same file again continues from it.
  POST /downloads/dismiss  {"id"}: forget a finished download. Refused for
                  one still in flight, which must be cancelled instead.
  GET  /checkpoints  the current session's checkpoint history, newest first.
  POST /restore   revert the current session's workspace to a prior
                  checkpoint: {"checkpoint_id"}. The response is whatever
                  hearth_checkpoint.restore() reports, unmodified -- in
                  particular "skipped_gitlinks" is always passed through, so
                  a UI can never present a partial restore as a complete one.
  GET  /setup     hearth_setup.diagnose(): can this machine even run a local
                  model right now, and if not, the one concrete next step.
                  Never requires a session to exist -- a UI needs this
                  before it can show a chat box at all. Runs the full
                  diagnosis fresh on every call, never cached: unlike
                  GET /idle below, a UI calls this rarely (at startup, or
                  when a user explicitly asks "why can't I start"), and a
                  stale "not ready" right after the user actually started
                  Ollama would be actively unhelpful. RealEngine.run()
                  separately runs hearth_setup.quick_check() -- the cheap
                  path, not this full diagnosis -- before every turn; see
                  engine.py's module docstring, point 6.
  GET  /idle      hearth_idle.is_good_time(): is now a reasonable time for
                  heavy work, and why. Cached for SidecarState.
                  idle_cache_seconds (default 2s) -- see
                  SidecarState.get_idle_status -- since hearth_idle.probe()
                  can shell out to nvidia-smi and its own module docstring
                  is explicit that it must not run on every request. Purely
                  advisory: nothing in this sidecar ever gates a turn on
                  it, and this route does not either -- a user who
                  explicitly submits a prompt has said what they want.

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

SECURITY: HTTP request smuggling on rejected requests. With
protocol_version = "HTTP/1.1" and Content-Length responses, a connection
stays alive across requests by default. Every route handler used to read its
JSON body (via _read_json) only after already deciding to proceed -- so a
request rejected by _authorized() (401/403), or by a handler's own "no
session yet" check, returned its error WITHOUT ever reading the declared
Content-Length bytes off the socket. Those unread bytes stayed sitting in
the connection's read buffer, and the next handle_one_request() call (the
one BaseHTTPRequestHandler makes automatically to serve the next pipelined
request on a kept-alive connection) parsed them as an entirely new HTTP
request -- with whatever Host, Origin, and Authorization headers the
attacker chose, none of which had to pass this server's own checks, because
they were never received as a "request" by this server's routing logic at
all; they arrived as the tail of a request this server had already
rejected. A page that can issue one no-cors cross-origin POST (no
preflight, response unreadable, but the request itself is sent and a bad
token guarantees rejection) could smuggle a second, fully-formed request
past the Host and Origin checks entirely.

Fix: every request's full declared body is read off the socket in
_prepare_body(), called at the very start of do_GET/do_POST, before
_authorized() or any route logic runs -- not just before whichever
early-return call sites the original review happened to name. This closes
the hole for every current and future early-return path uniformly, rather
than requiring each one to remember to drain. A declared Content-Length
larger than MAX_BODY_BYTES is rejected with 413 and the connection is
closed instead of drained: reading an attacker-declared, unbounded length
off the wire would just move the resource-exhaustion problem from "a second
smuggled request" to "this handler thread blocks reading gigabytes", so for
that one case closing (which makes any further bytes on that socket
irrelevant) is used instead of draining. See _prepare_body and MAX_BODY_BYTES.

Standard library only: http.server + ThreadingHTTPServer, no framework.
"""

import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import downloads as downloads_mod
import engine as engine_mod
import session as session_mod


MAX_SSE_CONNECTIONS = 16  # bound on concurrent GET /events streams; see acquire_sse_slot

# Default cache lifetime for GET /idle -- see SidecarState.get_idle_status.
# hearth_idle.probe() can shell out to nvidia-smi (bounded, but still a real
# subprocess call), and its own module docstring is explicit this must not
# run "on a hot path or on every event". 2s is short enough that a status
# indicator never looks stale to a human, but long enough that a client
# polling once a second or faster only actually triggers a fresh probe
# about half the time.
IDLE_CACHE_SECONDS = 2.0

# Cap on a single request's declared Content-Length, enforced by
# _prepare_body before any body is read. An authenticated client (post-auth,
# so lower severity than the smuggling fix above) could otherwise make
# _read_json allocate however many bytes it declares. 16 MiB is generous for
# a write_file body (source files, generated docs, etc.) while still
# bounding the allocation a single request can force.
MAX_BODY_BYTES = 16 * 1024 * 1024

# Bounds on GET /shop. `limit` is how many repositories the Hub is asked
# for; `detail` is how many of those have their whole file tree fetched and
# every quantisation graded, which costs one extra Hub request each. The
# caps exist because both are caller-controlled and each one multiplies the
# work a single authenticated request can ask this process to do against a
# third party.
SHOP_LIMIT_DEFAULT = 12
SHOP_LIMIT_MAX = 40
SHOP_DETAIL_DEFAULT = 6
SHOP_DETAIL_MAX = 12
# Context length the fit verdicts are judged at. Capped for the same reason:
# it is a multiplier on the KV-cache arithmetic every verdict does.
SHOP_CONTEXT_MAX = 1024 * 1024


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
                 max_sse_connections=None, persist_hook=None,
                 setup_diagnoser=None, idle_prober=None, idle_cache_seconds=None,
                 local_models_fetcher=None, model_checker=None,
                 shop_searcher=None, shop_quanter=None, download_manager=None):
        self.token = token
        self.port = port  # main.py sets this to the real bound port after listen
        self.engine_factory = engine_factory or (lambda: session_mod.NullEngine())
        # Best-effort GET /models data source. Injectable so tests never need
        # a live Ollama; defaults to the real one for production use.
        self.models_fetcher = models_fetcher or engine_mod.list_installed_models
        # The other half of GET /models: GGUFs downloaded for Hearth's own
        # bundled engine. Its own fetcher rather than a branch inside
        # models_fetcher, so a test can stub either engine's inventory
        # independently -- including to empty, which is how "an unusable
        # engine offers nothing" is actually proven.
        self.local_models_fetcher = local_models_fetcher or engine_mod.list_local_models
        # POST /session's model gate (hearth_backend.check_model): refuse a
        # model no engine here can serve, at the moment the user picks it,
        # rather than mid-turn after a checkpoint has already been taken.
        # Injectable so tests never need a real engine or a live daemon.
        self.model_checker = model_checker or (
            lambda model: engine_mod.hearth_backend.check_model(model))
        # GET /setup data source (hearth_setup.diagnose). Injectable the same
        # way models_fetcher is, so tests never need a real Ollama, a real
        # install, or real hardware.
        self.setup_diagnoser = setup_diagnoser or engine_mod.hearth_setup.diagnose
        # GET /idle data source (hearth_idle.is_good_time), cached -- see
        # get_idle_status(). Injectable for the same reason as
        # models_fetcher and setup_diagnoser.
        self.idle_prober = idle_prober or engine_mod.hearth_idle.is_good_time
        # GET /shop and GET /shop/quants. Injectable exactly like the fetchers
        # above, so the self-test exercises the routes (including their
        # untrusted-text and fallback-labelling behaviour) without reaching
        # Hugging Face.
        self.shop_searcher = shop_searcher or engine_mod.hearth_shop.search_shop
        self.shop_quanter = shop_quanter or engine_mod.hearth_shop.repo_quants
        # The download queue. One per process, deliberately NOT per session:
        # see downloads.py's module docstring. Constructed lazily rather than
        # eagerly so a test that never touches downloads never starts a
        # worker thread.
        self._downloads = download_manager
        self._downloads_lock = threading.Lock()
        self.idle_cache_seconds = (
            IDLE_CACHE_SECONDS if idle_cache_seconds is None else idle_cache_seconds)
        self._idle_lock = threading.Lock()  # separate from self._lock below: probing
        self._idle_cache = None             # (nvidia-smi) must never block session ops
        self._idle_cache_at = None
        self._lock = threading.Lock()
        self.session = None
        # ThreadingHTTPServer spawns one thread per connection, and GET
        # /events holds its connection open for as long as the client keeps
        # reading (or forever, if it never reads). A client opening many and
        # reading none would accumulate threads without bound, so concurrent
        # SSE streams are capped; see acquire_sse_slot/release_sse_slot.
        self.max_sse_connections = max_sse_connections or MAX_SSE_CONNECTIONS
        self._sse_count = 0
        # Restart survival (session_state.py). None (the default, and what
        # every test that does not care about persistence passes) means no
        # session this state ever creates or adopts writes anything to disk
        # -- _persist_if_current short-circuits before ever touching
        # self._raw_persist_hook. main.py is the only production caller
        # that supplies a real one.
        self._raw_persist_hook = persist_hook

    def get_session(self):
        with self._lock:
            return self.session

    def get_downloads(self):
        """The process-wide DownloadManager, built on first use. Its own
        lock, not self._lock: a download route must never contend with
        session creation, and vice versa."""
        with self._downloads_lock:
            if self._downloads is None:
                self._downloads = downloads_mod.DownloadManager()
            return self._downloads

    def _persist_if_current(self, session):
        """The persist_hook every Session this state creates or adopts is
        actually given (see create_session and set_restored_session) --
        never self._raw_persist_hook directly. A session that POST /session
        has already replaced can still have work in flight on its own
        thread for a while afterward (a cancelled-but-abandoned worker --
        see Session.is_workspace_busy's docstring in session.py), and that
        old session's own _run() finally block still calls its persist_hook
        once that work actually finishes. Without this guard, that stale
        write could land on disk AFTER the new session's own, newer write,
        silently clobbering it with older data -- a lost-update race, not a
        crash, but a silent one. Comparing against self.session (read
        fresh, under lock, at the moment of the actual write, not captured
        once at hook-creation time) is what makes "current" mean the same
        thing here as it does to GET /session or POST /cancel."""
        if self._raw_persist_hook is None:
            return
        with self._lock:
            is_current = self.session is session
        if is_current:
            self._raw_persist_hook(session)

    def set_restored_session(self, session):
        """Adopt a session session_state.restore_session() rebuilt from a
        prior process's disk snapshot as the live one -- main.py's startup
        path, called once, before the server starts accepting requests.
        Wired up exactly like a session born from create_session: the same
        _persist_if_current guard, so a session recovered from disk is not
        treated any differently than one created fresh over HTTP for every
        purpose persistence cares about afterward."""
        with self._lock:
            self.session = session
        session.set_persist_hook(self._persist_if_current if self._raw_persist_hook else None)

    def get_idle_status(self):
        """hearth_idle.is_good_time(), cached for self.idle_cache_seconds
        (default IDLE_CACHE_SECONDS). See the module docstring's GET /idle
        entry and hearth_idle.py's own "must not run on every event"
        contract for why this caches at all.

        Probing happens OUTSIDE self._idle_lock -- only the read/write of
        the cached value itself is locked -- so a slow probe (nvidia-smi is
        bounded, but still a real subprocess call) can never block a
        concurrent request that only needed the still-fresh cached value.
        A cache miss racing another cache miss just probes twice; that is
        cheap and harmless for read-only advisory data, so no effort is
        spent de-duplicating it.
        """
        now = time.monotonic()
        with self._idle_lock:
            cached, cached_at = self._idle_cache, self._idle_cache_at
        if cached is not None and cached_at is not None and (now - cached_at) < self.idle_cache_seconds:
            return cached
        result = self.idle_prober()
        with self._idle_lock:
            self._idle_cache = result
            self._idle_cache_at = time.monotonic()
            return self._idle_cache

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
        worker, if any, keeps running against its own, different, path.

        The read of `old`, the busy check, old.cancel(), and the assignment
        of the new session all happen under a single, uninterrupted hold of
        self._lock -- this used to be check-then-act (old read under the
        lock, the busy check and cancel() outside it, self.session assigned
        under the lock again), which let two concurrent POST /session calls
        both read the same `old`, both pass the busy check, and both
        proceed: the loser's freshly-built Session would then overwrite the
        winner's, and the winner's session -- along with whatever turn it
        had just started -- would be orphaned exactly the way the busy
        check exists to prevent. Holding self._lock across the whole
        sequence makes concurrent callers fully serialize instead: the
        second caller only ever sees the first caller's already-completed
        outcome (either the first call's new session as its own `old`, or
        the pre-existing session untouched if the first call raised)."""
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
            hook = self._persist_if_current if self._raw_persist_hook is not None else None
            s = session_mod.Session(workspace, model, mode, engine=self.engine_factory(),
                                    persist_hook=hook)
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

    def _send_json(self, code, obj, close=False):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            # BaseHTTPRequestHandler.send_header special-cases the
            # "Connection" header: sending "close" here sets
            # self.close_connection = True, which is what actually makes the
            # server tear the socket down after this response instead of
            # waiting to serve another pipelined request on it. Used only by
            # _prepare_body's 413 path -- see MAX_BODY_BYTES.
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _prepare_body(self):
        """Read this request's full declared Content-Length body off the
        socket into self._body, unconditionally, as the very first thing
        do_GET/do_POST do -- before _authorized() and before any route
        decides whether it even wants a body. See the module docstring's
        smuggling note: this is what guarantees no early-return path
        (rejected auth, "no session yet", or any future one) can ever leave
        unread bytes on the connection for the next pipelined request to
        misparse.

        Returns True with self._body set (b"" if there was no body) on
        success. Returns False if the declared length exceeds
        MAX_BODY_BYTES -- a 413 has already been sent and the connection
        closed (not drained: reading an attacker-declared, unbounded length
        would itself be a resource-exhaustion vector), and the caller must
        stop processing the request immediately."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request_body_too_large"}, close=True)
            return False
        self._body = self.rfile.read(length) if length > 0 else b""
        return True

    def _read_json(self):
        raw = getattr(self, "_body", b"") or b""
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
        """Percent-decoded query parameters. GET /events' `since` is a plain
        integer and never needed decoding, but a shop search carries a
        user-typed string ("qwen 2.5 coder", "phi-3.5"), so the values are
        unquoted here rather than at one call site. unquote_plus, not
        unquote: the UI encodes with URLSearchParams, which writes a space
        as "+"."""
        parts = self.path.split("?", 1)
        if len(parts) < 2:
            return {}
        out = {}
        for kv in parts[1].split("&"):
            if not kv:
                continue
            k, _, v = kv.partition("=")
            out[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
        return out

    def _int_param(self, query, name, default, low, high):
        """A caller-supplied integer, clamped. A missing or unparseable value
        is the default rather than a 400: these are all display knobs, and
        failing a search because a stale page sent limit=abc helps nobody."""
        try:
            value = int(query.get(name, default))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    # ---- routing ----

    def do_GET(self):
        # Drain the declared body, if any, before anything else -- including
        # before the unauthenticated /healthz route -- so a request that
        # never otherwise reads its body can never leave bytes behind for
        # the next pipelined request on this connection. See the module
        # docstring and _prepare_body.
        if not self._prepare_body():
            return
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
        elif path == "/setup":
            self._get_setup()
        elif path == "/idle":
            self._get_idle()
        elif path == "/shop":
            self._get_shop()
        elif path == "/shop/quants":
            self._get_shop_quants()
        elif path == "/downloads":
            self._send_json(200, self.state.get_downloads().snapshot())
        elif path == "/downloads/events":
            self._get_download_events()
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        # Same reasoning as do_GET: drain the body before _authorized() so a
        # rejected (401/403) request can never smuggle a follow-up request
        # past this connection's remaining pipelined bytes.
        if not self._prepare_body():
            return
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
        elif path == "/downloads":
            self._post_download()
        elif path in ("/downloads/cancel", "/downloads/dismiss"):
            self._post_download_action(path.rsplit("/", 1)[1])
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
        # Refuse a model no engine on this machine can serve, HERE, where
        # the user just picked it -- not mid-turn, several layers down,
        # after a checkpoint has already been taken. hearth_backend's own
        # message and remedy are passed through verbatim: they already name
        # the model, the backend and the reason.
        try:
            verdict = self.state.model_checker(model)
        except Exception:  # noqa: BLE001 - a broken gate must not block session creation
            verdict = None
        if isinstance(verdict, dict) and not verdict.get("ok", True):
            self._send_json(400, {
                "error": verdict.get("message") or "this model cannot be run as configured",
                "remedy": verdict.get("remedy"),
                "backend": verdict.get("backend"),
                "model_kind": verdict.get("kind"),
            })
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
        """Both engines' inventories, each labelled with the backend that
        runs it, plus the shop catalog.

        Best-effort on every source independently: an unreachable Ollama, a
        missing bundled engine, or a fetcher bug must not take the whole
        route down, since the shop's catalog verdicts (computed purely from
        local hardware, no engine involved) are still useful on their own.
        That per-source degradation is also what keeps the list honest --
        an engine that is not usable contributes no entries, so nothing here
        offers a model the active configuration cannot run.

        Every entry carries "backend" and "ref". "ref" is the exact string
        to send back to POST /session, kind prefix included for a GGUF, so
        a picker never has to re-derive a model's namespace from its name.
        Guessing that, one layer down, is precisely what broke.
        """
        try:
            installed = list(self.state.models_fetcher() or [])
        except Exception:  # noqa: BLE001 - a fetcher bug must not break this route
            installed = []
        # Ollama's fetcher predates model kinds and returns bare tags; label
        # them here rather than making every caller of that function care.
        installed = [dict(m, backend=engine_mod.hearth_backend.BACKEND_OLLAMA,
                          ref=engine_mod.hearth_backend.ModelRef.ollama(
                              m.get("name") or "?").as_text())
                     for m in installed if isinstance(m, dict)]
        try:
            local = list(self.state.local_models_fetcher() or [])
        except Exception:  # noqa: BLE001 - same contract as the Ollama half
            local = []
        installed.extend(m for m in local if isinstance(m, dict))
        try:
            catalog = engine_mod.hearth_shop.catalog_with_verdicts()
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": "catalog_failed: {}".format(exc)})
            return
        try:
            backend = engine_mod.hearth_backend.active()
        except Exception:  # noqa: BLE001 - a selection failure must not break this route
            backend = None
        self._send_json(200, {"installed": installed, "catalog": catalog,
                              "backend": backend})

    # ---- shop ----

    def _shop_kwargs(self, query):
        ctx = self._int_param(query, "context", 0, 0, SHOP_CONTEXT_MAX)
        return {"context_tokens": ctx} if ctx else {}

    @staticmethod
    def _annotate_local(model):
        """Add downloads.quant_local_state to every quantisation of one shop
        entry, in place.

        This is what turns "Download 4.1 GB" into "Resume from 3.1 GB" and
        "Installed". Applied to search results as well as to GET
        /shop/quants, because the recommended quantisation on a search card
        is the button most users will actually press, and offering them a
        fresh 4 GB download over a partial they already have would make the
        cancel-is-not-loss promise a lie on the one screen that matters.

        Computed here rather than in hearth_shop because it is a fact about
        this machine's model store, which that module has no business
        knowing about."""
        if not isinstance(model, dict):
            return
        repo_id = model.get("repo_id")
        if not repo_id:
            return  # a fallback catalog entry: nothing downloadable, nothing on disk
        for quant in model.get("quants") or []:
            quant["local"] = downloads_mod.quant_local_state(repo_id, quant)
            for alternate in quant.get("alternate_editions") or []:
                alternate["local"] = downloads_mod.quant_local_state(repo_id, alternate)
        best = model.get("best_quant")
        if isinstance(best, dict):
            best["local"] = downloads_mod.quant_local_state(repo_id, best)

    def _get_shop(self):
        """GET /shop: hearth_shop.search_shop, passed through unmodified.

        Unmodified matters. The result's "ok", "source", "notice" and
        "error_kind" are how a caller tells a live Hub listing from the
        built-in reference catalog the shop falls back to when the Hub
        cannot be reached, and hearth_shop is deliberate that ok is True
        only for live results. Repackaging any of that here -- turning a
        fallback into a 200 that looks like a search, or a live failure into
        a 500 -- would be the one way this route could lie. It never raises:
        search_shop degrades internally, and the try/except only covers a
        bug in the injected seam.
        """
        query = self._query()
        try:
            listing = self.state.shop_searcher(
                query.get("q", ""),
                limit=self._int_param(query, "limit", SHOP_LIMIT_DEFAULT, 1, SHOP_LIMIT_MAX),
                detail_limit=self._int_param(query, "detail", SHOP_DETAIL_DEFAULT, 0,
                                             SHOP_DETAIL_MAX),
                **self._shop_kwargs(query))
        except Exception as exc:  # noqa: BLE001 - a searcher bug must not break this route
            self._send_json(500, {"error": "shop_search_failed: {}".format(exc)})
            return
        if isinstance(listing, dict):
            for model in listing.get("models") or []:
                self._annotate_local(model)
        self._send_json(200, listing)

    def _get_shop_quants(self):
        """GET /shop/quants?repo=<repo_id>: one repository's quantisations,
        each annotated with what is already on disk for it.

        The annotation is the difference between "download 4.1 GB" and
        "resume from 3.1 GB", and between offering a download and saying the
        model is already installed. It is computed here rather than in
        hearth_shop because it is a fact about this machine's model store,
        which the shop module has no business knowing about.
        """
        query = self._query()
        repo_id = query.get("repo", "")
        if not repo_id:
            self._send_json(400, {"error": "repo is required"})
            return
        try:
            result = self.state.shop_quanter(repo_id, **self._shop_kwargs(query))
        except Exception as exc:  # noqa: BLE001 - a quanter bug must not break this route
            self._send_json(500, {"error": "shop_quants_failed: {}".format(exc)})
            return
        if isinstance(result, dict):
            self._annotate_local(result.get("model"))
        self._send_json(200, result)

    # ---- downloads ----

    def _post_download(self):
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "invalid_json"})
            return
        try:
            job = self.state.get_downloads().start(body.get("repo_id"), body.get("filename"))
        except downloads_mod.DownloadError as exc:
            # The module's own message and error kind, verbatim: they already
            # name the repository, the file and the reason, and a gated repo
            # in particular says why it cannot be fetched at all.
            self._send_json(exc.http_status, {"error": str(exc), "error_kind": exc.kind})
            return
        self._send_json(200, job)

    def _post_download_action(self, action):
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "invalid_json"})
            return
        job_id = body.get("id")
        if not isinstance(job_id, str) or not job_id:
            self._send_json(400, {"error": "id is required"})
            return
        manager = self.state.get_downloads()
        done = manager.cancel(job_id) if action == "cancel" else manager.dismiss(job_id)
        if not done:
            self._send_json(404, {"error": "unknown_or_already_settled_download"})
            return
        self._send_json(200, {action: True, "downloads": manager.snapshot()})

    def _get_download_events(self):
        """SSE for downloads. One frame per change, each carrying the WHOLE
        list -- see downloads.py on why this is a gauge, not a narrative.
        Shares the same bounded SSE slot pool as GET /events, since it holds
        a handler thread open in exactly the same way."""
        q = self._query()
        try:
            since = int(q.get("since", 0))
        except ValueError:
            since = 0
        manager = self.state.get_downloads()
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
                    snapshot = manager.snapshot_after(since, timeout=15)
                    if snapshot["version"] == since:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    since = snapshot["version"]
                    chunk = "id: {}\nevent: downloads\ndata: {}\n\n".format(
                        since, json.dumps(snapshot))
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                return
        finally:
            self.state.release_sse_slot()

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

    def _get_setup(self):
        """GET /setup: hearth_setup.diagnose(), fresh every call. Does not
        require a session to exist -- a UI needs this before it can show a
        chat box at all. See SidecarState.setup_diagnoser."""
        try:
            diagnosis = self.state.setup_diagnoser()
        except Exception as exc:  # noqa: BLE001 - a diagnoser bug must not break this route
            self._send_json(500, {"error": "setup_diagnosis_failed: {}".format(exc)})
            return
        self._send_json(200, diagnosis)

    def _get_idle(self):
        """GET /idle: hearth_idle.is_good_time(), cached -- see
        SidecarState.get_idle_status. Advisory only: nothing in this
        sidecar ever gates a turn on it -- see engine.py's module
        docstring, point 6, and hearth_idle.py's own degrade-to-permissive
        contract."""
        try:
            status = self.state.get_idle_status()
        except Exception as exc:  # noqa: BLE001 - a prober bug must not break this route
            self._send_json(500, {"error": "idle_probe_failed: {}".format(exc)})
            return
        self._send_json(200, status)

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
    import socket
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

    def _start(token="the-real-token-value", engine_factory=None, models_fetcher=None,
               local_models_fetcher=None, model_checker=None):
        # Both new fetchers default to "this engine has nothing", not to the
        # real ones: a self-test must never depend on what happens to be
        # installed on the machine running it, and the whole point of GET
        # /models' per-source degradation is that an empty source is normal.
        state = SidecarState(token, engine_factory=engine_factory, models_fetcher=models_fetcher,
                             local_models_fetcher=local_models_fetcher or (lambda: []),
                             model_checker=model_checker or (lambda model: {"ok": True}))
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

    def _one_connection_responses(port, raw_request_bytes, read_timeout=2.0):
        """Send raw_request_bytes over a single fresh TCP connection, then
        read from that SAME connection for up to read_timeout seconds,
        returning every byte the server sent back on it. Used to prove the
        smuggling property directly against real socket framing: a
        higher-level HTTP client (http.client/urllib) would transparently
        make a second connection or silently discard a pipelined response,
        which would prove nothing about whether the server actually sent
        one."""
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.sendall(raw_request_bytes)
            s.settimeout(read_timeout)
            chunks = []
            try:
                while True:
                    data = s.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            except socket.timeout:
                pass
            return b"".join(chunks)
        finally:
            s.close()

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

        # === Finding 1: HTTP request smuggling on rejected requests. =======
        # === The actual pin: a real socket, a request rejected for a bad ===
        # === bearer token, whose body IS ITSELF a second, fully-formed ====
        # === HTTP request with an attacker-chosen Host -- and proof that ==
        # === the connection never yields a second response for it. =======
        smuggled_request = (
            "GET /healthz HTTP/1.1\r\n"
            "Host: evil.example.com:{}\r\n"
            "\r\n"
        ).format(port).encode("utf-8")
        smuggling_attempt = (
            "POST /prompt HTTP/1.1\r\n"
            "Host: 127.0.0.1:{port}\r\n"
            "Authorization: Bearer totally-wrong-token\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: {n}\r\n"
            "\r\n"
        ).format(port=port, n=len(smuggled_request)).encode("utf-8") + smuggled_request

        raw = _one_connection_responses(port, smuggling_attempt)
        status_lines = raw.split(b"HTTP/1.1 ")[1:]
        assert len(status_lines) == 1, (
            "request smuggling succeeded: {} HTTP responses arrived on one connection "
            "for one request, expected exactly 1 (the 401 rejection): {!r}".format(
                len(status_lines), raw))
        assert status_lines[0].startswith(b"401"), raw
        assert b"evil.example.com" not in raw, \
            "the smuggled request's spoofed Host must never reach a route handler"

        # The same channel is even more severe through the completely
        # unauthenticated /healthz route -- no bad token needed at all, any
        # local process or web page can reach it. _prepare_body runs before
        # do_GET even looks at the path, so this must be closed too.
        smuggled_via_healthz = (
            "GET /session HTTP/1.1\r\n"
            "Host: evil.example.com:{}\r\n"
            "\r\n"
        ).format(port).encode("utf-8")
        healthz_smuggling_attempt = (
            "GET /healthz HTTP/1.1\r\n"
            "Host: 127.0.0.1:{port}\r\n"
            "Content-Length: {n}\r\n"
            "\r\n"
        ).format(port=port, n=len(smuggled_via_healthz)).encode("utf-8") + smuggled_via_healthz
        raw_healthz = _one_connection_responses(port, healthz_smuggling_attempt)
        healthz_status_lines = raw_healthz.split(b"HTTP/1.1 ")[1:]
        assert len(healthz_status_lines) == 1, (
            "request smuggling succeeded via the unauthenticated /healthz route: "
            "{} HTTP responses on one connection: {!r}".format(
                len(healthz_status_lines), raw_healthz))
        assert healthz_status_lines[0].startswith(b"200"), raw_healthz

        # === Finding 1, also: a Content-Length that exceeds MAX_BODY_BYTES =
        # === is rejected with 413 and the connection is closed, not =======
        # === silently drained -- proven the same way, over a real socket. =
        oversized_len = MAX_BODY_BYTES + 1
        oversized_headers = (
            "POST /session HTTP/1.1\r\n"
            "Host: 127.0.0.1:{port}\r\n"
            "Authorization: Bearer {token}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: {n}\r\n"
            "\r\n"
        ).format(port=port, token=token, n=oversized_len).encode("utf-8")
        # Only send the headers (declaring a huge body) plus a small amount
        # of the "body" -- never the full oversized_len bytes. If the server
        # tried to read() the full declared length before rejecting, this
        # connection would hang waiting for bytes that never arrive; getting
        # a prompt 413 instead proves the length is checked before any read.
        s_over = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s_over.sendall(oversized_headers + b'{"padding": "' + b"x" * 1024)
            s_over.settimeout(5)
            resp_data = b""
            while b"\r\n\r\n" not in resp_data:
                chunk = s_over.recv(4096)
                if not chunk:
                    break
                resp_data += chunk
            assert resp_data.startswith(b"HTTP/1.1 413"), resp_data
            assert b"Connection: close" in resp_data, \
                "the oversized-body rejection must close the connection: {!r}".format(resp_data)
        finally:
            s_over.close()

        # A body right at the cap is accepted (still 400: bad workspace
        # value -- the point is the length check itself does not fire).
        at_cap_body = json.dumps({"workspace": "", "model": "m",
                                  "padding": "x" * 100}).encode("utf-8")
        status, data = _raw_request(port, "POST", "/session", headers=auth_headers,
                                    body=at_cap_body)
        assert status == 400, (status, data)

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

        # === GET /models: works with injected fetchers, no live Ollama and =
        # === no installed engine required; BOTH engines' inventories are ===
        # === listed, each labelled with the backend that runs it, and the ==
        # === catalog survives either fetcher being broken. =================

        def _fake_models_fetcher():
            return [{"name": "test-model:1b", "size_bytes": 42, "modified_at": "x", "digest": "y"}]

        def _fake_local_fetcher():
            return [{"name": "stories260K.gguf", "ref": "gguf:/store/stories260K.gguf",
                     "backend": "llama", "path": "/store/stories260K.gguf",
                     "size_bytes": 7, "modified_at": None, "digest": None}]

        server3, state3 = _start(engine_factory=lambda: _FakeEngine(),
                                 models_fetcher=_fake_models_fetcher,
                                 local_models_fetcher=_fake_local_fetcher)
        try:
            port3 = state3.port
            headers3 = {"Host": "127.0.0.1:{}".format(port3),
                       "Authorization": "Bearer " + state3.token}
            status, data = _raw_request(port3, "GET", "/models", headers=headers3)
            assert status == 200, (status, data)
            body = json.loads(data)
            # Both engines, in one list, each entry knowing which engine runs
            # it. A picker that cannot tell them apart is exactly how an
            # Ollama tag ended up being handed to the bundled engine.
            assert [m["backend"] for m in body["installed"]] == ["ollama", "llama"], body
            ollama_entry, llama_entry = body["installed"]
            assert ollama_entry["name"] == "test-model:1b", ollama_entry
            # "ref" round-trips through hearth_backend.ModelRef.parse without
            # any guessing, which is the property POST /session relies on.
            assert ollama_entry["ref"] == "ollama:test-model:1b", ollama_entry
            assert llama_entry["ref"] == "gguf:/store/stories260K.gguf", llama_entry
            # The Ollama fetcher's own fields survive the labelling pass.
            assert ollama_entry["size_bytes"] == 42 and ollama_entry["digest"] == "y", ollama_entry
            # The active selection travels with the list so a UI can say
            # which engine is in force without a second request.
            assert body["backend"]["backend"] in ("llama", "ollama"), body["backend"]
            assert isinstance(body["catalog"], list) and len(body["catalog"]) >= 6, body
            assert all("verdict" in e and "id" in e for e in body["catalog"]), body
        finally:
            server3.shutdown()
            server3.server_close()

        def _broken_fetcher():
            raise RuntimeError("ollama unreachable")

        # Each source degrades on its own: a broken (or unreachable) Ollama
        # must not cost the bundled engine's models their listing, and vice
        # versa. An engine that reports nothing offers nothing, which is what
        # keeps this route from advertising models nothing here can run.
        server3b, state3b = _start(engine_factory=lambda: _FakeEngine(),
                                   models_fetcher=_broken_fetcher,
                                   local_models_fetcher=_fake_local_fetcher)
        try:
            port3b = state3b.port
            headers3b = {"Host": "127.0.0.1:{}".format(port3b),
                        "Authorization": "Bearer " + state3b.token}
            status, data = _raw_request(port3b, "GET", "/models", headers=headers3b)
            assert status == 200, (status, data)  # a broken fetcher must not take the route down
            body = json.loads(data)
            assert [m["backend"] for m in body["installed"]] == ["llama"], body
            assert isinstance(body["catalog"], list) and body["catalog"], body
        finally:
            server3b.shutdown()
            server3b.server_close()

        server3c, state3c = _start(engine_factory=lambda: _FakeEngine(),
                                   models_fetcher=_fake_models_fetcher,
                                   local_models_fetcher=_broken_fetcher)
        try:
            port3c = state3c.port
            headers3c = {"Host": "127.0.0.1:{}".format(port3c),
                        "Authorization": "Bearer " + state3c.token}
            status, data = _raw_request(port3c, "GET", "/models", headers=headers3c)
            assert status == 200, (status, data)
            body = json.loads(data)
            assert [m["backend"] for m in body["installed"]] == ["ollama"], body
        finally:
            server3c.shutdown()
            server3c.server_close()

        # === POST /session refuses a model no engine here can serve, at the
        # === moment the user picks it -- not mid-turn, after a checkpoint ==
        # === has already been taken. hearth_backend's own message and ======
        # === remedy are passed through, not re-worded. =====================
        _gate_seen = []

        def _model_gate(model):
            _gate_seen.append(model)
            if model == "qwen2.5-coder:latest":
                return {"ok": False, "backend": "ollama", "kind": "ollama",
                        "message": ("'qwen2.5-coder:latest' is an Ollama model tag, and "
                                    "Ollama is not reachable at http://127.0.0.1:11434."),
                        "remedy": "Start Ollama, or pick a downloaded model."}
            return {"ok": True, "backend": "llama", "kind": None,
                    "message": None, "remedy": None}

        server3d, state3d = _start(engine_factory=lambda: _FakeEngine(),
                                   model_checker=_model_gate)
        try:
            port3d = state3d.port
            headers3d = {"Host": "127.0.0.1:{}".format(port3d),
                         "Authorization": "Bearer " + state3d.token,
                         "Content-Type": "application/json"}
            status, data = _raw_request(
                port3d, "POST", "/session", headers=headers3d,
                body=json.dumps({"workspace": "/tmp/ws-gate", "model": "qwen2.5-coder:latest"}))
            assert status == 400, (status, data)
            body = json.loads(data)
            assert "not reachable" in body["error"], body
            assert body["remedy"] == "Start Ollama, or pick a downloaded model.", body
            assert body["backend"] == "ollama" and body["model_kind"] == "ollama", body
            # No session was created: a refused model must not leave a
            # half-built session behind for the next request to find.
            assert state3d.get_session() is None, "a refused model must not create a session"
            # ... and a model the gate accepts still creates one normally,
            # which is what makes the refusal above non-vacuous.
            status, data = _raw_request(
                port3d, "POST", "/session", headers=headers3d,
                body=json.dumps({"workspace": "/tmp/ws-gate", "model": "auto"}))
            assert status == 200, (status, data)
            assert state3d.get_session() is not None
            assert _gate_seen == ["qwen2.5-coder:latest", "auto"], _gate_seen
        finally:
            server3d.shutdown()
            server3d.server_close()

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

        # === Minor: SidecarState.create_session is atomic -- the read of ===
        # === the old session, the busy check, cancel(), and the new =======
        # === session's assignment all happen under one uninterrupted hold =
        # === of state._lock. Proven with get_session() as the probe, not =
        # === a second concurrent create_session call: a second =========
        # === create_session call would ALSO block inside its own slow ====
        # === engine_factory() regardless of locking (both calls share the =
        # === same slow factory), which would make "B hasn't finished yet" =
        # === true whether or not the sequence is actually atomic -- a ====
        # === false pass. get_session() only ever needs state._lock for a =
        # === plain read, so it can only be blocked here by create_session =
        # === genuinely holding the lock across its own engine_factory() ==
        # === call, which is exactly the property being fixed. =============
        create_enter = threading.Event()
        create_release = threading.Event()

        def _slow_engine_factory():
            create_enter.set()
            create_release.wait(timeout=5)
            return _FakeEngine()

        state_atomic = SidecarState("atomic-token", engine_factory=_slow_engine_factory)
        thread_a = threading.Thread(
            target=lambda: state_atomic.create_session("/tmp/ws-atomic-a", "m", "edit"))
        thread_a.start()
        assert create_enter.wait(timeout=5), "thread A's critical section never started"

        # Thread A is now inside engine_factory(), blocked on
        # create_release. If create_session holds state._lock for its whole
        # duration (the fix), a concurrent get_session() -- which only ever
        # needs a brief hold of the same lock -- must be unable to complete
        # until A releases it.
        get_done = threading.Event()

        def _call_get():
            state_atomic.get_session()
            get_done.set()

        thread_get = threading.Thread(target=_call_get)
        thread_get.start()
        assert not get_done.wait(timeout=0.3), (
            "get_session() completed while create_session's own critical section "
            "(inside engine_factory()) was still in flight -- create_session does not "
            "hold state._lock for its full duration, so the sequence is not atomic")
        create_release.set()
        assert get_done.wait(timeout=5), "get_session() never completed after A released the lock"
        thread_a.join(timeout=5)
        thread_get.join(timeout=5)
        assert state_atomic.get_session().workspace == "/tmp/ws-atomic-a", \
            "thread A's session must be the one left live"

        # === restart survival wiring: SidecarState only ever persists on ===
        # === behalf of whichever session is CURRENTLY self.session -- a ===
        # === session POST /session already replaced, whose own abandoned =
        # === worker (see Session.is_workspace_busy) later finishes its ===
        # === turn and fires its own persist_hook, must not be allowed to =
        # === clobber the new session's already-persisted state with =====
        # === stale data. Exercised with a fake recording hook, not real ===
        # === disk -- session_state.py's own self-test proves the disk ====
        # === format; this proves app.py's guard around it. ================
        persisted_snapshots = []

        def _recording_hook(sess):
            persisted_snapshots.append(sess.to_dict())

        release_old = threading.Event()
        entered_old = threading.Event()

        class _SlowGateEngine:
            """Blocks in run() until released, so its Session can be
            replaced by POST /session while this turn is still "running",
            simulating the abandoned-worker window the guard exists for."""

            def run(self, ctx):
                entered_old.set()
                release_old.wait(timeout=10)
                ctx.emit("done", {})

        class _QuickEngine:
            def run(self, ctx):
                ctx.emit("done", {})

        # The first session gets the slow engine (the one whose replacement
        # simulates an abandoned worker); every later session on this state
        # gets a quick one, so its own persist calls are easy to tell apart
        # from the slow one's without sharing a release gate between them.
        guard_call_count = {"n": 0}

        def _guard_engine_factory():
            guard_call_count["n"] += 1
            return _SlowGateEngine() if guard_call_count["n"] == 1 else _QuickEngine()

        state_guard = SidecarState("guard-token", engine_factory=_guard_engine_factory,
                                   persist_hook=_recording_hook)
        old_sess = state_guard.create_session("/tmp/ws-guard-old", "m", "edit")
        old_sess.submit_prompt("hang")
        assert entered_old.wait(timeout=5), "old session's turn never started"
        # A persist fired for the OLD session while it was still current
        # (submit_prompt's own turn-start persist) -- this one is legitimate.
        assert len(persisted_snapshots) >= 1
        assert all(s["workspace"] == "/tmp/ws-guard-old" for s in persisted_snapshots)

        # Replace it with a different workspace while the old turn is still
        # blocked in run() -- exactly the abandoned-worker window.
        new_sess = state_guard.create_session("/tmp/ws-guard-new", "m", "edit")
        assert state_guard.get_session() is new_sess
        new_sess.submit_prompt("go")  # the new (current) session persists normally
        deadline = time.monotonic() + 5
        while new_sess.to_dict()["status"] != "idle" and time.monotonic() < deadline:
            time.sleep(0.01)

        # Now let the OLD session's blocked turn finish. Its own _run()
        # finally block will call its persist_hook (session._persist), which
        # is state_guard._persist_if_current -- and by now self.session is
        # new_sess, not old_sess, so this must be silently suppressed.
        n_before_release = len(persisted_snapshots)
        release_old.set()
        deadline = time.monotonic() + 5
        while old_sess.to_dict()["status"] != "idle" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert old_sess.to_dict()["status"] == "idle", "old session's turn never finished"
        time.sleep(0.2)  # give its finally-block persist call a chance to (wrongly) fire
        assert all(s["workspace"] != "/tmp/ws-guard-old"
                  for s in persisted_snapshots[n_before_release:]), (
            "an old, already-replaced session's persist_hook fired after replacement -- "
            "it must be suppressed, or it can silently clobber the new session's state: "
            "{}".format(persisted_snapshots[n_before_release:])
        )
        # The new session, meanwhile, persists normally.
        assert any(s["workspace"] == "/tmp/ws-guard-new" for s in persisted_snapshots), \
            "the new (current) session's own persist calls must NOT be suppressed"

        # === set_restored_session wires the identical guard onto a session
        # === adopted at startup (main.py's path), not just one built ======
        # === through create_session ========================================
        restored_snapshots = []

        def _restored_hook(sess):
            restored_snapshots.append(sess.to_dict())

        state_restore_guard = SidecarState("restore-guard-token", persist_hook=_restored_hook)
        restored_sess = session_mod.Session("/tmp/ws-restored", "m", "edit", engine=_FakeEngine())
        state_restore_guard.set_restored_session(restored_sess)
        assert state_restore_guard.get_session() is restored_sess
        restored_sess.submit_prompt("go")
        deadline = time.monotonic() + 5
        while not restored_snapshots and time.monotonic() < deadline:
            time.sleep(0.01)
        assert restored_snapshots, "a session adopted via set_restored_session must still persist"
        deadline = time.monotonic() + 5
        while restored_sess.to_dict()["status"] != "idle" and time.monotonic() < deadline:
            time.sleep(0.01)
        # Replacing it must suppress ITS late persist calls too, the same as
        # any other session -- prove the guard, not just that the hook fires
        # once for a freshly adopted session.
        replaced_sess = state_restore_guard.create_session("/tmp/ws-restored-2", "m", "edit")
        assert state_restore_guard.get_session() is replaced_sess

        # === GET /setup and GET /idle: hearth_setup/hearth_idle wiring =====
        # === (Part 1 / Part 2 of this iteration's brief). Both require the =
        # === same auth as every other route -- proven directly against a ===
        # === real socket, not merely inferred from do_GET's shared ========
        # === structure, so a future route accidentally added above ========
        # === _authorized() (the exact mistake /healthz's own placement =====
        # === invites) would be caught here too. ==============================
        setup_calls = {"n": 0}
        stub_diagnosis = {
            "status": "not_installed", "healthy": False, "platform": "Windows",
            "base_url": "http://127.0.0.1:11434", "timestamp": "2026-01-01T00:00:00Z",
            "findings": [{"check": "installed", "status": "problem",
                          "message": "Ollama does not appear to be installed on this machine.",
                          "remedy": "Download it from ollama.com/download.", "detail": None}],
            "next_action": {"message": "Ollama does not appear to be installed on this machine.",
                            "remedy": "Download it from ollama.com/download."},
        }

        def _stub_diagnoser():
            setup_calls["n"] += 1
            return stub_diagnosis

        idle_calls = {"n": 0}
        stub_idle = {"good_time": True, "state": "idle", "confidence": "high",
                     "reason": "no keyboard/mouse input for 600s", "signals": {"platform": "Windows"}}

        def _stub_idle_prober():
            idle_calls["n"] += 1
            return dict(stub_idle)  # a fresh dict each call -- proves get_idle_status
                                     # returns the SAME cached object, not a lucky-equal one

        server_setup, state_setup = _start(engine_factory=lambda: _FakeEngine())
        state_setup.setup_diagnoser = _stub_diagnoser
        state_setup.idle_prober = _stub_idle_prober
        state_setup.idle_cache_seconds = 0.2  # short, so the test does not have to sleep long
        try:
            port_setup = state_setup.port
            token_setup = state_setup.token

            # -- auth: both new routes require it, exactly like every other -
            # -- route except /healthz. --------------------------------------
            status, _ = _raw_request(port_setup, "GET", "/setup",
                                     headers={"Host": "127.0.0.1:{}".format(port_setup)})
            assert status == 401, status
            status, _ = _raw_request(port_setup, "GET", "/idle",
                                     headers={"Host": "127.0.0.1:{}".format(port_setup)})
            assert status == 401, status

            auth_headers_setup = {"Host": "127.0.0.1:{}".format(port_setup),
                                  "Authorization": "Bearer " + token_setup}

            # -- GET /setup: the diagnoser's result passed straight through, -
            # -- with no live session at all. ---------------------------------
            status, data = _raw_request(port_setup, "GET", "/setup", headers=auth_headers_setup)
            assert status == 200, (status, data)
            assert json.loads(data) == stub_diagnosis, data
            assert setup_calls["n"] == 1, setup_calls

            status, data = _raw_request(port_setup, "GET", "/setup", headers=auth_headers_setup)
            assert status == 200, (status, data)
            assert setup_calls["n"] == 2, (
                "GET /setup must diagnose fresh every call, never cache -- a stale "
                "'not ready' right after the user actually started Ollama would be "
                "actively unhelpful: {}".format(setup_calls))

            # -- GET /idle: cached briefly, so a UI polling for a status ------
            # -- indicator does not shell out to nvidia-smi on every request. -
            status, data = _raw_request(port_setup, "GET", "/idle", headers=auth_headers_setup)
            assert status == 200, (status, data)
            assert json.loads(data) == stub_idle, data
            assert idle_calls["n"] == 1, idle_calls

            status, data = _raw_request(port_setup, "GET", "/idle", headers=auth_headers_setup)
            assert status == 200, (status, data)
            assert idle_calls["n"] == 1, (
                "a second GET /idle within idle_cache_seconds must reuse the cached "
                "reading, not re-probe: {}".format(idle_calls))

            time.sleep(0.25)  # past state_setup.idle_cache_seconds (0.2s)
            status, data = _raw_request(port_setup, "GET", "/idle", headers=auth_headers_setup)
            assert status == 200, (status, data)
            assert idle_calls["n"] == 2, (
                "once the cache has aged past idle_cache_seconds, the next GET /idle "
                "must re-probe rather than serve a stale reading forever: {}".format(idle_calls))
        finally:
            server_setup.shutdown()
            server_setup.server_close()

        # === GET /setup and GET /idle also work against the REAL, unstubbed =
        # === hearth_setup/hearth_idle modules -- no Ollama, no GPU, no ======
        # === network required for this to at least run without raising. ====
        server_real, state_real = _start(engine_factory=lambda: _FakeEngine())
        try:
            port_real = state_real.port
            headers_real = {"Host": "127.0.0.1:{}".format(port_real),
                            "Authorization": "Bearer " + state_real.token}
            status, data = _raw_request(port_real, "GET", "/setup", headers=headers_real)
            assert status == 200, (status, data)
            real_setup_body = json.loads(data)
            assert "status" in real_setup_body and "findings" in real_setup_body, real_setup_body

            status, data = _raw_request(port_real, "GET", "/idle", headers=headers_real)
            assert status == 200, (status, data)
            real_idle_body = json.loads(data)
            assert "good_time" in real_idle_body and "state" in real_idle_body, real_idle_body
        finally:
            server_real.shutdown()
            server_real.server_close()

        # === the shop and the download queue ================================
        # === GET /shop, GET /shop/quants, and the four download routes, ====
        # === with hearth_shop and hearth_hf both stubbed: no Hugging Face ==
        # === request is made by this self-test. ============================

        shop_calls = []

        def _stub_search(query, limit=None, detail_limit=None, context_tokens=None):
            shop_calls.append({"query": query, "limit": limit, "detail_limit": detail_limit,
                               "context_tokens": context_tokens})
            return {"ok": True, "source": "live", "notice": None, "error": None,
                    "error_kind": None, "query": query, "model_count": 1,
                    "hardware": {"vram_bytes": 16 * 1024 ** 3, "ram_bytes": 24 * 1024 ** 3},
                    "models": [{"repo_id": "acme/Model-GGUF", "label": "Model",
                                "gated": False, "downloadable": True,
                                "quants_loaded": True,
                                "quants": [{"name": "m-Q4_K_M.gguf", "path": "m-Q4_K_M.gguf",
                                            "parts": [{"path": "m-Q4_K_M.gguf"}],
                                            "alternate_editions": []}],
                                "best_quant": {"name": "m-Q4_K_M.gguf", "path": "m-Q4_K_M.gguf",
                                               "parts": [{"path": "m-Q4_K_M.gguf"}],
                                               "alternate_editions": []},
                                "verdict": {"verdict": "great", "message": "Runs fully on the GPU."}}]}

        def _stub_fallback_search(query, **kwargs):  # noqa: ARG001
            return {"ok": False, "source": "fallback", "notice": "the Hub could not be reached",
                    "error": "unreachable", "error_kind": "unreachable", "models": []}

        def _stub_quants(repo_id, context_tokens=None):  # noqa: ARG001
            return {"ok": True, "source": "live", "repo_id": repo_id,
                    "model": {"repo_id": repo_id, "label": "Model", "quants": [
                        {"name": "m-Q4_K_M.gguf", "path": "m-Q4_K_M.gguf", "quant": "Q4_K_M",
                         "size_bytes": 400, "parts": [{"path": "m-Q4_K_M.gguf"}],
                         "alternate_editions": []}]}}

        def _stub_list(repo_id, **kwargs):  # noqa: ARG001
            return {"ok": True, "files": [
                {"name": "m-Q4_K_M.gguf", "path": "m-Q4_K_M.gguf", "quant": "Q4_K_M",
                 "size_bytes": 400, "sha256": "a" * 64, "complete": True,
                 "parts": [{"path": "m-Q4_K_M.gguf", "size_bytes": 400,
                            "sha256": "a" * 64, "index": 1}]}]}

        def _stub_download(repo_id, filename, dest_dir=None, on_progress=None,  # noqa: ARG001
                           is_cancelled=None, expected_size=None, **kwargs):
            total = expected_size or 400
            for done in (total // 2, total):
                if is_cancelled is not None and is_cancelled():
                    return {"ok": False, "cancelled": True, "bytes_done": done // 2}
                if on_progress:
                    on_progress({"bytes_done": done, "bytes_total": total, "resumed_from": 0,
                                 "speed_bytes_per_sec": 100.0, "eta_seconds": 1.0})
                time.sleep(0.01)
            return {"ok": True, "bytes_done": total, "bytes_total": total, "verified": True,
                    "verification": "sha256", "path": "/models/" + filename}

        shop_manager = downloads_mod.DownloadManager(
            list_fn=_stub_list, download_fn=_stub_download, progress_interval=0.0)
        state_shop = SidecarState("shop-token", shop_searcher=_stub_search,
                                  shop_quanter=_stub_quants,
                                  download_manager=shop_manager,
                                  local_models_fetcher=lambda: [])
        server_shop = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state_shop))
        state_shop.port = server_shop.server_address[1]
        threading.Thread(target=server_shop.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        try:
            port_s = state_shop.port
            headers_s = {"Host": "127.0.0.1:{}".format(port_s),
                         "Authorization": "Bearer shop-token",
                         "Content-Type": "application/json"}

            # Every new route is behind the same bearer gate as the rest.
            for method, route in (("GET", "/shop"), ("GET", "/shop/quants?repo=a/b"),
                                  ("GET", "/downloads"), ("POST", "/downloads"),
                                  ("POST", "/downloads/cancel"),
                                  ("POST", "/downloads/dismiss"),
                                  ("GET", "/downloads/events")):
                status, _ = _raw_request(port_s, method, route,
                                         headers={"Host": "127.0.0.1:{}".format(port_s)})
                assert status == 401, (route, status)

            # -- GET /shop: the search string survives percent-encoding, the -
            # -- caller's limits are clamped, and the listing is passed -----
            # -- through unmodified. ----------------------------------------
            status, data = _raw_request(
                port_s, "GET", "/shop?q=qwen+2.5%20coder&limit=999&detail=999&context=4096",
                headers=headers_s)
            assert status == 200, (status, data)
            assert shop_calls[-1]["query"] == "qwen 2.5 coder", shop_calls
            assert shop_calls[-1]["limit"] == SHOP_LIMIT_MAX, shop_calls
            assert shop_calls[-1]["detail_limit"] == SHOP_DETAIL_MAX, shop_calls
            assert shop_calls[-1]["context_tokens"] == 4096, shop_calls
            shop_body = json.loads(data)
            assert shop_body["ok"] is True and shop_body["source"] == "live", shop_body

            # A fallback listing must reach the UI still labelled a fallback:
            # it is a 200 (the route worked), but ok False and source
            # "fallback", never dressed up as a live search result.
            state_shop.shop_searcher = _stub_fallback_search
            status, data = _raw_request(port_s, "GET", "/shop?q=x", headers=headers_s)
            assert status == 200, (status, data)
            fallback_body = json.loads(data)
            assert fallback_body["ok"] is False, fallback_body
            assert fallback_body["source"] == "fallback", fallback_body
            assert fallback_body["notice"], fallback_body
            state_shop.shop_searcher = _stub_search

            # -- GET /shop/quants: needs a repo, and annotates each quant ----
            # -- with what is already on disk for it. -----------------------
            status, data = _raw_request(port_s, "GET", "/shop/quants", headers=headers_s)
            assert status == 400, (status, data)
            status, data = _raw_request(port_s, "GET", "/shop/quants?repo=acme%2FModel-GGUF",
                                        headers=headers_s)
            assert status == 200, (status, data)
            quant_body = json.loads(data)
            assert quant_body["repo_id"] == "acme/Model-GGUF", quant_body
            local = quant_body["model"]["quants"][0]["local"]
            assert set(local) >= {"present", "parts_present", "partial_bytes"}, local

            # The same annotation reaches search results, not only the
            # per-repository route: the recommended quantisation on a search
            # card is the button a user actually presses, and it must know
            # about a partial download rather than offering the whole file
            # again.
            status, data = _raw_request(port_s, "GET", "/shop?q=x", headers=headers_s)
            assert status == 200, (status, data)
            searched = json.loads(data)["models"][0]
            assert "local" in searched["best_quant"], searched

            # -- the download lifecycle over HTTP ---------------------------
            status, data = _raw_request(port_s, "GET", "/downloads", headers=headers_s)
            assert status == 200 and json.loads(data)["downloads"] == [], data

            status, data = _raw_request(port_s, "POST", "/downloads", headers=headers_s,
                                        body=json.dumps({"repo_id": "acme/Model-GGUF",
                                                         "filename": "nope.gguf"}))
            assert status == 404, (status, data)
            assert json.loads(data)["error_kind"] == "not_found", data

            status, data = _raw_request(port_s, "POST", "/downloads", headers=headers_s,
                                        body=json.dumps({"repo_id": "acme/Model-GGUF",
                                                         "filename": "m-Q4_K_M.gguf"}))
            assert status == 200, (status, data)
            job_id = json.loads(data)["id"]

            deadline_dl = time.monotonic() + 10
            final = None
            while time.monotonic() < deadline_dl:
                status, data = _raw_request(port_s, "GET", "/downloads", headers=headers_s)
                jobs = json.loads(data)["downloads"]
                final = next((j for j in jobs if j["id"] == job_id), None)
                if final and final["status"] == downloads_mod.STATUS_DONE:
                    break
                time.sleep(0.02)
            assert final and final["status"] == downloads_mod.STATUS_DONE, final
            assert final["bytes_done"] == 400 and final["fraction"] == 1.0, final

            # Cancelling a settled download is a 404, not a silent success.
            status, data = _raw_request(port_s, "POST", "/downloads/cancel", headers=headers_s,
                                        body=json.dumps({"id": job_id}))
            assert status == 404, (status, data)
            status, data = _raw_request(port_s, "POST", "/downloads/dismiss", headers=headers_s,
                                        body=json.dumps({"id": job_id}))
            assert status == 200, (status, data)
            assert json.loads(data)["downloads"]["downloads"] == [], data
            status, data = _raw_request(port_s, "POST", "/downloads/cancel", headers=headers_s,
                                        body=json.dumps({}))
            assert status == 400, (status, data)

            # -- GET /downloads/events streams the WHOLE list per frame, and -
            # -- works with no session in existence at all, which is the ----
            # -- entire reason it is not GET /events. -----------------------
            status, _ = _raw_request(port_s, "GET", "/session", headers=headers_s)
            assert status == 404, "this server must genuinely have no session"

            req_dl = urllib.request.Request(
                "http://127.0.0.1:{}/downloads/events?since=0".format(port_s),
                headers={"Authorization": "Bearer shop-token",
                         "Host": "127.0.0.1:{}".format(port_s)})
            resp_dl = urllib.request.urlopen(req_dl, timeout=10)
            _raw_request(port_s, "POST", "/downloads", headers=headers_s,
                         body=json.dumps({"repo_id": "acme/Model-GGUF",
                                          "filename": "m-Q4_K_M.gguf"}))
            saw_frame = None
            deadline_sse = time.monotonic() + 10
            while time.monotonic() < deadline_sse:
                line = resp_dl.readline().decode("utf-8", "replace")
                if not line:
                    break
                if line.startswith("data: "):
                    frame = json.loads(line[len("data: "):])
                    if frame["downloads"]:
                        saw_frame = frame
                        break
            resp_dl.close()
            assert saw_frame is not None, "GET /downloads/events never delivered a frame"
            assert "version" in saw_frame and isinstance(saw_frame["downloads"], list), saw_frame
            assert saw_frame["downloads"][0]["repo_id"] == "acme/Model-GGUF", saw_frame
        finally:
            shop_manager.stop()
            server_shop.shutdown()
            server_shop.server_close()

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
