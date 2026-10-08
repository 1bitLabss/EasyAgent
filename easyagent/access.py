"""Who may open EasyAgent.

The computer running the process can always open it. A phone on the
network can open it only when EASYAGENT_TOKEN is set and the request
presents that token. The peer address comes from the socket. Forwarded
headers are ignored.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import socket
from pathlib import Path

# Starlette's TestClient uses the host name "testclient". That value is not
# a TCP address a phone can choose.
_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


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


def is_local(host: str | None) -> bool:
    if not host:
        return False
    cleaned = host.split("%", 1)[0].strip().lower()
    if cleaned.startswith("::ffff:"):
        cleaned = cleaned.removeprefix("::ffff:")
    return cleaned in _LOCAL


def presented_token(headers) -> str:
    direct = (headers.get("x-easyagent-token") or "").strip()
    if direct:
        return direct
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def access_decision(host: str | None, presented: str) -> str:
    """allow, refuse (no token configured), or need_token."""
    if is_local(host):
        return "allow"
    expected = configured_token()
    if not expected:
        return "refuse"
    if token_matches(presented, expected):
        return "allow"
    return "need_token"


def token_matches(presented: str, expected: str) -> bool:
    if not presented or not expected:
        return False
    left = hashlib.sha256(presented.encode()).digest()
    right = hashlib.sha256(expected.encode()).digest()
    return secrets.compare_digest(left, right)


def is_shell(path: str) -> bool:
    """The page and its scripts. No transcript is in these files."""
    return path in {"/", "/favicon.ico"} or path.startswith("/static/")


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


def refusal_html() -> str:
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
  <h1>Remote access is off.</h1>
  <p>This browser is not on the computer running EasyAgent. Set <code>EASYAGENT_TOKEN</code> there, start the app again, and open this address. Type that token on the page. Chats stay on that computer.</p>
</main>
</body>
</html>
"""
