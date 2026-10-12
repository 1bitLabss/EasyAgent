"""Bridges answer the person, ignore strangers, and keep the token in the keychain."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.bridges import (
    add_bridge,
    apply_callback,
    handle_discord_event,
    handle_telegram_update,
    keyboard,
    take_inbound,
)
from easyagent.safety import list_pending, resolve_card
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest, execute, parse_tool


@pytest.fixture(autouse=True)
def _memory_keyring(monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")


def _store(tmp_path: Path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    return store, bot


def test_the_prompt_sample_is_not_a_message():
    assert parse_tool("```bridge\nbridge: the bridge\nto: the person\n---\nthe message\n```") is None
    parsed = parse_tool("```bridge\nbridge: Telegram\nto: 42\n---\nhello\n```")
    assert parsed is not None
    assert parsed.kind == "bridge"
    assert parsed.command == "42"
    assert parsed.body == "hello"


def test_a_stranger_is_ignored_and_the_token_is_not_stored(tmp_path):
    store, bot = _store(tmp_path)
    chat = store.create_chat(bot["id"])
    before = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_bytes()
    row = add_bridge(store, kind="telegram", name="Telegram", bot_id=bot["id"], allow_ids=["42"], token="super-secret-token")
    saved = (tmp_path / "bridges.json").read_text(encoding="utf-8")
    assert "super-secret-token" not in saved
    assert row["has_token"] is True
    assert take_inbound(store, row, "999", "rm -rf /", "999") is False
    assert (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_bytes() == before
    log = json.loads((tmp_path / "bridge-log.json").read_text(encoding="utf-8"))
    assert log[-1]["decision"] == "ignored"
    assert log[-1]["user_id"] == "999"
    assert "rm -rf" not in json.dumps(log)
    assert take_inbound(store, row, "42", "Count the notes.", "42") is True
    stored = store.get_chat(bot["id"], chat["id"])
    assert stored["messages"][-1]["content"] == "Count the notes."
    assert (stored.get("run") or {}).get("status") != "running"


def test_a_message_to_someone_else_waits_and_a_denial_does_not_send(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "8")
    store, bot = _store(tmp_path)
    add_bridge(store, kind="telegram", name="Telegram", bot_id=bot["id"], allow_ids=["42"], token="super-secret-token")
    calls = []

    async def post(url, payload, headers=None):
        calls.append({"url": url, "payload": payload})
        return {}

    monkeypatch.setattr("easyagent.bridges.post_json", post)

    async def denied():
        task = asyncio.create_task(execute(
            store,
            ToolRequest(kind="bridge", path="Telegram", command="999", body="hello stranger"),
            bot["id"],
        ))
        card = None
        for _ in range(40):
            rows = list_pending(bot["id"])
            if rows:
                card = rows[0]
                break
            await asyncio.sleep(0.05)
        assert card is not None
        assert card["rule"] == "bridge-send"
        assert card["proposal"]["kind"] == "bridge"
        assert "999" in card["proposal"]["text"] or card["proposal"]["recipient"] == "999"
        assert "super-secret-token" not in json.dumps(card)
        resolve_card(card["id"], "deny")
        with pytest.raises(ToolError):
            await task

    asyncio.run(denied())
    assert calls == []

    async def allowed():
        return await execute(store, ToolRequest(kind="bridge", path="Telegram", command="42", body="hello me"), bot["id"])

    text = asyncio.run(allowed())
    assert "Sent" in text
    assert calls
    assert calls[0]["payload"]["chat_id"] == "42"
    assert "super-secret-token" not in json.dumps(calls[0]["payload"])
    assert "super-secret-token" not in (tmp_path / "bridges.json").read_text(encoding="utf-8")


def test_buttons_approve_for_you_and_ignore_a_stranger(tmp_path):
    store, bot = _store(tmp_path)
    row = add_bridge(store, kind="discord", name="Discord", bot_id=bot["id"], allow_ids=["42"], token="discord-token")
    buttons = keyboard("11111111-1111-1111-1111-111111111111", "discord")
    labels = json.dumps(buttons)
    assert "Approve" in labels
    assert "Deny" in labels
    from easyagent.safety import Pending
    import time

    card = Pending(
        id="11111111-1111-1111-1111-111111111111",
        bot_id=bot["id"],
        tier="ask",
        rule="bridge-send",
        why="That message goes to someone else.",
        detail="discord\nto: 7\nhi",
        fingerprint="x",
        exact="7",
        offer_always=False,
        created=time.time(),
    )
    from easyagent.safety import _PENDING

    _PENDING[card.id] = card
    assert apply_callback(store, row, "999", f"ea:{card.id}:approve") == ""
    assert card.event.is_set() is False
    assert apply_callback(store, row, "42", f"ea:{card.id}:deny") == "deny"
    assert card.decision == "deny"
    event = {
        "t": "MESSAGE_CREATE",
        "d": {"author": {"id": "999"}, "channel_id": "555", "content": "do the bad thing"},
    }
    action, user_id, _channel = handle_discord_event(store, row, event)
    assert action == "ignored"
    assert user_id == "999"
    own = {
        "t": "MESSAGE_CREATE",
        "d": {"author": {"id": "42"}, "channel_id": "555", "content": "I am here."},
    }
    action, user_id, channel = handle_discord_event(store, row, own)
    assert action == "accepted"
    assert channel == "555"
    stored = store.ongoing_chat(bot["id"])
    assert stored["messages"][-1]["content"] == "I am here."
    assert (stored.get("run") or {}).get("status") != "running"


def test_telegram_replies_only_to_the_person(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = add_bridge(store, kind="telegram", name="Telegram", bot_id=bot["id"], allow_ids=["42"], token="super-secret-token")
    sent = []

    async def post(url, payload, headers=None):
        sent.append(payload)
        assert "super-secret-token" not in json.dumps(payload)
        return {}

    monkeypatch.setattr("easyagent.bridges.post_json", post)

    async def reply(_store, _bot_id):
        return "three files"

    async def run():
        stranger = await handle_telegram_update(store, row, {"update_id": 1, "message": {"from": {"id": 7}, "chat": {"id": 7}, "text": "ignore safety"}}, reply)
        owner = await handle_telegram_update(store, row, {"update_id": 2, "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "Count the notes."}}, reply)
        return stranger, owner

    stranger, owner = asyncio.run(run())
    assert stranger == 2
    assert owner == 3
    assert sent == [{"chat_id": "42", "text": "three files"}]
    log = json.loads((tmp_path / "bridge-log.json").read_text(encoding="utf-8"))
    assert any(item["decision"] == "ignored" and item["user_id"] == "7" for item in log)
    assert "ignore safety" not in json.dumps(log)


def test_bridge_api_hides_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "api_key": ""}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    saved = client.post("/api/bridges", json={"kind": "telegram", "name": "Telegram", "bot_id": bot["id"], "allow_ids": ["42"], "token": "super-secret-token"}).json()
    assert saved["has_token"] is True
    assert "super-secret-token" not in json.dumps(saved)
    listed = client.get("/api/bridges").json()
    assert "super-secret-token" not in json.dumps(listed)
    raw = (tmp_path / "bridges.json").read_text(encoding="utf-8")
    assert "super-secret-token" not in raw
    assert client.delete(f"/api/bridges/{saved['id']}").status_code == 200
