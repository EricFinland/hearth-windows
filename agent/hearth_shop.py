#!/usr/bin/env python3
r"""hearth model shop: live Hugging Face model listings, with an honest verdict
on whether each quantisation will actually run well on the machine in front
of the user.

Most model listings show a parameter count and a download size and leave the
user to guess. That is a bad trade for a desktop app whose whole pitch is "no
command line, no config files, just click and run." This module is the data
and logic layer behind the shop: it asks hearth_hf for real Hugging Face
search results and for the real GGUF files inside a repository, and it turns
"6GB of VRAM" and "a 7B model at Q4_K_M and 8k of context" into a verdict a
non-technical user can act on.

THE KV CACHE IS STILL THE PART PEOPLE GET WRONG. See hearth_hw for the full
argument: model weights are not the whole story, and the exact same model at
a longer context can need meaningfully more memory once its KV cache is
counted in. Nothing here compares a file size against a VRAM size; every
verdict goes through hearth_hw.fits(), which adds context_tokens *
kv_bytes_per_token to the weights before deciding.

## Per quantisation, not per repository

A Hub repository does not hold "a model". It holds five to fifteen
quantisations of one set of weights, from an IQ1 that fits almost anywhere
and answers badly to an F16 that needs a datacentre, differing in size by
more than an order of magnitude. A single per-repository verdict would
therefore be meaningless. Every fit verdict here is computed per
quantisation, against that quantisation's real file size as the Hub reports
it (summed across parts for a split model), and the shop's per-repository
answer is a *choice among* those quants: the largest one in the best verdict
tier the repository offers.

Best tier first and size second, in that order, because a quantisation that
runs with room to spare (great) beats a larger one that only just fits
(good), and "only just fits" is precisely what a longer prompt or a second
loaded model breaks. Within one tier the larger file is the more faithful
one and wins. When nothing fits in VRAM at all the size preference inverts
to the smallest, which is the one that spills least and gives back the most
context. See rank_quants() and best_quant().

One row per quantisation, not per file. Qwen's official GGUF repositories
publish most quantisations twice, once as a single file and once split into
numbered parts a couple of hundred bytes apart in total, and listing both
puts two visually identical rows in front of the user for nearly every
quantisation in the repository. The editions of one quantisation are
collapsed into the row _edition_sort_key prefers - the single file, at an
equal verdict - and the other edition rides on that row as
alternate_editions, with split_part_count saying how many pieces the split
arrives in, because "this download is four files" and "there is a version of
this that fits my filesystem's file size limit" are real questions. That
preference lives in exactly one place, and the listing (quant_verdicts) and
the offer (best_quant) both reach it through _edition_groups, so they cannot
disagree about which edition survives.

## Where kv_bytes_per_token comes from now

The Hub's search and tree APIs report sizes, filenames, and hashes. They do
not report layer counts, KV head counts, or head dimensions, which is what
the KV arithmetic actually needs. Three sources are used, in this order, and
every entry reports which one it used in kv_source/kv_confidence:

  1. KV_FAMILIES, a table of known model families keyed by name and
     parameter count. Each entry carries the same explicit
     layers/kv_heads/head_dim arithmetic the CATALOG entries carry, and each
     is labelled with how confident that architecture reading is.
  2. _kv_heuristic_bytes(), a size heuristic, for an unrecognised family. It
     assumes a dense transformer with hidden_size = 128 * layers and PLAIN
     MULTI-HEAD ATTENTION, then multiplies by a safety factor. Most models
     released since roughly 2023 use grouped-query attention, which needs
     several times less KV cache than that, so this deliberately over-counts.
     Over-counting yields a pessimistic verdict; under-counting yields a
     confident wrong "it fits", so the pessimistic direction is the correct
     one to be wrong in. The self-test pins the claim that this heuristic
     never under-counts any multi-head-attention entry in KV_FAMILIES.
  3. UNKNOWN_KV_BYTES_PER_TOKEN, one fixed conservative figure, when neither
     a family nor a parameter count could be determined at all.
     kv_confidence is "unknown" for those and a UI should say so rather than
     present the verdict flatly.

Parameter counts are read out of the repository or file name where one is
present ("...-7B-..."), and otherwise estimated from the file size and the
quantisation's bits per weight (QUANT_BITS_PER_WEIGHT). Both are estimates,
and params_source says which was used.

## When the Hub is unreachable

search_shop() returns ok False with a machine-readable error_kind, source
"fallback", and the built-in reference catalog (CATALOG) graded against the
same hardware, with every entry marked source "fallback" and downloadable
False. ok is True ONLY for live Hub results, so a caller that ignores
"source" entirely still cannot mistake the fallback for live data. The
fallback exists so that a first run with no network can still answer "what
could this machine run"; it deliberately does not pretend to answer "what
should I download", because nothing can be downloaded without a network.

CATALOG is also still the tier definition hearth_router buckets against and
the reference list hearth_setup matches installed models against, so it is
load-bearing beyond the offline fallback.

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

Two more facts ride alongside every verdict, deliberately not folded into
the tier itself since they are about how much to trust the reading the
tier was computed from, not about the tier's own meaning:
  gpu_detected      - False when hearth_hw found no GPU at all. "No GPU was
                   detected" and "your GPU is too small for this model" are
                   different statements, and a wont_fit or cpu_spillover
                   verdict must say which one is true rather than let the
                   user infer "nothing runs here" from an absent GPU that
                   was never even looked for successfully.
  shared_memory_likely - True when the GPU behind this verdict reports as
                   Intel or vendor-unknown (see SHARED_MEMORY_LIKELY_VENDORS):
                   hearth_hw could not confirm this is a discrete card with
                   real dedicated VRAM, as opposed to a shared-memory figure
                   carved out of system RAM. Shared memory does not perform
                   like VRAM, so a verdict resting on one is graded
                   conservatively - a would-be great is downgraded to good -
                   exactly like an approximate VRAM reading already is.

Disk space is a separate, independent check (free_disk_bytes / disk_ok):
a user with 8GB free cannot install a 17GB model, and finding that out at 90%
through a download is a terrible experience.

Deliberately out of scope, matching hearth_hw's stance: no downloading (that
is hearth_hf.download's job) and no tokens-per-second prediction. Throughput
prediction from bandwidth and parameter count breaks badly on
mixture-of-experts models; Hearth measures real throughput on the user's
machine instead of guessing at it.

Standard library only. This module writes no files. Two halves, split by
whether they touch the network:

  Offline, no network under any circumstances: verdict_for,
  catalog_with_verdicts, recommend, rank_quants, best_quant, quant_verdicts,
  kv_for_model, parse_params_b, free_disk_bytes (a local disk-space call).

  Networked, through hearth_hf, and never raising on a network failure:
  search_shop, repo_quants, recommend_live.

hw=None on the public functions is a convenience that defers to
hearth_hw.probe() (pure local detection, no network); pass an explicit hw
dict to stay hermetic.
"""

import math
import os
import re
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

# Where a shop entry's facts came from. SOURCE_LIVE means the Hugging Face
# Hub answered and this entry describes a repository that really exists with
# the file sizes the Hub reported. SOURCE_FALLBACK means the Hub could not be
# reached and this entry came from the built-in reference CATALOG below: it
# is a statement about what this hardware could run, not an offer to download
# anything.
SOURCE_LIVE = "live"
SOURCE_FALLBACK = "fallback"

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

# GPU vendors whose reported vram_bytes this module trusts as real, dedicated
# VRAM. Intel's shipping lineup is overwhelmingly integrated graphics that
# shares system RAM (Intel does sell discrete Arc cards, but hearth_hw has no
# signal beyond vendor name to tell the two apart), and vendor "unknown"
# means hearth_hw could not even confirm the card is one of these two. Both
# get graded conservatively rather than trusted at face value: shared system
# memory does not perform like dedicated VRAM, so a "great" built on a
# shared-memory figure would be exactly the kind of dishonest verdict this
# module exists to avoid. AMD and NVIDIA discrete desktop/laptop cards are
# the trusted common case; AMD APUs exist too and are a known gap here, the
# same kind of judgment call this module already documents for kv_confidence.
SHARED_MEMORY_LIKELY_VENDORS = (hearth_hw.VENDOR_INTEL, hearth_hw.VENDOR_UNKNOWN)


# -- built-in reference catalog ------------------------------------------------
#
# Not the shop's listing any more: search_shop() sources that from Hugging
# Face. This list has three remaining jobs, all of them real:
#   1. the offline fallback, when the Hub cannot be reached (search_shop
#      returns it with ok False and source "fallback", never as live data);
#   2. the tier definition hearth_router buckets coding models into;
#   3. the reference list hearth_setup matches already-installed models
#      against so it can grade them.
#
# Roughly six to twelve entries, weighted toward coding since Hearth Code is
# the flagship surface. download_bytes is an approximate figure matching the
# model's typical library listing for that quantisation; the exact blob size
# drifts a little between quant builds and is not load-bearing here - what
# matters for the fit calculation is kv_bytes_per_token, which is derived
# from architecture, not guessed from parameter count.
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
    try:
        target = path or os.getcwd()
        return shutil.disk_usage(target).free
    except OSError:
        # os.getcwd() itself raises OSError (FileNotFoundError on POSIX,
        # a WinError on Windows) if the process's working directory has
        # been deleted out from under it - that used to happen outside
        # this try block, so it escaped this function's own "never
        # raises" promise. Both failure points now share one net.
        return 0


def _gpu_vram_bytes(hw):
    """Everything verdict_for needs to know about the single GPU a model
    would be loaded onto, from a hearth_hw-shaped dict, as a dict with keys
    vram_bytes, approximate, vendor, shared_memory_likely, gpu_detected.

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
    honest.

    vendor is the detected GPU's vendor string (see hearth_hw's VENDOR_*
    constants), or None when no GPU was detected. shared_memory_likely is
    True when that vendor is in SHARED_MEMORY_LIKELY_VENDORS: an Intel or
    unknown-vendor card's reported "VRAM" may actually be shared system
    memory, which does not perform like dedicated VRAM.

    gpu_detected is False only when hearth_hw's gpu list was empty, i.e. no
    GPU was found at all - a fact distinct from "a GPU was found and it is
    too small", which is what vram_bytes == 0 alone cannot tell a caller.
    When gpu_detected is False, vram_bytes is 0, approximate is False (an
    empty list is an exact reading of "nothing", not a guess), vendor is
    None, and shared_memory_likely is False (there is no GPU for the flag
    to describe).
    """
    gpus = hw.get("gpus") or []
    if not gpus:
        return {
            "vram_bytes": 0,
            "approximate": False,
            "vendor": None,
            "shared_memory_likely": False,
            "gpu_detected": False,
        }
    best = max(gpus, key=lambda g: g.get("vram_bytes", 0))
    vendor = best.get("vendor")
    return {
        "vram_bytes": best.get("vram_bytes", 0),
        "approximate": bool(best.get("approximate", False)),
        "vendor": vendor,
        "shared_memory_likely": vendor in SHARED_MEMORY_LIKELY_VENDORS,
        "gpu_detected": True,
    }


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

# Appended to a verdict's message whenever the GPU behind it is Intel or
# vendor-unknown (see SHARED_MEMORY_LIKELY_VENDORS / _gpu_vram_bytes), so a
# reading that may actually be shared system memory is never presented with
# the same confidence as a confirmed discrete card.
_SHARED_MEMORY_NOTE = (
    " This GPU's memory could not be confirmed as dedicated VRAM (it may be "
    "shared system memory reported by an integrated or unrecognized GPU), "
    "which does not perform like real VRAM, so this verdict is graded "
    "conservatively."
)


def _gpu_clause(gpu_detected, platform_name):
    """The opening clause for a does-not-fit-VRAM message: distinguishes
    "no GPU was ever found" from "a GPU was found and it does not fit",
    which are different facts and must read as different sentences (see
    the gpu_detected note in the module docstring).
    """
    if gpu_detected:
        return "Does not fit the GPU"
    if platform_name:
        return "No GPU was detected on this {} machine".format(platform_name)
    return "No GPU was detected on this machine"


def verdict_for(model, hw, context_tokens=None):
    """The shop's core judgment call: will `model` actually run well on `hw`?

    `model` needs exactly two keys: its resident size, as either
    "download_bytes" (what a CATALOG entry calls it) or "size_bytes" (what a
    hearth_hf file entry calls it), and "kv_bytes_per_token". Anything else
    on the dict is ignored, which is what lets one function grade a CATALOG
    entry, a live Hub quantisation, and a synthetic test fixture identically.
    Raises ValueError when neither size key is an integer, rather than
    computing a verdict from a None.

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
      gpu_detected          - False when hearth_hw found no GPU at all, as
                              opposed to finding one that is simply too
                              small. See the module docstring's verdict
                              vocabulary section for why this must not be
                              collapsed into wont_fit/cpu_spillover's normal
                              "does not fit" phrasing.
      gpu_vendor            - the detected GPU's vendor string, or None when
                              gpu_detected is False.
      shared_memory_likely  - True when gpu_vendor is Intel or unknown (see
                              SHARED_MEMORY_LIKELY_VENDORS): the vram_bytes
                              reading may be shared system memory rather
                              than dedicated VRAM. Like vram_approximate,
                              this downgrades a would-be "great" to "good"
                              and hedges the message.
      platform              - hw["platform"] (e.g. "Windows", "Linux"),
                              passed through so a caller can render an
                              accurate "no GPU was detected on this X
                              machine" message without re-deriving it.
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
    model_bytes = model.get("download_bytes")
    if model_bytes is None:
        # hearth_hf's file entries call the same quantity "size_bytes".
        # Accepting both means a Hub file entry can be graded directly,
        # without a caller having to rename a key to talk to this function.
        model_bytes = model.get("size_bytes")
    if not isinstance(model_bytes, int):
        raise ValueError(
            "verdict_for needs an integer download_bytes (or size_bytes); got {!r}".format(
                model_bytes,
            )
        )
    kv_per_token = model["kv_bytes_per_token"]
    gpu_info = _gpu_vram_bytes(hw)
    vram_bytes = gpu_info["vram_bytes"]
    vram_approximate = gpu_info["approximate"]
    gpu_vendor = gpu_info["vendor"]
    shared_memory_likely = gpu_info["shared_memory_likely"]
    gpu_detected = gpu_info["gpu_detected"]
    platform_name = hw.get("platform")
    # A "great" or "wont_fit" is only as honest as the VRAM figure it rests
    # on. Both an approximate reading (WMI/wmic AdapterRAM) and a
    # shared-memory-likely reading (Intel/unknown vendor) are reasons not to
    # trust that figure fully, so both gate the same downgrade below.
    vram_trustworthy = not vram_approximate and not shared_memory_likely
    # ram_bytes means one thing everywhere in this function's output: the
    # reserve-adjusted budget actually available for a spilled-over model,
    # never the raw hw["system_ram_bytes"] total. Computed once, reused in
    # every returned dict below, so the arithmetic always reconciles.
    ram_bytes = _ram_budget_bytes(hw)

    common_fields = {
        "vram_bytes": vram_bytes,
        "vram_approximate": vram_approximate,
        "gpu_detected": gpu_detected,
        "gpu_vendor": gpu_vendor,
        "shared_memory_likely": shared_memory_likely,
        "platform": platform_name,
        "ram_bytes": ram_bytes,
    }

    vram_fit = hearth_hw.fits(model_bytes, context_tokens, kv_per_token, vram_bytes)

    if vram_fit["fits"]:
        ratio = (vram_fit["headroom_bytes"] / vram_bytes) if vram_bytes > 0 else 0.0
        roomy = ratio >= GREAT_HEADROOM_RATIO
        if roomy and vram_trustworthy:
            verdict = VERDICT_GREAT
            message = "Runs fully on the GPU with plenty of headroom."
        elif roomy:
            # Would have been "great" on a fully trustworthy reading, but a
            # "great" built on a guessed or possibly-shared-memory number is
            # not really great - see the module docstring and hearth_hw on
            # why WMI/wmic AdapterRAM can be badly wrong, and why an Intel or
            # unknown-vendor card's VRAM figure may just be system RAM.
            # Soften the verdict itself, not just the copy.
            reasons = []
            if vram_approximate:
                reasons.append("the VRAM reading behind it is approximate")
            if shared_memory_likely:
                reasons.append(
                    "this GPU's memory could not be confirmed as dedicated VRAM"
                )
            verdict = VERDICT_GOOD
            message = (
                "Runs fully on the GPU with headroom to spare, but is graded "
                "good rather than great because " + " and ".join(reasons) + "."
            )
        else:
            verdict = VERDICT_GOOD
            message = "Runs fully on the GPU, but headroom is tight."
        if vram_approximate:
            message += _VRAM_APPROX_NOTE
        if shared_memory_likely:
            message += _SHARED_MEMORY_NOTE
        return {
            **common_fields,
            "verdict": verdict,
            "message": message,
            "requested_context_tokens": context_tokens,
            "max_context_tokens": context_tokens,
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
        if shared_memory_likely:
            message += _SHARED_MEMORY_NOTE
        return {
            **common_fields,
            "verdict": VERDICT_REDUCED_CONTEXT,
            "message": message,
            "requested_context_tokens": context_tokens,
            "max_context_tokens": max_context_tokens,
            "required_bytes": reduced_fit["required_bytes"],
            "headroom_bytes": reduced_fit["headroom_bytes"],
        }

    # Not usefully fittable in VRAM at all. Would it run on CPU / spilled
    # across CPU and GPU, i.e. does it fit in the system RAM budget?
    ram_fit = hearth_hw.fits(model_bytes, context_tokens, kv_per_token, ram_bytes)
    if ram_fit["fits"]:
        message = ("{}; will run on CPU (or spill partly onto it) and be "
                    "noticeably slower.").format(_gpu_clause(gpu_detected, platform_name))
        if vram_approximate:
            message += _VRAM_APPROX_NOTE
        if shared_memory_likely:
            message += _SHARED_MEMORY_NOTE
        return {
            **common_fields,
            "verdict": VERDICT_CPU_SPILLOVER,
            "message": message,
            "requested_context_tokens": context_tokens,
            "max_context_tokens": None,
            "required_bytes": ram_fit["required_bytes"],
            "headroom_bytes": ram_fit["headroom_bytes"],
        }

    if gpu_detected:
        message = "Does not fit in VRAM or system RAM on this machine."
    else:
        message = "{}, and the model does not fit in system RAM either.".format(
            _gpu_clause(gpu_detected, platform_name)
        )
    if vram_approximate:
        message += _VRAM_APPROX_NOTE
    if shared_memory_likely:
        message += _SHARED_MEMORY_NOTE
    return {
        **common_fields,
        "verdict": VERDICT_WONT_FIT,
        "message": message,
        "requested_context_tokens": context_tokens,
        "max_context_tokens": None,
        "required_bytes": ram_fit["required_bytes"],
        "headroom_bytes": ram_fit["headroom_bytes"],
    }


def catalog_with_verdicts(hw=None, context_tokens=None, disk_path=None):
    """The full built-in reference CATALOG, each entry merged with its
    verdict_for() result plus a disk-space check, sorted best-verdict-first
    (ties broken by the larger model, since among equally-runnable options
    bigger is the safer proxy for more capable - see recommend() for why that
    proxy is fine here but not everywhere).

    This is the OFFLINE listing. It makes no network calls and it is not the
    Hugging Face shop: search_shop() is. Every entry carries source
    SOURCE_FALLBACK to say so, since these entries name models rather than
    downloadable Hub files.

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
        entry["source"] = SOURCE_FALLBACK
        results.append(entry)

    results.sort(key=lambda e: (VERDICT_RANK[e["verdict"]["verdict"]], -e["params_b"]))
    return results


def recommend(hw=None, context_tokens=None, listing=None):
    """A short, ranked recommendation for this hardware: the best all-round
    coding model this machine can actually run, plus a lighter fallback.

    Two modes, decided by `listing`:

      listing=None (the default, and what every existing caller passes)
        Ranks the built-in reference CATALOG, offline, exactly as this
        function always has, and returns catalog_with_verdicts()-shaped
        entries. No network, no quantisation choice: a CATALOG entry has
        exactly one size.

      listing=<a search_shop() result>
        Ranks the live Hugging Face repositories in that listing instead.
        Each returned entry is a search_shop() model entry whose best_quant
        is the quantisation being recommended, with an added "reasoning"
        string explaining the choice of repository AND of quantisation.
        recommend_live() is the convenience wrapper that fetches a listing
        and calls this.

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
    if listing is not None:
        return _recommend_from_listing(listing)

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


# ---------------------------------------------------------------------------
# Live sourcing from Hugging Face
# ---------------------------------------------------------------------------

# How many repositories a search asks the Hub for, and how many of those get
# their file listing fetched. Each detailed repository costs one extra HTTP
# round trip (hearth_hf.list_gguf_files), so detailing all twelve would make
# the shop's first screen wait on twelve requests. Repositories past
# detail_limit come back with quants_loaded False and an empty quants list;
# repo_quants() fills one in on demand, which is what a UI should call when
# the user opens a row.
DEFAULT_SEARCH_LIMIT = 12
DEFAULT_DETAIL_LIMIT = 5

# What recommend_live() searches for when the caller names nothing. Mirrors
# recommend()'s existing coding-first stance (Hearth Code is the flagship
# surface). It is a plain Hub search string, not a curated repository list.
DEFAULT_RECOMMEND_QUERY = "coder"

# Returned in a result's error_kind when hearth_hf itself could not be
# imported. Every other error_kind these functions return is one of
# hearth_hf's own ERR_* strings, passed through unchanged, so a UI has a
# single vocabulary to branch on.
ERR_HF_UNAVAILABLE = "hf_unavailable"


def _hf():
    """The hearth_hf module, imported at call time rather than at import time.

    hearth_hf imports this module (it uses free_disk_bytes), so a
    module-level import here would be an import cycle whose resolution
    depends on which of the two a process happened to import first.
    Deferring to call time removes that ordering hazard entirely, and keeps
    every offline function in this module usable on an installation where
    hearth_hf is missing or broken.
    """
    import hearth_hf  # noqa: E402 - deferred deliberately, see above
    return hearth_hf


def _gb(n):
    """A short human-readable size for a message, or a phrase saying the
    size is unknown. Never invents a number for None.
    """
    if not isinstance(n, int) or n <= 0:
        return "an unknown size"
    return "{:.1f} GB".format(n / float(1024 ** 3))


# -- parameter counts ---------------------------------------------------------

# "8x7B": a mixture-of-experts count, matched first and read as the PRODUCT.
# Mixtral-8x7B is 56B of weights sitting in memory even though only about 13B
# are active per token, and memory is what this module decides about.
_PARAMS_MOE_RE = re.compile(r"(?<![0-9a-z])(\d{1,2})\s*x\s*(\d+(?:\.\d+)?)\s*b(?![0-9a-z])")

# "7B", "1.5b", "0.5B": a number followed by a b that is not part of a longer
# word. The lookbehind is what keeps the "3b" of Qwen3-30B-A3B (that model's
# ACTIVE parameter count, not its size) and the "7b" inside "8x7b" from being
# read as the whole model's size.
_PARAMS_RE = re.compile(r"(?<![0-9a-z.])(\d+(?:\.\d+)?)\s*b(?![0-9a-z])")

# Outside this range it is a coincidence in a filename, not a parameter count.
_PARAMS_MIN_B = 0.01
_PARAMS_MAX_B = 2000.0

# Approximate bits per weight for each GGUF quantisation, used ONLY to
# estimate a parameter count from a file size when the name does not state
# one. These are the commonly published llama.cpp figures for the mixed K and
# I quants (a "4-bit" K quant is not 4.0 bits per weight: it keeps some
# tensors at higher precision), rounded to two decimals. They are never used
# to predict quality, and a few percent of error here only shifts a parameter
# estimate that is already labelled an estimate.
QUANT_BITS_PER_WEIGHT = {
    "F32": 32.0, "FP32": 32.0,
    "F16": 16.0, "FP16": 16.0, "BF16": 16.0,
    "Q8_0": 8.5,
    "Q6_K": 6.56,
    "Q5_1": 6.0, "Q5_0": 5.5, "Q5_K": 5.67, "Q5_K_M": 5.67, "Q5_K_S": 5.52,
    "Q4_1": 5.0, "Q4_0": 4.5, "Q4_K": 4.83, "Q4_K_M": 4.83, "Q4_K_S": 4.58,
    "Q3_K": 3.91, "Q3_K_L": 4.27, "Q3_K_M": 3.91, "Q3_K_S": 3.50,
    "Q2_K": 3.35, "Q2_K_S": 2.96,
    "IQ4_NL": 4.50, "IQ4_XS": 4.25,
    "IQ3_M": 3.66, "IQ3_S": 3.44, "IQ3_XS": 3.30, "IQ3_XXS": 3.06,
    "IQ2_M": 2.70, "IQ2_S": 2.50, "IQ2_XS": 2.31, "IQ2_XXS": 2.06,
    "IQ1_M": 1.75, "IQ1_S": 1.56,
    "TQ2_0": 2.06, "TQ1_0": 1.69,
    "MXFP4": 4.25, "MXFP4_MOE": 4.25, "NVFP4": 4.25,
}


def _normalize_name(name):
    """A model name lowered, with underscores turned into hyphens, so one set
    of patterns matches "Qwen2.5_Coder_7B" and "qwen2.5-coder-7b" alike.
    """
    return str(name or "").lower().replace("_", "-")


def parse_params_b(name):
    """The parameter count in billions stated by a repository or file name,
    or None when the name states none.

    "Qwen2.5-Coder-7B-Instruct-GGUF" gives 7.0, "Llama-3.2-1B" gives 1.0,
    "Mixtral-8x7B" gives 56.0 (see _PARAMS_MOE_RE for why the product rather
    than the expert size). When a name states two counts it is stating a
    total and an active or per-expert count, and the larger is the one that
    has to fit in memory, so the largest plausible match wins.

    A name is a naming convention, not a config file. This is a parse of the
    convention: callers label the result params_source "name" so nothing
    downstream mistakes it for a reading of the model's real architecture.
    Returns None rather than guessing when nothing matches, which is the
    signal to fall back to params_from_size().
    """
    text = _normalize_name(name)
    candidates = []
    for experts, per_expert in _PARAMS_MOE_RE.findall(text):
        try:
            candidates.append(float(experts) * float(per_expert))
        except ValueError:
            continue
    for raw in _PARAMS_RE.findall(text):
        try:
            candidates.append(float(raw))
        except ValueError:
            continue
    plausible = [c for c in candidates if _PARAMS_MIN_B <= c <= _PARAMS_MAX_B]
    return max(plausible) if plausible else None


def params_from_size(size_bytes, quant):
    """Estimate a parameter count in billions from a GGUF's size and its
    quantisation label, or None when the label is not in
    QUANT_BITS_PER_WEIGHT or the size is unusable.

    The second-choice source for a parameter count, used when the name states
    none. Callers label it params_source "file_size". It is an estimate on
    top of an approximate table, and it exists only to feed the conservative
    KV heuristic.
    """
    if not isinstance(size_bytes, int) or size_bytes <= 0:
        return None
    bits = QUANT_BITS_PER_WEIGHT.get(str(quant or "").upper())
    if not bits:
        return None
    params = (size_bytes * 8.0) / bits / 1e9
    if not (_PARAMS_MIN_B <= params <= _PARAMS_MAX_B):
        return None
    return params


# -- KV cache cost per token ---------------------------------------------------

_ATTENTION_MHA = "mha"   # kv_heads == attention heads, the pre-2023 default
_ATTENTION_GQA = "gqa"   # kv_heads < attention heads, far cheaper to cache


def _kv_entry(params_b, layers, kv_heads, head_dim, attention, confidence,
              bytes_per_element=2):
    """One KV_FAMILIES size entry, with the arithmetic kept explicit rather
    than pre-multiplied, so the self-test can prove the number matches its
    own stated derivation exactly as it already does for CATALOG.
    """
    return {
        "params_b": params_b,
        "kv_bytes_per_token": 2 * layers * kv_heads * head_dim * bytes_per_element,
        "kv_confidence": confidence,
        "kv_calc": {
            "layers": layers,
            "kv_heads": kv_heads,
            "head_dim": head_dim,
            "bytes_per_element": bytes_per_element,
            "attention": attention,
        },
    }


# Known model families, matched by name and then by nearest declared
# parameter count. Deliberately short: every entry is a claim about a model's
# architecture, and a claim that UNDER-counts KV cost produces a confident
# "it fits" that is wrong on the user's machine. A family that is not listed
# falls through to _kv_heuristic_bytes, which over-counts and so fails safe.
# kv_confidence uses the same three words CATALOG uses and introduces none:
# published_config, recalled_estimate, conservative_overestimate.
#
# bytes_per_element is 2 throughout because llama.cpp's default KV cache type
# is f16. A user who configures a quantised KV cache uses less than this,
# which is again the safe direction to be wrong in.
KV_FAMILIES = (
    {
        # Qwen2 and Qwen2.5, including the Coder variants, which share the
        # base architecture at each size. Qwen3 deliberately does NOT match:
        # its architecture is not confidently known here, so it falls through
        # to the conservative heuristic rather than borrowing these numbers.
        "family": "qwen2.5",
        "pattern": re.compile(r"qwen-?2(?:\.5)?(?![0-9])"),
        "sizes": [
            _kv_entry(0.5, 24, 2, 64, _ATTENTION_GQA, "recalled_estimate"),
            _kv_entry(1.5, 28, 2, 128, _ATTENTION_GQA, "published_config"),
            _kv_entry(3.0, 36, 2, 128, _ATTENTION_GQA, "recalled_estimate"),
            _kv_entry(7.0, 28, 4, 128, _ATTENTION_GQA, "published_config"),
            _kv_entry(14.0, 48, 8, 128, _ATTENTION_GQA, "published_config"),
            _kv_entry(32.0, 64, 8, 128, _ATTENTION_GQA, "published_config"),
            _kv_entry(72.0, 80, 8, 128, _ATTENTION_GQA, "recalled_estimate"),
        ],
    },
    {
        "family": "llama3",
        "pattern": re.compile(r"llama-?3(?:\.[123])?(?![0-9])"),
        "sizes": [
            _kv_entry(1.0, 16, 8, 64, _ATTENTION_GQA, "recalled_estimate"),
            _kv_entry(3.0, 28, 8, 128, _ATTENTION_GQA, "recalled_estimate"),
            _kv_entry(8.0, 32, 8, 128, _ATTENTION_GQA, "published_config"),
            _kv_entry(70.0, 80, 8, 128, _ATTENTION_GQA, "published_config"),
        ],
    },
    {
        # Llama 2 and Code Llama. The 7B and 13B sizes are plain multi-head
        # attention (only the 70B in that family uses grouped-query), which
        # is why their KV cost dwarfs a Llama 3 of the same parameter count,
        # and exactly why "same size, same memory" reasoning goes wrong.
        "family": "llama2",
        "pattern": re.compile(r"code-?llama|llama-?2(?![0-9])"),
        "sizes": [
            _kv_entry(7.0, 32, 32, 128, _ATTENTION_MHA, "published_config"),
            _kv_entry(13.0, 40, 40, 128, _ATTENTION_MHA, "published_config"),
            _kv_entry(70.0, 80, 8, 128, _ATTENTION_GQA, "published_config"),
        ],
    },
    {
        "family": "mistral7b",
        "pattern": re.compile(r"mistral"),
        "sizes": [
            _kv_entry(7.0, 32, 8, 128, _ATTENTION_GQA, "published_config"),
        ],
    },
    {
        "family": "phi3-mini",
        "pattern": re.compile(r"phi-?3(?:\.5)?"),
        "sizes": [
            # Recalled as plain multi-head attention, flagged conservative in
            # case a grouped-query variant exists, exactly as the matching
            # CATALOG entry is.
            _kv_entry(3.8, 32, 32, 96, _ATTENTION_MHA, "conservative_overestimate"),
        ],
    },
    {
        "family": "deepseek-coder",
        "pattern": re.compile(r"deepseek-?coder(?!-?v2)"),
        "sizes": [
            _kv_entry(6.7, 32, 32, 128, _ATTENTION_MHA, "conservative_overestimate"),
        ],
    },
    {
        "family": "starcoder2",
        "pattern": re.compile(r"starcoder-?2"),
        "sizes": [
            _kv_entry(15.0, 40, 4, 128, _ATTENTION_GQA, "recalled_estimate"),
        ],
    },
)

# A family entry is used only when the parsed parameter count is within this
# fraction of that entry's declared count. "Qwen2.5-14B" must not be graded
# with the 7B entry's KV cost merely because 7 is the nearest number in the
# table; outside the band the conservative heuristic takes over instead.
KV_FAMILY_SIZE_TOLERANCE = 0.15

# The geometric shape assumed for an unrecognised dense transformer:
# hidden_size = 128 * layers, and params ~= 12 * layers * hidden_size**2,
# which rearranges to params ~= 196608 * layers**3. Checked against shapes
# this module already knows: it puts a 7B at 32.9 layers (Llama 2 7B has 32)
# and a 13B at 40.6 (Llama 2 13B has 40).
_DENSE_PARAM_COEFF = 196608
_DENSE_ASPECT = 128

# Applied on top of the multi-head-attention estimate. The estimate is
# already pessimistic for any grouped-query model; this margin covers models
# whose layer count runs above the geometric fit (Phi-3.5 Mini is one). The
# self-test pins the resulting claim: the heuristic never under-counts any
# multi-head-attention entry in KV_FAMILIES.
_KV_HEURISTIC_SAFETY = 1.25

# Floor, so an implausibly small parameter estimate cannot produce a KV cost
# so tiny that a broken listing looks like it fits anywhere.
_KV_HEURISTIC_FLOOR = 8192

# Used when neither a family nor a parameter count could be determined at
# all. Equal to a 7B model with plain multi-head attention at f16
# (2 * 32 layers * 32 KV heads * 128 head dim * 2 bytes), the largest KV
# footprint among the well-known 7B-class architectures. A deliberately large
# stand-in, not a measurement: entries using it carry kv_confidence "unknown"
# so a UI can say the fit reading is a guess.
UNKNOWN_KV_BYTES_PER_TOKEN = 524_288


def _kv_heuristic_bytes(params_b):
    """A conservative KV bytes-per-token estimate for a model of `params_b`
    billion parameters whose architecture is not known.

    Assumes a dense transformer with hidden_size = 128 * layers and PLAIN
    MULTI-HEAD ATTENTION, then applies _KV_HEURISTIC_SAFETY. Most models
    released since roughly 2023 use grouped-query attention and need several
    times less than this, so the estimate usually over-counts, and that is
    the intended direction: an over-count produces a pessimistic verdict,
    while an under-count produces a confident "it fits" that is wrong only
    after the user has downloaded several gigabytes.
    """
    if not params_b or params_b <= 0:
        return UNKNOWN_KV_BYTES_PER_TOKEN
    layers = (params_b * 1e9 / _DENSE_PARAM_COEFF) ** (1.0 / 3.0)
    hidden = _DENSE_ASPECT * layers
    # Per layer: K and V, each of full hidden width (multi-head attention),
    # at bytes_per_element = 2.
    per_token = 2 * layers * hidden * 2
    return max(_KV_HEURISTIC_FLOOR, int(math.ceil(per_token * _KV_HEURISTIC_SAFETY)))


def kv_for_model(name, size_bytes=None, quant=None, params_b=None):
    """How many bytes of KV cache one token costs for the model `name`, plus
    a full account of where that number came from.

    Returns a dict with:
      kv_bytes_per_token - the figure verdict_for() needs.
      kv_source          - "family", "size_heuristic", or "unknown_default".
      kv_confidence      - "published_config", "recalled_estimate",
                           "conservative_overestimate", or "unknown".
      kv_calc            - the layers/kv_heads/head_dim arithmetic behind a
                           family hit; None for the heuristic and the
                           default, which have no architecture to report.
      kv_family          - the KV_FAMILIES family name, or None.
      params_b           - the parameter count used, or None.
      params_source      - "name", "file_size", "caller", "family", or None.

    The KV cost is a property of the WEIGHTS, not of the quantisation: a
    Q4_K_M and a Q8_0 of the same model cache the same K and V tensors at
    llama.cpp's f16 default. size_bytes and quant are therefore used only as
    a second route to a parameter count when the name states none.

    See the module docstring for the three-source order and why the fallback
    over-counts on purpose.
    """
    text = _normalize_name(name)
    params_source = None
    if params_b is not None:
        params_source = "caller"
    else:
        params_b = parse_params_b(name)
        if params_b is not None:
            params_source = "name"
        else:
            params_b = params_from_size(size_bytes, quant)
            if params_b is not None:
                params_source = "file_size"

    family_hit = None
    for family in KV_FAMILIES:
        if not family["pattern"].search(text):
            continue
        if params_b is None:
            # The family is right but no size is stated anywhere. Take the
            # family's most expensive size rather than guess a middle one:
            # within a known family, the largest KV cost is the only figure
            # that cannot under-count.
            family_hit = (family, max(family["sizes"], key=lambda s: s["kv_bytes_per_token"]))
            break
        near = [
            size for size in family["sizes"]
            if abs(size["params_b"] - params_b) <= KV_FAMILY_SIZE_TOLERANCE * size["params_b"]
        ]
        if near:
            family_hit = (family, min(near, key=lambda s: abs(s["params_b"] - params_b)))
        # First matching family wins, hit or miss on size: a name that looks
        # like llama2 is not going to be better served by a later family's
        # numbers, and falling through to the conservative heuristic is the
        # correct outcome for a size this table does not cover.
        break

    if family_hit is not None:
        family, size = family_hit
        return {
            "kv_bytes_per_token": size["kv_bytes_per_token"],
            "kv_source": "family",
            "kv_confidence": size["kv_confidence"],
            "kv_calc": dict(size["kv_calc"]),
            "kv_family": family["family"],
            "params_b": size["params_b"] if params_b is None else params_b,
            "params_source": params_source or "family",
        }

    if params_b is not None:
        return {
            "kv_bytes_per_token": _kv_heuristic_bytes(params_b),
            "kv_source": "size_heuristic",
            "kv_confidence": "conservative_overestimate",
            "kv_calc": None,
            "kv_family": None,
            "params_b": params_b,
            "params_source": params_source,
        }

    return {
        "kv_bytes_per_token": UNKNOWN_KV_BYTES_PER_TOKEN,
        "kv_source": "unknown_default",
        "kv_confidence": "unknown",
        "kv_calc": None,
        "kv_family": None,
        "params_b": None,
        "params_source": None,
    }


def kv_for_repo(repo_id, files):
    """kv_for_model() for a whole repository listing.

    The KV cost belongs to the weights, so it is resolved once per repository
    and applied to every quantisation. The repository name is tried first;
    when it states no parameter count, the LARGEST quantisation present is
    used to estimate one, since the largest file carries the smallest
    relative error against its bits-per-weight figure. Vision projectors are
    excluded from that estimate: they are a fraction of the model's size and
    would understate it badly.
    """
    sized = [
        f for f in (files or [])
        if isinstance(f.get("size_bytes"), int) and not is_projector(f)
    ]
    biggest = max(sized, key=lambda f: f["size_bytes"]) if sized else None
    if parse_params_b(repo_id) is not None or biggest is None:
        return kv_for_model(repo_id)
    # Fold the biggest file's own name in: a repository called
    # "TheBloke/foo-GGUF" can still hold "foo-7B-Q4_K_M.gguf", and that name
    # is worth reading before falling back to arithmetic on the file size.
    combined = "{} {}".format(repo_id, biggest.get("name") or "")
    return kv_for_model(combined, size_bytes=biggest["size_bytes"], quant=biggest.get("quant"))


# -- per-quantisation grading --------------------------------------------------

# A name heuristic, nothing more. The Hub has no reliable machine-readable
# "this is a coding model" signal (pipeline_tag reads "text-generation" for
# all of them). Used only to order recommend()'s live picks, never to hide a
# result from a search.
_CODING_NAME_RE = re.compile(r"cod(?:e|er)|starcoder|devstral|sqlcoder")


def guess_focus(name):
    """"coding" or "general", guessed from a repository or file name.

    A guess, and labelled one everywhere it is used. Hearth Code is the
    flagship surface so recommend() prefers coding models, but search_shop()
    never filters on this.
    """
    return "coding" if _CODING_NAME_RE.search(_normalize_name(name)) else "general"


def _quant_sort_key(quant):
    """Sort key implementing the shop's per-quantisation preference.

    Three components, in order:

      1. Complete before incomplete. A split model missing a part cannot be
         loaded at all, so it sorts behind everything regardless of how well
         it would otherwise have fitted.
      2. The verdict rank (VERDICT_RANK), best first. This is the point of
         the whole exercise: choosing between quantisations is a fit
         decision, not a size decision. A great beats a good even though the
         good is the larger file, because a good is by definition running
         with tight headroom and the great is not.
      3. Size, with the direction FLIPPING on the verdict. Among quants that
         fit fully in VRAM (great, good), the LARGEST file wins its tier:
         they all run, so take the most faithful one. Among quants that do
         not (reduced_context, cpu_spillover, wont_fit), the SMALLEST wins:
         it gives back the most context and spills the least. Sorting by
         size in one fixed direction would be wrong at one end or the other.
    """
    verdict = quant.get("verdict")
    name = quant.get("name") or ""
    incomplete = 0 if quant.get("complete", True) else 1
    if not verdict:
        # No size from the Hub means no verdict; those sort last within
        # their completeness group rather than pretending to a tier.
        return (incomplete, len(VERDICT_RANK), 0, name)
    rank = VERDICT_RANK[verdict["verdict"]]
    size = quant.get("size_bytes") or 0
    if verdict["verdict"] in (VERDICT_GREAT, VERDICT_GOOD):
        return (incomplete, rank, -size, name)
    return (incomplete, rank, size, name)


def rank_quants(quants):
    """The quantisations of one repository, ordered best-choice-first for
    this machine. Returns a new list; the input is not modified. See
    _quant_sort_key for the ordering and why the size preference flips.
    """
    return sorted(quants or [], key=_quant_sort_key)


# -- editions of one quantisation ---------------------------------------------
#
# Qwen's official GGUF repositories publish most quantisations TWICE: once as
# one file, "<stem>.gguf", and once split into "<stem>-00001-of-0000N.gguf"
# parts, which hearth_hf groups into a second logical entry named "<stem>".
# The two hold the same weights at the same quantisation and differ by a
# couple of hundred bytes of container header, so both land in the same
# verdict tier and a listing that shows both shows the user two rows that
# look identical and are, for every purpose except how the download arrives.
#
# They are collapsed to one row per quantisation, and the surviving row
# carries the other edition rather than discarding it: a user whose
# filesystem will not take a 15GB file, or who wants to know a download
# arrives in four pieces, is asking a real question.

# Every GGUF filename ends in this, and a split's grouped name is exactly the
# single file's name without it. That is the whole matching rule.
_GGUF_SUFFIX = ".gguf"


def _edition_id(quant):
    """Which quantisation this entry is an EDITION OF: two entries share an
    id when one is "<stem>.gguf" and the other is the split of "<stem>" in
    the same directory.

    Deliberately not keyed on the quantisation label. Genuinely different
    builds can share one label: hearth_hf.parse_quant reads both
    "foo-Q4_K_M.gguf" and the imatrix build "foo-i1-Q4_K_M.gguf" as Q4_K_M,
    and those are two real choices, not one duplicated. Matching on the
    filename matches only the case this is about, the same file published
    whole and in parts, and leaves everything else alone.

    An entry with no usable name is given an id of its own, so a listing that
    somehow carries several nameless entries never collapses them into one on
    no evidence.
    """
    parts = quant.get("parts") or []
    path = quant.get("path")
    if not path and parts and isinstance(parts[0], dict):
        # A split model has no single path of its own; its parts carry the
        # directory, which is part of the identity (a repository can put each
        # quantisation in its own folder).
        path = parts[0].get("path")
    text = str(path or "")
    directory = text.rsplit("/", 1)[0] if "/" in text else ""
    base = str(quant.get("name") or "").rsplit("/", 1)[-1]
    if base.lower().endswith(_GGUF_SUFFIX):
        base = base[: -len(_GGUF_SUFFIX)]
    if not base:
        return (directory, "", id(quant))
    return (directory, base)


def _edition_sort_key(quant):
    """Ordering WITHIN one edition group, i.e. between editions of the same
    quantisation. _quant_sort_key first, with one component inserted: at an
    equal verdict, the SINGLE-FILE edition beats the split one.

    One request, one hash to verify, nothing to reassemble, and no way to
    end up with a half-downloaded model that llama.cpp refuses to load. The
    two editions differ by a couple of hundred bytes, so ranking them on size
    alone picks between them essentially at random.

    The preference sits BELOW the verdict rank on purpose: preferring the
    single file must never trade a "great" down to a "good". It sits ABOVE
    size, but only inside a group - as a component of the listing-wide
    _quant_sort_key it would make a smaller single-file quantisation beat a
    larger split one of a DIFFERENT quantisation in the same tier, which is
    the opposite of what the shop wants.

    This is the one place the single-file preference is expressed. The
    listing (quant_verdicts) and the offer (best_quant) both reach it through
    _edition_groups, so they cannot disagree about which edition survives.
    """
    incomplete, rank, size, name = _quant_sort_key(quant)
    return (incomplete, rank, 1 if quant.get("multipart") else 0, size, name)


def _edition_groups(quants):
    """`quants` grouped by _edition_id, each group ordered by
    _edition_sort_key (the edition to surface first), the groups themselves
    in the order their first member appeared. Returns new lists; the entries
    inside them are the caller's own objects, not copies.
    """
    groups = {}
    for quant in quants or []:
        groups.setdefault(_edition_id(quant), []).append(quant)
    return [sorted(group, key=_edition_sort_key) for group in groups.values()]


# A GGUF whose base name starts with this is a multimodal vision projector,
# not a language model: it is the image encoder that rides ALONGSIDE one.
# hearth_hf's own filename survey found these among the files that carry no
# parseable quantisation label. They are listed like any other file, because
# a user with a vision model does need to download one, but they are never
# the answer to "which quantisation should I run", and on a small card a
# 1.4GB projector would otherwise be the first thing to earn a "great".
_PROJECTOR_PREFIX = "mmproj"


def is_projector(quant):
    """True when this file is a multimodal vision projector rather than a
    model to run. Matched on the filename prefix, which is the convention
    llama.cpp's own conversion tooling emits.
    """
    base = str(quant.get("name") or "").rsplit("/", 1)[-1].lower()
    return base.startswith(_PROJECTOR_PREFIX)


def _offerable(quant):
    """True when this entry is a thing a user could actually download and
    run: complete, not a vision projector, graded, and not wont_fit.
    """
    if not quant.get("complete", True) or is_projector(quant):
        return False
    verdict = quant.get("verdict")
    return bool(verdict) and verdict["verdict"] != VERDICT_WONT_FIT


def best_quant(quants):
    """The single quantisation to offer for this repository, or None.

    One edition per quantisation is considered (see _edition_sort_key: the
    single-file edition wins at an equal verdict, a split that lands in a
    better tier still wins outright), and the offer is the first of those in
    rank_quants() order that is offerable (see _offerable).

    None when every quantisation in the repository is unusable on this
    hardware, which is the honest answer and is what keeps a repository
    nothing can run from being recommended.

    The returned entry is one of the objects in `quants`, never a copy, so a
    caller can find it again by identity. `quants` is not modified.
    """
    surviving = [group[0] for group in _edition_groups(quants)]
    return next((q for q in rank_quants(surviving) if _offerable(q)), None)


def quant_verdicts(files, hw=None, context_tokens=None, kv=None, free_disk=None):
    """Grade every quantisation in `files` against `hw`, one verdict each.

    `files` is the "files" list from hearth_hf.list_gguf_files(): one entry
    per LOGICAL model, so a split model arrives already summed across its
    parts and is graded on that sum rather than on one part.

    THE VERDICT IS PER QUANTISATION. A repository offers one set of weights
    at many sizes, an order of magnitude apart end to end, so a single
    repository-level verdict would be meaningless. Each returned entry is the
    hearth_hf file entry plus:

      verdict          - the full verdict_for() dict, or None when the Hub
                         reported no size and there is nothing honest to
                         compute.
      disk_ok          - whether there is room on disk, or None when the size
                         is unknown.
      free_disk_bytes  - what disk_ok was judged against.
      kv_bytes_per_token / kv_confidence - the KV figure the verdict used and
                         how much to trust it.
      alternate_editions - the other editions of this same quantisation, each
                         a fully graded entry of the same shape. Almost
                         always empty or one entry: the split edition of a
                         quantisation this row publishes as a single file.
      split_part_count - how many files this quantisation arrives in if
                         downloaded as a split, whether that split is this
                         row or its alternate edition. None when the
                         repository publishes no split edition of it.

    ONE ROW PER QUANTISATION. Every file in `files` is graded, but the
    editions of one quantisation (see _edition_id: the same weights published
    both whole and split) collapse into a single row, the one
    _edition_sort_key prefers, with the others hanging off it in
    alternate_editions. Returning both as sibling rows puts two visually
    identical lines in front of the user for every quantisation a repository
    publishes twice, which in Qwen's own repositories is most of them.

    `kv` is a kv_for_model()/kv_for_repo() result; when None it is resolved
    from the files themselves. Every verdict goes through verdict_for(), so
    the KV cache is counted in exactly as it is for a CATALOG entry: nothing
    here compares a file size against a VRAM size.

    Returned in rank_quants() order, best choice for this machine first.
    """
    if hw is None:
        hw = hearth_hw.probe()
    if free_disk is None:
        free_disk = free_disk_bytes()
    files = list(files or [])
    if kv is None:
        kv = kv_for_repo("", files)

    graded = []
    for entry in files:
        quant = dict(entry)
        size = entry.get("size_bytes")
        if isinstance(size, int) and size > 0:
            quant["verdict"] = verdict_for(
                {"size_bytes": size, "kv_bytes_per_token": kv["kv_bytes_per_token"]},
                hw, context_tokens=context_tokens,
            )
            quant["disk_ok"] = free_disk >= (size + DISK_BUFFER_BYTES)
        else:
            quant["verdict"] = None
            quant["disk_ok"] = None
        quant["free_disk_bytes"] = free_disk
        quant["kv_bytes_per_token"] = kv["kv_bytes_per_token"]
        quant["kv_confidence"] = kv["kv_confidence"]
        quant["projector"] = is_projector(entry)
        # Set on every entry, including the ones that end up as an alternate
        # edition, so nothing in a listing has to be read with a .get().
        quant["alternate_editions"] = []
        quant["split_part_count"] = quant.get("part_count") if quant.get("multipart") else None
        graded.append(quant)

    listed = []
    for group in _edition_groups(graded):
        surviving, alternates = group[0], group[1:]
        surviving["alternate_editions"] = alternates
        split = next((q for q in group if q.get("multipart")), None)
        surviving["split_part_count"] = split.get("part_count") if split else None
        listed.append(surviving)
    return rank_quants(listed)


# -- shop listings -------------------------------------------------------------

def _repo_label(repo_id):
    """A short display name for a repository: the name half, with the
    conventional GGUF suffix trimmed, since every result in a filter=gguf
    search carries it and repeating it adds nothing.
    """
    name = str(repo_id or "").rsplit("/", 1)[-1]
    for suffix in ("-GGUF", "-gguf", ".GGUF", ".gguf"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name or str(repo_id or "")


def _shop_entry_from_search(meta, context_tokens, free_disk):
    """The parts of a shop entry that come from a search result alone, before
    (or without) a file listing.
    """
    repo_id = meta.get("repo_id")
    params_b = parse_params_b(repo_id)
    return {
        "source": SOURCE_LIVE,
        "repo_id": repo_id,
        "label": _repo_label(repo_id),
        "author": meta.get("author"),
        "downloads": meta.get("downloads"),
        "likes": meta.get("likes"),
        "last_modified": meta.get("last_modified"),
        "gated": bool(meta.get("gated")),
        "gated_mode": meta.get("gated_mode"),
        "private": bool(meta.get("private")),
        "tags": list(meta.get("tags") or []),
        "url": meta.get("url"),
        "focus": guess_focus(repo_id),
        # A gated repository is listed but cannot be fetched anonymously,
        # which is exactly what hearth_hf reports too. Saying so up front
        # beats a 401 after the user clicks download.
        "downloadable": not bool(meta.get("gated")),
        "quants": [],
        "quant_count": 0,
        "quants_loaded": False,
        "best_quant": None,
        "verdict": None,
        "files_error": None,
        "files_error_kind": None,
        "free_disk_bytes": free_disk,
        "context_tokens": context_tokens,
        "params_b": params_b,
        "params_source": "name" if params_b is not None else None,
        "kv_bytes_per_token": None,
        "kv_confidence": None,
        "kv_source": None,
        "kv_calc": None,
        "kv_family": None,
    }


def _attach_files(entry, files, hw, context_tokens, free_disk):
    """Fold a repository's GGUF file listing into a shop entry: resolve the
    KV cost once, grade every quantisation, and pick the one to offer.
    """
    kv = kv_for_repo(entry["repo_id"], files)
    entry.update({
        "params_b": kv["params_b"],
        "params_source": kv["params_source"],
        "kv_bytes_per_token": kv["kv_bytes_per_token"],
        "kv_confidence": kv["kv_confidence"],
        "kv_source": kv["kv_source"],
        "kv_calc": kv["kv_calc"],
        "kv_family": kv["kv_family"],
    })
    quants = quant_verdicts(files, hw, context_tokens=context_tokens, kv=kv, free_disk=free_disk)
    # quant_verdicts() has already collapsed the editions, so best_quant()
    # here is choosing among the very rows the listing shows, and returns one
    # of them by identity rather than a lookalike.
    chosen = best_quant(quants)
    for quant in quants:
        quant["recommended"] = chosen is not None and quant is chosen
        for alternate in quant["alternate_editions"]:
            # An alternate edition is never the offer: it is the same
            # quantisation at the same verdict as the row it hangs off, which
            # is the row that was chosen.
            alternate["recommended"] = False
    entry["quants"] = quants
    entry["quant_count"] = len(quants)
    entry["quants_loaded"] = True
    entry["best_quant"] = chosen
    entry["verdict"] = chosen["verdict"] if chosen else None
    return entry


def _hardware_summary(hw, context_tokens, free_disk):
    """The hardware every verdict in a listing was judged against, reported
    once at the top level so a UI can render it without digging a verdict out
    of an entry. Same fields, and the same caveats, verdict_for() reports.
    """
    gpu = _gpu_vram_bytes(hw)
    return {
        "platform": hw.get("platform"),
        "vram_bytes": gpu["vram_bytes"],
        "vram_approximate": gpu["approximate"],
        "gpu_detected": gpu["gpu_detected"],
        "gpu_vendor": gpu["vendor"],
        "shared_memory_likely": gpu["shared_memory_likely"],
        "ram_bytes": _ram_budget_bytes(hw),
        "system_ram_bytes": hw.get("system_ram_bytes", 0) or 0,
        "free_disk_bytes": free_disk,
        "context_tokens": context_tokens,
    }


def _fallback_models(hw, context_tokens, free_disk):
    """The built-in reference CATALOG, graded, in shop-entry shape.

    Every entry is marked source SOURCE_FALLBACK and downloadable False, and
    carries repo_id None, because these name models rather than Hub files and
    nothing can be downloaded while the Hub is unreachable anyway. The
    listing carrying them also has ok False (see search_shop), so a caller
    cannot mistake them for live data even by ignoring "source".
    """
    entries = []
    for model in catalog_with_verdicts(hw, context_tokens=context_tokens):
        entries.append({
            "source": SOURCE_FALLBACK,
            "repo_id": None,
            "reference_id": model["id"],
            "label": model["label"],
            "author": None,
            "downloads": None,
            "likes": None,
            "last_modified": None,
            "gated": False,
            "gated_mode": None,
            "private": False,
            "tags": [],
            "url": None,
            "focus": model["focus"],
            "downloadable": False,
            "description": model["description"],
            "license": model["license"],
            "params_b": model["params_b"],
            "params_source": "reference_catalog",
            "kv_bytes_per_token": model["kv_bytes_per_token"],
            "kv_confidence": model["kv_confidence"],
            "kv_source": "reference_catalog",
            "kv_calc": model.get("kv_calc"),
            "kv_family": None,
            "quants": [],
            "quant_count": 0,
            "quants_loaded": False,
            "best_quant": None,
            "verdict": model["verdict"],
            "files_error": None,
            "files_error_kind": None,
            "free_disk_bytes": free_disk,
            "disk_ok": model["disk_ok"],
            "context_tokens": context_tokens,
        })
    return entries


_FALLBACK_NOTICE = (
    "These are Hearth's built-in reference models, not live Hugging Face "
    "results: the Hub could not be reached, so the shop cannot show what is "
    "actually available or download anything. The fit verdicts below are "
    "still real - they were computed against this machine's hardware - but "
    "they answer \"what could this machine run\", not \"what should I "
    "install right now\"."
)


def _shop_entry_sort_key(entry):
    """Graded repositories first (best verdict first), then repositories
    whose quantisations have not been fetched yet, then repositories where
    nothing at all fits. Python's sort is stable, so within each group the
    Hub's own most-downloaded-first order survives untouched.

    Repositories that cannot run here sort LAST rather than being dropped: a
    user searching for a specific model deserves to see it and be told why it
    will not work, not to have it silently vanish from the results.
    """
    verdict = entry.get("verdict")
    if verdict:
        return (0, VERDICT_RANK[verdict["verdict"]])
    if not entry.get("quants_loaded"):
        return (1, 0)
    return (2, 0)


def _call_hf(fn, *args, **kwargs):
    """Call one of hearth_hf's result-returning functions and normalise a
    raised exception into the same result shape.

    hearth_hf documents that it never raises on a network failure, and it
    does not. This exists for the injected search_fn/list_fn seam and for a
    genuine bug, so that neither can turn a shop search into a traceback in
    front of a user.
    """
    try:
        out = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - classified, never propagated
        return {
            "ok": False,
            "error": "{} failed unexpectedly: {}: {}".format(
                getattr(fn, "__name__", "the Hugging Face client"), type(exc).__name__, exc,
            ),
            "error_kind": ERR_HF_UNAVAILABLE,
        }
    return out if isinstance(out, dict) else {
        "ok": False,
        "error": "The Hugging Face client returned a {}, not a result dict.".format(
            type(out).__name__,
        ),
        "error_kind": ERR_HF_UNAVAILABLE,
    }


def search_shop(query="", hw=None, context_tokens=None, limit=DEFAULT_SEARCH_LIMIT,
                detail_limit=DEFAULT_DETAIL_LIMIT, timeout=None, disk_path=None,
                search_fn=None, list_fn=None):
    """Search Hugging Face for GGUF models and grade every quantisation of
    the first `detail_limit` results against this machine.

    This is the shop's listing. Always returns a dict, never raises:

      ok             - True ONLY for live Hub results. A fallback listing is
                       always ok False, so a caller that never looks at
                       "source" still cannot mistake it for live data.
      error, error_kind - None on success. On failure, error_kind is one of
                       hearth_hf's ERR_* strings (unreachable, rate_limited,
                       auth_required, ...) or ERR_HF_UNAVAILABLE.
      source         - SOURCE_LIVE or SOURCE_FALLBACK.
      notice         - a sentence explaining a fallback listing, else None.
      query, context_tokens - what was asked for.
      hardware       - the machine every verdict was judged against.
      models         - the shop entries, best-fitting first (see
                       _shop_entry_sort_key). On a fallback listing, the
                       built-in reference catalog in the same shape.
      model_count, rate_limit, url.

    Each entry carries repo_id, label, author, downloads, likes,
    last_modified, gated/gated_mode, tags, url, focus, downloadable, the
    resolved params_b/params_source and kv_* fields, and:

      quants        - every quantisation in the repository, ONE ROW EACH,
                      each the hearth_hf file entry plus its own verdict,
                      ordered best-choice-first. Empty when quants_loaded is
                      False. A quantisation the repository publishes both as
                      one file and as a split is one row, not two: the other
                      edition rides on that row as alternate_editions, and
                      split_part_count says how many pieces the split
                      arrives in. See quant_verdicts().
      quants_loaded - False for results past detail_limit, which were not
                      fetched. Call repo_quants() to fill one in.
      best_quant    - the quantisation to offer, or None when nothing in the
                      repository runs here.
      verdict       - best_quant's verdict, or None. THE REPOSITORY HAS NO
                      VERDICT OF ITS OWN: this is the verdict of the one
                      quantisation the shop would pick.
      files_error / files_error_kind - set when that repository's file
                      listing failed while the search itself succeeded (a
                      gated repository does this), so one bad repository
                      degrades to one explained row rather than an empty
                      screen.

    search_fn and list_fn default to hearth_hf.search_models and
    hearth_hf.list_gguf_files. They are an injection seam: the self-test
    passes captured fixtures through them so the whole listing path is
    exercised with no network, and a caller with cached results can reuse
    this grading without refetching.
    """
    if hw is None:
        hw = hearth_hw.probe()
    ctx = DEFAULT_CONTEXT_TOKENS if context_tokens is None else context_tokens
    free_disk = free_disk_bytes(disk_path)
    result = {
        "ok": False,
        "error": None,
        "error_kind": None,
        "source": SOURCE_FALLBACK,
        "notice": None,
        "query": query,
        "context_tokens": ctx,
        "hardware": _hardware_summary(hw, ctx, free_disk),
        "models": [],
        "model_count": 0,
        "rate_limit": None,
        "url": None,
    }

    def degrade(error, error_kind):
        result["error"] = error
        result["error_kind"] = error_kind
        result["notice"] = _FALLBACK_NOTICE
        result["models"] = _fallback_models(hw, ctx, free_disk)
        result["model_count"] = len(result["models"])
        return result

    if search_fn is None or list_fn is None:
        try:
            hf = _hf()
        except Exception as exc:  # noqa: BLE001 - a broken install, not a bug here
            return degrade(
                "Hearth's Hugging Face client could not be loaded ({}: {}), so the shop "
                "cannot search.".format(type(exc).__name__, exc),
                ERR_HF_UNAVAILABLE,
            )
        search_fn = search_fn or hf.search_models
        list_fn = list_fn or hf.list_gguf_files

    search_kwargs = {"limit": limit}
    if timeout is not None:
        search_kwargs["timeout"] = timeout
    found = _call_hf(search_fn, query or "", **search_kwargs)
    result["rate_limit"] = found.get("rate_limit")
    result["url"] = found.get("url")
    if not found.get("ok"):
        return degrade(
            found.get("error") or "The Hugging Face search failed for an unstated reason.",
            found.get("error_kind") or ERR_HF_UNAVAILABLE,
        )

    list_kwargs = {} if timeout is None else {"timeout": timeout}
    entries = []
    for index, meta in enumerate(found.get("models") or []):
        entry = _shop_entry_from_search(meta, ctx, free_disk)
        if entry["repo_id"] and index < max(0, detail_limit):
            listing = _call_hf(list_fn, entry["repo_id"], **list_kwargs)
            if listing.get("ok"):
                _attach_files(entry, listing.get("files") or [], hw, ctx, free_disk)
            else:
                entry["files_error"] = listing.get("error")
                entry["files_error_kind"] = listing.get("error_kind")
        entries.append(entry)

    entries.sort(key=_shop_entry_sort_key)
    result["ok"] = True
    result["source"] = SOURCE_LIVE
    result["models"] = entries
    result["model_count"] = len(entries)
    return result


def repo_quants(repo_id, hw=None, context_tokens=None, disk_path=None, timeout=None,
                list_fn=None, meta=None):
    """Every quantisation in one Hugging Face repository, graded against this
    machine. Always returns a dict, never raises.

    What a UI calls when the user opens a search result that search_shop()
    left with quants_loaded False, and what a caller pointing straight at a
    known repository calls without searching first.

      ok, error, error_kind, source - as search_shop().
      repo_id, hardware, context_tokens.
      model - one shop entry, exactly as search_shop() produces, with
              quants_loaded True on success. None when the listing failed.

    `meta` is an optional search entry for the same repository (downloads,
    likes, gated, and so on); without it those fields are None, since this
    function asks the Hub for a file tree, not for repository metadata.
    """
    if hw is None:
        hw = hearth_hw.probe()
    ctx = DEFAULT_CONTEXT_TOKENS if context_tokens is None else context_tokens
    free_disk = free_disk_bytes(disk_path)
    result = {
        "ok": False,
        "error": None,
        "error_kind": None,
        "source": SOURCE_LIVE,
        "repo_id": repo_id,
        "context_tokens": ctx,
        "hardware": _hardware_summary(hw, ctx, free_disk),
        "model": None,
    }

    if list_fn is None:
        try:
            list_fn = _hf().list_gguf_files
        except Exception as exc:  # noqa: BLE001
            result["error_kind"] = ERR_HF_UNAVAILABLE
            result["error"] = (
                "Hearth's Hugging Face client could not be loaded ({}: {}), so the "
                "quantisations of {} cannot be listed.".format(
                    type(exc).__name__, exc, repo_id,
                )
            )
            return result

    listing = _call_hf(list_fn, repo_id, **({} if timeout is None else {"timeout": timeout}))
    base = dict(meta or {})
    base.setdefault("repo_id", repo_id)
    entry = _shop_entry_from_search(base, ctx, free_disk)
    if not listing.get("ok"):
        result["error"] = listing.get("error") or "Listing {} failed.".format(repo_id)
        result["error_kind"] = listing.get("error_kind") or ERR_HF_UNAVAILABLE
        entry["files_error"] = result["error"]
        entry["files_error_kind"] = result["error_kind"]
        result["model"] = entry
        return result

    _attach_files(entry, listing.get("files") or [], hw, ctx, free_disk)
    result["ok"] = True
    result["model"] = entry
    return result


def _capability(entry):
    """The capability proxy recommend() ranks live entries by: the parameter
    count, or 0 when the name did not state one and it could not be
    estimated. Sorting an unknown as 0 means a repository whose size could
    not be read is never the headline pick while a known one is available,
    which is the honest ordering: the shop should not claim a model is the
    biggest thing that fits when it does not know how big it is.
    """
    return entry.get("params_b") or 0.0


def _recommendation_reasoning(entry, role, listing):
    """The honest account of why this repository and this quantisation were
    picked: the choice, the competition it beat, the verdict's own message,
    and how much to trust the parameter and KV figures behind it.
    """
    verdict = entry["verdict"]
    ctx = verdict.get("requested_context_tokens")
    lead = ("Best pick for this machine" if role == "best"
            else "Lighter fallback if the best pick feels slow")
    name = entry.get("label") or entry.get("repo_id") or "this model"
    sentences = []

    quant = entry.get("best_quant")
    if quant is not None:
        graded = [q for q in entry.get("quants") or [] if q.get("verdict")]
        if verdict["verdict"] in (VERDICT_GREAT, VERDICT_GOOD):
            why = (
                "the largest of the {} graded quantisation(s) in this repository in the "
                "best verdict tier available at {} tokens of context".format(len(graded), ctx)
            )
        else:
            why = (
                "the smallest of the {} graded quantisation(s) in this repository, since "
                "none of them fit fully in VRAM at {} tokens of context".format(len(graded), ctx)
            )
        sentences.append("{}: {} at {} ({}), chosen as {}.".format(
            lead, name, quant.get("quant") or quant.get("name"),
            _gb(quant.get("size_bytes")), why,
        ))
    else:
        sentences.append(
            "{}: {}, from Hearth's built-in reference list rather than Hugging Face, "
            "because the Hub could not be reached. It cannot be downloaded until it "
            "can.".format(lead, name)
        )

    sentences.append(verdict["message"])

    if entry.get("params_b"):
        sentences.append("Parameter count taken as {:g}B, from {}.".format(
            entry["params_b"], (entry.get("params_source") or "an unstated source"),
        ))
    else:
        sentences.append(
            "The parameter count could not be read from the name or estimated from the "
            "file size, so the KV cache figure below is a conservative stand-in."
        )

    sentences.append(
        "KV cache figured at {} bytes per token ({}, confidence {}).".format(
            entry.get("kv_bytes_per_token"),
            (entry.get("kv_source") or "unknown source"),
            (entry.get("kv_confidence") or "unknown"),
        )
    )

    if entry.get("kv_source") == "size_heuristic":
        sentences.append(
            "That figure assumes plain multi-head attention because this model family is "
            "not in Hearth's table, which over-counts for any grouped-query model, so the "
            "verdict here is pessimistic rather than optimistic."
        )
    if listing and listing.get("source") == SOURCE_FALLBACK:
        sentences.append(_FALLBACK_NOTICE)
    return " ".join(s for s in sentences if s)


def _recommend_from_listing(listing):
    """recommend()'s live half: pick a headline repository and a lighter
    fallback out of a search_shop() listing.

    Mirrors the offline half's rules exactly, one level up. Among entries
    that fit fully in VRAM the largest by parameter count wins; when none do,
    the smallest reduced_context entry is preferred over the smallest
    cpu_spillover one, matching VERDICT_RANK. Coding models are preferred, by
    the guess_focus() name heuristic, but if the listing holds none, the
    general ones are ranked instead rather than returning nothing.

    An entry that cannot run is never picked, and the tier walk below is what
    guarantees it: wont_fit is not one of the tiers considered, at either the
    headline or the fallback step, exactly as in the offline half. An entry
    with no runnable quantisation carries verdict None and is dropped before
    the walk, since there is nothing to rank it by.
    """
    models = [m for m in (listing or {}).get("models") or [] if m.get("verdict")]
    if not models:
        return []
    coding = [m for m in models if m.get("focus") == "coding"]
    pool = coding or models

    runs_on_gpu = [m for m in pool if m["verdict"]["verdict"] in (VERDICT_GREAT, VERDICT_GOOD)]
    if runs_on_gpu:
        best = max(runs_on_gpu, key=_capability)
    else:
        reduced = [m for m in pool if m["verdict"]["verdict"] == VERDICT_REDUCED_CONTEXT]
        spill = [m for m in pool if m["verdict"]["verdict"] == VERDICT_CPU_SPILLOVER]
        if reduced:
            best = min(reduced, key=_capability)
        elif spill:
            best = min(spill, key=_capability)
        else:
            return []

    second = None
    for tier in (VERDICT_GREAT, VERDICT_GOOD, VERDICT_REDUCED_CONTEXT, VERDICT_CPU_SPILLOVER):
        candidates = [
            m for m in pool
            if m is not best and m["verdict"]["verdict"] == tier
        ]
        if candidates:
            second = min(candidates, key=_capability)
            break

    picks = []
    for role, entry in (("best", best), ("fallback", second)):
        if entry is None:
            continue
        out = dict(entry)
        out["recommended_role"] = role
        out["reasoning"] = _recommendation_reasoning(entry, role, listing)
        picks.append(out)
    return picks


def recommend_live(query=None, hw=None, context_tokens=None, limit=DEFAULT_SEARCH_LIMIT,
                   detail_limit=DEFAULT_DETAIL_LIMIT, timeout=None, disk_path=None,
                   search_fn=None, list_fn=None):
    """"What should I run on this machine", answered over live Hugging Face
    results. Always returns a dict, never raises.

    A search_shop() listing with one extra key, "picks": the 0 to 2 entries
    recommend() chose out of it, each with a "reasoning" string and a
    "recommended_role" of "best" or "fallback".

    query=None searches DEFAULT_RECOMMEND_QUERY. When the Hub is unreachable
    the listing degrades exactly as search_shop() does (ok False, source
    fallback, notice set) and the picks come from the built-in reference
    catalog, each one saying so in its reasoning.
    """
    listing = search_shop(
        DEFAULT_RECOMMEND_QUERY if query is None else query,
        hw=hw, context_tokens=context_tokens, limit=limit, detail_limit=detail_limit,
        timeout=timeout, disk_path=disk_path, search_fn=search_fn, list_fn=list_fn,
    )
    listing["picks"] = recommend(listing=listing)
    return listing


def _self_test():
    # -- free_disk_bytes: real filesystem, plausible values ------------------
    here_free = free_disk_bytes(os.path.dirname(os.path.abspath(__file__)))
    assert isinstance(here_free, int)
    assert here_free > 0, "the worktree's own volume must report positive free space"
    assert free_disk_bytes(None) >= 0

    # A nonexistent path must degrade to 0, not raise - this exercises the
    # shutil.disk_usage OSError branch.
    assert free_disk_bytes(r"Z:\this\path\does\not\exist\anywhere") == 0

    # os.getcwd() itself can raise OSError (a deleted working directory);
    # that call used to sit outside this function's try block, so it
    # could escape the "never raises" promise entirely. Prove it can't:
    # simulate the failure and confirm free_disk_bytes(None) still
    # degrades to 0 instead of propagating.
    old_getcwd = os.getcwd
    try:
        os.getcwd = lambda: (_ for _ in ()).throw(OSError("cwd is gone"))
        assert free_disk_bytes(None) == 0, (
            "free_disk_bytes(None) must degrade to 0 when os.getcwd() "
            "itself raises, not propagate the exception"
        )
    finally:
        os.getcwd = old_getcwd

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
    def _hw(vram_bytes, ram_bytes, approximate=False, vendor="nvidia", platform="synthetic"):
        gpus = [{"name": "synthetic", "vram_bytes": vram_bytes, "vendor": vendor,
                 "approximate": approximate}] if vram_bytes else []
        return {"platform": platform, "gpus": gpus, "system_ram_bytes": ram_bytes,
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
    assert v["gpu_detected"] is False, v
    assert v["gpu_vendor"] is None, v
    assert v["shared_memory_likely"] is False, v
    assert "no gpu was detected" in v["message"].lower(), (
        "a wont_fit verdict with no GPU at all must say so explicitly, "
        "not read identically to a wont_fit caused by a too-small GPU", v,
    )

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

    # -- CRITICAL 3: vendor/platform must not be thrown away by the seam ----
    # between hearth_hw and hearth_shop, the same class of bug as the
    # approximate flag above, one field over. An Intel or unknown-vendor
    # "GPU" may just be reporting shared system memory, not real VRAM, so it
    # must not be trusted the same as a confirmed discrete NVIDIA/AMD card.
    hw_24gb_intel = _hw(24 * 1024 ** 3, 64 * 1024 ** 3, vendor=hearth_hw.VENDOR_INTEL)
    hw_24gb_unknown = _hw(24 * 1024 ** 3, 64 * 1024 ** 3, vendor=hearth_hw.VENDOR_UNKNOWN)
    hw_24gb_amd = _hw(24 * 1024 ** 3, 64 * 1024 ** 3, vendor=hearth_hw.VENDOR_AMD)

    v_nvidia = verdict_for(syn_14gb, hw_24gb, context_tokens=8192)
    v_intel = verdict_for(syn_14gb, hw_24gb_intel, context_tokens=8192)
    v_unknown = verdict_for(syn_14gb, hw_24gb_unknown, context_tokens=8192)
    v_amd = verdict_for(syn_14gb, hw_24gb_amd, context_tokens=8192)

    assert v_nvidia["verdict"] == VERDICT_GREAT, v_nvidia
    assert v_amd["verdict"] == VERDICT_GREAT, (
        "AMD is a trusted discrete-vendor reading, same as NVIDIA", v_amd,
    )
    # Same VRAM, same model, same context as the NVIDIA "great" case above -
    # the only difference is the vendor string. An Intel or unknown vendor
    # must never earn "great": the reading might be shared system memory,
    # which does not perform like dedicated VRAM.
    for v_conservative, vendor_label in ((v_intel, "intel"), (v_unknown, "unknown")):
        assert v_conservative["verdict"] == VERDICT_GOOD, (
            "an Intel/unknown-vendor GPU must never earn a 'great' verdict "
            "on VRAM that might actually be shared system memory",
            vendor_label, v_conservative,
        )
        assert v_conservative["shared_memory_likely"] is True, (vendor_label, v_conservative)
        assert v_conservative["verdict"] != v_nvidia["verdict"], (
            "vendor must actually change the outcome, not just be a silent field",
            vendor_label, v_conservative,
        )
        assert "dedicated vram" in v_conservative["message"].lower(), v_conservative
        # The arithmetic must be identical to the trusted reading - only the
        # confidence in the verdict changes, never the numbers.
        assert v_conservative["required_bytes"] == v_nvidia["required_bytes"], v_conservative
        assert v_conservative["headroom_bytes"] == v_nvidia["headroom_bytes"], v_conservative

    assert v_nvidia["shared_memory_likely"] is False, v_nvidia
    assert v_amd["shared_memory_likely"] is False, v_amd
    assert v_intel["gpu_vendor"] == hearth_hw.VENDOR_INTEL, v_intel
    assert v_unknown["gpu_vendor"] == hearth_hw.VENDOR_UNKNOWN, v_unknown

    # -- CRITICAL 4: "no GPU was detected" must read as a different sentence
    # from "a GPU was detected and it does not fit" - conflating the two is
    # exactly the dishonesty bug 1 exists to close. Compare a wont_fit caused
    # by a genuinely tiny (but present) GPU against a wont_fit caused by no
    # GPU at all: same verdict tier, different underlying fact, so the
    # message and the gpu_detected field must both say so.
    hw_tiny_gpu_starved_ram = _hw(1 * 1024 * 1024, 512 * 1024 ** 2)  # 1MiB "GPU", ~0 RAM budget
    hw_no_gpu_starved_ram = _hw(0, 512 * 1024 ** 2)
    assert _ram_budget_bytes(hw_tiny_gpu_starved_ram) == 0
    assert _ram_budget_bytes(hw_no_gpu_starved_ram) == 0

    v_tiny_gpu = verdict_for(syn_14gb, hw_tiny_gpu_starved_ram, context_tokens=8192)
    v_no_gpu = verdict_for(syn_14gb, hw_no_gpu_starved_ram, context_tokens=8192)
    assert v_tiny_gpu["verdict"] == VERDICT_WONT_FIT, v_tiny_gpu
    assert v_no_gpu["verdict"] == VERDICT_WONT_FIT, v_no_gpu
    assert v_tiny_gpu["gpu_detected"] is True, v_tiny_gpu
    assert v_no_gpu["gpu_detected"] is False, v_no_gpu
    assert v_tiny_gpu["gpu_detected"] != v_no_gpu["gpu_detected"], (
        "gpu_detected must actually distinguish the two cases, not be a "
        "silent field", v_tiny_gpu, v_no_gpu,
    )
    assert v_tiny_gpu["message"] != v_no_gpu["message"], (
        "wont_fit for a too-small GPU and wont_fit for no GPU at all must "
        "not read as the same sentence", v_tiny_gpu["message"], v_no_gpu["message"],
    )
    assert "no gpu was detected" not in v_tiny_gpu["message"].lower(), v_tiny_gpu
    assert "no gpu was detected" in v_no_gpu["message"].lower(), v_no_gpu
    assert "synthetic" in v_no_gpu["message"].lower(), (
        "the no-GPU message should surface the platform it ran the check "
        "on (hw['platform']), not a generic sentence", v_no_gpu,
    )
    assert v_no_gpu["platform"] == "synthetic", v_no_gpu

    # Same distinction must hold on the cpu_spillover branch, not just
    # wont_fit: a small-but-present GPU with generous RAM spills over with
    # "Does not fit the GPU" phrasing; no GPU at all with the same generous
    # RAM spills over with "No GPU was detected" phrasing instead.
    hw_tiny_gpu_big_ram = _hw(1 * 1024 * 1024, 64 * 1024 ** 3, vendor=hearth_hw.VENDOR_NVIDIA)
    hw_no_gpu_big_ram = _hw(0, 64 * 1024 ** 3)
    v_tiny_spill = verdict_for(syn_14gb, hw_tiny_gpu_big_ram, context_tokens=8192)
    v_no_gpu_spill = verdict_for(syn_14gb, hw_no_gpu_big_ram, context_tokens=8192)
    assert v_tiny_spill["verdict"] == VERDICT_CPU_SPILLOVER, v_tiny_spill
    assert v_no_gpu_spill["verdict"] == VERDICT_CPU_SPILLOVER, v_no_gpu_spill
    assert "does not fit the gpu" in v_tiny_spill["message"].lower(), v_tiny_spill
    assert "no gpu was detected" in v_no_gpu_spill["message"].lower(), v_no_gpu_spill

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

    _self_test_hf(_hw)

    print("hearth-shop self-test OK")
    return 0


# ---------------------------------------------------------------------------
# Self-test fixtures for the Hugging Face half. Raw Hub tree entries, fed
# through hearth_hf.group_gguf_files so the shop is tested against that
# function's REAL output shape rather than against a hand-written guess at
# it. No network: search and listing both arrive through the search_fn /
# list_fn seam.
# ---------------------------------------------------------------------------

def _fixture_tree(entries):
    """[(path, size), ...] into the raw Hub tree shape, every file LFS-backed
    with a plausible 64-hex sha256, which is what a real GGUF over a few
    hundred kilobytes always is.
    """
    out = []
    for index, (path, size) in enumerate(entries):
        oid = "{:064x}".format(index + 1)
        out.append({"type": "file", "oid": "git" + str(index), "size": size,
                    "lfs": {"oid": oid, "size": size, "pointerSize": 136},
                    "path": path})
    return out


# Qwen's own GGUF repository, whose real contents are the fixture further
# down. A constant because the fixture, the search stub, and the test that
# reads them all name it.
_EDITION_REPO_ID = "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF"

# Sizes are the real orders of magnitude for these quantisations of these
# models. The 7B repo is the calibration case: on a 6GB card at 8192 tokens
# it spans every verdict tier from great to wont_fit, which is what makes it
# able to catch a broken fit calculation.
_FIXTURE_REPO_FILES = {
    "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF": _fixture_tree([
        ("Qwen2.5-Coder-7B-Instruct-Q2_K.gguf", 3_180_000_000),
        ("Qwen2.5-Coder-7B-Instruct-Q3_K_M.gguf", 3_808_000_000),
        ("Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf", 4_683_073_344),
        # Qwen's official repositories really do publish the SAME quant both
        # as one file and as a split, a fraction of a percent apart in size.
        # The split is listed a hair larger here so that ranking on size
        # alone would pick it, which is the wrong download of the two.
        ("Qwen2.5-Coder-7B-Instruct-Q4_K_M-00001-of-00002.gguf", 2_342_000_000),
        ("Qwen2.5-Coder-7B-Instruct-Q4_K_M-00002-of-00002.gguf", 2_342_000_000),
        ("Qwen2.5-Coder-7B-Instruct-Q5_K_M.gguf", 5_444_831_104),
        ("Qwen2.5-Coder-7B-Instruct-Q6_K.gguf", 6_254_199_264),
        ("Qwen2.5-Coder-7B-Instruct-Q8_0.gguf", 8_098_525_184),
        ("Qwen2.5-Coder-7B-Instruct-f16.gguf", 15_237_852_672),
        # A vision projector: a real .gguf with no parseable quant label and
        # no business being offered as a model to run.
        ("mmproj-Qwen2.5-Coder-7B.gguf", 1_400_000_000),
    ]),
    "bartowski/Qwen2.5-Coder-32B-Instruct-GGUF": _fixture_tree([
        ("Qwen2.5-Coder-32B-Instruct-IQ2_M.gguf", 11_264_441_312),
        ("Qwen2.5-Coder-32B-Instruct-Q2_K.gguf", 12_313_099_000),
        ("Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf", 19_851_336_704),
        # A complete two-part split, which must be offered as ONE 34GB
        # choice rather than two 17GB ones.
        ("Qwen2.5-Coder-32B-Instruct-Q8_0-00001-of-00002.gguf", 17_000_000_000),
        ("Qwen2.5-Coder-32B-Instruct-Q8_0-00002-of-00002.gguf", 17_800_000_000),
        # An INCOMPLETE split: part 2 of 2 never made it into the repo. Sized
        # deliberately so that, judged on its one present part alone, it
        # would look like the best choice on a 6GB card. It cannot be loaded,
        # so it must never be picked.
        ("Qwen2.5-Coder-32B-Instruct-Q3_K_S-00001-of-00002.gguf", 6_000_000_000),
    ]),
    "TheBloke/CodeLlama-13B-Instruct-GGUF": _fixture_tree([
        ("codellama-13b-instruct.Q3_K_M.gguf", 6_337_769_472),
        ("codellama-13b-instruct.Q4_K_M.gguf", 7_866_070_016),
    ]),
    "nebula/Nebula-Mix-9B-GGUF": _fixture_tree([
        ("Nebula-Mix-9B-Q4_K_M.gguf", 5_500_000_000),
    ]),
    "mystery/Mystery-Model-GGUF": _fixture_tree([
        ("Mystery-Model-Q4_K_M.gguf", 4_683_073_344),
    ]),
    # The real thing, captured from the live Hub. Every path and every byte
    # below is what Qwen's own repository actually contains, because the
    # duplicate-editions bug this pins survived a fixture that had one
    # hand-written split twin in it: a fixture where every quantisation is
    # simple except one cannot show that a listing is systematically twice as
    # long as it should be. Note the SHAPE, which is what makes it a test:
    #   Q4_0, Q4_K_M, Q5_K_M, Q6_K, Q8_0, FP16 - both editions
    #   Q2_K, Q3_K_M                           - single file only
    #   Q5_0                                   - SPLIT ONLY, no single file,
    #                                            so a rule that just drops
    #                                            splits would lose it entirely
    # and that the splits are 2, 3, and 4 parts, so part counts cannot be
    # assumed.
    _EDITION_REPO_ID: _fixture_tree([
        ("qwen2.5-coder-7b-instruct-q2_k.gguf", 3_015_940_032),
        ("qwen2.5-coder-7b-instruct-q3_k_m.gguf", 3_808_391_104),
        ("qwen2.5-coder-7b-instruct-q4_0.gguf", 4_431_390_720),
        ("qwen2.5-coder-7b-instruct-q4_0-00001-of-00002.gguf", 3_983_228_352),
        ("qwen2.5-coder-7b-instruct-q4_0-00002-of-00002.gguf", 448_162_496),
        ("qwen2.5-coder-7b-instruct-q4_k_m.gguf", 4_683_073_536),
        ("qwen2.5-coder-7b-instruct-q4_k_m-00001-of-00002.gguf", 3_993_201_376),
        ("qwen2.5-coder-7b-instruct-q4_k_m-00002-of-00002.gguf", 689_872_288),
        ("qwen2.5-coder-7b-instruct-q5_0-00001-of-00002.gguf", 4_001_112_160),
        ("qwen2.5-coder-7b-instruct-q5_0-00002-of-00002.gguf", 1_314_064_416),
        ("qwen2.5-coder-7b-instruct-q5_k_m.gguf", 5_444_831_232),
        ("qwen2.5-coder-7b-instruct-q5_k_m-00001-of-00002.gguf", 3_989_841_792),
        ("qwen2.5-coder-7b-instruct-q5_k_m-00002-of-00002.gguf", 1_454_989_568),
        ("qwen2.5-coder-7b-instruct-q6_k.gguf", 6_254_198_784),
        ("qwen2.5-coder-7b-instruct-q6_k-00001-of-00002.gguf", 3_950_642_496),
        ("qwen2.5-coder-7b-instruct-q6_k-00002-of-00002.gguf", 2_303_556_416),
        ("qwen2.5-coder-7b-instruct-q8_0.gguf", 8_098_525_184),
        ("qwen2.5-coder-7b-instruct-q8_0-00001-of-00003.gguf", 3_980_069_280),
        ("qwen2.5-coder-7b-instruct-q8_0-00002-of-00003.gguf", 3_942_935_680),
        ("qwen2.5-coder-7b-instruct-q8_0-00003-of-00003.gguf", 175_520_480),
        ("qwen2.5-coder-7b-instruct-fp16.gguf", 15_237_853_184),
        ("qwen2.5-coder-7b-instruct-fp16-00001-of-00004.gguf", 3_951_521_376),
        ("qwen2.5-coder-7b-instruct-fp16-00002-of-00004.gguf", 3_864_909_312),
        ("qwen2.5-coder-7b-instruct-fp16-00003-of-00004.gguf", 3_864_894_976),
        ("qwen2.5-coder-7b-instruct-fp16-00004-of-00004.gguf", 3_556_527_872),
    ]),
}

# What the live repository really publishes: quantisation label -> (how many
# editions of it exist, how many parts the split edition has, or None when
# there is no split edition). Written out separately from the file list above
# so the expectation is stated rather than derived from the same data it is
# checking.
_EDITION_REPO_SHAPE = {
    "Q2_K": (1, None),
    "Q3_K_M": (1, None),
    "Q4_0": (2, 2),
    "Q4_K_M": (2, 2),
    "Q5_0": (1, 2),
    "Q5_K_M": (2, 2),
    "Q6_K": (2, 2),
    "Q8_0": (2, 3),
    "FP16": (2, 4),
}


def _edition_search_fn(query, limit=DEFAULT_SEARCH_LIMIT, timeout=None):
    """A one-result search onto _EDITION_REPO_ID, so the same repository can
    be checked through search_shop() and through repo_quants().
    """
    return {
        "ok": True, "error": None, "error_kind": None, "query": query,
        "models": [{"repo_id": _EDITION_REPO_ID, "author": "Qwen",
                    "downloads": 123456, "likes": 300,
                    "last_modified": "2024-11-12T00:00:00.000Z",
                    "gated": False, "gated_mode": None, "private": False,
                    "tags": ["gguf", "code"],
                    "url": "https://huggingface.co/" + _EDITION_REPO_ID}][:limit],
        "rate_limit": None,
        "url": "https://huggingface.co/api/models?search=" + str(query),
    }


_FIXTURE_SEARCH_MODELS = [
    {"repo_id": "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF", "author": "bartowski",
     "downloads": 214637, "likes": 219, "last_modified": "2024-11-09T12:45:18.000Z",
     "gated": False, "gated_mode": None, "private": False,
     "tags": ["transformers", "gguf", "code"],
     "url": "https://huggingface.co/bartowski/Qwen2.5-Coder-7B-Instruct-GGUF"},
    {"repo_id": "bartowski/Qwen2.5-Coder-32B-Instruct-GGUF", "author": "bartowski",
     "downloads": 190000, "likes": 400, "last_modified": "2024-11-12T09:00:00.000Z",
     "gated": False, "gated_mode": None, "private": False,
     "tags": ["gguf", "code"],
     "url": "https://huggingface.co/bartowski/Qwen2.5-Coder-32B-Instruct-GGUF"},
    {"repo_id": "TheBloke/CodeLlama-13B-Instruct-GGUF", "author": "TheBloke",
     "downloads": 90000, "likes": 180, "last_modified": "2023-09-01T00:00:00.000Z",
     "gated": False, "gated_mode": None, "private": False, "tags": ["gguf"],
     "url": "https://huggingface.co/TheBloke/CodeLlama-13B-Instruct-GGUF"},
    # Gated: listed by search, refused by the tree endpoint for an anonymous
    # client, exactly as the live Hub behaves.
    {"repo_id": "meta-llama/Llama-3.1-70B-Instruct-GGUF", "author": "meta-llama",
     "downloads": 7948428, "likes": 4000, "last_modified": "2024-08-03T22:11:58.000Z",
     "gated": True, "gated_mode": "manual", "private": False, "tags": ["gguf"],
     "url": "https://huggingface.co/meta-llama/Llama-3.1-70B-Instruct-GGUF"},
    {"repo_id": "nebula/Nebula-Mix-9B-GGUF", "author": "nebula",
     "downloads": 120, "likes": 2, "last_modified": "2025-01-01T00:00:00.000Z",
     "gated": False, "gated_mode": None, "private": False, "tags": ["gguf"],
     "url": "https://huggingface.co/nebula/Nebula-Mix-9B-GGUF"},
    {"repo_id": "mystery/Mystery-Model-GGUF", "author": "mystery",
     "downloads": 30, "likes": 0, "last_modified": "2025-02-01T00:00:00.000Z",
     "gated": False, "gated_mode": None, "private": False, "tags": ["gguf"],
     "url": "https://huggingface.co/mystery/Mystery-Model-GGUF"},
]


def _fixture_search_fn(query, limit=DEFAULT_SEARCH_LIMIT, timeout=None):
    return {
        "ok": True, "error": None, "error_kind": None, "query": query,
        "models": _FIXTURE_SEARCH_MODELS[:limit],
        "rate_limit": {"remaining": 499, "reset_seconds": 121},
        "url": "https://huggingface.co/api/models?search=" + str(query),
    }


def _fixture_list_fn(repo_id, timeout=None):
    hf = _hf()
    if repo_id not in _FIXTURE_REPO_FILES:
        return {
            "ok": False,
            "error": "{} is gated on Hugging Face.".format(repo_id),
            "error_kind": hf.ERR_GATED,
            "repo_id": repo_id, "files": [], "file_count": 0,
        }
    files = hf.group_gguf_files(_FIXTURE_REPO_FILES[repo_id])
    return {
        "ok": True, "error": None, "error_kind": None, "repo_id": repo_id,
        "revision": "main", "files": files, "file_count": len(files),
        "rate_limit": None, "url": hf.tree_url(repo_id),
    }


def _self_test_hf(_hw):
    """The Hugging Face half of the self-test. Offline throughout: every Hub
    call goes through the search_fn/list_fn seam onto the fixtures above.
    """
    # -- parameter counts out of names ---------------------------------------
    assert parse_params_b("bartowski/Qwen2.5-Coder-7B-Instruct-GGUF") == 7.0
    assert parse_params_b("Qwen2.5-Coder-1.5B-Instruct-Q4_K_M.gguf") == 1.5
    assert parse_params_b("Llama-3.2-1B-Instruct") == 1.0
    assert parse_params_b("Meta-Llama-3-70B-Instruct.Q8_0.00001-of-00002.gguf") == 70.0
    # Mixture of experts: 8x7B is 56B of weights to hold in memory, which is
    # the number this module has to decide against, not the 7B expert size.
    assert parse_params_b("mistralai/Mixtral-8x7B-Instruct-v0.1-GGUF") == 56.0
    # "A3B" is Qwen3-30B-A3B's ACTIVE parameter count. Reading it as the
    # model's size would understate a 30B model by a factor of ten.
    assert parse_params_b("Qwen3-30B-A3B-Instruct") == 30.0
    # Names that state no parameter count must say so, not produce a number.
    assert parse_params_b("tinyllamas/stories260K.gguf") is None
    assert parse_params_b("mystery/Mystery-Model-GGUF") is None
    assert parse_params_b("Qwen2.5-Coder-7B-Instruct-BF16.gguf") == 7.0, (
        "BF16 must not be misread as a parameter count"
    )
    assert parse_params_b("") is None and parse_params_b(None) is None

    # -- parameter counts out of a file size ---------------------------------
    # 4.68GB of Q4_K_M at 4.83 bits per weight is a 7B-class model.
    est = params_from_size(4_683_073_344, "Q4_K_M")
    assert est is not None and 7.0 <= est <= 8.5, est
    assert params_from_size(4_683_073_344, "q4_k_m") == est, "quant label is case-insensitive"
    assert params_from_size(4_683_073_344, "APEX-Compact") is None, (
        "an unrecognised quant label must produce no estimate, not a default one"
    )
    assert params_from_size(None, "Q4_K_M") is None
    assert params_from_size(0, "Q4_K_M") is None

    # -- KV_FAMILIES: the arithmetic must reproduce its own stated numbers ---
    seen_families = set()
    for family in KV_FAMILIES:
        assert family["family"] not in seen_families, family["family"]
        seen_families.add(family["family"])
        assert family["sizes"], family["family"]
        for size in family["sizes"]:
            calc = size["kv_calc"]
            computed = (2 * calc["layers"] * calc["kv_heads"] * calc["head_dim"]
                        * calc["bytes_per_element"])
            assert computed == size["kv_bytes_per_token"], (family["family"], size)
            assert calc["attention"] in (_ATTENTION_MHA, _ATTENTION_GQA), size
            assert size["kv_confidence"] in (
                "published_config", "recalled_estimate", "conservative_overestimate",
            ), size

    # THE CLAIM THE HEURISTIC MAKES: it never under-counts a plain
    # multi-head-attention model. Under-counting is the one failure mode that
    # produces a confident "it fits" that is wrong on the user's machine, so
    # this is pinned against every MHA architecture the table knows.
    for family in KV_FAMILIES:
        for size in family["sizes"]:
            if size["kv_calc"]["attention"] != _ATTENTION_MHA:
                continue
            assert _kv_heuristic_bytes(size["params_b"]) >= size["kv_bytes_per_token"], (
                "the size heuristic must never under-count a multi-head-attention "
                "model; that is the failure mode that produces a wrong 'it fits'",
                family["family"], size,
            )

    # -- every CATALOG entry must resolve through KV_FAMILIES to its own -----
    # -- vetted KV number. Two tables of architecture facts that disagree ----
    # -- would mean the same model is graded differently depending on whether
    # -- it arrived from the Hub or from the built-in list.
    for model in CATALOG:
        kv = kv_for_model(model["id"])
        assert kv["kv_source"] == "family", (model["id"], kv)
        assert kv["kv_bytes_per_token"] == model["kv_bytes_per_token"], (
            "KV_FAMILIES and CATALOG must agree about the same model",
            model["id"], kv["kv_bytes_per_token"], model["kv_bytes_per_token"],
        )
        assert abs(kv["params_b"] - model["params_b"]) < 0.001, (model["id"], kv)

    # A real Hub repo name for a cataloged model must resolve the same way.
    kv = kv_for_model("bartowski/Qwen2.5-Coder-7B-Instruct-GGUF")
    assert kv["kv_source"] == "family" and kv["kv_family"] == "qwen2.5", kv
    assert kv["kv_bytes_per_token"] == 57_344, kv

    # A size the family table does not cover falls through to the heuristic
    # rather than borrowing the nearest listed size's numbers.
    kv = kv_for_model("Qwen2.5-Coder-20B-Instruct-GGUF")
    assert kv["kv_source"] == "size_heuristic", kv
    assert kv["kv_family"] is None, kv

    # An unknown family with a stated size: heuristic, flagged conservative.
    kv = kv_for_model("nebula/Nebula-Mix-9B-GGUF")
    assert kv["kv_source"] == "size_heuristic", kv
    assert kv["kv_confidence"] == "conservative_overestimate", kv
    assert kv["params_source"] == "name", kv

    # Nothing at all: the fixed conservative stand-in, and it must SAY it is
    # a guess rather than present itself as a reading.
    kv = kv_for_model("mystery/Mystery-Model-GGUF")
    assert kv["kv_source"] == "unknown_default", kv
    assert kv["kv_confidence"] == "unknown", kv
    assert kv["kv_bytes_per_token"] == UNKNOWN_KV_BYTES_PER_TOKEN, kv

    # Same repo, but now with a file to estimate from: the size route fires.
    hf = _hf()
    mystery_files = hf.group_gguf_files(_FIXTURE_REPO_FILES["mystery/Mystery-Model-GGUF"])
    kv = kv_for_repo("mystery/Mystery-Model-GGUF", mystery_files)
    assert kv["params_source"] == "file_size", kv
    assert kv["kv_source"] == "size_heuristic", kv

    # -- the calibration machines --------------------------------------------
    # RTX 2060, 6GB, the project's Linux host. Every number below was worked
    # out by hand against these fixtures; see the comments for the arithmetic.
    hw_2060 = _hw(6_442_450_944, 16 * 1024 ** 3)
    hw_5080 = _hw(17_094_934_528, 33 * 1024 ** 3)

    listing = search_shop(
        "qwen coder", hw=hw_2060, context_tokens=8192, detail_limit=10,
        search_fn=_fixture_search_fn, list_fn=_fixture_list_fn,
    )
    assert listing["ok"] is True, listing
    assert listing["source"] == SOURCE_LIVE, listing
    assert listing["notice"] is None, listing
    assert listing["model_count"] == len(_FIXTURE_SEARCH_MODELS), listing["model_count"]
    by_repo = {m["repo_id"]: m for m in listing["models"]}

    # -- CRITICAL: the fit verdict is PER QUANTISATION -----------------------
    # One repository, seven graded quantisations, and on a 6GB card they land
    # in four different verdict tiers. A repository-level verdict cannot say
    # anything true here, which is the whole reason this module grades quants.
    qwen7 = by_repo["bartowski/Qwen2.5-Coder-7B-Instruct-GGUF"]
    assert qwen7["quants_loaded"] is True, qwen7
    assert qwen7["kv_bytes_per_token"] == 57_344, qwen7
    tiers = {q["quant"]: q["verdict"]["verdict"] for q in qwen7["quants"] if q["verdict"]}
    # KV cache at 8192 tokens is 57344 * 8192 = 469,762,048 bytes.
    #   Q2_K   3.18GB + 0.47GB = 3.65GB, 2.79GB spare of 6.00GB -> great
    #   Q4_K_M 4.68GB + 0.47GB = 5.15GB, 1.29GB spare           -> great
    #   Q5_K_M 5.44GB + 0.47GB = 5.91GB, 0.53GB spare (8%)      -> good
    #   Q6_K   6.25GB weights alone fit, but not with the cache  -> reduced
    #   Q8_0   8.10GB weights alone exceed the card, fits RAM    -> spillover
    #   f16   15.24GB + 0.47GB exceeds the RAM budget too        -> wont_fit
    assert tiers["Q2_K"] == VERDICT_GREAT, tiers
    assert tiers["Q5_K_M"] == VERDICT_GOOD, tiers
    assert tiers["Q6_K"] == VERDICT_REDUCED_CONTEXT, tiers
    assert tiers["Q8_0"] == VERDICT_CPU_SPILLOVER, tiers
    assert tiers["F16"] == VERDICT_WONT_FIT, tiers
    assert len(set(tiers.values())) >= 4, (
        "one repository's quantisations must be able to land in different "
        "verdict tiers; if they all agree, the per-quant grading is not "
        "actually doing anything", tiers,
    )

    # The KV cache is what separates Q6_K from a naive size comparison: its
    # weights alone DO fit the card, and it still does not fit at 8192
    # tokens. This is the exact mistake a file-size-against-VRAM check makes.
    q6 = next(q for q in qwen7["quants"] if q["quant"] == "Q6_K")
    assert q6["size_bytes"] < hw_2060["gpus"][0]["vram_bytes"], (
        "fixture is wrong: Q6_K's weights must fit the card on size alone, "
        "or this test proves nothing about the KV cache", q6["size_bytes"],
    )
    assert q6["verdict"]["verdict"] == VERDICT_REDUCED_CONTEXT, q6["verdict"]
    assert q6["verdict"]["required_bytes"] == q6["size_bytes"] + 57_344 * q6["verdict"][
        "max_context_tokens"], q6["verdict"]

    # -- CRITICAL: which quantisation the shop offers ------------------------
    # The largest one that still fits fully in VRAM at the requested context.
    # Not the first listed, not the smallest, not the biggest in the repo.
    best = qwen7["best_quant"]
    assert best is not None, qwen7
    assert best["quant"] == "Q4_K_M", (
        "the shop must offer the largest quantisation in the BEST VERDICT "
        "TIER: Q5_K_M is the larger file but only earns 'good' (0.53GB "
        "spare), while Q4_K_M earns 'great' (1.29GB spare)",
        best["quant"], tiers,
    )
    # Both halves of that rule, pinned separately so neither can rot alone.
    great = [q for q in qwen7["quants"]
             if q["verdict"] and q["verdict"]["verdict"] == VERDICT_GREAT
             and not q["projector"]]
    assert best["size_bytes"] == max(q["size_bytes"] for q in great), (
        "within the winning tier, the largest quantisation wins", best["quant"],
    )
    bigger_that_also_runs = [
        q for q in qwen7["quants"]
        if q["verdict"] and q["verdict"]["verdict"] == VERDICT_GOOD
        and q["size_bytes"] > best["size_bytes"]
    ]
    assert bigger_that_also_runs, (
        "fixture is wrong: there must be a LARGER quantisation that still "
        "fits, or this proves nothing about tier beating size",
    )
    # The winning quant must be the SINGLE-FILE Q4_K_M, not the split
    # edition of the same quant that this repository also ships a hair
    # larger. Ranking on size alone would take the split one.
    assert best["multipart"] is False, (
        "when a repository ships the same quantisation as one file and as a "
        "split, the single file is the better download", best["name"],
    )
    assert [q["name"] for q in qwen7["quants"] if q["quant"] == "Q4_K_M"] == [best["name"]], (
        "the two editions of Q4_K_M must be ONE row, not two rows that look "
        "identical to a user", [q["name"] for q in qwen7["quants"]],
    )
    assert len(best["alternate_editions"]) == 1, best["alternate_editions"]
    split_twin = best["alternate_editions"][0]
    assert split_twin["multipart"] is True and split_twin["quant"] == "Q4_K_M", split_twin
    assert split_twin["size_bytes"] > best["size_bytes"], (
        "fixture must make the split edition rank first on size, or this "
        "proves nothing", split_twin["size_bytes"], best["size_bytes"],
    )
    assert split_twin["verdict"]["verdict"] == best["verdict"]["verdict"], split_twin
    assert best["split_part_count"] == split_twin["part_count"] == 2, best
    assert split_twin["recommended"] is False, split_twin
    # The split edition is not thrown away: it is still fully graded, and
    # still carries everything needed to download it, for the user whose
    # filesystem will not take one enormous file.
    assert split_twin["disk_ok"] is not None and split_twin["parts"], split_twin

    # Preferring the single-file edition must never cost a verdict tier. A
    # repository can ship a split that lands in "great" and a single file of
    # the same quantisation, a little larger, that only lands in "good";
    # swapping to the single file there would be a downgrade dressed up as a
    # convenience.
    def _edition(name, size, multipart):
        return {"name": name, "quant": "Q4_K_M", "size_bytes": size,
                "multipart": multipart, "complete": True, "part_count": 2 if multipart else 1,
                "verdict": verdict_for(
                    {"size_bytes": size, "kv_bytes_per_token": 57_344},
                    hw_2060, context_tokens=8192)}

    # Same stem, so these two really are editions of one another by
    # _edition_id's rule; only the verdict separates them.
    split_great = _edition("tier-guard-Q4_K_M", 4_600_000_000, True)
    single_good = _edition("tier-guard-Q4_K_M.gguf", 5_200_000_000, False)
    assert _edition_id(split_great) == _edition_id(single_good), (
        "fixture is wrong: these must be editions of the same quantisation, "
        "or the rule under test never fires",
        _edition_id(split_great), _edition_id(single_good),
    )
    assert split_great["verdict"]["verdict"] == VERDICT_GREAT, split_great["verdict"]
    assert single_good["verdict"]["verdict"] == VERDICT_GOOD, single_good["verdict"]
    assert best_quant([split_great, single_good])["name"] == "tier-guard-Q4_K_M", (
        "the single-file preference only applies at an equal verdict; it "
        "must never trade a great down to a good",
        best_quant([split_great, single_good])["name"],
    )
    # ... and the listing must survive with the same edition the offer chose,
    # not with the other one. This is the coincidence-versus-construction
    # point: both answers come out of _edition_groups.
    tier_guard = quant_verdicts(
        [split_great, single_good], hw_2060, context_tokens=8192,
        kv={"kv_bytes_per_token": 57_344, "kv_confidence": "published_config"},
        free_disk=10 ** 15,
    )
    assert [q["name"] for q in tier_guard] == ["tier-guard-Q4_K_M"], (
        "the listing must keep the edition in the better tier, exactly as "
        "best_quant does", [q["name"] for q in tier_guard],
    )
    assert [a["name"] for a in tier_guard[0]["alternate_editions"]] == [
        "tier-guard-Q4_K_M.gguf"], tier_guard[0]["alternate_editions"]

    # An entry with no name is never folded into another on no evidence.
    nameless = [{"name": None, "quant": "Q4_K_M", "size_bytes": 1_000_000_000,
                 "multipart": False, "complete": True},
                {"name": "", "quant": "Q4_K_M", "size_bytes": 2_000_000_000,
                 "multipart": False, "complete": True}]
    assert len(_edition_groups(nameless)) == 2, _edition_groups(nameless)

    # Two genuinely different builds can share one quantisation label, and
    # they are two real choices rather than one duplicated. This is why
    # editions are matched on the filename and not on the label.
    assert hf.parse_quant("foo-Q4_K_M.gguf") == hf.parse_quant("foo-i1-Q4_K_M.gguf") == "Q4_K_M"
    imatrix = [{"name": "foo-Q4_K_M.gguf", "path": "foo-Q4_K_M.gguf", "quant": "Q4_K_M",
                "size_bytes": 4_000_000_000, "multipart": False, "complete": True},
               {"name": "foo-i1-Q4_K_M.gguf", "path": "foo-i1-Q4_K_M.gguf", "quant": "Q4_K_M",
                "size_bytes": 4_100_000_000, "multipart": False, "complete": True}]
    assert len(_edition_groups(imatrix)) == 2, _edition_groups(imatrix)

    # -- CRITICAL: one row per quantisation, against the REAL repository -----
    # Everything above is a fixture with one hand-placed split twin in it, and
    # a listing can pass all of it while still showing the user every
    # quantisation twice: a repository where one quant is duplicated and a
    # repository where nearly all of them are look the same to a test that
    # only ever asks about one quant. _EDITION_REPO_ID is Qwen's own
    # repository, file for file and byte for byte off the live Hub, where six
    # quantisations ship in two editions, two ship as a single file only, and
    # one ships ONLY as a split.
    raw_files = _fixture_list_fn(_EDITION_REPO_ID)["files"]
    assert len(raw_files) == sum(count for count, _ in _EDITION_REPO_SHAPE.values()) == 15, (
        "fixture is wrong: the repository must really publish more files than "
        "it has quantisations, or the collapse below is untested", len(raw_files),
    )

    detailed = repo_quants(_EDITION_REPO_ID, hw=hw_5080, context_tokens=8192,
                           list_fn=_fixture_list_fn)
    assert detailed["ok"] is True, detailed
    rows = detailed["model"]["quants"]
    labels = [q["quant"] for q in rows]
    assert len(labels) == len(set(labels)), (
        "every quantisation must appear EXACTLY ONCE in the listing; two rows "
        "for one quantisation differ only in how the download arrives and "
        "read to a user as a bug", labels,
    )
    assert sorted(labels) == sorted(_EDITION_REPO_SHAPE), labels
    assert detailed["model"]["quant_count"] == len(rows) == 9, detailed["model"]["quant_count"]

    by_quant = {q["quant"]: q for q in rows}
    for label, (editions, split_parts) in sorted(_EDITION_REPO_SHAPE.items()):
        row = by_quant[label]
        assert len(row["alternate_editions"]) == editions - 1, (label, row)
        assert row["split_part_count"] == split_parts, (label, row["split_part_count"])
        if editions == 1:
            continue
        assert row["multipart"] is False, (
            "where a repository publishes a quantisation both whole and "
            "split, the surviving row is the single file", label, row["name"],
        )
        alternate = row["alternate_editions"][0]
        assert alternate["multipart"] is True and alternate["quant"] == label, (label, alternate)
        assert alternate["verdict"]["verdict"] == row["verdict"]["verdict"], (
            "the two editions of one quantisation are the same weights and "
            "must land in the same tier; if they do not, collapsing them is "
            "hiding a different answer rather than a duplicate", label,
        )

    # A rule that simply dropped split entries would lose Q5_0 entirely: this
    # repository publishes it in no other form.
    q5_0 = by_quant["Q5_0"]
    assert q5_0["multipart"] is True and q5_0["part_count"] == 2, q5_0
    assert q5_0["alternate_editions"] == [], q5_0

    # Nothing is silently discarded: every logical file the Hub reported is
    # still reachable, as a row or as that row's alternate edition.
    reachable = {q["name"] for q in rows}
    reachable |= {a["name"] for q in rows for a in q["alternate_editions"]}
    assert reachable == {f["name"] for f in raw_files}, (
        sorted(reachable ^ {f["name"] for f in raw_files}),
    )

    # The offer is one of the rows on screen, by identity, not a lookalike.
    offer = detailed["model"]["best_quant"]
    assert any(q is offer for q in rows), (offer or {}).get("name")
    assert offer["quant"] == "Q8_0" and offer["multipart"] is False, offer["name"]
    assert sum(1 for q in rows if q["recommended"]) == 1, rows

    # search_shop() builds its entries down the same path, so it must produce
    # the same rows. A fix that reached only one of the two would leave a UI
    # showing duplicates on the search screen and not on the detail screen.
    searched = search_shop("qwen coder", hw=hw_5080, context_tokens=8192, detail_limit=1,
                           search_fn=_edition_search_fn, list_fn=_fixture_list_fn)
    assert searched["ok"] is True, searched
    shape = [(q["name"], q["quant"], q["split_part_count"],
              [a["name"] for a in q["alternate_editions"]]) for q in rows]
    assert [(q["name"], q["quant"], q["split_part_count"],
             [a["name"] for a in q["alternate_editions"]])
            for q in searched["models"][0]["quants"]] == shape, (
        "search_shop() and repo_quants() must agree on the listing",
        searched["models"][0]["quants"],
    )
    assert searched["models"][0]["best_quant"]["name"] == offer["name"], searched["models"][0]

    # rank_quants() and best_quant() are public and must do their own
    # ordering: the Hub returns files in its own order, and a caller can hand
    # them an unsorted list. Feeding them a deliberately reversed list must
    # not change either answer. (Without this, best_quant() passing its input
    # straight through goes unnoticed, because quant_verdicts() happens to
    # hand it an already-ranked list.)
    shuffled = list(reversed(qwen7["quants"]))
    assert [q["name"] for q in rank_quants(shuffled)] == [q["name"] for q in qwen7["quants"]], (
        "rank_quants must impose its own order, not preserve the input's",
    )
    assert best_quant(shuffled)["quant"] == best["quant"], (
        "best_quant must rank its input rather than trust the order it "
        "arrived in", best_quant(shuffled)["quant"], best["quant"],
    )

    assert qwen7["verdict"] is best["verdict"], (
        "a repository's headline verdict is its chosen quantisation's "
        "verdict, not a verdict of its own", qwen7["verdict"],
    )
    assert sum(1 for q in qwen7["quants"] if q.get("recommended")) == 1, qwen7["quants"]

    # At a longer context the same repository must offer a SMALLER
    # quantisation, because the KV cache grows and pushes Q5_K_M out. A shop
    # that ignored context would answer identically at both lengths.
    listing_32k = search_shop(
        "qwen coder", hw=hw_2060, context_tokens=32768, detail_limit=1,
        search_fn=_fixture_search_fn, list_fn=_fixture_list_fn,
    )
    best_32k = listing_32k["models"][0]["best_quant"]
    assert best_32k is not None, listing_32k["models"][0]
    assert best_32k["size_bytes"] < best["size_bytes"], (
        "a longer context must force a smaller quantisation; if it does not, "
        "the KV cache is not being counted",
        best_32k["quant"], best["quant"],
    )

    # A stronger card must be offered at least as large a quantisation as a
    # weaker one for the same repository. Backwards would be absurd.
    listing_5080 = search_shop(
        "qwen coder", hw=hw_5080, context_tokens=8192, detail_limit=1,
        search_fn=_fixture_search_fn, list_fn=_fixture_list_fn,
    )
    best_5080 = listing_5080["models"][0]["best_quant"]
    assert best_5080["size_bytes"] >= best["size_bytes"], (best_5080["quant"], best["quant"])

    # -- the quant with no parseable label is still listed, honestly ---------
    mmproj = next(q for q in qwen7["quants"] if q["name"].startswith("mmproj"))
    assert mmproj["quant"] is None, mmproj
    assert mmproj["projector"] is True, mmproj
    assert mmproj["verdict"]["verdict"] == VERDICT_GREAT, (
        "fixture must make the projector look attractive, or the guard below "
        "proves nothing", mmproj["verdict"],
    )
    assert mmproj is not best, "a vision projector must not be offered as the model to run"
    assert best_quant([mmproj]) is None, (
        "a vision projector is the image encoder that rides alongside a "
        "model, not a model. It must not be offered even when it is the only "
        "thing on the machine that fits.", best_quant([mmproj]),
    )

    # -- multi-part models are one choice, summed ----------------------------
    qwen32 = by_repo["bartowski/Qwen2.5-Coder-32B-Instruct-GGUF"]
    split = next(q for q in qwen32["quants"] if q["multipart"])
    assert split["part_count"] == 2 and split["complete"] is True, split
    assert split["size_bytes"] == 17_000_000_000 + 17_800_000_000, split
    assert split["verdict"]["verdict"] == VERDICT_WONT_FIT, split["verdict"]
    # Graded on the SUM, not on one part. Grading a split on a single part
    # would make a 34GB model look like a 17GB one, which is the worst
    # possible direction for this module to be wrong in.
    assert split["verdict"]["required_bytes"] == (
        split["size_bytes"] + qwen32["kv_bytes_per_token"] * 8192
    ), (split["verdict"]["required_bytes"], split["size_bytes"])

    # Nothing in the 32B repo fits a 6GB card in VRAM, so the preference
    # flips: the SMALLEST option wins, not the largest.
    best32 = qwen32["best_quant"]
    assert best32["quant"] == "IQ2_M", (
        "when nothing fits in VRAM the shop must offer the smallest option, "
        "which spills least", best32["quant"],
    )
    assert best32["verdict"]["verdict"] == VERDICT_CPU_SPILLOVER, best32["verdict"]

    # An incomplete split cannot be loaded at all, so it is never the answer,
    # however well its present bytes would have fitted. The fixture is sized
    # so it WOULD have won on fit alone, which is what makes this a test.
    broken = next(q for q in qwen32["quants"] if not q["complete"])
    assert broken["multipart"] is True and broken["missing_parts"] == [2], broken
    assert broken["size_bytes"] < best32["size_bytes"], (
        "fixture must make the incomplete split look like the better choice",
        broken["size_bytes"], best32["size_bytes"],
    )
    assert broken["verdict"]["verdict"] == VERDICT_CPU_SPILLOVER, broken["verdict"]
    assert best_quant([broken]) is None, (
        "a split model missing a part is not downloadable into a working "
        "model and must never be offered", best_quant([broken]),
    )

    # -- a 13B multi-head-attention model on a 6GB card ----------------------
    # Its KV cache alone (819200 * 8192 = 6.7GB) is larger than the whole
    # card. A shop reasoning about file sizes would call Q3_K_M (6.34GB) a
    # fit; it is not one.
    codellama = by_repo["TheBloke/CodeLlama-13B-Instruct-GGUF"]
    assert codellama["kv_bytes_per_token"] == 819_200, codellama
    q3 = next(q for q in codellama["quants"] if q["quant"] == "Q3_K_M")
    assert q3["size_bytes"] < hw_2060["gpus"][0]["vram_bytes"], q3["size_bytes"]
    assert q3["verdict"]["verdict"] == VERDICT_CPU_SPILLOVER, q3["verdict"]

    # -- disk space is an independent check on live quantisations too -------
    # A user with 1KB free cannot install a 5GB model however comfortably it
    # would have fitted in VRAM, and finding that out at 90% of a download is
    # the experience this check exists to prevent.
    old_free = globals()["free_disk_bytes"]
    try:
        globals()["free_disk_bytes"] = lambda path=None: 1024
        starved = search_shop("qwen coder", hw=hw_2060, context_tokens=8192, detail_limit=1,
                              search_fn=_fixture_search_fn, list_fn=_fixture_list_fn)
        starved_quants = [q for q in starved["models"][0]["quants"] if q["verdict"]]
        assert starved_quants, starved["models"][0]
        assert all(q["disk_ok"] is False for q in starved_quants), starved_quants
        # The fit verdict must be untouched by the disk reading: they are two
        # independent facts, and collapsing them would hide one of them.
        assert starved["models"][0]["best_quant"]["quant"] == best["quant"], (
            "a full disk must not change which quantisation fits",
            starved["models"][0]["best_quant"]["quant"],
        )
        globals()["free_disk_bytes"] = lambda path=None: 10 ** 15
        roomy = search_shop("qwen coder", hw=hw_2060, context_tokens=8192, detail_limit=1,
                            search_fn=_fixture_search_fn, list_fn=_fixture_list_fn)
        roomy_quants = [q for q in roomy["models"][0]["quants"] if q["verdict"]]
        assert all(q["disk_ok"] is True for q in roomy_quants), roomy_quants
    finally:
        globals()["free_disk_bytes"] = old_free

    # -- one bad repository degrades to one explained row --------------------
    gated = by_repo["meta-llama/Llama-3.1-70B-Instruct-GGUF"]
    assert gated["gated"] is True and gated["gated_mode"] == "manual", gated
    assert gated["downloadable"] is False, gated
    assert gated["files_error_kind"] == hf.ERR_GATED, gated
    assert gated["quants_loaded"] is False and gated["quants"] == [], gated
    assert gated["verdict"] is None, (
        "a repository whose files could not be listed has no verdict; "
        "inventing one would be a claim with nothing behind it", gated,
    )
    assert listing["ok"] is True, "one gated repository must not fail the whole search"

    # -- ordering: runnable first, unrunnable last but still present ---------
    ranks = []
    for entry in listing["models"]:
        ranks.append(_shop_entry_sort_key(entry))
    assert ranks == sorted(ranks), [m["repo_id"] for m in listing["models"]]
    assert listing["models"][0]["repo_id"] == "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF", (
        "on a 6GB card the 7B repo is the only one with a great-or-good "
        "quantisation, so it must lead", [m["repo_id"] for m in listing["models"]],
    )

    # -- detail_limit: unfetched repositories say so rather than look empty --
    partial = search_shop(
        "qwen coder", hw=hw_2060, context_tokens=8192, detail_limit=2,
        search_fn=_fixture_search_fn, list_fn=_fixture_list_fn,
    )
    loaded = [m for m in partial["models"] if m["quants_loaded"]]
    assert len(loaded) == 2, [m["repo_id"] for m in loaded]
    for entry in partial["models"]:
        if entry["quants_loaded"]:
            continue
        assert entry["verdict"] is None and entry["files_error"] is None, entry

    # repo_quants() fills one of those in on demand, with the same shape.
    filled = repo_quants(
        "TheBloke/CodeLlama-13B-Instruct-GGUF", hw=hw_2060, context_tokens=8192,
        list_fn=_fixture_list_fn,
    )
    assert filled["ok"] is True, filled
    assert filled["model"]["quants_loaded"] is True, filled
    assert filled["model"]["kv_bytes_per_token"] == codellama["kv_bytes_per_token"], filled

    failed = repo_quants("meta-llama/Llama-3.1-70B-Instruct-GGUF", hw=hw_2060,
                         context_tokens=8192, list_fn=_fixture_list_fn)
    assert failed["ok"] is False and failed["error_kind"] == hf.ERR_GATED, failed
    assert failed["model"]["files_error"], failed

    # -- offline: a clear structured error, never an exception or a blank ----
    def dead_search(query, limit=None, timeout=None):
        return {"ok": False, "error": "Could not reach Hugging Face: URLError: [Errno 11001]",
                "error_kind": hf.ERR_UNREACHABLE, "models": [], "rate_limit": None,
                "url": "https://huggingface.co/api/models"}

    offline = search_shop("qwen coder", hw=hw_2060, context_tokens=8192,
                          search_fn=dead_search, list_fn=_fixture_list_fn)
    assert offline["ok"] is False, (
        "a fallback listing must never report ok True; that flag is the "
        "guarantee a caller ignoring 'source' still cannot mistake the "
        "built-in list for live Hub results", offline["ok"],
    )
    assert offline["source"] == SOURCE_FALLBACK, offline["source"]
    assert offline["error_kind"] == hf.ERR_UNREACHABLE, offline["error_kind"]
    assert offline["error"], offline
    assert offline["notice"] and "not live hugging face" in offline["notice"].lower(), offline
    assert offline["models"], (
        "offline must not be a blank screen: the built-in reference list, "
        "clearly labelled, is more useful than nothing", offline,
    )
    for entry in offline["models"]:
        assert entry["source"] == SOURCE_FALLBACK, entry
        assert entry["downloadable"] is False, (
            "nothing is downloadable while the Hub is unreachable, and the "
            "entry must say so rather than offer a button that cannot work", entry,
        )
        assert entry["repo_id"] is None, entry
        assert entry["verdict"], "a fallback entry still carries a real hardware verdict"
    # The verdicts in a fallback listing are the same real verdicts the
    # offline catalog produces; only their provenance is different.
    assert len(offline["models"]) == len(CATALOG), offline["model_count"]

    # An exception out of the seam is classified, not propagated: a shop that
    # raises in front of a user is the failure this whole path exists to
    # avoid.
    def exploding_search(query, limit=None, timeout=None):
        raise RuntimeError("socket exploded")

    boom = search_shop("qwen coder", hw=hw_2060, context_tokens=8192,
                       search_fn=exploding_search, list_fn=_fixture_list_fn)
    assert boom["ok"] is False and boom["error_kind"] == ERR_HF_UNAVAILABLE, boom
    assert "socket exploded" in boom["error"], boom
    assert boom["models"], boom

    # A search that succeeds but returns nothing is a real answer, not an
    # error, and must not be dressed up as a fallback.
    def empty_search(query, limit=None, timeout=None):
        return {"ok": True, "error": None, "error_kind": None, "models": [],
                "rate_limit": None, "url": "https://huggingface.co/api/models"}

    empty = search_shop("zzzz no such model", hw=hw_2060, context_tokens=8192,
                        search_fn=empty_search, list_fn=_fixture_list_fn)
    assert empty["ok"] is True and empty["source"] == SOURCE_LIVE, empty
    assert empty["models"] == [] and empty["notice"] is None, empty

    # -- recommend() over live results ---------------------------------------
    picks = recommend(listing=listing)
    assert 1 <= len(picks) <= 2, picks
    head = picks[0]
    assert head["recommended_role"] == "best", head
    assert head["repo_id"] == "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF", head["repo_id"]
    assert head["best_quant"]["quant"] == "Q4_K_M", head["best_quant"]["quant"]
    assert head["verdict"]["verdict"] in (VERDICT_GREAT, VERDICT_GOOD), head["verdict"]
    # The reasoning must be honest about the actual choice, not boilerplate:
    # it has to name the quantisation, the context it was judged at, and how
    # much to trust the KV figure behind the verdict.
    reasoning = head["reasoning"]
    assert "Q4_K_M" in reasoning, reasoning
    assert "8192" in reasoning, reasoning
    assert "KV cache" in reasoning, reasoning
    assert head["kv_confidence"] in reasoning, reasoning
    for pick in picks:
        assert pick["verdict"]["verdict"] != VERDICT_WONT_FIT, pick

    # recommend() must never headline something nothing can run.
    hw_nothing = _hw(0, 512 * 1024 ** 2)
    listing_nothing = search_shop(
        "qwen coder", hw=hw_nothing, context_tokens=8192, detail_limit=10,
        search_fn=_fixture_search_fn, list_fn=_fixture_list_fn,
    )
    for entry in listing_nothing["models"]:
        if entry["quants_loaded"]:
            assert entry["best_quant"] is None, (
                "no quantisation of any of these repositories fits a machine "
                "with no GPU and no RAM budget", entry["repo_id"], entry["best_quant"],
            )
    assert recommend(listing=listing_nothing) == [], recommend(listing=listing_nothing)

    # recommend(listing=...) takes any listing a caller hands it, not only
    # one search_shop() built, so its own wont_fit guard has to hold on its
    # own. best_quant() already refuses to select a wont_fit quantisation, so
    # a search_shop() listing can never contain one; a hand-built listing
    # can, and the answer must still be "nothing here runs".
    hand_built = {
        "source": SOURCE_LIVE,
        "models": [{
            "repo_id": "someone/Too-Big-70B-GGUF",
            "label": "Too Big 70B",
            "focus": "coding",
            "params_b": 70.0,
            "params_source": "name",
            "kv_bytes_per_token": 327_680,
            "kv_confidence": "published_config",
            "kv_source": "family",
            "quants": [],
            "best_quant": None,
            "verdict": verdict_for(
                {"size_bytes": 74_975_000_000, "kv_bytes_per_token": 327_680},
                hw_2060, context_tokens=8192,
            ),
        }],
    }
    assert hand_built["models"][0]["verdict"]["verdict"] == VERDICT_WONT_FIT, hand_built
    assert recommend(listing=hand_built) == [], (
        "recommend() must never headline a model that cannot run, whatever "
        "listing it is handed", recommend(listing=hand_built),
    )

    # -- the live and offline halves agree on the same catalog ---------------
    # recommend() over a fallback listing must reach the same conclusion as
    # recommend() over the same catalog offline; the two paths share the
    # ranking rules, and a divergence would mean one of them is wrong.
    offline_picks = recommend(listing=offline)
    catalog_picks = recommend(hw_2060, context_tokens=8192)
    assert [p["label"] for p in offline_picks] == [p["label"] for p in catalog_picks], (
        [p["label"] for p in offline_picks], [p["label"] for p in catalog_picks],
    )
    assert offline_picks[0]["reasoning"], offline_picks[0]
    assert "could not be reached" in offline_picks[0]["reasoning"], offline_picks[0]["reasoning"]

    # recommend(listing=None) must still be the untouched offline function
    # every existing caller relies on.
    assert recommend(hw_2060, context_tokens=8192)[0]["id"] == "qwen2.5-coder:7b-instruct-q4_K_M"

    # -- recommend_live(): a listing plus picks, offline-safe ----------------
    live = recommend_live(hw=hw_2060, context_tokens=8192, detail_limit=10,
                          search_fn=_fixture_search_fn, list_fn=_fixture_list_fn)
    assert live["ok"] is True and live["source"] == SOURCE_LIVE, live
    assert live["picks"] and live["picks"][0]["recommended_role"] == "best", live["picks"]
    live_offline = recommend_live(hw=hw_2060, context_tokens=8192,
                                  search_fn=dead_search, list_fn=_fixture_list_fn)
    assert live_offline["ok"] is False and live_offline["source"] == SOURCE_FALLBACK, live_offline
    assert live_offline["picks"], "even offline, recommend_live must say something useful"

    # -- the intuition check: a 7B Q4_K_M fits a 6GB card, a 70B Q8_0 does not
    seven_b_q4 = verdict_for(
        {"size_bytes": 4_683_073_344, "kv_bytes_per_token": 57_344}, hw_2060,
        context_tokens=8192,
    )
    assert seven_b_q4["verdict"] in (VERDICT_GREAT, VERDICT_GOOD), seven_b_q4
    seventy_b_q8 = verdict_for(
        {"size_bytes": 74_975_000_000, "kv_bytes_per_token": 327_680}, hw_2060,
        context_tokens=8192,
    )
    assert seventy_b_q8["verdict"] == VERDICT_WONT_FIT, seventy_b_q8

    # -- verdict_for accepts either size key, and refuses neither ------------
    a = verdict_for({"download_bytes": 4_000_000_000, "kv_bytes_per_token": 57_344},
                    hw_2060, context_tokens=8192)
    b = verdict_for({"size_bytes": 4_000_000_000, "kv_bytes_per_token": 57_344},
                    hw_2060, context_tokens=8192)
    assert a == b, (a, b)
    try:
        verdict_for({"kv_bytes_per_token": 57_344}, hw_2060, context_tokens=8192)
    except ValueError:
        pass
    else:
        raise AssertionError("verdict_for must refuse a model dict with no size")

    # -- guess_focus is a name heuristic and is labelled as one --------------
    assert guess_focus("bartowski/Qwen2.5-Coder-7B-Instruct-GGUF") == "coding"
    assert guess_focus("TheBloke/CodeLlama-13B-Instruct-GGUF") == "coding"
    assert guess_focus("meta-llama/Llama-3.1-8B-Instruct-GGUF") == "general"


def _live_check():
    """A real, networked search against Hugging Face. Not part of --self-test:
    it needs the internet, and a self-test that fails when the Wi-Fi drops is
    a self-test nobody trusts.
    """
    listing = search_shop("qwen coder", detail_limit=3)
    print("source={} ok={} error_kind={}".format(
        listing["source"], listing["ok"], listing["error_kind"]))
    if listing["error"]:
        print("error: {}".format(listing["error"]))
    hardware = listing["hardware"]
    print("vram={} approximate={} vendor={} context={}".format(
        hardware["vram_bytes"], hardware["vram_approximate"], hardware["gpu_vendor"],
        hardware["context_tokens"]))
    for entry in listing["models"]:
        best = entry["best_quant"]
        if best:
            outcome = "{} {}".format(best["quant"], best["verdict"]["verdict"])
        elif entry["files_error_kind"]:
            outcome = entry["files_error_kind"]
        elif not entry["quants_loaded"]:
            outcome = "not fetched (past detail_limit)"
        else:
            outcome = "no runnable quantisation"
        print("  {} params={} kv={}/tok ({}) -> {}".format(
            entry["repo_id"], entry["params_b"], entry["kv_bytes_per_token"],
            entry["kv_source"], outcome,
        ))
    # One repository's full listing, quantisation by quantisation. This is
    # where duplicate editions would show up, and a per-repository summary
    # line cannot show them, so print the rows themselves.
    detailed = next((e for e in listing["models"] if e["quants_loaded"] and e["quants"]), None)
    if detailed:
        print("quantisations of {}:".format(detailed["repo_id"]))
        for quant in detailed["quants"]:
            alternates = ", ".join(
                "{} ({} parts)".format(a["name"], a["part_count"])
                for a in quant["alternate_editions"]
            )
            print("  {:<8} {:>9} {:<16} {:<7} split_parts={} {}{}".format(
                quant["quant"] or "-", _gb(quant["size_bytes"]),
                quant["verdict"]["verdict"] if quant["verdict"] else "ungraded",
                "split" if quant["multipart"] else "single",
                quant["split_part_count"],
                "<- offered " if quant.get("recommended") else "",
                ("also as " + alternates) if alternates else "",
            ))
    for pick in recommend(listing=listing):
        print("{}: {}".format(pick["recommended_role"], pick["reasoning"]))
    return 0 if listing["ok"] else 1


if __name__ == "__main__":
    if "--live" in sys.argv:
        sys.exit(_live_check())
    if "--self-test" in sys.argv or len(sys.argv) == 1:
        sys.exit(_self_test())
