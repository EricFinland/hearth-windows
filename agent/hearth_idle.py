#!/usr/bin/env python3
r"""hearth idle awareness: is now a good time to run heavy work, and why.

Hearth's target user wants to run local models hard, sometimes overnight,
sometimes for hours in the background. The same machine is also the one they
game on, take calls on, and work on. A model saturating the GPU during a
video call is a bad neighbour, and a user who gets burned once will stop
leaving Hearth running unattended. The job of this module is not "throttle
the AI" - it is "use the machine when the human is not, and get out of the
way when they are."

SIGNALS, AND WHICH ONES ARE HONEST.

  User input idleness (Windows only). GetLastInputInfo via ctypes.windll
  gives milliseconds since the last keyboard or mouse event, system-wide,
  cheaply, from the standard library. This is the single best signal
  available anywhere in this module. On Linux there is no headless
  equivalent: X11 idle time needs the XScreenSaver or XSync extension
  (a library this module does not have, and would not always be present
  even if it did - many Linux boxes running Hearth are headless), and
  Wayland does not expose global input idleness to unprivileged clients at
  all, by design. So on Linux this signal is simply unavailable, and this
  module says so rather than faking a number.

  GPU contention. nvidia-smi can report overall utilization and memory
  used. agent/hearth_hw.py already shells out to nvidia-smi safely, with a
  bounded timeout and clean degrade-to-nothing on any failure; the
  subprocess helper below follows that exact pattern (same timeout
  discipline, same encoding="utf-8", errors="replace" safety, same "any
  failure at all becomes no data, never an exception").

  This was wrong in an earlier version of this module, in two ways found
  by testing against real machines rather than by inspection, and both are
  worth recording because the fix depends on understanding why they broke:

  1. Utilization and memory are not interchangeable evidence, and treating
     a low memory threshold as independently decisive was the bug.
     Sustained utilization means the GPU is actively computing right now.
     Resting memory means something is merely resident - a desktop
     compositor's framebuffer alone holds VRAM at every idle machine with
     a display attached (observed empirically: 10% resident, 0%
     utilization, on a real Windows desktop untouched for 52 minutes).
     A memory-only threshold anywhere near that baseline reads every idle
     machine as busy essentially forever - the exact failure the module
     exists to avoid, and silently worse than having no detection at all,
     because it looks like the feature is working. So utilization is now
     the primary, decisive signal; memory is corroboration only, at a
     threshold (gpu_busy_mem_percent, default 40%) picked to sit well
     above what a compositor and background apps hold at rest across
     common GPU sizes, not just nudged up from the old number.

  2. "The card as a whole" conflates two different processes: something
     else competing for the GPU, versus Hearth's own inference stack
     already having a model loaded. Reading raw total memory used, that
     distinction is invisible - and a model Hearth itself loaded and left
     idle would then read as permanent contention, meaning the feature
     disables itself the first time it is actually used (observed on a
     real host: Ollama holding a model resident, 76% of VRAM, nothing
     generating, and the module answering "busy" anyway). OWNERSHIP is
     resolved by asking Ollama directly: GET {ollama_base_url}/api/ps
     reports size_vram per currently-loaded model - the same field
     agent/hearth_bench.py's residency() already reads - and the sum of
     that is treated as "ours". Only nvidia-smi's total memory used minus
     that sum ("other") ever counts toward the memory threshold. A model
     resident but idle is exactly the state a caller wants to run in, so
     it must read as idle; a model actively generating is genuinely
     contention for compute cycles regardless of who asked for the
     generation, which utilization already catches without needing to
     know who "who" is. If Ollama's own residency cannot be determined
     (unreachable, not running), this falls back to treating the full
     reading as "other" - the conservative direction, since getting
     ownership wrong should widen what looks like contention, not narrow
     it - and the raised memory threshold from fix 1 keeps that fallback
     from tripping on ordinary desktop baseline anyway.

  Full-screen foreground state (Windows only). GetForegroundWindow plus
  GetWindowRect compared against GetSystemMetrics(SM_CXSCREEN/CYSCREEN) is
  a handful of ctypes calls, no subprocess, effectively free to poll. It
  catches borderless/exclusive fullscreen games and fullscreen video
  players reliably. It does NOT catch a windowed video call (a Zoom
  window that is not maximized) - that would need hooking DXGI presentation
  or per-app heuristics this module does not attempt. It also only compares
  against the primary monitor's resolution, so a fullscreen window on a
  differently-sized secondary monitor is a coarser check. These are real
  gaps, not hidden ones.

  Power state. GetSystemPowerStatus on Windows, /sys/class/power_supply on
  Linux. A laptop running on battery is a "not now" regardless of how idle
  the mouse looks - heavy inference will not make the user's afternoon
  better if it also kills their battery during a meeting.

  CPU load. os.getloadavg() on Linux is a genuine, cheap, already-averaged
  signal. Windows has no standard-library equivalent that does not require
  either a blocking sample-over-interval or persisted state between calls;
  rather than build that complexity for a signal this spec already calls
  "weak corroborating, not primary", CPU load is simply unavailable on
  Windows here. Where it is available it never independently declares
  busy; it only downgrades confidence when it disagrees with a signal that
  otherwise says idle.

HYSTERESIS. A naive threshold flaps: the user twitches the mouse, work
pauses, idleness resumes a second later, work resumes, repeat. Flapping is
worse than either steady state, because starting and stopping inference
thrashes VRAM. IdleTracker requires a raw reading to persist before it acts
on it, with different thresholds in each direction:

  idle_after_seconds  = 300  (5 minutes) before declaring idle.
    Long enough that someone pausing mid-thought, or reading a paragraph,
    does not trigger a VRAM-thrashing start. Roughly in line with common
    screensaver/lock defaults, which is already a "they stepped away" bar
    most people have some intuition for.

  active_after_seconds = 15  (15 seconds) before declaring busy again.
    Deliberately much shorter than the idle side. The asymmetry is the
    whole point: it costs little to be slow to *start*, but it costs the
    user's trust to be slow to *stop*. Getting out of the way fast is what
    makes leaving Hearth running overnight feel safe to do again tomorrow.

Where the raw signal already carries a real elapsed duration (Windows input
idle seconds, computed by the OS itself), that duration is used directly
rather than waiting for enough polls to accumulate the same span - a user
who has genuinely been away 10 minutes should not wait an extra 5 for this
module to notice. Signals that are plain booleans (fullscreen, GPU busy,
on battery) have no such built-in duration, so IdleTracker times their
persistence itself, across successive update() calls, using an injectable
clock so this is testable without touching a real wall clock.

DEGRADE TO PERMISSIVE. If no signal is available at all - the normal case
on a headless Linux box with no GPU - the answer is state=unknown,
confidence=low, good_time=True. A wrong "busy" means nothing ever runs,
which is indistinguishable from Hearth being broken; a wrong "idle" merely
means a heavy job ran when a human happened to be present but undetectable.
Those failure modes are not symmetric, so this module is deliberately
asymmetric about them: refuse only on positive evidence, never on absence
of evidence.

Never blocks: every external call is wrapped in a bounded timeout (GPU
query) or is a handful of ctypes calls with no possibility of blocking
(input idleness, fullscreen, power state, os.getloadavg()). No writes, no
network, no side effects. Standard library only, ctypes included.
"""

import ctypes
import dataclasses
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

STATE_IDLE = "idle"
STATE_BUSY = "busy"
STATE_UNKNOWN = "unknown"

GPU_SUBPROCESS_TIMEOUT = 3  # seconds; polled frequently, must never hang the caller.
OLLAMA_PS_TIMEOUT = 2  # seconds; also polled frequently, same bounded-and-cheap contract.

# Same default and env override as agent/hearth_bench.py, agent/hearth_doctor.py,
# etc: local Ollama's fixed default port.
DEFAULT_OLLAMA = os.environ.get("HEARTH_OLLAMA", "http://127.0.0.1:11434")


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class IdlePolicy:
    """Knobs a caller can tune. See module docstring for the reasoning
    behind the default thresholds and their asymmetry."""

    idle_after_seconds: float = 300.0   # persistence required before -> idle
    active_after_seconds: float = 15.0  # persistence required before -> busy
    block_on_battery: bool = True
    gpu_busy_util_percent: float = 15.0  # primary, decisive: real compute happening
    gpu_busy_mem_percent: float = 40.0   # secondary corroboration only; see module docstring
    cpu_busy_frac: float = 0.90  # corroboration only, never decisive alone
    ollama_base_url: str = DEFAULT_OLLAMA  # where to ask what Hearth's own inference stack owns


DEFAULT_POLICY = IdlePolicy()


# ---------------------------------------------------------------------------
# Raw signal collection - each function degrades to (None, False) on any
# failure and never raises. None means "could not determine"; the caller
# must not confuse that with a definite negative.
# ---------------------------------------------------------------------------

def _input_idle_seconds():
    """Seconds since the last keyboard/mouse event, Windows only.

    Uses GetTickCount (32-bit), not GetTickCount64, to match the 32-bit
    dwTime field GetLastInputInfo fills in: both wrap at the same ~49.7 day
    boundary, so the unsigned subtraction below wraps correctly around that
    boundary instead of producing a huge bogus value the way mixing a
    64-bit tick count with a 32-bit dwTime would.
    """
    if platform.system() != "Windows":
        return None, False
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
        user32.GetLastInputInfo.restype = ctypes.c_int
        kernel32.GetTickCount.restype = ctypes.c_uint32

        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(lii)):
            return None, False
        now_ticks = kernel32.GetTickCount()
        idle_ms = ctypes.c_uint32(now_ticks - lii.dwTime).value
        return idle_ms / 1000.0, True
    except Exception:
        return None, False


def _is_lock_or_logon_window(hwnd, user32, kernel32):
    """True if hwnd belongs to the Windows lock screen or credential/logon
    UI (LockApp.exe or LogonUI.exe, the two processes that have hosted
    that UI across supported Windows versions).

    Both of those legitimately cover the entire screen, and both mean the
    *opposite* of what full-screen coverage normally implies here: the
    machine is locked, nobody is looking at it. Counting that as "a
    fullscreen app is in the way" would be exactly backwards - this is
    verified empirically, not theoretically: probing this module against
    a real locked Windows session during development showed
    GetForegroundWindow() returning LockApp.exe's CoreWindow (screen-sized)
    rather than 0, which is what a naive "no foreground window -> not
    fullscreen" assumption would have missed entirely.
    """
    try:
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False
        process_query_limited_information = 0x1000
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid.value)
        if not handle:
            return False
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.c_ulong(260)
            kernel32.QueryFullProcessImageNameW.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong),
            ]
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return False
            name = os.path.basename(buf.value).lower()
            return name in ("lockapp.exe", "logonui.exe")
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _foreground_fullscreen():
    """True if the foreground window covers the whole primary monitor.

    Catches borderless/exclusive fullscreen games and fullscreen video
    players. Does not catch a windowed video call, and only compares
    against the primary monitor - see module docstring for both caveats.
    The lock screen and the credential/logon UI are excluded explicitly
    (see _is_lock_or_logon_window): both are screen-sized but mean the
    user is away, not that something is in the way.
    """
    if platform.system() != "Windows":
        return None, False
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        class _RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long),
            ]

        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetShellWindow.restype = ctypes.c_void_p
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        user32.GetWindowRect.restype = ctypes.c_int
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            # No window currently has focus. Not evidence of a fullscreen
            # app in the way.
            return False, True

        shell_hwnd = user32.GetShellWindow()
        if hwnd == shell_hwnd:
            # The desktop itself always spans the screen; that is not
            # "someone's app is in the way".
            return False, True

        if _is_lock_or_logon_window(hwnd, user32, kernel32):
            return False, True

        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None, False

        screen_w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
        screen_h = user32.GetSystemMetrics(1)  # SM_CYSCREEN
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top
        if screen_w <= 0 or screen_h <= 0 or win_w <= 0 or win_h <= 0:
            return None, False
        return (win_w >= screen_w and win_h >= screen_h), True
    except Exception:
        return None, False


def _on_battery():
    """True if running on battery power, False if on AC or no battery
    present at all, None if it cannot be determined."""
    system = platform.system()
    if system == "Windows":
        try:
            class _SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SYSTEM_POWER_STATUS)]
            kernel32.GetSystemPowerStatus.restype = ctypes.c_int
            status = _SYSTEM_POWER_STATUS()
            if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return None, False
            ac = status.ACLineStatus
            if ac == 1:
                return False, True
            if ac == 0:
                return True, True
            return None, False  # 255 == "unknown" per the Win32 contract
        except Exception:
            return None, False
    if system == "Linux":
        base = "/sys/class/power_supply"
        try:
            entries = os.listdir(base)
        except OSError:
            return None, False
        battery_found = False
        for entry in entries:
            type_path = os.path.join(base, entry, "type")
            try:
                with open(type_path, encoding="utf-8", errors="replace") as f:
                    kind = f.read().strip()
            except OSError:
                continue
            if kind != "Battery":
                continue
            battery_found = True
            status_path = os.path.join(base, entry, "status")
            try:
                with open(status_path, encoding="utf-8", errors="replace") as f:
                    st = f.read().strip().lower()
            except OSError:
                continue
            if st == "discharging":
                return True, True
            if st in ("charging", "full", "not charging"):
                return False, True
        if not battery_found:
            return False, True  # a desktop with no battery is never "on battery"
        return None, False
    return None, False


def _cpu_load_frac():
    """Normalized 1-minute load average (0..~4), Linux only. See module
    docstring for why this is unavailable on Windows without either a
    blocking sample or persisted state this module intentionally avoids."""
    if hasattr(os, "getloadavg"):
        try:
            load1, _, _ = os.getloadavg()
            n = os.cpu_count() or 1
            return min(load1 / n, 4.0), True
        except OSError:
            return None, False
    return None, False


def _run_bounded(cmd, timeout=GPU_SUBPROCESS_TIMEOUT):
    """Exception-safe, timeout-bounded subprocess call. Mirrors the _run()
    helper in hearth_hw.py: any failure whatsoever - missing executable,
    timeout, nonzero exit, undecodable output - becomes None, never an
    exception. This is polled, so it must never be the thing that hangs."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _ollama_resident_vram_bytes(base_url=None):
    """Total bytes of VRAM Ollama itself currently reports resident, summed
    across every model GET {base_url}/api/ps lists. This is "ours": the
    same size_vram field agent/hearth_bench.py's residency() already reads
    off the identical endpoint, so this reuses Ollama's own accounting
    rather than guessing at process names via nvidia-smi.

    Returns (bytes, True) on a clean read, (None, False) on any failure at
    all - unreachable server, non-2xx response, malformed JSON, unexpected
    shape - never raises. A caller must treat False as "ownership unknown",
    not as "Ollama owns nothing": see _gpu_contention for how the two are
    handled differently.
    """
    base_url = (base_url or DEFAULT_OLLAMA or "").rstrip("/")
    if not base_url:
        return None, False
    try:
        req = urllib.request.Request(base_url + "/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=OLLAMA_PS_TIMEOUT) as resp:
            raw = resp.read()
        data = json.loads(raw)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None, False
    if not isinstance(data, dict):
        return None, False
    models = data.get("models")
    if not isinstance(models, list):
        return None, False
    total = 0
    for entry in models:
        if not isinstance(entry, dict):
            continue
        size_vram = entry.get("size_vram")
        if isinstance(size_vram, (int, float)) and size_vram > 0:
            total += size_vram
    return int(total), True


def _gpu_contention(policy):
    """(busy, util_percent, other_mem_percent, available) from nvidia-smi,
    with Ollama's own resident VRAM subtracted out before the memory
    threshold is ever checked. See the module docstring's GPU contention
    section for the full reasoning; summary:

      - busy is driven primarily by utilization (real compute happening,
        regardless of whose request caused it).
      - other_mem_percent is memory used that is NOT accounted for by
        Ollama's own /api/ps residency - i.e. memory some other process is
        holding - and only trips the (deliberately high) memory threshold
        as corroboration, never as the sole reason.
      - a model Hearth's own Ollama has loaded and left resident, but is
        not generating with, must read as idle - that is the state a
        caller wants to run heavy work in, not a reason to refuse.

    Reports the busiest GPU's utilization and the aggregate memory picture
    across every GPU nvidia-smi lists: one contended card is enough reason
    to stay out of the way even if a second card is idle.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None, None, None, False
    out = _run_bounded([
        exe, "--query-gpu=utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return None, None, None, False

    best_util = None
    total_mem_used_bytes = 0.0
    total_mem_capacity_bytes = 0.0
    saw_a_line = False
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            util = float(parts[0])
            mem_used_mib = float(parts[1])
            mem_total_mib = float(parts[2])
        except ValueError:
            continue
        saw_a_line = True
        if best_util is None or util > best_util:
            best_util = util
        total_mem_used_bytes += mem_used_mib * 1024 * 1024
        total_mem_capacity_bytes += mem_total_mib * 1024 * 1024
    if not saw_a_line or best_util is None:
        return None, None, None, False

    own_vram_bytes, own_available = _ollama_resident_vram_bytes(policy.ollama_base_url)
    if own_available and own_vram_bytes is not None:
        other_mem_bytes = max(0.0, total_mem_used_bytes - own_vram_bytes)
    else:
        # Ollama's own residency could not be determined (not running,
        # unreachable, bad response). Falling back to the unsubtracted
        # total is the conservative direction: an unknown ownership split
        # should widen what counts as possible contention, not quietly
        # ignore memory altogether. The raised gpu_busy_mem_percent default
        # (see module docstring, fix 1) keeps this fallback from tripping
        # on an ordinary idle desktop's compositor baseline regardless.
        other_mem_bytes = total_mem_used_bytes

    other_mem_pct = (
        (other_mem_bytes / total_mem_capacity_bytes * 100.0)
        if total_mem_capacity_bytes > 0 else 0.0
    )

    busy = best_util >= policy.gpu_busy_util_percent or other_mem_pct >= policy.gpu_busy_mem_percent
    return busy, best_util, other_mem_pct, True


# ---------------------------------------------------------------------------
# Classification - pure function of a signals dict, no I/O, no clock. Kept
# separate from collection so the self-test can drive it with synthetic
# signals and never touch the real machine.
# ---------------------------------------------------------------------------

def _classify(signals, policy):
    """(state, confidence, reasons) from one instantaneous signals reading.

    Checked in order of how strong and how immediate the evidence is:
    fullscreen and battery are checked first because they are the clearest
    "not now" signals available, then GPU contention, then input idleness
    (which, uniquely, carries a real OS-measured duration rather than being
    a plain instantaneous boolean). If nothing at all is available, this
    returns unknown/low - the permissive default - rather than guessing.
    """
    reasons = []

    if signals.get("fullscreen_foreground") is True:
        reasons.append("a foreground app is filling the screen (game, video, presentation)")
        return STATE_BUSY, "high", reasons

    if policy.block_on_battery and signals.get("on_battery") is True:
        reasons.append("running on battery power")
        return STATE_BUSY, "high", reasons

    if signals.get("gpu_busy") is True:
        util = signals.get("gpu_util_percent") or 0.0
        other_mem = signals.get("gpu_other_mem_percent") or 0.0
        reasons.append(
            "GPU already under contention (util {:.0f}%, other-process mem {:.0f}%)"
            .format(util, other_mem)
        )
        return STATE_BUSY, "medium", reasons

    if signals.get("input_idle_available"):
        idle_s = signals.get("input_idle_seconds") or 0.0
        if idle_s < policy.active_after_seconds:
            reasons.append("keyboard/mouse input {:.0f}s ago".format(idle_s))
            return STATE_BUSY, "high", reasons
        if idle_s >= policy.idle_after_seconds:
            reasons.append("no keyboard/mouse input for {:.0f}s".format(idle_s))
            confidence = "high"
            if signals.get("cpu_signal_available") and signals.get("cpu_load_frac") is not None \
                    and signals["cpu_load_frac"] >= policy.cpu_busy_frac:
                reasons.append(
                    "CPU load is high ({:.0%}) despite no input; kept as idle, "
                    "confidence lowered".format(signals["cpu_load_frac"])
                )
                confidence = "medium"
            return STATE_IDLE, confidence, reasons
        reasons.append(
            "input idle {:.0f}s, between the {:.0f}s active and {:.0f}s idle thresholds"
            .format(idle_s, policy.active_after_seconds, policy.idle_after_seconds)
        )
        return STATE_UNKNOWN, "low", reasons

    reasons.append("no idleness signal available on this platform ({})".format(signals.get("platform")))
    if signals.get("gpu_signal_available") and signals.get("gpu_busy") is False:
        reasons.append("GPU currently idle")
    if signals.get("power_signal_available") and signals.get("on_battery") is False:
        reasons.append("on AC power")
    return STATE_UNKNOWN, "low", reasons


def probe(policy=None):
    """Everything this module can determine about the machine right now,
    as one JSON-safe dict: {state, confidence, reason, signals}.

    This is a single instantaneous reading with no memory of previous
    calls - callers that poll repeatedly should feed these through an
    IdleTracker for hysteresis rather than acting on probe() directly.
    Never raises, never blocks longer than GPU_SUBPROCESS_TIMEOUT.
    """
    policy = policy or DEFAULT_POLICY
    system = platform.system()

    input_idle_seconds, input_available = _input_idle_seconds()
    gpu_busy, gpu_util, gpu_other_mem_pct, gpu_available = _gpu_contention(policy)
    fullscreen, fullscreen_available = _foreground_fullscreen()
    on_battery, power_available = _on_battery()
    cpu_load_frac, cpu_available = _cpu_load_frac()

    signals = {
        "platform": system,
        "input_idle_seconds": input_idle_seconds,
        "input_idle_available": input_available,
        "gpu_busy": gpu_busy,
        "gpu_util_percent": gpu_util,
        "gpu_other_mem_percent": gpu_other_mem_pct,
        "gpu_signal_available": gpu_available,
        "fullscreen_foreground": fullscreen,
        "fullscreen_signal_available": fullscreen_available,
        "on_battery": on_battery,
        "power_signal_available": power_available,
        "cpu_load_frac": cpu_load_frac,
        "cpu_signal_available": cpu_available,
    }

    state, confidence, reasons = _classify(signals, policy)
    return {
        "state": state,
        "confidence": confidence,
        "reason": "; ".join(reasons) if reasons else "no signal available",
        "signals": signals,
    }


def is_good_time(policy=None):
    """One-shot convenience: a single probe, classified, no hysteresis.

    Because there is no history on a single call, this cannot smooth a
    flappy boolean signal the way a polled IdleTracker can - it only
    benefits from real elapsed duration where the OS supplies one (input
    idleness). Callers that poll on an interval should hold their own
    IdleTracker instead, so hysteresis has a history to work with.

    Returns {good_time, state, confidence, reason, signals}. good_time is
    False only when state is definitely busy; both idle and unknown
    proceed, per this module's permissive-by-default contract.
    """
    result = probe(policy=policy)
    return {
        "good_time": result["state"] != STATE_BUSY,
        "state": result["state"],
        "confidence": result["confidence"],
        "reason": result["reason"],
        "signals": result["signals"],
    }


# ---------------------------------------------------------------------------
# IdleTracker - hysteresis across polls
# ---------------------------------------------------------------------------

class IdleTracker:
    """Holds hysteresis state across repeated update() calls.

    A raw classification (from _classify, via probe()) is noisy per-sample
    on its own: a single fullscreen reading, a single GPU blip. This
    tracker requires a raw state to persist for policy.idle_after_seconds
    (going idle) or policy.active_after_seconds (going busy) before it
    changes the *decided* state it reports - that persistence requirement
    is the hysteresis. Until enough evidence accumulates in either
    direction, it holds the previous decision; the very first decision,
    before any evidence at all, is STATE_UNKNOWN, which is permissive
    (good_time True) by this module's degrade-to-permissive contract.

    The clock is injectable so tests can drive persistence deterministically
    without sleeping or touching the real wall clock.
    """

    def __init__(self, policy=None, clock=None, probe_fn=None):
        self.policy = policy or IdlePolicy()
        self._clock = clock or time.monotonic
        self._probe_fn = probe_fn or (lambda: probe(policy=self.policy))
        self._raw_state = None
        self._raw_since = None
        self._decided_state = STATE_UNKNOWN
        self._decided_confidence = "low"
        self._decided_reason = "no observations yet"

    def update(self, raw=None, now=None):
        """Feed one new observation in. raw defaults to a fresh probe();
        now defaults to the tracker's clock. Both are overridable so this
        can be driven entirely synthetically in tests.

        Returns {good_time, state, confidence, reason, raw, raw_streak_seconds}.
        """
        raw = raw if raw is not None else self._probe_fn()
        now = self._clock() if now is None else now
        raw_state = raw["state"]

        if raw_state != self._raw_state:
            self._raw_state = raw_state
            self._raw_since = now
        streak = (now - self._raw_since) if self._raw_since is not None else 0.0

        # Windows input idleness is a real OS-measured duration, not
        # something this tracker accumulated itself across polls - give it
        # immediate credit instead of waiting for enough polls to add up to
        # the same span.
        input_idle_seconds = raw.get("signals", {}).get("input_idle_seconds")
        if raw_state == STATE_IDLE and input_idle_seconds is not None:
            streak = max(streak, input_idle_seconds)

        if raw_state == STATE_IDLE and streak >= self.policy.idle_after_seconds:
            self._decided_state = STATE_IDLE
            self._decided_confidence = raw["confidence"]
            self._decided_reason = "{}; idle for >= {:.0f}s".format(raw["reason"], self.policy.idle_after_seconds)
        elif raw_state == STATE_BUSY and streak >= self.policy.active_after_seconds:
            self._decided_state = STATE_BUSY
            self._decided_confidence = raw["confidence"]
            self._decided_reason = "{}; active for >= {:.0f}s".format(raw["reason"], self.policy.active_after_seconds)
        else:
            # Not enough persistence yet in the new direction: hold the
            # previous decision. This is the debounce.
            if self._decided_state == STATE_UNKNOWN:
                self._decided_reason = (
                    "insufficient persistence yet (raw={}, {:.1f}s observed); "
                    "holding unknown, proceeding by default".format(raw_state, streak)
                )
            else:
                self._decided_reason = (
                    "holding previous decision ({}) - raw={} has been observed for {:.1f}s, "
                    "not yet past its threshold".format(self._decided_state, raw_state, streak)
                )

        return {
            "good_time": self._decided_state != STATE_BUSY,
            "state": self._decided_state,
            "confidence": self._decided_confidence,
            "reason": self._decided_reason,
            "raw": raw,
            "raw_streak_seconds": streak,
        }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _make_raw(state, confidence="high", reason="synthetic", **signal_overrides):
    """Build a synthetic probe()-shaped dict for driving _classify/IdleTracker
    without touching the real machine."""
    signals = {
        "platform": "Test",
        "input_idle_seconds": None,
        "input_idle_available": False,
        "gpu_busy": None,
        "gpu_util_percent": None,
        "gpu_other_mem_percent": None,
        "gpu_signal_available": False,
        "fullscreen_foreground": None,
        "fullscreen_signal_available": False,
        "on_battery": None,
        "power_signal_available": False,
        "cpu_load_frac": None,
        "cpu_signal_available": False,
    }
    signals.update(signal_overrides)
    return {"state": state, "confidence": confidence, "reason": reason, "signals": signals}


def _self_test():
    # -- _classify: fullscreen is decisive and checked first ----------------
    s, c, r = _classify({"fullscreen_foreground": True, "platform": "Windows"}, DEFAULT_POLICY)
    assert s == STATE_BUSY and c == "high", (s, c, r)
    assert "screen" in r[0]

    # -- _classify: battery blocks when policy says so -----------------------
    s, c, r = _classify({"fullscreen_foreground": False, "on_battery": True, "platform": "Windows"}, DEFAULT_POLICY)
    assert s == STATE_BUSY, (s, c, r)
    assert "battery" in r[0]

    no_battery_block = dataclasses.replace(DEFAULT_POLICY, block_on_battery=False)
    s, c, r = _classify(
        {"fullscreen_foreground": False, "on_battery": True, "platform": "Windows",
         "input_idle_available": False, "gpu_signal_available": False, "power_signal_available": True},
        no_battery_block,
    )
    assert s == STATE_UNKNOWN, (s, c, r)  # battery ignored, nothing else to go on

    # -- _classify: GPU contention -------------------------------------------
    s, c, r = _classify(
        {"fullscreen_foreground": False, "on_battery": False, "gpu_busy": True,
         "gpu_util_percent": 40.0, "gpu_other_mem_percent": 55.0, "platform": "Linux"},
        DEFAULT_POLICY,
    )
    assert s == STATE_BUSY and "GPU" in r[0], (s, c, r)

    # -- _classify: input idleness thresholds, all three zones --------------
    busy_signals = {
        "fullscreen_foreground": False, "on_battery": False, "gpu_busy": False,
        "input_idle_available": True, "input_idle_seconds": 3.0, "platform": "Windows",
    }
    s, c, r = _classify(busy_signals, DEFAULT_POLICY)
    assert s == STATE_BUSY and c == "high", (s, c, r)

    dead_zone_signals = dict(busy_signals, input_idle_seconds=120.0)
    s, c, r = _classify(dead_zone_signals, DEFAULT_POLICY)
    assert s == STATE_UNKNOWN, (s, c, r)  # between active_after and idle_after

    idle_signals = dict(busy_signals, input_idle_seconds=600.0)
    s, c, r = _classify(idle_signals, DEFAULT_POLICY)
    assert s == STATE_IDLE and c == "high", (s, c, r)

    # High CPU load corroborates against (lowers confidence of) an idle
    # verdict but does not flip it to busy on its own - it's corroborating,
    # not primary, per the module's contract.
    idle_high_cpu = dict(idle_signals, cpu_signal_available=True, cpu_load_frac=0.95)
    s, c, r = _classify(idle_high_cpu, DEFAULT_POLICY)
    assert s == STATE_IDLE and c == "medium", (s, c, r)

    # -- _classify: nothing available at all -> permissive unknown ----------
    nothing = {
        "fullscreen_foreground": None, "on_battery": None, "gpu_busy": None,
        "input_idle_available": False, "gpu_signal_available": False,
        "power_signal_available": False, "platform": "Linux",
    }
    s, c, r = _classify(nothing, DEFAULT_POLICY)
    assert s == STATE_UNKNOWN and c == "low", (s, c, r)

    # -- _gpu_contention: ownership-aware, regression fixtures for both real -
    # -- bugs a coordinator review found by testing against real machines ---
    # Both _run_bounded (the nvidia-smi call) and _ollama_resident_vram_bytes
    # (the /api/ps call) are swapped for canned fixtures, exactly like
    # hearth_hw.py's self-test does for its Windows WMI/CIM parsers - this
    # never touches a real GPU or a real Ollama server, so it is
    # deterministic on a machine with neither.
    old_run_bounded = globals()["_run_bounded"]
    old_ollama_resident = globals()["_ollama_resident_vram_bytes"]
    try:
        # Regression fixture for finding 1: a genuinely idle Windows
        # desktop - 0% utilization, ~10% VRAM resident from the compositor's
        # framebuffer alone. This is the exact reading a coordinator review
        # captured against a real Windows machine untouched for 52 minutes.
        # No Ollama reachable. Must NOT read as busy.
        def _fixture_idle_desktop(cmd, timeout=GPU_SUBPROCESS_TIMEOUT):
            return "0, 1024, 10240\n"  # util%, mem.used MiB, mem.total MiB -> 10% used
        globals()["_run_bounded"] = _fixture_idle_desktop
        globals()["_ollama_resident_vram_bytes"] = lambda base_url=None: (None, False)
        busy, util, other_pct, avail = _gpu_contention(DEFAULT_POLICY)
        assert avail is True, (busy, util, other_pct, avail)
        assert util == 0.0 and abs(other_pct - 10.0) < 0.01, (util, other_pct)
        assert busy is False, (
            "an idle desktop's resting VRAM baseline must never read as GPU "
            "contention on its own: got busy={} util={} other_mem_pct={}"
            .format(busy, util, other_pct)
        )

        # Regression fixture for finding 2: Ollama holding a model resident
        # (76% of VRAM, matching the coordinator's reported reading) and
        # idle - no generation happening (util 0%). That memory is ours, and
        # it is resting: this must NOT read as busy, or the feature disables
        # itself the first time it is actually used.
        def _fixture_ollama_loaded_idle(cmd, timeout=GPU_SUBPROCESS_TIMEOUT):
            return "0, 18240, 24000\n"  # 76% used, 0% util: resident, not generating
        globals()["_run_bounded"] = _fixture_ollama_loaded_idle
        own_bytes = int(18240 * 1024 * 1024 * 0.995)  # nearly all of the resident memory is ours
        globals()["_ollama_resident_vram_bytes"] = lambda base_url=None: (own_bytes, True)
        busy, util, other_pct, avail = _gpu_contention(DEFAULT_POLICY)
        assert busy is False, (
            "Hearth's own resident-but-idle model must not make the module "
            "declare itself busy - this is the exact deadlock a coordinator "
            "review reported against a real Linux host: got busy={} util={} "
            "other_mem_pct={}".format(busy, util, other_pct)
        )
        assert other_pct < DEFAULT_POLICY.gpu_busy_mem_percent, (other_pct, "ownership subtraction should leave 'other' near zero")

        # Genuine contention, memory-driven: something that is NOT Ollama
        # holding a large chunk of VRAM, Ollama not even running. Must
        # still read as busy - the ownership fix must not blind this module
        # to a real competing process just because Ollama is uninvolved.
        def _fixture_other_app_heavy_vram(cmd, timeout=GPU_SUBPROCESS_TIMEOUT):
            return "5, 14400, 24000\n"  # 60% used, only 5% util
        globals()["_run_bounded"] = _fixture_other_app_heavy_vram
        globals()["_ollama_resident_vram_bytes"] = lambda base_url=None: (None, False)
        busy, util, other_pct, avail = _gpu_contention(DEFAULT_POLICY)
        assert busy is True, (
            "60% of VRAM held by something that is not Ollama, with Ollama "
            "not even running, must read as contention: got busy={} util={} "
            "other_mem_pct={}".format(busy, util, other_pct)
        )

        # Genuine contention, utilization-driven: Ollama actively generating
        # (does not matter for whom - the GPU is computing right now), even
        # though the resident memory itself is entirely ours. Utilization
        # must override ownership attribution here.
        def _fixture_ollama_generating(cmd, timeout=GPU_SUBPROCESS_TIMEOUT):
            return "70, 18240, 24000\n"
        globals()["_run_bounded"] = _fixture_ollama_generating
        globals()["_ollama_resident_vram_bytes"] = lambda base_url=None: (own_bytes, True)
        busy, util, other_pct, avail = _gpu_contention(DEFAULT_POLICY)
        assert busy is True, (
            "sustained high utilization must read as busy even when all of "
            "the resident memory is ours: got busy={} util={} other_mem_pct={}"
            .format(busy, util, other_pct)
        )
    finally:
        globals()["_run_bounded"] = old_run_bounded
        globals()["_ollama_resident_vram_bytes"] = old_ollama_resident

    # -- IdleTracker: fake clock, synthetic raw samples only -----------------
    clock = {"t": 0.0}

    def fake_clock():
        return clock["t"]

    # A single busy sample must not immediately flip a fresh tracker to
    # busy - the very first decision is unknown (permissive) until evidence
    # persists past active_after_seconds.
    tracker = IdleTracker(clock=fake_clock)
    out = tracker.update(raw=_make_raw(STATE_BUSY), now=0.0)
    assert out["state"] == STATE_UNKNOWN, out
    assert out["good_time"] is True, out

    # Busy persisting past active_after_seconds (15s default) flips to busy.
    out = tracker.update(raw=_make_raw(STATE_BUSY), now=20.0)
    assert out["state"] == STATE_BUSY, out
    assert out["good_time"] is False, out

    # A single idle sample right after must not immediately flip back -
    # idle requires idle_after_seconds (300s default) of persistence.
    out = tracker.update(raw=_make_raw(STATE_IDLE), now=21.0)
    assert out["state"] == STATE_BUSY, out  # holding previous decision

    out = tracker.update(raw=_make_raw(STATE_IDLE), now=21.0 + 300.0)
    assert out["state"] == STATE_IDLE, out
    assert out["good_time"] is True, out

    # Windows input-idle duration gets immediate credit: a tracker that has
    # never seen a sample before, fed a single reading claiming 400s of
    # real OS-measured idleness, should go straight to idle without waiting
    # an extra idle_after_seconds on top of that.
    fresh = IdleTracker(clock=fake_clock)
    out = fresh.update(
        raw=_make_raw(STATE_IDLE, input_idle_available=True, input_idle_seconds=400.0),
        now=0.0,
    )
    assert out["state"] == STATE_IDLE, out

    # -- Hysteresis, proven non-vacuous ---------------------------------------
    # Drive the SAME alternating busy/idle/busy/... sequence, one second
    # apart, through two policies: the real default (hysteresis intact) and
    # a "thresholds removed" variant (idle_after_seconds=0,
    # active_after_seconds=0, which makes every single sample immediately
    # decisive - equivalent to deleting the persistence check entirely).
    #
    # With thresholds removed, the decided state MUST flap every step: this
    # is what proves the assertion below ("stable under real policy") is
    # not vacuously true because nothing could ever flap in this harness.
    # If hysteresis in IdleTracker.update() were deleted or broken, the
    # "stable" assertions further down would fail exactly the way the
    # flapping assertions below succeed.
    flappy_policy = IdlePolicy(idle_after_seconds=0.0, active_after_seconds=0.0)
    flappy_tracker = IdleTracker(policy=flappy_policy, clock=fake_clock)
    flap_states = []
    t = 0.0
    for i in range(10):
        raw_state = STATE_BUSY if i % 2 == 0 else STATE_IDLE
        out = flappy_tracker.update(raw=_make_raw(raw_state), now=t)
        flap_states.append(out["state"])
        t += 1.0
    assert flap_states == [STATE_BUSY, STATE_IDLE] * 5, (
        "with thresholds at 0 the tracker should flap on every single sample; "
        "got {}".format(flap_states)
    )
    assert len(set(flap_states)) > 1, "expected genuine flapping with no hysteresis"

    stable_tracker = IdleTracker(policy=DEFAULT_POLICY, clock=fake_clock)
    stable_states = []
    t = 0.0
    for i in range(10):
        raw_state = STATE_BUSY if i % 2 == 0 else STATE_IDLE
        out = stable_tracker.update(raw=_make_raw(raw_state), now=t)
        stable_states.append(out["state"])
        t += 1.0
    # None of these single-second flips ever persist long enough to cross
    # either threshold (15s / 300s), so the decided state must never leave
    # its permissive starting point.
    assert set(stable_states) == {STATE_UNKNOWN}, (
        "hysteresis should have absorbed 1s-apart flapping entirely; got {}".format(stable_states)
    )
    assert all(s == STATE_UNKNOWN for s in stable_states)

    # -- Real machine: shape-only checks, no assertions on the actual value -
    # This must pass on a machine with no GPU, on a headless Linux host, and
    # while a human is actively typing on it - so nothing here may assert a
    # particular state/confidence/signal value, only that probe() and
    # is_good_time() run without raising and return well-shaped, JSON-safe
    # data.
    real = probe()
    assert real["state"] in (STATE_IDLE, STATE_BUSY, STATE_UNKNOWN), real
    assert real["confidence"] in ("high", "medium", "low"), real
    assert isinstance(real["reason"], str) and real["reason"], real
    assert isinstance(real["signals"], dict), real
    encoded = json.dumps(real)
    assert json.loads(encoded) == real

    real_good_time = is_good_time()
    assert isinstance(real_good_time["good_time"], bool), real_good_time
    assert real_good_time["state"] in (STATE_IDLE, STATE_BUSY, STATE_UNKNOWN), real_good_time
    json.dumps(real_good_time)  # must be JSON-safe

    real_tracker = IdleTracker()
    tracked = real_tracker.update()
    assert isinstance(tracked["good_time"], bool), tracked
    assert tracked["state"] in (STATE_IDLE, STATE_BUSY, STATE_UNKNOWN), tracked
    json.dumps(tracked)  # must be JSON-safe, including the nested raw probe

    # -- Platform honesty: on non-Windows, input idleness must say so -------
    if platform.system() != "Windows":
        idle_s, avail = _input_idle_seconds()
        assert idle_s is None and avail is False, (idle_s, avail)
        fs, fs_avail = _foreground_fullscreen()
        assert fs is None and fs_avail is False, (fs, fs_avail)

    print("hearth-idle self-test OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv or len(sys.argv) == 1:
        sys.exit(_self_test())
    print(json.dumps(is_good_time(), indent=2))
