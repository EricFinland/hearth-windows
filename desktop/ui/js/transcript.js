/* The transcript: messages, tool cards, approval cards, notices.
 *
 * Streaming. This file used to carry a note explaining that the sidecar did
 * not stream -- hearth_loop.chat posted with `"stream": False`, engine.py
 * emitted one `delta` event holding the entire finished message, and this
 * class dribbled that already-arrived block onto the screen over about three
 * quarters of a second so it would look live. Nothing arrived early. That is
 * gone in both halves: the model call streams, engine.py emits deltas as
 * tokens land (coalesced on a 100ms window, see its module docstring's point
 * 7), and this file renders each one the moment it arrives. There is no
 * pacing left to disable, which is why `prefers-reduced-motion` no longer
 * appears here -- text appearing as it is generated is not motion, it is the
 * content arriving.
 *
 * Every delta carries `stream_id` (which assistant message it belongs to) and
 * `index` (its character offset within that message), and appendAgent uses
 * both rather than blindly concatenating. That is what makes rendering
 * idempotent across a reconnect: GET /events resumes from a bookmark, and a
 * resumed stream that replays a delta the page already drew must not draw it
 * twice. An overlapping delta is trimmed to its new tail; a delta that starts
 * beyond what has been drawn is a genuine gap (the session's event ring wrapped
 * while this window was away, which also produces its own `events_dropped`
 * notice) and is appended rather than silently discarded. A new stream_id
 * always opens a new bubble, which is also how a tool call interrupting the
 * text comes out right.
 *
 * Text is rendered into a plain text node while it streams and re-rendered
 * through renderProse (code fences, inline code) once the message closes.
 * Both are textContent-only paths -- see safe-text.js -- so a partial token
 * cannot become markup at any point in between. xss-check.html drives real
 * payloads through this fragment by fragment for exactly that reason.
 *
 * Bidi. The approval card is the only thing standing between a hostile or
 * prompt-injected model and the user's machine, so it has to display the
 * characters it is actually going to execute. A U+202E in a tool argument
 * reorders the glyphs on screen while leaving the bytes alone, which turns the
 * card from a defence into the attack's delivery mechanism. Every text node
 * built here therefore comes from dom.js's textNode/setText/neutralize, which
 * render those controls as a visible `<U+202E>` marker. The raw string is kept
 * intact in `stream.full`, because the delta bookkeeping indexes into it and
 * because it is what closeAgent has to re-tokenize; only what reaches a text
 * node is neutralized.
 */

import { el, icon, appendAll, clear, textNode, neutralize } from "./dom.js";
import { renderProse, blob, labelledBlob } from "./safe-text.js";

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
    textNode(isSecret
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

  /** The placeholder a brand new install lands on.
   *
   *  A first run has no model downloaded, so there is nothing to chat with
   *  and the model dropdown has nothing in it. Saying so and offering the
   *  one button that fixes it is the difference between an empty app and an
   *  app that tells you what to do first. `steps` are rendered as plain
   *  text lines; `action` is {label, onClick}.
   */
  showFirstRun(title, body, steps = [], action = null) {
    this.reset();
    const box = el("div", { class: "empty" }, [
      el("h1", { text: title }),
      el("p", { text: body }),
    ]);
    if (steps.length) {
      appendAll(box, [el("ol", { class: "empty-steps" },
        steps.map((step) => el("li", { text: step })))]);
    }
    if (action) {
      appendAll(box, [el("button", {
        class: "btn btn-primary empty-action",
        type: "button",
        text: action.label,
        on: { click: action.onClick },
      })]);
    }
    this._append(box);
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

  /** Render one delta into the active agent bubble, opening one if needed.
   *
   *  `meta` is the delta event's own `stream_id` and `index`. Both are
   *  optional so a caller with neither (a test, or a replayed event from a
   *  sidecar older than this) still renders correctly by plain append; when
   *  they are present they are what makes a replayed or resumed stream
   *  idempotent. See this file's header.
   */
  appendAgent(text, meta) {
    if (!text) return;
    this.clearPlaceholder();
    const streamId = meta && meta.streamId !== undefined && meta.streamId !== null
      ? String(meta.streamId) : null;
    const index = meta && Number.isFinite(meta.index) ? meta.index : null;

    // A different message than the one open: close the old bubble first, so
    // a tool call interrupting the text (or a turn boundary) never appends
    // the next message into the previous one's bubble.
    if (this.stream && streamId !== null && this.stream.id !== null
        && this.stream.id !== streamId) {
      this.closeAgent();
    }

    if (this.stream && index !== null) {
      const drawn = this.stream.full.length;
      if (index + text.length <= drawn) return;   // wholly a replay; already on screen
      if (index < drawn) text = text.slice(drawn - index);  // partial overlap: keep the tail
      // index > drawn is a real gap (the sidecar's event ring wrapped while
      // this window was disconnected, which arrives with its own
      // events_dropped notice). Append it rather than dropping text the
      // model really produced.
    }

    if (!this.stream) {
      // Named streamText rather than textNode so it cannot shadow dom.js's
      // textNode() helper, which is what every other text node here is built
      // with. This one starts empty and is only ever written through
      // neutralize() below.
      const streamText = document.createTextNode("");
      const caret = el("span", { class: "caret" });
      const paragraph = appendAll(el("p"), [streamText, caret]);
      const prose = appendAll(el("div", { class: "prose" }), [paragraph]);
      const bubble = appendAll(el("div", { class: "bubble" }), [prose]);
      const msgEl = appendAll(el("div", { class: "msg msg-agent" }), [
        el("span", { class: "msg-role", text: "hearth" }),
        bubble,
      ]);
      this._append(msgEl);
      this.stream = { msgEl, bubble, textNode: streamText, full: "", id: streamId };
    }
    if (this.stream.id === null) this.stream.id = streamId;

    // Straight to the text node. No queue, no frame budget, no reveal rate:
    // the text is drawn the instant it arrives, because it arrives at the
    // rate the model produces it. `full` keeps the raw string, because the
    // overlap arithmetic above and closeAgent's tokenizer both index into it;
    // only the copy that reaches the screen is neutralized. The whole
    // accumulation is neutralized rather than each delta on its own: that is
    // one linear scan of a string this line then assigns wholesale anyway, so
    // it is the same order of work as the assignment it feeds, and it needs no
    // argument about what a control character split across a delta boundary
    // would do.
    this.stream.full += String(text);
    this.stream.textNode.data = neutralize(this.stream.full);
    this._autoscroll();
  }

  /** Close the active bubble, replacing the streamed plain-text node with the
   *  structured render (code fences, inline code). Both paths only ever set
   *  textContent, so nothing about closing can turn text into markup.
   *
   *  The "the model wrote its tool call as text" fold is decided HERE rather
   *  than on the first delta, and that moved because of streaming: any prefix
   *  of a JSON tool call is just an incomplete fragment, so mid-stream the
   *  question cannot be answered. By the time a message closes it can, and the
   *  bubble is swapped for the fold so the raw call does not sit in the
   *  transcript next to the tool card engine.py is about to render for it.
   *  Nothing is discarded: the fold still holds the exact text.
   */
  closeAgent() {
    const s = this.stream;
    if (!s) return;
    this.stream = null;
    if (isToolCallText(s.full)) {
      const fold = el("details", { class: "fold" });
      fold.appendChild(el("summary", { text: "the model wrote its tool call as text" }));
      fold.appendChild(blob(s.full));
      s.msgEl.replaceWith(appendAll(el("div", { class: "notice notice-quiet" }), [
        appendAll(el("div", { class: "notice-inner" }), [icon("i-terminal"), fold]),
      ]));
      this._autoscroll();
      return;
    }
    clear(s.bubble);
    s.bubble.appendChild(renderProse(s.full));
    this._autoscroll();
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
      text.appendChild(textNode(detail));
    }
    if (blobText) text.appendChild(blob(String(blobText)));
    const inner = appendAll(el("div", { class: "notice-inner" }), [icon(iconId), text]);
    return this._append(appendAll(el("div", { class: "notice notice-" + kind }), [inner]));
  }
}
