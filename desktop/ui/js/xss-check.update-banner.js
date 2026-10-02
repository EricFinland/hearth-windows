/* Untrusted-content check for the update banner (js/update-banner.js).
 *
 * The banner shows a version and release notes out of a signed manifest.
 * Signed is a statement about who wrote them, not about whether they are safe
 * to render, so hostile values are planted in every field the banner reads
 * and driven through the REAL exported updateBannerCard, in every state that
 * shows the banner, and in the states that must not show it at all.
 *
 * Two properties, the same two xss-check.js checks:
 *   1. INERT: no element the payload named exists, no on* attribute exists,
 *      and nothing executed.
 *   2. HONEST: a bidi override inside the version shows as a visible
 *      <U+XXXX> marker and is not present raw. This is the line that says
 *      which version the user is about to install.
 *
 * Its own section, its own verdict and window.__xssCheckUpdateBanner, so it
 * can be added and removed without touching the main harness.
 */

import { updateBannerCard } from "./update-banner.js";
import { el } from "./dom.js";

window.__xssFired = window.__xssFired || false;

const PAYLOADS = [
  `<img src=x onerror="window.__xssFired=true">`,
  `<script>window.__xssFired=true</` + `script>`,
  `<svg onload="window.__xssFired=true"></svg>`,
  `"><img src=x onerror=alert(1)><span a="`,
  `<a href="javascript:window.__xssFired=true">click</a>`,
  `<style>body{display:none}</style>`,
];

const BANNED_TAGS = new Set(["IMG", "SCRIPT", "IFRAME", "OBJECT", "EMBED", "STYLE", "LINK", "A", "FORM", "SVG"]);

function mount() {
  const wrap = document.querySelector(".wrap") || document.body;
  const section = el("section", { class: "update-banner-xss" });
  const verdict = el("div", { class: "verdict", text: "running..." });
  const rows = el("tbody");
  const stage = el("div", { class: "probe" });
  section.appendChild(el("h2", { text: "Update banner" }));
  section.appendChild(verdict);
  section.appendChild(el("table", {}, [
    el("thead", {}, [el("tr", {}, [el("th", { text: "Surface" }), el("th", { text: "Payload" }), el("th", { text: "Result" })])]),
    rows,
  ]));
  section.appendChild(stage);
  wrap.appendChild(section);
  return { verdict, rows, stage };
}

function run() {
  const { verdict, rows, stage } = mount();
  let failures = 0;

  const report = (surface, payload, ok, detail) => {
    if (!ok) failures++;
    rows.appendChild(el("tr", {}, [
      el("td", { text: surface }),
      el("td", { class: "p", text: payload }),
      el("td", { class: "r " + (ok ? "ok" : "bad"), text: (ok ? "PASS" : "FAIL") + " - " + detail }),
    ]));
  };

  const injected = (root) => {
    const bad = [];
    for (const node of root.querySelectorAll("*")) {
      if (BANNED_TAGS.has(node.tagName.toUpperCase())) { bad.push(node.tagName.toLowerCase()); continue; }
      for (const attr of node.attributes) {
        if (attr.name.toLowerCase().startsWith("on")) bad.push(`${node.tagName.toLowerCase()}[${attr.name}]`);
      }
    }
    return bad;
  };

  const hostile = (p, state) => ({
    state,
    current_version: p,
    available: { version: p, notes: p, released_at: p, size_bytes: 1, name: p },
    staged: state === "ready" ? { version: p, sha256: p, signed_by: p, path: p } : null,
    bytes_done: 5, bytes_total: 10,
    error: p, message: p,
  });

  for (const p of PAYLOADS) {
    for (const state of ["available", "downloading", "ready"]) {
      const host = el("div");
      stage.appendChild(host);
      const body = updateBannerCard(hostile(p, state), { onInstall() {}, onLater() {} });
      if (body) host.appendChild(body);
      const bad = injected(host);
      // The notes are cut to an excerpt and whitespace-folded, so check the
      // version line, which carries the payload whole.
      const literal = host.textContent.includes(p);
      const ok = Boolean(body) && bad.length === 0 && literal && window.__xssFired === false;
      report(`banner (${state})`, p, ok, !body ? "rendered nothing"
        : bad.length ? `created ${bad.join(", ")}`
          : !literal ? "payload text missing from the DOM"
            : window.__xssFired ? "a payload executed" : "inert text");
    }
    // A failure, an unconfigured build and an up-to-date check never show a
    // banner, however hostile the message they carry.
    for (const state of ["failed", "unconfigured", "up-to-date", "checking", "idle", p]) {
      const body = updateBannerCard(hostile(p, state), {});
      report(`no banner (${String(state).slice(0, 20)})`, p, body === null,
        body === null ? "nothing shown" : "a banner was drawn for a non-actionable state");
    }
  }

  // HONEST: an override in the version must not reorder what is on screen.
  for (const ch of ["‮", "⁦", "​"]) {
    const version = `0.2.0${ch}9.9.9`;
    const body = updateBannerCard({ state: "available", available: { version, notes: "" } }, {});
    const title = body && body.querySelector(".update-banner-title");
    const text = title ? title.textContent : "";
    const marker = "<U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0") + ">";
    const ok = Boolean(title) && !text.includes(ch) && text.includes(marker);
    report("banner version (bidi)", marker, ok, ok ? "shown as a visible marker" : `raw control present: ${JSON.stringify(text)}`);
  }

  verdict.textContent = failures === 0 ? "PASS: update banner" : `FAIL: ${failures} update banner check(s)`;
  verdict.className = "verdict " + (failures === 0 ? "ok" : "bad");
  window.__xssCheckUpdateBanner = { failures, done: true };
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", run);
} else {
  run();
}
