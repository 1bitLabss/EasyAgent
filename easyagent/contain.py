"""The tool process cannot read the data directory. The OS enforces it.

Linux hides that directory inside bubblewrap and binds back only this bot's
workspace, workbench, and tmp. macOS denies file-read on it in the
sandbox-exec profile. Windows launches an AppContainer that is granted those
folders only, or a restricted token with a deny ACE on the data directory
when the container profile cannot be created. The container is not granted
the keychain. Profile creation and folder grants are a one-time setup.
``/sandbox`` reports the mechanism the startup probe actually launched.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

# Buffers the child process still needs after the function that built them returns.
_PINNED: list = []

# ProcThreadAttributeSecurityCapabilities is 9 | PROC_THREAD_ATTRIBUTE_INPUT.
# 0x00020000 is ProcThreadAttributeParentProcess, and Windows rejects the
# SECURITY_CAPABILITIES struct with Win32 error 24 (ERROR_BAD_LENGTH).
SECURITY_CAPABILITIES = 0x00020009
PROFILE_NAME = "EasyAgent.Bot"
PROBE_PROFILE = "EasyAgent.Probe"
_FALLBACK: dict[str, str] = {}
# HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS). CreateAppContainerProfile returns this
# when the profile is already on the computer. Derive still has to run after it.
_HRESULT_ALREADY_EXISTS = 0x800700B7
_HRESULT_NOT_FOUND = 0x80070002
_MAPPINGS = (
    r"Software\Classes\Local Settings\Software\Microsoft\Windows"
    r"\CurrentVersion\AppContainer\Mappings"
)
# internetClient and privateNetworkClientServer. Loopback is still blocked.
NETWORK_CAPABILITY_SIDS = ("S-1-15-3-1", "S-1-15-3-3")
# Everyone, BUILTIN\Users, and RESTRICTED. A restricting set of only the user
# cannot load system DLLs or open the desktop (exit 0xC0000022).
RESTRICTING_SIDS = ("S-1-1-0", "S-1-5-32-545", "S-1-5-12")
LOW_INTEGRITY_SID = "S-1-16-4096"
TOKEN_INTEGRITY_LEVEL = 25
SE_GROUP_INTEGRITY = 0x20
SE_GROUP_ENABLED = 0x2
SE_GROUP_LOGON_ID = 0xC0000000
# Traverse is execute only. RX, R, and W are the generic file masks.
FILE_TRAVERSE = 0x20
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_EXECUTE = 0x001200A0
FILE_READ_EXECUTE = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
FILE_MODIFY = 0x001301BF
FILE_ALL_ACCESS = 0x001F01FF
OBJECT_INHERIT_ACE = 0x1
CONTAINER_INHERIT_ACE = 0x2
INHERITED_ACE = 0x10
GRANT_ACCESS = 1
DENY_ACCESS = 3
REVOKE_ACCESS = 4
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x4
LABEL_SECURITY_INFORMATION = 0x10
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11
SYSTEM_MANDATORY_LABEL_NO_WRITE_UP = 0x1
ACL_REVISION = 2
SE_DACL_PROTECTED = 0x1000
OWNER_SECURITY_INFORMATION = 0x1
ERROR_ACCESS_DENIED = 5
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
SE_PRIVILEGE_ENABLED = 0x00000002
# Longer names first so RX is not read as R plus X.
_RIGHT_NAMES = (
    ("RX", FILE_READ_EXECUTE),
    ("F", FILE_ALL_ACCESS),
    ("M", FILE_MODIFY),
    ("R", FILE_GENERIC_READ),
    ("W", FILE_GENERIC_WRITE),
    ("X", FILE_TRAVERSE),
)
_INHERIT_MARKS = ("(OI)", "(CI)", "(IO)", "(NP)", "(I)")
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0
NO_MULTIPLE_TRUSTEE = 0
_SIGS: dict | None = None


class AclError(OSError):
    """An ACE was not applied or could not be read back. Do not start a weaker process."""


def _hresult_code(hr: int) -> int:
    return int(hr) & 0xFFFFFFFF


def _hr_text(hr: int) -> str:
    return f"HRESULT 0x{_hresult_code(hr):08X}"


def _hresult_ok(hr: int) -> bool:
    return _hresult_code(hr) == 0


def _already_exists(hr: int) -> bool:
    return _hresult_code(hr) == _HRESULT_ALREADY_EXISTS


def _last_error() -> int:
    """The Win32 code from the last call. Zero where ctypes has no last-error slot."""
    import ctypes

    getter = getattr(ctypes, "get_last_error", None)
    if getter is None:
        return 0
    return int(getter() or 0)


def _fail(action: str, err: int | None = None) -> OSError:
    """An OSError that names the Win32 code. Callers pass `err` when a later call would replace it."""
    code = _last_error() if err is None else int(err)
    return OSError(code, f"{action} failed (Win32 {code})")


def _acl_fail(action: str, err: int) -> AclError:
    """A failed ACE. The text is the refusal the person sees. The launch does not continue."""
    code = int(err)
    return AclError(code, f"{action} failed (Win32 {code}). The restricted token was not started.")


def win32_signatures() -> dict[str, dict[str, tuple]]:
    """restype and argtypes for every kernel32, advapi32, and userenv function this module calls."""
    global _SIGS
    if _SIGS is not None:
        return _SIGS
    import ctypes
    from ctypes import wintypes

    handle = wintypes.HANDLE
    dword = wintypes.DWORD
    boolean = wintypes.BOOL
    kernel32 = {
        "GetCurrentProcess": (handle, []),
        "CloseHandle": (boolean, [handle]),
        "CreatePipe": (boolean, [ctypes.POINTER(handle), ctypes.POINTER(handle), ctypes.c_void_p, dword]),
        "CreateProcessW": (
            boolean,
            [
                wintypes.LPCWSTR,
                wintypes.LPWSTR,
                ctypes.c_void_p,
                ctypes.c_void_p,
                boolean,
                dword,
                ctypes.c_void_p,
                wintypes.LPCWSTR,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
        ),
        "GetExitCodeProcess": (boolean, [handle, ctypes.POINTER(dword)]),
        "InitializeProcThreadAttributeList": (
            boolean,
            [ctypes.c_void_p, dword, dword, ctypes.POINTER(ctypes.c_size_t)],
        ),
        "LocalFree": (ctypes.c_void_p, [ctypes.c_void_p]),
        "ReadFile": (boolean, [handle, ctypes.c_void_p, dword, ctypes.POINTER(dword), ctypes.c_void_p]),
        "SetHandleInformation": (boolean, [handle, dword, dword]),
        "TerminateProcess": (boolean, [handle, wintypes.UINT]),
        "UpdateProcThreadAttribute": (
            boolean,
            [ctypes.c_void_p, dword, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p],
        ),
        "WaitForSingleObject": (dword, [handle, dword]),
    }
    advapi32 = {
        "AddAccessDeniedAce": (boolean, [ctypes.c_void_p, dword, dword, ctypes.c_void_p]),
        "AllocateAndInitializeSid": (
            boolean,
            [
                ctypes.c_void_p,
                ctypes.c_byte,
                dword,
                dword,
                dword,
                dword,
                dword,
                dword,
                dword,
                dword,
                ctypes.POINTER(ctypes.c_void_p),
            ],
        ),
        "ConvertSidToStringSidW": (boolean, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]),
        "CreateProcessAsUserW": (
            boolean,
            [
                handle,
                wintypes.LPCWSTR,
                wintypes.LPWSTR,
                ctypes.c_void_p,
                ctypes.c_void_p,
                boolean,
                dword,
                ctypes.c_void_p,
                wintypes.LPCWSTR,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
        ),
        "CreateRestrictedToken": (
            boolean,
            [handle, dword, dword, ctypes.c_void_p, dword, ctypes.c_void_p, dword, ctypes.c_void_p, ctypes.POINTER(handle)],
        ),
        "ConvertStringSidToSidW": (boolean, [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]),
        "CreateWellKnownSid": (boolean, [dword, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(dword)]),
        "GetTokenInformation": (boolean, [handle, dword, ctypes.c_void_p, dword, ctypes.POINTER(dword)]),
        "LookupPrivilegeValueW": (boolean, [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]),
        "AdjustTokenPrivileges": (
            boolean,
            [handle, boolean, ctypes.c_void_p, dword, ctypes.c_void_p, ctypes.POINTER(dword)],
        ),
        "OpenProcessToken": (boolean, [handle, dword, ctypes.POINTER(handle)]),
        "SetTokenInformation": (boolean, [handle, dword, ctypes.c_void_p, dword]),
        # These return a Win32 error code, not a BOOL. Zero is success.
        "SetEntriesInAclW": (dword, [dword, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
        "SetNamedSecurityInfoW": (
            dword,
            [wintypes.LPWSTR, dword, dword, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
        ),
        "GetNamedSecurityInfoW": (
            dword,
            [
                wintypes.LPCWSTR,
                dword,
                dword,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
            ],
        ),
        "GetAce": (boolean, [ctypes.c_void_p, dword, ctypes.POINTER(ctypes.c_void_p)]),
        "EqualSid": (boolean, [ctypes.c_void_p, ctypes.c_void_p]),
        "GetSecurityDescriptorControl": (
            boolean,
            [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(dword)],
        ),
    }
    userenv = {
        "CreateAppContainerProfile": (
            ctypes.c_long,
            [
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                ctypes.c_void_p,
                dword,
                ctypes.POINTER(ctypes.c_void_p),
            ],
        ),
        "DeleteAppContainerProfile": (ctypes.c_long, [wintypes.LPCWSTR]),
        "DeriveAppContainerSidFromAppContainerName": (
            ctypes.c_long,
            [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)],
        ),
    }
    _SIGS = {"kernel32": kernel32, "advapi32": advapi32, "userenv": userenv}
    return _SIGS


def bind_signatures(dll, library: str) -> None:
    """Set restype and argtypes before any call. A missing export is left for the caller to notice."""
    for name, (restype, argtypes) in win32_signatures().get(library, {}).items():
        try:
            fn = getattr(dll, name)
        except AttributeError:
            continue
        fn.restype = restype
        fn.argtypes = list(argtypes)


def _dll(library: str):
    import ctypes

    dll = ctypes.WinDLL(library, use_last_error=True)
    bind_signatures(dll, library)
    return dll


def windows_mechanism() -> str:
    """What a Windows tool process is launched with. ``none`` when that launch is not this one."""
    if os.name != "nt":
        return "none"
    if _appcontainer_ready():
        return "appcontainer"
    if _restricted_ready():
        return "restricted-token"
    return "none"


def popen_contained(argv: list[str], *, env: dict, cwd: str, data_root: Path, allow: list[Path], web: bool = True):
    """Start ``argv`` so it cannot read ``data_root``.

    When a container or a restricted token is the reported mechanism, a failed
    launch is an error. It does not start an ordinary process that can read
    the data folder.
    """
    if os.name != "nt":
        return subprocess.Popen(
            argv,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    return _launch_windows(argv, env, cwd, Path(data_root), allow, web)


def popen_mcp(argv: list[str], *, env: dict, cwd: str, data_root: Path, allow: list[Path], web: bool = True):
    """A contained MCP server with binary stdin and stdout.

    The shell launcher does not attach stdin. Connectors need it, so this is a
    separate entry that still uses the AppContainer or the restricted token.
    """
    if os.name != "nt":
        raise OSError("not windows")
    return _launch_windows(list(argv), env, cwd, Path(data_root), list(allow), web, True)


def _appcontainer_ready() -> bool:
    try:
        userenv = _dll("userenv")
        return hasattr(userenv, "CreateAppContainerProfile")
    except (AttributeError, OSError):
        return False


def _restricted_ready() -> bool:
    try:
        advapi = _dll("advapi32")
        return hasattr(advapi, "CreateRestrictedToken") and hasattr(advapi, "AddAccessDeniedAce")
    except (AttributeError, OSError):
        return False


def _launch_windows(argv, env, cwd, data_root: Path, allow: list[Path], web: bool, stdio: bool = False):
    """Start a contained process. A failed AppContainer falls back once.

    This does not create a profile and it does not change an ACL. Those happen
    in setup, after the person agrees. The keychain folders are never named.
    """
    from easyagent.selftest import cached_probe

    report = cached_probe()
    if not report.get("passed"):
        reason = report.get("reason") or "Windows containment is unavailable."
        raise OSError(f"{reason} The command was not run.")
    if not consented():
        raise OSError(
            "Windows containment passed its check, and the one-time profile is not created yet. "
            "Approve the card, or run: easyagent contain setup. The command was not run."
        )
    folders = [path for path in allow if not blocked_acl_target(path)]
    if _profile_exists(PROFILE_NAME) and report.get("mechanism") != "restricted-token":
        try:
            _ensure_grants(folders, data_root)
            return _spawn_windows(argv, env, cwd, data_root, folders, "appcontainer", web, stdio)
        except AclError:
            raise
        except OSError as exc:
            if not _restricted_ready():
                raise OSError(f"The AppContainer did not start ({exc}). The command was not run.") from exc
            _FALLBACK["mechanism"] = "restricted-token"
            _FALLBACK["reason"] = f"AppContainer launch failed: {exc}"
            _ensure_data_deny(data_root)
            _ensure_low_labels(folders)
            _audit_contain(None, None, _FALLBACK["reason"] + " The restricted token is the launch now.")
            return _spawn_windows(argv, env, cwd, data_root, folders, "restricted-token", web, stdio)
    _ensure_data_deny(data_root)
    _ensure_low_labels(folders)
    return _spawn_windows(argv, env, cwd, data_root, folders, "restricted-token", web, stdio)


def next_mechanism(appcontainer_error: str | None, restricted: bool) -> str:
    """Which launch to try. A failed AppContainer uses the restricted token."""
    if not appcontainer_error:
        return "appcontainer"
    if restricted:
        return "restricted-token"
    raise OSError(f"The AppContainer did not start ({appcontainer_error}). The command was not run.")


def _spawn_windows(argv, env, cwd, data_root: Path, allow: list[Path], name: str, web: bool, stdio: bool = False):
    """CreateProcess with an AppContainer, or a restricted token. No ACL changes here."""
    import ctypes

    del allow, data_root
    kernel32 = _dll("kernel32")
    if name == "appcontainer":
        sid = _container_sid(PROFILE_NAME, create=False)
        return _create_appcontainer(kernel32, argv, env, cwd, sid, web, stdio)
    sid = _deny_sid()
    return _create_restricted(kernel32, argv, env, cwd, sid, stdio)


def _container_sid(name: str = PROFILE_NAME, *, create: bool = False):
    """The profile SID. Creating one calls CreateAppContainerProfile even when Derive already returns a SID.

    DeriveAppContainerSidFromAppContainerName succeeds when no profile exists. Launching
    with that SID fails with Win32 error 2. Create, treat HRESULT 0x800700B7 as success, then derive.
    """
    import ctypes

    userenv = _dll("userenv")
    if create:
        _create_profile(userenv, name)
    sid = ctypes.c_void_p()
    hr = userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
    if not _hresult_ok(hr) or not sid.value:
        raise OSError(f"DeriveAppContainerSidFromAppContainerName failed ({_hr_text(hr)})")
    return sid


def _sid_from_text(text: str):
    """A SID Windows allocated. The caller keeps it for the life of the process."""
    import ctypes

    advapi = _dll("advapi32")
    sid = ctypes.c_void_p()
    if not advapi.ConvertStringSidToSidW(text, ctypes.byref(sid)) or not sid.value:
        raise _fail("ConvertStringSidToSidW")
    _PINNED.append(sid)
    return sid


def _fill_sid_attributes(sid_attr_type, sid_values):
    caps = (sid_attr_type * len(sid_values))()
    for index, sid in enumerate(sid_values):
        caps[index].Sid = sid
        caps[index].Attributes = SE_GROUP_ENABLED
    _PINNED.append(caps)
    return caps


def _create_profile(userenv, name: str) -> None:
    """Create the profile with network capabilities so Windows adds the firewall rules.

    An existing profile keeps the capabilities it was born with. Delete it and
    create it again so a profile from before this fix gains internetClient and
    privateNetworkClientServer. The SID comes from the name, so the grants stay.
    """
    import ctypes
    from ctypes import wintypes

    class _SidAttr(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    sids = [_sid_from_text(text) for text in NETWORK_CAPABILITY_SIDS]
    caps = _fill_sid_attributes(_SidAttr, sids)

    def once():
        created = ctypes.c_void_p()
        return userenv.CreateAppContainerProfile(
            name,
            "EasyAgent Bot",
            "EasyAgent tool process",
            ctypes.cast(caps, ctypes.c_void_p),
            len(sids),
            ctypes.byref(created),
        )

    hr = once()
    if _hresult_ok(hr):
        return
    if not _already_exists(hr):
        raise OSError(f"CreateAppContainerProfile failed ({_hr_text(hr)})")
    try:
        _delete_profile(name)
    except OSError:
        pass
    hr = once()
    if _hresult_ok(hr) or _already_exists(hr):
        return
    raise OSError(f"CreateAppContainerProfile failed ({_hr_text(hr)})")


def _profile_exists(name: str) -> bool:
    """True only when the profile is really on this computer.

    Derive succeeds with no profile, so it cannot answer this. The registry mapping
    or the Packages folder can.
    """
    if os.name != "nt":
        return False
    local = os.environ.get("LOCALAPPDATA") or ""
    if local and os.path.isdir(os.path.join(local, "Packages", name)):
        return True
    try:
        text = _sid_text(_container_sid(name, create=False))
    except OSError:
        return False
    if not text:
        return False
    return _mapping_exists(text)


def _mapping_exists(sid_text: str) -> bool:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MAPPINGS + "\\" + sid_text)
    except OSError:
        return False
    winreg.CloseKey(key)
    return True


def _delete_profile(name: str) -> None:
    userenv = _dll("userenv")
    hr = userenv.DeleteAppContainerProfile(name)
    if _hresult_ok(hr) or _hresult_code(hr) == _HRESULT_NOT_FOUND:
        return
    raise OSError(f"DeleteAppContainerProfile failed ({_hr_text(hr)})")


def _deny_sid():
    """A SID the parent token does not carry. The deny ACE names it, and the child token does too."""
    import ctypes
    from ctypes import wintypes

    advapi = _dll("advapi32")

    class _Authority(ctypes.Structure):
        _fields_ = [("Value", ctypes.c_byte * 6)]

    authority = _Authority()
    authority.Value[5] = 5
    sid = ctypes.c_void_p()
    advapi.AllocateAndInitializeSid.argtypes = [
        ctypes.POINTER(_Authority),
        ctypes.c_byte,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ok = advapi.AllocateAndInitializeSid(
        ctypes.byref(authority),
        1,
        0x0000EA13,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        ctypes.byref(sid),
    )
    if not ok or not sid.value:
        raise _fail("AllocateAndInitializeSid")
    _PINNED.append(sid)
    return sid


def _sid_text(sid) -> str:
    import ctypes
    from ctypes import wintypes

    advapi = _dll("advapi32")
    text = ctypes.c_wchar_p()
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise _fail("ConvertSidToStringSidW")
    value = text.value or ""
    _dll("kernel32").LocalFree(text)
    return value


def blocked_acl_target(path: Path) -> bool:
    """Keychain and DPAPI folders are never granted or denied by EasyAgent."""
    text = str(path).replace("/", "\\").lower()
    needles = (
        "\\microsoft\\credentials",
        "\\microsoft\\vault",
        "\\microsoft\\protect",
        "\\.easyagent",
    )
    return any(item in text for item in needles)


class _TRUSTEE_W(ctypes.Structure):
    """TRUSTEE_W. ptstrName holds a SID when TrusteeForm is TRUSTEE_IS_SID.

    Default alignment matches the Windows ABI: 32 bytes on 64-bit, 20 on 32-bit.
    """

    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", ctypes.c_void_p),
    ]


class _EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", ctypes.c_uint32),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", ctypes.c_uint32),
        ("Trustee", _TRUSTEE_W),
    ]


def _sid_bytes(text: str) -> bytes:
    """The binary SID for a string such as S-1-5-59923. Any well-formed SID, mapped or not."""
    parts = (text or "").split("-")
    if len(parts) < 3 or parts[0] != "S":
        raise AclError(0, f"The SID {text} is not well formed. The restricted token was not started.")
    try:
        revision = int(parts[1])
        authority = int(parts[2])
        subs = [int(part) for part in parts[3:]]
    except ValueError as exc:
        raise AclError(0, f"The SID {text} is not well formed. The restricted token was not started.") from exc
    if revision != 1 or authority < 0 or authority > 0xFFFFFFFFFFFF or len(subs) > 15:
        raise AclError(0, f"The SID {text} is not well formed. The restricted token was not started.")
    raw = bytearray(8 + 4 * len(subs))
    raw[0] = revision
    raw[1] = len(subs)
    raw[2:8] = authority.to_bytes(6, "big")
    for index, sub in enumerate(subs):
        if sub < 0 or sub > 0xFFFFFFFF:
            raise AclError(0, f"The SID {text} is not well formed. The restricted token was not started.")
        raw[8 + 4 * index : 12 + 4 * index] = sub.to_bytes(4, "little")
    return bytes(raw)


def _ace_sid_bytes(ace: bytes) -> bytes:
    """The SID inside an ACE. The tail may be padded, so the SID header decides the length."""
    if len(ace) < 10:
        return b""
    count = ace[9]
    need = 8 + 4 * count
    if 8 + need > len(ace):
        return b""
    return ace[8 : 8 + need]


def _pack_acl(aces: list[bytes], revision: int = ACL_REVISION) -> bytes:
    body = b"".join(aces)
    size = 8 + len(body)
    if size > 65535:
        raise AclError(0, "The ACL is too large. The restricted token was not started.")
    header = bytearray(8)
    header[0] = revision & 0xFF
    header[2:4] = size.to_bytes(2, "little")
    header[4:6] = len(aces).to_bytes(2, "little")
    return bytes(header) + body


def _iter_aces(blob: bytes) -> list[bytes]:
    if len(blob) < 8:
        raise AclError(0, "The DACL could not be read. The restricted token was not started.")
    count = int.from_bytes(blob[4:6], "little")
    limit = int.from_bytes(blob[2:4], "little")
    offset = 8
    found = []
    for _ in range(count):
        if offset + 4 > len(blob) or offset + 4 > limit:
            raise AclError(0, "The DACL could not be read. The restricted token was not started.")
        size = int.from_bytes(blob[offset + 2 : offset + 4], "little")
        if size < 8 or offset + size > len(blob) or offset + size > limit:
            raise AclError(0, "The DACL could not be read. The restricted token was not started.")
        found.append(blob[offset : offset + size])
        offset += size
    return found


def _acl_without_sid(blob: bytes, sid: bytes) -> bytes:
    """A new ACL with every ACE for this SID removed. Allow and deny both go."""
    kept = [ace for ace in _iter_aces(blob) if _ace_sid_bytes(ace) != sid]
    return _pack_acl(kept, blob[0] or ACL_REVISION)


def _ace_in_blob(blob: bytes, sid: bytes, ace_type: int, mask: int, inheritance: int) -> bool:
    """True when an explicit ACE in this ACL has this SID, type, mask, and inheritance."""
    inherit_bits = inheritance & (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)
    try:
        aces = _iter_aces(blob)
    except AclError:
        return False
    for ace in aces:
        kind = ace[0]
        flags = ace[1]
        ace_mask = int.from_bytes(ace[4:8], "little")
        if _ace_sid_bytes(ace) != sid or kind != ace_type or flags & INHERITED_ACE:
            continue
        if (flags & (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)) != inherit_bits:
            continue
        if (ace_mask & mask) != mask:
            continue
        return True
    return False


def _label_acl(sid: bytes) -> bytes:
    """One Low mandatory label: NO_WRITE_UP, inherited by files and folders."""
    ace_size = 8 + len(sid)
    ace = bytearray(ace_size)
    ace[0] = SYSTEM_MANDATORY_LABEL_ACE_TYPE
    ace[1] = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
    ace[2:4] = ace_size.to_bytes(2, "little")
    ace[4:8] = SYSTEM_MANDATORY_LABEL_NO_WRITE_UP.to_bytes(4, "little")
    ace[8:] = sid
    return _pack_acl([bytes(ace)])


def _mask_for(rights: str) -> int:
    """F, M, RX, R, W, X, and combinations. Inheritance marks are not part of the mask.

    ``(X)`` is traverse. The last character of that string is a parenthesis, so the
    mask cannot be decided by the final character.
    """
    text = (rights or "").upper().replace(" ", "")
    for mark in _INHERIT_MARKS:
        text = text.replace(mark, "")
    text = text.replace("(", "").replace(")", "")
    if not text:
        raise AclError(0, f"Unknown ACE rights {rights}. The restricted token was not started.")
    mask = 0
    for part in text.split(","):
        if not part:
            raise AclError(0, f"Unknown ACE rights {rights}. The restricted token was not started.")
        mask |= _one_mask(part, rights)
    return mask


def _one_mask(part: str, original: str) -> int:
    mask = 0
    rest = part
    while rest:
        matched = False
        for name, value in _RIGHT_NAMES:
            if rest.startswith(name):
                mask |= value
                rest = rest[len(name) :]
                matched = True
                break
        if not matched:
            raise AclError(0, f"Unknown ACE rights {original}. The restricted token was not started.")
    return mask


def _inherit_for(rights: str) -> int:
    text = (rights or "").upper()
    flags = 0
    if "(OI)" in text:
        flags |= OBJECT_INHERIT_ACE
    if "(CI)" in text:
        flags |= CONTAINER_INHERIT_ACE
    return flags


def _as_sid(sid):
    import ctypes

    if isinstance(sid, ctypes.c_void_p):
        return sid
    return ctypes.c_void_p(int(sid))


def _free(pointer) -> None:
    import ctypes

    value = pointer
    if not isinstance(pointer, int):
        value = int(getattr(pointer, "value", 0) or 0)
    if value:
        _dll("kernel32").LocalFree(value)


def _acl_bytes(pointer) -> bytes:
    import ctypes

    address = pointer if isinstance(pointer, int) else int(getattr(pointer, "value", 0) or 0)
    if not address:
        raise AclError(0, "The DACL was empty. The restricted token was not started.")
    header = ctypes.string_at(address, 8)
    size = int.from_bytes(header[2:4], "little")
    if size < 8 or size > 65535:
        raise AclError(0, "The DACL could not be read. The restricted token was not started.")
    return ctypes.string_at(address, size)


def _clear_inherited(blob: bytes) -> bytes:
    """Copy inherited ACEs onto the object, then the caller marks the DACL protected."""
    raw = bytearray(blob)
    count = int.from_bytes(raw[4:6], "little")
    offset = 8
    for _ in range(count):
        if offset + 4 > len(raw):
            raise AclError(0, "The DACL could not be copied. The restricted token was not started.")
        size = int.from_bytes(raw[offset + 2 : offset + 4], "little")
        if size < 4 or offset + size > len(raw):
            raise AclError(0, "The DACL could not be copied. The restricted token was not started.")
        raw[offset + 1] = raw[offset + 1] & ~INHERITED_ACE
        offset += size
    return bytes(raw)


def _read_dacl(advapi, path: Path):
    """The current DACL. A missing DACL raises instead of replacing the ACL with one ACE."""
    import ctypes
    from ctypes import wintypes

    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    sacl = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    name = ctypes.create_unicode_buffer(str(path))
    err = int(
        advapi.GetNamedSecurityInfoW(
            name,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            ctypes.byref(sacl),
            ctypes.byref(sd),
        )
        or 0
    )
    if err:
        raise _acl_fail("GetNamedSecurityInfoW", err)
    if not sd.value or not dacl.value:
        if sd.value:
            _free(sd)
        raise AclError(0, f"{path} has no DACL to update. The restricted token was not started.")
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not advapi.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
        _free(sd)
        raise _acl_fail("GetSecurityDescriptorControl", _last_error())
    protected = bool(int(control.value) & SE_DACL_PROTECTED)
    return sd, dacl, protected


def _merge_ace(advapi, old_dacl, sid, mode: int, mask: int, inheritance: int):
    import ctypes

    entry = _EXPLICIT_ACCESS_W()
    entry.grfAccessPermissions = mask
    entry.grfAccessMode = mode
    entry.grfInheritance = inheritance
    entry.Trustee.pMultipleTrustee = None
    entry.Trustee.MultipleTrusteeOperation = NO_MULTIPLE_TRUSTEE
    entry.Trustee.TrusteeForm = TRUSTEE_IS_SID
    entry.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
    entry.Trustee.ptstrName = _as_sid(sid)
    new_acl = ctypes.c_void_p()
    err = int(advapi.SetEntriesInAclW(1, ctypes.byref(entry), old_dacl, ctypes.byref(new_acl)) or 0)
    if err or not new_acl.value:
        raise _acl_fail("SetEntriesInAclW", err)
    return new_acl


def _set_dacl(advapi, path: Path, acl, info: int) -> None:
    _set_security(advapi, path, info, acl, None)


def _set_security(advapi, path: Path, info: int, dacl, sacl) -> None:
    import ctypes

    name = ctypes.create_unicode_buffer(str(path))
    err = int(
        advapi.SetNamedSecurityInfoW(
            name,
            SE_FILE_OBJECT,
            info,
            None,
            None,
            dacl,
            sacl,
        )
        or 0
    )
    if err:
        raise _acl_fail("SetNamedSecurityInfoW", err)


def _read_label(advapi, path: Path):
    """The mandatory-label ACL. A folder with no label has a null SACL."""
    import ctypes

    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    sacl = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    name = ctypes.create_unicode_buffer(str(path))
    err = int(
        advapi.GetNamedSecurityInfoW(
            name,
            SE_FILE_OBJECT,
            LABEL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            ctypes.byref(sacl),
            ctypes.byref(sd),
        )
        or 0
    )
    if err:
        raise _acl_fail("GetNamedSecurityInfoW", err)
    if not sd.value:
        raise AclError(0, f"{path} has no security descriptor. The restricted token was not started.")
    return sd, sacl


def _aces_without_sid(advapi, acl, sid) -> bytes:
    """Walk GetAce and copy every ACE whose SID does not match. Allow and deny both drop."""
    import ctypes

    blob = _acl_bytes(acl)
    kept = []
    wanted = _as_sid(sid)
    for index, ace_bytes in enumerate(_iter_aces(blob)):
        ace = ctypes.c_void_p()
        if not advapi.GetAce(acl, index, ctypes.byref(ace)) or not ace.value:
            raise _acl_fail("GetAce", _last_error())
        _kind, _flags, _mask, sid_at = _ace_at(ace)
        if advapi.EqualSid(sid_at, wanted):
            continue
        kept.append(ace_bytes)
    return _pack_acl(kept, blob[0] or ACL_REVISION)


def _sid_remains(advapi, acl, sid) -> bool:
    import ctypes

    if acl is None or not getattr(acl, "value", None):
        return False
    blob = _acl_bytes(acl)
    wanted = _as_sid(sid)
    for index, _ace_bytes in enumerate(_iter_aces(blob)):
        ace = ctypes.c_void_p()
        if not advapi.GetAce(acl, index, ctypes.byref(ace)) or not ace.value:
            raise _acl_fail("GetAce", _last_error())
        _kind, _flags, _mask, sid_at = _ace_at(ace)
        if advapi.EqualSid(sid_at, wanted):
            return True
    return False


def _label_matches(advapi, sacl, sid) -> bool:
    import ctypes

    if sacl is None or not getattr(sacl, "value", None):
        return False
    blob = _acl_bytes(sacl)
    wanted = _as_sid(sid)
    inherit = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
    for index, _ace_bytes in enumerate(_iter_aces(blob)):
        ace = ctypes.c_void_p()
        if not advapi.GetAce(sacl, index, ctypes.byref(ace)) or not ace.value:
            raise _acl_fail("GetAce", _last_error())
        kind, flags, mask, sid_at = _ace_at(ace)
        if kind != SYSTEM_MANDATORY_LABEL_ACE_TYPE or flags & INHERITED_ACE:
            continue
        if not advapi.EqualSid(sid_at, wanted):
            continue
        if (flags & inherit) != inherit:
            continue
        if (mask & SYSTEM_MANDATORY_LABEL_NO_WRITE_UP) != SYSTEM_MANDATORY_LABEL_NO_WRITE_UP:
            continue
        return True
    return False


def _access_denied(exc: BaseException) -> bool:
    if getattr(exc, "winerror", None) == ERROR_ACCESS_DENIED or getattr(exc, "errno", None) == ERROR_ACCESS_DENIED:
        return True
    return "Win32 5" in str(exc)


def _process_token(access: int):
    import ctypes
    from ctypes import wintypes

    kernel32 = _dll("kernel32")
    advapi = _dll("advapi32")
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel32.GetCurrentProcess(), access, ctypes.byref(token)):
        raise _fail("OpenProcessToken")
    return advapi, token


def _enable_take_ownership(advapi, token) -> None:
    """Turn on SeTakeOwnershipPrivilege when this token has it. A standard user often does not."""
    import ctypes
    from ctypes import wintypes

    class _Luid(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class _LuidAttr(ctypes.Structure):
        _fields_ = [("Luid", _Luid), ("Attributes", wintypes.DWORD)]

    class _Privileges(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", _LuidAttr * 1)]

    luid = _Luid()
    if not advapi.LookupPrivilegeValueW(None, "SeTakeOwnershipPrivilege", ctypes.byref(luid)):
        raise _fail("LookupPrivilegeValueW")
    privs = _Privileges()
    privs.PrivilegeCount = 1
    privs.Privileges[0].Luid = luid
    privs.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
    if not advapi.AdjustTokenPrivileges(token, False, ctypes.byref(privs), 0, None, None):
        raise _fail("AdjustTokenPrivileges")
    if _last_error() == 1300:
        raise OSError(1300, "AdjustTokenPrivileges failed (Win32 1300)")


def _set_owner(advapi, folder: Path, sid) -> None:
    import ctypes

    name = ctypes.create_unicode_buffer(str(folder))
    err = int(
        advapi.SetNamedSecurityInfoW(
            name,
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION,
            _as_sid(sid),
            None,
            None,
            None,
        )
        or 0
    )
    if err:
        raise _acl_fail("SetNamedSecurityInfoW", err)


def _claim_workspace(folder: Path) -> None:
    """This user owns a workspace EasyAgent created. Grant Full control so the Low label can be written.

    Modify does not include WRITE_OWNER. The owner can still rewrite the DACL. Taking ownership
    is only the step before that, and only for this folder.
    """
    if blocked_acl_target(folder):
        raise AclError(ERROR_ACCESS_DENIED, f"{folder} is a keychain folder. The restricted token was not started.")
    advapi, token = _process_token(TOKEN_QUERY | TOKEN_ADJUST_PRIVILEGES)
    try:
        user = _token_user_sid(advapi, token)
        try:
            _set_owner(advapi, folder, user)
        except OSError:
            try:
                _enable_take_ownership(advapi, token)
                _set_owner(advapi, folder, user)
            except OSError:
                pass
        _apply_ace(user, folder, GRANT_ACCESS, FILE_ALL_ACCESS, OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)
    finally:
        _dll("kernel32").CloseHandle(token)


def _apply_low_label(folder: Path) -> None:
    sid = _sid_from_text(LOW_INTEGRITY_SID)
    blob = _label_acl(_sid_bytes(LOW_INTEGRITY_SID))
    advapi = _dll("advapi32")
    buf = ctypes.create_string_buffer(blob)
    _set_security(advapi, folder, LABEL_SECURITY_INFORMATION, None, ctypes.cast(buf, ctypes.c_void_p))
    sd, sacl = _read_label(advapi, folder)
    try:
        if not _label_matches(advapi, sacl, sid):
            raise AclError(0, f"The Low label on {folder} was not on the folder after it was written. The restricted token was not started.")
    finally:
        _free(sd)


def _set_low_label(folder: Path) -> None:
    """Low integrity, NO_WRITE_UP, inherited. A Low token cannot write up to a Medium folder.

    A folder this user created can still lack WRITE_OWNER when the parent only granted Modify.
    Take ownership, grant this user Full control, and try the label once more. A second denial
    refuses the restricted launch.
    """
    folder = Path(folder)
    if not folder.exists():
        raise AclError(0, f"{folder} is not there. The restricted token was not started.")
    try:
        _apply_low_label(folder)
    except AclError as exc:
        if not _access_denied(exc):
            raise
        try:
            _claim_workspace(folder)
            _apply_low_label(folder)
        except OSError as again:
            code = int(getattr(again, "errno", 0) or ERROR_ACCESS_DENIED)
            raise AclError(
                code,
                f"Could not set the Low label on {folder} (Win32 {code}). "
                "This user cannot own that workspace, so the restricted token was not started.",
            ) from again


def _remove_low_label(folder: Path) -> None:
    """Drop the Low label and read the SACL back. Zero ACEs for that SID must remain."""
    sid = _sid_from_text(LOW_INTEGRITY_SID)
    advapi = _dll("advapi32")
    sd, sacl = _read_label(advapi, folder)
    try:
        if not sacl.value or not _sid_remains(advapi, sacl, sid):
            return
        rebuilt = _aces_without_sid(advapi, sacl, sid)
    finally:
        _free(sd)
    buf = ctypes.create_string_buffer(rebuilt)
    try:
        _set_security(advapi, folder, LABEL_SECURITY_INFORMATION, None, ctypes.cast(buf, ctypes.c_void_p))
    except AclError:
        if int.from_bytes(rebuilt[4:6], "little") != 0:
            raise
        # An empty label ACL is rejected on some Windows versions. NULL clears it.
        _set_security(advapi, folder, LABEL_SECURITY_INFORMATION, None, None)
    sd, sacl = _read_label(advapi, folder)
    try:
        if sacl.value and _sid_remains(advapi, sacl, sid):
            raise AclError(0, f"The Low label on {folder} is still there. The restricted token was not started.")
    finally:
        _free(sd)


def _remove_sid_aces(path: Path, sid) -> None:
    """Drop every explicit allow and deny for this SID, then read the DACL back."""
    advapi = _dll("advapi32")
    sd, dacl, protected = _read_dacl(advapi, path)
    try:
        rebuilt = _aces_without_sid(advapi, dacl, sid)
        buf = ctypes.create_string_buffer(rebuilt)
        info = DACL_SECURITY_INFORMATION
        if protected:
            info |= PROTECTED_DACL_SECURITY_INFORMATION
        _set_security(advapi, path, info, ctypes.cast(buf, ctypes.c_void_p), None)
    finally:
        _free(sd)
    sd, dacl, _protected = _read_dacl(advapi, path)
    try:
        if _sid_remains(advapi, dacl, sid):
            raise AclError(0, f"The ACE on {path} is still there. The restricted token was not started.")
    finally:
        _free(sd)


def _ace_at(ace):
    import ctypes

    address = int(ace.value or 0)
    if not address:
        raise AclError(0, "An ACE on the DACL could not be read. The restricted token was not started.")
    raw = ctypes.string_at(address, 8)
    kind = raw[0]
    flags = raw[1]
    size = int.from_bytes(raw[2:4], "little")
    if size < 8 or size > 65535:
        raise AclError(0, "An ACE on the DACL could not be read. The restricted token was not started.")
    full = ctypes.string_at(address, size)
    ace_mask = int.from_bytes(full[4:8], "little")
    return kind, flags, ace_mask, ctypes.c_void_p(address + 8)


def _dacl_has(advapi, dacl, sid, mode: int, mask: int, inheritance: int) -> bool:
    import ctypes

    count = int.from_bytes(_acl_bytes(dacl)[4:6], "little")
    want = ACCESS_DENIED_ACE_TYPE if mode == DENY_ACCESS else ACCESS_ALLOWED_ACE_TYPE
    inherit_bits = inheritance & (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)
    wanted = _as_sid(sid)
    for index in range(count):
        ace = ctypes.c_void_p()
        if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
            raise _acl_fail("GetAce", _last_error())
        kind, flags, ace_mask, sid_at = _ace_at(ace)
        if not advapi.EqualSid(sid_at, wanted):
            continue
        if mode == REVOKE_ACCESS:
            return True
        if kind != want or flags & INHERITED_ACE:
            continue
        if (flags & (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)) != inherit_bits:
            continue
        if (ace_mask & mask) != mask:
            continue
        return True
    return False


def _verify_ace(advapi, path: Path, sid, mode: int, mask: int, inheritance: int) -> None:
    sd, dacl, _protected = _read_dacl(advapi, path)
    try:
        found = _dacl_has(advapi, dacl, sid, mode, mask, inheritance)
    finally:
        _free(sd)
    if mode == REVOKE_ACCESS:
        if found:
            raise AclError(0, f"The ACE on {path} is still there. The restricted token was not started.")
        return
    if not found:
        raise AclError(0, f"The ACE on {path} was not on the DACL after it was written. The restricted token was not started.")


def _apply_ace(sid, path: Path, mode: int, mask: int, inheritance: int) -> None:
    """Merge one ACE and read the DACL back. A miss raises, and the caller does not launch."""
    advapi = _dll("advapi32")
    sd, dacl, protected = _read_dacl(advapi, path)
    try:
        new_acl = _merge_ace(advapi, dacl, sid, mode, mask, inheritance)
        try:
            info = DACL_SECURITY_INFORMATION
            if protected:
                info |= PROTECTED_DACL_SECURITY_INFORMATION
            _set_dacl(advapi, path, new_acl, info)
        finally:
            _free(new_acl)
    finally:
        _free(sd)
    _verify_ace(advapi, path, sid, mode, mask, inheritance)


def _verify_protected(advapi, path: Path) -> None:
    sd, _dacl, protected = _read_dacl(advapi, path)
    _free(sd)
    if not protected:
        raise AclError(0, f"{path} is still inheriting ACEs. The restricted token was not started.")


def _prepare_folder(folder: Path) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AclError(getattr(exc, "errno", 0) or 0, f"Could not create {folder}. The restricted token was not started.") from exc


def _grant_rights(sid, folder: Path, rights: str) -> None:
    folder = Path(folder)
    if blocked_acl_target(folder):
        return
    _prepare_folder(folder)
    _apply_ace(sid, folder, GRANT_ACCESS, _mask_for(rights), _inherit_for(rights))


def _grant_sid(sid, folders: list[Path]) -> None:
    for folder in folders:
        folder = Path(folder)
        if blocked_acl_target(folder):
            continue
        _prepare_folder(folder)
        _apply_ace(sid, folder, GRANT_ACCESS, FILE_MODIFY, OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)
        try:
            _set_low_label(folder)
        except OSError:
            try:
                _remove_sid_aces(folder, sid)
            except OSError:
                pass
            raise


def _protect_folder(folder: Path) -> None:
    """Stop a later deny on the data folder from inheriting onto a granted folder."""
    import ctypes

    folder = Path(folder)
    if blocked_acl_target(folder):
        raise AclError(0, f"{folder} is a keychain folder. The restricted token was not started.")
    _prepare_folder(folder)
    advapi = _dll("advapi32")
    sd, dacl, _protected = _read_dacl(advapi, folder)
    try:
        explicit = _clear_inherited(_acl_bytes(dacl))
        buf = ctypes.create_string_buffer(explicit)
        _set_dacl(
            advapi,
            folder,
            ctypes.cast(buf, ctypes.c_void_p),
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
        )
    finally:
        _free(sd)
    _verify_protected(advapi, folder)


def _deny_file(sid, path: Path) -> None:
    """One file, no inheritance. The user's own access is unchanged."""
    path = Path(path)
    if blocked_acl_target(path):
        raise AclError(0, "The deny was not applied to a keychain folder. The restricted token was not started.")
    if not path.exists():
        raise AclError(0, f"{path} is not there. The restricted token was not started.")
    _apply_ace(sid, path, DENY_ACCESS, FILE_ALL_ACCESS, 0)


def _deny_sid_on(sid, folder: Path) -> None:
    folder = Path(folder)
    if blocked_acl_target(folder):
        raise AclError(0, "The deny was not applied to a keychain folder. The restricted token was not started.")
    if not folder.exists():
        raise AclError(0, f"{folder} is not there. The restricted token was not started.")
    _apply_ace(sid, folder, DENY_ACCESS, FILE_ALL_ACCESS, OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)


class _PipeProcess:
    """A CreateProcess handle with the same communicate() shape as Popen."""

    def __init__(self, process, pid, stdout_read, stderr_read) -> None:
        self._process = process
        self.returncode: int | None = None
        self.pid = int(pid)
        self._out: list[str] = []
        self._err: list[str] = []
        self._threads = [
            threading.Thread(target=self._drain, args=(stdout_read, self._out), daemon=True),
            threading.Thread(target=self._drain, args=(stderr_read, self._err), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def communicate(self, timeout=None):
        import ctypes
        from ctypes import wintypes

        kernel32 = _dll("kernel32")
        waited = kernel32.WaitForSingleObject(self._process, int((timeout or 30) * 1000))
        if waited != 0:
            raise subprocess.TimeoutExpired("contained", timeout or 30)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(self._process, ctypes.byref(code))
        self.returncode = int(code.value)
        for thread in self._threads:
            thread.join(timeout=2)
        return "".join(self._out), "".join(self._err)

    def kill(self) -> None:
        import ctypes

        _dll("kernel32").TerminateProcess(self._process, 1)

    def wait(self, timeout=None):
        self.communicate(timeout=timeout)
        return self.returncode

    def poll(self):
        return self.returncode

    def _drain(self, handle, sink: list[str]) -> None:
        sink.append(self._read(handle))

    @staticmethod
    def _read(handle) -> str:
        import ctypes
        from ctypes import wintypes

        if not handle:
            return ""
        kernel32 = _dll("kernel32")
        chunks: list[bytes] = []
        buf = ctypes.create_string_buffer(4096)
        read = wintypes.DWORD()
        while kernel32.ReadFile(handle, buf, 4096, ctypes.byref(read), None) and read.value:
            chunks.append(buf.raw[: read.value])
        return b"".join(chunks).decode("utf-8", errors="replace")


class _StdioProcess:
    """CreateProcess handles with binary stdin, stdout, and stderr for an MCP server."""

    def __init__(self, process, pid, stdin_write, stdout_read, stderr_read) -> None:
        import msvcrt

        self._process = process
        self.returncode: int | None = None
        self.pid = int(pid)
        self.stdin = os.fdopen(msvcrt.open_osfhandle(int(stdin_write), os.O_BINARY), "wb", buffering=0)
        self.stdout = os.fdopen(msvcrt.open_osfhandle(int(stdout_read), os.O_BINARY), "rb", buffering=0)
        self.stderr = os.fdopen(msvcrt.open_osfhandle(int(stderr_read), os.O_BINARY), "rb", buffering=0)

    def terminate(self) -> None:
        self.kill()

    def kill(self) -> None:
        import ctypes

        _dll("kernel32").TerminateProcess(self._process, 1)
        self.returncode = 1

    def wait(self, timeout=None):
        import ctypes
        from ctypes import wintypes

        kernel32 = _dll("kernel32")
        limit = 30 if timeout is None else timeout
        waited = kernel32.WaitForSingleObject(self._process, int(float(limit) * 1000))
        if waited != 0:
            raise subprocess.TimeoutExpired("contained", limit)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(self._process, ctypes.byref(code))
        self.returncode = int(code.value)
        return self.returncode


def _stdio_handles():
    """stdin read is inherited. stdout and stderr reads stay with the parent."""
    import ctypes

    in_read, in_write = _pipe()
    out_read, out_write = _pipe()
    err_read, err_write = _pipe()
    kernel32 = _dll("kernel32")
    kernel32.SetHandleInformation.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    kernel32.SetHandleInformation(in_read, 1, 1)
    kernel32.SetHandleInformation(in_write, 1, 0)
    return (in_read, in_write), (out_read, out_write), (err_read, err_write)


def _create_stdio_attribute(kernel32, argv, env, cwd, attribute, value_ptr, value_size):
    """Same security attribute as the shell, with a stdin pipe the connector can read."""
    import ctypes
    from ctypes import wintypes

    class _Startup(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _StartupEx(ctypes.Structure):
        _fields_ = [("StartupInfo", _Startup), ("lpAttributeList", ctypes.c_void_p)]

    size = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attrs = ctypes.create_string_buffer(size.value)
    if not kernel32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
        raise _fail("InitializeProcThreadAttributeList")
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    if not kernel32.UpdateProcThreadAttribute(attrs, 0, attribute, value_ptr, value_size, None, None):
        err = _last_error()
        raise OSError(err, f"UpdateProcThreadAttribute failed (attribute {attribute:#x}, Win32 {err}).")
    (in_read, in_write), (out_read, out_write), (err_read, err_write) = _stdio_handles()
    info = _StartupEx()
    info.StartupInfo.cb = ctypes.sizeof(info)
    info.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
    info.StartupInfo.dwFlags = 0x00000100
    info.StartupInfo.hStdInput = in_read
    info.StartupInfo.hStdOutput = out_write
    info.StartupInfo.hStdError = err_write
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
    flags = 0x00080000 | 0x00000400

    class _ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    created = _ProcessInfo()
    env_block = _env_block(env)
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    ok = kernel32.CreateProcessW(
        None,
        command,
        None,
        None,
        True,
        flags,
        env_block,
        cwd,
        ctypes.byref(info),
        ctypes.byref(created),
    )
    err = _last_error() if not ok else 0
    kernel32.CloseHandle(in_read)
    kernel32.CloseHandle(out_write)
    kernel32.CloseHandle(err_write)
    if not ok:
        raise _fail("CreateProcessW", err)
    return _StdioProcess(created.hProcess, int(created.dwProcessId), in_write, out_read, err_read)


def _create_stdio_as_user(advapi, token, command, env, cwd):
    import ctypes
    from ctypes import wintypes

    kernel32 = _dll("kernel32")

    class _Startup(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    (in_read, in_write), (out_read, out_write), (err_read, err_write) = _stdio_handles()
    info = _Startup()
    info.cb = ctypes.sizeof(info)
    info.dwFlags = 0x00000100
    info.hStdInput = in_read
    info.hStdOutput = out_write
    info.hStdError = err_write
    created = _ProcessInfo()
    command_buf = command if isinstance(command, ctypes.Array) else ctypes.create_unicode_buffer(str(command))
    env_block = _env_block(env)
    advapi.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi.CreateProcessAsUserW.restype = wintypes.BOOL
    ok = advapi.CreateProcessAsUserW(
        token,
        None,
        command_buf,
        None,
        None,
        True,
        0x08000000 | 0x00000400,
        env_block,
        cwd,
        ctypes.byref(info),
        ctypes.byref(created),
    )
    err = _last_error() if not ok else 0
    kernel32.CloseHandle(in_read)
    kernel32.CloseHandle(out_write)
    kernel32.CloseHandle(err_write)
    if not ok:
        raise _fail("CreateProcessAsUserW", err)
    return _StdioProcess(created.hProcess, int(created.dwProcessId), in_write, out_read, err_read)


def _create_appcontainer(kernel32, argv, env, cwd, sid, web: bool, stdio: bool = False):
    import ctypes
    from ctypes import wintypes

    class _SidAttr(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _Caps(ctypes.Structure):
        _fields_ = [
            ("AppContainerSid", ctypes.c_void_p),
            ("Capabilities", ctypes.POINTER(_SidAttr)),
            ("CapabilityCount", wintypes.DWORD),
            ("Reserved", wintypes.DWORD),
        ]

    security = _Caps()
    security.AppContainerSid = sid
    caps_array = None
    if web:
        # The same two SIDs the profile was created with. Loopback is not exempted.
        granted = [_sid_from_text(text) for text in NETWORK_CAPABILITY_SIDS]
        caps_array = _fill_sid_attributes(_SidAttr, granted)
        security.Capabilities = ctypes.cast(caps_array, ctypes.POINTER(_SidAttr))
        security.CapabilityCount = len(granted)
    else:
        security.CapabilityCount = 0
    _PINNED.append(security)
    if caps_array is not None:
        _PINNED.append(caps_array)
    if stdio:
        return _create_stdio_attribute(
            kernel32, argv, env, cwd, SECURITY_CAPABILITIES, ctypes.byref(security), ctypes.sizeof(security)
        )
    return _create_with_attribute(
        kernel32, argv, env, cwd, SECURITY_CAPABILITIES, ctypes.byref(security), ctypes.sizeof(security)
    )


def _create_restricted(kernel32, argv, env, cwd, sid, stdio: bool = False):
    import ctypes
    from ctypes import wintypes

    # The pseudo-handle is pointer-sized (-1). Without a HANDLE restype, ctypes
    # truncates it to 32 bits and OpenProcessToken fails with Win32 error 6.
    bind_signatures(kernel32, "kernel32")
    advapi = _dll("advapi32")
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel32.GetCurrentProcess(), 0x02000000, ctypes.byref(token)):
        raise _fail("OpenProcessToken")
    user_sid = _token_user_sid(advapi, token)
    logon = _logon_sid(advapi, token)

    class _SidAttr(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    values = _restricting_sid_values(user_sid, sid, logon)
    restrict = (_SidAttr * len(values))()
    for index, item in enumerate(values):
        restrict[index].Sid = item
        restrict[index].Attributes = 0
    _PINNED.append(restrict)
    restricted = wintypes.HANDLE()
    advapi.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_SidAttr),
        ctypes.POINTER(wintypes.HANDLE),
    ]
    # DISABLE_MAX_PRIVILEGE. Restricting SIDs must still be able to load system DLLs.
    if not advapi.CreateRestrictedToken(
        token, 0x1, 0, None, 0, None, len(values), restrict, ctypes.byref(restricted)
    ):
        raise _fail("CreateRestrictedToken")
    _low_integrity(advapi, restricted)
    command = subprocess.list2cmdline(list(argv))
    if stdio:
        return _create_stdio_as_user(advapi, restricted, command, env, cwd)
    return _CreateProcessAsUser(advapi, restricted, command, env, cwd)


def _restricting_sid_values(user_sid, deny_sid, logon_sid):
    """User, the deny SID, Everyone, Users, RESTRICTED, and the logon SID when the token has one."""
    sids = [user_sid, deny_sid]
    for text in RESTRICTING_SIDS:
        sids.append(_sid_from_text(text))
    if logon_sid:
        sids.append(logon_sid)
    return sids


def _logon_sid(advapi, token):
    """The logon SID (SE_GROUP_LOGON_ID). The desktop ACL names it."""
    import ctypes
    from ctypes import wintypes

    needed = wintypes.DWORD(0)
    advapi.GetTokenInformation(token, 2, None, 0, ctypes.byref(needed))
    if needed.value < 4:
        return None
    buf = ctypes.create_string_buffer(needed.value)

    class _SidAttr(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _Groups(ctypes.Structure):
        _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", _SidAttr * 1)]

    if not advapi.GetTokenInformation(token, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
        raise _fail("GetTokenInformation")
    view = ctypes.cast(buf, ctypes.POINTER(_Groups)).contents
    sid_size = ctypes.sizeof(_SidAttr)
    offset = _Groups.Groups.offset
    available = max((len(buf) - offset) // sid_size, 0)
    count = min(int(view.GroupCount), available)
    for index in range(count):
        group = ctypes.cast(
            ctypes.addressof(buf) + offset + index * sid_size,
            ctypes.POINTER(_SidAttr),
        ).contents
        if int(group.Attributes) & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID:
            _PINNED.append(buf)
            return group.Sid
    return None


def _low_integrity(advapi, token) -> None:
    """SetTokenInformation(TokenIntegrityLevel) to the Low label S-1-16-4096."""
    import ctypes
    from ctypes import wintypes

    class _SidAttr(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _Label(ctypes.Structure):
        _fields_ = [("Label", _SidAttr)]

    label = _Label()
    label.Label.Sid = _sid_from_text(LOW_INTEGRITY_SID)
    label.Label.Attributes = SE_GROUP_INTEGRITY
    _PINNED.append(label)
    if not advapi.SetTokenInformation(
        token,
        TOKEN_INTEGRITY_LEVEL,
        ctypes.cast(ctypes.byref(label), ctypes.c_void_p),
        ctypes.sizeof(label),
    ):
        raise _fail("SetTokenInformation")


def _well_known_sid(kind: int):
    import ctypes
    from ctypes import wintypes

    advapi = _dll("advapi32")
    size = wintypes.DWORD(68)
    buf = ctypes.create_string_buffer(68)
    advapi.CreateWellKnownSid.argtypes = [
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    if not advapi.CreateWellKnownSid(kind, None, buf, ctypes.byref(size)):
        raise _fail("CreateWellKnownSid")
    sid = ctypes.c_void_p()
    advapi.ConvertSidToStringSidW  # keep the import used
    copied = ctypes.create_string_buffer(buf.raw[: size.value])
    _PINNED.append(copied)
    _PINNED.append(buf)
    return ctypes.cast(copied, ctypes.c_void_p)


def _create_with_attribute(kernel32, argv, env, cwd, attribute, value_ptr, value_size):
    import ctypes
    from ctypes import wintypes

    class _Startup(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _StartupEx(ctypes.Structure):
        _fields_ = [("StartupInfo", _Startup), ("lpAttributeList", ctypes.c_void_p)]

    size = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attrs = ctypes.create_string_buffer(size.value)
    if not kernel32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
        raise _fail("InitializeProcThreadAttributeList")
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    if not kernel32.UpdateProcThreadAttribute(attrs, 0, attribute, value_ptr, value_size, None, None):
        err = _last_error()
        raise OSError(err, f"UpdateProcThreadAttribute failed (attribute {attribute:#x}, Win32 {err}).")
    info = _StartupEx()
    info.StartupInfo.cb = ctypes.sizeof(info)
    info.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
    out_read, out_write = _pipe()
    err_read, err_write = _pipe()
    info.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
    info.StartupInfo.hStdOutput = out_write
    info.StartupInfo.hStdError = err_write
    info.StartupInfo.hStdInput = None
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
    flags = 0x00080000 | 0x00000400  # EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
    class _ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    created = _ProcessInfo()
    env_block = _env_block(env)
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    ok = kernel32.CreateProcessW(
        None,
        command,
        None,
        None,
        True,
        flags,
        env_block,
        cwd,
        ctypes.byref(info),
        ctypes.byref(created),
    )
    err = _last_error() if not ok else 0
    kernel32.CloseHandle(out_write)
    kernel32.CloseHandle(err_write)
    if not ok:
        raise _fail("CreateProcessW", err)
    return _PipeProcess(created.hProcess, int(created.dwProcessId), out_read, err_read)


def _CreateProcessAsUser(advapi, token, command, env, cwd):
    import ctypes
    from ctypes import wintypes

    kernel32 = _dll("kernel32")

    class _Startup(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    info = _Startup()
    info.cb = ctypes.sizeof(info)
    out_read, out_write = _pipe()
    err_read, err_write = _pipe()
    info.dwFlags = 0x00000100
    info.hStdOutput = out_write
    info.hStdError = err_write
    created = _ProcessInfo()
    command_buf = command if isinstance(command, ctypes.Array) else ctypes.create_unicode_buffer(str(command))
    env_block = _env_block(env)
    advapi.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi.CreateProcessAsUserW.restype = wintypes.BOOL
    ok = advapi.CreateProcessAsUserW(
        token,
        None,
        command_buf,
        None,
        None,
        True,
        0x08000000 | 0x00000400,
        env_block,
        cwd,
        ctypes.byref(info),
        ctypes.byref(created),
    )
    err = _last_error() if not ok else 0
    kernel32.CloseHandle(out_write)
    kernel32.CloseHandle(err_write)
    if not ok:
        raise _fail("CreateProcessAsUserW", err)
    return _PipeProcess(created.hProcess, int(created.dwProcessId), out_read, err_read)


def _token_user_sid(advapi, token):
    import ctypes
    from ctypes import wintypes

    class _SidAttr(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("User", _SidAttr)]

    needed = wintypes.DWORD(0)
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
    buf = ctypes.create_string_buffer(max(needed.value, 64))
    if not advapi.GetTokenInformation(token, 1, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
        raise _fail("GetTokenInformation")
    _PINNED.append(buf)
    user = ctypes.cast(buf, ctypes.POINTER(_TokenUser)).contents
    return user.User.Sid


def _pipe():
    import ctypes
    from ctypes import wintypes

    kernel32 = _dll("kernel32")
    class _Security(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    security = _Security()
    security.nLength = ctypes.sizeof(security)
    security.bInheritHandle = True
    read = wintypes.HANDLE()
    write = wintypes.HANDLE()
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    if not kernel32.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 0):
        raise _fail("CreatePipe")
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.SetHandleInformation(read, 1, 0)
    return read, write


def _env_block(env: dict):
    import ctypes

    parts = []
    for key, value in sorted((env or {}).items()):
        parts.append(f"{key}={value}")
    text = "\0".join(parts) + "\0\0"
    return ctypes.create_unicode_buffer(text)


def ledger_path() -> Path:
    override = (os.environ.get("EASYAGENT_CONTAIN_LEDGER") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".easyagent" / "contain.json"


def load_ledger() -> dict:
    path = ledger_path()
    if not path.is_file():
        return {"consented": False, "profile": "", "aces": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"consented": False, "profile": "", "aces": []}
    if not isinstance(data, dict):
        return {"consented": False, "profile": "", "aces": []}
    data.setdefault("aces", [])
    return data


def save_ledger(data: dict) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def consented() -> bool:
    return bool(load_ledger().get("consented"))


def _ace_recorded(aces: list, action: str, folder: Path, rights: str) -> bool:
    wanted = str(folder)
    for item in aces:
        if not isinstance(item, dict):
            continue
        if item.get("action") == action and item.get("path") == wanted and item.get("rights") == rights:
            return True
    return False


def grant_targets(store, bot_id: str | None = None, extra: list[Path] | None = None) -> list[tuple[Path, str]]:
    """Folders to grant, and the ACE rights. Keychain paths are never included.

    The work folders get modify. The directories that lead to them, inside the
    data folder, get traverse only so the container can reach the workspace
    without a read grant on secrets.db.
    """
    folders: list[Path] = []
    if store is not None and bot_id:
        base = Path(store.root) / "bots" / str(bot_id)
        for name in ("workspace", "workbench", "tmp"):
            folders.append(base / name)
    elif store is not None:
        folders.extend(_default_allows(store))
    for path in extra or []:
        folders.append(Path(path))
    found: list[tuple[Path, str]] = []
    seen: set[str] = set()
    data = None
    if store is not None:
        try:
            data = Path(store.root).resolve()
        except OSError:
            data = Path(store.root)
    for folder in folders:
        if blocked_acl_target(folder):
            continue
        if data is not None:
            try:
                rel = folder.resolve().relative_to(data)
            except (OSError, ValueError):
                rel = None
            if rel is not None:
                acc = data
                for part in rel.parts[:-1]:
                    acc = acc / part
                    key = str(acc)
                    if key in seen or blocked_acl_target(acc):
                        continue
                    seen.add(key)
                    found.append((acc, "(X)"))
        key = str(folder)
        if key in seen or blocked_acl_target(folder):
            continue
        seen.add(key)
        found.append((folder, "(OI)(CI)M"))
    return found


def _default_allows(store) -> list[Path]:
    if store is None:
        return []
    root = Path(store.root)
    found: list[Path] = []
    bots = root / "bots"
    if not bots.is_dir():
        return found
    for child in bots.iterdir():
        if not child.is_dir():
            continue
        for name in ("workspace", "workbench", "tmp"):
            found.append(child / name)
    return found


def setup(store=None, bot_id: str | None = None, *, allow: list[Path] | None = None) -> dict:
    """Create the profile and the work-folder grants once. The keychain is not touched."""
    if os.name != "nt":
        save_ledger({"consented": True, "profile": "", "sid": "", "aces": [], "platform": sys.platform})
        _audit_contain(
            store,
            bot_id,
            "Turned on OS containment. This computer has no Windows profile to create, and no ACL was changed.",
        )
        from easyagent.selftest import reset_probe

        reset_probe()
        return load_ledger()
    targets = grant_targets(store, bot_id, allow)
    sid = _container_sid(PROFILE_NAME, create=True)
    text = _sid_text(sid)
    aces: list[dict] = []
    for folder, rights in targets:
        if rights == "(OI)(CI)M":
            _grant_sid(sid, [folder])
            aces.append({"action": "label", "path": str(folder), "sid": LOW_INTEGRITY_SID, "rights": "(OI)(CI)"})
        else:
            _grant_rights(sid, folder, rights)
        aces.append({"action": "grant", "path": str(folder), "sid": text, "rights": rights})
    save_ledger({"consented": True, "profile": PROFILE_NAME, "sid": text, "aces": aces})
    _audit_contain(
        store,
        bot_id,
        "Created the EasyAgent.Bot profile and granted the work folders. The keychain was not changed.",
    )
    from easyagent.selftest import reset_probe

    reset_probe()
    return load_ledger()


def undo(store=None, bot_id: str | None = None) -> dict:
    """Remove the profile and every ACE this ledger recorded."""
    ledger = load_ledger()
    errors: list[OSError] = []
    kept: list = []
    if os.name == "nt":
        for ace in list(ledger.get("aces") or []):
            try:
                _remove_ace(ace)
            except OSError as exc:
                errors.append(exc)
                kept.append(ace)
        profile = str(ledger.get("profile") or PROFILE_NAME)
        if profile:
            try:
                _delete_profile(profile)
            except OSError as exc:
                errors.append(exc)
    if errors:
        save_ledger(
            {
                "consented": True,
                "profile": str(ledger.get("profile") or ""),
                "sid": str(ledger.get("sid") or ""),
                "aces": kept,
            }
        )
        raise errors[0]
    save_ledger({"consented": False, "profile": "", "aces": []})
    _audit_contain(store, bot_id, "Removed the containment setup, including the profile and every ACE EasyAgent added.")
    from easyagent.selftest import reset_probe

    reset_probe()
    return load_ledger()


def _remove_ace(ace: dict) -> None:
    """Drop matching allow and deny ACEs by rewriting the DACL. REVOKE_ACCESS leaves denies in place."""
    raw = str(ace.get("path") or "")
    sid_text = str(ace.get("sid") or "")
    if not raw or blocked_acl_target(Path(raw)):
        return
    path = Path(raw)
    if not path.exists():
        return
    action = str(ace.get("action") or "")
    rights = str(ace.get("rights") or "")
    errors: list[OSError] = []
    if action == "label" or (action == "grant" and rights and (_mask_for(rights) & FILE_GENERIC_WRITE)):
        try:
            _remove_low_label(path)
        except OSError as exc:
            errors.append(exc)
    if action != "label" and sid_text:
        try:
            _remove_sid_aces(path, _sid_from_text(sid_text))
        except OSError as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


def _ensure_grants(folders: list[Path], data_root: Path) -> None:
    del data_root
    ledger = load_ledger()
    if not ledger.get("consented"):
        return
    sid = _container_sid(PROFILE_NAME, create=False)
    text = _sid_text(sid)
    aces = list(ledger.get("aces") or [])
    changed = False
    for folder in folders:
        if blocked_acl_target(folder) or _ace_recorded(aces, "grant", folder, "(OI)(CI)M"):
            continue
        _grant_sid(sid, [folder])
        aces.append({"action": "grant", "path": str(folder), "sid": text, "rights": "(OI)(CI)M"})
        changed = True
    if _record_low_labels(folders, aces):
        changed = True
    if not changed:
        return
    ledger["aces"] = aces
    save_ledger(ledger)
    _audit_contain(None, None, "Granted the container SID on a work folder. The keychain was not changed.")


def _label_is_present(folder: Path) -> bool:
    try:
        sid = _sid_from_text(LOW_INTEGRITY_SID)
        advapi = _dll("advapi32")
        sd, sacl = _read_label(advapi, folder)
    except OSError:
        return False
    try:
        return _label_matches(advapi, sacl, sid)
    finally:
        _free(sd)


def _record_low_labels(folders: list[Path], aces: list) -> bool:
    """Remember a Low label that is already on the folder so undo can take it off."""
    changed = False
    for folder in folders:
        folder = Path(folder)
        if blocked_acl_target(folder) or not folder.exists():
            continue
        if _ace_recorded(aces, "label", folder, "(OI)(CI)"):
            continue
        if not _label_is_present(folder):
            continue
        aces.append({"action": "label", "path": str(folder), "sid": LOW_INTEGRITY_SID, "rights": "(OI)(CI)"})
        changed = True
    return changed


def _ensure_low_labels(folders: list[Path]) -> None:
    """A Low token cannot write a folder whose label is still Medium."""
    ledger = load_ledger()
    aces = list(ledger.get("aces") or [])
    changed = False
    for folder in folders:
        folder = Path(folder)
        if blocked_acl_target(folder) or not folder.exists():
            continue
        if not _label_is_present(folder):
            _set_low_label(folder)
        if not _ace_recorded(aces, "label", folder, "(OI)(CI)"):
            aces.append({"action": "label", "path": str(folder), "sid": LOW_INTEGRITY_SID, "rights": "(OI)(CI)"})
            changed = True
    if changed and ledger.get("consented"):
        ledger["aces"] = aces
        save_ledger(ledger)


def _ensure_data_deny(data_root: Path) -> None:
    """One deny ACE on the data directory, after consent. Not on the keychain.

    A failed apply raises AclError. The caller must not start the process.
    """
    if blocked_acl_target(data_root):
        raise AclError(0, "The data directory is not a keychain folder. The deny was not applied. The restricted token was not started.")
    ledger = load_ledger()
    if not ledger.get("consented"):
        raise OSError("The restricted-token deny needs the one-time setup. The command was not run.")
    aces = list(ledger.get("aces") or [])
    # A ledger row is not the ACE. The old deny command failed on S-1-5-59923 and the row was saved anyway.
    if _ace_recorded(aces, "deny", data_root, "(OI)(CI)F") and _deny_is_present(data_root, aces):
        return
    try:
        sid = _deny_sid()
        for item in aces:
            if item.get("action") == "grant" and item.get("path"):
                _protect_folder(Path(str(item["path"])))
        _deny_sid_on(sid, data_root)
        text = _sid_text(sid)
    except AclError:
        raise
    except OSError as exc:
        raise AclError(getattr(exc, "errno", 0) or 0, f"{exc}. The restricted token was not started.") from exc
    if not _ace_recorded(aces, "deny", data_root, "(OI)(CI)F"):
        aces.append({"action": "deny", "path": str(data_root), "sid": text, "rights": "(OI)(CI)F"})
        ledger["aces"] = aces
        save_ledger(ledger)
    _audit_contain(None, None, "Denied the restricted token on the data directory. The keychain was not changed.")


def _deny_is_present(data_root: Path, aces: list) -> bool:
    """True only when the recorded deny SID is actually on the directory DACL."""
    text = ""
    for item in aces:
        if not isinstance(item, dict):
            continue
        if item.get("action") == "deny" and item.get("path") == str(data_root) and item.get("rights") == "(OI)(CI)F":
            text = str(item.get("sid") or "")
            break
    if not text:
        return False
    try:
        sid = _sid_from_text(text)
        _verify_ace(_dll("advapi32"), data_root, sid, DENY_ACCESS, FILE_ALL_ACCESS, OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)
    except OSError:
        return False
    return True


def _audit_contain(store, bot_id: str | None, why: str) -> None:
    from easyagent.store import atomic_write_json, now_iso, read_json

    row = {"at": now_iso(), "decision": "contain", "rule": "contain-setup", "why": why, "detail": ""}
    if store is not None:
        path = Path(store.root) / "contain-audit.json"
        rows = read_json(path) if path.is_file() else []
        if not isinstance(rows, list):
            rows = []
        rows.append(row)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, rows[-200:])
        if bot_id:
            try:
                from easyagent.safety import _audit

                _audit(store, bot_id, row)
            except Exception:
                pass
        return
    path = Path.home() / ".easyagent" / "contain-audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        rows = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, json.JSONDecodeError):
        rows = []
    if not isinstance(rows, list):
        rows = []
    rows.append(row)
    path.write_text(json.dumps(rows[-200:], indent=2), encoding="utf-8")


async def ensure_consent(store, bot_id: str | None) -> None:
    """Ask once before any persistent containment setup. A routine cannot approve it."""
    from easyagent.tools import ToolError

    if consented():
        return
    from easyagent.safety import ASK, Pending, _PENDING, _audit, _wait
    from easyagent.store import new_id, now_iso

    try:
        from easyagent.safety import is_unattended as _unattended
    except ImportError:
        def _unattended() -> bool:
            return False
    if _unattended():
        raise ToolError(
            "OS containment needs a one-time approval. A routine cannot approve it. The command was not run."
        )
    why = (
        "EasyAgent needs a one-time setup before tool commands run in an OS container. "
        "On Windows this creates an AppContainer profile and grants this bot's workspace, workbench, and tmp. "
        "A traverse-only grant is added on the folders that lead there. "
        "Credentials, Vault, and Protect are not changed. "
        "You can remove it later with python -m easyagent contain --undo."
    )
    card = Pending(
        id=new_id(),
        bot_id=bot_id or "",
        tier=ASK,
        rule="contain-setup",
        why=why,
        detail="easyagent contain setup",
        fingerprint="contain-setup",
        exact="contain-setup",
        offer_always=False,
        created=__import__("time").time(),
    )
    _PENDING[card.id] = card
    _audit(store, bot_id, {"at": now_iso(), "decision": "ask", "rule": "contain-setup", "why": why, "detail": ""})
    try:
        decision = await _wait(card)
    finally:
        _PENDING.pop(card.id, None)
    _audit(
        store,
        bot_id,
        {"at": now_iso(), "decision": decision or "deny", "rule": "contain-setup", "why": why, "detail": ""},
    )
    if decision not in {"approve", "always"}:
        raise ToolError("OS containment was not approved. The command was not run.")
    try:
        setup(store, bot_id)
    except OSError as exc:
        raise ToolError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="easyagent contain")
    parser.add_argument("--undo", action="store_true")
    parser.add_argument("--setup", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.undo:
            undo()
            print("Removed the containment setup. On Windows that includes the profile and every ACE EasyAgent added.", flush=True)
            return 0
        setup()
        print("OS containment is on. On Windows the keychain folders were not changed.", flush=True)
        return 0
    except OSError as exc:
        print(str(exc), flush=True)
        return 1
