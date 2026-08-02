/* Sidecar client.
 *
 * SSE authentication, and why this does not use EventSource
 * ---------------------------------------------------------
 * Every sidecar route except GET /healthz requires an `Authorization: Bearer`
 * header (desktop/server/auth.py, check_bearer). `EventSource` cannot set
 * request headers at all, and desktop/server/app.py's _get_events offers no
 * alternative: reading it, the only two things GET /events takes besides the
 * bearer header are `?since=<id>` and a `Last-Event-ID` header, and both of
 * those are resumption bookmarks, not credentials. There is no token query
 * parameter, and adding one would have meant putting the token in a URL, which
 * is exactly what must not happen (URLs land in history, in referrers, and in
 * any logging the transport does).
 *
 * So this reads the stream with `fetch()` and consumes `response.body` as a
 * ReadableStream, parsing the SSE framing here. `fetch` can set the header,
 * `AbortController` gives a clean way to tear the stream down on reconnect,
 * and the token stays in an `Authorization` header on the wire and in a
 * module-private variable in memory. It is never written to localStorage or
 * sessionStorage, never placed in a URL or query string, and never put in the
 * document title.
 *
 * The `since` bookmark is carried in the query string, which is fine: it is an
 * event sequence number, not a secret. app.py replays from the session's event
 * ring, and emits a synthetic `events_dropped` event if the bookmark has fallen
 * out of that ring, so a reconnect can tell "caught up" from "missed some".
 */

const SIDECAR_ROUTES = new Set([
  "/healthz", "/session", "/prompt", "/events", "/approve",
  "/cancel", "/models", "/checkpoints", "/restore", "/setup", "/idle",
  "/shop", "/shop/quants",
  "/downloads", "/downloads/events", "/downloads/cancel", "/downloads/dismiss",
  "/engine", "/engine/events",
  "/loop", "/loop/events",
  "/swarm", "/swarm/events",
]);

export class HttpError extends Error {
  constructor(status, body) {
    super(typeof body?.error === "string" ? body.error : `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

export class Sidecar {
  /** `origin` is where the requests are sent. In the browser dev host that is
   *  the page's own origin, which reverse-proxies to the sidecar's ephemeral
   *  port (see dev-host.mjs); the sidecar refuses a cross-origin browser
   *  request outright, so a same-origin proxy is not a convenience here, it is
   *  the only way a page can reach it at all. */
  constructor(origin = "") {
    this.origin = origin.replace(/\/$/, "");
    this._token = null;
  }

  /** Point the client at the origin the shell reported. Empty string means
   *  "wherever this page came from", which is what both current shells give
   *  us; it is settable so a shell that serves the UI and the API from
   *  different places does not need this file changed. */
  setOrigin(origin) {
    this.origin = (origin || "").replace(/\/$/, "");
  }

  /** Kept in a private field for the lifetime of the page and nowhere else. */
  setToken(token) {
    this._token = token || null;
  }

  get authenticated() {
    return Boolean(this._token);
  }

  _headers(extra) {
    const headers = new Headers(extra || {});
    if (this._token) headers.set("Authorization", "Bearer " + this._token);
    return headers;
  }

  async request(method, path, body) {
    if (!SIDECAR_ROUTES.has(path.split("?")[0])) {
      throw new Error(`refusing to call unknown sidecar route: ${path}`);
    }
    const headers = this._headers();
    let payload;
    if (body !== undefined) {
      headers.set("Content-Type", "application/json");
      payload = JSON.stringify(body);
    }
    const response = await fetch(this.origin + path, {
      method,
      headers,
      body: payload,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
    const raw = await response.text();
    let parsed = null;
    if (raw) {
      try { parsed = JSON.parse(raw); } catch { parsed = { error: raw.slice(0, 500) }; }
    }
    if (!response.ok) throw new HttpError(response.status, parsed);
    return parsed;
  }

  health()                 { return this.request("GET", "/healthz"); }
  setup()                  { return this.request("GET", "/setup"); }
  models()                 { return this.request("GET", "/models"); }
  getSession()             { return this.request("GET", "/session"); }
  createSession(body)      { return this.request("POST", "/session", body); }
  prompt(message)          { return this.request("POST", "/prompt", { message }); }
  approve(id, decision)    { return this.request("POST", "/approve", { id, decision }); }
  cancel()                 { return this.request("POST", "/cancel"); }
  checkpoints()            { return this.request("GET", "/checkpoints"); }
  restore(checkpointId)    { return this.request("POST", "/restore", { checkpoint_id: checkpointId }); }

  /** Search the shop. The query is a user-typed string and goes in the query
   *  string, which is fine: it is not a secret. The bearer token never does --
   *  it only ever travels in an Authorization header (see the note at the top
   *  of this file). */
  shop({ q = "", limit, detail, context } = {}) {
    const params = new URLSearchParams({ q });
    if (limit) params.set("limit", String(limit));
    if (detail) params.set("detail", String(detail));
    if (context) params.set("context", String(context));
    return this.request("GET", "/shop?" + params.toString());
  }

  shopQuants(repoId, { context } = {}) {
    const params = new URLSearchParams({ repo: repoId });
    if (context) params.set("context", String(context));
    return this.request("GET", "/shop/quants?" + params.toString());
  }

  downloads()                     { return this.request("GET", "/downloads"); }
  startDownload(repoId, filename) { return this.request("POST", "/downloads", { repo_id: repoId, filename }); }
  cancelDownload(id)              { return this.request("POST", "/downloads/cancel", { id }); }
  dismissDownload(id)             { return this.request("POST", "/downloads/dismiss", { id }); }

  /** The work loop's run state: which turn, what it has spent against each
   *  ceiling, what it may do, whether a stop is pending, how many abandoned
   *  tool calls are still live, and the account once it ends. A versioned
   *  whole snapshot, not a delta log.
   *
   *  There is deliberately no stopLoop(): a loop is stopped with cancel(),
   *  the same call that stops a chat turn, because two things that mean
   *  "stop" are two things that can disagree. */
  loop()               { return this.request("GET", "/loop"); }

  /** Open GET /loop/events and call `onSnapshot(snapshot)` per frame. Same
   *  contract as streamDownloads: every frame is the whole state. */
  async streamLoop({ since = 0, onSnapshot, signal }) {
    return this._streamSnapshots("/loop/events", { since, onSnapshot, signal });
  }

  /** The agent swarm's run state: which role is active, the phases so far,
   *  and the ONE budget every role shares. Same versioned-whole-snapshot
   *  contract as loop(), and there is deliberately no stopSwarm() either --
   *  cancel() is the one kill switch for every engine. */
  swarm()              { return this.request("GET", "/swarm"); }

  /** Open GET /swarm/events and call `onSnapshot(snapshot)` per frame. */
  async streamSwarm({ since = 0, onSnapshot, signal }) {
    return this._streamSnapshots("/swarm/events", { since, onSnapshot, signal });
  }

  engine()             { return this.request("GET", "/engine"); }
  /** Start (or, with force, retry) the GPU engine fetch. Returns at once:
   *  the fetch runs on the sidecar's own thread and Hearth stays usable on
   *  the bundled CPU engine while it happens. */
  fetchEngine(force = false) { return this.request("POST", "/engine", { force }); }

  /** Open GET /engine/events and call `onSnapshot(snapshot)` per frame.
   *  Same contract as streamDownloads: every frame is the whole state. */
  async streamEngine({ since = 0, onSnapshot, signal }) {
    return this._streamSnapshots("/engine/events", { since, onSnapshot, signal });
  }

  /** Open GET /downloads/events and call `onSnapshot({version, downloads})`
   *  for each frame. Unlike /events this is not a delta log: every frame is
   *  the complete list, so a caller replaces its state rather than folding
   *  events into it, and a reload needs no replay. */
  async streamDownloads({ since = 0, onSnapshot, signal }) {
    return this._streamSnapshots("/downloads/events", { since, onSnapshot, signal });
  }

  /** The shared body of streamDownloads and streamEngine. Both surfaces send
   *  whole snapshots rather than deltas, so the only thing that differs is
   *  the path; one implementation means a fix to the frame parsing cannot
   *  land on one stream and miss the other. */
  async _streamSnapshots(path, { since = 0, onSnapshot, signal }) {
    const url = `${this.origin}${path}?since=${encodeURIComponent(since)}`;
    const response = await fetch(url, {
      headers: this._headers({ Accept: "text/event-stream" }),
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      signal,
    });
    if (!response.ok) {
      let body = null;
      try { body = JSON.parse(await response.text()); } catch { /* body is not JSON */ }
      throw new HttpError(response.status, body);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let split;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const parsed = parseFrame(frame);
          if (parsed && parsed.payload && onSnapshot) onSnapshot(parsed.payload);
        }
      }
    } finally {
      try { reader.cancel().catch(() => {}); } catch { /* already released */ }
    }
  }

  /** Open GET /events and call `onEvent({id, kind, turn_id, data, ts})` for
   *  each frame until `signal` aborts or the connection ends. Resolves when
   *  the stream closes; rejects only on a transport failure. */
  async streamEvents({ since = 0, onEvent, onOpen, signal }) {
    const url = `${this.origin}/events?since=${encodeURIComponent(since)}`;
    const response = await fetch(url, {
      headers: this._headers({ Accept: "text/event-stream" }),
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      signal,
    });
    if (!response.ok) {
      let body = null;
      try { body = JSON.parse(await response.text()); } catch { /* body is not JSON */ }
      throw new HttpError(response.status, body);
    }
    if (onOpen) onOpen();

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // Frames are separated by a blank line. Keep the trailing partial.
        let split;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const parsed = parseFrame(frame);
          if (parsed && onEvent) onEvent(parsed);
        }
      }
    } finally {
      // cancel() returns a promise that rejects if the stream was already torn
      // down by an abort, which is the normal path on reconnect. Swallow it
      // here rather than leaving an unhandled rejection behind.
      try { reader.cancel().catch(() => {}); } catch { /* already released */ }
    }
  }
}

/** Parse one SSE frame. Comment lines (": keep-alive") and frames without a
 *  JSON data payload yield null. app.py writes exactly one `data:` line per
 *  frame, containing a single-line JSON object. */
function parseFrame(frame) {
  let id = null;
  let data = null;
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") id = Number(value);
    else if (field === "data") data = data === null ? value : data + "\n" + value;
  }
  if (data === null) return null;
  let payload;
  try { payload = JSON.parse(data); } catch { return null; }
  return {
    id: Number.isFinite(id) ? id : null,
    kind: payload.kind,
    turn_id: payload.turn_id,
    data: payload.data || {},
    ts: payload.ts,
    // The whole decoded object, for streams whose frames are not turn events.
    // GET /downloads/events sends {version, downloads} rather than the
    // {kind, turn_id, data} shape GET /events uses.
    payload,
  };
}

/* ------------------------------------------------------------ the shell
 *
 * Everything below is the whole contract between this UI and whatever is
 * hosting it. Two calls: where the sidecar is and how to authenticate to it,
 * and a native folder chooser. Nothing here names Electron, or Tauri, or the
 * dev host, and nothing else in desktop/ui/ talks to the host at all, so
 * changing hosts is a change to the host.
 *
 * `globalThis.hearth` is the packaged path: the shell reads the handshake off
 * the sidecar's stdout in its own process and passes it in over a private
 * channel. The HTTP fallback is dev-host.mjs, which serves the same values at
 * GET /__hearth/handshake to anything on loopback that asks. That is a real
 * widening and it is why the dev host is a development tool and not the
 * shipped app -- see the note at the top of dev-host.mjs. Preferring the
 * bridge whenever one exists means the packaged app never takes that path.
 */

/** True when a shell has injected its bridge. */
export function hasShellBridge() {
  const bridge = globalThis.hearth;
  return Boolean(bridge && typeof bridge.handshake === "function");
}

/** {origin, token, default_workspace} for the sidecar to talk to. */
export async function readHandshake() {
  if (hasShellBridge()) {
    const shell = await globalThis.hearth.handshake();
    return {
      origin: shell.origin || "",
      token: shell.token,
      default_workspace: shell.defaultWorkspace || "",
    };
  }
  const response = await fetch("/__hearth/handshake", {
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  if (!response.ok) throw new HttpError(response.status, null);
  const body = await response.json();
  return { origin: "", token: body.token, default_workspace: body.default_workspace || "" };
}

/** Native folder chooser. Resolves {path} or {error}; never throws. */
export async function pickFolder() {
  try {
    if (globalThis.hearth && typeof globalThis.hearth.pickFolder === "function") {
      return await globalThis.hearth.pickFolder();
    }
    const response = await fetch("/__hearth/pick-folder", {
      method: "POST",
      cache: "no-store",
      credentials: "omit",
    });
    return await response.json();
  } catch {
    return { error: "No folder picker available here." };
  }
}
