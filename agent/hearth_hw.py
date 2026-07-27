#!/usr/bin/env python3
r"""hearth hardware detection: what this machine actually has, for the model shop.

The shop's hardest problem is honesty: a user with a 6GB laptop GPU should not
be shown a 32B model as if it will work, and a user with a 24GB card should
not be steered to a tiny model just to be safe. This module is the foundation
for that honesty. It detects local hardware and reports it as plain data;
hearth_hw does not decide what to recommend, it only tells the truth about
what is in the box.

THE KV CACHE IS THE PART PEOPLE GET WRONG. Model weights are not the whole
story: context length is usually what breaks the fit. Every token of context
an attention model holds onto costs a fixed number of bytes per token, spread
across every layer and every KV head, and that cost scales linearly with
context length regardless of how big the weights are. A 14B model at a short
context might comfortably fit in 16GB, and the exact same model at 32k
context can need roughly twice the memory of its weights alone once the KV
cache is added in. fits() below is the calculator: required_bytes is always
model weights plus context_tokens * kv_bytes_per_token, never weights alone.
kv_bytes_per_token is a property of the model's architecture (hidden size,
layer count, KV head count, cache precision), so the caller supplies it; this
module does no architecture-specific reasoning, only arithmetic.

Deliberately out of scope: predicting tokens per second. Throughput
prediction from memory bandwidth and parameter count breaks badly on
mixture-of-experts models, where active parameters are a fraction of total
weights. Hearth measures real throughput on the user's machine instead of
guessing at it.

Detection preference order:
  Windows: nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,
           then PowerShell Get-CimInstance Win32_VideoController,
           then wmic path Win32_VideoController get Name,AdapterRAM.
           Win32_VideoController.AdapterRAM is a signed 32-bit field: it
           wraps for any card above ~4GB, so a 24GB card can report a small
           or even negative number. Readings from this path are marked
           "approximate": True and should be treated as a floor, not a truth.
  Linux:   nvidia-smi first, then /sys/class/drm/*/device/mem_info_vram_total
           for AMD, then lspci for a name-only fallback with no VRAM figure.
           System RAM comes from /proc/meminfo.

Standard library only. Every external command is optional: a missing tool,
a timeout, or a nonzero exit degrades to "no data", never a raised exception.
Pure detection: no writes, no network.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys

SUBPROCESS_TIMEOUT = 5  # seconds; a hung driver query must never hang hearth.

VENDOR_NVIDIA = "nvidia"
VENDOR_AMD = "amd"
VENDOR_INTEL = "intel"
VENDOR_UNKNOWN = "unknown"

_UNIT_MULTIPLIERS = {
    "mib": 1024 ** 2,
    "gib": 1024 ** 3,
    "kib": 1024,
    "mb": 1000 ** 2,
    "gb": 1000 ** 3,
    "kb": 1000,
}


def _run(cmd, timeout=SUBPROCESS_TIMEOUT):
    """Run cmd, return stdout text, or None on any failure whatsoever.

    This is the single chokepoint every external tool call goes through, so
    that a missing executable, a timeout, or a nonzero exit all degrade the
    same way: no data, no exception.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _vendor_from_name(name):
    """Best-effort vendor guess from a free-text GPU name string."""
    n = (name or "").lower()
    if any(tok in n for tok in ("nvidia", "geforce", "quadro", "rtx", "gtx", "tesla")):
        return VENDOR_NVIDIA
    if any(tok in n for tok in ("amd", "radeon", "advanced micro")):
        return VENDOR_AMD
    if any(tok in n for tok in ("intel", "iris")):
        return VENDOR_INTEL
    return VENDOR_UNKNOWN


def _parse_nvidia_smi(output):
    """Parse `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader`.

    A line looks like "NVIDIA GeForce RTX 4090, 24564 MiB". Multi-GPU
    machines produce one line per GPU.
    """
    gpus = []
    if not output:
        return gpus
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        name = parts[0]
        m = re.match(r"([\d.]+)\s*(\w+)?", parts[1])
        if not m:
            continue
        value = float(m.group(1))
        unit = (m.group(2) or "MiB").lower()
        vram_bytes = int(value * _UNIT_MULTIPLIERS.get(unit, _UNIT_MULTIPLIERS["mib"]))
        gpus.append({
            "name": name,
            "vram_bytes": vram_bytes,
            "vendor": VENDOR_NVIDIA,
            "approximate": False,
        })
    return gpus


def _gpus_nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"])
    return _parse_nvidia_smi(out)


def _gpus_windows_powershell():
    """Fallback GPU read via PowerShell Get-CimInstance Win32_VideoController.

    AdapterRAM is signed 32-bit in this WMI class, so it wraps above ~4GB and
    can even read negative for a large card. Treat this reading as a floor,
    never a truth, hence approximate=True on every entry from this path.
    """
    ps = shutil.which("powershell") or shutil.which("powershell.exe")
    if not ps:
        return []
    script = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"
    )
    out = _run([ps, "-NoProfile", "-NonInteractive", "-Command", script], timeout=10)
    if not out or not out.strip():
        return []
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    gpus = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("Name") or "Unknown GPU"
        ram = item.get("AdapterRAM")
        vram_bytes = int(ram) if isinstance(ram, (int, float)) and ram > 0 else 0
        gpus.append({
            "name": name,
            "vram_bytes": vram_bytes,
            "vendor": _vendor_from_name(name),
            "approximate": True,
        })
    return gpus


def _gpus_windows_wmic():
    """Last-resort GPU read via the deprecated `wmic` CSV output.

    Same AdapterRAM 32-bit-signed caveat as the PowerShell path applies here.
    """
    exe = shutil.which("wmic")
    if not exe:
        return []
    out = _run([exe, "path", "Win32_VideoController", "get", "Name,AdapterRAM", "/format:csv"])
    if not out:
        return []
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) < 2:
        return []
    header = [h.strip() for h in lines[0].split(",")]
    try:
        name_idx = header.index("Name")
        ram_idx = header.index("AdapterRAM")
    except ValueError:
        return []
    gpus = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) <= max(name_idx, ram_idx):
            continue
        name = parts[name_idx].strip()
        if not name:
            continue
        ram_str = parts[ram_idx].strip()
        vram_bytes = int(ram_str) if ram_str.isdigit() else 0
        gpus.append({
            "name": name,
            "vram_bytes": vram_bytes,
            "vendor": _vendor_from_name(name),
            "approximate": True,
        })
    return gpus


def _gpus_linux_amd_sysfs():
    """AMD (and any DRM-exposed) VRAM totals from /sys/class/drm/*/device/."""
    gpus = []
    base = "/sys/class/drm"
    try:
        entries = os.listdir(base)
    except OSError:
        return gpus
    for entry in sorted(entries):
        if not re.match(r"^card\d+$", entry):
            continue
        vram_path = os.path.join(base, entry, "device", "mem_info_vram_total")
        vendor_path = os.path.join(base, entry, "device", "vendor")
        try:
            with open(vram_path) as f:
                vram_bytes = int(f.read().strip())
        except (OSError, ValueError):
            continue
        vendor = VENDOR_UNKNOWN
        try:
            with open(vendor_path) as f:
                vid = f.read().strip().lower()
            vendor = {"0x1002": VENDOR_AMD, "0x10de": VENDOR_NVIDIA, "0x8086": VENDOR_INTEL}.get(vid, VENDOR_UNKNOWN)
        except OSError:
            pass
        gpus.append({
            "name": entry,
            "vram_bytes": vram_bytes,
            "vendor": vendor,
            "approximate": False,
        })
    return gpus


def _gpus_linux_lspci():
    """Name-only fallback when no VRAM figure is available at all."""
    exe = shutil.which("lspci")
    if not exe:
        return []
    out = _run([exe, "-mm"])
    if not out:
        return []
    gpus = []
    for line in out.splitlines():
        if not any(tag in line for tag in ("VGA compatible controller", "3D controller", "Display controller")):
            continue
        name = line.strip()
        gpus.append({
            "name": name,
            "vram_bytes": 0,
            "vendor": _vendor_from_name(name),
            "approximate": True,
        })
    return gpus


def gpus():
    """Detected GPUs as a list of dicts with keys name, vram_bytes, vendor
    (plus an "approximate" flag: True when the vram_bytes reading is a
    known-unreliable fallback, as documented in the module docstring).

    Empty list when nothing could be detected; this function never raises.
    """
    system = platform.system()
    found = _gpus_nvidia_smi()
    if found:
        return found
    if system == "Windows":
        found = _gpus_windows_powershell()
        if found:
            return found
        return _gpus_windows_wmic()
    if system == "Linux":
        found = _gpus_linux_amd_sysfs()
        if found:
            return found
        return _gpus_linux_lspci()
    return []


def system_ram_bytes():
    """Total physical system RAM in bytes. 0 if it cannot be determined."""
    system = platform.system()
    if system == "Linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb * 1024
        except (OSError, ValueError, IndexError):
            return 0
        return 0
    if system == "Windows":
        try:
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
            if ok:
                return int(stat.ullTotalPhys)
        except Exception:
            return 0
        return 0
    return 0


def cpu_count():
    """Logical CPU count, at least 1."""
    return os.cpu_count() or 1


def probe():
    """Everything this module knows about the machine, as one JSON-safe dict."""
    return {
        "platform": platform.system(),
        "gpus": gpus(),
        "system_ram_bytes": system_ram_bytes(),
        "cpu_count": cpu_count(),
    }


def fits(model_bytes, context_tokens, kv_bytes_per_token, vram_bytes):
    """Will model_bytes of weights, plus the KV cache for context_tokens of
    context at kv_bytes_per_token each, fit in vram_bytes of VRAM?

    This is the "will it run" calculator for the shop. required_bytes is
    always weights plus KV cache, never weights alone: see the module
    docstring for why context length is usually what breaks the fit, not
    the weights.

    Returns a dict with at least fits (bool), required_bytes (int), and
    headroom_bytes (int, negative when it does not fit).
    """
    kv_bytes = context_tokens * kv_bytes_per_token
    required_bytes = model_bytes + kv_bytes
    headroom_bytes = vram_bytes - required_bytes
    return {
        "fits": headroom_bytes >= 0,
        "required_bytes": required_bytes,
        "headroom_bytes": headroom_bytes,
        "model_bytes": model_bytes,
        "kv_bytes": kv_bytes,
        "context_tokens": context_tokens,
    }


def _self_test():
    # -- fits(): exact arithmetic, three concrete scenarios --------------

    # 1. Comfortable fit: an 8GB model at a modest context on a 24GB card.
    r = fits(model_bytes=8_000_000_000, context_tokens=2048,
             kv_bytes_per_token=100_000, vram_bytes=24_000_000_000)
    assert r["kv_bytes"] == 204_800_000, r
    assert r["required_bytes"] == 8_204_800_000, r
    assert r["headroom_bytes"] == 15_795_200_000, r
    assert r["fits"] is True, r

    # 2. Fits only at short context: same model+card, long context breaks it.
    #    14GB weights, 500KB/token KV, 20GB card.
    short = fits(model_bytes=14_000_000_000, context_tokens=4096,
                 kv_bytes_per_token=500_000, vram_bytes=20_000_000_000)
    assert short["kv_bytes"] == 2_048_000_000, short
    assert short["required_bytes"] == 16_048_000_000, short
    assert short["headroom_bytes"] == 3_952_000_000, short
    assert short["fits"] is True, short

    long_ctx = fits(model_bytes=14_000_000_000, context_tokens=16384,
                     kv_bytes_per_token=500_000, vram_bytes=20_000_000_000)
    assert long_ctx["kv_bytes"] == 8_192_000_000, long_ctx
    assert long_ctx["required_bytes"] == 22_192_000_000, long_ctx
    assert long_ctx["headroom_bytes"] == -2_192_000_000, long_ctx
    assert long_ctx["fits"] is False, long_ctx

    # 3. Cannot fit at all: weights alone exceed VRAM, context is irrelevant.
    hopeless = fits(model_bytes=40_000_000_000, context_tokens=0,
                     kv_bytes_per_token=0, vram_bytes=24_000_000_000)
    assert hopeless["kv_bytes"] == 0, hopeless
    assert hopeless["required_bytes"] == 40_000_000_000, hopeless
    assert hopeless["headroom_bytes"] == -16_000_000_000, hopeless
    assert hopeless["fits"] is False, hopeless

    # 4. Exact boundary: headroom of precisely 0 counts as fitting.
    boundary = fits(model_bytes=10_000_000_000, context_tokens=0,
                     kv_bytes_per_token=0, vram_bytes=10_000_000_000)
    assert boundary["headroom_bytes"] == 0, boundary
    assert boundary["fits"] is True, boundary

    # -- vendor detection --------------------------------------------------
    assert _vendor_from_name("NVIDIA GeForce RTX 4090") == VENDOR_NVIDIA
    assert _vendor_from_name("AMD Radeon RX 7900 XTX") == VENDOR_AMD
    assert _vendor_from_name("Intel(R) Iris(R) Xe Graphics") == VENDOR_INTEL
    assert _vendor_from_name("Some Mystery Card") == VENDOR_UNKNOWN
    assert _vendor_from_name("") == VENDOR_UNKNOWN
    assert _vendor_from_name(None) == VENDOR_UNKNOWN

    # -- nvidia-smi CSV parsing ---------------------------------------------
    parsed = _parse_nvidia_smi("NVIDIA GeForce RTX 4090, 24564 MiB\n")
    assert len(parsed) == 1, parsed
    assert parsed[0]["name"] == "NVIDIA GeForce RTX 4090", parsed
    assert parsed[0]["vram_bytes"] == 24564 * 1024 * 1024, parsed
    assert parsed[0]["vendor"] == VENDOR_NVIDIA, parsed
    assert parsed[0]["approximate"] is False, parsed

    multi = _parse_nvidia_smi("GPU A, 8192 MiB\nGPU B, 16384 MiB\n")
    assert len(multi) == 2, multi
    assert multi[1]["vram_bytes"] == 16384 * 1024 * 1024, multi

    assert _parse_nvidia_smi("") == []
    assert _parse_nvidia_smi(None) == []
    assert _parse_nvidia_smi("garbage line with no comma\n") == []

    gib_case = _parse_nvidia_smi("Some GPU, 24 GiB\n")
    assert gib_case[0]["vram_bytes"] == 24 * 1024 ** 3, gib_case

    # -- _run: missing executables never raise ------------------------------
    assert _run(["this-executable-does-not-exist-anywhere-12345"]) is None
    assert _run(["python", "-c", "import sys; sys.exit(1)"]) is None

    # -- gpus(): shape holds regardless of what hardware is present ---------
    detected = gpus()
    assert isinstance(detected, list)
    for g in detected:
        assert set(("name", "vram_bytes", "vendor")).issubset(g.keys()), g
        assert isinstance(g["name"], str) and g["name"], g
        assert isinstance(g["vram_bytes"], int), g
        assert g["vram_bytes"] >= 0, g
        assert g["vendor"] in (VENDOR_NVIDIA, VENDOR_AMD, VENDOR_INTEL, VENDOR_UNKNOWN), g

    # -- system_ram_bytes(): a real machine reports a plausible positive value
    ram = system_ram_bytes()
    assert isinstance(ram, int)
    assert ram >= 0
    if platform.system() in ("Windows", "Linux"):
        # Any machine capable of running this self-test has at least 512MB.
        assert ram > 512 * 1024 * 1024, ram

    # -- cpu_count(): always at least 1 -------------------------------------
    n = cpu_count()
    assert isinstance(n, int)
    assert n >= 1

    # -- probe(): the JSON-safe combined view --------------------------------
    p = probe()
    assert set(("platform", "gpus", "system_ram_bytes", "cpu_count")).issubset(p.keys()), p
    assert p["platform"] == platform.system()
    assert isinstance(p["gpus"], list)
    assert isinstance(p["system_ram_bytes"], int)
    assert isinstance(p["cpu_count"], int)
    # Must be trivially JSON-serialisable, since the shop ships it over the wire.
    encoded = json.dumps(p)
    assert json.loads(encoded) == p

    # -- Windows WMI/CIM parsers: exercised with canned fixtures so the ------
    # -- self-test does not depend on this host's specific hardware ---------
    # Simulate a large card the 32-bit-signed AdapterRAM field would wrap on.
    old_run = globals()["_run"]
    try:
        def _fake_ps(cmd, timeout=SUBPROCESS_TIMEOUT):
            if cmd and "Win32_VideoController" in " ".join(cmd) and "ConvertTo-Json" in " ".join(cmd):
                return json.dumps([{"Name": "NVIDIA GeForce RTX 4090", "AdapterRAM": -2147483648}])
            return None
        globals()["_run"] = _fake_ps
        if shutil.which("powershell") or shutil.which("powershell.exe"):
            fake_result = _gpus_windows_powershell()
            assert len(fake_result) == 1, fake_result
            # A negative/wrapped AdapterRAM must clamp to 0, never go negative.
            assert fake_result[0]["vram_bytes"] == 0, fake_result
            assert fake_result[0]["approximate"] is True, fake_result
            assert fake_result[0]["vendor"] == VENDOR_NVIDIA, fake_result

        def _fake_ps_good(cmd, timeout=SUBPROCESS_TIMEOUT):
            return json.dumps([{"Name": "AMD Radeon RX 6600", "AdapterRAM": 8589934592}])
        globals()["_run"] = _fake_ps_good
        if shutil.which("powershell") or shutil.which("powershell.exe"):
            good_result = _gpus_windows_powershell()
            assert good_result[0]["vram_bytes"] == 8589934592, good_result
            assert good_result[0]["vendor"] == VENDOR_AMD, good_result

        def _fake_none(cmd, timeout=SUBPROCESS_TIMEOUT):
            return None
        globals()["_run"] = _fake_none
        assert _gpus_windows_powershell() == []
        assert _gpus_windows_wmic() == []
    finally:
        globals()["_run"] = old_run

    # -- wmic CSV parser fixture ---------------------------------------------
    wmic_csv = "Node,AdapterRAM,Name\r\nHOST,4294967296,NVIDIA GeForce RTX 3080\r\n"
    old_run2 = globals()["_run"]
    try:
        def _fake_wmic(cmd, timeout=SUBPROCESS_TIMEOUT):
            return wmic_csv
        globals()["_run"] = _fake_wmic
        if shutil.which("wmic"):
            wr = _gpus_windows_wmic()
            assert wr[0]["name"] == "NVIDIA GeForce RTX 3080", wr
            assert wr[0]["vram_bytes"] == 4294967296, wr
    finally:
        globals()["_run"] = old_run2

    print("hearth-hw self-test OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv or len(sys.argv) == 1:
        sys.exit(_self_test())
    print(json.dumps(probe(), indent=2))
