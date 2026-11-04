/* Untrusted-content check for attached files (js/attach.js).
 *
 * A filename is whatever the file was called on somebody's disk, a download
 * folder full of internet files included. It reaches the page in four places:
 * the chip in the tray, the warning and note lines under it, the chip's
 * tooltip, and the sent-file chips the transcript puts under the user's
 * message. Each is driven here with hostile names through the REAL exported
 * renderers (renderAttachmentChip, renderAttachmentNotes and
 * Transcript.addUserAttachments), never through a lookalike.
 *
 * Two properties per surface, the same two xss-check.js checks:
 *   1. INERT: no element the payload named exists, no on* attribute exists,
 *      the payload's characters are present as text, nothing executed.
 *   2. HONEST: "invoice<U+202E>fdp.exe" must not display as "invoiceexe.pdf".
 *      The override shows as a visible <U+202E> marker, in the text and in
 *      the tooltip, and the raw character is nowhere in the page.
 *
 * Its own section, its own verdict and window.__xssCheckAttach, so this file
 * can be added and removed without touching the main harness.
 */

import { renderAttachmentChip, renderAttachmentNotes } from "./attach.js";
import { Transcript } from "./transcript.js";
import { el } from "./dom.js";

window.__xssFired = window.__xssFired || false;

const PAYLOADS = [
  `<img src=x onerror="window.__xssFired=true">.png`,
  `<script>window.__xssFired=true</` + `script>.txt`,
  `<svg onload="window.__xssFired=true"></svg>.svg`,
  `"><img src=x onerror=alert(1)><span a=".pdf`,
  `it's "quoted" & <b>bold</b>.md`,
  `</span></div><iframe src="javascript:window.__xssFired=true"></iframe>.html`,
  `<a href="javascript:window.__xssFired=true">click</a>.csv`,
  "n".repeat(300) + ".log",
];

const BANNED_TAGS = new Set(["IMG", "SCRIPT", "IFRAME", "OBJECT", "EMBED", "STYLE", "LINK", "A", "FORM", "B"]);

const wrap = document.querySelector(".wrap") || document.body;
const section = el("section", { class: "attach-xss" });
const verdict = el("div", { class: "verdict", text: "running..." });
const rows = el("tbody");
const stage = el("div", { class: "probe" });
section.appendChild(el("h2", { text: "Attached files (names, notes, sent-file chips)" }));
section.appendChild(verdict);
section.appendChild(el("table", {}, [
  el("thead", {}, [el("tr", {}, [el("th", { text: "Surface" }), el("th", { text: "Payload" }), el("th", { text: "Result" })])]),
  rows,
]));
section.appendChild(stage);
wrap.appendChild(section);

let failures = 0;

function report(surface, payload, ok, detail) {
  if (!ok) failures++;
  rows.appendChild(el("tr", {}, [
    el("td", { text: surface }),
    el("td", { class: "p", text: payload.length > 90 ? payload.slice(0, 90) + "..." : payload }),
    el("td", { class: "r " + (ok ? "ok" : "bad"), text: (ok ? "PASS" : "FAIL") + " - " + detail }),
  ]));
}

function injected(root) {
  const bad = [];
  for (const node of root.querySelectorAll("*")) {
    if (BANNED_TAGS.has(node.tagName)) { bad.push(node.tagName.toLowerCase()); continue; }
    for (const attr of node.attributes) {
      if (attr.name.toLowerCase().startsWith("on")) bad.push(`${node.tagName.toLowerCase()}[${attr.name}]`);
    }
  }
  return bad;
}

function probe(build) {
  const host = el("div");
  stage.appendChild(host);
  build(host);
  return host;
}

function checkInert(surface, payload, host) {
  const bad = injected(host);
  const literal = host.textContent.includes(payload);
  const ok = bad.length === 0 && literal && window.__xssFired === false;
  report(surface, payload, ok, bad.length ? `created ${bad.join(", ")}`
    : !literal ? "payload text missing from the DOM (altered or dropped)"
      : window.__xssFired ? "a payload executed" : "inert text");
}

/** An item in every state the tray draws, each carrying the payload in every
 *  text field it has: name, stored name, note, warning summaries, reason. */
function items(p) {
  const record = {
    name: p, path: "imports/" + p, size: 2048, kind: "text", status: "ok", readable: true,
    text_chars: 100, budget_chars: 5000, ignored: true, note: p,
    warnings: [{ type: "secret", summary: p }, { type: "injection", summary: p }],
  };
  return [
    { name: p, size: 10, status: "queued" },
    { name: p, size: 10, status: "importing", progress: 0.4 },
    { name: p, size: 10, status: "ready", record },
    { name: p, size: 10, status: "ready", record: { ...record, readable: false, status: "image", note: p } },
    { name: p, size: 10, status: "skipped", reason: p },
    { name: p, size: 10, status: "failed", reason: p },
  ];
}

for (const p of PAYLOADS) {
  for (const item of items(p)) {
    const label = `tray chip (${item.status}${item.record && !item.record.readable ? ", unread" : ""})`;
    checkInert(label, p, probe((h) => h.appendChild(
      renderAttachmentChip(item, { plan: "excerpt", onRemove: () => {} }))));
  }
  checkInert("warning and note lines", p, probe((h) => h.appendChild(renderAttachmentNotes(items(p)))));
  checkInert("sent-file chips in the transcript", p, probe((h) => {
    const t = new Transcript(h);
    t.addUser("see attached");
    t.addUserAttachments([{ name: p, path: "imports/" + p, plan: "full" }, { name: p, plan: "excerpt" }]);
  }));
}

/* A 300-character name is cut by CSS, never in the DOM: the whole name is
 * still the chip's text and its tooltip. */
{
  const long = "n".repeat(300) + ".log";
  const chip = renderAttachmentChip({ name: long, size: 1, status: "queued" }, {});
  const ok = chip.querySelector(".att-name").textContent === long && chip.getAttribute("title").startsWith(long);
  report("300-character name", long, ok, ok ? "kept whole, truncated only visually" : "the name was altered");
}

/* Display honesty: invisible and reordering characters in a name. */
const BIDI = [
  { name: "RLO reversing an extension", text: "invoice‮fdp.exe", marker: "<U+202E>", raw: "‮" },
  { name: "RLI isolate", text: "report⁧.pdf⁩.exe", marker: "<U+2067>", raw: "⁧" },
  { name: "zero-width space", text: "pay​roll.xlsx", marker: "<U+200B>", raw: "​" },
  { name: "BOM inside a name", text: "notes﻿.txt", marker: "<U+FEFF>", raw: "﻿" },
  { name: "LRM mark", text: "a‎b.txt", marker: "<U+200E>", raw: "‎" },
];

function rawAnywhere(host, raw) {
  if (host.textContent.includes(raw)) return true;
  for (const node of host.querySelectorAll("*")) {
    for (const attr of node.attributes) if (attr.value.includes(raw)) return true;
  }
  return false;
}

for (const b of BIDI) {
  const record = {
    name: b.text, path: "imports/" + b.text, size: 1, readable: true, text_chars: 1,
    budget_chars: 5000, note: b.text, warnings: [{ type: "secret", summary: b.text }],
  };
  const surfaces = [
    ["tray chip", (h) => h.appendChild(renderAttachmentChip({ name: b.text, size: 1, status: "ready", record }, { onRemove: () => {} }))],
    ["warning lines", (h) => h.appendChild(renderAttachmentNotes([{ name: b.text, record }]))],
    ["sent-file chips", (h) => {
      const t = new Transcript(h);
      t.addUser("x");
      t.addUserAttachments([{ name: b.text, path: "imports/" + b.text, plan: "full" }]);
    }],
  ];
  for (const [label, build] of surfaces) {
    const host = probe(build);
    const shown = host.textContent.includes(b.marker);
    const raw = rawAnywhere(host, b.raw);
    const titled = Array.from(host.querySelectorAll("[title]")).every((n) => !n.getAttribute("title").includes(b.raw));
    const ok = shown && !raw && titled;
    report(label + " (display order)", b.name, ok, ok ? "shown as " + b.marker + ", tooltip too"
      : raw ? "the raw control character reached the page" : "no visible marker");
  }
}

const total = rows.childElementCount;
verdict.className = "verdict " + (failures === 0 ? "pass" : "fail");
verdict.textContent = failures === 0
  ? `PASS - ${total} checks on attached-file names, every payload inert, every control character visible`
  : `FAIL - ${failures} of ${total} attached-file checks let markup through or hid a control character`;

// Read by the harness that drives this page.
window.__xssCheckAttach = { failures, total, fired: window.__xssFired };
