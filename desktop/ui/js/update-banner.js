/* The update banner: "Hearth X.Y.Z is available", once, at the top of the app.
 *
 * The Updates panel in the sidebar is the full story (every state, the hash,
 * the auto-check setting). This is the one sentence most people need, put
 * where they will see it, and it shows ONLY when there is something to act
 * on: an update a signed manifest verified, one that is downloading, or one
 * that is verified and staged. Never for "could not check", never for "no
 * release feed", never for a refusal. Those stay in the panel, where the
 * honest wording lives; a banner that appeared for a failure would either
 * alarm people on every offline launch or teach them to dismiss it.
 *
 * Everything shown here (the version, the release notes) comes from a signed
 * manifest, and is still rendered as text through dom.js's `el`, exactly like
 * update.js: who wrote a string and whether it is safe to render are
 * different questions. Notes are cut to a short excerpt; the panel has the
 * full text.
 *
 * "Later" hides the banner until the next launch and changes nothing else.
 * It is a module-level flag rather than a persisted setting on purpose: an
 * update that matters (a security fix) should be offered again next time.
 */

import { el, clear, $ } from "./dom.js";

/** Banner-worthy states. Everything else hides it. */
const SHOWN = new Set(["available", "downloading", "ready"]);

/** Notes beyond this are cut, at a word boundary, with an ellipsis. */
const NOTES_EXCERPT_CHARS = 220;

let hiddenForLaunch = false;

/** Hide the banner until Hearth is next started. */
export function hideUpdateBannerForLaunch() {
  hiddenForLaunch = true;
}

/** Undo a "Later", for when the user starts an install from the panel and
 *  should see its progress up top as well. */
export function showUpdateBannerAgain() {
  hiddenForLaunch = false;
}

function excerpt(text) {
  const flat = String(text ?? "").replace(/\s+/g, " ").trim();
  if (flat.length <= NOTES_EXCERPT_CHARS) return flat;
  const cut = flat.slice(0, NOTES_EXCERPT_CHARS);
  const space = cut.lastIndexOf(" ");
  return (space > NOTES_EXCERPT_CHARS / 2 ? cut.slice(0, space) : cut) + "…";
}

function megabytes(bytes) {
  return `${((Number(bytes) || 0) / 1e6).toFixed(0)} MB`;
}

/** The banner body for one snapshot, or null when there is nothing to show.
 *  Pure apart from reading the "Later" flag, so the XSS harness can drive it
 *  with hostile input and inspect exactly what one call produced. */
export function updateBannerCard(snapshot, handlers = {}) {
  const snap = snapshot && typeof snapshot === "object" ? snapshot : {};
  const state = String(snap.state ?? "");
  if (!SHOWN.has(state)) return null;

  const available = snap.available && typeof snap.available === "object" ? snap.available : null;
  const staged = snap.staged && typeof snap.staged === "object" ? snap.staged : null;
  const offered = staged || available;
  const version = String((offered && offered.version) ?? "").trim();
  if (!version) return null;

  const title = state === "ready" ? `Hearth ${version} is ready to install`
    : state === "downloading" ? `Downloading Hearth ${version}`
    : `Hearth ${version} is available`;

  const root = el("div", { class: "update-banner-body" });
  root.appendChild(el("p", { class: "update-banner-title", text: title }));

  const notes = excerpt(available && available.notes);
  if (notes && state !== "downloading") {
    root.appendChild(el("p", { class: "update-banner-notes", text: notes }));
  }

  if (state === "downloading") {
    const total = Number(snap.bytes_total) || 0;
    const done = Number(snap.bytes_done) || 0;
    const pct = total > 0 ? Math.max(0, Math.min(100, (done / total) * 100)) : 0;
    const fill = el("div", { class: "bar-fill" });
    fill.style.width = `${pct.toFixed(1)}%`;
    root.appendChild(el("div", { class: "bar update-banner-bar" }, [fill]));
    root.appendChild(el("p", {
      class: "update-banner-pct",
      text: total > 0
        ? `${pct.toFixed(0)}% of ${megabytes(total)}. Its SHA-256 is checked against the signed release as it arrives.`
        : "Starting the download…",
    }));
  } else if (state === "ready") {
    root.appendChild(el("p", {
      class: "update-banner-notes",
      text: "Verified against Hearth's release key. Installing closes Hearth, "
        + "updates it and opens it again; your models, chats and settings are kept.",
    }));
  }

  const actions = el("div", { class: "update-banner-actions" });
  const add = (text, kind, fn) => {
    if (!fn) return null;
    const button = el("button", { class: `btn ${kind} btn-sm`, type: "button", text });
    button.addEventListener("click", () => fn(button));
    actions.appendChild(button);
    return button;
  };
  if (state === "available" || state === "ready") {
    add("Install now", "btn-primary", handlers.onInstall);
  }
  add(state === "downloading" ? "Hide" : "Later", "btn-ghost", handlers.onLater);
  root.appendChild(actions);
  return root;
}

/** Draw (or hide) the banner for this snapshot. */
export function renderUpdateBanner(snapshot, handlers = {}) {
  const host = $("#update-banner");
  if (!host) return null;
  const body = hiddenForLaunch ? null : updateBannerCard(snapshot, {
    ...handlers,
    onLater: (button) => {
      hiddenForLaunch = true;
      clear(host);
      host.hidden = true;
      if (handlers.onLater) handlers.onLater(button);
    },
  });
  clear(host);
  if (!body) {
    host.hidden = true;
    return null;
  }
  host.appendChild(body);
  host.hidden = false;
  return body;
}
