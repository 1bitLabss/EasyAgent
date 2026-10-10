"""Content-Length frames for a Model Context Protocol session."""

from __future__ import annotations

import json


def encode(payload: dict) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def read_message(stream) -> dict | None:
    """One JSON object, or None at end of stream."""
    headers = b""
    while b"\r\n\r\n" not in headers:
        chunk = stream.read(1)
        if not chunk:
            return None
        headers += chunk
        if len(headers) > 8192:
            raise ValueError("The connector header was too long.")
    head, _sep, extra = headers.partition(b"\r\n\r\n")
    length = 0
    for line in head.decode("ascii", "replace").split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    if length <= 0 or length > 2_000_000:
        raise ValueError("The connector sent a bad length.")
    body = extra
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    data = json.loads(body[:length].decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("The connector did not send an object.")
    return data
