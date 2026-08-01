/* The transcript: messages, tool cards, approval cards, notices.
 *
 * Streaming note, stated plainly because it would be easy to overclaim.
 * The sidecar's model call is not a streaming call: agent/hearth_loop.py posts
 * to Ollama with `"stream": False`, and desktop/server/engine.py emits one
 * `delta` event carrying the whole assistant message. So text arrives at this
 * UI a message at a time, not a token at a time. A long reply slamming into
 * the page as one block reads as a hang followed by a dump, so the reveal
 * below paces text into the bubble over roughly three quarters of a second
 * per delta. That is client-side pacing of text that has already arrived, not
 * per-token streaming. Across a multi-step tool-using turn the transcript does
 * genuinely grow incrementally, because each model round trip is its own
 * delta, interleaved with real tool cards as they happen.
 *
 * `prefers-reduced-motion` disables the pacing entirely.
 */

import { el, icon, appendAll, clear } from "./dom.js";
import { renderProse, blob, labelledBlob } from "./safe-text.js";

const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const REVEAL_FRAMES = 45;   // ~750ms at 60fps for whatever is currently pending
const MIN_CHARS_PER_FRAME = 3;

const READ_TOOLS = new Set(["read_file", "list_files", "list_tree", "git_status", "git_diff"]);
const WRITE_TOOLS = new Set(["write_file", "edit_file", "replace_in_files"]);

function toolIcon(tool) {
  if (WRITE_TOOLS.has(tool)) return "i-pencil";
  if (tool === "run_command") return "i-terminal";
  if (tool === "search_files") return "i-search";
  if (tool === "git_status" || tool === "git_diff") return "i-git";
  if (READ_TOOLS.has(tool)) return "i-file";
  return "i-file";
}

/** A one-line human summary of what a call is aimed at, for the card header. */
function toolTarget(tool, args) {
  if (!args || typeof args !== "object") return "";
  if (tool === "run_command") return String(args.command ?? "");
  if (typeof args.path === "string") return args.path;
  if (typeof args.query === "string") return args.query;
  if (typeof args.find === "string") return args.find;
  return "";
}

/* The argument that IS the action, per tool. These always get their own
 * labelled block even when they are short enough to fit on one line, so the
 * thing a user is actually authorizing is always in the same place on the
 * card instead of moving depending on how long it happens to be. */
const PAYLOAD_ARGS = {
  write_file: new Set(["content"]),
  edit_file: new Set(["find", "replace"]),
  replace_in_files: new Set(["find", "replace"]),
  run_command: new Set(["command"]),
};

const ARG_ORDER = {
  write_file: ["path", "content"],
  edit_file: ["path", "find", "replace"],
  replace_in_files: ["path", "glob", "find", "replace"],
  read_file: ["path", "start", "end"],
  run_command: ["command", "cwd"],
  search_files: ["query", "path", "glob"],
};

/** Render a tool call's arguments. Short scalars become a key/value row; long
 *  or multi-line values become their own labelled block, because for a write
 *  the argument IS the thing being authorized and burying it in a one-line
 *  row would defeat the point of showing it at all. */
function renderArgs(tool, args) {
  const frag = document.createDocumentFragment();
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    frag.appendChild(blob(args === undefined ? "(no arguments)" : args));
    return frag;
  }
  const keys = Object.keys(args);
  const preferred = ARG_ORDER[tool] || [];
  keys.sort((a, b) => {
    const ia = preferred.indexOf(a), ib = preferred.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });

  const payload = PAYLOAD_ARGS[tool] || new Set();
  const rows = [];
  const blocks = [];
  for (const key of keys) {
    const value = args[key];
    const isShortScalar = !payload.has(key) && (
      (typeof value === "string" && value.length <= 80 && !value.includes("\n")) ||
      typeof value === "number" || typeof value === "boolean" || value === null);
    if (isShortScalar) {
      rows.push(el("span", { class: "kv-k", text: key }));
      rows.push(el("span", { class: "kv-v", text: value === null ? "null" : String(value) }));
    } else {
      blocks.push({ key, value });
    }
  }
  if (rows.length) frag.appendChild(appendAll(el("div", { class: "kv" }), rows));
  for (const { key, value } of blocks) {
    frag.appendChild(labelledBlob(key, value === null || value === undefined ? String(value) : value,
      { tall: key === "content" }));
  }
  if (!rows.length && !blocks.length) frag.appendChild(el("p", { class: "panel-note", text: "no arguments" }));
  return frag;
}

/** Split text into its top-level JSON values, or null if anything outside one
 *  is found. A brace/bracket scanner rather than JSON.parse, because a weak
 *  model emits several pretty-printed objects back to back, which is not itself
 *  valid JSON and which no single parse call will accept. */
function splitTopLevelJson(text) {
  const values = [];
  let depth = 0, start = -1, inString = false, escaped = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') { inString = true; continue; }
    if (ch === "{" || ch === "[") { if (depth === 0) start = i; depth++; continue; }
    if (ch === "}" || ch === "]") {
      depth--;
      if (depth < 0) return null;
      if (depth === 0) { values.push(text.slice(start, i + 1)); start = -1; }
      continue;
    }
    if (depth === 0 && ch.trim() !== "") return null; // prose outside the JSON
  }
  return depth === 0 && values.length ? values : null;
}

/** True when a delta is nothing but tool calls the model wrote as text rather
 *  than as a structured tool_calls field. hearth_loop.parse_content_tool_calls
 *  picks those up and engine.py dispatches them, so the very next tool_call
 *  event renders the same call properly as a card. Detecting that here lets the
 *  raw copy be folded away instead of sitting in the transcript twice. Nothing
 *  is discarded: the fold still contains the exact text. */
function isToolCallText(text) {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return false;
  const chunks = splitTopLevelJson(trimmed);
  if (!chunks) return false;
  try {
    return chunks.every((chunk) => {
      const value = JSON.parse(chunk);
      return value && typeof value === "object" && typeof value.name === "string"
        && Object.prototype.hasOwnProperty.call(value, "arguments");
    });
  } catch { return false; }
}

function card(extraClass) {
  const inner = el("div", { class: "card-inner" });
  const outer = el("div", { class: extraClass ? "card " + extraClass : "card" }, [inner]);
  return { outer, inner };
}

function cardHead({ iconId, tool, sub, badge, badgeClass }) {
  const head = el("div", { class: "card-head" });
  head.appendChild(icon(iconId));
  head.appendChild(el("span", { class: "card-tool", text: tool }));
  if (sub) head.appendChild(el("span", { class: "card-sub", text: sub }));
  const badgeEl = el("span", { class: "card-badge " + (badgeClass || ""), text: badge });
  head.appendChild(badgeEl);
  return { head, badgeEl };
}

function findingBlock(kind, finding) {
  if (!finding || typeof finding !== "object") return null;
  const isSecret = kind === "secret";
  const wrap = el("div", { class: isSecret ? "finding is-secret" : "finding" });
  wrap.appendChild(appendAll(el("div", { class: "finding-head" }), [
    icon(isSecret ? "i-key" : "i-alert", "i"),
    document.createTextNode(isSecret
      ? `possible credential in this write (${finding.severity ?? "?"})`
      : `prompt injection in what was just read (${finding.severity ?? "?"})`),
  ]));
  const explanation = isSecret ? finding.reason : finding.explanation;
  if (explanation) wrap.appendChild(el("p", { text: String(explanation) }));
  const detail = [];
  if (isSecret) {
    if (finding.kind) detail.push(`kind: ${finding.kind}`);
    if (finding.line) detail.push(`line: ${finding.line}`);
    if (finding.masked) detail.push(`match: ${finding.masked}`);
  } else {
    if (finding.category) detail.push(`category: ${finding.category}`);
    if (finding.source) detail.push(`source: ${finding.source}`);
  }
  if (detail.length) wrap.appendChild(el("p", { text: detail.join("  ·  ") }));
  const excerpt = isSecret ? finding.context : finding.matched;
  if (excerpt) wrap.appendChild(blob(String(excerpt)));
  return wrap;
}

export class Transcript {
  constructor(container) {
    this.root = container;
    this.stream = null;          // active agent bubble, if any
    this.openToolCards = [];     // tool_call cards still awaiting a tool_result
    this.approvals = new Map();  // approval id -> {outer, badgeEl, actions}
    this.pinned = true;          // stick to the bottom unless the user scrolls up
    this._frame = null;

    this.root.addEventListener("scroll", () => {
      const gap = this.root.scrollHeight - this.root.scrollTop - this.root.clientHeight;
      this.pinned = gap < 140;
    }, { passive: true });
  }

  // ---- layout plumbing ----

  _append(node) {
    this.root.appendChild(node);
    this._autoscroll();
    return node;
  }

  _autoscroll() {
    if (!this.pinned) return;
    this.root.scrollTop = this.root.scrollHeight;
  }

  reset() {
    clear(this.root);
    this.stream = null;
    this.openToolCards = [];
    this.approvals.clear();
    this.pinned = true;
  }

  showPlaceholder(title, body) {
    this.reset();
    this._append(appendAll(el("div", { class: "empty" }), [
      el("h1", { text: title }),
      el("p", { text: body }),
    ]));
  }

  clearPlaceholder() {
    const placeholder = this.root.querySelector(".empty");
    if (placeholder) placeholder.remove();
  }

  // ---- messages ----

  addUser(text) {
    this.clearPlaceholder();
    this.closeAgent();
    this._append(appendAll(el("div", { class: "msg msg-user" }), [
      el("span", { class: "msg-role", text: "you" }),
      el("div", { class: "bubble", text: String(text) }),
    ]));
  }

  /** Queue assistant text for the active bubble, opening one if needed. */
  appendAgent(text) {
    if (!text) return;
    this.clearPlaceholder();
    if (!this.stream && isToolCallText(text)) {
      const fold = el("details", { class: "fold" });
      fold.appendChild(el("summary", { text: "the model wrote its tool call as text" }));
      fold.appendChild(blob(String(text)));
      this._append(appendAll(el("div", { class: "notice notice-quiet" }), [
        appendAll(el("div", { class: "notice-inner" }), [icon("i-terminal"), fold]),
      ]));
      return;
    }
    if (!this.stream) {
      const textNode = document.createTextNode("");
      const caret = el("span", { class: "caret" });
      const paragraph = appendAll(el("p"), [textNode, caret]);
      const prose = appendAll(el("div", { class: "prose" }), [paragraph]);
      const bubble = appendAll(el("div", { class: "bubble" }), [prose]);
      this._append(appendAll(el("div", { class: "msg msg-agent" }), [
        el("span", { class: "msg-role", text: "hearth" }),
        bubble,
      ]));
      this.stream = { bubble, textNode, shown: "", pending: "", full: "", closed: false };
    }
    this.stream.pending += String(text);
    this.stream.full += String(text);
    if (REDUCED_MOTION) this._drain();
    else this._schedule();
  }

  _schedule() {
    if (this._frame !== null) return;
    this._frame = requestAnimationFrame(() => {
      this._frame = null;
      this._step();
    });
  }

  _step() {
    const s = this.stream;
    if (!s) return;
    const take = Math.max(MIN_CHARS_PER_FRAME, Math.ceil(s.pending.length / REVEAL_FRAMES));
    s.shown += s.pending.slice(0, take);
    s.pending = s.pending.slice(take);
    s.textNode.data = s.shown;
    this._autoscroll();
    if (s.pending.length) this._schedule();
    else if (s.closed) this._finalize();
  }

  _drain() {
    const s = this.stream;
    if (!s) return;
    s.shown += s.pending;
    s.pending = "";
    s.textNode.data = s.shown;
    if (s.closed) this._finalize();
    else this._autoscroll();
  }

  /** Replace the paced plain-text node with the structured render (code
   *  fences, inline code). Both paths only ever set textContent. */
  _finalize() {
    const s = this.stream;
    if (!s) return;
    clear(s.bubble);
    s.bubble.appendChild(renderProse(s.full));
    this.stream = null;
    this._autoscroll();
  }

  /** Mark the active bubble complete. It finishes revealing first. */
  closeAgent() {
    const s = this.stream;
    if (!s) return;
    s.closed = true;
    if (!s.pending.length) this._finalize();
  }

  // ---- tool cards ----

  addToolCall(data) {
    this.clearPlaceholder();
    this.closeAgent();
    const tool = String(data.tool ?? "unknown");
    const denied = data.decision === "deny";
    // engine.py emits tool_call the moment a gate resolves, so a still-pending
    // approval card for this tool has in fact been answered. Settling it here
    // rather than only on the click keeps a replayed history (a reconnect, or a
    // session restored from disk at startup) from showing a resolved approval
    // as though it were still waiting.
    this._settlePendingApproval(tool, denied ? "deny" : "allow");
    const { outer, inner } = card();
    const { head, badgeEl } = cardHead({
      iconId: toolIcon(tool),
      tool,
      sub: toolTarget(tool, data.args),
      badge: denied ? "denied" : "running",
      badgeClass: denied ? "is-deny" : "is-run",
    });
    inner.appendChild(head);
    const body = el("div", { class: "card-body" });
    body.appendChild(renderArgs(tool, data.args));
    inner.appendChild(body);
    this._append(outer);
    const entry = { tool, inner, badgeEl, denied };
    if (!denied) this.openToolCards.push(entry);
    return entry;
  }

  addToolResult(data) {
    const tool = String(data.tool ?? "");
    let entry = null;
    for (let i = this.openToolCards.length - 1; i >= 0; i--) {
      if (this.openToolCards[i].tool === tool) { entry = this.openToolCards.splice(i, 1)[0]; break; }
    }
    const output = typeof data.output === "string" ? data.output : JSON.stringify(data.output ?? "", null, 2);
    const failed = data.denied === true || /^error:/i.test(output.trim()) || /^denied:/i.test(output.trim());

    if (!entry) {
      // A result with no matching open card (a denial emitted before any card,
      // or a reconnect that replayed past the call). Show it standalone rather
      // than dropping it.
      const { outer, inner } = card();
      const { head } = cardHead({
        iconId: toolIcon(tool), tool, sub: "",
        badge: failed ? "failed" : "done",
        badgeClass: failed ? "is-deny" : "is-ok",
      });
      inner.appendChild(head);
      inner.appendChild(appendAll(el("div", { class: "card-body" }), [blob(output)]));
      this._append(outer);
      return;
    }

    entry.badgeEl.className = "card-badge " + (failed ? "is-deny" : "is-ok");
    entry.badgeEl.textContent = failed ? "failed" : "done";

    const body = el("div", { class: "card-body" });
    const lines = output.split("\n").length;
    if (output.length > 400 || lines > 8) {
      const fold = el("details", { class: "fold" });
      fold.appendChild(el("summary", { text: `output · ${lines} line${lines === 1 ? "" : "s"}, ${output.length} chars` }));
      fold.appendChild(blob(output));
      body.appendChild(fold);
    } else {
      body.appendChild(blob(output || "(no output)"));
    }
    entry.inner.appendChild(body);
    this._autoscroll();
  }

  // ---- approvals ----

  addApproval(data, onDecide) {
    this.clearPlaceholder();
    this.closeAgent();
    const id = String(data.id ?? "");
    const tool = String(data.tool ?? "unknown");
    const { outer, inner } = card("card-approval");
    outer.dataset.approvalId = id;

    const { head, badgeEl } = cardHead({
      iconId: "i-shield",
      tool,
      sub: toolTarget(tool, data.args),
      badge: "needs approval",
      badgeClass: "is-pending",
    });
    inner.appendChild(head);

    const body = el("div", { class: "card-body" });
    body.appendChild(el("p", {
      class: "approval-ask",
      text: WRITE_TOOLS.has(tool)
        ? "hearth wants to change a file in your workspace. Review exactly what it will write."
        : tool === "run_command"
          ? "hearth wants to run a shell command. run_command is not confined to the workspace."
          : "hearth wants to run this tool.",
    }));
    body.appendChild(renderArgs(tool, data.args));
    const injection = findingBlock("injection", data.injection_finding);
    if (injection) body.appendChild(injection);
    const secret = findingBlock("secret", data.secrets_finding);
    if (secret) body.appendChild(secret);
    inner.appendChild(body);

    const allowBtn = el("button", { class: "btn btn-ok", type: "button" });
    appendAll(allowBtn, [icon("i-check"), document.createTextNode("Approve")]);
    const denyBtn = el("button", { class: "btn btn-danger", type: "button" });
    appendAll(denyBtn, [icon("i-cross"), document.createTextNode("Deny")]);
    const actions = appendAll(el("div", { class: "approval-actions" }), [allowBtn, denyBtn]);
    inner.appendChild(actions);

    const entry = { tool, outer, inner, badgeEl, actions, allowBtn, denyBtn, resolved: false };
    this.approvals.set(id, entry);

    allowBtn.addEventListener("click", () => onDecide(id, "allow"));
    denyBtn.addEventListener("click", () => onDecide(id, "deny"));

    this._append(outer);
    allowBtn.focus({ preventScroll: true });
    return entry;
  }

  /** Settle the oldest unresolved approval for `tool`, if there is one. */
  _settlePendingApproval(tool, decision) {
    for (const [id, entry] of this.approvals) {
      if (entry.resolved || entry.tool !== tool) continue;
      this.resolveApproval(id, decision);
      return;
    }
  }

  get pendingApprovalIds() {
    return [...this.approvals.entries()].filter(([, e]) => !e.resolved).map(([id]) => id);
  }

  resolveApproval(id, decision, note) {
    const entry = this.approvals.get(String(id));
    if (!entry || entry.resolved) return;
    entry.resolved = true;
    entry.outer.classList.add("is-resolved");
    entry.badgeEl.className = "card-badge " + (decision === "allow" ? "is-ok" : "is-deny");
    entry.badgeEl.textContent = decision === "allow" ? "approved" : "denied";
    clear(entry.actions);
    entry.actions.appendChild(el("span", {
      class: "panel-note",
      text: note || (decision === "allow" ? "You approved this call." : "You denied this call."),
    }));
  }

  resolveAllPending(decision, note) {
    for (const id of this.pendingApprovalIds) this.resolveApproval(id, decision, note);
  }

  // ---- notices ----

  addNotice(kind, title, detail, blobText) {
    this.clearPlaceholder();
    this.closeAgent();
    const iconId = kind === "error" ? "i-alert"
      : kind === "warn" ? "i-alert"
      : kind === "ok" ? "i-check"
      : kind === "quiet" ? "i-clock"
      : "i-clock";
    const text = el("div", { class: "notice-text" });
    text.appendChild(el("strong", { text: title }));
    if (detail) {
      text.appendChild(document.createTextNode(" "));
      text.appendChild(document.createTextNode(String(detail)));
    }
    if (blobText) text.appendChild(blob(String(blobText)));
    const inner = appendAll(el("div", { class: "notice-inner" }), [icon(iconId), text]);
    return this._append(appendAll(el("div", { class: "notice notice-" + kind }), [inner]));
  }
}
