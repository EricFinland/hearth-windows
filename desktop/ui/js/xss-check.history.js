/* Untrusted-content check for the Chats sidebar (js/history.js).
 *
 * A conversation's title is derived from the first thing typed into the
 * composer, a rename is free text, and the index both come from is a file in
 * a directory the agent's own run_command can write. So a title can be
 * anything at all, and the sidebar is one more place the page draws text it
 * did not choose. This drives hostile titles, workspaces and models through
 * the exact functions the live sidebar renders with, and through a real
 * HistoryPanel fed by a stub sidecar, then inspects the DOM the same way
 * xss-check.js does: nothing the payload named may exist, no on* attribute
 * may exist, the payload's characters must be present as text, and nothing
 * may have run.
 *
 * Bidi and control characters get their own rows: the sidecar strips them
 * from titles, but the index is a file and the UI must not depend on the
 * sidecar for this. A title carrying U+202E must render with a visible
 * `<U+202E>` marker, never the raw override.
 *
 * Standalone on purpose (its own section, its own verdict, its own
 * window.__xssCheckHistory), so it adds coverage without editing xss-check.js.
 */

import { renderConversationRow, renderHistoryList, HistoryPanel } from "./history.js";
import { el } from "./dom.js";

window.__xssFired = window.__xssFired || false;

const PAYLOADS = [
  `<img src=x onerror=alert(1)>`,
  `<img src=x onerror="window.__xssFired=true">`,
  `<script>window.__xssFired=true</` + `script>`,
  `<svg onload="window.__xssFired=true"></svg>`,
  `<iframe src="javascript:window.__xssFired=true"></iframe>`,
  `"><img src=x onerror=alert(1)><span a="`,
  `<a href="javascript:window.__xssFired=true">click</a>`,
  `<style>body{display:none}</style>`,
];

const BIDI = [
  { name: "RLO in a title", text: "notes \u202Eexe.txt", raw: "\u202E", marker: "<U+202E>" },
  { name: "isolate in a title", text: "a\u2066b\u2069c", raw: "\u2066", marker: "<U+2066>" },
  { name: "zero-width in a title", text: "sec\u200Bret", raw: "\u200B", marker: "<U+200B>" },
  { name: "ESC in a title", text: "\u001B[31mred", raw: "\u001B", marker: "<U+001B>" },
];

const BANNED_TAGS = new Set(["IMG", "SCRIPT", "IFRAME", "OBJECT", "EMBED", "STYLE", "LINK", "A", "FORM"]);

function findInjected(root) {
  const bad = [];
  for (const node of root.querySelectorAll("*")) {
    if (BANNED_TAGS.has(node.tagName)) { bad.push(node.tagName.toLowerCase()); continue; }
    for (const attr of node.attributes) {
      if (attr.name.toLowerCase().startsWith("on")) bad.push(`${node.tagName.toLowerCase()}[${attr.name}]`);
    }
  }
  return bad;
}

/* Text a person could actually see: element text plus the attributes that
 * are displayed text by another name. */
function visibleText(root) {
  let out = root.textContent;
  for (const node of root.querySelectorAll("[title], [aria-label]")) {
    out += " " + (node.getAttribute("title") || "") + " " + (node.getAttribute("aria-label") || "");
  }
  return out;
}

const section = el("section", { class: "hist-xss" });
const verdict = el("div", { class: "verdict", text: "running..." });
const rows = el("tbody");
const stage = el("div", { class: "hist-xss-stage" });
section.append(
  el("h2", { text: "Chats sidebar (xss-check.history.js)" }),
  verdict,
  el("table", {}, [
    el("thead", {}, [el("tr", {}, [
      el("th", { text: "Surface" }), el("th", { text: "Payload" }), el("th", { text: "Result" }),
    ])]),
    rows,
  ]),
  stage,
);
(document.querySelector(".wrap") || document.body).appendChild(section);

let failures = 0;

function row(surface, payload, ok, detail) {
  if (!ok) failures++;
  rows.appendChild(el("tr", {}, [
    el("td", { text: surface }),
    el("td", { class: "p", text: payload }),
    el("td", { class: "r " + (ok ? "ok" : "bad"), text: (ok ? "PASS" : "FAIL") + " - " + detail }),
  ]));
}

function check(surface, payload, container) {
  const injected = findInjected(container);
  const literal = visibleText(container).includes(payload);
  const ok = injected.length === 0 && literal && window.__xssFired === false;
  row(surface, payload, ok, injected.length ? `created ${injected.join(", ")}`
    : !literal ? "payload text missing (altered or dropped)"
    : window.__xssFired ? "a payload executed" : "inert text");
}

function probe(build) {
  const host = el("div", { class: "hist-xss-probe" });
  stage.appendChild(host);
  build(host);
  return host;
}

function hostile(payload, id) {
  return {
    id, title: payload, title_source: "user", workspace: "C:\\work\\" + payload,
    model: payload, mode: "edit", engine: payload, created_at: Date.now() / 1000,
    updated_at: Date.now() / 1000,
  };
}

const ID = "0123456789abcdef0123456789abcdef";

for (const payload of PAYLOADS) {
  check("renderConversationRow", payload,
    probe((host) => host.appendChild(renderConversationRow(hostile(payload, ID), { active: true }))));
  check("renderHistoryList", payload,
    probe((host) => host.appendChild(renderHistoryList(
      [hostile(payload, ID), hostile(payload, ID.replace("0", "1"))], { activeId: ID }))));
  check("renderHistoryList (search)", payload,
    probe((host) => host.appendChild(renderHistoryList([hostile(payload, ID)], { query: payload }))));
}

for (const sample of BIDI) {
  const host = probe((h) => h.appendChild(renderConversationRow(hostile(sample.text, ID), { active: true })));
  const seen = visibleText(host);
  const ok = !seen.includes(sample.raw) && seen.includes(sample.marker);
  row("bidi: " + sample.name, JSON.stringify(sample.text), ok,
    ok ? "shown as a visible marker" : "the raw control character reached the page");
}

// A real panel, driven by a stub sidecar that serves hostile titles, so the
// panel's own paths (its list, its notes, the delete confirmation it builds)
// are covered too and not only the exported helpers.
async function panelChecks() {
  for (const payload of PAYLOADS.slice(0, 3)) {
    const items = [hostile(payload, ID)];
    const sidecar = {
      authenticated: true,
      async request(method, path) {
        if (method === "GET" && path === "/conversations") {
          return { enabled: true, active_id: ID, items };
        }
        throw new Error(payload);  // an error string is shown in the note
      },
    };
    let modal = null;
    const root = probe((host) => host.appendChild(el("aside", { class: "history" })))
      .querySelector("aside");
    const panel = new HistoryPanel(root, {
      sidecar,
      openModal: (title, nodes) => { modal = el("div", {}, [el("h3", { text: title }), ...nodes]); },
      isRunning: () => false,
      hasSession: () => true,
      onSwitched: () => {},
    });
    await panel.refresh();
    check("HistoryPanel list", payload, root);
    panel.confirmDelete(items[0]);
    const modalHost = probe((host) => { if (modal) host.appendChild(modal); });
    check("HistoryPanel delete confirmation", payload, modalHost);
    await panel.newChat();  // the stub throws, so the panel shows the error text
    check("HistoryPanel error note", payload, root);
  }
}

panelChecks().catch((err) => row("HistoryPanel", "-", false, "threw: " + err)).finally(() => {
  const total = rows.childElementCount;
  verdict.className = "verdict " + (failures === 0 ? "pass" : "fail");
  verdict.textContent = failures === 0
    ? `PASS - ${total} Chats sidebar checks, every hostile title inert and every control character visible`
    : `FAIL - ${failures} of ${total} Chats sidebar checks`;
  // Read by the harness that drives this page, next to window.__xssCheck.
  window.__xssCheckHistory = { failures, total, fired: window.__xssFired };
});
