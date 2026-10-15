"""A cut request cannot stay running, waits are per connection, and a routine chat is checked up front."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from easyagent.app import (
    WEDGE_TEXT,
    _finish_reply,
    _seal_wedged_run,
    _watch_wedged,
    create_app,
)
from easyagent.bridges import add_bridge
from easyagent.generation import GenerationLimit, GenerationWatch, Limits, limits_from_endpoint
from easyagent.plan import enqueue, queued_items
from easyagent.safety import Pending, _PENDING
from easyagent.store import now_iso
from easyagent import turn as turn_mod
from tests.test_plan_0320 import _client


def _clock(box: dict):
    return lambda: box["t"]


def test_first_token_waits_longer_than_the_gap_between_tokens():
    assert limits_from_endpoint(None).first_token_seconds == 120
    assert limits_from_endpoint(None).stall_seconds == 45
    tuned = limits_from_endpoint({"stall_seconds": 30, "first_token_seconds": 90})
    assert tuned.stall_seconds == 30
    assert tuned.first_token_seconds == 90
    assert limits_from_endpoint({"stall_seconds": 80, "first_token_seconds": 20}).first_token_seconds == 80

    box = {"t": 0.0}
    limits = Limits(max_tokens=1000, stall_seconds=10, first_token_seconds=30, wall_seconds=100, hard_seconds=200)
    waiting = GenerationWatch(limits, clock=_clock(box))
    box["t"] = 10
    waiting.poll()
    box["t"] = 30
    with pytest.raises(GenerationLimit) as first:
        waiting.poll()
    assert first.value.reason == "first_token"

    box["t"] = 0.0
    moving = GenerationWatch(limits, clock=_clock(box))
    moving.observe("token")
    box["t"] = 10
    with pytest.raises(GenerationLimit) as stalled:
        moving.poll()
    assert stalled.value.reason == "stall"


def test_connection_saves_the_two_waits(tmp_path):
    app, client, bot, _chat = _client(tmp_path)
    saved = client.patch(
        f"/api/endpoints/{bot['endpoint_id']}",
        json={"stall_seconds": 20, "first_token_seconds": 100},
    ).json()
    assert saved["stall_seconds"] == 20
    assert saved["first_token_seconds"] == 100
    again = client.get("/api/endpoints").json()[0]
    assert again["stall_seconds"] == 20
    assert again["first_token_seconds"] == 100
    client.close()
    stored = app.state.store.get_endpoint(bot["endpoint_id"])
    assert stored["stall_seconds"] == 20
    assert stored["first_token_seconds"] == 100


def test_stall_guard_uses_the_first_token_wait(monkeypatch):
    import easyagent.llm as llm
    from easyagent.generation import _limits

    class SlowResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def __init__(self, delay: float):
            self.delay = delay

        async def aiter_lines(self):
            await asyncio.sleep(self.delay)
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class SlowClient:
        delay = 1.0

        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            return None

        def stream(self, method, url, json=None, headers=None):
            delay = SlowClient.delay

            class Context:
                async def __aenter__(self_inner):
                    return SlowResponse(delay)

                async def __aexit__(self_inner, *args):
                    return False

            return Context()

    monkeypatch.setattr(llm.httpx, "AsyncClient", SlowClient)
    token = _limits.set(Limits(max_tokens=256, stall_seconds=2, first_token_seconds=0.05, wall_seconds=30, hard_seconds=60))

    async def read():
        async for _piece in llm._stream_attempt(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="stub",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        ):
            pass

    try:
        with pytest.raises(GenerationLimit) as first:
            asyncio.run(read())
        assert first.value.reason == "first_token"
    finally:
        _limits.reset(token)


def test_a_guard_cut_pauses_and_keeps_the_line_seen(tmp_path, monkeypatch):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["run"] = {
        "id": "run-cut",
        "status": "running",
        "started_at": now_iso(),
        "last_activity_at": now_iso(),
        "current_step": "Waiting on model",
        "reason": "",
    }
    doc["messages"].append({
        "id": "live-cut",
        "role": "assistant",
        "content": "still writing",
        "live": True,
        "created_at": now_iso(),
    })
    store.save_chat(doc)
    enqueue(store, bot["id"], chat["id"], "while it runs")
    slot = turn_mod.slot_for(store, chat["id"])
    slot.busy = False
    slot.worker = None
    slot.client = None

    async def cut(**_kwargs):
        raise GenerationLimit("stall")

    monkeypatch.setattr("easyagent.app.complete_with_tools", cut)
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "go on"},
    )
    assert sent.status_code == 200, sent.text
    saved = store.get_chat(bot["id"], chat["id"])
    assert saved["run"]["status"] == "paused"
    line = next(item for item in saved["messages"] if item.get("content") == "while it runs")
    assert line["chip"] == "seen"
    assert "while it runs" in [item["content"] for item in queued_items(store, bot["id"], chat["id"])]
    assert turn_mod.request_in_flight(store, chat["id"]) is False
    client.close()


def test_stop_clears_a_stuck_running_chat(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    old = (datetime.now(timezone.utc) - timedelta(seconds=400)).replace(microsecond=0).isoformat()
    doc["run"] = {
        "id": "run-stuck",
        "status": "running",
        "started_at": old,
        "last_activity_at": old,
        "current_step": "Waiting on model",
        "reason": "",
    }
    doc["messages"].append({
        "id": "live-stuck",
        "role": "assistant",
        "content": "",
        "live": True,
        "created_at": now_iso(),
    })
    store.save_chat(doc)
    slot = turn_mod.slot_for(store, chat["id"])
    slot.busy = True
    slot.worker = None
    slot.client = None
    stopped = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/stop")
    assert stopped.status_code == 200, stopped.text
    saved = store.get_chat(bot["id"], chat["id"])
    assert saved["run"]["status"] != "running"
    assert turn_mod.is_busy(store, chat["id"]) is False
    client.close()


def test_watchdog_pauses_a_quiet_run_with_nothing_in_flight(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    old = (datetime.now(timezone.utc) - timedelta(seconds=400)).replace(microsecond=0).isoformat()
    doc["run"] = {
        "id": "run-wedge",
        "status": "running",
        "started_at": old,
        "last_activity_at": old,
        "current_step": "Waiting on model",
        "reason": "",
    }
    store.save_chat(doc)
    enqueue(store, bot["id"], chat["id"], "hold this")
    slot = turn_mod.slot_for(store, chat["id"])
    slot.client = None
    slot.proc = None
    _watch_wedged(store)
    saved = store.get_chat(bot["id"], chat["id"])
    assert saved["run"]["status"] == "paused"
    assert any(item.get("content") == WEDGE_TEXT for item in saved["messages"])
    line = next(item for item in saved["messages"] if item.get("content") == "hold this")
    assert line["chip"] == "seen"
    assert "hold this" in [item["content"] for item in queued_items(store, bot["id"], chat["id"])]
    assert turn_mod.request_in_flight(store, chat["id"]) is False

    fresh = (datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    other = store.get_chat(bot["id"], chat["id"])
    other["run"] = {
        "id": "run-live",
        "status": "running",
        "started_at": fresh,
        "last_activity_at": fresh,
        "current_step": "Waiting on model",
        "reason": "",
    }
    store.save_chat(other)
    _seal_wedged_run(store, bot["id"], chat["id"], force=False)
    assert store.get_chat(bot["id"], chat["id"])["run"]["status"] == "running"

    card = Pending(
        id="card-wait",
        bot_id=bot["id"],
        tier="ask",
        rule="shell",
        why="Run this?",
        detail="echo hi",
        fingerprint="fp",
        exact="echo hi",
        offer_always=False,
        created=0.0,
    )
    other = store.get_chat(bot["id"], chat["id"])
    other["run"]["last_activity_at"] = old
    store.save_chat(other)
    _PENDING[card.id] = card
    try:
        _watch_wedged(store)
        assert store.get_chat(bot["id"], chat["id"])["run"]["status"] == "running"
    finally:
        _PENDING.pop(card.id, None)
    client.close()


def test_auto_resume_does_not_repeat_the_answer(tmp_path, monkeypatch):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "Look at the kiln.", "created_at": now_iso()},
        {
            "id": "a1",
            "role": "assistant",
            "content": "The kiln is fine.",
            "live": True,
            "created_at": now_iso(),
        },
    ]
    doc["run"] = {
        "id": "run-dead",
        "status": "running",
        "started_at": now_iso(),
        "last_activity_at": now_iso(),
        "current_step": "Waiting on model",
        "reason": "",
    }
    store.save_chat(doc)
    from easyagent.app import _pause_for_resume

    _pause_for_resume(store, bot["id"], chat["id"])

    async def same(**_kwargs):
        return "The kiln is fine."

    monkeypatch.setattr("easyagent.llm.complete", same)
    resumed = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    saved = store.get_chat(bot["id"], chat["id"])
    answers = [
        item.get("content")
        for item in saved["messages"]
        if item.get("role") == "assistant" and item.get("content") == "The kiln is fine."
    ]
    assert answers == ["The kiln is fine."]
    client.close()


def test_a_different_resume_answer_is_kept(tmp_path):
    app, _client_unused, bot, chat = _client(tmp_path)
    store = app.state.store
    chat_doc = store.get_chat(bot["id"], chat["id"])
    chat_doc["messages"] = [
        {"id": "a1", "role": "assistant", "content": "The kiln is fine.", "created_at": now_iso()},
        {
            "id": "note",
            "role": "assistant",
            "kind": "resume-note",
            "content": "Picked up where it stopped.",
            "created_at": now_iso(),
        },
    ]
    store.save_chat(chat_doc)
    done = _finish_reply(store, bot["id"], chat["id"], "The kiln needed a new shelf.", [])
    texts = [item.get("content") for item in done["chat"]["messages"] if item.get("role") == "assistant"]
    assert "The kiln is fine." in texts
    assert "The kiln needed a new shelf." in texts
    _client_unused.close()


def test_schedule_rejects_a_chat_that_is_not_allowlisted(tmp_path):
    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    body = {
        "name": "Brief",
        "prompt": "Say good morning.",
        "daily": "8:00 AM",
        "deliver": "telegram",
        "deliver_chat": "999",
    }
    refused = client.post(f"/api/bots/{bot['id']}/schedules", json=body)
    assert refused.status_code == 400, refused.text
    assert store.list_schedules(bot["id"]) == []
    add_bridge(store, kind="telegram", name="Phone", bot_id=bot["id"], allow_ids=["42"], token="token-value")
    still = client.post(f"/api/bots/{bot['id']}/schedules", json=body)
    assert still.status_code == 400, still.text
    assert store.list_schedules(bot["id"]) == []
    body["deliver_chat"] = "42"
    saved = client.post(f"/api/bots/{bot['id']}/schedules", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["deliver_chat"] == "42"
    assert len(store.list_schedules(bot["id"])) == 1
    client.close()
