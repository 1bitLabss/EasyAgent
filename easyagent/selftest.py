"""Startup probe for OS containment, and `python -m easyagent selftest`.

`selftest contain` is the OS probe. The Windows probe uses a throwaway
AppContainer and removes it before returning. Linux and macOS run the same
checks in bubblewrap, Landlock, or sandbox-exec. Nothing is written to the
user's keychain folders.

`selftest all` is the release gate in easyagent.selftest_all.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

_CACHE: dict | None = None
_LOCK = threading.Lock()


def reset_probe() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


def cached_probe() -> dict:
    """The startup result. The first call runs the probe; later calls reuse it."""
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            _CACHE = _summarize(_platform_rows())
        report = dict(_CACHE)
    from easyagent.contain import _FALLBACK

    if _FALLBACK.get("mechanism"):
        report["mechanism"] = _FALLBACK["mechanism"]
        if _FALLBACK.get("reason"):
            report["reason"] = _FALLBACK["reason"]
    return report


def format_table(rows: list[tuple[str, str, str]]) -> str:
    lines = []
    for name, status, detail in rows:
        lines.append(f"{name:<18} {status:<4}  {detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["all"]:
        from easyagent.selftest_all import main as all_main

        return all_main(args[1:])
    if args != ["contain"]:
        print("usage: python -m easyagent selftest contain", flush=True)
        print("       python -m easyagent selftest all --data COPY --port PORT [--report FILE]", flush=True)
        return 2
    rows = _platform_rows()
    print(format_table(rows), flush=True)
    if rows and rows[-1][1] == "FAIL":
        return 1
    return 0


def _platform_rows() -> list[tuple[str, str, str]]:
    if os.name == "nt":
        return windows_rows()
    if sys.platform == "darwin":
        return _darwin_rows()
    return _linux_rows()


def _summarize(rows: list[tuple[str, str, str]]) -> dict:
    result = rows[-1] if rows else ("RESULT", "FAIL", "no probe ran")
    passed = result[1] == "PASS"
    mechanism = "none"
    if passed:
        if os.name == "nt":
            mechanism = "appcontainer"
        elif sys.platform == "darwin":
            mechanism = "sandbox-exec"
        else:
            mechanism = "bubblewrap"
        for name, status, detail in rows:
            if name == "mechanism" and status == "PASS" and detail:
                mechanism = detail
    reason = result[2] if result[2] else ("" if passed else "containment is unavailable")
    return {
        "passed": passed,
        "mechanism": mechanism if passed else "none",
        "status": "active" if passed else "unavailable",
        "reason": reason,
        "rows": rows,
    }


def _linux_rows() -> list[tuple[str, str, str]]:
    from easyagent.sandbox import landlock_available

    if shutil.which("bwrap"):
        mechanism = "bubblewrap"
    elif landlock_available():
        mechanism = "landlock"
    else:
        detail = "bubblewrap is not installed and Landlock is not available"
        return [("container", "FAIL", detail), ("RESULT", "FAIL", detail)]
    return _posix_rows(mechanism)


def _darwin_rows() -> list[tuple[str, str, str]]:
    if not shutil.which("sandbox-exec"):
        detail = "sandbox-exec is not installed"
        return [("sandbox-exec", "FAIL", detail), ("RESULT", "FAIL", detail)]
    return _posix_rows("sandbox-exec")


def _posix_rows(mechanism: str) -> list[tuple[str, str, str]]:
    """Launch the probe script. The canary text stays in the file, not on the command line."""
    from easyagent.sandbox import listen_for_probe, popen_contained, probe_command

    temp = Path(tempfile.mkdtemp(prefix="easyagent-contain-"))
    data = temp / "data"
    bot = "probe"
    work = data / "bots" / bot / "workspace"
    data.mkdir(parents=True)
    work.mkdir(parents=True)
    token = "CANARY-" + os.urandom(4).hex()
    secret_token = "SECRET-" + os.urandom(4).hex()
    canary = data / "endpoints.json"
    secret = data / "secrets.db"
    canary.write_text(token, encoding="utf-8")
    secret.write_text(secret_token, encoding="utf-8")
    sock = None
    rows: list[tuple[str, str, str]] = [("mechanism", "PASS", mechanism)]
    try:
        sock, port = listen_for_probe()
        env = dict(os.environ)
        env["EASYAGENT_PROBE_DATA"] = str(canary)
        env["EASYAGENT_PROBE_SECRET"] = str(secret)
        env["EASYAGENT_PROBE_WORK"] = str(work)
        env["EASYAGENT_PROBE_WEB_PORT"] = str(port)

        class _Store:
            def __init__(self, root: Path):
                self.root = root

        try:
            proc = popen_contained(_Store(data), bot, probe_command(), env, str(work))
            out, err = proc.communicate(timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = str(exc)[:200]
            rows.append(("launch", "FAIL", detail))
            rows.append(("RESULT", "FAIL", detail))
            return rows
        heard = (out or "") + "\n" + (err or "")
        fields = _probe_fields(heard)
        identity = fields.get("IDENTITY", "")
        rows.append(("whoami", "PASS" if identity and identity != "none" else "FAIL", identity[:120] or "no identity"))
        leaked = token in heard or secret_token in heard
        rows.append(("data-read", "FAIL" if leaked or fields.get("DATA") != "DENIED" else "PASS", "denied" if fields.get("DATA") == "DENIED" and not leaked else "the canary was readable"))
        rows.append(("secrets", "FAIL" if fields.get("SECRETS") != "DENIED" or secret_token in heard else "PASS", "denied" if fields.get("SECRETS") == "DENIED" else "secrets.db was readable"))
        marker = work / "probe.txt"
        wrote = fields.get("WROTE") == "ok" and marker.is_file() and "probe-ok" in marker.read_text(encoding="utf-8", errors="replace")
        rows.append(("workspace-write", "PASS" if wrote else "FAIL", "wrote probe.txt" if wrote else "the workspace was not writable"))
        connected = False
        try:
            client, _addr = sock.accept()
            client.close()
            connected = True
        except OSError:
            connected = False
        web_ok = connected and fields.get("WEB") == "ok"
        rows.append(("web", "PASS" if web_ok else "FAIL", "connected" if web_ok else "the container could not reach the local listener"))
    finally:
        if sock is not None:
            sock.close()
        shutil.rmtree(temp, ignore_errors=True)
    core = {name: status for name, status, _detail in rows}
    passed = all(core.get(name) == "PASS" for name in ("whoami", "data-read", "secrets", "workspace-write", "web"))
    rows.append(("RESULT", "PASS" if passed else "FAIL", "" if passed else "the container did not hold"))
    return rows


def _probe_fields(heard: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in (heard or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in {"IDENTITY", "DATA", "SECRETS", "WROTE", "WEB"}:
            found[parts[0]] = parts[1].strip()
    return found


def windows_rows() -> list[tuple[str, str, str]]:
    """Throwaway AppContainer, then the restricted-token fallback. Both are removed before return."""
    from easyagent.contain import SECURITY_CAPABILITIES

    rows: list[tuple[str, str, str]] = [
        ("attribute", "PASS", f"SECURITY_CAPABILITIES {SECURITY_CAPABILITIES:#x}"),
    ]
    if os.name != "nt":
        rows.append(("platform", "SKIP", f"this probe runs on Windows ({sys.platform})"))
        rows.append(("RESULT", "SKIP", "nothing was changed"))
        return rows
    return _windows_probe(rows)


def _probe_scratch() -> Path:
    """A folder this user owns. The system temp directory often grants only Modify, and a Low label needs WRITE_OWNER."""
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        root = Path(local) / "EasyAgent"
    else:
        home = (os.environ.get("USERPROFILE") or "").strip()
        if not home:
            return Path(tempfile.mkdtemp(prefix="easyagent-contain-"))
        root = Path(home) / "AppData" / "Local" / "EasyAgent"
    root.mkdir(parents=True, exist_ok=True)
    scratch = root / f"probe-{os.urandom(4).hex()}"
    scratch.mkdir()
    return scratch


def _windows_probe(rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    from easyagent.contain import (
        PROBE_PROFILE,
        _container_sid,
        _delete_profile,
        _dll,
        _grant_rights,
        _grant_sid,
        _profile_exists,
        _remove_ace,
        _sid_text,
    )
    from easyagent.sandbox import web_enabled

    temp = _probe_scratch()
    data = temp / "data"
    work = data / "workspace"
    data.mkdir()
    work.mkdir()
    token = "CANARY-" + os.urandom(4).hex()
    secret_token = "SECRET-" + os.urandom(4).hex()
    canary = data / "endpoints.json"
    secret = data / "secrets.db"
    canary.write_text(token, encoding="utf-8")
    secret.write_text(secret_token, encoding="utf-8")
    aces: list[dict] = []
    web = web_enabled()
    try:
        try:
            sid = _container_sid(PROBE_PROFILE, create=True)
            text = _sid_text(sid)
            _grant_rights(sid, data, "(X)")
            aces.append({"action": "grant", "path": str(data), "sid": text, "rights": "(X)"})
            _grant_sid(sid, [work])
            aces.append({"action": "grant", "path": str(work), "sid": text, "rights": "(OI)(CI)M"})
            aces.append({"action": "label", "path": str(work), "sid": "S-1-16-4096", "rights": "(OI)(CI)"})
            kernel32 = _dll("kernel32")
            env = dict(os.environ)
            _record_launch(rows, kernel32, env, work, sid, canary, secret, token, secret_token, web)
            if any(name == "whoami" and status == "PASS" for name, status, _detail in rows):
                rows.append(("mechanism", "PASS", "appcontainer"))
        except OSError as exc:
            rows.append(("appcontainer", "FAIL", str(exc)))
        try:
            _record_restricted(rows, data, work, token, secret_token, aces)
        except OSError as nested:
            rows.append(("restricted-token", "FAIL", str(nested)))
    finally:
        remove_error = ""
        for ace in aces:
            try:
                _remove_ace(ace)
            except OSError as exc:
                remove_error = str(exc)
        delete_error = ""
        try:
            _delete_profile(PROBE_PROFILE)
        except OSError as exc:
            delete_error = str(exc)
        shutil.rmtree(temp, ignore_errors=True)
        gone = not _profile_exists(PROBE_PROFILE)
        if not gone:
            detail = "profile still exists"
            if delete_error:
                detail = f"{detail} ({delete_error})"
            if remove_error:
                detail = f"{detail} ({remove_error})"
            rows.append(("cleanup", "FAIL", detail))
        elif remove_error:
            rows.append(("cleanup", "FAIL", f"profile removed ({remove_error})"))
        else:
            rows.append(("cleanup", "PASS", "profile removed"))
    passed = _windows_passed(rows, web)
    rows.append((
        "RESULT",
        "PASS" if passed else "FAIL",
        "AppContainer and the restricted token held" if passed else "the container did not hold",
    ))
    return rows


def _windows_passed(rows: list[tuple[str, str, str]], web: bool) -> bool:
    core = {name: status for name, status, _detail in rows}
    needed = [
        "whoami",
        "control",
        "data-read",
        "secrets",
        "workspace-write",
        "restricted-whoami",
        "restricted-control",
        "restricted-data-read",
        "restricted-secrets",
        "restricted-workspace-write",
    ]
    del web
    if core.get("cleanup") == "FAIL":
        return False
    return all(core.get(name) == "PASS" for name in needed)


def _record_restricted(rows, data: Path, work: Path, token: str, secret_token: str, aces: list[dict]) -> None:
    """The fallback launch, run even when the AppContainer succeeded. Its ACEs are removed after."""
    from easyagent.contain import _create_restricted, _deny_sid, _deny_sid_on, _dll, _grant_sid, _protect_folder, _sid_text

    sid = _deny_sid()
    text = _sid_text(sid)
    _protect_folder(work)
    _grant_sid(sid, [work])
    aces.append({"action": "grant", "path": str(work), "sid": text, "rights": "(OI)(CI)M"})
    aces.append({"action": "label", "path": str(work), "sid": "S-1-16-4096", "rights": "(OI)(CI)"})
    _deny_sid_on(sid, data)
    aces.append({"action": "deny", "path": str(data), "sid": text, "rights": "(OI)(CI)F"})
    kernel32 = _dll("kernel32")
    env = dict(os.environ)

    def launch(argv):
        return _create_restricted(kernel32, argv, env, str(work), sid)

    proc = launch(["whoami.exe", "/groups"])
    heard, code, started = _child_result(proc)
    low = "S-1-16-4096" in heard or "low mandatory level" in heard.lower()
    if started and code in (0, None) and low:
        rows.append(("restricted-whoami", "PASS", "restricted token at Low"))
    elif not started:
        rows.append(("restricted-whoami", "FAIL", f"whoami did not run (exit {_code_text(code)})"))
    else:
        rows.append(("restricted-whoami", "FAIL", "whoami ran without the Low label"))
    ran = _record_reads(rows, launch, data, token, secret_token, "restricted")
    if ran:
        _record_write(rows, launch, work, "restricted")
    else:
        rows.append(("restricted-workspace-write", "INCONCLUSIVE", "the child did not run"))
    if any(name == "restricted-whoami" and status == "PASS" for name, status, _detail in rows):
        rows.append(("restricted-mechanism", "PASS", "restricted-token"))


def _record_launch(rows, kernel32, env, work: Path, sid, canary: Path, secret: Path, token: str, secret_token: str, web: bool) -> None:
    from easyagent.contain import _create_appcontainer

    proc = _create_appcontainer(kernel32, ["whoami.exe", "/groups"], env, str(work), sid, web)
    heard, code, started = _child_result(proc)
    sid_ok = "S-1-15-2-" in heard
    low_ok = "S-1-16-4096" in heard or "low mandatory level" in heard.lower()
    if sid_ok and low_ok:
        rows.append(("whoami", "PASS", "AppContainer SID and Low label"))
    elif sid_ok:
        rows.append(("whoami", "PASS", "AppContainer SID"))
    elif low_ok:
        rows.append(("whoami", "PASS", "Low label"))
    elif not started:
        rows.append(("whoami", "FAIL", f"whoami did not run (exit {_code_text(code)})"))
    else:
        rows.append(("whoami", "FAIL", "neither the AppContainer SID nor the Low label was present"))
    data = canary.parent

    def launch(argv):
        return _create_appcontainer(kernel32, argv, env, str(work), sid, web)

    ran = _record_reads(rows, launch, data, token, secret_token, "")
    if ran:
        _record_write(rows, launch, work, "")
    else:
        rows.append(("workspace-write", "INCONCLUSIVE", "the child did not run"))
    if web:
        _record_web(rows, launch)
    del secret


def _row_name(prefix: str, name: str) -> str:
    if not prefix:
        return name
    return f"{prefix}-{name}"


def _code_text(code) -> str:
    if code is None:
        return "none"
    value = int(code) & 0xFFFFFFFF
    if value > 0xFFFF:
        return f"0x{value:08X}"
    return str(value)


def _child_result(proc) -> tuple[str, int | None, bool]:
    """Output, exit code, and whether the child actually started.

    Exit 0xC0000022 with no output means the process was refused before it ran.
    That is not a denied file read.
    """
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        return "", None, False
    heard = (out or "") + (err or "")
    code = getattr(proc, "returncode", None)
    started = bool(heard.strip()) or code in (0, None)
    return heard, code, started


def _ps(script: str) -> list[str]:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]


def _record_reads(rows, launch, data: Path, token: str, secret_token: str, prefix: str = "") -> bool:
    """PASS a denial only after a control command in this same container printed its marker."""
    control = launch(_ps("Write-Output 'EASYAGENT-RAN'"))
    heard, code, started = _child_result(control)
    ran = started and code in (0, None) and "EASYAGENT-RAN" in heard
    if ran:
        rows.append((_row_name(prefix, "control"), "PASS", "the container ran"))
    else:
        detail = f"the child did not run (exit {_code_text(code)})"
        rows.append((_row_name(prefix, "control"), "FAIL", detail))
        rows.append((_row_name(prefix, "data-read"), "INCONCLUSIVE", detail))
        rows.append((_row_name(prefix, "secrets"), "INCONCLUSIVE", detail))
        return False
    _record_one_read(rows, launch, data / "endpoints.json", token, _row_name(prefix, "data-read"), "the canary was readable")
    _record_one_read(rows, launch, data / "secrets.db", secret_token, _row_name(prefix, "secrets"), "secrets.db was readable")
    return True


def _record_one_read(rows, launch, path: Path, secret: str, name: str, leak: str) -> None:
    proc = launch(_ps(f"Get-Content -LiteralPath '{path}'"))
    heard, code, started = _child_result(proc)
    if not started:
        rows.append((name, "INCONCLUSIVE", f"the child did not run (exit {_code_text(code)})"))
    elif secret in heard:
        rows.append((name, "FAIL", leak))
    else:
        rows.append((name, "PASS", "denied"))


def _record_write(rows, launch, work: Path, prefix: str = "") -> None:
    marker = work / "probe.txt"
    proc = launch(_ps(f"Set-Content -LiteralPath '{marker}' -Value 'probe-ok'"))
    _child_result(proc)
    try:
        wrote = marker.is_file() and "probe-ok" in marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        wrote = False
    rows.append((_row_name(prefix, "workspace-write"), "PASS" if wrote else "FAIL", "wrote probe.txt" if wrote else "the workspace was not writable"))


def _tcp_script(host: str, port: int, label: str, timeout_ms: int = 4000) -> list[str]:
    """TCP connect only. The child does not send any bytes."""
    wait = int(timeout_ms)
    script = (
        "$c = New-Object System.Net.Sockets.TcpClient; "
        f"try {{ $iar = $c.BeginConnect('{host}', {int(port)}, $null, $null); "
        f"if (-not $iar.AsyncWaitHandle.WaitOne({wait}, $false)) {{ "
        f"'{label} DENIED timeout' }} else {{ $c.EndConnect($iar); '{label} ok' }} }} "
        f"catch {{ '{label} DENIED ' + $_.Exception.Message }} finally {{ $c.Close() }}"
    )
    return _ps(script)


def _host_lan_ip() -> str:
    """The address this computer uses for a non-loopback route. Empty when there is none."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        ip = sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()
    if not ip or ip.startswith("127."):
        return ""
    return ip


FIREWALL_WARN = (
    "a third-party firewall is blocking sandboxed bots from the network; "
    "EasyAgent's own web search still works"
)


def _ipv4(host: str) -> str:
    parts = (host or "").split(".")
    if len(parts) != 4:
        return ""
    try:
        if all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
            return host
    except ValueError:
        return ""
    return ""


def _on_windows() -> bool:
    """The platform this module should treat as Windows. Tests patch this, not os.name."""
    return os.name == "nt"


def third_party_firewalls() -> list[str]:
    """Firewall products other than Windows Firewall. Empty off Windows or when WMI cannot be read."""
    if not _on_windows():
        return []
    script = (
        "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName FirewallProduct "
        "-ErrorAction SilentlyContinue | ForEach-Object { $_.displayName }"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = []
    for line in (proc.stdout or "").splitlines():
        item = " ".join(line.split())
        if not item:
            continue
        folded = item.casefold()
        if "windows" in folded and "firewall" in folded:
            continue
        names.append(item)
    return names


def _record_own_address(rows, launch, host: str, name: str) -> None:
    """This computer's own addresses are INFO. Windows treats them like loopback for an AppContainer."""
    safe = _ipv4(host)
    if not safe:
        rows.append((name, "INFO", "this computer's own address was not probed"))
        return
    proc = launch(_tcp_script(safe, 9, "OWN", 2000))
    heard, _code, _started = _child_result(proc)
    if "OWN ok" in heard:
        detail = f"{safe} answered; an AppContainer treats this computer's own addresses like loopback"
    else:
        detail = f"{safe} is treated like loopback for an AppContainer; no exemption was added"
    rows.append((name, "INFO", detail))


def _record_web(rows, launch) -> None:
    """Own addresses are INFO. An outside TCP connect is a warning when a firewall blocks it."""
    _record_own_address(rows, launch, "127.0.0.1", "loopback")
    host = _host_lan_ip()
    if host:
        _record_own_address(rows, launch, host, "lan")
    else:
        rows.append(("lan", "INFO", "this computer has no other address of its own"))
    proc = launch(_tcp_script("1.1.1.1", 443, "WEB", 4000))
    heard, code, started = _child_result(proc)
    if started and "WEB ok" in heard:
        rows.append(("web", "PASS", "connected to 1.1.1.1"))
        return
    if not started:
        rows.append(("web", "WARN", f"the child did not run (exit {_code_text(code)})"))
        return
    firewalls = third_party_firewalls()
    if firewalls:
        rows.append(("web", "WARN", f"{FIREWALL_WARN} ({', '.join(firewalls)})"))
        return
    detail = " ".join(heard.split())[:160] or "the container could not reach 1.1.1.1"
    rows.append(("web", "WARN", detail))
