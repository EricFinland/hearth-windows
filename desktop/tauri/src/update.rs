//! Running a staged update, and the four refusals in front of it.
//!
//! This is the only code in Hearth that executes a downloaded file, and it is
//! here rather than in the sidecar on purpose: agent/hearth_update.py decides
//! whether bytes are trustworthy, and a module that makes that decision must
//! not also be able to act on it. The sidecar fetches a release manifest,
//! verifies its Ed25519 signature against the key pinned in release/trust.json,
//! refuses downgrades and expired manifests, downloads the installer, checks
//! its SHA-256 against the SIGNED manifest, and stops. Then this runs.
//!
//! Three checks happen HERE, and none of them is ceremony:
//!
//!   1. The path must be inside the staging directory, which this process
//!      derives itself from the same rule agent/hearth_paths.py uses, rather
//!      than believing the path it was handed. A response that could name any
//!      file would turn "install the update" into "run that".
//!   2. The size and SHA-256 are recomputed from the file on disk. The sidecar
//!      already verified it, but a verified installer then SITS on disk for as
//!      long as the user takes to click, and every other process running as
//!      that user can write to it in that window. Hashing immediately before
//!      the spawn narrows the window to the width of one syscall.
//!   3. The version must be strictly greater than the running one. Under
//!      Electron that value came out of the asar, which a fuse made
//!      tamper-evident. Here it is compiled into the executable by
//!      tauri-build, which is a stronger answer for the same reason: there is
//!      no separate archive next to the binary to edit.
//!
//! Then the user is shown what will run, and only a yes proceeds.

use std::io::{Read, Write};
use std::net::{Ipv4Addr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use sha2::{Digest, Sha256};
use std::os::windows::process::CommandExt;

/// Detached, so the installer outlives this process: its first job is to
/// replace the executable that started it.
const DETACHED_PROCESS: u32 = 0x0000_0008;
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

/// An update snapshot is a few hundred bytes. Anything past this is a
/// malfunction, not a snapshot.
const MAX_SNAPSHOT_BYTES: usize = 64 * 1024;

#[derive(Debug)]
pub struct Staged {
    pub path: PathBuf,
    pub version: String,
    pub size_bytes: u64,
    pub sha256: String,
    pub signed_by: String,
}

/// Where verified installers are staged, derived here rather than trusted.
///
/// Mirrors agent/hearth_paths.data_dir(): HEARTH_DATA_DIR when set, else
/// %LOCALAPPDATA%\Hearth on Windows. Deriving it independently is the whole
/// point -- a containment check that asks the thing being checked where the
/// boundary is checks nothing.
pub fn staging_root() -> PathBuf {
    if let Ok(dir) = std::env::var("HEARTH_UPDATE_DIR") {
        return PathBuf::from(dir).join("staged");
    }
    let data = match std::env::var("HEARTH_DATA_DIR") {
        Ok(dir) => PathBuf::from(dir),
        Err(_) => {
            let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
            PathBuf::from(local).join("Hearth")
        }
    };
    data.join("update").join("staged")
}

/// `[major, minor, patch]` or None. Same strictness as
/// hearth_update.parse_version: exactly three dotted decimals, because the
/// comparison downgrade protection rests on has to be a total order with no
/// surprises.
pub fn parse_version(text: &str) -> Option<[u32; 3]> {
    let parts: Vec<&str> = text.trim().split('.').collect();
    if parts.len() != 3 {
        return None;
    }
    let mut out = [0u32; 3];
    for (i, part) in parts.iter().enumerate() {
        if part.is_empty() || part.len() > 6 || !part.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        out[i] = part.parse().ok()?;
    }
    Some(out)
}

pub fn newer_than(a: [u32; 3], b: [u32; 3]) -> bool {
    a > b
}

/// True when `target` is `root` or sits underneath it. Both are compared after
/// canonicalisation, so a junction, a `..`, a short 8.3 name or a differently
/// cased drive letter cannot smuggle a path past this.
fn contained(target: &Path, root: &Path) -> bool {
    let (target, root) = match (target.canonicalize(), root.canonicalize()) {
        (Ok(t), Ok(r)) => (t, r),
        // A staging root that does not exist contains nothing.
        _ => return false,
    };
    let target = target.to_string_lossy().to_lowercase();
    let root = root.to_string_lossy().to_lowercase();
    target == root || target.starts_with(&(root + "\\"))
}

/// GET a sidecar route from the shell process, with the bearer token.
pub fn sidecar_get(port: u16, token: &str, path: &str) -> Result<serde_json::Value, String> {
    let mut stream = TcpStream::connect((Ipv4Addr::LOCALHOST, port))
        .map_err(|err| format!("the sidecar is not answering: {}", err))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(15)));
    let request = format!(
        "GET {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n\
         Accept: application/json\r\nConnection: close\r\n\r\n",
        path, port, token
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|err| err.to_string())?;

    let mut raw = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&chunk[..n]);
                if raw.len() > MAX_SNAPSHOT_BYTES {
                    return Err("the sidecar's reply is implausibly large".into());
                }
            }
            Err(err) => return Err(err.to_string()),
        }
    }
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or("the sidecar sent no response head")?;
    let head = String::from_utf8_lossy(&raw[..split]);
    let status = head
        .lines()
        .next()
        .and_then(|line| line.split(' ').nth(1))
        .unwrap_or("");
    if status != "200" {
        return Err(format!("the sidecar answered {} for {}", status, path));
    }
    serde_json::from_slice(&raw[split + 4..]).map_err(|err| format!("unreadable reply: {}", err))
}

/// Read the sidecar's verified receipt and check it against this process's own
/// rules. Returns the staged installer, or a sentence saying why not.
pub fn verify(snapshot: &serde_json::Value, running_version: &str) -> Result<Staged, String> {
    let staged = snapshot
        .get("staged")
        .filter(|value| !value.is_null())
        .ok_or("There is no verified update ready to install.")?;

    let path = staged
        .get("path")
        .and_then(|v| v.as_str())
        .ok_or("There is no verified update ready to install.")?;
    let target = PathBuf::from(path);
    if !contained(&target, &staging_root()) {
        return Err("Refusing to run an installer from outside Hearth's update folder.".into());
    }
    let is_exe = target
        .extension()
        .map(|ext| ext.eq_ignore_ascii_case("exe"))
        .unwrap_or(false);
    if !is_exe || !target.is_file() {
        return Err("The staged installer is not where it should be. Check for updates again.".into());
    }

    let version = staged
        .get("version")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let installed =
        parse_version(running_version).ok_or("Refusing an update whose version cannot be read.")?;
    let offered = parse_version(&version).ok_or("Refusing an update whose version cannot be read.")?;
    if !newer_than(offered, installed) {
        return Err(format!(
            "Refusing to \"update\" from {} to {}. That is a downgrade, not an update.",
            running_version, version
        ));
    }

    let expected_size = staged.get("size_bytes").and_then(|v| v.as_u64()).unwrap_or(0);
    let expected_sha = staged.get("sha256").and_then(|v| v.as_str()).unwrap_or("");

    let size = std::fs::metadata(&target)
        .map_err(|err| format!("Hearth could not read the staged installer: {}", err))?
        .len();
    let digest = sha256_of(&target)
        .map_err(|err| format!("Hearth could not read the staged installer: {}", err))?;
    if size != expected_size || digest != expected_sha {
        return Err("The staged installer changed on disk after it was verified, so it will \
                    not be run. Nothing has been installed. Check for updates again."
            .into());
    }

    Ok(Staged {
        path: target,
        version,
        size_bytes: size,
        sha256: digest,
        signed_by: staged
            .get("signed_by")
            .and_then(|v| v.as_str())
            .unwrap_or("the release key")
            .to_string(),
    })
}

fn sha256_of(path: &Path) -> std::io::Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut chunk = vec![0u8; 1 << 20];
    loop {
        let n = file.read(&mut chunk)?;
        if n == 0 {
            break;
        }
        hasher.update(&chunk[..n]);
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|b| format!("{:02x}", b))
        .collect())
}

/// Run the verified installer. `/S` is the NSIS silent switch: the user has
/// already been asked, with the version and the hash in front of them, and
/// asking twice trains people to click through.
pub fn launch(staged: &Staged) -> Result<(), String> {
    Command::new(&staged.path)
        .arg("/S")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        .spawn()
        .map(|_| ())
        .map_err(|err| format!("The installer would not start: {}", err))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_parsing_is_strict() {
        assert_eq!(parse_version("1.2.3"), Some([1, 2, 3]));
        assert_eq!(parse_version(" 0.1.0 "), Some([0, 1, 0]));
        assert!(parse_version("1.2").is_none());
        assert!(parse_version("1.2.3.4").is_none());
        assert!(parse_version("1.2.x").is_none());
        assert!(parse_version("1.2.-3").is_none());
        assert!(parse_version("1.2.3-beta").is_none());
    }

    #[test]
    fn ordering_refuses_downgrades_and_reinstalls() {
        assert!(newer_than([0, 2, 0], [0, 1, 9]));
        assert!(newer_than([1, 0, 0], [0, 99, 99]));
        assert!(!newer_than([0, 1, 0], [0, 1, 0]));
        assert!(!newer_than([0, 0, 9], [0, 1, 0]));
    }

    #[test]
    fn containment_refuses_a_missing_root() {
        assert!(!contained(
            Path::new(r"C:\Windows\System32\calc.exe"),
            Path::new(r"C:\no-such-hearth-staging-root\staged")
        ));
    }

    #[test]
    fn containment_refuses_a_sibling_of_the_root() {
        let temp = std::env::temp_dir().join("hearth-contain-test");
        let root = temp.join("staged");
        let outside = temp.join("elsewhere");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        let inside_file = root.join("setup.exe");
        let outside_file = outside.join("setup.exe");
        std::fs::write(&inside_file, b"x").unwrap();
        std::fs::write(&outside_file, b"x").unwrap();
        assert!(contained(&inside_file, &root));
        assert!(!contained(&outside_file, &root));
        // ..\ back out of the root is normalised away by canonicalisation.
        assert!(!contained(&root.join("..").join("elsewhere").join("setup.exe"), &root));
        let _ = std::fs::remove_dir_all(&temp);
    }
}
