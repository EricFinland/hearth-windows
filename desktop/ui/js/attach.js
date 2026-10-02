/* Attaching files to a chat message.
 *
 * Three ways in: the paperclip button (a plain <input type=file multiple>,
 * which WebView2 serves with the normal Windows picker), dropping files on the
 * chat, and pasting files into the composer. Every file becomes a chip in the
 * tray above the composer, uploads to the sidecar in base64 chunks (the
 * packaged shell's proxy refuses bodies over 4 MiB, so a 20 MB file cannot be
 * one request), and is stored in <workspace>/imports/ by
 * desktop/server/attachments.py, which also reads what text it can out of it.
 * Only files that finished importing ("ready") go with the next message;
 * one still importing waits for the message after.
 *
 * Every filename shown here came off somebody's disk, so it is untrusted
 * text: a name like "invoice<U+202E>fdp.exe" displays as "invoiceexe.pdf" if
 * the override is allowed to act. Names, notes and warnings therefore only
 * reach the page through dom.js's el({text}), which renders such controls as
 * visible markers. xss-check.attach.js drives the real chip renderer and the
 * transcript's sent-file chips with hostile names to prove it.
 *
 * State lives in this module, not in app.js: app.js calls initAttachments()
 * once, takeAttachments() when it sends, and returnAttachments() if the send
 * failed. The tray follows the composer's enabled state by watching its
 * `disabled` attribute rather than by being told, so nothing in app.js's turn
 * bookkeeping has to know this feature exists.
 */

import { el, icon, appendAll, clear, setText, $ } from "./dom.js";

/** The caps attachments.py enforces, checked here first so a person is told
 *  before anything is uploaded rather than after. */
export const LIMITS = Object.freeze({
  fileBytes: 20 * 1024 * 1024,
  files: 10,
  messageBytes: 50 * 1024 * 1024,
});

const CHUNK_BYTES = 1024 * 1024;
// attachments.py's _SET_OVERHEAD_CHARS, BLOCK_OVERHEAD_CHARS and
// MIN_EXCERPT_CHARS: the chip's "full text" or "excerpt" is the same
// arithmetic the sidecar will do at send time, so the label does not promise
// something the prompt will not do. Each finish record carries its own
// overhead_chars (the fence grows with the file's name); the constant is only
// the fallback for a record without one. The sidecar checks the rendered
// length, so where this estimate is off it errs towards "too big to inline".
const SET_OVERHEAD_CHARS = 400;
const BLOCK_OVERHEAD_CHARS = 520;
const MIN_EXCERPT_CHARS = 200;
const NOTICE_MS = 5000;

class Cancelled extends Error {}

/** "1.4 MB", for a size in bytes. */
export function formatBytes(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v < 0) return "";
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(v < 10 * 1024 ? 1 : 0)} KB`;
  return `${(v / (1024 * 1024)).toFixed(1)} MB`;
}

/** Characters of attached text one message may carry, as compose() will
 *  reckon it: each record's budget_chars, and what is left under the
 *  conversation ceiling (ceiling_chars) once the typed words are counted,
 *  since they share one user message with the files. */
export function messageBudget(records, typedChars = 0) {
  const list = Array.isArray(records) ? records : [];
  if (!list.length) return 0;
  const typed = Math.max(0, Number(typedChars) || 0);
  let budget = Infinity;
  for (const r of list) {
    budget = Math.min(budget, Number(r && r.budget_chars) || 0);
    const ceiling = Number(r && r.ceiling_chars);
    if (Number.isFinite(ceiling)) budget = Math.min(budget, ceiling - typed);
  }
  return Math.max(0, budget);
}

/** False when even compose()'s compact layout (each file's path, size and
 *  kind, no text) will not fit, so the sidecar would refuse the send with a
 *  413. Uses each record's compact_chars and compact_overhead_chars. */
export function fitsContext(records, typedChars = 0) {
  const list = Array.isArray(records) ? records : [];
  if (!list.length) return true;
  let need = 0;
  for (const r of list) {
    need = Math.max(need, Number(r && r.compact_overhead_chars) || 0);
  }
  for (const r of list) need += Number(r && r.compact_chars) || 0;
  return need <= messageBudget(list, typedChars);
}

/** How each file on one message will reach the model: "full", "excerpt" or
 *  "none". `records` are POST /attach/finish responses, `typedChars` the
 *  length of the message typed with them. Mirrors compose() and
 *  attachments.allocate(): what is left of the budget after every file's
 *  framing is shared shortest file first, each taking at most an equal share
 *  of what is left. */
export function planInline(records, typedChars = 0) {
  const list = Array.isArray(records) ? records : [];
  if (!list.length) return [];
  const overhead = list.reduce(
    (sum, r) => sum + (Number(r && r.overhead_chars) || BLOCK_OVERHEAD_CHARS), SET_OVERHEAD_CHARS);
  let remaining = Math.max(0, messageBudget(list, typedChars) - overhead);
  const plan = list.map(() => "none");
  const readable = list
    .map((r, i) => ({ i, len: r && r.readable ? Number(r.text_chars) || 0 : -1 }))
    .filter((x) => x.len >= 0)
    .sort((a, b) => a.len - b.len);
  let left = readable.length;
  for (const { i, len } of readable) {
    const give = Math.min(len, Math.floor(remaining / left));
    remaining -= give;
    left -= 1;
    if (give >= len && !list[i].truncated) plan[i] = "full";
    else if (give >= Math.min(len, MIN_EXCERPT_CHARS)) plan[i] = "excerpt";
  }
  return plan;
}

const UNREAD_LABEL = {
  image: "stored, image not read",
  no_text: "stored, no text found",
  encrypted: "stored, encrypted",
  empty: "stored, empty",
};

/** The one-line status a chip shows, and the tone it is drawn in. */
export function describeStatus(item, plan) {
  switch (item.status) {
    case "queued":
      return { label: "waiting", tone: "busy" };
    case "importing": {
      const pct = Math.floor((Number(item.progress) || 0) * 100);
      return { label: `importing ${pct}%`, tone: "busy" };
    }
    case "ready": {
      const rec = item.record || {};
      const warn = Array.isArray(rec.warnings) && rec.warnings.length > 0;
      if (!rec.readable) {
        return { label: UNREAD_LABEL[rec.status] || "stored, not readable", tone: "quiet" };
      }
      const how = plan === "full" ? "full text" : plan === "excerpt" ? "excerpt" : "too big to inline";
      return { label: `ready, ${how}`, tone: warn ? "warn" : "ok" };
    }
    case "skipped":
      return { label: `skipped: ${item.reason || ""}`, tone: "bad" };
    default:
      return { label: `failed: ${item.reason || "unknown error"}`, tone: "bad" };
  }
}

function warningLabel(w) {
  return w && w.type === "secret" ? "possible secret" : "possible injection";
}

/** One chip in the tray. Exported so the XSS harness drives the real thing.
 *  `item` is {name, size, status, progress, reason, record}. */
export function renderAttachmentChip(item, { plan = null, onRemove = null } = {}) {
  const status = describeStatus(item, plan);
  const rec = item.record || null;
  const shownName = String((rec && rec.name) || item.name || "attachment");
  const titleLines = [shownName];
  if (rec && rec.path && rec.name !== item.name) titleLines.push(`stored as ${rec.path}`);
  if (rec && rec.note) titleLines.push(String(rec.note));
  const chip = el("div", {
    class: `att-chip att-${status.tone}`,
    role: "listitem",
    title: titleLines.join("\n"),
  });
  appendAll(chip, [
    icon("i-clip", "i att-chip-icon"),
    el("span", { class: "att-name", text: shownName }),
    el("span", { class: "att-size", text: formatBytes(rec ? rec.size : item.size) }),
    el("span", { class: "att-status", text: status.label }),
  ]);
  if (item.status === "importing") {
    chip.appendChild(el("progress", {
      class: "att-progress", max: "1", value: String(Number(item.progress) || 0),
      "aria-label": `importing ${shownName}`,
    }));
  }
  for (const w of (rec && Array.isArray(rec.warnings) ? rec.warnings : [])) {
    chip.appendChild(el("span", { class: "att-flag", title: String(w.summary || "") }, [
      icon("i-alert", "i"), warningLabel(w),
    ]));
  }
  if (onRemove) {
    chip.appendChild(el("button", {
      class: "att-remove",
      type: "button",
      "aria-label": `Remove ${shownName}`,
      title: item.status === "ready"
        ? "Do not send this file (the copy in imports/ stays in your workspace)"
        : "Remove",
      on: { click: onRemove },
    }, [icon("i-cross", "i")]));
  }
  return chip;
}

/** The sentences under the chips: every warning and every "not read" note in
 *  full, because a tooltip is not where a person finds out their file has a
 *  credential in it. */
export function renderAttachmentNotes(items) {
  const list = el("ul", { class: "att-notes" });
  for (const item of items) {
    const rec = item.record;
    if (!rec) continue;
    const name = String(rec.name || item.name || "attachment");
    for (const w of Array.isArray(rec.warnings) ? rec.warnings : []) {
      list.appendChild(el("li", { class: "att-note att-note-warn" }, [
        el("span", { class: "att-note-name", text: name }), String(w.summary || ""),
      ]));
    }
    if (!rec.readable && rec.note) {
      list.appendChild(el("li", { class: "att-note" }, [
        el("span", { class: "att-note-name", text: name }), String(rec.note),
      ]));
    } else if (rec.ignored && rec.note) {
      list.appendChild(el("li", { class: "att-note att-note-warn" }, [
        el("span", { class: "att-note-name", text: name }), String(rec.note),
      ]));
    }
  }
  return list.childElementCount ? list : null;
}

function toBase64(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

function hasFiles(event) {
  const types = event.dataTransfer && event.dataTransfer.types;
  return Boolean(types) && Array.from(types).includes("Files");
}

function errorText(err) {
  return err && err.message ? err.message : String(err);
}

class AttachTray {
  constructor({ sidecar, getSession, isRunning }) {
    this.sidecar = sidecar;
    this.getSession = getSession || (() => null);
    this.isRunning = isRunning || (() => false);
    this.root = $("#att-tray");
    this.button = $("#btn-attach");
    this.input = $("#att-input");
    this.composer = $("#composer");
    this.composerBox = $(".composer");
    this.chat = $(".chat");
    this.items = [];
    this.seq = 0;
    this.generation = 0;
    this.workspace = undefined;
    this.available = false;
    this.enabled = false;
    this.busy = false;
    this.notice = "";
    this.noticeTimer = null;
    if (!this.root || !this.button || !this.input || !this.composer) return;

    this.button.addEventListener("click", () => {
      this.sync();
      if (this.enabled) this.input.click();
    });
    this.input.addEventListener("change", () => {
      const files = Array.from(this.input.files || []);
      this.input.value = "";
      this.addFiles(files);
    });
    this.composer.addEventListener("paste", (e) => this.onPaste(e));
    // What fits depends on the typed words too, so the chips' labels follow
    // the typing; a full redraw only when a label would actually change.
    this.composer.addEventListener("input", () => {
      if (this.planKey() !== this.lastPlanKey) this.render();
      else this.renderHint();
    });

    // A file dropped anywhere else must not make the webview navigate to it.
    document.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
    document.addEventListener("drop", (e) => { if (hasFiles(e)) e.preventDefault(); });
    if (this.chat) {
      const over = (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        this.sync();
        e.dataTransfer.dropEffect = this.enabled ? "copy" : "none";
        this.composerBox?.classList.toggle("att-drop-target", this.enabled);
      };
      this.chat.addEventListener("dragenter", over);
      this.chat.addEventListener("dragover", over);
      this.chat.addEventListener("dragleave", (e) => {
        if (!this.chat.contains(e.relatedTarget)) this.composerBox?.classList.remove("att-drop-target");
      });
      this.chat.addEventListener("drop", (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        this.composerBox?.classList.remove("att-drop-target");
        this.addFiles(Array.from(e.dataTransfer.files || []));
      });
    }

    // The composer's disabled attribute is the turn state app.js already
    // maintains; the titlebar chips change whenever the session does.
    const observer = new MutationObserver(() => this.sync());
    observer.observe(this.composer, { attributes: true, attributeFilter: ["disabled"] });
    const chips = $("#chips");
    if (chips) observer.observe(chips, { childList: true, subtree: true, characterData: true });
    this.sync();
  }

  sync() {
    const session = this.getSession();
    const workspace = session ? String(session.workspace || "") : null;
    if (this.workspace !== undefined && workspace !== this.workspace) {
      // Another folder: files imported into the old one are not in this one.
      this.dropAll();
    }
    this.workspace = workspace;
    this.available = Boolean(session) && (session.engine || "chat") === "chat";
    this.enabled = this.available && !this.composer.disabled && !this.isRunning();
    this.button.hidden = !this.available;
    this.button.disabled = !this.enabled;
    this.render();
  }

  dropAll() {
    this.generation += 1;
    for (const item of this.items) {
      item.removed = true;
      if (item.uploadId) this.sidecar.cancelAttach(item.uploadId).catch(() => {});
    }
    this.items = [];
  }

  say(text) {
    this.notice = text;
    clearTimeout(this.noticeTimer);
    this.noticeTimer = setTimeout(() => { this.notice = ""; this.render(); }, NOTICE_MS);
    this.render();
  }

  onPaste(e) {
    const data = e.clipboardData;
    const files = data && data.files ? Array.from(data.files) : [];
    if (!files.length) return;
    // Copying text out of Word or a browser often carries an image rendition
    // too. A paste with text in it stays a text paste.
    if (data.getData("text/plain")) return;
    e.preventDefault();
    this.addFiles(files);
  }

  counted() {
    return this.items.filter((i) => i.status === "queued" || i.status === "importing" || i.status === "ready");
  }

  refusal(file) {
    if (file.size > LIMITS.fileBytes) return `larger than ${formatBytes(LIMITS.fileBytes)}`;
    const live = this.counted();
    if (live.length >= LIMITS.files) return `at most ${LIMITS.files} files per message`;
    const total = live.reduce((sum, i) => sum + (Number(i.size) || 0), 0);
    if (total + file.size > LIMITS.messageBytes) return `over ${formatBytes(LIMITS.messageBytes)} for one message`;
    return "";
  }

  addFiles(files) {
    if (!files.length) return;
    this.sync();
    if (!this.available) {
      this.say(this.getSession()
        ? "Files can only be attached in a chat session, not a work loop or a swarm."
        : "Start a session before attaching files.");
      return;
    }
    if (!this.enabled) {
      this.say("Wait for the current turn to finish before attaching files.");
      return;
    }
    for (const file of files) {
      this.seq += 1;
      const item = {
        key: this.seq, name: file.name || "attachment", size: file.size,
        status: "queued", progress: 0, reason: "", file, record: null, uploadId: null,
      };
      const why = this.refusal(file);
      if (why) {
        item.status = "skipped";
        item.reason = why;
        item.file = null;
      }
      this.items.push(item);
    }
    this.render();
    this.pump();
  }

  async pump() {
    if (this.busy) return;
    this.busy = true;
    try {
      for (;;) {
        const next = this.items.find((i) => i.status === "queued");
        if (!next) break;
        await this.upload(next);
      }
    } finally {
      this.busy = false;
    }
  }

  async upload(item) {
    const generation = this.generation;
    const stillWanted = () => !item.removed && generation === this.generation;
    item.status = "importing";
    this.render();
    try {
      const begun = await this.sidecar.beginAttach(item.name, item.size);
      item.uploadId = begun.id;
      const step = Math.max(1, Math.min(Number(begun.chunk_bytes) || CHUNK_BYTES, CHUNK_BYTES));
      let offset = 0;
      while (offset < item.size) {
        if (!stillWanted()) throw new Cancelled();
        const bytes = new Uint8Array(await item.file.slice(offset, offset + step).arrayBuffer());
        if (!bytes.length) throw new Error("the file changed while it was being read");
        await this.sidecar.attachChunk(item.uploadId, offset, toBase64(bytes));
        offset += bytes.length;
        item.progress = offset / item.size;
        this.renderProgress(item);
      }
      if (!stillWanted()) throw new Cancelled();
      const record = await this.sidecar.finishAttach(item.uploadId);
      item.uploadId = null;
      item.file = null;
      // Removed while the last request was in flight: the file is already in
      // imports/, which is fine; it is just not sent.
      if (!stillWanted()) return;
      item.record = record;
      item.status = "ready";
    } catch (err) {
      if (item.uploadId) this.sidecar.cancelAttach(item.uploadId).catch(() => {});
      item.uploadId = null;
      item.file = null;
      if (err instanceof Cancelled || !stillWanted()) return;
      item.status = "failed";
      item.reason = errorText(err);
    }
    this.render();
  }

  remove(item) {
    // An upload in flight notices this between chunks and cancels itself.
    item.removed = true;
    this.items = this.items.filter((i) => i !== item);
    this.render();
    this.composer.focus();
  }

  /** Hand the ready files to send(): they leave the tray now. Files still
   *  importing stay for the next message; skipped and failed ones are
   *  cleared, since the message they were meant for is going without them. */
  take() {
    this.sync();
    if (!this.available) return [];
    const ready = this.items.filter((i) => i.status === "ready");
    const plan = planInline(ready.map((i) => i.record), this.typedChars());
    this.items = this.items.filter((i) => i.status === "queued" || i.status === "importing");
    this.render();
    return ready.map((i, n) => ({
      name: i.record.name, path: i.record.path, size: i.record.size, kind: i.record.kind,
      plan: plan[n], warnings: (i.record.warnings || []).map((w) => w.type), record: i.record,
    }));
  }

  /** A send failed: put its files back so the person does not attach them again. */
  restore(list) {
    if (!Array.isArray(list) || !list.length) return;
    const back = list.filter((a) => a && a.record).map((a) => {
      this.seq += 1;
      return { key: this.seq, name: a.record.name, size: a.record.size, status: "ready",
        progress: 1, reason: "", file: null, record: a.record, uploadId: null };
    });
    this.items = back.concat(this.items);
    this.render();
  }

  /** Progress ticks touch only that chip's bar and label, so a person
   *  tabbing through the tray does not lose focus once a second. */
  renderProgress(item) {
    const chip = this.root && this.root.querySelector(`[data-key="${item.key}"]`);
    if (!chip) { this.render(); return; }
    const bar = chip.querySelector(".att-progress");
    if (bar) bar.value = Number(item.progress) || 0;
    const label = chip.querySelector(".att-status");
    if (label) setText(label, describeStatus(item, null).label);
  }

  renderHint() {
    const hint = this.root && this.root.querySelector(".att-hint");
    if (hint) setText(hint, this.hintText());
  }

  typedChars() {
    return this.composer ? this.composer.value.length : 0;
  }

  /** The ready files' plan plus whether they fit at all, as one string. */
  planKey() {
    const records = this.items.filter((i) => i.status === "ready").map((i) => i.record);
    return planInline(records, this.typedChars()).join(",") + "|" +
      fitsContext(records, this.typedChars());
  }

  hintText() {
    if (this.notice) return this.notice;
    const ready = this.items.some((i) => i.status === "ready");
    const pending = this.items.some((i) => i.status === "queued" || i.status === "importing");
    if (ready && !this.composer.value.trim()) return "Type a message to send with these files.";
    if (ready && !fitsContext(this.items.filter((i) => i.status === "ready").map((i) => i.record),
      this.typedChars())) {
      return "These files do not fit in what is left of the model's context window. " +
        "Remove some, shorten the message, or start a new chat.";
    }
    if (pending && ready) return "Files still importing go with the next message, not this one.";
    if (pending) return "Importing into imports/ in your workspace.";
    return "";
  }

  render() {
    if (!this.root) return;
    this.lastPlanKey = this.planKey();
    clear(this.root);
    const visible = (this.available && this.items.length > 0) || Boolean(this.notice);
    this.root.hidden = !visible;
    if (!visible) return;
    const ready = this.items.filter((i) => i.status === "ready");
    const plan = planInline(ready.map((i) => i.record), this.typedChars());
    const planOf = new Map(ready.map((i, n) => [i, plan[n]]));
    const row = el("div", { class: "att-row", role: "list", "aria-label": "files to attach" });
    for (const item of this.available ? this.items : []) {
      const chip = renderAttachmentChip(item, {
        plan: planOf.get(item) || null,
        onRemove: () => this.remove(item),
      });
      chip.dataset.key = String(item.key);
      row.appendChild(chip);
    }
    if (row.childElementCount) this.root.appendChild(row);
    const notes = renderAttachmentNotes(this.available ? this.items : []);
    if (notes) this.root.appendChild(notes);
    const hint = this.hintText();
    this.root.appendChild(el("p", { class: "att-hint" + (this.notice ? " is-notice" : ""), text: hint }));
  }
}

let tray = null;

/** Build the tray. app.js calls this once, after its own bindings. */
export function initAttachments(options) {
  if (!tray) tray = new AttachTray(options || {});
  return tray;
}

/** The ready files for the message being sent, removed from the tray. */
export function takeAttachments() {
  return tray ? tray.take() : [];
}

/** Give a failed send's files back to the tray. */
export function returnAttachments(list) {
  if (tray) tray.restore(list);
}
