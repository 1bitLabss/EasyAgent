"""0.3.71 regression: a second room message queues behind the running turn.

Without the per-room lock, a second message overwrote _ROOM_RUNS: Stop then
cancelled the wrong run and the two bot turns ran concurrently against the
same transcript. Like test_0360, TestClient buffers SSE so it cannot hold two
streams open; these tests run uvicorn on a real port and read live streams
with plain httpx.
"""

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest

import easyagent.app
from easyagent.app import create_app


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    import uvicorn

    gate = threading.Event()
    calls: list[dict] = []

    async def complete(**kwargs):
        calls.append(kwargs)
        await asyncio.to_thread(gate.wait)
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
    yield f"http://127.0.0.1:{port}", gate, calls
    gate.set()
    server.should_exit = True
    thread.join(timeout=10)


def _room_with_bot(client: httpx.Client):
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "max_parallel": 2},
    ).json()
    alpha = client.post("/api/bots", json={"name": "Alpha", "endpoint_id": endpoint["id"]}).json()
    client.patch(f"/api/bots/{alpha['id']}", json={"check_enabled": False})
    room = client.post("/api/rooms", json={"name": "Runs"}).json()
    client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": alpha["id"]})
    return room, alpha


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


def _wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_second_message_queues_and_stop_hits_the_running_turn(live_server):
    base, gate, calls = live_server
    with httpx.Client(base_url=base, timeout=30) as client:
        room, _alpha = _room_with_bot(client)
        room_id = room["id"]

        # Run A: stream message 1; the fake completion parks on the gate.
        stream_a = client.stream(
            "POST",
            f"/api/rooms/{room_id}/messages",
            json={"content": "first"},
            headers={"Accept": "text/event-stream"},
        )
        try:
            resp_a = stream_a.__enter__()
            next_a = _event_reader(resp_a.iter_lines())
            assert next_a()["type"] == "user"
            thinking = next_a()
            assert thinking["type"] == "bot_status" and thinking["state"] == "thinking"
            assert _wait_for(lambda: room_id in easyagent.app._ROOM_RUNS)
            run_a = easyagent.app._ROOM_RUNS[room_id]
            assert _wait_for(lambda: len(calls) == 1)

            # Run B from a second window: its user echo lands while A is
            # still streaming, but its bot turn must not start.
            b_events: list[dict] = []
            b_errors: list[str] = []
            with httpx.Client(base_url=base, timeout=30) as client_b:
                with client_b.stream(
                    "POST",
                    f"/api/rooms/{room_id}/messages",
                    json={"content": "second"},
                    headers={"Accept": "text/event-stream"},
                ) as resp_b:
                    def drain_b():
                        try:
                            next_event = _event_reader(resp_b.iter_lines())
                            while True:
                                event = next_event()
                                b_events.append(event)
                                if event.get("type") == "done":
                                    break
                        except BaseException as exc:  # noqa: BLE001 - surfaced in asserts
                            b_errors.append(repr(exc))

                    drain = threading.Thread(target=drain_b, daemon=True)
                    drain.start()
                    assert _wait_for(
                        lambda: any(
                            e.get("type") == "user" and e.get("message", {}).get("content") == "second"
                            for e in b_events
                        ) or b_errors
                    ), f"B's user echo never arrived while A was streaming; errors={b_errors}"
                    assert not b_errors, b_errors

                    # _ROOM_RUNS still points at the run actually streaming.
                    assert easyagent.app._ROOM_RUNS[room_id] is run_a

                    # A full second later B has no bot events and its bot has
                    # not been called: it is queued behind A, not concurrent.
                    time.sleep(1.0)
                    assert not [e for e in b_events if e.get("type") in ("bot_status", "message")], b_events
                    assert len(calls) == 1

                    # Stop cancels the run that is actually running (A).
                    assert client.post(f"/api/rooms/{room_id}/stop").status_code == 200
                    assert run_a["cancel"].is_set()
                    assert easyagent.app._ROOM_RUNS[room_id] is run_a

                    # Release the fake: A finishes, then B runs to completion.
                    gate.set()
                    events_a = _until_done(next_a)
                    assert events_a[-1]["type"] == "done"
                    drain.join(timeout=20)
                    assert not drain.is_alive(), f"B never finished: {b_events} {b_errors}"
                    assert not b_errors, b_errors
                    types_b = [e.get("type") for e in b_events]
                    assert types_b[0] == "user" and types_b[-1] == "done"
                    replies = [e["message"] for e in b_events if e.get("type") == "message"]
                    assert replies and all(m["speaker_name"] == "Alpha" for m in replies)
        finally:
            gate.set()
            stream_a.__exit__(None, None, None)


def test_room_runs_entry_is_cleaned_up_when_both_turns_finish(live_server):
    base, gate, _calls = live_server
    gate.set()
    with httpx.Client(base_url=base, timeout=30) as client:
        room, _alpha = _room_with_bot(client)
        room_id = room["id"]
        for text in ("first", "second"):
            with client.stream(
                "POST",
                f"/api/rooms/{room_id}/messages",
                json={"content": text},
                headers={"Accept": "text/event-stream"},
            ) as resp:
                _until_done(_event_reader(resp.iter_lines()))
        assert _wait_for(lambda: room_id not in easyagent.app._ROOM_RUNS)
        assert room_id not in easyagent.app._ROOM_RUNS


def test_a_single_message_still_streams_user_then_bot_then_done(live_server):
    base, gate, _calls = live_server
    gate.set()
    with httpx.Client(base_url=base, timeout=30) as client:
        room, _alpha = _room_with_bot(client)
        with client.stream(
            "POST",
            f"/api/rooms/{room['id']}/messages",
            json={"content": "hello"},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            events = _until_done(_event_reader(resp.iter_lines()))
    types = [event.get("type") for event in events]
    assert types[0] == "user"
    assert types[-1] == "done"
    replies = [event["message"] for event in events if event.get("type") == "message"]
    assert [m["content"] for m in replies] == ["Noted."]
