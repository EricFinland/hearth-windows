/* The model shop.
 *
 * The promise this screen exists to keep: a user downloads whichever model
 * they want with a button, no code, and is told before they click whether it
 * will actually run on the machine they are sitting at.
 *
 * Three rules shape everything below.
 *
 * 1. UNTRUSTED TEXT. Repository ids, labels, authors, tags, filenames, and
 *    every error string in a listing come from Hugging Face, which means they
 *    come from whoever uploaded the repository. Nothing here builds an HTML
 *    string and nothing here calls innerHTML: every one of those values
 *    reaches the page through el({text}) or textContent, which is why a
 *    repository literally named `<img src=x onerror=alert(1)>` renders as
 *    twenty-eight visible characters. The render functions are exported so
 *    xss-check.html can drive hostile payloads through the exact code paths
 *    the live screen uses, rather than through a lookalike.
 *
 * 2. THE VERDICT IS THE PRODUCT. hearth_shop grades every quantisation
 *    against this machine's real VRAM, RAM and free disk, and writes a
 *    sentence explaining the grade. That sentence is rendered verbatim, next
 *    to the colour, because "reduced context" is not actionable and "does not
 *    fit the GPU at 8192 tokens, but fits up to about 4424 tokens" is. The
 *    hardware the grades were computed against is shown at the top of the
 *    screen so the numbers are checkable rather than magic.
 *
 * 3. REAL PROGRESS ONLY. Every byte count, percentage, rate and ETA comes
 *    from hearth_hf's own progress callbacks. When the total size is unknown
 *    the bar goes indeterminate instead of inventing a denominator, and a
 *    download that has not moved shows the bytes it actually has.
 */

import { el, icon, appendAll, clear } from "./dom.js";

// ------------------------------------------------------------------ formatting

const KIB = 1024;

/** Bytes as a short human string. Returns "" for anything that is not a
 *  finite number, so an absent size renders as absent rather than as "0 B",
 *  which would read as a claim. */
export function formatBytes(n) {
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < KIB) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / KIB;
  let unit = 0;
  while (value >= KIB && unit < units.length - 1) { value /= KIB; unit++; }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(value >= 10 ? 1 : 2)} ${units[unit]}`;
}

export function formatRate(bytesPerSecond) {
  if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) return "";
  return `${formatBytes(bytesPerSecond)}/s`;
}

export function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function formatCount(n) {
  if (!Number.isFinite(n)) return "";
  if (n < 1000) return String(n);
  if (n < 1e6) return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
  return `${(n / 1e6).toFixed(n < 1e7 ? 1 : 0)}M`;
}

// -------------------------------------------------------------------- verdicts

/* The five grades hearth_shop can return, in its own vocabulary. The label is
 * deliberately plain language: a user should not have to learn what
 * "cpu_spillover" means to read this screen, and the module's own `message`
 * carries the detail underneath it either way. */
const VERDICTS = {
  great:           { label: "Runs great here",     cls: "is-great" },
  good:            { label: "Runs well here",      cls: "is-good" },
  reduced_context: { label: "Shorter context",     cls: "is-reduced" },
  cpu_spillover:   { label: "Slow, runs on CPU",   cls: "is-spill" },
  wont_fit:        { label: "Will not run here",   cls: "is-wont" },
};

export function verdictMeta(kind) {
  return VERDICTS[kind] || { label: String(kind ?? "not graded"), cls: "is-unknown" };
}

/** The pill alone. `kind` is a hearth_shop VERDICT_* string; anything else
 *  falls through to a neutral pill carrying that string as text, which is the
 *  honest rendering of a grade this page does not recognise. */
export function renderVerdictPill(kind) {
  const meta = verdictMeta(kind);
  return el("span", { class: "verdict-pill " + meta.cls, text: meta.label });
}

/** The pill plus hearth_shop's own explanation of it. The message is the
 *  whole point: two machines see different sentences for the same model, and
 *  a colour alone cannot say "fits up to about 4424 tokens". */
export function renderVerdict(verdict) {
  if (!verdict) {
    return el("div", { class: "verdict" }, [
      el("span", { class: "verdict-pill is-unknown", text: "Not graded" }),
      el("p", { class: "verdict-msg", text: "Hugging Face published no size for this file, so nothing honest can be said about whether it fits." }),
    ]);
  }
  const nodes = [renderVerdictPill(verdict.verdict)];
  if (verdict.message) nodes.push(el("p", { class: "verdict-msg", text: String(verdict.message) }));

  const facts = [];
  if (Number.isFinite(verdict.required_bytes)) {
    facts.push(`needs ${formatBytes(verdict.required_bytes)} with a ${verdict.requested_context_tokens ?? "?"}-token context`);
  }
  if (Number.isFinite(verdict.headroom_bytes)) {
    facts.push(verdict.headroom_bytes >= 0
      ? `${formatBytes(verdict.headroom_bytes)} to spare`
      : `${formatBytes(Math.abs(verdict.headroom_bytes))} short`);
  }
  if (verdict.vram_approximate) facts.push("the VRAM reading behind this is approximate");
  if (facts.length) nodes.push(el("p", { class: "verdict-facts", text: facts.join(" · ") }));
  return el("div", { class: "verdict" }, nodes);
}

// -------------------------------------------------------------------- hardware

/** The machine every verdict on the screen was judged against. Rendered once,
 *  at the top, so the grades are checkable instead of assertions. */
export function renderHardware(hardware) {
  const hw = hardware || {};
  const items = [];
  const push = (label, value, note) => {
    if (!value) return;
    items.push(appendAll(el("div", { class: "hw-item" }), [
      el("span", { class: "hw-k", text: label }),
      el("span", { class: "hw-v", text: value }),
      note ? el("span", { class: "hw-note", text: note }) : null,
    ]));
  };
  push("GPU", hw.gpu_detected ? (hw.gpu_vendor ? String(hw.gpu_vendor) : "detected") : "none detected");
  push("VRAM", formatBytes(hw.vram_bytes), hw.vram_approximate ? "approximate" : null);
  push("RAM budget", formatBytes(hw.ram_bytes));
  push("Free disk", formatBytes(hw.free_disk_bytes));
  push("Context", hw.context_tokens ? `${hw.context_tokens} tokens` : "");
  if (!items.length) return el("div", { class: "hw-bar" });
  return appendAll(el("div", { class: "hw-bar" }), [
    el("span", { class: "hw-lead", text: "Graded against this machine:" }),
    ...items,
  ]);
}

// ------------------------------------------------------------------- honesty

/** The two limits that must not be discovered by clicking. Both are permanent
 *  facts about this build, not error states, so they sit on the screen rather
 *  than waiting to become an error message. */
export function renderLimitations() {
  return appendAll(el("div", { class: "shop-limits" }), [
    appendAll(el("div", { class: "limit" }), [
      icon("i-file"),
      el("div", {}, [
        el("strong", { text: "Hearth runs GGUF models." }),
        el("span", { text: " This searches Hugging Face for repositories that publish GGUF files. A model released only as safetensors will not appear here and cannot be run by Hearth, however popular it is." }),
      ]),
    ]),
    appendAll(el("div", { class: "limit" }), [
      icon("i-key"),
      el("div", {}, [
        el("strong", { text: "Gated models cannot be downloaded yet." }),
        el("span", { text: " Llama, Gemma and similar repositories require a licence to be accepted on huggingface.co before their files can be fetched. Hearth has no Hugging Face sign-in yet, so those results are shown and labelled but their download button is withheld rather than left to fail with a 401." }),
      ]),
    ]),
  ]);
}

/** The banner for a listing that is NOT live Hub results. hearth_shop sets
 *  ok False and source "fallback" together, and writes its own notice; both
 *  are rendered, and the results below keep a "reference model" marking of
 *  their own, so a fallback can never be read as a search result. */
export function renderSourceNotice(listing) {
  if (!listing || listing.source !== "fallback") return null;
  const nodes = [
    el("strong", { text: "These are not search results." }),
    el("p", { text: String(listing.notice || "The Hugging Face Hub could not be reached.") }),
  ];
  if (listing.error) nodes.push(el("p", { class: "shop-banner-error", text: String(listing.error) }));
  return appendAll(el("div", { class: "shop-banner is-fallback" }), [icon("i-alert"), el("div", {}, nodes)]);
}

// ---------------------------------------------------------------- quant rows

function quantLabel(quant) {
  return String(quant.quant || quant.name || "unnamed");
}

/** What the download button should say and whether it should be live.
 *  Exported because "cancel did not throw your 3 GB away" is a claim this UI
 *  makes in a button label, and it should be testable as a pure function. */
export function downloadAction(entry, quant) {
  const local = quant.local || {};
  const size = Number.isFinite(quant.size_bytes) ? formatBytes(quant.size_bytes) : "";
  if (local.present) {
    return { label: "Installed", disabled: true, kind: "installed" };
  }
  if (!entry.downloadable || entry.gated) {
    return { label: "Licence required", disabled: true, kind: "gated" };
  }
  if (!quant.path && !(quant.parts || []).length) {
    return { label: "Unavailable", disabled: true, kind: "unavailable" };
  }
  if (quant.complete === false) {
    return { label: "Incomplete upload", disabled: true, kind: "incomplete" };
  }
  if (local.partial_bytes) {
    return {
      label: `Resume from ${formatBytes(local.partial_bytes)}`,
      disabled: false, kind: "resume",
    };
  }
  if (quant.disk_ok === false) {
    return { label: size ? `No room (${size})` : "No room on disk", disabled: true, kind: "no_space" };
  }
  return { label: size ? `Download ${size}` : "Download", disabled: false, kind: "download" };
}

/** The filename POST /downloads wants: a single file's repo path, or a split
 *  model's logical name (its parts have no single path). */
export function quantTarget(quant) {
  return quant.path || quant.name || "";
}

/** One quantisation, graded. Used for both the recommended row and the rows
 *  inside the "all quantisations" fold; `featured` only changes the styling. */
export function renderQuantRow(entry, quant, handlers = {}, { featured = false } = {}) {
  const action = downloadAction(entry, quant);
  const tags = [];
  if (quant.recommended) tags.push("recommended");
  if (quant.split_part_count) tags.push(`${quant.split_part_count} files`);
  if (quant.projector) tags.push("vision projector");
  if (quant.local?.partial_bytes) tags.push(`${formatBytes(quant.local.partial_bytes)} already downloaded`);
  if (quant.local?.present) tags.push("on disk");

  const button = el("button", {
    class: "btn " + (featured && !action.disabled ? "btn-primary" : ""),
    type: "button",
    text: action.label,
    disabled: action.disabled,
    dataset: { action: action.kind },
  });
  if (!action.disabled && handlers.onDownload) {
    button.addEventListener("click", () => handlers.onDownload(entry, quant));
  }

  return appendAll(el("div", { class: "quant" + (featured ? " is-featured" : "") }), [
    appendAll(el("div", { class: "quant-main" }), [
      appendAll(el("div", { class: "quant-line" }), [
        el("span", { class: "quant-name", text: quantLabel(quant) }),
        Number.isFinite(quant.size_bytes)
          ? el("span", { class: "quant-size", text: formatBytes(quant.size_bytes) })
          : el("span", { class: "quant-size", text: "size unknown" }),
        renderVerdictPill(quant.verdict?.verdict),
      ]),
      quant.verdict?.message
        ? el("p", { class: "quant-msg", text: String(quant.verdict.message) })
        : null,
      tags.length ? el("p", { class: "quant-tags", text: tags.join(" · ") }) : null,
      el("p", { class: "quant-file mono", text: String(quant.name || quant.path || "") }),
    ]),
    el("div", { class: "quant-action" }, [button]),
  ]);
}

// ---------------------------------------------------------------- model cards

function metaLine(entry) {
  const bits = [];
  if (entry.author) bits.push(String(entry.author));
  if (Number.isFinite(entry.downloads)) bits.push(`${formatCount(entry.downloads)} downloads`);
  if (Number.isFinite(entry.likes)) bits.push(`${formatCount(entry.likes)} likes`);
  if (Number.isFinite(entry.params_b)) bits.push(`${entry.params_b}B parameters`);
  if (entry.focus && entry.focus !== "general") bits.push(String(entry.focus));
  return bits.join(" · ");
}

/** One repository. The recommended quantisation is the headline, because a
 *  non-expert must never have to know what Q4_K_M means to get a working
 *  model; every other quantisation is one click away in the fold, because an
 *  expert must not be prevented from choosing. */
export function renderModelCard(entry, handlers = {}) {
  const head = [
    el("span", { class: "model-name", text: String(entry.label || entry.repo_id || "unnamed") }),
  ];
  if (entry.gated) {
    head.push(el("span", { class: "model-badge is-gated", text: "Licence required" }));
  }
  if (entry.source === "fallback") {
    head.push(el("span", { class: "model-badge is-reference", text: "Reference, not a live result" }));
  }
  if (entry.verdict) head.push(renderVerdictPill(entry.verdict.verdict));

  const body = [];
  const meta = metaLine(entry);
  if (entry.repo_id) body.push(el("p", { class: "model-repo mono", text: String(entry.repo_id) }));
  if (meta) body.push(el("p", { class: "model-meta", text: meta }));
  if (entry.description) body.push(el("p", { class: "model-desc", text: String(entry.description) }));

  if (entry.gated) {
    body.push(el("p", {
      class: "model-note is-gated",
      text: entry.gated_mode === "manual"
        ? "This repository is gated: its owner approves each request by hand on huggingface.co. Hearth cannot sign in, so it cannot fetch these files."
        : "This repository is gated: Hugging Face requires its licence to be accepted before download. Hearth cannot sign in, so it cannot fetch these files.",
    }));
  }

  if (entry.source === "fallback") {
    body.push(el("p", {
      class: "model-note",
      text: "This is one of Hearth's built-in reference models. The fit verdict was still computed against this machine, but nothing can be downloaded while the Hub is unreachable.",
    }));
  }

  if (entry.files_error) {
    body.push(el("p", { class: "model-note is-error", text: String(entry.files_error) }));
  }

  if (entry.best_quant) {
    body.push(el("p", { class: "quant-lead", text: "Recommended for this machine" }));
    body.push(renderQuantRow(entry, entry.best_quant, handlers, { featured: true }));
  } else if (entry.quants_loaded && entry.quants?.length) {
    body.push(el("p", {
      class: "model-note is-warn",
      text: "No quantisation in this repository fits this machine. Every option is listed below with the reason.",
    }));
  }

  // Filter on the server-set `recommended` flag rather than on object
  // identity with best_quant. hearth_shop returns the very same object in
  // both places, but JSON has no identity: after the response is parsed,
  // entry.best_quant and its twin inside entry.quants are two distinct
  // objects, and an identity filter silently showed the recommended
  // quantisation twice.
  const others = (entry.quants || []).filter((q) => !(entry.best_quant && q.recommended));
  if (entry.quants_loaded && others.length) {
    const fold = el("details", { class: "fold quant-fold" });
    fold.appendChild(el("summary", {
      text: entry.best_quant
        ? `${others.length} other quantisation${others.length === 1 ? "" : "s"}`
        : `All ${others.length} quantisation${others.length === 1 ? "" : "s"}`,
    }));
    for (const quant of others) fold.appendChild(renderQuantRow(entry, quant, handlers));
    body.push(fold);
  } else if (entry.repo_id && !entry.quants_loaded && !entry.files_error) {
    const more = el("button", {
      class: "btn btn-sm", type: "button",
      text: "Grade this model against my machine",
    });
    if (handlers.onLoadQuants) {
      more.addEventListener("click", () => handlers.onLoadQuants(entry, more));
    }
    body.push(el("div", { class: "model-more" }, [more]));
  }

  return appendAll(el("article", { class: "model-card" }), [
    appendAll(el("header", { class: "model-head" }), head),
    appendAll(el("div", { class: "model-body" }), body),
  ]);
}

// ------------------------------------------------------------------ downloads

const DOWNLOAD_STATUS = {
  queued:      "Waiting",
  resolving:   "Reading the repository",
  downloading: "Downloading",
  verifying:   "Verifying",
  done:        "Ready",
  cancelled:   "Paused",
  error:       "Failed",
};

/** The one sentence under a download's bar. Every number in it comes from
 *  hearth_hf; nothing here estimates. Exported so the wording of the
 *  cancel-is-not-loss claim is testable on its own. */
export function downloadDetail(job) {
  const done = Number.isFinite(job.bytes_done) ? job.bytes_done : 0;
  const total = Number.isFinite(job.bytes_total) ? job.bytes_total : null;
  const bits = [];

  if (job.status === "cancelled") {
    bits.push(done > 0
      ? `Paused at ${formatBytes(done)}${total ? ` of ${formatBytes(total)}` : ""}. Those bytes are kept on disk; downloading again resumes from there rather than starting over.`
      : "Cancelled before any bytes arrived.");
    return bits.join(" ");
  }
  if (job.status === "error") {
    if (job.error_kind === "no_space") {
      bits.push("Not enough free disk space.");
    } else if (job.error_kind === "hash_mismatch") {
      bits.push("The downloaded bytes did not match the checksum Hugging Face published, so the file was discarded rather than kept.");
    } else if (job.error_kind === "gated") {
      bits.push("This repository requires a Hugging Face licence, which Hearth cannot accept for you yet.");
    }
    if (job.error) bits.push(String(job.error));
    if (done > 0) bits.push(`${formatBytes(done)} was downloaded and is kept for a retry.`);
    return bits.join(" ");
  }
  if (job.status === "done") {
    bits.push(job.already_present
      ? "Already on disk and verified."
      : `${formatBytes(total || done)} downloaded.`);
    bits.push(job.verification === "sha256"
      ? "Checksum verified against Hugging Face."
      : "Hugging Face published no checksum for this file, so only its length was checked.");
    bits.push("It is selectable as a session model now, with nothing to restart.");
    return bits.join(" ");
  }

  bits.push(total ? `${formatBytes(done)} of ${formatBytes(total)}` : `${formatBytes(done)} so far (total size not published)`);
  if (job.part_count > 1) bits.push(`file ${job.part_index} of ${job.part_count}`);
  if (job.queue_position) bits.push(`${job.queue_position} ahead in the queue`);
  const rate = formatRate(job.speed_bytes_per_sec);
  if (rate) bits.push(rate);
  const eta = formatDuration(job.eta_seconds);
  if (eta) bits.push(`about ${eta} left`);
  if (job.resumed_from) bits.push(`resumed from ${formatBytes(job.resumed_from)}`);
  return bits.join(" · ");
}

/** One download. The bar is driven by the real fraction, and goes
 *  indeterminate rather than guessing when the total is unknown. */
export function renderDownloadRow(job, handlers = {}) {
  const fraction = Number.isFinite(job.fraction) ? Math.max(0, Math.min(1, job.fraction)) : null;
  const fill = el("div", { class: "bar-fill" });
  const bar = el("div", {
    class: "bar" + (fraction === null && job.cancellable ? " is-indeterminate" : "") +
      (job.status === "error" ? " is-error" : job.status === "done" ? " is-done" : ""),
  }, [fill]);
  if (fraction !== null) fill.style.width = `${(fraction * 100).toFixed(1)}%`;

  const actions = [];
  if (job.cancellable && handlers.onCancel) {
    const cancel = el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "Cancel" });
    cancel.addEventListener("click", () => handlers.onCancel(job));
    actions.push(cancel);
  }
  if (!job.cancellable && job.status !== "done" && handlers.onRetry) {
    const retry = el("button", {
      class: "btn btn-sm", type: "button",
      text: job.status === "cancelled" ? "Resume" : "Try again",
    });
    retry.addEventListener("click", () => handlers.onRetry(job));
    actions.push(retry);
  }
  if (job.status === "done" && handlers.onUse) {
    const use = el("button", { class: "btn btn-sm btn-primary", type: "button", text: "Use this model" });
    use.addEventListener("click", () => handlers.onUse(job));
    actions.push(use);
  }
  if (!job.cancellable && handlers.onDismiss) {
    const dismiss = el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "Dismiss" });
    dismiss.addEventListener("click", () => handlers.onDismiss(job));
    actions.push(dismiss);
  }

  const percent = fraction === null ? "" : `${Math.round(fraction * 100)}%`;

  return appendAll(el("div", { class: "dl dl-" + String(job.status || "unknown") }), [
    appendAll(el("div", { class: "dl-head" }), [
      el("span", { class: "dl-name", text: String(job.label || job.filename || "") }),
      el("span", { class: "dl-repo mono", text: String(job.repo_id || "") }),
      el("span", { class: "dl-status", text: DOWNLOAD_STATUS[job.status] || String(job.status || "") }),
      percent ? el("span", { class: "dl-pct", text: percent }) : null,
    ]),
    bar,
    el("p", { class: "dl-detail", text: downloadDetail(job) }),
    actions.length ? appendAll(el("div", { class: "dl-actions" }), actions) : null,
  ]);
}

/** The whole downloads panel. Returns null when there is nothing to show, so
 *  the section can be hidden entirely rather than sitting empty. */
export function renderDownloads(jobs, handlers = {}) {
  const list = Array.isArray(jobs) ? jobs : [];
  if (!list.length) return null;
  const wrap = el("div", { class: "dl-list" });
  for (const job of list) wrap.appendChild(renderDownloadRow(job, handlers));
  return wrap;
}

// ------------------------------------------------------------------ the view

/** Wires the render functions above to the sidecar.
 *
 * `deps` supplies what belongs to the rest of the app rather than to the shop:
 *   sidecar        - the Sidecar client
 *   onModelReady(job) - a download finished; the app refreshes its model list
 *   onUseModel(job)   - the user asked to use a downloaded model now
 *   onError(message)  - surface a failure outside this screen
 */
export class ShopView {
  constructor(root, deps = {}) {
    this.root = root;
    this.sidecar = deps.sidecar;
    this.onModelReady = deps.onModelReady || (() => {});
    this.onUseModel = deps.onUseModel || (() => {});
    this.onDownloadsChanged = deps.onDownloadsChanged || (() => {});
    this.listing = null;
    this.jobs = [];
    this.searched = false;
    this.searching = false;
    this.lastQuery = "";
    this._seenDone = new Set();
    this._streamAbort = null;
    this._build();
  }

  _build() {
    clear(this.root);

    this.input = el("input", {
      class: "input", type: "search", id: "shop-q", spellcheck: false,
      autocomplete: "off", placeholder: "Search Hugging Face: qwen coder, phi, mistral...",
    });
    this.searchBtn = el("button", { class: "btn btn-primary", type: "button", text: "Search" });
    const form = appendAll(el("form", { class: "shop-search" }), [
      this.input, this.searchBtn,
    ]);
    form.addEventListener("submit", (event) => { event.preventDefault(); this.search(); });
    this.searchBtn.addEventListener("click", () => this.search());

    this.hardware = el("div", { class: "hw-slot" });
    this.downloadsSlot = el("section", { class: "shop-downloads", hidden: true });
    this.banner = el("div", { class: "shop-banner-slot" });
    this.results = el("div", { class: "shop-results" });
    this.status = el("p", { class: "shop-status" });

    this.root.appendChild(appendAll(el("div", { class: "shop-inner" }), [
      appendAll(el("header", { class: "shop-head" }), [
        el("h1", { class: "shop-title", text: "Model shop" }),
        el("p", {
          class: "shop-lede",
          text: "Pick a model and press download. Nothing here needs a terminal. Every quantisation is graded against the GPU, memory and disk in this machine, so you can see before you download whether it will actually run.",
        }),
        form,
        this.hardware,
      ]),
      this.downloadsSlot,
      renderLimitations(),
      this.banner,
      this.status,
      this.results,
    ]));

    this.status.textContent = "Search for a model, or press Search with an empty box for the most downloaded GGUF models on Hugging Face.";
  }

  focus() {
    this.input?.focus({ preventScroll: true });
  }

  // ---- search ----

  async search() {
    if (this.searching) return;
    const query = this.input.value.trim();
    this.lastQuery = query;
    this.searching = true;
    this.searchBtn.disabled = true;
    this.status.className = "shop-status";
    this.status.textContent = query
      ? `Searching Hugging Face for "${query}" and grading each result against this machine...`
      : "Loading the most downloaded GGUF models and grading each against this machine...";
    clear(this.results);
    clear(this.banner);
    try {
      this.listing = await this.sidecar.shop({ q: query });
      this.searched = true;
      this._paint();
    } catch (err) {
      this.status.className = "shop-status is-error";
      this.status.textContent = "The shop search failed: " + (err?.message || String(err));
    } finally {
      this.searching = false;
      this.searchBtn.disabled = false;
    }
  }

  _handlers() {
    return {
      onDownload: (entry, quant) => this._startDownload(entry, quant),
      onLoadQuants: (entry, button) => this._loadQuants(entry, button),
    };
  }

  _paint() {
    const listing = this.listing || {};
    clear(this.hardware);
    this.hardware.appendChild(renderHardware(listing.hardware));

    clear(this.banner);
    const notice = renderSourceNotice(listing);
    if (notice) this.banner.appendChild(notice);

    const models = Array.isArray(listing.models) ? listing.models : [];
    clear(this.results);
    if (!models.length) {
      this.status.className = "shop-status";
      this.status.textContent = listing.error
        ? String(listing.error)
        : "No GGUF repositories matched that search. Try a shorter query: model families are usually one word.";
      return;
    }
    const gated = models.filter((m) => m.gated).length;
    const parts = [`${models.length} repositor${models.length === 1 ? "y" : "ies"}`];
    if (listing.source === "fallback") parts.push("from Hearth's built-in reference list, not Hugging Face");
    else parts.push("from Hugging Face, best fit for this machine first");
    if (gated) parts.push(`${gated} gated and not downloadable`);
    this.status.className = "shop-status";
    this.status.textContent = parts.join(" · ") + ".";

    const handlers = this._handlers();
    for (const entry of models) this.results.appendChild(renderModelCard(entry, handlers));
  }

  async _loadQuants(entry, button) {
    button.disabled = true;
    button.textContent = "Reading the repository...";
    try {
      const result = await this.sidecar.shopQuants(entry.repo_id);
      const model = result?.model;
      if (model) {
        Object.assign(entry, model);
        const models = this.listing?.models || [];
        const index = models.indexOf(entry);
        if (index !== -1) models[index] = entry;
        this._paint();
        return;
      }
      button.disabled = false;
      button.textContent = "Grade this model against my machine";
    } catch (err) {
      button.disabled = false;
      button.textContent = "Could not read it: " + (err?.message || String(err));
    }
  }

  // ---- downloads ----

  async _startDownload(entry, quant) {
    const filename = quantTarget(quant);
    if (!entry.repo_id || !filename) return;
    try {
      await this.sidecar.startDownload(entry.repo_id, filename);
    } catch (err) {
      this.status.className = "shop-status is-error";
      this.status.textContent = "Could not start that download: " + (err?.message || String(err));
      return;
    }
    await this.refreshDownloads();
  }

  async refreshDownloads() {
    try {
      const snapshot = await this.sidecar.downloads();
      this.applyDownloads(snapshot);
    } catch { /* the stream is the authority; a failed poll is not worth reporting */ }
  }

  /** Replace the download state wholesale. GET /downloads/events sends the
   *  complete list on every change precisely so this can be an assignment
   *  rather than a merge. */
  applyDownloads(snapshot) {
    const jobs = Array.isArray(snapshot?.downloads) ? snapshot.downloads : [];
    this.jobs = jobs;
    for (const job of jobs) {
      if (job.status === "done" && !this._seenDone.has(job.id)) {
        this._seenDone.add(job.id);
        this.onModelReady(job);
      }
    }
    this._paintDownloads();
    this.onDownloadsChanged(jobs);
  }

  _paintDownloads() {
    clear(this.downloadsSlot);
    const handlers = {
      onCancel: async (job) => {
        try { await this.sidecar.cancelDownload(job.id); } catch { /* already settled */ }
        await this.refreshDownloads();
      },
      onDismiss: async (job) => {
        try { await this.sidecar.dismissDownload(job.id); } catch { /* already gone */ }
        await this.refreshDownloads();
      },
      // Resuming starts a NEW job against the same .part file, so the row
      // that was paused is superseded the moment the new one exists.
      // Leaving both on screen would show a stalled 29% next to a live 66%
      // for the same file, which reads as two downloads fighting rather
      // than as one that carried on.
      onRetry: async (job) => {
        try {
          await this.sidecar.startDownload(job.repo_id, job.filename);
          await this.sidecar.dismissDownload(job.id);
        } catch { /* the refreshed list below shows whatever actually happened */ }
        await this.refreshDownloads();
      },
      onUse: (job) => this.onUseModel(job),
    };
    const list = renderDownloads(this.jobs, handlers);
    if (!list) {
      this.downloadsSlot.hidden = true;
      return;
    }
    this.downloadsSlot.hidden = false;
    appendAll(this.downloadsSlot, [
      el("h2", { class: "shop-section-title", text: "Downloads" }),
      list,
    ]);
  }

  /** Keep the download list live for the lifetime of the page, whichever view
   *  is on screen. Reconnects with backoff; each reconnect starts from
   *  version 0, which is correct precisely because every frame is a full
   *  snapshot. */
  startDownloadStream() {
    if (this._streamAbort) return;
    (async () => {
      let backoff = 400;
      for (;;) {
        const controller = new AbortController();
        this._streamAbort = controller;
        try {
          await this.sidecar.streamDownloads({
            since: 0,
            signal: controller.signal,
            onSnapshot: (snapshot) => { backoff = 400; this.applyDownloads(snapshot); },
          });
          await new Promise((done) => setTimeout(done, 150));
        } catch (err) {
          if (controller.signal.aborted) return;
          await new Promise((done) => setTimeout(done, backoff));
          backoff = Math.min(backoff * 2, 5000);
        }
      }
    })();
  }

  stopDownloadStream() {
    if (this._streamAbort) {
      this._streamAbort.abort();
      this._streamAbort = null;
    }
  }
}
