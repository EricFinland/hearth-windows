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
deterministic generation against the user's own Ollama server on their own
hardware, reads back what the server itself reports actually happened, and
shows that number. If a measurement cannot be taken, the module says so
plainly instead of guessing.

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

## Energy, only if it can be read honestly

If `nvidia-smi --query-gpu=power.draw` is readable, measure() samples GPU
power draw immediately before and immediately after the generation call
and reports energy_wh_per_1k_tokens, the local analogue of a hosted tool
reporting dollars. This is a real but rough approximation (an average of
two instantaneous samples standing in for the true power curve over the
generation), so it is documented as such. When nvidia-smi or a GPU is not
available, the field is left out of the result entirely. It is never
fabricated or replaced with a guess.

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
# Core measurement
# --------------------------------------------------------------------------

def measure(base_url, model, prompt=None, num_predict=DEFAULT_NUM_PREDICT, timeout=GENERATE_TIMEOUT):
    """Run one short, deterministic generation against Ollama and report
    what actually happened. See the module docstring for which timing
    fields are trusted and why, how residency is attached, and the
    conditions under which energy_wh_per_1k_tokens appears.

    Always returns a dict, never raises: connection refused, model not
    pulled, a timeout, or a malformed response all come back as
    ok: False with error explaining what went wrong.
    """
    prompt = DEFAULT_PROMPT if prompt is None else prompt
    base_url = (base_url or "").rstrip("/")

    result = {
        "ok": False,
        "error": None,
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

    power_before = _gpu_power_watts()
    t0 = time.monotonic()
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
    power_after = _gpu_power_watts()

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

    # Energy: only when both power samples were actually read. Never guess.
    if power_before is not None and power_after is not None:
        duration_for_energy = eval_seconds if eval_seconds else wall_seconds
        if duration_for_energy and duration_for_energy > 0:
            avg_watts = (power_before + power_after) / 2.0
            energy_wh = avg_watts * duration_for_energy / 3600.0
            result["energy_wh_per_1k_tokens"] = energy_wh * 1000.0 / eval_count

    result["ok"] = True
    return result


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


def _cache_key(model, hw_sig):
    return "{}::{}".format(hw_sig, model)


def cached_measure(base_url, model, prompt=None, num_predict=DEFAULT_NUM_PREDICT,
                    force=False, timeout=GENERATE_TIMEOUT):
    """measure(), but only once per (model, hardware_signature()).

    A hit returns the stored result with from_cache: True added. A miss
    (including "hardware changed since the last measurement") runs
    measure() and, if it succeeded, stores the result before returning it.
    A failed measurement (ok: False) is never cached, so a transient
    problem (Ollama briefly down) does not stick around and shadow a
    working server on the next call.
    """
    hw_sig = hardware_signature()
    cache = _load_cache()
    key = _cache_key(model, hw_sig)
    entry = cache.get(key)
    if entry and not force and entry.get("ok"):
        hit = dict(entry)
        hit["from_cache"] = True
        return hit

    result = measure(base_url, model, prompt=prompt, num_predict=num_predict, timeout=timeout)
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


def compare(base_url, models, prompt=None, num_predict=DEFAULT_NUM_PREDICT,
            force=False, timeout=GENERATE_TIMEOUT):
    """Measure several models (via cached_measure) and return them ranked
    fastest first. A model that could not be measured sorts to the bottom,
    regardless of any partial numbers it produced, and keeps ok: False so
    a caller can tell "slow" from "unmeasurable" apart. Each entry gets a
    1-based rank field.
    """
    results = [
        cached_measure(base_url, m, prompt=prompt, num_predict=num_predict,
                        force=force, timeout=timeout)
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
        description="Measure real Ollama throughput on this machine. Never predicts it.",
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--base-url", default=DEFAULT_OLLAMA)
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

    if args.command == "measure":
        if args.no_cache:
            result = measure(args.base_url, args.model, num_predict=args.num_predict)
        else:
            result = cached_measure(args.base_url, args.model, num_predict=args.num_predict, force=args.force)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "residency":
        print(json.dumps(residency(args.base_url, args.model), indent=2))
        return 0

    if args.command == "compare":
        ranked = compare(args.base_url, args.models, force=args.force)
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

        r = measure(base_url, "fixture-model:latest", num_predict=40)
        assert r["ok"] is True, r
        assert r["tokens_generated"] == 40, r
        assert r["tokens_per_second"] == 20.0, r
        assert r["tokens_per_second_source"] == "server_eval_duration", r
        assert abs(r["time_to_first_token_seconds"] - 0.4) < 1e-9, r
        assert r["residency"]["loaded"] is True, r
        assert r["residency"]["fully_resident"] is True, r

        missing = measure(base_url, "missing-model", num_predict=40)
        assert missing["ok"] is False, missing
        assert "missing-model" in (missing["error"] or ""), missing

        res = residency(base_url, "fixture-model")
        assert res["loaded"] is True, res
        assert res["vram_fraction"] == 1.0, res
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("hearth-bench: live-http self-test OK (real sockets, real urllib)")


def _self_test():
    global _http_post, _http_get, _gpu_power_watts

    orig_http_post = _http_post
    orig_http_get = _http_get
    orig_power = _gpu_power_watts
    orig_probe = hearth_hw.probe
    orig_monotonic = time.monotonic

    try:
        # -- measure(): full server timing available -------------------------
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
        _gpu_power_watts = lambda: 200.0  # noqa: E731 - constant stub, before and after

        r = measure("http://x:11434", "m", num_predict=100)
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
        # energy: 200W avg * 5s / 3600 = 0.277..Wh total, per 1k tokens = 25/9
        assert "energy_wh_per_1k_tokens" in r, r
        assert abs(r["energy_wh_per_1k_tokens"] - (25.0 / 9.0)) < 1e-9, r

        # -- measure(): missing power reading omits energy, does not fabricate
        _gpu_power_watts = lambda: None  # noqa: E731
        r_no_power = measure("http://x:11434", "m", num_predict=100)
        assert r_no_power["ok"] is True, r_no_power
        assert "energy_wh_per_1k_tokens" not in r_no_power, r_no_power

        # -- measure(): one of the two power samples missing also omits energy
        power_seq = iter([200.0, None])
        _gpu_power_watts = lambda: next(power_seq)  # noqa: E731
        r_half_power = measure("http://x:11434", "m", num_predict=100)
        assert "energy_wh_per_1k_tokens" not in r_half_power, r_half_power
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
            r_wall = measure("http://x:11434", "m", num_predict=50)
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
        r_zero = measure("http://x:11434", "m")
        assert r_zero["ok"] is False, r_zero
        assert r_zero["error"] == "context deadline exceeded", r_zero

        # -- measure(): connection refused never raises -----------------------
        def post_refused(url, payload, timeout):
            raise urllib.error.URLError("Connection refused")

        _http_post = post_refused
        r_refused = measure("http://x:11434", "m")
        assert r_refused["ok"] is False, r_refused
        assert "Connection refused" in r_refused["error"], r_refused

        # -- measure(): model not pulled (HTTP 404 with a body) never raises -
        def post_404(url, payload, timeout):
            body = json.dumps({"error": 'model "ghost" not found, try pulling it first'}).encode()
            raise urllib.error.HTTPError(url=url, code=404, msg="Not Found", hdrs=None, fp=io.BytesIO(body))

        _http_post = post_404
        r_404 = measure("http://x:11434", "ghost")
        assert r_404["ok"] is False, r_404
        assert "404" in r_404["error"] and "ghost" in r_404["error"], r_404

        # -- measure(): malformed JSON never raises ---------------------------
        def post_bad_json(url, payload, timeout):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

        _http_post = post_bad_json
        r_bad_json = measure("http://x:11434", "m")
        assert r_bad_json["ok"] is False, r_bad_json
        assert "invalid JSON" in r_bad_json["error"], r_bad_json

        # -- measure(): empty base_url / empty model never raise -------------
        assert measure("", "m")["error"] == "empty base_url"
        assert measure("http://x:11434", "")["error"] == "empty model"

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
            first = cached_measure("http://x:11434", "cache-model", num_predict=100)
            assert first["ok"] is True, first
            assert first["from_cache"] is False, first
            assert call_count["n"] == 1, call_count
            assert first["tokens_per_second"] == 20.0, first

            second = cached_measure("http://x:11434", "cache-model", num_predict=100)
            assert second["from_cache"] is True, second
            assert call_count["n"] == 1, call_count  # no second HTTP call: served from cache
            assert second["tokens_per_second"] == 20.0, second

            forced = cached_measure("http://x:11434", "cache-model", num_predict=100, force=True)
            assert forced["from_cache"] is False, forced
            assert call_count["n"] == 2, call_count

            # hardware change invalidates the cache for the same model
            hearth_hw.probe = lambda: rtx5080
            third = cached_measure("http://x:11434", "cache-model", num_predict=100)
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
            failed = cached_measure("http://x:11434", "flaky-model", num_predict=100)
            assert failed["ok"] is False, failed
            assert failed["from_cache"] is False, failed
            cache_after_failure = _load_cache()
            assert not any(k.endswith("::flaky-model") for k in cache_after_failure), cache_after_failure
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

            ranked = compare("http://x:11434", ["cmp-slow", "cmp-fast", "cmp-broken"])
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
        hearth_hw.probe = orig_probe
        time.monotonic = orig_monotonic

    # -- hardware_signature() and _gpu_power_watts() work against the real ---
    # -- machine too (whatever it is), and never raise -----------------------
    real_sig = hardware_signature()
    assert isinstance(real_sig, str) and len(real_sig) == 16, real_sig
    real_power = _gpu_power_watts()
    assert real_power is None or (isinstance(real_power, float) and real_power >= 0.0), real_power

    _run_live_http_self_test()

    print("hearth-bench self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
