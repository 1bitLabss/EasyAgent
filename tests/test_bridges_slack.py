"""A Slack bridge sends out, shows approval buttons, and keeps the token in the keychain."""

import asyncio
import json

import pytest

from easyagent.bridges import (
    add_bridge,
    keyboard,
    public_record,
    remember_sender,
    slack_send,
    _send_card,
)
from easyagent.safety import list_pending, resolve_card
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest, execute

SLACK_URL = "https://slack.com/api/chat.postMessage"


@pytest.fixture(autouse=True)
def _memory_keyring(monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")


def _store(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    return store, bot


def _fake_post(calls, response=None):
    async def post(url, payload, headers=None):
        calls.append({"url": url, "payload": payload, "headers": headers or {}})
        return response if response is not None else {}

    return post


def test_add_bridge_accepts_slack_and_hides_the_token(tmp_path):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["C123"], token="xoxb-super-secret",
    )
    assert row["kind"] == "slack"
    assert row["has_token"] is True
    public = public_record(row)
    assert public["has_token"] is True
    assert "xoxb-super-secret" not in json.dumps(public)
    saved = (tmp_path / "bridges.json").read_text(encoding="utf-8")
    assert "xoxb-super-secret" not in saved


def test_allowlisted_send_posts_to_slack_with_token_only_in_headers(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["C123"], token="xoxb-super-secret",
    )
    calls = []
    monkeypatch.setattr("easyagent.bridges.post_json", _fake_post(calls))

    async def send():
        return await execute(
            store, ToolRequest(kind="bridge", path="Slack", command="C123", body="hello me"), bot["id"]
        )

    text = asyncio.run(send())
    assert "Sent" in text
    assert len(calls) == 1
    assert calls[0]["url"] == SLACK_URL
    assert calls[0]["payload"] == {"channel": "C123", "text": "hello me"}
    assert calls[0]["headers"] == {"Authorization": "Bearer xoxb-super-secret"}
    assert "xoxb-super-secret" not in json.dumps(calls[0]["payload"])
    assert "xoxb-super-secret" not in (tmp_path / "bridges.json").read_text(encoding="utf-8")


def test_slack_keyboard_uses_block_kit_action_ids():
    card_id = "11111111-1111-1111-1111-111111111111"
    buttons = keyboard(card_id, "slack")
    elements = buttons["blocks"][0]["elements"]
    assert buttons["blocks"][0]["type"] == "actions"
    action_ids = [element["action_id"] for element in elements]
    assert action_ids == [f"ea:{card_id}:approve", f"ea:{card_id}:deny"]
    labels = [element["text"]["text"] for element in elements]
    assert labels == ["Approve", "Deny"]
    styles = [element["style"] for element in elements]
    assert styles == ["primary", "danger"]


def test_slack_ok_false_raises(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "easyagent.bridges.post_json",
        _fake_post(calls, {"ok": False, "error": "channel_not_found"}),
    )
    with pytest.raises(RuntimeError, match="channel_not_found"):
        asyncio.run(slack_send("xoxb-super-secret", "C123", "hello"))
    assert len(calls) == 1


def test_slack_ok_false_surfaces_as_tool_error(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["C123"], token="xoxb-super-secret",
    )
    monkeypatch.setattr(
        "easyagent.bridges.post_json",
        _fake_post([], {"ok": False, "error": "not_in_channel"}),
    )

    async def send():
        return await execute(
            store, ToolRequest(kind="bridge", path="Slack", command="C123", body="hello me"), bot["id"]
        )

    with pytest.raises(ToolError):
        asyncio.run(send())


def test_approval_card_sends_block_kit_buttons(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["C123"], token="xoxb-super-secret",
    )
    remember_sender(store, row["id"], "C123", "C123")
    calls = []
    monkeypatch.setattr("easyagent.bridges.post_json", _fake_post(calls))

    class Card:
        id = "11111111-1111-1111-1111-111111111111"
        why = "That message goes to someone else."
        detail = "slack\nto: 7\nhi"

    asyncio.run(_send_card(store, bot["id"], Card()))
    assert len(calls) == 1
    assert calls[0]["url"] == SLACK_URL
    assert calls[0]["payload"]["channel"] == "C123"
    blocks = calls[0]["payload"]["blocks"]
    action_ids = [element["action_id"] for element in blocks[0]["elements"]]
    assert action_ids == [
        "ea:11111111-1111-1111-1111-111111111111:approve",
        "ea:11111111-1111-1111-1111-111111111111:deny",
    ]
    assert "xoxb-super-secret" not in json.dumps(calls[0]["payload"])


def test_a_message_to_someone_else_waits_for_a_card(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "8")
    store, bot = _store(tmp_path)
    add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["C123"], token="xoxb-super-secret",
    )
    calls = []
    monkeypatch.setattr("easyagent.bridges.post_json", _fake_post(calls))

    async def denied():
        task = asyncio.create_task(execute(
            store,
            ToolRequest(kind="bridge", path="Slack", command="C999", body="hello stranger"),
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
        assert card["proposal"]["recipient"] == "C999"
        assert "xoxb-super-secret" not in json.dumps(card)
        resolve_card(card["id"], "deny")
        with pytest.raises(ToolError):
            await task

    asyncio.run(denied())
    assert calls == []
