#!/usr/bin/env python3
r"""hearth engine acquisition: turning the user's GPU on, after installation.

Hearth's installer bundles llama.cpp's Windows CPU build. That is the right
thing to bundle and the wrong thing to run: it is the only artifact that
cannot fail to start on an arbitrary machine, and on a machine with a real
GPU it is roughly a twelfth of the speed the hardware can deliver. Measured
on an RTX 5080 with the pinned build 10105, Qwen2.5-7B-Instruct Q4_K_M:

    CPU build      13.8 tokens/s generation,   185 tokens/s prompt
    Vulkan build  169.2 tokens/s generation,  8436 tokens/s prompt
    CUDA 13.3     169.2 tokens/s generation,  8861 tokens/s prompt

A user who never gets past the first line concludes that local models are
bad. They are not. This module is what closes that gap: after installation
it detects the GPU, fetches the right engine through the same pinned and
checksummed supply chain the installer used, proves the new binary actually
runs on this machine, and only then makes it the engine Hearth launches.

## Vulkan, not CUDA, and the measurement that decided it

The manifest used to say NVIDIA hardware should fetch CUDA. The numbers
above are why it no longer does. CUDA is five to seven per cent faster at
prompt processing on the 7B, Vulkan is twenty-five per cent faster at prompt
processing on a 0.5B F16, and generation is identical to three significant
figures. That margin costs 504 MB: 146 MB of engine plus a 391 MB cudart
companion, against Vulkan's 33 MB, and Vulkan additionally covers AMD and
Intel from the same artifact. So every vendor gets Vulkan, and the CUDA,
ROCm, SYCL and OpenVINO builds stay pinned behind HEARTH_GPU_ENGINE for
people whose hardware or workload differs from the one card this was
measured on.

## Integrated graphics gets Vulkan too, and that was measured, not assumed

The numbers above are from a desktop with a discrete card, which is the
machine this was developed on and not the machine most people have. Most
laptops have an AMD or Intel GPU sharing the CPU's memory, and there is a
real argument that such a GPU is not worth 33 MB and a download, because
generation on an integrated part is limited by the same memory bus the CPU
is already using. So it was measured rather than assumed, on a Ryzen AI 9
HX 370 with a Radeon 880M and 32 GB of LPDDR5X, build 10105, llama-server's
own timings, a 2071-token prompt, best of three:

    Qwen2.5-Coder-7B-Instruct Q4_K_M   prompt tok/s   generation tok/s
      bundled CPU build                        41.4              11.9
      Vulkan, no layers offloaded             230.2              10.3
      Vulkan, all layers offloaded             90.2              13.8

    Qwen2.5-Coder-1.5B-Instruct Q4_0   prompt tok/s   generation tok/s
      bundled CPU build                       196.6              45.6
      Vulkan, no layers offloaded            1045.0              37.1
      Vulkan, all layers offloaded           1425.2              57.2

Generation is where the sceptical argument holds: on the 7B, moving every
layer onto the integrated GPU buys 16 per cent, and moving none of them
actually costs 13 per cent against the CPU build. Prompt processing is
where the argument collapses. It is compute-bound rather than
bandwidth-bound, and the Vulkan build is 5.6 times faster at it on the 7B
even with zero layers resident, because llama.cpp still runs the batched
matrix multiplications on the GPU.

For a coding assistant that is the number that decides, because the prompt
is always the large half. One realistic turn on the 7B, 2071 tokens in and
128 out: 60.8 seconds on the CPU build, 21.4 seconds on Vulkan. The
crossover, below which the CPU build is genuinely faster, is a prompt of
roughly 230 tokens. Hearth Code never sends one that short.

So integrated graphics gets Vulkan, and the policy is the same for every
vendor. One consequence is worth stating plainly, because it looks like a
bug and is not: hearth_hw reports an integrated GPU's small dedicated
carve-out (512 MB here) rather than the shared heap the driver advertises,
so hearth_llama.choose_gpu_layers offloads NO layers on this machine. That
is the fastest of the three configurations for the 7B, and the second
fastest for the 1.5B. Reporting the shared heap instead would move both
models to the "all layers offloaded" row, which is a 2.5x LOSS on 7B prompt
processing, and would also have the shop calling a 14B a comfortable fit on
a machine with no video memory at all.

## CUDA 12.4 versus 13.3 is not a free choice

Both are pinned, and picking wrong produces a binary that cannot load rather
than one that runs slowly. CUDA 12.4 predates Blackwell entirely and tops
out at compute capability 9.0, so an RTX 50-series card (12.0) will not run
it. CUDA 13 went the other way and dropped everything below 7.5, so a
Pascal or Volta card will not run that. The choice is therefore driven by
two facts read off the card itself, never off its marketing name:
hearth_hw.nvidia_detail() reports the compute capability and the highest
CUDA major version the installed driver can run, and cuda_variant_for()
turns those into a variant or into a refusal. A machine whose capability
cannot be established gets no CUDA build at all, because the whole point is
to avoid installing something that will not load.

## Nothing becomes the engine until it has run here

The order is fetch, verify the bytes against the pin, extract, RUN THE
BINARY, and only then write the pointer that makes it active. The run is
llama-server's own `--version` and `--list-devices`, which load the full
backend registry and enumerate devices, so a build whose vendor DLLs are
missing fails here rather than in front of the user. Activation requires
all of: the binary executed, it reported a version, its effective backend is
the one the variant claims, and it can actually see a GPU device. A build
that fails any of those is deleted and recorded as failed for this hardware,
and the bundled CPU engine stays active. There is no state in which Hearth
is pointed at an engine that has never run on this machine.

The verification is a run, not a full generation, because on first launch
there is usually no model to generate with. The remaining gap, a build that
enumerates devices and then dies loading a model, is covered from the other
end: hearth_llama.start() demotes an auto-selected engine that dies before
it announces a port, and retries on the bundled build. Between the two, an
engine that cannot work here cannot stay selected.

## It never blocks the first launch

Acquisition runs on a background thread behind a versioned snapshot, the
same shape desktop/server/downloads.py uses for model downloads. Hearth is
usable on the CPU build the whole time; the engine swaps in for the next
server start once the fetch is verified. Every terminal state is honest:
"skipped" with a reason when there is nothing to fetch, "failed" with the
error when a fetch or a verification did not work, "active" with the
backend and device name when it did.

## Layout

    <data>/engines/active.json          the pointer. Which engine is live.
    <data>/engines/state.json           acquisition history, including the
                                        variants that failed on this machine.
    <data>/engines/<tag>-<variant>/     the installed engine and its runtime.
    <data>/engines/cache/               downloaded archives, shared.

It lives under the user's data directory rather than beside the application
because the application directory is Program Files on a normal install and
is not writable by the user Hearth runs as.

## Constraints

Python standard library only. The supply chain is scripts/vendor_llama.py
verbatim, not a second implementation: this module decides WHICH variant and
WHETHER to activate it, and vendor_llama does every download, every hash
check and every extraction. Nothing here executes a downloaded file until
verify_engine does, deliberately and after the hash has been checked. The
default self-test needs no network, no GPU and no binary; the parts that
need any of those are behind --live.
"""

import argparse
import json
import os
import platform
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hearth_hw
import hearth_paths

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Override the engines root, so a test never writes to the real one.
ENV_ENGINE_DIR = "HEARTH_ENGINE_DIR"

#: What to fetch, overriding the manifest policy. Accepts "off"/"none"/"cpu"
#: to stay on the bundled build, "auto" for the policy, a backend name
#: ("vulkan", "cuda"), or an exact variant name from the manifest.
ENV_ENGINE = "HEARTH_GPU_ENGINE"

ACTIVE_NAME = "active.json"
STATE_NAME = "state.json"
CACHE_NAME = "cache"

#: Acquisition states, in the order a successful run passes through them.
STATE_IDLE = "idle"
STATE_PLANNING = "planning"
STATE_DOWNLOADING = "downloading"
STATE_INSTALLING = "installing"
STATE_VERIFYING = "verifying"
STATE_ACTIVE = "active"        # a GPU engine is installed, verified and live
STATE_SKIPPED = "skipped"      # nothing to fetch, and that is a fine answer
STATE_FAILED = "failed"        # we tried; the CPU build is still what runs

TERMINAL_STATES = (STATE_ACTIVE, STATE_SKIPPED, STATE_FAILED)

#: Values of ENV_ENGINE that mean "do not fetch anything".
_OFF_VALUES = ("off", "none", "no", "cpu", "0", "false", "disabled")
_AUTO_VALUES = ("", "auto", "default", "policy")

#: Backend name -> the variant that provides it, for the friendly spellings
#: of ENV_ENGINE. CUDA is absent on purpose: which CUDA build a machine can
#: run is a question about the card, answered by cuda_variant_for.
_BACKEND_ALIASES = {
    "vulkan": "win-vulkan-x64",
    "rocm": "win-hip-radeon-x64",
    "hip": "win-hip-radeon-x64",
    "sycl": "win-sycl-x64",
    "openvino": "win-openvino-x64",
}

#: What each pinned CUDA build can actually run, as compute capabilities.
#:
#: These are the numbers that make the 12.4/13.3 choice a decision rather
#: than a coin toss, and both bounds matter. CUDA 12.4 was released before
#: Blackwell and its highest supported architecture is Hopper (9.0), so it
#: cannot run an RTX 50-series card (12.0) at all. CUDA 13 dropped Maxwell,
#: Pascal and Volta, so its floor is Turing (7.5). A card outside a build's
#: range does not run slowly on it; the binary fails to load.
_CUDA_SUPPORT = {
    "win-cuda-13.3-x64": {"driver_major": 13, "min_cap": (7, 5), "max_cap": None},
    "win-cuda-12.4-x64": {"driver_major": 12, "min_cap": (5, 0), "max_cap": (9, 0)},
}

#: Preference order when more than one CUDA build would work: newer first.
_CUDA_PREFERENCE = ("win-cuda-13.3-x64", "win-cuda-12.4-x64")

#: Free space required before a fetch starts, on top of the archives
#: themselves, to cover the extracted tree. The largest pinned pair
#: (win-cuda-13.3 plus its cudart) expands to well under this.
DISK_HEADROOM_BYTES = 2 * 1024 ** 3

#: How long a snapshot_after waiter blocks before returning unchanged, so an
#: SSE stream can send a keep-alive rather than holding a socket silently.
SNAPSHOT_WAIT_SECONDS = 15


class EngineError(RuntimeError):
    """Acquisition could not proceed. Carries a message a user can read."""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def engines_root(env=None):
    """Where fetched engines and their bookkeeping live."""
    env = os.environ if env is None else env
    override = (env.get(ENV_ENGINE_DIR) or "").strip().strip('"')
    if override:
        return override
    return os.path.join(hearth_paths.data_dir(), "engines")


def active_path(env=None):
    return os.path.join(engines_root(env), ACTIVE_NAME)


def state_path(env=None):
    return os.path.join(engines_root(env), STATE_NAME)


def cache_dir(env=None):
    return os.path.join(engines_root(env), CACHE_NAME)


def variant_dir(release_tag, variant, env=None):
    """One installed engine's directory, named for the pin that produced it.

    The release tag is in the directory name so that bumping the manifest
    installs alongside rather than over the top of the running engine: a
    half-replaced engine directory is a broken engine, and Hearth may well
    have the old one open when the new pin arrives.
    """
    return os.path.join(engines_root(env), "{}-{}".format(release_tag, variant))


def _vendor_llama():
    """scripts/vendor_llama, imported from wherever this Hearth is installed.

    The packaged application stages the script next to the agent modules
    (see scripts/build_windows.py), so the same relative path works from a
    checkout and from an install. Imported lazily because the hot path,
    active_server_path(), must not pay for it.
    """
    import hearth_llama  # deferred: see the module docstring on the cycle

    scripts = os.path.join(hearth_llama.app_root(), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import vendor_llama
    except ImportError as exc:
        raise EngineError(
            "this Hearth install has no scripts/vendor_llama.py, so it cannot "
            "fetch an engine: looked in {}".format(scripts)) from exc
    return vendor_llama


# --------------------------------------------------------------------------
# The active engine pointer
# --------------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def active(env=None):
    """The active fetched engine, or None.

    None covers every reason there might not be one: nothing was ever
    fetched, the pointer is unreadable, or it names a binary that is no
    longer on disk. A caller that gets None falls back to the bundled CPU
    build, which is always present, so this function has no failure mode
    that needs reporting as an error.

    Deliberately does NOT re-run the binary. Verification happens once, at
    activation; paying for a subprocess on every find_server() would put a
    process spawn in front of every model load.
    """
    data = _read_json(active_path(env))
    if not data:
        return None
    server = data.get("server")
    if not server or not os.path.isfile(hearth_paths.long_path(server)):
        return None
    return data


def active_server_path(env=None):
    """The active engine's llama-server, or None. Never raises."""
    try:
        data = active(env)
    except Exception:  # noqa: BLE001 - engine discovery must never break a launch
        return None
    return (data or {}).get("server") or None


def read_state(env=None):
    """Acquisition bookkeeping: the last attempt and what has failed here."""
    return _read_json(state_path(env)) or {
        "state": STATE_IDLE, "variant": None, "message": "", "error": None,
        "failed": [], "updated_at": None,
    }


def hardware_signature(gpus=None, nvidia=None):
    """A short, stable string identifying the GPU side of this machine.

    Recorded with every failure so that "the CUDA build did not load here"
    is remembered against the hardware it was true of, and a user who
    changes their card or updates their driver gets a fresh attempt instead
    of a permanent refusal.
    """
    gpus = hearth_hw.gpus() if gpus is None else gpus
    names = sorted((g.get("name") or "?") for g in gpus)
    driver = ""
    if nvidia:
        drivers = sorted({(d.get("driver_version") or "") for d in nvidia if d})
        driver = "/".join(d for d in drivers if d)
    return "|".join(names) + ("@" + driver if driver else "")


# --------------------------------------------------------------------------
# Choosing a variant
# --------------------------------------------------------------------------

def _plan(variant=None, backend=None, vendor=None, reason="", source="policy",
          entry=None, requires=(), size=0):
    return {"variant": variant, "backend": backend, "vendor": vendor,
            "reason": reason, "source": source, "requires": list(requires),
            "size_bytes": size, "entry": entry}


#: How each vendor spells its own name, for messages a user reads. The
#: internal constants are lowercase, and "a amd GPU" was shipped in a
#: user-facing string because a format placeholder was fed one of them
#: directly. Every sentence below is built so that no article has to be
#: chosen from a value at runtime.
_VENDOR_LABELS = {
    hearth_hw.VENDOR_NVIDIA: "NVIDIA",
    hearth_hw.VENDOR_AMD: "AMD",
    hearth_hw.VENDOR_INTEL: "Intel",
}


def _vendor_label(vendor):
    return _VENDOR_LABELS.get(vendor, vendor or "unrecognised")


def _pick_gpu(gpus):
    """The GPU whose vendor decides which engine this machine fetches.

    This used to be "the first entry hearth_hw returned", and that was a
    real bug rather than a stylistic one. Windows enumerates virtual
    display adapters in the same class as real hardware, and on a laptop
    with Parsec installed the shim sorted first: its vendor is "unknown",
    the policy has no engine for "unknown", and a Radeon 880M that Vulkan
    covers perfectly ran on the CPU build instead. hearth_hw now excludes
    those shims, and this function is the second half of the fix, so that a
    machine listing more than one real GPU still picks a sensible one
    rather than whichever the driver stack happened to enumerate first.

    The order of preference:
      1. an adapter that is not virtual, if any is;
      2. among those, one whose vendor could actually be identified, since
         a vendor of "unknown" is the one value the policy cannot act on;
      3. among those, a discrete GPU over an integrated one, because on a
         laptop with both it is the discrete card that will run the model;
      4. among those, the largest.

    Returns None for an empty list. An adapter whose integrated flag is
    None (hearth_hw could not tell) is ranked WITH the discrete ones: not
    knowing is not a reason to demote a card below an integrated one.
    """
    candidates = [g for g in gpus if not g.get("virtual")] or list(gpus)
    known = [g for g in candidates
             if g.get("vendor") and g.get("vendor") != hearth_hw.VENDOR_UNKNOWN]
    pool = known or candidates
    if not pool:
        return None
    return max(pool, key=lambda g: (0 if g.get("integrated") is True else 1,
                                    g.get("vram_bytes") or 0))


def cuda_variant_for(nvidia, available=None):
    """The CUDA build this machine's NVIDIA hardware can run, or None.

    `nvidia` is hearth_hw.nvidia_detail()'s list. Returns
    (variant_name_or_None, reason), and the reason always names the actual
    numbers, because "your card is not supported" is useless and "compute
    capability 12.0 needs CUDA 13, and this driver tops out at CUDA 12" is
    something a user can act on.

    Every card is required to be supported, not just the biggest one. A
    machine holding a GTX 1080 (6.1) and an RTX 5080 (12.0) has no CUDA
    build that covers both, and the honest answer there is to fetch neither
    and let Vulkan, which covers both, do the work.
    """
    available = _CUDA_PREFERENCE if available is None else tuple(available)
    if nvidia is None:
        return None, "nvidia-smi could not be run, so the card's CUDA support is unknown"
    if not nvidia:
        return None, "no NVIDIA GPU was reported"

    caps = [d.get("compute_capability") for d in nvidia]
    if any(c is None for c in caps):
        unknown = [d.get("name") or "?" for d in nvidia
                   if d.get("compute_capability") is None]
        return None, ("the compute capability of {} could not be read, and a CUDA "
                      "build chosen without it may not load".format(", ".join(unknown)))
    lowest, highest = min(caps), max(caps)
    ceilings = [d.get("cuda_driver_major") for d in nvidia]
    ceiling = min([c for c in ceilings if c is not None] or [None])

    tried = []
    for name in _CUDA_PREFERENCE:
        if name not in available:
            continue
        spec = _CUDA_SUPPORT[name]
        if lowest < spec["min_cap"]:
            tried.append("{} needs compute capability {}.{} or newer and this "
                         "machine has {}.{}".format(name, spec["min_cap"][0],
                                                    spec["min_cap"][1], *lowest))
            continue
        if spec["max_cap"] is not None and highest > spec["max_cap"]:
            tried.append("{} supports up to compute capability {}.{} and this "
                         "machine has {}.{}".format(name, spec["max_cap"][0],
                                                    spec["max_cap"][1], *highest))
            continue
        if ceiling is None:
            tried.append("{} needs a CUDA {} driver and the installed driver "
                         "version could not be read".format(name, spec["driver_major"]))
            continue
        if ceiling < spec["driver_major"]:
            tried.append("{} needs a CUDA {} driver and this one supports up to "
                         "CUDA {}".format(name, spec["driver_major"], ceiling))
            continue
        return name, ("compute capability {}.{} with a CUDA {} driver runs "
                      "{}".format(highest[0], highest[1], ceiling, name))
    return None, "; ".join(tried) or "no pinned CUDA build covers this hardware"


def _usable(manifest, name, system, machine):
    """The manifest entry for `name`, or an EngineError saying why not."""
    entry = (manifest.get("variants") or {}).get(name)
    if entry is None:
        raise EngineError("{!r} is not a variant this Hearth pins; it has: {}".format(
            name, ", ".join(sorted((manifest.get("variants") or {})))))
    want_os = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(system)
    if entry.get("os") != want_os:
        raise EngineError("{!r} is a {} build and this is {}".format(
            name, entry.get("os"), system))
    want_arch = {"AMD64": "x86_64", "x86_64": "x86_64",
                 "ARM64": "arm64", "aarch64": "arm64"}.get(machine)
    if entry.get("arch") != want_arch:
        raise EngineError("{!r} is an {} build and this machine is {}".format(
            name, entry.get("arch"), machine))
    if entry.get("backend") in ("cpu", "runtime"):
        raise EngineError("{!r} is a {} artifact, not a GPU engine".format(
            name, entry.get("backend")))
    return entry


def choose_variant(manifest, gpus=None, nvidia=None, system=None, machine=None,
                   env=None):
    """Which engine, if any, this machine should fetch. Never raises.

    Returns a plan dict: variant (None means "stay on the CPU build"),
    backend, vendor, size_bytes, the manifest entry, and a reason that is
    always populated, including on the None path. "There is nothing worth
    fetching" is a real answer and gets a real explanation.

    The mapping from GPU vendor to variant is the manifest's
    policy.first_run_gpu_fetch, not a table in this file, so the decision
    and the checksums that back it live in the same committed document.
    HEARTH_GPU_ENGINE overrides the policy but not the hardware checks: a
    CUDA build that this card cannot load is refused however it was asked
    for, because installing it would replace slow inference with a launch
    failure.
    """
    env = os.environ if env is None else env
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine

    override = (env.get(ENV_ENGINE) or "").strip().strip('"').lower()
    if override in _OFF_VALUES:
        return _plan(reason="{}={} asks Hearth to stay on the bundled CPU "
                            "build".format(ENV_ENGINE, override), source="override")

    if gpus is None:
        try:
            gpus = hearth_hw.gpus()
        except Exception:  # noqa: BLE001 - detection must never break a launch
            gpus = []
    # hearth_hw.gpus() already drops virtual adapters; this repeats the
    # filter because choose_variant is also called with a caller-supplied
    # list, and a shim must not decide the engine down either path.
    shims = [g.get("name") or "?" for g in gpus if g.get("virtual")]
    gpus = [g for g in gpus if not g.get("virtual")]
    picked = _pick_gpu(gpus)
    vendor = (picked or {}).get("vendor") or None
    gpu_name = (picked or {}).get("name") or "?"
    if nvidia is None and (vendor == hearth_hw.VENDOR_NVIDIA or override == "cuda"):
        try:
            nvidia = hearth_hw.nvidia_detail()
        except Exception:  # noqa: BLE001
            nvidia = None

    def _finish(name, source, reason):
        try:
            entry = _usable(manifest, name, system, machine)
        except EngineError as exc:
            return _plan(vendor=vendor, reason=str(exc), source=source)
        return _plan(variant=name, backend=entry.get("backend"), vendor=vendor,
                     reason=reason, source=source, entry=entry,
                     requires=entry.get("requires") or [],
                     size=_total_size(manifest, name))

    if override and override not in _AUTO_VALUES:
        if override == "cuda":
            name, why = cuda_variant_for(nvidia)
            if not name:
                return _plan(vendor=vendor, source="override",
                             reason="{}=cuda was asked for, but {}".format(ENV_ENGINE, why))
            return _finish(name, "override", "{}=cuda, and {}".format(ENV_ENGINE, why))
        name = _BACKEND_ALIASES.get(override, override)
        if name in _CUDA_SUPPORT:
            picked, why = cuda_variant_for(nvidia, available=(name,))
            if picked != name:
                return _plan(vendor=vendor, source="override",
                             reason="{}={} was asked for, but {}".format(
                                 ENV_ENGINE, override, why))
        return _finish(name, "override", "{}={}".format(ENV_ENGINE, override))

    if not gpus:
        if shims:
            return _plan(reason="the only display adapters on this machine are "
                                "virtual ones ({}), which cannot run a model, so "
                                "the bundled CPU build is already the right "
                                "engine".format(", ".join(shims)))
        return _plan(reason="no GPU was detected, so the bundled CPU build is "
                            "already the right engine")

    policy = (manifest.get("policy") or {}).get("first_run_gpu_fetch") or {}
    if not policy:
        return _plan(vendor=vendor,
                     reason="this Hearth's pin carries no first-run GPU policy")
    name = policy.get(vendor) if vendor else None
    if not name:
        # Two different situations, and they must not read the same. One is
        # "we could not work out what this GPU is", which is a limitation of
        # Hearth's detection and something the user can override. The other
        # is "we know exactly what it is and have no engine for it", which
        # is a limitation of the pin.
        if vendor in (None, hearth_hw.VENDOR_UNKNOWN):
            return _plan(vendor=vendor,
                         reason="Hearth could not work out who makes the graphics "
                                "in this machine, which reports itself as {!r}. "
                                "Rather than install a GPU engine that might not "
                                "load, it is staying on the bundled CPU build: "
                                "everything works, just slower than the hardware "
                                "probably can. If you know this GPU supports "
                                "Vulkan, set {}=vulkan and Hearth will fetch the "
                                "Vulkan engine and test it here before using "
                                "it.".format(gpu_name, ENV_ENGINE))
        return _plan(vendor=vendor,
                     reason="this Hearth's pin has no GPU engine for {} graphics "
                            "({}), so the bundled CPU build stays".format(
                                _vendor_label(vendor), gpu_name))
    return _finish(name, "policy",
                   "this machine's {} GPU ({}) is covered by the pinned {} "
                   "build".format(_vendor_label(vendor), gpu_name, name))


def _total_size(manifest, variant):
    """Bytes to download for `variant`, including its companions."""
    variants = manifest.get("variants") or {}
    entry = variants.get(variant) or {}
    total = int(entry.get("size_bytes") or 0)
    for req in entry.get("requires") or []:
        total += int((variants.get(req) or {}).get("size_bytes") or 0)
    return total


# --------------------------------------------------------------------------
# Verification: the binary has to run HERE before it becomes the engine
# --------------------------------------------------------------------------

def verify_engine(server_path, expected_backend=None):
    """Run a freshly installed engine and decide whether it may be activated.

    Returns (ok, info, reason). `info` is hearth_llama.probe_binary's dict,
    which is kept whether or not the check passed, because a failure that
    reports "backend: cpu, no devices" is a different diagnosis from one
    that reports "could not run: the specified module could not be found",
    and both belong in front of whoever is reading the log.

    Four conditions, and all of them have to hold:

      1. The binary executed and printed a version. This is the check that
         catches the failure that matters most: a GPU build whose vendor
         DLLs are absent does not run slowly, it does not start, and a
         missing DLL on Windows produces no output at all.
      2. Its effective backend is the one the variant claimed. A CUDA build
         that quietly falls back to CPU is not a CUDA engine, and
         activating it would put a GPU claim in front of a user who is not
         getting one.
      3. It reports at least one GPU device.
      4. gpu_offload is true, which is hearth_llama's own answer to "will
         -ngl do anything on this build".

    THIS IS THE FIRST AND ONLY PLACE A DOWNLOADED FILE IS EXECUTED, and it
    runs only after vendor_llama has checked the bytes against the pinned
    hash. Do not move it earlier.
    """
    import hearth_llama  # deferred: see the module docstring on the cycle

    info = hearth_llama.probe_binary(server_path)
    if not info.get("ok"):
        return False, info, (info.get("error")
                             or "it ran but reported no version")
    backend = info.get("backend")
    if expected_backend and backend != expected_backend:
        return False, info, (
            "it reports the {} backend, but this variant is supposed to provide "
            "{}".format(backend, expected_backend))
    devices = [d for d in (info.get("devices") or [])
               if d.get("backend") in hearth_llama.GPU_BACKENDS]
    if not devices:
        return False, info, "it ran but found no GPU device to offload to"
    if not info.get("gpu_offload"):
        return False, info, "it ran but reports that it cannot offload layers"
    return True, info, "{} backend, {}".format(backend, devices[0].get("name") or "a GPU")


def activate(variant, entry_dir, probe_info, manifest, env=None, source="policy"):
    """Make a verified engine the one hearth_llama launches.

    Writes the pointer LAST, after every check has passed, so there is no
    window in which active.json names something unproven. Returns the
    pointer's contents.
    """
    import hearth_llama  # deferred

    server = os.path.join(entry_dir, hearth_llama._exe(hearth_llama.SERVER_BASENAME))
    if not os.path.isfile(server):
        raise EngineError("there is no {} in {}".format(
            hearth_llama._exe(hearth_llama.SERVER_BASENAME), entry_dir))
    devices = [d for d in (probe_info.get("devices") or [])
               if d.get("backend") in hearth_llama.GPU_BACKENDS]
    data = {
        "variant": variant,
        "backend": probe_info.get("backend"),
        "build": probe_info.get("build"),
        "release_tag": manifest.get("release_tag"),
        "dir": entry_dir,
        "server": server,
        "devices": devices,
        "device": (devices[0].get("name") if devices else None),
        "source": source,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hardware": hardware_signature(),
    }
    _write_json(active_path(env), data)
    return data


def demote(reason, server_path=None, env=None):
    """Stop using the active fetched engine, and remember why.

    Called when an engine that verified at install time turns out not to
    work at launch time. Removing the pointer is enough to fall back:
    find_server() drops through to the bundled CPU build, which is always
    present, so the next launch works rather than failing differently.

    `server_path` scopes the demotion. A caller that saw a specific binary
    fail passes it, and a pointer naming some other binary is left alone,
    so a failure caused by the model or by the environment cannot take out
    an engine that was not even involved.

    Returns True when something was demoted. Never raises.
    """
    try:
        data = active(env)
        if not data:
            return False
        if server_path and os.path.normcase(os.path.abspath(server_path)) != \
                os.path.normcase(os.path.abspath(data.get("server") or "")):
            return False
        try:
            os.unlink(active_path(env))
        except OSError:
            return False
        state = read_state(env)
        failures = [f for f in (state.get("failed") or [])
                    if f.get("variant") != data.get("variant")]
        failures.append({
            "variant": data.get("variant"),
            "reason": reason,
            "hardware": data.get("hardware") or hardware_signature(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stage": "launch",
        })
        state.update({"state": STATE_FAILED, "variant": data.get("variant"),
                      "error": reason, "failed": failures[-8:],
                      "message": "fell back to the bundled CPU engine: {}".format(reason),
                      "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        _write_json(state_path(env), state)
        return True
    except Exception:  # noqa: BLE001 - a failed demotion must not mask the
        # failure that triggered it; the worst case is that the next launch
        # tries the same engine again and demotes then.
        return False


def _recorded_failure(state, variant, signature):
    """A previous failure of `variant` on this same hardware, or None."""
    for f in reversed(state.get("failed") or []):
        if f.get("variant") == variant and f.get("hardware") == signature:
            return f
    return None


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------

class Acquirer:
    """The background fetch, behind a versioned snapshot.

    One instance per process. `start()` returns immediately and the work
    happens on a thread, because 33 MB on a slow connection is a minute
    that first launch does not have to spend. Progress is published as a
    whole snapshot rather than as a stream of deltas, for the same reason
    downloads.py does: a client that reconnects wants the current state,
    not a narrative it has to replay.

    Every dependency the tests need to replace is an argument: the
    manifest, the installer, the verifier, and the hardware readings. The
    default self-test drives a complete acquisition, including both failure
    paths, with none of a network, a GPU or a binary.
    """

    def __init__(self, manifest=None, env=None, install_fn=None, verify_fn=None,
                 gpus_fn=None, nvidia_fn=None, disk_fn=None):
        self._env = env
        self._manifest = manifest
        self._install_fn = install_fn
        self._verify_fn = verify_fn
        self._gpus_fn = gpus_fn
        self._nvidia_fn = nvidia_fn
        self._disk_fn = disk_fn or (lambda path: shutil.disk_usage(path).free)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._thread = None
        self._version = 0
        self._snap = {
            "state": STATE_IDLE, "variant": None, "backend": None, "vendor": None,
            "message": "", "error": None, "reason": "",
            "bytes_done": 0, "bytes_total": 0, "asset": None,
            "started_at": None, "finished_at": None,
        }

    # -- snapshot ----------------------------------------------------------

    def _set(self, **fields):
        with self._cond:
            self._snap.update(fields)
            self._version += 1
            self._cond.notify_all()

    def _public(self):
        data = dict(self._snap)
        data["version"] = self._version
        data["active"] = self.active_summary()
        data["running"] = bool(self._thread and self._thread.is_alive())
        return data

    def snapshot(self):
        """The whole current state, with a version a client can wait on."""
        with self._cond:
            return self._public()

    def snapshot_after(self, version, timeout=None):
        """Block until the snapshot moves past `version`, then return it.

        Returns the unchanged snapshot on timeout, so an SSE loop can tell
        "nothing happened" from "something happened" by comparing versions
        rather than by catching an exception.
        """
        deadline = time.monotonic() + (SNAPSHOT_WAIT_SECONDS if timeout is None
                                       else timeout)
        with self._cond:
            while self._version <= version:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._public()

    def active_summary(self):
        """What is live right now, from the pointer on disk, or None."""
        data = active(self._env)
        if not data:
            return None
        return {k: data.get(k) for k in
                ("variant", "backend", "build", "device", "release_tag",
                 "server", "verified_at", "source")}

    # -- driving -----------------------------------------------------------

    def start(self, force=False):
        """Kick off acquisition on a background thread. Idempotent.

        Returns the snapshot. A second call while a run is in flight is a
        no-op rather than a second download.
        """
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return self._public()
            self._thread = threading.Thread(
                target=self._run, kwargs={"force": force},
                name="hearth-engine-acquire", daemon=True)
            self._thread.start()
            return self._public()

    def join(self, timeout=None):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.snapshot()

    def _run(self, force=False):
        try:
            self.run_once(force=force)
        except Exception as exc:  # noqa: BLE001 - a background thread that
            # raises would take the acquisition state with it and leave the
            # UI showing "downloading" forever. Every failure is a state.
            self._fail("engine acquisition failed unexpectedly: {}".format(exc))

    def _fail(self, message, variant=None, stage="acquire", record=True):
        self._set(state=STATE_FAILED, error=message,
                  message="Hearth is still using the CPU engine: {}".format(message),
                  finished_at=_now())
        if record and variant:
            state = read_state(self._env)
            failures = [f for f in (state.get("failed") or [])
                        if f.get("variant") != variant]
            failures.append({"variant": variant, "reason": message,
                             "hardware": self._signature(), "at": _now(),
                             "stage": stage})
            state["failed"] = failures[-8:]
            state.update({"state": STATE_FAILED, "variant": variant,
                          "error": message, "updated_at": _now()})
            _write_json(state_path(self._env), state)
        else:
            state = read_state(self._env)
            state.update({"state": STATE_FAILED, "variant": variant,
                          "error": message, "updated_at": _now()})
            _write_json(state_path(self._env), state)
        return self.snapshot()

    def _signature(self):
        gpus = self._gpus_fn() if self._gpus_fn else None
        nvidia = self._nvidia_fn() if self._nvidia_fn else None
        try:
            return hardware_signature(gpus, nvidia)
        except Exception:  # noqa: BLE001
            return ""

    def run_once(self, force=False):
        """The whole flow, synchronously. Returns the final snapshot.

        Never raises: every failure lands in the snapshot as STATE_FAILED
        with an error a user can read, and the bundled CPU engine is still
        what runs. That is the contract the whole module rests on, and it
        is why the caller of this function does not need a try block.
        """
        self._set(state=STATE_PLANNING, error=None, message="Checking your GPU",
                  started_at=_now(), finished_at=None, bytes_done=0, bytes_total=0)

        live = active(self._env)
        try:
            vl = _vendor_llama()
            manifest = self._manifest or vl.load_manifest()
        except Exception as exc:  # noqa: BLE001
            if live:
                # An engine that is already installed and verified keeps
                # working whether or not the pin can be re-read. Reporting
                # this as a failure would overwrite a perfectly good
                # "active" with an error about a fetch nobody needs, which
                # is how a healthy install ends up looking broken.
                self._set(state=STATE_ACTIVE, variant=live.get("variant"),
                          backend=live.get("backend"), finished_at=_now(),
                          message="{} is running on your {}".format(
                              live.get("backend"), live.get("device") or "GPU"),
                          reason="the engine pin could not be re-read: {}".format(exc))
                return self.snapshot()
            return self._fail("could not read the engine pin: {}".format(exc),
                              record=False)

        if live and live.get("release_tag") == manifest.get("release_tag") and not force:
            message = "{} is already running on your {}".format(
                live.get("backend"), live.get("device") or "GPU")
            self._set(state=STATE_ACTIVE, variant=live.get("variant"),
                      backend=live.get("backend"), finished_at=_now(),
                      message=message)
            # Persisted, not just published: an earlier failed attempt left
            # its error in state.json, and leaving that behind means every
            # later reader of status() sees an error next to a working
            # engine. Confirming success has to clear the record it
            # supersedes.
            state = read_state(self._env)
            if state.get("state") != STATE_ACTIVE or state.get("error"):
                state.update({"state": STATE_ACTIVE, "variant": live.get("variant"),
                              "error": None, "message": message,
                              "updated_at": _now()})
                _write_json(state_path(self._env), state)
            return self.snapshot()

        gpus = self._gpus_fn() if self._gpus_fn else None
        nvidia = self._nvidia_fn() if self._nvidia_fn else None
        plan = choose_variant(manifest, gpus=gpus, nvidia=nvidia, env=self._env)
        self._set(vendor=plan["vendor"], reason=plan["reason"])

        if not plan["variant"]:
            # Not a failure. A machine with no GPU, or one the pin has no
            # engine for, is correctly served by the build it already has.
            self._set(state=STATE_SKIPPED, message=plan["reason"],
                      finished_at=_now())
            state = read_state(self._env)
            state.update({"state": STATE_SKIPPED, "variant": None,
                          "message": plan["reason"], "error": None,
                          "updated_at": _now()})
            _write_json(state_path(self._env), state)
            return self.snapshot()

        variant = plan["variant"]
        signature = hardware_signature(gpus, nvidia)
        state = read_state(self._env)
        previous = _recorded_failure(state, variant, signature)
        if previous is not None and not force:
            message = ("{} was already tried on this hardware and did not work: "
                       "{}".format(variant, previous.get("reason")))
            self._set(state=STATE_SKIPPED, variant=variant, message=message,
                      finished_at=_now())
            return self.snapshot()

        total = plan["size_bytes"]
        self._set(state=STATE_DOWNLOADING, variant=variant,
                  backend=plan["backend"], bytes_total=total,
                  message="Downloading the {} engine ({:.0f} MB)".format(
                      plan["backend"], total / 1e6))

        dest = variant_dir(manifest["release_tag"], variant, self._env)
        try:
            os.makedirs(engines_root(self._env), exist_ok=True)
            free = self._disk_fn(engines_root(self._env))
            if free < total + DISK_HEADROOM_BYTES:
                return self._fail(
                    "there is not enough free disk space for the {} engine: it "
                    "needs about {:.0f} MB and {:.0f} MB is free".format(
                        plan["backend"], (total + DISK_HEADROOM_BYTES) / 1e6,
                        free / 1e6), variant, stage="disk", record=False)
        except OSError as exc:
            return self._fail("could not prepare {}: {}".format(
                engines_root(self._env), exc), variant, stage="disk", record=False)

        def _progress(done, _total, asset):
            self._set(bytes_done=done, asset=asset)

        installer = self._install_fn or (lambda **kw: vl.install_variant(**kw))
        try:
            result = installer(variant=variant, dest=dest, manifest=manifest,
                               cache=cache_dir(self._env), on_progress=_progress)
        except Exception as exc:  # noqa: BLE001 - a checksum failure, a dead
            # network and a full disk all mean the same thing to the user:
            # the CPU engine is still what runs, and here is why.
            shutil.rmtree(dest, ignore_errors=True)
            return self._fail("could not install the {} engine: {}".format(
                plan["backend"], exc), variant, stage="install", record=False)

        self._set(state=STATE_VERIFYING, bytes_done=total,
                  message="Checking that the {} engine runs on this "
                          "machine".format(plan["backend"]))
        verifier = self._verify_fn or verify_engine
        try:
            ok, info, why = verifier(result["server"], plan["backend"])
        except Exception as exc:  # noqa: BLE001
            ok, info, why = False, {}, "the check itself failed: {}".format(exc)

        if not ok:
            # The engine is deleted rather than left on disk looking
            # installed. A directory that holds a binary Hearth has decided
            # not to use is a trap for the next person to read the tree.
            shutil.rmtree(dest, ignore_errors=True)
            return self._fail(
                "the {} engine downloaded correctly but does not run on this "
                "machine: {}".format(plan["backend"], why), variant,
                stage="verify")

        try:
            data = activate(variant, dest, info, manifest, env=self._env,
                            source=plan["source"])
        except Exception as exc:  # noqa: BLE001
            return self._fail("could not activate the {} engine: {}".format(
                plan["backend"], exc), variant, stage="activate", record=False)

        message = "{} acceleration is ready on your {}".format(
            data.get("backend"), data.get("device") or "GPU")
        self._set(state=STATE_ACTIVE, variant=variant, backend=data.get("backend"),
                  message=message, error=None, finished_at=_now())
        state = read_state(self._env)
        state.update({"state": STATE_ACTIVE, "variant": variant, "error": None,
                      "message": message, "updated_at": _now()})
        _write_json(state_path(self._env), state)
        return self.snapshot()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------
# Process-wide instance, for the sidecar
# --------------------------------------------------------------------------

_ACQUIRER = None
_ACQUIRER_LOCK = threading.Lock()


def acquirer():
    """The process's single Acquirer, made on first use."""
    global _ACQUIRER
    with _ACQUIRER_LOCK:
        if _ACQUIRER is None:
            _ACQUIRER = Acquirer()
        return _ACQUIRER


def status(env=None):
    """A summary for a caller that does not want to own an Acquirer.

    Cheap: reads two small JSON files and runs nothing.
    """
    live = active(env)
    state = read_state(env)
    return {
        "active": live,
        "state": (STATE_ACTIVE if live else state.get("state") or STATE_IDLE),
        "message": state.get("message") or "",
        "error": state.get("error"),
        "variant": (live or {}).get("variant") or state.get("variant"),
        "backend": (live or {}).get("backend"),
        "device": (live or {}).get("device"),
        "failed": state.get("failed") or [],
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_parser():
    p = argparse.ArgumentParser(
        prog="hearth-engine",
        description="Detect the GPU, fetch the matching llama.cpp engine, "
                    "verify it runs here, and make it active.")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--live", action="store_true",
                   help="also run the checks that need the network and a real binary")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("status", help="what engine is active, and what was tried")
    sub.add_parser("plan", help="what this machine would fetch, without fetching")
    acq = sub.add_parser("acquire", help="fetch, verify and activate")
    acq.add_argument("--force", action="store_true",
                     help="retry even a variant that already failed here")
    dem = sub.add_parser("demote", help="stop using the fetched engine")
    dem.add_argument("--reason", default="demoted from the command line")
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.self_test:
        _self_test(live=args.live)
        print("hearth_engine self-test: ok")
        return 0

    if args.command == "demote":
        print("demoted" if demote(args.reason) else "nothing to demote")
        return 0

    if args.command == "plan":
        try:
            manifest = _vendor_llama().load_manifest()
        except EngineError as exc:
            print("error: {}".format(exc), file=sys.stderr)
            return 1
        plan = choose_variant(manifest)
        plan.pop("entry", None)
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    if args.command == "acquire":
        acq = Acquirer()
        last = None
        acq.start(force=args.force)
        while True:
            snap = acq.snapshot_after(last or 0, timeout=1)
            if snap["version"] != last:
                last = snap["version"]
                line = "{:<12} {}".format(snap["state"], snap["message"])
                if snap["state"] == STATE_DOWNLOADING and snap["bytes_total"]:
                    line += "  {:.0f}%".format(
                        100.0 * snap["bytes_done"] / snap["bytes_total"])
                print(line)
            if snap["state"] in TERMINAL_STATES and not snap["running"]:
                return 0 if snap["state"] != STATE_FAILED else 1

    print(json.dumps(status(), indent=2, sort_keys=True, default=str))
    return 0


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _fake_manifest():
    """A pin with the same shape as the real one, and no real assets."""
    return {
        "release_tag": "b9999",
        "bundled_variant": "win-cpu-x64",
        "policy": {
            "bundled_variant": "win-cpu-x64",
            "first_run_gpu_fetch": {"nvidia": "win-vulkan-x64",
                                    "amd": "win-vulkan-x64",
                                    "intel": "win-vulkan-x64",
                                    "other_or_none": None},
        },
        "variants": {
            "win-cpu-x64": {"asset": "a.zip", "sha256": "a" * 64, "size_bytes": 10,
                            "sha256_source": "downloaded_and_hashed", "os": "windows",
                            "arch": "x86_64", "backend": "cpu", "gpu_vendors": [],
                            "requires": [], "note": ""},
            "win-vulkan-x64": {"asset": "v.zip", "sha256": "b" * 64,
                               "size_bytes": 33_000_000,
                               "sha256_source": "downloaded_and_hashed", "os": "windows",
                               "arch": "x86_64", "backend": "vulkan",
                               "gpu_vendors": ["nvidia", "amd", "intel"],
                               "requires": [], "note": ""},
            "win-cuda-13.3-x64": {"asset": "c13.zip", "sha256": "c" * 64,
                                  "size_bytes": 146_000_000,
                                  "sha256_source": "github_api_digest", "os": "windows",
                                  "arch": "x86_64", "backend": "cuda",
                                  "gpu_vendors": ["nvidia"],
                                  "requires": ["cudart-cuda-13.3-x64"], "note": ""},
            "cudart-cuda-13.3-x64": {"asset": "rt13.zip", "sha256": "d" * 64,
                                     "size_bytes": 391_000_000,
                                     "sha256_source": "github_api_digest",
                                     "os": "windows", "arch": "x86_64",
                                     "backend": "runtime", "gpu_vendors": ["nvidia"],
                                     "requires": [], "note": ""},
            "win-cuda-12.4-x64": {"asset": "c12.zip", "sha256": "e" * 64,
                                  "size_bytes": 250_000_000,
                                  "sha256_source": "github_api_digest", "os": "windows",
                                  "arch": "x86_64", "backend": "cuda",
                                  "gpu_vendors": ["nvidia"],
                                  "requires": ["cudart-cuda-12.4-x64"], "note": ""},
            "cudart-cuda-12.4-x64": {"asset": "rt12.zip", "sha256": "f" * 64,
                                     "size_bytes": 391_000_000,
                                     "sha256_source": "github_api_digest",
                                     "os": "windows", "arch": "x86_64",
                                     "backend": "runtime", "gpu_vendors": ["nvidia"],
                                     "requires": [], "note": ""},
            "win-cpu-arm64": {"asset": "arm.zip", "sha256": "1" * 64, "size_bytes": 12,
                              "sha256_source": "github_api_digest", "os": "windows",
                              "arch": "arm64", "backend": "cpu", "gpu_vendors": [],
                              "requires": [], "note": ""},
        },
    }


def _nvidia(cap, driver_major=13, name="NVIDIA GeForce RTX 5080", driver="610.47"):
    return [{"index": 0, "name": name, "compute_capability": cap,
             "driver_version": driver, "cuda_driver_major": driver_major}]


def _self_test(live=False):
    import tempfile

    tmp = tempfile.mkdtemp(prefix="hearth-engine-test-")
    manifest = _fake_manifest()
    nvidia_gpu = [{"name": "NVIDIA GeForce RTX 5080", "vram_bytes": 17_094_934_528,
                   "vendor": "nvidia", "approximate": False}]
    amd_gpu = [{"name": "AMD Radeon RX 7900 XTX", "vram_bytes": 25_757_220_864,
                "vendor": "amd", "approximate": False}]
    win = {"system": "Windows", "machine": "AMD64"}

    try:
        # -- the 12.4 versus 13.3 split, which is the whole CUDA question ---
        # Every one of these is a real card, and picking wrong produces a
        # binary that will not load rather than one that is slow.
        got, why = cuda_variant_for(_nvidia((12, 0), 13))       # RTX 5080
        assert got == "win-cuda-13.3-x64", (got, why)
        got, why = cuda_variant_for(_nvidia((8, 9), 13))        # RTX 4090
        assert got == "win-cuda-13.3-x64", (got, why)
        # A Blackwell card on a driver too old for CUDA 13 gets NOTHING,
        # not 12.4: 12.4 cannot run compute capability 12.0 at all, so
        # "fall back to the older CUDA" would be installing a launch failure.
        got, why = cuda_variant_for(_nvidia((12, 0), 12))
        assert got is None, (got, why)
        assert "12.0" in why and "CUDA 12" in why, why
        # A Turing card whose driver only reaches CUDA 12 gets 12.4.
        got, why = cuda_variant_for(_nvidia((7, 5), 12))
        assert got == "win-cuda-12.4-x64", (got, why)
        # Pascal: below CUDA 13's floor, inside 12.4's range.
        got, why = cuda_variant_for(_nvidia((6, 1), 13))
        assert got == "win-cuda-12.4-x64", (got, why)
        # Kepler: below both floors.
        got, why = cuda_variant_for(_nvidia((3, 5), 13))
        assert got is None, (got, why)
        # Unknown capability is never assumed modern.
        got, why = cuda_variant_for(_nvidia(None, 13))
        assert got is None and "could not be read" in why, (got, why)
        # nvidia-smi absent entirely.
        got, why = cuda_variant_for(None)
        assert got is None, (got, why)
        # Two cards with no CUDA build between them: neither, so that
        # Vulkan (which covers both) gets the job.
        mixed = _nvidia((12, 0), 13) + _nvidia((6, 1), 13, name="GTX 1080")
        got, why = cuda_variant_for(mixed)
        assert got is None, (got, why)
        # An unreadable driver ceiling is not a licence to install anyway.
        got, why = cuda_variant_for(_nvidia((12, 0), None))
        assert got is None and "driver" in why, (got, why)

        # -- the policy decides, and it says Vulkan for everyone ------------
        env = {}
        plan = choose_variant(manifest, gpus=nvidia_gpu, nvidia=_nvidia((12, 0)),
                              env=env, **win)
        assert plan["variant"] == "win-vulkan-x64", plan
        assert plan["backend"] == "vulkan" and plan["source"] == "policy", plan
        assert plan["size_bytes"] == 33_000_000, plan
        plan = choose_variant(manifest, gpus=amd_gpu, env=env, **win)
        assert plan["variant"] == "win-vulkan-x64", plan

        # No GPU: nothing to fetch, and the reason says so rather than
        # reading as a failure.
        plan = choose_variant(manifest, gpus=[], env=env, **win)
        assert plan["variant"] is None and "no GPU" in plan["reason"], plan

        # -- THE REGRESSION: a virtual adapter must not decide the engine --
        # This is the literal Win32_VideoController reading from an ASUS
        # G14 with Parsec installed, in the order Windows returns it. The
        # picker used to read the FIRST entry, get "unknown" from Parsec's
        # shim, and leave a Vulkan-capable Radeon 880M running on the CPU
        # build. All three assertions failed before this commit.
        g14 = [
            {"name": "Parsec Virtual Display Adapter", "vram_bytes": 0,
             "vendor": "unknown", "approximate": True, "virtual": True,
             "integrated": None},
            {"name": "AMD Radeon(TM) 880M Graphics", "vram_bytes": 536870912,
             "vendor": "amd", "approximate": True, "virtual": False,
             "integrated": True},
        ]
        plan = choose_variant(manifest, gpus=g14, env=env, **win)
        assert plan["variant"] == "win-vulkan-x64", plan
        assert plan["vendor"] == "amd", plan
        assert "880M" in plan["reason"], plan

        # An integrated GPU is still a GPU: Vulkan is the artifact chosen
        # precisely because it covers AMD and Intel from one build.
        for name, vendor in (("Intel(R) Iris(R) Xe Graphics", "intel"),
                             ("AMD Radeon(TM) Graphics", "amd")):
            igpu = [{"name": name, "vendor": vendor, "vram_bytes": 128 * 1024 ** 2,
                     "virtual": False, "integrated": True}]
            p = choose_variant(manifest, gpus=igpu, env=env, **win)
            assert p["variant"] == "win-vulkan-x64", (name, p)

        # With both a discrete card and the integrated one beside it, the
        # discrete card decides, whatever order they were enumerated in.
        hybrid = [
            {"name": "AMD Radeon(TM) 880M Graphics", "vram_bytes": 536870912,
             "vendor": "amd", "virtual": False, "integrated": True},
            {"name": "NVIDIA GeForce RTX 4060 Laptop GPU",
             "vram_bytes": 8 * 1024 ** 3, "vendor": "nvidia",
             "virtual": False, "integrated": False},
        ]
        assert _pick_gpu(hybrid)["vendor"] == "nvidia", _pick_gpu(hybrid)
        assert _pick_gpu(list(reversed(hybrid)))["vendor"] == "nvidia"
        # Through choose_variant, not just _pick_gpu directly: the picker
        # has to be the thing choose_variant actually consults, or reading
        # the first entry again would pass every assertion above.
        for order in (hybrid, list(reversed(hybrid))):
            p = choose_variant(manifest, gpus=order, nvidia=_nvidia((8, 9)),
                               env=env, **win)
            assert p["vendor"] == "nvidia", (order, p)
            assert "RTX 4060" in p["reason"], p
        # A real adapter that is not a shim and whose vendor could not be
        # established must not out-rank the identifiable GPU behind it,
        # whichever order they come in. Reading the first entry gets this
        # wrong, and "unknown" is the one value the policy cannot act on.
        muddled = [{"name": "Standard Display Adapter", "vram_bytes": 0,
                    "vendor": "unknown", "virtual": False, "integrated": None},
                   {"name": "AMD Radeon(TM) 880M Graphics", "vram_bytes": 536870912,
                    "vendor": "amd", "virtual": False, "integrated": True}]
        p = choose_variant(manifest, gpus=muddled, env=env, **win)
        assert p["vendor"] == "amd", p
        assert p["variant"] == "win-vulkan-x64", p
        # A card whose integrated flag could not be established is ranked
        # with the discrete ones, not demoted below an integrated part.
        unsure = [{"name": "AMD Radeon(TM) 880M Graphics", "vram_bytes": 536870912,
                   "vendor": "amd", "virtual": False, "integrated": True},
                  {"name": "AMD Radeon Series", "vram_bytes": 0, "vendor": "amd",
                   "virtual": False, "integrated": None}]
        assert _pick_gpu(unsure)["name"] == "AMD Radeon Series", _pick_gpu(unsure)
        assert _pick_gpu([]) is None

        # Only shims: there is no GPU here, and the reason names them
        # rather than implying the machine was never looked at.
        plan = choose_variant(manifest, gpus=[g14[0]], env=env, **win)
        assert plan["variant"] is None, plan
        assert "Parsec Virtual Display Adapter" in plan["reason"], plan
        assert "virtual" in plan["reason"], plan

        # An unrecognisable GPU is not guessed at, and the message has to
        # tell a person what that means for them and what they can do about
        # it. The old one read "the pin's first-run policy has no GPU engine
        # for a unknown GPU, so the CPU build stays", which names an
        # internal document, says nothing actionable, and is not English.
        plan = choose_variant(manifest, gpus=[{"name": "S3 ViRGE", "vendor": "unknown",
                                               "vram_bytes": 1}], env=env, **win)
        assert plan["variant"] is None, plan
        assert "S3 ViRGE" in plan["reason"], plan
        assert ENV_ENGINE in plan["reason"], plan
        assert "a unknown GPU" not in plan["reason"], plan
        assert "first-run policy" not in plan["reason"], plan
        # A GPU Hearth CAN identify but the pin has no build for reads
        # differently: that is the pin's limit, not a detection failure.
        no_build = json.loads(json.dumps(manifest))
        no_build["policy"]["first_run_gpu_fetch"] = {"nvidia": "win-vulkan-x64"}
        plan = choose_variant(no_build, gpus=g14, env=env, **win)
        assert plan["variant"] is None, plan
        assert "no GPU engine for AMD graphics" in plan["reason"], plan
        # No sentence a user reads picks an article from an internal
        # constant. "a amd GPU" shipped once; it does not ship again.
        for bad in (" a amd ", " a intel ", " a unknown ", " a nvidia "):
            for probe_gpus in (g14, [g14[0]], hybrid):
                r = choose_variant(manifest, gpus=probe_gpus, env=env,
                                   nvidia=_nvidia((8, 9)), **win)["reason"]
                assert bad not in " " + r + " ", (bad, r)

        # A pin with no policy at all does not crash and does not invent one.
        empty = json.loads(json.dumps(manifest))
        empty["policy"] = {}
        plan = choose_variant(empty, gpus=nvidia_gpu, env=env, **win)
        assert plan["variant"] is None and "no first-run" in plan["reason"], plan

        # -- HEARTH_GPU_ENGINE overrides the policy, not the hardware ------
        plan = choose_variant(manifest, gpus=nvidia_gpu, env={ENV_ENGINE: "off"}, **win)
        assert plan["variant"] is None and plan["source"] == "override", plan
        plan = choose_variant(manifest, gpus=nvidia_gpu, env={ENV_ENGINE: "cuda"},
                              nvidia=_nvidia((12, 0), 13), **win)
        assert plan["variant"] == "win-cuda-13.3-x64", plan
        assert plan["requires"] == ["cudart-cuda-13.3-x64"], plan
        # The size a user is asked to accept includes the cudart companion.
        assert plan["size_bytes"] == 146_000_000 + 391_000_000, plan
        # Asking for CUDA on a card that cannot run any CUDA build is
        # refused rather than honoured into a broken install.
        plan = choose_variant(manifest, gpus=nvidia_gpu, env={ENV_ENGINE: "cuda"},
                              nvidia=_nvidia((3, 5), 13), **win)
        assert plan["variant"] is None and plan["source"] == "override", plan
        # Naming the wrong CUDA build explicitly is refused too.
        plan = choose_variant(manifest, gpus=nvidia_gpu,
                              env={ENV_ENGINE: "win-cuda-12.4-x64"},
                              nvidia=_nvidia((12, 0), 13), **win)
        assert plan["variant"] is None, plan
        assert "12.4" in plan["reason"], plan["reason"]
        # A backend alias resolves; an unknown name is refused by name.
        plan = choose_variant(manifest, gpus=amd_gpu, env={ENV_ENGINE: "vulkan"}, **win)
        assert plan["variant"] == "win-vulkan-x64", plan
        plan = choose_variant(manifest, gpus=amd_gpu, env={ENV_ENGINE: "banana"}, **win)
        assert plan["variant"] is None and "banana" in plan["reason"], plan
        # The CPU build is not a GPU engine, whoever asks for it.
        plan = choose_variant(manifest, gpus=amd_gpu, env={ENV_ENGINE: "win-cpu-x64"},
                              **win)
        assert plan["variant"] is None and "not a GPU engine" in plan["reason"], plan
        # An engine for the wrong architecture is refused.
        plan = choose_variant(manifest, gpus=amd_gpu, env={ENV_ENGINE: "win-vulkan-x64"},
                              system="Windows", machine="ARM64")
        assert plan["variant"] is None and "ARM64" in plan["reason"], plan
        # ... and for the wrong operating system.
        plan = choose_variant(manifest, gpus=amd_gpu, env={ENV_ENGINE: "win-vulkan-x64"},
                              system="Linux", machine="x86_64")
        assert plan["variant"] is None and "Linux" in plan["reason"], plan

        # -- verify_engine: what may and may not become the engine ---------
        # Driven through the same shape probe_binary returns.
        def _probe(backend, devices, ok=True, error=None, offload=None):
            return {"ok": ok, "path": "x", "build": 10105, "commit": "abc",
                    "backend": backend, "devices": devices,
                    "gpu_offload": (backend in ("cuda", "vulkan", "rocm", "sycl",
                                                "metal")) if offload is None else offload,
                    "error": error}

        gpu_dev = [{"tag": "Vulkan0", "backend": "vulkan",
                    "name": "NVIDIA GeForce RTX 5080", "total_mib": 15977,
                    "free_mib": 15209}]

        import hearth_llama  # the real probe shape, and GPU_BACKENDS

        original_probe = hearth_llama.probe_binary
        try:
            hearth_llama.probe_binary = lambda p, **kw: _probe("vulkan", gpu_dev)
            ok, info, why = verify_engine("x", "vulkan")
            assert ok, (ok, why)
            assert "RTX 5080" in why, why

            # A build whose DLLs are missing does not run at all. This is
            # the failure that matters most and it must never activate.
            hearth_llama.probe_binary = lambda p, **kw: _probe(
                "unknown", [], ok=False,
                error="could not run x: [WinError 126] The specified module could "
                      "not be found")
            ok, _, why = verify_engine("x", "vulkan")
            assert not ok and "WinError 126" in why, (ok, why)

            # A GPU build that quietly came up on CPU is not a GPU engine.
            hearth_llama.probe_binary = lambda p, **kw: _probe("cpu", [])
            ok, _, why = verify_engine("x", "vulkan")
            assert not ok and "cpu" in why, (ok, why)

            # The right backend but no device: the driver is there and the
            # card is not, which is still not something to activate.
            hearth_llama.probe_binary = lambda p, **kw: _probe("vulkan", [])
            ok, _, why = verify_engine("x", "vulkan")
            assert not ok and "no GPU device" in why, (ok, why)

            # Backend and devices agree but the build says it cannot offload.
            hearth_llama.probe_binary = lambda p, **kw: _probe(
                "vulkan", gpu_dev, offload=False)
            ok, _, why = verify_engine("x", "vulkan")
            assert not ok and "cannot offload" in why, (ok, why)
        finally:
            hearth_llama.probe_binary = original_probe

        # -- a whole acquisition, offline, with no binary anywhere ---------
        root = os.path.join(tmp, "engines")
        fenv = {ENV_ENGINE_DIR: root}

        installed = {"calls": 0}

        def fake_install(variant=None, dest=None, manifest=None, cache=None,
                         on_progress=None, **_kw):
            installed["calls"] += 1
            os.makedirs(dest, exist_ok=True)
            server = os.path.join(dest, "llama-server.exe")
            with open(server, "wb") as fh:
                fh.write(b"MZ")
            if on_progress:
                total = manifest["variants"][variant]["size_bytes"]
                on_progress(total // 2, total, "v.zip")
                on_progress(total, total, "v.zip")
            return {"action": "installed", "variant": variant, "server": server,
                    "dir": dest, "files": ["llama-server.exe"]}

        def good_verify(server, backend):
            return True, _probe("vulkan", gpu_dev), "vulkan backend, RTX 5080"

        acq = Acquirer(manifest=manifest, env=fenv, install_fn=fake_install,
                       verify_fn=good_verify, gpus_fn=lambda: nvidia_gpu,
                       nvidia_fn=lambda: _nvidia((12, 0)),
                       disk_fn=lambda _p: 500 * 1024 ** 3)
        snap = acq.run_once()
        assert snap["state"] == STATE_ACTIVE, snap
        assert snap["variant"] == "win-vulkan-x64", snap
        assert snap["bytes_total"] == 33_000_000, snap
        assert snap["bytes_done"] == 33_000_000, snap
        assert "RTX 5080" in snap["message"], snap
        assert snap["active"]["backend"] == "vulkan", snap["active"]

        # The pointer is on disk and find_server-shaped.
        live = active(fenv)
        assert live and os.path.isfile(live["server"]), live
        assert active_server_path(fenv) == live["server"]
        assert live["release_tag"] == "b9999", live

        # A second run is a no-op: nothing is downloaded again.
        before = installed["calls"]
        snap = acq.run_once()
        assert snap["state"] == STATE_ACTIVE and installed["calls"] == before, snap

        # Confirming an already-active engine CLEARS a stale error from an
        # earlier attempt. Observed for real: a failed fetch from a badly
        # staged build left its message in state.json, and every later
        # reader saw an error sitting next to a working Vulkan engine.
        stale = read_state(fenv)
        stale.update({"state": STATE_FAILED, "error": "an old failure"})
        _write_json(state_path(fenv), stale)
        assert status(fenv)["error"] == "an old failure", status(fenv)
        acq.run_once()
        assert status(fenv)["error"] is None, status(fenv)
        assert read_state(fenv)["state"] == STATE_ACTIVE, read_state(fenv)

        # A moved pin re-acquires, because the engine has to match the
        # release the rest of Hearth was tested against.
        moved = json.loads(json.dumps(manifest))
        moved["release_tag"] = "b10000"
        acq_moved = Acquirer(manifest=moved, env=fenv, install_fn=fake_install,
                             verify_fn=good_verify, gpus_fn=lambda: nvidia_gpu,
                             nvidia_fn=lambda: _nvidia((12, 0)),
                             disk_fn=lambda _p: 500 * 1024 ** 3)
        snap = acq_moved.run_once()
        assert snap["state"] == STATE_ACTIVE and installed["calls"] == before + 1, snap
        assert active(fenv)["release_tag"] == "b10000", active(fenv)

        # A pin that cannot be read at all, with an engine already active:
        # the engine keeps working and the record is NOT overwritten with a
        # failure. Observed for real, from a packaged build staged without
        # its copy of the fetcher: the machine had a perfectly good Vulkan
        # engine and the panel would have called it broken.
        class _NoManifest(Acquirer):
            def run_once(self, force=False):
                original = globals()["_vendor_llama"]
                globals()["_vendor_llama"] = lambda: (_ for _ in ()).throw(
                    EngineError("this Hearth install has no scripts/vendor_llama.py"))
                try:
                    return super().run_once(force=force)
                finally:
                    globals()["_vendor_llama"] = original

        broken_pin = _NoManifest(env=fenv, install_fn=fake_install,
                                 verify_fn=good_verify, gpus_fn=lambda: nvidia_gpu,
                                 nvidia_fn=lambda: _nvidia((12, 0)),
                                 disk_fn=lambda _p: 500 * 1024 ** 3)
        snap = broken_pin.run_once()
        assert snap["state"] == STATE_ACTIVE, snap
        assert snap["error"] is None, snap
        assert active(fenv) is not None
        assert read_state(fenv)["state"] != STATE_FAILED, read_state(fenv)
        # With NO engine active it is a real failure, reported as one.
        shutil.rmtree(root, ignore_errors=True)
        snap = broken_pin.run_once()
        assert snap["state"] == STATE_FAILED, snap
        assert "vendor_llama" in snap["error"], snap

        # -- MUTATION: an engine that installs but does not run here -------
        # This is the load-time failure the whole design exists to prevent.
        # It must leave a working CPU install, delete the broken engine,
        # and say what happened.
        shutil.rmtree(root, ignore_errors=True)

        def bad_verify(server, backend):
            return False, _probe("unknown", [], ok=False,
                                 error="[WinError 126]"), "vulkan-1.dll is missing"

        acq_bad = Acquirer(manifest=manifest, env=fenv, install_fn=fake_install,
                           verify_fn=bad_verify, gpus_fn=lambda: nvidia_gpu,
                           nvidia_fn=lambda: _nvidia((12, 0)),
                           disk_fn=lambda _p: 500 * 1024 ** 3)
        snap = acq_bad.run_once()
        assert snap["state"] == STATE_FAILED, snap
        assert "vulkan-1.dll is missing" in snap["error"], snap
        assert active(fenv) is None, "a build that failed verification must not activate"
        assert active_server_path(fenv) is None
        assert not os.path.isdir(variant_dir("b9999", "win-vulkan-x64", fenv)), (
            "a build that failed verification must not be left on disk")

        # The failure is remembered against this hardware, so the next
        # launch does not download 33 MB again to fail the same way.
        calls_before = installed["calls"]
        snap = acq_bad.run_once()
        assert snap["state"] == STATE_SKIPPED, snap
        assert installed["calls"] == calls_before, "a known-bad variant was refetched"
        assert "did not work" in snap["message"], snap
        # ... but --force retries it, so a driver install is not a dead end.
        snap = acq_bad.run_once(force=True)
        assert snap["state"] == STATE_FAILED and installed["calls"] > calls_before, snap

        # A different machine (a new card, a new driver) is a fresh start.
        other = Acquirer(manifest=manifest, env=fenv, install_fn=fake_install,
                         verify_fn=good_verify, gpus_fn=lambda: amd_gpu,
                         nvidia_fn=lambda: None, disk_fn=lambda _p: 500 * 1024 ** 3)
        snap = other.run_once()
        assert snap["state"] == STATE_ACTIVE, snap

        # -- MUTATION: the download itself fails ---------------------------
        shutil.rmtree(root, ignore_errors=True)

        def exploding_install(**_kw):
            raise RuntimeError("SHA-256 mismatch for v.zip")

        acq_dl = Acquirer(manifest=manifest, env=fenv, install_fn=exploding_install,
                          verify_fn=good_verify, gpus_fn=lambda: nvidia_gpu,
                          nvidia_fn=lambda: _nvidia((12, 0)),
                          disk_fn=lambda _p: 500 * 1024 ** 3)
        snap = acq_dl.run_once()
        assert snap["state"] == STATE_FAILED and "SHA-256" in snap["error"], snap
        assert active(fenv) is None, "a failed download must not activate anything"

        # -- offline first run: no network, still a working install --------
        def offline_install(**_kw):
            raise RuntimeError("download of https://github.com/... failed: "
                               "<urlopen error [Errno 11001] getaddrinfo failed>")

        acq_off = Acquirer(manifest=manifest, env=fenv, install_fn=offline_install,
                           verify_fn=good_verify, gpus_fn=lambda: nvidia_gpu,
                           nvidia_fn=lambda: _nvidia((12, 0)),
                           disk_fn=lambda _p: 500 * 1024 ** 3)
        snap = acq_off.run_once()
        assert snap["state"] == STATE_FAILED, snap
        assert "getaddrinfo" in snap["error"], snap
        assert active(fenv) is None
        assert "CPU engine" in snap["message"], snap

        # -- a full disk is refused before anything is downloaded ----------
        shutil.rmtree(root, ignore_errors=True)
        acq_disk = Acquirer(manifest=manifest, env=fenv, install_fn=fake_install,
                            verify_fn=good_verify, gpus_fn=lambda: nvidia_gpu,
                            nvidia_fn=lambda: _nvidia((12, 0)),
                            disk_fn=lambda _p: 100 * 1024 ** 2)
        calls_before = installed["calls"]
        snap = acq_disk.run_once()
        assert snap["state"] == STATE_FAILED and "disk space" in snap["error"], snap
        assert installed["calls"] == calls_before, "a full disk must stop the download"

        # -- no GPU: skipped, not failed -----------------------------------
        shutil.rmtree(root, ignore_errors=True)
        acq_none = Acquirer(manifest=manifest, env=fenv, install_fn=fake_install,
                            verify_fn=good_verify, gpus_fn=lambda: [],
                            nvidia_fn=lambda: None, disk_fn=lambda _p: 500 * 1024 ** 3)
        snap = acq_none.run_once()
        assert snap["state"] == STATE_SKIPPED, snap
        assert snap["error"] is None, snap
        assert active(fenv) is None

        # -- demote: the launch-time fallback ------------------------------
        shutil.rmtree(root, ignore_errors=True)
        acq2 = Acquirer(manifest=manifest, env=fenv, install_fn=fake_install,
                        verify_fn=good_verify, gpus_fn=lambda: nvidia_gpu,
                        nvidia_fn=lambda: _nvidia((12, 0)),
                        disk_fn=lambda _p: 500 * 1024 ** 3)
        assert acq2.run_once()["state"] == STATE_ACTIVE
        server = active_server_path(fenv)
        assert server

        # A failure blamed on some OTHER binary must not demote this one.
        assert demote("unrelated", server_path=os.path.join(tmp, "elsewhere.exe"),
                      env=fenv) is False
        assert active_server_path(fenv) == server, "an unrelated failure demoted the engine"

        # A failure of THIS binary demotes it, and the CPU build takes over
        # because there is no longer a pointer to anything else.
        assert demote("it exited with code 3221225781 before announcing a port",
                      server_path=server, env=fenv) is True
        assert active(fenv) is None
        assert active_server_path(fenv) is None
        state = read_state(fenv)
        assert state["state"] == STATE_FAILED, state
        assert any(f["stage"] == "launch" for f in state["failed"]), state
        # Demoting twice is not an error and does not corrupt the record.
        assert demote("again", env=fenv) is False

        # -- snapshot versioning, which the SSE stream depends on ----------
        acq3 = Acquirer(manifest=manifest, env=fenv, install_fn=fake_install,
                        verify_fn=good_verify, gpus_fn=lambda: nvidia_gpu,
                        nvidia_fn=lambda: _nvidia((12, 0)),
                        disk_fn=lambda _p: 500 * 1024 ** 3)
        first = acq3.snapshot()
        assert first["state"] == STATE_IDLE and first["version"] == 0, first
        # A waiter that is already current times out with the same version
        # rather than blocking forever or raising.
        same = acq3.snapshot_after(first["version"], timeout=0.05)
        assert same["version"] == first["version"], same
        acq3.start()
        final = acq3.join(timeout=20)
        assert final["state"] == STATE_ACTIVE, final
        assert final["version"] > first["version"], final
        assert final["running"] is False, final
        # start() while a run is in flight does not start a second one.
        assert acq3.start()["state"] == STATE_ACTIVE

        # -- status(): cheap, runs nothing, never raises -------------------
        st = status(fenv)
        assert st["state"] == STATE_ACTIVE and st["backend"] == "vulkan", st
        assert st["device"] == "NVIDIA GeForce RTX 5080", st
        shutil.rmtree(root, ignore_errors=True)
        st = status(fenv)
        assert st["active"] is None and st["state"] == STATE_IDLE, st

        # -- a pointer to a binary that has been deleted is not active -----
        os.makedirs(root, exist_ok=True)
        _write_json(active_path(fenv), {"variant": "win-vulkan-x64",
                                        "server": os.path.join(tmp, "gone.exe"),
                                        "backend": "vulkan"})
        assert active(fenv) is None, "a pointer to a missing binary must not be honoured"
        assert active_server_path(fenv) is None
        # ... and neither is a corrupt one.
        with open(active_path(fenv), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert active(fenv) is None
        os.unlink(active_path(fenv))

        # -- the real pin's policy is one this module can actually act on --
        real = _vendor_llama().load_manifest()
        plan = choose_variant(real, gpus=nvidia_gpu, nvidia=_nvidia((12, 0)),
                              env={}, **win)
        assert plan["variant"], plan
        assert plan["backend"] in ("vulkan", "cuda"), plan
        for vendor in ("nvidia", "amd", "intel"):
            fake_gpu = [{"name": "test", "vendor": vendor, "vram_bytes": 1}]
            p = choose_variant(real, gpus=fake_gpu, nvidia=_nvidia((12, 0)),
                               env={}, **win)
            assert p["variant"], (vendor, p)
            assert vendor in (p["entry"]["gpu_vendors"] or []), (vendor, p)

        # -- live: the real fetch, on the real machine ---------------------
        if live:
            live_root = os.path.join(tmp, "live-engines")
            lenv = {ENV_ENGINE_DIR: live_root}
            real_acq = Acquirer(env=lenv)
            snap = real_acq.run_once()
            assert snap["state"] in (STATE_ACTIVE, STATE_SKIPPED), snap
            if snap["state"] == STATE_ACTIVE:
                data = active(lenv)
                assert os.path.isfile(data["server"]), data
                import hearth_llama as _hl
                info = _hl.probe_binary(data["server"])
                assert info["gpu_offload"] is True, info
                assert info["backend"] == data["backend"], (info, data)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
