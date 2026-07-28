#!/usr/bin/env python3
r"""hearth first-run setup diagnosis: why a new user cannot just start working.

Hearth's promise is download, open, start working with a local model. Today
the engine assumes Ollama is installed, running, and has a model pulled. If
any of that is false the user gets an obscure connection error from deep
inside hearth_loop, not a sentence they can act on. This module is the
difference between "something went wrong" and "Ollama is installed but not
running; start it and I will continue."

## Check order

Checks run in dependency order, and diagnose() stops at the first one that
is actually wrong, rather than piling up a wall of downstream failures a
user cannot act on anyway (there is no point reporting "no models pulled"
to someone whose Ollama binary does not exist at all):

  1. installed  - is the Ollama binary present at all (PATH, then the
                   platform's known install locations)?
  2. running    - is it reachable over HTTP? An installed-but-stopped
                   service is the single most common state and the
                   cheapest to fix, so this is checked before anything
                   that requires talking to the server.
  3. version    - what does it report? Some behaviour this project depends
                   on (structured tool calls, /api/ps, /api/show
                   capabilities) is version-sensitive. See
                   MIN_SUPPORTED_VERSION below for exactly how confident
                   that floor is.
  4. models     - is anything pulled? A running Ollama with an empty model
                   list is a distinct, very common state, not a subset of
                   "not running".
  5. fit        - does the machine actually run the pulled model well?
                   Reuses hearth_hw (hardware detection) and hearth_shop
                   (fit verdicts) rather than reimplementing that
                   arithmetic here.
  6. disk       - only relevant when step 4 finds nothing pulled: is there
                   room to pull one? Folded into the "no models" finding's
                   remedy (via hearth_pull's disk helpers) rather than
                   being its own stopping point, since it only matters in
                   service of that one remedy.

Detection at startup uses `GET /api/version` with a short timeout, never a
shelled-out `ollama --version`: with no server running, the CLI prints
warning lines to stderr and no clean version line on stdout, which is a
worse signal than a plain connection failure.

## Honesty, not optimism

Every finding is one of three finding-level states: ok, problem, or
unknown. "Unknown" is a real, first-class outcome here, not a fallback for
lazy error handling: if a check cannot run (a malformed response, a model
not present in Hearth's small catalog so its fit cannot be graded), the
finding says so explicitly rather than defaulting to "ok". A false "all
good" that leads into an obscure failure two steps later is exactly the
experience this module exists to prevent - see hearth_hw and hearth_shop's
own stance on the same tradeoff.

## Constraints

Standard library only, Python 3.12+. Every external call (subprocess-free
here; only urllib.request over HTTP and local filesystem checks) is
bounded by a timeout and degrades to "unknown", never raises out of
diagnose() or quick_check(). No writes, no installing anything, no side
effects: this module diagnoses, it does not fix. Reuses hearth_hw.probe()
for hardware, hearth_shop.verdict_for()/recommend() for fit verdicts, and
hearth_pull.free_space_for_models()/model_store_dir() for disk - none of
that arithmetic is reimplemented here.
"""

import argparse
import json
import os
import platform
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_hw  # noqa: E402
import hearth_pull  # noqa: E402
import hearth_shop  # noqa: E402

DEFAULT_OLLAMA = "http://127.0.0.1:11434"

# Bounded timeouts for every network call this module makes. Short: this
# runs at app startup and possibly on a manual retry, never in a background
# loop, so a hung Ollama process must not stall the UI for long.
VERSION_TIMEOUT_SECONDS = 2
TAGS_TIMEOUT_SECONDS = 5
QUICK_TIMEOUT_SECONDS = 2

# The floor below which Ollama is not expected to work with this project.
# Recalled as the release that introduced tool/function-calling support
# (Ollama 0.3.x), which the agent loop depends on - not independently
# reverified against Ollama's own changelog for this branch. This is the
# same open question the project's own design doc flags (Ollama minimum
# supported version, confidence: low): if this floor is ever found to be
# wrong, only the version comparison below is affected, no other check in
# this module depends on the exact number.
MIN_SUPPORTED_VERSION = "0.3.0"

# -- overall diagnosis states, for a UI to branch on --------------------------

STATUS_READY = "ready"
STATUS_NOT_INSTALLED = "not_installed"
STATUS_NOT_RUNNING = "not_running"
STATUS_VERSION_TOO_OLD = "version_too_old"
STATUS_NO_MODELS = "no_models"
STATUS_MODEL_UNSUITABLE = "model_unsuitable"
STATUS_UNKNOWN = "unknown"

# -- per-finding states -------------------------------------------------------

FINDING_OK = "ok"
FINDING_PROBLEM = "problem"
FINDING_UNKNOWN = "unknown"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _human_bytes(n):
    """Short human-readable rendering of a byte count, or "an unknown
    amount" for None. Mirrors hearth_pull._human_bytes; not imported from
    there so this module's own arithmetic stays self-contained, same
    reasoning hearth_bench gives for duplicating DISK_BUFFER_BYTES rather
    than importing it.
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
# Chokepoints: every network / filesystem-lookup call this module makes
# goes through one of these, so the self-test can replace them with
# fixtures and exercise the ordering and remedy logic without a real
# Ollama, a GPU, or a network connection. Same pattern as hearth_hw.py's
# _run and hearth_bench.py's _http_post/_http_get.
# --------------------------------------------------------------------------

def _which(name):
    return shutil.which(name)


def _isfile(path):
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _http_get_json(url, timeout):
    """GET url, return the parsed JSON body. Raises on any failure
    whatsoever; callers decide how to degrade (see _probe_ollama and
    _list_models, the only two callers).
    """
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------
# 1. Installed?
# --------------------------------------------------------------------------

def _candidate_install_paths():
    """Platform-specific locations to check when the binary is not on PATH.
    Windows and Linux only - the two platforms this project targets - and
    honest about the difference: they do not share an install layout.
    """
    system = platform.system()
    candidates = []
    if system == "Windows":
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            candidates.append(os.path.join(localappdata, "Programs", "Ollama", "ollama.exe"))
    elif system == "Linux":
        candidates.extend(["/usr/local/bin/ollama", "/usr/bin/ollama"])
    return candidates


def detect_installed():
    """Is the Ollama binary present on this machine at all?

    Checks PATH first, then the platform's known install locations (see
    _candidate_install_paths) rather than assuming PATH is authoritative -
    the Windows installer's PATH registration has been unreliable enough
    in the wild that "not on PATH" is not the same fact as "not
    installed". Never raises: shutil.which and a guarded os.path.isfile
    are the only operations here, both safe by construction.

    Returns {"installed": bool, "path": str or None, "source": "path" |
    "known_location" | None}.
    """
    exe = _which("ollama")
    if exe:
        return {"installed": True, "path": exe, "source": "path"}
    for candidate in _candidate_install_paths():
        if _isfile(candidate):
            return {"installed": True, "path": candidate, "source": "known_location"}
    return {"installed": False, "path": None, "source": None}


def _install_remedy():
    system = platform.system()
    if system == "Windows":
        return (
            "Ollama is not installed. Download the Windows installer from "
            "ollama.com/download and run it, then relaunch this app. If you "
            "already installed it, check whether ollama.exe exists under "
            "%LOCALAPPDATA%\\Programs\\Ollama."
        )
    if system == "Linux":
        return (
            "Ollama is not installed. See ollama.com/download for the install "
            "instructions for this distribution, then relaunch this app."
        )
    return (
        "Ollama is not installed. See ollama.com/download for install "
        "instructions for this platform, then relaunch this app."
    )


# --------------------------------------------------------------------------
# 2 & 3. Running, and what version? One HTTP call answers both.
# --------------------------------------------------------------------------

def _probe_ollama(base_url, timeout=VERSION_TIMEOUT_SECONDS):
    """GET base_url/api/version. Never raises: any failure (connection
    refused, DNS failure, timeout, malformed body) comes back as
    reachable: False with error explaining what happened.

    Deliberately does not shell out to `ollama --version`: with no server
    running that CLI path prints warning lines and produces no clean
    version string on stdout, a strictly worse signal than a plain HTTP
    connection failure.
    """
    url = base_url.rstrip("/") + "/api/version"
    try:
        data = _http_get_json(url, timeout)
    except Exception as exc:  # noqa: BLE001 - every failure degrades the same way
        return {"reachable": False, "version": None, "error": "{}: {}".format(type(exc).__name__, exc)}
    version = None
    if isinstance(data, dict):
        v = data.get("version")
        if isinstance(v, str) and v:
            version = v
    return {"reachable": True, "version": version, "error": None}


def _start_remedy():
    system = platform.system()
    if system == "Windows":
        return (
            "Ollama is installed but not running. Start it from the Start "
            "menu (search for \"Ollama\"), or open a terminal and run: "
            "ollama serve"
        )
    if system == "Linux":
        return (
            "Ollama is installed but not running. If it was installed as a "
            "systemd service, start it with: sudo systemctl start ollama "
            "-- otherwise run: ollama serve"
        )
    return "Ollama is installed but not running. Start the Ollama application, or run: ollama serve"


def _version_tuple(v):
    """Parse a version string into a comparable tuple of ints, e.g.
    "0.3.10" -> (0, 3, 10), "v0.4.0-rc1" -> (0, 4, 0). None if nothing
    numeric could be extracted at all.
    """
    if not v or not isinstance(v, str):
        return None
    core = v.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    nums = []
    for part in core.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        nums.append(int(digits))
    return tuple(nums) if nums else None


def _version_at_least(version, floor):
    """True/False if both parse cleanly, None (genuinely unknown) if
    either does not - never raises, never guesses.
    """
    v = _version_tuple(version)
    f = _version_tuple(floor)
    if v is None or f is None:
        return None
    return v >= f


def _version_remedy(version):
    return (
        "Ollama {} is older than the version this app expects (>= {}). "
        "Update Ollama to the latest release.".format(version, MIN_SUPPORTED_VERSION)
    )


# --------------------------------------------------------------------------
# 4. Models pulled?
# --------------------------------------------------------------------------

def _list_models(base_url, timeout=TAGS_TIMEOUT_SECONDS):
    """GET base_url/api/tags -> the "models" list, or None on any failure
    (unreachable, malformed body, unexpected shape). None is distinct
    from an empty list: None means "could not ask", [] means "asked, got
    nothing pulled".
    """
    url = base_url.rstrip("/") + "/api/tags"
    try:
        data = _http_get_json(url, timeout)
    except Exception:  # noqa: BLE001 - degrade to "could not ask", never raise
        return None
    if not isinstance(data, dict):
        return None
    models = data.get("models")
    return models if isinstance(models, list) else None


def _disk_report(disk):
    """A human-readable clause describing whether there is room to pull
    at least the smallest catalog model, or that free space could not be
    determined - never a guessed figure.
    """
    free = disk.get("free_bytes")
    store = disk.get("store_dir")
    if free is None:
        return "free disk space at {} could not be determined".format(store or "the model store")
    smallest = min(hearth_shop.CATALOG, key=lambda m: m["download_bytes"])
    needed = smallest["download_bytes"] + hearth_pull.DISK_BUFFER_BYTES
    if free < needed:
        return (
            "only {} free at {} -- not enough for even the smallest catalog "
            "model ({}, about {})"
        ).format(_human_bytes(free), store, smallest["label"], _human_bytes(smallest["download_bytes"]))
    return "{} free at {}, enough room to pull a model".format(_human_bytes(free), store)


def _no_models_remedy(disk):
    return (
        "No models are pulled yet. Pull one to get started, for example: "
        "ollama pull qwen2.5-coder:7b-instruct-q4_K_M ({}).".format(_disk_report(disk))
    )


# --------------------------------------------------------------------------
# 5. Does a pulled model actually fit this hardware?
# --------------------------------------------------------------------------

def _match_catalog(model_name):
    """Match a /api/tags model name against hearth_shop.CATALOG: exact id
    first, then the part before the colon on both sides (a bare pull name
    should match a tagged catalog entry and vice versa) - same approach
    hearth_bench._find_ps_entry and hearth_pull._find_tag_entry already
    use for the equivalent problem against different endpoints.
    """
    for entry in hearth_shop.CATALOG:
        if entry["id"] == model_name:
            return entry
    base = model_name.split(":", 1)[0]
    for entry in hearth_shop.CATALOG:
        if entry["id"].split(":", 1)[0] == base:
            return entry
    return None


def _best_pulled_model_verdict(models, hw):
    """Among the models Ollama reports as pulled, find every one that
    matches a Hearth catalog entry, grade each with hearth_shop.verdict_for
    against `hw`, and return the best-ranked (name, catalog_entry, verdict)
    triple - or None if none of the pulled models are in the catalog at
    all, which this module cannot grade and must not guess about.
    """
    matched = []
    for entry in models:
        name = entry.get("name") or entry.get("model")
        if not isinstance(name, str) or not name:
            continue
        cat = _match_catalog(name)
        if cat is None:
            continue
        verdict = hearth_shop.verdict_for(cat, hw)
        matched.append((name, cat, verdict))
    if not matched:
        return None
    matched.sort(key=lambda t: hearth_shop.VERDICT_RANK[t[2]["verdict"]])
    return matched[0]


def _fit_remedy(hw):
    rec = hearth_shop.recommend(hw)
    if not rec:
        return (
            "The pulled model does not fit this machine, and nothing in "
            "Hearth's catalog fits either. This hardware may not be able to "
            "run a local coding model comfortably."
        )
    best = rec[0]
    return "Consider pulling a lighter model instead, for example: ollama pull {} ({}).".format(
        best["id"], best["label"],
    )


# --------------------------------------------------------------------------
# Finding / result shaping
# --------------------------------------------------------------------------

def _finding(check, status, message, remedy=None, detail=None):
    return {"check": check, "status": status, "message": message, "remedy": remedy, "detail": detail}


def _result(status, findings, base_url):
    """Assemble diagnose()'s return value. next_action is derived from the
    findings list itself (the last "problem" finding, if any) rather than
    threaded through separately, so it can never drift out of sync with
    what findings actually says.
    """
    next_action = None
    for f in reversed(findings):
        if f["status"] == FINDING_PROBLEM:
            next_action = {"message": f["message"], "remedy": f.get("remedy")}
            break
    return {
        "status": status,
        "healthy": status == STATUS_READY and next_action is None,
        "platform": platform.system(),
        "base_url": base_url,
        "timestamp": _now_iso(),
        "findings": findings,
        "next_action": next_action,
    }


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

def diagnose(base_url=None, hw=None):
    """Diagnose the local environment's readiness to run Hearth's local
    model, in dependency order, stopping at the first real problem (see
    the module docstring's "Check order" section).

    hw=None defers to hearth_hw.probe() (pure local detection, no
    network), and is only invoked at all if execution reaches the fit
    check (step 5) - the earlier steps never touch hardware detection.
    Pass an explicit hw dict to stay hermetic in a test or to reuse an
    already-probed reading.

    Always returns a dict, never raises:
      status       - one of the STATUS_* constants.
      healthy      - True only when status is STATUS_READY and no finding
                     carries a "problem".
      platform     - platform.system().
      base_url     - the Ollama URL this diagnosis was run against.
      timestamp    - ISO 8601 UTC.
      findings     - ordered list of {check, status, message, remedy,
                     detail} dicts, in the order each check actually ran.
                     Stops (does not include later checks) once a
                     "problem" finding is appended, except where a later
                     check exists purely to inform an earlier finding's
                     remedy (disk space folded into the "no models"
                     finding's message, per the module docstring).
      next_action  - {message, remedy} for the single most important
                     thing to do next, or None when nothing needs doing.
    """
    base_url = base_url or DEFAULT_OLLAMA
    findings = []

    # 1. installed?
    install = detect_installed()
    if not install["installed"]:
        findings.append(_finding(
            "installed", FINDING_PROBLEM,
            "Ollama does not appear to be installed on this machine.",
            _install_remedy(), detail=install,
        ))
        return _result(STATUS_NOT_INSTALLED, findings, base_url)
    findings.append(_finding(
        "installed", FINDING_OK,
        "Ollama is installed" + (" at {}".format(install["path"]) if install["path"] else "") + ".",
        detail=install,
    ))

    # 2. running / reachable?
    probe = _probe_ollama(base_url)
    if not probe["reachable"]:
        findings.append(_finding(
            "running", FINDING_PROBLEM,
            "Ollama is installed but not reachable at {} ({}).".format(base_url, probe["error"]),
            _start_remedy(), detail=probe,
        ))
        return _result(STATUS_NOT_RUNNING, findings, base_url)
    findings.append(_finding(
        "running", FINDING_OK,
        "Ollama is running and reachable at {}.".format(base_url), detail=probe,
    ))

    # 3. version - informational unless it is below the floor.
    version = probe["version"]
    if version is None:
        findings.append(_finding(
            "version", FINDING_UNKNOWN,
            "Ollama is running, but its version could not be determined from "
            "/api/version's response.",
        ))
    else:
        at_least = _version_at_least(version, MIN_SUPPORTED_VERSION)
        if at_least is False:
            findings.append(_finding(
                "version", FINDING_PROBLEM,
                "Ollama {} is older than the minimum supported version ({}).".format(
                    version, MIN_SUPPORTED_VERSION,
                ),
                _version_remedy(version), detail={"version": version},
            ))
            return _result(STATUS_VERSION_TOO_OLD, findings, base_url)
        elif at_least is None:
            findings.append(_finding(
                "version", FINDING_UNKNOWN,
                "Ollama reported a version ('{}') that could not be parsed for "
                "comparison.".format(version), detail={"version": version},
            ))
        else:
            findings.append(_finding(
                "version", FINDING_OK,
                "Ollama version {} meets the minimum ({}).".format(version, MIN_SUPPORTED_VERSION),
                detail={"version": version},
            ))

    # 4. models pulled?
    models = _list_models(base_url)
    if models is None:
        findings.append(_finding(
            "models_pulled", FINDING_PROBLEM,
            "Ollama is reachable, but its model list could not be retrieved. "
            "It may be busy or restarting.",
            "Wait a moment and try again.",
        ))
        return _result(STATUS_UNKNOWN, findings, base_url)
    if len(models) == 0:
        disk = {
            "free_bytes": hearth_pull.free_space_for_models(),
            "store_dir": hearth_pull.model_store_dir(),
        }
        findings.append(_finding(
            "models_pulled", FINDING_PROBLEM,
            "Ollama is running, but no models are pulled yet.",
            _no_models_remedy(disk), detail={"disk": disk},
        ))
        return _result(STATUS_NO_MODELS, findings, base_url)
    findings.append(_finding(
        "models_pulled", FINDING_OK,
        "{} model(s) pulled.".format(len(models)),
        detail={"models": [m.get("name") or m.get("model") for m in models]},
    ))

    # 5. does a pulled model actually fit this hardware?
    hw = hw if hw is not None else hearth_hw.probe()
    best = _best_pulled_model_verdict(models, hw)
    if best is None:
        findings.append(_finding(
            "model_fit", FINDING_UNKNOWN,
            "None of the pulled models are in Hearth's catalog, so this app "
            "cannot judge whether they fit this machine's hardware.",
        ))
        return _result(STATUS_READY, findings, base_url)

    name, _cat, verdict = best
    if verdict["verdict"] == hearth_shop.VERDICT_WONT_FIT:
        findings.append(_finding(
            "model_fit", FINDING_PROBLEM,
            "The best-fitting pulled model ({}) does not fit this machine: {}".format(
                name, verdict["message"],
            ),
            _fit_remedy(hw), detail=verdict,
        ))
        return _result(STATUS_MODEL_UNSUITABLE, findings, base_url)

    findings.append(_finding(
        "model_fit", FINDING_OK,
        "{} runs on this hardware ({}). {}".format(name, verdict["verdict"], verdict["message"]),
        detail=verdict,
    ))
    return _result(STATUS_READY, findings, base_url)


def quick_check(base_url=None):
    """A cheap "can I proceed" gate: is Ollama reachable and does it have
    at least one model pulled? One bounded HTTP call, no hardware probing,
    no filesystem checks. Never raises; any failure reads as False.

    Deliberately coarser than diagnose(): this does not distinguish "not
    installed" from "not running" from "installed, running, no models" -
    it only answers the one question a caller needs before attempting a
    chat turn. Call diagnose() for the reason why, when quick_check()
    returns False.
    """
    models = _list_models(base_url or DEFAULT_OLLAMA, timeout=QUICK_TIMEOUT_SECONDS)
    return bool(models)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def format_report(result):
    lines = ["hearth setup diagnosis ({})".format(result["platform"])]
    symbol = {FINDING_OK: "OK  ", FINDING_PROBLEM: "FAIL", FINDING_UNKNOWN: "?   "}
    for f in result["findings"]:
        lines.append("  [{}] {}: {}".format(symbol.get(f["status"], "?"), f["check"], f["message"]))
        if f["status"] != FINDING_OK and f.get("remedy"):
            lines.append("        -> {}".format(f["remedy"]))
    lines.append("status: {}".format(result["status"].upper()))
    if result["next_action"]:
        lines.append("next: {}".format(result["next_action"]["remedy"] or result["next_action"]["message"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hearth-setup",
        description="Diagnose whether this machine can run Hearth's local model, and what to do about it.",
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--base-url", default=DEFAULT_OLLAMA)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a report")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("diagnose", help="full diagnosis (default)")
    sub.add_parser("quick", help="cheap reachable+has-models check only")

    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if args.command == "quick":
        ok = quick_check(args.base_url)
        print("yes" if ok else "no")
        return 0 if ok else 1

    result = diagnose(args.base_url)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_report(result))
    return 0 if result["healthy"] else 1


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _self_test():
    global detect_installed, _which, _isfile, _http_get_json

    orig_detect_installed = detect_installed
    orig_which = _which
    orig_isfile = _isfile
    orig_get_json = _http_get_json

    try:
        # -- _version_tuple / _version_at_least ------------------------------
        assert _version_tuple("0.3.10") == (0, 3, 10)
        assert _version_tuple("v0.4.0-rc1") == (0, 4, 0)
        assert _version_tuple("0.1") == (0, 1)
        assert _version_tuple("") is None
        assert _version_tuple(None) is None
        assert _version_tuple("nightly") is None
        assert _version_at_least("0.3.0", "0.3.0") is True
        assert _version_at_least("0.3.1", "0.3.0") is True
        assert _version_at_least("0.2.9", "0.3.0") is False
        assert _version_at_least("nightly", "0.3.0") is None
        assert _version_at_least("0.3.0", None) is None

        # -- _match_catalog: exact id, then base-name match ------------------
        exact = _match_catalog("qwen2.5-coder:7b-instruct-q4_K_M")
        assert exact is not None and exact["id"] == "qwen2.5-coder:7b-instruct-q4_K_M", exact
        # Same base name, different tag -> matches by base name (loose on
        # purpose: a bare pull name should match a tagged catalog entry and
        # vice versa, same convention hearth_pull._find_tag_entry and
        # hearth_bench._find_ps_entry already use).
        by_base = _match_catalog("mistral:latest")
        assert by_base is not None and by_base["id"].startswith("mistral:"), by_base
        # A base name that appears nowhere in the catalog must not match.
        assert _match_catalog("totally-unknown-model:latest") is None

        # -- detect_installed(): on PATH -------------------------------------
        _which = lambda name: r"C:\fake\ollama.exe" if name == "ollama" else None
        _isfile = lambda p: False
        r = detect_installed()
        assert r == {"installed": True, "path": r"C:\fake\ollama.exe", "source": "path"}, r

        # -- detect_installed(): not on PATH, but at a known location --------
        _which = lambda name: None
        seen_paths = []

        def fake_isfile(p):
            seen_paths.append(p)
            return "Programs" in p or "Ollama" in p

        _isfile = fake_isfile
        old_platform_system = platform.system
        try:
            platform.system = lambda: "Windows"
            old_env = os.environ.get("LOCALAPPDATA")
            os.environ["LOCALAPPDATA"] = r"C:\Users\test\AppData\Local"
            r = detect_installed()
            assert r["installed"] is True, r
            assert r["source"] == "known_location", r
            assert "Programs" in r["path"] and "Ollama" in r["path"], r
        finally:
            platform.system = old_platform_system
            if old_env is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old_env

        # -- detect_installed(): nowhere at all -------------------------------
        _which = lambda name: None
        _isfile = lambda p: False
        r = detect_installed()
        assert r == {"installed": False, "path": None, "source": None}, r

        _which = orig_which
        _isfile = orig_isfile

        # -- _probe_ollama(): stubbed HTTP -----------------------------------
        _http_get_json = lambda url, timeout: {"version": "0.3.14"}
        p = _probe_ollama("http://x:11434")
        assert p == {"reachable": True, "version": "0.3.14", "error": None}, p

        def boom(url, timeout):
            raise ConnectionRefusedError("nobody home")
        _http_get_json = boom
        p = _probe_ollama("http://x:11434")
        assert p["reachable"] is False, p
        assert "connectionrefusederror" in p["error"].lower(), p

        _http_get_json = lambda url, timeout: {"no_version_key": True}
        p = _probe_ollama("http://x:11434")
        assert p == {"reachable": True, "version": None, "error": None}, p

        # -- _list_models(): stubbed HTTP -------------------------------------
        _http_get_json = lambda url, timeout: {"models": [{"name": "a:latest"}, {"name": "b:latest"}]}
        assert len(_list_models("http://x:11434")) == 2

        _http_get_json = lambda url, timeout: {"models": []}
        assert _list_models("http://x:11434") == []

        _http_get_json = lambda url, timeout: {"unexpected": "shape"}
        assert _list_models("http://x:11434") is None

        def boom2(url, timeout):
            raise TimeoutError("slow")
        _http_get_json = boom2
        assert _list_models("http://x:11434") is None

        _http_get_json = orig_get_json

        # -- synthetic hardware, matching hearth_shop's self-test fixtures ---
        def _hw(vram_bytes, ram_bytes, vendor="nvidia", platform_name="synthetic"):
            gpus = [{"name": "synthetic", "vram_bytes": vram_bytes, "vendor": vendor,
                     "approximate": False}] if vram_bytes else []
            return {"platform": platform_name, "gpus": gpus, "system_ram_bytes": ram_bytes, "cpu_count": 8}

        hw_strong = _hw(24 * 1024 ** 3, 64 * 1024 ** 3)   # comfortably runs everything in CATALOG
        hw_weak = _hw(0, 512 * 1024 ** 2)                 # no GPU, starved RAM: nothing fits
        hw_small_gpu = _hw(6_442_450_944, 16 * 1024 ** 3)  # 6GB card: 32B won't fit, 7B does

        # ==================================================================
        # diagnose(): full end-to-end scenarios, each stubbing only what
        # that scenario needs. detect_installed and _http_get_json are the
        # only two chokepoints diagnose() ever touches directly (plus the
        # hw= parameter, injected explicitly rather than monkeypatching
        # hearth_hw, so the fit check stays hermetic too).
        # ==================================================================

        # -- A: not installed -> stops at check 1, exactly one finding -------
        detect_installed = lambda: {"installed": False, "path": None, "source": None}
        r = diagnose("http://x:11434", hw=hw_strong)
        assert r["status"] == STATUS_NOT_INSTALLED, r
        assert r["healthy"] is False, r
        assert len(r["findings"]) == 1, r
        assert r["findings"][0]["check"] == "installed", r
        assert r["findings"][0]["status"] == FINDING_PROBLEM, r
        assert r["next_action"] is not None and "installed" in r["next_action"]["message"].lower(), r
        assert "ollama.com/download" in r["next_action"]["remedy"], r

        # -- THE ORDERING PROOF ------------------------------------------------
        # Not installed, but EVERYTHING downstream is stubbed to look
        # perfect: a recent version, a pulled model that fits great on
        # hw_strong. If diagnose() failed to stop at check 1 - the actual
        # bug class this ordering exists to prevent - this would silently
        # report STATUS_READY with 5 findings instead of STATUS_NOT_INSTALLED
        # with 1. This assertion is the one that must fail if the early
        # `return` after check 1 is ever removed; see the iteration report
        # for the before/after transcript proving that.
        detect_installed = lambda: {"installed": False, "path": None, "source": None}

        def fake_all_good(url, timeout):
            if url.endswith("/api/version"):
                return {"version": "9.9.9"}
            if url.endswith("/api/tags"):
                return {"models": [{"name": "qwen2.5-coder:7b-instruct-q4_K_M"}]}
            raise AssertionError("unexpected URL: " + url)

        _http_get_json = fake_all_good
        r = diagnose("http://x:11434", hw=hw_strong)
        assert r["status"] == STATUS_NOT_INSTALLED, (
            "a machine with no Ollama binary must be reported as not_installed, "
            "never masked by a downstream check that happens to look fine", r,
        )
        assert r["status"] != STATUS_READY, r
        assert len(r["findings"]) == 1, (
            "diagnose() must stop at the first real problem, not run every "
            "check regardless", r,
        )
        _http_get_json = orig_get_json

        # -- B: installed, not reachable -> stops at check 2 ------------------
        detect_installed = lambda: {"installed": True, "path": "/usr/bin/ollama", "source": "path"}

        def refused(url, timeout):
            raise ConnectionRefusedError("nobody home")
        _http_get_json = refused
        r = diagnose("http://x:11434", hw=hw_strong)
        assert r["status"] == STATUS_NOT_RUNNING, r
        assert len(r["findings"]) == 2, r
        assert [f["check"] for f in r["findings"]] == ["installed", "running"], r
        assert r["findings"][0]["status"] == FINDING_OK, r
        assert r["findings"][1]["status"] == FINDING_PROBLEM, r
        assert "not reachable" in r["next_action"]["message"].lower(), r
        assert "ollama serve" in r["next_action"]["remedy"] or "start" in r["next_action"]["remedy"].lower(), r
        _http_get_json = orig_get_json

        # -- C: installed, reachable, version too old -> stops at check 3 ----
        def old_version(url, timeout):
            assert url.endswith("/api/version"), url
            return {"version": "0.1.5"}
        _http_get_json = old_version
        r = diagnose("http://x:11434", hw=hw_strong)
        assert r["status"] == STATUS_VERSION_TOO_OLD, r
        assert len(r["findings"]) == 3, r
        assert [f["check"] for f in r["findings"]] == ["installed", "running", "version"], r
        assert "0.1.5" in r["findings"][2]["message"], r
        assert "update ollama" in r["next_action"]["remedy"].lower(), r
        _http_get_json = orig_get_json

        # -- D: reachable, current version, no models -> stops at check 4 ----
        def no_models(url, timeout):
            if url.endswith("/api/version"):
                return {"version": "0.5.0"}
            if url.endswith("/api/tags"):
                return {"models": []}
            raise AssertionError(url)
        _http_get_json = no_models
        old_free = hearth_pull.free_space_for_models
        old_store = hearth_pull.model_store_dir
        try:
            hearth_pull.free_space_for_models = lambda: 200 * 1024 ** 3
            hearth_pull.model_store_dir = lambda: "/home/test/.ollama/models"
            r = diagnose("http://x:11434", hw=hw_strong)
            assert r["status"] == STATUS_NO_MODELS, r
            assert len(r["findings"]) == 4, r
            assert [f["check"] for f in r["findings"]] == ["installed", "running", "version", "models_pulled"], r
            assert "ollama pull" in r["next_action"]["remedy"], r
            assert "enough room" in r["next_action"]["remedy"], r

            # Same scenario, but disk is too small: the remedy must say so
            # rather than cheerfully suggesting a pull that will fail.
            hearth_pull.free_space_for_models = lambda: 10 * 1024 * 1024  # 10MB
            r_tight = diagnose("http://x:11434", hw=hw_strong)
            assert r_tight["status"] == STATUS_NO_MODELS, r_tight
            assert "not enough" in r_tight["next_action"]["remedy"], r_tight

            # Disk itself unknown: honest about that too, never a fabricated figure.
            hearth_pull.free_space_for_models = lambda: None
            r_unk_disk = diagnose("http://x:11434", hw=hw_strong)
            assert "could not be determined" in r_unk_disk["next_action"]["remedy"], r_unk_disk
        finally:
            hearth_pull.free_space_for_models = old_free
            hearth_pull.model_store_dir = old_store
        _http_get_json = orig_get_json

        # -- E: models pulled, none in the catalog -> READY, fit unknown -----
        def unknown_model(url, timeout):
            if url.endswith("/api/version"):
                return {"version": "0.5.0"}
            if url.endswith("/api/tags"):
                return {"models": [{"name": "some-custom-finetune:latest"}]}
            raise AssertionError(url)
        _http_get_json = unknown_model
        r = diagnose("http://x:11434", hw=hw_strong)
        assert r["status"] == STATUS_READY, r
        assert len(r["findings"]) == 5, r
        assert r["findings"][4]["check"] == "model_fit", r
        assert r["findings"][4]["status"] == FINDING_UNKNOWN, r
        assert r["next_action"] is None, (
            "an unverifiable (not a hard problem) fit finding must not "
            "manufacture a next_action", r,
        )
        _http_get_json = orig_get_json

        # -- F: a pulled catalog model that does not fit this hardware -------
        def wont_fit_model(url, timeout):
            if url.endswith("/api/version"):
                return {"version": "0.5.0"}
            if url.endswith("/api/tags"):
                return {"models": [{"name": "qwen2.5-coder:32b-instruct-q4_K_M"}]}
            raise AssertionError(url)
        _http_get_json = wont_fit_model
        r = diagnose("http://x:11434", hw=hw_small_gpu)
        assert r["status"] == STATUS_MODEL_UNSUITABLE, r
        assert len(r["findings"]) == 5, r
        assert r["findings"][4]["status"] == FINDING_PROBLEM, r
        assert "ollama pull" in r["next_action"]["remedy"], r

        # Same too-big-a-model scenario, but on hardware so weak nothing in
        # the catalog fits at all: the remedy must say so honestly rather
        # than suggest a pull that would also fail.
        r_nothing_fits = diagnose("http://x:11434", hw=hw_weak)
        assert r_nothing_fits["status"] == STATUS_MODEL_UNSUITABLE, r_nothing_fits
        assert "ollama pull" not in r_nothing_fits["next_action"]["remedy"], r_nothing_fits
        assert "nothing in hearth" in r_nothing_fits["next_action"]["remedy"].lower(), r_nothing_fits
        _http_get_json = orig_get_json

        # -- G: everything genuinely fine -> READY, healthy ------------------
        def all_good(url, timeout):
            if url.endswith("/api/version"):
                return {"version": "0.5.0"}
            if url.endswith("/api/tags"):
                return {"models": [{"name": "qwen2.5-coder:7b-instruct-q4_K_M"}]}
            raise AssertionError(url)
        _http_get_json = all_good
        r = diagnose("http://x:11434", hw=hw_strong)
        assert r["status"] == STATUS_READY, r
        assert r["healthy"] is True, r
        assert r["next_action"] is None, r
        assert len(r["findings"]) == 5, r
        assert all(f["status"] == FINDING_OK for f in r["findings"]), r
        _http_get_json = orig_get_json

        # -- H: /api/tags fails after a successful /api/version -> unknown ---
        def flaky_tags(url, timeout):
            if url.endswith("/api/version"):
                return {"version": "0.5.0"}
            raise TimeoutError("server busy")
        _http_get_json = flaky_tags
        r = diagnose("http://x:11434", hw=hw_strong)
        assert r["status"] == STATUS_UNKNOWN, r
        assert len(r["findings"]) == 4, r
        assert r["findings"][3]["check"] == "models_pulled", r
        assert r["next_action"] is not None, r
        _http_get_json = orig_get_json

        detect_installed = orig_detect_installed

        # -- quick_check(): stubbed --------------------------------------------
        _http_get_json = lambda url, timeout: {"models": [{"name": "a"}]}
        assert quick_check("http://x:11434") is True

        _http_get_json = lambda url, timeout: {"models": []}
        assert quick_check("http://x:11434") is False

        def refused2(url, timeout):
            raise ConnectionRefusedError("nobody home")
        _http_get_json = refused2
        assert quick_check("http://x:11434") is False
        _http_get_json = orig_get_json

        # -- format_report(): shape sanity, not a golden-text pin -------------
        _http_get_json = all_good
        detect_installed = lambda: {"installed": True, "path": "/usr/bin/ollama", "source": "path"}
        rep = format_report(diagnose("http://x:11434", hw=hw_strong))
        assert "status: READY" in rep, rep
        detect_installed = orig_detect_installed
        _http_get_json = orig_get_json

        # -- next_action is a DICT, and every consumer reads it as one --------
        # The contract is {"message": str, "remedy": str|None} or None, never
        # a pre-rendered sentence. This is pinned here because format_report's
        # own next_action branch was otherwise only ever exercised on the
        # READY path above, where next_action is None -- so a consumer that
        # treated it as a string would not have been caught by anything. The
        # docstrings on diagnose() and _result() state the dict shape; this
        # asserts it, and asserts that the one in-module renderer really does
        # index into it rather than interpolating the whole object.
        detect_installed = lambda: {"installed": False, "path": None, "source": None}
        r_na = diagnose("http://x:11434", hw=hw_strong)
        detect_installed = orig_detect_installed
        assert isinstance(r_na["next_action"], dict), (
            "next_action must be a dict, not a rendered sentence: {!r}".format(r_na["next_action"]))
        assert set(r_na["next_action"]) == {"message", "remedy"}, r_na["next_action"]
        assert isinstance(r_na["next_action"]["message"], str), r_na["next_action"]
        rep_na = format_report(r_na)
        assert r_na["next_action"]["remedy"] in rep_na, rep_na
        assert "{" not in rep_na and "'message'" not in rep_na, (
            "format_report interpolated the next_action dict instead of "
            "indexing into it: {!r}".format(rep_na))

        # ==================================================================
        # Real, unstubbed smoke tests: must never raise regardless of
        # whether THIS machine has Ollama absent, stopped, or running with
        # models - the actual property the task asks this self-test to
        # hold across all three states, not just the fixtures above.
        # ==================================================================
        real_install = detect_installed()
        assert isinstance(real_install, dict) and "installed" in real_install, real_install
        assert isinstance(real_install["installed"], bool), real_install

        real_quick = quick_check()
        assert isinstance(real_quick, bool), real_quick

        real_diag = diagnose()
        assert real_diag["status"] in (
            STATUS_READY, STATUS_NOT_INSTALLED, STATUS_NOT_RUNNING,
            STATUS_VERSION_TOO_OLD, STATUS_NO_MODELS, STATUS_MODEL_UNSUITABLE, STATUS_UNKNOWN,
        ), real_diag
        assert isinstance(real_diag["findings"], list) and len(real_diag["findings"]) >= 1, real_diag
        # Must be trivially JSON-serialisable, since a UI consumes this over IPC.
        encoded = json.dumps(real_diag)
        assert json.loads(encoded)["status"] == real_diag["status"]

        print("hearth-setup self-test OK")
        return 0
    finally:
        detect_installed = orig_detect_installed
        _which = orig_which
        _isfile = orig_isfile
        _http_get_json = orig_get_json


if __name__ == "__main__":
    if "--self-test" in sys.argv or len(sys.argv) == 1:
        sys.exit(_self_test())
    sys.exit(main())
