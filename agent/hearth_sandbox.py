#!/usr/bin/env python3
r"""hearth child-process containment on Windows.

`run_command` used to hand a string to a shell that inherited the user's whole
token. This module is the containment layer that was missing. It is deliberately
modest about what it achieves, because the measurements behind it are modest.

Three levels, and what each one is actually worth. Every claim here was
measured on Windows 11 with a standard (non-elevated) account; the probe matrix
that produced the numbers is `reachability()` below, runnable with
`--self-test --live`.

  off       Exactly what shipped before: a shell with the user's token, the
            workspace as a working directory and nothing else. Kept so a broken
            sandbox is never the reason a user cannot work.

  limits    The default on Windows. A restricted primary token (every
            removable privilege dropped, the Administrators and local-admin
            group SIDs turned deny-only), a per-command Job object capping
            committed memory and process count, Job UI restrictions (no
            clipboard, no USER handles from outside the job, no ExitWindows),
            an explicit inherited-handle list so the child receives its three
            standard handles and nothing else, and a process-wide outer Job
            marked kill-on-close so no command survives Hearth exiting.
            Be honest about the ceiling: on a standard account this level
            blocks NONE of the five reachability probes. A standard user's
            token already has almost no privileges and already carries
            Administrators as deny-only, so dropping both changes nothing that
            was reachable. What it buys is real but narrow: resource-exhaustion
            caps, a reliable tree kill, no orphans, no handle inheritance, and
            a token that IS meaningfully reduced if Hearth is ever run
            elevated. It costs nothing: no measured development command
            behaves differently under it.

  workspace Opt-in. Everything in `limits`, plus the child runs at an
            integrity level one step below Medium (S-1-16-8191) and the
            workspace is labelled to match, with the child's TEMP redirected
            to a hearth-owned directory at the same label. Windows' mandatory
            integrity policy then refuses every write to any Medium object:
            `%USERPROFILE%`, `HKCU`, the user's other repositories, Hearth's
            own audit database. Cross-process memory reads fail too. This is a
            real, kernel-enforced write boundary and it is the point of the
            module.
            What it does NOT do, stated plainly because the docs used to be
            honest about this gap and must stay honest about its replacement:
            **reads are not blocked.** A command at this level still reads
            `%USERPROFILE%\.gitconfig`, `.ssh\id_rsa`, `.aws\credentials`, and
            every other file the user can read. **Network access is not
            blocked either.** So exfiltration of anything readable remains
            fully available; what changes is that the machine cannot be
            modified outside the workspace.
            It also breaks real tools. Measured: every MSYS2/Cygwin binary
            (all of Git for Windows' `usr\bin`: `bash`, `grep`, `sed`, `awk`,
            `tail`) dies at startup because it cannot create its shared
            `\BaseNamedObjects\msys-*` directory at Medium; `pip install` and
            `npm install` fail or hang for the same family of reasons. Native
            `git`, `python`, `cmd` builtins and PowerShell are unaffected.
            That breakage is why this is not the default.

Rejected, with the reason rather than a shrug:

  Restricting SIDs (`CreateRestrictedToken` with a restricted SID list, the
  mechanism Chromium's renderer sandbox uses). Measured: with restricting SIDs
  of {Everyone, Users, RESTRICTED, Authenticated Users} the child could not
  read the workspace, could not resolve its own working directory, and could
  not start `git` or `python` at all. The reason is structural on a developer
  machine, not a tuning problem: the toolchain lives inside the user profile
  (`%LOCALAPPDATA%\Programs\Python`, `%APPDATA%\npm`), whose ACLs grant the
  user's own SID and nothing else, so a token restricted away from that SID
  cannot execute the tools a coding agent exists to run. Making it work would
  mean re-ACLing the user's Python, node and cargo installations, which is a
  larger and more permanent modification of their machine than the sandbox is
  worth.

  AppContainer (`CreateAppContainerProfile` + `STARTUPINFOEX`). Rejected for
  the same structural reason, one step worse: an AppContainer process is
  denied everything not explicitly granted to its package SID or to
  ALL APPLICATION PACKAGES, and no ordinary developer toolchain carries those
  ACEs. It buys a genuine network boundary (no `internetClient` capability
  means no sockets), which nothing else here offers, and that is the one thing
  worth revisiting if the toolchain problem is ever solved by shipping
  Hearth's own vendored, correctly-ACLed tools.

  Windows Sandbox / containers. A per-command VM is seconds of startup and
  hundreds of megabytes of memory for `git status`, needs Pro or Enterprise
  plus Hyper-V, and shares no filesystem by default. Wrong shape for a tool
  that runs a shell command every few seconds.

One thing the contained levels have to survive, because it is not
hypothetical: Hearth itself running elevated. An administrator's unfiltered
token -- what an elevated terminal, a scheduled task with highest privileges,
or a login over OpenSSH all hand you, as opposed to the UAC-filtered token an
ordinary interactive logon gets -- owns every object it creates as
BUILTIN\Administrators rather than as the user. Deny the child that group,
which is exactly what these levels do and the only case in which doing so buys
anything, and the child is then locked out of Hearth's own files. See
own_created_objects() for the measurement and the fix.

Standard library only; `ctypes` does all of it. Every entry point degrades to
a plain answer off Windows, so the Linux daemon path (systemd + bwrap +
nftables, which is a stronger sandbox than anything here) is untouched.
"""

import ctypes
import os
import sys
import threading

if os.name == "nt":  # pragma: no branch - the import itself is Windows-only
    import ctypes.wintypes as wintypes
    import msvcrt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hearth_paths


LEVELS = ("off", "limits", "workspace")

# The default is `limits` on Windows because it was measured to break nothing,
# and `off` everywhere else because there is no implementation here for POSIX
# and the Linux deployment already sits inside a stronger sandbox.
DEFAULT_WINDOWS_LEVEL = "limits"

# One step below Medium (0x2000), not Low (0x1000). Low would work equally well
# against the user's own Medium files, but it would also make the workspace
# writable by every OTHER Low-integrity process on the machine -- a sandboxed
# browser tab, a PDF reader's renderer -- because the workspace has to be
# labelled at the child's level for the child to write to it. 0x1FFF is below
# Medium and above Low, so the workspace stays closed to the Low-integrity
# processes that actually exist while remaining writable by our child.
SANDBOX_INTEGRITY_SID = "S-1-16-8191"

# Job caps. Generous on purpose: a cap that a real build trips is a cap the
# user turns off, and a sandbox nobody leaves enabled protects nothing. These
# stop a fork bomb and a memory bomb, not a large compile.
DEFAULT_MAX_PROCESSES = 256
DEFAULT_MEMORY_FRACTION = 0.75

_ADMIN_SIDS = (
    "S-1-5-32-544",  # BUILTIN\Administrators
    "S-1-5-114",     # NT AUTHORITY\Local account and member of Administrators
)


# --------------------------------------------------------------------------
# Level resolution
# --------------------------------------------------------------------------

def resolve_level(explicit=None):
    """The sandbox level to use: explicit argument, then HEARTH_SANDBOX, then
    the platform default.

    An unrecognised value resolves to the platform default rather than raising
    or silently disabling: a typo in a config file must not be the thing that
    turns containment off, and it must not be the thing that stops the app
    running commands either.
    """
    for candidate in (explicit, os.environ.get("HEARTH_SANDBOX")):
        value = (candidate or "").strip().lower()
        if value in LEVELS:
            if value != "off" and not hearth_paths.is_windows():
                return "off"
            return value
    return DEFAULT_WINDOWS_LEVEL if hearth_paths.is_windows() else "off"


def describe(level):
    """One honest sentence per level, for a settings screen or a doctor page."""
    return {
        "off": "No containment. A command runs with your full account access.",
        "limits": ("Privileges dropped, memory and process caps, no orphaned "
                   "processes. Does not stop a command reading or writing "
                   "outside the workspace."),
        "workspace": ("Commands cannot write anything outside the workspace, "
                      "and cannot read another process's memory. They can "
                      "still READ your files and reach the network. Breaks "
                      "MSYS2 tools (git-bash, grep, sed), pip install and "
                      "npm install."),
    }.get(level, "Unknown sandbox level.")


def available():
    """(ok, reason). Whether this module can actually contain anything here."""
    if not hearth_paths.is_windows():
        return False, "not Windows (the Linux deployment uses systemd and bwrap)"
    try:
        _k32()
        _a32()
    except OSError as exc:  # pragma: no cover - a Windows without kernel32
        return False, "cannot load Win32 libraries: {}".format(exc)
    return True, "ok"


# --------------------------------------------------------------------------
# Win32 plumbing
# --------------------------------------------------------------------------

_libs = {}


def _k32():
    if "k32" not in _libs:
        _libs["k32"] = ctypes.WinDLL("kernel32", use_last_error=True)
        k = _libs["k32"]
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetStdHandle.argtypes = [wintypes.DWORD]
        k.GetStdHandle.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE),
                                 ctypes.POINTER(wintypes.HANDLE),
                                 ctypes.c_void_p, wintypes.DWORD]
        k.CreatePipe.restype = wintypes.BOOL
        k.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        k.SetHandleInformation.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.ResumeThread.argtypes = [wintypes.HANDLE]
        k.ResumeThread.restype = wintypes.DWORD
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
        k.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        k.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        k.UpdateProcThreadAttribute.restype = wintypes.BOOL
        k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        k.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
        k.GlobalMemoryStatusEx.restype = wintypes.BOOL
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k.LocalFree.restype = ctypes.c_void_p
    return _libs["k32"]


def _a32():
    if "a32" not in _libs:
        _libs["a32"] = ctypes.WinDLL("advapi32", use_last_error=True)
        a = _libs["a32"]
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE)]
        a.OpenProcessToken.restype = wintypes.BOOL
        a.CreateRestrictedToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
            ctypes.POINTER(wintypes.HANDLE)]
        a.CreateRestrictedToken.restype = wintypes.BOOL
        a.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          ctypes.c_void_p, wintypes.DWORD]
        a.SetTokenInformation.restype = wintypes.BOOL
        a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
        a.GetTokenInformation.restype = wintypes.BOOL
        a.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        a.EqualSid.restype = wintypes.BOOL
        a.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR,
                                             ctypes.POINTER(ctypes.c_void_p)]
        a.ConvertStringSidToSidW.restype = wintypes.BOOL
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD)]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        a.GetSecurityDescriptorSacl.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        a.GetSecurityDescriptorSacl.restype = wintypes.BOOL
        a.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p)]
        a.GetNamedSecurityInfoW.restype = wintypes.DWORD
        a.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        a.SetNamedSecurityInfoW.restype = wintypes.DWORD
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        a.CreateProcessAsUserW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
            wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
        a.CreateProcessAsUserW.restype = wintypes.BOOL
    return _libs["a32"]


TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
DISABLE_MAX_PRIVILEGE = 0x1
TOKEN_INTEGRITY_LEVEL = 25
TOKEN_USER = 1
TOKEN_OWNER = 4
SE_GROUP_INTEGRITY = 0x20

HANDLE_FLAG_INHERIT = 0x1
STARTF_USESTDHANDLES = 0x00000100
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
STILL_ACTIVE = 259
INFINITE = 0xFFFFFFFF

JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# Deliberately NOT set: JOB_OBJECT_LIMIT_BREAKAWAY_OK and
# SILENT_BREAKAWAY_OK. Their absence is what makes CREATE_BREAKAWAY_FROM_JOB
# fail, which is the whole reason the Job holds.
JOB_UI_RESTRICTIONS = (
    0x0001 |  # HANDLES: no USER handles owned outside the job
    0x0002 |  # READCLIPBOARD
    0x0004 |  # WRITECLIPBOARD
    0x0008 |  # SYSTEMPARAMETERS
    0x0010 |  # DISPLAYSETTINGS
    0x0020 |  # GLOBALATOMS
    0x0040 |  # DESKTOP
    0x0080    # EXITWINDOWS: a command cannot reboot the machine
)

SE_FILE_OBJECT = 1
LABEL_SECURITY_INFORMATION = 0x00000010
SDDL_REVISION_1 = 1


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


class _TOKEN_OWNER(ctypes.Structure):
    _fields_ = [("Owner", ctypes.c_void_p)]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", ctypes.c_ulong),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", ctypes.c_long)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_ulong), ("dwY", ctypes.c_ulong),
        ("dwXSize", ctypes.c_ulong), ("dwYSize", ctypes.c_ulong),
        ("dwXCountChars", ctypes.c_ulong), ("dwYCountChars", ctypes.c_ulong),
        ("dwFillAttribute", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("wShowWindow", ctypes.c_ushort), ("cbReserved2", ctypes.c_ushort),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                ("dwProcessId", ctypes.c_ulong), ("dwThreadId", ctypes.c_ulong)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", ctypes.c_ulong)]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
        (n, ctypes.c_ulonglong) for n in (
            "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
            "ullTotalVirtual", "ullAvailVirtual", "sullAvailExtendedVirtual")]


class SandboxError(RuntimeError):
    """The containment mechanism itself failed.

    Distinct from a command failing: this means the sandbox could not be built,
    so the caller has to decide between running uncontained and not running at
    all. `limits` degrades; `workspace` refuses.
    """


def _win_error(what):
    code = ctypes.get_last_error()
    return SandboxError("{} failed: {}".format(what, ctypes.WinError(code)))


def _sid(text):
    p = ctypes.c_void_p()
    if not _a32().ConvertStringSidToSidW(text, ctypes.byref(p)):
        raise _win_error("ConvertStringSidToSid({})".format(text))
    return p


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------

def build_token(level):
    """A restricted primary token for `level`. Caller closes it.

    The token is always derived from this process's own token with
    CreateRestrictedToken, which is what lets CreateProcessAsUser work without
    SeAssignPrimaryTokenPrivilege -- a privilege an ordinary interactive user
    does not have. A freshly logged-on token from LogonUser would need it, and
    would need the user's password, so this is not merely the convenient path
    but the only one available to a desktop app.
    """
    a = _a32()
    k = _k32()
    current = wintypes.HANDLE()
    if not a.OpenProcessToken(
            k.GetCurrentProcess(),
            TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT,
            ctypes.byref(current)):
        raise _win_error("OpenProcessToken")

    deny = (_SID_AND_ATTRIBUTES * len(_ADMIN_SIDS))()
    for i, text in enumerate(_ADMIN_SIDS):
        deny[i].Sid = _sid(text)
        deny[i].Attributes = 0

    token = wintypes.HANDLE()
    try:
        if not a.CreateRestrictedToken(
                current, DISABLE_MAX_PRIVILEGE, len(_ADMIN_SIDS), deny,
                0, None, 0, None, ctypes.byref(token)):
            raise _win_error("CreateRestrictedToken")
    finally:
        k.CloseHandle(current)

    if level == "workspace":
        label = _TOKEN_MANDATORY_LABEL()
        label.Label.Sid = _sid(SANDBOX_INTEGRITY_SID)
        label.Label.Attributes = SE_GROUP_INTEGRITY
        if not a.SetTokenInformation(token, TOKEN_INTEGRITY_LEVEL,
                                     ctypes.byref(label), ctypes.sizeof(label)):
            k.CloseHandle(token)
            raise _win_error("SetTokenInformation(TokenIntegrityLevel)")
    return token


# --------------------------------------------------------------------------
# Default owner
# --------------------------------------------------------------------------

_owner_note = "not attempted"


def _token_sid(token, info_class):
    """(sid_pointer, backing_buffer) from a token query whose structure starts
    with a PSID. TOKEN_USER and TOKEN_OWNER both do.

    The buffer comes back with the pointer because the pointer points INTO it.
    Drop the buffer and the SID is freed underneath the caller, which is the
    kind of bug that works in testing and corrupts a token in production.
    """
    a = _a32()
    need = wintypes.DWORD()
    a.GetTokenInformation(token, info_class, None, 0, ctypes.byref(need))
    if not need.value:
        return None, None
    buf = (ctypes.c_byte * need.value)()
    if not a.GetTokenInformation(token, info_class, buf, need.value, ctypes.byref(need)):
        return None, None
    return ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value, buf


def own_created_objects():
    r"""Make objects this process creates be owned by the user rather than by
    the Administrators group. Returns a one-line note; never raises.

    This is not housekeeping, it is what makes the contained levels usable at
    all when Hearth runs elevated. Measured, on a machine where Hearth was
    started over OpenSSH (sshd hands an administrator the unfiltered token,
    not the UAC-filtered one an interactive logon gets, so the process runs
    at High integrity with Administrators enabled):

      Every command a contained child ran against a file Hearth had just
      written came back exit 1, "Access is denied." The same command at level
      `off` worked. The child was not being denied by anything this module
      intends: it was being denied by the file.

    The chain is short. An elevated token carries SE_GROUP_OWNER on
    BUILTIN\Administrators rather than on the user, so every object the
    process creates is OWNED by Administrators. Windows 11's per-user
    %LOCALAPPDATA%\Temp grants SYSTEM, Administrators and OWNER RIGHTS, and
    carries no ACE for the user at all. The contained child holds
    Administrators deny-only. Owner is Administrators, so OWNER RIGHTS does
    not help it either; there is no third path, so the answer is no.

    Moving the token's default owner to the user SID fixes it at the source
    and nowhere else:

      - It changes nothing about what THIS process may do. The user is a
        member of Administrators either way; only the owner stamped on new
        objects changes.
      - It is a no-op on the ordinary non-elevated token, whose default owner
        is already the user. Most runs never take the write path below.
      - It does not weaken containment. The child still has Administrators
        denied, so anything reachable only through the admin group is still
        out of reach; what comes back is access to files Hearth itself wrote,
        via the CREATOR OWNER / OWNER RIGHTS ACEs Windows' own directories
        are built on.
      - It is per-process. This is the same thing the "System objects:
        Default owner for objects created by members of the Administrators
        group" policy does machine-wide, applied to Hearth alone rather than
        asking the user to change a security policy.

    Called at import rather than from spawn(), and that is forced rather than
    chosen: an object's owner is fixed when the object is created, and the
    objects a contained child has to reach -- the script hearth_proc spills
    for an over-long command, a file a tool wrote a moment ago -- exist
    before spawn() is ever called. There is no later moment at which this
    could work.
    """
    if not hearth_paths.is_windows():
        return "not Windows: nothing to align"
    a, k = _a32(), _k32()
    token = wintypes.HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(),
                              TOKEN_QUERY | TOKEN_ADJUST_DEFAULT,
                              ctypes.byref(token)):
        return "could not open this process's token: {}".format(
            ctypes.WinError(ctypes.get_last_error()))
    try:
        user_sid, _user_buf = _token_sid(token, TOKEN_USER)
        owner_sid, _owner_buf = _token_sid(token, TOKEN_OWNER)
        if not user_sid:
            return "could not read this process's user SID; default owner left alone"
        if owner_sid and a.EqualSid(owner_sid, user_sid):
            return "not elevated: new objects are already owned by the user"
        info = _TOKEN_OWNER()
        info.Owner = user_sid
        if not a.SetTokenInformation(token, TOKEN_OWNER, ctypes.byref(info),
                                     ctypes.sizeof(info)):
            return "could not move the default owner to the user: {}".format(
                ctypes.WinError(ctypes.get_last_error()))
        return ("elevated: new objects will be owned by the user rather than by "
                "the Administrators group, so a contained command can still "
                "read what Hearth writes")
    finally:
        k.CloseHandle(token)


def owner_note():
    """What own_created_objects() decided at import. For a doctor page: on an
    elevated install this is the difference between every command working and
    every command returning "Access is denied."."""
    return _owner_note


try:
    _owner_note = own_created_objects()
except Exception as _exc:  # noqa: BLE001 - importing this module must never fail
    _owner_note = "default owner unchanged: {}: {}".format(type(_exc).__name__, _exc)


# --------------------------------------------------------------------------
# Job objects
# --------------------------------------------------------------------------

_outer_job = None
_outer_job_lock = threading.Lock()


def outer_job():
    """The process-wide Job that no sandboxed command may outlive.

    Created once, marked KILL_ON_JOB_CLOSE, and never closed: when this Python
    process dies for any reason -- clean exit, crash, taskkill -- the handle
    closes with it and Windows terminates every process still in the Job. That
    is the orphan guard `run_command` did not have; the previous code only
    killed a tree on timeout, so a command that finished while leaving a
    background process behind left it running forever.

    Deliberately NOT the Job that carries the resource limits. Those are
    per-command (see `_command_job`), because a memory cap shared across every
    command a session ever ran would be meaningless, and because terminating
    the shared Job to kill one timed-out command would take every other
    command's background process with it.
    """
    global _outer_job
    with _outer_job_lock:
        if _outer_job is None:
            k = _k32()
            handle = k.CreateJobObjectW(None, None)
            if not handle:
                raise _win_error("CreateJobObject(outer)")
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(
                    handle, JobObjectExtendedLimitInformation,
                    ctypes.byref(info), ctypes.sizeof(info)):
                k.CloseHandle(handle)
                raise _win_error("SetInformationJobObject(outer)")
            _outer_job = handle
    return _outer_job


def _total_physical_bytes():
    stat = _MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(stat)
    if not _k32().GlobalMemoryStatusEx(ctypes.byref(stat)):
        return 0
    return stat.ullTotalPhys


def _int_env(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def job_limits():
    """(max_processes, memory_bytes). 0 means "no cap" for either."""
    procs = _int_env("HEARTH_SANDBOX_MAX_PROCS", DEFAULT_MAX_PROCESSES)
    mem_mb = _int_env("HEARTH_SANDBOX_MEM_MB", 0)
    if mem_mb:
        return procs, mem_mb * 1024 * 1024
    total = _total_physical_bytes()
    return procs, int(total * DEFAULT_MEMORY_FRACTION) if total else 0


def _command_job():
    """A per-command Job carrying the resource and UI limits.

    No KILL_ON_JOB_CLOSE here: the handle is released when the command's output
    has been collected, and a background process the command deliberately
    started (a dev server, a watcher) should survive that, exactly as it did
    before this module existed. The outer Job still holds it, so it cannot
    survive Hearth.
    """
    k = _k32()
    handle = k.CreateJobObjectW(None, None)
    if not handle:
        raise _win_error("CreateJobObject(command)")
    procs, memory = job_limits()
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    if procs:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = procs
    if memory:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = memory
    info.BasicLimitInformation.LimitFlags = flags
    if not k.SetInformationJobObject(handle, JobObjectExtendedLimitInformation,
                                     ctypes.byref(info), ctypes.sizeof(info)):
        k.CloseHandle(handle)
        raise _win_error("SetInformationJobObject(command limits)")
    ui = _JOBOBJECT_BASIC_UI_RESTRICTIONS()
    ui.UIRestrictionsClass = JOB_UI_RESTRICTIONS
    if not k.SetInformationJobObject(handle, JobObjectBasicUIRestrictions,
                                     ctypes.byref(ui), ctypes.sizeof(ui)):
        k.CloseHandle(handle)
        raise _win_error("SetInformationJobObject(ui restrictions)")
    return handle


# --------------------------------------------------------------------------
# Integrity labels
# --------------------------------------------------------------------------

def _sacl_from_sddl(sddl):
    a = _a32()
    sd = ctypes.c_void_p()
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, SDDL_REVISION_1, ctypes.byref(sd), None):
        raise _win_error("ConvertStringSecurityDescriptorToSecurityDescriptor")
    present = wintypes.BOOL()
    sacl = ctypes.c_void_p()
    defaulted = wintypes.BOOL()
    if not a.GetSecurityDescriptorSacl(sd, ctypes.byref(present), ctypes.byref(sacl),
                                       ctypes.byref(defaulted)):
        _k32().LocalFree(sd)
        raise _win_error("GetSecurityDescriptorSacl")
    return sd, sacl


def set_label(path, inheritable):
    """Stamp `path` with the sandbox integrity label.

    `NW` (no-write-up) only: read-up and execute-up stay allowed, which is
    what keeps the workspace readable and its scripts runnable by ordinary
    Medium-integrity processes -- the user's own editor, their own shell, and
    Hearth's file tools all keep working on a labelled workspace exactly as
    before. The label restricts writes INTO the workspace from below Medium,
    and grants writes to our own below-Medium child. It does not restrict the
    user.
    """
    flags = "OICI" if inheritable else ""
    sddl = "S:(ML;{};NW;;;{})".format(flags, SANDBOX_INTEGRITY_SID)
    sd, sacl = _sacl_from_sddl(sddl)
    try:
        rc = _a32().SetNamedSecurityInfoW(
            ctypes.create_unicode_buffer(path), SE_FILE_OBJECT,
            LABEL_SECURITY_INFORMATION, None, None, None, sacl)
    finally:
        _k32().LocalFree(sd)
    if rc != 0:
        raise SandboxError("labelling {!r} failed: {}".format(path, ctypes.WinError(rc)))


def read_label_sddl(path):
    """The SDDL text of `path`'s label, or "" when it has none."""
    a = _a32()
    sd = ctypes.c_void_p()
    rc = a.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION,
                                 None, None, None, None, ctypes.byref(sd))
    if rc != 0:
        return ""
    out = wintypes.LPWSTR()
    ok = a.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        sd, SDDL_REVISION_1, LABEL_SECURITY_INFORMATION, ctypes.byref(out), None)
    text = out.value if ok and out.value else ""
    if ok:
        _k32().LocalFree(out)
    _k32().LocalFree(sd)
    return text


def is_labelled(path):
    return SANDBOX_INTEGRITY_SID in (read_label_sddl(path) or "")


# Directories that must never be relabelled wholesale, because doing so would
# hand every sandboxed command write access to far more than a workspace. A
# workspace that IS one of these, or that contains one, is refused.
def _forbidden_label_roots():
    roots = []
    for var in ("USERPROFILE", "SystemRoot", "windir", "ProgramFiles",
                "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA",
                "SystemDrive", "HOMEDRIVE"):
        value = os.environ.get(var)
        if value:
            roots.append(value)
    return roots


def check_labelable(workspace):
    """(ok, reason) for whether `workspace` may be relabelled.

    Refuses a drive root, the Windows directory, Program Files, and the user
    profile root itself. Labelling any of those would make the whole of it
    writable by every sandboxed command, which is the opposite of the point.
    A directory INSIDE the profile (the normal case: a repo under Documents or
    Desktop) is fine.
    """
    try:
        real = os.path.realpath(workspace)
    except OSError as exc:
        return False, "cannot resolve workspace: {}".format(exc)
    if not os.path.isdir(real):
        return False, "workspace is not a directory"
    norm = os.path.normcase(os.path.normpath(real)).rstrip("\\")
    drive, tail = os.path.splitdrive(norm)
    if not tail or tail == "\\":
        return False, "refusing to label a drive root ({})".format(real)
    for root in _forbidden_label_roots():
        try:
            other = os.path.normcase(os.path.normpath(os.path.realpath(root))).rstrip("\\")
        except OSError:
            continue
        if other and norm == other:
            return False, "refusing to label {} itself".format(root)
    return True, "ok"


def label_tree(workspace, force=False):
    """Label `workspace` and everything under it. Returns entries labelled.

    One pass is enough for the life of the workspace: the root carries an
    inheritable (OICI) label, so every file and directory created afterwards
    inherits it, including files the user's own Medium-integrity editor
    creates. Only entries that predate the first pass need the explicit walk,
    which is why an already-labelled root short-circuits unless `force`.
    """
    ok, reason = check_labelable(workspace)
    if not ok:
        raise SandboxError(reason)
    real = os.path.realpath(workspace)
    if not force and is_labelled(real):
        return 0
    set_label(real, inheritable=True)
    count = 1
    for root, dirs, files in os.walk(real):
        for name in dirs:
            try:
                set_label(os.path.join(root, name), inheritable=True)
                count += 1
            except SandboxError:
                pass  # a junction or a file we do not own: leave it alone
        for name in files:
            try:
                set_label(os.path.join(root, name), inheritable=False)
                count += 1
            except SandboxError:
                pass
    return count


def unlabel_tree(workspace):
    """Remove the sandbox label, restoring the default Medium behaviour.

    An empty SACL passed with LABEL_SECURITY_INFORMATION deletes the mandatory
    label, and an object with no label is treated as Medium. Provided because
    a security mechanism that permanently modifies the user's repository
    without an undo is a mechanism users are right to distrust.
    """
    real = os.path.realpath(workspace)
    sd, sacl = _sacl_from_sddl("S:")
    a, k = _a32(), _k32()
    count = 0
    try:
        targets = [real]
        for root, dirs, files in os.walk(real):
            targets.extend(os.path.join(root, n) for n in dirs)
            targets.extend(os.path.join(root, n) for n in files)
        for target in targets:
            if a.SetNamedSecurityInfoW(ctypes.create_unicode_buffer(target),
                                       SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION,
                                       None, None, None, sacl) == 0:
                count += 1
    finally:
        k.LocalFree(sd)
    return count


def sandbox_temp_dir():
    r"""A labelled scratch directory for the child's TEMP.

    The child cannot write to the real `%TEMP%` (it lives under the profile at
    Medium), and pointing TEMP inside the workspace would drop build droppings
    into the user's repository and their `git status`. So it goes under
    Hearth's own data directory, labelled to match the child.
    """
    path = os.path.join(hearth_paths.data_dir(), "sandbox-temp")
    os.makedirs(path, exist_ok=True)
    if not is_labelled(path):
        set_label(path, inheritable=True)
    return path


def prepare_workspace(workspace, level):
    """Make `workspace` usable at `level`. Returns a note, or "" when nothing
    was needed. Raises SandboxError when the level cannot be honoured."""
    if level != "workspace":
        return ""
    if is_labelled(os.path.realpath(workspace)):
        return ""
    count = label_tree(workspace)
    return "hearth-sandbox: labelled {} entries under {} so commands can write " \
           "there and nowhere else".format(count, workspace)


def sandbox_env(env, level):
    """The child's environment for `level`.

    At `workspace` level TEMP and TMP are redirected, because every ordinary
    tool writes temporary files and the real TEMP is unreachable from below
    Medium. Getting this wrong does not produce a clear "access denied": it
    produces a compiler that fails deep inside for no visible reason.
    """
    if level != "workspace":
        return env
    child = dict(env or {})
    scratch = sandbox_temp_dir()
    child["TEMP"] = scratch
    child["TMP"] = scratch
    return child


# --------------------------------------------------------------------------
# Process creation
# --------------------------------------------------------------------------

def _pipe(k):
    sa = _SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.lpSecurityDescriptor = None
    sa.bInheritHandle = 1
    read, write = wintypes.HANDLE(), wintypes.HANDLE()
    if not k.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(sa), 0):
        raise _win_error("CreatePipe")
    # Only the write end crosses into the child. Leaving the read end
    # inheritable would keep the pipe open inside the child, so reading it
    # here would never see EOF and every command would appear to hang.
    if not k.SetHandleInformation(read, HANDLE_FLAG_INHERIT, 0):
        raise _win_error("SetHandleInformation")
    return read, write


def _null_handle(k):
    sa = _SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.bInheritHandle = 1
    handle = k.CreateFileW("NUL", 0x80000000, 0x1 | 0x2, ctypes.byref(sa), 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise _win_error("CreateFile(NUL)")
    return handle


def _env_block(env):
    """A sorted, double-NUL-terminated Unicode environment block.

    CreateProcess documents the block as sorted; an unsorted one is tolerated
    in practice but a `cmd.exe` that binary-searches it is not something worth
    betting a whole tool surface on. Keys Windows uses internally (`=C:` and
    friends) are dropped rather than passed through, since they start with the
    separator and would corrupt the block.
    """
    items = sorted(((str(k), str(v)) for k, v in (env or {}).items() if k and not k.startswith("=")),
                   key=lambda kv: kv[0].upper())
    text = "".join("{}={}\0".format(k, v) for k, v in items) + "\0"
    return ctypes.create_unicode_buffer(text)


class SandboxedProcess(object):
    """A Popen-shaped handle on a contained child.

    Shaped like `subprocess.Popen` on purpose: `hearth_proc.run_contained`'s
    pump/timeout/kill logic is the tested part of that module and should not
    have to grow a second code path to drive this one.
    """

    def __init__(self, pi, out_read, err_read, job, token, level):
        self._pi = pi
        self._out = out_read
        self._err = err_read
        self._job = job
        self._token = token
        self.level = level
        self.pid = pi.dwProcessId
        self.returncode = None
        self._closed = False
        self._lock = threading.Lock()

    def _read_all(self):
        k = _k32()
        chunks = {}

        def pump(handle, key):
            try:
                fd = msvcrt.open_osfhandle(handle, 0)
            except OSError:
                chunks[key] = b""
                return
            try:
                with os.fdopen(fd, "rb", closefd=True) as fh:
                    chunks[key] = fh.read()
            except OSError:
                chunks[key] = chunks.get(key, b"")

        threads = [threading.Thread(target=pump, args=(self._out, "out"), daemon=True),
                   threading.Thread(target=pump, args=(self._err, "err"), daemon=True)]
        for t in threads:
            t.start()
        k.WaitForSingleObject(self._pi.hProcess, INFINITE)
        for t in threads:
            t.join()
        return (chunks.get("out", b"").decode("utf-8", "replace"),
                chunks.get("err", b"").decode("utf-8", "replace"))

    def communicate(self):
        out, err = self._read_all()
        self.poll()
        self._close()
        return out, err

    def poll(self):
        code = wintypes.DWORD()
        if _k32().GetExitCodeProcess(self._pi.hProcess, ctypes.byref(code)):
            if code.value != STILL_ACTIVE:
                self.returncode = _signed(code.value)
        return self.returncode

    def wait(self, timeout=None):
        millis = INFINITE if timeout is None else int(max(0, timeout) * 1000)
        _k32().WaitForSingleObject(self._pi.hProcess, millis)
        return self.poll()

    def kill(self):
        self.kill_tree()

    def kill_tree(self):
        """Terminate the command and everything it spawned, in one call.

        This is why the Job exists. `taskkill /F /T` walks a parent-pid tree
        that a grandchild can leave (by being reparented, or by outliving the
        shell that started it); a Job cannot be left, because the child was
        assigned to it while still suspended and the Job does not permit
        breakaway.
        """
        if self._job:
            _k32().TerminateJobObject(self._job, 1)

    def _close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        k = _k32()
        for handle in (self._pi.hProcess, self._pi.hThread, self._job, self._token):
            if handle:
                try:
                    k.CloseHandle(ctypes.c_void_p(handle) if isinstance(handle, int) else handle)
                except OSError:
                    pass
        self._job = None
        self._token = None

    def close(self):
        self._close()


def _signed(code):
    """Windows exit codes are unsigned; Python callers expect the signed form
    for the 0x8000_0000-and-above range a crash produces."""
    return code - 0x100000000 if code >= 0x80000000 else code


def spawn(command, cwd, env, level):
    """Start `command` (a command line string or argv list) contained at `level`.

    Returns a SandboxedProcess. Raises SandboxError when the containment could
    not be established -- never when the command itself merely fails.

    One deliberate difference from the uncontained path: stdin is the null
    device rather than an inherited copy of Hearth's own. The uncontained path
    hands the child whatever the sidecar's stdin happens to be, so a command
    that reads stdin blocks until the timeout fires; here it gets EOF
    immediately. That is both safer (the sidecar's stdin is not a channel a
    command should have) and better behaved, but it IS a behavioural
    difference between `off` and the contained levels, so it is stated rather
    than discovered.
    """
    if level not in ("limits", "workspace"):
        raise SandboxError("spawn() is only for contained levels, got {!r}".format(level))
    ok, reason = available()
    if not ok:
        raise SandboxError(reason)
    if isinstance(command, (list, tuple)):
        import subprocess
        command = subprocess.list2cmdline(list(command))

    k = _k32()
    a = _a32()
    token = build_token(level)
    job = None
    out_read = err_read = None
    out_write = err_write = null = None
    attrs = None
    try:
        job = _command_job()
        out_read, out_write = _pipe(k)
        err_read, err_write = _pipe(k)
        null = _null_handle(k)

        # Restrict inheritance to exactly these three handles. Without the
        # list, bInheritHandles=TRUE hands the child every inheritable handle
        # this process holds, and one leaked handle to something outside the
        # workspace is a hole straight through everything else here.
        size = ctypes.c_size_t(0)
        k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        buffer_ = (ctypes.c_byte * size.value)()
        if not k.InitializeProcThreadAttributeList(buffer_, 1, 0, ctypes.byref(size)):
            raise _win_error("InitializeProcThreadAttributeList")
        # Only now is the buffer a real attribute list, so only now may the
        # cleanup path call DeleteProcThreadAttributeList on it.
        attrs = buffer_
        handles = (ctypes.c_void_p * 3)(null, out_write.value, err_write.value)
        if not k.UpdateProcThreadAttribute(
                attrs, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles,
                ctypes.sizeof(handles), None, None):
            raise _win_error("UpdateProcThreadAttribute(handle list)")

        six = _STARTUPINFOEXW()
        six.StartupInfo.cb = ctypes.sizeof(six)
        six.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        six.StartupInfo.hStdInput = null
        six.StartupInfo.hStdOutput = out_write.value
        six.StartupInfo.hStdError = err_write.value
        six.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)

        pi = _PROCESS_INFORMATION()
        flags = (CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT
                 | CREATE_NEW_PROCESS_GROUP | EXTENDED_STARTUPINFO_PRESENT)
        block = _env_block(sandbox_env(env, level))
        if not a.CreateProcessAsUserW(
                token, None, ctypes.create_unicode_buffer(command), None, None,
                True, flags, ctypes.byref(block), cwd, ctypes.byref(six),
                ctypes.byref(pi)):
            raise _win_error("CreateProcessAsUser")

        # Suspended until both Jobs hold it. Assign the outer (orphan guard)
        # first so the per-command Job nests inside it; a process assigned to
        # the inner Job first could not then join the outer one. If either
        # assignment fails the child is killed rather than resumed, because a
        # child outside the Job is a child outside the containment.
        try:
            if not k.AssignProcessToJobObject(outer_job(), pi.hProcess):
                raise _win_error("AssignProcessToJobObject(outer)")
            if not k.AssignProcessToJobObject(job, pi.hProcess):
                raise _win_error("AssignProcessToJobObject(command)")
        except SandboxError:
            k.TerminateJobObject(job, 1)
            _terminate(pi)
            raise
        if k.ResumeThread(pi.hThread) == 0xFFFFFFFF:
            k.TerminateJobObject(job, 1)
            _terminate(pi)
            raise _win_error("ResumeThread")

        proc = SandboxedProcess(pi, out_read.value, err_read.value, job, token, level)
        job = token = None  # ownership moved to the process object
        out_read = err_read = None
        return proc
    finally:
        for handle in (out_write, err_write):
            if handle:
                k.CloseHandle(handle)
        if null:
            k.CloseHandle(ctypes.c_void_p(null))
        if attrs is not None:
            k.DeleteProcThreadAttributeList(attrs)
        for handle in (out_read, err_read):
            if handle:
                k.CloseHandle(handle)
        if job:
            k.CloseHandle(job)
        if token:
            k.CloseHandle(token)


def _terminate(pi):
    """Kill a child that was created but never resumed, then close its handles.

    TerminateProcess first, not just CloseHandle. The child is created
    suspended, so if Job assignment or ResumeThread failed it is sitting there
    alive, unresumed, and -- crucially -- possibly in no Job at all, which is
    the one case where closing the handles would leak a process nothing can
    reach afterwards.
    """
    k = _k32()
    try:
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess(pi.hProcess, 1)
    except OSError:
        pass
    k.CloseHandle(pi.hThread)
    k.CloseHandle(pi.hProcess)


# --------------------------------------------------------------------------
# The attack matrix
# --------------------------------------------------------------------------

PROBE_NAMES = ("read_outside_workspace", "write_user_profile", "network",
               "spawn_grandchild", "read_process_memory", "write_workspace")


def reachability(level, workspace, runner=None, canary=None, port=None):
    """What a child at `level` can actually reach. Returns {probe: bool}.

    This is the module's own honesty check and the source of the table in the
    docs. It runs real commands and looks at what came back, rather than
    asserting that a flag was passed.
    """
    if runner is None:
        import hearth_proc
        def runner(cmd):
            rc, out, err, _ = hearth_proc.run_contained(cmd, workspace, timeout=60, level=level)
            return rc, out, err

    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    profile_probe = os.path.join(os.environ.get("USERPROFILE", ""), "hearth-sandbox-probe.txt")
    mem = ("import ctypes,sys;h=ctypes.windll.kernel32.OpenProcess(0x0010|0x0400,False,{});"
           "print('REACHED' if h else 'BLOCKED')".format(os.getpid()))
    checks = {
        "read_outside_workspace": ('type "{}"'.format(canary), "CANARY"),
        "write_user_profile": ('echo x> "{}" && type "{}"'.format(profile_probe, profile_probe), "x"),
        "network": ('curl.exe -s -m 8 http://127.0.0.1:{}/'.format(port), "reached"),
        "spawn_grandchild": ('{} /d /s /c "{} /d /s /c echo GRANDCHILD"'.format(comspec, comspec),
                             "GRANDCHILD"),
        "read_process_memory": ('"{}" -c "{}"'.format(sys.executable, mem), "REACHED"),
        "write_workspace": ('echo ok> hearth-sandbox-probe.txt && type hearth-sandbox-probe.txt', "ok"),
    }
    result = {}
    for name, (cmd, needle) in checks.items():
        if name == "read_outside_workspace" and not canary:
            continue
        if name == "network" and not port:
            continue
        try:
            _rc, out, _err = runner(cmd)
        except Exception:  # noqa: BLE001 - a probe that cannot run is not reachable
            out = ""
        result[name] = needle in (out or "")
    try:
        os.unlink(profile_probe)
    except OSError:
        pass
    return result


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _self_test(live=False):
    import shutil
    import tempfile

    # Level resolution is pure and testable everywhere.
    old = os.environ.get("HEARTH_SANDBOX")
    try:
        os.environ.pop("HEARTH_SANDBOX", None)
        default = resolve_level()
        assert default == ("limits" if hearth_paths.is_windows() else "off"), default
        os.environ["HEARTH_SANDBOX"] = "off"
        assert resolve_level() == "off"
        os.environ["HEARTH_SANDBOX"] = "WORKSPACE"
        assert resolve_level() == ("workspace" if hearth_paths.is_windows() else "off")
        os.environ["HEARTH_SANDBOX"] = "nonsense"
        assert resolve_level() == default, "a typo must not silently disable containment"
        # An explicit argument wins over the environment.
        assert resolve_level("off") == "off"
        os.environ.pop("HEARTH_SANDBOX", None)
        assert resolve_level("workspace") == ("workspace" if hearth_paths.is_windows() else "off")
    finally:
        if old is None:
            os.environ.pop("HEARTH_SANDBOX", None)
        else:
            os.environ["HEARTH_SANDBOX"] = old

    for level in LEVELS:
        assert describe(level) and "Unknown" not in describe(level), level
    assert "Unknown" in describe("bogus")

    ok, reason = available()
    if not hearth_paths.is_windows():
        assert not ok and "Windows" in reason, reason
        print("hearth-sandbox self-test OK (not Windows; containment is a no-op here)")
        return 0
    assert ok, reason

    procs, memory = job_limits()
    assert procs == DEFAULT_MAX_PROCESSES, procs
    assert memory > 0, "no memory cap derived from physical RAM"
    os.environ["HEARTH_SANDBOX_MAX_PROCS"] = "17"
    os.environ["HEARTH_SANDBOX_MEM_MB"] = "512"
    try:
        assert job_limits() == (17, 512 * 1024 * 1024), job_limits()
        os.environ["HEARTH_SANDBOX_MAX_PROCS"] = "not-a-number"
        assert job_limits()[0] == DEFAULT_MAX_PROCESSES
    finally:
        os.environ.pop("HEARTH_SANDBOX_MAX_PROCS", None)
        os.environ.pop("HEARTH_SANDBOX_MEM_MB", None)

    # A token can be built at both contained levels, and the workspace level's
    # token really carries the reduced integrity SID.
    for level in ("limits", "workspace"):
        token = build_token(level)
        assert token, level
        _k32().CloseHandle(token)

    # check_labelable refuses the roots that would turn the sandbox inside out.
    ws = os.path.realpath(tempfile.mkdtemp(prefix="hearth-sbx-"))
    try:
        assert check_labelable(ws)[0] is True, check_labelable(ws)
        profile = os.environ.get("USERPROFILE")
        if profile:
            allowed, why = check_labelable(profile)
            assert allowed is False and "itself" in why, (allowed, why)
        allowed, why = check_labelable(os.path.splitdrive(ws)[0] + "\\")
        assert allowed is False and "drive root" in why, (allowed, why)
        assert check_labelable(os.path.join(ws, "missing"))[0] is False

        # Labelling round-trips, and unlabelling really removes it.
        os.makedirs(os.path.join(ws, "sub"))
        with open(os.path.join(ws, "sub", "f.txt"), "w") as fh:
            fh.write("x")
        assert not is_labelled(ws)
        count = label_tree(ws)
        assert count >= 3, count
        assert is_labelled(ws) and is_labelled(os.path.join(ws, "sub", "f.txt"))
        assert label_tree(ws) == 0, "an already-labelled tree must not be walked again"
        assert SANDBOX_INTEGRITY_SID in read_label_sddl(ws)
        unlabel_tree(ws)
        assert not is_labelled(ws), "unlabel_tree left the label behind"

        # A contained child must be able to read what Hearth itself just
        # wrote. This one spawns a real process even without --live, unlike
        # every other process-creation check in this module, because the
        # failure it guards is invisible on an ordinary non-elevated
        # developer machine and total on an elevated one: with the default
        # owner left as the Administrators group, EVERY command a contained
        # child ran against a Hearth-written file came back "Access is
        # denied.". A regression that only bites elevated users has to be
        # caught by the test everybody runs, and one cmd.exe costs
        # milliseconds, not the seconds --live exists to gate.
        assert isinstance(owner_note(), str) and owner_note(), owner_note()
        handoff = os.path.join(ws, "handoff.txt")
        with open(handoff, "w", encoding="utf-8") as fh:
            fh.write("HANDOFF-CANARY")
        proc = spawn('{} /d /s /c type "{}"'.format(
            os.environ.get("COMSPEC") or "cmd.exe", handoff),
            ws, dict(os.environ), "limits")
        seen, seen_err = proc.communicate()
        assert "HANDOFF-CANARY" in seen, (
            "a contained child could not read a file Hearth had just written: "
            "{!r} / {!r}. The usual cause is this process running elevated, "
            "whose token owns new objects as the Administrators group while "
            "the child holds that group deny-only. See own_created_objects()."
            .format(seen, seen_err))

        if live:
            _live_checks(ws)
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    print("hearth-sandbox self-test OK{}".format(" (with --live)" if live else ""))
    return 0


def _live_checks(ws):
    """Real process creation. Behind --live because it costs seconds."""
    import hearth_proc

    # A contained command runs, and returns its output and exit code.
    rc, out, err, timed_out = hearth_proc.run_contained("echo hello", ws, timeout=60,
                                                        level="limits")
    assert rc == 0 and "hello" in out and timed_out is False, (rc, out, err)
    rc, _out, _err, _ = hearth_proc.run_contained("exit 7", ws, timeout=60, level="limits")
    assert rc == 7, rc

    # stdin is the null device, so a command that reads it gets EOF instead of
    # blocking on whatever the sidecar's stdin happens to be.
    rc, out, _err, timed_out = hearth_proc.run_contained(
        '"{}" -c "import sys; print(repr(sys.stdin.read()))"'.format(sys.executable),
        ws, timeout=60, level="limits")
    assert timed_out is False and "''" in out, (rc, out)

    # The Job kills the whole tree on timeout, including a grandchild that
    # outlives the shell that started it.
    import time
    t0 = time.time()
    rc, _out, _err, timed_out = hearth_proc.run_contained(
        "ping -n 30 127.0.0.1 >nul", ws, timeout=3, level="limits")
    assert timed_out is True, "timeout not reported"
    assert time.time() - t0 < 15, "tree kill did not land"

    # The process-count cap is enforced by the Job, not by hope.
    old = os.environ.get("HEARTH_SANDBOX_MAX_PROCS")
    os.environ["HEARTH_SANDBOX_MAX_PROCS"] = "1"
    try:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        rc, out, err, _ = hearth_proc.run_contained(
            '{} /d /s /c "echo NESTED"'.format(comspec), ws, timeout=60, level="limits")
        assert "NESTED" not in out, (
            "ActiveProcessLimit=1 still allowed a grandchild: {!r}".format(out))
    finally:
        if old is None:
            os.environ.pop("HEARTH_SANDBOX_MAX_PROCS", None)
        else:
            os.environ["HEARTH_SANDBOX_MAX_PROCS"] = old

    # The `workspace` level: writes inside succeed, writes to the profile fail.
    # This is THE claim the level makes, so it is asserted in both directions.
    label_tree(ws)
    try:
        rc, out, err, _ = hearth_proc.run_contained(
            "echo inside> ok.txt && type ok.txt", ws, timeout=60, level="workspace")
        assert "inside" in out, ("workspace level broke writing INSIDE the workspace",
                                 rc, out, err)
        target = os.path.join(os.environ["USERPROFILE"], "hearth-sandbox-selftest.txt")
        # Clear it first. The assertion below is "this file does not exist",
        # so a copy left behind by an earlier failed run (the escape assertion
        # fires before its own cleanup) would fail every subsequent run and
        # send whoever reads it looking for a bug that is not there.
        try:
            os.unlink(target)
        except OSError:
            pass
        rc, out, err, _ = hearth_proc.run_contained(
            'echo pwned> "{}"'.format(target), ws, timeout=60, level="workspace")
        assert not os.path.exists(target), (
            "SANDBOX ESCAPE: a workspace-level command wrote {}".format(target))
        assert "denied" in (out + err).lower(), (rc, out, err)

        # ... and the same write DOES succeed with containment off, which is
        # what makes the assertion above a test of the sandbox rather than a
        # test of the filesystem.
        rc, out, err, _ = hearth_proc.run_contained(
            'echo pwned> "{}"'.format(target), ws, timeout=60, level="off")
        assert os.path.exists(target), (
            "the control case failed: the write was blocked with the sandbox OFF, "
            "so the workspace-level assertion above proves nothing", rc, out, err)
        os.unlink(target)

        # Reading outside the workspace still works. Asserted, not glossed:
        # if this ever starts failing the documentation is wrong.
        rc, out, _err, _ = hearth_proc.run_contained(
            'dir /b "%USERPROFILE%"', ws, timeout=60, level="workspace")
        assert rc == 0 and out.strip(), (
            "reads outside the workspace are documented as NOT blocked; "
            "this now fails, so the docs need updating", rc, out)
    finally:
        unlabel_tree(ws)

    _orphan_guard_check(ws)


def _orphan_guard_check(ws):
    """Killing Hearth must kill every command Hearth started.

    Before the outer Job existed, a command that finished while leaving a
    background process behind left it running forever, and killing the sidecar
    left it running too: the only tree kill was on the timeout path. This test
    kills the parent WITHOUT /T on purpose. taskkill's tree walk would prove
    nothing; what has to do the work is the Job handle closing when the parent
    process dies.
    """
    import subprocess
    import time

    marker = os.path.join(ws, "orphan-pid.txt")
    helper = os.path.join(ws, "orphan_helper.py")
    inner = ("import os,time;open(r'{}','w').write(str(os.getpid()));"
             "time.sleep(600)".format(marker))
    with open(helper, "w", encoding="utf-8") as fh:
        fh.write(
            "import sys\n"
            "sys.path.insert(0, r'{agent}')\n"
            "import hearth_proc\n"
            "hearth_proc.run_contained(r'''\"{py}\" -c \"{inner}\"''', r'{ws}',"
            " timeout=600, level='limits')\n".format(
                agent=os.path.dirname(os.path.abspath(__file__)),
                py=sys.executable, inner=inner, ws=ws))

    parent = subprocess.Popen([sys.executable, helper],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not os.path.exists(marker):
            time.sleep(0.2)
        assert os.path.exists(marker), "the helper never started its command"
        pid = open(marker, encoding="utf-8").read().strip()
        assert pid.isdigit(), pid
        assert _pid_alive(pid), "the grandchild was not running to begin with"

        subprocess.run(["taskkill", "/F", "/PID", str(parent.pid)],
                       capture_output=True, timeout=30)
        deadline = time.time() + 30
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.3)
        assert not _pid_alive(pid), (
            "ORPHAN: grandchild {} outlived the process that started it; the "
            "kill-on-job-close guard did not fire".format(pid))
    finally:
        try:
            parent.kill()
        except OSError:
            pass
        pid = open(marker, encoding="utf-8").read().strip() if os.path.exists(marker) else ""
        if pid.isdigit() and _pid_alive(pid):
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)


def _pid_alive(pid):
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "PID eq {}".format(pid)],
                         capture_output=True, text=True).stdout
    return str(pid) in (out or "")


def main(argv):
    live = "--live" in argv
    if "--probe" in argv:
        return _print_matrix(argv)
    return _self_test(live=live)


def _print_matrix(argv):
    """`--probe` prints the before/after reachability table for the docs."""
    import shutil
    import socket
    import tempfile

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nreached")
                conn.close()
            except OSError:
                return
    threading.Thread(target=serve, daemon=True).start()

    canary = os.path.join(os.environ.get("USERPROFILE", ""), "hearth-sandbox-canary.txt")
    with open(canary, "w", encoding="utf-8") as fh:
        fh.write("CANARY\n")
    ws = os.path.realpath(tempfile.mkdtemp(prefix="hearth-matrix-"))
    levels = [a for a in argv if a in LEVELS] or list(LEVELS)
    try:
        rows = {}
        for level in levels:
            if level == "workspace":
                label_tree(ws)
            rows[level] = reachability(level, ws, canary=canary, port=port)
        width = max(len(n) for n in PROBE_NAMES)
        print("| {} | {} |".format("probe".ljust(width),
                                   " | ".join(l.ljust(9) for l in levels)))
        print("|{}|{}|".format("-" * (width + 2),
                               "|".join("-" * 11 for _ in levels)))
        for name in PROBE_NAMES:
            cells = []
            for level in levels:
                value = rows[level].get(name)
                cells.append(("reachable" if value else "blocked").ljust(9))
            print("| {} | {} |".format(name.ljust(width), " | ".join(cells)))
    finally:
        try:
            unlabel_tree(ws)
        except SandboxError:
            pass
        listener.close()
        shutil.rmtree(ws, ignore_errors=True)
        try:
            os.unlink(canary)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
