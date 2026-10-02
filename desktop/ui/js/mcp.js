/* The Tools screen: MCP servers, managed from the app instead of a JSON file.
 *
 * Four rules shape everything below.
 *
 * 1. UNTRUSTED TEXT. A server's name, command and arguments come from a
 *    config file anything running as this user can write, and its tool names,
 *    descriptions, error messages and stderr come from the server process
 *    itself. None of it is ever parsed as markup: every value reaches the
 *    page through dom.js's el({text}) / appendAll / setText, which also turn
 *    bidi overrides and control characters into visible <U+XXXX> markers, so
 *    a server cannot make its command read as something it is not on the very
 *    dialog that asks whether to run it. The render functions are exported so
 *    xss-check.mcp.js drives hostile values through the same code paths.
 *
 * 2. THE SIDECAR DECIDES. Every rule (what a command may look like, when a
 *    change needs an acknowledgement, when a change must wait for a turn to
 *    finish) lives in desktop/server/mcp_admin.py. This page asks, renders
 *    the answer, and when the answer is "needs_acknowledge" shows the dialog
 *    and asks again. It never decides on its own that something is safe.
 *
 * 3. SAYING WHAT RUNS. Adding a server lets a program run with the user's
 *    permissions. The confirmation says that in those words, shows the
 *    command and every argument as separate pieces (so an argument containing
 *    a space cannot pass for two), lists the sidecar's warnings, and leaves
 *    the new server disabled unless its own checkbox is ticked.
 *
 * 4. SECRETS STAY IN THE SIDECAR. A masked value arrives as a hint and a
 *    length, never the value. Saving one unchanged sends a keep sentinel; to
 *    change it, the user replaces it outright. This page never holds it.
 *
 * Nothing here opens a stream. A page gets six connections to its origin and
 * already holds five open forever (see app.js on why the update panel polls),
 * so this screen polls GET /mcp, and only while it is visible.
 */

import { HttpError } from "./api.js";
import { el, icon, appendAll, clear, setText } from "./dom.js";

const POLL_IDLE_MS = 5000;
const POLL_BUSY_MS = 1000;
const EDITOR_FIELDS = new Set(["key", "command", "args", "env"]);

/* What each live status means, in words. `idle` is the common case for an
 * enabled server: Hearth starts servers lazily, on the first turn that needs
 * tools, so "not running" is not a fault. */
export const STATUS = {
  running:  { label: "Running",                         cls: "is-running" },
  starting: { label: "Starting",                        cls: "is-busy" },
  idle:     { label: "Starts when a turn needs tools",  cls: "is-idle" },
  disabled: { label: "Disabled",                        cls: "is-off" },
  error:    { label: "Could not start",                 cls: "is-error" },
  exited:   { label: "Stopped unexpectedly",            cls: "is-error" },
  invalid:  { label: "Ignored: the entry is not valid", cls: "is-error" },
};

/* hearth's three risk classes, as permissions.decide applies them. The badge
 * shows the class; the tooltip says what it does, because "dangerous" alone
 * reads as a judgement about the server rather than as "asks first". */
export const RISK = {
  safe:      { label: "safe",      cls: "is-safe",
               title: "Read only: runs without asking, in every mode" },
  edit:      { label: "edit",      cls: "is-edit",
               title: "Runs without asking in auto mode, asks in edit mode, refused in plan mode" },
  dangerous: { label: "dangerous", cls: "is-danger",
               title: "Asks before every call, and is refused in plan mode" },
};

export function statusMeta(status) {
  return STATUS[status] || { label: String(status ?? "unknown"), cls: "is-idle" };
}

function riskMeta(risk) {
  return RISK[risk] || RISK.dangerous;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** A masked value as text: the hint the sidecar chose to show, and a length.
 *  Never more, because the page never has more. */
export function maskedText(item) {
  const length = plural(Number.isFinite(item?.length) ? item.length : 0, "character");
  const hint = typeof item?.hint === "string" ? item.hint : "";
  return hint ? `${hint}... hidden, ${length}` : `hidden, ${length}`;
}

// ------------------------------------------------------------------ pieces

export function renderStatus(server) {
  const meta = statusMeta(server.status);
  const parts = [meta.label];
  if (server.status === "running" && Number.isFinite(server.tool_count)) {
    parts.push(plural(server.tool_count, "tool"));
  }
  return appendAll(el("span", { class: "mcp-status " + meta.cls }), [
    el("span", { class: "mcp-dot", "aria-hidden": "true" }),
    el("span", { class: "mcp-status-text", text: parts.join(" \u00b7 ") }),
  ]);
}

/** The command and every argument as separate pills. Joining them with
 *  spaces would let `a b` (one argument) and `a`, `b` (two) look identical,
 *  which is exactly the ambiguity the confirmation dialog must not have. */
export function renderCommandLine(command, args) {
  const line = el("div", { class: "mcp-cmdline" });
  line.appendChild(el("code", { class: "mcp-pill mcp-pill-cmd", text: command ?? "(no command)" }));
  for (const arg of args || []) {
    if (arg && typeof arg === "object") {
      line.appendChild(appendAll(el("code", { class: "mcp-pill is-masked", title: "Hidden: this looks like a credential" }), [
        icon("i-key"), maskedText(arg),
      ]));
    } else {
      line.appendChild(el("code", { class: "mcp-pill", text: arg === "" ? "(empty)" : String(arg) }));
    }
  }
  return line;
}

export function renderEnvList(env) {
  const rows = Array.isArray(env) ? env : [];
  if (!rows.length) return null;
  const list = el("dl", { class: "mcp-env" });
  for (const row of rows) {
    list.appendChild(el("dt", { class: "mcp-env-k", text: row.name }));
    if (row.masked) {
      list.appendChild(appendAll(el("dd", { class: "mcp-env-v is-masked" }), [icon("i-key"), maskedText(row)]));
    } else {
      list.appendChild(el("dd", { class: "mcp-env-v", text: row.value === "" ? "(empty)" : row.value }));
    }
  }
  return list;
}

export function renderRiskBadge(risk) {
  const meta = riskMeta(risk);
  return el("span", { class: "mcp-risk " + meta.cls, text: meta.label, title: meta.title });
}

/** A server's tools: name, risk badge, and the server's own description. All
 *  three name and description strings are the server's words, not Hearth's. */
export function renderToolList(tools) {
  const list = el("ul", { class: "mcp-tool-list" });
  for (const tool of tools || []) {
    const head = appendAll(el("div", { class: "mcp-tool-head" }), [
      el("code", { class: "mcp-tool-name", text: tool.name, title: tool.full_name || null }),
      renderRiskBadge(tool.risk),
    ]);
    if (tool.derived && tool.derived !== tool.risk) {
      head.appendChild(el("span", {
        class: "mcp-tool-note",
        text: `tightened by config (server says ${tool.derived})`,
      }));
    }
    const item = appendAll(el("li", { class: "mcp-tool" }), [head]);
    if (tool.description) item.appendChild(el("p", { class: "mcp-tool-desc", text: tool.description }));
    list.appendChild(item);
  }
  return list;
}

/** The tool fold, with its open state carried across re-renders so a poll
 *  does not snap shut a list somebody is reading. */
function toolFold(id, title, tools, openSet) {
  const fold = el("details", { class: "fold mcp-fold", open: openSet.has(id) });
  fold.addEventListener("toggle", () => {
    if (fold.open) openSet.add(id); else openSet.delete(id);
  });
  return appendAll(fold, [el("summary", { text: title }), renderToolList(tools)]);
}

/** The last Test, as data the sidecar produced. */
export function renderTestResult(test, openSet = new Set(), key = "") {
  if (!test) return null;
  if (test.state === "running") {
    return appendAll(el("div", { class: "mcp-test is-running", role: "status" }), [
      el("span", { class: "mcp-spin", "aria-hidden": "true" }),
      el("span", { text: "Testing: starting a separate copy of the server and asking for its tools..." }),
    ]);
  }
  const ok = test.state === "ok";
  const box = el("div", { class: "mcp-test " + (ok ? "is-ok" : "is-failed"), role: "status" });
  const seconds = !Number.isFinite(test.elapsed) ? ""
    : test.elapsed < 1 ? ` in ${Math.max(1, Math.round(test.elapsed * 1000))} ms`
      : ` in ${test.elapsed.toFixed(1)}s`;
  if (ok) {
    const who = [test.server_info?.name, test.server_info?.version].filter(Boolean).join(" ");
    const facts = [`Handshake OK${seconds}`];
    if (who) facts.push(who);
    if (test.protocol) facts.push(`protocol ${test.protocol}`);
    facts.push(plural(Number(test.tool_count) || 0, "tool"));
    box.appendChild(appendAll(el("p", { class: "mcp-test-head" }), [icon("i-check"), facts.join(" \u00b7 ")]));
    if (test.guarded === false) {
      box.appendChild(el("p", {
        class: "mcp-test-note",
        text: "Hearth could not attach this server to its exit guard, so it may outlive a crash of Hearth.",
      }));
    }
    if (test.instructions) {
      box.appendChild(appendAll(el("p", { class: "mcp-test-note" }), [
        el("span", { class: "mcp-test-k", text: "The server describes itself as: " }),
        test.instructions,
      ]));
    }
    if (Array.isArray(test.tools) && test.tools.length) {
      box.appendChild(toolFold("test:" + key, `Tools it offers (${test.tools.length})`, test.tools, openSet));
    }
  } else {
    box.appendChild(appendAll(el("p", { class: "mcp-test-head" }), [icon("i-alert"), `Test failed${seconds}`]));
    box.appendChild(el("pre", { class: "mcp-error", text: test.error || "The server did not complete the handshake." }));
  }
  return box;
}

// -------------------------------------------------------------------- card

/** One server. `handlers` carries onToggle, onTest, onEdit, onRemove; the
 *  xss harness passes none and gets inert buttons. */
export function renderServerCard(server, handlers = {}, openSet = new Set()) {
  const enabled = Boolean(server.enabled);
  const testing = server.test?.state === "running";
  const card = el("article", {
    class: "mcp-card" + (enabled ? "" : " is-disabled") + (server.valid ? "" : " is-invalid"),
    "aria-label": "MCP server " + server.key,
  });

  const toggle = el("input", {
    class: "mcp-switch-input", type: "checkbox", role: "switch",
    checked: enabled && server.valid, disabled: !server.valid,
    "aria-label": !server.valid ? `${server.key} is ignored until it is fixed`
      : (enabled ? "Disable " : "Enable ") + server.key,
  });
  toggle.addEventListener("change", () => handlers.onToggle?.(server, toggle.checked, toggle));

  card.appendChild(appendAll(el("header", { class: "mcp-card-head" }), [
    appendAll(el("div", { class: "mcp-card-title" }), [
      el("h3", { class: "mcp-name", text: server.key }),
      renderStatus(server),
    ]),
    appendAll(el("label", { class: "mcp-switch", title: !server.valid ? "Ignored" : enabled ? "Enabled" : "Disabled" }), [
      toggle,
      el("span", { class: "mcp-switch-track", "aria-hidden": "true" }),
      el("span", { class: "mcp-switch-label", text: enabled && server.valid ? "On" : "Off" }),
    ]),
  ]));

  const body = el("div", { class: "mcp-card-body" });
  if (!server.valid) {
    body.appendChild(appendAll(el("p", { class: "mcp-problem" }), [
      icon("i-alert"),
      "Hearth ignores this entry: " + (server.problem || "it is not valid") + ". Edit it here, or fix it in the file.",
    ]));
  }
  body.appendChild(renderCommandLine(server.command, server.args));
  const env = renderEnvList(server.env);
  if (env) body.appendChild(env);

  const meta = [];
  if (server.cwd) meta.push(`runs in ${server.cwd}`);
  if (Number.isFinite(server.timeout)) meta.push(`${server.timeout}s per tool call`);
  if (server.risk_overrides) meta.push(`${plural(server.risk_overrides, "risk override")} in the file`);
  meta.push(`tools appear as ${server.prefix}...`);
  body.appendChild(el("p", { class: "mcp-meta", text: meta.join(" \u00b7 ") }));

  for (const warning of server.warnings || []) {
    body.appendChild(appendAll(el("p", { class: "mcp-warning" }), [icon("i-alert"), warning]));
  }
  if (server.error) body.appendChild(el("pre", { class: "mcp-error", text: server.error }));
  if (Array.isArray(server.tools) && server.tools.length) {
    body.appendChild(toolFold("live:" + server.key, `${plural(server.tools.length, "tool")} available to the model`,
      server.tools, openSet));
  }
  const test = renderTestResult(server.test, openSet, server.key);
  if (test) body.appendChild(test);
  card.appendChild(body);

  const testBtn = el("button", {
    class: "btn btn-sm", type: "button", disabled: !enabled || !server.valid || testing,
    title: !enabled ? "Enable it to test it: testing starts the program" : "Start a separate copy and list its tools",
  });
  if (testing) testBtn.appendChild(el("span", { class: "mcp-spin", "aria-hidden": "true" }));
  testBtn.appendChild(el("span", { text: testing ? "Testing" : "Test" }));
  testBtn.addEventListener("click", () => handlers.onTest?.(server, testBtn));

  const editBtn = el("button", { class: "btn btn-sm btn-ghost", type: "button" }, [icon("i-pencil"), el("span", { text: "Edit" })]);
  editBtn.addEventListener("click", () => handlers.onEdit?.(server));
  const removeBtn = el("button", { class: "btn btn-sm btn-ghost mcp-remove", type: "button" }, [icon("i-cross"), el("span", { text: "Remove" })]);
  removeBtn.addEventListener("click", () => handlers.onRemove?.(server));

  card.appendChild(appendAll(el("footer", { class: "mcp-card-actions" }), [
    testBtn, el("span", { class: "mcp-grow" }), editBtn, removeBtn,
  ]));
  const note = el("p", { class: "mcp-card-note", role: "status" });
  card.appendChild(note);
  return card;
}

// --------------------------------------------------------- acknowledgement

/** The body of the "this runs a program" dialog. Returns the nodes and, for a
 *  new server, the enable checkbox (unticked: adding and starting are two
 *  decisions). `info` is the sidecar's own needs_acknowledge refusal, so the
 *  command shown is the one the sidecar will store, not the page's copy. */
export function renderAcknowledgeBody(info) {
  const command = String(info.command ?? "");
  const nodes = [
    appendAll(el("p"), [
      "Saving this lets ",
      el("strong", { class: "mcp-ack-cmd", text: command }),
      " run on your computer with your permissions. Hearth starts it whenever a turn needs tools, "
        + "and it can do anything you can: read and change your files, use the network, and start other programs.",
    ]),
    renderCommandLine(command, info.args),
  ];
  const envNames = Array.isArray(info.env_names) ? info.env_names : [];
  if (envNames.length) {
    // Names only: a value may be a credential, and this dialog is about what
    // runs, which the names say.
    nodes.push(appendAll(el("p", { class: "mcp-ack-env" }), [
      "With these environment variables set: ",
      el("code", { class: "mcp-ack-env-names", text: envNames.join(", ") }),
    ]));
  }
  for (const warning of info.warnings || []) {
    nodes.push(appendAll(el("p", { class: "mcp-warning" }), [icon("i-alert"), warning]));
  }
  nodes.push(el("p", {
    class: "mcp-ack-note",
    text: "Only add a server you trust, from a source you trust. Hearth asks before running any of its tools "
      + "that does not promise to be read only, but it cannot limit what the program itself does.",
  }));
  let enableBox = null;
  if (info.create) {
    enableBox = el("input", { type: "checkbox", class: "mcp-ack-enable" });
    nodes.push(appendAll(el("label", { class: "mcp-ack-enable-row" }), [
      enableBox,
      el("span", { text: "Enable it now. Otherwise it is saved switched off, and nothing runs until you turn it on." }),
    ]));
  }
  return { nodes, enableBox };
}

// ------------------------------------------------------------------ editor

/** The add/edit form. Builds rows for arguments and environment variables;
 *  a masked value from the sidecar becomes a locked row that saves as a keep
 *  sentinel until the user chooses to replace it. */
export class ServerEditor {
  constructor(server, { onSave, onCancel } = {}) {
    this.server = server;           // null when adding
    this.onSave = onSave || (() => {});
    this.onCancel = onCancel || (() => {});
    this.argRows = [];
    this.envRows = [];
    this.root = this._build();
  }

  _build() {
    const editing = Boolean(this.server);
    this.form = el("form", { class: "mcp-editor", novalidate: true });
    this.form.addEventListener("submit", (event) => { event.preventDefault(); this.onSave(this); });
    this.form.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); this.onCancel(this); }
    });

    this.keyInput = el("input", {
      class: "input mono", type: "text", spellcheck: false, autocomplete: "off",
      maxlength: "48", placeholder: "filesystem", value: editing ? this.server.key : "",
      readonly: editing, "aria-label": "Server name",
    });
    this.commandInput = el("input", {
      class: "input mono", type: "text", spellcheck: false, autocomplete: "off",
      placeholder: "npx, or C:\\path\\to\\server.exe", value: editing ? (this.server.command || "") : "",
      "aria-label": "Command",
    });
    // A pasted Windows path often arrives wrapped in quotes. Taking them off
    // in the field, visibly, is kinder than a refusal and changes nothing the
    // user cannot see. Anything else stays exactly as typed; the sidecar
    // decides whether it is acceptable.
    this.commandInput.addEventListener("blur", () => {
      const v = this.commandInput.value.trim();
      const unquoted = /^"[^"]*"$/.test(v) ? v.slice(1, -1) : v;
      if (unquoted !== this.commandInput.value) this.commandInput.value = unquoted;
    });

    this.argList = el("div", { class: "mcp-rows" });
    this.envList = el("div", { class: "mcp-rows" });
    for (const [i, arg] of (editing ? this.server.args || [] : []).entries()) this.addArg(arg, i);
    for (const row of editing ? this.server.env || [] : []) this.addEnv(row);

    const addArg = el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "+ Argument" });
    addArg.addEventListener("click", () => this.addArg("", null, true));
    const addEnv = el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "+ Variable" });
    addEnv.addEventListener("click", () => this.addEnv(null, true));

    this.error = el("p", { class: "mcp-form-error", role: "alert", hidden: true });
    this.saveBtn = el("button", { class: "btn btn-primary", type: "submit", text: editing ? "Save changes" : "Add server" });
    const cancel = el("button", { class: "btn btn-ghost", type: "button", text: "Cancel" });
    cancel.addEventListener("click", () => this.onCancel(this));

    const field = (label, hint, control, name) => appendAll(el("div", { class: "mcp-field", dataset: { field: name } }), [
      el("span", { class: "field-label", text: label }),
      control,
      hint ? el("span", { class: "mcp-hint", text: hint }) : null,
    ]);

    return appendAll(this.form, [
      el("h2", { class: "mcp-editor-title", text: editing ? `Edit ${this.server.key}` : "Add an MCP server" }),
      field("Name", editing
        ? "The name cannot be changed; remove the server and add it again to rename it."
        : "Letters, digits, - and _. Its tools appear to the model as mcp__<name>__<tool>.",
      this.keyInput, "key"),
      field("Command", "One program: a name on your PATH, or the full path to it. Not a command line: "
        + "arguments go below, one per row. On Windows, npm servers usually need cmd as the command, "
        + "with /c, npx, -y and the package as arguments.", this.commandInput, "command"),
      appendAll(el("div", { class: "mcp-field", dataset: { field: "args" } }), [
        el("span", { class: "field-label", text: "Arguments" }),
        this.argList,
        addArg,
      ]),
      appendAll(el("div", { class: "mcp-field", dataset: { field: "env" } }), [
        el("span", { class: "field-label", text: "Environment variables" }),
        el("span", { class: "mcp-hint", text: "Added to the small environment Hearth gives the server. "
          + "Values that look like credentials are hidden after saving." }),
        this.envList,
        addEnv,
      ]),
      this.error,
      appendAll(el("div", { class: "mcp-editor-actions" }), [cancel, this.saveBtn]),
    ]);
  }

  focus() {
    (this.server ? this.commandInput : this.keyInput).focus({ preventScroll: false });
  }

  _removeButton(label, onRemove) {
    const btn = el("button", { class: "btn btn-ghost btn-icon btn-sm mcp-row-remove", type: "button", title: label, "aria-label": label }, [icon("i-cross")]);
    btn.addEventListener("click", onRemove);
    return btn;
  }

  addArg(value, index = null, focus = false) {
    const row = { node: el("div", { class: "mcp-row" }), keep: null, input: null };
    const remove = this._removeButton("Remove this argument", () => {
      row.node.remove();
      this.argRows = this.argRows.filter((r) => r !== row);
    });
    const asInput = (initial) => {
      row.keep = null;
      row.input = el("input", {
        class: "input mono", type: "text", spellcheck: false, autocomplete: "off",
        value: initial, "aria-label": "Argument",
      });
      return row.input;
    };
    if (value && typeof value === "object" && value.masked) {
      row.keep = Number.isInteger(value.index) ? value.index : index;
      const replace = el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "Replace" });
      const locked = appendAll(el("span", { class: "mcp-locked mono" }), [icon("i-key"), maskedText(value)]);
      replace.addEventListener("click", () => {
        const input = asInput("");
        locked.replaceWith(input);
        replace.remove();
        input.focus();
      });
      appendAll(row.node, [locked, replace, remove]);
    } else {
      appendAll(row.node, [asInput(String(value ?? "")), remove]);
    }
    this.argList.appendChild(row.node);
    this.argRows.push(row);
    if (focus) row.input?.focus();
  }

  addEnv(existing = null, focus = false) {
    const row = { node: el("div", { class: "mcp-row mcp-row-env" }), keep: false, name: null, value: null };
    const remove = this._removeButton("Remove this variable", () => {
      row.node.remove();
      this.envRows = this.envRows.filter((r) => r !== row);
    });
    row.name = el("input", {
      class: "input mono mcp-env-name", type: "text", spellcheck: false, autocomplete: "off",
      placeholder: "NAME", value: existing ? existing.name : "", "aria-label": "Variable name",
    });
    const valueInput = (initial) => el("input", {
      class: "input mono", type: "text", spellcheck: false, autocomplete: "off",
      placeholder: "value", value: initial, "aria-label": "Variable value",
    });
    if (existing && existing.masked) {
      // The name is fixed while the value is a kept secret: the sidecar keeps
      // a value by name, so renaming would orphan it.
      row.keep = true;
      row.name.readOnly = true;
      const replace = el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "Replace" });
      const locked = appendAll(el("span", { class: "mcp-locked mono" }), [icon("i-key"), maskedText(existing)]);
      replace.addEventListener("click", () => {
        row.keep = false;
        row.name.readOnly = false;
        row.value = valueInput("");
        locked.replaceWith(row.value);
        replace.remove();
        row.value.focus();
      });
      appendAll(row.node, [row.name, locked, replace, remove]);
    } else {
      row.value = valueInput(existing ? String(existing.value ?? "") : "");
      appendAll(row.node, [row.name, row.value, remove]);
    }
    this.envList.appendChild(row.node);
    this.envRows.push(row);
    if (focus) row.name.focus();
  }

  /** The POST /mcp/save body, minus the acknowledgement. Empty rows (no name
   *  and no value) are dropped rather than sent as errors: an "+ Argument"
   *  pressed once too often is not a mistake worth a refusal. */
  read() {
    const args = [];
    for (const row of this.argRows) {
      if (row.keep !== null) args.push({ keep: row.keep });
      else if (row.input) args.push(row.input.value);
    }
    while (args.length && args[args.length - 1] === "") args.pop();
    const env = {};
    for (const row of this.envRows) {
      const name = row.name.value.trim();
      if (row.keep) { env[name] = { keep: true }; continue; }
      const value = row.value ? row.value.value : "";
      if (!name && !value) continue;
      env[name] = value;
    }
    const body = { key: this.keyInput.value.trim(), command: this.commandInput.value.trim(), args, env };
    if (!this.server) body.create = true;
    return body;
  }

  showError(message, field) {
    for (const node of this.form.querySelectorAll(".mcp-field.is-wrong")) node.classList.remove("is-wrong");
    if (!message) { this.error.hidden = true; return; }
    setText(this.error, message.charAt(0).toUpperCase() + message.slice(1));
    this.error.hidden = false;
    const target = EDITOR_FIELDS.has(field) && this.form.querySelector(`.mcp-field[data-field="${field}"]`);
    if (target) target.classList.add("is-wrong");
  }

  setBusy(busy) {
    this.saveBtn.disabled = busy;
  }
}

// ------------------------------------------------------------------- panel

export class McpPanel {
  constructor(root, { sidecar, openModal, closeModal, isRunning } = {}) {
    this.root = root;
    this.sidecar = sidecar;
    this.openModal = openModal;
    this.closeModal = closeModal;
    this.isRunning = isRunning || (() => false);
    this.listing = null;
    this.lastJson = "";
    this.editor = null;
    this.openSet = new Set();
    this.timer = null;
    this.loading = false;
    this.again = false;
    this.cards = new Map();
    // Per-server messages (a refused toggle, a refused test) live here rather
    // than only in the DOM, so the re-render that follows every action does
    // not wipe the explanation the user needs to read.
    this.cardNotes = new Map();
    this._build();
  }

  _build() {
    clear(this.root);
    this.addBtn = el("button", { class: "btn btn-primary", type: "button" }, [el("span", { text: "Add server" })]);
    this.addBtn.addEventListener("click", () => this.edit(null));
    this.refreshBtn = el("button", { class: "btn btn-ghost btn-icon", type: "button", title: "Refresh", "aria-label": "Refresh" }, [icon("i-refresh")]);
    this.refreshBtn.addEventListener("click", () => this.refresh());

    this.notes = el("div", { class: "mcp-notes" });
    this.flash = el("p", { class: "mcp-flash", role: "status", "aria-live": "polite" });
    this.editorSlot = el("div", { class: "mcp-editor-slot" });
    this.list = el("div", { class: "mcp-list" });

    this.root.appendChild(appendAll(el("div", { class: "mcp-inner" }), [
      appendAll(el("header", { class: "mcp-head" }), [
        appendAll(el("div", { class: "mcp-head-text" }), [
          el("h1", { class: "mcp-title", text: "Tools" }),
          el("p", {
            class: "mcp-lede",
            text: "MCP servers are programs that give the model extra tools, like a game editor or a "
              + "database. Each one runs on this computer with your permissions, so add only ones you "
              + "trust. Hearth asks before running any tool that does not promise to be read only.",
          }),
        ]),
        appendAll(el("div", { class: "mcp-head-actions" }), [this.refreshBtn, this.addBtn]),
      ]),
      this.notes,
      this.flash,
      this.editorSlot,
      this.list,
    ]));
    this.list.appendChild(el("p", { class: "mcp-empty-note", text: "Loading..." }));
  }

  /** Called by setView when the tab is chosen. Refreshes at once and keeps a
   *  light poll going only for as long as the screen is visible. */
  show() {
    this.refresh();
  }

  _schedule() {
    clearTimeout(this.timer);
    if (this.root.hidden) return;
    const busy = (this.listing?.servers || []).some((s) => s.test?.state === "running" || s.status === "starting");
    this.timer = setTimeout(() => this.refresh(), busy ? POLL_BUSY_MS : POLL_IDLE_MS);
  }

  async refresh() {
    // One request at a time. A refresh asked for while one is in flight runs
    // straight after it, so the screen never settles on a reading taken
    // before the action the user just performed.
    if (this.loading) { this.again = true; return; }
    this.loading = true;
    this.again = false;
    try {
      const listing = await this.sidecar.request("GET", "/mcp");
      this.listing = listing;
      const json = JSON.stringify(listing) + String(this.isRunning());
      // Re-render only on change: a re-render rebuilds every button, which
      // would yank keyboard focus out from under someone tabbing through.
      if (json !== this.lastJson) {
        this.lastJson = json;
        this._render();
      }
    } catch (err) {
      clear(this.notes);
      this.notes.appendChild(this._banner("Could not read the MCP servers: " + errorText(err), "is-error"));
      this.lastJson = "";
    } finally {
      this.loading = false;
      if (this.again) this.refresh(); else this._schedule();
    }
  }

  _banner(text, cls = "", extra = []) {
    return appendAll(el("div", { class: "mcp-banner " + cls }), [icon("i-alert"), appendAll(el("div"), [el("p", { text }), ...extra])]);
  }

  _render() {
    const listing = this.listing || { servers: [] };
    clear(this.notes);
    this.notes.appendChild(appendAll(el("p", { class: "mcp-path" }), [
      el("span", { text: listing.exists ? "Saved in " : "Will be saved in " }),
      el("code", { text: listing.config_path || "mcp.json" }),
    ]));
    if (listing.problem) {
      this.notes.appendChild(this._banner(listing.problem, "is-error", [
        el("p", { text: "Nothing here can change the file until it can be read. Fix it by hand, or move it aside to start fresh." }),
      ]));
    }
    if (this.isRunning()) {
      this.notes.appendChild(el("p", {
        class: "mcp-busy-note",
        text: "A turn is running. Changes to enabled servers wait until it finishes, so a tool call is never cut off.",
      }));
    }
    this.addBtn.disabled = Boolean(listing.problem) || Boolean(this.editor);

    clear(this.list);
    this.cards.clear();
    const servers = listing.servers || [];
    if (!servers.length && !listing.problem) {
      const add = el("button", { class: "btn btn-primary", type: "button", text: "Add your first server" });
      add.addEventListener("click", () => this.edit(null));
      this.list.appendChild(appendAll(el("div", { class: "mcp-empty" }), [
        el("h2", { text: "No MCP servers yet" }),
        el("p", { text: "Without one, the model has Hearth's built-in tools only: reading, writing and "
          + "searching files and running commands in the workspace. A server adds tools of its own, "
          + "and you can test it here before any model sees it." }),
        add,
      ]));
      return;
    }
    const handlers = {
      onToggle: (server, on, input) => this.toggle(server, on, input),
      onTest: (server, button) => this.test(server, button),
      onEdit: (server) => this.edit(server),
      onRemove: (server) => this.remove(server),
    };
    for (const server of servers) {
      const card = renderServerCard(server, handlers, this.openSet);
      const note = this.cardNotes.get(server.key);
      if (note) setText(card.querySelector(".mcp-card-note"), note);
      this.cards.set(server.key, card);
      this.list.appendChild(card);
    }
  }

  _say(text, kind = "") {
    this.flash.className = "mcp-flash " + kind;
    setText(this.flash, text);
  }

  _cardNote(server, text) {
    if (text) this.cardNotes.set(server.key, text); else this.cardNotes.delete(server.key);
    const note = this.cards.get(server.key)?.querySelector(".mcp-card-note");
    if (note) setText(note, text || "");
  }

  // ---------------------------------------------------------------- actions

  edit(server) {
    clear(this.editorSlot);
    this.editor = new ServerEditor(server, {
      onSave: (editor) => this.save(editor),
      onCancel: () => this.closeEditor(),
    });
    this.editorSlot.appendChild(this.editor.root);
    this.addBtn.disabled = true;
    this.editor.root.scrollIntoView({ block: "nearest" });
    this.editor.focus();
  }

  closeEditor() {
    clear(this.editorSlot);
    this.editor = null;
    this.addBtn.disabled = Boolean(this.listing?.problem);
    this.addBtn.focus({ preventScroll: true });
  }

  async save(editor, extra = {}) {
    const body = Object.assign(editor.read(), extra);
    editor.showError(null);
    editor.setBusy(true);
    try {
      const out = await this.sidecar.request("POST", "/mcp/save", body);
      this.closeEditor();
      this._say(!out?.changed ? "Nothing changed."
        : out.restarted ? `Saved ${body.key}. MCP servers start again, with these settings, on the next turn that needs tools.`
          : `Saved ${body.key}.`, "is-ok");
      this.lastJson = "";
      await this.refresh();
    } catch (err) {
      const info = err instanceof HttpError ? err.body : null;
      if (info?.needs_acknowledge && !extra.acknowledge) {
        this._confirmRunsProgram(editor, info);
        return;
      }
      editor.showError(errorText(err), info?.field);
    } finally {
      editor.setBusy(false);
    }
  }

  _confirmRunsProgram(editor, info) {
    const { nodes, enableBox } = renderAcknowledgeBody(info);
    this.openModal(info.create ? "Let this program run on your computer?" : "Run the changed program?", nodes, [
      { label: "Cancel", variant: "btn-ghost", run: () => editor.focus() },
      {
        label: info.create ? "Allow and add" : "Allow and save",
        variant: "btn-danger",
        run: () => {
          const extra = { acknowledge: info.acknowledge || "runs-program" };
          if (info.create) extra.enabled = Boolean(enableBox?.checked);
          this.save(editor, extra);
        },
      },
    ]);
  }

  async toggle(server, on, input) {
    input.disabled = true;
    try {
      await this.sidecar.request("POST", "/mcp/toggle", { key: server.key, enabled: on });
      this._cardNote(server, "");
      this._say(on ? `${server.key} is on. It starts with the next turn that needs tools.`
        : `${server.key} is off, and Hearth stopped it.`, "is-ok");
    } catch (err) {
      input.checked = !on;
      this._cardNote(server, errorText(err));
    } finally {
      input.disabled = false;
      this.lastJson = "";
      this.refresh();
    }
  }

  async test(server, button) {
    button.disabled = true;
    try {
      await this.sidecar.request("POST", "/mcp/test", { key: server.key });
      this._cardNote(server, "");
    } catch (err) {
      this._cardNote(server, errorText(err));
      button.disabled = false;
      return;
    }
    this.lastJson = "";
    this.refresh();
  }

  remove(server) {
    this.openModal(`Remove ${server.key}?`, [
      appendAll(el("p"), [
        "Hearth stops ", el("strong", { text: server.key }),
        " if it is running and deletes its entry from the config file, including its environment values. "
          + "This cannot be undone.",
      ]),
      renderCommandLine(server.command, server.args),
    ], [
      { label: "Cancel", variant: "btn-ghost" },
      {
        label: "Remove", variant: "btn-danger",
        run: async () => {
          try {
            await this.sidecar.request("POST", "/mcp/remove", { key: server.key });
            this._say(`Removed ${server.key}.`, "is-ok");
            this.cardNotes.delete(server.key);
            this.openSet.delete("live:" + server.key);
            this.openSet.delete("test:" + server.key);
          } catch (err) {
            this._say(errorText(err), "is-error");
          }
          this.lastJson = "";
          this.refresh();
        },
      },
    ]);
  }
}

function errorText(err) {
  if (err instanceof HttpError) return err.message;
  if (err && err.message) return err.message;
  return String(err);
}
