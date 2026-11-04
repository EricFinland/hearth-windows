#!/usr/bin/env python3
r"""hearth model fingerprint: has the model changed under you?

    python agent/hearth_fingerprint.py --record --model qwen2.5-coder:7b
    python agent/hearth_fingerprint.py --check  --model qwen2.5-coder:7b
    python agent/hearth_fingerprint.py --determinism --model <m> --rounds 5

THE PROBLEM
-----------
A local setup is not a fixed thing. You re-quantize a model, swap a GGUF for
one with the same name, let the engine update, change GPU offload, or move
from the bundled llama.cpp to Ollama. Any of those can change what the model
says while every version string you can see stays the same. An agent whose
behaviour quietly shifted is the worst kind of broken: nothing errors, the
benchmark you ran last month no longer describes the thing you are running,
and you have no way to point at when it moved.

This records a short, fixed set of probes at temperature zero, normalizes the
answers, and hashes them into one short digest. Re-run it later: same digest
means the stack still answers the way it did, a different digest names which
probes moved and shows both answers. It is a tripwire, not a benchmark. It
cannot tell you the model got worse, only that it is no longer the same.

WHY THE PROBE SET IS VERSIONED
------------------------------
The digest covers the prompts as well as the answers, and the record carries
PROBE_VERSION. Two fingerprints taken with different probe sets are not
comparable, and comparing them would invent drift that never happened, so
compare() refuses rather than guessing.

DETERMINISM IS A SEPARATE QUESTION
----------------------------------
Temperature zero is not a promise of a stable answer. llama.cpp output can
shift with batch size, thread count and how many layers sit on the GPU, and
an engine is free to ignore the sampling options it is handed. --determinism
asks the same prompt several times in a row and reports whether this stack is
actually reproducible. Run it before trusting a fingerprint: on a stack that
is not deterministic, a changed digest means nothing.

Generation is injectable (generate_fn), so everything here is testable with
no model, no engine and no network. Standard library only.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RECORD_VERSION = 1
PROBE_VERSION = 1
SEED = 1337  # fixed so an engine that honours a seed has no excuse to wander

# Short, cheap, and each one sensitive to a different kind of drift:
# instruction following, arithmetic, pattern continuation, format fidelity,
# and plain recall. Changing this tuple must bump PROBE_VERSION.
PROBES = (
    "Reply with exactly the word: hearth",
    "What is 17 * 23? Reply with only the number.",
    "Continue with one more item and nothing else: red, orange, yellow,",
    "Reply with this and nothing else: {\"ok\": true}",
    "In one word, what is the capital of France?",
)


def _normalize(text):
    """Collapse the differences that are not drift: surrounding space, run
    length inside the answer, and line-ending flavour. Case is left alone,
    because a model that stopped capitalizing has changed."""
    return re.sub(r"\s+", " ", (text or "").replace("\r\n", "\n")).strip()


def digest(prompts, responses):
    """A short digest over the probes AND their answers. Including the
    prompts means a fingerprint cannot be mistaken for one taken with a
    different probe set."""
    h = hashlib.sha256()
    h.update("hearth-fingerprint/{}\x00".format(PROBE_VERSION).encode("utf-8"))
    for prompt, response in zip(prompts, responses):
        h.update(_normalize(prompt).encode("utf-8"))
        h.update(b"\x00")
        h.update(_normalize(response).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def fingerprint(generate_fn, probes=PROBES, model="", backend="", now=None):
    """Run every probe once and return a record. A probe that raises is
    recorded as an empty answer rather than aborting the run: a stack that
    fails one probe has still told you something, and the digest will differ
    from one where it answered."""
    prompts = tuple(probes)
    responses, failures = [], []
    for prompt in prompts:
        try:
            responses.append(generate_fn(prompt))
        except Exception as exc:  # noqa: BLE001 - a failed probe is data
            responses.append("")
            failures.append({"prompt": prompt, "error": str(exc)})
    return {
        "record_version": RECORD_VERSION,
        "probe_version": PROBE_VERSION,
        "digest": digest(prompts, responses),
        "model": model,
        "backend": backend,
        "recorded_at": (now or datetime.now(timezone.utc)).isoformat(),
        "probes": [{"prompt": p, "response": r} for p, r in zip(prompts, responses)],
        "failures": failures,
    }


def compare(old, new):
    """Compare two records. Returns comparable/same plus the probes that
    moved, each with both answers.

    Records from different probe sets are not comparable and say so: the
    honest answer to "did it drift?" when the question changed is that you
    cannot tell from these two."""
    if not old or not new:
        return {"comparable": False, "reason": "missing record", "same": False, "changed": []}
    if old.get("probe_version") != new.get("probe_version"):
        return {"comparable": False, "same": False, "changed": [],
                "reason": "probe set changed (v{} then v{}): take a new baseline".format(
                    old.get("probe_version"), new.get("probe_version"))}
    old_by_prompt = {p["prompt"]: p.get("response", "") for p in old.get("probes", [])}
    changed = []
    for probe in new.get("probes", []):
        prompt = probe["prompt"]
        if prompt not in old_by_prompt:
            changed.append({"prompt": prompt, "before": None,
                            "after": probe.get("response", "")})
            continue
        before, after = old_by_prompt[prompt], probe.get("response", "")
        if _normalize(before) != _normalize(after):
            changed.append({"prompt": prompt, "before": before, "after": after})
    return {
        "comparable": True,
        "same": not changed and old.get("digest") == new.get("digest"),
        "changed": changed,
        "before_digest": old.get("digest"),
        "after_digest": new.get("digest"),
    }


def determinism(generate_fn, prompt=None, rounds=3):
    """Ask one prompt `rounds` times and report whether the answers agree.

    A stack that fails this is not broken, but a fingerprint taken on it
    cannot distinguish drift from noise, so this is the check that makes the
    other one meaningful."""
    prompt = PROBES[1] if prompt is None else prompt
    rounds = max(2, int(rounds))
    answers = []
    for _ in range(rounds):
        try:
            answers.append(_normalize(generate_fn(prompt)))
        except Exception as exc:  # noqa: BLE001 - a failure is an answer too
            answers.append("<error: {}>".format(exc))
    unique = sorted(set(answers))
    return {
        "prompt": prompt, "rounds": rounds, "answers": answers,
        "unique": len(unique), "deterministic": len(unique) == 1,
        "variants": unique,
    }


def save(record, path):
    """Write a record, creating its directory. Written whole then moved, so
    an interrupted write cannot leave a half-parsed baseline behind."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def load(path):
    """Read a record, or None when there is not one (or it is unreadable)."""
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def default_path(model, data_dir=None):
    """Where a baseline for this model lives. The model name goes through a
    filter because a GGUF path or an Ollama tag contains separators that are
    not legal in a filename."""
    if data_dir is None:
        import hearth_paths
        data_dir = os.path.join(hearth_paths.data_dir(), "fingerprints")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", model or "model").strip("_") or "model"
    return os.path.join(data_dir, safe + ".json")


def live_generate(model, env=None):
    """A generate_fn backed by Hearth's own inference stack, pinned as hard
    as the interface allows. An engine may ignore these options, which is
    what --determinism is for."""
    import hearth_backend
    ref = hearth_backend.ModelRef.parse(model)
    backend = hearth_backend.get_backend(model=model)

    def generate(prompt):
        got = backend.chat(ref, [{"role": "user", "content": prompt}],
                           options={"temperature": 0.0, "top_k": 1, "seed": SEED})
        return ((got or {}).get("message") or {}).get("content") or ""

    return generate, getattr(backend, "name", "") or type(backend).__name__


def format_compare(result):
    if not result.get("comparable"):
        return "cannot compare: {}".format(result.get("reason", "unknown"))
    if result["same"]:
        return "unchanged: {} still answers the same on every probe".format(
            result.get("after_digest"))
    lines = ["CHANGED: {} -> {}".format(result.get("before_digest"),
                                        result.get("after_digest")),
             "", "{} probe(s) moved:".format(len(result["changed"]))]
    for c in result["changed"]:
        lines += ["", "  probe:  {}".format(c["prompt"]),
                  "  before: {!r}".format(c["before"]),
                  "  after:  {!r}".format(c["after"])]
    return "\n".join(lines)


def _self_test():
    import tempfile

    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    answers = {
        PROBES[0]: "hearth", PROBES[1]: "391",
        PROBES[2]: "green", PROBES[3]: '{"ok": true}', PROBES[4]: "Paris",
    }

    def stable(prompt):
        return answers[prompt]

    # _normalize: whitespace and line endings are not drift, case is
    assert _normalize("  a   b \r\n c ") == "a b c"
    assert _normalize(None) == "" and _normalize("") == ""
    assert _normalize("Paris") != _normalize("paris"), "case change is drift"

    # digest: stable, short, and sensitive to any answer changing
    d1 = digest(PROBES, [answers[p] for p in PROBES])
    assert d1 == digest(PROBES, [answers[p] for p in PROBES]), "digest is stable"
    assert len(d1) == 16 and re.fullmatch(r"[0-9a-f]{16}", d1)
    moved = [answers[p] for p in PROBES]
    moved[1] = "392"
    assert digest(PROBES, moved) != d1, "a changed answer changes the digest"
    # whitespace alone does not
    spaced = ["  " + answers[p] + "\n" for p in PROBES]
    assert digest(PROBES, spaced) == d1, "whitespace is not drift"
    # the same answers under different prompts are a different fingerprint
    assert digest(tuple(reversed(PROBES)), [answers[p] for p in PROBES]) != d1

    # fingerprint: one record, every probe, digest matching a direct call
    rec = fingerprint(stable, model="m", backend="b", now=now)
    assert rec["digest"] == d1 and rec["probe_version"] == PROBE_VERSION
    assert len(rec["probes"]) == len(PROBES) and rec["failures"] == []
    assert rec["model"] == "m" and rec["backend"] == "b"
    assert rec["recorded_at"] == now.isoformat()

    # a probe that raises is recorded, not fatal, and shifts the digest
    def flaky(prompt):
        if prompt == PROBES[2]:
            raise RuntimeError("engine hung up")
        return answers[prompt]
    bad = fingerprint(flaky, now=now)
    assert len(bad["failures"]) == 1 and "hung up" in bad["failures"][0]["error"]
    assert bad["digest"] != d1 and len(bad["probes"]) == len(PROBES)

    # compare: same stack is unchanged
    same = compare(rec, fingerprint(stable, now=now))
    assert same["comparable"] and same["same"] and same["changed"] == []
    assert "unchanged" in format_compare(same)

    # compare: a drifted answer is named, with both sides
    def drifted(prompt):
        return "392" if prompt == PROBES[1] else answers[prompt]
    diff = compare(rec, fingerprint(drifted, now=now))
    assert diff["comparable"] and not diff["same"]
    assert len(diff["changed"]) == 1, diff["changed"]
    assert diff["changed"][0]["before"] == "391" and diff["changed"][0]["after"] == "392"
    report = format_compare(diff)
    assert "CHANGED" in report and "391" in report and "392" in report

    # compare: a different probe set refuses rather than inventing drift
    other = dict(rec, probe_version=PROBE_VERSION + 1)
    refused = compare(rec, other)
    assert not refused["comparable"] and not refused["same"]
    assert "probe set changed" in refused["reason"]
    assert "cannot compare" in format_compare(refused)
    assert not compare(None, rec)["comparable"]

    # compare: a probe present now but not then is a change, not a crash
    trimmed = dict(rec, probes=rec["probes"][:2])
    grown = compare(trimmed, rec)
    assert grown["comparable"] and len(grown["changed"]) == len(PROBES) - 2
    assert all(c["before"] is None for c in grown["changed"])

    # determinism: agreement, disagreement, and the floor of two rounds
    det = determinism(stable, rounds=4)
    assert det["deterministic"] and det["unique"] == 1 and det["rounds"] == 4
    assert determinism(stable, rounds=1)["rounds"] == 2, "fewer than two proves nothing"

    seq = iter(["391", "391", "392"])
    wobble = determinism(lambda p: next(seq), rounds=3)
    assert not wobble["deterministic"] and wobble["unique"] == 2
    assert wobble["variants"] == ["391", "392"], wobble["variants"]

    def always_raises(prompt):
        raise RuntimeError("no engine")
    err = determinism(always_raises, rounds=2)
    assert err["deterministic"] and "no engine" in err["answers"][0]

    # save/load round trip, and a missing or corrupt file is None not a crash
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "nested", "m.json")
        save(rec, path)
        assert load(path) == rec
        assert not os.path.exists(path + ".tmp"), "no temp file left behind"
        assert load(os.path.join(d, "absent.json")) is None
        junk = os.path.join(d, "junk.json")
        with open(junk, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert load(junk) is None
        listish = os.path.join(d, "list.json")
        with open(listish, "w", encoding="utf-8") as fh:
            fh.write("[1, 2]")
        assert load(listish) is None, "a list is not a record"

        # default_path: separators in a model name never escape the directory
        for model in ("qwen2.5-coder:7b", "C:\\models\\a b.gguf", "../../etc/passwd", ""):
            p = default_path(model, data_dir=d)
            assert os.path.dirname(os.path.abspath(p)) == os.path.abspath(d), (model, p)
            assert os.path.basename(p).endswith(".json")
        assert default_path("a:b", data_dir=d) == default_path("a_b", data_dir=d)

    print("hearth-fingerprint self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-fingerprint",
                                description="detect a local model changing under you")
    p.add_argument("--model", default="", help="model ref, as hearth names it")
    p.add_argument("--path", default=None, help="baseline file (default: hearth's data dir)")
    p.add_argument("--record", action="store_true", help="take a baseline and write it")
    p.add_argument("--check", action="store_true", help="probe again and diff the baseline")
    p.add_argument("--determinism", action="store_true",
                   help="ask one probe repeatedly; does this stack reproduce?")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not (a.record or a.check or a.determinism):
        p.error("nothing to do (use --record, --check, --determinism or --self-test)")
    if not a.model:
        p.error("--model is required to probe a model")

    generate, backend_name = live_generate(a.model)
    path = a.path or default_path(a.model)

    if a.determinism:
        got = determinism(generate, rounds=a.rounds)
        if a.as_json:
            print(json.dumps(got, indent=2))
        else:
            verdict = "deterministic" if got["deterministic"] else "NOT deterministic"
            print("{} over {} rounds ({} distinct answer(s))".format(
                verdict, got["rounds"], got["unique"]))
            for v in got["variants"]:
                print("  {!r}".format(v))
        return 0 if got["deterministic"] else 1

    record = fingerprint(generate, model=a.model, backend=backend_name)
    if a.record:
        save(record, path)
        print("baseline {} written to {}".format(record["digest"], path))
        return 0

    previous = load(path)
    if previous is None:
        print("no baseline at {}: run --record first".format(path))
        return 1
    result = compare(previous, record)
    print(json.dumps(result, indent=2) if a.as_json else format_compare(result))
    return 0 if result.get("same") else 1


if __name__ == "__main__":
    sys.exit(main())
