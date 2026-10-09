"""Phone access on the home network.

The computer running EasyAgent can always open it. A phone on the same
LAN can open it only after Phone access is turned on and the request
presents a pairing token. An address outside the LAN is refused, token
or not. The peer address comes from the socket. Forwarded headers are
ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from easyagent.access import configured_token, is_lan, is_local, lan_ip, token_matches

FIREWALL_HINT = (
    "Windows Defender Firewall, Advanced settings, Inbound Rules: "
    "allow TCP port 44721 from the local subnet. "
    "The button adds that rule when you agree."
)


def device_name(user_agent: str) -> str:
    text = (user_agent or "").lower()
    if "iphone" in text:
        return "iPhone"
    if "ipad" in text:
        return "iPad"
    if "android" in text:
        return "Android"
    return "Phone"


def _chmod(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        return


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class PhoneBook:
    """phone.json next to the chats. The invite is the QR. Devices keep a hash."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "phone.json"
        self._lock = threading.Lock()
        self.enabled = False
        self.invite = ""
        self.devices: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        self.enabled = bool(raw.get("enabled"))
        self.invite = str(raw.get("invite") or "")
        devices = raw.get("devices") or []
        kept = []
        if isinstance(devices, list):
            for item in devices:
                if not isinstance(item, dict):
                    continue
                digest = str(item.get("token_sha256") or "")
                ident = str(item.get("id") or "")
                if not digest or not ident:
                    continue
                kept.append(
                    {
                        "id": ident,
                        "name": str(item.get("name") or "Phone"),
                        "token_sha256": digest,
                        "paired_at": str(item.get("paired_at") or ""),
                    }
                )
        self.devices = kept

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "enabled": self.enabled,
            "invite": self.invite,
            "devices": self.devices,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        _chmod(tmp)
        tmp.replace(self.path)
        _chmod(self.path)

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = bool(enabled)
            if self.enabled and not self.invite:
                self.invite = secrets.token_urlsafe(24)
            self._save()

    def accepts(self, presented: str) -> bool:
        if not self.enabled or not presented:
            return False
        with self._lock:
            if self.invite and token_matches(presented, self.invite):
                return True
            digest = hashlib.sha256(presented.encode()).hexdigest()
            for device in self.devices:
                if secrets.compare_digest(digest, device["token_sha256"]):
                    return True
            extra = configured_token()
            if extra and token_matches(presented, extra):
                return True
        return False

    def claim_if_invite(self, presented: str, user_agent: str) -> bool:
        """The first use of the QR token becomes one device, and the QR rotates."""
        if not presented:
            return False
        with self._lock:
            if not self.enabled or not self.invite or not token_matches(presented, self.invite):
                return False
            self.devices.append(
                {
                    "id": secrets.token_hex(8),
                    "name": device_name(user_agent),
                    "token_sha256": hashlib.sha256(presented.encode()).hexdigest(),
                    "paired_at": _now(),
                }
            )
            self.invite = secrets.token_urlsafe(24)
            self._save()
            return True

    def revoke(self, device_id: str) -> None:
        with self._lock:
            kept = [item for item in self.devices if item["id"] != device_id]
            if len(kept) == len(self.devices):
                raise KeyError(device_id)
            self.devices = kept
            self._save()

    def public_devices(self) -> list[dict]:
        with self._lock:
            return [
                {"id": item["id"], "name": item["name"], "paired_at": item["paired_at"]}
                for item in self.devices
            ]

    def pair_url(self, port: int) -> str:
        with self._lock:
            if not self.enabled or not self.invite:
                return ""
            ip = lan_ip()
            if not ip:
                return ""
            return f"http://{ip}:{port}/?pair={self.invite}"


def access_for(host: str | None, presented: str, phone: PhoneBook) -> str:
    """allow, refuse, or need_token."""
    if is_local(host):
        return "allow"
    if not is_lan(host) or not phone.enabled:
        return "refuse"
    if phone.accepts(presented):
        return "allow"
    return "need_token"


def status_payload(phone: PhoneBook, port: int, *, listening: bool) -> dict:
    url = phone.pair_url(port)
    return {
        "enabled": phone.enabled,
        "lan_ip": lan_ip(),
        "port": port,
        "pair_url": url,
        "listening": listening,
        "devices": phone.public_devices(),
        "firewall": {"hint": FIREWALL_HINT, "windows": sys.platform == "win32"},
    }


def add_firewall_rule(port: int, consent: bool, *, platform_name: str | None = None, runner=None) -> dict:
    """Add the inbound rule only on Windows, and only after consent."""
    system = sys.platform if platform_name is None else platform_name
    if not consent:
        return {"added": False, "detail": "Nothing was changed. Agree to add the Windows Firewall rule."}
    if system != "win32":
        return {"added": False, "detail": "This computer is not Windows, so no firewall rule was added."}
    command = [
        "netsh",
        "advfirewall",
        "firewall",
        "add",
        "rule",
        "name=EasyAgent phone",
        "dir=in",
        "action=allow",
        "protocol=TCP",
        f"localport={int(port)}",
        "remoteip=localsubnet",
    ]
    if runner is None:
        import subprocess

        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        code = completed.returncode
    else:
        completed = runner(command)
        code = getattr(completed, "returncode", 0)
    if code:
        return {"added": False, "detail": "Windows Firewall did not add the rule."}
    return {"added": True, "detail": "Windows Firewall allows TCP 44721 from the local subnet."}


def launcher_firewall(enabled: bool, port: int) -> dict | None:
    """EASYAGENT_FIREWALL=1 on the launcher is consent. Anything else does nothing."""
    if not enabled or os.environ.get("EASYAGENT_FIREWALL", "").strip() != "1":
        return None
    return add_firewall_rule(port, consent=True)


def qr_svg(text: str) -> str:
    import segno

    code = segno.make(text, error="h")
    return code.svg_inline(scale=4, dark="#1c1b19", light="#f6f4ef")


class LanPort:
    """A second socket on the LAN address. Loopback stays the other socket."""

    def __init__(self) -> None:
        self.listener = None

    @property
    def listening(self) -> bool:
        return self.listener is not None

    async def apply(self, server, port: int, enabled: bool) -> bool:
        await self.close(server)
        if not enabled or server is None:
            return False
        host = lan_ip()
        if not host:
            return False
        primary = getattr(getattr(server, "config", None), "host", None)
        if primary in {None, "0.0.0.0", "::", host}:
            return False
        self.listener = await _listen_beside(server, host, int(port))
        return self.listener is not None

    async def close(self, server) -> None:
        listener = self.listener
        if listener is None:
            return
        self.listener = None
        listener.close()
        await listener.wait_closed()
        servers = getattr(server, "servers", None) if server is not None else None
        if servers is not None and listener in servers:
            servers.remove(listener)


async def _listen_beside(server, host: str, port: int):
    import asyncio

    config = server.config
    loop = asyncio.get_running_loop()

    def create_protocol(_loop=None):
        return config.http_protocol_class(
            config=config,
            server_state=server.server_state,
            app_state=server.lifespan.state,
            _loop=_loop,
        )

    listener = await loop.create_server(
        create_protocol,
        host=host,
        port=port,
        ssl=getattr(config, "ssl", None),
        backlog=config.backlog,
    )
    servers = getattr(server, "servers", None)
    if servers is not None:
        servers.append(listener)
    return listener
