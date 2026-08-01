"use strict";
/* The loopback origin the renderer is served from, and the only path its
 * requests take to the sidecar.
 *
 * Why an HTTP origin exists at all in a desktop app
 * ------------------------------------------------
 * The sidecar refuses a browser page served from anywhere else. auth.py's
 * check_host wants a Host header of exactly 127.0.0.1:<sidecar port> or
 * localhost:<sidecar port>, check_origin refuses any Origin header naming a
 * different port, app.py sends no CORS headers and answers no OPTIONS, and a
 * file:// page sends Origin: null. So the page must be same-origin with
 * whatever it talks to, and the sidecar serves no static files. Something has
 * to serve the UI and forward the API on one origin. This is that something.
 *
 * What this does NOT do, and dev-host.mjs does
 * --------------------------------------------
 * dev-host.mjs answers GET /__hearth/handshake with the sidecar's bearer
 * token, to any process on loopback that asks. Its own comments call that out
 * as the reason it is not shippable. There is no such route here and no route
 * that returns the token in any form. The packaged shell reads the handshake
 * off the sidecar's stdout and hands it to the renderer over Electron IPC,
 * which no other process can call.
 *
 * The authority this server holds
 * -------------------------------
 * None of its own, which is the whole design. It mints no credentials and
 * injects none: the Authorization header is forwarded exactly as the page
 * sent it, and a request to anything but /healthz that arrives without one is
 * refused here with 401 before the sidecar is contacted. Holding the token is
 * still the only way to drive the sidecar, and the token only ever leaves the
 * main process over IPC. Beyond that this adds three checks dev-host does not
 * have: the request must come from loopback, its Origin (when it sends one)
 * must be this exact origin, and its path must be in the sidecar's route
 * allowlist. The static files it serves are the UI's own source, which is
 * shipped in the installer and is not secret.
 *
 * Portability note for the eventual Tauri port
 * -------------------------------------------
 * This file is the Electron-shaped part of the shell. Tauri replaces it
 * outright: there the Rust side owns the sidecar and the webview reaches it
 * without a browser origin at all. Nothing above this layer knows it exists --
 * the UI is handed {origin, token} and uses them, so swapping the transport
 * is a change to this file and to main.js, not to desktop/ui/.
 */

const { createServer, request: httpRequest } = require("node:http");
const { readFile } = require("node:fs/promises");
const { resolve, join, sep, extname } = require("node:path");

/* Exactly the routes desktop/server/app.py serves. Anything else is either a
 * static asset or a 404: this list is the whole proxy surface, so a typo in
 * the page cannot turn into an open forwarder. Kept in step with the same
 * list in desktop/ui/js/api.js and dev-host.mjs. */
const SIDECAR_ROUTES = new Set([
  "/healthz", "/session", "/prompt", "/events", "/approve",
  "/cancel", "/models", "/checkpoints", "/restore", "/setup", "/idle",
  "/shop", "/shop/quants",
  "/downloads", "/downloads/events", "/downloads/cancel", "/downloads/dismiss",
  "/engine", "/engine/events",
  "/loop", "/loop/events",
]);

const HOP_BY_HOP = new Set([
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade",
]);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".png": "image/png",
  ".woff2": "font/woff2",
};

/* The UI loads nothing from anywhere but itself and talks to nothing but its
 * own origin. This is belt and braces on top of the no-innerHTML rule in
 * js/dom.js, not a substitute for it. Identical to what dev-host.mjs sends,
 * so a page that passes xss-check under the dev host passes here. */
const CSP =
  "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
  "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

function isLoopback(socket) {
  const address = socket.remoteAddress || "";
  return address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1";
}

function proxyToSidecar(req, res, sidecarPort) {
  const headers = {};
  for (const [name, value] of Object.entries(req.headers)) {
    const lower = name.toLowerCase();
    if (HOP_BY_HOP.has(lower)) continue;
    if (lower === "host" || lower === "origin" || lower === "referer") continue;
    headers[name] = value;
  }
  // auth.py's DNS-rebinding defence checks this literal value.
  headers.host = `127.0.0.1:${sidecarPort}`;

  const upstream = httpRequest(
    { host: "127.0.0.1", port: sidecarPort, method: req.method, path: req.url, headers },
    (upstreamRes) => {
      const outHeaders = {};
      for (const [name, value] of Object.entries(upstreamRes.headers)) {
        if (HOP_BY_HOP.has(name.toLowerCase())) continue;
        outHeaders[name] = value;
      }
      outHeaders["cache-control"] = outHeaders["cache-control"] || "no-store";
      res.writeHead(upstreamRes.statusCode || 502, outHeaders);
      // Token streaming and download progress are SSE. Neither may sit in a
      // buffer waiting for more bytes.
      res.flushHeaders();
      if (res.socket) res.socket.setNoDelay(true);
      upstreamRes.pipe(res);
    },
  );

  upstream.on("error", (err) => {
    if (!res.headersSent) {
      res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: `sidecar unreachable: ${err.message}` }));
    } else {
      res.end();
    }
  });

  // A renderer aborting an SSE fetch (reload, reconnect) must tear the
  // upstream request down too, or the sidecar's bounded SSE slots leak.
  const teardown = () => upstream.destroy();
  res.on("close", teardown);
  req.on("aborted", teardown);

  req.pipe(upstream);
}

async function serveStatic(req, res, uiDir) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { "content-type": "text/plain" }).end("method not allowed");
    return;
  }
  const url = new URL(req.url, "http://localhost");
  let pathname;
  try {
    pathname = decodeURIComponent(url.pathname);
  } catch {
    res.writeHead(400, { "content-type": "text/plain" }).end("bad request");
    return;
  }
  if (pathname === "/") pathname = "/index.html";

  const target = resolve(uiDir, "." + pathname);
  if (target !== uiDir && !target.startsWith(uiDir + sep)) {
    res.writeHead(403, { "content-type": "text/plain" }).end("forbidden");
    return;
  }
  try {
    const body = await readFile(target);
    res.writeHead(200, {
      "content-type": MIME[extname(target).toLowerCase()] || "application/octet-stream",
      "content-length": body.length,
      "cache-control": "no-store",
      "content-security-policy": CSP,
      "referrer-policy": "no-referrer",
      "x-content-type-options": "nosniff",
    });
    res.end(req.method === "HEAD" ? undefined : body);
  } catch {
    res.writeHead(404, { "content-type": "text/plain" }).end("not found");
  }
}

/** Start the origin. Resolves with {origin, port, close()}.
 *
 *  uiDir       directory holding index.html and js/
 *  sidecarPort the port from the sidecar's handshake line
 */
function startOrigin({ uiDir, sidecarPort }) {
  const root = resolve(uiDir);
  let selfOrigin = null;

  const server = createServer((req, res) => {
    if (!isLoopback(req.socket)) {
      res.writeHead(403, { "content-type": "text/plain" }).end("loopback only");
      return;
    }
    // A page in the user's browser cannot read a cross-origin response
    // without CORS, but it can still send one. Refusing a foreign Origin
    // outright means it cannot even do that, and costs nothing: our own
    // renderer sends either this origin or no Origin at all.
    const origin = req.headers.origin;
    if (origin && origin !== selfOrigin) {
      res.writeHead(403, { "content-type": "text/plain" }).end("forbidden origin");
      return;
    }

    const path = (req.url || "/").split("?")[0];

    if (SIDECAR_ROUTES.has(path)) {
      // This proxy adds no authority: no token here means no request upstream.
      if (path !== "/healthz" && !req.headers.authorization) {
        res.writeHead(401, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: "missing Authorization header" }));
        return;
      }
      proxyToSidecar(req, res, sidecarPort);
      return;
    }

    serveStatic(req, res, root);
  });

  server.on("clientError", (err, socket) => {
    if (socket.writable) socket.end("HTTP/1.1 400 Bad Request\r\n\r\n");
  });

  return new Promise((done, fail) => {
    server.once("error", fail);
    // Port 0: the OS picks. A fixed port would let a stale process from a
    // previous run answer this one's requests.
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      selfOrigin = `http://127.0.0.1:${port}`;
      done({
        origin: selfOrigin,
        port,
        close: () => new Promise((closed) => server.close(closed)),
      });
    });
  });
}

module.exports = { startOrigin, SIDECAR_ROUTES, CSP };
