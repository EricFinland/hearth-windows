//! Starting, reading and stopping the Python sidecar.
//!
//! The handshake
//! -------------
//! desktop/server/main.py binds 127.0.0.1 on an ephemeral port, mints a bearer
//! token, and prints exactly one line of JSON on stdout:
//!
//!     {"port": <int>, "token": "<str>", "pid": <int>}
//!
//! That line is the only place the token is ever emitted. It is read here, in
//! the shell process, and goes to the renderer over Tauri's IPC. It is never
//! written to a file, never put on a command line (any local process can read
//! another process's argv), and never served over HTTP.
//!
//! Nothing may be orphaned
//! -----------------------
//! The sidecar can be holding a llama-server child with several gigabytes of
//! VRAM. Three mechanisms keep that from outliving the app, and they cover
//! different failures:
//!
//!   1. llama-server is inside a kill-on-close Windows Job Object owned by the
//!      Python process (agent/hearth_llama._win_job). When Python dies, by any
//!      means including TerminateProcess, the kernel closes the last handle to
//!      that job and kills what is inside it. This shell must not break that
//!      chain, which means it must not put itself between them: it starts
//!      Python directly and lets Python own its own child.
//!
//!   2. Python is started with --watch-parent and a piped stdin that this
//!      process holds open, plus a newline written down that pipe every
//!      HEARTBEAT. The pipe alone is not enough: a parent that leaks an
//!      inheritable copy of the write handle into the child produces no EOF at
//!      all, ever, which is what CPython's own subprocess module does on
//!      Windows. Rust's std does not, and neither did libuv, but the sidecar
//!      does not have to take that on trust -- it shuts down when the beats
//!      stop, whatever the pipe thinks. See desktop/server/main.py's
//!      watch_parent.
//!
//!   3. A second Job Object, created HERE, holding the Python process, also
//!      kill-on-close. This is new in the Tauri shell and it closes the last
//!      gap: (2) is a sixty-second timeout, so a hard kill of this process
//!      used to leave the sidecar and its model resident for up to a minute.
//!      With this, the kernel kills it in the same breath as this process.
//!      Job membership is inherited, so llama-server is inside this one too
//!      and is covered twice over. Nesting is legal on Windows 8 and later; if
//!      the assignment fails anyway the heartbeat is still there, so this is
//!      an improvement that cannot become a regression.
//!
//! stop() below is the polite version of the same thing: close stdin, wait,
//! and only then kill. The kill is the fallback, not the mechanism.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;

const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(30);
const STOP_GRACE: Duration = Duration::from_secs(4);

/// Comfortably inside the sidecar's PARENT_HEARTBEAT_TIMEOUT of 60s, so a
/// momentarily busy shell cannot be mistaken for a dead one.
const HEARTBEAT: Duration = Duration::from_secs(15);

/// So the vendored interpreter does not flash a console window.
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Debug, Clone)]
pub struct Handshake {
    pub port: u16,
    pub token: String,
}

pub struct Sidecar {
    child: Arc<Mutex<Child>>,
    stdin: Arc<Mutex<Option<ChildStdin>>>,
    stopping: Arc<AtomicBool>,
    /// Held for the lifetime of the process. Dropping it closes the job and
    /// the kernel kills what is inside; see (3) above.
    _job: JobHandle,
}

/// A Windows job object handle that kills its members when it is closed.
struct JobHandle(isize);
unsafe impl Send for JobHandle {}
unsafe impl Sync for JobHandle {}

impl Drop for JobHandle {
    fn drop(&mut self) {
        if self.0 != 0 {
            unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0 as _) };
        }
    }
}

/// Create a kill-on-close job and put `child` in it. Returns a handle whose
/// lifetime is the guarantee; a failure is logged by the caller and is not
/// fatal, because the heartbeat covers the same failure more slowly.
fn contain(child: &Child) -> Option<JobHandle> {
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return None;
        }
        let job = JobHandle(job as isize);
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = SetInformationJobObject(
            job.0 as _,
            JobObjectExtendedLimitInformation,
            &mut limits as *mut _ as *mut _,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if ok == 0 {
            return None;
        }
        if AssignProcessToJobObject(job.0 as _, child.as_raw_handle() as _) == 0 {
            return None;
        }
        Some(job)
    }
}

/// Start the sidecar and return once it has printed its handshake.
///
/// `on_log` receives the sidecar's diagnostic output line by line. Handshake
/// fields never reach it: the line that parses as a handshake is consumed here
/// and not forwarded, so a log sink cannot become a token sink.
///
/// `on_exit` fires if the sidecar goes away on its own. It does not fire for a
/// stop() this shell asked for.
pub fn start(
    python: &Path,
    server_dir: &Path,
    extra_env: HashMap<String, String>,
    on_log: Arc<dyn Fn(String) + Send + Sync>,
    on_exit: Arc<dyn Fn(Option<i32>) + Send + Sync>,
) -> Result<(Sidecar, Handshake), String> {
    let mut command = Command::new(python);
    command
        .arg("main.py")
        .arg("--watch-parent")
        .current_dir(server_dir)
        // stdin is a pipe this process holds open. It is the liveness handle
        // --watch-parent keys on; see the note above.
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(CREATE_NO_WINDOW)
        .env("PYTHONUNBUFFERED", "1");
    for (key, value) in extra_env {
        command.env(key, value);
    }

    let mut child = command
        .spawn()
        .map_err(|err| format!("could not start {}: {}", python.display(), err))?;

    let job = contain(&child).unwrap_or_else(|| {
        on_log("could not put the sidecar in a job object; the stdin heartbeat is the only orphan guard".into());
        JobHandle(0)
    });

    let stdin = child.stdin.take();
    let stdout = child.stdout.take().ok_or("the sidecar has no stdout")?;
    let stderr = child.stderr.take().ok_or("the sidecar has no stderr")?;

    let (tx, rx) = mpsc::channel::<Handshake>();
    let stopping = Arc::new(AtomicBool::new(false));

    {
        let on_log = Arc::clone(&on_log);
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                on_log(line);
            }
        });
    }

    {
        let on_log = Arc::clone(&on_log);
        let on_exit = Arc::clone(&on_exit);
        let stopping = Arc::clone(&stopping);
        thread::spawn(move || {
            let mut sent = false;
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if !sent {
                    if let Some(handshake) = parse_handshake(&line) {
                        sent = true;
                        let _ = tx.send(handshake);
                        continue;
                    }
                }
                on_log(line);
            }
            // stdout reached EOF, so the sidecar is on its way out. If this
            // shell did not ask for that, the user has to be told: everything
            // Hearth does happens on the other side of this pipe.
            drop(tx);
            thread::sleep(Duration::from_millis(200));
            if !stopping.load(Ordering::SeqCst) {
                on_exit(None);
            }
        });
    }

    let handshake = rx
        .recv_timeout(HANDSHAKE_TIMEOUT)
        .map_err(|_| "the sidecar printed no handshake within 30s".to_string())?;

    let stdin = Arc::new(Mutex::new(stdin));
    start_heartbeat(Arc::clone(&stdin), Arc::clone(&stopping));

    Ok((
        Sidecar {
            child: Arc::new(Mutex::new(child)),
            stdin,
            stopping,
            _job: job,
        },
        handshake,
    ))
}

fn parse_handshake(line: &str) -> Option<Handshake> {
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    let port = value.get("port")?.as_u64()?;
    let token = value.get("token")?.as_str()?;
    if port == 0 || port > u16::MAX as u64 || token.is_empty() {
        return None;
    }
    Some(Handshake {
        port: port as u16,
        token: token.to_string(),
    })
}

/// Write a newline down the sidecar's stdin every HEARTBEAT.
///
/// The bytes carry nothing: the sidecar reads and discards them, and has no
/// stdin protocol at all. Their arrival is the whole message.
fn start_heartbeat(stdin: Arc<Mutex<Option<ChildStdin>>>, stopping: Arc<AtomicBool>) {
    thread::spawn(move || loop {
        thread::sleep(HEARTBEAT);
        if stopping.load(Ordering::SeqCst) {
            return;
        }
        let mut guard = match stdin.lock() {
            Ok(guard) => guard,
            Err(_) => return,
        };
        let write = match guard.as_mut() {
            Some(pipe) => pipe.write_all(b"\n").and_then(|()| pipe.flush()),
            None => return,
        };
        if write.is_err() {
            return; // the child is going away; there is nothing left to tell
        }
    });
}

impl Sidecar {
    /// Stop the sidecar and wait for it to actually be gone.
    ///
    /// Closing stdin is the request; killing is the fallback if it is ignored.
    /// Returns when the process has exited, or after the grace period, at
    /// which point it has been killed and its job object has taken
    /// llama-server with it either way.
    pub fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        if let Ok(mut guard) = self.stdin.lock() {
            guard.take(); // dropping the pipe closes it
        }
        let deadline = Instant::now() + STOP_GRACE;
        loop {
            {
                let mut child = match self.child.lock() {
                    Ok(child) => child,
                    Err(_) => return,
                };
                match child.try_wait() {
                    Ok(Some(_)) => return,
                    Ok(None) => {}
                    Err(_) => return,
                }
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return;
                }
            }
            thread::sleep(Duration::from_millis(50));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn handshake_parsing_is_strict() {
        let good = parse_handshake(r#"{"port": 51234, "token": "abc", "pid": 9}"#).unwrap();
        assert_eq!(good.port, 51234);
        assert_eq!(good.token, "abc");
        assert!(parse_handshake("not json").is_none());
        assert!(parse_handshake(r#"{"port": 51234}"#).is_none());
        assert!(parse_handshake(r#"{"port": 0, "token": "abc"}"#).is_none());
        assert!(parse_handshake(r#"{"port": 99999, "token": "abc"}"#).is_none());
        assert!(parse_handshake(r#"{"port": 1, "token": ""}"#).is_none());
    }
}
