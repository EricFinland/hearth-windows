#!/usr/bin/env python3
r"""hearth throughput measurement: the truth about tokens per second.

The model shop (agent/hearth_shop.py, docs/model-shop.md) deliberately
refuses to PREDICT throughput from memory bandwidth and parameter count.
That formula breaks badly on mixture-of-experts models: a 30B-A3B model
downloads about 30B worth of weights but only activates roughly 3B of them
per token, so a naive bandwidth/parameter-count estimate underestimates
real speed by close to an order of magnitude and would hide some of the
best local coding models from exactly the users who need them most. See
docs/model-shop.md, "Why there's no predicted tokens per second", for the
full argument. That decision is not revisited here.

The honest alternative is to MEASURE. This module runs one short,
deterministic generation on the user's own hardware, reads back what
actually happened, and shows that number. If a measurement cannot be taken,
the module says so plainly instead of guessing.

## Two engines, and why the numbers are not the same kind of number

Hearth runs models on its own bundled llama-server by default, and on
Ollama when the user already has one (see agent/hearth_backend.py). Both
are measured, but not identically, and the difference is recorded in every
result rather than smoothed over:

  Ollama reports its own load_duration / prompt_eval_duration /
  eval_duration, measured inside the process doing the arithmetic. So
  tokens_per_second there is eval_count / eval_duration, with
  tokens_per_second_source "server_eval_duration", and the HTTP round trip
  is excluded. All of the reasoning below about which of those fields to
  trust applies to that path, and only to it.

  llama-server also keeps a timings block, but hearth_llama's stream
  consumer folds only the usage counts out of a response and discards it,
  so there is no server-reported rate to read. The bundled path therefore
  measures wall clock and says tokens_per_second_source "wall_clock". That
  is a slightly pessimistic number, since it includes request overhead,
  and comparing it against a server_eval_duration figure is not apples to
  apples. tokens_per_second_source is in every result so a caller can tell
  which it has; the "backend" field says which engine produced it.

Because those two numbers are not interchangeable, the measurement cache
is keyed by the backend-qualified model reference (ModelRef.as_text(), so
"ollama:qwen2.5-coder:latest" and "gguf:C:\models\qwen.gguf" can never
collide) alongside the hardware signature.

The target user runs local models around the clock, often on modest
hardware shared with everything else the machine does. For them, real
tokens per second and what a generation actually cost in GPU time are the
numbers that matter, the local equivalent of a hosted tool showing dollars.

## Which timing numbers this module trusts, and why

Ollama's /api/generate response (stream=False) carries several duration
fields, all in nanoseconds: eval_duration (time spent generating the
output tokens), eval_count (how many output tokens that covers),
prompt_eval_duration and prompt_eval_count (the same for processing the
prompt), load_duration (time spent loading the model into memory before
any inference happened), and total_duration (the sum of the above plus a
small amount of bookkeeping overhead).

Those numbers are measured by the inference engine itself, on the same
process that is doing the matrix multiplication. This module's own
wall-clock timing, by contrast, wraps the entire HTTP round trip: DNS and
socket setup, JSON serialization on the way out, JSON parsing on the way
back, plus whatever the OS scheduler was doing to this process in the
meantime. For a short generation, that overhead is not noise, it can be a
meaningfully large fraction of the total elapsed time and would understate
real generation speed. So tokens_per_second is computed from eval_count /
(eval_duration / 1e9) whenever the server reports a positive
eval_duration, and only falls back to eval_count / wall_seconds when the
server does not report timing at all (an older Ollama build, or a
malformed response that still happens to carry a token count). The result
records tokens_per_second_source so a caller can tell which one it got.

Ollama's non-streaming response has no explicit "first token" timestamp,
so time_to_first_token_seconds is an approximation: load_duration plus
prompt_eval_duration, i.e. everything that has to happen before the engine
starts producing output tokens. That is the same quantity Ollama itself
adds into total_duration alongside eval_duration, so it is consistent with
the server's own accounting, just not literally observed at the socket
level the way a streaming client could observe it.

## Residency over raw speed

GET /api/ps reports, per loaded model, size (total weight bytes) and
size_vram (how many of those bytes are actually sitting in VRAM right
now). When size_vram is meaningfully below size, part of the model is
running on CPU and throughput falls off a cliff, often by an order of
magnitude. "Your model is half on the CPU" is a far more actionable thing
to tell a user than a bare tokens-per-second number that they cannot
otherwise explain, so measure() always attaches a residency() reading
taken right after the generation completes (the moment the model is
certain to be loaded, if it loaded at all).

## Caching

Measuring costs real wall-clock time and real GPU work, so cached_measure()
does it once per (model, hardware) pair and stores the result under
hearth_paths.data_dir()/bench/cache.json as plain indented JSON, easy to
open and read by hand. The cache key folds in hardware_signature(), a
short hash of platform + GPU names/VRAM + CPU count + system RAM, so a
stale reading from a different machine (or the same machine after a GPU
swap) is simply never matched rather than needing an explicit eviction
pass.

## Energy, sampled over the window it is attributed to

If `nvidia-smi --query-gpu=power.draw` is readable, measure() runs a
background thread (_PowerSampler) that samples GPU power repeatedly for
the whole duration of the /api/generate call, instead of reading power
once immediately before the call and once immediately after it returns.
That before/after approach bracketed the entire round trip (model load,
prompt evaluation, and generation) but was then multiplied by
eval_seconds, the generation-only sub-interval: an average power measured
over one window multiplied by the duration of a different, shorter
window. On identical generation work, that produced energy figures
roughly 14 percent apart purely because the surrounding load and prompt
phases took different amounts of wall-clock time between two runs. See
_attribute_energy() for the fix: only the power samples whose timestamps
fall inside the generation sub-window (estimated from the server's own
load_duration and prompt_eval_duration, i.e. when generation is expected
to start) are averaged and multiplied by eval_seconds, so the power
average and the duration it is multiplied by always refer to the same
interval. If the call was faster than the sampling interval, or the
server did not report enough timing detail to locate the window,
energy_wh_per_1k_tokens is omitted and energy_note explains why: a
missing number is preferable to one that moves 14 percent on identical
work.

nvidia-smi reports power for every GPU it can see, with no attribution to
which one is actually running the model under test. On a single-GPU
machine that is unambiguous. On a multi-GPU machine - which is exactly
the shared, do-everything machine this module's target user is most
likely to have - the figure sums power across every GPU nvidia-smi
reports, so a browser, a game, or a second GPU drawing power during the
measurement is silently folded in. This module does not attempt to
identify which physical GPU is hosting the Ollama process: nvidia-smi's
per-process GPU attribution is unreliable across driver and platform
combinations, and is frequently unavailable outright on Windows consumer
cards running in WDDM mode, which is what this module's own calibration
hardware uses. Instead, when more than one GPU is visible,
energy_gpu_count records how many, and energy_note states plainly that
the figure is machine-wide, not per-model. Treat energy_wh_per_1k_tokens
on a multi-GPU box as an upper bound on what this model actually cost,
not an exact figure.

When nvidia-smi or a GPU is not available at all, the energy fields are
left out of the result entirely. They are never fabricated or replaced
with a guess.

## What this module will never do

Predict, estimate, or otherwise infer tokens per second from parameters,
memory bandwidth, or any static model metadata. Every number in this
module was either read from the Ollama server or measured with a stopwatch
around a real HTTP call. If neither of those is possible, the result says
ok: False and why, never a fabricated figure.

Standard library only. Every network or subprocess call in this module is
optional in the sense that its failure degrades to a clear "could not
measure" result, never a raised exception reaching the caller.

Real-world calibration this module was checked against (see the project's
docs and CLAUDE.md): a Linux host with an RTX 2060 (6GB VRAM) running
qwen2.5-coder (4.75GB) fully resident at a 16384-token context, and a
Windows machine with an RTX 5080 (~16GB VRAM). compare() is expected to
rank sensibly differently on those two boxes; neither figure is hardcoded
here, both come from measure() actually talking to Ollama.
"""

import argparse
import hashlib
import http.server
import io
import json
import os
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_backend  # noqa: E402
import hearth_paths  # noqa: E402
import hearth_hw  # noqa: E402

DEFAULT_OLLAMA = "http://127.0.0.1:11434"

# A short, fixed prompt so repeated measurements are comparable to each
# other. temperature=0 and a fixed seed (set in measure()) are about
# comparability of the benchmark run, not a claim of bit-identical output
# across every backend Ollama might use.
DEFAULT_PROMPT = "Write a three-line haiku about a slow compiler."
DEFAULT_NUM_PREDICT = 64

GENERATE_TIMEOUT = 180  # seconds; generous enough to cover a cold model load
PS_TIMEOUT = 8
POWER_TIMEOUT = 5

# size_vram / size at or above this counts as "fully resident". Not 1.0:
# Ollama's own byte accounting has a little rounding/bookkeeping slack, and
# treating anything below perfect as "partially offloaded" would cry wolf
# on a model that is, for all practical purposes, entirely in VRAM.
RESIDENCY_FULL_THRESHOLD = 0.98

CACHE_SUBDIR = "bench"
CACHE_FILENAME = "cache.json"


# --------------------------------------------------------------------------
# Chokepoints: every external call this module makes goes through one of
# these three functions. Self-tests replace them with stand-ins, the same
# pattern hearth_hw.py uses for _run, so parsing/ranking/caching logic can
# be exercised without a live Ollama, a GPU, or a network connection.
# --------------------------------------------------------------------------

def _http_post(url, payload, timeout):
    """POST JSON to url, return the parsed JSON body.

    Raises whatever urllib/json raises on failure; callers are responsible
    for turning that into a result dict rather than letting it propagate.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get(url, timeout):
    """GET url, return the parsed JSON body. Same failure contract as _http_post."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gpu_power_watts():
    """Instantaneous total GPU power draw in watts, summed across all GPUs
    nvidia-smi reports, or None if nvidia-smi is not on PATH, times out, or
    returns anything this function cannot confidently parse.

    None must never be treated as "zero watts"; it means "unknown", and
    callers must omit dependent fields rather than compute from it.

    This deliberately does not attribute power to a specific GPU: on a
    multi-GPU machine it folds in whatever every visible GPU is drawing,
    not just the one running the model under test. See _detect_gpu_count()
    and the module docstring's "Energy" section for how callers are
    expected to disclose that rather than present the sum as exclusive to
    the model being measured.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=POWER_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        return sum(float(ln) for ln in lines)
    except ValueError:
        return None


def _detect_gpu_count():
    """How many GPUs nvidia-smi currently reports, or None if nvidia-smi is
    not on PATH, times out, or its output cannot be parsed at all.

    Used only to attach a multi-GPU caveat to an energy figure (see
    measure() and the module docstring's "Energy" section); never used to
    compute the figure itself.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=index", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=POWER_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    return len(lines) if lines else None


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Hardware signature: cache invalidation key
# --------------------------------------------------------------------------

def hardware_signature():
    """A short, deterministic hash of the machine's hardware shape, used to
    key the measurement cache. Two probes of the same hardware always
    produce the same signature (GPU order does not matter: the list is
    sorted first); a real hardware change (different GPU, different VRAM
    reading, different CPU count) always produces a different one.

    Deliberately hardware-only: it has no software-version component. An
    Ollama upgrade, a GPU driver update, or swapping a model for a
    requantized build published under the same name can all change real
    throughput or energy behaviour without changing anything this function
    reads, so a cached result can go stale on completely unchanged
    hardware and still look current. cached_measure(..., force=True) is
    the escape hatch: it ignores whatever is cached and measures again.
    """
    probe = hearth_hw.probe()
    gpu_sig = sorted(
        "{}:{}".format(g.get("name", ""), g.get("vram_bytes", 0))
        for g in probe.get("gpus", [])
    )
    parts = {
        "platform": probe.get("platform"),
        "gpus": gpu_sig,
        "cpu_count": probe.get("cpu_count"),
        "system_ram_bytes": probe.get("system_ram_bytes"),
    }
    blob = json.dumps(parts, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Residency
# --------------------------------------------------------------------------

def _find_ps_entry(models, model):
    """Match a requested model name against /api/ps entries.

    Ollama model names carry a tag (e.g. "qwen2.5-coder:latest"), and
    callers frequently pass the name without one. Try an exact match
    first, then fall back to comparing the part before the colon on both
    sides, so "qwen2.5-coder" matches an entry reported as
    "qwen2.5-coder:latest" and vice versa.
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


def residency(base_url, model, timeout=PS_TIMEOUT):
    """Is `model` currently loaded on the Ollama server, and if so, how much
    of it is actually sitting in VRAM right now?

    Returns a dict with at least: loaded (bool), error (str or None),
    size_bytes, size_vram_bytes, vram_fraction, fully_resident (bool or
    None when it cannot be computed), and note (a human-readable warning
    when part of the model is running on CPU, else None). Never raises:
    an unreachable server, a malformed response, or a model that simply
    is not loaded right now all come back as loaded: False with error set,
    not an exception.
    """
    result = {
        "loaded": False,
        "error": None,
        "size_bytes": None,
        "size_vram_bytes": None,
        "vram_fraction": None,
        "fully_resident": None,
        "note": None,
    }
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        result["error"] = "empty base_url"
        return result

    try:
        data = _http_get(base_url + "/api/ps", timeout)
    except urllib.error.HTTPError as exc:
        result["error"] = "HTTP {}".format(exc.code)
        return result
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        result["error"] = "{}: {}".format(type(exc).__name__, exc)
        return result
    except (ValueError, json.JSONDecodeError) as exc:
        result["error"] = "invalid JSON from Ollama: {}".format(exc)
        return result

    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        result["error"] = "unexpected /api/ps response shape"
        return result

    entry = _find_ps_entry(models, model)
    if entry is None:
        result["error"] = "model not currently loaded"
        return result

    result["loaded"] = True
    size = entry.get("size")
    size_vram = entry.get("size_vram")
    if isinstance(size, (int, float)):
        result["size_bytes"] = size
    if isinstance(size_vram, (int, float)):
        result["size_vram_bytes"] = size_vram

    if isinstance(size, (int, float)) and size > 0 and isinstance(size_vram, (int, float)):
        fraction = size_vram / size
        result["vram_fraction"] = fraction
        fully = fraction >= RESIDENCY_FULL_THRESHOLD
        result["fully_resident"] = fully
        if not fully:
            result["note"] = (
                "only {:.0f}% of this model is resident in VRAM; the rest is "
                "running on CPU and throughput will fall off sharply".format(fraction * 100)
            )
    return result


# --------------------------------------------------------------------------
# Energy: background power sampling and window attribution
# --------------------------------------------------------------------------

POWER_SAMPLE_INTERVAL = 0.05  # seconds between GPU power samples while a
# request is in flight. Small enough to catch several samples during even
# a sub-second generation phase, large enough not to spawn a new
# nvidia-smi subprocess needlessly fast.

_SAMPLER_MAX_SECONDS = GENERATE_TIMEOUT + 30  # hard safety cap: the
# sampler must never outlive the request it belongs to, even if a bug
# elsewhere means stop() never gets called.


class _PowerSampler:
    """Samples GPU power on a background thread for the duration of one
    /api/generate call, producing a (timestamp, watts) series instead of
    the two endpoint reads (immediately before, immediately after) the
    previous implementation used. See the module docstring's "Energy"
    section and _attribute_energy() below for why that replacement
    matters: the old before/after reading bracketed the whole round trip
    but was multiplied by the generation-only sub-duration, which is the
    Important review finding this class exists to fix.

    Uses time.perf_counter() for its own timestamps, deliberately not
    time.monotonic(): the self-test monkeypatches the real time.monotonic
    with a small, finite, fake sequence while exercising the wall-clock
    fallback path elsewhere in this module, and a background thread
    reading that same patched, finite fake clock concurrently would race
    the main thread for it and could raise StopIteration on a daemon
    thread. perf_counter is a second, independent monotonic clock that
    nothing else in this module touches, so the two never collide.

    start() launches the thread. stop() signals it to exit and joins with
    a bounded timeout, so calling code always gets control back even if
    something goes wrong; it is safe to call stop() more than once. A hard
    max_seconds cap means the thread exits on its own even if stop() is
    never called at all.
    """

    def __init__(self, interval=POWER_SAMPLE_INTERVAL, power_fn=None,
                 clock_fn=None, max_seconds=_SAMPLER_MAX_SECONDS):
        self._interval = interval
        self._power_fn = power_fn if power_fn is not None else (lambda: _gpu_power_watts())
        self._clock_fn = clock_fn if clock_fn is not None else time.perf_counter
        self._max_seconds = max_seconds
        self._samples = []
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _sample_once(self):
        ts = self._clock_fn()
        watts = self._power_fn()
        if watts is not None:
            self._samples.append((ts, watts))

    def _run(self):
        deadline = self._clock_fn() + self._max_seconds
        while not self._stop_event.is_set():
            self._sample_once()
            if self._clock_fn() >= deadline:
                break
            self._stop_event.wait(self._interval)

    def stop(self, join_timeout=5.0):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None

    def samples(self):
        return list(self._samples)


def _attribute_energy(samples, t0_perf, load_seconds, prompt_eval_seconds,
                       eval_seconds, eval_count):
    """Turn a (timestamp, watts) series collected over a whole
    /api/generate call into a Wh-per-1k-tokens figure for the generation
    phase specifically, or explain why that cannot be done honestly.

    The samples bracket the entire request: model load, then prompt
    evaluation, then token generation, in that order (the same order
    Ollama's own load_duration / prompt_eval_duration / eval_duration add
    up in). Only the samples whose timestamp falls inside the generation
    sub-window - [t0_perf + load_seconds + prompt_eval_seconds, ... +
    eval_seconds] - are averaged, so the average power this returns is
    always paired with the same duration it gets multiplied by.

    This is the fix for the reproduced review finding: an average power
    sampled over the WHOLE call (load + prompt + generate) was previously
    multiplied by eval_seconds alone, so identical generation work
    produced energy figures that moved with however long the surrounding
    load and prompt phases happened to take, purely an artifact of the
    measurement window, not of anything the GPU actually did differently.

    Returns (energy_wh_per_1k_tokens, note, sample_count). energy is None
    when the window cannot be computed at all (missing load/prompt/eval
    timing) or no sample happened to land inside it (the call was faster
    than the sampling interval, or the background thread was not
    scheduled in time); in both cases note explains why, and the caller
    should omit the field rather than fabricate a number from samples
    outside the window.
    """
    if load_seconds is None or prompt_eval_seconds is None or not eval_seconds:
        return None, (
            "server did not report enough timing detail (load/prompt/eval "
            "duration) to isolate the generation window; omitting energy "
            "rather than mixing load or prompt time into the figure"
        ), 0

    t_eval_start = t0_perf + load_seconds + prompt_eval_seconds
    t_eval_end = t_eval_start + eval_seconds
    eval_watts = [w for (ts, w) in samples if t_eval_start <= ts <= t_eval_end]
    if not eval_watts:
        return None, (
            "GPU power was sampled during this call, but no sample landed "
            "inside the estimated {:.3f}s generation window; omitting "
            "rather than guessing".format(eval_seconds)
        ), 0

    avg_watts = sum(eval_watts) / len(eval_watts)
    energy_wh = avg_watts * eval_seconds / 3600.0
    return energy_wh * 1000.0 / eval_count, None, len(eval_watts)


# --------------------------------------------------------------------------
# Core measurement
# --------------------------------------------------------------------------

def measure(model, base_url=None, prompt=None, num_predict=DEFAULT_NUM_PREDICT,
            timeout=GENERATE_TIMEOUT, backend=None):
    """Measure throughput on whichever engine is active.

    `model` may be a ModelRef or a string; a bare string is read in the
    active backend's own namespace (a GGUF path for the bundled engine, a
    registry tag for Ollama), and an explicitly prefixed string of the
    wrong kind is refused rather than coerced. See hearth_backend.ModelRef.

    `base_url` names the Ollama server and is used only when the Ollama
    backend is in use. The bundled engine is a process Hearth starts
    itself on an ephemeral loopback port it chooses, so there is no URL for
    a caller to supply and this argument is ignored on that path.

    `backend` overrides the active-backend lookup, which is what the
    self-test uses to exercise one engine's path deterministically on a
    machine that has neither installed.

    Always returns a dict, never raises. Every result carries "backend" and
    "tokens_per_second_source" so a caller can tell which engine produced
    the number and how it was derived; see the module docstring for why
    those two are not comparable across engines.
    """
    backend = backend if backend is not None else hearth_backend.get_backend(
        ollama_url=base_url)
    if backend.name == hearth_backend.BACKEND_OLLAMA:
        # Route through the backend's own base_url when the caller did not
        # name one, so an Ollama on a non-default port still gets measured.
        return measure_ollama(base_url or backend.base_url, model, prompt=prompt,
                              num_predict=num_predict, timeout=timeout)
    return backend.measure(model, prompt=prompt, num_predict=num_predict,
                           timeout=timeout)


def measure_ollama(base_url, model, prompt=None, num_predict=DEFAULT_NUM_PREDICT,
                   timeout=GENERATE_TIMEOUT):
    """Run one short, deterministic generation against Ollama and report
    what actually happened. See the module docstring for which timing
    fields are trusted and why, how residency is attached, and the
    conditions under which energy_wh_per_1k_tokens appears.

    The Ollama-specific implementation, called by measure() when that
    backend is active and by hearth_backend.OllamaBackend.measure. `model`
    is an Ollama registry tag; a ModelRef of that kind is accepted and
    unwrapped, and a GGUF ref is refused.

    Always returns a dict, never raises: connection refused, model not
    pulled, a timeout, or a malformed response all come back as
    ok: False with error explaining what went wrong.
    """
    if isinstance(model, hearth_backend.ModelRef):
        if model.kind != hearth_backend.KIND_OLLAMA:
            return {"ok": False, "backend": hearth_backend.BACKEND_OLLAMA,
                    "model": model.value, "tokens_per_second": None,
                    "tokens_per_second_source": None, "wall_seconds": None,
                    "tokens_generated": 0,
                    "error": "measure_ollama needs an ollama model reference, "
                             "not a {} one".format(model.kind)}
        model = model.value
    prompt = DEFAULT_PROMPT if prompt is None else prompt
    base_url = (base_url or "").rstrip("/")

    result = {
        "ok": False,
        "error": None,
        "backend": hearth_backend.BACKEND_OLLAMA,
        "model": model,
        "base_url": base_url,
        "timestamp": _now_iso(),
        "num_predict": num_predict,
        "tokens_generated": 0,
        "prompt_tokens": 0,
        "wall_seconds": None,
        "eval_seconds": None,
        "load_seconds": None,
        "total_seconds": None,
        "tokens_per_second": None,
        "tokens_per_second_source": None,
        "time_to_first_token_seconds": None,
        "residency": None,
    }
    # energy_wh_per_1k_tokens, energy_sample_count, energy_gpu_count, and
    # energy_note are added conditionally below, not pre-populated with
    # None: their absence is itself meaningful (see the module docstring's
    # "Energy" section), and pre-populating with None would make "we
    # could not measure this" indistinguishable from "we did not try".

    if not base_url:
        result["error"] = "empty base_url"
        return result
    if not model:
        result["error"] = "empty model"
        return result

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "seed": 42, "num_predict": num_predict},
    }

    sampler = _PowerSampler()
    sampler.start()
    t0 = time.monotonic()
    t0_perf = time.perf_counter()
    try:
        try:
            data = _http_post(base_url + "/api/generate", payload, timeout)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - reading the error body is best-effort
                body = ""
            result["error"] = "HTTP {}: {}".format(exc.code, body[:300] or exc.reason)
            return result
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            result["error"] = "{}: {}".format(type(exc).__name__, exc)
            return result
        except (ValueError, json.JSONDecodeError) as exc:
            result["error"] = "invalid JSON from Ollama: {}".format(exc)
            return result
        wall_seconds = time.monotonic() - t0
        sampler.stop()
        power_samples = sampler.samples()

        if not isinstance(data, dict):
            result["error"] = "unexpected response shape from Ollama"
            return result

        eval_count = int(data.get("eval_count") or 0)
        prompt_eval_count = int(data.get("prompt_eval_count") or 0)
        eval_duration_ns = data.get("eval_duration")
        prompt_eval_duration_ns = data.get("prompt_eval_duration")
        load_duration_ns = data.get("load_duration")
        total_duration_ns = data.get("total_duration")

        result["tokens_generated"] = eval_count
        result["prompt_tokens"] = prompt_eval_count
        result["wall_seconds"] = wall_seconds
        result["load_seconds"] = load_duration_ns / 1e9 if isinstance(load_duration_ns, (int, float)) else None
        result["total_seconds"] = total_duration_ns / 1e9 if isinstance(total_duration_ns, (int, float)) else None

        if eval_count <= 0:
            result["error"] = data.get("error") or "no tokens generated"
            return result

        eval_seconds = None
        if isinstance(eval_duration_ns, (int, float)) and eval_duration_ns > 0:
            eval_seconds = eval_duration_ns / 1e9
        result["eval_seconds"] = eval_seconds

        if eval_seconds:
            result["tokens_per_second"] = eval_count / eval_seconds
            result["tokens_per_second_source"] = "server_eval_duration"
        elif wall_seconds > 0:
            # Older Ollama build or a response missing timing fields: fall back
            # to our own stopwatch. Strictly worse (it also counts JSON and
            # network round-trip time), but a degraded number beats none.
            result["tokens_per_second"] = eval_count / wall_seconds
            result["tokens_per_second_source"] = "wall_clock"

        if isinstance(load_duration_ns, (int, float)) and isinstance(prompt_eval_duration_ns, (int, float)):
            result["time_to_first_token_seconds"] = (load_duration_ns + prompt_eval_duration_ns) / 1e9

        # Residency at the moment right after generation: the model is certain
        # to be loaded now if it is ever going to be.
        result["residency"] = residency(base_url, model, timeout=PS_TIMEOUT)

        # Energy: attribute only the power samples that fall inside the
        # generation sub-window, never the whole call. See _attribute_energy()
        # and the module docstring's "Energy" section for why.
        if power_samples:
            prompt_eval_seconds = (
                prompt_eval_duration_ns / 1e9
                if isinstance(prompt_eval_duration_ns, (int, float))
                else None
            )
            energy_per_1k, energy_note, sample_count = _attribute_energy(
                power_samples, t0_perf, result["load_seconds"], prompt_eval_seconds,
                eval_seconds, eval_count,
            )
            if energy_per_1k is not None:
                result["energy_wh_per_1k_tokens"] = energy_per_1k
                result["energy_sample_count"] = sample_count
                # Multi-GPU disclosure: nvidia-smi sums power across every
                # GPU it can see, with no attribution to which one is
                # actually running this model. Say so rather than stay silent.
                gpu_count = _detect_gpu_count()
                if gpu_count is not None:
                    result["energy_gpu_count"] = gpu_count
                    if gpu_count > 1:
                        result["energy_note"] = (
                            "{} GPUs are visible to nvidia-smi; this figure sums "
                            "power draw across all of them, not just the one "
                            "running this model, so any other GPU load on this "
                            "machine inflates it. Treat it as a machine-wide "
                            "figure, not a per-model one.".format(gpu_count)
                        )
            elif energy_note:
                result["energy_note"] = energy_note
        # else: nvidia-smi/GPU unavailable entirely -> omit silently, as documented.

        result["ok"] = True
        return result
    finally:
        sampler.stop()


# --------------------------------------------------------------------------
# Cache: measure once per (model, hardware)
# --------------------------------------------------------------------------

def _cache_path():
    return os.path.join(hearth_paths.data_dir(), CACHE_SUBDIR, CACHE_FILENAME)


def _load_cache():
    path = _cache_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(data):
    """Write the cache as indented, human-readable JSON. A write failure
    (read-only filesystem, disk full) is swallowed: a caller that just
    measured something real should not crash because the cache could not
    be persisted.
    """
    path = _cache_path()
    parent = os.path.dirname(path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def _cache_key(model, hw_sig, backend_name=None):
    """The cache key for one measurement.

    The model part is the backend-qualified ModelRef text, not the bare
    name, so a GGUF file and an Ollama tag can never share an entry. That
    matters because the two engines produce a tokens_per_second derived a
    different way (see the module docstring), and serving one from the
    other's cache entry would silently mix them.
    """
    if isinstance(model, hearth_backend.ModelRef):
        name = model.as_text()
    elif backend_name is not None:
        name = hearth_backend.ModelRef.parse(
            model,
            default_kind=(hearth_backend.KIND_OLLAMA
                          if backend_name == hearth_backend.BACKEND_OLLAMA
                          else hearth_backend.KIND_GGUF),
        ).as_text()
    else:
        name = str(model)
    return "{}::{}".format(hw_sig, name)


def cached_measure(model, base_url=None, prompt=None, num_predict=DEFAULT_NUM_PREDICT,
                    force=False, timeout=GENERATE_TIMEOUT, backend=None):
    """measure(), but only once per (model, backend, hardware_signature()).

    A hit returns the stored result with from_cache: True added. A miss
    (including "hardware changed since the last measurement") runs
    measure() and, if it succeeded, stores the result before returning it.
    A failed measurement (ok: False) is never cached, so a transient
    problem (the engine briefly unavailable) does not stick around and
    shadow a working one on the next call.
    """
    backend = backend if backend is not None else hearth_backend.get_backend(
        ollama_url=base_url)
    hw_sig = hardware_signature()
    cache = _load_cache()
    key = _cache_key(model, hw_sig, backend.name)
    entry = cache.get(key)
    if entry and not force and entry.get("ok"):
        hit = dict(entry)
        hit["from_cache"] = True
        return hit

    result = measure(model, base_url=base_url, prompt=prompt, num_predict=num_predict,
                     timeout=timeout, backend=backend)
    result["hardware_signature"] = hw_sig
    result["from_cache"] = False
    if result.get("ok"):
        cache[key] = result
        _save_cache(cache)
    return result


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def _rank_key(result):
    """Sort key for compare(): ok results before failed ones, always, then
    fastest first within each group.

    ok is checked FIRST, as its own tuple element, deliberately: with
    today's measure() a failed result always carries tokens_per_second of
    None (coerced to 0.0 here), which happens to already sort last under a
    plain speed comparison. That is an accident of the current
    implementation, not a guarantee, so this key does not rely on it: a
    result with ok is False must rank below every ok result even if it
    somehow carried a large, stale, or corrupted tokens_per_second value
    (a hand-edited cache file, for instance).
    """
    ok = bool(result.get("ok"))
    tps = result.get("tokens_per_second") or 0.0
    return (0 if ok else 1, -tps)


def compare(models, base_url=None, prompt=None, num_predict=DEFAULT_NUM_PREDICT,
            force=False, timeout=GENERATE_TIMEOUT, backend=None):
    """Measure several models (via cached_measure) and return them ranked
    fastest first. A model that could not be measured sorts to the bottom,
    regardless of any partial numbers it produced, and keeps ok: False so
    a caller can tell "slow" from "unmeasurable" apart. Each entry gets a
    1-based rank field.

    Every model is measured on the SAME backend, so the ranking compares
    like with like. Ranking a wall-clock figure from the bundled engine
    against a server-reported one from Ollama would not be a fair
    comparison (see the module docstring), and this function does not
    offer a way to ask for one.
    """
    backend = backend if backend is not None else hearth_backend.get_backend(
        ollama_url=base_url)
    results = [
        cached_measure(m, base_url=base_url, prompt=prompt, num_predict=num_predict,
                        force=force, timeout=timeout, backend=backend)
        for m in models
    ]
    ranked = sorted(results, key=_rank_key)
    for i, r in enumerate(ranked):
        r["rank"] = i + 1
    return ranked


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hearth-bench",
        description="Measure real throughput on this machine. Never predicts it.",
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--base-url", default=DEFAULT_OLLAMA,
                        help="Ollama base URL; ignored when the bundled engine is in use")
    parser.add_argument("--backend", choices=hearth_backend.BACKENDS, default=None,
                        help="force an engine instead of using the active one")
    sub = parser.add_subparsers(dest="command")

    p_measure = sub.add_parser("measure", help="measure one model")
    p_measure.add_argument("model")
    p_measure.add_argument("--num-predict", type=int, default=DEFAULT_NUM_PREDICT)
    p_measure.add_argument("--no-cache", action="store_true")
    p_measure.add_argument("--force", action="store_true", help="ignore a cached result")

    p_res = sub.add_parser("residency", help="check VRAM residency for a loaded model")
    p_res.add_argument("model")

    p_cmp = sub.add_parser("compare", help="measure several models, ranked fastest first")
    p_cmp.add_argument("models", nargs="+")
    p_cmp.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    backend = (hearth_backend.build(args.backend, args.base_url) if args.backend
               else hearth_backend.get_backend(ollama_url=args.base_url))

    if args.command == "measure":
        if args.no_cache:
            result = measure(args.model, base_url=args.base_url,
                             num_predict=args.num_predict, backend=backend)
        else:
            result = cached_measure(args.model, base_url=args.base_url,
                                    num_predict=args.num_predict, force=args.force,
                                    backend=backend)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "residency":
        if backend.name == hearth_backend.BACKEND_OLLAMA:
            print(json.dumps(residency(args.base_url, args.model), indent=2))
        else:
            print(json.dumps(backend.residency(backend.check_ref(args.model)), indent=2))
        return 0

    if args.command == "compare":
        ranked = compare(args.models, base_url=args.base_url, force=args.force,
                         backend=backend)
        print(json.dumps(ranked, indent=2))
        return 0 if all(r.get("ok") for r in ranked) else 1

    parser.print_help()
    return 0


# --------------------------------------------------------------------------
# Self-test fixtures for the live-HTTP section below
# --------------------------------------------------------------------------

class _FakeOllamaHandler(http.server.BaseHTTPRequestHandler):
    """Just enough of the Ollama HTTP API to exercise the real urllib code
    path (_http_post / _http_get, real sockets, real JSON, real HTTP
    status handling) without needing an actual Ollama install or a GPU.
    """

    def log_message(self, *a, **k):  # keep self-test output quiet
        pass

    def _write_json(self, status, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path != "/api/generate":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}
        model = body.get("model")
        if model == "missing-model":
            self._write_json(404, {"error": 'model "missing-model" not found, try pulling it first'})
            return
        self._write_json(200, {
            "model": model,
            "response": "fixture response",
            "done": True,
            "eval_count": 40,
            "eval_duration": 2_000_000_000,        # 2.0s -> 20 tok/s
            "prompt_eval_count": 12,
            "prompt_eval_duration": 300_000_000,   # 0.3s
            "load_duration": 100_000_000,          # 0.1s
            "total_duration": 2_400_000_000,
        })

    def do_GET(self):
        if self.path != "/api/ps":
            self.send_response(404)
            self.end_headers()
            return
        self._write_json(200, {
            "models": [{"name": "fixture-model:latest", "size": 1000, "size_vram": 1000}],
        })


def _run_live_http_self_test():
    """Exercise the real urllib.request-based _http_post/_http_get against a
    throwaway local HTTP server, instead of the stubbed versions used
    elsewhere in the self-test. This is as close as a self-test can get to
    proving the live path works without an actual Ollama install: real
    sockets, real HTTP status/error handling, real JSON encode/decode.

    Skips (does not fail the suite) if this sandbox will not allow binding
    a loopback socket, since the self-test contract is that it must pass
    with no network available at all.
    """
    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
    except OSError as exc:
        print("hearth-bench: skipping live-http self-test, no loopback socket ({})".format(exc))
        return
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = "http://127.0.0.1:{}".format(port)
        # An explicit Ollama backend: the point of this test is the real
        # urllib path against a real socket, not backend selection, and the
        # machine running it may have no engine installed at all.
        live_backend = hearth_backend.OllamaBackend(base_url)

        r = measure("fixture-model:latest", base_url=base_url, num_predict=40,
                    backend=live_backend)
        assert r["ok"] is True, r
        assert r["backend"] == hearth_backend.BACKEND_OLLAMA, r
        assert r["tokens_generated"] == 40, r
        assert r["tokens_per_second"] == 20.0, r
        assert r["tokens_per_second_source"] == "server_eval_duration", r
        assert abs(r["time_to_first_token_seconds"] - 0.4) < 1e-9, r
        assert r["residency"]["loaded"] is True, r
        assert r["residency"]["fully_resident"] is True, r

        missing = measure("missing-model", base_url=base_url, num_predict=40,
                          backend=live_backend)
        assert missing["ok"] is False, missing
        assert "missing-model" in (missing["error"] or ""), missing

        res = residency(base_url, "fixture-model")
        assert res["loaded"] is True, res
        assert res["vram_fraction"] == 1.0, res

        # The same request through OllamaBackend.measure, which is what
        # hearth_backend hands callers: it must reach this same real socket
        # and produce the same number, so the delegation is proved rather
        # than assumed.
        via = live_backend.measure("fixture-model:latest", num_predict=40)
        assert via["ok"] is True and via["tokens_per_second"] == 20.0, via
        assert via["backend"] == hearth_backend.BACKEND_OLLAMA, via
        # A GGUF reference must be refused here too, not sent to Ollama as
        # if a file path were a registry tag.
        bad = live_backend.measure(hearth_backend.ModelRef.gguf("/m/a.gguf"))
        assert bad["ok"] is False and bad["error"], bad
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("hearth-bench: live-http self-test OK (real sockets, real urllib)")


def _self_test():
    global _http_post, _http_get, _gpu_power_watts, _detect_gpu_count

    orig_http_post = _http_post
    orig_http_get = _http_get
    orig_power = _gpu_power_watts
    orig_gpu_count = _detect_gpu_count
    orig_probe = hearth_hw.probe
    orig_monotonic = time.monotonic

    # An explicit Ollama backend for every test that exercises the Ollama
    # measurement path. Passed rather than selected, so these tests measure
    # what they say they measure on a machine with neither engine installed
    # (this one), instead of quietly routing to whatever select() picks.
    _ob = hearth_backend.OllamaBackend("http://x:11434")

    try:
        # -- _attribute_energy(): THE regression pin. These are the exact ----
        # -- numbers from the live-hardware review: identical generation work
        # -- (0.475s eval) measured twice, with wildly different surrounding
        # -- load/prompt time (making total duration 10.73s vs 0.85s), must
        # -- produce the SAME energy figure. The old before/after
        # -- implementation moved 14 percent between these two; this is the
        # -- property that must never regress again.
        #
        # Each run's sample series includes BOTH load/prompt-phase samples
        # (lower, constant wattage, outside the generation window) and
        # eval-phase samples (higher, constant wattage, inside it) - and
        # deliberately different COUNTS of load-phase samples between the
        # two runs (5 vs 2), mirroring how a longer load/prompt phase in
        # real life gets sampled more times. A correct implementation
        # ignores the load-phase samples entirely and averages only the
        # eval-phase ones, so both runs land on the same figure. An
        # implementation that (incorrectly) averages every sample from the
        # whole call would dilute run 1's average far more than run 2's
        # (more low-wattage samples pulling the mean down), reproducing the
        # exact 14-percent-style drift the review found.
        eval_seconds = 0.475
        eval_count = 40
        load_watts = 50.0
        eval_watts = 150.0

        # run 1: long load+prompt phase (pre_eval = 10.73 - 0.475 = 10.255s),
        # sampled 5 times during load/prompt, 3 times during generation.
        pre_eval_1 = 10.73 - eval_seconds
        samples_1 = [
            (1.0, load_watts), (3.0, load_watts), (5.0, load_watts),
            (7.0, load_watts), (9.0, load_watts),
            (pre_eval_1 + 0.05, eval_watts),
            (pre_eval_1 + 0.20, eval_watts),
            (pre_eval_1 + 0.40, eval_watts),
        ]
        energy_1, note_1, count_1 = _attribute_energy(
            samples_1, 0.0, pre_eval_1 - 0.1, 0.1, eval_seconds, eval_count,
        )
        assert note_1 is None, note_1
        assert count_1 == 3, count_1

        # run 2: short load+prompt phase (pre_eval = 0.85 - 0.475 = 0.375s),
        # sampled only 2 times during load/prompt, same 3 times during
        # generation - the SAME generation work as run 1.
        pre_eval_2 = 0.85 - eval_seconds
        samples_2 = [
            (0.1, load_watts), (0.2, load_watts),
            (pre_eval_2 + 0.05, eval_watts),
            (pre_eval_2 + 0.20, eval_watts),
            (pre_eval_2 + 0.40, eval_watts),
        ]
        energy_2, note_2, count_2 = _attribute_energy(
            samples_2, 0.0, pre_eval_2 - 0.1, 0.1, eval_seconds, eval_count,
        )
        assert note_2 is None, note_2
        assert count_2 == 3, count_2

        assert energy_1 is not None and energy_2 is not None, (energy_1, energy_2)
        assert abs(energy_1 - energy_2) < 1e-9, (
            "same generation work must yield the same energy figure "
            "regardless of surrounding load/prompt duration", energy_1, energy_2,
        )
        # sanity: 150W * 0.475s / 3600 = 0.019791..Wh total, per 1k tokens
        expected = eval_watts * eval_seconds / 3600.0 * 1000.0 / eval_count
        assert abs(energy_1 - expected) < 1e-9, (energy_1, expected)

        # -- _attribute_energy(): missing load/prompt timing -> omit with note
        no_timing_energy, no_timing_note, no_timing_count = _attribute_energy(
            samples_1, 0.0, None, 0.1, eval_seconds, eval_count,
        )
        assert no_timing_energy is None, no_timing_energy
        assert no_timing_note is not None, no_timing_note
        assert no_timing_count == 0, no_timing_count

        # -- _attribute_energy(): samples exist but none land inside the -----
        # -- generation window -> omit with note, not a number computed from
        # -- the wrong interval
        samples_before_window = [(0.0, 999.0), (0.01, 999.0)]
        outside_energy, outside_note, outside_count = _attribute_energy(
            samples_before_window, 0.0, 5.0, 1.0, eval_seconds, eval_count,
        )
        assert outside_energy is None, outside_energy
        assert outside_note is not None and "no sample landed" in outside_note, outside_note

        # -- _attribute_energy(): only in-window samples are averaged, ------
        # -- samples from the load/prompt phase must not pollute the figure
        mixed_samples = [
            (0.0, 50.0),    # load phase: well before the window, must be excluded
            (4.9, 999.0),   # still before the window (window starts at 5.0)
            (5.1, 100.0),   # inside window [5.0, 5.0+eval_seconds]
            (5.3, 100.0),   # inside window
            (5.0 + eval_seconds + 1.0, 999.0),  # after the window, must be excluded
        ]
        mixed_energy, mixed_note, mixed_count = _attribute_energy(
            mixed_samples, 0.0, 4.0, 1.0, eval_seconds, eval_count,
        )
        assert mixed_note is None, mixed_note
        assert mixed_count == 2, mixed_count  # only the two 100.0W samples
        expected_mixed = 100.0 * eval_seconds / 3600.0 * 1000.0 / eval_count
        assert abs(mixed_energy - expected_mixed) < 1e-9, (mixed_energy, expected_mixed)

        # -- _PowerSampler: sampling/recording logic, driven directly (no ----
        # -- real thread, fully deterministic). None readings must be
        # -- dropped, not recorded as a bogus 0.0 watts.
        fake_clock = iter([0.0, 0.1, 0.2, 0.3])
        fake_power = iter([10.0, None, 20.0, None])
        sampler_unit = _PowerSampler(
            power_fn=lambda: next(fake_power),
            clock_fn=lambda: next(fake_clock),
        )
        sampler_unit._sample_once()
        sampler_unit._sample_once()
        sampler_unit._sample_once()
        sampler_unit._sample_once()
        assert sampler_unit.samples() == [(0.0, 10.0), (0.2, 20.0)], sampler_unit.samples()

        # -- _PowerSampler: a real background thread actually collects ------
        # -- samples over real (short) elapsed time. Constant power value
        # -- keeps this assertion exact regardless of exactly how many
        # -- samples land, which is inherently timing-dependent.
        real_sampler = _PowerSampler(interval=0.01, power_fn=lambda: 123.0)
        real_sampler.start()
        time.sleep(0.15)
        real_sampler.stop()
        real_samples = real_sampler.samples()
        assert len(real_samples) >= 1, "background thread collected no samples at all"
        assert all(w == 123.0 for _, w in real_samples), real_samples

        # -- _PowerSampler: stop() is idempotent and never blocks forever ---
        real_sampler.stop()
        real_sampler.stop()

        # -- measure(): full server timing available --------------------------
        resp_full = {
            "model": "m",
            "eval_count": 100,
            "eval_duration": 5_000_000_000,        # 5s -> 20 tok/s
            "prompt_eval_count": 20,
            "prompt_eval_duration": 500_000_000,   # 0.5s
            "load_duration": 250_000_000,          # 0.25s
            "total_duration": 5_750_000_000,
        }
        ps_full = {"models": [{"name": "m:latest", "size": 8_000_000_000, "size_vram": 8_000_000_000}]}

        def post_full(url, payload, timeout):
            assert url.endswith("/api/generate"), url
            assert payload["options"]["temperature"] == 0, payload
            assert payload["options"]["seed"] == 42, payload
            return resp_full

        def get_full(url, timeout):
            assert url.endswith("/api/ps"), url
            return ps_full

        _http_post = post_full
        _http_get = get_full
        _gpu_power_watts = lambda: 200.0  # noqa: E731 - constant stub

        # The fake HTTP call above is a plain in-process function call and
        # returns effectively instantly; the response claims a 5.75s total
        # duration, so the generation window (starting 0.75s after t0) is
        # never actually reached in real elapsed time. Per the "omit rather
        # than guess" rule, energy must be absent here: this is exactly the
        # case a fudged before/after reading would have papered over.
        r = measure_ollama("http://x:11434", "m", num_predict=100)
        assert r["ok"] is True, r
        assert r["error"] is None, r
        assert r["tokens_generated"] == 100, r
        assert r["prompt_tokens"] == 20, r
        assert r["eval_seconds"] == 5.0, r
        assert r["tokens_per_second"] == 20.0, r
        assert r["tokens_per_second_source"] == "server_eval_duration", r
        assert abs(r["time_to_first_token_seconds"] - 0.75) < 1e-9, r
        assert r["load_seconds"] == 0.25, r
        assert r["total_seconds"] == 5.75, r
        assert r["residency"]["loaded"] is True, r
        assert r["residency"]["fully_resident"] is True, r
        assert "energy_wh_per_1k_tokens" not in r, r

        # -- measure(): missing power reading omits energy, does not fabricate
        _gpu_power_watts = lambda: None  # noqa: E731
        r_no_power = measure_ollama("http://x:11434", "m", num_predict=100)
        assert r_no_power["ok"] is True, r_no_power
        assert "energy_wh_per_1k_tokens" not in r_no_power, r_no_power
        assert "energy_note" not in r_no_power, r_no_power

        # -- measure(): full pipeline through the real background sampler, --
        # -- with a fake HTTP call that actually sleeps roughly as long as it
        # -- claims to have taken, so the sampler has a genuine chance to
        # -- land samples inside the generation window. Constant power keeps
        # -- the expected value exact. This is the end-to-end proof that the
        # -- wiring (not just _attribute_energy in isolation) works, AND that
        # -- it stays stable when the surrounding load/prompt phase is much
        # -- longer relative to the same generation work - the actual
        # -- regression scenario from the review, exercised through measure()
        # -- itself rather than only the pure helper above.
        _gpu_power_watts = lambda: 77.0  # noqa: E731 - constant, so avg is exact regardless of sample count/positions
        _http_get = lambda url, timeout: {"models": []}  # noqa: E731

        def make_realtime_post(load_ns, prompt_ns, eval_ns, sleep_seconds):
            resp = {
                "model": "m",
                "eval_count": 20,
                "eval_duration": eval_ns,
                "prompt_eval_count": 5,
                "prompt_eval_duration": prompt_ns,
                "load_duration": load_ns,
                "total_duration": load_ns + prompt_ns + eval_ns,
            }

            def _post(url, payload, timeout):
                time.sleep(sleep_seconds)
                return resp
            return _post

        # short load/prompt: 20ms load + 20ms prompt + 200ms eval, sleep covers it
        _http_post = make_realtime_post(20_000_000, 20_000_000, 200_000_000, 0.30)
        r_short_load = measure_ollama("http://x:11434", "m", num_predict=20)
        assert r_short_load["ok"] is True, r_short_load

        # long load/prompt: 500ms load + 100ms prompt + the SAME 200ms eval
        _http_post = make_realtime_post(500_000_000, 100_000_000, 200_000_000, 0.90)
        r_long_load = measure_ollama("http://x:11434", "m", num_predict=20)
        assert r_long_load["ok"] is True, r_long_load

        # Both must have actually measured energy (proves the sampler landed
        # real samples inside the window in both cases; a machine too slow or
        # too busy to schedule the background thread in time is the only way
        # this could legitimately fail, in which case energy would be absent
        # rather than wrong - never both present and different).
        assert "energy_wh_per_1k_tokens" in r_short_load, r_short_load
        assert "energy_wh_per_1k_tokens" in r_long_load, r_long_load
        # 77W constant -> exact regardless of exactly which samples landed
        expected_realtime = 77.0 * 0.2 / 3600.0 * 1000.0 / 20
        assert abs(r_short_load["energy_wh_per_1k_tokens"] - expected_realtime) < 1e-6, r_short_load
        assert abs(r_long_load["energy_wh_per_1k_tokens"] - expected_realtime) < 1e-6, r_long_load
        assert abs(r_short_load["energy_wh_per_1k_tokens"] - r_long_load["energy_wh_per_1k_tokens"]) < 1e-9, (
            "identical generation work took a 4x longer load/prompt phase in "
            "one run and the energy figure still moved", r_short_load, r_long_load,
        )

        # -- measure(): multi-GPU disclosure - the figure is still reported --
        # -- but with a plain caveat and the GPU count, never silently summed
        _detect_gpu_count = lambda: 3  # noqa: E731
        r_multi_gpu = measure_ollama("http://x:11434", "m", num_predict=20)
        assert r_multi_gpu["ok"] is True, r_multi_gpu
        assert "energy_wh_per_1k_tokens" in r_multi_gpu, r_multi_gpu
        assert r_multi_gpu.get("energy_gpu_count") == 3, r_multi_gpu
        assert r_multi_gpu.get("energy_note") is not None, r_multi_gpu
        assert "3 GPUs" in r_multi_gpu["energy_note"], r_multi_gpu

        # -- measure(): single GPU carries the count but no caveat -----------
        _detect_gpu_count = lambda: 1  # noqa: E731
        r_single_gpu = measure_ollama("http://x:11434", "m", num_predict=20)
        assert r_single_gpu.get("energy_gpu_count") == 1, r_single_gpu
        assert "energy_note" not in r_single_gpu, r_single_gpu

        _detect_gpu_count = orig_gpu_count
        _gpu_power_watts = lambda: None  # noqa: E731

        # -- measure(): server omits duration fields -> wall-clock fallback --
        resp_no_timing = {
            "model": "m",
            "eval_count": 50,
            "prompt_eval_count": 5,
        }

        def post_no_timing(url, payload, timeout):
            return resp_no_timing

        _http_post = post_no_timing
        _http_get = lambda url, timeout: {"models": []}  # noqa: E731 - not loaded, that's fine here

        fake_times = iter([1000.0, 1002.0])  # wall_seconds == 2.0, exactly
        time.monotonic = lambda: next(fake_times)  # noqa: E731
        try:
            r_wall = measure_ollama("http://x:11434", "m", num_predict=50)
        finally:
            time.monotonic = orig_monotonic
        assert r_wall["ok"] is True, r_wall
        assert r_wall["eval_seconds"] is None, r_wall
        assert r_wall["wall_seconds"] == 2.0, r_wall
        assert r_wall["tokens_per_second"] == 25.0, r_wall  # 50 / 2.0
        assert r_wall["tokens_per_second_source"] == "wall_clock", r_wall
        assert r_wall["time_to_first_token_seconds"] is None, r_wall  # no load/prompt duration reported
        assert r_wall["residency"]["loaded"] is False, r_wall

        # -- measure(): eval_count == 0 is a failure, not a silent zero ------
        _http_post = lambda url, payload, timeout: {"eval_count": 0, "error": "context deadline exceeded"}  # noqa: E731
        r_zero = measure_ollama("http://x:11434", "m")
        assert r_zero["ok"] is False, r_zero
        assert r_zero["error"] == "context deadline exceeded", r_zero

        # -- measure(): connection refused never raises -----------------------
        def post_refused(url, payload, timeout):
            raise urllib.error.URLError("Connection refused")

        _http_post = post_refused
        r_refused = measure_ollama("http://x:11434", "m")
        assert r_refused["ok"] is False, r_refused
        assert "Connection refused" in r_refused["error"], r_refused

        # -- measure(): model not pulled (HTTP 404 with a body) never raises -
        def post_404(url, payload, timeout):
            body = json.dumps({"error": 'model "ghost" not found, try pulling it first'}).encode()
            raise urllib.error.HTTPError(url=url, code=404, msg="Not Found", hdrs=None, fp=io.BytesIO(body))

        _http_post = post_404
        r_404 = measure_ollama("http://x:11434", "ghost")
        assert r_404["ok"] is False, r_404
        assert "404" in r_404["error"] and "ghost" in r_404["error"], r_404

        # -- measure(): malformed JSON never raises ---------------------------
        def post_bad_json(url, payload, timeout):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

        _http_post = post_bad_json
        r_bad_json = measure_ollama("http://x:11434", "m")
        assert r_bad_json["ok"] is False, r_bad_json
        assert "invalid JSON" in r_bad_json["error"], r_bad_json

        # -- measure(): empty base_url / empty model never raise -------------
        assert measure_ollama("", "m")["error"] == "empty base_url"
        assert measure_ollama("http://x:11434", "")["error"] == "empty model"

        # -- residency(): exact match, tag-stripped match, partial residency -
        _http_get = lambda url, timeout: {  # noqa: E731
            "models": [
                {"name": "qwen2.5-coder:latest", "size": 4_750_000_000, "size_vram": 4_750_000_000},
                {"name": "big-model:latest", "size": 20_000_000_000, "size_vram": 10_000_000_000},
            ],
        }
        exact = residency("http://x:11434", "qwen2.5-coder:latest")
        assert exact["loaded"] is True and exact["fully_resident"] is True, exact

        tag_stripped = residency("http://x:11434", "qwen2.5-coder")
        assert tag_stripped["loaded"] is True, tag_stripped
        assert tag_stripped["size_bytes"] == 4_750_000_000, tag_stripped

        partial = residency("http://x:11434", "big-model")
        assert partial["loaded"] is True, partial
        assert partial["vram_fraction"] == 0.5, partial
        assert partial["fully_resident"] is False, partial
        assert partial["note"] is not None and "50%" in partial["note"], partial

        not_loaded = residency("http://x:11434", "nope")
        assert not_loaded["loaded"] is False, not_loaded
        assert not_loaded["error"] == "model not currently loaded", not_loaded

        _http_get = lambda url, timeout: (_ for _ in ()).throw(urllib.error.URLError("refused"))  # noqa: E731
        failed_res = residency("http://x:11434", "anything")
        assert failed_res["loaded"] is False, failed_res
        assert failed_res["error"] is not None, failed_res

        assert residency("", "anything")["error"] == "empty base_url"

        # -- hardware_signature(): deterministic, order-independent, ----------
        # -- and different hardware really does produce a different signature
        rtx2060 = {
            "platform": "Linux",
            "gpus": [{"name": "NVIDIA GeForce RTX 2060", "vram_bytes": 6_000_000_000, "vendor": "nvidia"}],
            "cpu_count": 8,
            "system_ram_bytes": 32_000_000_000,
        }
        rtx5080 = {
            "platform": "Windows",
            "gpus": [{"name": "NVIDIA GeForce RTX 5080", "vram_bytes": 16_000_000_000, "vendor": "nvidia"}],
            "cpu_count": 24,
            "system_ram_bytes": 64_000_000_000,
        }
        dual_gpu_a = dict(rtx2060, gpus=[
            {"name": "GPU A", "vram_bytes": 8_000_000_000, "vendor": "nvidia"},
            {"name": "GPU B", "vram_bytes": 16_000_000_000, "vendor": "nvidia"},
        ])
        dual_gpu_b_reordered = dict(rtx2060, gpus=[
            {"name": "GPU B", "vram_bytes": 16_000_000_000, "vendor": "nvidia"},
            {"name": "GPU A", "vram_bytes": 8_000_000_000, "vendor": "nvidia"},
        ])

        hearth_hw.probe = lambda: rtx2060
        sig_2060_a = hardware_signature()
        sig_2060_b = hardware_signature()
        assert sig_2060_a == sig_2060_b, (sig_2060_a, sig_2060_b)  # deterministic

        hearth_hw.probe = lambda: rtx5080
        sig_5080 = hardware_signature()
        assert sig_5080 != sig_2060_a, (sig_5080, sig_2060_a)  # real HW change -> real signature change

        hearth_hw.probe = lambda: dual_gpu_a
        sig_dual_a = hardware_signature()
        hearth_hw.probe = lambda: dual_gpu_b_reordered
        sig_dual_b = hardware_signature()
        assert sig_dual_a == sig_dual_b, (sig_dual_a, sig_dual_b)  # GPU order must not matter

        # -- cached_measure(): hit/miss, disk format, hardware invalidation --
        tmp_dir = tempfile.mkdtemp(prefix="hearth-bench-test-")
        old_data_dir_env = os.environ.get("HEARTH_DATA_DIR")
        os.environ["HEARTH_DATA_DIR"] = tmp_dir
        try:
            call_count = {"n": 0}

            def post_counting(url, payload, timeout):
                call_count["n"] += 1
                return resp_full

            _http_post = post_counting
            _http_get = lambda url, timeout: {"models": []}  # noqa: E731

            hearth_hw.probe = lambda: rtx2060
            first = cached_measure("cache-model", base_url="http://x:11434", num_predict=100,
                                    backend=_ob)
            assert first["ok"] is True, first
            assert first["from_cache"] is False, first
            assert call_count["n"] == 1, call_count
            assert first["tokens_per_second"] == 20.0, first

            second = cached_measure("cache-model", base_url="http://x:11434", num_predict=100,
                                    backend=_ob)
            assert second["from_cache"] is True, second
            assert call_count["n"] == 1, call_count  # no second HTTP call: served from cache
            assert second["tokens_per_second"] == 20.0, second

            forced = cached_measure("cache-model", base_url="http://x:11434", num_predict=100,
                                     force=True, backend=_ob)
            assert forced["from_cache"] is False, forced
            assert call_count["n"] == 2, call_count

            # hardware change invalidates the cache for the same model
            hearth_hw.probe = lambda: rtx5080
            third = cached_measure("cache-model", base_url="http://x:11434", num_predict=100,
                                    backend=_ob)
            assert third["from_cache"] is False, third
            assert call_count["n"] == 3, call_count

            cache_file = os.path.join(tmp_dir, CACHE_SUBDIR, CACHE_FILENAME)
            assert os.path.exists(cache_file), cache_file
            with open(cache_file, "r", encoding="utf-8") as f:
                raw = f.read()
            assert "\n" in raw, "cache file should be indented, human-readable JSON"
            on_disk = json.loads(raw)
            assert isinstance(on_disk, dict) and len(on_disk) == 2, on_disk  # one entry per hw signature

            # a failed measurement is never cached
            hearth_hw.probe = lambda: rtx2060
            _http_post = lambda url, payload, timeout: (_ for _ in ()).throw(urllib.error.URLError("down"))  # noqa: E731
            failed = cached_measure("flaky-model", base_url="http://x:11434", num_predict=100,
                                     backend=_ob)
            assert failed["ok"] is False, failed
            assert failed["from_cache"] is False, failed
            cache_after_failure = _load_cache()
            assert not any(k.endswith("::ollama:flaky-model") for k in cache_after_failure), cache_after_failure
        finally:
            if old_data_dir_env is None:
                os.environ.pop("HEARTH_DATA_DIR", None)
            else:
                os.environ["HEARTH_DATA_DIR"] = old_data_dir_env

        # -- _rank_key(): ok always outranks not-ok, even against a stale or --
        # -- corrupted tokens_per_second on the failed side. This is the case
        # -- a plain "-tokens_per_second" sort would get wrong, since today's
        # -- measure() never actually produces this combination on its own.
        healthy_but_slow = {"ok": True, "tokens_per_second": 1.0}
        failed_but_looks_fast = {"ok": False, "tokens_per_second": 9999.0}
        assert _rank_key(healthy_but_slow) < _rank_key(failed_but_looks_fast), (
            healthy_but_slow, failed_but_looks_fast,
        )

        # -- compare(): ranked fastest first, failures sort last --------------
        tmp_dir2 = tempfile.mkdtemp(prefix="hearth-bench-test-cmp-")
        old_data_dir_env2 = os.environ.get("HEARTH_DATA_DIR")
        os.environ["HEARTH_DATA_DIR"] = tmp_dir2
        try:
            hearth_hw.probe = lambda: rtx2060

            responses = {
                "cmp-slow": {"eval_count": 10, "eval_duration": 1_000_000_000},   # 10 tok/s
                "cmp-fast": {"eval_count": 50, "eval_duration": 1_000_000_000},   # 50 tok/s
            }

            def post_compare(url, payload, timeout):
                model = payload["model"]
                if model == "cmp-broken":
                    raise urllib.error.URLError("refused")
                return responses[model]

            _http_post = post_compare
            _http_get = lambda url, timeout: {"models": []}  # noqa: E731

            ranked = compare(["cmp-slow", "cmp-fast", "cmp-broken"],
                              base_url="http://x:11434", backend=_ob)
            assert [r["model"] for r in ranked] == ["cmp-fast", "cmp-slow", "cmp-broken"], ranked
            assert [r["rank"] for r in ranked] == [1, 2, 3], ranked
            assert ranked[0]["tokens_per_second"] == 50.0, ranked
            assert ranked[1]["tokens_per_second"] == 10.0, ranked
            assert ranked[2]["ok"] is False, ranked
        finally:
            if old_data_dir_env2 is None:
                os.environ.pop("HEARTH_DATA_DIR", None)
            else:
                os.environ["HEARTH_DATA_DIR"] = old_data_dir_env2

    finally:
        _http_post = orig_http_post
        _http_get = orig_http_get
        _gpu_power_watts = orig_power
        _detect_gpu_count = orig_gpu_count
        hearth_hw.probe = orig_probe
        time.monotonic = orig_monotonic

    # -- hardware_signature() and _gpu_power_watts() work against the real ---
    # -- machine too (whatever it is), and never raise -----------------------
    real_sig = hardware_signature()
    assert isinstance(real_sig, str) and len(real_sig) == 16, real_sig
    real_power = _gpu_power_watts()
    assert real_power is None or (isinstance(real_power, float) and real_power >= 0.0), real_power
    real_gpu_count = _detect_gpu_count()
    assert real_gpu_count is None or (isinstance(real_gpu_count, int) and real_gpu_count >= 0), real_gpu_count

    # -- _load_cache(): a corrupt or truncated cache.json is a clean miss, ---
    # -- never a crash. Covers both "not JSON at all" and "valid JSON that
    # -- isn't the dict shape this module writes".
    corrupt_dir = tempfile.mkdtemp(prefix="hearth-bench-test-corrupt-")
    old_data_dir_env3 = os.environ.get("HEARTH_DATA_DIR")
    os.environ["HEARTH_DATA_DIR"] = corrupt_dir
    try:
        cache_file = os.path.join(corrupt_dir, CACHE_SUBDIR, CACHE_FILENAME)
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)

        with open(cache_file, "w", encoding="utf-8") as f:
            f.write("{not valid json at all")
        assert _load_cache() == {}, "truncated/garbage JSON must be a clean cache miss"

        with open(cache_file, "w", encoding="utf-8") as f:
            f.write("")
        assert _load_cache() == {}, "an empty cache file must be a clean cache miss"

        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)  # valid JSON, but not the dict shape this module writes
        assert _load_cache() == {}, "a valid-JSON-but-wrong-shape cache file must be a clean cache miss"

        # cached_measure() must recover cleanly from a corrupt cache file
        # rather than raising: a miss, then a normal successful measurement.
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write("{garbage")
        _http_post = lambda url, payload, timeout: {  # noqa: E731
            "eval_count": 10, "eval_duration": 1_000_000_000,
        }
        _http_get = lambda url, timeout: {"models": []}  # noqa: E731
        _gpu_power_watts = lambda: None  # noqa: E731
        hearth_hw.probe = lambda: rtx2060
        recovered = cached_measure("recovery-model", base_url="http://x:11434", num_predict=10,
                                    backend=_ob)
        assert recovered["ok"] is True, recovered
        assert recovered["from_cache"] is False, recovered
    finally:
        _http_post = orig_http_post
        _http_get = orig_http_get
        _gpu_power_watts = orig_power
        hearth_hw.probe = orig_probe
        if old_data_dir_env3 is None:
            os.environ.pop("HEARTH_DATA_DIR", None)
        else:
            os.environ["HEARTH_DATA_DIR"] = old_data_dir_env3

    # -- Backend dispatch: measure() routes to the ACTIVE engine ------------
    #
    # The point of the cutover. Neither engine is installed on the machine
    # running this test, so both paths are driven with explicit stand-in
    # backends rather than by whatever select() would pick here.
    class _StubBackend:
        name = hearth_backend.BACKEND_LLAMA

        def __init__(self):
            self.calls = []

        def measure(self, model, prompt=None, num_predict=None, timeout=None):
            self.calls.append((model, num_predict))
            return {"ok": True, "backend": self.name, "model": str(model),
                    "tokens_generated": 12, "wall_seconds": 1.0,
                    "tokens_per_second": 12.0,
                    "tokens_per_second_source": "wall_clock"}

    stub = _StubBackend()
    r = measure("C:\\models\\a.gguf", backend=stub, num_predict=32)
    assert r["backend"] == hearth_backend.BACKEND_LLAMA, r
    # The llama path is wall-clock, and says so. A caller comparing this
    # against an Ollama figure needs that field to know they are not the
    # same kind of number.
    assert r["tokens_per_second_source"] == "wall_clock", r
    assert stub.calls == [("C:\\models\\a.gguf", 32)], stub.calls
    # base_url is meaningless to the bundled engine and must not reach it.
    r = measure("C:\\models\\a.gguf", base_url="http://x:11434", backend=stub)
    assert r["ok"] is True, r

    # The Ollama backend routes to measure_ollama, with its server-reported
    # rate, against the same stubbed transport used above.
    _http_post = lambda url, payload, timeout: {  # noqa: E731
        "eval_count": 10, "eval_duration": 1_000_000_000}
    _http_get = lambda url, timeout: {"models": []}  # noqa: E731
    _gpu_power_watts = lambda: None  # noqa: E731
    try:
        r = measure("m:latest", base_url="http://x:11434", backend=_ob)
        assert r["backend"] == hearth_backend.BACKEND_OLLAMA, r
        assert r["tokens_per_second"] == 10.0, r
        assert r["tokens_per_second_source"] == "server_eval_duration", r
        # An Ollama backend with no explicit base_url falls back to its own,
        # so a non-default port is still measured rather than silently
        # replaced by the module default.
        r = measure("m:latest", backend=hearth_backend.OllamaBackend("http://other:9999"))
        assert r["base_url"] == "http://other:9999", r
    finally:
        _http_post = orig_http_post
        _http_get = orig_http_get
        _gpu_power_watts = orig_power

    # measure_ollama refuses a GGUF reference rather than sending a file
    # path to Ollama as if it were a registry tag.
    r = measure_ollama("http://x:11434", hearth_backend.ModelRef.gguf("/m/a.gguf"))
    assert r["ok"] is False and "ollama model reference" in r["error"], r
    # ...and unwraps a correctly-typed one.
    assert measure_ollama("", hearth_backend.ModelRef.ollama("m"))["error"] == "empty base_url"

    # -- Cache keys are backend-qualified ----------------------------------
    #
    # A GGUF path and an Ollama tag must never share a cache entry: the two
    # engines derive tokens_per_second differently, so serving one from the
    # other's entry would silently mix a wall-clock number with a
    # server-reported one.
    k_ol = _cache_key("qwen2.5-coder", "HWSIG", hearth_backend.BACKEND_OLLAMA)
    k_gg = _cache_key("qwen2.5-coder", "HWSIG", hearth_backend.BACKEND_LLAMA)
    assert k_ol != k_gg, (k_ol, k_gg)
    assert k_ol == "HWSIG::ollama:qwen2.5-coder", k_ol
    assert k_gg == "HWSIG::gguf:qwen2.5-coder", k_gg
    # An explicit ModelRef keys off its own kind, whatever backend is named.
    assert _cache_key(hearth_backend.ModelRef.gguf("/m/a.gguf"), "HWSIG",
                      hearth_backend.BACKEND_OLLAMA) == "HWSIG::gguf:/m/a.gguf"
    # Same hardware, same model, same backend is a stable key.
    assert k_ol == _cache_key("qwen2.5-coder", "HWSIG", hearth_backend.BACKEND_OLLAMA)

    _run_live_http_self_test()

    print("hearth-bench self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
