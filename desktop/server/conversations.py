#!/usr/bin/env python3
"""hearth desktop sidecar conversation history: several saved chats instead
of one.

session_state.py made a restart a resumption instead of a reset, for the ONE
session the sidecar holds. That was the right first step and the wrong
ceiling: starting a fresh chat threw the old one away, because there was
only one file to put it in. This module keeps one file per conversation and
an index over them, so "new chat" leaves the previous one in a list the user
can go back to, rename, or delete.

Layout, under hearth_paths.data_dir():

  desktop/conversations/<id>.json   one conversation: exactly a
                                    session_state.snapshot(), plus a small
                                    "conversation" block (id, title,
                                    title_source, created_at)
  desktop/conversations/index.json  {"version", "active_id", "items": [...]}
  desktop/session_state.json        the single-session file older builds
                                    wrote; migrated once, see migrate_legacy

The per-conversation FORMAT is deliberately not new. Every file here is read
back through session_state.load() and rebuilt through
session_state.restore_session(), so every check that module applies to the
one file it used to own -- "bypass" is never restored, a conversation whose
system prompt this process did not write is dropped, a loop or swarm config
is re-validated in main.py -- applies to every saved conversation, on every
switch, not only at startup. That matters more here than it did there: this
directory is in the data dir the agent's own run_command can write, and
"open an old chat" is now a click away rather than a restart away.

The index is a cache, never the authority. It exists so listing the chats
does not mean parsing every conversation file (each can be megabytes of
model context). If it is missing, unreadable or the wrong shape it is
rebuilt by scanning the directory, and on every read it is reconciled with
the directory: an entry whose file has gone is dropped, a file the index
does not know about is read and added. A conversation file that fails
session_state's own validation is skipped -- left on disk untouched, simply
not listed -- so one corrupt file never hides the others.

Ids are uuid4().hex and nothing else. Every id that reaches a filename is
checked against ^[0-9a-f]{32}$ first (_path_for), which is the whole defence
against a request naming "../../something": there is no path in the API,
only an id, and an id that is not 32 lowercase hex digits names no file.

Titles are untrusted text. A title is derived from the conversation's first
user prompt, deterministically -- no model call, so it is instant, free, and
the same every time -- and a rename is user input. Both go through
clean_title(), which removes C0/C1 controls, bidi overrides and zero-width
characters (the UI neutralizes them again on display; this is the second
layer, so a title is clean in the file too), collapses whitespace and caps
the length on a word boundary. The index is re-cleaned on every read,
because the index is also a file something else could have written.

Writes are atomic (session_state._write_atomic: a uniquely named temp file
in the same directory, then os.replace) and serialised by one lock per
store, so the persist hook firing on a turn's worker thread and a rename
arriving over HTTP cannot interleave their read-modify-write of the index.
A deleted conversation is remembered for the life of the process, so a
persist that was already on its way when the delete landed cannot write the
file back.

Standard library only.
"""

import json
import os
import re
import sys
import threading
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import session_state  # noqa: E402 - desktop/server sibling; also puts agent/ on sys.path
import hearth_paths  # noqa: E402

STORE_DIRNAME = "conversations"
INDEX_FILENAME = "index.json"
INDEX_VERSION = 1
MIGRATED_SUFFIX = ".migrated"

#: An auto title is a glance, not a summary.
TITLE_MAX = 60
#: A title the user typed is allowed a little more room.
RENAME_MAX = 80

_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_FILE_RE = re.compile(r"\A([0-9a-f]{32})\.json\Z")

# Characters a title never keeps. Whitespace-like controls become a space
# (so "fix\nthis" reads "fix this"); the rest are removed outright. The set
# mirrors desktop/ui/js/dom.js's neutralize(): C0 and C1 controls, the
# directional marks and overrides, and the zero-width characters that make
# text display shorter than it is. U+200C/U+200D are kept for the same reason
# dom.js keeps them: real words in Persian and Hindi need them.
_AS_SPACE = re.compile("[\u0000-\u001f\u007f-\u009f\u2028\u2029]")
_REMOVED = re.compile("[\u061c\u200b\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff"
                      "\ud800-\udfff]")


def new_id():
    return uuid.uuid4().hex


def valid_id(cid):
    return isinstance(cid, str) and bool(_ID_RE.match(cid))


def clean_title(text, limit=TITLE_MAX):
    """`text` as a one-line title of at most `limit` characters, or "".

    Deterministic and total: any input, including non-strings, gives a
    string. Truncation prefers the last word boundary in the back half of
    the allowance, so "Refactor the authentication middleware to..." rather
    than "Refactor the authentication middleware t...", and always ends in
    an ASCII "..." so a cut title is visibly cut."""
    if not isinstance(text, str):
        return ""
    text = _REMOVED.sub("", _AS_SPACE.sub(" ", text))
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:max(1, limit - 3)]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" .,;:-") + "..."


def first_prompt(snapshot):
    """The first thing the user asked in a persisted snapshot, or None.

    The "user_prompt" event POST /prompt records is the exact text the user
    typed, so it wins. A conversation saved before that event existed (a
    migrated legacy file) falls back to the first user message in the
    engine's own context, and a work loop's goal after that."""
    if not isinstance(snapshot, dict):
        return None
    for ev in snapshot.get("recent_events") or []:
        if isinstance(ev, dict) and ev.get("kind") == "user_prompt":
            data = ev.get("data")
            text = data.get("text") if isinstance(data, dict) else None
            if isinstance(text, str) and text.strip():
                return text
    state = snapshot.get("engine_state")
    if isinstance(state, dict):
        for msg in state.get("messages") or []:
            if (isinstance(msg, dict) and msg.get("role") == "user"
                    and isinstance(msg.get("content"), str) and msg["content"].strip()):
                return msg["content"]
        goal = state.get("goal")
        if isinstance(goal, str) and goal.strip():
            return goal
    return None


def derive_title(snapshot):
    return clean_title(first_prompt(snapshot) or "", TITLE_MAX)


def _number(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _text(value, limit=4096):
    """A short string field from a file this module does not trust: a
    string, cut to a sane length, or ""."""
    return value[:limit] if isinstance(value, str) else ""


class ConversationStore:
    """Every saved conversation, behind one lock.

    `root` overrides the directory (tests); by default it is computed on
    every call from hearth_paths.data_dir(), so HEARTH_DATA_DIR redirects it
    exactly like every other piece of Hearth state. main.py builds one store
    and hands the same instance to both the persist hook and SidecarState --
    one lock is only a lock if everyone takes the same one."""

    def __init__(self, root=None):
        self._root = root
        self._lock = threading.RLock()
        self._deleted = set()
        self._unreadable = {}  # id -> (mtime_ns, size) of a file that failed validation

    # ---- locations ----

    def root(self):
        if self._root:
            return self._root
        return os.path.join(hearth_paths.data_dir(), session_state.STATE_SUBDIR, STORE_DIRNAME)

    def index_path(self):
        return os.path.join(self.root(), INDEX_FILENAME)

    def legacy_path(self):
        """Where an older build kept its single session. Next to the store,
        so a store with an overridden root migrates from its own sibling
        rather than from the real user's data dir."""
        return os.path.join(os.path.dirname(self.root()), session_state.STATE_FILENAME)

    def _path_for(self, cid):
        if not valid_id(cid):
            raise ValueError("not a conversation id")
        return os.path.join(self.root(), cid + ".json")

    def _ids_on_disk(self):
        try:
            names = os.listdir(self.root())
        except OSError:
            return []
        return [m.group(1) for m in map(_FILE_RE.match, names) if m]

    # ---- the index ----

    def _item_from_snapshot(self, cid, data, fallback_time):
        meta = data.get("conversation") if isinstance(data.get("conversation"), dict) else {}
        source = meta.get("title_source") if meta.get("title_source") in ("auto", "user") else "auto"
        limit = RENAME_MAX if source == "user" else TITLE_MAX
        title = clean_title(meta.get("title"), limit) or derive_title(data)
        saved = _number(data.get("saved_at"), fallback_time)
        return {
            "id": cid,
            "title": title,
            "title_source": source,
            "workspace": _text(data.get("workspace")),
            "model": _text(data.get("model"), 512),
            "mode": _text(data.get("mode"), 32),
            "engine": _text(data.get("engine_kind"), 32) or "chat",
            "created_at": _number(meta.get("created_at"), saved),
            "updated_at": saved,
        }

    def _scan_item(self, cid):
        """Read one conversation file into an index entry, or None if it is
        not a conversation session_state would load. Corrupt is skipped,
        never deleted: it is the user's data, and a parse failure is not
        proof it is worthless."""
        path = self._path_for(cid)
        try:
            st = os.stat(path)
            stamp, mtime = (st.st_mtime_ns, st.st_size), st.st_mtime
        except OSError:
            return None
        # A file already found unreadable, and unchanged since, is not parsed
        # (or complained about) again on every listing.
        if self._unreadable.get(cid) == stamp:
            return None
        data = session_state.load(path)
        if data is None:
            self._unreadable[cid] = stamp
            return None
        self._unreadable.pop(cid, None)
        return self._item_from_snapshot(cid, data, mtime)

    @staticmethod
    def _normalize_item(item):
        """An index entry as this module trusts it, or None. The index is a
        file in a directory the agent can write, so a title read from it is
        cleaned again rather than believed."""
        if not isinstance(item, dict) or not valid_id(item.get("id")):
            return None
        source = item.get("title_source") if item.get("title_source") in ("auto", "user") else "auto"
        now = time.time()
        updated = _number(item.get("updated_at"), now)
        return {
            "id": item["id"],
            "title": clean_title(item.get("title"), RENAME_MAX if source == "user" else TITLE_MAX),
            "title_source": source,
            "workspace": _text(item.get("workspace")),
            "model": _text(item.get("model"), 512),
            "mode": _text(item.get("mode"), 32),
            "engine": _text(item.get("engine"), 32) or "chat",
            "created_at": _number(item.get("created_at"), updated),
            "updated_at": updated,
        }

    def _read_index(self):
        """The index, reconciled with the directory. Rebuilt from a scan if
        it is missing or not the shape this module writes; written back only
        when reconciling actually changed something."""
        on_disk = set(self._ids_on_disk())
        raw = None
        try:
            with open(self.index_path(), "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            raw = None
        if not (isinstance(raw, dict) and raw.get("version") == INDEX_VERSION
                and isinstance(raw.get("items"), list)):
            if raw is not None or os.path.exists(self.index_path()):
                print("[hearth-conversations] {} is unreadable; rebuilding it from the "
                      "conversation files".format(self.index_path()), file=sys.stderr)
            raw = {"version": INDEX_VERSION, "active_id": None, "items": []}
            dirty = bool(on_disk)
        else:
            dirty = False

        items, seen = [], set()
        for entry in raw["items"]:
            item = self._normalize_item(entry)
            if item is None or item["id"] in seen:
                dirty = True
                continue
            if item["id"] not in on_disk:
                dirty = True  # its file is gone; the file is the authority
                continue
            seen.add(item["id"])
            items.append(item)
        for cid in sorted(on_disk - seen):
            item = self._scan_item(cid)
            if item is not None:
                items.append(item)
                dirty = True
        active = raw.get("active_id")
        if active is not None and active not in {i["id"] for i in items}:
            # An active id with no file yet is legitimate for an instant (a
            # conversation created but not yet saved) and is kept; one that is
            # not even an id is not.
            if not valid_id(active):
                active, dirty = None, True
        index = {"version": INDEX_VERSION, "active_id": active, "items": items}
        if dirty:
            self._write_index(index)
        return index

    def _write_index(self, index):
        try:
            session_state._write_atomic(self.index_path(), json.dumps(index, indent=1, sort_keys=True))
            return True
        except Exception as exc:  # noqa: BLE001 - the index is a cache; losing a write costs a rebuild
            print("[hearth-conversations] could not write the conversation index: {}: {}".format(
                type(exc).__name__, exc), file=sys.stderr)
            return False

    @staticmethod
    def _find(index, cid):
        for item in index["items"]:
            if item["id"] == cid:
                return item
        return None

    # ---- reading ----

    def list(self):
        """{"active_id", "items"}, newest activity first."""
        with self._lock:
            index = self._read_index()
        items = sorted(index["items"], key=lambda i: i["updated_at"], reverse=True)
        return {"active_id": index["active_id"], "items": items}

    def get(self, cid):
        if not valid_id(cid):
            return None
        with self._lock:
            item = self._find(self._read_index(), cid)
        return dict(item) if item else None

    def active_id(self):
        with self._lock:
            index = self._read_index()
        active = index["active_id"]
        return active if self._find(index, active) else None

    def load(self, cid):
        """The validated snapshot of one conversation (session_state.load's
        checks), or None if it does not exist or is unusable."""
        if not valid_id(cid):
            return None
        with self._lock:
            return session_state.load(self._path_for(cid))

    # ---- writing ----

    def save(self, cid, snapshot):
        """Write one conversation's snapshot and bring its index entry up to
        date. Best-effort like session_state.save: False on any failure,
        never raises. The title is derived once, from the first prompt, and
        then left alone -- and never touched again after a rename."""
        if not valid_id(cid) or not isinstance(snapshot, dict):
            return False
        with self._lock:
            if cid in self._deleted:
                return False
            index = self._read_index()
            now = time.time()
            item = self._find(index, cid)
            if item is None:
                item = {"id": cid, "title": "", "title_source": "auto", "created_at": now}
                index["items"].append(item)
            if not item["title"] and item["title_source"] != "user":
                item["title"] = derive_title(snapshot)
            item.update({
                "workspace": _text(snapshot.get("workspace")),
                "model": _text(snapshot.get("model"), 512),
                "mode": _text(snapshot.get("mode"), 32),
                "engine": _text(snapshot.get("engine_kind"), 32) or "chat",
                "updated_at": now,
            })
            doc = dict(snapshot)
            doc["conversation"] = {"id": cid, "title": item["title"],
                                   "title_source": item["title_source"],
                                   "created_at": item["created_at"]}
            if not session_state.save(doc, path=self._path_for(cid)):
                return False
            self._write_index(index)
            return True

    def save_session(self, session):
        """The persist hook main.py installs: snapshot `session` into the
        conversation it belongs to (SidecarState stamps that id on every
        session it creates or adopts). A session with no id has nowhere to
        go and is not written."""
        cid = getattr(session, "conversation_id", None)
        if not valid_id(cid):
            return False
        return self.save(cid, session_state.snapshot(session))

    def set_active(self, cid):
        """Remember which conversation is open, so the next start reopens
        it. None means none is."""
        if cid is not None and not valid_id(cid):
            raise ValueError("not a conversation id")
        with self._lock:
            index = self._read_index()
            if index["active_id"] != cid:
                index["active_id"] = cid
                self._write_index(index)

    def rename(self, cid, title):
        """Set a user-chosen title. Returns the updated entry, or None for an
        unknown id; raises ValueError for a title with nothing left in it
        once cleaned. Written into the conversation file too, so a rebuilt
        index still knows it."""
        cleaned = clean_title(title, RENAME_MAX)
        if not cleaned:
            raise ValueError("the title is empty")
        if not valid_id(cid):
            return None
        with self._lock:
            index = self._read_index()
            item = self._find(index, cid)
            if item is None:
                return None
            item["title"], item["title_source"] = cleaned, "user"
            data = session_state.load(self._path_for(cid))
            if data is not None:
                meta = data.get("conversation") if isinstance(data.get("conversation"), dict) else {}
                meta.update({"id": cid, "title": cleaned, "title_source": "user",
                             "created_at": item["created_at"]})
                data["conversation"] = meta
                session_state.save(data, path=self._path_for(cid))
            self._write_index(index)
            return dict(item)

    def delete(self, cid):
        """Remove one conversation for good. True if there was one."""
        if not valid_id(cid):
            return False
        with self._lock:
            self._deleted.add(cid)
            index = self._read_index()
            item = self._find(index, cid)
            existed = item is not None
            try:
                os.remove(self._path_for(cid))
                existed = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                print("[hearth-conversations] could not delete {}: {}".format(cid, exc),
                      file=sys.stderr)
                self._deleted.discard(cid)
                return False
            if item is not None:
                index["items"].remove(item)
            if index["active_id"] == cid:
                index["active_id"] = None
            self._write_index(index)
            return existed

    def prune_if_empty(self, cid):
        """Delete `cid` if nothing ever happened in it: no conversation and
        no events. Pressing "New chat" twice should not leave a trail of
        blank entries behind, and neither should switching away from a chat
        that was never used. A conversation the user renamed is kept: they
        made something of it. An unreadable file is kept too -- unreadable
        is not the same as empty."""
        if not valid_id(cid):
            return False
        with self._lock:
            item = self._find(self._read_index(), cid)
            if item is not None and item["title_source"] == "user":
                return False
            data = session_state.load(self._path_for(cid))
            if data is None or data.get("engine_state") or data.get("recent_events"):
                return False
            return self.delete(cid)

    # ---- migration ----

    def migrate_legacy(self):
        """Fold an older build's single session_state.json into the store,
        once. Returns the new conversation's id, or None if there was
        nothing to do.

        Runs only while the store holds no conversation at all, which is
        what makes it idempotent: after a migration the store is never
        empty again, and the legacy file has been renamed anyway. The copy
        is the file's exact bytes, never a re-serialisation, and the
        original is renamed to session_state.json.migrated rather than
        deleted -- if anything here is wrong, the user's last session is
        still sitting next to it. A legacy file that fails session_state's
        validation is not copied in (it would only ever be skipped) but is
        still renamed aside, so it stops shadowing the store."""
        with self._lock:
            legacy = self.legacy_path()
            if not os.path.isfile(legacy) or self._ids_on_disk():
                return None
            data = session_state.load(legacy)
            cid = None
            if data is not None:
                cid = new_id()
                try:
                    with open(legacy, "rb") as fh:
                        raw = fh.read()
                    session_state._write_atomic(self._path_for(cid), raw)
                except Exception as exc:  # noqa: BLE001 - leave the legacy file where it is
                    print("[hearth-conversations] could not migrate {}: {}: {}; it stays "
                          "where it is".format(legacy, type(exc).__name__, exc), file=sys.stderr)
                    return None
                index = self._read_index()
                index["active_id"] = cid
                self._write_index(index)
            target = legacy + MIGRATED_SUFFIX
            n = 1
            while os.path.exists(target):
                n += 1
                target = "{}{}.{}".format(legacy, MIGRATED_SUFFIX, n)
            try:
                os.replace(legacy, target)
            except OSError as exc:
                print("[hearth-conversations] migrated {} but could not rename it aside: "
                      "{}".format(legacy, exc), file=sys.stderr)
            return cid


def _self_test():
    import shutil
    import tempfile

    old_data_dir = os.environ.get("HEARTH_DATA_DIR")
    scratch = tempfile.mkdtemp(prefix="hearth-conversations-selftest-")
    os.environ["HEARTH_DATA_DIR"] = scratch
    try:
        _self_test_body(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if old_data_dir is None:
            os.environ.pop("HEARTH_DATA_DIR", None)
        else:
            os.environ["HEARTH_DATA_DIR"] = old_data_dir
    print("hearth-desktop-conversations self-test OK")
    return 0


def _snap(workspace="/tmp/ws", prompt=None, mode="edit", engine_state=None, events=None):
    """A snapshot shaped exactly like session_state.snapshot()'s output."""
    evs = list(events or [])
    if prompt is not None:
        evs.insert(0, {"id": 1, "turn_id": "t", "kind": "user_prompt", "ts": 1.0,
                       "data": {"text": prompt}})
    return {"version": session_state.STATE_VERSION, "saved_at": time.time(),
            "workspace": workspace, "model": "m", "mode": mode,
            "status_at_save": "idle", "turn_id_at_save": None,
            "pending_approval_tool": None, "engine_kind": "chat", "engine_config": None,
            "engine_state": engine_state, "recent_events": evs}


def _self_test_body(scratch):
    import session as session_mod

    # === titles: deterministic, cleaned, capped on a word boundary ========
    assert clean_title("  fix   the\nlogin\tbug  ") == "fix the login bug"
    assert clean_title(None) == "" and clean_title(42) == "" and clean_title("   ") == ""
    # Bidi overrides, isolates, marks and zero-width characters are removed,
    # C0/C1 controls become spaces. This is the attack dom.js's neutralize()
    # exists for, refused here a second time so it never even reaches a file.
    hostile = "git status #\u202e dda/ resu ten\u2066\u200b\u200f\u0007\u009b"
    cleaned = clean_title(hostile)
    for ch in "\u202e\u2066\u200b\u200f\u0007\u009b":
        assert ch not in cleaned, (ch, cleaned)
    assert cleaned == "git status # dda/ resu ten", cleaned
    # ZWJ/ZWNJ are real spelling in several scripts and are kept.
    assert clean_title("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645") == \
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
    # A lone surrogate (JSON can carry one) cannot reach the index.
    assert clean_title("a\ud800b") == "ab"
    long_prompt = ("Refactor the authentication middleware so that every route checks "
                   "the session token before reading the body")
    t = clean_title(long_prompt)
    assert len(t) <= TITLE_MAX and t.endswith("..."), t
    assert t == "Refactor the authentication middleware so that every...", t
    assert clean_title(long_prompt) == t, "deterministic"
    # No spaces at all: still capped, never longer than the limit.
    t2 = clean_title("x" * 500)
    assert len(t2) == TITLE_MAX and t2.endswith("..."), t2
    assert len(clean_title("y" * 500, RENAME_MAX)) == RENAME_MAX

    assert derive_title(_snap(prompt="  hello\nthere ")) == "hello there"
    assert derive_title(_snap(engine_state={"messages": [
        {"role": "system", "content": "sys"}, {"role": "user", "content": "from context"}]})) \
        == "from context", "a conversation saved before user_prompt events falls back"
    assert derive_title(_snap(engine_state={"goal": "tidy up", "messages": []})) == "tidy up"
    assert derive_title(_snap()) == ""

    # === ids: nothing that is not 32 hex digits ever names a file ========
    root = os.path.join(scratch, "store-a")
    store = ConversationStore(root=root)
    for bad in ("../../etc/passwd", "..", "", None, 7, "A" * 32, "a" * 31, "a" * 33,
                ("a" * 32) + "\n", "../" + "a" * 32, "a" * 31 + "/"):
        assert not valid_id(bad), bad
        try:
            store._path_for(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("_path_for accepted {!r}".format(bad))
        assert store.load(bad) is None and store.get(bad) is None
        assert store.save(bad, _snap()) is False
        assert store.delete(bad) is False and store.rename(bad, "x") is None
    assert valid_id(new_id()) and new_id() != new_id()

    # === save, list, index, active =======================================
    assert store.list() == {"active_id": None, "items": []}
    assert not os.path.exists(root), "reading an empty store must not create anything"
    a, b = new_id(), new_id()
    assert store.save(a, _snap("/tmp/ws-a")) is True
    assert store.get(a)["title"] == "", "no prompt yet, no title yet"
    time.sleep(0.01)
    assert store.save(b, _snap("/tmp/ws-b", prompt="Write a README for this repo")) is True
    assert store.save(a, _snap("/tmp/ws-a", prompt="first question, in a")) is True
    listing = store.list()
    assert [i["id"] for i in listing["items"]] == [a, b], "newest activity first"
    assert listing["items"][0]["title"] == "first question, in a"
    # The auto title is set once and then left alone.
    store.save(a, _snap("/tmp/ws-a", prompt="a later prompt is not the title"))
    assert store.get(a)["title"] == "first question, in a"
    store.set_active(b)
    assert store.list()["active_id"] == b and store.active_id() == b
    with open(os.path.join(root, a + ".json"), encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["conversation"]["title"] == "first question, in a", on_disk["conversation"]
    assert on_disk["workspace"] == "/tmp/ws-a"
    assert not [n for n in os.listdir(root) if n.startswith(".hearth-tmp-")], \
        "atomic writes must not leave temp files behind"
    # The token-never-reaches-disk backstop still applies to every write.
    assert store.save(a, dict(_snap(), token="nope")) is False
    with open(os.path.join(root, a + ".json"), encoding="utf-8") as fh:
        assert "nope" not in fh.read()

    # === rename: cleaned, capped, refused when empty, survives a rebuild ==
    renamed = store.rename(a, "  My \u202echat\n about\tauth  ")
    assert renamed["title"] == "My chat about auth" and renamed["title_source"] == "user", renamed
    try:
        store.rename(a, " \u200b\u202e\n ")
    except ValueError:
        pass
    else:
        raise AssertionError("a title with nothing left in it must be refused")
    assert store.rename(new_id(), "x") is None, "unknown id"
    assert len(store.rename(b, "z" * 300)["title"]) == RENAME_MAX
    store.save(a, _snap("/tmp/ws-a", prompt="x"))
    assert store.get(a)["title"] == "My chat about auth", "a save never overwrites a rename"

    # === the index is a cache: deleted or corrupt, it is rebuilt =========
    os.remove(store.index_path())
    rebuilt = store.list()
    assert {i["id"] for i in rebuilt["items"]} == {a, b}, rebuilt
    assert store.get(a)["title"] == "My chat about auth", \
        "a rename lives in the conversation file too, so a rebuild keeps it"
    for junk in ("{not json", "[]", json.dumps({"version": 99, "items": []}), ""):
        with open(store.index_path(), "w", encoding="utf-8") as fh:
            fh.write(junk)
        assert {i["id"] for i in store.list()["items"]} == {a, b}, junk
    # A hostile index entry is cleaned on the way out, not believed.
    with open(store.index_path(), "w", encoding="utf-8") as fh:
        json.dump({"version": INDEX_VERSION, "active_id": "../../x", "items": [
            {"id": a, "title": "<img src=x>\u202eevil", "updated_at": "soon",
             "title_source": "root"},
            {"id": "../../boot.ini", "title": "x"},
            {"id": new_id(), "title": "no file behind this one"},
        ]}, fh)
    listing = store.list()
    assert listing["active_id"] is None, listing
    ids = [i["id"] for i in listing["items"]]
    assert a in ids and b in ids and len(ids) == 2, listing
    item_a = store.get(a)
    assert item_a["title"] == "<img src=x>evil" and item_a["title_source"] == "auto", item_a
    assert isinstance(item_a["updated_at"], float)

    # === one corrupt conversation file never hides the others ============
    c = new_id()
    with open(os.path.join(root, c + ".json"), "w", encoding="utf-8") as fh:
        fh.write('{"version": 1, "workspace": "/tmp/ws-c", "mo')  # truncated mid-write
    d = new_id()
    with open(os.path.join(root, d + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "workspace": "/tmp/ws-d"}, fh)  # no model or mode
    with open(os.path.join(root, "notes.json"), "w", encoding="utf-8") as fh:
        fh.write("not a conversation, not an id")
    os.remove(store.index_path())
    ids = {i["id"] for i in store.list()["items"]}
    assert ids == {a, b}, ids
    assert store.load(c) is None and store.load(d) is None
    assert os.path.exists(os.path.join(root, c + ".json")), "corrupt is skipped, never deleted"
    assert store.prune_if_empty(c) is False, "unreadable is not the same as empty"
    os.remove(os.path.join(root, c + ".json"))
    os.remove(os.path.join(root, d + ".json"))

    # === delete, and a late persist cannot bring it back =================
    store.set_active(b)
    assert store.delete(b) is True
    assert store.get(b) is None and not os.path.exists(os.path.join(root, b + ".json"))
    assert store.list()["active_id"] is None, "deleting the active one clears active_id"
    assert store.save(b, _snap(prompt="the worker finished after the delete")) is False
    assert not os.path.exists(os.path.join(root, b + ".json"))
    assert store.delete(b) is False

    # === pruning: only a conversation nothing ever happened in ===========
    e = new_id()
    store.save(e, _snap("/tmp/ws-e"))
    assert store.prune_if_empty(e) is True and store.get(e) is None
    f = new_id()
    store.save(f, _snap("/tmp/ws-f", prompt="something happened"))
    assert store.prune_if_empty(f) is False and store.get(f) is not None
    g = new_id()
    store.save(g, _snap("/tmp/ws-g"))
    store.rename(g, "kept on purpose")
    assert store.prune_if_empty(g) is False, "a renamed conversation is the user's, keep it"

    # === save_session: the persist hook, end to end through a real Session
    sess = session_mod.Session("/tmp/ws-live", "m", "edit")
    assert store.save_session(sess) is False, "a session with no conversation id is not written"
    sess.conversation_id = new_id()
    assert store.save_session(sess) is True
    assert store.get(sess.conversation_id)["workspace"] == "/tmp/ws-live"

    # === migration: lossless, idempotent, never deletes ==================
    mroot = os.path.join(scratch, "migrate", "desktop", STORE_DIRNAME)
    mstore = ConversationStore(root=mroot)
    legacy = mstore.legacy_path()
    os.makedirs(os.path.dirname(legacy))
    legacy_doc = _snap("/tmp/ws-legacy", engine_state={"messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "the conversation from before the upgrade"}]})
    legacy_bytes = json.dumps(legacy_doc, indent=2, sort_keys=True).encode("utf-8")
    with open(legacy, "wb") as fh:
        fh.write(legacy_bytes)
    cid = mstore.migrate_legacy()
    assert valid_id(cid), cid
    with open(os.path.join(mroot, cid + ".json"), "rb") as fh:
        assert fh.read() == legacy_bytes, "the migrated copy must be the exact bytes"
    assert not os.path.exists(legacy)
    with open(legacy + MIGRATED_SUFFIX, "rb") as fh:
        assert fh.read() == legacy_bytes, "the original is renamed aside, never deleted"
    assert mstore.active_id() == cid, "the migrated session is the one that reopens"
    assert mstore.get(cid)["title"] == "the conversation from before the upgrade"
    assert mstore.load(cid)["workspace"] == "/tmp/ws-legacy"
    # Idempotent: a second run, and a run with a new legacy file beside a
    # populated store, both do nothing.
    assert mstore.migrate_legacy() is None
    with open(legacy, "wb") as fh:
        fh.write(legacy_bytes)
    assert mstore.migrate_legacy() is None
    assert os.path.exists(legacy), "a populated store never touches a legacy file"
    assert len(mstore.list()["items"]) == 1
    # A corrupt legacy file is not copied in, but is moved aside (not lost).
    croot = os.path.join(scratch, "migrate-corrupt", "desktop", STORE_DIRNAME)
    cstore = ConversationStore(root=croot)
    os.makedirs(os.path.dirname(cstore.legacy_path()))
    with open(cstore.legacy_path(), "w", encoding="utf-8") as fh:
        fh.write("{truncated")
    assert cstore.migrate_legacy() is None
    assert cstore.list()["items"] == []
    with open(cstore.legacy_path() + MIGRATED_SUFFIX, encoding="utf-8") as fh:
        assert fh.read() == "{truncated"
    # No legacy file at all: nothing happens, nothing is created.
    nroot = os.path.join(scratch, "fresh", "desktop", STORE_DIRNAME)
    assert ConversationStore(root=nroot).migrate_legacy() is None
    assert not os.path.exists(nroot)

    # === a bypass-mode conversation file loads (it is valid JSON) but =====
    # === restore_session refuses to make it a live session ===============
    h = new_id()
    with open(os.path.join(root, h + ".json"), "w", encoding="utf-8") as fh:
        json.dump(_snap("C:\\", mode="bypass", prompt="planted by run_command"), fh)
    planted = store.load(h)
    assert planted is not None and planted["mode"] == "bypass"
    assert session_state.restore_session(planted, lambda: session_mod.NullEngine()) is None

    # === the default root follows HEARTH_DATA_DIR =========================
    assert ConversationStore().root() == os.path.join(scratch, "desktop", STORE_DIRNAME)
    assert ConversationStore().legacy_path() == session_state.state_path()


if __name__ == "__main__":
    sys.exit(_self_test())
