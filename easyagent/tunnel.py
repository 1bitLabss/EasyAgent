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


def _scope(method: str, path: str, headers: dict) -> tuple[dict, str]:
    raw_path, _, query = path.partition("?")
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
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
    return scope, raw_path


async def call_app(app, method: str, path: str, headers: dict, body: bytes):
    """Run one request inside the local app. The peer is this computer, not the phone."""
    status, response_headers, chunks = 500, {}, []
    async for kind, payload in iter_app(app, method, path, headers, body):
        if kind == "start":
            status, response_headers = payload
        elif kind == "chunk" and payload:
            chunks.append(payload)
    return status, response_headers, b"".join(chunks)


async def iter_app(app, method: str, path: str, headers: dict, body: bytes):
    """Yield start, then each body chunk, as the local app writes them."""
    scope, _raw_path = _scope(method, path, headers)
    delivered = False
    finished = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        # put_nowait: the phone's socket must not stall the model read.
        # The queue is unbounded. A slow viewer grows it; the read keeps going.
        if message["type"] == "http.response.start":
            status = int(message["status"])
            response_headers: dict[str, str] = {}
            for key, value in message.get("headers") or []:
                name = key.decode("latin1").lower()
                if name in {"content-type", "cache-control"}:
                    response_headers[name] = value.decode("latin1")
            queue.put_nowait(("start", (status, response_headers)))
        elif message["type"] == "http.response.body":
            chunk = message.get("body") or b""
            if chunk:
                queue.put_nowait(("chunk", chunk))
            if not message.get("more_body", False):
                queue.put_nowait(("end", None))

    async def run():
        try:
            await app(scope, receive, send)
        finally:
            finished.set()
            await queue.put(("end", None))

    task = asyncio.create_task(run())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "end":
                return
            yield kind, payload
    finally:
        if not task.done():
            task.cancel()
        with_suppress = True
        del with_suppress
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


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
        send_lock = asyncio.Lock()
        async for raw in socket:
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("type") != "http":
                continue
            asyncio.create_task(_answer(app, socket, message, send_lock))


async def _send_frame(socket, send_lock: asyncio.Lock, payload: dict) -> None:
    async with send_lock:
        await socket.send(json.dumps(payload))


async def _answer(app, socket, message: dict, send_lock: asyncio.Lock) -> None:
    request_id = message.get("id")
    try:
        body = base64.b64decode(message.get("body") or "", validate=True)
        started = False
        async for kind, payload in iter_app(
            app,
            message.get("method") or "GET",
            message.get("path") or "/",
            message.get("headers") or {},
            body,
        ):
            if kind == "start":
                status, headers = payload
                await _send_frame(socket, send_lock, {
                    "type": "start",
                    "id": request_id,
                    "status": status,
                    "headers": headers,
                })
                started = True
            elif kind == "chunk" and payload:
                await _send_frame(socket, send_lock, {
                    "type": "chunk",
                    "id": request_id,
                    "body": base64.b64encode(payload).decode("ascii"),
                })
        if not started:
            raise RuntimeError("no response")
        await _send_frame(socket, send_lock, {"type": "end", "id": request_id})
    except Exception:
        await _send_frame(socket, send_lock, {
            "type": "http",
            "id": request_id,
            "status": 502,
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(b'{"detail":"The computer could not answer."}').decode("ascii"),
        })
