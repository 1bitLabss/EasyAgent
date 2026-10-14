"""Harness rows stay on screen and stay out of the model request."""

from __future__ import annotations

import json

from easyagent import llm
from easyagent import turn as turn_mod
from easyagent.context import coalesce_assistants, is_harness_message, prepare_context
from easyagent.store import now_iso
from easyagent.tools import LOOP_TEXT
from tests.test_plan_0320 import _client

HARBOR = "add the harbor line"
ASIDE = "do not send this aside"
STILL = "Still on this job. Working. Last activity: Reading notes. Elapsed 1s. Queued: 1."
PICKUP = "Picked up your message"
ACTIVITY = "Writing notes.txt"
STOPPED = "Stopped: you pressed Stop"
OPENED = "I opened the file."
READ = "I read the first page."
LATER = "later partial"
AGAIN = "and another real line"

_BANNED = (STILL, PICKUP, ACTIVITY, STOPPED, LOOP_TEXT, ASIDE, "Not sent (job stopped)")


def test_prepare_context_skips_harness_rows_and_merges_assistants():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "kind": "status", "content": STILL},
        {"role": "assistant", "kind": "pickup", "content": PICKUP},
        {"role": "assistant", "id": "activity", "kind": "status", "about": "activity", "content": ACTIVITY},
        {"role": "assistant", "content": STOPPED},
        {"role": "assistant", "kind": "resume-offer", "content": LOOP_TEXT},
        {"role": "user", "content": ASIDE, "chip": "unsent", "refuse_reason": "Not sent (job stopped)"},
        {"role": "assistant", "content": OPENED},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": READ},
        {"role": "user", "content": HARBOR},
    ]
    prepared = prepare_context(messages, "", 0)
    blob = json.dumps({"summary": prepared.summary, "tail": prepared.tail})
    for phrase in _BANNED:
        assert phrase not in blob
    assert [item["content"] for item in prepared.tail] == ["hello", OPENED, "", READ, HARBOR]
    merged = coalesce_assistants(
        [{"role": "system", "content": "rules"}, *prepared.tail]
    )
    roles = [item["role"] for item in merged]
    assert roles == ["system", "user", "assistant", "user"]
    assert merged[2]["content"] == f"{OPENED}\n\n{READ}"
    assert is_harness_message({"role": "assistant", "content": "I said Stopped: only as part of a longer reply."}) is False
    assert is_harness_message({"role": "user", "content": "Stopped: please continue"}) is False


def _plant(store, bot_id, chat_id):
    chat = store.get_chat(bot_id, chat_id)
    chat["messages"] = [
        {"id": "u-start", "role": "user", "content": "start the notes", "created_at": now_iso()},
        {"id": "a-open", "role": "assistant", "content": OPENED, "created_at": now_iso()},
        {"id": "a-read", "role": "assistant", "content": READ, "created_at": now_iso()},
        {
            "id": "u-aside",
            "role": "user",
            "content": ASIDE,
            "chip": "unsent",
            "refuse_reason": "Not sent (job stopped)",
            "created_at": now_iso(),
        },
        {"id": "s-status", "role": "assistant", "kind": "status", "content": STILL, "about": "u-harbor", "created_at": now_iso()},
        {"id": "s-pick", "role": "assistant", "kind": "pickup", "content": PICKUP, "about": "u-harbor", "created_at": now_iso()},
        {
            "id": "activity",
            "role": "assistant",
            "kind": "status",
            "about": "activity",
            "content": ACTIVITY,
            "created_at": now_iso(),
        },
        {"id": "s-stop", "role": "assistant", "content": STOPPED, "created_at": now_iso()},
        {"id": "s-loop", "role": "assistant", "kind": "resume-offer", "content": LOOP_TEXT, "created_at": now_iso()},
        {
            "id": "u-harbor",
            "role": "user",
            "content": HARBOR,
            "chip": "unsent",
            "refuse_reason": "Not sent (job stopped)",
            "steer": True,
            "created_at": now_iso(),
        },
        {"id": "a-later", "role": "assistant", "content": LATER, "created_at": now_iso()},
        {"id": "a-empty", "role": "assistant", "content": "   ", "created_at": now_iso()},
        {"id": "a-again", "role": "assistant", "content": AGAIN, "created_at": now_iso()},
    ]
    chat["run"] = {"id": "run-keep", "status": "stopped", "current_step": "", "reason": "you pressed Stop"}
    store.save_chat(chat)


def _strict_complete(seen: list):
    async def complete(**kwargs):
        messages = kwargs.get("messages") or []
        seen.append(messages)
        roles = [item.get("role") for item in messages if item.get("role") != "system"]
        if len(roles) >= 2 and roles[-1] == "assistant" and roles[-2] == "assistant":
            raise llm.ProviderError("400 Cannot have 2 or more assistant messages at the end of the list")
        return "Sent it."

    return complete


def _assert_request(seen: list):
    assert seen, "the model was not called"
    blob = json.dumps(seen)
    for phrase in _BANNED:
        assert phrase not in blob
    messages = seen[0]
    roles = [item.get("role") for item in messages if item.get("role") != "system"]
    assert roles[-1] == "user"
    assert not (len(roles) >= 2 and roles[-1] == "assistant" and roles[-2] == "assistant")
    for left, right in zip(roles, roles[1:]):
        assert (left, right) != ("assistant", "assistant")
    last = messages[-1]
    body = last.get("content")
    text = body if isinstance(body, str) else json.dumps(body)
    assert HARBOR in text
    joined = json.dumps(messages)
    assert OPENED in joined and READ in joined and LATER in joined and AGAIN in joined
    assistant = next(item for item in messages if item.get("role") == "assistant" and OPENED in str(item.get("content")))
    assert READ in str(assistant.get("content"))
    assert LATER in str(assistant.get("content"))


def test_send_now_puts_the_user_last_and_omits_harness_rows(tmp_path, monkeypatch):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _plant(store, bot["id"], chat["id"])
    slot = turn_mod.slot_for(store, chat["id"])
    slot.busy = False
    slot.user_stop = False
    seen: list = []
    monkeypatch.setattr("easyagent.llm.complete", _strict_complete(seen))
    sent = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/u-harbor/send-now")
    assert sent.status_code == 200, sent.text
    _assert_request(seen)
    body = sent.json()
    saved = body.get("messages") or body.get("chat", {}).get("messages") or []
    text = json.dumps(saved)
    assert STILL in text and PICKUP in text and STOPPED in text and LOOP_TEXT in text
    aside = next(item for item in saved if item.get("id") == "u-aside")
    assert aside["chip"] == "unsent"
    assert any(item.get("content") == "Sent it." for item in saved)
    client.close()


def test_stop_and_send_puts_the_user_last_and_omits_harness_rows(tmp_path, monkeypatch):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _plant(store, bot["id"], chat["id"])
    slot = turn_mod.slot_for(store, chat["id"])
    slot.busy = True
    slot.user_stop = False
    slot.worker = None
    seen: list = []
    monkeypatch.setattr("easyagent.llm.complete", _strict_complete(seen))
    sent = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/u-harbor/stop-and-send")
    assert sent.status_code == 200, sent.text
    _assert_request(seen)
    body = sent.json()
    saved = body.get("messages") or body.get("chat", {}).get("messages") or []
    text = json.dumps(saved)
    assert STILL in text and STOPPED in text and LOOP_TEXT in text
    assert "Not sent (job stopped)" in text
    aside = next(item for item in saved if item.get("id") == "u-aside")
    assert aside["chip"] == "unsent"
    harbor = next(item for item in saved if item.get("id") == "u-harbor")
    assert harbor["chip"] == "answered"
    assert any(item.get("content") == "Sent it." for item in saved)
    client.close()
