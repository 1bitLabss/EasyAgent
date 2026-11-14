"""A Slack bridge sends out, shows approval buttons, and keeps the token in the keychain."""

import asyncio
import json

import pytest

from easyagent import bridges as bridges_mod
from easyagent.bridges import (
    add_bridge,
    handle_slack_event,
    keyboard,
    public_record,
    remember_sender,
    run_bridges,
    slack_send,
    _send_card,
    _slack_socket_session,
)
from easyagent.safety import list_pending, resolve_card
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest, execute
from tests.test_plan_0320 import _client

SLACK_URL = "https://slack.com/api/chat.postMessage"
OPEN_URL = "https://slack.com/api/apps.connections.open"
SOCKET_URL = "wss://wss-primary.slack.com/link/tokens/x-y-z"
TOKEN = "«redacted:xox…»"


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


# --- inbound Socket Mode -----------------------------------------------------


class _SocketClosed(Exception):
    """Raised by the fake socket once the queued envelopes run out."""


class FakeSocket:
    """A stand-in for a websockets connection: recv yields queued envelopes."""

    def __init__(self, envelopes):
        self._envelopes = list(envelopes)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        if not self._envelopes:
            raise _SocketClosed("the fake socket ran dry")
        return json.dumps(self._envelopes.pop(0))

    async def send(self, frame):
        self.sent.append(json.loads(frame))


def _envelope(payload, envelope_id="env-1"):
    return {"envelope_id": envelope_id, "type": "events_api", "payload": payload}


def _message_event(user="U123", channel="C555", text="Count the notes.", **extra):
    event = {"type": "message", "user": user, "channel": channel, "text": text}
    event.update(extra)
    return {"type": "event_callback", "event": event}


def _wire_socket(monkeypatch, envelopes, open_response=None):
    """Patch the HTTP open call and websockets.connect; return (socket, http_calls, connect_calls)."""
    http_calls = []
    response = open_response if open_response is not None else {"ok": True, "url": SOCKET_URL}

    async def post(url, payload, headers=None):
        http_calls.append({"url": url, "payload": payload, "headers": headers or {}})
        return response

    monkeypatch.setattr("easyagent.bridges.post_json", post)
    socket = FakeSocket(envelopes)
    connect_calls = []

    def connect(url, **kwargs):
        connect_calls.append({"url": url, "kwargs": kwargs})
        return socket

    monkeypatch.setattr("websockets.connect", connect)
    return socket, http_calls, connect_calls


def _run_session(store, row):
    async def reply(_store, _bot_id):
        return "three files"

    with pytest.raises(_SocketClosed):
        asyncio.run(_slack_socket_session(store, dict(row), TOKEN, reply))


def test_allowlisted_message_reaches_the_bot_and_the_reply_is_sent(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["U123"], token=TOKEN,
    )
    socket, _http, connect_calls = _wire_socket(
        monkeypatch, [_envelope(_message_event(), "env-a")]
    )
    sent = []

    async def fake_slack_send(token, chat_id, text, blocks=None):
        sent.append({"token": token, "chat_id": chat_id, "text": text})

    monkeypatch.setattr("easyagent.bridges.slack_send", fake_slack_send)
    _run_session(store, row)

    assert connect_calls[0]["url"] == SOCKET_URL
    assert socket.sent[0] == {"type": "events_api", "accept_response_payload": {}}
    assert {"envelope_id": "env-a"} in socket.sent
    assert sent == [{"token": TOKEN, "chat_id": "C555", "text": "three files"}]
    stored = store.ongoing_chat(bot["id"])
    assert stored["messages"][-1]["content"] == "Count the notes."
    log = json.loads((tmp_path / "bridge-log.json").read_text(encoding="utf-8"))
    assert log[-1]["decision"] == "sent"


def test_a_stranger_does_not_reach_the_bot(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["U123"], token=TOKEN,
    )
    chat = store.create_chat(bot["id"])
    before = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_bytes()
    socket, _http, _connect = _wire_socket(
        monkeypatch, [_envelope(_message_event(user="U999", text="rm -rf /"), "env-b")]
    )
    sent = []

    async def fake_slack_send(token, chat_id, text, blocks=None):
        sent.append(text)

    monkeypatch.setattr("easyagent.bridges.slack_send", fake_slack_send)
    _run_session(store, row)

    assert sent == []
    assert (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_bytes() == before
    log = json.loads((tmp_path / "bridge-log.json").read_text(encoding="utf-8"))
    assert log[-1]["decision"] == "ignored"
    assert log[-1]["user_id"] == "U999"
    assert "rm -rf" not in json.dumps(log)
    # Even an ignored envelope is acknowledged.
    assert {"envelope_id": "env-b"} in socket.sent


def test_bot_edits_and_empty_messages_are_skipped(tmp_path):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["U123"], token=TOKEN,
    )
    action, user_id, _channel = handle_slack_event(
        store, row, _message_event(text="my own echo", bot_id="B777")
    )
    assert action == "skip"
    assert handle_slack_event(store, row, _message_event(text="edited", subtype="message_changed"))[0] == "skip"
    assert handle_slack_event(store, row, _message_event(text=""))[0] == "skip"
    assert handle_slack_event(store, row, {"type": "url_verification"})[0] == "skip"
    stored = store.ongoing_chat(bot["id"])
    assert stored["messages"] == []


def test_every_envelope_is_acked_by_its_envelope_id(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["U123"], token=TOKEN,
    )
    envelopes = [
        _envelope(_message_event(), "env-1"),
        _envelope({"type": "interactive", "user": {"id": "U123"}, "actions": []}, "env-2"),
        _envelope({"type": "something_else"}, "env-3"),
    ]
    socket, _http, _connect = _wire_socket(monkeypatch, envelopes)

    async def fake_slack_send(token, chat_id, text, blocks=None):
        return None

    monkeypatch.setattr("easyagent.bridges.slack_send", fake_slack_send)
    _run_session(store, row)

    acks = [frame for frame in socket.sent if set(frame) == {"envelope_id"}]
    assert [frame["envelope_id"] for frame in acks] == ["env-1", "env-2", "env-3"]


def test_interactive_approve_resolves_the_card(tmp_path):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["U123"], token=TOKEN,
    )
    from easyagent.safety import Pending, _PENDING
    import time

    def make_card(card_id, fingerprint):
        return Pending(
            id=card_id,
            bot_id=bot["id"],
            tier="ask",
            rule="bridge-send",
            why="That message goes to someone else.",
            detail="slack\nto: 7\nhi",
            fingerprint=fingerprint,
            exact="7",
            offer_always=False,
            created=time.time(),
        )

    card = make_card("22222222-2222-2222-2222-222222222222", "x")
    _PENDING[card.id] = card
    payload = {
        "type": "interactive",
        "user": {"id": "U123"},
        "channel": {"id": "C555"},
        "actions": [{"action_id": f"ea:{card.id}:approve", "value": f"ea:{card.id}:approve"}],
    }
    action, user_id, channel = handle_slack_event(store, row, payload)
    assert action == "callback"
    assert user_id == "U123"
    assert channel == "C555"
    assert card.decision == "approve"
    assert card.event.is_set() is True
    # The keyboard only sets action_id, so the value may be absent.
    card2 = make_card("33333333-3333-3333-3333-333333333333", "y")
    _PENDING[card2.id] = card2
    payload2 = {
        "type": "interactive",
        "user": {"id": "U123"},
        "channel": {"id": "C555"},
        "actions": [{"action_id": f"ea:{card2.id}:deny"}],
    }
    assert handle_slack_event(store, row, payload2)[0] == "callback"
    assert card2.decision == "deny"
    # A stranger tap is ignored.
    card3 = make_card("44444444-4444-4444-4444-444444444444", "z")
    _PENDING[card3.id] = card3
    payload3 = {
        "type": "interactive",
        "user": {"id": "U999"},
        "channel": {"id": "C555"},
        "actions": [{"action_id": f"ea:{card3.id}:approve"}],
    }
    assert handle_slack_event(store, row, payload3)[0] == "callback"
    assert card3.event.is_set() is False


def test_open_failure_exits_quietly_without_connecting(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["U123"], token=TOKEN,
    )
    _socket, http_calls, connect_calls = _wire_socket(
        monkeypatch, [], {"ok": False, "error": "invalid_auth"}
    )
    result = asyncio.run(_slack_socket_session(store, dict(row), TOKEN, None))
    assert result is None
    assert connect_calls == []
    assert http_calls[0]["url"] == OPEN_URL


def test_token_stays_out_of_urls_and_frames(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["U123"], token=TOKEN,
    )
    socket, http_calls, connect_calls = _wire_socket(
        monkeypatch, [_envelope(_message_event(), "env-t")]
    )
    sent = []

    async def fake_slack_send(token, chat_id, text, blocks=None):
        sent.append({"chat_id": chat_id, "text": text})

    monkeypatch.setattr("easyagent.bridges.slack_send", fake_slack_send)
    _run_session(store, row)

    assert http_calls[0]["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert TOKEN not in http_calls[0]["url"]
    assert TOKEN not in json.dumps(http_calls[0]["payload"])
    assert TOKEN not in connect_calls[0]["url"]
    assert TOKEN not in json.dumps(socket.sent)
    assert TOKEN not in json.dumps(sent)


def test_run_bridges_starts_a_slack_session(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["U123"], token=TOKEN,
    )
    started = []
    stop = asyncio.Event()

    def fake_ensure(_store, bridge_row, token, _reply):
        started.append((bridge_row["id"], token))
        stop.set()

    monkeypatch.setattr(bridges_mod, "_ensure_slack", fake_ensure)

    async def drive():
        await asyncio.wait_for(run_bridges(store, stop, None), timeout=5)

    asyncio.run(drive())
    assert started == [(row["id"], TOKEN)]


# --- routine delivery gate ----------------------------------------------------


def test_schedule_saves_a_slack_routine_for_an_allowlisted_chat(tmp_path):
    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["C123"], token=TOKEN,
    )
    body = {
        "name": "Brief",
        "prompt": "Say good morning.",
        "daily": "8:00 AM",
        "deliver": "slack",
        "deliver_chat": "C123",
    }
    saved = client.post(f"/api/bots/{bot['id']}/schedules", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["deliver_chat"] == "C123"
    assert len(store.list_schedules(bot["id"])) == 1
    client.close()


def test_schedule_rejects_a_slack_chat_that_is_not_allowlisted(tmp_path):
    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["C123"], token=TOKEN,
    )
    body = {
        "name": "Brief",
        "prompt": "Say good morning.",
        "daily": "8:00 AM",
        "deliver": "slack",
        "deliver_chat": "C999",
    }
    refused = client.post(f"/api/bots/{bot['id']}/schedules", json=body)
    assert refused.status_code == 400, refused.text
    assert (
        refused.json()["detail"]
        == "That chat is not on this bot's Slack allow list. The routine was not saved."
    )
    assert store.list_schedules(bot["id"]) == []
    client.close()


def test_routine_run_posts_to_the_allowlisted_slack_chat(tmp_path, monkeypatch):
    from easyagent.schedule import _deliver, compile_routine

    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    add_bridge(
        store, kind="slack", name="Slack", bot_id=bot["id"],
        allow_ids=["C123"], token=TOKEN,
    )
    calls = []

    async def fake_slack_send(token, chat_id, text, blocks=None):
        calls.append((chat_id, text))

    monkeypatch.setattr("easyagent.bridges.slack_send", fake_slack_send)
    record = compile_routine({
        "name": "Brief",
        "prompt": "Say good morning.",
        "daily": "8:00 AM",
        "deliver": "slack",
        "deliver_chat": "C123",
    })
    entry = {"status": "ok", "output": "Good morning.", "name": "Brief"}
    asyncio.run(_deliver(store, bot, record, entry))
    assert calls == [("C123", "Good morning.")]
    client.close()


def test_schedule_rejects_a_telegram_chat_that_is_not_allowlisted(tmp_path):
    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    add_bridge(
        store, kind="telegram", name="Phone", bot_id=bot["id"],
        allow_ids=["42"], token="token-value",
    )
    body = {
        "name": "Brief",
        "prompt": "Say good morning.",
        "daily": "8:00 AM",
        "deliver": "telegram",
        "deliver_chat": "999",
    }
    refused = client.post(f"/api/bots/{bot['id']}/schedules", json=body)
    assert refused.status_code == 400, refused.text
    assert (
        refused.json()["detail"]
        == "That chat is not on this bot's Telegram allow list. The routine was not saved."
    )
    assert store.list_schedules(bot["id"]) == []
    client.close()
