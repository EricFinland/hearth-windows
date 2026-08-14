#!/usr/bin/env python3
r"""hearth's bundled llama.cpp server: find it, run it, talk to it, kill it.

Hearth's promise is that the user installs exactly one thing. Today that is
false: the engine assumes Ollama is installed, running, and has a model
pulled (see hearth_setup for how many ways that goes wrong). The plan is to
ship llama.cpp's `llama-server` inside Hearth and drive it ourselves. This
module owns that binary's entire lifecycle: discovery, launch, readiness,
model swap, one streaming chat call, and death.

## What was measured, and against what

Everything asserted below about llama-server's command line, logging, and
HTTP surface was measured against a real binary, not inferred from docs:

    llama-server --version
      version: 9608 (70b54e1)
      built with GNU 15.2.0 for Linux x86_64

on an x86_64 Linux host, CPU/BLAS build. Findings that matter here:

  * `--version` writes to STDERR, not stdout, and exits 0. A naive
    capture of stdout alone gets an empty string and reads as "the binary
    is broken". _parse_version reads whatever stream you hand it; the
    caller in probe_binary() merges both.
  * `--port 0` really does bind an ephemeral port. This is the whole
    reason Hearth never needs a fixed port: no collision with another
    Hearth, no collision with whatever else the user runs, nothing
    listening on a predictable number.
  * The chosen port is announced on STDERR as
        srv  llama_server: server is listening on http://127.0.0.1:45727
    That line survives `--no-log-prefix --no-log-timestamps` (only the
    leading timestamp and level are dropped), so PORT_RE keys off the
    "listening on http://host:port" tail rather than any prefix.
  * The announcement lands AFTER the model finishes loading, so seeing it
    is already a readiness signal in this build. Upstream has moved that
    line around across releases, so start() does not trust it alone: it
    then polls /health, which is authoritative in every build.
  * `GET /health` returns 503 with
        {"error":{"message":"Loading model","type":"unavailable_error",...}}
    while the weights are loading, and 200 with {"status":"ok"} once the
    model is resident. Measured by racing a poller against startup: 36
    consecutive 503s, then 200s. This is the "still loading" versus
    "ready" distinction, and it is why READY is a poll result and not a
    guess based on the process being alive.
  * A failed load exits 1 with `srv llama_server: exiting due to model
    loading error` on stderr. So "the process is gone" and "the model is
    slow" are genuinely distinguishable, and wait_ready() distinguishes
    them.
  * `POST /v1/chat/completions` is OpenAI-shaped. Streaming emits
    `data: {json}` frames separated by blank lines and terminated by
    `data: [DONE]`. Usage counts are ONLY present when the request sets
    stream_options.include_usage; without it the final frame carries
    llama.cpp's own `timings` object and no `usage`. Measured both ways.
  * Errors come back as {"error":{"code":400,"message":...,"type":...}}.

## Build variants

llama.cpp is compiled per backend, and the binary cannot switch: a CPU
build ignores -ngl entirely and says so on stderr

    warning: no usable GPU found, --gpu-layers option will be ignored
    warning: one possible reason is that llama.cpp was compiled without GPU support

which is why Hearth has to know which variant it shipped rather than
assuming -ngl did anything. `--list-devices` reports it:

    Available devices:
      BLAS: BLAS (0 MiB, 0 MiB free)

The device tag before the colon comes from ggml's backend registry, so a
CUDA build lists CUDA0, a Vulkan build Vulkan0, and so on. Only the
BLAS/CPU shape above was observed live; the CUDA and Vulkan tags are read
from that same registry naming and are classified by prefix, so an
unrecognised tag degrades to BACKEND_UNKNOWN rather than being guessed at.

## What the Windows build does differently

A second set of measurements, against the binary Hearth actually vendors:

    llama-server --version
      version: 10105 (e6dd0e29a)
      built with Clang 20.1.8 for Windows x86_64

from llama-b10105-bin-win-cpu-x64. Three differences from the Linux build
above, all of which this module had to be taught:

  * `--list-devices` on the Windows CPU build prints the header and NOTHING
    else -- literally `Available devices:\r\n` on stdout, zero device lines,
    no BLAS pseudo-device. The Linux CPU build's `BLAS: BLAS (0 MiB, ...)`
    entry does not exist here. An empty list therefore has to mean
    "CPU-only build", not "we could not tell"; see _pick_backend, which
    keys that distinction off the header rather than off the list being
    empty, so a binary that could not be run at all still reports UNKNOWN.
  * Startup stderr is COLOURISED, and not optionally so in Hearth's spawn
    path. llama.cpp's `--log-colors` defaults to `auto`, meaning "on when
    output is to a terminal". Hearth launches with stdout=DEVNULL, and
    Windows' CRT reports the NUL device as a character device, so isatty()
    answers true and `auto` resolves to ON. Every log line then arrives
    wrapped in SGR escapes:

        \x1b[34m0.00.035.313\x1b[0m \x1b[32mI \x1b[0msrv  llama_server: listening on http://127.0.0.1:65509

    That is a verbatim capture from _spawn_guarded's own pipe, not a
    reconstruction. It is why LOG_PREFIX_RE, anchored on `^\d`, matched
    nothing on Windows and _parse_devices silently returned []. Everything
    read off stderr now goes through _strip_ansi first, and the escapes are
    additionally suppressed at the source (see _llama_env). Measured, not
    assumed: `--no-log-prefix`/`--no-log-timestamps` do NOT suppress the
    colour (they drop the timestamp and level and leave the escapes), and
    NO_COLOR is not honoured at all; only --log-colors, or its twin
    LLAMA_ARG_LOG_COLORS=off, actually turns it off.
  * The port announcement lost a word. Build 9608 said `server is listening
    on http://...`; build 10105 says `listening on http://...`. PORT_RE
    survived that change untouched because it anchors on the tail, which is
    the entire argument for keeping it loose.

The startup device block also gained a log-module prefix in 10105: what was
`  - BLAS    : BLAS (0 MiB, 0 MiB free)` is now emitted as
`cmn  common_param:   - CPU     : AMD Ryzen 9 9900X ... (31800 MiB, 13835 MiB free)`,
so _parse_devices strips an optional `module  function: ` prefix as well as
the timestamp. The memory suffix stays mandatory, which is what keeps that
extra tolerance from turning log chatter into imaginary devices.

For Windows specifically the variants that matter are: cpu (works
everywhere, slow), cuda (NVIDIA, the fast path most Hearth users on a
gaming machine will get), and vulkan (AMD and Intel, the only broad
non-NVIDIA GPU option on Windows). Which one is bundled is a packaging
decision, not this module's; this module only reports what is present so a
setup screen can say "your GPU will not be used" before the user waits
five minutes for a CPU generation.

## Never leave an orphan

llama-server holds several GB of VRAM. If the Python sidecar dies (crash,
taskkill, power-user ending the task) the child must not survive holding
it, because nothing else will ever reap it and the next launch will fail
to allocate.

On Windows this is solved with a Job Object: hearth creates a job with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and assigns the child to it. The job
handle is owned by the Python process, so when that process dies by ANY
means -- including TerminateProcess, which runs no cleanup code -- the
kernel closes the last handle to the job and kills everything inside it.
That is the only mechanism on Windows that survives a hard kill of the
parent; atexit handlers, finally blocks and signal handlers all do not.
This is exercised by _self_test's orphan test, which hard-kills a parent
with `taskkill /F` (no /T, so the child is not in the blast radius) and
then asserts the child is gone anyway.

On Linux the equivalent is prctl(PR_SET_PDEATHSIG, SIGKILL) in the child,
set via preexec_fn after Popen has already called setsid() for us.

Orderly shutdown is separate and reuses hearth_proc's tree kill rather
than reimplementing taskkill/killpg here.

## Constraints

Standard library only, Python 3.12+. Shipping the llama-server binary is a
packaging concern and adds no Python dependency. Every call that touches
the network or a subprocess is bounded by a timeout. Nothing here writes to
the user's data directory. This module does not wire itself into
hearth_loop; the surface is deliberately shaped to sit behind a backend
abstraction next to the existing Ollama path, and that wiring is someone
else's task.
"""

import argparse
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hearth_engine
import hearth_hw
import hearth_paths
import hearth_proc


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Environment override for the binary. Checked first by find_server(), so a
#: developer or a packager can point Hearth at a specific build without
#: touching the install tree.
ENV_SERVER = "HEARTH_LLAMA_SERVER"

#: Environment override for the directory the bundled binary lives in, for
#: the case where the layout is known but the filename should stay default.
ENV_SERVER_DIR = "HEARTH_LLAMA_DIR"

#: Override for the -ngl choice, mirroring hearth_loop's HEARTH_NUM_CTX
#: escape hatch. A non-negative integer, or the strings "auto"/"all", which
#: llama-server accepts directly.
ENV_GPU_LAYERS = "HEARTH_GPU_LAYERS"

SERVER_BASENAME = "llama-server"

#: Subdirectories, relative to the application root, where a packaged Hearth
#: is expected to drop the llama.cpp binaries. Ordered most-specific first.
BUNDLED_SUBDIRS = ("llama", os.path.join("bin", "llama"), "bin", "vendor/llama")

#: Discovery outcomes.
FOUND_ENV = "env"
FOUND_ACQUIRED = "acquired"
FOUND_BUNDLED = "bundled"
FOUND_PATH = "path"
FOUND_MISSING = "missing"

#: Backend tags, as classified from `--list-devices` / the startup device
#: block. BACKEND_UNKNOWN is a real answer, not a failure: an unrecognised
#: ggml backend name must not be silently reported as CPU.
BACKEND_CPU = "cpu"
BACKEND_CUDA = "cuda"
BACKEND_VULKAN = "vulkan"
BACKEND_ROCM = "rocm"
BACKEND_SYCL = "sycl"
BACKEND_METAL = "metal"
BACKEND_UNKNOWN = "unknown"

#: ggml backend registry name prefix -> Hearth backend tag. Matched
#: case-insensitively against the text before the colon in a device line.
#: BLAS and CPU both mean "no GPU offload is possible with this build".
_BACKEND_PREFIXES = (
    ("cuda", BACKEND_CUDA),
    ("vulkan", BACKEND_VULKAN),
    ("rocm", BACKEND_ROCM),
    ("hip", BACKEND_ROCM),
    ("sycl", BACKEND_SYCL),
    ("metal", BACKEND_METAL),
    ("blas", BACKEND_CPU),
    ("cpu", BACKEND_CPU),
)

#: Backends that can actually hold model layers. A build whose only devices
#: are CPU/BLAS ignores -ngl, and says so on stderr.
GPU_BACKENDS = (BACKEND_CUDA, BACKEND_VULKAN, BACKEND_ROCM, BACKEND_SYCL, BACKEND_METAL)

#: Server lifecycle states, as reported by poll_state()/wait_ready().
STATE_STARTING = "starting"   # process alive, port not announced yet
STATE_LOADING = "loading"     # port known, /health says 503 Loading model
STATE_READY = "ready"         # /health says 200 ok
STATE_CRASHED = "crashed"     # process exited before becoming ready
STATE_STOPPED = "stopped"     # we stopped it on purpose

#: Bounded waits. Loading a large GGUF off a cold spinning disk genuinely
#: takes minutes, so the readiness ceiling is generous; every other timeout
#: here is short, because they cover operations that are fast or broken.
DEFAULT_READY_TIMEOUT = 600
HEALTH_TIMEOUT = 5
PROBE_TIMEOUT = 15
STOP_GRACE_SECONDS = 10
POLL_INTERVAL = 0.25

#: The default chat request timeout. Generation on a CPU-only build is slow
#: enough that a short ceiling would cut off legitimate answers.
DEFAULT_CHAT_TIMEOUT = 600

#: Measured against the real binary: the port announcement is on stderr and
#: reads `srv  llama_server: server is listening on http://127.0.0.1:45727`.
#: Anchored on the "listening on" tail so it survives --no-log-prefix and
#: any future change to the log module name, and it accepts https:// in case
#: a build is ever launched with --ssl-cert-file.
#:
#: DO NOT tighten this into a full-line anchor. The looseness has already
#: paid for itself twice against real builds: 9608 wrote `server is
#: listening on http://...` and 10105 dropped the "server is", and 10105
#: additionally wraps the whole line in ANSI colour escapes (see
#: _strip_ansi), so a pattern anchored on the line start would have broken
#: on both changes. Matching the smallest distinctive tail is the point.
PORT_RE = re.compile(r"listening on\s+https?://(?P<host>[^\s:/]+):(?P<port>\d+)")

#: SGR/CSI colour escapes, plus the OSC and single-character forms for
#: completeness. Build 10105 colourises stderr whenever llama.cpp's
#: `--log-colors auto` decides it is talking to a terminal, which on Windows
#: it does even when stderr is a pipe, because Hearth's stdout=DEVNULL makes
#: the CRT's isatty() answer true for the NUL character device. Every
#: alternative below is written so no quantifier can overlap the token that
#: follows it (`[0-9;:?]`, `[ -/]` and `[@-~]` are disjoint ranges), which
#: keeps this linear on hostile input rather than backtracking.
ANSI_RE = re.compile(
    r"\x1b\[[0-9;:?]*[ -/]*[@-~]"           # CSI ... final byte (SGR is CSI m)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC ... BEL or ST
    r"|\x1b[@-Z\\-_]"                       # two-character Fe escapes
)

#: `version: 9608 (70b54e1)` -- llama.cpp reports a monotonic build number
#: and a commit, not a semver, so there is nothing to compare with a `>=`.
VERSION_RE = re.compile(r"^\s*version:\s*(?P<build>\d+)\s*(?:\((?P<commit>[0-9a-f]+)\))?", re.MULTILINE)

#: A device line from `--list-devices` or the startup device block, e.g.
#: `  BLAS: BLAS (0 MiB, 0 MiB free)` or `  - CPU     : Intel(R) ... (15854 MiB, ...)`.
#:
#: The trailing "(N MiB, M MiB free)" is REQUIRED, not optional, and that is
#: what makes this safe to run over raw stderr: llama-server's own warnings
#: are also "word: text" shaped ("warning: no usable GPU found, ..."), so a
#: pattern without the memory suffix happily reports a device named
#: "warning". Every device line observed on the real binary carries the
#: suffix, including the 0 MiB BLAS pseudo-device.
DEVICE_RE = re.compile(
    r"^\s*-?\s*(?P<tag>[A-Za-z][A-Za-z0-9_]*)\s*:\s*(?P<name>.*?)"
    r"\s*\((?P<total>[\d.]+)\s*MiB,\s*(?P<free>[\d.]+)\s*MiB free\)\s*$"
)

#: llama-server's default log prefix: elapsed time then a one-letter level,
#: e.g. `0.00.032.295 I `. Stripped before matching a device line, so the
#: same parser works on `--list-devices` output (no prefix) and on the
#: startup device_info block (prefixed). Suppressible with --no-log-prefix,
#: hence "strip if present" rather than "require".
#:
#: This is anchored on `^\d` and so cannot match a colourised line, which is
#: exactly how the Windows device-block parsing came to find nothing.
#: _strip_ansi has to run BEFORE this, never after.
LOG_PREFIX_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+\s+[A-Z]\s+")

#: The log module and function that build 10105 prints after the timestamp
#: and level, e.g. `cmn  common_param: ` or `srv    load_model: `. Build 9608
#: put the startup device block directly after the level, 10105 puts it after
#: this, so it is stripped too -- if present, and only at the very start of
#: an already-prefix-stripped line.
#:
#: Two words are required, which is what keeps it away from the single-word
#: `warning: ...` and `load: ...` lines that sit in the same stream and must
#: not be mistaken for structure.
LOG_MODULE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\s+[A-Za-z_][A-Za-z0-9_()<>]*:\s")

#: The header `--list-devices` always prints, even when it goes on to list
#: nothing at all. Its presence is the difference between "the binary ran and
#: found no offloadable device" (a CPU-only build) and "we never got an
#: answer" (genuinely unknown); see _pick_backend.
DEVICE_HEADER_RE = re.compile(r"^\s*Available devices:", re.MULTILINE)

#: Stderr lines that mean the build cannot offload at all, whatever -ngl said.
NO_GPU_MARKER = "no usable GPU found"

#: How much VRAM to leave unclaimed when picking -ngl, mirroring
#: hearth_loop's MIN_HEADROOM_BYTES/MIN_HEADROOM_FRACTION exactly rather than
#: inventing a second, differently-tuned safety margin for the same GPU.
MIN_HEADROOM_BYTES = 512 * 1024 ** 2
MIN_HEADROOM_FRACTION = 0.05

#: Used when a GGUF's real layer count is not known. Deliberately a count
#: that no real model exceeds by much, since -ngl is clamped by llama-server
#: to the model's actual layer count anyway.
ASSUMED_LAYERS = 32

#: How many bytes of stderr to retain from a running server. Enough to hold
#: the whole startup banner plus a loading error, bounded so a server that
#: logs for a week cannot grow the sidecar's memory without limit.
STDERR_RING_BYTES = 64 * 1024


class LlamaError(RuntimeError):
    """Anything this module refuses to paper over: no binary, a server that
    died during load, a chat call against a server that is not up."""


# --------------------------------------------------------------------------
# Binary discovery
# --------------------------------------------------------------------------

def _strip_ansi(text):
    """`text` with terminal colour escapes removed.

    Applied to everything read off llama-server's stderr, for two reasons.
    The parsers are anchored patterns and an escape sequence at the start of
    a line defeats every one of them, and the stderr tail is shown to the
    user inside LlamaError messages, where raw escape bytes are noise at
    best and mojibake in a GUI text box at worst.

    This is the downstream half of the defence. The escapes are also
    suppressed at the source (see _llama_env), but that relies on the binary
    honouring an option, and this module deliberately runs against whatever
    build it is pointed at, including ones that predate it.
    """
    if not text or "\x1b" not in text:
        return text or ""
    return ANSI_RE.sub("", text)


def _llama_env(extra=None):
    """The environment to run llama-server in.

    hearth_proc.child_env()'s allow list, plus one pin: colour off.

    llama.cpp's `--log-colors` defaults to `auto`, which its own help text
    describes as "enables colors when output is to a terminal". Hearth
    launches with stdout=DEVNULL, and on Windows the CRT reports the NUL
    device as a character device, so isatty() says terminal and `auto`
    resolves to ON. Pinning the environment twin settles it instead of
    depending on how a given build asks that question.

    The environment twin is used rather than an argv flag on purpose. Build
    10105 spells this `--log-colors [on|off|auto]`, but older builds spell
    the same option as a bare boolean flag, and passing `--log-colors off` to
    one of those would leave `off` as an unparseable positional and fail the
    launch outright. An unrecognised environment variable is ignored
    instead, and a build whose twin is boolean acts only on "1"/"true", so
    "off" is inert there and those builds default to colourless anyway.

    Measured against 10105 rather than assumed: `off` is accepted and an
    invalid value is rejected loudly (exit 1, "unknown value for
    --log-colors"), which is how "off" was confirmed to be a value this
    build understands rather than one it silently ignores. Also measured:
    --no-log-prefix/--no-log-timestamps do NOT suppress the colour, and
    NO_COLOR is not honoured at all, so neither is used here.
    """
    env = hearth_proc.child_env(extra)
    env["LLAMA_ARG_LOG_COLORS"] = "off"
    return env


def _exe(name):
    """The platform's executable filename for `name`."""
    return name + ".exe" if hearth_paths.is_windows() else name


def app_root():
    """The directory a packaged Hearth is installed into.

    For a frozen build that is the directory holding the executable; running
    from a checkout it is the repository root (this file's parent's parent).
    Overridable with HEARTH_DATA_DIR's sibling env var so a test never has to
    guess where the harness put things.
    """
    override = os.environ.get(ENV_SERVER_DIR)
    if override:
        return override
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bundled_candidates():
    """Paths under the install root where the bundled binary may sit."""
    root = app_root()
    out = [os.path.join(root, _exe(SERVER_BASENAME))]
    for sub in BUNDLED_SUBDIRS:
        out.append(os.path.join(root, sub.replace("/", os.sep), _exe(SERVER_BASENAME)))
    return out


def _is_runnable(path):
    """True when `path` names a file this process could execute.

    long_path() is applied for the stat, because a Hearth installed deep
    under a redirected OneDrive Desktop can easily push a bundled binary past
    Windows' 260-character MAX_PATH, at which point a plain os.path.isfile
    reports False for a file that is really there.
    """
    if not path:
        return False
    try:
        p = hearth_paths.long_path(path)
        if not os.path.isfile(p):
            return False
    except (OSError, ValueError):
        return False
    if hearth_paths.is_windows():
        return True
    return os.access(p, os.X_OK)


def find_server(env=None):
    """Locate the llama-server executable.

    Resolution order, most explicit first:

      1. %HEARTH_LLAMA_SERVER% -- a full path to the binary.
      2. The GPU engine hearth_engine fetched and verified for this machine,
         if there is one. It comes before the bundled build because it is
         strictly better when it exists: it only exists at all after it was
         downloaded against a pinned hash AND proved to run here AND
         reported a real GPU device. When it does not exist, or its binary
         has been deleted, this step is silently skipped and the bundled
         build takes over, which is what makes the fallback free.
      3. The bundled location under the install root (see BUNDLED_SUBDIRS),
         which is where a shipped Hearth actually finds it.
      4. PATH, so a developer with llama.cpp already installed does not have
         to stage a copy.

    Returns a dict, never raises:

        {"found": bool, "path": str|None, "source": FOUND_*,
         "searched": [str, ...], "reason": str}

    `searched` lists every location tried, in order, so a setup screen can
    tell the user exactly where Hearth looked instead of just "not found".
    An env override that points at a missing file is reported as a distinct
    reason rather than silently falling through to PATH: a wrong override is
    a configuration mistake the user needs told about, not something to
    quietly work around.
    """
    env = os.environ if env is None else env
    searched = []

    override = (env.get(ENV_SERVER) or "").strip().strip('"')
    if override:
        searched.append(override)
        if _is_runnable(override):
            return {"found": True, "path": os.path.abspath(override), "source": FOUND_ENV,
                    "searched": searched,
                    "reason": "{} points at this binary".format(ENV_SERVER)}
        return {"found": False, "path": None, "source": FOUND_MISSING, "searched": searched,
                "reason": ("{} is set to {!r} but no executable is there; unset it or fix "
                           "the path".format(ENV_SERVER, override))}

    acquired = hearth_engine.active_server_path(env)
    if acquired:
        searched.append(acquired)
        if _is_runnable(acquired):
            live = hearth_engine.active(env) or {}
            return {"found": True, "path": os.path.abspath(acquired),
                    "source": FOUND_ACQUIRED, "searched": searched,
                    "reason": "the {} engine Hearth fetched and verified for this "
                              "machine".format(live.get("backend") or "GPU")}

    for cand in _bundled_candidates():
        searched.append(cand)
        if _is_runnable(cand):
            return {"found": True, "path": os.path.abspath(cand), "source": FOUND_BUNDLED,
                    "searched": searched, "reason": "bundled with this Hearth install"}

    on_path = shutil.which(_exe(SERVER_BASENAME)) or shutil.which(SERVER_BASENAME)
    searched.append("PATH ({})".format(_exe(SERVER_BASENAME)))
    if on_path and _is_runnable(on_path):
        return {"found": True, "path": os.path.abspath(on_path), "source": FOUND_PATH,
                "searched": searched, "reason": "found on PATH"}

    return {"found": False, "path": None, "source": FOUND_MISSING, "searched": searched,
            "reason": ("no {} executable found; Hearth ships one, so this install is "
                       "incomplete".format(_exe(SERVER_BASENAME)))}


def _parse_version(text):
    """(build_number, commit) from llama-server's `--version` output.

    llama.cpp prints `version: 9608 (70b54e1)` and reports a monotonically
    increasing build number rather than a semver, so there is no meaningful
    `>=` comparison to make and none is offered here. (None, None) when the
    text does not contain a version line at all.

    Note for callers: the real binary writes this to STDERR and exits 0.
    """
    m = VERSION_RE.search(text or "")
    if not m:
        return None, None
    return int(m.group("build")), m.group("commit")


def _classify_backend(tag):
    """Map a ggml device tag ("CUDA0", "BLAS", "Vulkan1") to a Hearth backend
    constant. BACKEND_UNKNOWN for anything unrecognised, so a future ggml
    backend is reported honestly instead of being filed under CPU."""
    low = (tag or "").strip().lower()
    for prefix, backend in _BACKEND_PREFIXES:
        if low.startswith(prefix):
            return backend
    return BACKEND_UNKNOWN


def _parse_devices(text):
    """Devices from `--list-devices` output (or the startup device block).

    Returns a list of {"tag", "backend", "name", "total_mib", "free_mib"}.
    Lines that are not device lines are skipped; the header
    ("Available devices:", "device_info:") is not a device because it has
    nothing after the colon.

    Each line is peeled in a fixed order before it is matched: colour
    escapes, then the timestamp/level prefix, then the log module prefix.
    The order is load-bearing -- LOG_PREFIX_RE is anchored on `^\\d` and a
    leading escape sequence makes it match nothing, which is precisely how
    this returned [] for every Windows startup block.
    """
    devices = []
    for raw in (text or "").splitlines():
        line = LOG_PREFIX_RE.sub("", _strip_ansi(raw))
        if not line.strip() or line.rstrip().endswith(":"):
            continue
        line = LOG_MODULE_RE.sub("", line)
        m = DEVICE_RE.match(line)
        if not m:
            continue
        name = (m.group("name") or "").strip()
        if not name:
            continue
        devices.append({
            "tag": m.group("tag"),
            "backend": _classify_backend(m.group("tag")),
            "name": name,
            "total_mib": int(float(m.group("total"))),
            "free_mib": int(float(m.group("free"))),
        })
    return devices


def _pick_backend(devices, text=""):
    """The build's effective backend: the first GPU-capable device if there is
    one, else CPU. An explicit "no usable GPU found" warning in `text` wins
    over any device list, because that warning is the binary telling us
    directly that -ngl will be ignored.

    BACKEND_UNKNOWN is reserved for the one case it should mean: we could not
    tell. Two situations look superficially like "no GPU" and are kept apart
    here, because collapsing them is what put "unknown backend" in front of
    users with a perfectly healthy CPU install:

      * The binary ran and listed devices, and there were none. The Windows
        CPU build does exactly this -- `Available devices:` and not one line
        after it, where the Linux CPU build emits a `BLAS: BLAS (0 MiB, ...)`
        pseudo-device. Nothing to offload to is what a CPU-only build IS, so
        that is reported as CPU. The header is the evidence that the listing
        happened at all; without it there is no answer to report.
      * The binary listed devices and none of their tags are recognised. That
        stays UNKNOWN. A future ggml backend must never be filed under CPU on
        the strength of "well, it is not one of the GPUs we know", because
        the caller would then be told -ngl is pointless when it may not be.
    """
    if NO_GPU_MARKER in (text or ""):
        return BACKEND_CPU
    for d in devices:
        if d["backend"] in GPU_BACKENDS:
            return d["backend"]
    if devices:
        # No GPU among them. Recognising at least one as CPU/BLAS makes this
        # a CPU build; recognising none of them makes it a build we cannot
        # describe, whatever else is true.
        return BACKEND_CPU if any(d["backend"] == BACKEND_CPU for d in devices) \
            else BACKEND_UNKNOWN
    if DEVICE_HEADER_RE.search(_strip_ansi(text)):
        return BACKEND_CPU
    return BACKEND_UNKNOWN


def probe_binary(path=None, timeout=PROBE_TIMEOUT):
    """What this llama-server build is: version, devices, effective backend.

    Runs `--version` and `--list-devices`, both of which load the backend
    registry and exit; neither loads a model, so this is cheap enough for a
    setup screen. stdout and stderr are merged because the real binary writes
    its version banner to stderr.

    Returns a dict, never raises:

        {"ok": bool, "path": str|None, "build": int|None, "commit": str|None,
         "backend": BACKEND_*, "gpu_offload": bool, "devices": [...],
         "error": str|None}

    gpu_offload is the answer to the only question a caller actually has:
    will -ngl do anything on this build?
    """
    if path is None:
        found = find_server()
        if not found["found"]:
            return {"ok": False, "path": None, "build": None, "commit": None,
                    "backend": BACKEND_UNKNOWN, "gpu_offload": False, "devices": [],
                    "error": found["reason"]}
        path = found["path"]

    def _run(args):
        try:
            proc = subprocess.run(
                [path] + args, capture_output=True, timeout=timeout,
                encoding="utf-8", errors="replace", env=_llama_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, str(exc)
        # Stripped here rather than in each parser: `error` strings built
        # from this text end up in front of the user too.
        return _strip_ansi((proc.stdout or "") + (proc.stderr or "")), None

    ver_text, err = _run(["--version"])
    if err is not None:
        return {"ok": False, "path": path, "build": None, "commit": None,
                "backend": BACKEND_UNKNOWN, "gpu_offload": False, "devices": [],
                "error": "could not run {}: {}".format(path, err)}
    build, commit = _parse_version(ver_text)

    dev_text, err = _run(["--list-devices"])
    if err is not None:
        dev_text = ""
    devices = _parse_devices(dev_text)
    backend = _pick_backend(devices, (dev_text or "") + (ver_text or ""))

    return {
        "ok": build is not None,
        "path": path,
        "build": build,
        "commit": commit,
        "backend": backend,
        "gpu_offload": backend in GPU_BACKENDS,
        "devices": devices,
        "error": None if build is not None else
                 "{} ran but printed no version line".format(path),
    }


# --------------------------------------------------------------------------
# Layer offload and context selection
# --------------------------------------------------------------------------

def _model_bytes(model_path):
    """The GGUF's size on disk, which is very close to its resident weight
    size (llama.cpp mmaps the file). None when it cannot be stat'd."""
    try:
        return os.path.getsize(hearth_paths.long_path(model_path))
    except OSError:
        return None


#: How much of system RAM an integrated GPU's offload may plan to occupy.
#: An iGPU shares system memory, so this is not a second pool being spent; it
#: is the same RAM the weights would sit in anyway. The fraction exists to
#: leave the rest of the machine usable, not to protect a separate device.
SHARED_MEMORY_FRACTION = 0.7


def choose_gpu_layers(model_path, backend=None, vram_bytes=None,
                      total_layers=ASSUMED_LAYERS, env=None, integrated=None):
    """How many layers to offload to the GPU, and why.

    Returns (n_gpu_layers, reason). n_gpu_layers is an int, or the string
    "all" when the whole model comfortably fits (llama-server accepts both).

    The arithmetic is hearth_hw.fits(), the same calculator the shop and
    hearth_loop's context ladder use, rather than a second opinion about the
    same GPU. The model's weights are prorated per layer: offloading k of n
    layers costs roughly k/n of the weights, plus the KV cache, which lives
    on the GPU whenever any layer does. A margin identical to hearth_loop's
    (MIN_HEADROOM_BYTES or MIN_HEADROOM_FRACTION, whichever is larger) is
    held back, because a model that fails to allocate is worse than one that
    runs a few layers on the CPU.

    Zero is returned, honestly and with a reason, when the build cannot
    offload at all or when no GPU was detected. HEARTH_GPU_LAYERS overrides
    everything, including the "this build has no GPU support" case, so an
    operator can force the issue on a build we misclassified.
    """
    env = os.environ if env is None else env

    override = (env.get(ENV_GPU_LAYERS) or "").strip()
    if override:
        if override.lower() in ("auto", "all"):
            return override.lower(), "{} override".format(ENV_GPU_LAYERS)
        try:
            v = int(override)
            if v >= 0:
                return v, "{} override".format(ENV_GPU_LAYERS)
        except ValueError:
            pass

    if backend is not None and backend not in GPU_BACKENDS:
        return 0, ("this llama-server build has no GPU backend ({}), so layers stay "
                   "on the CPU".format(backend))

    if vram_bytes is None:
        try:
            found = hearth_hw.gpus()
        except Exception:  # noqa: BLE001 - hardware probing must never break a launch
            found = []
        best = max(found, key=lambda g: g.get("vram_bytes") or 0) if found else None
        vram_bytes = (best.get("vram_bytes") or 0) if best else 0
        if integrated is None and best is not None:
            integrated = bool(best.get("integrated"))

    if not vram_bytes or vram_bytes <= 0:
        return 0, "no GPU VRAM detected; running entirely on the CPU"

    weights = _model_bytes(model_path)
    if not weights:
        return 0, "cannot size {!r}; refusing to guess an offload".format(model_path)

    if not total_layers or total_layers <= 0:
        total_layers = ASSUMED_LAYERS

    # An integrated GPU has no memory of its own, so its reported VRAM is not
    # a budget and must not be spent like one.
    #
    # Windows reports an iGPU's memory through Win32_VideoController.AdapterRAM,
    # which is a signed 32-bit field describing a carve-out rather than a
    # capacity: a Radeon 880M in a machine with 33 GiB of RAM reports 512 MiB.
    # hearth_hw already says so, marking such readings integrated=True and
    # approximate=True. This function used to ignore both and divide 512 MiB by
    # the per-layer cost, which puts essentially the whole model on the CPU.
    #
    # The reason that is wrong is not that the number is small. It is that the
    # number is not describing the constraint at all. Weights for an iGPU live
    # in system RAM whichever processor reads them, so offloading a layer moves
    # work across the die, it does not move bytes into a second pool. The
    # question worth asking is whether the model fits in system RAM, which is
    # the question asked below.
    #
    # Measured on a Radeon 880M running Dolphin 3.0 Llama 3.1 8B Q4_K_M through
    # the Vulkan build: 9.7 tok/s with no layers offloaded against 17.1 with all
    # of them. A 1.8x speedup declined on the strength of a number that never
    # meant what it was read as meaning.
    #
    # Worth recording how that reading was obtained, because it is a second
    # trap. That laptop also holds a discrete RTX 5070 Ti, and at the time of
    # the measurement Windows was not enumerating it at all: nvidia-smi could
    # not reach a driver and Win32_VideoController listed only the iGPU, because
    # NVIDIA's dynamic switching had the card powered down. Every probe was
    # telling the truth about what was visible, and what was visible was not the
    # whole machine. So a GPU census taken while nothing is drawing on the GPU
    # can be wrong in the direction of finding less, and the iGPU path here is
    # what such a machine falls back to. It should be fast when it is used.
    if integrated:
        try:
            ram = hearth_hw.system_ram_bytes()
        except Exception:  # noqa: BLE001 - never break a launch over a probe
            ram = 0
        if ram > 0:
            allowance = int(ram * SHARED_MEMORY_FRACTION)
            if weights < allowance:
                return "all", (
                    "an integrated GPU shares system memory, so its reported "
                    "{:.1f} GiB is a carve-out and not a limit; the {:,}-byte model "
                    "fits inside {:.0f}% of {:.1f} GiB of system RAM, so every layer "
                    "goes to the GPU".format(
                        vram_bytes / 1024 ** 3, weights,
                        SHARED_MEMORY_FRACTION * 100, ram / 1024 ** 3))
            return 0, (
                "an integrated GPU shares system memory and the {:,}-byte model does "
                "not fit inside {:.0f}% of {:.1f} GiB of system RAM; staying on the "
                "CPU".format(weights, SHARED_MEMORY_FRACTION * 100, ram / 1024 ** 3))

    margin = max(MIN_HEADROOM_BYTES, int(vram_bytes * MIN_HEADROOM_FRACTION))
    budget = vram_bytes - margin
    if budget <= 0:
        return 0, "{:.1f} GiB VRAM is entirely consumed by the safety margin".format(
            vram_bytes / 1024 ** 3)

    per_layer = weights / float(total_layers)
    n = int(budget // per_layer) if per_layer > 0 else 0
    n = max(0, min(n, total_layers))

    if n >= total_layers:
        return "all", ("{:.1f} GiB VRAM holds all ~{} layers of a {:,}-byte model with "
                       "{:.1f} GiB to spare".format(vram_bytes / 1024 ** 3, total_layers,
                                                    weights, (budget - weights) / 1024 ** 3))
    return n, ("{:.1f} GiB VRAM (less a {:.1f} GiB margin) holds {} of ~{} layers of a "
               "{:,}-byte model".format(vram_bytes / 1024 ** 3, margin / 1024 ** 3,
                                        n, total_layers, weights))


def choose_ctx_size(model_path=None, vram_bytes=None, kv_bytes_per_token=None):
    """The context size to launch with, from hearth_loop's existing ladder.

    Imported lazily and by name so this module owns no second ladder: if
    hearth_loop's CONTEXT_LADDER or FALLBACK_NUM_CTX changes, this follows.
    hearth_loop's chooser talks to Ollama's /api/show for the model's
    architecture, which is exactly the dependency Hearth is removing, so the
    pure arithmetic (_choose_num_ctx) is used directly with hearth_loop's own
    conservative constants when the caller cannot supply better numbers.
    """
    import hearth_loop  # deferred: hearth_loop is large and rarely needed here

    if vram_bytes is None:
        try:
            found = hearth_hw.gpus()
        except Exception:  # noqa: BLE001 - hardware probing must never break a launch
            found = []
        vram_bytes = max((g.get("vram_bytes") or 0) for g in found) if found else 0

    weights = _model_bytes(model_path) if model_path else None
    if not weights:
        weights = hearth_loop.FALLBACK_MODEL_BYTES
    if not kv_bytes_per_token:
        kv_bytes_per_token = hearth_loop.FALLBACK_KV_BYTES_PER_TOKEN

    return hearth_loop._choose_num_ctx(vram_bytes, weights, kv_bytes_per_token)


def build_argv(server_path, model_path, port=0, host="127.0.0.1", n_gpu_layers=None,
               ctx_size=None, alias=None, parallel=1, extra=None):
    """The exact command line to launch llama-server with.

    Split out from start() so the whole argument surface is testable without
    a binary present, and so a caller can log or show the user precisely what
    Hearth is about to run.

    Deliberate choices, each measured against the real `--help`:

      --host 127.0.0.1  loopback only. llama-server's own default is already
                        127.0.0.1, but it is passed explicitly so that a
                        stray LLAMA_ARG_HOST in the user's environment (every
                        flag has an env twin) cannot silently expose the
                        model to the network.
      --port 0          ephemeral. The kernel picks; we read it back.
      --no-webui        the bundled browser UI is dead weight inside Hearth
                        and a second HTTP attack surface.
      --metrics/--props are NOT enabled. Both are off by default and stay off.
      --no-warmup       is NOT passed. llama-server's warmup pass is enabled
                        by default and logs "warming up the model with an
                        empty run"; readiness already waits for it, so paying
                        it once at launch is cheaper than paying it inside
                        the user's first turn.

    An explicit `port` other than 0 is allowed for tests and for the rare
    caller that has already reserved one; nothing in Hearth passes a fixed
    port in normal operation.
    """
    argv = [
        server_path,
        "--model", model_path,
        "--host", host,
        "--port", str(port),
        "--no-webui",
    ]
    if n_gpu_layers is not None:
        argv += ["-ngl", str(n_gpu_layers)]
    if ctx_size is not None:
        argv += ["-c", str(int(ctx_size))]
    if alias:
        argv += ["--alias", alias]
    if parallel:
        argv += ["-np", str(int(parallel))]
    if extra:
        argv += list(extra)
    return argv


# --------------------------------------------------------------------------
# Orphan prevention
# --------------------------------------------------------------------------

# Windows Job Object constants, from winnt.h. Declared here rather than
# pulled from a package, because the whole point of this module is that
# Hearth adds no Python dependency.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JobObjectExtendedLimitInformation
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

_PR_SET_PDEATHSIG = 1  # linux/prctl.h
_SIGKILL = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


# One job per process, created lazily and then never closed. Keeping the
# handle alive for the life of the interpreter is the mechanism, not an
# oversight: the kill fires when the LAST handle to the job closes, which
# happens when this process dies by any means at all, including a hard
# TerminateProcess that runs no Python cleanup.
_job_handle = None
_job_lock = threading.Lock()


def _win_job():
    """The process-wide kill-on-close Job Object, or None if unavailable.

    Returns None rather than raising when job objects cannot be used (an
    ancient Windows without nested job support, a locked-down container).
    The caller falls back to explicit tree-kill on stop(), which still covers
    the ordinary shutdown path; only the hard-kill-the-parent case degrades.
    """
    global _job_handle
    if not hearth_paths.is_windows():
        return None
    with _job_lock:
        if _job_handle is not None:
            return _job_handle or None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                _job_handle = 0
                return None
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                ctypes.c_void_p(handle), _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info))
            if not ok:
                kernel32.CloseHandle(ctypes.c_void_p(handle))
                _job_handle = 0
                return None
            _job_handle = handle
            return handle
        except (OSError, AttributeError):
            _job_handle = 0
            return None


def _assign_to_job(proc):
    """Put `proc` in the kill-on-close job. True on success.

    llama-server spawns no child processes of its own (it is one process with
    a thread pool), so assigning immediately after Popen is sufficient: there
    is no grandchild that could be created in the window between spawn and
    assignment and thereby escape the job. If that ever changes, this must
    move to a CREATE_SUSPENDED launch with an explicit ResumeThread.
    """
    if not hearth_paths.is_windows():
        return False
    handle = _win_job()
    if not handle:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return bool(kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(handle), ctypes.c_void_p(int(proc._handle))))
    except (OSError, AttributeError, ValueError):
        return False


def _posix_die_with_parent():
    """preexec_fn that asks the kernel to SIGKILL this child when its parent
    dies. Linux only; a no-op elsewhere, and never allowed to raise, because
    an exception in preexec_fn fails the whole spawn."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, _SIGKILL, 0, 0, 0)
    except Exception:  # noqa: BLE001 - best effort; must never fail the spawn
        pass


def _pdeathsig_available():
    """True when the libc this process can load exposes prctl.

    preexec_fn runs in the forked child and cannot report back, so the
    child's actual prctl() return value is unobservable from here. Probing
    the same libc symbol in the parent is the closest honest proxy, and it is
    what _spawn_guarded reports rather than assuming "Linux, therefore
    guarded".
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        return hasattr(ctypes.CDLL("libc.so.6", use_errno=True), "prctl")
    except OSError:
        return False


def _spawn_guarded(argv, env=None, stderr_sink=None):
    """Start `argv` so that it cannot outlive this Python process.

    Windows: assigned to a JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE job, plus
    CREATE_NEW_PROCESS_GROUP so a console Ctrl-C aimed at Hearth does not
    also interrupt the model mid-generation.
    Linux: setsid (so hearth_proc's killpg reaches the whole group) plus
    PR_SET_PDEATHSIG.

    Returns (proc, guarded) where `guarded` says whether the hard-kill
    protection actually engaged. Exposed separately from start() so the
    self-test can prove the orphan protection with a cheap stub process
    instead of needing a real llama-server.
    """
    kw = {
        "stdout": subprocess.DEVNULL,
        "stderr": stderr_sink if stderr_sink is not None else subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "env": env or _llama_env(),
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if hearth_paths.is_windows():
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
        if sys.platform.startswith("linux"):
            kw["preexec_fn"] = _posix_die_with_parent

    proc = subprocess.Popen(argv, **kw)
    guarded = _assign_to_job(proc) if hearth_paths.is_windows() else _pdeathsig_available()
    return proc, guarded


# --------------------------------------------------------------------------
# Server lifecycle
# --------------------------------------------------------------------------

def _http_json(url, timeout, body=None, method=None):
    """(status, parsed_json_or_None, error_or_None) for one bounded request.

    Never raises. A 503 with a JSON body is a normal, expected answer here
    (that is llama-server saying "still loading"), so an HTTPError is
    unwrapped into the same shape as a success rather than treated as a
    failure.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw), None
            except ValueError:
                return resp.status, None, "non-JSON body: {!r}".format(raw[:200])
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the status code is the signal here
            pass
        try:
            return exc.code, json.loads(raw), None
        except ValueError:
            return exc.code, None, raw[:200] or str(exc)
    except Exception as exc:  # noqa: BLE001 - urllib raises a wide family
        return None, None, str(exc)


def health(base_url, timeout=HEALTH_TIMEOUT):
    """Ask a running llama-server whether its model is loaded.

    Returns one of STATE_READY, STATE_LOADING, or None (unreachable).
    Measured against the real binary: 200 {"status":"ok"} once resident,
    503 {"error":{"message":"Loading model",...}} while loading.
    """
    status, _body, _err = _http_json(base_url.rstrip("/") + "/health", timeout)
    if status == 200:
        return STATE_READY
    if status == 503:
        return STATE_LOADING
    return None


class Server:
    """One running llama-server, with exactly one model resident.

    One model at a time is the deliberate default: a 7B model at a useful
    context can fill most of a consumer card, and two resident models means
    neither loads. Swapping is therefore a stop-and-start (see swap_model),
    made explicit and cheap to call rather than hidden behind a cache that
    would silently OOM the second model.

    A Server is not thread-safe for start/stop; chat() from multiple threads
    against an already-started server is fine, since llama-server has its own
    slot scheduler.
    """

    def __init__(self, server_path, model_path, port=None, host="127.0.0.1"):
        self.server_path = server_path
        self.model_path = model_path
        self.host = host
        self.port = port
        self.proc = None
        self.argv = None
        self.guarded = False
        self.state = STATE_STOPPED
        self.launch_reason = {}
        self._stderr_lines = []
        self._stderr_bytes = 0
        self._reader = None
        self._lock = threading.Lock()

    # -- stderr plumbing ---------------------------------------------------

    def _drain_stderr(self):
        """Read the child's stderr forever, keeping a bounded tail and
        watching for the port announcement.

        This runs in a thread because it has to: llama-server writes its
        whole banner to stderr, and a full pipe buffer would block the child
        before it ever finishes loading. The historic version of this bug in
        hearth_proc is the same shape.

        Colour escapes are stripped as the lines arrive, so the ring buffer
        holds plain text: everything downstream -- PORT_RE here,
        stderr_tail() and therefore every LlamaError message, and any device
        parsing over the retained block -- gets text it can actually match
        and a user can actually read.
        """
        stream = self.proc.stderr
        if stream is None:
            return
        try:
            for raw in stream:
                line = _strip_ansi(raw)
                with self._lock:
                    self._stderr_lines.append(line)
                    self._stderr_bytes += len(line)
                    while self._stderr_bytes > STDERR_RING_BYTES and len(self._stderr_lines) > 1:
                        self._stderr_bytes -= len(self._stderr_lines.pop(0))
                    if self.port is None:
                        m = PORT_RE.search(line)
                        if m:
                            self.port = int(m.group("port"))
        except Exception:  # noqa: BLE001 - a closed pipe on shutdown is normal
            pass

    def stderr_tail(self, limit=40):
        """The last few stderr lines, for an error message a user can act on."""
        with self._lock:
            return "".join(self._stderr_lines[-limit:])

    # -- lifecycle ---------------------------------------------------------

    @property
    def base_url(self):
        """The server's HTTP root, or None until the port is known."""
        if self.port is None:
            return None
        return "http://{}:{}".format(self.host, self.port)

    def spawn(self, n_gpu_layers=None, ctx_size=None, alias=None, parallel=1, extra=None):
        """Launch the process. Does not wait for readiness; see wait_ready."""
        port = 0 if self.port is None else self.port
        self.argv = build_argv(self.server_path, self.model_path, port=port, host=self.host,
                               n_gpu_layers=n_gpu_layers, ctx_size=ctx_size, alias=alias,
                               parallel=parallel, extra=extra)
        self.proc, self.guarded = _spawn_guarded(self.argv)
        self.state = STATE_STARTING
        self._reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()
        return self

    def poll_state(self):
        """The server's current state, without waiting.

        Distinguishes, in order: the process is gone (CRASHED, and the next
        request would otherwise hang or connection-refuse with no
        explanation); the port is not announced yet (STARTING); the model is
        still loading (LOADING); it is serving (READY).
        """
        if self.proc is None:
            return STATE_STOPPED
        if self.proc.poll() is not None:
            self.state = STATE_CRASHED
            return STATE_CRASHED
        if self.port is None:
            self.state = STATE_STARTING
            return STATE_STARTING
        got = health(self.base_url, timeout=HEALTH_TIMEOUT)
        if got is None:
            # Port announced but nothing answering: the socket may not be
            # accepting yet. Still starting, not crashed -- the process
            # check above already ruled crashed out.
            self.state = STATE_STARTING
            return STATE_STARTING
        self.state = got
        return got

    def wait_ready(self, timeout=DEFAULT_READY_TIMEOUT, interval=POLL_INTERVAL):
        """Block until the model is loaded and serving.

        Returns STATE_READY on success. Raises LlamaError with the tail of
        the server's own stderr when the process dies during load (the
        binary exits 1 and says why), and on timeout, which reports the last
        state seen so "it was still loading after ten minutes" is
        distinguishable from "it never even bound a port".
        """
        deadline = time.monotonic() + timeout
        last = STATE_STARTING
        while time.monotonic() < deadline:
            last = self.poll_state()
            if last == STATE_READY:
                return STATE_READY
            if last == STATE_CRASHED:
                rc = self.proc.returncode if self.proc else None
                raise LlamaError(
                    "llama-server exited with code {} before the model finished "
                    "loading:\n{}".format(rc, self.stderr_tail()))
            time.sleep(interval)
        raise LlamaError(
            "llama-server was still {} after {:.0f}s (model {!r}); last output:\n{}".format(
                last, timeout, self.model_path, self.stderr_tail()))

    def stop(self, grace=STOP_GRACE_SECONDS):
        """Terminate the server and everything under it.

        The tree kill is hearth_proc's, not a second implementation: that
        module already handles taskkill /T on Windows and killpg on POSIX,
        including the case where the tree refuses to confirm death. A polite
        terminate() is tried first so llama-server gets to log its
        "cleaning up before exit" and release the GPU in an orderly way.

        Returns the process's exit code, or None when it would not confirm
        death. Safe to call on an already-stopped server.

        Do NOT read that code as success or failure. It is whatever the OS
        reports for a process we deliberately killed, and on Windows that is
        always 1, because Popen.terminate() is TerminateProcess(handle, 1)
        and the exit code is the second argument. Measured against the real
        binary: a llama-server that started cleanly, served a full stream and
        shut down with no orphan still returns 1 here. A caller that wants to
        know whether the server was healthy should ask poll_state() before
        stopping it; the value returned here is for logging, and None (the
        tree would not die) is the only outcome that means something went
        wrong. It is left unnormalised on purpose: reporting the code the
        kernel actually gave is more useful than inventing a 0.
        """
        proc, self.proc = self.proc, None
        self.state = STATE_STOPPED
        if proc is None:
            return None
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            hearth_proc._kill_tree(proc)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                return None
        except OSError:
            pass
        if self._reader is not None:
            self._reader.join(timeout=2)
        self.port = None
        return proc.returncode

    def swap_model(self, model_path, **kwargs):
        """Stop this server and start a fresh one on `model_path`.

        Explicit and cheap on purpose. Only one model is resident at a time
        because two rarely fit in consumer VRAM, so a swap is genuinely a
        restart; pretending otherwise would just mean the second load fails
        to allocate. Returns self so a caller can chain into wait_ready.
        """
        self.stop()
        self.model_path = model_path
        self.port = None
        self._stderr_lines = []
        self._stderr_bytes = 0
        return self.spawn(**kwargs)

    # -- chat --------------------------------------------------------------

    def chat(self, messages, on_token=None, tools=None, timeout=DEFAULT_CHAT_TIMEOUT,
             model=None, **params):
        """One streaming chat turn against this server.

        Thin on purpose: `messages`, `tools` and `**params` (temperature,
        max_tokens, top_p, ...) go through to llama-server's OpenAI-shaped
        POST /v1/chat/completions untranslated, so this can sit behind a
        backend abstraction beside the existing Ollama path without a lossy
        middle format. `on_token(text)` is called for each content delta.

        Returns {"content": str, "tool_calls": list, "finish_reason": str,
        "tokens_in": int, "tokens_out": int, "model": str}.

        stream_options.include_usage is always set, because it was measured
        that llama-server omits the usage object entirely without it and
        reports only its own `timings` block; the spend breaker needs real
        token counts, and hearth_loop's history of under-counting prompts is
        exactly what that omission would cause here.
        """
        if self.proc is None or self.proc.poll() is not None:
            raise LlamaError("llama-server is not running (state={})".format(self.state))
        if self.port is None:
            raise LlamaError("llama-server has not announced a port yet")

        body = dict(params)
        body["messages"] = messages
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
        if model or getattr(self, "alias", None):
            body["model"] = model or self.alias

        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - the code is the signal
                pass
            raise LlamaError("llama-server rejected the request ({}): {}".format(
                exc.code, _error_message(detail) or detail[:300])) from exc
        except Exception as exc:  # noqa: BLE001 - urllib raises a wide family
            if self.proc.poll() is not None:
                raise LlamaError(
                    "llama-server died mid-request (exit {}):\n{}".format(
                        self.proc.returncode, self.stderr_tail())) from exc
            raise LlamaError("could not reach llama-server: {}".format(exc)) from exc

        with resp:
            try:
                return consume_stream(_iter_lines(resp), on_token=on_token)
            except Exception as exc:  # noqa: BLE001 - classify before re-raising
                if self.proc.poll() is not None:
                    raise LlamaError(
                        "llama-server died mid-stream (exit {}):\n{}".format(
                            self.proc.returncode, self.stderr_tail())) from exc
                raise


def _iter_lines(resp):
    """Decoded lines from an HTTP response body, without the trailing newline."""
    for raw in resp:
        yield raw.decode("utf-8", "replace").rstrip("\r\n")


def _error_message(raw):
    """The human-readable part of llama-server's error body, if it is one.

    Measured shape: {"error":{"code":400,"message":"...","type":"..."}}.
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    err = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(err, dict):
        return err.get("message")
    return None


def consume_stream(lines, on_token=None):
    """Fold llama-server's SSE frames into one result dict.

    A pure function over an iterable of already-decoded lines, so the frame
    parsing is testable against captured real output with no server, no
    socket and no subprocess. The format below is transcribed from a real
    capture, not from documentation:

        data: {"choices":[{"index":0,"delta":{"role":"assistant","content":null}}],...}
        <blank>
        data: {"choices":[{"index":0,"delta":{"content":"He"}}],...}
        <blank>
        data: {"choices":[{"index":0,"finish_reason":"length","delta":{}}],...}
        <blank>
        data: {"choices":[],"usage":{"completion_tokens":6,"prompt_tokens":42,...}}
        <blank>
        data: [DONE]

    Three details that a hand-written parser gets wrong:
      * the first delta carries "content": null, not "", so a naive
        concatenation raises TypeError on the very first frame;
      * the usage frame has an EMPTY choices list, so indexing choices[0]
        raises IndexError right at the end of an otherwise good stream;
      * tool call deltas arrive in fragments with an "index" field and must
        be reassembled per index, since arguments stream a piece at a time.
    """
    content = []
    tool_calls = {}
    finish_reason = None
    tokens_in = 0
    tokens_out = 0
    model = None
    saw_done = False

    for line in lines:
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            saw_done = True
            break
        try:
            frame = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue

        err = frame.get("error")
        if isinstance(err, dict):
            raise LlamaError("llama-server streamed an error: {}".format(
                err.get("message") or err))

        model = frame.get("model") or model

        usage = frame.get("usage")
        if isinstance(usage, dict):
            tokens_in = int(usage.get("prompt_tokens") or tokens_in)
            tokens_out = int(usage.get("completion_tokens") or tokens_out)

        for choice in frame.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:  # None on the opening frame, "" on keepalives
                content.append(piece)
                if on_token is not None:
                    on_token(piece)
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", len(tool_calls))
                slot = tool_calls.setdefault(
                    idx, {"id": None, "type": "function",
                          "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]

    return {
        "content": "".join(content),
        "tool_calls": [tool_calls[k] for k in sorted(tool_calls)],
        "finish_reason": finish_reason,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "model": model,
        "complete": saw_done,
    }


def engine_level_failure(server):
    """True when `server` died in a way that blames the BINARY, not the model.

    The distinction is what makes the automatic fallback safe to have. A
    llama-server that cannot load its backend DLLs never gets as far as
    printing its own banner: on Windows the loader kills it before main()
    runs, so there is no version line and no port announcement, and that
    silence is the signature. A server that started fine and then failed on
    a corrupt GGUF, or ran out of VRAM, has already printed its version and
    usually its device block, and demoting the engine for that would throw
    away a working GPU because of a bad file.

    So: no port, and no version line in anything it managed to say.
    """
    if server is None or server.port is not None:
        return False
    build, _commit = _parse_version(server.stderr_tail(limit=10000))
    return build is None


def start(model_path, server_path=None, ctx_size=None, n_gpu_layers=None, alias=None,
          parallel=1, ready_timeout=DEFAULT_READY_TIMEOUT, extra=None, wait=True,
          _allow_fallback=True):
    """Launch a llama-server on `model_path` and wait until it is serving.

    The one call a caller normally makes. Picks -ngl from measured VRAM (see
    choose_gpu_layers) and the context size from hearth_loop's ladder (see
    choose_ctx_size) unless told otherwise, binds loopback on an ephemeral
    port, and returns a started Server.

    When the binary was CHOSEN here rather than named by the caller, and the
    chosen one is a GPU engine hearth_engine fetched, a startup failure that
    blames the binary demotes that engine and retries once on the bundled
    CPU build (see engine_level_failure for what "blames the binary" means).
    A caller that passed an explicit server_path gets no such second-
    guessing: it asked for that binary and gets that binary's error.

    Raises LlamaError when no binary can be found, when the process dies
    during load, or when readiness times out; the message carries the
    server's own stderr tail, because "exiting due to model loading error"
    plus the line above it is the actual answer the user needs.
    """
    chosen_here = server_path is None
    if server_path is None:
        found = find_server()
        if not found["found"]:
            raise LlamaError("{} (searched: {})".format(
                found["reason"], "; ".join(found["searched"])))
        server_path = found["path"]
        chosen_here = found["source"] == FOUND_ACQUIRED

    # long_path, because a GGUF sitting in a redirected Documents folder is
    # exactly the kind of path that goes past MAX_PATH and then "does not
    # exist" according to a plain isfile().
    if not os.path.isfile(hearth_paths.long_path(model_path)):
        raise LlamaError("no model file at {!r}".format(model_path))

    def _fall_back(why):
        """Demote the fetched engine and retry on the bundled build.

        Returns a started Server, or None when there was nothing to demote,
        in which case the caller re-raises the original error. The retry
        passes _allow_fallback=False so a broken bundled build cannot put
        this into a loop.
        """
        if not (chosen_here and _allow_fallback):
            return None
        if not hearth_engine.demote(why, server_path=server_path):
            return None
        return start(model_path, server_path=None, ctx_size=ctx_size,
                     n_gpu_layers=n_gpu_layers, alias=alias, parallel=parallel,
                     ready_timeout=ready_timeout, extra=extra, wait=wait,
                     _allow_fallback=False)

    info = probe_binary(server_path)
    if chosen_here and not info.get("ok"):
        # The engine verified when it was installed and does not run now: a
        # driver was removed, a DLL was deleted, the card was swapped. Do
        # not spawn it; fall straight back to the build that always works.
        retried = _fall_back("it no longer runs on this machine: {}".format(
            info.get("error") or "it reported no version"))
        if retried is not None:
            return retried
    reason = {}
    if n_gpu_layers is None:
        n_gpu_layers, reason["gpu_layers"] = choose_gpu_layers(
            model_path, backend=info.get("backend"))
    else:
        reason["gpu_layers"] = "caller supplied -ngl {}".format(n_gpu_layers)
    if ctx_size is None:
        ctx_size = choose_ctx_size(model_path)
        reason["ctx_size"] = "hearth_loop context ladder -> {}".format(ctx_size)
    else:
        reason["ctx_size"] = "caller supplied -c {}".format(ctx_size)
    reason["backend"] = info.get("backend")
    reason["build"] = info.get("build")

    server = Server(server_path, model_path)
    server.alias = alias
    server.launch_reason = reason
    server.spawn(n_gpu_layers=n_gpu_layers, ctx_size=ctx_size, alias=alias,
                 parallel=parallel, extra=extra)
    if wait:
        try:
            server.wait_ready(timeout=ready_timeout)
        except LlamaError as exc:
            engine_failed = engine_level_failure(server)
            server.stop()
            if engine_failed:
                retried = _fall_back(
                    "it exited before announcing a port and printed no version, "
                    "which is what a build whose libraries are missing does")
                if retried is not None:
                    return retried
            raise exc
    return server


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        prog="hearth-llama",
        description="Find, launch and talk to Hearth's bundled llama.cpp server.",
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--live", action="store_true",
                        help="also run the tests that need a real llama-server binary")
    parser.add_argument("--model", help="GGUF to load for --live / chat")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("find", help="report where the llama-server binary is (default)")
    sub.add_parser("probe", help="report the binary's version, devices and backend")
    chat_p = sub.add_parser("chat", help="load a model and stream one reply")
    chat_p.add_argument("prompt")
    # Accepted on either side of the subcommand. SUPPRESS matters: without
    # it, argparse would write None into the shared `model` dest whenever the
    # subparser's flag is omitted, silently discarding a --model given before
    # the subcommand.
    chat_p.add_argument("--model", default=argparse.SUPPRESS)
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test(live=args.live, model=args.model)

    if args.command == "probe":
        info = probe_binary()
        print(json.dumps(info, indent=2) if args.json else
              "llama-server build {} ({}), backend {}, gpu offload {}".format(
                  info["build"], info["commit"], info["backend"],
                  "available" if info["gpu_offload"] else "NOT available"))
        return 0 if info["ok"] else 1

    if args.command == "chat":
        if not args.model:
            parser.error("chat needs --model")
        server = start(args.model)
        try:
            result = server.chat([{"role": "user", "content": args.prompt}],
                                 on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()))
            print("\n[{} in, {} out]".format(result["tokens_in"], result["tokens_out"]))
        finally:
            server.stop()
        return 0

    found = find_server()
    if args.json:
        print(json.dumps(found, indent=2))
    else:
        print("{}: {}".format("found" if found["found"] else "NOT FOUND",
                              found["path"] or found["reason"]))
        if not found["found"]:
            for p in found["searched"]:
                print("  tried: {}".format(p))
    return 0 if found["found"] else 1


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

# Real output captured from llama-server build 9608 (70b54e1). These are
# transcriptions of what the binary actually emitted, not invented fixtures,
# which is the only reason the offline tests below mean anything.

FIXTURE_VERSION = (
    "version: 9608 (70b54e1)\n"
    "built with GNU 15.2.0 for Linux x86_64\n"
)

FIXTURE_LISTEN = (
    "0.00.056.147 I srv  llama_server: model loaded\n"
    "0.00.056.149 I srv  llama_server: server is listening on http://127.0.0.1:45727\n"
    "0.00.056.152 I srv  update_slots: all slots are idle\n"
)

FIXTURE_LISTEN_NO_PREFIX = "srv  llama_server: server is listening on http://127.0.0.1:34889\n"

FIXTURE_DEVICES_CPU = (
    "Available devices:\n"
    "  BLAS: BLAS (0 MiB, 0 MiB free)\n"
)

FIXTURE_DEVICE_BLOCK = (
    "warning: no usable GPU found, --gpu-layers option will be ignored\n"
    "warning: one possible reason is that llama.cpp was compiled without GPU support\n"
    "0.00.032.293 I device_info:\n"
    "0.00.032.295 I   - BLAS    : BLAS (0 MiB, 0 MiB free)\n"
    "0.00.032.299 I   - CPU     : Intel(R) Core(TM) i7-10750H CPU @ 2.60GHz (15854 MiB, 15854 MiB free)\n"
)

# Real output captured from the binary Hearth vendors: build 10105
# (e6dd0e29a), llama-b10105-bin-win-cpu-x64. Every escape byte below was
# read off a pipe, not typed in; the colourised ones came out of
# _spawn_guarded's own stderr handle, which is the exact path Hearth uses.

FIXTURE_WIN_VERSION = (
    "version: 10105 (e6dd0e29a)\n"
    "built with Clang 20.1.8 for Windows x86_64\n"
)

# The whole of `--list-devices` on the Windows CPU build. Not a truncation:
# the header is all there is, on stdout, and stderr is empty. This is the
# input that used to produce "backend: unknown".
FIXTURE_WIN_DEVICES_NONE = "Available devices:\n"

# Startup stderr, colourised. Note the port line lost the "server is" that
# build 9608 had, and note the stray reset at the start of a line that
# follows an unterminated colour run -- both are verbatim.
FIXTURE_WIN_LISTEN_ANSI = (
    "\x1b[34m0.00.035.307\x1b[0m \x1b[32mI \x1b[0msrv  llama_server: model loaded\n"
    "\x1b[34m0.00.035.313\x1b[0m \x1b[32mI \x1b[0msrv  llama_server: "
    "listening on http://127.0.0.1:65509\n"
)

FIXTURE_WIN_WARN_ANSI = (
    "\x1b[34m0.00.017.839\x1b[0m \x1b[35mW srv  llama_server: -----------------\n"
    "\x1b[0m\x1b[34m0.00.017.841\x1b[0m \x1b[35mW srv  llama_server: CORS is set to "
    "allow all origins ('*') and no API key is set\n"
)

# The startup device block from the same build, colourised, and carrying the
# `cmn  common_param: ` log-module prefix that 9608 did not have.
FIXTURE_WIN_DEVICE_BLOCK_ANSI = (
    "\x1b[34m0.00.017.035\x1b[0m \x1b[32mI \x1b[0mcmn  common_param: device_info:\n"
    "\x1b[34m0.00.017.042\x1b[0m \x1b[32mI \x1b[0mcmn  common_param:   - CPU     : "
    "AMD Ryzen 9 9900X 12-Core Processor             (31800 MiB, 13835 MiB free)\n"
)

FIXTURE_LOAD_FAILURE = (
    "0.00.018.344 E llama_model_load_from_file_impl: failed to load model\n"
    "0.00.018.345 E common_init_from_params: failed to load model '/tmp/NOPE.gguf'\n"
    "0.00.018.346 E srv    load_model: failed to load model, '/tmp/NOPE.gguf'\n"
    "0.00.018.556 E srv  llama_server: exiting due to model loading error\n"
)

# A complete streaming response, verbatim from the wire (blank lines between
# frames included, since the parser has to tolerate them).
FIXTURE_SSE = """data: {"choices":[{"finish_reason":null,"index":0,"delta":{"role":"assistant","content":null}}],"created":1785553083,"id":"chatcmpl-P7bQ","model":"hearth-test","system_fingerprint":"b9608-70b54e1","object":"chat.completion.chunk"}

data: {"choices":[{"finish_reason":null,"index":0,"delta":{"content":"\\""}}],"created":1785553083,"id":"chatcmpl-P7bQ","model":"hearth-test","object":"chat.completion.chunk"}

data: {"choices":[{"finish_reason":null,"index":0,"delta":{"content":"H"}}],"created":1785553083,"id":"chatcmpl-P7bQ","model":"hearth-test","object":"chat.completion.chunk"}

data: {"choices":[{"finish_reason":null,"index":0,"delta":{"content":"e"}}],"created":1785553083,"id":"chatcmpl-P7bQ","model":"hearth-test","object":"chat.completion.chunk"}

data: {"choices":[{"finish_reason":null,"index":0,"delta":{"content":"re"}}],"created":1785553083,"id":"chatcmpl-P7bQ","model":"hearth-test","object":"chat.completion.chunk"}

data: {"choices":[{"finish_reason":null,"index":0,"delta":{"content":","}}],"created":1785553083,"id":"chatcmpl-P7bQ","model":"hearth-test","object":"chat.completion.chunk"}

data: {"choices":[{"finish_reason":null,"index":0,"delta":{"content":"\\""}}],"created":1785553083,"id":"chatcmpl-P7bQ","model":"hearth-test","object":"chat.completion.chunk"}

data: {"choices":[{"finish_reason":"length","index":0,"delta":{}}],"created":1785553083,"id":"chatcmpl-P7bQ","model":"hearth-test","object":"chat.completion.chunk"}

data: {"choices":[],"created":1785553083,"id":"chatcmpl-P7bQ","model":"hearth-test","object":"chat.completion.chunk","usage":{"completion_tokens":6,"prompt_tokens":42,"total_tokens":48,"prompt_tokens_details":{"cached_tokens":41}},"timings":{"cache_n":41,"prompt_n":1}}

data: [DONE]
"""

# The same stream as it arrives with stream_options.include_usage omitted:
# the final frame carries llama.cpp's timings and NO usage object at all.
FIXTURE_SSE_NO_USAGE = """data: {"choices":[{"finish_reason":null,"index":0,"delta":{"role":"assistant","content":null}}],"model":"stories260K.gguf","object":"chat.completion.chunk"}

data: {"choices":[{"finish_reason":"length","index":0,"delta":{}}],"model":"stories260K.gguf","object":"chat.completion.chunk","timings":{"cache_n":0,"prompt_n":42,"predicted_n":3}}

data: [DONE]
"""

FIXTURE_ERROR_BODY = (
    '{"error":{"code":400,"message":"Expected \'messages\' to be an array",'
    '"type":"invalid_request_error"}}'
)


def _stub_server_script(hold_seconds=120):
    """A Python one-liner that behaves like a long-running child, for the
    process-lifecycle tests. Prints its own pid so a parent can report it."""
    return ("import os,sys,time;"
            "sys.stdout.write(str(os.getpid()));sys.stdout.flush();"
            "time.sleep({})".format(hold_seconds))


def _pid_alive(pid):
    """True when `pid` names a live process. Windows uses tasklist because
    os.kill(pid, 0) succeeds against a zombie handle; POSIX uses signal 0."""
    if hearth_paths.is_windows():
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq {}".format(pid), "/NH"],
                capture_output=True, timeout=20, encoding="utf-8", errors="replace",
            ).stdout or ""
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _orphan_probe(disable_guard=False):
    """Spawn a grandchild through _spawn_guarded from a throwaway child
    process, hard-kill that child, and report whether the grandchild died.

    This is the actual test of the orphan-prevention mechanism, and it has to
    be done this way: the guarantee is specifically about the parent dying
    WITHOUT running cleanup code, so the parent is killed with taskkill /F
    (Windows) or SIGKILL (POSIX), and deliberately NOT with /T, which would
    kill the grandchild by itself and make the test pass for the wrong
    reason.

    `disable_guard` is the mutation lever: it makes the child process spawn
    the grandchild with the job assignment (or PDEATHSIG) removed, so the
    test can be proved to fail when the mechanism is broken.

    Returns (grandchild_pid, survived_bool).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    child_src = (
        "import os,sys,time\n"
        "sys.path.insert(0, {here!r})\n"
        "import hearth_llama as L\n"
        "if {disable}:\n"
        "    L._assign_to_job = lambda proc: False\n"
        "    L._posix_die_with_parent = lambda: None\n"
        "p, g = L._spawn_guarded([sys.executable, '-c', {stub!r}])\n"
        "sys.stdout.write(str(p.pid) + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(300)\n"
    ).format(here=here, disable=bool(disable_guard), stub=_stub_server_script())

    parent = subprocess.Popen(
        [sys.executable, "-c", child_src], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
    )
    try:
        line = parent.stdout.readline().strip()
        assert line.isdigit(), "probe parent did not report a pid: {!r} {}".format(
            line, parent.stderr.read() if parent.poll() is not None else "")
        grandchild = int(line)
        assert _pid_alive(grandchild), "grandchild {} never started".format(grandchild)

        # Kill ONLY the parent, without cleanup and without /T.
        if hearth_paths.is_windows():
            subprocess.run(["taskkill", "/F", "/PID", str(parent.pid)],
                           capture_output=True, timeout=30)
        else:
            os.kill(parent.pid, 9)
        parent.wait(timeout=30)

        # Give the kernel a moment to tear the job down.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not _pid_alive(grandchild):
                return grandchild, False
            time.sleep(0.25)
        return grandchild, _pid_alive(grandchild)
    finally:
        try:
            parent.kill()
        except OSError:
            pass
        for stream in (parent.stdout, parent.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - closing a dead pipe
                pass


def _self_test(live=False, model=None):
    import tempfile
    from unittest import mock

    # -- version parsing, against the real banner -------------------------
    build, commit = _parse_version(FIXTURE_VERSION)
    assert build == 9608, build
    assert commit == "70b54e1", commit
    # The banner arrives on stderr, so a stdout-only capture is empty; the
    # parser must say "no version" rather than inventing one.
    assert _parse_version("") == (None, None)
    assert _parse_version("built with GNU 15.2.0 for Linux x86_64") == (None, None)
    # A build with no commit suffix still yields the build number.
    assert _parse_version("version: 1234")[0] == 1234
    # The Windows build Hearth actually ships, whose commit hash is 9 hex
    # characters where 9608's was 7. Nothing may depend on that width.
    assert _parse_version(FIXTURE_WIN_VERSION) == (10105, "e6dd0e29a"), FIXTURE_WIN_VERSION

    # -- port announcement parsing ----------------------------------------
    m = PORT_RE.search(FIXTURE_LISTEN)
    assert m and int(m.group("port")) == 45727, FIXTURE_LISTEN
    assert m.group("host") == "127.0.0.1"
    m2 = PORT_RE.search(FIXTURE_LISTEN_NO_PREFIX)
    assert m2 and int(m2.group("port")) == 34889, "must survive --no-log-prefix"
    # Nothing in the earlier startup banner may be mistaken for the port line.
    assert PORT_RE.search("0.00.033.569 I srv start: binding port with default address family") is None
    assert PORT_RE.search(FIXTURE_LOAD_FAILURE) is None

    # -- device / backend classification ----------------------------------
    devs = _parse_devices(FIXTURE_DEVICES_CPU)
    assert len(devs) == 1, devs
    assert devs[0]["tag"] == "BLAS" and devs[0]["backend"] == BACKEND_CPU, devs
    assert devs[0]["total_mib"] == 0, devs
    block = _parse_devices(FIXTURE_DEVICE_BLOCK)
    tags = [d["tag"] for d in block]
    assert tags == ["BLAS", "CPU"], tags
    assert block[1]["name"].startswith("Intel(R) Core"), block[1]
    assert block[1]["total_mib"] == 15854, block[1]
    # The "Available devices:" / "device_info:" headers are not devices, and
    # neither are the two "warning: ..." lines that sit directly above the
    # device block. Those warnings are "word: text" shaped, so a looser
    # pattern reports a device called "warning" -- which is exactly what an
    # earlier draft of DEVICE_RE did.
    assert all(d["tag"] not in ("Available", "device_info", "warning") for d in block + devs), block
    assert _parse_devices(
        "warning: no usable GPU found, --gpu-layers option will be ignored\n"
        "warning: one possible reason is that llama.cpp was compiled without GPU support\n"
    ) == [], "warning lines must not parse as devices"
    assert _classify_backend("CUDA0") == BACKEND_CUDA
    assert _classify_backend("Vulkan1") == BACKEND_VULKAN
    assert _classify_backend("ROCm0") == BACKEND_ROCM
    assert _classify_backend("Metal") == BACKEND_METAL
    # An unrecognised ggml backend must not be quietly filed as CPU.
    assert _classify_backend("Frobnicator0") == BACKEND_UNKNOWN
    assert _pick_backend(devs, FIXTURE_DEVICES_CPU) == BACKEND_CPU
    assert _pick_backend(block, FIXTURE_DEVICE_BLOCK) == BACKEND_CPU
    # A synthetic CUDA listing picks the GPU over the CPU entry...
    cuda_text = "Available devices:\n  CUDA0: NVIDIA GeForce RTX 2060 (6144 MiB, 5731 MiB free)\n"
    cuda_devs = _parse_devices(cuda_text)
    assert cuda_devs[0]["backend"] == BACKEND_CUDA and cuda_devs[0]["total_mib"] == 6144, cuda_devs
    assert _pick_backend(cuda_devs, cuda_text) == BACKEND_CUDA
    # ... but the binary's own "no usable GPU" warning overrides the list,
    # because that warning is it telling us -ngl will be ignored.
    assert _pick_backend(cuda_devs, cuda_text + NO_GPU_MARKER) == BACKEND_CPU
    assert _pick_backend([], "") == BACKEND_UNKNOWN

    # -- defect 1: the Windows CPU build lists no devices at all -----------
    # `--list-devices` on llama-b10105-bin-win-cpu-x64 prints the header and
    # stops. The Linux build's BLAS pseudo-device does not exist there, so an
    # empty list used to fall through to BACKEND_UNKNOWN and hearth_backend
    # told the user "unknown backend" about a completely healthy install.
    assert _parse_devices(FIXTURE_WIN_DEVICES_NONE) == [], FIXTURE_WIN_DEVICES_NONE
    assert _pick_backend([], FIXTURE_WIN_DEVICES_NONE) == BACKEND_CPU, (
        "a binary that listed devices and found none is a CPU-only build")
    # The header is what carries that meaning, and it survives colour.
    assert _pick_backend([], "\x1b[32mAvailable devices:\x1b[0m\n") == BACKEND_CPU
    # Without it there is no evidence the listing ever happened, so the
    # honest answer is still "we could not tell". This is the line between
    # the two, and it must not be collapsed.
    assert _pick_backend([], "") == BACKEND_UNKNOWN
    assert _pick_backend([], "could not run C:\\nope\\llama-server.exe") == BACKEND_UNKNOWN
    # An unrecognised tag is NOT quietly promoted to CPU just because it is
    # not a GPU we know: a future ggml backend may well offload fine, and
    # telling the caller "cpu" would have it skip -ngl for no reason.
    frob_text = "Available devices:\n  Frobnicator0: Frobnicator (8192 MiB, 8000 MiB free)\n"
    frob = _parse_devices(frob_text)
    assert len(frob) == 1 and frob[0]["backend"] == BACKEND_UNKNOWN, frob
    assert _pick_backend(frob, frob_text) == BACKEND_UNKNOWN, (
        "an unrecognised device tag must stay unknown, not become cpu")
    # A recognised CPU device alongside an unrecognised one is still CPU.
    mixed_text = frob_text + "  CPU: Some CPU (16000 MiB, 8000 MiB free)\n"
    assert _pick_backend(_parse_devices(mixed_text), mixed_text) == BACKEND_CPU

    # -- defect 2: ANSI colour escapes on Windows stderr -------------------
    # Build 10105 colourises stderr in Hearth's own spawn path, because
    # llama.cpp's --log-colors=auto asks isatty() and Hearth passes
    # stdout=DEVNULL, which Windows' CRT reports as a character device.
    assert "\x1b" in FIXTURE_WIN_LISTEN_ANSI, "the fixture must carry real escapes"
    plain = _strip_ansi(FIXTURE_WIN_LISTEN_ANSI)
    assert "\x1b" not in plain and "[34m" not in plain, repr(plain)
    assert plain.endswith("srv  llama_server: listening on http://127.0.0.1:65509\n"), repr(plain)
    # Stripping is exact: nothing but the escapes is removed.
    assert "0.00.035.313" in plain and "model loaded" in plain, repr(plain)
    # Text with no escapes is returned untouched, and None/"" do not crash.
    assert _strip_ansi(FIXTURE_LISTEN) == FIXTURE_LISTEN
    assert _strip_ansi("") == "" and _strip_ansi(None) == ""
    # The stray reset that starts a line after an unterminated colour run is
    # removed too, or the line would still not match an anchored pattern.
    warn = _strip_ansi(FIXTURE_WIN_WARN_ANSI)
    assert "\x1b" not in warn, repr(warn)
    assert warn.splitlines()[1].startswith("0.00.017.841 W srv"), repr(warn.splitlines()[1])

    # LOG_PREFIX_RE is anchored on ^\d and so matches NOTHING on a coloured
    # line. That is the whole defect, pinned here so the ordering inside
    # _parse_devices (strip escapes first, always) cannot be reversed.
    coloured = FIXTURE_WIN_DEVICE_BLOCK_ANSI.splitlines()[1]
    assert LOG_PREFIX_RE.sub("", coloured) == coloured, \
        "LOG_PREFIX_RE cannot match a colourised line; _strip_ansi must run first"
    assert LOG_PREFIX_RE.sub("", _strip_ansi(coloured)) != _strip_ansi(coloured)

    # The real colourised startup device block now parses. Before the fix
    # this returned [], silently, on every Windows launch.
    win_block = _parse_devices(FIXTURE_WIN_DEVICE_BLOCK_ANSI)
    assert len(win_block) == 1, win_block
    assert win_block[0]["tag"] == "CPU" and win_block[0]["backend"] == BACKEND_CPU, win_block
    assert win_block[0]["name"] == "AMD Ryzen 9 9900X 12-Core Processor", win_block
    assert win_block[0]["total_mib"] == 31800 and win_block[0]["free_mib"] == 13835, win_block
    # The `device_info:` header line is still not a device, colour or not.
    assert all(d["tag"] != "device_info" for d in win_block), win_block
    # ... and the extra tolerance for the log-module prefix does not turn
    # ordinary log chatter into imaginary devices. The memory suffix is what
    # holds that line; these are real 10105 stderr lines that must parse as
    # nothing at all.
    assert _parse_devices(FIXTURE_WIN_LISTEN_ANSI) == [], "log lines are not devices"
    assert _parse_devices(FIXTURE_WIN_WARN_ANSI) == [], "warnings are not devices"
    assert _parse_devices(
        "\x1b[34m0.00.017.665\x1b[0m \x1b[32mI \x1b[0mcmn  common_param: "
        "common_params_print_info: verbosity = 3 (adjust with the `-lv N` CLI arg)\n"
    ) == [], "a parenthesised tail without MiB is not a device"
    # The 9608 Linux block, which has no module prefix, still parses exactly
    # as it did: the new stripping is additive, not a replacement.
    assert [d["tag"] for d in _parse_devices(FIXTURE_DEVICE_BLOCK)] == ["BLAS", "CPU"]

    # PORT_RE keeps working on a coloured line and across the 9608 -> 10105
    # wording change, because it anchors on the tail. Both are real captures.
    mw = PORT_RE.search(FIXTURE_WIN_LISTEN_ANSI)
    assert mw and int(mw.group("port")) == 65509, FIXTURE_WIN_LISTEN_ANSI
    assert mw.group("host") == "127.0.0.1", mw.group("host")
    assert "server is listening" in FIXTURE_LISTEN, "9608 said 'server is listening on'"
    assert "server is listening" not in FIXTURE_WIN_LISTEN_ANSI, "10105 says just 'listening on'"
    # And on the stripped form, which is what the reader thread now stores.
    assert PORT_RE.search(_strip_ansi(FIXTURE_WIN_LISTEN_ANSI)).group("port") == "65509"

    # -- the colour is also suppressed at the source -----------------------
    # Downstream stripping is the defence; the environment pin is the fix.
    # Both, because the pin depends on the binary honouring a flag and this
    # module runs against whatever build it is pointed at.
    assert _llama_env()["LLAMA_ARG_LOG_COLORS"] == "off", _llama_env()
    # It really is llama.cpp's own option name, and it does not disturb the
    # allow list hearth_proc built.
    assert "PATH" in _llama_env() or "Path" in _llama_env(), _llama_env()
    assert "HEARTH_DB" not in _llama_env(), "the child env allow list must still hold"

    # -- error body extraction --------------------------------------------
    assert _error_message(FIXTURE_ERROR_BODY) == "Expected 'messages' to be an array"
    assert _error_message("not json") is None
    assert _error_message('{"ok":1}') is None

    # -- SSE frame folding, against a verbatim capture ---------------------
    out = consume_stream(FIXTURE_SSE.splitlines())
    assert out["content"] == '"Here,"', repr(out["content"])
    assert out["finish_reason"] == "length", out
    assert out["tokens_in"] == 42 and out["tokens_out"] == 6, out
    assert out["model"] == "hearth-test", out
    assert out["complete"] is True, out
    assert out["tool_calls"] == [], out
    # The callback sees every content delta, in order, and never the opening
    # null-content frame.
    seen = []
    consume_stream(FIXTURE_SSE.splitlines(), on_token=seen.append)
    assert seen == ['"', "H", "e", "re", ",", '"'], seen
    assert "".join(seen) == out["content"]
    # Without include_usage the counts really are absent: the parser must
    # report 0/0 rather than silently sourcing numbers from `timings`, so a
    # caller can tell it forgot the option.
    nousage = consume_stream(FIXTURE_SSE_NO_USAGE.splitlines())
    assert nousage["tokens_in"] == 0 and nousage["tokens_out"] == 0, nousage
    assert nousage["complete"] is True and nousage["finish_reason"] == "length", nousage
    # A truncated stream (no [DONE]) is reported as incomplete rather than
    # passed off as a finished answer.
    truncated = consume_stream(FIXTURE_SSE.splitlines()[:5])
    assert truncated["complete"] is False, truncated
    assert truncated["content"], truncated
    # A stream that ends in an error frame raises rather than returning a
    # cheerful empty answer.
    try:
        consume_stream(['data: {"error":{"message":"context overflow"}}'])
        raise AssertionError("an error frame must raise")
    except LlamaError as exc:
        assert "context overflow" in str(exc), exc
    # Fragmented tool calls are reassembled per index, arguments concatenated.
    tc_frames = [
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"function":{"name":"get_","arguments":"{\\"a\\":"}}]}}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        '"function":{"name":"weather","arguments":"1}"}}]}}]}',
        "data: [DONE]",
    ]
    tc = consume_stream(tc_frames)
    assert len(tc["tool_calls"]) == 1, tc
    assert tc["tool_calls"][0]["id"] == "call_1", tc
    assert tc["tool_calls"][0]["function"]["name"] == "get_weather", tc
    assert tc["tool_calls"][0]["function"]["arguments"] == '{"a":1}', tc
    # Junk between frames (SSE comments, blank lines, a half-written frame)
    # is skipped, not fatal.
    noisy = consume_stream([": ping", "", "data: {broken", "event: message"] +
                           FIXTURE_SSE.splitlines())
    assert noisy["content"] == out["content"], noisy

    # -- argv construction -------------------------------------------------
    argv = build_argv("C:\\Program Files\\Hearth\\llama\\llama-server.exe",
                      "D:\\models\\my model.gguf", n_gpu_layers=20, ctx_size=8192,
                      alias="hearth")
    assert argv[0].endswith("llama-server.exe")
    # Paths with spaces stay ONE argv element; quoting is subprocess's job and
    # pre-quoting here would make llama-server look for a file whose name
    # contains literal quotes.
    assert "D:\\models\\my model.gguf" in argv, argv
    assert '"' not in "".join(argv), argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1", argv
    assert argv[argv.index("--port") + 1] == "0", "must default to an ephemeral port"
    assert "--no-webui" in argv
    assert argv[argv.index("-ngl") + 1] == "20", argv
    assert argv[argv.index("-c") + 1] == "8192", argv
    assert argv[argv.index("--alias") + 1] == "hearth", argv
    # Never 0.0.0.0, never a fixed well-known port, and no metrics/props
    # endpoints opened by default.
    assert "0.0.0.0" not in argv, argv
    assert "8080" not in argv, argv
    assert "--metrics" not in argv and "--props" not in argv, argv
    assert "--api-key" not in argv, argv
    # -ngl "all" and 0 are both passed through verbatim, since llama-server
    # accepts an integer, "auto" or "all".
    assert "all" in build_argv("s", "m", n_gpu_layers="all"), build_argv("s", "m", n_gpu_layers="all")
    zero = build_argv("s", "m", n_gpu_layers=0)
    assert zero[zero.index("-ngl") + 1] == "0", zero
    # Omitting -ngl entirely is distinct from -ngl 0.
    assert "-ngl" not in build_argv("s", "m"), build_argv("s", "m")
    assert build_argv("s", "m", extra=["--flash-attn", "on"])[-2:] == ["--flash-attn", "on"]

    # -- binary discovery --------------------------------------------------
    scratch = tempfile.mkdtemp(prefix="hearth-llama-")
    try:
        # Every find_server() call below passes this, so no test can be
        # answered by whatever engine the machine running it happens to
        # have fetched. Discovery now consults hearth_engine, and without
        # this the "bundled build is found" case silently becomes "the
        # developer's own GPU engine is found".
        no_engine = {hearth_engine.ENV_ENGINE_DIR: os.path.join(scratch, "no-engines")}

        fake = os.path.join(scratch, _exe(SERVER_BASENAME))
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        if not hearth_paths.is_windows():
            os.chmod(fake, 0o700)

        # 1. Env override wins, and is reported as such.
        r = find_server(env=dict(no_engine, **{ENV_SERVER: fake}))
        assert r["found"] is True and r["source"] == FOUND_ENV, r
        assert os.path.samefile(r["path"], fake), r
        assert r["searched"] == [fake], r
        # A quoted path (as a Windows user pastes it) still resolves.
        r = find_server(env=dict(no_engine, **{ENV_SERVER: '"{}"'.format(fake)}))
        assert r["found"] is True, r

        # 2. A BROKEN override is a distinct, named failure -- it must not
        # fall through to PATH and pretend everything is fine, or the user
        # never learns their setting is wrong.
        missing = os.path.join(scratch, "nope", _exe(SERVER_BASENAME))
        r = find_server(env=dict(no_engine, **{ENV_SERVER: missing}))
        assert r["found"] is False and r["source"] == FOUND_MISSING, r
        assert ENV_SERVER in r["reason"] and "nope" in r["reason"], r

        # 3. The bundled location, under a redirected app root.
        bundle = os.path.join(scratch, "install", "llama")
        os.makedirs(bundle)
        bundled = os.path.join(bundle, _exe(SERVER_BASENAME))
        with open(bundled, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        if not hearth_paths.is_windows():
            os.chmod(bundled, 0o700)
        with mock.patch.dict(os.environ, {ENV_SERVER_DIR: os.path.join(scratch, "install")}):
            r = find_server(env=no_engine)
            assert r["found"] is True and r["source"] == FOUND_BUNDLED, r
            assert os.path.samefile(r["path"], bundled), r
            # Every location tried is reported, so a setup screen can show them.
            assert len(r["searched"]) >= 2, r

        # 4. Nothing anywhere: not found, with the full search list and a
        # reason that says the install is incomplete.
        empty = os.path.join(scratch, "empty")
        os.makedirs(empty)
        with mock.patch.dict(os.environ, {ENV_SERVER_DIR: empty}), \
             mock.patch.object(sys.modules[__name__], "shutil",
                               mock.Mock(which=lambda *a, **k: None)):
            r = find_server(env=no_engine)
            assert r["found"] is False and r["path"] is None, r
            assert any("PATH" in s for s in r["searched"]), r
            assert r["reason"], r

        # 5. Long paths: a bundled binary buried past Windows' MAX_PATH is
        # still detected, because _is_runnable goes through long_path().
        deep = scratch
        for _ in range(12):
            deep = os.path.join(deep, "d" * 24)
        os.makedirs(hearth_paths.long_path(deep), exist_ok=True)
        deep_exe = os.path.join(deep, _exe(SERVER_BASENAME))
        with open(hearth_paths.long_path(deep_exe), "w", encoding="utf-8") as fh:
            fh.write("x")
        if not hearth_paths.is_windows():
            os.chmod(hearth_paths.long_path(deep_exe), 0o700)
        assert len(deep_exe) > hearth_paths.MAX_COMFORTABLE_PATH, len(deep_exe)
        assert _is_runnable(deep_exe) is True, deep_exe
        assert _is_runnable(os.path.join(deep, "absent.exe")) is False
        assert _is_runnable(None) is False and _is_runnable("") is False
        # A directory is not a runnable binary.
        assert _is_runnable(scratch) is False

        # -- 6. the fetched GPU engine outranks the bundled CPU one --------
        # It only exists after hearth_engine downloaded it against a pinned
        # hash and proved it runs here, so when it exists it wins; when it
        # does not, this step costs nothing and the bundled build takes over.
        eng_root = os.path.join(scratch, "engines")
        acq_dir = os.path.join(eng_root, "b1-win-vulkan-x64")
        os.makedirs(acq_dir)
        acq_exe = os.path.join(acq_dir, _exe(SERVER_BASENAME))
        with open(acq_exe, "w", encoding="utf-8") as fh:
            fh.write("x")
        if not hearth_paths.is_windows():
            os.chmod(acq_exe, 0o700)
        pointer = {"variant": "win-vulkan-x64", "backend": "vulkan", "build": 10105,
                   "release_tag": "b1", "dir": acq_dir, "server": acq_exe,
                   "device": "NVIDIA GeForce RTX 5080", "devices": [],
                   "source": "policy", "verified_at": "2026-08-01T00:00:00Z"}
        eng_env = {hearth_engine.ENV_ENGINE_DIR: eng_root}
        hearth_engine._write_json(hearth_engine.active_path(eng_env), pointer)
        install_root = os.path.join(scratch, "install")
        with mock.patch.dict(os.environ, {ENV_SERVER_DIR: install_root}):
            r = find_server(env=eng_env)
            assert r["found"] is True and r["source"] == FOUND_ACQUIRED, r
            assert os.path.samefile(r["path"], acq_exe), r
            assert "vulkan" in r["reason"], r

            # An explicit env override still outranks it: a developer
            # pointing at a specific build gets that build.
            r = find_server(env=dict(eng_env, **{ENV_SERVER: fake}))
            assert r["source"] == FOUND_ENV, r

            # THE FALLBACK, at its cheapest: the pointer survives but the
            # binary is gone. Nothing raises, nothing is reported as broken,
            # and the bundled CPU build is simply what runs.
            os.unlink(acq_exe)
            r = find_server(env=eng_env)
            assert r["found"] is True and r["source"] == FOUND_BUNDLED, r
            assert os.path.samefile(r["path"], bundled), r
            with open(acq_exe, "w", encoding="utf-8") as fh:
                fh.write("x")
            if not hearth_paths.is_windows():
                os.chmod(acq_exe, 0o700)

            # No pointer at all is the ordinary case and behaves the same.
            os.unlink(hearth_engine.active_path(eng_env))
            r = find_server(env=eng_env)
            assert r["source"] == FOUND_BUNDLED, r
            hearth_engine._write_json(hearth_engine.active_path(eng_env), pointer)

        # -- engine_level_failure: blaming the binary, not the model -------
        # The whole automatic fallback rests on this classification, so it
        # is driven from the real stderr fixtures rather than from prose.
        class _StderrOnly:
            def __init__(self, text, port=None):
                self.port = port
                self._text = text

            def stderr_tail(self, limit=40):  # noqa: ARG002 - signature parity
                return self._text

        # Nothing at all on stderr and no port: this is what a Windows
        # binary whose DLLs are missing looks like. Blame the binary.
        assert engine_level_failure(_StderrOnly("")) is True
        assert engine_level_failure(_StderrOnly(
            "The specified module could not be found.\n")) is True
        # It printed its version, so it loaded: a later failure is the
        # model's or the machine's, and must NOT cost the user their GPU.
        assert engine_level_failure(_StderrOnly(FIXTURE_VERSION)) is False
        assert engine_level_failure(
            _StderrOnly(FIXTURE_VERSION + FIXTURE_LOAD_FAILURE)) is False
        assert engine_level_failure(_StderrOnly(FIXTURE_WIN_VERSION)) is False
        # It got as far as binding a port, so it is running.
        assert engine_level_failure(_StderrOnly("", port=45727)) is False
        assert engine_level_failure(None) is False

        # -- start() demotes an engine that dies at launch, and retries ----
        # This is the fallback proven end to end: a GPU engine that
        # verified at install time but cannot start now must leave the user
        # on a WORKING CPU engine rather than on an error.
        gguf = os.path.join(scratch, "fallback.gguf")
        with open(gguf, "wb") as fh:
            fh.write(b"\0" * 16)

        class _FakeProc:
            def __init__(self, rc=None):
                self.returncode = rc
                self.pid = 4242

            def poll(self):
                return self.returncode

        spawned = []

        def _fake_spawn(self, n_gpu_layers=None, ctx_size=None, alias=None,
                        parallel=1, extra=None):  # noqa: ARG001
            spawned.append(self.server_path)
            self.proc = _FakeProc()
            self.argv = [self.server_path]

        def _fake_stop(self, grace=None):  # noqa: ARG001
            self.proc = None
            return 1

        def _dll_failure_then_ok(self):
            """wait_ready that fails like a missing DLL on the fetched
            engine and succeeds on anything else."""
            def _inner(inner_self, timeout=None, interval=None):  # noqa: ARG001
                if os.path.normcase(inner_self.server_path) == os.path.normcase(acq_exe):
                    inner_self.proc = _FakeProc(rc=3221225781)
                    raise LlamaError("llama-server exited with code 3221225781")
                inner_self.port = 45727
                inner_self.state = STATE_READY
                return STATE_READY
            return _inner

        with mock.patch.dict(os.environ, {ENV_SERVER_DIR: install_root,
                                          hearth_engine.ENV_ENGINE_DIR: eng_root}), \
             mock.patch.object(sys.modules[__name__], "probe_binary",
                               lambda p, **k: {"ok": True, "path": p, "build": 10105,
                                               "commit": "abc", "backend": BACKEND_VULKAN,
                                               "gpu_offload": True, "devices": [],
                                               "error": None}), \
             mock.patch.object(Server, "spawn", _fake_spawn), \
             mock.patch.object(Server, "stop", _fake_stop), \
             mock.patch.object(Server, "wait_ready", _dll_failure_then_ok(None)):
            assert hearth_engine.active_server_path() == acq_exe
            server = start(gguf, ctx_size=2048, n_gpu_layers=0)
            # It tried the fetched engine first, then the bundled one, and
            # the caller got a running server rather than an exception.
            assert [os.path.normcase(p) for p in spawned] == [
                os.path.normcase(acq_exe), os.path.normcase(bundled)], spawned
            assert os.path.normcase(server.server_path) == os.path.normcase(bundled)
            assert server.state == STATE_READY
            # The engine was demoted, so the NEXT launch does not repeat the
            # failure, and the reason is recorded for the setup screen.
            assert hearth_engine.active() is None
            state = hearth_engine.read_state()
            assert state["state"] == hearth_engine.STATE_FAILED, state
            assert any(f["stage"] == "launch" for f in state["failed"]), state

        # MUTATION: the same crash, but the binary DID print its version, so
        # the fault is the model's. The engine must survive.
        hearth_engine._write_json(hearth_engine.active_path(eng_env), pointer)
        spawned[:] = []

        def _model_failure(inner_self, timeout=None, interval=None):  # noqa: ARG001
            inner_self._stderr_lines = [FIXTURE_VERSION, FIXTURE_LOAD_FAILURE]
            inner_self.proc = _FakeProc(rc=1)
            raise LlamaError("llama-server exited with code 1")

        with mock.patch.dict(os.environ, {ENV_SERVER_DIR: install_root,
                                          hearth_engine.ENV_ENGINE_DIR: eng_root}), \
             mock.patch.object(sys.modules[__name__], "probe_binary",
                               lambda p, **k: {"ok": True, "path": p, "build": 10105,
                                               "commit": "abc", "backend": BACKEND_VULKAN,
                                               "gpu_offload": True, "devices": [],
                                               "error": None}), \
             mock.patch.object(Server, "spawn", _fake_spawn), \
             mock.patch.object(Server, "stop", _fake_stop), \
             mock.patch.object(Server, "wait_ready", _model_failure):
            try:
                start(gguf, ctx_size=2048, n_gpu_layers=0)
                raise AssertionError("a model that will not load must still raise")
            except LlamaError:
                pass
            assert len(spawned) == 1, ("a model failure must not retry on another "
                                       "engine", spawned)
            assert hearth_engine.active() is not None, (
                "a model failure must not cost the user their GPU engine")

        # MUTATION: an engine whose binary no longer runs at all is caught
        # BEFORE it is spawned, by the probe, and still falls back.
        spawned[:] = []
        with mock.patch.dict(os.environ, {ENV_SERVER_DIR: install_root,
                                          hearth_engine.ENV_ENGINE_DIR: eng_root}), \
             mock.patch.object(
                 sys.modules[__name__], "probe_binary",
                 lambda p, **k: {"ok": os.path.normcase(p) != os.path.normcase(acq_exe),
                                 "path": p, "build": 10105, "commit": "abc",
                                 "backend": BACKEND_CPU, "gpu_offload": False,
                                 "devices": [],
                                 "error": None if os.path.normcase(p) !=
                                 os.path.normcase(acq_exe) else
                                 "[WinError 126] The specified module could not be found"}), \
             mock.patch.object(Server, "spawn", _fake_spawn), \
             mock.patch.object(Server, "stop", _fake_stop), \
             mock.patch.object(Server, "wait_ready",
                               lambda s, timeout=None, interval=None: setattr(
                                   s, "port", 45727) or STATE_READY):
            server = start(gguf, ctx_size=2048, n_gpu_layers=0)
            assert [os.path.normcase(p) for p in spawned] == [
                os.path.normcase(bundled)], (
                "a broken engine must not even be spawned", spawned)
            assert hearth_engine.active() is None

        # A caller that names a binary explicitly is never second-guessed:
        # its failure is its own, and no engine is demoted for it.
        hearth_engine._write_json(hearth_engine.active_path(eng_env), pointer)
        with mock.patch.dict(os.environ, {ENV_SERVER_DIR: install_root,
                                          hearth_engine.ENV_ENGINE_DIR: eng_root}), \
             mock.patch.object(sys.modules[__name__], "probe_binary",
                               lambda p, **k: {"ok": True, "path": p, "build": 10105,
                                               "commit": "abc", "backend": BACKEND_VULKAN,
                                               "gpu_offload": True, "devices": [],
                                               "error": None}), \
             mock.patch.object(Server, "spawn", _fake_spawn), \
             mock.patch.object(Server, "stop", _fake_stop), \
             mock.patch.object(Server, "wait_ready", _dll_failure_then_ok(None)):
            spawned[:] = []
            try:
                start(gguf, server_path=acq_exe, ctx_size=2048, n_gpu_layers=0)
                raise AssertionError("an explicitly named binary's failure must raise")
            except LlamaError:
                pass
            assert len(spawned) == 1, spawned
            assert hearth_engine.active() is not None
            os.unlink(hearth_engine.active_path(eng_env))

        # -- -ngl selection ------------------------------------------------
        # A 4 GiB model file, so the per-layer arithmetic has real numbers.
        model_file = os.path.join(scratch, "m.gguf")
        with open(model_file, "wb") as fh:
            fh.write(b"\0")
        four_gib = 4 * 1024 ** 3
        with mock.patch.object(sys.modules[__name__], "_model_bytes", lambda p: four_gib):
            # A CPU-only build offloads nothing, and says why.
            n, why = choose_gpu_layers(model_file, backend=BACKEND_CPU, vram_bytes=24 * 1024 ** 3)
            assert n == 0 and "no GPU backend" in why, (n, why)
            # No GPU detected: also nothing, also with a reason.
            n, why = choose_gpu_layers(model_file, backend=BACKEND_CUDA, vram_bytes=0)
            assert n == 0 and "no GPU VRAM" in why, (n, why)
            # 24 GiB against a 4 GiB model: everything fits.
            n, why = choose_gpu_layers(model_file, backend=BACKEND_CUDA,
                                       vram_bytes=24 * 1024 ** 3, total_layers=32)
            assert n == "all", (n, why)
            # 6 GiB (an RTX 2060) against the same model: a partial offload.
            # Budget = 6GiB - max(512MiB, 5% of 6GiB) = 6GiB - 512MiB.
            # Per layer = 4GiB/32 = 128MiB. floor(5632/128) = 44 -> clamped
            # to the model's 32 layers, so this one still fits entirely.
            n, why = choose_gpu_layers(model_file, backend=BACKEND_CUDA,
                                       vram_bytes=6 * 1024 ** 3, total_layers=32)
            assert n == "all", (n, why)
            # 2 GiB: genuinely partial. Budget = 2GiB - 512MiB = 1536MiB;
            # 1536/128 = 12 layers of 32.
            n, why = choose_gpu_layers(model_file, backend=BACKEND_CUDA,
                                       vram_bytes=2 * 1024 ** 3, total_layers=32)
            assert n == 12, (n, why)
            assert "12 of ~32" in why, why
            # 256 MiB: the margin eats the whole card, so zero layers, and
            # the reason says so rather than pretending a fit.
            n, why = choose_gpu_layers(model_file, backend=BACKEND_CUDA,
                                       vram_bytes=256 * 1024 ** 2, total_layers=32)
            assert n == 0, (n, why)
            # The margin is real: it must never hand out the whole card.
            n, why = choose_gpu_layers(model_file, backend=BACKEND_CUDA,
                                       vram_bytes=four_gib, total_layers=32)
            assert n != "all" and n < 32, (n, why)

            # --- integrated GPUs: the reported VRAM is not a budget ---------
            #
            # The exact reading that prompted this: a Radeon 880M reports
            # 512 MiB through Win32_VideoController.AdapterRAM while sitting in
            # a machine with 33 GiB of RAM it shares. Judged as a discrete card
            # that is zero layers, and measuring it cost 1.8x of throughput
            # (9.7 tok/s against 17.1 on Dolphin 3.0 8B Q4_K_M). Judged as what
            # it is, every layer goes over, because the weights occupy that
            # same RAM either way.
            #
            # The discrete case above already pins that 256 MiB means zero, so
            # these two assertions together are what stop the fix from being a
            # blanket "small VRAM means offload anyway".
            igpu_vram = 512 * 1024 ** 2
            n, why = choose_gpu_layers(model_file, backend=BACKEND_VULKAN,
                                       vram_bytes=igpu_vram, total_layers=32,
                                       integrated=True)
            assert n == "all", (n, why)
            assert "shares system memory" in why, why
            # Same reading, discrete: still zero. The flag is doing the work,
            # not the number.
            n, why = choose_gpu_layers(model_file, backend=BACKEND_VULKAN,
                                       vram_bytes=igpu_vram, total_layers=32,
                                       integrated=False)
            assert n == 0, (n, why)
            # The override wins over every one of the above, including the
            # "this build cannot offload" case.
            n, why = choose_gpu_layers(model_file, backend=BACKEND_CPU, vram_bytes=0,
                                       env={ENV_GPU_LAYERS: "17"})
            assert n == 17 and ENV_GPU_LAYERS in why, (n, why)
            n, why = choose_gpu_layers(model_file, backend=BACKEND_CPU, vram_bytes=0,
                                       env={ENV_GPU_LAYERS: "all"})
            assert n == "all", (n, why)
            # Garbage in the override falls through to the real decision
            # rather than crashing a launch.
            n, why = choose_gpu_layers(model_file, backend=BACKEND_CPU, vram_bytes=0,
                                       env={ENV_GPU_LAYERS: "lots"})
            assert n == 0, (n, why)
        # An unstat-able model refuses to guess.
        n, why = choose_gpu_layers(os.path.join(scratch, "absent.gguf"),
                                   backend=BACKEND_CUDA, vram_bytes=four_gib)
        assert n == 0 and "cannot size" in why, (n, why)

        # -- context size comes from hearth_loop's ladder, not a copy ------
        import hearth_loop
        ctx = choose_ctx_size(model_path=None, vram_bytes=0)
        assert ctx == hearth_loop.FALLBACK_NUM_CTX, ctx
        big = choose_ctx_size(model_path=None, vram_bytes=48 * 1024 ** 3,
                              kv_bytes_per_token=1024)
        assert big in hearth_loop.CONTEXT_LADDER, big
        assert big > ctx, (big, ctx)
        # Every value this module can pick is a rung of that one ladder.
        for vram in (0, 2 * 1024 ** 3, 8 * 1024 ** 3, 24 * 1024 ** 3, 80 * 1024 ** 3):
            assert choose_ctx_size(vram_bytes=vram) in hearth_loop.CONTEXT_LADDER, vram
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    # -- readiness state machine, against a stub process -------------------
    # No llama-server needed: a Server is pointed at a stub child and its
    # health endpoint is stubbed, so every transition is exercised offline.
    stub_argv = [sys.executable, "-c", _stub_server_script(60)]
    srv = Server("stub", "model.gguf")
    srv.proc, srv.guarded = _spawn_guarded(stub_argv)
    try:
        # No port announced yet -> STARTING, not READY and not CRASHED.
        assert srv.port is None
        assert srv.poll_state() == STATE_STARTING, srv.state
        # Port known, health says loading -> LOADING. "Process is alive" is
        # explicitly NOT accepted as ready.
        srv.port = 12345
        with mock.patch.object(sys.modules[__name__], "health", lambda *a, **k: STATE_LOADING):
            assert srv.poll_state() == STATE_LOADING, srv.state
            # ... and wait_ready must NOT return while it is still loading.
            try:
                srv.wait_ready(timeout=0.6, interval=0.1)
                raise AssertionError("wait_ready returned while the model was still loading")
            except LlamaError as exc:
                assert "still loading" in str(exc), exc
                assert "model.gguf" in str(exc), exc
        # Health says ok -> READY.
        with mock.patch.object(sys.modules[__name__], "health", lambda *a, **k: STATE_READY):
            assert srv.poll_state() == STATE_READY
            assert srv.wait_ready(timeout=5) == STATE_READY
        # Port announced but nothing listening yet is STARTING, not CRASHED.
        with mock.patch.object(sys.modules[__name__], "health", lambda *a, **k: None):
            assert srv.poll_state() == STATE_STARTING, srv.state
        assert srv.base_url == "http://127.0.0.1:12345", srv.base_url
    finally:
        rc = srv.stop(grace=10)
    # stop() really kills it, and is idempotent.
    assert srv.proc is None and srv.state == STATE_STOPPED
    assert srv.stop() is None
    assert rc is not None, "stop() could not confirm the stub process died"

    # -- crash detection ---------------------------------------------------
    # A server whose process has exited must be reported as CRASHED with its
    # own stderr attached, not hang on the next request.
    crasher = Server("stub", "bad.gguf")
    crasher.proc, _ = _spawn_guarded(
        [sys.executable, "-c",
         "import sys;sys.stderr.write({!r});sys.exit(1)".format(FIXTURE_LOAD_FAILURE)])
    crasher._reader = threading.Thread(target=crasher._drain_stderr, daemon=True)
    crasher._reader.start()
    crasher.proc.wait(timeout=30)
    crasher._reader.join(timeout=5)
    assert crasher.poll_state() == STATE_CRASHED, crasher.state
    try:
        crasher.wait_ready(timeout=5)
        raise AssertionError("wait_ready must raise when the server died")
    except LlamaError as exc:
        assert "exited with code 1" in str(exc), exc
        # The user gets llama-server's own explanation, not just a code.
        assert "exiting due to model loading error" in str(exc), exc
    # chat() against a dead server fails immediately instead of hanging.
    try:
        crasher.chat([{"role": "user", "content": "hi"}])
        raise AssertionError("chat must refuse a dead server")
    except LlamaError as exc:
        assert "not running" in str(exc), exc
    crasher.stop()

    # -- the port is really read from the child's stderr -------------------
    # A stub that prints the real announcement line proves the reader thread
    # and PORT_RE work together against a live pipe, not just a fixture.
    echoer = Server("stub", "m.gguf")
    echoer.proc, _ = _spawn_guarded(
        [sys.executable, "-c",
         "import sys,time;sys.stderr.write({!r});sys.stderr.flush();time.sleep(20)".format(
             FIXTURE_LISTEN)])
    echoer._reader = threading.Thread(target=echoer._drain_stderr, daemon=True)
    echoer._reader.start()
    try:
        deadline = time.monotonic() + 20
        while echoer.port is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert echoer.port == 45727, "port was not read back from stderr"
        assert echoer.base_url == "http://127.0.0.1:45727", echoer.base_url
        assert "model loaded" in echoer.stderr_tail(), echoer.stderr_tail()
    finally:
        echoer.stop()

    # -- and the same thing over a REAL colourised stream -------------------
    # A stub that replays the exact bytes build 10105 wrote down Hearth's own
    # stderr pipe. This proves the reader thread, _strip_ansi and PORT_RE
    # work together on the live path, which is where the escapes actually
    # broke things -- the fixture tests above only prove the parsing.
    ansi_echoer = Server("stub", "m.gguf")
    ansi_echoer.proc, _ = _spawn_guarded(
        [sys.executable, "-c",
         "import sys,time;sys.stderr.write({!r});sys.stderr.flush();time.sleep(20)".format(
             FIXTURE_WIN_WARN_ANSI + FIXTURE_WIN_LISTEN_ANSI)])
    ansi_echoer._reader = threading.Thread(target=ansi_echoer._drain_stderr, daemon=True)
    ansi_echoer._reader.start()
    try:
        deadline = time.monotonic() + 20
        while ansi_echoer.port is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ansi_echoer.port == 65509, "port was not read back from colourised stderr"
        tail = ansi_echoer.stderr_tail()
        # The tail goes into LlamaError messages and therefore in front of a
        # user, so it must not carry escape bytes.
        assert "\x1b" not in tail, repr(tail)
        assert "[34m" not in tail and "[0m" not in tail, repr(tail)
        # Stripped of colour, not of content.
        assert "llama_server: model loaded" in tail, tail
        assert "CORS is set to allow all origins" in tail, tail
        assert "0.00.035.313" in tail, tail
    finally:
        ansi_echoer.stop()

    # -- health() against a real HTTP server, with real llama-server bodies -
    # http.server is stdlib, so the 503-then-200 transition that was measured
    # against llama-server can be replayed for real over a socket.
    import http.server

    phase = {"loading": True}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if phase["loading"]:
                body = (b'{"error":{"message":"Loading model",'
                        b'"type":"unavailable_error","code":503}}')
                self.send_response(503)
            else:
                body = b'{"status":"ok"}'
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        url = "http://127.0.0.1:{}".format(httpd.server_address[1])
        assert health(url) == STATE_LOADING, "503 must read as loading, not as an error"
        phase["loading"] = False
        assert health(url) == STATE_READY
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    # Nothing listening at all is None (unreachable), which is neither
    # loading nor ready.
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    assert health("http://127.0.0.1:{}".format(dead_port), timeout=2) is None

    # -- orphan prevention, and its mutation --------------------------------
    # The guarantee under test: kill the Python parent WITHOUT cleanup and
    # the model server must not survive holding VRAM. Windows uses a
    # kill-on-close Job Object; Linux uses PR_SET_PDEATHSIG. Both are
    # exercised here; on any other platform there is no mechanism to test, so
    # the test is skipped rather than faked.
    if hearth_paths.is_windows() or sys.platform.startswith("linux"):
        pid, survived = _orphan_probe(disable_guard=False)
        assert survived is False, (
            "grandchild {} survived its parent being hard-killed: the orphan "
            "protection is not working".format(pid))
        # Mutation: with the guard removed the same probe must FAIL, which is
        # what proves the assertion above is testing the mechanism and not
        # something incidental (an inherited console, a helpful shell).
        pid, survived = _orphan_probe(disable_guard=True)
        assert survived is True, (
            "with the guard disabled the grandchild should have survived; it "
            "did not, so the positive test above proves nothing")
        # Clean up the deliberately-orphaned process from the mutation run.
        if hearth_paths.is_windows():
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=30)
        else:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        # And the guard reports honestly whether it engaged.
        p, guarded = _spawn_guarded([sys.executable, "-c", _stub_server_script(5)])
        try:
            assert guarded is True, "orphan protection did not engage on this platform"
        finally:
            hearth_proc._kill_tree(p)
            try:
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass

    # -- probe_binary degrades honestly when there is no binary -------------
    with mock.patch.object(sys.modules[__name__], "find_server",
                           lambda **k: {"found": False, "path": None, "source": FOUND_MISSING,
                                        "searched": [], "reason": "not here"}):
        info = probe_binary()
        assert info["ok"] is False and info["error"] == "not here", info
        assert info["backend"] == BACKEND_UNKNOWN and info["gpu_offload"] is False, info
    # A path that is not an executable is an error, never a fake success.
    info = probe_binary(os.path.join(tempfile.gettempdir(), "definitely-not-here-hl"))
    assert info["ok"] is False and info["error"], info
    # start() with no binary raises with the search list in the message.
    with mock.patch.object(sys.modules[__name__], "find_server",
                           lambda **k: {"found": False, "path": None, "source": FOUND_MISSING,
                                        "searched": ["/a", "/b"], "reason": "nope"}):
        try:
            start("whatever.gguf")
            raise AssertionError("start must raise without a binary")
        except LlamaError as exc:
            assert "/a" in str(exc) and "/b" in str(exc), exc
    # start() with a binary but no model file names the model, not the binary.
    with mock.patch.object(sys.modules[__name__], "find_server",
                           lambda **k: {"found": True, "path": sys.executable,
                                        "source": FOUND_PATH, "searched": [],
                                        "reason": "stub"}):
        try:
            start(os.path.join(tempfile.gettempdir(), "absent-model-hl.gguf"))
            raise AssertionError("start must raise without a model")
        except LlamaError as exc:
            assert "no model file" in str(exc), exc

    # -- CLI argument wiring ------------------------------------------------
    # --model has to work on BOTH sides of the subcommand. The failure this
    # guards is silent: without default=SUPPRESS on the subparser's copy,
    # argparse writes None over the outer value and `chat` reports "chat
    # needs --model" to someone who plainly supplied one.
    parser = _build_parser()
    a = parser.parse_args(["--model", "m.gguf", "chat", "hello"])
    assert a.model == "m.gguf" and a.prompt == "hello", a
    b = parser.parse_args(["chat", "hello", "--model", "m.gguf"])
    assert b.model == "m.gguf", b
    c = parser.parse_args(["--self-test", "--live", "--model", "m.gguf"])
    assert c.self_test and c.live and c.model == "m.gguf", c
    # No subcommand at all means "find", which is what a bare run should do.
    d = parser.parse_args([])
    assert d.command is None and d.self_test is False, d

    if live:
        _live_test(model)

    print("hearth-llama self-test OK{}".format(" (with --live)" if live else ""))
    return 0


def _live_test(model_path):
    """The tests that need a real llama-server and a real GGUF.

    Deliberately behind --live: this machine ships no binary, and a test that
    silently passes because it skipped the only thing it was meant to check
    is worse than no test. Run as:

        python agent/hearth_llama.py --self-test --live --model path/to.gguf
    """
    found = find_server()
    assert found["found"], "no llama-server for --live: {}".format(found["reason"])
    assert model_path and os.path.isfile(model_path), \
        "--live needs --model pointing at a real GGUF"

    info = probe_binary(found["path"])
    assert info["ok"], info
    assert isinstance(info["build"], int) and info["build"] > 0, info
    assert info["backend"] in (BACKEND_CPU, BACKEND_CUDA, BACKEND_VULKAN, BACKEND_ROCM,
                               BACKEND_SYCL, BACKEND_METAL, BACKEND_UNKNOWN), info
    # Defect 1, against whatever real binary is installed: a binary we could
    # run and get a version out of must be describable. "unknown" here is the
    # bug -- it is what the Windows CPU build reported, and what
    # hearth_backend.diagnose() then showed the user.
    assert info["backend"] != BACKEND_UNKNOWN, (
        "probe_binary could not classify a binary it successfully ran: {}".format(info))
    # gpu_offload must agree with the backend rather than being a second,
    # independently-wrong opinion.
    assert info["gpu_offload"] == (info["backend"] in GPU_BACKENDS), info

    server = start(model_path, server_path=found["path"], ready_timeout=300,
                   alias="hearth-live")
    try:
        assert server.state == STATE_READY, server.state
        # An ephemeral port, never the default 8080, and loopback only.
        assert server.port and server.port != 8080, server.port
        assert server.host == "127.0.0.1"
        assert health(server.base_url) == STATE_READY
        # Defect 2, against the real binary: the port was read back off a
        # stream that build 10105 colourises, and the retained stderr the
        # user would be shown carries no escape bytes.
        tail = server.stderr_tail()
        assert tail, "no stderr was retained from a server that started"
        assert "\x1b" not in tail, repr(tail[-400:])
        # The startup banner really was captured, not just emptied.
        assert "llama_server" in tail, tail[-400:]
        # A real streaming turn, with real usage counts.
        seen = []
        out = server.chat([{"role": "user", "content": "Say hello."}],
                          on_token=seen.append, max_tokens=16)
        assert out["complete"] is True, out
        assert out["content"] and "".join(seen) == out["content"], (out, seen)
        assert out["tokens_in"] > 0 and out["tokens_out"] > 0, out
        # A model swap really restarts on a different port.
        old_port = server.port
        server.swap_model(model_path, ctx_size=512, n_gpu_layers=0)
        server.wait_ready(timeout=300)
        assert server.port and server.port != old_port, (old_port, server.port)
        assert health(server.base_url) == STATE_READY
    finally:
        rc = server.stop()
    assert server.poll_state() == STATE_STOPPED, server.state
    # stop() must confirm the tree died. The CODE it reports is not a verdict:
    # on Windows a terminate() is TerminateProcess(handle, 1), so a perfectly
    # healthy server that served this whole test exits 1. None is the only
    # value that means trouble, and that is what is asserted here.
    assert rc is not None, "stop() could not confirm llama-server died"
    if hearth_paths.is_windows():
        assert rc == 1, (
            "expected TerminateProcess's exit code 1 on Windows, got {!r}; if this "
            "changed, stop()'s docstring needs revisiting".format(rc))
    print("hearth-llama live checks OK (build {}, backend {}, stop() exit code {})".format(
        info["build"], info["backend"], rc))


if __name__ == "__main__":
    if "--self-test" in sys.argv or len(sys.argv) == 1:
        sys.exit(main(sys.argv[1:] if len(sys.argv) > 1 else ["--self-test"]))
    sys.exit(main())
