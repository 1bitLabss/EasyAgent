"""A relay with no data directory.

EasyAgent at home dials out and holds one socket. The phone talks only to
this process. Bodies exist in memory while a response is in flight, then
they are dropped. Nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from easyagent.access import (
    configured_token,
    contained_file,
    is_shell,
    presented_token,
    standalone_font_css,
    token_matches,
)

FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"

MAX_BODY = 8_000_000


class Offline(Exception):
    pass


class Idle(Exception):
    """The home socket is still up, but no chunk arrived."""


_END = object()
_FAILED = object()


class _Flight:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.status = 200
        self.headers: dict[str, str] = {}
        self.started = False

    def fail(self, exc: BaseException) -> None:
        del exc
        self.queue.put_nowait(_FAILED)


class Hub:
    """One home connection and the in-flight replies waiting on it."""

    def __init__(self):
        self.socket: WebSocket | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.flights: dict[str, "_Flight"] = {}
        self._send_lock = asyncio.Lock()

    @property
    def online(self) -> bool:
        return self.socket is not None

    async def attach(self, socket: WebSocket) -> None:
        previous = self.socket
        self.socket = socket
        if previous is not None and previous is not socket:
            self._fail_pending()
            try:
                await previous.close()
            except Exception:
                pass

    def detach(self, socket: WebSocket) -> None:
        if self.socket is socket:
            self.socket = None
            self._fail_pending()

    def _fail_pending(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(Offline())
        self.pending.clear()
        for flight in self.flights.values():
            flight.fail(Offline())
        self.flights.clear()

    async def forward(self, method: str, path: str, headers: dict, body: bytes):
        """Collect one reply. Idle gaps refresh the wait. The socket being gone is offline."""
        status, resp_headers, chunks = 502, {}, []
        async for kind, payload in self.iter_response(method, path, headers, body):
            if kind == "start":
                status, resp_headers = payload
            elif kind == "chunk" and payload:
                chunks.append(payload)
        return status, resp_headers, b"".join(chunks)

    async def iter_response(self, method: str, path: str, headers: dict, body: bytes):
        socket = self.socket
        if socket is None:
            raise Offline()
        request_id = uuid.uuid4().hex
        flight = _Flight()
        self.flights[request_id] = flight
        payload = {
            "type": "http",
            "id": request_id,
            "method": method,
            "path": path,
            "headers": headers,
            "body": base64.b64encode(body).decode("ascii"),
        }
        try:
            async with self._send_lock:
                if self.socket is not socket:
                    raise Offline()
                await socket.send_json(payload)
            while True:
                try:
                    item = await asyncio.wait_for(flight.queue.get(), timeout=60)
                except asyncio.TimeoutError as exc:
                    raise TimeoutError("The home computer sent nothing for 60 s.") from exc
                if item is _END:
                    if not flight.started:
                        raise Offline()
                    return
                if item is _FAILED:
                    raise Offline()
                if isinstance(item, tuple) and item and item[0] == "start":
                    flight.started = True
                    yield "start", (flight.status, flight.headers)
                    continue
                if isinstance(item, (bytes, bytearray)):
                    yield "chunk", bytes(item)
        finally:
            self.flights.pop(request_id, None)

    def finish(self, message: dict) -> None:
        """A whole reply in one frame, or a start / chunk / end / ping frame."""
        request_id = str(message.get("id") or "")
        flight = self.flights.get(request_id)
        future = self.pending.get(request_id)
        kind = str(message.get("type") or "http")
        if flight is None and future is None:
            return
        if kind == "ping":
            if flight is not None:
                flight.queue.put_nowait(b"")
            return
        if kind == "start":
            if flight is None:
                return
            flight.status = int(message.get("status") or 502)
            headers = message.get("headers") if isinstance(message.get("headers"), dict) else {}
            flight.headers = {str(key).lower(): str(value) for key, value in headers.items()}
            flight.queue.put_nowait(("start", None))
            return
        if kind == "chunk":
            if flight is None:
                return
            try:
                body = base64.b64decode(message.get("body") or "", validate=True)
            except Exception:
                flight.fail(Offline())
                return
            flight.queue.put_nowait(body)
            return
        if kind == "end":
            if flight is not None:
                flight.queue.put_nowait(_END)
            return
        try:
            body = base64.b64decode(message.get("body") or "", validate=True)
        except Exception:
            if flight is not None:
                flight.fail(Offline())
            if future is not None and not future.done():
                future.set_exception(Offline())
            return
        headers = message.get("headers") if isinstance(message.get("headers"), dict) else {}
        status = int(message.get("status") or 502)
        if future is not None and not future.done():
            future.set_result((status, headers, body))
        if flight is not None:
            flight.status = status
            flight.headers = {str(key).lower(): str(value) for key, value in headers.items()}
            flight.queue.put_nowait(("start", None))
            if body:
                flight.queue.put_nowait(body)
            flight.queue.put_nowait(_END)


def create_relay() -> FastAPI:
    app = FastAPI(title="EasyAgent relay")
    app.state.hub = Hub()

    @app.websocket("/agent")
    async def agent_socket(socket: WebSocket):
        await socket.accept()
        try:
            hello = await socket.receive_json()
        except Exception:
            await socket.close(code=4401)
            return
        token = configured_token()
        presented = hello.get("token") if isinstance(hello, dict) else ""
        if hello.get("type") != "hello" or not token or not token_matches(str(presented or ""), token):
            try:
                await socket.send_json({"type": "refused"})
            except Exception:
                pass
            await socket.close(code=4401)
            return
        await socket.send_json({"type": "ok"})
        await app.state.hub.attach(socket)
        try:
            while True:
                message = await socket.receive_json()
                if isinstance(message, dict) and message.get("type") in {"http", "start", "chunk", "end", "ping"}:
                    app.state.hub.finish(message)
        except WebSocketDisconnect:
            pass
        finally:
            app.state.hub.detach(socket)

    @app.middleware("http")
    async def phone(request: Request, call_next):
        return await handle_phone(app.state.hub, request)

    return app


def font_file(path: str) -> Path | None:
    """A face file shipped with the relay. Not a transcript.

    ``Path.relative_to`` is case-sensitive. On Windows, ``resolve()`` can
    change the drive-letter case and that check then hides a font that is
    on disk. The offline page would answer 503 instead of the face.
    """
    prefix = "/static/fonts/"
    if not path.startswith(prefix):
        return None
    name = path[len(prefix):].replace("\\", "/")
    if not name or "/" in name or name.startswith(".") or not name.endswith(".woff2"):
        return None
    return contained_file(FONT_DIR, name)


async def handle_phone(hub: Hub, request: Request):
    headers = {"Cache-Control": "no-store"}
    if request.url.path == "/healthz":
        return JSONResponse({"ok": True, "agent": hub.online}, headers=headers)
    face = font_file(request.url.path) if request.method == "GET" else None
    if face is not None:
        return Response(
            content=face.read_bytes(),
            media_type="font/woff2",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    token = configured_token()
    if not token:
        detail = "Set EASYAGENT_TOKEN on the relay. No chat was sent."
        if _html(request):
            return HTMLResponse(_simple_page("Relay token is not set.", detail), status_code=403, headers=headers)
        return JSONResponse({"detail": detail}, status_code=403, headers=headers)
    if not hub.online:
        return _offline(request)
    path = request.url.path
    if not is_shell(path) and not token_matches(presented_token(request.headers), token):
        return JSONResponse(
            {"detail": "This network needs the shared token."},
            status_code=401,
            headers=headers,
        )
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"detail": "Request is too large."}, status_code=413, headers=headers)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    forwarded = {
        "accept": request.headers.get("accept", ""),
        "content-type": request.headers.get("content-type", ""),
    }
    try:
        stream = hub.iter_response(request.method, target, forwarded, body)
        kind, payload = await stream.__anext__()
    except StopAsyncIteration:
        return _offline(request)
    except Offline:
        return _offline(request)
    except TimeoutError:
        return JSONResponse(
            {"detail": "EasyAgent is quiet. The computer at home did not send the next part."},
            status_code=504,
            headers=headers,
        )
    if kind != "start":
        return _offline(request)
    status, resp_headers = payload
    safe = {"cache-control": "no-store"}
    for key, value in resp_headers.items():
        if str(key).lower() == "content-type" and value:
            safe["content-type"] = str(value)

    async def chunks():
        sent = 0
        try:
            async for part_kind, part in stream:
                if part_kind != "chunk" or not part:
                    continue
                sent += len(part)
                if sent > MAX_BODY:
                    return
                yield part
        except Offline:
            return
        except TimeoutError:
            return

    from fastapi.responses import StreamingResponse

    return StreamingResponse(chunks(), status_code=status, headers=safe)


def _html(request: Request) -> bool:
    if request.url.path == "/":
        return True
    return "text/html" in request.headers.get("accept", "")


def _offline(request: Request):
    headers = {"Cache-Control": "no-store"}
    detail = "EasyAgent is offline. The computer at home is not connected."
    if _html(request):
        return HTMLResponse(offline_html(), status_code=503, headers=headers)
    return JSONResponse({"detail": detail, "offline": True}, status_code=503, headers=headers)


def offline_html() -> str:
    return _simple_page(
        "EasyAgent is offline.",
        "The computer at home is not connected. Chats stay there. This relay is not holding them. Reload when that computer is back.",
    )


def _simple_page(title: str, copy: str) -> str:
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
</style>
</head>
<body>
<main>
  <h1>{title}</h1>
  <p>{copy}</p>
</main>
</body>
</html>
"""


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT") or os.environ.get("EASYAGENT_RELAY_PORT", "44731"))
    host = os.environ.get("EASYAGENT_RELAY_HOST", "0.0.0.0")
    print(f"EasyAgent relay listening on {host}:{port}", flush=True)
    print("This process has no data directory.", flush=True)
    if configured_token():
        print("Phone URL is this host. The token is EASYAGENT_TOKEN. It is not printed here.", flush=True)
    else:
        print("Set EASYAGENT_TOKEN before a phone or the home computer connects.", flush=True)
    uvicorn.run(create_relay(), host=host, port=port)


if __name__ == "__main__":
    main()
