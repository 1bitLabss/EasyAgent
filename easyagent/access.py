"""Who may open EasyAgent.

The computer running the process can always open it. A phone on the home
LAN can open it only when Phone access is on and the request presents a
pairing token. An address outside that LAN is refused. The peer address
comes from the socket. Forwarded headers are ignored.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
import socket
import threading
import time
from pathlib import Path

# Starlette's TestClient uses the host name "testclient". That value is not
# a TCP address a phone can choose.
_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}
# Home networks only. Python's ipaddress.is_private also marks documentation
# ranges such as 203.0.113.0/24, and those are not a house LAN.
_LAN_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def configured_token() -> str:
    return os.environ.get("EASYAGENT_TOKEN", "").strip()


def lan_ip() -> str | None:
    """An address other phones on this network can use. Nothing is sent."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if not ip or ip.startswith("127."):
        return None
    return ip


def _cleaned(host: str | None) -> str:
    if not host:
        return ""
    cleaned = host.split("%", 1)[0].strip().lower()
    if cleaned.startswith("::ffff:"):
        cleaned = cleaned.removeprefix("::ffff:")
    return cleaned


def is_local(host: str | None) -> bool:
    return _cleaned(host) in _LOCAL


def is_lan(host: str | None) -> bool:
    """A private or link-local address. Loopback is local, not LAN."""
    cleaned = _cleaned(host)
    if not cleaned or cleaned in _LOCAL:
        return False
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    return any(ip in network for network in _LAN_NETS)


def presented_token(headers) -> str:
    direct = (headers.get("x-easyagent-token") or "").strip()
    if direct:
        return direct
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


# A person at the keyboard. Anything else is the bot, or a path the bot can steer.
_YOLO_ACTORS = frozenset({"user-message", "user-click"})


def yolo_actor_allowed(source: str | None) -> bool:
    """True only when a person turned YOLO on for this chat.

    ``user-message`` is a slash command typed into an idle chat. ``user-click``
    is the header control. A bot, a steered line, a bridge, a routine, a helper,
    a schedule, or a resume is refused. The caller passes the source. A request
    body cannot name it.
    """
    return (source or "").strip() in _YOLO_ACTORS


def access_decision(host: str | None, presented: str, phone=None) -> str:
    """allow, refuse, or need_token.

    Loopback is allowed. Anything outside the home LAN is refused, even
    with a token. A LAN phone needs Phone access and a pairing token.
    """
    if is_local(host):
        return "allow"
    if not is_lan(host):
        return "refuse"
    if phone is None or not getattr(phone, "enabled", False):
        return "refuse"
    accepts = getattr(phone, "accepts", None)
    if accepts and accepts(presented):
        return "allow"
    return "need_token"


class TokenGuessGuard:
    """A per-address counter of wrong tokens. In-process, no dependencies.

    After ``max_wrong`` wrong tokens inside ``window_seconds``, that address
    cannot try another token until ``lockout_seconds`` pass. Requests that
    carry no token are not counted, so the token gate stays open for someone
    who simply has not paired yet.
    """

    def __init__(
        self,
        max_wrong: int = 20,
        window_seconds: float = 300.0,
        lockout_seconds: float = 300.0,
        clock=time.monotonic,
    ) -> None:
        self.max_wrong = max(1, int(max_wrong))
        self.window_seconds = float(window_seconds)
        self.lockout_seconds = float(lockout_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._wrong: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def locked(self, host: str | None) -> bool:
        now = self._clock()
        with self._lock:
            return now < self._locked_until.get(host or "", 0.0)

    def note_wrong(self, host: str | None) -> None:
        key = host or ""
        now = self._clock()
        with self._lock:
            if now < self._locked_until.get(key, 0.0):
                self._locked_until[key] = now + self.lockout_seconds
                return
            recent = [t for t in self._wrong.get(key, []) if now - t < self.window_seconds]
            recent.append(now)
            if len(recent) >= self.max_wrong:
                self._locked_until[key] = now + self.lockout_seconds
                recent = []
            self._wrong[key] = recent

    def note_ok(self, host: str | None) -> None:
        key = host or ""
        with self._lock:
            self._wrong.pop(key, None)
            self._locked_until.pop(key, None)


def token_matches(presented: str, expected: str) -> bool:
    if not presented or not expected:
        return False
    left = hashlib.sha256(presented.encode()).digest()
    right = hashlib.sha256(expected.encode()).digest()
    return secrets.compare_digest(left, right)


def is_shell(path: str) -> bool:
    """The page and its scripts. No transcript is in these files."""
    return path in {"/", "/favicon.ico", "/classic", "/manifest.webmanifest", "/sw.js"} or path.startswith("/static/") or path.startswith("/ui/")


def contained_file(root: Path, relative: str) -> Path | None:
    """A file inside root. Windows path case must not hide a file that is there."""
    text = (relative or "").replace("\\", "/").strip().lstrip("/")
    if not text:
        return None
    parts: list[str] = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            return None
        parts.append(part)
    if not parts:
        return None
    candidate = root.joinpath(*parts)
    if not candidate.is_file():
        return None
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except OSError:
        return None
    root_key = os.path.normcase(str(root_resolved))
    file_key = os.path.normcase(str(resolved))
    sep = os.path.normcase(os.sep)
    prefix = root_key.rstrip(sep) + sep
    if not file_key.startswith(prefix):
        return None
    return resolved


def standalone_font_css() -> str:
    """Inter and JetBrains Mono, the same two faces as the main UI."""
    return """
@font-face { font-family: Inter; font-style: normal; font-weight: 100 900; font-display: swap; src: url("/static/fonts/InterVariable.woff2") format("woff2"), url("https://cdn.jsdelivr.net/fontsource/fonts/inter:vf@5.2.8/latin-wght-normal.woff2") format("woff2"); }
@font-face { font-family: "JetBrains Mono"; font-style: normal; font-weight: 400; font-display: swap; src: url("/static/fonts/JetBrainsMono-Regular.woff2") format("woff2"); }
@font-face { font-family: "JetBrains Mono"; font-style: normal; font-weight: 500; font-display: swap; src: url("/static/fonts/JetBrainsMono-Medium.woff2") format("woff2"); }
@font-face { font-family: "JetBrains Mono"; font-style: normal; font-weight: 700; font-display: swap; src: url("/static/fonts/JetBrainsMono-Bold.woff2") format("woff2"); }
"""


def refusal_html(kind: str = "public") -> str:
    if kind == "off":
        title = "Phone access is off."
        body = (
            "This phone is on the home network, and Phone access is turned off "
            "on the computer running EasyAgent. Turn it on in Settings there. "
            "Chats stay on that computer."
        )
    else:
        title = "This network is not home."
        body = (
            "EasyAgent answers the computer it runs on, and a paired phone on that "
            "home network. This address is outside that network. Chats stay on that computer."
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EasyAgent</title>
<style>
{standalone_font_css()}
  body {{ margin: 0; padding: 20px 16px; background: #f6f4ef; color: #1c1b19; font-family: Inter, ui-sans-serif, system-ui, sans-serif; font-size: 14px; line-height: 1.45; }}
  main {{ max-width: 36rem; }}
  h1 {{ margin: 0 0 8px; font-size: 16px; font-weight: 600; letter-spacing: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
  code {{ font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 0.95rem; }}
</style>
</head>
<body>
<main>
  <h1>{title}</h1>
  <p>{body}</p>
</main>
</body>
</html>
"""
