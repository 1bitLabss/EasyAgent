"""Home side of the relay. The computer dials out. Replies come from the local app."""

from __future__ import annotations

import asyncio
import base64
import json
import os

from easyagent.access import configured_token


def websocket_url(http_url: str) -> str:
    url = (http_url or "").strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :] + "/agent"
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :] + "/agent"
    raise ValueError("EASYAGENT_RELAY_URL must start with http:// or https://.")


async def call_app(app, method: str, path: str, headers: dict, body: bytes):
    """Run one request inside the local app. The peer is this computer, not the phone."""
    raw_path, _, query = path.partition("?")
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    status = 500
    response_headers: dict[str, str] = {}
    chunks: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
            for key, value in message.get("headers") or []:
                name = key.decode("latin1").lower()
                if name in {"content-type", "cache-control"}:
                    response_headers[name] = value.decode("latin1")
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body") or b"")

    encoded = []
    for key, value in headers.items():
        if value:
            encoded.append((key.lower().encode("latin1"), str(value).encode("latin1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": (method or "GET").upper(),
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode("ascii", "ignore"),
        "query_string": query.encode("ascii", "ignore"),
        "headers": encoded,
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 80),
    }
    await app(scope, receive, send)
    return status, response_headers, b"".join(chunks)


async def relay_loop(app, stop: asyncio.Event) -> None:
    url = os.environ.get("EASYAGENT_RELAY_URL", "").strip()
    if not url:
        return
    token = configured_token()
    if not token:
        print("EASYAGENT_RELAY_URL is set and EASYAGENT_TOKEN is empty. Not dialing.", flush=True)
        return
    try:
        ws_url = websocket_url(url)
    except ValueError as exc:
        print(str(exc), flush=True)
        return
    print(f"Dialing relay {url}", flush=True)
    while not stop.is_set():
        try:
            await run_relay_session(app, ws_url, token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Relay disconnected ({exc.__class__.__name__}). Retrying.", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=3)
        except asyncio.TimeoutError:
            continue


async def run_relay_session(app, ws_url: str, token: str) -> None:
    import websockets

    async with websockets.connect(ws_url, max_size=12_000_000) as socket:
        await socket.send(json.dumps({"type": "hello", "token": token}))
        hello = json.loads(await socket.recv())
        if hello.get("type") != "ok":
            print("Relay refused the token.", flush=True)
            await asyncio.sleep(5)
            return
        print("Relay connected.", flush=True)
        async for raw in socket:
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("type") != "http":
                continue
            await _answer(app, socket, message)


async def _answer(app, socket, message: dict) -> None:
    request_id = message.get("id")
    try:
        body = base64.b64decode(message.get("body") or "", validate=True)
        status, headers, payload = await call_app(
            app,
            message.get("method") or "GET",
            message.get("path") or "/",
            message.get("headers") or {},
            body,
        )
    except Exception:
        status, headers, payload = 502, {"content-type": "application/json"}, b'{"detail":"The computer could not answer."}'
    await socket.send(
        json.dumps(
            {
                "type": "http",
                "id": request_id,
                "status": status,
                "headers": headers,
                "body": base64.b64encode(payload).decode("ascii"),
            }
        )
    )
