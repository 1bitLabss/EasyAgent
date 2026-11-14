"""An Email bridge reads unread mail, answers allowlisted senders, and keeps the app password in the keychain."""

import asyncio
import json
import time

import pytest

from easyagent import bridges as bridges_mod
from easyagent import mailbridge
from easyagent.bridges import (
    _poll_email,
    _send_card,
    add_bridge,
    keyboard,
    post_allowlisted,
    public_record,
    remember_sender,
    BridgeDenied,
)
from easyagent.safety import Pending, _PENDING
from easyagent.store import Store, StoreError
from easyagent.tools import ToolRequest, execute
from tests.test_plan_0320 import _client

APP_PASS = "«redacted:app-password»"
ME = "me@example.com"
STRANGER = "stranger@example.com"
BOT_MAIL = "bot@example.com"
MAIL_FIELDS = {
    "mail_user": BOT_MAIL,
    "mail_imap_host": "imap.example.com",
    "mail_smtp_host": "smtp.example.com",
}


@pytest.fixture(autouse=True)
def _memory_keyring(monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")


def _store(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    return store, bot


def _email_bridge(store, bot, allow=None):
    return add_bridge(
        store, kind="email", name="Mail", bot_id=bot["id"],
        allow_ids=allow or [ME], token=APP_PASS, **MAIL_FIELDS,
    )


def _wire_mail(monkeypatch, messages):
    """Fake the three mailbridge network primitives; return (fetched, seen, sent)."""
    seen = []
    sent = []
    monkeypatch.setattr(mailbridge, "fetch_unseen", lambda row, token: list(messages))
    monkeypatch.setattr(mailbridge, "mark_seen", lambda row, token, uid: seen.append(uid))

    def fake_smtp(row, token, to_addr, subject, body):
        sent.append({"token": token, "to": to_addr, "subject": subject, "body": body})

    monkeypatch.setattr(mailbridge, "smtp_send", fake_smtp)
    return seen, sent


# --- config -------------------------------------------------------------------


def test_email_bridge_refuses_without_the_mail_fields(tmp_path):
    store, bot = _store(tmp_path)
    with pytest.raises(StoreError) as exc:
        add_bridge(
            store, kind="email", name="Mail", bot_id=bot["id"],
            allow_ids=[ME], token=APP_PASS, mail_user=BOT_MAIL,
        )
    assert exc.value.message == "An email bridge needs the address and the mail server names."
    assert not (tmp_path / "bridges.json").is_file()


def test_api_refuses_an_email_bridge_without_the_mail_fields(tmp_path):
    _app, client, bot, _chat = _client(tmp_path)
    refused = client.post(
        "/api/bridges",
        json={"kind": "email", "name": "Mail", "bot_id": bot["id"], "allow_ids": [ME], "token": APP_PASS},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"] == "An email bridge needs the address and the mail server names."
    assert client.get("/api/bridges").json() == []
    client.close()


def test_add_bridge_accepts_a_complete_email_bridge(tmp_path):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="email", name="Mail", bot_id=bot["id"],
        allow_ids=["Me@Example.com"], token=APP_PASS, **MAIL_FIELDS,
    )
    assert row["kind"] == "email"
    assert row["allow_ids"] == [ME]
    assert row["mail_user"] == BOT_MAIL
    public = public_record(row)
    assert public["mail_imap_host"] == "imap.example.com"
    assert public["mail_smtp_host"] == "smtp.example.com"
    assert public["has_token"] is True
    assert APP_PASS not in json.dumps(public)
    saved = (tmp_path / "bridges.json").read_text(encoding="utf-8")
    assert APP_PASS not in saved


# --- outbound -----------------------------------------------------------------


def test_deliver_sends_email_through_smtp(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    _email_bridge(store, bot)
    seen, sent = _wire_mail(monkeypatch, [])

    async def send():
        return await execute(
            store, ToolRequest(kind="bridge", path="Mail", command=ME, body="hello me"), bot["id"]
        )

    text = asyncio.run(send())
    assert "Sent" in text
    assert len(sent) == 1
    assert sent[0]["to"] == ME
    assert sent[0]["body"] == "hello me"
    assert sent[0]["token"] == APP_PASS
    assert APP_PASS not in json.dumps(sent[0])
    assert seen == []


def test_post_allowlisted_refuses_a_stranger_before_the_network(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = _email_bridge(store, bot)
    _seen, sent = _wire_mail(monkeypatch, [])
    with pytest.raises(BridgeDenied):
        asyncio.run(post_allowlisted(store, row, STRANGER, "hello stranger"))
    assert sent == []


def test_approval_card_email_ends_with_the_reply_line(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = _email_bridge(store, bot)
    remember_sender(store, row["id"], ME, ME)
    _seen, sent = _wire_mail(monkeypatch, [])
    card_id = "11111111-1111-1111-1111-111111111111"

    class Card:
        id = card_id
        why = "That message goes to someone else."
        detail = "email\nto: 7\nhi"

    asyncio.run(_send_card(store, bot["id"], Card()))
    assert len(sent) == 1
    assert sent[0]["to"] == ME
    assert sent[0]["subject"] == "EasyAgent needs you"
    assert f"Reply with: approve {card_id} or deny {card_id}" in sent[0]["body"]
    assert "That message goes to someone else." in sent[0]["body"]
    assert APP_PASS not in json.dumps(sent[0])


def test_email_keyboard_is_a_reply_note():
    card_id = "11111111-1111-1111-1111-111111111111"
    note = keyboard(card_id, "email")
    assert note == {"reply_note": f"Reply with: approve {card_id} or deny {card_id}"}


# --- inbound --------------------------------------------------------------------


def _mail(uid, from_addr, body):
    return {"uid": uid, "from_addr": from_addr, "subject": "re", "body_text": body}


def _answer(store, bot_id):
    async def reply(_store, _bot_id):
        return "three files"

    return reply


def test_inbound_allowlisted_sender_gets_a_reply_and_strangers_are_ignored(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = _email_bridge(store, bot)
    messages = [
        _mail("1", ME, "Count the notes."),
        _mail("2", STRANGER, "buy now"),
        _mail("3", BOT_MAIL, "my own echo"),
    ]
    seen, sent = _wire_mail(monkeypatch, messages)
    asyncio.run(_poll_email(store, dict(row), APP_PASS, _answer(store, bot["id"])))

    assert sent == [{"token": APP_PASS, "to": ME, "subject": "three files", "body": "three files"}]
    assert seen == ["1", "2", "3"]
    stored = store.ongoing_chat(bot["id"])
    assert stored["messages"][-1]["content"] == "Count the notes."
    log = json.loads((tmp_path / "bridge-log.json").read_text(encoding="utf-8"))
    decisions = [(entry["user_id"], entry["decision"]) for entry in log]
    assert (STRANGER, "ignored") in decisions
    assert (ME, "sent") in decisions
    assert all(entry["user_id"] != BOT_MAIL for entry in log)


def test_approve_by_text_resolves_the_card(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = _email_bridge(store, bot)
    card = Pending(
        id="22222222-2222-2222-2222-222222222222",
        bot_id=bot["id"],
        tier="ask",
        rule="bridge-send",
        why="That message goes to someone else.",
        detail="email\nto: 7\nhi",
        fingerprint="x",
        exact="7",
        offer_always=False,
        created=time.time(),
    )
    _PENDING[card.id] = card
    messages = [_mail("9", ME, f"Approve {card.id}")]
    seen, sent = _wire_mail(monkeypatch, messages)
    asyncio.run(_poll_email(store, dict(row), APP_PASS, _answer(store, bot["id"])))

    assert card.decision == "approve"
    assert card.event.is_set() is True
    assert seen == ["9"]
    assert sent == []

    deny_card = Pending(
        id="33333333-3333-3333-3333-333333333333",
        bot_id=bot["id"],
        tier="ask",
        rule="bridge-send",
        why="That message goes to someone else.",
        detail="email\nto: 7\nhi",
        fingerprint="y",
        exact="7",
        offer_always=False,
        created=time.time(),
    )
    _PENDING[deny_card.id] = deny_card
    seen2, sent2 = _wire_mail(monkeypatch, [_mail("10", ME, f"deny {deny_card.id}")])
    asyncio.run(_poll_email(store, dict(row), APP_PASS, _answer(store, bot["id"])))
    assert deny_card.decision == "deny"
    assert seen2 == ["10"]

    stranger_card = Pending(
        id="44444444-4444-4444-4444-444444444444",
        bot_id=bot["id"],
        tier="ask",
        rule="bridge-send",
        why="That message goes to someone else.",
        detail="email\nto: 7\nhi",
        fingerprint="z",
        exact="7",
        offer_always=False,
        created=time.time(),
    )
    _PENDING[stranger_card.id] = stranger_card
    _seen3, sent3 = _wire_mail(monkeypatch, [_mail("11", STRANGER, f"approve {stranger_card.id}")])
    asyncio.run(_poll_email(store, dict(row), APP_PASS, _answer(store, bot["id"])))
    assert stranger_card.event.is_set() is False
    assert sent3 == []


def test_a_busy_lane_leaves_the_mail_unseen_for_the_next_poll(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = _email_bridge(store, bot)
    seen, sent = _wire_mail(monkeypatch, [_mail("5", ME, "Count the notes.")])

    async def busy(_store, _bot_id):
        return True

    monkeypatch.setattr(bridges_mod, "_lane_holding", busy)
    asyncio.run(_poll_email(store, dict(row), APP_PASS, _answer(store, bot["id"])))
    assert seen == []
    assert sent == []


def test_run_bridges_polls_an_email_bridge(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = _email_bridge(store, bot)
    started = []
    stop = asyncio.Event()
    bridges_mod._EMAIL_LAST_POLL.clear()

    async def fake_poll(_store, bridge_row, token, _reply):
        started.append((bridge_row["id"], token))
        stop.set()

    monkeypatch.setattr(bridges_mod, "_poll_email", fake_poll)

    async def drive():
        await asyncio.wait_for(bridges_mod.run_bridges(store, stop, None), timeout=5)

    asyncio.run(drive())
    assert started == [(row["id"], APP_PASS)]
    bridges_mod._EMAIL_LAST_POLL.clear()


# --- routine delivery gate ------------------------------------------------------


def _schedule_body(deliver_kind, chat):
    return {
        "name": "Brief",
        "prompt": "Say good morning.",
        "daily": "8:00 AM",
        "deliver": deliver_kind,
        "deliver_chat": chat,
    }


def test_schedule_saves_an_email_routine_for_an_allowlisted_address(tmp_path):
    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    _email_bridge(store, bot)
    saved = client.post(f"/api/bots/{bot['id']}/schedules", json=_schedule_body("email", ME))
    assert saved.status_code == 200, saved.text
    assert saved.json()["deliver_chat"] == ME
    assert len(store.list_schedules(bot["id"])) == 1
    client.close()


def test_schedule_rejects_an_email_address_that_is_not_allowlisted(tmp_path):
    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    _email_bridge(store, bot)
    refused = client.post(f"/api/bots/{bot['id']}/schedules", json=_schedule_body("email", STRANGER))
    assert refused.status_code == 400, refused.text
    assert (
        refused.json()["detail"]
        == "That chat is not on this bot's Email allow list. The routine was not saved."
    )
    assert store.list_schedules(bot["id"]) == []
    client.close()


def test_schedule_rejects_an_unknown_delivery_kind(tmp_path):
    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    refused = client.post(f"/api/bots/{bot['id']}/schedules", json=_schedule_body("carrier-pigeon", ME))
    assert refused.status_code == 400, refused.text
    assert (
        refused.json()["detail"]
        == "A routine can post to Telegram, Discord, Slack, or Email, or stay in this chat."
    )
    assert store.list_schedules(bot["id"]) == []
    client.close()


def test_routine_run_posts_to_the_allowlisted_email_address(tmp_path, monkeypatch):
    from easyagent.schedule import _deliver, compile_routine

    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    _email_bridge(store, bot)
    _seen, sent = _wire_mail(monkeypatch, [])
    record = compile_routine(_schedule_body("email", ME))
    entry = {"status": "ok", "output": "Good morning.", "name": "Brief"}
    asyncio.run(_deliver(store, bot, record, entry))
    assert len(sent) == 1
    assert sent[0]["to"] == ME
    assert sent[0]["body"] == "Good morning."
    client.close()
