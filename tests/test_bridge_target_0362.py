"""A fresh bridge can name who to card before anyone has written in.

Regression for the stall where an approval card went nowhere because
_send_card only looked at last_user_id/last_chat_id, which only inbound
traffic fills in.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.bridges import (
    _send_card,
    add_bridge,
    public_record,
    remember_sender,
    update_bridge,
)
from easyagent.store import Store, StoreError


@pytest.fixture(autouse=True)
def _memory_keyring(monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")


def _store(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    return store, bot


def _card():
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        why="The bot wants to run a command.",
        detail="run: delete the temp files",
    )


def _fake_posts(monkeypatch):
    posts = []

    async def fake_post(url, payload, headers=None):
        posts.append({"url": url, "payload": payload})
        return {}

    monkeypatch.setattr("easyagent.bridges.post_json", fake_post)
    return posts


def test_a_default_target_gets_the_card_before_any_inbound(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    add_bridge(
        store, kind="telegram", name="Telegram", bot_id=bot["id"], allow_ids=["42"],
        token="super-secret-token", default_user_id="42", default_chat_id="777",
    )
    posts = _fake_posts(monkeypatch)
    asyncio.run(_send_card(store, bot["id"], _card()))
    assert len(posts) == 1
    assert "api.telegram.org" in posts[0]["url"]
    assert posts[0]["payload"]["chat_id"] == "777"
    assert "The bot wants to run a command." in posts[0]["payload"]["text"]
    assert "delete the temp files" in posts[0]["payload"]["text"]
    assert "super-secret-token" not in json.dumps(posts[0]["payload"])


def test_a_missing_default_chat_falls_back_to_the_user_id(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    add_bridge(
        store, kind="telegram", name="Telegram", bot_id=bot["id"], allow_ids=["42"],
        token="super-secret-token", default_user_id="42",
    )
    posts = _fake_posts(monkeypatch)
    asyncio.run(_send_card(store, bot["id"], _card()))
    assert len(posts) == 1
    assert posts[0]["payload"]["chat_id"] == "42"


def test_a_real_inbound_still_wins_over_the_defaults(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="telegram", name="Telegram", bot_id=bot["id"], allow_ids=["42"],
        token="super-secret-token", default_user_id="42", default_chat_id="777",
    )
    remember_sender(store, row["id"], "42", "999")
    posts = _fake_posts(monkeypatch)
    asyncio.run(_send_card(store, bot["id"], _card()))
    assert len(posts) == 1
    assert posts[0]["payload"]["chat_id"] == "999"


def test_a_default_outside_the_allow_list_is_refused_at_creation(tmp_path):
    store, bot = _store(tmp_path)
    with pytest.raises(StoreError) as exc:
        add_bridge(
            store, kind="telegram", name="Telegram", bot_id=bot["id"], allow_ids=["42"],
            token="super-secret-token", default_user_id="999",
        )
    assert exc.value.message == "That default user is not on the allow list, so their replies would be ignored."
    assert not (tmp_path / "bridges.json").is_file()


def test_a_default_outside_the_allow_list_is_refused_at_patch(tmp_path):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="telegram", name="Telegram", bot_id=bot["id"], allow_ids=["42"],
        token="super-secret-token",
    )
    with pytest.raises(StoreError) as exc:
        update_bridge(store, row["id"], enabled=None, allow_ids=None, token=None, default_user_id="999")
    assert exc.value.message == "That default user is not on the allow list, so their replies would be ignored."
    saved = json.loads((tmp_path / "bridges.json").read_text(encoding="utf-8"))
    assert saved[0].get("default_user_id", "") == ""
    patched = update_bridge(store, row["id"], enabled=None, allow_ids=None, token=None,
                            default_user_id="42", default_chat_id="555")
    assert patched["default_user_id"] == "42"
    assert patched["default_chat_id"] == "555"


def test_email_defaults_are_compared_case_insensitively(tmp_path):
    store, bot = _store(tmp_path)
    row = add_bridge(
        store, kind="email", name="Mail", bot_id=bot["id"],
        allow_ids=["Me@Example.com"], token="app-pass",
        mail_user="bot@example.com", mail_imap_host="imap.example.com",
        mail_smtp_host="smtp.example.com", default_user_id="ME@example.com",
    )
    assert row["default_user_id"] == "me@example.com"


def test_public_record_shows_the_defaults_and_legacy_rows_read_empty():
    fresh = public_record({
        "id": "x", "default_user_id": "42", "default_chat_id": "777",
    })
    assert fresh["default_user_id"] == "42"
    assert fresh["default_chat_id"] == "777"
    legacy = public_record({"id": "x"})
    assert legacy["default_user_id"] == ""
    assert legacy["default_chat_id"] == ""


def test_a_bridge_with_no_target_at_all_is_skipped(tmp_path, monkeypatch):
    store, bot = _store(tmp_path)
    add_bridge(
        store, kind="telegram", name="Telegram", bot_id=bot["id"], allow_ids=["42"],
        token="super-secret-token",
    )
    posts = _fake_posts(monkeypatch)
    asyncio.run(_send_card(store, bot["id"], _card()))
    assert posts == []


def test_the_api_passes_the_default_target_through(tmp_path):
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={
        "name": "local", "base_url": "http://127.0.0.1:9/v1", "api_key": ""}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    made = client.post("/api/bridges", json={
        "kind": "telegram", "name": "Telegram", "bot_id": bot["id"],
        "allow_ids": ["42"], "token": "super-secret-token", "default_user_id": "42",
    })
    assert made.status_code == 200
    assert made.json()["default_user_id"] == "42"
    refused = client.post("/api/bridges", json={
        "kind": "telegram", "name": "Nope", "bot_id": bot["id"],
        "allow_ids": ["42"], "token": "super-secret-token", "default_user_id": "999",
    })
    assert refused.status_code == 400
    assert refused.json()["detail"] == "That default user is not on the allow list, so their replies would be ignored."
    patched = client.post(f"/api/bridges/{made.json()['id']}", json={"default_chat_id": "555"})
    assert patched.status_code == 200
    assert patched.json()["default_chat_id"] == "555"
    assert patched.json()["default_user_id"] == "42"
