#!/usr/bin/env node
/* hearth UI dev host.
 *
 *   node desktop/ui/dev-host.mjs                 start the sidecar and serve the UI
 *   node desktop/ui/dev-host.mjs --port 4173     choose the dev host port
 *   node desktop/ui/dev-host.mjs --attach '{"port":1234,"token":"..."}'
 *                                                use an already-running sidecar
 *   node desktop/ui/dev-host.mjs --open          also open the default browser
 *
 * Why this exists at all, rather than opening index.html directly
 * ---------------------------------------------------------------
 * The sidecar rejects a browser page served from any other origin. Reading
 * desktop/server/auth.py: check_host requires the Host header to be exactly
 * 127.0.0.1:<sidecar port> or localhost:<sidecar port>, and check_origin
 * requires any Origin header that IS present to name that same port. A page
 * served on a different port sends its own Origin on every cross-origin fetch,
 * so it is refused with 403; app.py also sends no CORS headers and has no
 * OPTIONS handler, so the preflight would fail first anyway. A file:// page
 * sends Origin: null and is refused too.
 *
 * So the page has to be same-origin with whatever it talks to. The sidecar
 * serves no static files (app.py's routing table 404s everything else) and is
 * out of scope to change. This process therefore serves desktop/ui/ AND
 * reverse-proxies the sidecar's routes on the same origin, which is the
 * arrangement Tauri will replace: there, the Rust side owns the sidecar
 * process and the webview reaches it without a browser origin at all.
 *
 * What this proxy does and does not do
 * ------------------------------------
 * It does: rewrite Host to the sidecar's real 127.0.0.1:<port>, drop the
 * browser's cross-port Origin so the request looks like the non-browser
 * clients auth.py already expects (scripts/e2e_live.py sends no Origin), and
 * stream bodies through untouched in both directions so SSE stays live.
 *
 * It does NOT mint or inject credentials. The Authorization header is
 * forwarded exactly as the page sent it, and a request that arrives without
 * one is refused here with 401 without contacting the sidecar. So this proxy
 * grants no authority of its own: holding the sidecar's token is still the
 * only way to drive it.
 *
 * Honest limitation: GET /__hearth/handshake below hands the sidecar's token
 * to whoever asks on loopback, which is how the page gets it. Any other local
 * process could ask too. This is a development shim and it is loopback-only,
 * but it is a real widening compared to the sidecar alone, and it is the
 * reason this file is not something to ship. In the Tauri build the token goes
 * to the webview over IPC and no HTTP endpoint ever returns it.
 */

import { createServer, request as httpRequest } from "node:http";
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve, sep, extname } from "node:path";
import process from "node:process";

const UI_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(UI_DIR, "..", "..");
const SERVER_DIR = join(REPO_ROOT, "desktop", "server");

/* Exactly the routes desktop/server/app.py serves. Anything else is either a
 * static asset from desktop/ui or a 404: this list is the whole proxy surface,
 * so a typo in the page cannot turn into an open forwarder. */
const SIDECAR_ROUTES = new Set([
  "/healthz", "/session", "/prompt", "/events", "/approve",
  "/cancel", "/models", "/checkpoints", "/restore", "/setup", "/idle",
  "/shop", "/shop/quants",
  "/downloads", "/downloads/events", "/downloads/cancel", "/downloads/dismiss",
  "/engine", "/engine/events",
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
  ".woff2": "font/woff2",
};

// --------------------------------------------------------------- arguments

function parseArgs(argv) {
  const out = { port: 4173, open: false, attach: null, python: null };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--port") out.port = Number(argv[++i]);
    else if (arg === "--open") out.open = true;
    else if (arg === "--attach") out.attach = argv[++i];
    else if (arg === "--python") out.python = argv[++i];
    else if (arg === "--help" || arg === "-h") { printUsage(); process.exit(0); }
    else { console.error(`unknown argument: ${arg}`); printUsage(); process.exit(2); }
  }
  return out;
}

function printUsage() {
  console.log([
    "usage: node desktop/ui/dev-host.mjs [options]",
    "  --port <n>        dev host port (default 4173)",
    "  --attach <json>   attach to a running sidecar instead of starting one,",
    "                    passing its handshake line verbatim",
    "  --python <exe>    python executable used to start the sidecar",
    "  --open            open the UI in the default browser",
  ].join("\n"));
}

// ----------------------------------------------------------------- sidecar

function defaultPython() {
  return process.env.HEARTH_PYTHON || (process.platform === "win32" ? "python" : "python3");
}

/** Start desktop/server/main.py and resolve with its one-line JSON handshake.
 *  This is the same launch scripts/e2e_live.py performs: run main.py with
 *  desktop/server as the working directory and read the first stdout line. */
function startSidecar(python) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(python, [join(SERVER_DIR, "main.py")], {
      cwd: SERVER_DIR,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });

    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill();
      rejectPromise(new Error("the sidecar printed no handshake within 20s"));
    }, 20000);

    child.on("error", (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      rejectPromise(new Error(`could not start ${python}: ${err.message}`));
    });

    child.stderr.on("data", (chunk) => process.stderr.write("[sidecar] " + chunk));

    const lines = createInterface({ input: child.stdout });
    lines.on("line", (line) => {
      if (settled) { process.stdout.write("[sidecar] " + line + "\n"); return; }
      let handshake;
      try { handshake = JSON.parse(line); } catch {
        process.stdout.write("[sidecar] " + line + "\n");
        return;
      }
      if (!handshake || typeof handshake.port !== "number" || typeof handshake.token !== "string") {
        return;
      }
      settled = true;
      clearTimeout(timer);
      resolvePromise({ child, handshake });
    });

    child.on("exit", (code) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        rejectPromise(new Error(`the sidecar exited with code ${code} before printing a handshake`));
      } else {
        console.error(`[dev-host] the sidecar exited with code ${code}`);
      }
    });
  });
}

// ------------------------------------------------------------------- proxy

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
      // SSE must not sit in a buffer waiting for more bytes.
      res.flushHeaders();
      res.socket?.setNoDelay(true);
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

  // A browser aborting an SSE fetch (page reload, reconnect) must tear the
  // upstream request down too, or the sidecar's bounded SSE slots leak.
  const teardown = () => upstream.destroy();
  res.on("close", teardown);
  req.on("aborted", teardown);

  req.pipe(upstream);
}

// -------------------------------------------------------------- static files

async function serveStatic(req, res) {
  const url = new URL(req.url, "http://localhost");
  let pathname = decodeURIComponent(url.pathname);
  if (pathname === "/") pathname = "/index.html";

  const target = resolve(UI_DIR, "." + pathname);
  if (target !== UI_DIR && !target.startsWith(UI_DIR + sep)) {
    res.writeHead(403).end("forbidden");
    return;
  }
  try {
    const body = await readFile(target);
    res.writeHead(200, {
      "content-type": MIME[extname(target).toLowerCase()] || "application/octet-stream",
      "cache-control": "no-store",
      // The UI loads nothing from anywhere but itself. This is belt and braces
      // on top of the no-innerHTML rule in js/dom.js, not a substitute for it.
      "content-security-policy":
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
      "referrer-policy": "no-referrer",
      "x-content-type-options": "nosniff",
    });
    res.end(body);
  } catch {
    res.writeHead(404, { "content-type": "text/plain" }).end("not found");
  }
}

// ------------------------------------------------------------ folder picker

/** Native folder chooser, Windows only, used by the sidebar's browse button.
 *  Nothing from the request reaches the command; there are no parameters. */
function pickFolder() {
  if (process.platform !== "win32") {
    return Promise.resolve({ error: "No native folder picker on this platform." });
  }
  const script = [
    "Add-Type -AssemblyName System.Windows.Forms | Out-Null",
    "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog",
    "$dialog.Description = 'Choose a workspace folder for hearth'",
    "$dialog.ShowNewFolderButton = $true",
    "$owner = New-Object System.Windows.Forms.Form",
    "$owner.TopMost = $true",
    "if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dialog.SelectedPath }",
    "$owner.Dispose()",
  ].join("; ");

  return new Promise((resolvePromise) => {
    const child = spawn("powershell.exe", ["-STA", "-NoProfile", "-NonInteractive", "-Command", script], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    child.stdout.on("data", (chunk) => { out += chunk; });
    child.on("error", () => resolvePromise({ error: "Could not open the folder picker." }));
    child.on("exit", () => {
      const path = out.trim();
      resolvePromise(path ? { path } : { error: "No folder chosen." });
    });
  });
}

// -------------------------------------------------------------------- main

async function main() {
  const args = parseArgs(process.argv.slice(2));

  let handshake;
  let child = null;
  if (args.attach) {
    try {
      handshake = JSON.parse(args.attach);
    } catch {
      console.error("--attach needs the sidecar's handshake line as JSON");
      process.exit(2);
    }
    if (typeof handshake.port !== "number" || typeof handshake.token !== "string") {
      console.error("--attach JSON needs numeric \"port\" and string \"token\"");
      process.exit(2);
    }
  } else {
    const python = args.python || defaultPython();
    console.log(`[dev-host] starting the sidecar with ${python}`);
    ({ child, handshake } = await startSidecar(python));
    console.log(`[dev-host] sidecar listening on 127.0.0.1:${handshake.port} (pid ${handshake.pid})`);
  }

  const server = createServer((req, res) => {
    if (!isLoopback(req.socket)) {
      res.writeHead(403, { "content-type": "text/plain" }).end("loopback only");
      return;
    }
    const path = (req.url || "/").split("?")[0];

    if (path === "/__hearth/handshake") {
      res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" });
      res.end(JSON.stringify({
        port: handshake.port,
        token: handshake.token,
        default_workspace: REPO_ROOT,
      }));
      return;
    }

    if (path === "/__hearth/pick-folder" && req.method === "POST") {
      pickFolder().then((result) => {
        res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" });
        res.end(JSON.stringify(result));
      });
      return;
    }

    if (SIDECAR_ROUTES.has(path)) {
      // This proxy adds no authority: no token here means no request upstream.
      if (path !== "/healthz" && !req.headers.authorization) {
        res.writeHead(401, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: "missing Authorization header" }));
        return;
      }
      proxyToSidecar(req, res, handshake.port);
      return;
    }

    serveStatic(req, res);
  });

  server.on("clientError", (err, socket) => {
    if (socket.writable) socket.end("HTTP/1.1 400 Bad Request\r\n\r\n");
  });

  await new Promise((done) => server.listen(args.port, "127.0.0.1", done));
  const url = `http://127.0.0.1:${server.address().port}/`;
  console.log(`[dev-host] hearth UI at ${url}`);
  if (args.open) {
    const opener = process.platform === "win32" ? ["cmd", ["/c", "start", "", url]]
      : process.platform === "darwin" ? ["open", [url]]
      : ["xdg-open", [url]];
    spawn(opener[0], opener[1], { detached: true, stdio: "ignore" }).unref();
  }

  const shutdown = () => {
    console.log("\n[dev-host] shutting down");
    server.close();
    if (child && child.exitCode === null) child.kill();
    setTimeout(() => process.exit(0), 250).unref();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

main().catch((err) => {
  console.error("[dev-host] " + err.message);
  process.exit(1);
});
