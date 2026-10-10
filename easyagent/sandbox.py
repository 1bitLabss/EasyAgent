"""OS containment for the bot shell.

Linux uses bubblewrap. Landlock is the fallback when bubblewrap is missing.
macOS uses sandbox-exec. Windows uses the AppContainer in ``contain``.

Nothing here is persistent until the person approves the one-time setup.
Until then the 0.3.6 file guards are the launch.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

_READ_BITS = (1 << 0) | (1 << 2) | (1 << 3)  # execute, read file, read dir
_WRITE_BITS = _READ_BITS | (1 << 1) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 8) | (1 << 13) | (1 << 14)
_HANDLED_FS = (1 << 15) - 1
_NR_CREATE = 444
_NR_ADD = 445
_NR_RESTRICT = 446


def web_enabled() -> bool:
    raw = (os.environ.get("EASYAGENT_CONTAIN_WEB") or "1").strip().lower()
    return raw not in {"0", "off", "false", "no"}


def work_folders(store, bot_id: str | None) -> list[Path]:
    from easyagent.contain import grant_targets

    return [path for path, rights in grant_targets(store, bot_id) if rights == "(OI)(CI)M"]


def shell_mode(store) -> dict:
    """How the next shell runs. Off until consent. Unavailable keeps the file guards."""
    from easyagent.contain import consented

    if not consented():
        return {"contained": False, "status": "off", "label": "off", "notice": "", "reason": "", "mechanism": "none"}
    from easyagent.selftest import cached_probe

    report = cached_probe()
    if report.get("passed"):
        mechanism = report.get("mechanism") or "none"
        return {
            "contained": True,
            "status": "active",
            "label": "active",
            "notice": "",
            "reason": report.get("reason") or "",
            "mechanism": mechanism,
        }
    reason = report.get("reason") or "the self-test did not pass"
    notice = f"OS containment is unavailable: {reason}. This command used the file guards."
    return {
        "contained": False,
        "status": "unavailable",
        "label": f"unavailable: {reason}",
        "notice": notice,
        "reason": reason,
        "mechanism": "none",
    }


def public_status(store, bot_id: str | None = None) -> dict:
    mode = shell_mode(store)
    folders = [str(path) for path in work_folders(store, bot_id)] if bot_id else []
    return {
        "status": mode["status"],
        "label": mode["label"],
        "reason": mode["reason"],
        "mechanism": mode["mechanism"],
        "active": mode["status"] == "active",
        "consented": mode["status"] != "off",
        "web": web_enabled(),
        "folders": folders,
        "undo": "python -m easyagent contain --undo",
    }


def warm_probe() -> None:
    """Start the cached self-test when containment is already on."""
    from easyagent.contain import consented

    if not consented():
        return
    import threading

    from easyagent.selftest import cached_probe

    threading.Thread(target=cached_probe, name="easyagent-contain-probe", daemon=True).start()


def popen_contained(store, bot_id: str | None, command: str, env: dict, cwd: str):
    """Start ``command`` inside the OS container. Raises OSError if that launch fails."""
    work = _contained_cwd(store, bot_id, cwd)
    if os.name == "nt":
        from easyagent.contain import popen_contained as win_popen

        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
        return win_popen(
            argv,
            env=env,
            cwd=work,
            data_root=Path(store.root),
            allow=work_folders(store, bot_id),
            web=web_enabled(),
        )
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        profile = macos_profile(work_folders(store, bot_id), Path(store.root))
        path = Path(tempfile.gettempdir()) / "easyagent-macos.sb"
        path.write_text(profile, encoding="utf-8")
        return subprocess.Popen(
            ["sandbox-exec", "-f", str(path), "bash", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=work,
            env=env,
            start_new_session=True,
        )
    if shutil.which("bwrap"):
        return subprocess.Popen(
            bwrap_argv(store, bot_id, command, work, web=web_enabled()),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=work,
            env=env,
            start_new_session=True,
        )
    if landlock_available():
        return subprocess.Popen(
            ["bash", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=work,
            env=env,
            start_new_session=True,
            preexec_fn=lambda: apply_landlock(store, bot_id, web=web_enabled()),
        )
    raise OSError("bubblewrap is not installed and Landlock is not available")


def bwrap_argv(store, bot_id: str | None, command: str, cwd: str, *, web: bool = True) -> list[str]:
    """Bubblewrap the command. The data directory and the keychain are not visible."""
    from easyagent.secrets import keychain_paths

    empty = Path(tempfile.gettempdir()) / "easyagent-seal-empty"
    empty.mkdir(parents=True, exist_ok=True)
    argv = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/"]
    if not web:
        argv.append("--unshare-net")
    try:
        root = Path(store.root).resolve()
    except OSError:
        root = Path(store.root)
    argv += ["--bind", str(empty), str(root)]
    for folder in work_folders(store, bot_id):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        argv += ["--bind", str(folder), str(folder)]
    for path in keychain_paths():
        if not path.exists():
            continue
        if path.is_dir():
            argv += ["--ro-bind", str(empty), str(path)]
        else:
            argv += ["--ro-bind", "/dev/null", str(path)]
    argv += ["--dev", "/dev", "--proc", "/proc", "--", "bash", "-c", command]
    del cwd
    return argv


def macos_profile(writable: list[Path], data_root: Path | None = None) -> str:
    """sandbox-exec profile. The data directory, ~/.easyagent, Keychains, and securityd are denied."""
    from easyagent.secrets import keychain_paths

    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        '(deny file-read* (regex #"(?i)(cookies|login data|logins\\.json|key4\\.db|Keychains|secrets\\.db)"))',
    ]
    if data_root is not None:
        lines.append(f'(deny file-read* (subpath "{data_root}"))')
        lines.append(f'(deny file-write* (subpath "{data_root}"))')
    for path in writable:
        lines.append(f'(allow file-read* (subpath "{path}"))')
        lines.append(f'(allow file-write* (subpath "{path}"))')
    for path in keychain_paths():
        lines.append(f'(deny file-read* (subpath "{path}"))')
        lines.append(f'(deny file-write* (subpath "{path}"))')
    lines.append('(deny mach-lookup (global-name "com.apple.securityd"))')
    lines.append('(deny mach-lookup (global-name "com.apple.SecurityServer"))')
    return "\n".join(lines) + "\n"


def landlock_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = _libc()
        abi = libc.syscall(_NR_CREATE, None, 0, 1)
    except OSError:
        return False
    return int(abi) >= 1


def apply_landlock(
    store,
    bot_id: str | None,
    *,
    web: bool = True,
    extra_write: list[Path] | None = None,
    extra_read: list[Path] | None = None,
) -> None:
    """Restrict this process. Call it from the child, before the command runs."""
    if not landlock_available():
        raise OSError("Landlock is not available")
    libc = _libc()
    handled = _ctypes().c_uint64(_HANDLED_FS)
    libc.syscall.argtypes = [_ctypes().c_long, _ctypes().c_void_p, _ctypes().c_size_t, _ctypes().c_uint32]
    ruleset = libc.syscall(_NR_CREATE, _ctypes().byref(handled), 8, 0)
    if ruleset < 0:
        raise OSError("landlock_create_ruleset failed")
    denied = _denied_roots(store)
    readable = _readable_roots()
    for path in list(extra_read or []):
        if path not in readable:
            readable.append(path)
    for path in _split_allows(readable, denied):
        _landlock_allow(libc, ruleset, path, _READ_BITS)
    writable = list(work_folders(store, bot_id))
    for folder in extra_write or []:
        if folder not in writable:
            writable.append(folder)
    for folder in writable:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        _landlock_allow(libc, ruleset, folder, _WRITE_BITS)
    libc.prctl.argtypes = [_ctypes().c_int, _ctypes().c_ulong, _ctypes().c_ulong, _ctypes().c_ulong, _ctypes().c_ulong]
    libc.prctl.restype = _ctypes().c_int
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError("prctl NO_NEW_PRIVS failed")
    libc.syscall.argtypes = [_ctypes().c_long, _ctypes().c_int, _ctypes().c_uint32]
    if libc.syscall(_NR_RESTRICT, ruleset, 0) != 0:
        raise OSError("landlock_restrict_self failed")
    os.close(ruleset)
    del web


def probe_command() -> str:
    """The script the self-test runs inside the container."""
    return (
        "python3 -c \""
        "import os, pathlib, socket\n"
        "def read(path):\n"
        "    try:\n"
        "        return pathlib.Path(path).read_text(encoding='utf-8')\n"
        "    except Exception:\n"
        "        return ''\n"
        "data = os.environ.get('EASYAGENT_PROBE_DATA', '')\n"
        "secret = os.environ.get('EASYAGENT_PROBE_SECRET', '')\n"
        "work = os.environ.get('EASYAGENT_PROBE_WORK', '')\n"
        "print('IDENTITY', os.popen('id').read().strip() or 'none')\n"
        "print('DATA', 'DENIED' if not read(data) else 'LEAK')\n"
        "print('SECRETS', 'DENIED' if not read(secret) else 'LEAK')\n"
        "marker = pathlib.Path(work) / 'probe.txt'\n"
        "wrote = 'DENIED'\n"
        "try:\n"
        "    marker.write_text('probe-ok', encoding='utf-8')\n"
        "    wrote = 'ok' if marker.read_text(encoding='utf-8') == 'probe-ok' else 'DENIED'\n"
        "except Exception:\n"
        "    wrote = 'DENIED'\n"
        "print('WROTE', wrote)\n"
        "port = int(os.environ.get('EASYAGENT_PROBE_WEB_PORT') or '0')\n"
        "web = 'DENIED'\n"
        "if port:\n"
        "    try:\n"
        "        sock = socket.create_connection(('127.0.0.1', port), 2)\n"
        "        sock.send(b'hi')\n"
        "        sock.close()\n"
        "        web = 'ok'\n"
        "    except Exception:\n"
        "        web = 'DENIED'\n"
        "print('WEB', web)\n"
        "\""
    )


def listen_for_probe() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(5)
    return sock, int(sock.getsockname()[1])


def _contained_cwd(store, bot_id: str | None, cwd: str) -> str:
    try:
        current = Path(cwd).resolve()
    except OSError:
        current = Path(cwd)
    for folder in work_folders(store, bot_id):
        try:
            resolved = folder.resolve()
        except OSError:
            resolved = folder
        if current == resolved or resolved in current.parents:
            return str(current)
    if bot_id:
        folder = Path(store.root) / "bots" / str(bot_id) / "workspace"
    else:
        folder = Path(cwd)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return cwd
    return str(folder)


def _denied_roots(store) -> list[Path]:
    from easyagent.secrets import keychain_paths

    found = list(keychain_paths())
    if store is not None:
        found.append(Path(store.root))
    return found


def _readable_roots() -> list[Path]:
    found = [Path(item) for item in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/etc", "/proc", "/dev", "/sys", "/opt")]
    prefix = Path(sys.prefix)
    if prefix not in found:
        found.append(prefix)
    exe = Path(sys.executable).resolve().parent if sys.executable else None
    if exe is not None:
        found.append(exe)
    return [path for path in found if path.exists()]


def _split_allows(paths: list[Path], denied: list[Path]) -> list[Path]:
    allowed: list[Path] = []
    for path in paths:
        allowed.extend(_split_one(path, denied))
    return allowed


def _split_one(path: Path, denied: list[Path]) -> list[Path]:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if any(_inside(resolved, root) for root in denied):
        return []
    covered = [root for root in denied if _inside(root, resolved)]
    if not covered:
        return [resolved]
    if not resolved.is_dir():
        return []
    found: list[Path] = []
    try:
        children = list(resolved.iterdir())
    except OSError:
        return []
    for child in children:
        found.extend(_split_one(child, denied))
    return found


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _landlock_allow(libc, ruleset: int, path: Path, rights: int) -> None:
    if not path.exists():
        return
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return
    beneath = _Beneath(rights, fd)
    libc.syscall.argtypes = [_ctypes().c_long, _ctypes().c_int, _ctypes().c_int, _ctypes().c_void_p, _ctypes().c_uint32]
    rc = libc.syscall(_NR_ADD, ruleset, 1, _ctypes().byref(beneath), 0)
    os.close(fd)
    if rc != 0:
        raise OSError(f"landlock_add_rule failed for {path}")


def _libc():
    libc = _ctypes().CDLL(None, use_errno=True)
    libc.syscall.restype = _ctypes().c_long
    return libc


def _ctypes():
    import ctypes

    return ctypes


class _Beneath:
    """A stand-in replaced after ctypes is imported. The real struct is built in apply."""

    def __new__(cls, rights: int, fd: int):
        class Beneath(_ctypes().Structure):
            _fields_ = [("allowed_access", _ctypes().c_uint64), ("parent_fd", _ctypes().c_int32)]

        return Beneath(rights, fd)
