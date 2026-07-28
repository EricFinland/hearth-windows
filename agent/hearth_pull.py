#!/usr/bin/env python3
r"""hearth model download: turning Ollama's /api/pull stream into honest progress.

hearth_shop.py tells a user what their hardware can run. hearth_hw.py detects
the hardware. Neither of them downloads anything. This module is the missing
layer: it drives Ollama's POST /api/pull, reads the newline-delimited JSON
(NDJSON) it streams back, and turns that into a progress signal a UI can put
a bar and a number on without lying to the user.

## Why the naive approach breaks

Ollama's pull stream is not one steady sequence of "N% done" lines. In
practice, and this module studies the actual shape rather than assuming
one:

  - Early lines carry no byte counts at all: {"status": "pulling manifest"}.
    There is nothing to compute a fraction from yet.
  - Layer-download lines carry a digest plus completed/total byte counts:
    {"status": "pulling sha256:abcd...", "digest": "sha256:abcd...",
     "total": 4700000000, "completed": 1200000000}.
  - The SAME digest is reported more than once as its download advances,
    and (over a real, possibly-buffered HTTP connection) a later line for
    one digest can be interleaved with an earlier-arriving line for a
    different digest, or even, in principle, arrive with a smaller
    "completed" value than a line already processed for that same digest
    (a slow write flushed after a faster one, a retried chunk, etc).
  - The full list of layers is never announced up front. Ollama downloads
    them one at a time and only reveals a layer's existence (and its
    total) when it starts that layer. So "bytes total across the whole
    pull" is itself something this module discovers progressively, not
    something it is ever handed in one line.
  - Trailing lines carry no byte counts either: "verifying sha256 digest",
    "writing manifest", then a final {"status": "success"}.

A percentage built from "whatever the latest line says" jumps backwards
whenever an out-of-order or repeated line arrives, and jumps sideways
whenever the discovered total grows. Both look like a broken progress bar
to a user staring at a multi-gigabyte download, which is exactly the
experience this module exists to prevent.

## The progress model

Every digest is tracked independently (_update_layer), and both its total
and its completed count are only ever allowed to increase (max, never
overwrite). bytes_done is the sum of completed across every digest seen so
far; it is therefore monotonically non-decreasing across the whole pull by
construction, not by accident. bytes_total is the sum of the totals
discovered so far and IS allowed to grow as new layers are announced (a
denominator that grows because more real work was discovered is honest;
a numerator that shrinks because a line arrived out of order is not). The
callback also gets a smoothed speed and ETA (_consume_pull_stream's EMA,
sampled no more often than MIN_DT_FOR_SPEED apart) rather than an
instantaneous rate computed between two arbitrarily close NDJSON lines,
which would swing wildly on line-arrival jitter alone.

When a line carries no byte counts, phase text is the only thing that
updates; bytes_done/bytes_total are carried forward unchanged rather than
invented. See PullProgress-shaped dicts from on_progress() for the exact
fields.

Success is decided by Ollama's own explicit signal, not inferred from the
numbers reaching 100%: only a line with status == "success" (observed
real-world Ollama behaviour) sets ok=True. Every other way the stream can
end - a clean disconnect with no such line, a line carrying an "error"
key, cancellation, or the overall timeout - is ok=False. This is a
deliberate asymmetry: trusting a byte count that happens to reach the
total is exactly the kind of inference this module refuses to make
elsewhere (see hearth_hw and hearth_bench's "never predict, only report
what was actually said" stance), and if a future Ollama build renames the
terminal status, this module fails safe (reports "not confirmed done"),
never the other way around.

## Cancellation

pull() takes is_cancelled, a zero-argument callable the caller controls,
checked before the request is even opened and then on every chunk of the
response (not just between whole NDJSON lines, so a single very large,
slow-arriving line cannot delay cancellation).

The real HTTP path reads the response body on a background thread and
hands chunks to the main thread over a queue, polled with a short timeout
(POLL_INTERVAL_SECONDS) so is_cancelled() and the overall deadline are
both re-checked regularly. This is deliberately NOT "set a short read
timeout on the socket and retry": that was the first approach tried here,
and it breaks. CPython's socket file objects permanently mark themselves
unusable ("OSError: cannot read from timed out object") the moment any
read on them times out, even after the timeout is later cleared or
raised - there is no such thing as retrying a read on a socket that has
already timed out once. That surfaced empirically while writing this
module's own self-test (see _open_pull_stream's docstring) and shaped the
design: the connect phase gets a bounded timeout, then the body-reading
socket is left with none at all, and cancellation instead works by
closing the connection from outside while the background thread is
blocked in a read - a close reliably raises in a thread blocked on that
socket, on both Windows and POSIX, and does not carry the same
poison-the-socket problem a timeout does. Cancelling a multi-gigabyte
download this way stops it within about one POLL_INTERVAL_SECONDS.

## Disk space, checked up front

free_space_for_models() and model_store_dir() answer "is there room" and
"where would this land", so pull() can refuse before opening a connection
at all when the numbers are known and insufficient. See model_store_dir's
own docstring for exactly what is assumed about Ollama's storage layout
and how confident that assumption is - this module does not reuse
hearth_shop.free_disk_bytes() because that function is explicit about
measuring the current working directory's volume, which is not where
Ollama necessarily keeps its blobs.

Ollama's local HTTP API has no endpoint that reports a model's download
size before that model is pulled (see estimate_required_bytes's
docstring), so the up-front check is only as good as what the caller can
supply. pull() accepts an explicit expected_bytes from the caller (e.g.
hearth_shop.CATALOG's download_bytes for a cataloged model) and prefers
it; only when neither the caller nor estimate_required_bytes can produce a
number does the up-front refusal step out of the way rather than either
blocking on missing information or inventing a threshold to check it
against.

## Digest recording

On a successful pull, the digest and byte size of every layer Ollama
reported is written to a small JSON file under
hearth_paths.data_dir()/pulls/<model>.json (see pull_record_path() /
load_pull_record()). That is the entire scope of this module's
"verification" story: it records what Ollama said it wrote, as a baseline
a later, separate integrity check could compare against. It does not
compute or check a hash itself - inventing a verification scheme here
would be worse than not having one, see the module's overall stance on
not fabricating confidence.

## Failure handling

Ollama not running, an unknown model name, a dropped connection mid-pull,
and insufficient disk space up front are all covered explicitly below (see
pull()'s docstring). Every path returns a result dict; nothing this module
does on the network raises out to the caller. Partial-download
resumability (does pulling the same model again pick up where a previous
attempt left off) is Ollama's own server-side behaviour, not this
module's; nothing here claims or relies on it, this module can only
report whatever byte counts Ollama's next pull happens to start from.

Standard library only, Python 3.12+. Never prints (the CLI entry point at
the bottom of this file is the one exception, since printing is its whole
job). Writes nothing outside hearth_paths.data_dir()/pulls/, documented
above.
"""

import argparse
import http.client
import http.server
import io
import json
import os
import platform
import queue
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_paths  # noqa: E402

DEFAULT_OLLAMA = "http://127.0.0.1:11434"

# How long the background reader thread's queue is polled for before this
# module gets control back to re-check is_cancelled() and the overall
# deadline. This is NOT a socket read timeout (see _open_pull_stream's
# docstring for why that approach does not work) - it only bounds how
# often the polling loop wakes up, so it can be small without any risk of
# poisoning the underlying connection.
POLL_INTERVAL_SECONDS = 0.5

# How long _open_pull_stream's initial TCP connect is allowed to take
# before giving up. Applies only to connecting, not to reading the
# response body afterward - see _open_pull_stream's docstring for why
# those two have to be different.
CONNECT_TIMEOUT_SECONDS = 30

# The overall wall-clock budget for one pull() call, a backstop against a
# connection that keeps trickling occasional bytes forever without ever
# finishing or erroring out. Generous on purpose: a multi-gigabyte model on
# a slow connection can legitimately take a long time, and the thing that
# should actually stop an unwanted download promptly is is_cancelled, not
# this timeout.
DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60

# Beyond the raw download, Ollama needs a little slack as the file lands.
# Mirrors hearth_shop.DISK_BUFFER_BYTES's exact value and reasoning; not
# imported from there to keep this module's disk arithmetic self-contained
# and testable without dragging in hearth_hw's hardware-probing self-test.
DISK_BUFFER_BYTES = 512 * 1024 ** 2

# Speed is only re-sampled when at least this much wall-clock time has
# passed since the last sample, so a burst of NDJSON lines arriving in the
# same instant (or the same instant on a fake clock in a test) cannot
# divide by a near-zero time delta and produce a wild instantaneous value.
MIN_DT_FOR_SPEED = 0.25

# Exponential moving average weight for the speed estimate: low enough that
# one unusually fast or slow interval does not swing the displayed number,
# high enough that the estimate still tracks a real, sustained change in
# throughput within a few samples.
SPEED_EMA_ALPHA = 0.3

SHOW_TIMEOUT = 8

PULLS_SUBDIR = "pulls"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _human_bytes(n):
    """A short human-readable rendering of a byte count, or "an unknown
    amount" for None. Used only in error/refusal messages.
    """
    if n is None:
        return "an unknown amount"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return "{:.0f} {}".format(n, unit) if unit == "B" else "{:.1f} {}".format(n, unit)
        n /= 1024.0
    return "{:.1f} PB".format(n)


# --------------------------------------------------------------------------
# Chokepoints: every network call this module makes goes through one of
# these, so a self-test can replace them with fixtures and exercise the
# parsing/progress/cancellation logic without Ollama, a network, or a real
# download. Same pattern as hearth_hw.py's _run and hearth_bench.py's
# _http_post/_http_get.
# --------------------------------------------------------------------------

class _StreamedResponse:
    """Pairs an http.client response with the connection it came from, so
    closing one closes both. Exposes only read(n)/close(), the interface
    _iter_stream_lines needs - the same shape the self-test's
    _FakeStreamResponse provides, so both are interchangeable there.

    Takes `sock` explicitly rather than reading it back off `conn` later:
    Ollama's pull response carries no Content-Length (it is an open-ended
    stream), and http.client.HTTPConnection.getresponse() reacts to that by
    detaching the connection from its socket right away (response.will_close
    is True, so getresponse() calls self.close(), which sets conn.sock to
    None even though the response's own reader keeps the real socket alive
    underneath it). Fetching sock from conn AFTER that point silently finds
    None and skips the shutdown below, which defeats cancellation entirely.
    Capturing the live reference at connect time, before that detach can
    happen, is what makes shutdown() actually reach the real socket.
    """

    def __init__(self, conn, resp, sock):
        self._conn = conn
        self._resp = resp
        self._sock = sock

    def read(self, n=8192):
        # read1(), not read(): HTTPResponse.read(n) is backed by an
        # io.BufferedReader, which for a positive size keeps issuing
        # further underlying reads until it collects the full n bytes
        # requested (or hits EOF) - it does not return early just because
        # some data has already arrived. For a body that is a genuine
        # stream of small NDJSON lines (a 30-byte status line, arriving
        # alone, with the next line seconds away) that means a plain
        # read(8192) sits blocked well past when the line that already
        # arrived could have been handed back. read1(n) is documented to
        # make "at most one underlying system call" and return whatever
        # is already available, which is the short-read behaviour actual
        # streaming needs. Confirmed empirically while writing this
        # module's self-test: with plain read(), a line written and
        # flushed by the server sat unseen by this side for the entire
        # gap before the next write, even though the socket already had
        # it buffered.
        return self._resp.read1(n)

    def close(self):
        """Force this connection closed right now, even if the background
        reader thread in _iter_stream_lines is currently blocked inside a
        read on it. Two mechanisms, both needed - verified empirically
        while writing this module's own self-test, not assumed:

          1. sock.shutdown(SHUT_RDWR). On POSIX this is normally already
             enough by itself: shutting down a socket wakes any thread
             blocked in recv() on it.

          2. On Windows, shutdown() alone does NOT wake a thread already
             blocked in a blocking recv() - confirmed directly against
             this platform. Neither does a plain sock.close(): the socket
             module defers the real close (WinSock closesocket()) for as
             long as _io_refs > 0, which it always is here, since
             http.client wraps this socket in a buffered reader via
             makefile(). Closing that buffered reader (resp.close()) DOES
             eventually trigger the real close - but io.BufferedReader.
             close() shares an internal lock with io.BufferedReader.read(),
             so calling it from this thread while the reader thread is
             mid-read simply blocks HERE until that read finishes on its
             own, which is exactly the delay cancellation exists to avoid.
             Forcing _io_refs to 0 and closing directly makes the real,
             immediate close happen without going anywhere near that lock.
             This reaches into a private attribute of the standard
             library's socket module; that is a deliberate, verified last
             resort for a real platform gap, not a guess.
        """
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock._io_refs = 0
                self._sock.close()
            except (AttributeError, OSError):
                pass
        try:
            self._resp.close()
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass


def _open_pull_stream(base_url, model, connect_timeout=CONNECT_TIMEOUT_SECONDS):
    """POST base_url/api/pull and return a live, streaming response object.

    Built directly on http.client rather than urllib.request.urlopen so
    the connect-phase timeout (connect_timeout, bounding how long this
    call can block if Ollama's host is unreachable) can be cleared before
    any response BODY bytes are read. That split is required, not
    cosmetic: Ollama can legitimately go quiet for a long stretch
    mid-stream with no bytes at all (hashing a large model's digest
    during "verifying sha256 digest" can take a while), and CPython's
    socket file objects permanently mark themselves unusable ("OSError:
    cannot read from timed out object") the instant any read on them
    times out - clearing or raising the timeout afterward does not
    un-poison it, there is no such thing as retrying a read on a socket
    that has already timed out once. This was discovered empirically
    while writing this module's own self-test, not assumed up front. See
    _iter_stream_lines for how cancellation and the overall deadline are
    enforced instead, without ever imposing a timeout on the body reads.

    Sends both "model" and "name" for the target model: current Ollama
    versions read "model", but including the older "name" key too is
    harmless against a server that only recognises one of them.

    Raises urllib.error.HTTPError for a non-2xx response (so callers can
    handle it the same way regardless of which HTTP client built it), or
    whatever http.client/socket raise on a connection failure (OSError,
    TimeoutError). pull() is responsible for turning any of that into a
    result dict rather than letting it propagate.
    """
    url = base_url.rstrip("/") + "/api/pull"
    split = urllib.parse.urlsplit(url)
    host = split.hostname or "127.0.0.1"
    conn_cls = http.client.HTTPSConnection if split.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, split.port, timeout=connect_timeout)
    conn.connect()
    # See docstring: the body-reading socket must never time out, so this
    # is cleared the moment the connection itself is established. sock is
    # also captured here, not read back off conn later: see
    # _StreamedResponse's docstring for why conn.sock cannot be trusted to
    # still be set by the time close() runs.
    sock = conn.sock
    sock.settimeout(None)

    payload = json.dumps({"model": model, "name": model, "stream": True}).encode("utf-8")
    request_path = split.path or "/"
    if split.query:
        request_path += "?" + split.query
    conn.request("POST", request_path, body=payload, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()

    if resp.status >= 400:
        body = resp.read()
        try:
            resp.close()
        finally:
            conn.close()
        raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, io.BytesIO(body))

    return _StreamedResponse(conn, resp, sock)


def _http_get_json(url, timeout):
    """GET url, return the parsed JSON body. Raises on failure; callers
    decide how to degrade.
    """
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _iter_stream_lines(resp, is_cancelled, deadline, poll_interval, now_fn):
    """Read resp incrementally and yield each non-empty NDJSON line as a
    decoded str, without ever holding the whole response in memory.

    The actual resp.read(8192) call happens on a background thread, which
    hands chunks back over a Queue that this generator polls with a
    poll_interval timeout - so is_cancelled() and the overall deadline
    (computed from now_fn()) are both re-checked regularly, without ever
    putting a timeout on the socket read itself. See _open_pull_stream's
    docstring for why a socket read timeout is not viable here (it
    permanently poisons the socket for further reads on this platform's
    stdlib). When cancellation or the deadline fires, resp.close() is
    called from this (main) thread, which reliably interrupts the
    background thread's blocked read with an OSError on both Windows and
    POSIX - the actual mechanism that makes cancellation prompt.

    Any exception from the background read (a genuine dropped connection,
    a chunked-encoding stream cut off mid-chunk, ...) propagates out of
    this generator to the caller, the honest signal that the pull did not
    finish cleanly. Raises TimeoutError itself if `deadline` passes before
    the stream ends. Stops (a plain return, ending the generator) as soon
    as is_cancelled() reports True.
    """
    chunks = queue.Queue()
    stop = threading.Event()

    def _read_loop():
        try:
            while not stop.is_set():
                chunk = resp.read(8192)
                chunks.put(("data", chunk))
                if not chunk:
                    return
        except Exception as exc:  # noqa: BLE001 - handed to the main thread, not lost
            if not stop.is_set():
                chunks.put(("error", exc))

    reader = threading.Thread(target=_read_loop, daemon=True)
    reader.start()

    buf = b""
    try:
        while True:
            if is_cancelled is not None and is_cancelled():
                return
            if now_fn() >= deadline:
                raise TimeoutError("pull exceeded its overall timeout budget")
            try:
                kind, payload = chunks.get(timeout=poll_interval)
            except queue.Empty:
                continue
            if kind == "error":
                raise payload
            if not payload:
                break
            buf += payload
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.strip()
                if text:
                    yield text.decode("utf-8", errors="replace")
    finally:
        # Unblocks the background thread if it is mid-read: closing the
        # connection from here is what makes cancellation and the
        # deadline both prompt, not just eventually consistent.
        stop.set()
        try:
            resp.close()
        except Exception:
            pass
    tail = buf.strip()
    if tail:
        yield tail.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Progress state: per-digest tracking, kept monotonic by construction.
# --------------------------------------------------------------------------

def _update_layer(layers, digest, total, completed):
    """Merge one line's (total, completed) reading for `digest` into
    `layers` (a dict keyed by digest). Both fields are clamped to never
    decrease: a repeated or out-of-order line for a digest already seen
    can only hold bytes_done steady or advance it, never pull it back.
    """
    entry = layers.get(digest)
    if entry is None:
        entry = {"total_bytes": None, "completed_bytes": 0}
        layers[digest] = entry
    if isinstance(total, int) and total >= 0:
        if entry["total_bytes"] is None or total > entry["total_bytes"]:
            entry["total_bytes"] = total
    if isinstance(completed, int) and completed >= 0:
        if completed > entry["completed_bytes"]:
            entry["completed_bytes"] = completed
    return entry


def _layers_totals(layers):
    """(bytes_done, bytes_total) across every digest seen so far.
    bytes_total is None until at least one digest has reported a total;
    it only ever grows as more digests are discovered (see module
    docstring on why a growing denominator is honest, unlike a shrinking
    numerator).
    """
    done = sum(e["completed_bytes"] for e in layers.values())
    totals = [e["total_bytes"] for e in layers.values() if e["total_bytes"] is not None]
    total = sum(totals) if totals else None
    return done, total


def _consume_pull_stream(lines, on_progress=None, is_cancelled=None, now_fn=None):
    """Drive the state machine over an iterable of raw NDJSON line strings,
    calling on_progress(dict) after every successfully parsed line and
    returning a final result dict once the iterable ends, cancellation is
    requested, or an "error"/"success" status line is seen.

    `lines` can be a plain list (as the self-test uses) or a live
    generator such as _iter_stream_lines (as pull() uses); this function
    does not care which, and never buffers more than the current line.

    Never raises: an exception surfacing while pulling the next line
    (a real network drop propagating out of a live generator, or a bug in
    is_cancelled) is caught and turned into an ok: False result carrying
    whatever progress had already been made, exactly like every other
    failure path here.

    Returns a dict with ok, cancelled, error, phase, bytes_done,
    bytes_total, elapsed_seconds, layers (a list of
    {digest, total_bytes, completed_bytes, complete}, sorted by digest -
    the baseline record pull() persists on success), and last_progress
    (the last dict handed to on_progress, or None if no line produced one).
    """
    now_fn = now_fn or time.monotonic
    layers = {}
    phase = "starting"
    ok = False
    error = None
    cancelled = False
    prev_time = None
    prev_bytes = 0
    speed = None
    last_progress = None
    start_time = now_fn()
    last_now = start_time

    def emit(current_digest):
        nonlocal prev_time, prev_bytes, speed, last_progress, last_now
        done, total = _layers_totals(layers)
        now = now_fn()
        last_now = now
        if prev_time is None:
            prev_time, prev_bytes = now, done
        else:
            dt = now - prev_time
            if dt >= MIN_DT_FOR_SPEED:
                inst = (done - prev_bytes) / dt
                if inst >= 0:
                    speed = inst if speed is None else (
                        SPEED_EMA_ALPHA * inst + (1 - SPEED_EMA_ALPHA) * speed
                    )
                prev_time, prev_bytes = now, done
        fraction = None
        if total:
            fraction = max(0.0, min(1.0, done / total))
        eta = None
        if speed and speed > 0 and total is not None:
            eta = max(0, total - done) / speed
        progress = {
            "phase": phase,
            "bytes_done": done,
            "bytes_total": total,
            "fraction": fraction,
            "speed_bytes_per_sec": speed,
            "eta_seconds": eta,
            "current_digest": current_digest,
            "layers_seen": len(layers),
            "layers_complete": sum(
                1 for e in layers.values()
                if e["total_bytes"] is not None and e["completed_bytes"] >= e["total_bytes"]
            ),
            "elapsed_seconds": now - start_time,
        }
        last_progress = progress
        if on_progress is not None:
            try:
                on_progress(progress)
            except Exception:
                # A bug in the caller's UI callback must not corrupt or
                # abort an in-flight multi-gigabyte download.
                pass
        return progress

    try:
        iterator = iter(lines)
        while True:
            if is_cancelled is not None and is_cancelled():
                cancelled = True
                error = "pull cancelled"
                break
            try:
                raw = next(iterator)
            except StopIteration:
                break
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue

            err = data.get("error")
            if err:
                error = str(err)
                break

            status = data.get("status")
            if isinstance(status, str) and status:
                phase = status

            digest = data.get("digest")
            current_digest = None
            if isinstance(digest, str) and digest:
                current_digest = digest
                total = data.get("total")
                completed = data.get("completed")
                _update_layer(
                    layers, digest,
                    total if isinstance(total, int) else None,
                    completed if isinstance(completed, int) else None,
                )

            emit(current_digest)

            if status == "success":
                ok = True
                break
    except Exception as exc:  # a dropped connection, a buggy is_cancelled, ...
        error = "pull stream interrupted: {}: {}".format(type(exc).__name__, exc)

    if not ok and not cancelled and error is None:
        error = "stream ended before Ollama reported success (last phase: '{}')".format(phase)

    done, total = _layers_totals(layers)
    return {
        "ok": ok,
        "cancelled": cancelled,
        "error": error,
        "phase": phase,
        "bytes_done": done,
        "bytes_total": total,
        "elapsed_seconds": last_now - start_time,
        "layers": [
            {
                "digest": d,
                "total_bytes": e["total_bytes"],
                "completed_bytes": e["completed_bytes"],
                "complete": e["total_bytes"] is not None and e["completed_bytes"] >= e["total_bytes"],
            }
            for d, e in sorted(layers.items())
        ],
        "last_progress": last_progress,
    }


# --------------------------------------------------------------------------
# Model store location and free space
# --------------------------------------------------------------------------

def model_store_dir():
    """Best-effort path to the directory Ollama stores its downloaded model
    blobs in, or None if even a guess is not possible (no resolvable home
    directory).

    What this assumes, and how confident it is:
      1. OLLAMA_MODELS, when set, is Ollama's own documented override for
         this location. Trusted outright - confidence: certain.
      2. On Linux, when /usr/share/ollama/.ollama/models already exists,
         it is preferred: this is the layout Ollama's official install
         script creates when it registers a systemd service running as a
         dedicated "ollama" user (whose home is /usr/share/ollama).
         Confidence: high, but only checked as an existence probe - this
         module never assumes it, only prefers it if it is already there.
      3. Otherwise, <home>/.ollama/models is used - the default location
         Ollama documents for a normal, non-service install on every
         platform (Linux, macOS, and Windows, where <home> resolves to
         C:\\Users\\<name> via os.path.expanduser). Confidence: high for
         Linux/macOS (long-standing, widely observed default); medium for
         Windows specifically, since this module's author has not
         independently reverified the Windows installer's default against
         a live install on this branch - if Windows Ollama ever diverges
         (e.g. an %LOCALAPPDATA%\\Ollama layout), this heuristic would be
         wrong and free_space_for_models() would be checking the wrong
         volume. OLLAMA_MODELS remains the reliable override either way.

      This function does no writing and no writability probing (unlike
      hearth_paths.data_dir()): Ollama's own server process owns writing
      here, not this module, so all that matters is picking the right
      volume to measure free space on.
    """
    env = os.environ.get("OLLAMA_MODELS")
    if env:
        return env
    home = os.path.expanduser("~")
    if not home or home == "~":
        return None
    candidates = []
    if platform.system() == "Linux":
        candidates.append("/usr/share/ollama/.ollama/models")
    default = os.path.join(home, ".ollama", "models")
    candidates.append(default)
    for c in candidates:
        if os.path.isdir(c):
            return c
    # Nothing exists yet (this machine has never pulled a model with
    # Ollama, or the store lives somewhere this heuristic cannot see) -
    # return the documented default anyway, so callers still have a
    # volume to check free space against rather than nothing at all.
    return default


def free_space_for_models():
    """Free bytes on the volume that would hold a newly pulled model, per
    model_store_dir(). None if the location or its free space cannot be
    determined; never raises.

    Walks up to the nearest existing ancestor directory before measuring,
    since model_store_dir() may return a path that does not exist yet on
    a machine that has never pulled anything with Ollama - the volume it
    would land on is still a meaningful thing to check.
    """
    path = model_store_dir()
    if not path:
        return None
    check = path
    while check and not os.path.exists(check):
        parent = os.path.dirname(check)
        if not parent or parent == check:
            check = None
            break
        check = parent
    if not check:
        return None
    try:
        return shutil.disk_usage(check).free
    except OSError:
        return None


def _find_tag_entry(models, model):
    """Match a requested model name against /api/tags entries, exact name
    first, then the part before the colon on both sides (a bare model
    name should match a tagged "model:latest" entry and vice versa). Same
    approach as hearth_bench._find_ps_entry, applied to a different
    endpoint.
    """
    for entry in models:
        name = entry.get("name") or entry.get("model")
        if name == model:
            return entry
    base = model.split(":", 1)[0]
    for entry in models:
        name = entry.get("name") or entry.get("model") or ""
        if name.split(":", 1)[0] == base:
            return entry
    return None


def estimate_required_bytes(base_url, model):
    """The download size of `model`, in bytes, or None when it cannot be
    known honestly.

    Ollama's local HTTP API has no endpoint that reports how large a
    pull will be BEFORE it happens: the total is only revealed
    incrementally, one already-in-progress layer at a time, inside the
    /api/pull stream itself (see the module docstring). There is no
    dry-run or manifest-only mode to call instead.

    What this function actually does: check GET /api/tags, which lists
    models Ollama already has locally with their on-disk size. That
    answers a narrower, real question - "how big is a model I already
    have, e.g. before a redundant re-pull or an update check" - and
    returns None for the common "pull something new" case this module
    cannot answer at all. Callers that know a model's approximate size
    another way (hearth_shop's CATALOG carries download_bytes for every
    cataloged model) should pass that to pull()'s expected_bytes instead
    of relying on this function for a model that is not local yet.

    Never raises: any failure (Ollama unreachable, malformed response,
    model absent from /api/tags) returns None.
    """
    base_url = (base_url or "").rstrip("/")
    if not base_url or not model:
        return None
    try:
        data = _http_get_json(base_url + "/api/tags", SHOW_TIMEOUT)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    models = data.get("models")
    if not isinstance(models, list):
        return None
    entry = _find_tag_entry(models, model)
    if entry is None:
        return None
    size = entry.get("size")
    return size if isinstance(size, int) and size > 0 else None


# --------------------------------------------------------------------------
# Digest recording: a baseline for a later integrity check, not a check
# itself.
# --------------------------------------------------------------------------

def _safe_filename(name):
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in (name or ""))
    return cleaned or "model"


def pull_record_path(model):
    """Where a successful pull's digest record is written for `model`."""
    return os.path.join(hearth_paths.data_dir(), PULLS_SUBDIR, _safe_filename(model) + ".json")


def _save_pull_record(model, record):
    """Write record as indented JSON. A write failure (read-only
    filesystem, disk full) is swallowed: a pull that just succeeded
    should not be reported as failed because the baseline record could
    not be persisted afterwards.
    """
    path = pull_record_path(model)
    parent = os.path.dirname(path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def load_pull_record(model):
    """The last successful pull's digest record for `model`, or None if
    there isn't one or it cannot be read.
    """
    path = pull_record_path(model)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# pull(): the public entry point
# --------------------------------------------------------------------------

def pull(base_url, model, on_progress=None, is_cancelled=None,
         timeout=DEFAULT_TIMEOUT_SECONDS, expected_bytes=None, now_fn=None):
    """Pull `model` from the Ollama server at base_url, calling
    on_progress(dict) as real progress arrives (see _consume_pull_stream's
    docstring for the dict's fields) and checking is_cancelled() regularly
    enough to abort within about POLL_INTERVAL_SECONDS of it turning True.

    Always returns a dict, never raises. Covers, each with ok: False and a
    clear error string:
      - Ollama not reachable at base_url at all (connection refused, DNS
        failure, ...).
      - model not found (Ollama reports this as an "error" line inside an
        otherwise-200 stream in current versions, or as a non-2xx HTTP
        status in others; both are handled).
      - the connection dropping mid-pull (propagates out of the live
        NDJSON generator and is caught, see _consume_pull_stream).
      - not enough free disk space, checked BEFORE any connection is
        opened, whenever a size is known (see below) - naming both how
        much is needed and how much is free.
      - cancellation (ok: False, cancelled: True).
      - the overall `timeout` budget being exceeded.

    Disk check: uses expected_bytes if the caller supplies it (e.g.
    hearth_shop.CATALOG's download_bytes for a cataloged model), else
    tries estimate_required_bytes(base_url, model) (honest about when it
    cannot answer - see its docstring). Compared against
    free_space_for_models(). When NEITHER produces a number, this module
    has nothing honest to refuse on, so the check is skipped rather than
    inventing a threshold; result["disk_check"]["checked"] says whether
    the comparison actually happened.

    On success, records what was pulled via _save_pull_record (see
    pull_record_path/load_pull_record) - a baseline for a later,
    separate integrity check, not a verification performed here.
    """
    now_fn = now_fn or time.monotonic
    base_url = (base_url or "").rstrip("/")
    result = {
        "ok": False,
        "error": None,
        "cancelled": False,
        "model": model,
        "base_url": base_url,
        "timestamp": _now_iso(),
        "phase": None,
        "bytes_done": 0,
        "bytes_total": None,
        "elapsed_seconds": 0.0,
        "layers": [],
        "expected_bytes": expected_bytes,
        "disk_check": None,
    }

    if not base_url:
        result["error"] = "empty base_url"
        return result
    if not model:
        result["error"] = "empty model"
        return result
    if is_cancelled is not None and is_cancelled():
        result["error"] = "pull cancelled before it started"
        result["cancelled"] = True
        return result

    want = expected_bytes if isinstance(expected_bytes, int) and expected_bytes > 0 else None
    if want is None:
        want = estimate_required_bytes(base_url, model)
    free = free_space_for_models()
    store_dir = model_store_dir()
    disk_check = {
        "expected_bytes": want,
        "free_bytes": free,
        "store_dir": store_dir,
        "checked": False,
        "ok": None,
    }
    if want is not None and free is not None:
        needed = want + DISK_BUFFER_BYTES
        disk_check["checked"] = True
        disk_check["ok"] = free >= needed
        if not disk_check["ok"]:
            result["disk_check"] = disk_check
            result["error"] = (
                "not enough free disk space to pull {}: need about {} "
                "(the download plus a safety margin), only {} free at {}".format(
                    model, _human_bytes(needed), _human_bytes(free),
                    store_dir or "the model store",
                )
            )
            return result
    result["disk_check"] = disk_check

    start = now_fn()
    deadline = start + max(1, timeout)

    try:
        resp = _open_pull_stream(base_url, model, CONNECT_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        result["error"] = "HTTP {} from Ollama: {}".format(exc.code, body[:300] or exc.reason)
        return result
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        result["error"] = "could not reach Ollama at {}: {}: {}".format(
            base_url, type(exc).__name__, exc,
        )
        return result

    try:
        lines = _iter_stream_lines(resp, is_cancelled, deadline, POLL_INTERVAL_SECONDS, now_fn)
        consumed = _consume_pull_stream(
            lines, on_progress=on_progress, is_cancelled=is_cancelled, now_fn=now_fn,
        )
    finally:
        try:
            resp.close()
        except Exception:
            pass

    result.update({
        "ok": consumed["ok"],
        "error": consumed["error"],
        "cancelled": consumed["cancelled"],
        "phase": consumed["phase"],
        "bytes_done": consumed["bytes_done"],
        "bytes_total": consumed["bytes_total"],
        "elapsed_seconds": consumed["elapsed_seconds"],
        "layers": consumed["layers"],
    })

    if result["ok"]:
        _save_pull_record(model, {
            "model": model,
            "base_url": base_url,
            "timestamp": result["timestamp"],
            "bytes_total": result["bytes_total"],
            "layers": result["layers"],
        })

    return result


# --------------------------------------------------------------------------
# Self-test fixtures
# --------------------------------------------------------------------------

# The exact NDJSON sequence documented in this module's docstring: a
# no-byte-count manifest line, a layer (aaa) reported repeatedly including
# an out-of-order line with a SMALLER completed value than already seen, a
# second layer (bbb) discovered partway through, two no-byte-count lines
# at the end, then success. Hand-computed bytes_done after each line is
# recorded alongside it so the self-test can pin the exact sequence, not
# just "it never decreases".
_FIXTURE_LINES = [
    '{"status":"pulling manifest"}',
    '{"status":"pulling sha256:aaa","digest":"sha256:aaa","total":1000,"completed":0}',
    '{"status":"pulling sha256:aaa","digest":"sha256:aaa","total":1000,"completed":400}',
    '{"status":"pulling sha256:bbb","digest":"sha256:bbb","total":500,"completed":100}',
    # Out of order: completed (250) is LESS than the 400 already recorded
    # for this same digest. A naive "just use the latest value" implementation
    # would drop bytes_done from 500 to 350 here; this must not happen.
    '{"status":"pulling sha256:aaa","digest":"sha256:aaa","total":1000,"completed":250}',
    # Repeated report of a layer already seen twice, now complete.
    '{"status":"pulling sha256:aaa","digest":"sha256:aaa","total":1000,"completed":1000}',
    '{"status":"pulling sha256:bbb","digest":"sha256:bbb","total":500,"completed":500}',
    '{"status":"verifying sha256 digest"}',
    '{"status":"writing manifest"}',
    '{"status":"success"}',
]
_FIXTURE_BYTES_DONE = [0, 0, 400, 500, 500, 1100, 1500, 1500, 1500, 1500]


class _FakeStreamResponse:
    """A minimal stand-in for the object _open_pull_stream returns: exposes
    .read(n) over an in-memory byte buffer and a no-op .close(), so pull()
    can be exercised end-to-end (disk check, streaming, record persistence)
    without a real socket at all.
    """

    def __init__(self, text):
        self._data = text.encode("utf-8")
        self._pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self):
        pass


class _FakePullHandler(http.server.BaseHTTPRequestHandler):
    """Just enough of Ollama's streaming /api/pull to exercise the real
    urllib.request + chunked-read code path (_open_pull_stream,
    _iter_stream_lines) without a real Ollama install. Two behaviours,
    selected by the requested model name:
      - "fixture-model": writes _FIXTURE_LINES with small delays and
        explicit flushes, then closes - a real (if artificially slow)
        successful stream.
      - "slow-model": writes one line, then sleeps far longer than any
        reasonable poll interval before writing more - used to prove
        cancellation actually aborts a real, still-open connection
        promptly rather than waiting for the server.
    """

    def log_message(self, *a, **k):  # keep self-test output quiet
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}
        model = body.get("model")

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()

        if model == "slow-model":
            self.wfile.write((_FIXTURE_LINES[0] + "\n").encode("utf-8"))
            self.wfile.flush()
            time.sleep(5.0)
            self.wfile.write((_FIXTURE_LINES[-1] + "\n").encode("utf-8"))
            return

        for line in _FIXTURE_LINES:
            self.wfile.write((line + "\n").encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.01)


def _run_live_http_self_test():
    """Exercise the real urllib-based streaming path (_open_pull_stream,
    _iter_stream_lines) against a throwaway local HTTP server: real
    sockets, real chunked reads, a real per-read timeout. This is as close
    as a self-test can get to proving the live path works without an
    actual Ollama install.

    Also proves cancellation aborts a real, still-open, slow-to-respond
    connection well before the server would have finished on its own -
    the property that matters most for "a user must be able to abort a
    multi-gigabyte download and have it stop promptly".

    Skips (does not fail the suite) if this sandbox will not allow
    binding a loopback socket.
    """
    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), _FakePullHandler)
    except OSError as exc:
        print("hearth-pull: skipping live-http self-test, no loopback socket ({})".format(exc))
        return
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = "http://127.0.0.1:{}".format(port)

        seen = []
        r = pull(base_url, "fixture-model", on_progress=lambda p: seen.append(p["bytes_done"]),
                  timeout=30)
        assert r["ok"] is True, r
        assert r["bytes_done"] == 1500, r
        assert r["bytes_total"] == 1500, r
        assert seen == _FIXTURE_BYTES_DONE, seen
        assert len(r["layers"]) == 2, r
        digests = {l["digest"]: l for l in r["layers"]}
        assert digests["sha256:aaa"]["completed_bytes"] == 1000, r
        assert digests["sha256:bbb"]["completed_bytes"] == 500, r
        assert all(l["complete"] for l in r["layers"]), r

        # Cancellation: the server holds the connection open for 5s after
        # its first line. A cancel flag that flips True after that first
        # progress callback must stop pull() in well under 5s - proving
        # this is a real abort of a live socket, not a hope that the
        # server eventually finishes on its own.
        cancel_after = {"flag": False}

        def on_progress(p):
            cancel_after["flag"] = True

        t0 = time.monotonic()
        r_cancel = pull(
            base_url, "slow-model",
            on_progress=on_progress,
            is_cancelled=lambda: cancel_after["flag"],
            timeout=30,
        )
        elapsed = time.monotonic() - t0
        assert r_cancel["ok"] is False, r_cancel
        assert r_cancel["cancelled"] is True, r_cancel
        assert elapsed < 4.0, (
            "cancellation must abort a live, still-open connection within a "
            "couple of poll intervals, not wait for the server", elapsed,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("hearth-pull: live-http self-test OK (real sockets, real cancellation)")


def _self_test():
    global _open_pull_stream, _http_get_json

    orig_open_stream = _open_pull_stream
    orig_get_json = _http_get_json
    old_env = {k: os.environ.get(k) for k in ("HEARTH_DATA_DIR", "OLLAMA_MODELS")}

    try:
        # -- _update_layer / _layers_totals: basic monotonic merge --------
        layers = {}
        _update_layer(layers, "d1", 100, 10)
        _update_layer(layers, "d1", 100, 40)
        _update_layer(layers, "d1", 100, 20)  # smaller than 40 already seen
        assert layers["d1"]["completed_bytes"] == 40, layers
        done, total = _layers_totals(layers)
        assert (done, total) == (40, 100), (done, total)

        # -- _consume_pull_stream: THE monotonicity pin --------------------
        # Exact bytes_done after every one of the ten fixture lines,
        # including the out-of-order line (index 4) that a naive
        # "overwrite with latest" implementation would have dropped from
        # 500 to 350. Pinning the full sequence (not just "non-decreasing")
        # proves this specific bug is closed, not merely untriggered.
        seen = []
        result = _consume_pull_stream(
            list(_FIXTURE_LINES), on_progress=lambda p: seen.append(p["bytes_done"]),
        )
        assert seen == _FIXTURE_BYTES_DONE, seen
        assert all(seen[i] <= seen[i + 1] for i in range(len(seen) - 1)), seen
        assert result["ok"] is True, result
        assert result["error"] is None, result
        assert result["bytes_done"] == 1500, result
        assert result["bytes_total"] == 1500, result
        assert result["phase"] == "success", result
        assert len(result["layers"]) == 2, result
        layers_by_digest = {l["digest"]: l for l in result["layers"]}
        assert layers_by_digest["sha256:aaa"] == {
            "digest": "sha256:aaa", "total_bytes": 1000, "completed_bytes": 1000, "complete": True,
        }, layers_by_digest
        assert layers_by_digest["sha256:bbb"] == {
            "digest": "sha256:bbb", "total_bytes": 500, "completed_bytes": 500, "complete": True,
        }, layers_by_digest

        # -- a status line with no byte counts must not touch bytes_done --
        # (already exercised above at indices 0, 7, 8 of _FIXTURE_BYTES_DONE,
        # asserted explicitly here too so this property has its own name)
        assert _FIXTURE_BYTES_DONE[6] == _FIXTURE_BYTES_DONE[7] == _FIXTURE_BYTES_DONE[8], (
            "the two no-byte-count status lines (verifying, writing manifest) "
            "must leave bytes_done exactly unchanged", _FIXTURE_BYTES_DONE,
        )

        # -- a stream that ends abruptly: truncate before the success line -
        abrupt = _FIXTURE_LINES[:6]  # ends right after the "aaa completed:1000" repeat
        seen_abrupt = []
        r_abrupt = _consume_pull_stream(
            abrupt, on_progress=lambda p: seen_abrupt.append(p["bytes_done"]),
        )
        assert r_abrupt["ok"] is False, r_abrupt
        assert r_abrupt["cancelled"] is False, r_abrupt
        assert "stream ended" in r_abrupt["error"].lower(), r_abrupt
        assert r_abrupt["bytes_done"] == 1100, r_abrupt
        assert seen_abrupt == _FIXTURE_BYTES_DONE[:6], seen_abrupt

        # -- an explicit "error" line stops the stream and is surfaced -----
        error_lines = [
            '{"status":"pulling manifest"}',
            '{"error":"pull model manifest: file does not exist"}',
        ]
        r_err = _consume_pull_stream(error_lines)
        assert r_err["ok"] is False, r_err
        assert "file does not exist" in r_err["error"], r_err

        # -- a malformed JSON line is skipped, not fatal --------------------
        messy = [
            '{"status":"pulling manifest"}',
            "not json at all {{{",
            '{"status":"pulling sha256:zzz","digest":"sha256:zzz","total":10,"completed":10}',
            '{"status":"success"}',
        ]
        r_messy = _consume_pull_stream(messy)
        assert r_messy["ok"] is True, r_messy
        assert r_messy["bytes_done"] == 10, r_messy

        # -- cancellation: stops promptly, reports cancelled distinctly ----
        calls = {"n": 0}

        def cancel_after_three():
            calls["n"] += 1
            return calls["n"] > 3

        seen_cancel = []
        r_cancel = _consume_pull_stream(
            list(_FIXTURE_LINES),
            on_progress=lambda p: seen_cancel.append(p["bytes_done"]),
            is_cancelled=cancel_after_three,
        )
        assert r_cancel["ok"] is False, r_cancel
        assert r_cancel["cancelled"] is True, r_cancel
        assert r_cancel["error"] == "pull cancelled", r_cancel
        # Cancelled after the 3rd is_cancelled() check succeeds (i.e. right
        # before a 4th line would be processed) - exactly the first three
        # fixture lines' worth of progress, no more.
        assert seen_cancel == _FIXTURE_BYTES_DONE[:3], seen_cancel

        # -- speed/ETA smoothing: deterministic via an injected clock -------
        # Two lines a fixed 1.0s apart (>= MIN_DT_FOR_SPEED), 100 bytes
        # apart, must yield an exact instantaneous speed of 100 B/s on the
        # first real sample, then an EMA-blended value on the next.
        clock = iter([0.0, 0.0, 1.0, 2.0])
        speed_lines = [
            '{"status":"pulling sha256:s","digest":"sha256:s","total":1000,"completed":0}',
            '{"status":"pulling sha256:s","digest":"sha256:s","total":1000,"completed":100}',
            '{"status":"pulling sha256:s","digest":"sha256:s","total":1000,"completed":250}',
        ]
        speeds = []
        _consume_pull_stream(
            speed_lines, on_progress=lambda p: speeds.append(p["speed_bytes_per_sec"]),
            now_fn=lambda: next(clock),
        )
        assert speeds[0] is None, speeds  # first sample only establishes the baseline
        assert abs(speeds[1] - 100.0) < 1e-9, speeds  # 100 bytes / 1.0s
        expected_ema = SPEED_EMA_ALPHA * 150.0 + (1 - SPEED_EMA_ALPHA) * 100.0  # 150 bytes / 1.0s
        assert abs(speeds[2] - expected_ema) < 1e-9, (speeds, expected_ema)

        # -- ETA uses the smoothed speed and the known total -----------------
        etas = []
        clock2 = iter([0.0, 0.0, 1.0])
        _consume_pull_stream(
            speed_lines[:2], on_progress=lambda p: etas.append(p["eta_seconds"]),
            now_fn=lambda: next(clock2),
        )
        assert etas[0] is None, etas
        assert abs(etas[1] - (1000 - 100) / 100.0) < 1e-9, etas

        # -- model_store_dir(): OLLAMA_MODELS always wins --------------------
        os.environ["OLLAMA_MODELS"] = os.path.join("Z:", "custom-models") if hearth_paths.is_windows() else "/tmp/custom-models"
        assert model_store_dir() == os.environ["OLLAMA_MODELS"], model_store_dir()
        os.environ.pop("OLLAMA_MODELS", None)

        # -- model_store_dir()/free_space_for_models(): plausible shape on --
        # -- whatever real machine runs this self-test ----------------------
        store = model_store_dir()
        assert store is None or isinstance(store, str), store
        free = free_space_for_models()
        assert free is None or (isinstance(free, int) and free >= 0), free

        # -- estimate_required_bytes(): stubbed /api/tags --------------------
        def fake_get_json_hit(url, timeout):
            assert url.endswith("/api/tags"), url
            return {"models": [{"name": "known-model:latest", "size": 4_700_000_000}]}

        _http_get_json = fake_get_json_hit
        assert estimate_required_bytes("http://x:11434", "known-model") == 4_700_000_000

        def fake_get_json_miss(url, timeout):
            return {"models": [{"name": "other-model:latest", "size": 123}]}

        _http_get_json = fake_get_json_miss
        assert estimate_required_bytes("http://x:11434", "unknown-model") is None

        def fake_get_json_boom(url, timeout):
            raise urllib.error.URLError("connection refused")

        _http_get_json = fake_get_json_boom
        assert estimate_required_bytes("http://x:11434", "anything") is None
        assert estimate_required_bytes("", "anything") is None
        assert estimate_required_bytes("http://x:11434", "") is None
        _http_get_json = orig_get_json

        # -- pull(): empty inputs never touch the network --------------------
        assert pull("", "model")["error"] == "empty base_url"
        assert pull("http://x:11434", "")["error"] == "empty model"
        r = pull("http://x:11434", "model", is_cancelled=lambda: True)
        assert r["cancelled"] is True and r["ok"] is False, r

        # -- pull(): disk preflight refuses BEFORE any connection is opened -
        open_calls = {"n": 0}

        def spy_open_stream(base_url, model, socket_timeout):
            open_calls["n"] += 1
            raise AssertionError("must not be called when disk space is insufficient")

        _open_pull_stream = spy_open_stream
        old_free = globals()["free_space_for_models"]
        globals()["free_space_for_models"] = lambda: 1_000_000  # 1MB free
        try:
            r_disk = pull("http://x:11434", "big-model", expected_bytes=20_000_000_000)
            assert r_disk["ok"] is False, r_disk
            assert open_calls["n"] == 0, "disk refusal must happen before opening a connection"
            assert "not enough free disk space" in r_disk["error"], r_disk
            assert "20." in r_disk["error"] or "19." in r_disk["error"], r_disk  # ~20 GB named
            assert r_disk["disk_check"]["checked"] is True, r_disk
            assert r_disk["disk_check"]["ok"] is False, r_disk
        finally:
            globals()["free_space_for_models"] = old_free
        _open_pull_stream = orig_open_stream

        # -- pull(): unknown expected/free space skips the refusal rather ---
        # -- than inventing a threshold, and DOES proceed to the network ----
        globals()["free_space_for_models"] = lambda: None
        called = {"n": 0}

        def fake_open_ok(base_url, model, socket_timeout):
            called["n"] += 1
            return _FakeStreamResponse('{"status":"success"}\n')

        _open_pull_stream = fake_open_ok
        try:
            r_unknown = pull("http://x:11434", "some-model", expected_bytes=None)
            assert r_unknown["disk_check"]["checked"] is False, r_unknown
            assert called["n"] == 1, "an unknown disk check must not block the pull from proceeding"
            assert r_unknown["ok"] is True, r_unknown
        finally:
            globals()["free_space_for_models"] = old_free
            _open_pull_stream = orig_open_stream

        # -- pull(): Ollama entirely unreachable ------------------------------
        def fake_open_refused(base_url, model, socket_timeout):
            raise ConnectionRefusedError("nobody home")

        _open_pull_stream = fake_open_refused
        r_refused = pull("http://x:11434", "model")
        assert r_refused["ok"] is False, r_refused
        assert "could not reach ollama" in r_refused["error"].lower(), r_refused
        _open_pull_stream = orig_open_stream

        # -- pull(): a non-2xx HTTP response (e.g. model not found) ----------
        def fake_open_404(base_url, model, socket_timeout):
            raise urllib.error.HTTPError(
                base_url, 404, "Not Found", hdrs=None,
                fp=__import__("io").BytesIO(b'{"error":"model not found"}'),
            )

        _open_pull_stream = fake_open_404
        r_404 = pull("http://x:11434", "nonexistent-model")
        assert r_404["ok"] is False, r_404
        assert "404" in r_404["error"], r_404
        _open_pull_stream = orig_open_stream

        # -- pull(): full success wiring end to end, including the digest ---
        # -- record persisted to disk (isolated HEARTH_DATA_DIR) ------------
        import tempfile
        tmp_data_dir = tempfile.mkdtemp(prefix="hearth-pull-selftest-")
        os.environ["HEARTH_DATA_DIR"] = tmp_data_dir
        try:
            def fake_open_success(base_url, model, socket_timeout):
                return _FakeStreamResponse("\n".join(_FIXTURE_LINES) + "\n")

            _open_pull_stream = fake_open_success
            progress_log = []
            r_full = pull(
                "http://x:11434", "demo-model:latest",
                on_progress=lambda p: progress_log.append(dict(p)),
                expected_bytes=None,
            )
            assert r_full["ok"] is True, r_full
            assert r_full["bytes_done"] == 1500, r_full
            assert [p["bytes_done"] for p in progress_log] == _FIXTURE_BYTES_DONE, progress_log

            record = load_pull_record("demo-model:latest")
            assert record is not None, "a successful pull must persist a digest record"
            assert record["bytes_total"] == 1500, record
            assert len(record["layers"]) == 2, record
            assert os.path.isfile(pull_record_path("demo-model:latest"))

            # A model that was never pulled has no record.
            assert load_pull_record("never-pulled-model") is None
            _open_pull_stream = orig_open_stream
        finally:
            shutil.rmtree(tmp_data_dir, ignore_errors=True)

        # -- real sockets: the actual urllib streaming + cancellation path --
        _run_live_http_self_test()

        print("hearth-pull self-test OK")
        return 0
    finally:
        _open_pull_stream = orig_open_stream
        _http_get_json = orig_get_json
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hearth-pull",
        description="Pull an Ollama model with real, honest progress. Never predicts, only reports.",
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--base-url", default=DEFAULT_OLLAMA)
    sub = parser.add_subparsers(dest="command")

    p_pull = sub.add_parser("pull", help="pull one model, printing progress as it arrives")
    p_pull.add_argument("model")
    p_pull.add_argument("--expected-bytes", type=int, default=None)

    p_store = sub.add_parser("store", help="print the detected model store directory and free space")

    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if args.command == "pull":
        def on_progress(p):
            # The CLI's own job is to print; the library itself never does.
            pct = "{:.1f}%".format(p["fraction"] * 100) if p["fraction"] is not None else "?"
            print("[{}] {} ({} bytes){}".format(
                pct, p["phase"], p["bytes_done"],
                " digest={}".format(p["current_digest"]) if p["current_digest"] else "",
            ))

        result = pull(args.base_url, args.model, on_progress=on_progress,
                       expected_bytes=args.expected_bytes)
        print(json.dumps({k: v for k, v in result.items() if k != "layers"}, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "store":
        print(json.dumps({
            "model_store_dir": model_store_dir(),
            "free_space_for_models": free_space_for_models(),
        }, indent=2))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv or len(sys.argv) == 1:
        sys.exit(_self_test())
    sys.exit(main())
