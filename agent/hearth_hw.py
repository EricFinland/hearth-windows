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
           Win32_VideoController.AdapterRAM is a 32-bit field: it tops out
           just under 4GB (a 16GB RTX 5080 reads 4293918720) and on some
           drivers wraps to a small or even negative number. So for each
           adapter the WMI paths find, the display driver's own 64-bit
           figure is read from the registry instead
           (HardwareInformation.qwMemorySize under the adapter's driver
           key, see _registry_vram). That figure is what Windows' own
           Task Manager and dxdiag show, and it is exact for AMD, Intel
           and NVIDIA alike. Only when it cannot be read does the entry
           fall back to AdapterRAM and get marked "approximate": True,
           to be treated as a floor, not a truth.
  Linux:   nvidia-smi first, then /sys/class/drm/*/device/mem_info_vram_total
           for AMD, then lspci for a name-only fallback with no VRAM figure.
           System RAM comes from /proc/meminfo.

NVIDIA IS NOT THE COMMON CASE. nvidia-smi answers first because it is the
only reading here that is exact, but most laptops have no NVIDIA card at
all: they have AMD or Intel graphics integrated into the CPU package, and
Vulkan is the engine that covers those. Everything below the nvidia-smi
path therefore has to work as well as that path does, and three facts get
read rather than guessed:

  vendor       Who makes the silicon. Read from the PCI vendor ID in
               Win32_VideoController.PNPDeviceID first (PCI\VEN_1002 is
               AMD, VEN_10DE NVIDIA, VEN_8086 Intel), then from
               AdapterCompatibility, then from the marketing name. The
               name is the weakest of the three and comes last: it is
               free text chosen by an OEM, and "AMD Radeon(TM) 880M
               Graphics" and "Intel(R) Arc(TM) Graphics" have nothing in
               common but the word Graphics.

  virtual      Whether this "GPU" is a real one. Windows lists screen
               capture and remote desktop shims in exactly the same class
               as real hardware: Parsec, RDP, Citrix, DisplayLink,
               VMware, IddSampleDriver. They have no compute capability
               whatsoever, and one of them sorting ahead of a real GPU is
               enough to make a machine look like it has no GPU. gpus()
               excludes them; display_adapters() still shows them, and
               probe() names them, so their presence is visible rather
               than silently dropped.

  integrated   Whether the memory figure is dedicated VRAM or a slice
               carved out of system RAM. An integrated GPU reports a
               small dedicated carve-out (512MB is typical) and then
               shares the rest with the CPU. That number is real, but it
               is not what "VRAM" means on a discrete card, and a caller
               that grades a model fit against it must know which kind it
               has. Reported as True, False, or None when this module
               genuinely cannot tell, never guessed into a confident
               answer.

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

try:
    import winreg  # Windows only; the registry read below is skipped elsewhere.
except ImportError:  # pragma: no cover - every non-Windows platform
    winreg = None

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
    same way: no data, no exception. encoding="utf-8", errors="replace" is
    deliberate: without it, subprocess.run(text=True) decodes stdout with
    the platform's default locale encoding, and malformed bytes or an
    unusual locale raise UnicodeDecodeError - a ValueError subclass, not
    caught by the (FileNotFoundError, OSError, subprocess.SubprocessError)
    handler below, so it would otherwise escape this chokepoint entirely and
    crash probe(). errors="replace" makes that failure mode impossible: a
    malformed byte becomes U+FFFD instead of an exception.
    """
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


#: PCI vendor IDs, as they appear in a Windows PNPDeviceID
#: ("PCI\VEN_1002&DEV_150E&..."), a Linux sysfs `vendor` file ("0x1002"),
#: or an lspci -nn tag. This is the only vendor signal that is a fact
#: rather than a reading of marketing text, so it is consulted first
#: wherever it exists. The second entry under each vendor is the ID used
#: by some of that vendor's older or secondary display devices.
_PCI_VENDOR_IDS = {
    "10de": VENDOR_NVIDIA, "12d2": VENDOR_NVIDIA,
    "1002": VENDOR_AMD, "1022": VENDOR_AMD,
    "8086": VENDOR_INTEL, "8087": VENDOR_INTEL, "163c": VENDOR_INTEL,
}

_PNP_VEN_RE = re.compile(r"ven_([0-9a-f]{4})", re.I)

#: Vendor from free text, in the order they are tried. Word boundaries on
#: the short and ambiguous tokens (rtx, gtx, nvs, ati, arc, hd graphics):
#: as bare substrings, "arc" matches "Architecture", "ati" matches
#: "Creative" and "Integrated", and one bad vendor call sends a machine to
#: the wrong engine or to no engine at all. The distinctive words (nvidia,
#: geforce, radeon, intel, iris) do not need boundaries and do not get
#: them, because OEMs punctuate them unpredictably: "Intel(R)", "NVIDIA,",
#: "AMD Radeon(TM)".
_VENDOR_NAME_PATTERNS = (
    (VENDOR_NVIDIA, re.compile(
        r"nvidia|geforce|quadro|tesla|titan|\brtx\b|\bgtx\b|\bnvs\b", re.I)),
    (VENDOR_AMD, re.compile(
        r"\bamd\b|radeon|advanced micro|firepro|instinct|\bati\b", re.I)),
    (VENDOR_INTEL, re.compile(
        r"intel|\biris\b|uhd graphics|\bhd graphics\b|\barc\b|\bxe graphics\b", re.I)),
)

#: Names that mean "this is not a GPU". Windows enumerates remote desktop,
#: screen capture and virtual monitor drivers in Win32_VideoController
#: alongside real hardware, and on this machine the Parsec one sorts FIRST,
#: which is how a Radeon 880M came to be reported as an unknown GPU. None
#: of these can run a single inference operation.
#:
#: "basic display" and "basic render" are here for a different reason: they
#: are real hardware running Microsoft's fallback driver because the vendor
#: driver is missing or failed. There is no Vulkan or CUDA runtime behind
#: them either, so fetching a GPU engine for one would install a build that
#: cannot load.
_VIRTUAL_ADAPTER_RE = re.compile(
    r"virtual|remote display|remote desktop|\brdp\b|indirect display|"
    r"displaylink|usb (?:display|graphics)|mirror driver|"
    r"basic display|basic render|hyper-?v video|vmware|virtualbox|"
    r"parallels|\bqxl\b|\bspice\b|citrix|teradici|\bvnc\b|nomachine|"
    r"\bidd(?:\w*driver)?\b|meta virtual|\bdummy\b", re.I)

#: Names that mean "the memory figure beside this is a slice of system RAM".
#:
#: AMD's integrated parts are named on a scheme that is unambiguous once
#: you look at it: three digits and an M (610M, 660M, 680M, 780M, 880M,
#: 890M) for the current APU graphics, a bare "Radeon Graphics" or
#: "Radeon(TM) Graphics" for the desktop APUs, "Vega 3" through "Vega 11"
#: for the Ryzen 2000-5000 APUs, "Radeon R2".."R7" for the older ones. The
#: discrete mobile parts use FOUR digits and an M (RX 5500M, RX 6800M), so
#: the three-digit rule does not touch them.
_AMD_INTEGRATED_RE = re.compile(
    r"\b\d{3}m\b"
    r"|radeon\s*(?:\(?(?:tm|r)\)?\s*)?graphics\b"
    r"|\bvega\s*(?:[3-9]|1[01])\b"
    r"|\bradeon\s*r[2-7]\b"
    r"|\bapu\b", re.I)

#: Intel's discrete line is Arc with a model number (A380, A770, B580).
#: Arc WITHOUT a model number is the integrated Arc in Meteor Lake and
#: Lunar Lake, and everything else Intel sells for a desktop or laptop
#: (UHD, HD, Iris, Iris Xe) is integrated.
_INTEL_ARC_RE = re.compile(r"\barc\b", re.I)
_INTEL_ARC_MODEL_RE = re.compile(r"\b[ab]\d{3}\b", re.I)

#: A dedicated-memory figure at or below this means integrated, for an AMD
#: or Intel part whose NAME did not already say so. No discrete card of the
#: last decade ships with a gibibyte or less of dedicated memory, and the
#: integrated parts carve out 512MB by default. Deliberately below the 2GB
#: line, where genuinely small discrete cards (an RX 550 2GB, a GT 1030
#: 2GB) do still exist.
INTEGRATED_VRAM_CEILING_BYTES = 1024 ** 3


def _vendor_from_name(name):
    """Best-effort vendor guess from a free-text GPU name string.

    The weakest of the three vendor signals this module has, and the last
    one tried: see _classify_adapter. Also used on
    Win32_VideoController.AdapterCompatibility, which is the same kind of
    free text ("Advanced Micro Devices, Inc.", "Intel Corporation") from a
    more disciplined source.
    """
    n = name or ""
    for vendor, pattern in _VENDOR_NAME_PATTERNS:
        if pattern.search(n):
            return vendor
    return VENDOR_UNKNOWN


def _vendor_from_pci_id(text):
    """Vendor from anything carrying a PCI vendor ID, or VENDOR_UNKNOWN.

    Accepts a Windows PNPDeviceID ("PCI\\VEN_1002&DEV_150E&..."), a bare
    four-hex-digit ID, or "0x1002" as Linux sysfs spells it.
    """
    t = (text or "").strip().lower()
    if not t:
        return VENDOR_UNKNOWN
    m = _PNP_VEN_RE.search(t)
    if m:
        return _PCI_VENDOR_IDS.get(m.group(1).lower(), VENDOR_UNKNOWN)
    if t.startswith("0x"):
        t = t[2:]
    if re.fullmatch(r"[0-9a-f]{4}", t):
        return _PCI_VENDOR_IDS.get(t, VENDOR_UNKNOWN)
    return VENDOR_UNKNOWN


def _is_virtual_adapter(name, pnp_id, vendor):
    """Is this display adapter a shim rather than a GPU?

    Two independent signals, either of which is enough:

      1. The name is one of the known virtual, remote or fallback drivers.
         Cheap, works on every platform, and covers the case seen here
         ("Parsec Virtual Display Adapter").

      2. On Windows, the device is not on the PCI bus AND no vendor could
         be established for it. Every real GPU on a PC, integrated ones
         included, enumerates as PCI\\VEN_xxxx; the shims enumerate under
         ROOT\\, SWD\\ or USB\\. The "and no vendor" half is what keeps
         this from becoming a rule that a real GPU could fall foul of: an
         adapter Hearth CAN identify as AMD, Intel or NVIDIA is treated as
         real however it is attached, so a non-PCI GPU on some future or
         unusual platform is not excluded by this rule alone.
    """
    if _VIRTUAL_ADAPTER_RE.search(name or ""):
        return True
    pnp = (pnp_id or "").strip().upper()
    if pnp and not pnp.startswith("PCI\\") and vendor == VENDOR_UNKNOWN:
        return True
    return False


def _is_integrated(name, vendor, vram_bytes):
    """True, False, or None when this module cannot tell.

    None is a real answer and is never rounded to False: a caller grading
    a model fit needs to know the difference between "this is dedicated
    VRAM" and "nobody checked", and reporting a shared-memory figure as
    dedicated VRAM would have the shop recommend a model that thrashes.
    """
    n = name or ""
    if vendor == VENDOR_NVIDIA:
        # NVIDIA ships no integrated PC graphics. Every GeForce, Quadro
        # and Tesla part carries its own memory.
        return False
    if vendor == VENDOR_INTEL:
        if _INTEL_ARC_RE.search(n) and _INTEL_ARC_MODEL_RE.search(n):
            return False
        return True
    if vendor == VENDOR_AMD:
        if _AMD_INTEGRATED_RE.search(n):
            return True
        vram = vram_bytes or 0
        if 0 < vram <= INTEGRATED_VRAM_CEILING_BYTES:
            return True
        if vram > INTEGRATED_VRAM_CEILING_BYTES:
            return False
        return None
    return None


def _classify_adapter(name, vram_bytes, pnp_id=None, compatibility=None,
                      vendor=None):
    """One display adapter as the dict every detection path here returns.

    The vendor decision lives in exactly one place, and it reads the three
    signals strongest-first: the PCI vendor ID, then AdapterCompatibility
    (the driver's own statement of who wrote it), then the marketing name.
    A caller that already KNOWS the vendor beyond doubt (nvidia-smi
    answered, or a sysfs vendor file was read) passes it in and the guesses
    are skipped.
    """
    if vendor is None:
        vendor = _vendor_from_pci_id(pnp_id)
        if vendor == VENDOR_UNKNOWN:
            vendor = _vendor_from_name(compatibility)
        if vendor == VENDOR_UNKNOWN:
            vendor = _vendor_from_name(name)
    return {
        "name": name,
        "vram_bytes": int(vram_bytes or 0),
        "vendor": vendor,
        "virtual": _is_virtual_adapter(name, pnp_id, vendor),
        "integrated": _is_integrated(name, vendor, vram_bytes),
    }


def _parse_nvidia_smi(output):
    """Parse `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader`.

    A line looks like "NVIDIA GeForce RTX 4090, 24564 MiB". Multi-GPU
    machines produce one line per GPU. Split from the right on the last
    comma, not the first: the memory field is always the final column and
    never itself contains a comma, but a GPU's name occasionally does (some
    OEM/workstation card names embed one), and splitting from the left would
    silently truncate or drop such a card.
    """
    gpus = []
    if not output:
        return gpus
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.rsplit(",", 1)]
        if len(parts) < 2:
            continue
        name = parts[0]
        m = re.match(r"([\d.]+)\s*(\w+)?", parts[1])
        if not m:
            continue
        try:
            value = float(m.group(1))
        except ValueError:
            # The regex admits digit-and-dot strings that are not valid
            # floats, e.g. "1.2.3" (a malformed or unexpected driver
            # string). float() raises ValueError on those, and this
            # function's caller chain (_gpus_nvidia_smi -> gpus() ->
            # probe()) promises never to raise, so skip the line instead
            # of letting a bad reading from one GPU take down the whole
            # probe - the same "degrade, don't crash" contract every other
            # parse failure in this function already follows.
            continue
        unit = (m.group(2) or "MiB").lower()
        vram_bytes = int(value * _UNIT_MULTIPLIERS.get(unit, _UNIT_MULTIPLIERS["mib"]))
        entry = _classify_adapter(name, vram_bytes, vendor=VENDOR_NVIDIA)
        entry["approximate"] = False
        # nvidia-smi enumerates CUDA-capable devices, not display adapters,
        # so anything it reports is a real GPU whatever it is called. The
        # name-based virtual check must not reach these: a vGPU slice is
        # commonly named "NVIDIA ... Virtual ...", and dropping it would
        # leave a machine that nvidia-smi just answered for with no GPU.
        entry["virtual"] = False
        gpus.append(entry)
    return gpus


def _gpus_nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"])
    return _parse_nvidia_smi(out)


# --------------------------------------------------------------------------
# The display driver's own memory figure, from the registry (Windows only)
# --------------------------------------------------------------------------

#: The display adapter device class. Every display driver ever installed on
#: the machine has a numbered subkey under it (0000, 0001, ...), INCLUDING
#: drivers for cards that have since been removed or for an older revision
#: of the same chip. That is why an adapter is matched to its key through
#: its own device instance first, and by hardware ID only as a fallback.
_DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"
_DISPLAY_CLASS_KEY = "SYSTEM\\CurrentControlSet\\Control\\Class\\" + _DISPLAY_CLASS_GUID
_ENUM_KEY = "SYSTEM\\CurrentControlSet\\Enum"

#: Registry value types, as the Windows API numbers them. Spelled out here
#: rather than read from winreg so the decoding below, and its self-test,
#: work on a machine that has no winreg module at all.
_REG_BINARY = 3
_REG_DWORD = 4
_REG_QWORD = 11

_SUBKEY_RE = re.compile(r"^\d{4}$")


def _reg_value(path, name):
    """One HKEY_LOCAL_MACHINE value as (data, type).

    Raises OSError for anything that cannot be read, PermissionError
    included (the class key's "Properties" subkey refuses a standard
    user). Deliberately thin: this and _reg_subkeys are the only two
    functions here that touch the real registry, so the self-test swaps
    them for a dictionary and exercises everything above them on any
    platform.
    """
    if winreg is None:
        raise OSError("no registry on this platform")
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
        return winreg.QueryValueEx(key, name)


def _reg_subkeys(path):
    """Names of the subkeys of one HKEY_LOCAL_MACHINE key. Raises OSError."""
    if winreg is None:
        raise OSError("no registry on this platform")
    names = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
        index = 0
        while True:
            try:
                names.append(winreg.EnumKey(key, index))
            except OSError:  # ERROR_NO_MORE_ITEMS ends the enumeration
                return names
            index += 1


def _decode_memory_size(found):
    """(bytes, is_64_bit) from a (data, type) registry pair; (0, False) if
    the value is missing or is not a shape a memory size comes in.

    is_64_bit is the whole point: a REG_QWORD, or an 8-byte REG_BINARY as
    some older AMD drivers write HardwareInformation.MemorySize, cannot have
    wrapped. A DWORD, or a 4-byte binary, is the same 32-bit ceiling
    AdapterRAM has and earns no more trust than AdapterRAM does.
    """
    if not found:
        return 0, False
    data, kind = found
    if kind == _REG_QWORD and isinstance(data, int):
        return max(0, data), True
    if kind == _REG_DWORD and isinstance(data, int):
        return data & 0xFFFFFFFF, False
    if kind == _REG_BINARY and isinstance(data, (bytes, bytearray)) and len(data) in (4, 8):
        return int.from_bytes(bytes(data), "little"), len(data) == 8
    return 0, False


def _driver_key_memory(subkey):
    """(bytes, is_64_bit) for one display class subkey such as "0005".

    qwMemorySize first: it is the 64-bit value every WDDM 2.x driver
    writes, and the one Task Manager reads. MemorySize is the older name,
    which a driver may write as a DWORD (32-bit, so no better than
    AdapterRAM) or as a little-endian binary blob of four or eight bytes.
    """
    path = _DISPLAY_CLASS_KEY + "\\" + subkey
    for name in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
        try:
            found = _reg_value(path, name)
        except OSError:
            continue
        size, wide = _decode_memory_size(found)
        if size > 0:
            return size, wide
    return 0, False


def _hardware_id_parts(text):
    """("PCI", {"VEN": "1002", "DEV": "13C0", ...}) from a device instance ID
    or a hardware ID, upper-cased; (None, {}) when it is not that shape.

    A device instance ID carries every component ("PCI\\VEN_1002&DEV_13C0&
    SUBSYS_7E571462&REV_C2\\4&3055F162&0&0041"); a driver's MatchingDeviceId
    is whichever hardware ID its INF matched on, which can be any subset
    ("PCI\\VEN_1002&DEV_13C0&REV_C2"), so the two are compared component by
    component and never as strings or prefixes.
    """
    pieces = (text or "").strip().upper().split("\\")
    if len(pieces) < 2 or not pieces[0]:
        return None, {}
    parts = {}
    for token in pieces[1].split("&"):
        key, sep, value = token.partition("_")
        if sep and key and value:
            parts[key] = value
    return pieces[0], parts


def _hardware_id_matches(matching_id, pnp_id):
    """Does a driver key's MatchingDeviceId describe this device instance?

    Every component the hardware ID names must agree with the instance, and
    it must name at least the vendor and the device. A class-code match
    ("PCI\\CC_0300", which the Microsoft Basic Display driver uses) names
    no device at all and so matches nothing here; the CC component never
    appears in an instance ID anyway.
    """
    bus, want = _hardware_id_parts(matching_id)
    have_bus, have = _hardware_id_parts(pnp_id)
    if not bus or bus != have_bus or "VEN" not in want or "DEV" not in want:
        return False
    return all(have.get(key) == value for key, value in want.items())


def _registry_vram(pnp_id, name):
    """The display driver's memory figure for one adapter, as
    (bytes, is_64_bit), or (0, False) when it cannot be read.

    Three ways in, strongest first:

      1. The device instance itself. HKLM\\...\\Enum\\<PNPDeviceID> has a
         Driver value naming its exact class subkey ("{4d36e968-...}\\0005").
         This is the only one that cannot pick the wrong key, and it is
         readable by a standard user.
      2. The hardware ID. Every class subkey whose MatchingDeviceId agrees
         with the instance ID, component by component.
      3. The name, only when no subkey matched by hardware ID: DriverDesc
         is the same marketing string Win32_VideoController reports.

    Routes 2 and 3 can find more than one key, because removed cards and
    old driver installs leave theirs behind. Identical figures are fine
    (two of the same card, or a reinstalled driver); different figures mean
    this module cannot tell which key belongs to the card in the machine, so
    it reports nothing rather than pick one. Never raises.
    """
    if pnp_id:
        try:
            driver, _kind = _reg_value(_ENUM_KEY + "\\" + pnp_id.strip(), "Driver")
        except (OSError, TypeError, ValueError):
            driver = None
        if isinstance(driver, str):
            guid, _, subkey = driver.strip().rpartition("\\")
            if guid.lower() == _DISPLAY_CLASS_GUID and _SUBKEY_RE.match(subkey):
                size, wide = _driver_key_memory(subkey)
                if size > 0:
                    return size, wide

    try:
        subkeys = [s for s in _reg_subkeys(_DISPLAY_CLASS_KEY) if _SUBKEY_RE.match(s)]
    except OSError:
        return 0, False
    by_id, by_name = [], []
    wanted_name = (name or "").strip().casefold()
    for subkey in subkeys:
        path = _DISPLAY_CLASS_KEY + "\\" + subkey
        try:
            matching = _reg_value(path, "MatchingDeviceId")[0]
        except (OSError, TypeError, ValueError, IndexError):
            matching = None
        try:
            desc = _reg_value(path, "DriverDesc")[0]
        except (OSError, TypeError, ValueError, IndexError):
            desc = None
        if pnp_id and isinstance(matching, str) and _hardware_id_matches(matching, pnp_id):
            by_id.append(subkey)
        elif wanted_name and isinstance(desc, str) and desc.strip().casefold() == wanted_name:
            by_name.append(subkey)
    readings = {_driver_key_memory(s) for s in (by_id or by_name)}
    readings.discard((0, False))
    if len(readings) == 1:
        return readings.pop()
    return 0, False


def _windows_vram(adapter_ram, pnp_id, name):
    """(vram_bytes, approximate) for one Win32_VideoController row.

    The registry's 64-bit figure wins outright and is the only thing that
    makes a reading exact. A 32-bit registry figure is used only where
    AdapterRAM gave nothing at all, and stays approximate, since it carries
    the same ceiling AdapterRAM does.
    """
    reg_bytes, wide = _registry_vram(pnp_id, name)
    if wide and reg_bytes > 0:
        return reg_bytes, False
    if not adapter_ram and reg_bytes > 0:
        return reg_bytes, True
    return adapter_ram, True


def _gpus_windows_powershell():
    """The Windows GPU read: PowerShell Get-CimInstance Win32_VideoController.

    This is the path that answers on every Windows machine without an
    NVIDIA card, which is most of them, so it asks for everything the class
    knows that bears on the three questions in the module docstring, not
    just a name and a size:

      PNPDeviceID           the PCI vendor ID, and whether this is a PCI
                            device at all.
      AdapterCompatibility  the driver's own statement of who wrote it
                            ("Advanced Micro Devices, Inc.").

    AdapterRAM is 32-bit in this WMI class, so it tops out near 4GB and can
    even read negative for a large card. Each row's figure is therefore
    replaced with the driver's 64-bit one from the registry wherever that
    can be read (_windows_vram), and approximate is True only on the rows
    where it could not, which are to be treated as a floor, never a truth.
    """
    ps = shutil.which("powershell") or shutil.which("powershell.exe")
    if not ps:
        return []
    script = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,PNPDeviceID,AdapterCompatibility | "
        "ConvertTo-Json -Compress"
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
        pnp = item.get("PNPDeviceID")
        pnp = pnp if isinstance(pnp, str) else None
        vram_bytes = int(ram) if isinstance(ram, (int, float)) and ram > 0 else 0
        vram_bytes, approximate = _windows_vram(vram_bytes, pnp, name)
        entry = _classify_adapter(
            name, vram_bytes,
            pnp_id=pnp,
            compatibility=item.get("AdapterCompatibility"),
        )
        entry["approximate"] = approximate
        gpus.append(entry)
    return gpus


def _gpus_windows_wmic():
    """Last-resort GPU read via the deprecated `wmic` CSV output.

    Same AdapterRAM 32-bit caveat as the PowerShell path, and the same
    registry figure replacing it wherever that can be read.

    PNPDeviceID is asked for and AdapterCompatibility deliberately is not.
    This output is split on commas with no quoting, and every real
    AdapterCompatibility value contains one ("Advanced Micro Devices,
    Inc."), which would shift every later column on the row. PNPDeviceID
    never contains a comma, and it is the stronger of the two signals
    anyway.
    """
    exe = shutil.which("wmic")
    if not exe:
        return []
    out = _run([exe, "path", "Win32_VideoController", "get",
                "Name,AdapterRAM,PNPDeviceID", "/format:csv"])
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
    pnp_idx = header.index("PNPDeviceID") if "PNPDeviceID" in header else None
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
        pnp = parts[pnp_idx].strip() if pnp_idx is not None and len(parts) > pnp_idx else None
        vram_bytes, approximate = _windows_vram(vram_bytes, pnp, name)
        entry = _classify_adapter(name, vram_bytes, pnp_id=pnp)
        entry["approximate"] = approximate
        gpus.append(entry)
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
            with open(vram_path, encoding="utf-8", errors="replace") as f:
                vram_bytes = int(f.read().strip())
        except (OSError, ValueError):
            continue
        vendor = VENDOR_UNKNOWN
        try:
            with open(vendor_path, encoding="utf-8", errors="replace") as f:
                vendor = _vendor_from_pci_id(f.read().strip())
        except OSError:
            pass
        # The name here is "card0", which says nothing, so the integrated
        # call falls to the size rule: an APU's mem_info_vram_total is the
        # same small carve-out Windows reports for one.
        found = _classify_adapter(entry, vram_bytes, vendor=vendor)
        found["approximate"] = False
        gpus.append(found)
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
        entry = _classify_adapter(name, 0)
        entry["approximate"] = True
        gpus.append(entry)
    return gpus


def display_adapters():
    """Every display adapter this machine reports, virtual ones included.

    Each is a dict with keys name, vram_bytes, vendor, approximate (True
    when the vram_bytes reading is a known-unreliable fallback, as
    documented in the module docstring), virtual, and integrated.

    Callers that want to run something on a GPU want gpus(), which is this
    list with the virtual adapters removed. This function exists so that
    "there is a Parsec adapter here and it is not a GPU" can be reported
    rather than silently dropped.

    Empty list when nothing could be detected; never raises.
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


def gpus():
    """Detected GPUs as a list of dicts with keys name, vram_bytes, vendor,
    approximate, virtual and integrated.

    Virtual display adapters are excluded: a Parsec, RDP, Citrix or
    DisplayLink shim is enumerated by Windows in the same class as real
    hardware and can run nothing. Leaving them in was a real bug and not a
    cosmetic one, because a caller that reads the FIRST entry (the engine
    picker did) or the vendor of any entry could be handed a shim's
    "unknown" and conclude the machine has no usable GPU. Empty list when
    nothing usable could be detected; never raises.
    """
    return [g for g in display_adapters() if not g.get("virtual")]


# --------------------------------------------------------------------------
# NVIDIA capability: what CUDA build this card and driver could actually run
# --------------------------------------------------------------------------

#: The minimum NVIDIA driver version each CUDA major release needs on
#: Windows, from NVIDIA's own CUDA compatibility table. Used only as a
#: FALLBACK when nvidia-smi does not print its own "CUDA Version" header,
#: which is the authoritative reading and is preferred wherever it exists.
_CUDA_MIN_DRIVER = ((13, 580.0), (12, 527.41), (11, 452.39))

#: `nvidia-smi`'s banner carries the highest CUDA version the installed
#: DRIVER can run, which is not the same thing as an installed toolkit.
#: Older builds print "CUDA Version: 12.4"; 610.x prints
#: "CUDA UMD Version: 13.3" (confirmed on the machine this was written on).
#: Both spellings are accepted, and neither is required.
_CUDA_HEADER_RE = re.compile(r"CUDA\s+(?:UMD\s+)?Version:\s*([\d.]+)")


def _parse_compute_cap(text):
    """A compute capability string like "12.0" as a (major, minor) tuple.

    None for anything unparseable, including nvidia-smi's own "[N/A]",
    which it prints for a GPU it can see but cannot fully query. A card
    whose architecture cannot be established must never be assumed modern:
    the caller's whole job is refusing to install a build that will not
    load, and a guess defeats it.
    """
    m = re.match(r"^\s*(\d+)\.(\d+)\s*$", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _driver_cuda_ceiling(driver_version):
    """The highest CUDA MAJOR version a driver of this version can run.

    A coarse floor-based reading of NVIDIA's compatibility table, used only
    when nvidia-smi did not report its own ceiling. Returns None when the
    driver version cannot be read at all.
    """
    try:
        value = float(re.match(r"^\s*(\d+(?:\.\d+)?)", driver_version or "").group(1))
    except (AttributeError, ValueError):
        return None
    for major, floor in _CUDA_MIN_DRIVER:
        if value >= floor:
            return major
    return None


def nvidia_detail():
    """Per-GPU NVIDIA facts that VRAM alone does not answer, or None.

    Returns a list of dicts:

        {"index": int, "name": str, "compute_capability": (major, minor)|None,
         "driver_version": str|None, "cuda_driver_major": int|None}

    None (not []) means the question could not be asked at all: no
    nvidia-smi, a timeout, a nonzero exit. That is different from an empty
    list, which would mean nvidia-smi ran and found no NVIDIA GPU, and the
    difference matters to a caller deciding whether to install a CUDA build.

    compute_capability is what actually decides which CUDA toolkit a card
    needs: 12.0 is Blackwell (RTX 50-series), which the CUDA 12.4 build
    predates entirely, while CUDA 13 dropped everything below 7.5. Neither
    fact is derivable from the card's marketing name.

    Never raises.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    out = _run([exe, "--query-gpu=index,name,compute_cap,driver_version",
                "--format=csv,noheader"])
    if out is None:
        return None
    ceiling = None
    banner = _run([exe])
    if banner:
        m = _CUDA_HEADER_RE.search(banner)
        if m:
            try:
                ceiling = int(float(m.group(1)))
            except ValueError:
                ceiling = None

    found = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split from the right: index is first, and compute_cap and
        # driver_version are the last two columns, but a card name can
        # itself contain a comma.
        head, _, rest = line.partition(",")
        parts = [p.strip() for p in rest.rsplit(",", 2)]
        if len(parts) < 3:
            continue
        name, cap_text, driver = parts
        try:
            index = int(head.strip())
        except ValueError:
            continue
        found.append({
            "index": index,
            "name": name,
            "compute_capability": _parse_compute_cap(cap_text),
            "driver_version": driver or None,
            "cuda_driver_major": ceiling if ceiling is not None
                                 else _driver_cuda_ceiling(driver),
        })
    return found


def system_ram_bytes():
    """Total physical system RAM in bytes. 0 if it cannot be determined."""
    system = platform.system()
    if system == "Linux":
        try:
            with open("/proc/meminfo", encoding="utf-8", errors="replace") as f:
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
    """Everything this module knows about the machine, as one JSON-safe dict.

    "gpus" holds the usable GPUs. "virtual_adapters" names the display
    adapters that were found and excluded, so that a machine reporting no
    GPU can say whether it really has none or only has shims.
    """
    adapters = display_adapters()
    return {
        "platform": platform.system(),
        "gpus": [g for g in adapters if not g.get("virtual")],
        "virtual_adapters": [g.get("name") for g in adapters if g.get("virtual")],
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
    # Every one of these is a string a real Windows machine reports. The
    # integrated parts are the ones that matter most here: they are what
    # most laptops have, they are what Vulkan exists to cover, and getting
    # one of them wrong is worth more lost performance than any of the
    # discrete cases, because the user has no faster alternative to fall
    # back on.
    for name in ("NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 5080",
                 "NVIDIA GeForce GTX 1080 Ti", "NVIDIA T1000 8GB",
                 "Quadro P2000", "NVIDIA TITAN Xp", "Tesla V100-PCIE-16GB"):
        assert _vendor_from_name(name) == VENDOR_NVIDIA, name
    for name in ("AMD Radeon RX 7900 XTX", "AMD Radeon(TM) 880M Graphics",
                 "AMD Radeon(TM) Graphics", "AMD Radeon RX 6800M",
                 "AMD Radeon Vega 8 Graphics", "Radeon RX 550",
                 "Advanced Micro Devices, Inc.", "AMD Radeon Pro W6800",
                 "ATI Mobility Radeon HD 4200"):
        assert _vendor_from_name(name) == VENDOR_AMD, name
    for name in ("Intel(R) Iris(R) Xe Graphics", "Intel(R) UHD Graphics 630",
                 "Intel(R) HD Graphics 520", "Intel(R) Arc(TM) A770 Graphics",
                 "Intel(R) Arc(TM) Graphics", "Intel Corporation"):
        assert _vendor_from_name(name) == VENDOR_INTEL, name
    assert _vendor_from_name("Some Mystery Card") == VENDOR_UNKNOWN
    assert _vendor_from_name("Parsec Cloud, Inc.") == VENDOR_UNKNOWN
    assert _vendor_from_name("") == VENDOR_UNKNOWN
    assert _vendor_from_name(None) == VENDOR_UNKNOWN
    # The short tokens are matched as words, not as substrings: without
    # boundaries every one of these becomes a false vendor call.
    assert _vendor_from_name("Matrox G200eR2 Architecture") == VENDOR_UNKNOWN
    assert _vendor_from_name("Creative Labs Display Device") == VENDOR_UNKNOWN

    # -- vendor from the PCI ID, which outranks any name -------------------
    assert _vendor_from_pci_id(r"PCI\VEN_1002&DEV_150E&SUBSYS_39A81043&REV_C1") == VENDOR_AMD
    assert _vendor_from_pci_id(r"PCI\VEN_10DE&DEV_2C02") == VENDOR_NVIDIA
    assert _vendor_from_pci_id(r"PCI\VEN_8086&DEV_7D55") == VENDOR_INTEL
    assert _vendor_from_pci_id("0x1002") == VENDOR_AMD          # Linux sysfs spelling
    assert _vendor_from_pci_id("10de") == VENDOR_NVIDIA
    assert _vendor_from_pci_id(r"PCI\VEN_15AD&DEV_0405") == VENDOR_UNKNOWN  # VMware
    assert _vendor_from_pci_id(r"ROOT\DISPLAY\0000") == VENDOR_UNKNOWN
    assert _vendor_from_pci_id("") == VENDOR_UNKNOWN
    assert _vendor_from_pci_id(None) == VENDOR_UNKNOWN

    # -- virtual adapters are not GPUs -------------------------------------
    # A Parsec adapter sorting ahead of a Radeon 880M is the exact bug this
    # pins: the picker read the first entry, got "unknown", and the machine
    # ran on CPU with a real GPU sitting idle beside it.
    for name in ("Parsec Virtual Display Adapter",
                 "Microsoft Remote Display Adapter",
                 "Microsoft Basic Display Adapter",
                 "Microsoft Basic Render Driver",
                 "Citrix Indirect Display Adapter",
                 "DisplayLink USB Device",
                 "VMware SVGA 3D",
                 "VirtualBox Graphics Adapter",
                 "Hyper-V Video",
                 "IddSampleDriver Device",
                 "Red Hat QXL controller"):
        assert _is_virtual_adapter(name, None, VENDOR_UNKNOWN) is True, name
    # A real GPU is never virtual, whatever else is true of it.
    for name in ("AMD Radeon(TM) 880M Graphics", "NVIDIA GeForce RTX 5080",
                 "Intel(R) Iris(R) Xe Graphics", "AMD Radeon RX 7900 XTX"):
        assert _is_virtual_adapter(name, r"PCI\VEN_1002&DEV_150E", VENDOR_AMD) is False, name
    # The structural rule: not on the PCI bus AND unidentifiable.
    assert _is_virtual_adapter("Something Odd", r"ROOT\DISPLAY\0000", VENDOR_UNKNOWN) is True
    # ... but a device Hearth CAN identify is real however it is attached,
    # so this rule can never be what excludes a genuine GPU on its own.
    assert _is_virtual_adapter("Intel(R) Arc(TM) Graphics", r"ACPI\INTC1234",
                               VENDOR_INTEL) is False
    assert _is_virtual_adapter("Some Card", None, VENDOR_UNKNOWN) is False

    # -- integrated versus dedicated memory ---------------------------------
    # True here means "the memory figure beside this is carved out of system
    # RAM", which the shop must know before it calls anything a good fit.
    assert _is_integrated("AMD Radeon(TM) 880M Graphics", VENDOR_AMD, 536870912) is True
    assert _is_integrated("AMD Radeon(TM) 780M Graphics", VENDOR_AMD, 0) is True
    assert _is_integrated("AMD Radeon(TM) Graphics", VENDOR_AMD, 0) is True
    assert _is_integrated("AMD Radeon Vega 8 Graphics", VENDOR_AMD, 0) is True
    assert _is_integrated("AMD Radeon R5 Graphics", VENDOR_AMD, 0) is True
    # AMD's discrete mobile parts use four digits and an M, so the
    # three-digit rule must not reach them.
    assert _is_integrated("AMD Radeon RX 6800M", VENDOR_AMD, 12 * 1024 ** 3) is False
    assert _is_integrated("AMD Radeon RX 7900 XTX", VENDOR_AMD, 24 * 1024 ** 3) is False
    # Vega 56 and 64 are discrete cards; Vega 3 to 11 are APU graphics.
    assert _is_integrated("Radeon RX Vega 64", VENDOR_AMD, 8 * 1024 ** 3) is False
    # A size at or below the ceiling says integrated even with a name that
    # gives nothing away.
    assert _is_integrated("AMD Radeon Series", VENDOR_AMD, 512 * 1024 ** 2) is True
    # An AMD card with no name signal and no readable size cannot be
    # classified, and says so rather than guessing "dedicated".
    assert _is_integrated("AMD Radeon Series", VENDOR_AMD, 0) is None
    # Intel is integrated unless it is an Arc with a model number.
    assert _is_integrated("Intel(R) UHD Graphics 630", VENDOR_INTEL, 1024 ** 3) is True
    assert _is_integrated("Intel(R) Iris(R) Xe Graphics", VENDOR_INTEL, 0) is True
    assert _is_integrated("Intel(R) Arc(TM) Graphics", VENDOR_INTEL, 0) is True
    assert _is_integrated("Intel(R) Arc(TM) A770 Graphics", VENDOR_INTEL, 16 * 1024 ** 3) is False
    assert _is_integrated("Intel(R) Arc(TM) B580 Graphics", VENDOR_INTEL, 12 * 1024 ** 3) is False
    # NVIDIA ships no integrated PC graphics.
    assert _is_integrated("NVIDIA GeForce RTX 5080", VENDOR_NVIDIA, 17 * 1024 ** 3) is False
    assert _is_integrated("Mystery Device", VENDOR_UNKNOWN, 0) is None

    # -- _classify_adapter: the PCI ID outranks a misleading name ----------
    # The PCI vendor ID ALONE, with a name that says nothing and no
    # AdapterCompatibility at all. This is the only assertion that pins the
    # strongest of the three vendor signals: every other fixture here also
    # carries a name or a compatibility string that would answer on its own,
    # so without this one the PCI reading could be deleted entirely and
    # nothing would notice.
    got = _classify_adapter("Standard Display Adapter", 536870912,
                            pnp_id=r"PCI\VEN_1002&DEV_150E")
    assert got["vendor"] == VENDOR_AMD, got
    assert got["virtual"] is False and got["integrated"] is True, got
    for pnp, want in ((r"PCI\VEN_10DE&DEV_2C02", VENDOR_NVIDIA),
                      (r"PCI\VEN_8086&DEV_7D55", VENDOR_INTEL)):
        got = _classify_adapter("Standard Display Adapter", 0, pnp_id=pnp)
        assert got["vendor"] == want, (pnp, got)
    # All three signals present and agreeing is the ordinary case.
    got = _classify_adapter("Standard Display Adapter", 536870912,
                            pnp_id=r"PCI\VEN_1002&DEV_150E",
                            compatibility="Advanced Micro Devices, Inc.")
    assert got["vendor"] == VENDOR_AMD, got
    # No PCI ID: AdapterCompatibility answers before the name is consulted.
    got = _classify_adapter("Standard Display Adapter", 0,
                            compatibility="Intel Corporation")
    assert got["vendor"] == VENDOR_INTEL, got
    # A caller that already knows the vendor is believed and not re-guessed.
    got = _classify_adapter("card0", 8 * 1024 ** 3, vendor=VENDOR_NVIDIA)
    assert got["vendor"] == VENDOR_NVIDIA and got["integrated"] is False, got

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

    # A malformed numeric field ("1.2.3" style) matches the regex
    # ([\d.]+) but is not a valid float; this must be skipped, not raise
    # ValueError out of _parse_nvidia_smi (and thus out of gpus()/probe(),
    # which both promise never to raise). Mixed with a good line to prove
    # the bad line is skipped rather than the whole parse aborting.
    malformed = _parse_nvidia_smi("Bad Driver GPU, 1.2.3 MiB\nGood GPU, 8192 MiB\n")
    assert malformed == [{
        "name": "Good GPU",
        "vram_bytes": 8192 * 1024 * 1024,
        "vendor": VENDOR_NVIDIA,
        "virtual": False,
        "integrated": False,
        "approximate": False,
    }], malformed
    assert _parse_nvidia_smi("Only Bad GPU, 1.2.3 MiB\n") == []

    # A GPU name containing a comma must not be dropped or truncated: split
    # from the right (the memory column is always last), not the left.
    comma_name = _parse_nvidia_smi("NVIDIA RTX 6000, Ada Generation, 49140 MiB\n")
    assert len(comma_name) == 1, comma_name
    assert comma_name[0]["name"] == "NVIDIA RTX 6000, Ada Generation", comma_name
    assert comma_name[0]["vram_bytes"] == 49140 * 1024 * 1024, comma_name

    # A vGPU slice is commonly named with the word "Virtual" in it, and
    # nvidia-smi only enumerates CUDA-capable devices, so what it reports is
    # real by definition. The name-based virtual rule must not reach this
    # path: excluding it would leave a machine nvidia-smi had just answered
    # for with no GPU at all.
    vgpu = _parse_nvidia_smi("NVIDIA A40-8Q Virtual GPU, 8192 MiB\n")
    assert len(vgpu) == 1, vgpu
    assert vgpu[0]["virtual"] is False, vgpu
    assert vgpu[0]["vendor"] == VENDOR_NVIDIA, vgpu

    # -- _run: missing executables never raise ------------------------------
    assert _run(["this-executable-does-not-exist-anywhere-12345"]) is None
    assert _run(["python", "-c", "import sys; sys.exit(1)"]) is None

    # -- _run: malformed stdout bytes must never raise UnicodeDecodeError ---
    # UnicodeDecodeError is a ValueError subclass, NOT caught by the
    # (FileNotFoundError, OSError, subprocess.SubprocessError) handler in
    # _run, so without an explicit encoding="utf-8", errors="replace" on the
    # subprocess.run call, a child process that writes bytes that are not
    # valid text under the platform's default locale encoding can crash
    # probe() outright (or, observed on some Windows/thread configurations,
    # silently corrupt the captured output instead of raising at all -
    # either way, a real failure mode this call must not exhibit).
    bad_bytes_cmd = [
        sys.executable, "-c",
        "import sys; sys.stdout.buffer.write(bytes([0x81, 0x8d, 0x90, 0xff])); "
        "sys.stdout.buffer.flush()",
    ]
    bad_bytes_out = _run(bad_bytes_cmd)
    assert bad_bytes_out is not None, "malformed stdout bytes must not become a None result"
    assert isinstance(bad_bytes_out, str), bad_bytes_out
    assert "�" in bad_bytes_out, (
        "malformed bytes should decode to U+FFFD replacement characters, "
        "not be silently dropped or raise", bad_bytes_out,
    )

    # -- gpus(): shape holds regardless of what hardware is present ---------
    detected = gpus()
    assert isinstance(detected, list)
    for g in detected:
        assert set(("name", "vram_bytes", "vendor", "virtual",
                    "integrated")).issubset(g.keys()), g
        assert isinstance(g["name"], str) and g["name"], g
        assert isinstance(g["vram_bytes"], int), g
        assert g["vram_bytes"] >= 0, g
        assert g["vendor"] in (VENDOR_NVIDIA, VENDOR_AMD, VENDOR_INTEL, VENDOR_UNKNOWN), g
        assert g["virtual"] is False, ("gpus() must not return a virtual adapter", g)
        assert g["integrated"] in (True, False, None), g
    for g in display_adapters():
        assert isinstance(g.get("virtual"), bool), g

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
    assert set(("platform", "gpus", "virtual_adapters", "system_ram_bytes",
                "cpu_count")).issubset(p.keys()), p
    assert p["platform"] == platform.system()
    assert isinstance(p["gpus"], list)
    assert isinstance(p["virtual_adapters"], list), p
    assert isinstance(p["system_ram_bytes"], int)
    assert isinstance(p["cpu_count"], int)
    # Must be trivially JSON-serialisable, since the shop ships it over the wire.
    encoded = json.dumps(p)
    assert json.loads(encoded) == p

    # -- the driver's 64-bit memory figure, from a fake registry -------------
    # Modelled on the registry of the machine this was written on (an RTX
    # 5080 beside an AMD iGPU), stale keys and all: 0001 is a card that was
    # swapped out, 0002 is the same AMD chip at an older revision, and 0000
    # is Microsoft's fallback driver matching by class code. AdapterRAM
    # reads 4293918720 for the 5080 there; the real figure is ~16GB.
    cls, enum = _DISPLAY_CLASS_KEY, _ENUM_KEY
    pnp_5080 = r"PCI\VEN_10DE&DEV_2C02&SUBSYS_53101462&REV_A1\03B6ED9B6B2DB04800"
    pnp_igpu = r"PCI\VEN_1002&DEV_13C0&SUBSYS_7E571462&REV_C2\4&3055F162&0&0041"
    gib = 1024 ** 3

    def _fake_registry(values, denied=()):
        def fake_value(path, name):
            if path in denied:
                raise PermissionError(5, "Access is denied")
            try:
                return values[path][name]
            except KeyError:
                raise FileNotFoundError(2, "The system cannot find the file specified")

        def fake_subkeys(path):
            if path in denied:
                raise PermissionError(5, "Access is denied")
            prefix = path + "\\"
            return sorted({k[len(prefix):].split("\\")[0]
                           for k in list(values) + list(denied) if k.startswith(prefix)})
        return fake_value, fake_subkeys

    machine = {
        cls + r"\0000": {"DriverDesc": ("Microsoft Basic Display Adapter", 1),
                         "MatchingDeviceId": (r"PCI\CC_0300", 1)},
        cls + r"\0001": {"DriverDesc": ("NVIDIA GeForce RTX 5060", 1),
                         "MatchingDeviceId": (r"pci\ven_10de&dev_2d05&subsys_53711462", 1),
                         "HardwareInformation.qwMemorySize": (8546942976, _REG_QWORD)},
        cls + r"\0002": {"DriverDesc": ("AMD Radeon(TM) Graphics", 1),
                         "MatchingDeviceId": (r"PCI\VEN_1002&DEV_13C0&REV_CB", 1),
                         "HardwareInformation.qwMemorySize": (2 * gib, _REG_QWORD)},
        cls + r"\0004": {"DriverDesc": ("AMD Radeon(TM) Graphics", 1),
                         "MatchingDeviceId": (r"PCI\VEN_1002&DEV_13C0&REV_C2", 1),
                         "HardwareInformation.qwMemorySize": (536870912, _REG_QWORD),
                         "HardwareInformation.MemorySize": (536870912, _REG_DWORD)},
        cls + r"\0005": {"DriverDesc": ("NVIDIA GeForce RTX 5080", 1),
                         "MatchingDeviceId": (r"pci\ven_10de&dev_2c02&subsys_53101462", 1),
                         "HardwareInformation.qwMemorySize": (17094934528, _REG_QWORD),
                         "HardwareInformation.MemorySize": (4293918720, _REG_DWORD)},
        cls + r"\0006": {"DriverDesc": ("Parsec Virtual Display Adapter", 1),
                         "MatchingDeviceId": (r"Root\Parsec\VDA", 1)},
        # An older AMD driver that writes only the binary MemorySize, eight
        # bytes little-endian.
        cls + r"\0008": {"DriverDesc": ("Radeon RX 580 Series", 1),
                         "MatchingDeviceId": (r"PCI\VEN_1002&DEV_67DF", 1),
                         "HardwareInformation.MemorySize":
                             ((8 * gib).to_bytes(8, "little"), _REG_BINARY)},
        enum + "\\" + pnp_5080: {"Driver": (_DISPLAY_CLASS_GUID + r"\0005", 1)},
    }
    # 0007 refuses a standard user outright, and "Properties" always does.
    denied = {cls + r"\0007", cls + r"\Properties"}

    assert _hardware_id_matches(r"pci\ven_10de&dev_2c02&subsys_53101462", pnp_5080)
    assert _hardware_id_matches(r"PCI\VEN_1002&DEV_13C0&REV_C2", pnp_igpu)
    assert not _hardware_id_matches(r"PCI\VEN_1002&DEV_13C0&REV_CB", pnp_igpu)
    assert not _hardware_id_matches(r"PCI\CC_0300", pnp_igpu)
    assert not _hardware_id_matches(r"Root\Parsec\VDA", r"ROOT\DISPLAY\0000")
    assert not _hardware_id_matches(r"PCI\VEN_1002&DEV_13C0", None)
    # Never a prefix test: DEV_13C is not DEV_13C0.
    assert not _hardware_id_matches(r"PCI\VEN_1002&DEV_13C", pnp_igpu)

    assert _decode_memory_size((17094934528, _REG_QWORD)) == (17094934528, True)
    assert _decode_memory_size((4293918720, _REG_DWORD)) == (4293918720, False)
    assert _decode_memory_size(((8 * gib).to_bytes(8, "little"), _REG_BINARY)) == (8 * gib, True)
    assert _decode_memory_size(((2 * gib).to_bytes(4, "little"), _REG_BINARY)) == (2 * gib, False)
    assert _decode_memory_size((b"\x00\x01\x02", _REG_BINARY)) == (0, False)
    assert _decode_memory_size(("16GB", 1)) == (0, False)
    assert _decode_memory_size(None) == (0, False)

    old_reg = (globals()["_reg_value"], globals()["_reg_subkeys"])
    try:
        globals()["_reg_value"], globals()["_reg_subkeys"] = _fake_registry(machine, denied)
        # qwMemorySize reached through the device instance's own Driver link.
        assert _registry_vram(pnp_5080, "NVIDIA GeForce RTX 5080") == (17094934528, True)
        # No Enum entry: matched by hardware ID, and the stale REV_CB key
        # with a different figure is not mistaken for this chip.
        assert _registry_vram(pnp_igpu, "AMD Radeon(TM) Graphics") == (536870912, True)
        # Binary MemorySize, matched by vendor and device alone.
        pnp_580 = r"PCI\VEN_1002&DEV_67DF&SUBSYS_E3531DA2&REV_E7\4&1A2B3C4D&0&0008"
        assert _registry_vram(pnp_580, "Radeon RX 580 Series") == (8 * gib, True)
        # No PNPDeviceID at all: the name finds it.
        assert _registry_vram(None, "NVIDIA GeForce RTX 5080") == (17094934528, True)
        # Nothing matches: no figure, and no exception.
        assert _registry_vram(r"PCI\VEN_8086&DEV_7D55\3&11583659&0&10",
                              "Intel(R) Arc(TM) Graphics") == (0, False)
        assert _registry_vram(None, None) == (0, False)
        # The Basic Display driver matches by class code only, and has no
        # memory figure: it can never lend one to a real card.
        assert _registry_vram(r"PCI\VEN_1234&DEV_1111\0", "Some Card") == (0, False)

        # Two identical cards with no Enum entries share one driver key:
        # both get its figure.
        twin = dict(machine)
        twin_a = r"PCI\VEN_1002&DEV_744C&SUBSYS_0E3B1002&REV_C8\6&AAAA&0&00000019"
        twin_b = r"PCI\VEN_1002&DEV_744C&SUBSYS_0E3B1002&REV_C8\6&BBBB&0&00000019"
        twin[cls + r"\0009"] = {
            "DriverDesc": ("AMD Radeon RX 7900 XTX", 1),
            "MatchingDeviceId": (r"PCI\VEN_1002&DEV_744C&REV_C8", 1),
            "HardwareInformation.qwMemorySize": (24 * gib, _REG_QWORD)}
        globals()["_reg_value"], globals()["_reg_subkeys"] = _fake_registry(twin, denied)
        assert _registry_vram(twin_a, "AMD Radeon RX 7900 XTX") == (24 * gib, True)
        assert _registry_vram(twin_b, "AMD Radeon RX 7900 XTX") == (24 * gib, True)
        # A reinstall left a second key with the SAME figure: still fine.
        twin[cls + r"\0010"] = dict(twin[cls + r"\0009"])
        assert _registry_vram(twin_a, "AMD Radeon RX 7900 XTX") == (24 * gib, True)
        # Two keys that disagree cannot both describe this card, and this
        # module cannot tell which one does, so it claims neither.
        twin[cls + r"\0010"]["HardwareInformation.qwMemorySize"] = (20 * gib, _REG_QWORD)
        assert _registry_vram(twin_a, "AMD Radeon RX 7900 XTX") == (0, False)

        # An Enum Driver link into some other device class is ignored.
        odd = dict(machine)
        odd[enum + "\\" + pnp_igpu] = {
            "Driver": (r"{36fc9e60-c465-11cf-8056-444553540000}\0005", 1)}
        globals()["_reg_value"], globals()["_reg_subkeys"] = _fake_registry(odd, denied)
        assert _registry_vram(pnp_igpu, "AMD Radeon(TM) Graphics") == (536870912, True)

        # The whole class key refusing access degrades to "no figure".
        globals()["_reg_value"], globals()["_reg_subkeys"] = _fake_registry({}, {cls})
        assert _registry_vram(pnp_igpu, "AMD Radeon(TM) Graphics") == (0, False)

        # _windows_vram: only a 64-bit figure makes a reading exact. A 32-bit
        # one fills a blank AdapterRAM but stays approximate.
        thirty_two = {
            cls + r"\0000": {"DriverDesc": ("Old Card", 1),
                             "MatchingDeviceId": (r"PCI\VEN_1002&DEV_6798", 1),
                             "HardwareInformation.MemorySize": (3 * gib, _REG_DWORD)}}
        globals()["_reg_value"], globals()["_reg_subkeys"] = _fake_registry(thirty_two)
        pnp_old = r"PCI\VEN_1002&DEV_6798&SUBSYS_30001002&REV_00\4&1&0&0008"
        assert _windows_vram(0, pnp_old, "Old Card") == (3 * gib, True)
        assert _windows_vram(2 * gib, pnp_old, "Old Card") == (2 * gib, True)
        globals()["_reg_value"], globals()["_reg_subkeys"] = _fake_registry(machine, denied)
        assert _windows_vram(4293918720, pnp_5080, "NVIDIA GeForce RTX 5080") == (17094934528, False)
        assert _windows_vram(0, None, "Unlisted Card") == (0, True)

        # End to end through the PowerShell path, which is what the shop
        # and the offload calculator actually read.
        def _fake_ps_machine(cmd, timeout=SUBPROCESS_TIMEOUT):
            return json.dumps([
                {"Name": "Parsec Virtual Display Adapter", "AdapterRAM": None,
                 "PNPDeviceID": "ROOT\\DISPLAY\\0000",
                 "AdapterCompatibility": "Parsec Cloud, Inc."},
                {"Name": "NVIDIA GeForce RTX 5080", "AdapterRAM": 4293918720,
                 "PNPDeviceID": pnp_5080, "AdapterCompatibility": "NVIDIA"},
                {"Name": "AMD Radeon(TM) Graphics", "AdapterRAM": 536870912,
                 "PNPDeviceID": pnp_igpu,
                 "AdapterCompatibility": "Advanced Micro Devices, Inc."},
            ])
        old_run_reg, old_which_reg = globals()["_run"], shutil.which
        try:
            globals()["_run"] = _fake_ps_machine
            shutil.which = lambda n: "powershell.exe" if "powershell" in n else None
            parsec, rtx, igpu = _gpus_windows_powershell()
            assert parsec["virtual"] is True and parsec["approximate"] is True, parsec
            assert rtx["vram_bytes"] == 17094934528 and rtx["approximate"] is False, rtx
            assert rtx["integrated"] is False, rtx
            # The iGPU's figure is exact AND still a carve-out: exactness must
            # not turn integrated memory into dedicated VRAM.
            assert igpu["vram_bytes"] == 536870912 and igpu["approximate"] is False, igpu
            assert igpu["integrated"] is True, igpu

            # A discrete AMD card read through a wrapped AdapterRAM: the
            # registry's 24GB is what reaches the shop.
            def _fake_ps_xtx(cmd, timeout=SUBPROCESS_TIMEOUT):
                return json.dumps([{"Name": "AMD Radeon RX 7900 XTX",
                                    "AdapterRAM": 4293918720, "PNPDeviceID": twin_a}])
            globals()["_run"] = _fake_ps_xtx
            globals()["_reg_value"], globals()["_reg_subkeys"] = _fake_registry(
                {cls + r"\0009": twin[cls + r"\0009"]})
            (xtx,) = _gpus_windows_powershell()
            assert xtx["vram_bytes"] == 24 * gib and xtx["approximate"] is False, xtx
            assert xtx["vendor"] == VENDOR_AMD and xtx["integrated"] is False, xtx

            # And through wmic, which carries PNPDeviceID too.
            def _fake_wmic_xtx(cmd, timeout=SUBPROCESS_TIMEOUT):
                return ("Node,AdapterRAM,Name,PNPDeviceID\r\n"
                        "HOST,4293918720,AMD Radeon RX 7900 XTX," + twin_a + "\r\n")
            globals()["_run"] = _fake_wmic_xtx
            shutil.which = lambda n: "wmic.exe" if n == "wmic" else None
            (xtx_wmic,) = _gpus_windows_wmic()
            assert xtx_wmic["vram_bytes"] == 24 * gib, xtx_wmic
            assert xtx_wmic["approximate"] is False, xtx_wmic
        finally:
            globals()["_run"] = old_run_reg
            shutil.which = old_which_reg
    finally:
        globals()["_reg_value"], globals()["_reg_subkeys"] = old_reg

    # The real registry, where there is one: never raises, and a device that
    # does not exist gets no figure.
    assert _registry_vram(r"PCI\VEN_FFFF&DEV_FFFF\0", "No Such Card Anywhere") == (0, False)

    # -- Windows WMI/CIM parsers: exercised with canned fixtures so the ------
    # -- self-test does not depend on this host's specific hardware ---------
    # Simulate a large card the 32-bit-signed AdapterRAM field would wrap on.
    # These fixtures pin the AdapterRAM fallback, so the registry is emptied
    # for them: on a host whose real registry knows one of these cards (the
    # G14 below is a real machine), the exact figure would rightly win.
    old_run = globals()["_run"]
    old_reg_fixture = (globals()["_reg_value"], globals()["_reg_subkeys"])
    try:
        globals()["_reg_value"], globals()["_reg_subkeys"] = _fake_registry({})

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

        # THE REGRESSION FIXTURE. This is the literal Win32_VideoController
        # reading from the machine the bug was found on: an ASUS G14 with a
        # Radeon 880M and Parsec installed. Parsec's shim sorts FIRST, has
        # no AdapterRAM and no identifiable vendor, and the engine picker
        # read the first entry's vendor, got "unknown", and left the machine
        # on the CPU build with a perfectly good Vulkan-capable GPU beside
        # it. Every assertion below failed before this commit.
        def _fake_g14(cmd, timeout=SUBPROCESS_TIMEOUT):
            return json.dumps([
                {"Name": "Parsec Virtual Display Adapter", "AdapterRAM": None,
                 "PNPDeviceID": "ROOT\\DISPLAY\\0000",
                 "AdapterCompatibility": "Parsec Cloud, Inc."},
                {"Name": "AMD Radeon(TM) 880M Graphics", "AdapterRAM": 536870912,
                 "PNPDeviceID": "PCI\\VEN_1002&DEV_150E&SUBSYS_39A81043&REV_C1"
                                "\\4&35FE04F8&0&0041",
                 "AdapterCompatibility": "Advanced Micro Devices, Inc."},
            ])
        globals()["_run"] = _fake_g14
        if shutil.which("powershell") or shutil.which("powershell.exe"):
            g14 = _gpus_windows_powershell()
            assert len(g14) == 2, g14
            parsec, radeon = g14
            assert parsec["virtual"] is True, parsec
            assert radeon["virtual"] is False, radeon
            assert radeon["vendor"] == VENDOR_AMD, radeon
            assert radeon["integrated"] is True, radeon
            assert radeon["vram_bytes"] == 536870912, radeon
            assert radeon["approximate"] is True, radeon

        def _fake_none(cmd, timeout=SUBPROCESS_TIMEOUT):
            return None
        globals()["_run"] = _fake_none
        assert _gpus_windows_powershell() == []
        assert _gpus_windows_wmic() == []
    finally:
        globals()["_run"] = old_run
        globals()["_reg_value"], globals()["_reg_subkeys"] = old_reg_fixture

    # -- gpus() drops the shims; display_adapters() and probe() name them ---
    # Driven through a stubbed display_adapters so this holds on any host.
    old_adapters = globals()["display_adapters"]
    try:
        fixture = [
            {"name": "Parsec Virtual Display Adapter", "vram_bytes": 0,
             "vendor": VENDOR_UNKNOWN, "approximate": True, "virtual": True,
             "integrated": None},
            {"name": "AMD Radeon(TM) 880M Graphics", "vram_bytes": 536870912,
             "vendor": VENDOR_AMD, "approximate": True, "virtual": False,
             "integrated": True},
        ]
        globals()["display_adapters"] = lambda: fixture
        assert [g["name"] for g in gpus()] == ["AMD Radeon(TM) 880M Graphics"], gpus()
        p_g14 = probe()
        assert p_g14["virtual_adapters"] == ["Parsec Virtual Display Adapter"], p_g14
        assert len(p_g14["gpus"]) == 1, p_g14

        # A machine whose ONLY adapter is a shim has no GPU, and the shim is
        # still named rather than vanishing.
        globals()["display_adapters"] = lambda: [fixture[0]]
        assert gpus() == []
        assert probe()["virtual_adapters"] == ["Parsec Virtual Display Adapter"]
    finally:
        globals()["display_adapters"] = old_adapters

    # -- wmic CSV parser fixture ---------------------------------------------
    wmic_csv = "Node,AdapterRAM,Name\r\nHOST,4294967296,NVIDIA GeForce RTX 3080\r\n"
    old_run2 = globals()["_run"]
    old_reg_wmic = (globals()["_reg_value"], globals()["_reg_subkeys"])
    try:
        globals()["_reg_value"], globals()["_reg_subkeys"] = _fake_registry({})

        def _fake_wmic(cmd, timeout=SUBPROCESS_TIMEOUT):
            return wmic_csv
        globals()["_run"] = _fake_wmic
        if shutil.which("wmic"):
            wr = _gpus_windows_wmic()
            assert wr[0]["name"] == "NVIDIA GeForce RTX 3080", wr
            assert wr[0]["vram_bytes"] == 4294967296, wr
    finally:
        globals()["_run"] = old_run2
        globals()["_reg_value"], globals()["_reg_subkeys"] = old_reg_wmic

    # -- nvidia_detail: compute capability and the driver's CUDA ceiling -----
    # These two readings decide whether a CUDA build can load at all, so
    # every one of them is driven from a fixture rather than from whatever
    # card happens to be in the machine running the test.
    assert _parse_compute_cap("12.0") == (12, 0)
    assert _parse_compute_cap(" 8.6 ") == (8, 6)
    # nvidia-smi prints this for a GPU it can see but cannot fully query. A
    # card whose architecture is unknown must stay unknown, never be
    # assumed modern: installing CUDA 13 on a card it dropped is precisely
    # the load-time failure this reading exists to prevent.
    for junk in ("[N/A]", "", None, "unknown", "12", "12.0.1"):
        assert _parse_compute_cap(junk) is None, junk

    assert _driver_cuda_ceiling("610.47") == 13, _driver_cuda_ceiling("610.47")
    assert _driver_cuda_ceiling("580.00") == 13
    assert _driver_cuda_ceiling("579.99") == 12
    assert _driver_cuda_ceiling("527.41") == 12
    assert _driver_cuda_ceiling("470.05") == 11
    assert _driver_cuda_ceiling("400.00") is None
    assert _driver_cuda_ceiling("") is None
    assert _driver_cuda_ceiling(None) is None

    old_run3 = globals()["_run"]
    old_which = globals()["shutil"].which
    try:
        # A card name with a comma in it, which is why the parse splits from
        # the right for the last two columns and from the left for the index.
        rows = ("0, NVIDIA RTX A6000, Ada, 8.9, 552.22\n"
                "1, NVIDIA GeForce RTX 5080, 12.0, 610.47\n")
        banner = ("| NVIDIA-SMI 610.47   KMD Version: 610.47   "
                  "CUDA UMD Version: 13.3 |\n")

        def _fake_smi(cmd, timeout=SUBPROCESS_TIMEOUT):
            return rows if any("query-gpu" in str(c) for c in cmd) else banner
        globals()["_run"] = _fake_smi
        globals()["shutil"].which = lambda _n: "nvidia-smi"
        got = nvidia_detail()
        assert len(got) == 2, got
        assert got[0]["name"] == "NVIDIA RTX A6000, Ada", got[0]
        assert got[0]["compute_capability"] == (8, 9), got[0]
        assert got[1]["compute_capability"] == (12, 0), got[1]
        assert got[1]["driver_version"] == "610.47", got[1]
        # The banner is authoritative, so BOTH cards report the driver's
        # ceiling of 13 rather than a per-card guess from its own version.
        assert [g["cuda_driver_major"] for g in got] == [13, 13], got

        # Older nvidia-smi spells the header without "UMD".
        banner = "| NVIDIA-SMI 550.54   Driver Version: 550.54   CUDA Version: 12.4 |\n"
        assert [g["cuda_driver_major"] for g in nvidia_detail()] == [12, 12]

        # No banner at all: fall back to the driver-version table, per card.
        banner = "no header here\n"
        assert [g["cuda_driver_major"] for g in nvidia_detail()] == [12, 13]

        # nvidia-smi ran but reported nothing: an empty list, which means
        # "asked, no NVIDIA GPU", not "could not ask".
        rows = ""
        assert nvidia_detail() == []

        # nvidia-smi failed. None, distinct from the empty list above.
        def _fake_dead(cmd, timeout=SUBPROCESS_TIMEOUT):
            return None
        globals()["_run"] = _fake_dead
        assert nvidia_detail() is None

        # No nvidia-smi on PATH at all.
        globals()["shutil"].which = lambda _n: None
        assert nvidia_detail() is None
    finally:
        globals()["_run"] = old_run3
        globals()["shutil"].which = old_which

    # The real machine, if it has one: never raises, and reports either
    # None or a list of well-shaped entries.
    real = nvidia_detail()
    assert real is None or isinstance(real, list), real
    for entry in real or []:
        assert isinstance(entry["index"], int), entry
        cap = entry["compute_capability"]
        assert cap is None or (isinstance(cap, tuple) and len(cap) == 2), entry

    print("hearth-hw self-test OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv or len(sys.argv) == 1:
        sys.exit(_self_test())
    print(json.dumps(probe(), indent=2))
