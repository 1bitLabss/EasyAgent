"""0.3.60: a room message sent by one client reaches every live listener's stream.

TestClient buffers a response until the handler returns, so it cannot hold two
SSE streams open at once. These tests run the app on a real uvicorn port and
read two live streams with plain httpx.
"""

import json
import socket
import threading
import time

import httpx
import pytest

from easyagent.app import create_app


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    import uvicorn

    async def complete(**kwargs):
        return "Noted."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        time.sleep(0.05)
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _room_with_two_bots(client: httpx.Client):
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "max_parallel": 2},
    ).json()
    alpha = client.post("/api/bots", json={"name": "Alpha", "endpoint_id": endpoint["id"]}).json()
    bravo = client.post("/api/bots", json={"name": "Bravo", "endpoint_id": endpoint["id"]}).json()
    for bot in (alpha, bravo):
        client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    room = client.post("/api/rooms", json={"name": "Sync"}).json()
    client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": alpha["id"]})
    client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": bravo["id"]})
    return room, alpha, bravo


def _event_reader(lines):
    """Return a reader that yields one parsed SSE frame per call."""
    iterator = iter(lines)

    def next_event() -> dict:
        data: list[str] = []
        for line in iterator:
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and data:
                return json.loads("\n".join(data))
        raise AssertionError("the live stream closed before the next event")

    return next_event


def _until_done(next_event) -> list[dict]:
    events = []
    while True:
        event = next_event()
        events.append(event)
        if event.get("type") == "done":
            return events


def test_a_plain_post_from_one_client_lands_in_another_clients_live_stream(live_server):
    with httpx.Client(base_url=live_server, timeout=30) as client:
        room, _alpha, _bravo = _room_with_two_bots(client)
        with client.stream("GET", f"/api/rooms/{room['id']}/live") as live:
            assert live.status_code == 200
            next_event = _event_reader(live.iter_lines())
            assert next_event()["type"] == "sync"
            posted = client.post(f"/api/rooms/{room['id']}/messages", json={"content": "hello room"})
            assert posted.status_code == 200
            events = _until_done(next_event)
            users = [event for event in events if event.get("type") == "user"]
            assert any(event["message"]["content"] == "hello room" for event in users)
            replies = [event["message"] for event in events if event.get("type") == "message"]
            assert [item["speaker_name"] for item in replies] == ["Alpha", "Bravo"]


def test_a_streamed_turn_pushes_statuses_and_replies_to_the_other_window(live_server):
    with httpx.Client(base_url=live_server, timeout=30) as client:
        room, _alpha, _bravo = _room_with_two_bots(client)
        with client.stream("GET", f"/api/rooms/{room['id']}/live") as live:
            assert live.status_code == 200
            next_event = _event_reader(live.iter_lines())
            assert next_event()["type"] == "sync"

            def send():
                with httpx.Client(base_url=live_server, timeout=30) as sender:
                    with sender.stream(
                        "POST",
                        f"/api/rooms/{room['id']}/messages",
                        json={"content": "what now?"},
                        headers={"Accept": "text/event-stream"},
                    ) as response:
                        response.read()

            worker = threading.Thread(target=send, daemon=True)
            worker.start()
            events = _until_done(next_event)
            assert any(
                event.get("type") == "user" and event.get("message", {}).get("content") == "what now?"
                for event in events
            )
            statuses = [event for event in events if event.get("type") == "bot_status"]
            assert any(event.get("state") == "thinking" for event in statuses)
            assert any(event.get("state") == "queued" for event in statuses)
            replies = [event["message"] for event in events if event.get("type") == "message"]
            assert [item["speaker_name"] for item in replies] == ["Alpha", "Bravo"]
            worker.join(timeout=20)
            assert not worker.is_alive()


def test_the_live_stream_stays_open_across_turns(live_server):
    with httpx.Client(base_url=live_server, timeout=30) as client:
        room, _alpha, _bravo = _room_with_two_bots(client)
        with client.stream("GET", f"/api/rooms/{room['id']}/live") as live:
            assert live.status_code == 200
            next_event = _event_reader(live.iter_lines())
            assert next_event()["type"] == "sync"
            for text in ("first", "second"):
                client.post(f"/api/rooms/{room['id']}/messages", json={"content": text})
                events = _until_done(next_event)
                assert any(
                    event.get("type") == "user" and event.get("message", {}).get("content") == text
                    for event in events
                ), text
