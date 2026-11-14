"""A webhook bridge with a post_url pushes out over HTTP; without one it behaves as before.

The HTTP layer is faked: no test here touches the network. The pinned inbound
webhook tests are imported at the bottom and run again in this file, so a
change to the outbound path cannot quietly break the inbound route.
"""

import asyncio
import json

import httpx
import pytest

from easyagent import bridges as bridges_mod
from easyagent.bridges import (
    BridgeDenied,
    _send_card,
    add_bridge,
    deliver,
    post_allowlisted,
    public_record,
    remember_sender,
    update_bridge,
)
from easyagent.store import Store, StoreError
from easyagent.tools import ToolError, ToolRequest, execute
from tests.test_0335_channels import (
    test_a_bad_token_and_a_stranger_do_not_touch_the_chat,
    test_a_webhook_to_someone_else_waits_and_the_token_stays_out_of_the_outbox,
)
from tests.test_plan_0320 import _client

TOKEN = "webhook-token-value"
RELAY = "http://127.0.0.1:9/relay"


@pytest.fixture(autouse=True)
def _memory_keyring(monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")


def _store(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    return store, bot


def _webhook_bridge(store, bot, post_url=""):
    return add_bridge(
        store, kind="webhook", name="Hook", bot_id=bot["id"],
        allow_ids=["ada"], token=TOKEN, post_url=post_url,
    )


def _wire_http(monkeypatch, fail=False):
    """Fake bridges.post_json; return the list of (url, payload) posts."""
    posts = []

    async def fake_post(url, payload, headers=None):
        if fail:
            raise RuntimeError("The bridge did not answer.")
        posts.append({"url": url, "payload": payload})
        return {}

    monkeypatch.setattr(bridges_mod, "post_json", fake_post)
    return posts


# --- config ---------------------------------------------------------------------


def test_add_bridge_saves_post_url_and_shows_it_publicly(tmp_path):
    store, bot = _store(tmp_path)
    row = _webhook_bridge(store, bot, RELAY)
    assert row["post_url"] == RELAY
    public = public_record(row)
    assert public["post_url"] == RELAY
    assert TOKEN not in json.dumps(public)
    saved = (tmp_path / "bridges.json").read_text(encoding="utf-8")
    assert TOKEN not in saved


def test_add_bridge_refuses_a_post_url_that_is_not_http(tmp_path):
    store, bot = _store(tmp_path)
    with pytest.raises(StoreError) as exc:
        _webhook_bridge(store, bot, "ftp://relay/here")
    assert exc.value.message == "The post address must start with http:// or https://."
    assert not (tmp_path / "bridges.json").is_file()


def test_update_bridge_sets_post_url_only_on_a_webhook_bridge(tmp_path):
    store, bot = _store(tmp_path)
    row = _webhook_bridge(store, bot)
    updated = update_bridge(store, row["id"], enabled=None, allow_ids=None, token=None, post_url=RELAY)
    assert updated["post_url"] == RELAY
    assert update_bridge(store, row["id"], enabled=None, allow_ids=None, token=None, post_url="")["post_url"] == ""
    tg = add_bridge(store, kind="telegram", name="TG", bot_id=bot["id"], allow_ids=["7"], token="t" * 12)
    untouched = update_bridge(store, tg["id"], enabled=None, allow_ids=None, token=None, post_url=RELAY)
    assert untouched.get("post_url", "") == ""


def test_api_round_trips_post_url(tmp_path):
    _app, client, bot, _chat = _client(tmp_path)
    made = client.post(
        "/api/bridges",
        json={"kind": "webhook", "name": "Hook", "bot_id": bot["id"], "allow_ids": ["ada"],
              "token": TOKEN, "post_url": RELAY},
    )
    assert made.status_code == 200, made.text
    assert made.json()["post_url"] == RELAY
    assert TOKEN not in made.text
    patched = client.post(f"/api/bridges/{made.json()['id']}", json={"post_url": "https://relay.example/hook"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["post_url"] == "https://relay.example/hook"
    refused = client.post(f"/api/bridges/{made.json()['id']}", json={"post_url": "carrier-pigeon:"})
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"] == "The post address must start with http:// or https://."
    client.close()


# --- outbound messages ------------------------------------------------------------


def test_deliver_posts_the_message_json_to_the_relay(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    _webhook_bridge(store, bot, RELAY)
    posts = _wire_http(monkeypatch)

    async def send():
        return await execute(
            store, ToolRequest(kind="bridge", path="Hook", command="ada", body="hello ada"), bot["id"]
        )

    text = asyncio.run(send())
    assert "Sent" in text
    assert len(posts) == 1
    assert posts[0]["url"] == RELAY
    assert posts[0]["payload"] == {"text": "hello ada", "bridge": "Hook", "recipient": "ada"}
    assert TOKEN not in json.dumps(posts[0])
    outbox = (tmp_path / "webhook-outbox.json").read_text(encoding="utf-8")
    assert "hello ada" in outbox


def test_deliver_without_post_url_keeps_the_old_outbox_behavior(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    _webhook_bridge(store, bot)
    posts = _wire_http(monkeypatch)

    async def send():
        return await execute(
            store, ToolRequest(kind="bridge", path="Hook", command="ada", body="hello ada"), bot["id"]
        )

    text = asyncio.run(send())
    assert "Sent" in text
    assert posts == []
    outbox = (tmp_path / "webhook-outbox.json").read_text(encoding="utf-8")
    assert "hello ada" in outbox


def test_post_allowlisted_posts_to_the_relay_and_refuses_a_stranger(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = _webhook_bridge(store, bot, RELAY)
    posts = _wire_http(monkeypatch)
    asyncio.run(post_allowlisted(store, row, "ada", "routine result"))
    assert posts == [{"url": RELAY, "payload": {"text": "routine result", "bridge": "Hook", "recipient": "ada"}}]
    with pytest.raises(BridgeDenied):
        asyncio.run(post_allowlisted(store, row, "stranger", "hello stranger"))
    assert len(posts) == 1


def test_post_allowlisted_without_post_url_behaves_as_before(tmp_path, monkeypatch):
    """No post_url must not raise on its own. The old else-telegram fallthrough stays."""
    store, bot = _store(tmp_path)
    row = _webhook_bridge(store, bot)
    posts = _wire_http(monkeypatch)
    asyncio.run(post_allowlisted(store, row, "ada", "hello ada"))
    assert len(posts) == 1
    assert "api.telegram.org" in posts[0]["url"]


# --- approval cards ----------------------------------------------------------------


class _Card:
    id = "11111111-1111-1111-1111-111111111111"
    why = "That message goes to someone else."
    detail = "webhook\nto: 7\nhi"


def test_card_delivery_carries_the_card_id_and_decision_texts(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = _webhook_bridge(store, bot, RELAY)
    remember_sender(store, row["id"], "ada", "ada")
    posts = _wire_http(monkeypatch)
    asyncio.run(_send_card(store, bot["id"], _Card()))
    assert len(posts) == 1
    assert posts[0]["url"] == RELAY
    payload = posts[0]["payload"]
    assert payload["card_id"] == _Card.id
    assert payload["approve_text"] == f"approve {_Card.id}"
    assert payload["deny_text"] == f"deny {_Card.id}"
    assert payload["callback_approve"] == f"ea:{_Card.id}:approve"
    assert payload["callback_deny"] == f"ea:{_Card.id}:deny"
    assert payload["bridge"] == "Hook"
    assert "That message goes to someone else." in payload["text"]
    assert TOKEN not in json.dumps(payload)


def test_card_without_post_url_behaves_as_before(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = _webhook_bridge(store, bot)
    remember_sender(store, row["id"], "ada", "ada")
    posts = _wire_http(monkeypatch)
    asyncio.run(_send_card(store, bot["id"], _Card()))
    assert len(posts) == 1
    assert "api.telegram.org" in posts[0]["url"]


# --- failures surface ---------------------------------------------------------------


def test_a_refused_relay_answer_raises(tmp_path):
    """The real post_json treats a non-2xx as a failure. No network: MockTransport."""
    def handler(request):
        return httpx.Response(403, json={"ok": False})

    real_client = httpx.AsyncClient
    monkey = httpx.MockTransport(handler)
    httpx.AsyncClient = lambda **kw: real_client(transport=monkey, **kw)
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(bridges_mod.post_json(RELAY, {"text": "hi"}))
    finally:
        httpx.AsyncClient = real_client


def test_deliver_reports_a_dead_relay_like_slack_does(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    _webhook_bridge(store, bot, RELAY)
    _wire_http(monkeypatch, fail=True)

    async def send():
        return await execute(
            store, ToolRequest(kind="bridge", path="Hook", command="ada", body="hello ada"), bot["id"]
        )

    with pytest.raises(ToolError) as exc:
        asyncio.run(send())
    assert "The bridge did not answer. It was not sent." in str(exc.value)
    assert not (tmp_path / "webhook-outbox.json").is_file()


# --- routine delivery -----------------------------------------------------------------


def test_routine_run_posts_to_the_webhook_relay(tmp_path, monkeypatch):
    from easyagent.schedule import _deliver, compile_routine

    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    add_bridge(
        store, kind="webhook", name="Hook", bot_id=bot["id"],
        allow_ids=["ada"], token=TOKEN, post_url=RELAY,
    )
    posts = _wire_http(monkeypatch)
    record = compile_routine({
        "name": "Brief", "prompt": "Say good morning.", "daily": "8:00 AM",
        "deliver": "webhook", "deliver_chat": "ada",
    })
    entry = {"status": "ok", "output": "Good morning.", "name": "Brief"}
    asyncio.run(_deliver(store, bot, record, entry))
    assert posts == [{"url": RELAY, "payload": {"text": "Good morning.", "bridge": "Hook", "recipient": "ada"}}]
    client.close()


def test_schedule_rejects_a_webhook_chat_that_is_not_allowlisted(tmp_path):
    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    _webhook_bridge(store, bot, RELAY)
    refused = client.post(
        f"/api/bots/{bot['id']}/schedules",
        json={"name": "Brief", "prompt": "Say good morning.", "daily": "8:00 AM",
              "deliver": "webhook", "deliver_chat": "stranger"},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"] == "That chat is not on this bot's Webhook allow list. The routine was not saved."
    assert store.list_schedules(bot["id"]) == []
    client.close()
