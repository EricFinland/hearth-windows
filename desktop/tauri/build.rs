//! Two jobs, in this order.
//!
//! 1. Assemble the UI tree that gets COMPILED INTO the executable. Tauri
//!    embeds `frontendDist` in the binary at build time, which is the reason
//!    this shell has no equivalent of Electron's asar-integrity problem: there
//!    is no application code on disk for anything to swap. desktop/ui/ cannot
//!    be pointed at directly because it also holds dev-host.mjs and the
//!    xss-check harness, neither of which belongs in an installed
//!    application, and `frontendDist` has no exclude list. So the tree is
//!    copied here, minus those, exactly as scripts/build_windows.py excludes
//!    them from the payload.
//!
//! 2. Run tauri-build, which reads tauri.conf.json, embeds the assets and the
//!    capability set, and writes the Windows resource block. It is also told
//!    the names of the three commands the shell answers, which is what makes
//!    `allow-handshake`, `allow-pick-folder` and `allow-install-update` exist
//!    as permissions for capabilities/main.json to grant. Without that list
//!    the page reaches none of them: it is served over http on loopback, and
//!    Tauri treats a page that is not on its own protocol as a remote origin,
//!    which is allowed exactly what a capability spells out and nothing more.

use std::fs;
use std::path::{Path, PathBuf};

/// Mirrors EXCLUDE_NAMES / EXCLUDE_PREFIXES in scripts/build_windows.py.
fn excluded(name: &str) -> bool {
    name == "__pycache__" || name == "dev-host.mjs" || name.starts_with("xss-check.")
}

fn copy_tree(src: &Path, dest: &Path) {
    fs::create_dir_all(dest).expect("create the embedded UI directory");
    for entry in fs::read_dir(src).expect("read desktop/ui") {
        let entry = entry.expect("read a desktop/ui entry");
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if excluded(&name) {
            continue;
        }
        let from = entry.path();
        let to = dest.join(entry.file_name());
        if from.is_dir() {
            copy_tree(&from, &to);
        } else {
            fs::copy(&from, &to).expect("copy a UI file");
            println!("cargo:rerun-if-changed={}", from.display());
        }
    }
}

fn main() {
    let crate_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let ui_src = crate_dir.join("..").join("ui");
    // Kept in step with `frontendDist` in tauri.conf.json.
    let ui_embed = crate_dir.join("..").join("..").join("build").join("ui-embed");

    println!("cargo:rerun-if-changed={}", ui_src.display());
    let _ = fs::remove_dir_all(&ui_embed);
    copy_tree(&ui_src, &ui_embed);

    // Kept in step with generate_handler! in src/main.rs. A command added
    // there and not here is unreachable from the page; a command named here
    // and not there fails the build.
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new()
                .commands(&["handshake", "pick_folder", "install_update"]),
        ),
    )
    .expect("tauri-build could not assemble the application");
}
