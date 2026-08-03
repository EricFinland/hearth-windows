//! The loopback origin the renderer is served from, and the only path its
//! requests take to the sidecar.
//!
//! Why an HTTP origin exists at all in a desktop app
//! ------------------------------------------------
//! The sidecar refuses a browser page served from anywhere else. auth.py's
//! check_host wants a Host header of exactly 127.0.0.1:<sidecar port> or
//! localhost:<sidecar port>, check_origin refuses any Origin header naming a
//! different port, app.py sends no CORS headers and answers no OPTIONS. So the
//! page must be same-origin with whatever it talks to, and the sidecar serves
//! no static files. Something has to serve the UI and forward the API on one
//! origin. This is that something, and it is a straight port of the Electron
//! shell's origin.js: the same allowlist, the same three refusals, the same
//! CSP, and the same rule that it grants no authority of its own.
//!
//! What is different from origin.js, and why
//! -----------------------------------------
//! The static half serves bytes that were compiled INTO this executable
//! (tauri.conf.json's `frontendDist`, embedded by tauri-build), not files in a
//! directory next to it. There is therefore no path to contain: `..` cannot
//! escape a lookup in a table that has no filesystem underneath it. Under
//! Electron the equivalent guarantee needed two fuses (OnlyLoadAppFromAsar and
//! EnableEmbeddedAsarIntegrityValidation) and a build gate to check they had
//! actually been applied. Here it is a property of how the binary was linked.
//!
//! The authority this server holds
//! -------------------------------
//! None of its own, which is the whole design. It mints no credentials and
//! injects none: the Authorization header is forwarded exactly as the page
//! sent it, and a request to anything but /healthz that arrives without one is
//! refused here with 401 before the sidecar is contacted. Holding the token is
//! still the only way to drive the sidecar, and the token only ever leaves the
//! shell process over Tauri's IPC (see main.rs). Beyond that this adds three
//! checks: the request must come from loopback, its Origin (when it sends one)
//! must be this exact origin, and its path must be in the sidecar's route
//! allowlist.
//!
//! Connection handling
//! -------------------
//! Every proxied request is forwarded with `Connection: close` and the
//! response is relayed with `Connection: close`, so one client connection
//! carries exactly one request. That is not a performance compromise on
//! loopback, and it removes the whole class of request-smuggling bug that
//! app.py's own docstring describes: a connection that never serves a second
//! request cannot serve an attacker's smuggled one. It also means the browser
//! releases its socket the moment a response ends rather than parking it in a
//! keep-alive pool, which matters because the page gets a fixed number of
//! them: measured in WebView2, ten sockets to this origin, after which the
//! page cannot reach itself at all. Chromium under Electron allowed six, and
//! the updater panel had to be rewritten to poll because of it. See
//! docs/packaging-windows.md for the measurement.

use std::collections::HashSet;
use std::io::{ErrorKind, Read, Write};
use std::net::{Ipv4Addr, Shutdown, TcpListener, TcpStream};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

/// Exactly the routes desktop/server/app.py serves. Anything else is either a
/// static asset or a 404: this list is the whole proxy surface, so a typo in
/// the page cannot turn into an open forwarder. Kept in step with the same
/// list in desktop/ui/js/api.js and desktop/ui/dev-host.mjs; app.py's
/// self-test diffs all three against its own router and fails if they drift.
pub const SIDECAR_ROUTES: &[&str] = &[
    "/healthz",
    "/session",
    "/prompt",
    "/events",
    "/approve",
    "/cancel",
    "/models",
    "/checkpoints",
    "/restore",
    "/setup",
    "/idle",
    "/shop",
    "/shop/quants",
    "/downloads",
    "/downloads/events",
    "/downloads/cancel",
    "/downloads/dismiss",
    "/engine",
    "/engine/events",
    "/loop",
    "/loop/events",
    "/swarm",
    "/swarm/events",
    "/update",
    "/update/events",
];

/// The UI loads nothing from anywhere but itself and talks to nothing but its
/// own origin. This is belt and braces on top of the no-innerHTML rule in
/// js/dom.js, not a substitute for it.
///
/// `connect-src` carries one entry the Electron build does not need:
/// `http://ipc.localhost` is the scheme WebView2 hands Tauri's `invoke` to,
/// and without it the three shell calls in js/api.js are blocked by this very
/// header. It is not a widening of what the page can reach -- it is the same
/// bridge Electron's contextBridge provided, named in the one place a browser
/// insists on being told about it. `default-src 'none'` still forbids
/// everything else, including frames, so no other document is ever created in
/// this webview for the bridge to be injected into.
pub const CSP: &str = concat!(
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; ",
    "connect-src 'self' ipc: http://ipc.localhost; font-src 'self'; base-uri 'none'; ",
    "form-action 'none'; frame-ancestors 'none'"
);

const HOP_BY_HOP: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

/// A request head longer than this is not a Hearth request. The largest one
/// the UI sends is a shop search, and headers are a few hundred bytes.
const MAX_HEAD_BYTES: usize = 32 * 1024;

/// Matches MAX_BODY_BYTES in desktop/server/app.py. A body over this is
/// refused here rather than streamed upstream to be refused there.
const MAX_BODY_BYTES: u64 = 4 * 1024 * 1024;

fn mime_for(path: &str) -> &'static str {
    let ext = path.rsplit('.').next().unwrap_or("");
    match ext {
        "html" => "text/html; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "svg" => "image/svg+xml",
        "ico" => "image/x-icon",
        "png" => "image/png",
        "woff2" => "font/woff2",
        _ => "application/octet-stream",
    }
}

/// Looks a normalised path ("/index.html") up in the assets compiled into this
/// executable, and returns None for anything the binary does not carry.
///
/// That second half is the caller's job and main.rs does it: Tauri's own
/// resolver is written for single-page applications and falls back to
/// index.html for every unknown path, which would turn this server into one
/// that answers 200 for any URL. The self-test supplies a fixture.
pub type AssetLookup = Arc<dyn Fn(&str) -> Option<Vec<u8>> + Send + Sync>;

pub struct Origin {
    pub origin: String,
}

struct State {
    origin: String,
    sidecar_port: u16,
    assets: AssetLookup,
    routes: HashSet<&'static str>,
}

/// Start the origin. Returns once it is listening.
///
/// Port 0: the OS picks. A fixed port would let a stale process from a
/// previous run answer this one's requests.
pub fn start(assets: AssetLookup, sidecar_port: u16) -> std::io::Result<Origin> {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
    let port = listener.local_addr()?.port();
    let origin = format!("http://127.0.0.1:{}", port);

    let state = Arc::new(State {
        origin: origin.clone(),
        sidecar_port,
        assets,
        routes: SIDECAR_ROUTES.iter().copied().collect(),
    });

    thread::Builder::new()
        .name("hearth-origin".into())
        .spawn(move || {
            for incoming in listener.incoming() {
                let stream = match incoming {
                    Ok(stream) => stream,
                    Err(_) => continue,
                };
                let state = Arc::clone(&state);
                // One thread per connection. The page holds five or six at a
                // time, so this never becomes a pool worth managing, and a
                // thread that blocks forever on an SSE stream is exactly what
                // is wanted.
                let _ = thread::Builder::new()
                    .stack_size(256 * 1024)
                    .spawn(move || serve(stream, &state));
            }
        })?;

    Ok(Origin { origin })
}

// ------------------------------------------------------------------ parsing

struct Request {
    method: String,
    target: String,
    headers: Vec<(String, String)>,
}

impl Request {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }

    /// The path with any query string removed.
    fn path(&self) -> &str {
        match self.target.find('?') {
            Some(i) => &self.target[..i],
            None => &self.target,
        }
    }
}

/// Read bytes until the end of a request head. Returns the head text and any
/// bytes of the body that arrived in the same read.
fn read_head(stream: &mut TcpStream) -> Option<(String, Vec<u8>)> {
    let mut buf = Vec::with_capacity(2048);
    let mut chunk = [0u8; 2048];
    loop {
        let n = match stream.read(&mut chunk) {
            Ok(0) => return None,
            Ok(n) => n,
            Err(_) => return None,
        };
        buf.extend_from_slice(&chunk[..n]);
        if let Some(end) = find(&buf, b"\r\n\r\n") {
            let body = buf[end + 4..].to_vec();
            buf.truncate(end + 2);
            let head = String::from_utf8(buf).ok()?;
            return Some((head, body));
        }
        if buf.len() > MAX_HEAD_BYTES {
            return None;
        }
    }
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.len() > haystack.len() {
        return None;
    }
    (0..=haystack.len() - needle.len()).find(|&i| &haystack[i..i + needle.len()] == needle)
}

fn parse_request(head: &str) -> Option<Request> {
    let mut lines = head.split("\r\n");
    let mut start = lines.next()?.split(' ');
    let method = start.next()?.to_string();
    let target = start.next()?.to_string();
    let version = start.next()?;
    if !version.starts_with("HTTP/1.") || method.is_empty() || !target.starts_with('/') {
        return None;
    }
    let mut headers = Vec::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        // A header line that continues the previous one (obs-fold) is
        // deprecated and never sent by anything here; refusing it outright
        // means this parser and the sidecar's cannot disagree about a request.
        if line.starts_with(' ') || line.starts_with('\t') {
            return None;
        }
        let colon = line.find(':')?;
        let name = line[..colon].trim().to_string();
        let value = line[colon + 1..].trim().to_string();
        if name.is_empty() {
            return None;
        }
        headers.push((name, value));
    }
    Some(Request {
        method,
        target,
        headers,
    })
}

// ----------------------------------------------------------------- responses

fn send(stream: &mut TcpStream, status: &str, content_type: &str, extra: &[(&str, &str)], body: &[u8], head_only: bool) {
    let mut head = format!(
        "HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n",
        status,
        content_type,
        body.len()
    );
    for (name, value) in extra {
        head.push_str(name);
        head.push_str(": ");
        head.push_str(value);
        head.push_str("\r\n");
    }
    head.push_str("\r\n");
    let _ = stream.write_all(head.as_bytes());
    if !head_only {
        let _ = stream.write_all(body);
    }
    let _ = stream.flush();
}

fn refuse(stream: &mut TcpStream, status: &str, message: &str) {
    send(stream, status, "text/plain; charset=utf-8", &[], message.as_bytes(), false);
}

// -------------------------------------------------------------------- serve

fn serve(mut stream: TcpStream, state: &State) {
    let _ = stream.set_nodelay(true);

    // A connection that opens and says nothing must not hold a thread. Once a
    // request has been read the timeout is cleared, because an SSE stream is
    // a connection that says nothing for minutes on purpose.
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));

    let peer = match stream.peer_addr() {
        Ok(addr) => addr,
        Err(_) => return,
    };
    if !peer.ip().is_loopback() {
        refuse(&mut stream, "403 Forbidden", "loopback only");
        return;
    }

    let (head, body_head) = match read_head(&mut stream) {
        Some(parts) => parts,
        None => return,
    };
    let _ = stream.set_read_timeout(None);

    let request = match parse_request(&head) {
        Some(request) => request,
        None => {
            refuse(&mut stream, "400 Bad Request", "bad request");
            return;
        }
    };

    // A page in the user's browser cannot read a cross-origin response
    // without CORS, but it can still send one. Refusing a foreign Origin
    // outright means it cannot even do that, and costs nothing: our own
    // renderer sends either this origin or no Origin at all.
    if let Some(origin) = request.header("origin") {
        if origin != state.origin {
            refuse(&mut stream, "403 Forbidden", "forbidden origin");
            return;
        }
    }

    if state.routes.contains(request.path()) {
        // This proxy adds no authority: no token here means no request
        // upstream.
        if request.path() != "/healthz" && request.header("authorization").is_none() {
            send(
                &mut stream,
                "401 Unauthorized",
                "application/json",
                &[],
                br#"{"error": "missing Authorization header"}"#,
                false,
            );
            return;
        }
        proxy(stream, &request, body_head, state.sidecar_port);
        return;
    }

    serve_asset(&mut stream, &request, state);
}

fn serve_asset(stream: &mut TcpStream, request: &Request, state: &State) {
    let head_only = request.method.eq_ignore_ascii_case("HEAD");
    if !head_only && !request.method.eq_ignore_ascii_case("GET") {
        refuse(stream, "405 Method Not Allowed", "method not allowed");
        return;
    }
    let mut path = match percent_decode(request.path()) {
        Some(path) => path,
        None => {
            refuse(stream, "400 Bad Request", "bad request");
            return;
        }
    };
    if path == "/" {
        path = "/index.html".to_string();
    }

    match (state.assets)(&path) {
        Some(bytes) => send(
            stream,
            "200 OK",
            mime_for(&path),
            &[
                ("Cache-Control", "no-store"),
                ("Content-Security-Policy", CSP),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
            ],
            &bytes,
            head_only,
        ),
        None => refuse(stream, "404 Not Found", "not found"),
    }
}

fn percent_decode(input: &str) -> Option<String> {
    if !input.contains('%') {
        return Some(input.to_string());
    }
    let raw = input.as_bytes();
    let mut out = Vec::with_capacity(raw.len());
    let mut i = 0;
    while i < raw.len() {
        if raw[i] == b'%' {
            if i + 2 >= raw.len() {
                return None;
            }
            let hi = (raw[i + 1] as char).to_digit(16)?;
            let lo = (raw[i + 2] as char).to_digit(16)?;
            out.push((hi * 16 + lo) as u8);
            i += 3;
        } else {
            out.push(raw[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

// -------------------------------------------------------------------- proxy

fn proxy(mut client: TcpStream, request: &Request, body_head: Vec<u8>, sidecar_port: u16) {
    // A chunked request body is never sent by this UI, and forwarding one
    // means the two servers have to agree about framing. Refuse it instead.
    if request.header("transfer-encoding").is_some() {
        refuse(&mut client, "411 Length Required", "chunked request bodies are not forwarded");
        return;
    }
    let declared: u64 = match request.header("content-length") {
        Some(value) => match value.trim().parse() {
            Ok(n) => n,
            Err(_) => {
                refuse(&mut client, "400 Bad Request", "bad content-length");
                return;
            }
        },
        None => 0,
    };
    if declared > MAX_BODY_BYTES {
        refuse(&mut client, "413 Payload Too Large", "body too large");
        return;
    }

    let mut upstream = match TcpStream::connect((Ipv4Addr::LOCALHOST, sidecar_port)) {
        Ok(stream) => stream,
        Err(err) => {
            send(
                &mut client,
                "502 Bad Gateway",
                "application/json",
                &[],
                format!(r#"{{"error": "sidecar unreachable: {}"}}"#, err).as_bytes(),
                false,
            );
            return;
        }
    };
    let _ = upstream.set_nodelay(true);

    let mut head = format!("{} {} HTTP/1.1\r\n", request.method, request.target);
    // auth.py's DNS-rebinding defence checks this literal value.
    head.push_str(&format!("Host: 127.0.0.1:{}\r\n", sidecar_port));
    head.push_str("Connection: close\r\n");
    for (name, value) in &request.headers {
        let lower = name.to_ascii_lowercase();
        if HOP_BY_HOP.contains(&lower.as_str()) {
            continue;
        }
        if lower == "host" || lower == "origin" || lower == "referer" {
            continue;
        }
        head.push_str(name);
        head.push_str(": ");
        head.push_str(value);
        head.push_str("\r\n");
    }
    head.push_str("\r\n");
    if upstream.write_all(head.as_bytes()).is_err() {
        return;
    }

    // The body. Anything already read with the head goes first; the rest is
    // pulled off the client until the declared length is met. app.py reads
    // its full Content-Length unconditionally as the first thing it does, so
    // a short body here would hang it rather than be forgiven.
    let mut sent = body_head.len() as u64;
    if !body_head.is_empty() && upstream.write_all(&body_head).is_err() {
        return;
    }
    let mut chunk = [0u8; 16 * 1024];
    while sent < declared {
        let want = std::cmp::min(chunk.len() as u64, declared - sent) as usize;
        match client.read(&mut chunk[..want]) {
            Ok(0) => break,
            Ok(n) => {
                if upstream.write_all(&chunk[..n]).is_err() {
                    return;
                }
                sent += n as u64;
            }
            Err(_) => return,
        }
    }
    let _ = upstream.flush();

    // The sidecar answers with Connection: close because we asked, so the
    // response ends when the socket does and no framing has to be understood
    // here. Relay bytes verbatim, flushing every read: token streaming and
    // download progress are SSE and may not sit in a buffer waiting for more.
    loop {
        match upstream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                if client.write_all(&chunk[..n]).is_err() || client.flush().is_err() {
                    // The renderer aborted an SSE fetch (reload, reconnect).
                    // Dropping `upstream` here tears the upstream request
                    // down too, or the sidecar's bounded SSE slots leak.
                    break;
                }
            }
            Err(ref err) if err.kind() == ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
    let _ = client.shutdown(Shutdown::Both);
}

// ---------------------------------------------------------------- self-test

/// `hearth.exe --self-test` runs this. It exercises the parsing and the
/// refusals against a real socket, because every one of them is a decision
/// made from bytes off a wire.
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn route_list_has_no_duplicates() {
        let unique: HashSet<&&str> = SIDECAR_ROUTES.iter().collect();
        assert_eq!(unique.len(), SIDECAR_ROUTES.len());
    }

    #[test]
    fn percent_decoding_refuses_rubbish() {
        assert_eq!(percent_decode("/js/app.js").unwrap(), "/js/app.js");
        assert_eq!(percent_decode("/a%2Fb").unwrap(), "/a/b");
        assert!(percent_decode("/a%zz").is_none());
        assert!(percent_decode("/a%2").is_none());
    }

    #[test]
    fn request_parsing_refuses_folded_headers() {
        assert!(parse_request("GET / HTTP/1.1\r\nHost: x\r\n obs-fold").is_none());
        assert!(parse_request("GET / HTTP/1.1\r\nHost: x").is_some());
        assert!(parse_request("GET http://evil/ HTTP/1.1").is_none());
    }
}
