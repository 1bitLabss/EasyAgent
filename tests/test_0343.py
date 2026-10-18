"""A new message is its own turn. An older error is not the reply."""

from __future__ import annotations

import pytest

from easyagent.generation import RUNAWAY_TEXT, GenerationLimit
from easyagent.llm import ProviderError
from easyagent.store import now_iso
from tests.test_plan_0320 import _client

UNREACHABLE = "Can't reach the model at 127.0.0.1:8080. Check the connection or address."
RESUME = "Resume the checklist where it stopped"


def _plant_runaway(store, bot_id, chat_id):
    chat = store.get_chat(bot_id, chat_id)
    chat["messages"] = [
        {"id": "u-old", "role": "user", "content": "finish the checklist", "created_at": now_iso()},
        {
            "id": "a-old",
            "role": "assistant",
            "kind": "resume-offer",
            "content": RUNAWAY_TEXT,
            "created_at": now_iso(),
        },
    ]
    chat["run"] = {
        "id": "run-old",
        "status": "paused",
        "started_at": now_iso(),
        "last_activity_at": now_iso(),
        "current_step": "",
        "reason": RUNAWAY_TEXT,
    }
    store.save_chat(chat)


def _texts_after(messages, content):
    rows = [item for item in messages if isinstance(item, dict)]
    index = next(i for i, item in enumerate(rows) if item.get("content") == content and item.get("role") == "user")
    return rows[index + 1 :]


def test_a_new_message_is_not_answered_with_the_stale_runaway(tmp_path, monkeypatch):
    """Nathan: unreachable resume stays held, and the next line is not the old stop."""
    monkeypatch.setenv("EASYAGENT_MODEL_RETRY_SECONDS", "0")

    async def pause(_seconds):
        return None

    monkeypatch.setattr("easyagent.retry.pause", pause)
    calls = {"n": 0, "last_user": ""}

    async def complete(**kwargs):
        calls["n"] += 1
        last = ""
        for item in reversed(kwargs.get("messages") or []):
            if item.get("role") == "user":
                last = item.get("content") or ""
                break
        calls["last_user"] = last
        if calls["n"] >= 3 and RESUME in last:
            return "Picking up the checklist from the step that stopped."
        raise ProviderError("All connection attempts failed calling http://127.0.0.1:8080/v1")

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app, client, bot, chat = _client(tmp_path)
    client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    endpoint_id = bot["endpoint_id"]
    patched = client.patch(f"/api/endpoints/{endpoint_id}", json={"base_url": "http://127.0.0.1:8080/v1"})
    assert patched.status_code == 200, patched.text
    store = app.state.store
    _plant_runaway(store, bot["id"], chat["id"])
    base = f"/api/bots/{bot['id']}/chats/{chat['id']}"

    resumed = client.post(f"{base}/resume")
    assert resumed.status_code == 200, resumed.text
    saved = store.get_chat(bot["id"], chat["id"])
    resume_row = next(item for item in saved["messages"] if item.get("role") == "user" and RESUME in (item.get("content") or ""))
    assert resume_row["chip"] == "held"
    after_resume = _texts_after(saved["messages"], resume_row["content"])
    assert after_resume
    assert after_resume[-1]["content"] == UNREACHABLE
    assert after_resume[-1].get("id") != "a-old"
    assert RUNAWAY_TEXT not in (after_resume[-1].get("content") or "")

    sent = client.post(f"{base}/messages", json={"content": "uh oh what happened"})
    assert sent.status_code == 200, sent.text
    saved = store.get_chat(bot["id"], chat["id"])
    order = [item.get("content") for item in saved["messages"] if item.get("role") == "user"]
    assert order.index(resume_row["content"]) < order.index("uh oh what happened")
    held = next(item for item in saved["messages"] if item.get("id") == resume_row["id"])
    assert held["chip"] == "held"
    fresh = _texts_after(saved["messages"], "uh oh what happened")
    replies = [item for item in fresh if item.get("role") == "assistant" and item.get("kind") not in {"status", "pickup", "activity"}]
    assert len(replies) == 1
    assert replies[0]["content"] == UNREACHABLE
    assert replies[0].get("id") != "a-old"
    assert replies[0]["content"] != RUNAWAY_TEXT
    assert saved["messages"][1]["id"] == "a-old"
    assert saved["messages"][1]["content"] == RUNAWAY_TEXT

    again = client.post(f"{base}/messages/{held['id']}/send-now")
    assert again.status_code == 200, again.text
    assert RESUME in calls["last_user"]
    saved = store.get_chat(bot["id"], chat["id"])
    users = [item.get("content") for item in saved["messages"] if item.get("role") == "user"]
    assert users.index(resume_row["content"]) < users.index("uh oh what happened")
    last = saved["messages"][-1]
    assert last["role"] == "assistant"
    assert last["content"] == "Picking up the checklist from the step that stopped."
    assert last["content"] != RUNAWAY_TEXT
    assert last.get("id") != "a-old"
    client.close()


def test_silence_is_not_the_old_runaway_sentence(tmp_path, monkeypatch):
    """No tokens is a new error for this turn, not the earlier 'kept generating' line."""
    monkeypatch.setenv("EASYAGENT_MODEL_RETRY_SECONDS", "0")

    async def complete(**_kwargs):
        raise GenerationLimit("first_token")

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app, client, bot, chat = _client(tmp_path)
    client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    store = app.state.store
    _plant_runaway(store, bot["id"], chat["id"])
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "uh oh what happened"},
    )
    assert sent.status_code == 200, sent.text
    saved = store.get_chat(bot["id"], chat["id"])
    fresh = _texts_after(saved["messages"], "uh oh what happened")
    replies = [item for item in fresh if item.get("role") == "assistant" and item.get("kind") not in {"status", "pickup", "activity"}]
    assert len(replies) == 1
    assert replies[0]["id"] != "a-old"
    assert replies[0]["content"] == "The model sent nothing for 2 minutes."
    assert replies[0].get("error") is True
    assert replies[0].get("kind") != "resume-offer"
    assert next(item for item in saved["messages"] if item.get("content") == "uh oh what happened")["chip"] == "held"
    assert next(item for item in saved["messages"] if item.get("id") == "a-old")["content"] == RUNAWAY_TEXT
    client.close()


def test_an_error_still_drains_the_next_queued_line(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_MODEL_RETRY_SECONDS", "0")

    async def pause(_seconds):
        return None

    monkeypatch.setattr("easyagent.retry.pause", pause)
    calls = {"n": 0}

    async def complete(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            from easyagent.plan import enqueue

            enqueue(store, bot["id"], chat["id"], "second line")
            raise ProviderError("All connection attempts failed calling http://127.0.0.1:9/v1")
        return "answered the second line"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app, client, bot, chat = _client(tmp_path)
    client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False, "busy_input": "follow"})
    store = app.state.store
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "first line"},
    )
    assert sent.status_code == 200, sent.text
    saved = store.get_chat(bot["id"], chat["id"])
    second = next(item for item in saved["messages"] if item.get("content") == "second line")
    assert second.get("chip") == "answered"
    assert any(item.get("content") == "answered the second line" for item in saved["messages"])
    first = next(item for item in saved["messages"] if item.get("content") == "first line")
    assert first.get("chip") == "held"
    client.close()


def test_a_stale_stop_does_not_cancel_the_next_run(tmp_path, monkeypatch):
    from easyagent import turn as turn_mod

    seen = {"cancelled": None}

    async def complete(**_kwargs):
        seen["cancelled"] = turn_mod.cancelled()
        return "still here"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app, client, bot, chat = _client(tmp_path)
    client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    slot = turn_mod.slot_for(app.state.store, chat["id"])
    slot.user_stop = True
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "are you there"},
    )
    assert sent.status_code == 200, sent.text
    assert seen["cancelled"] is False
    assert any(item.get("content") == "still here" for item in sent.json().get("messages") or sent.json().get("chat", {}).get("messages") or [])
    client.close()
