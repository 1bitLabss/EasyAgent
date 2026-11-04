"""0.3.53g: a room turn answers one bot at a time, so later bots see earlier replies."""

import asyncio
import json
import time

from fastapi.testclient import TestClient

from easyagent.app import create_app


def _events(body: str) -> list[dict]:
    rows = []
    for block in body.split("\n\n"):
        data = "\n".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
        if data:
            rows.append(json.loads(data))
    return rows


def _room_with_two_bots(client: TestClient):
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "max_parallel": 2},
    ).json()
    alpha = client.post("/api/bots", json={"name": "Alpha", "endpoint_id": endpoint["id"]}).json()
    bravo = client.post("/api/bots", json={"name": "Bravo", "endpoint_id": endpoint["id"]}).json()
    for bot in (alpha, bravo):
        client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    room = client.post("/api/rooms", json={"name": "Chain"}).json()
    client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": alpha["id"]})
    client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": bravo["id"]})
    return room, alpha, bravo


def _stream_message(client: TestClient, room_id: str, content: str) -> str:
    with client.stream(
        "POST",
        f"/api/rooms/{room_id}/messages",
        json={"content": content},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        return response.read().decode()


def test_a_later_bot_sees_the_earlier_bots_reply(tmp_path, monkeypatch):
    calls: list[list[dict]] = []

    async def complete(**kwargs):
        messages = kwargs["messages"]
        calls.append(messages)
        system = messages[0]["content"]
        if "You are Alpha" in system:
            return "ALPHA-MARKER here."
        return "Bravo read the room."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    client = TestClient(create_app(tmp_path))
    room, _alpha, _bravo = _room_with_two_bots(client)
    body = _stream_message(client, room["id"], "what should we do?")
    events = _events(body)
    messages = [item["message"] for item in events if item.get("type") == "message"]
    assert [m["speaker_name"] for m in messages] == ["Alpha", "Bravo"]

    def calls_for(name: str) -> list[list[dict]]:
        return [c for c in calls if f"You are {name}" in c[0]["content"]]

    alpha_calls = calls_for("Alpha")
    bravo_calls = calls_for("Bravo")
    assert alpha_calls and bravo_calls
    # Sequential: Alpha was asked before Bravo, never after.
    assert calls.index(alpha_calls[0]) < calls.index(bravo_calls[0])
    # Bravo's model call carries Alpha's same-turn reply.
    assert any("ALPHA-MARKER" in str(m.get("content")) for m in bravo_calls[-1])
    # Alpha went first, so it could not have seen its own marker as input.
    assert not any("ALPHA-MARKER" in str(m.get("content")) for m in alpha_calls[0])
    saved = client.get(f"/api/rooms/{room['id']}").json()
    assert [m["content"] for m in saved["messages"]] == [
        "what should we do?",
        "ALPHA-MARKER here.",
        "Bravo read the room.",
    ]


def test_a_failing_first_bot_does_not_stop_the_second(tmp_path, monkeypatch):
    async def complete(**kwargs):
        system = kwargs["messages"][0]["content"]
        if "You are Alpha" in system:
            raise RuntimeError("boom")
        return "Bravo still answers."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    client = TestClient(create_app(tmp_path))
    room, _alpha, _bravo = _room_with_two_bots(client)
    body = _stream_message(client, room["id"], "both of you")
    events = _events(body)
    messages = [item["message"] for item in events if item.get("type") == "message"]
    speakers = [m["speaker_name"] for m in messages]
    assert "Alpha" in speakers and "Bravo" in speakers
    alpha_message = next(m for m in messages if m["speaker_name"] == "Alpha")
    assert alpha_message.get("error") or "boom" in alpha_message["content"]
    bravo_message = next(m for m in messages if m["speaker_name"] == "Bravo")
    assert bravo_message["content"] == "Bravo still answers."
    assert any(item.get("type") == "done" for item in events)


def test_a_waiting_approval_does_not_block_the_later_bot(tmp_path, monkeypatch):
    """Alpha waits on an approval card; Bravo answers anyway; Alpha finishes after approval."""
    from easyagent import safety
    from easyagent.safety import Pending

    safety.reset_for_tests()
    safety.clear_pending()
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "60")
    app = create_app(tmp_path)
    client = TestClient(app)
    room, alpha, bravo = _room_with_two_bots(client)

    async def fake_complete(_store, room_row, bot_id):
        if bot_id == bravo["id"]:
            return "Bravo answered while Alpha waited.", [], None, [], False
        card = Pending(
            id="card-0353g",
            bot_id=alpha["id"],
            tier="ask",
            rule="test",
            why="testing the chain",
            detail="testing the chain",
            fingerprint="fp-0353g",
            exact="x",
            offer_always=False,
            created=time.time(),
            chat_id=room_row["id"],
        )
        safety._PENDING[card.id] = card
        try:
            # The person approves a second later. Bravo must not wait for it.
            await asyncio.sleep(1.5)
            safety.resolve_card(card.id, "approve", alpha["id"])
        finally:
            safety._PENDING.pop(card.id, None)
        return "Alpha answered after approval.", [], None, [], False

    monkeypatch.setattr("easyagent.app._complete_for_room_bot", fake_complete)
    body = _stream_message(client, room["id"], "chain and approve")
    events = _events(body)
    assert any(item.get("type") == "approval" for item in events)
    messages = [item["message"] for item in events if item.get("type") == "message"]
    contents = [m["content"] for m in messages]
    assert "Bravo answered while Alpha waited." in contents
    assert "Alpha answered after approval." in contents
    # Bravo did not wait for Alpha's approval: its reply landed first.
    assert contents.index("Bravo answered while Alpha waited.") < contents.index(
        "Alpha answered after approval."
    )
    saved = client.get(f"/api/rooms/{room['id']}").json()
    saved_contents = [m["content"] for m in saved["messages"]]
    assert "Bravo answered while Alpha waited." in saved_contents
    assert "Alpha answered after approval." in saved_contents
