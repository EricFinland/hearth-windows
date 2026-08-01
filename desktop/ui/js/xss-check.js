/* Untrusted-content regression check for desktop/ui.
 *
 * Model output, tool output, file bodies, and tool-call arguments are all
 * attacker-influenceable: a local model can be induced to emit markup, and a
 * file the agent reads can simply contain it. This drives real payloads
 * through every rendering path the UI has for that content and then inspects
 * the resulting DOM, rather than asserting the property by reading the source.
 *
 * A surface passes only when all three hold:
 *   1. no element the payload named exists in the rendered subtree,
 *   2. no attribute whose name starts with "on" exists there,
 *   3. the payload's exact characters ARE present as text (so a pass cannot be
 *      earned by silently dropping the content instead of rendering it), and
 *      window.__xssFired is still false.
 *
 * Open http://127.0.0.1:4173/xss-check.html with the dev host running.
 */

import { renderProse, blob } from "./safe-text.js";
import { Transcript } from "./transcript.js";
import { el } from "./dom.js";

// Any payload that manages to execute sets this. It must stay false.
window.__xssFired = false;

const PAYLOADS = [
  `<img src=x onerror=alert(1)>`,
  `<img src=x onerror="window.__xssFired=true">`,
  `<script>window.__xssFired=true</` + `script>`,
  `<svg onload="window.__xssFired=true"></svg>`,
  `<iframe src="javascript:window.__xssFired=true"></iframe>`,
  `"><img src=x onerror=alert(1)><span a="`,
  `</p></div><img src=x onerror=alert(1)><div><p>`,
  `<a href="javascript:window.__xssFired=true">click</a>`,
  `<style>body{display:none}</style>`,
  `<object data="javascript:alert(1)"></object>`,
];

const BANNED_TAGS = new Set(["IMG", "SCRIPT", "IFRAME", "OBJECT", "EMBED", "STYLE", "LINK", "A", "FORM"]);

/** An element counts as injected if the payload named it, or if it carries any
 *  event-handler attribute. The icons this UI builds itself are <svg>/<use>
 *  carrying only class, aria-hidden, and href, so they never trip this. */
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

const rows = document.getElementById("rows");
const probes = document.getElementById("probes");
const stage = document.getElementById("transcript");
const transcript = new Transcript(stage);

let failures = 0;

function check(surface, payload, container) {
  const injected = findInjected(container);
  const literal = container.textContent.includes(payload);
  const ok = injected.length === 0 && literal && window.__xssFired === false;
  if (!ok) failures++;
  const detail = injected.length ? `created ${injected.join(", ")}`
    : !literal ? "payload text missing from the DOM (altered or dropped)"
    : window.__xssFired ? "a payload executed"
    : "inert text";
  rows.appendChild(el("tr", {}, [
    el("td", { text: surface }),
    el("td", { class: "p", text: payload }),
    el("td", { class: "r " + (ok ? "ok" : "bad"), text: (ok ? "PASS" : "FAIL") + " - " + detail }),
  ]));
}

/** Render one payload into its own attached-but-hidden host, so the inspection
 *  sees exactly what that one call produced. Attached rather than detached
 *  because an <img> only actually fetches (and fires onerror) once it is in the
 *  document, and this check should fail loudly if that ever becomes possible. */
function probe(build) {
  const host = el("div", { class: "probe" });
  probes.appendChild(host);
  build(host);
  return host;
}

for (const payload of PAYLOADS) {
  check("renderProse", payload, probe((host) => host.appendChild(renderProse(payload))));
  check("blob", payload, probe((host) => host.appendChild(blob(payload))));
  check("fenced code block", payload,
    probe((host) => host.appendChild(renderProse("```html\n" + payload + "\n```"))));
  check("inline code span", payload,
    probe((host) => host.appendChild(renderProse("look: `" + payload + "` done"))));
}

// The live transcript surfaces, rendered into the visible stage so the result
// can be read by eye as well as asserted.
transcript.addUser(PAYLOADS[0]);
transcript.appendAgent(
  "Here is what I found:\n\n```html\n" + PAYLOADS[1] + "\n```\n\nAnd inline " + PAYLOADS[5]);
transcript.closeAgent();
transcript.addToolCall({
  tool: "write_file",
  args: { path: "evil" + PAYLOADS[6], content: PAYLOADS[2] },
  decision: "allow",
});
transcript.addToolResult({ tool: "write_file", output: PAYLOADS[3] });
transcript.addApproval({
  id: "appr-test",
  tool: "write_file",
  args: { path: "notes.md", content: PAYLOADS[4] },
  injection_finding: {
    severity: "high", category: "instruction_override",
    matched: PAYLOADS[7], explanation: PAYLOADS[8],
  },
  secrets_finding: {
    severity: "high", kind: "test", masked: "ab****yz",
    context: PAYLOADS[9], reason: PAYLOADS[0],
  },
}, () => {});
transcript.addNotice("warn", PAYLOADS[0], PAYLOADS[2], PAYLOADS[4]);

await new Promise((done) => setTimeout(done, 500)); // let the paced reveal finish

for (const payload of [PAYLOADS[0], PAYLOADS[1], PAYLOADS[2], PAYLOADS[3],
                       PAYLOADS[4], PAYLOADS[6], PAYLOADS[7], PAYLOADS[9]]) {
  check("live transcript (message, tool card, approval card, notice)", payload, stage);
}

const verdict = document.getElementById("verdict");
verdict.className = "verdict " + (failures === 0 ? "pass" : "fail");
verdict.textContent = failures === 0
  ? `PASS - ${rows.childElementCount} checks, every payload inert, window.__xssFired === false`
  : `FAIL - ${failures} of ${rows.childElementCount} checks let markup through`;

// Read by the harness that drives this page.
window.__xssCheck = { failures, total: rows.childElementCount, fired: window.__xssFired };
