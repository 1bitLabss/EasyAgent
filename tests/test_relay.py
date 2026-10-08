"""The relay forwards. It does not keep the transcript."""

import asyncio
import base64
import json
import socket
import threading
import time

import httpx
import uvicorn

from easyagent.app import create_app
from easyagent.relay import Hub, create_relay
from easyagent.store import Store
from easyagent.tunnel import call_app, run_relay_session, websocket_url

TOKEN = "away-token"
MARKER = "keep-me-on-the-computer-relay"


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        if server.started:
            return server
        time.sleep(0.02)
    raise RuntimeError("relay did not start")


def seed(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ann", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    chat["messages"] = [{"role": "user", "content": MARKER}]
    store.save_chat(chat)
    return create_app(tmp_path), bot, chat


def test_websocket_url_switches_scheme():
    assert websocket_url("https://ea.example.com") == "wss://ea.example.com/agent"
    assert websocket_url("http://127.0.0.1:44731/") == "ws://127.0.0.1:44731/agent"


def test_hub_drops_the_body_after_the_reply():
    async def run():
        hub = Hub()

        class Fake:
            async def send_json(self, payload):
                encoded = base64.b64encode(MARKER.encode()).decode("ascii")
                hub.finish(
                    {
                        "id": payload["id"],
                        "status": 200,
                        "headers": {"content-type": "text/plain"},
                        "body": encoded,
                    }
                )

        hub.socket = Fake()
        status, _headers, body = await hub.forward("GET", "/api/bots", {}, b"")
        assert status == 200
        assert body == MARKER.encode()
        assert hub.pending == {}
        dumped = json.dumps({key: repr(value) for key, value in hub.__dict__.items()})
        assert MARKER not in dumped

    asyncio.run(run())


def test_offline_page_has_no_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_TOKEN", TOKEN)
    seed(tmp_path)
    relay = create_relay()
    port = free_port()
    server = serve(relay, port)
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/", timeout=3)
        assert response.status_code == 503
        assert "EasyAgent is offline" in response.text
        assert "Georgia" not in response.text
        assert "Inter" in response.text
        assert MARKER not in response.text
        face = httpx.get(f"http://127.0.0.1:{port}/static/fonts/InterVariable.woff2", timeout=3)
        assert face.status_code == 200
        assert face.headers["content-type"].startswith("font/woff2")
        assert face.content[:4] == b"wOF2"
        assert MARKER.encode() not in face.content
        api = httpx.get(f"http://127.0.0.1:{port}/api/bots", timeout=3)
        assert api.status_code == 503
        assert api.json()["offline"] is True
        assert MARKER not in api.text
        health = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=3)
        assert health.status_code == 200
        assert health.json() == {"ok": True, "agent": False}
        assert MARKER not in health.text
    finally:
        server.should_exit = True


def test_phone_reads_the_home_chat_and_a_stopped_home_is_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_TOKEN", TOKEN)
    app, bot, chat = seed(tmp_path)
    relay = create_relay()
    port = free_port()
    server = serve(relay, port)
    url = f"http://127.0.0.1:{port}"

    async def session():
        task = asyncio.create_task(run_relay_session(app, websocket_url(url), TOKEN))
        for _ in range(50):
            if relay.state.hub.online:
                break
            await asyncio.sleep(0.05)
        assert relay.state.hub.online
        async with httpx.AsyncClient(timeout=5) as client:
            page = await client.get(url + "/")
            assert page.status_code == 200
            assert "token-gate" in page.text
            assert MARKER not in page.text
            denied = await client.get(f"{url}/api/bots/{bot['id']}/chats/{chat['id']}")
            assert denied.status_code == 401
            assert MARKER not in denied.text
            allowed = await client.get(
                f"{url}/api/bots/{bot['id']}/chats/{chat['id']}",
                headers={"X-EasyAgent-Token": TOKEN},
            )
            assert allowed.status_code == 200
            assert allowed.json()["messages"][0]["content"] == MARKER
            assert allowed.headers["cache-control"] == "no-store"
        assert relay.state.hub.pending == {}
        dumped = json.dumps({key: repr(value) for key, value in relay.state.hub.__dict__.items()})
        assert MARKER not in dumped
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for _ in range(50):
            if not relay.state.hub.online:
                break
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient(timeout=5) as client:
            offline = await client.get(url + "/", headers={"X-EasyAgent-Token": TOKEN})
            assert offline.status_code == 503
            assert "EasyAgent is offline" in offline.text
            assert MARKER not in offline.text

    try:
        asyncio.run(session())
        on_disk = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text()
        assert MARKER in on_disk
    finally:
        server.should_exit = True


def test_call_app_is_the_local_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_TOKEN", TOKEN)
    app, bot, chat = seed(tmp_path)

    async def run():
        status, _headers, body = await call_app(app, "GET", f"/api/bots/{bot['id']}/chats/{chat['id']}", {}, b"")
        assert status == 200
        assert MARKER in body.decode()

    asyncio.run(run())
