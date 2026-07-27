#!/usr/bin/env python3
r"""hearth model shop: a curated catalog of local models, with an honest verdict
on whether each one will actually run well on the machine in front of the user.

Most model catalogues show a parameter count and a download size and leave the
user to guess. That is a bad trade for a desktop app whose whole pitch is "no
command line, no config files, just click and run." This module is the data
and logic layer behind the shop: a small, defensible catalog (CATALOG),
weighted toward coding since Hearth Code is the flagship surface, plus the
arithmetic that turns "16GB of VRAM" and "a 14B model at 8k context" into a
verdict a non-technical user can act on.

THE KV CACHE IS STILL THE PART PEOPLE GET WRONG. See hearth_hw for the full
argument: model weights are not the whole story, and the exact same model at
a longer context can need meaningfully more memory once its KV cache is
counted in. Every CATALOG entry carries kv_bytes_per_token, derived from that
model's architecture (layer count, KV head count, head dimension), never
guessed from parameter count. Where the architecture is not confidently known,
the entry says so in its "kv_confidence" field and uses a conservative
(deliberately large) estimate rather than inventing a number - see the
kv_calc comments on each entry for the arithmetic and its source.

Verdict vocabulary (best to worst), see verdict_for():
  great          - fits fully in VRAM at the requested context, with roomy
                   headroom (>= GREAT_HEADROOM_RATIO of total VRAM). Runs
                   fast, nothing to worry about.
  good           - fits fully in VRAM at the requested context, but headroom
                   is tight. Runs fine today; a bigger prompt or a second
                   loaded model could push it over.
  reduced_context - does not fit in VRAM at the requested context, but does
                   fit at a shorter one. max_context_tokens says how short.
  cpu_spillover  - does not fit in VRAM at any useful context, but the
                   weights and KV cache fit in system RAM. It will run, just
                   slowly (partly or fully on CPU); no numeric slowdown is
                   predicted, see hearth_hw's throughput stance.
  wont_fit       - does not fit in VRAM or in system RAM. Do not offer this
                   model on this machine.

Disk space is a separate, independent check (free_disk_bytes / disk_ok):
a user with 8GB free cannot install a 17GB model, and finding that out at 90%
through a download is a terrible experience.

Deliberately out of scope, matching hearth_hw's stance: no downloading, no
network calls of any kind, no tokens-per-second prediction. Throughput
prediction from bandwidth and parameter count breaks badly on
mixture-of-experts models; Hearth measures real throughput on the user's
machine instead of guessing at it.

Standard library only. Pure and offline: this module computes and returns
data, it does not write files or touch the network. hw=None on the public
functions is a convenience that defers to hearth_hw.probe() (itself pure
local detection, no network); pass an explicit hw dict to stay hermetic.
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_hw  # noqa: E402

# -- verdict vocabulary -------------------------------------------------------

VERDICT_GREAT = "great"
VERDICT_GOOD = "good"
VERDICT_REDUCED_CONTEXT = "reduced_context"
VERDICT_CPU_SPILLOVER = "cpu_spillover"
VERDICT_WONT_FIT = "wont_fit"

# Best to worst, used for sorting the catalog listing.
VERDICT_RANK = {
    VERDICT_GREAT: 0,
    VERDICT_GOOD: 1,
    VERDICT_REDUCED_CONTEXT: 2,
    VERDICT_CPU_SPILLOVER: 3,
    VERDICT_WONT_FIT: 4,
}

# -- tunables, documented since every one is a judgment call ------------------

# A "typical coding session" context: enough for a few files of surrounding
# code plus a real prompt, without assuming the user configured anything.
DEFAULT_CONTEXT_TOKENS = 8192

# Below this, a coding assistant can barely hold one file in context. A model
# that only fits at a shorter context than this is not "reduced_context", it
# is effectively unusable here - see verdict_for's cpu/wont_fit branch.
MIN_USEFUL_CONTEXT_TOKENS = 2048

# A model that fits with less than this fraction of the GPU's total VRAM left
# over is "good" (it runs, today, as configured) rather than "great" (it runs
# with room to spare - a bigger prompt or a second loaded model won't push it
# over). This is a judgment call, not a spec: 20% of a 24GB card is ~4.8GB of
# slack, comfortably more than most single-response KV growth; 20% of a 6GB
# card is a little over 1GB, which is tight but real headroom.
GREAT_HEADROOM_RATIO = 0.20

# When a model does not fit in VRAM, it can still run on CPU (or partly on
# CPU, partly on GPU) if it fits in system RAM. The OS, the shell, and Hearth
# itself need some of that RAM too, so reserve a slice before checking.
RAM_RESERVE_RATIO = 0.15
RAM_RESERVE_MIN_BYTES = 2 * 1024 ** 3

# Beyond the raw download, Ollama needs room for the file as it lands plus a
# little slack; without this, a user could clear a download at 99% free disk
# and then fail on the last few bytes.
DISK_BUFFER_BYTES = 512 * 1024 ** 2


# -- curated catalog -----------------------------------------------------------
#
# Roughly six to twelve entries, weighted toward coding since Hearth Code is
# the flagship surface. download_bytes is an approximate figure matching the
# model's typical Ollama library listing for that quantisation; the exact
# blob size drifts a little between quant builds and is not load-bearing here
# - what matters for the fit calculation is kv_bytes_per_token, which is
# derived from architecture, not guessed from parameter count.
#
# kv_calc records the arithmetic: kv_bytes_per_token = 2 (K and V) *
# layers * kv_heads * head_dim * bytes_per_element. kv_confidence is one of:
#   "published_config"      - taken from the model family's published
#                              architecture config (layer/head counts), high
#                              confidence.
#   "recalled_estimate"      - recalled from the model's technical report,
#                              not independently reverified against a live
#                              config file; treat as good-faith, not gospel.
#   "conservative_overestimate" - the exact attention configuration (in
#                              particular, whether it uses grouped-query
#                              attention) is not confidently known, so this
#                              assumes plain multi-head attention (kv_heads
#                              == attention heads), which is never smaller
#                              than the true figure. Safe to over-count KV
#                              cost, never safe to under-count it.
CATALOG = [
    {
        "id": "qwen2.5-coder:1.5b-instruct-q4_K_M",
        "label": "Qwen2.5 Coder 1.5B",
        "params_b": 1.5,
        "quantization": "Q4_K_M",
        "download_bytes": 986_000_000,
        "description": "Smallest capable coding assistant; fits on integrated "
                        "graphics or a CPU-only box, the always-works option.",
        "license": "Apache-2.0",
        "focus": "coding",
        "kv_bytes_per_token": 28_672,
        "kv_confidence": "published_config",
        "kv_calc": {"layers": 28, "kv_heads": 2, "head_dim": 128, "bytes_per_element": 2},
    },
    {
        "id": "qwen2.5-coder:7b-instruct-q4_K_M",
        "label": "Qwen2.5 Coder 7B",
        "params_b": 7.0,
        "quantization": "Q4_K_M",
        "download_bytes": 4_700_000_000,
        "description": "The reference all-rounder: strong at completion, "
                        "refactors, and multi-file reasoning at a size that "
                        "fits an 8GB card.",
        "license": "Apache-2.0",
        "focus": "coding",
        "kv_bytes_per_token": 57_344,
        "kv_confidence": "published_config",
        "kv_calc": {"layers": 28, "kv_heads": 4, "head_dim": 128, "bytes_per_element": 2},
    },
    {
        "id": "qwen2.5-coder:14b-instruct-q4_K_M",
        "label": "Qwen2.5 Coder 14B",
        "params_b": 14.0,
        "quantization": "Q4_K_M",
        "download_bytes": 9_000_000_000,
        "description": "Noticeably sharper than the 7B on harder refactors "
                        "and longer reasoning chains; wants a 16GB+ card.",
        "license": "Apache-2.0",
        "focus": "coding",
        "kv_bytes_per_token": 196_608,
        "kv_confidence": "published_config",
        "kv_calc": {"layers": 48, "kv_heads": 8, "head_dim": 128, "bytes_per_element": 2},
    },
    {
        "id": "qwen2.5-coder:32b-instruct-q4_K_M",
        "label": "Qwen2.5 Coder 32B",
        "params_b": 32.0,
        "quantization": "Q4_K_M",
        "download_bytes": 19_000_000_000,
        "description": "The largest practical local coding model; near "
                        "frontier-model quality on many tasks, needs a 24GB "
                        "card to breathe.",
        "license": "Apache-2.0",
        "focus": "coding",
        "kv_bytes_per_token": 262_144,
        "kv_confidence": "published_config",
        "kv_calc": {"layers": 64, "kv_heads": 8, "head_dim": 128, "bytes_per_element": 2},
    },
    {
        "id": "deepseek-coder:6.7b-instruct-q4_K_M",
        "label": "DeepSeek Coder 6.7B",
        "params_b": 6.7,
        "quantization": "Q4_K_M",
        "download_bytes": 3_800_000_000,
        "description": "Trained on a huge code-heavy corpus; a strong second "
                        "opinion alongside Qwen Coder, at a similar size.",
        "license": "DeepSeek License (custom; free for research and most "
                    "commercial use, see model card)",
        "focus": "coding",
        # Whether this family uses grouped-query attention is not confidently
        # known here, so this assumes plain multi-head attention (kv_heads ==
        # attention heads) as a conservative, deliberately large estimate.
        "kv_bytes_per_token": 524_288,
        "kv_confidence": "conservative_overestimate",
        "kv_calc": {"layers": 32, "kv_heads": 32, "head_dim": 128, "bytes_per_element": 2},
    },
    {
        "id": "codellama:13b-instruct-q4_K_M",
        "label": "Code Llama 13B",
        "params_b": 13.0,
        "quantization": "Q4_K_M",
        "download_bytes": 7_400_000_000,
        "description": "Meta's established code model; broad language "
                        "coverage and infill support, a solid known baseline.",
        "license": "Llama 2 Community License",
        "focus": "coding",
        # Llama 2 13B is well documented as plain multi-head attention (no
        # GQA; only the 70B size in that family uses it).
        "kv_bytes_per_token": 819_200,
        "kv_confidence": "published_config",
        "kv_calc": {"layers": 40, "kv_heads": 40, "head_dim": 128, "bytes_per_element": 2},
    },
    {
        "id": "starcoder2:15b-q4_K_M",
        "label": "StarCoder2 15B",
        "params_b": 15.0,
        "quantization": "Q4_K_M",
        "download_bytes": 9_100_000_000,
        "description": "Trained purely on permissively licensed code; excels "
                        "at completion across a very wide range of languages.",
        "license": "BigCode OpenRAIL-M",
        "focus": "coding",
        "kv_bytes_per_token": 81_920,
        "kv_confidence": "recalled_estimate",
        "kv_calc": {"layers": 40, "kv_heads": 4, "head_dim": 128, "bytes_per_element": 2},
    },
    {
        "id": "llama3.1:8b-instruct-q4_K_M",
        "label": "Llama 3.1 8B",
        "params_b": 8.0,
        "quantization": "Q4_K_M",
        "download_bytes": 4_700_000_000,
        "description": "General-purpose fallback for chat, writing, and "
                        "reasoning outside of coding tasks.",
        "license": "Llama 3.1 Community License",
        "focus": "general",
        "kv_bytes_per_token": 131_072,
        "kv_confidence": "published_config",
        "kv_calc": {"layers": 32, "kv_heads": 8, "head_dim": 128, "bytes_per_element": 2},
    },
    {
        "id": "mistral:7b-instruct-q4_K_M",
        "label": "Mistral 7B Instruct",
        "params_b": 7.0,
        "quantization": "Q4_K_M",
        "download_bytes": 4_100_000_000,
        "description": "Fast, well-rounded general chat model; a lighter "
                        "alternative to Llama 3.1 8B.",
        "license": "Apache-2.0",
        "focus": "general",
        "kv_bytes_per_token": 131_072,
        "kv_confidence": "published_config",
        "kv_calc": {"layers": 32, "kv_heads": 8, "head_dim": 128, "bytes_per_element": 2},
    },
    {
        "id": "phi3.5:3.8b-mini-instruct-q4_K_M",
        "label": "Phi-3.5 Mini 3.8B",
        "params_b": 3.8,
        "quantization": "Q4_K_M",
        "download_bytes": 2_200_000_000,
        "description": "Small, punches above its weight on reasoning; the "
                        "go-to for weak or integrated GPUs.",
        "license": "MIT",
        "focus": "general",
        # Recalled as plain multi-head attention (kv_heads == attention
        # heads); flagged conservative in case a GQA variant exists.
        "kv_bytes_per_token": 393_216,
        "kv_confidence": "conservative_overestimate",
        "kv_calc": {"layers": 32, "kv_heads": 32, "head_dim": 96, "bytes_per_element": 2},
    },
]


def free_disk_bytes(path=None):
    """Free bytes on the volume containing path (default: the current
    working directory). 0 if it cannot be determined; never raises.
    """
    target = path or os.getcwd()
    try:
        return shutil.disk_usage(target).free
    except OSError:
        return 0


def _gpu_vram_bytes(hw):
    """(vram_bytes, approximate) for the single GPU a model would be loaded
    onto, from a hearth_hw-shaped dict.

    Deliberately the *largest single GPU*, not the sum across GPUs: Ollama
    does not reliably pool multiple cards' VRAM for one model, and the shop's
    job is honesty, not optimism. A machine with two 8GB cards is treated
    like an 8GB machine, not a 16GB one.

    vram_bytes is the GPU's total nameplate capacity as reported by
    hearth_hw, not "available" or "free" VRAM: it has no accounting for
    memory already claimed by the OS, a browser, or another loaded model.
    Callers that show this figure to a user should be clear it is a ceiling,
    not a live free-memory reading.

    approximate is True when that reading came from hearth_hw's WMI/CIM or
    wmic fallback path rather than nvidia-smi (see hearth_hw's module
    docstring: Win32_VideoController.AdapterRAM is a signed 32-bit field
    that misreports anything above ~4GB, in either direction - it can read
    low, high, or even negative-clamped-to-zero). Every caller that turns
    vram_bytes into a verdict or a message MUST also look at approximate;
    a confident "great" or "wont_fit" built on a guessed number is not
    honest. (0, False) when no GPU was detected at all - that is an exact
    reading of "nothing", not a guess.
    """
    gpus = hw.get("gpus") or []
    if not gpus:
        return 0, False
    best = max(gpus, key=lambda g: g.get("vram_bytes", 0))
    return best.get("vram_bytes", 0), bool(best.get("approximate", False))


def _ram_budget_bytes(hw):
    """System RAM actually available to a spilled-over model: total RAM minus
    a reserve for the OS, the shell, and Hearth itself. Never negative.
    """
    ram = hw.get("system_ram_bytes", 0) or 0
    reserve = max(RAM_RESERVE_MIN_BYTES, int(ram * RAM_RESERVE_RATIO))
    return max(0, ram - reserve)


# Appended to a verdict's message whenever the VRAM figure it was judged
# against is a known-unreliable reading (see _gpu_vram_bytes), so no caller
# can display a verdict without also surfacing the uncertainty behind it.
_VRAM_APPROX_NOTE = (
    " This machine's VRAM size could not be read precisely (no nvidia-smi "
    "available), so the figure behind this verdict is an approximate "
    "reading and could be significantly off, in either direction."
)


def verdict_for(model, hw, context_tokens=None):
    """The shop's core judgment call: will `model` actually run well on `hw`?

    Returns a dict with at least:
      verdict              - one of the VERDICT_* constants.
      message               - a short human-readable explanation. Hedged
                              with _VRAM_APPROX_NOTE whenever vram_approximate
                              is True.
      requested_context_tokens - the context length this verdict was judged
                              against.
      max_context_tokens    - the largest context that fits in VRAM alone,
                              given the model's full weights already resident.
                              None when weights alone do not fit in VRAM at
                              any context.
      vram_bytes            - VRAM this verdict was judged against (the
                              largest single GPU; see _gpu_vram_bytes).
      vram_approximate      - True when vram_bytes came from a known-unreliable
                              fallback reading rather than nvidia-smi (see
                              _gpu_vram_bytes and hearth_hw's module
                              docstring). A caller MUST check this before
                              presenting the verdict with any confidence; a
                              "great" computed from an approximate reading is
                              downgraded to "good" here for exactly that
                              reason (see below).
      ram_bytes             - the RAM budget considered for spillover (total
                              RAM minus reserve; see _ram_budget_bytes). This
                              is always the reserve-adjusted budget, in every
                              verdict branch, never the raw system total - so
                              ram_bytes - required_bytes reconciles with
                              headroom_bytes whenever the verdict was decided
                              against the RAM pool.
      required_bytes        - weights + KV cache at requested_context_tokens.
      headroom_bytes        - required_bytes subtracted from whichever pool
                              (VRAM or RAM) the verdict was decided against;
                              negative means it does not fit that pool.
    """
    if context_tokens is None:
        context_tokens = DEFAULT_CONTEXT_TOKENS

    # A GGUF's on-disk (download) size is a close proxy for its resident
    # memory footprint: weights are used close to as-is, with no separate
    # decompression step. It is not exact (loader overhead, mmap alignment),
    # but it is the same approximation every local-model memory estimator
    # makes, and this module does not have a better number to reach for.
    model_bytes = model["download_bytes"]
    kv_per_token = model["kv_bytes_per_token"]
    vram_bytes, vram_approximate = _gpu_vram_bytes(hw)
    # ram_bytes means one thing everywhere in this function's output: the
    # reserve-adjusted budget actually available for a spilled-over model,
    # never the raw hw["system_ram_bytes"] total. Computed once, reused in
    # every returned dict below, so the arithmetic always reconciles.
    ram_bytes = _ram_budget_bytes(hw)

    vram_fit = hearth_hw.fits(model_bytes, context_tokens, kv_per_token, vram_bytes)

    if vram_fit["fits"]:
        ratio = (vram_fit["headroom_bytes"] / vram_bytes) if vram_bytes > 0 else 0.0
        roomy = ratio >= GREAT_HEADROOM_RATIO
        if roomy and not vram_approximate:
            verdict = VERDICT_GREAT
            message = "Runs fully on the GPU with plenty of headroom."
        elif roomy and vram_approximate:
            # Would have been "great" on a precise reading, but a "great"
            # built on a guessed VRAM number is not really great - see the
            # module docstring and hearth_hw on why WMI/wmic AdapterRAM can
            # be badly wrong. Soften the verdict itself, not just the copy.
            verdict = VERDICT_GOOD
            message = (
                "Runs fully on the GPU with headroom to spare, but is graded "
                "good rather than great because the VRAM reading behind it "
                "is approximate."
            )
        else:
            verdict = VERDICT_GOOD
            message = "Runs fully on the GPU, but headroom is tight."
        if vram_approximate:
            message += _VRAM_APPROX_NOTE
        return {
            "verdict": verdict,
            "message": message,
            "requested_context_tokens": context_tokens,
            "max_context_tokens": context_tokens,
            "vram_bytes": vram_bytes,
            "vram_approximate": vram_approximate,
            "ram_bytes": ram_bytes,
            "required_bytes": vram_fit["required_bytes"],
            "headroom_bytes": vram_fit["headroom_bytes"],
        }

    # Does not fit at the requested context. Would a shorter context fit
    # entirely in VRAM? fits() is linear in context_tokens, so the largest
    # context that fits is a direct division, not a search.
    if vram_bytes > model_bytes and kv_per_token > 0:
        max_context_tokens = (vram_bytes - model_bytes) // kv_per_token
    else:
        max_context_tokens = 0  # weights alone do not fit in VRAM at all

    if max_context_tokens >= MIN_USEFUL_CONTEXT_TOKENS:
        reduced_fit = hearth_hw.fits(model_bytes, max_context_tokens, kv_per_token, vram_bytes)
        message = (
            f"Does not fit the GPU at {context_tokens} tokens of context, "
            f"but fits up to about {max_context_tokens} tokens."
        )
        if vram_approximate:
            message += _VRAM_APPROX_NOTE
        return {
            "verdict": VERDICT_REDUCED_CONTEXT,
            "message": message,
            "requested_context_tokens": context_tokens,
            "max_context_tokens": max_context_tokens,
            "vram_bytes": vram_bytes,
            "vram_approximate": vram_approximate,
            "ram_bytes": ram_bytes,
            "required_bytes": reduced_fit["required_bytes"],
            "headroom_bytes": reduced_fit["headroom_bytes"],
        }

    # Not usefully fittable in VRAM at all. Would it run on CPU / spilled
    # across CPU and GPU, i.e. does it fit in the system RAM budget?
    ram_fit = hearth_hw.fits(model_bytes, context_tokens, kv_per_token, ram_bytes)
    if ram_fit["fits"]:
        message = ("Does not fit the GPU; will run on CPU (or spill "
                    "partly onto it) and be noticeably slower.")
        if vram_approximate:
            message += _VRAM_APPROX_NOTE
        return {
            "verdict": VERDICT_CPU_SPILLOVER,
            "message": message,
            "requested_context_tokens": context_tokens,
            "max_context_tokens": None,
            "vram_bytes": vram_bytes,
            "vram_approximate": vram_approximate,
            "ram_bytes": ram_bytes,
            "required_bytes": ram_fit["required_bytes"],
            "headroom_bytes": ram_fit["headroom_bytes"],
        }

    message = "Does not fit in VRAM or system RAM on this machine."
    if vram_approximate:
        message += _VRAM_APPROX_NOTE
    return {
        "verdict": VERDICT_WONT_FIT,
        "message": message,
        "requested_context_tokens": context_tokens,
        "max_context_tokens": None,
        "vram_bytes": vram_bytes,
        "vram_approximate": vram_approximate,
        "ram_bytes": ram_bytes,
        "required_bytes": ram_fit["required_bytes"],
        "headroom_bytes": ram_fit["headroom_bytes"],
    }


def catalog_with_verdicts(hw=None, context_tokens=None, disk_path=None):
    """The full CATALOG, each entry merged with its verdict_for() result plus
    a disk-space check, sorted best-verdict-first (ties broken by the larger
    model, since among equally-runnable options bigger is the safer proxy for
    more capable - see recommend() for why that proxy is fine here but not
    everywhere).

    hw=None defers to hearth_hw.probe() (pure local detection, no network).
    """
    if hw is None:
        hw = hearth_hw.probe()
    free_disk = free_disk_bytes(disk_path)

    results = []
    for model in CATALOG:
        entry = dict(model)
        entry["verdict"] = verdict_for(model, hw, context_tokens=context_tokens)
        entry["free_disk_bytes"] = free_disk
        entry["disk_ok"] = free_disk >= (model["download_bytes"] + DISK_BUFFER_BYTES)
        results.append(entry)

    results.sort(key=lambda e: (VERDICT_RANK[e["verdict"]["verdict"]], -e["params_b"]))
    return results


def recommend(hw=None, context_tokens=None):
    """A short, ranked recommendation for this hardware: the best all-round
    coding model this machine can actually run, plus a lighter fallback.

    "Best" is chosen from models that fit fully in VRAM at the requested
    context (verdict great or good) - reduced_context, cpu_spillover, and
    wont_fit models are never the headline pick, only ever a fallback of
    last resort on very weak hardware. Among GPU-comfortable models, the
    largest by parameter count wins: params_b is a rough, defensible proxy
    for capability, not a quality benchmark (see the module docstring on why
    this deliberately does not predict throughput or quality). Ranked on
    what the hardware supports, not on raw size: a 32B model that only
    spills onto the CPU loses to a 14B model that runs great on the GPU.

    The fallback is the smallest coding model (other than the pick already
    chosen as best) that still runs comfortably (great, then good if none are
    great, and so on down the verdict tiers) - a genuinely lighter, faster
    option, not just "one size down".

    wont_fit models are never the headline pick, full stop - that is the
    catalog's own vocabulary ("Do not offer this model on this machine"),
    and recommending one anyway would be exactly the dishonesty this module
    exists to avoid. When nothing in the catalog earns a better verdict than
    wont_fit, this returns an empty list rather than headline something that
    cannot run: an honest "nothing fits" beats a confident wrong answer.

    Returns a list of 0-2 dicts (each a catalog_with_verdicts()-shaped
    entry): [best] or [best, fallback], in that order. Also empty if the
    catalog has no coding entries at all, which should never happen.
    """
    listed = catalog_with_verdicts(hw, context_tokens=context_tokens)
    coding = [e for e in listed if e["focus"] == "coding"]
    if not coding:
        return []

    runs_on_gpu = [e for e in coding if e["verdict"]["verdict"] in (VERDICT_GREAT, VERDICT_GOOD)]
    if runs_on_gpu:
        best = max(runs_on_gpu, key=lambda e: e["params_b"])
    else:
        # Nothing fits the GPU cleanly at the requested context. Prefer a
        # reduced_context model (still fully resident in VRAM, just at a
        # shorter context) over CPU spillover, and CPU spillover (smaller
        # model = less RAM pressure, still honestly labeled slow) over
        # nothing at all. This mirrors VERDICT_RANK's ordering.
        reduced = [e for e in coding if e["verdict"]["verdict"] == VERDICT_REDUCED_CONTEXT]
        spill = [e for e in coding if e["verdict"]["verdict"] == VERDICT_CPU_SPILLOVER]
        if reduced:
            best = min(reduced, key=lambda e: e["params_b"])
        elif spill:
            best = min(spill, key=lambda e: e["params_b"])
        else:
            # Every coding model in the catalog is wont_fit on this hardware.
            # wont_fit is never a headline pick (see docstring above): return
            # nothing rather than recommend a model that cannot run here.
            return []

    fallback = None
    for tier in (VERDICT_GREAT, VERDICT_GOOD, VERDICT_REDUCED_CONTEXT, VERDICT_CPU_SPILLOVER):
        candidates = [
            e for e in coding
            if e["id"] != best["id"] and e["verdict"]["verdict"] == tier
        ]
        if candidates:
            fallback = min(candidates, key=lambda e: e["params_b"])
            break

    return [best, fallback] if fallback else [best]


def _self_test():
    # -- free_disk_bytes: real filesystem, plausible values ------------------
    here_free = free_disk_bytes(os.path.dirname(os.path.abspath(__file__)))
    assert isinstance(here_free, int)
    assert here_free > 0, "the worktree's own volume must report positive free space"
    assert free_disk_bytes(None) >= 0

    # -- CATALOG shape: modest, defensible, weighted toward coding -----------
    assert isinstance(CATALOG, list)
    assert 6 <= len(CATALOG) <= 12, len(CATALOG)
    ids = [m["id"] for m in CATALOG]
    assert len(ids) == len(set(ids)), "duplicate catalog id"
    coding_count = 0
    for m in CATALOG:
        for key in ("id", "label", "params_b", "quantization", "download_bytes",
                    "description", "license", "kv_bytes_per_token", "focus",
                    "kv_confidence"):
            assert key in m, (m.get("id"), key)
        assert isinstance(m["download_bytes"], int) and m["download_bytes"] > 0, m
        assert isinstance(m["kv_bytes_per_token"], int) and m["kv_bytes_per_token"] > 0, m
        assert m["focus"] in ("coding", "general"), m
        assert isinstance(m["license"], str) and m["license"], m
        if m["focus"] == "coding":
            coding_count += 1
        # kv_calc, where present, must actually reproduce kv_bytes_per_token -
        # this is what keeps the numbers honest instead of hand-waved.
        if "kv_calc" in m:
            c = m["kv_calc"]
            computed = 2 * c["layers"] * c["kv_heads"] * c["head_dim"] * c["bytes_per_element"]
            assert computed == m["kv_bytes_per_token"], (m["id"], computed, m["kv_bytes_per_token"])
    assert coding_count >= len(CATALOG) // 2, "catalog must be weighted toward coding"

    # -- synthetic hardware, so the self-test passes on a machine with no ----
    # -- GPU and does not depend on whatever hardware happens to be present --
    def _hw(vram_bytes, ram_bytes, approximate=False):
        gpus = [{"name": "synthetic", "vram_bytes": vram_bytes, "vendor": "nvidia",
                 "approximate": approximate}] if vram_bytes else []
        return {"platform": "synthetic", "gpus": gpus, "system_ram_bytes": ram_bytes,
                "cpu_count": 8}

    hw_24gb = _hw(24 * 1024 ** 3, 64 * 1024 ** 3)
    hw_16gb = _hw(16 * 1024 ** 3, 32 * 1024 ** 3)
    hw_6gb_32ram = _hw(6_442_450_944, 32 * 1024 ** 3)
    hw_no_gpu_16ram = _hw(0, 16 * 1024 ** 3)

    # A synthetic 14GB coding model, deliberately not tied to any catalog
    # entry, so the arithmetic below is exact and easy to hand-check.
    syn_14gb = {
        "id": "synthetic/14b-test", "label": "Synthetic 14GB", "params_b": 14.0,
        "quantization": "Q4_K_M", "download_bytes": 15_032_385_536,
        "description": "test fixture", "license": "test", "focus": "coding",
        "kv_bytes_per_token": 200_000, "kv_confidence": "published_config",
    }

    # 24GB card: comfortable fit, roomy headroom -> great.
    v = verdict_for(syn_14gb, hw_24gb, context_tokens=8192)
    assert v["verdict"] == VERDICT_GREAT, v
    assert v["required_bytes"] == 15_032_385_536 + 200_000 * 8192, v
    assert v["headroom_bytes"] == 24 * 1024 ** 3 - v["required_bytes"], v

    # 16GB card: fits, but headroom is under 20% of VRAM -> good.
    v = verdict_for(syn_14gb, hw_16gb, context_tokens=8192)
    assert v["verdict"] == VERDICT_GOOD, v
    required = 15_032_385_536 + 200_000 * 8192
    assert v["required_bytes"] == required, v
    headroom = 16 * 1024 ** 3 - required
    assert v["headroom_bytes"] == headroom, v
    assert 0 <= headroom / (16 * 1024 ** 3) < GREAT_HEADROOM_RATIO, v

    # 6GB card (paired with generous 32GB system RAM): weights alone (14GB)
    # exceed the card, so it cannot run in VRAM at any context; it does fit
    # in the RAM budget -> cpu_spillover.
    v = verdict_for(syn_14gb, hw_6gb_32ram, context_tokens=8192)
    assert v["verdict"] == VERDICT_CPU_SPILLOVER, v
    assert v["max_context_tokens"] is None, v

    # No GPU at all, paired with a modest 16GB of RAM: the same 14GB model
    # plus its KV cache does not fit even the RAM budget -> wont_fit.
    v = verdict_for(syn_14gb, hw_no_gpu_16ram, context_tokens=8192)
    assert v["verdict"] == VERDICT_WONT_FIT, v
    assert v["vram_bytes"] == 0, v

    # A second synthetic model with a deliberately huge KV cost, to exercise
    # reduced_context on the 16GB card: weights fit VRAM alone, but the full
    # 8192-token KV cache does not; a shorter context does.
    syn_huge_kv = {
        "id": "synthetic/huge-kv-test", "label": "Synthetic huge-KV", "params_b": 10.0,
        "quantization": "Q4_K_M", "download_bytes": 10_737_418_240,
        "description": "test fixture", "license": "test", "focus": "coding",
        "kv_bytes_per_token": 1_000_000, "kv_confidence": "published_config",
    }
    v = verdict_for(syn_huge_kv, hw_16gb, context_tokens=8192)
    assert v["verdict"] == VERDICT_REDUCED_CONTEXT, v
    expected_max_ctx = (16 * 1024 ** 3 - 10_737_418_240) // 1_000_000
    assert v["max_context_tokens"] == expected_max_ctx, v
    assert v["max_context_tokens"] >= MIN_USEFUL_CONTEXT_TOKENS, v

    # A context so short even a tiny model can't clear MIN_USEFUL_CONTEXT_TOKENS
    # worth of headroom on a starved card should still degrade gracefully
    # rather than raise: a laughably small VRAM budget against a real model.
    v = verdict_for(CATALOG[0], _hw(200 * 1024 ** 2, 8 * 1024 ** 3), context_tokens=4096)
    assert v["verdict"] in (VERDICT_CPU_SPILLOVER, VERDICT_WONT_FIT), v

    # -- CRITICAL 1: the approximate VRAM flag must survive the seam --------
    # between hearth_hw and hearth_shop, not get thrown away by
    # _gpu_vram_bytes/verdict_for. Every verdict must say whether the VRAM
    # figure behind it is a guess.
    for hw_case, ctx in (
        (hw_24gb, 8192), (hw_16gb, 8192), (hw_6gb_32ram, 8192), (hw_no_gpu_16ram, 8192),
    ):
        v = verdict_for(syn_14gb, hw_case, context_tokens=ctx)
        assert "vram_approximate" in v, v
        assert v["vram_approximate"] is False, v  # these fixtures are all precise readings

    # Same 24GB card, same model, same context as the first "great" case
    # above - the only difference is the GPU entry is marked approximate
    # (as hearth_hw does for a WMI/CIM or wmic AdapterRAM reading). A
    # confident "great" must not survive that: it is downgraded to "good",
    # and the message must say why.
    hw_24gb_approx = _hw(24 * 1024 ** 3, 64 * 1024 ** 3, approximate=True)
    v_precise = verdict_for(syn_14gb, hw_24gb, context_tokens=8192)
    v_approx = verdict_for(syn_14gb, hw_24gb_approx, context_tokens=8192)
    assert v_precise["verdict"] == VERDICT_GREAT, v_precise
    assert v_approx["vram_approximate"] is True, v_approx
    assert v_approx["verdict"] == VERDICT_GOOD, (
        "an approximate VRAM reading must never earn a 'great' verdict", v_approx,
    )
    assert v_approx["verdict"] != v_precise["verdict"], (
        "approximate must actually change the outcome, not just be a silent field", v_approx,
    )
    assert "approximate" in v_approx["message"].lower(), v_approx
    # The arithmetic (required/headroom) must be identical either way - only
    # the confidence in the verdict changes, not the numbers themselves.
    assert v_approx["required_bytes"] == v_precise["required_bytes"], (v_approx, v_precise)
    assert v_approx["headroom_bytes"] == v_precise["headroom_bytes"], (v_approx, v_precise)

    # An approximate reading must also be flagged (and hedged) on the
    # non-GPU-fitting branches: reduced_context, cpu_spillover, wont_fit. The
    # WMI/wmic bug this guards against can underreport too (a wrapped signed
    # 32-bit AdapterRAM can even clamp to 0), so a "does not fit" verdict is
    # just as capable of being wrong as a "great" one.
    hw_6gb_32ram_approx = _hw(6_442_450_944, 32 * 1024 ** 3, approximate=True)
    v = verdict_for(syn_14gb, hw_6gb_32ram_approx, context_tokens=8192)
    assert v["verdict"] == VERDICT_CPU_SPILLOVER, v
    assert v["vram_approximate"] is True, v
    assert "approximate" in v["message"].lower(), v

    hw_no_gpu_16ram_approx = _hw(0, 16 * 1024 ** 3, approximate=True)
    # No GPU detected at all is an exact reading of "nothing" (approximate
    # only applies to a GPU that WAS detected via a shaky path), so this
    # must stay False even with approximate=True requested in the fixture -
    # there is no GPU dict for the flag to live on.
    v = verdict_for(syn_14gb, hw_no_gpu_16ram_approx, context_tokens=8192)
    assert v["vram_approximate"] is False, v

    # -- IMPORTANT 3: ram_bytes must mean the reserve-adjusted budget, in ---
    # every branch, so the displayed numbers reconcile: ram_bytes minus
    # required_bytes must equal headroom_bytes whenever the verdict was
    # decided against the RAM pool (cpu_spillover, wont_fit).
    v = verdict_for(syn_14gb, hw_6gb_32ram, context_tokens=8192)
    assert v["verdict"] == VERDICT_CPU_SPILLOVER, v
    assert v["ram_bytes"] == _ram_budget_bytes(hw_6gb_32ram), v
    assert v["ram_bytes"] != hw_6gb_32ram["system_ram_bytes"], (
        "ram_bytes must be the reserve-adjusted budget, not the raw total", v,
    )
    assert v["ram_bytes"] - v["required_bytes"] == v["headroom_bytes"], v

    v = verdict_for(syn_14gb, hw_no_gpu_16ram, context_tokens=8192)
    assert v["verdict"] == VERDICT_WONT_FIT, v
    assert v["ram_bytes"] == _ram_budget_bytes(hw_no_gpu_16ram), v
    assert v["ram_bytes"] - v["required_bytes"] == v["headroom_bytes"], v

    # -- catalog_with_verdicts: shape, sorting, disk check --------------------
    listed = catalog_with_verdicts(hw_24gb, context_tokens=8192)
    assert len(listed) == len(CATALOG)
    for e in listed:
        assert "verdict" in e and "disk_ok" in e and "free_disk_bytes" in e, e
    ranks = [VERDICT_RANK[e["verdict"]["verdict"]] for e in listed]
    assert ranks == sorted(ranks), "catalog_with_verdicts must be sorted best-verdict-first"

    # A near-zero free-disk figure must flip disk_ok to False for every model.
    old_free = globals()["free_disk_bytes"]
    try:
        globals()["free_disk_bytes"] = lambda path=None: 1024
        starved = catalog_with_verdicts(hw_24gb, context_tokens=8192)
        assert all(not e["disk_ok"] for e in starved), starved
    finally:
        globals()["free_disk_bytes"] = old_free

    # -- recommend(): the two real calibration machines produce sensibly ------
    # -- different answers. RTX 5080 (this Windows box) vs. RTX 2060 (the ----
    # -- project's Linux host) is the whole point of the fit calculation. ----
    hw_5080 = _hw(17_094_934_528, 33 * 1024 ** 3)
    hw_2060 = _hw(6_442_450_944, 16 * 1024 ** 3)

    rec_5080 = recommend(hw_5080, context_tokens=8192)
    rec_2060 = recommend(hw_2060, context_tokens=8192)

    assert 1 <= len(rec_5080) <= 2, rec_5080
    assert 1 <= len(rec_2060) <= 2, rec_2060
    best_5080, best_2060 = rec_5080[0], rec_2060[0]
    assert best_5080["focus"] == "coding" and best_2060["focus"] == "coding"
    # The stronger card must not be steered to a smaller "best" pick than the
    # weaker one - that would be exactly backwards for this product.
    assert best_5080["params_b"] >= best_2060["params_b"], (best_5080["id"], best_2060["id"])
    # A real, well-known data point: qwen2.5-coder 7B is the right call on a
    # 6GB card, not the tiniest model available, even though 1.5B is "great"
    # and 7B is only "good" - all-round capability beats a slightly cooler
    # verdict tier once both actually run.
    assert best_2060["id"] == "qwen2.5-coder:7b-instruct-q4_K_M", best_2060["id"]
    if len(rec_5080) == 2:
        assert rec_5080[1]["params_b"] <= best_5080["params_b"], rec_5080
        assert rec_5080[1]["id"] != best_5080["id"]
    if len(rec_2060) == 2:
        assert rec_2060[1]["params_b"] <= best_2060["params_b"], rec_2060
        assert rec_2060[1]["id"] != best_2060["id"]

    # A machine with no GPU and little RAM must still return something usable
    # rather than an empty list or a crash - the shop should never go silent.
    rec_weak = recommend(_hw(0, 8 * 1024 ** 3), context_tokens=8192)
    assert len(rec_weak) >= 1, rec_weak
    assert rec_weak[0]["verdict"]["verdict"] != VERDICT_GREAT, rec_weak  # no GPU, can't be "great"

    # -- CRITICAL 2: recommend() must never headline a wont_fit model -------
    # Hardware weak enough that literally nothing in the catalog fits,
    # unlike hw_weak above (which still has a cpu_spillover candidate - the
    # exact fixture gap that let this bug hide from the shipped self-test
    # originally). No GPU, and RAM below the fixed RAM_RESERVE_MIN_BYTES
    # floor, so the spillover budget clamps to exactly 0.
    hw_nothing_fits = _hw(0, 512 * 1024 ** 2)
    assert _ram_budget_bytes(hw_nothing_fits) == 0, "fixture must zero out the RAM budget"

    # Verify the premise, not just the conclusion: every coding entry in the
    # catalog really is wont_fit on this hardware, so an empty recommendation
    # is the honest answer here, not an artifact of a lucky assertion.
    listed_nothing = catalog_with_verdicts(hw_nothing_fits, context_tokens=8192)
    coding_nothing = [e for e in listed_nothing if e["focus"] == "coding"]
    assert coding_nothing, "catalog must have coding entries"
    assert all(e["verdict"]["verdict"] == VERDICT_WONT_FIT for e in coding_nothing), coding_nothing

    rec_nothing = recommend(hw_nothing_fits, context_tokens=8192)
    assert rec_nothing == [], (
        "recommend() must never headline a wont_fit model; when nothing in "
        "the catalog can run, an empty list is the honest answer", rec_nothing,
    )

    print("hearth-shop self-test OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv or len(sys.argv) == 1:
        sys.exit(_self_test())
