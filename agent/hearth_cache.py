#!/usr/bin/env python3
"""hearth cache: content-addressed cache for model responses.

Local inference is free in money and expensive in minutes. An agent loop
re-asks the identical question constantly: the same file summarised after an
unrelated edit, the same classification of the same error string, the same
plan step retried because a tool call failed next to it. On a 7B model at
roughly 20 tokens a second, a cache hit is the difference between instant and
a minute, and the minute is paid again on every loop iteration.

So: `Cache(directory, max_entries=..., ttl_seconds=...)` with

  key(model, messages, options)  stable sha256 of the whole request
  get(key, now=None)             value, or None on miss
  put(key, value, now=None)      store, then prune to max_entries
  stats()                        hits / misses / evictions / entries
  clear()                        drop every entry, return how many

The key is a digest of canonical JSON (keys sorted, no insignificant
whitespace, non-ASCII escaped), so it does not depend on dict ordering: a
request assembled from `{"temperature": 0, "top_p": 1}` and one assembled
from `{"top_p": 1, "temperature": 0}` are the same request and hit each
other. It does depend on the model name, on the full message list in order,
and on the sampling options. That last part is deliberate and is the whole
reason options are in the key: a cache that ignores temperature will serve
one frozen answer to every creative request, so the second ask of "give me
another name for this" returns the first name forever. A cache that ignores
the model will serve the 7B answer when the operator deliberately reached for
the 14B. Both are worse than no cache, because they are wrong quietly.

Entries are individual JSON files named by digest, written to a temp file in
the same directory and moved into place with os.replace. A kill, a full disk
or a power cut during a write therefore leaves either the old entry or the
new one, never half of one. The alternative, writing in place, produces a
truncated file that parses as nothing or, worse, as a shorter answer, and a
cache serves whatever it reads as truth. On top of that, anything unreadable,
unparseable, schema-mismatched or past its TTL is treated as a MISS and
deleted, never as a hit: a corrupt cache must degrade into a slow cache, not
a lying one.

Eviction is least recently used by the access stamp stored inside each entry,
bounded by max_entries, which costs one small rewrite per hit and is still
several orders of magnitude cheaper than the inference it saves. Access time
lives in the entry rather than in filesystem mtime because mtime is what the
backup tool, the sync client and the archive extractor all feel free to
rewrite.

What this deliberately does NOT do:

  - No cross-process locking. Entries are independent files and a write is
    atomic, so the worst outcome of a race is one redundant inference and a
    last-writer-wins entry. A lock file would add a failure mode (a stale
    lock stalls the agent) to buy nothing.
  - No prompt normalisation. Trailing whitespace, a reordered system prompt
    or a reworded question all miss. A different prompt is a different
    question, and guessing otherwise is how a cache starts answering things
    it was never asked.
  - No byte-size budget, no LFU, no compression, no in-memory tier, no
    background pruning thread. max_entries is the one knob.
  - No negative caching: a failed or refused generation is never stored, so
    a transient model error cannot be pinned in place for a day.
  - No streaming or partial entries. A response is cached once it is whole.
  - No sharing between machines and no notion of a remote cache. The
    directory is local, disposable state; deleting it costs only time.

Time is injectable (`now`) everywhere it matters, so expiry and LRU order are
testable without sleeping. Standard library only.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time

SCHEMA = 1
HASH_LEN = 64
HEX = set("0123456789abcdef")
SUFFIX = ".json"
TMP_PREFIX = ".tmp-"
DEFAULT_MAX_ENTRIES = 512
DEFAULT_TTL_SECONDS = 86400


def _canonical(obj):
    """Canonical JSON text for hashing: keys sorted so dict ordering cannot
    change the digest, no insignificant whitespace, every non-ASCII character
    escaped so the digest cannot depend on a file or terminal encoding."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def make_key(model, messages, options=None):
    """Digest the whole request: schema tag, model, message list in order,
    and sampling options. Independent of dict ordering, dependent on
    everything that can change the answer."""
    payload = {
        "schema": SCHEMA,
        "model": model,
        "messages": messages,
        "options": options if options is not None else {},
    }
    text = _canonical(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_key(key):
    """True for a 64 character lowercase hex digest and nothing else. Entry
    names come from this, so a caller cannot steer a write out of the cache
    directory with a key like "../../id_rsa"."""
    if not isinstance(key, str) or len(key) != HASH_LEN:
        return False
    return all(ch in HEX for ch in key)


def _is_number(value):
    """True for a real int or float. bool passes isinstance(int) and a JSON
    `true` where a timestamp belongs means the entry is not ours."""
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


class Cache(object):
    """A directory of content-addressed JSON entries, LRU bounded, TTL aged.

    max_entries bounds the directory; ttl_seconds ages entries from the time
    they were stored (not from last access, so a hot entry still gets
    re-asked eventually and a stale summary cannot live forever). Pass
    ttl_seconds=None for no expiry at all.
    """

    def __init__(self, directory, max_entries=DEFAULT_MAX_ENTRIES,
                 ttl_seconds=DEFAULT_TTL_SECONDS):
        if max_entries is None or int(max_entries) < 1:
            raise ValueError("max_entries must be at least 1")
        if ttl_seconds is not None and float(ttl_seconds) <= 0:
            raise ValueError("ttl_seconds must be positive or None")
        self.directory = os.path.abspath(directory)
        self.max_entries = int(max_entries)
        self.ttl_seconds = None if ttl_seconds is None else float(ttl_seconds)
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        os.makedirs(self.directory, exist_ok=True)

    # -- keys and paths ---------------------------------------------------

    def key(self, model, messages, options=None):
        """Stable key for a request. See make_key."""
        return make_key(model, messages, options)

    def entry_path(self, key):
        """Absolute path of the entry file for key. Raises ValueError on a
        key that is not a digest, rather than touching the path."""
        if not _is_key(key):
            raise ValueError("not a cache key: {!r}".format(key))
        return os.path.join(self.directory, key + SUFFIX)

    # -- reads ------------------------------------------------------------

    def get(self, key, now=None):
        """Return the cached value, or None on a miss. A miss covers: never
        stored, expired, unreadable, unparseable and schema-mismatched, and
        the last three delete the offending file on the way out. On a hit the
        access stamp is refreshed so LRU order reflects use, not writes."""
        path = self.entry_path(key)
        stamp = self._now(now)
        entry = self._load(path)
        if entry is None or self._expired(entry, stamp):
            if entry is not None:
                self._discard(path)
            self._misses += 1
            return None
        entry["accessed"] = stamp
        try:
            self._write(path, entry)
        except (IOError, OSError):
            # A read-only or full disk costs us LRU accuracy, not the hit.
            pass
        self._hits += 1
        return entry["value"]

    def stats(self):
        """Counters for this Cache instance, plus the live entry count.
        hits + misses is every get() call; evictions counts entries dropped
        to stay under max_entries (expiries land in misses, not here)."""
        return {
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "entries": len(self._entry_names()),
        }

    # -- writes -----------------------------------------------------------

    def put(self, key, value, now=None):
        """Store value under key, then prune to max_entries. The value must
        be JSON serialisable; anything else raises TypeError and leaves the
        previous entry, and the temp file, untouched. Re-storing an existing
        key resets its TTL: it is a fresh answer to the same question."""
        path = self.entry_path(key)
        stamp = self._now(now)
        entry = {
            "schema": SCHEMA,
            "key": key,
            "value": value,
            "created": stamp,
            "accessed": stamp,
        }
        self._write(path, entry)
        self._prune()
        return key

    def clear(self):
        """Delete every entry, return how many went. Counters are left alone:
        they describe this process's behaviour, not the directory."""
        gone = 0
        for name in self._entry_names():
            if self._discard(os.path.join(self.directory, name)):
                gone += 1
        return gone

    # -- internals --------------------------------------------------------

    def _now(self, now):
        return time.time() if now is None else float(now)

    def _expired(self, entry, now):
        if self.ttl_seconds is None:
            return False
        return (now - entry["created"]) > self.ttl_seconds

    def _valid(self, entry):
        """Is this one of our entries, in a shape we can serve?"""
        if not isinstance(entry, dict):
            return False
        if entry.get("schema") != SCHEMA:
            return False
        if "value" not in entry:
            return False
        return _is_number(entry.get("created")) and _is_number(
            entry.get("accessed"))

    def _load(self, path):
        """Parse an entry. Returns None for a miss. A file that exists but is
        corrupt or foreign is deleted here, so one bad write cannot be served
        forever and cannot sit in the way of a good one."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except (IOError, OSError):
            return None  # simply not cached
        except (ValueError, UnicodeDecodeError):
            self._discard(path)  # truncated, binary garbage, half a write
            return None
        if not self._valid(entry):
            self._discard(path)
            return None
        return entry

    def _peek(self, path):
        """Like _load but never deletes, for the pruning scan where deletion
        is counted separately."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except (IOError, OSError, ValueError, UnicodeDecodeError):
            return None
        return entry if self._valid(entry) else None

    def _write(self, path, entry):
        """Write an entry atomically: temp file in the same directory, fsync,
        os.replace. An interrupted write leaves the old entry or the new one,
        never a corrupt file that is then served as truth."""
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=TMP_PREFIX,
                                   suffix=SUFFIX)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            self._discard(tmp)
            raise

    def _discard(self, path):
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    def _entry_names(self):
        """Entry file names only. Temp files are named ".tmp-*" and so are
        never counted as entries, nor evicted as if they were."""
        try:
            names = os.listdir(self.directory)
        except OSError:
            return []
        keep = []
        for name in names:
            if not name.endswith(SUFFIX):
                continue
            if _is_key(name[:-len(SUFFIX)]):
                keep.append(name)
        return keep

    def _prune(self):
        """Evict least recently accessed entries until max_entries holds. An
        unreadable entry sorts first and goes first: it could never be served
        anyway. Ties break on name so the order is deterministic."""
        names = self._entry_names()
        extra = len(names) - self.max_entries
        if extra <= 0:
            return 0
        stamped = []
        for name in names:
            entry = self._peek(os.path.join(self.directory, name))
            accessed = entry["accessed"] if entry else -1.0
            stamped.append((accessed, name))
        stamped.sort()
        gone = 0
        for accessed, name in stamped[:extra]:
            if self._discard(os.path.join(self.directory, name)):
                gone += 1
                self._evictions += 1
        return gone


def _self_test():
    """Offline and deterministic: a temp directory, and every clock reading
    passed in, so TTL and LRU are exercised without sleeping."""
    with tempfile.TemporaryDirectory() as root:
        # -- keys: ordering must not matter, content must ------------------
        c = Cache(os.path.join(root, "a"), max_entries=4, ttl_seconds=100)
        msgs = [{"role": "user", "content": "summarise agent/hearth_run.py"}]
        msgs_reordered = [{"content": "summarise agent/hearth_run.py",
                           "role": "user"}]
        opts = {"temperature": 0.0, "top_p": 1.0, "seed": 7}
        opts_reordered = {"seed": 7, "top_p": 1.0, "temperature": 0.0}
        k1 = c.key("qwen2.5-7b", msgs, opts)
        k2 = c.key("qwen2.5-7b", msgs_reordered, opts_reordered)
        assert k1 == k2, "dict ordering must not change the key"
        assert len(k1) == HASH_LEN and _is_key(k1), k1
        hot = c.key("qwen2.5-7b", msgs,
                    {"temperature": 0.9, "top_p": 1.0, "seed": 7})
        assert hot != k1, "temperature must be part of the key"
        bigger = c.key("qwen2.5-14b", msgs, opts)
        assert bigger != k1, "model must be part of the key"
        followup = c.key("qwen2.5-7b",
                         msgs + [{"role": "assistant", "content": "ok"}], opts)
        assert followup != k1, "the full message list must be part of the key"

        # -- store and hit, including from the reordered key ---------------
        assert c.get(k1, now=1000) is None, "empty cache -> miss"
        c.put(k1, {"text": "it runs the agent loop"}, now=1000)
        assert c.get(k2, now=1050) == {"text": "it runs the agent loop"}
        assert c.get(hot, now=1050) is None, "changed temperature -> miss"

        # -- TTL: fresh at the boundary, expired past it, file removed -----
        assert c.get(k1, now=1100) is not None, "age == ttl is still fresh"
        assert c.get(k1, now=1101) is None, "age > ttl -> miss"
        assert c.stats()["entries"] == 0, "an expired entry is deleted"

        # -- corrupt on disk: a miss, and it does not survive the read -----
        c.put(k1, "value", now=2000)
        path = c.entry_path(k1)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"schema": 1, "value": "half a wr')
        assert c.get(k1, now=2001) is None, "corrupt entry -> miss"
        assert not os.path.exists(path), "corrupt entry must be removed"

        # -- foreign schema: same treatment, never served ------------------
        c.put(k1, "value", now=2000)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 99, "value": "from a future version",
                       "created": 2000, "accessed": 2000}, fh)
        assert c.get(k1, now=2001) is None, "schema mismatch -> miss"
        assert not os.path.exists(path), "mismatched entry must be removed"

        st = c.stats()
        assert st == {"hits": 2, "misses": 5, "evictions": 0, "entries": 0}, st
        assert st["hits"] + st["misses"] == 7, "one count per get() call"

        # -- LRU: least recently ACCESSED goes, not oldest written ---------
        lru_dir = os.path.join(root, "b")
        d = Cache(lru_dir, max_entries=2, ttl_seconds=None)
        ka = d.key("m", [{"role": "user", "content": "a"}], {})
        kb = d.key("m", [{"role": "user", "content": "b"}], {})
        kc = d.key("m", [{"role": "user", "content": "c"}], {})
        d.put(ka, "A", now=10)
        d.put(kb, "B", now=20)
        assert d.get(ka, now=30) == "A"  # a is oldest written, now newest used
        d.put(kc, "C", now=40)
        assert d.stats()["entries"] == 2, d.stats()
        assert d.get(ka, now=50) == "A", "oldest written but recently used"
        assert d.get(kc, now=50) == "C"
        assert d.get(kb, now=50) is None, "least recently accessed evicted"

        # -- a refused value leaves the good entry, and no temp litter -----
        try:
            d.put(kc, object(), now=60)
        except TypeError:
            pass
        else:
            raise AssertionError("a non-serialisable value must be refused")
        assert d.get(kc, now=61) == "C", "the previous entry survives"
        assert sorted(os.listdir(lru_dir)) == sorted(d._entry_names()), \
            "no temp files left behind"

        st = d.stats()
        assert st == {"hits": 4, "misses": 1, "evictions": 1, "entries": 2}, st
        assert st["hits"] + st["misses"] == 5, "one count per get() call"

        # -- a key that is not a digest never becomes a path ---------------
        for bad in ("../../escape", "", "ZZ", "a" * 63, "/etc/passwd", None):
            try:
                d.entry_path(bad)
            except ValueError:
                continue
            raise AssertionError("accepted a bad key: {!r}".format(bad))

        # -- entries are files, so they outlive the process ----------------
        e = Cache(lru_dir, max_entries=2, ttl_seconds=None)
        assert e.get(ka, now=70) == "A", "entries persist across instances"
        assert e.clear() == 2, "clear removes every entry"
        assert e.get(ka, now=71) is None
        assert e.stats()["entries"] == 0

    print("hearth-cache self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-cache")
    p.add_argument("--dir", help="cache directory to inspect")
    p.add_argument("--stats", action="store_true",
                   help="print entry count for --dir")
    p.add_argument("--clear", action="store_true",
                   help="delete every entry in --dir")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.stats or a.clear:
        if not a.dir:
            p.error("--stats and --clear need --dir")
        cache = Cache(a.dir)
        if a.clear:
            print("cleared {} entr(ies)".format(cache.clear()))
        if a.stats:
            print(json.dumps(cache.stats(), sort_keys=True))
        return 0
    p.error("nothing to do (use --self-test, --stats or --clear)")


if __name__ == "__main__":
    sys.exit(main())
