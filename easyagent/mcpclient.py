"""MCP client for a stdio process or a streamable HTTP endpoint.

A stdio server is launched with binary pipes. When OS containment is on, that
process uses the same wrapper as the bot shell. Secrets in ``env_extra`` are
for this process only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import httpx

from easyagent import __version__
from easyagent.mcpframe import encode, read_message

_BLOCKED_ENV = {
    "DBUS_SESSION_BUS_ADDRESS",
    "DBUS_SESSION_BUS_PID",
    "EASYAGENT_SECRETS_PASSPHRASE",
    "EASYAGENT_SECRETS_PASSFILE",
    "EASYAGENT_VAULT_KEY_FILE",
    "EASYAGENT_KEYRING",
}


class McpError(RuntimeError):
    pass


def _result_text(result: Any) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            if parts:
                return "\n".join(parts)
        if result.get("isError"):
            return "The connector reported an error."
    return json.dumps(result, default=str)[:12000]


def server_cwd(command: list[str]) -> str:
    """The folder the server was given. The parent process working directory is not used."""
    parts = [str(part) for part in command]
    for flag in ("--root", "--file"):
        if flag not in parts:
            continue
        index = parts.index(flag)
        if index + 1 >= len(parts):
            continue
        path = Path(parts[index + 1]).expanduser()
        try:
            path = path.resolve()
        except OSError:
            pass
        if path.is_file():
            path = path.parent
        if path.is_dir():
            return str(path)
    return tempfile.mkdtemp(prefix="easyagent-mcp-")


def process_env(store, extra: dict[str, str] | None) -> dict[str, str]:
    """Parent environment without keychain material, then this connector's own values."""
    if store is not None:
        from easyagent.secrets import scrub_env

        clean = scrub_env(store, os.environ.copy())
    else:
        clean = {key: value for key, value in os.environ.items() if key not in _BLOCKED_ENV}
    # The connector root is not the checkout. Keep the package importable anyway.
    package_parent = str(Path(__file__).resolve().parents[1])
    current = clean.get("PYTHONPATH", "")
    parts = [package_parent] if package_parent else []
    if current:
        parts.extend(part for part in current.split(os.pathsep) if part and part not in parts)
    if parts:
        clean["PYTHONPATH"] = os.pathsep.join(parts)
    for key, value in (extra or {}).items():
        if key and value:
            clean[str(key)] = str(value)
    return clean


def mcp_bwrap_argv(store, bot_id: str | None, command: list[str], roots: list[Path]) -> list[str]:
    """Bubblewrap a connector. The data directory is hidden. The connector root stays writable."""
    from easyagent.sandbox import web_enabled, work_folders
    from easyagent.secrets import keychain_paths

    empty = Path(tempfile.gettempdir()) / "easyagent-seal-empty"
    empty.mkdir(parents=True, exist_ok=True)
    argv = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/"]
    if not web_enabled():
        argv.append("--unshare-net")
    try:
        root = Path(store.root).resolve()
    except OSError:
        root = Path(store.root)
    # Hide the data directory after the root bind. A later bind of a parent
    # such as /tmp would put it back, so the workspace is bound after the seal.
    argv += ["--bind", str(empty), str(root)]
    bound: list[Path] = []
    for folder in list(work_folders(store, bot_id)) + list(roots):
        try:
            folder = folder.resolve()
        except OSError:
            pass
        if folder in bound:
            continue
        bound.append(folder)
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
    argv += ["--dev", "/dev", "--proc", "/proc", "--", *command]
    return argv


def _allow_roots(store, bot_id: str | None, cwd: str) -> list[Path]:
    from easyagent.sandbox import work_folders

    roots = list(work_folders(store, bot_id))
    extra = Path(cwd)
    if extra not in roots:
        roots.append(extra)
    return roots


def _popen_contained(store, bot_id, command: list[str], env: dict, cwd: str):
    """Start the connector inside the same container as the shell. Raises OSError on failure."""
    roots = _allow_roots(store, bot_id, cwd)
    if os.name == "nt":
        from easyagent.contain import popen_mcp
        from easyagent.sandbox import web_enabled

        return popen_mcp(
            command,
            env=env,
            cwd=cwd,
            data_root=Path(store.root),
            allow=roots,
            web=web_enabled(),
        )
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        from easyagent.sandbox import macos_profile

        profile = macos_profile(roots, Path(store.root))
        path = Path(tempfile.gettempdir()) / "easyagent-mcp.sb"
        path.write_text(profile, encoding="utf-8")
        return subprocess.Popen(
            ["sandbox-exec", "-f", str(path), *command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    if shutil.which("bwrap"):
        return subprocess.Popen(
            mcp_bwrap_argv(store, bot_id, command, roots),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    from easyagent.sandbox import apply_landlock, landlock_available, web_enabled

    if landlock_available():
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            preexec_fn=lambda: apply_landlock(store, bot_id, web=web_enabled(), extra_write=roots),
        )
    raise OSError("bubblewrap is not installed and Landlock is not available")


def launch_stdio(command: list[str], env: dict, cwd: str, store=None, bot_id: str | None = None):
    """Binary stdio pipes. Containment when it is on. A failed container falls back with a notice."""
    notice = ""
    mode = {"contained": False}
    if store is not None:
        from easyagent.sandbox import shell_mode

        mode = shell_mode(store)
    if mode.get("contained"):
        try:
            proc = _popen_contained(store, bot_id, command, env, cwd)
            return proc, notice
        except OSError as exc:
            from easyagent.contain import AclError

            if isinstance(exc, AclError):
                raise
            notice = f"OS containment is unavailable: {exc}. This connector used the file guards."
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
    )
    return proc, notice


class StdioClient:
    def __init__(self, command: list[str], env_extra: dict[str, str] | None = None, *, store=None, bot_id: str | None = None, cwd: str | None = None):
        self.notice = ""
        folder = cwd or server_cwd(command)
        env = process_env(store, env_extra)
        self.proc, self.notice = launch_stdio(command, env, folder, store, bot_id)
        self._id = 0
        self._lock = threading.Lock()
        self._stderr = ""
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        if self.proc.stderr is None:
            return
        try:
            data = self.proc.stderr.read(4000)
        except Exception:
            return
        if isinstance(data, bytes):
            self._stderr = data.decode("utf-8", "replace")
        else:
            self._stderr = str(data)

    def request(self, method: str, params: dict | None = None, timeout: float = 20) -> Any:
        with self._lock:
            self._id += 1
            msg_id = self._id
            payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
            if self.proc.stdin is None or self.proc.stdout is None:
                raise McpError("The connector is not running.")
            self.proc.stdin.write(encode(payload))
            self.proc.stdin.flush()
            message = self._read(timeout)
        if message is None:
            detail = self._stderr.strip()
            raise McpError(detail or "The connector closed.")
        if message.get("error"):
            err = message["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            raise McpError(str(text))
        return message.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        if self.proc.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self.proc.stdin.write(encode(payload))
        self.proc.stdin.flush()

    def _read(self, timeout: float) -> dict | None:
        box: dict = {}

        def run() -> None:
            try:
                box["message"] = read_message(self.proc.stdout)
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise McpError("The connector took too long.")
        if box.get("error"):
            raise McpError(str(box["error"]))
        return box.get("message")

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                return


class HttpClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = {key: value for key, value in (headers or {}).items() if key and value}
        self.session = ""
        self.notice = ""

    def request(self, method: str, params: dict | None = None, timeout: float = 20) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.headers,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        try:
            response = httpx.post(self.url, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise McpError("The connector did not answer.") from exc
        if response.status_code >= 400:
            raise McpError(f"The connector returned {response.status_code}.")
        session = response.headers.get("mcp-session-id") or response.headers.get("Mcp-Session-Id") or ""
        if session:
            self.session = session
        message = _http_body(response)
        if message.get("error"):
            err = message["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            raise McpError(str(text))
        return message.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        headers = {"Content-Type": "application/json", **self.headers}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        try:
            httpx.post(self.url, json=payload, headers=headers, timeout=10)
        except httpx.HTTPError:
            return

    def close(self) -> None:
        return


def _http_body(response: httpx.Response) -> dict:
    kind = (response.headers.get("content-type") or "").lower()
    if "text/event-stream" in kind:
        last = None
        for line in response.text.splitlines():
            if line.startswith("data:"):
                raw = line.split(":", 1)[1].strip()
                if raw and raw != "[DONE]":
                    try:
                        last = json.loads(raw)
                    except ValueError:
                        continue
        if isinstance(last, dict):
            return last
        raise McpError("The connector stream had no result.")
    try:
        data = response.json()
    except ValueError as exc:
        raise McpError("The connector did not return JSON.") from exc
    if not isinstance(data, dict):
        raise McpError("The connector did not return an object.")
    return data


def session_for(
    record: dict,
    env_extra: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    *,
    store=None,
    bot_id: str | None = None,
    cwd: str | None = None,
):
    if record.get("transport") == "http":
        return HttpClient(str(record.get("url") or ""), headers)
    command = [str(part) for part in (record.get("command") or []) if str(part).strip()]
    if not command:
        raise McpError("This connector has no command.")
    return StdioClient(command, env_extra, store=store, bot_id=bot_id, cwd=cwd)


def handshake(client) -> None:
    client.request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "EasyAgent", "version": __version__},
    })
    client.notify("notifications/initialized", {})


def list_tools(
    record: dict,
    env_extra: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    *,
    store=None,
    bot_id: str | None = None,
    cwd: str | None = None,
) -> list[dict]:
    client = session_for(record, env_extra, headers, store=store, bot_id=bot_id, cwd=cwd)
    try:
        handshake(client)
        result = client.request("tools/list", {})
    finally:
        client.close()
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return []
    found = []
    for tool in tools:
        if isinstance(tool, dict) and str(tool.get("name") or "").strip():
            found.append({
                "name": str(tool["name"]).strip(),
                "description": str(tool.get("description") or "").strip()[:400],
            })
    return found


def call_tool(
    record: dict,
    name: str,
    arguments: dict,
    env_extra: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    *,
    store=None,
    bot_id: str | None = None,
    cwd: str | None = None,
) -> str:
    client = session_for(record, env_extra, headers, store=store, bot_id=bot_id, cwd=cwd)
    try:
        handshake(client)
        result = client.request("tools/call", {"name": name, "arguments": arguments or {}})
        text = _result_text(result)
    finally:
        notice = getattr(client, "notice", "") or ""
        client.close()
    if notice:
        return (notice + "\n" + text).strip()
    return text
