/* Chats: every saved conversation, in a sidebar on the Chat tab.
 *
 * The sidecar keeps one file per conversation (desktop/server/conversations.py)
 * and says which one the live session belongs to. This lists them grouped by
 * when they were last active, and opens, starts, renames and deletes them
 * through GET /conversations and POST /conversations/{new,open,rename,delete}.
 * Nothing here owns the session or the transcript: switching hands the new
 * session to app.js (`onSwitched`), which tears the old event stream down and
 * replays the new one, exactly as starting a session does.
 *
 * Three rules shape it.
 *
 * 1. UNTRUSTED TEXT. A title is derived from whatever the user (or something
 *    pasted into the composer) typed first, a rename is free text, and the
 *    index the titles come from is a file in a directory the agent's own
 *    run_command can write. The sidecar strips control and bidi characters,
 *    and every title, workspace and model still reaches the page only through
 *    dom.js's el({text}), which turns any control character that survives
 *    into a visible `<U+202E>` marker. The rename box is the one form field,
 *    and it holds the user's own text back to them. The render functions are
 *    exported so xss-check.history.js can drive hostile titles through the
 *    exact code the live sidebar uses.
 *
 * 2. A RUNNING TURN IS NEVER ORPHANED. Switching away from a chat whose turn
 *    is still running would leave that turn (and any tool call it cannot
 *    take back) running behind a session nothing can reach. The sidecar
 *    refuses it with 409; this refuses first, and says why, so the user is
 *    told "stop it first" rather than shown an error.
 *
 * 3. NOTHING IS LOST QUIETLY. Delete asks first and says what it does and
 *    does not touch. A chat nothing happened in is tidied away by the sidecar
 *    when it is left; anything with a prompt in it stays until the user
 *    deletes it.
 */

import { el, icon, appendAll, clear, setText, $ } from "./dom.js";

const COLLAPSED_KEY = "hearth.historyCollapsed"; // a layout preference, nothing more
const POLL_MS = 15000;
const ID_RE = /^[0-9a-f]{32}$/;

export const GROUPS = ["Today", "Previous 7 days", "Older"];

/** Which group a conversation last active at `updatedAt` (seconds since the
 *  epoch, as the sidecar reports it) falls in, by the LOCAL calendar: "Today"
 *  starts at local midnight, not 24 hours ago. A timestamp in the future (a
 *  clock that moved) counts as today rather than vanishing. */
export function groupFor(updatedAt, now = new Date()) {
  const t = Number(updatedAt) * 1000;
  if (!Number.isFinite(t)) return "Older";
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (t >= today) return "Today";
  const weekAgo = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7).getTime();
  return t >= weekAgo ? "Previous 7 days" : "Older";
}

export function displayTitle(item) {
  return item && typeof item.title === "string" && item.title ? item.title : "New chat";
}

/** The last path component, for a compact "which folder" hint. */
export function workspaceName(path) {
  const parts = String(path ?? "").split(/[\\/]+/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path ?? "");
}

export function whenLabel(updatedAt, now = new Date()) {
  const d = new Date(Number(updatedAt) * 1000);
  if (!Number.isFinite(d.getTime())) return "";
  return groupFor(updatedAt, now) === "Today"
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function matchesQuery(item, query) {
  const q = String(query ?? "").trim().toLowerCase();
  if (!q) return true;
  return [displayTitle(item), item.workspace, item.model]
    .some((v) => String(v ?? "").toLowerCase().includes(q));
}

/** One conversation row. `handlers` are {onOpen, onRename, onDelete}, each
 *  called with (item, li). */
export function renderConversationRow(item, { active = false, handlers = {} } = {}) {
  const title = displayTitle(item);
  const open = el("button", {
    class: "hist-open",
    type: "button",
    title,
    "aria-current": active ? "page" : null,
  }, [
    el("span", { class: "hist-title" + (item.title ? "" : " is-untitled"), text: title }),
    el("span", { class: "hist-meta" }, [
      active ? el("span", { class: "hist-live", title: "A turn is running" }) : null,
      item.engine && item.engine !== "chat"
        ? el("span", { class: "hist-tag", text: item.engine }) : null,
      el("span", { class: "hist-ws", text: workspaceName(item.workspace), title: item.workspace || "" }),
      el("span", { class: "hist-when", text: whenLabel(item.updated_at) }),
    ]),
  ]);

  const rename = el("button", {
    class: "btn btn-ghost btn-icon hist-act", type: "button",
    title: "Rename (F2)", "aria-label": "Rename " + title,
  }, [icon("i-pencil")]);
  const remove = el("button", {
    class: "btn btn-ghost btn-icon hist-act hist-act-danger", type: "button",
    title: "Delete (Del)", "aria-label": "Delete " + title,
  }, [icon("i-trash")]);

  const li = el("li", {
    class: "hist-item" + (active ? " is-active" : ""),
    dataset: { id: item.id },
  }, [open, el("div", { class: "hist-acts" }, [rename, remove])]);

  open.addEventListener("click", () => handlers.onOpen?.(item, li));
  open.addEventListener("keydown", (event) => {
    if (event.key === "F2") { event.preventDefault(); handlers.onRename?.(item, li); }
    if (event.key === "Delete") { event.preventDefault(); handlers.onDelete?.(item, li); }
  });
  rename.addEventListener("click", () => handlers.onRename?.(item, li));
  remove.addEventListener("click", () => handlers.onDelete?.(item, li));
  return li;
}

/** Every conversation matching `query`, grouped Today / Previous 7 days /
 *  Older, newest first within each group (the sidecar's order). */
export function renderHistoryList(items, { activeId = null, query = "", handlers = {}, now = new Date() } = {}) {
  const frag = document.createDocumentFragment();
  const shown = (items || []).filter((item) => matchesQuery(item, query));
  if (!shown.length) {
    frag.appendChild(el("p", {
      class: "hist-empty",
      text: String(query).trim() ? "No chats match." : "No saved chats yet. Start a session and it is kept here.",
    }));
    return frag;
  }
  for (const label of GROUPS) {
    const group = shown.filter((item) => groupFor(item.updated_at, now) === label);
    if (!group.length) continue;
    frag.appendChild(appendAll(el("section", { class: "hist-group", "aria-label": label }), [
      el("h3", { class: "hist-group-title", text: label }),
      appendAll(el("ul", { class: "hist-items" }),
        group.map((item) => renderConversationRow(item, { active: item.id === activeId, handlers }))),
    ]));
  }
  return frag;
}

function errorText(err) {
  return err && err.message ? err.message : String(err);
}

function readCollapsed() {
  try { return localStorage.getItem(COLLAPSED_KEY) === "1"; } catch { return false; }
}

function writeCollapsed(value) {
  try { localStorage.setItem(COLLAPSED_KEY, value ? "1" : "0"); } catch { /* private mode */ }
}

export class HistoryPanel {
  /** `root` is the empty <aside id="history">. Dependencies are injected so
   *  this module never reaches into app.js:
   *    sidecar        the shared Sidecar client (only request() is used)
   *    openModal      app.js's confirm dialog
   *    isRunning      () => whether the live session has a turn running
   *    hasSession     () => whether there is a live session at all
   *    onSwitched     (session, title, body) => adopt a session as the open one
   *    onCleared      () => the open conversation was deleted; show "no session"
   *    startFromForm  () => start a session from the sidebar form */
  constructor(root, deps) {
    this.root = root;
    this.sidecar = deps.sidecar;
    this.openModal = deps.openModal;
    this.isRunning = deps.isRunning || (() => false);
    this.hasSession = deps.hasSession || (() => true);
    this.onSwitched = deps.onSwitched;
    this.onCleared = deps.onCleared || (() => {});
    this.startFromForm = deps.startFromForm || (() => {});

    this.items = [];
    this.activeId = null;
    this.enabled = true;
    this.query = "";
    this.renaming = null;
    this.busy = false;
    this._inflight = null;
    this._again = false;
    this._soon = null;
    this._noteTimer = null;

    this._build();
    this.setCollapsed(readCollapsed());
    this._bindKeys();
    this.refresh();

    // The list changes from elsewhere too: a first prompt names a chat, a
    // finished turn moves it to the top. A slow poll catches what no event
    // here announces; app.js also asks for a refresh when a prompt lands.
    setInterval(() => {
      if (document.visibilityState === "visible" && !this.collapsed) this.refresh();
    }, POLL_MS);
    // Cheap and local: whether switching is possible right now. Drives the
    // "running" dot and the not-allowed cursor without asking the sidecar.
    setInterval(() => this.root.classList.toggle("is-running", Boolean(this.isRunning())), 500);
    window.addEventListener("focus", () => this.refresh());
  }

  _build() {
    this.toggleBtn = el("button", {
      class: "btn btn-ghost btn-icon hist-toggle", type: "button", "aria-controls": "hist-body",
    }, [icon("i-sidebar")]);
    this.newBtn = el("button", {
      class: "btn btn-ghost btn-icon hist-new", type: "button",
      title: "New chat (Ctrl+N)", "aria-label": "New chat",
    }, [icon("i-plus")]);
    this.queryInput = el("input", {
      class: "input hist-query", type: "search", placeholder: "Search chats",
      "aria-label": "Search chats", spellcheck: "false", autocomplete: "off",
    });
    // Until the first GET /conversations answers, say so: an empty sidebar
    // reads as "no saved chats", which may not be true.
    this.list = el("div", { class: "hist-list", "aria-busy": "true" }, [
      el("p", { class: "hist-empty", text: "Loading chats..." }),
    ]);
    this.noteEl = el("p", { class: "hist-note", role: "status", "aria-live": "polite", hidden: true });

    appendAll(this.root, [
      appendAll(el("div", { class: "hist-head" }), [
        this.toggleBtn,
        el("h2", { class: "hist-heading", text: "Chats" }),
        this.newBtn,
      ]),
      appendAll(el("div", { class: "hist-body", id: "hist-body" }), [
        appendAll(el("div", { class: "hist-search" }), [icon("i-search"), this.queryInput]),
        this.noteEl,
        this.list,
      ]),
    ]);

    this.toggleBtn.addEventListener("click", () => this.setCollapsed(!this.collapsed));
    this.newBtn.addEventListener("click", () => this.newChat());
    this.queryInput.addEventListener("input", () => {
      this.query = this.queryInput.value;
      this.render();
    });
    this.queryInput.addEventListener("keydown", (event) => {
      // Escape clears the search first. It must not reach app.js's own Escape
      // handler while there is a search to clear: that one stops the turn.
      if (event.key === "Escape" && this.queryInput.value) {
        event.preventDefault();
        event.stopPropagation();
        this.queryInput.value = "";
        this.query = "";
        this.render();
      }
    });
  }

  setCollapsed(collapsed) {
    this.collapsed = Boolean(collapsed);
    this.root.classList.toggle("is-collapsed", this.collapsed);
    this.toggleBtn.setAttribute("aria-expanded", String(!this.collapsed));
    const label = this.collapsed ? "Show chats" : "Hide chats";
    this.toggleBtn.setAttribute("aria-label", label);
    this.toggleBtn.title = label;
    writeCollapsed(this.collapsed);
    if (!this.collapsed) this.refresh();
  }

  _bindKeys() {
    document.addEventListener("keydown", (event) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
      if (event.key !== "n" && event.key !== "N") return;
      // Always swallowed: WebView2, like any Chromium, would otherwise treat
      // Ctrl+N as "open a new window".
      event.preventDefault();
      const scrim = $("#modal-scrim");
      if (scrim && !scrim.hidden) return;
      const chat = this.root.closest(".chat");
      if (chat && chat.hidden) return;
      this.newChat();
    });
  }

  note(text, kind = "") {
    clearTimeout(this._noteTimer);
    this.noteEl.className = "hist-note" + (kind ? " is-" + kind : "");
    setText(this.noteEl, text || "");
    this.noteEl.hidden = !text;
    if (text && kind !== "error") {
      this._noteTimer = setTimeout(() => { this.noteEl.hidden = true; }, 6000);
    }
  }

  refreshSoon(ms = 600) {
    clearTimeout(this._soon);
    this._soon = setTimeout(() => this.refresh(), ms);
  }

  /** Re-read the list. Calls that arrive while one is in flight share it and
   *  queue exactly one more, so a burst of triggers is two requests, not N,
   *  and an awaited refresh() always resolves after a render. */
  refresh() {
    if (this._inflight) {
      this._again = true;
      return this._inflight;
    }
    this._inflight = this._refresh().finally(() => {
      this._inflight = null;
      if (this._again) {
        this._again = false;
        this.refresh();
      }
    });
    return this._inflight;
  }

  async _refresh() {
    if (!this.sidecar.authenticated) return;
    try {
      const body = await this.sidecar.request("GET", "/conversations");
      this.enabled = body.enabled !== false;
      this.items = (Array.isArray(body.items) ? body.items : [])
        .filter((item) => item && typeof item.id === "string" && ID_RE.test(item.id));
      this.activeId = typeof body.active_id === "string" ? body.active_id : null;
      this.render();
    } catch (err) {
      if (!this.items.length) {
        if (this.list.getAttribute("aria-busy") === "true") clear(this.list);
        this.note("Could not read saved chats: " + errorText(err), "error");
      }
    } finally {
      this.list.removeAttribute("aria-busy");
    }
  }

  render() {
    // A re-render mid-rename would throw the user's typing away; the rename
    // finishing renders anyway.
    if (this.renaming) return;
    const focused = document.activeElement && this.list.contains(document.activeElement)
      ? document.activeElement.closest(".hist-item")?.dataset.id : null;
    clear(this.list);
    this.queryInput.disabled = !this.enabled;
    if (!this.enabled) {
      this.list.appendChild(el("p", { class: "hist-empty", text: "Saved chats are off in this build." }));
      return;
    }
    this.list.appendChild(renderHistoryList(this.items, {
      activeId: this.activeId,
      query: this.query,
      handlers: {
        onOpen: (item) => this.open(item),
        onRename: (item, li) => this.startRename(item, li),
        onDelete: (item) => this.confirmDelete(item),
      },
    }));
    if (focused && ID_RE.test(focused)) {
      this.list.querySelector(`.hist-item[data-id="${focused}"] .hist-open`)?.focus({ preventScroll: true });
    }
  }

  _blockedByRun(what) {
    if (!this.isRunning()) return false;
    this.note(`A turn is running in this chat. Stop it (Esc) before ${what}.`, "warn");
    return true;
  }

  async open(item) {
    if (item.id === this.activeId || this.busy) return;
    if (this._blockedByRun("switching")) return;
    this.busy = true;
    try {
      const body = await this.sidecar.request("POST", "/conversations/open", { id: item.id });
      this.activeId = body.active_id;
      const session = body.session;
      this.note("");
      await this.onSwitched(session, displayTitle(body.conversation || item),
        `${session.mode} mode in ${session.workspace}. Its saved history replays below.`);
    } catch (err) {
      this.note(errorText(err), "error");
    } finally {
      this.busy = false;
      this.refresh();
    }
  }

  async newChat() {
    if (this.busy) return;
    if (this._blockedByRun("starting a new chat")) return;
    if (!this.hasSession()) {
      // Nothing to copy settings from: the sidebar form is how a first
      // session is described, so use it.
      this.startFromForm();
      return;
    }
    this.busy = true;
    try {
      const body = await this.sidecar.request("POST", "/conversations/new", {});
      this.activeId = body.active_id;
      const session = body.session;
      this.note("");
      await this.onSwitched(session, "New chat",
        `${session.mode} mode in ${session.workspace}. It is saved as you go.`);
    } catch (err) {
      this.note(errorText(err), "error");
    } finally {
      this.busy = false;
      this.refresh();
    }
  }

  startRename(item, li) {
    if (this.renaming) return;
    const openBtn = li.querySelector(".hist-open");
    if (!openBtn) return;
    this.renaming = item.id;
    const input = el("input", {
      class: "input hist-rename", type: "text", value: item.title || "",
      maxlength: "80", spellcheck: "false", autocomplete: "off", "aria-label": "Chat title",
    });
    openBtn.replaceWith(input);
    li.classList.add("is-renaming");
    input.focus();
    input.select();

    let finished = false;
    const finish = async (save) => {
      if (finished) return;
      finished = true;
      const title = input.value.trim();
      if (save && title && title !== item.title) {
        try {
          const body = await this.sidecar.request("POST", "/conversations/rename", { id: item.id, title });
          const updated = body && body.conversation;
          const local = this.items.find((i) => i.id === item.id);
          if (local && updated) Object.assign(local, updated);
        } catch (err) {
          this.note("Could not rename: " + errorText(err), "error");
        }
      }
      this.renaming = null;
      this.render();
      this.list.querySelector(`.hist-item[data-id="${item.id}"] .hist-open`)?.focus({ preventScroll: true });
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
      // Escape cancels the rename. It must not bubble to app.js, whose
      // Escape handler stops a running turn.
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  }

  confirmDelete(item) {
    const isOpen = item.id === this.activeId;
    if (isOpen && this._blockedByRun("deleting it")) return;
    const body = [
      el("p", { text: `Delete "${displayTitle(item)}"?` }),
      el("p", { text: "The conversation and its saved history are removed for good. This cannot be undone." }),
      el("p", { text: isOpen
        ? "It is the chat that is open now, so the session ends too. Files in the workspace and its checkpoints are not touched."
        : "Files in the workspace and its checkpoints are not touched." }),
    ];
    this.openModal("Delete chat", body, [
      { label: "Cancel", variant: "btn-ghost" },
      { label: "Delete", variant: "btn-danger", run: () => this.remove(item) },
    ]);
  }

  async remove(item) {
    try {
      const body = await this.sidecar.request("POST", "/conversations/delete", { id: item.id });
      this.items = this.items.filter((i) => i.id !== item.id);
      if (body && body.cleared_session) {
        this.activeId = null;
        this.onCleared();
      }
      this.render();
    } catch (err) {
      this.note("Could not delete: " + errorText(err), "error");
    } finally {
      this.refresh();
    }
  }
}
