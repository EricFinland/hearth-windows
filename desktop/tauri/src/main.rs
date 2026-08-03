#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
//! Hearth's desktop shell.
//!
//! What it is responsible for, and nothing else:
//!
//!   1. Start the Python sidecar with the interpreter shipped beside it, and
//!      read the one-line handshake off its stdout.
//!   2. Serve desktop/ui/ on a loopback origin that also forwards the
//!      sidecar's routes, because the sidecar refuses a page served from
//!      anywhere else (see origin.rs for the full reason).
//!   3. Hand {origin, token} to the renderer over IPC, and nothing else.
//!   4. Make sure that when this process stops existing, so does the sidecar,
//!      and therefore so does llama-server and the several gigabytes of VRAM
//!      it may be holding (see sidecar.rs).
//!
//! Everything a user actually sees lives in desktop/ui/, which has no npm
//! dependencies and no build step, and everything Hearth actually does lives
//! in the Python sidecar. This file is deliberately thin. It replaced an
//! Electron shell that did exactly the same four things and cost 364 MB to do
//! them; the reason a swap that large was cheap is that the numbered list
//! above is the whole of what the host is for.
//!
//! Layout
//! ------
//! Packaged, the payload sits beside the executable:
//!
//!   Hearth.exe                 this program, with desktop/ui/ compiled in
//!   python/                    the vendored CPython (scripts/vendor_python.py)
//!   hearth/agent/              the agent modules, unchanged
//!   hearth/desktop/server/
//!   hearth/vendor/llama/llama-server.exe
//!
//! That layout is not arbitrary. agent/hearth_llama.app_root() resolves to the
//! parent of the agent directory and then looks for vendor/llama beneath it,
//! so putting the payload together this way makes find_server() locate the
//! bundled engine with no packaging-specific code in the agent at all.
//!
//! Unpackaged (cargo run), the payload is the checkout itself, so the shell
//! can be run and debugged against the working tree before anything is built.

mod origin;
mod sidecar;
mod update;

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use serde::Serialize;
use tauri::Manager;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

const WINDOW_LABEL: &str = "main";

/// A string the shipped executable carries so a build gate can read back what
/// was actually compiled, rather than what the manifest asked for.
///
/// Tauri's inspector is a compile-time feature, not a runtime flag, which is a
/// genuinely better answer than Electron's EnableNodeCliInspectArguments fuse:
/// a release binary built without it has no inspector to open by any key
/// combination, environment variable or automation, because the code is not
/// there. But "was it built without it" is a question about the bytes, and
/// asking Cargo.toml is asking the request rather than the result. So the
/// answer is written into the binary here and read back out of the shipped
/// file by scripts/verify_binary.py. Built the wrong way, the wrong marker
/// ships and the build fails.
///
/// It is passed to the sidecar in HEARTH_SHELL_BUILD rather than merely
/// declared, because a constant nothing reads is a constant the linker is
/// entitled to discard, and this one did get discarded: `#[used]` on an
/// anchoring static was not enough under fat LTO. A string that is actually
/// put in a child process's environment survives, and the sidecar gets to
/// know what started it, which it did not before.
#[cfg(feature = "devtools")]
pub const BUILD_MARKER: &str = "hearth-build-marker:devtools-enabled";
#[cfg(not(feature = "devtools"))]
pub const BUILD_MARKER: &str = "hearth-build-marker:devtools-disabled";

/// Environment variables the WebView2 runtime reads before this program gets a
/// say, and what each one can do to it.
///
/// This is the closest thing Tauri has to the problem Electron's fuses exist
/// for. There is no Node runtime to turn on, so ELECTRON_RUN_AS_NODE and
/// NODE_OPTIONS have no analogue at all -- but WebView2 is a Chromium, and
/// Chromium takes its orders from the environment:
///
///   WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
///       appended to the browser process command line, which accepts
///       --remote-debugging-port (an unauthenticated DevTools protocol
///       endpoint on loopback, which would hand any local process the page,
///       and with it the bearer token) and --disable-web-security (which
///       would retire the Content-Security-Policy and the same-origin rule
///       that origin.rs exists to enforce).
///   WEBVIEW2_BROWSER_EXECUTABLE_FOLDER
///       loads the browser binaries from a folder of someone else's
///       choosing: arbitrary code, in this process, before any of this runs.
///   WEBVIEW2_USER_DATA_FOLDER
///       relocates the profile, which is where a cookie or a cache entry
///       would land.
///   WEBVIEW2_RELEASE_CHANNEL_PREFERENCE, WEBVIEW2_CHANNEL_SEARCH_KIND
///       select a different, possibly pre-release runtime.
///
/// Anyone who can set these can also usually do worse. That was equally true
/// of ELECTRON_RUN_AS_NODE, and Hearth fused it off regardless, because a
/// poisoned shortcut or an Image File Execution Options key is a much smaller
/// ask than code execution and this closes it for the price of five removals.
/// Cleared here, at the top of main, before the runtime is loaded and before
/// any webview exists.
const WEBVIEW2_ENV: &[&str] = &[
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
    "WEBVIEW2_USER_DATA_FOLDER",
    "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE",
    "WEBVIEW2_CHANNEL_SEARCH_KIND",
];

fn disown_webview2_environment() {
    for name in WEBVIEW2_ENV {
        std::env::remove_var(name);
    }
}

/// The bridge the renderer gets, and the whole of it.
///
/// Four names. No filesystem, no process APIs, no Rust internals -- only these
/// cross, and each of the three calls is a request the shell validates before
/// answering. `withGlobalTauri` is false and the capability in
/// capabilities/main.json grants the page no plugin permission at all, so
/// `__TAURI_INTERNALS__.invoke` reaches these three commands and nothing else:
/// every `plugin:` command compiled into this binary, including the dialog and
/// filesystem ones this shell itself uses, is refused by the access-control
/// list before it runs.
///
/// Why the token comes through here rather than over HTTP: dev-host.mjs serves
/// the sidecar's bearer token at GET /__hearth/handshake to any process on
/// loopback, which its own comments give as the reason it is not shippable.
/// This channel is not reachable from outside this application, so the token
/// goes from the sidecar's stdout, to this process, to this renderer, and
/// nowhere else.
///
/// `shell` names the host so desktop/ui/ can stay shell-agnostic. The Electron
/// build exposed the same four names with the same shapes and nothing in
/// desktop/ui/ had to know which one it was talking to; that is what made this
/// port a change to one directory.
const BRIDGE: &str = r#"
(function () {
  "use strict";
  function call(command) {
    var internals = window.__TAURI_INTERNALS__;
    if (!internals || typeof internals.invoke !== "function") {
      return Promise.reject(new Error("no shell bridge"));
    }
    return internals.invoke(command, {});
  }
  Object.defineProperty(window, "hearth", {
    value: Object.freeze({
      shell: "tauri",
      handshake: function () { return call("handshake"); },
      pickFolder: function () { return call("pick_folder"); },
      installUpdate: function () { return call("install_update"); }
    }),
    writable: false,
    configurable: false,
    enumerable: true
  });
})();
"#;

// ------------------------------------------------------------------- state

struct Shell {
    sidecar: sidecar::Sidecar,
    handshake: sidecar::Handshake,
    origin: String,
    default_workspace: String,
}

static SHELL: OnceLock<Mutex<Option<Shell>>> = OnceLock::new();
static QUITTING: AtomicBool = AtomicBool::new(false);
/// Bounded: a sidecar that spews for an hour must not grow this process
/// without limit. Kept only so a startup failure can be shown to the user;
/// never written to disk, and the handshake line never reaches it.
static LOG: OnceLock<Mutex<Vec<String>>> = OnceLock::new();

fn shell_slot() -> &'static Mutex<Option<Shell>> {
    SHELL.get_or_init(|| Mutex::new(None))
}

fn log_slot() -> &'static Mutex<Vec<String>> {
    LOG.get_or_init(|| Mutex::new(Vec::new()))
}

fn log(line: String) {
    if let Ok(mut lines) = log_slot().lock() {
        lines.push(line);
        if lines.len() > 400 {
            lines.remove(0);
        }
    }
}

fn tail() -> String {
    log_slot()
        .lock()
        .map(|lines| {
            let start = lines.len().saturating_sub(12);
            lines[start..].join("\n")
        })
        .unwrap_or_default()
}

// ------------------------------------------------------------------ layout

struct Layout {
    python: PathBuf,
    server_dir: PathBuf,
}

/// Packaged, the vendored interpreter is the only candidate. There is
/// deliberately no fallback to a Python on PATH, because a user's own Python
/// is a different version with different site-packages and "it worked on the
/// machine that had Python" is exactly the failure this whole exercise exists
/// to remove. Unpackaged, the vendored interpreter is still preferred so
/// development exercises what ships.
fn resolve_layout(app: &tauri::AppHandle) -> Result<Layout, String> {
    let resources = app
        .path()
        .resource_dir()
        .map_err(|err| format!("Hearth cannot find its own resources: {}", err))?;
    let packaged = resources.join("hearth").join("agent").is_dir();
    if packaged {
        return Ok(Layout {
            python: resources.join("python").join("python.exe"),
            server_dir: resources.join("hearth").join("desktop").join("server"),
        });
    }

    // The checkout: this file is desktop/tauri/src/main.rs.
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..");
    let vendored = repo.join("vendor").join("python").join("python.exe");
    let python = if vendored.is_file() {
        vendored
    } else {
        PathBuf::from(std::env::var("HEARTH_PYTHON").unwrap_or_else(|_| "python".into()))
    };
    Ok(Layout {
        python,
        server_dir: repo.join("desktop").join("server"),
    })
}

/// Where a session starts if the user has not chosen anywhere. A folder in the
/// user's home directory, created on demand. It is only a default: the
/// workspace is whatever the user types or picks, and the sidecar's
/// containment boundary is what enforces it, not this.
fn default_workspace() -> String {
    let home = std::env::var("USERPROFILE").unwrap_or_default();
    if home.is_empty() {
        return String::new();
    }
    let dir = PathBuf::from(&home).join("Hearth");
    match std::fs::create_dir_all(&dir) {
        Ok(()) => dir.to_string_lossy().into_owned(),
        Err(_) => home,
    }
}

// --------------------------------------------------------------- IPC surface

#[derive(Serialize)]
struct HandshakeReply {
    origin: String,
    token: String,
    #[serde(rename = "defaultWorkspace")]
    default_workspace: String,
}

/// True when this call came from our own window, showing our own origin.
///
/// Tauri's access-control list already refuses IPC from any webview that is
/// not this one at a URL that is not ours, so this is the second lock rather
/// than the first. The thing behind the door is the sidecar's bearer token and
/// it costs one comparison.
fn from_our_renderer(webview: &tauri::Webview, origin: &str) -> bool {
    if webview.label() != WINDOW_LABEL {
        return false;
    }
    match webview.url() {
        Ok(url) => {
            let url = url.as_str();
            url == origin || url.starts_with(&format!("{}/", origin))
        }
        Err(_) => false,
    }
}

/// Run `body` with the shell state, but only for our own renderer.
fn with_shell<T, F>(webview: &tauri::Webview, body: F) -> Result<T, String>
where
    F: FnOnce(&Shell) -> Result<T, String>,
{
    let guard = shell_slot().lock().map_err(|_| "refused".to_string())?;
    let shell = guard.as_ref().ok_or("Hearth is still starting up.")?;
    if !from_our_renderer(webview, &shell.origin) {
        return Err("refused".into());
    }
    body(shell)
}

#[tauri::command]
async fn handshake(webview: tauri::Webview) -> Result<HandshakeReply, String> {
    with_shell(&webview, |shell| {
        Ok(HandshakeReply {
            origin: shell.origin.clone(),
            token: shell.handshake.token.clone(),
            default_workspace: shell.default_workspace.clone(),
        })
    })
}

#[derive(Serialize)]
struct FolderReply {
    #[serde(skip_serializing_if = "Option::is_none")]
    path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

#[tauri::command]
async fn pick_folder(app: tauri::AppHandle, webview: tauri::Webview) -> Result<FolderReply, String> {
    with_shell(&webview, |_| Ok(()))?;
    let chosen = app
        .dialog()
        .file()
        .set_title("Choose a workspace folder for Hearth")
        .blocking_pick_folder();
    Ok(match chosen {
        Some(path) => FolderReply {
            path: Some(path.to_string()),
            error: None,
        },
        None => FolderReply {
            path: None,
            error: Some("No folder chosen.".into()),
        },
    })
}

#[derive(Serialize, Default)]
struct UpdateReply {
    #[serde(skip_serializing_if = "Option::is_none")]
    started: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    cancelled: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

fn update_error(message: String) -> UpdateReply {
    UpdateReply {
        error: Some(message),
        ..Default::default()
    }
}

#[tauri::command]
async fn install_update(
    app: tauri::AppHandle,
    webview: tauri::Webview,
) -> Result<UpdateReply, String> {
    let (port, token) = with_shell(&webview, |shell| {
        Ok((shell.handshake.port, shell.handshake.token.clone()))
    })?;

    let snapshot = match update::sidecar_get(port, &token, "/update") {
        Ok(value) => value,
        Err(err) => {
            return Ok(update_error(format!(
                "Hearth could not read its own update state: {}",
                err
            )))
        }
    };
    let running = app.package_info().version.to_string();
    let staged = match update::verify(&snapshot, &running) {
        Ok(staged) => staged,
        Err(message) => return Ok(update_error(message)),
    };

    // Everything the user needs in order to answer, in front of them: which
    // version, who signed it, how big it is, and the digest this process just
    // recomputed from the bytes on disk.
    let message = format!(
        "Install Hearth {}?\n\n\
         Hearth {} will close and the installer will run.\n\n\
         This installer was signed by {} and its contents match that signature.\n\n\
         {:.1} MB\nSHA-256: {}",
        staged.version,
        running,
        staged.signed_by,
        staged.size_bytes as f64 / 1e6,
        staged.sha256
    );
    let answer = app
        .dialog()
        .message(message)
        .title(format!("Install Hearth {}", staged.version))
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Install and restart".into(),
            "Not now".into(),
        ))
        .blocking_show();
    if !answer {
        return Ok(UpdateReply {
            cancelled: Some(true),
            ..Default::default()
        });
    }

    if let Err(message) = update::launch(&staged) {
        return Ok(update_error(message));
    }

    // Quit immediately rather than waiting: the sidecar (and with it
    // llama-server, and with it several gigabytes of VRAM) has to be gone
    // before the installer can replace the files it is running from.
    let version = staged.version.clone();
    let handle = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(200));
        shutdown();
        handle.exit(0);
    });
    Ok(UpdateReply {
        started: Some(true),
        version: Some(version),
        ..Default::default()
    })
}

// -------------------------------------------------------------------- boot

fn boot(app: &tauri::AppHandle) -> Result<(), String> {
    let layout = resolve_layout(app)?;
    if !layout.python.is_file() && layout.python.is_absolute() {
        return Err(format!(
            "The bundled Python interpreter is not where it should be:\n\n{}\n\n\
             Reinstalling Hearth should fix this.",
            layout.python.display()
        ));
    }

    let mut env = HashMap::new();
    // The running version, compiled into this executable by tauri-build
    // rather than read from a file in the install directory. See
    // agent/hearth_update.current_version.
    env.insert(
        "HEARTH_APP_VERSION".to_string(),
        app.package_info().version.to_string(),
    );
    // See BUILD_MARKER. This is the reference that keeps it in the binary.
    env.insert("HEARTH_SHELL_BUILD".to_string(), BUILD_MARKER.to_string());

    let on_exit_app = app.clone();
    let (side, handshake) = sidecar::start(
        &layout.python,
        &layout.server_dir,
        env,
        Arc::new(log),
        Arc::new(move |code| {
            if QUITTING.load(Ordering::SeqCst) {
                return;
            }
            let detail = format!(
                "The Hearth service exited{}.\n\n{}",
                match code {
                    Some(code) => format!(" with code {}", code),
                    None => String::new(),
                },
                tail()
            );
            fatal(&on_exit_app, "Hearth stopped unexpectedly", &detail);
        }),
    )
    .map_err(|err| {
        format!(
            "{}\n\nInterpreter: {}\nService: {}\n\n{}",
            err,
            layout.python.display(),
            layout.server_dir.display(),
            tail()
        )
    })?;

    // The UI is compiled into this executable; the resolver reads it out of
    // the binary's own read-only data, never off disk.
    //
    // The set of keys is taken first because Tauri's get_asset() is written
    // for single-page applications and falls back, in order, to `<path>.html`,
    // `<path>/index.html` and finally to index.html itself. An origin built
    // on that alone answers 200 for every URL that has ever been typed, which
    // is not what the Electron shell did and not what a route allowlist is
    // worth anything next to: it also decodes percent-escapes a second time,
    // after origin.rs has already decoded them once, so a doubly-encoded
    // `..%252f` would arrive at the lookup as `../`. Asking only about paths
    // the binary demonstrably carries makes both of those go away, and a 404
    // stay a 404. Measured: without this, GET /not-a-route returned index.html
    // with status 200.
    let resolver = app.asset_resolver();
    let known: HashSet<String> = resolver
        .iter()
        .map(|(key, _)| format!("/{}", key.trim_start_matches('/')))
        .collect();
    let assets: origin::AssetLookup = Arc::new(move |path: &str| {
        if !known.contains(path) {
            return None;
        }
        resolver.get(path.to_string()).map(|asset| asset.bytes)
    });

    let served = origin::start(assets, handshake.port)
        .map_err(|err| format!("Hearth could not open its local origin: {}", err))?;

    let origin_url = served.origin.clone();
    if let Ok(mut slot) = shell_slot().lock() {
        *slot = Some(Shell {
            sidecar: side,
            handshake,
            origin: origin_url.clone(),
            default_workspace: default_workspace(),
        });
    }

    create_window(app, &origin_url)
}

fn create_window(app: &tauri::AppHandle, origin: &str) -> Result<(), String> {
    let url = tauri::Url::parse(&format!("{}/", origin))
        .map_err(|err| format!("Hearth built an origin it cannot parse: {}", err))?;

    // The renderer is a local application UI, not a browser. It may not
    // navigate anywhere but its own origin. Links that want a browser get the
    // user's real browser instead.
    let allowed = origin.to_string();
    tauri::WebviewWindowBuilder::new(app, WINDOW_LABEL, tauri::WebviewUrl::External(url))
        .title("Hearth")
        .inner_size(1280.0, 860.0)
        .min_inner_size(900.0, 600.0)
        .background_color(tauri::webview::Color(0x14, 0x10, 0x0d, 0xff))
        .initialization_script(BRIDGE)
        .on_navigation(move |url| {
            let text = url.as_str();
            if text == allowed || text.starts_with(&format!("{}/", allowed)) {
                return true;
            }
            if url.scheme() == "http" || url.scheme() == "https" {
                open_externally(text);
            }
            false
        })
        .build()
        .map_err(|err| format!("Hearth could not open its window: {}", err))?;
    Ok(())
}

/// Hand a URL to the user's real browser. ShellExecuteW rather than a plugin,
/// so there is no `plugin:opener|open` command compiled into this binary for a
/// page to try to reach.
fn open_externally(url: &str) {
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    let mut wide: Vec<u16> = url.encode_utf16().collect();
    wide.push(0);
    let mut verb: Vec<u16> = "open".encode_utf16().collect();
    verb.push(0);
    unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            verb.as_ptr(),
            wide.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL,
        );
    }
}

fn fatal(app: &tauri::AppHandle, title: &str, detail: &str) {
    app.dialog()
        .message(detail)
        .title(title)
        .kind(MessageDialogKind::Error)
        .blocking_show();
    shutdown();
    app.exit(1);
}

/// Stop the sidecar and wait for it to be gone. Idempotent.
fn shutdown() {
    if QUITTING.swap(true, Ordering::SeqCst) {
        return;
    }
    if let Ok(mut slot) = shell_slot().lock() {
        if let Some(shell) = slot.as_ref() {
            shell.sidecar.stop();
        }
        // Dropping the Shell drops the job handle, which closes the job, which
        // kills anything the polite stop above did not.
        slot.take();
    }
}

// --------------------------------------------------------- single instance

/// A second launch must not start a second sidecar: two of them would each
/// hold their own llama-server and their own copy of the model in memory.
/// Returns false when another Hearth already owns the name.
fn claim_single_instance() -> bool {
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS};
    use windows_sys::Win32::System::Threading::CreateMutexW;
    // Local\ rather than Global\: one Hearth per logged-in user, which is what
    // a per-user install means. A Global\ name would let one user's Hearth
    // stop another's from starting.
    let mut name: Vec<u16> = "Local\\com.hearthlocal.hearth.instance"
        .encode_utf16()
        .collect();
    name.push(0);
    unsafe {
        let handle = CreateMutexW(std::ptr::null(), 1, name.as_ptr());
        if handle.is_null() {
            return true; // cannot tell; starting is better than refusing
        }
        // The handle is deliberately leaked: the mutex must live exactly as
        // long as this process, and the kernel releases it when we exit.
        GetLastError() != ERROR_ALREADY_EXISTS
    }
}

/// Bring the Hearth that is already running to the front.
fn focus_existing() {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        FindWindowW, IsIconic, SetForegroundWindow, ShowWindow, SW_RESTORE,
    };
    let mut title: Vec<u16> = "Hearth".encode_utf16().collect();
    title.push(0);
    unsafe {
        let window = FindWindowW(std::ptr::null(), title.as_ptr());
        if window.is_null() {
            return;
        }
        if IsIconic(window) != 0 {
            ShowWindow(window, SW_RESTORE);
        }
        SetForegroundWindow(window);
    }
}

// -------------------------------------------------------------------- main

fn main() {
    // Before the WebView2 runtime is loaded, and therefore before anything
    // it was told from outside can take effect. See WEBVIEW2_ENV.
    disown_webview2_environment();

    if !claim_single_instance() {
        focus_existing();
        return;
    }

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            handshake,
            pick_folder,
            install_update
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            if let Err(message) = boot(&handle) {
                fatal(&handle, "Hearth could not start", &message);
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Hearth could not build its application context");

    app.run(|_handle, event| {
        if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
            shutdown();
        }
    });
}
