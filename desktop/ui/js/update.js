/* The Updates panel.
 *
 * Everything this draws comes from a release manifest that was signed with a
 * key pinned inside the application, so a release host cannot put words on
 * this screen. That is a statement about who WROTE the text, not about
 * whether the text is safe to render, and the two are different questions:
 * a signing key can leak, an operator can paste something odd into a release
 * note, and a UI whose safety depends on the honesty of remote content is not
 * safe. So the release notes and the version string go through `el`'s `text:`
 * like every other untrusted string in this UI, which means textContent and
 * dom.js's `neutralize` -- no markup, and no bidi override that could make
 * the version on screen read differently from the version being installed.
 *
 * Release notes are rendered as plain preformatted text rather than through
 * safe-text.js's markdown tokenizer. Not because the tokenizer is unsafe (it
 * only ever sets textContent either), but because release notes are short and
 * the smallest render path that does the job is the one worth having on the
 * screen that talks the user into running an installer.
 *
 * THE HONESTY RULE, which is most of why this file is not three lines. There
 * are four different things this panel can be looking at and they must never
 * be confused with each other:
 *
 *   * "this build has no release feed"  -- nothing has been published, so
 *     Hearth cannot check. It says so.
 *   * "the check failed"                -- the feed was unreachable, or the
 *     manifest was refused. It says which, and it says what version is
 *     running, because a failed check is not evidence of being up to date.
 *   * "up to date"                      -- a signed manifest verified and it
 *     describes what is already installed.
 *   * "an update is available"          -- a signed manifest verified and it
 *     describes something newer.
 *
 * Rendering the first two as a quiet "you're up to date" is the single most
 * common way an updater lies to people, and it is exactly the state an
 * attacker who can drop traffic wants the user to see.
 */

import { el, clear } from "./dom.js";

const STATE_TEXT = {
  unconfigured: "no release feed",
  idle: "not checked yet",
  checking: "checking…",
  "up-to-date": "up to date",
  available: "update available",
  downloading: "downloading",
  ready: "ready to install",
  failed: "check failed",
};

function megabytes(bytes) {
  const n = Number(bytes) || 0;
  return `${(n / 1e6).toFixed(0)} MB`;
}

/** The whole panel body for one snapshot. Pure: it reads the snapshot and
 *  the handlers and touches nothing else, so xss-check.html can drive it
 *  with hostile input and inspect exactly what one call produced. */
export function updateCard(snapshot, handlers = {}) {
  const snap = snapshot && typeof snapshot === "object" ? snapshot : {};
  const state = String(snap.state ?? "idle");
  const running = String(snap.current_version ?? "") || "unknown";
  const root = el("div", { class: "update-body is-" + state });

  root.appendChild(el("div", { class: "update-head" }, [
    el("span", { class: "update-version", text: `Hearth ${running}` }),
    el("span", {
      class: "update-state" + (state === "failed" ? " is-bad"
        : state === "available" || state === "ready" ? " is-new"
        : state === "up-to-date" ? " is-ok" : ""),
      text: STATE_TEXT[state] ?? state,
    }),
  ]));

  // The message the sidecar wrote. It is the honest sentence in every branch:
  // "could not check", "expired", "refusing this update", "is the newest
  // release". Preferring `error` when there is one means a failure is never
  // papered over by a stale success message.
  const line = snap.error || snap.message || "";
  if (line) root.appendChild(el("p", { class: "update-note", text: String(line) }));

  if (snap.trust_error) {
    root.appendChild(el("p", {
      class: "update-note is-bad",
      text: `Hearth cannot read its own update trust file: ${String(snap.trust_error)}`,
    }));
  }

  const available = snap.available && typeof snap.available === "object" ? snap.available : null;
  const staged = snap.staged && typeof snap.staged === "object" ? snap.staged : null;
  const offered = staged || available;

  if (offered) {
    const meta = [];
    if (offered.released_at) meta.push(`released ${String(offered.released_at)}`);
    if (offered.size_bytes) meta.push(megabytes(offered.size_bytes));
    if (snap.signed_by || offered.signed_by) {
      meta.push(`signed by ${String(offered.signed_by ?? snap.signed_by)}`);
    }
    if (meta.length) {
      root.appendChild(el("p", { class: "update-meta", text: meta.join(" · ") }));
    }
    const notes = String((available && available.notes) || "");
    if (notes.trim()) {
      root.appendChild(el("pre", { class: "update-notes", text: notes }));
    }
  }

  if (state === "downloading") {
    const total = Number(snap.bytes_total) || 0;
    const done = Number(snap.bytes_done) || 0;
    const pct = total > 0 ? Math.max(0, Math.min(100, (done / total) * 100)) : 0;
    const fill = el("div", { class: "bar-fill" });
    fill.style.width = `${pct.toFixed(1)}%`;
    root.appendChild(el("div", { class: "bar" }, [fill]));
    root.appendChild(el("span", {
      class: "update-pct",
      text: total > 0 ? `${pct.toFixed(0)}% of ${megabytes(total)}` : "starting…",
    }));
  }

  if (staged && staged.sha256) {
    // Shown, not hidden behind a details pane. This is the number the shell
    // recomputes immediately before it runs the file, and a person who wants
    // to check it by hand against the download page can.
    root.appendChild(el("p", {
      class: "update-hash mono",
      text: `SHA-256 ${String(staged.sha256)}`,
    }));
  }

  root.appendChild(buttons(snap, state, handlers));

  if (snap.configured) {
    const box = el("input", { type: "checkbox", id: "update-auto", class: "update-auto" });
    box.checked = snap.auto_check !== false;
    box.addEventListener("change", () => {
      if (handlers.onAutoCheck) handlers.onAutoCheck(box.checked);
    });
    const label = el("label", { class: "update-auto-row", for: "update-auto" });
    label.appendChild(box);
    label.appendChild(el("span", {
      text: "Check for updates automatically. Nothing is ever downloaded or "
        + "installed without asking.",
    }));
    root.appendChild(label);
  }

  return root;
}

function buttons(snap, state, handlers) {
  const row = el("div", { class: "update-actions" });
  const add = (text, kind, fn, extra = {}) => {
    if (!fn) return;
    const button = el("button", { class: `btn ${kind} btn-sm`, type: "button", text, ...extra });
    button.addEventListener("click", () => fn(button));
    row.appendChild(button);
  };

  if (state === "unconfigured") {
    // No button at all. There is nothing to check, and a "check now" that is
    // guaranteed to fail is worse than no button.
    return row;
  }
  if (state === "ready") {
    add("Install and restart", "btn-primary", handlers.onInstall);
    add("Not now", "btn-ghost", handlers.onDismiss);
    return row;
  }
  if (state === "available") {
    add(`Download ${String((snap.available || {}).version ?? "")}`.trim(),
      "btn-primary", handlers.onDownload);
    add("Not now", "btn-ghost", handlers.onDismiss);
    return row;
  }
  if (state === "downloading") {
    add("Cancel", "btn-ghost", handlers.onCancel);
    return row;
  }
  add(state === "failed" ? "Try again" : "Check for updates", "btn-ghost", handlers.onCheck,
    { disabled: state === "checking" });
  return row;
}

/** Draw the panel into `node`, replacing whatever was there. */
export function renderUpdate(node, snapshot, handlers) {
  if (!node) return null;
  clear(node);
  const body = updateCard(snapshot, handlers);
  node.appendChild(body);
  return body;
}
