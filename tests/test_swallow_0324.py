"""A message that was accepted is answered. It is not left queued on an idle chat."""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from easyagent import llm
from easyagent import turn as turn_mod
from easyagent.app import create_app
from easyagent.app import kick
from easyagent.plan import HELD_REASON, QUEUE_CAP, WAITING_CAP, enqueue, mark_answered, mark_held, queued_items
from tests.test_plan_0320 import _client, _running


def _quiet(client, bot_id: str) -> None:
    client.patch(f"/api/bots/{bot_id}", json={"check_enabled": False})


def test_a_message_landing_in_the_finish_gap_is_drained(tmp_path, monkeypatch):
    import easyagent.app as app_mod

    calls = {"n": 0, "gapped": False}

    async def complete(**_kwargs):
        calls["n"] += 1
        return "Done."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app, client, bot, chat = _client(tmp_path)
    _quiet(client, bot["id"])
    store = app.state.store

    async def gap(gap_store, bot_id, chat_id):
        if calls["gapped"]:
            return
        calls["gapped"] = True
        from easyagent.plan import enqueue

        enqueue(gap_store, bot_id, chat_id, "landed in the gap")

    monkeypatch.setattr(app_mod, "_FINISH_GAP", gap)
    response = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello"},
    )
    assert response.status_code == 200, response.text
    saved = None
    for _ in range(40):
        saved = store.get_chat(bot["id"], chat["id"])
        late = next((item for item in saved["messages"] if item.get("content") == "landed in the gap"), None)
        if late and late.get("chip") == "answered" and not turn_mod.is_busy(store, chat["id"]):
            break
        client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}")
    assert saved is not None
    late = next(item for item in saved["messages"] if item.get("content") == "landed in the gap")
    assert late["chip"] == "answered"
    assert calls["n"] >= 2
    assert queued_items(store, bot["id"], chat["id"]) == []
    assert turn_mod.is_busy(store, chat["id"]) is False
    client.close()


def test_follow_cap_leaves_the_rest_for_a_fresh_turn(tmp_path, monkeypatch):
    calls = {"n": 0}
    app, client, bot, chat = _client(tmp_path)
    _quiet(client, bot["id"])
    client.patch(f"/api/bots/{bot['id']}", json={"busy_input": "follow"})
    store = app.state.store

    async def complete(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            from easyagent.plan import enqueue

            for number in range(4):
                enqueue(store, bot["id"], chat["id"], f"follow {number}")
        return f"Answer {calls['n']}"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    response = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "start"},
    )
    assert response.status_code == 200, response.text
    for _ in range(40):
        saved = store.get_chat(bot["id"], chat["id"])
        last = next((item for item in saved["messages"] if item.get("content") == "follow 3"), None)
        if last and last.get("chip") == "answered":
            break
        client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}")
    saved = store.get_chat(bot["id"], chat["id"])
    last = next(item for item in saved["messages"] if item.get("content") == "follow 3")
    assert last["chip"] == "answered"
    assert calls["n"] >= 5
    assert calls["n"] < 20
    client.close()


def test_a_full_queue_stays_visible_and_send_now_retries_it(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    for number in range(QUEUE_CAP):
        assert client.post(url, json={"content": f"n{number}"}).status_code == 200
    refused = client.post(url, json={"content": "one past the cap"})
    assert refused.status_code == 200
    assert "queue is full" in refused.json()["reply"]
    row = next(item for item in store.get_chat(bot["id"], chat["id"])["messages"] if item.get("content") == "one past the cap")
    assert row["chip"] == "held"
    assert "Retry" in row.get("refuse_reason", "")
    sent = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/{row['id']}/send-now")
    assert sent.status_code == 200
    again = next(item for item in sent.json()["messages"] if item["id"] == row["id"])
    assert again["chip"] == "queued"
    client.close()


def test_an_error_does_not_mark_a_picked_up_line_answered(tmp_path, monkeypatch):
    plain = {
        "messages": [
            {"role": "user", "chip": "seen", "content": "hi"},
            {"role": "assistant", "content": "Stopped: 500 from the model", "error": True},
        ]
    }
    assert mark_answered(plain) is False
    assert plain["messages"][0]["chip"] == "seen"
    assert mark_held(plain) is not None
    assert plain["messages"][0]["chip"] == "held"
    assert plain["messages"][0]["refuse_reason"] == HELD_REASON
    answered = {
        "messages": [
            {"role": "user", "chip": "seen", "content": "hi"},
            {"role": "assistant", "content": "Here is the reply."},
        ]
    }
    assert mark_held(answered) == set()
    assert answered["messages"][0]["chip"] == "seen"

    calls = {"n": 0}
    app, client, bot, chat = _client(tmp_path)
    _quiet(client, bot["id"])
    store = app.state.store

    async def complete(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            from easyagent.plan import enqueue

            enqueue(store, bot["id"], chat["id"], "still waiting")
            raise llm.ProviderError("500 from http://127.0.0.1:9/v1: boom")
        return "Recovered."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    response = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello"},
    )
    assert response.status_code == 200, response.text
    saved = store.get_chat(bot["id"], chat["id"])
    hello = next(item for item in saved["messages"] if item.get("content") == "hello")
    assert hello["chip"] == "held"
    assert "Retry" in hello.get("refuse_reason", "")
    waiting = next(item for item in saved["messages"] if item.get("content") == "still waiting")
    assert waiting["chip"] == "answered"
    assert any(item.get("content") == "Recovered." for item in saved["messages"])
    assert any(str(item.get("content") or "").startswith("Stopped:") for item in saved["messages"])
    client.close()


def test_stop_keeps_queued_lines_for_the_next_turn(tmp_path, monkeypatch):
    seen: list = []

    async def complete(**kwargs):
        seen.append(kwargs.get("messages"))
        return "Sent it."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app, client, bot, chat = _client(tmp_path)
    _quiet(client, bot["id"])
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    assert client.post(url, json={"content": "alpha-queued"}).status_code == 200
    assert client.post(url, json={"content": "beta-queued"}).status_code == 200
    stopped = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/stop")
    assert stopped.status_code == 200
    users = [item for item in stopped.json()["messages"] if item.get("role") == "user"]
    assert [item["content"] for item in users] == ["alpha-queued", "beta-queued"]
    assert [item["chip"] for item in users] == ["answered", "answered"]
    assert asyncio.run(kick(store)) == 0
    assert queued_items(store, bot["id"], chat["id"]) == []
    blobs = [json.dumps(batch) for batch in seen]
    first = next(blob for blob in blobs if "alpha-queued" in blob)
    assert first.index("alpha-queued") < first.index("beta-queued")
    assert "gamma-now" not in first
    sent = client.post(url, json={"content": "gamma-now"})
    assert sent.status_code == 200, sent.text
    blobs = [json.dumps(batch) for batch in seen]
    gamma_at = next(index for index, blob in enumerate(blobs) if "gamma-now" in blob)
    assert blobs.index(first) < gamma_at
    saved = store.get_chat(bot["id"], chat["id"])
    by_text = {item.get("content"): item for item in saved["messages"] if item.get("role") == "user"}
    assert by_text["alpha-queued"]["chip"] == "answered"
    assert by_text["beta-queued"]["chip"] == "answered"
    client.close()


def test_the_same_client_id_is_stored_once(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    first = client.post(url, json={"content": "once", "client_id": "client-key-1"})
    second = client.post(url, json={"content": "once", "client_id": "client-key-1"})
    assert first.status_code == 200
    assert second.status_code == 200
    saved = store.get_chat(bot["id"], chat["id"])
    copies = [item for item in saved["messages"] if item.get("client_id") == "client-key-1"]
    assert len(copies) == 1
    assert copies[0]["chip"] == "queued"
    client.close()


def test_interrupt_restarts_the_model_call_with_the_new_message(tmp_path, monkeypatch):
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def complete(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            slot = turn_mod.current_slot()
            assert slot is not None
            slot.client = httpx.AsyncClient()
            started.set()
            await release.wait()
            if turn_mod.cancelled():
                raise turn_mod.TurnCancelled()
            return "late"
        assert "ping" in json.dumps(kwargs.get("messages"))
        return "pong"

    monkeypatch.setattr("easyagent.llm.complete", complete)

    async def scenario():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            endpoint = (await client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"})).json()
            bot = (await client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]})).json()
            await client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
            chat = (await client.post(f"/api/bots/{bot['id']}/chats")).json()
            url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"

            async def first():
                response = await client.post(url, json={"content": "hello"})
                return response.status_code, response.json()

            task = asyncio.create_task(first())
            await asyncio.wait_for(started.wait(), timeout=5)
            second = await client.post(url, json={"content": "ping"})
            assert second.status_code == 200
            slot = turn_mod.slot_for(app.state.store, chat["id"])
            assert slot.user_stop is False
            release.set()
            status, body = await asyncio.wait_for(task, timeout=5)
        assert status == 200
        assert calls["n"] >= 2
        saved = body.get("chat") or body
        texts = [item.get("content") for item in saved.get("messages") or []]
        assert "pong" in texts
        assert "ping" in texts

    asyncio.run(scenario())


def test_queue_mode_does_not_abort_the_model_call(tmp_path, monkeypatch):
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def complete(**_kwargs):
        calls["n"] += 1
        slot = turn_mod.current_slot()
        assert slot is not None
        slot.client = httpx.AsyncClient()
        started.set()
        await release.wait()
        return "finished"

    monkeypatch.setattr("easyagent.llm.complete", complete)

    async def scenario():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            endpoint = (await client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"})).json()
            bot = (await client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]})).json()
            await client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False, "busy_input": "queue"})
            chat = (await client.post(f"/api/bots/{bot['id']}/chats")).json()
            url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"

            async def first():
                return await client.post(url, json={"content": "hello"})

            task = asyncio.create_task(first())
            await asyncio.wait_for(started.wait(), timeout=5)
            second = await client.post(url, json={"content": "later"})
            assert second.status_code == 200
            assert "Working" in second.json()["reply"]
            slot = turn_mod.slot_for(app.state.store, chat["id"])
            assert slot.cancel.is_set() is False
            assert calls["n"] == 1
            release.set()
            response = await asyncio.wait_for(task, timeout=5)
        assert response.status_code == 200

    asyncio.run(scenario())


def test_a_running_command_is_not_killed_when_a_message_arrives(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    slot = turn_mod.slot_for(store, chat["id"])

    class Proc:
        def poll(self):
            return None

    slot.proc = Proc()
    slot.client = object()
    assert turn_mod.redirect_model(store, chat["id"]) == "hold"
    assert slot.cancel.is_set() is False
    assert slot.user_stop is False
    slot.proc = None
    assert turn_mod.redirect_model(store, chat["id"]) == "redirect"
    assert slot.user_stop is False
    assert slot.redirect is True
    client.close()


def test_slash_commands_set_the_busy_mode(tmp_path, monkeypatch):
    async def complete(**_kwargs):
        raise AssertionError("a mode command must not call the model")

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app, client, bot, chat = _client(tmp_path)
    _quiet(client, bot["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    queued = client.post(url, json={"content": "/queue"})
    assert queued.status_code == 200, queued.text
    assert client.get(f"/api/bots/{bot['id']}").json()["busy_input"] == "queue"
    steered = client.post(url, json={"content": "/steer"})
    assert steered.status_code == 200
    assert client.get(f"/api/bots/{bot['id']}").json()["busy_input"] == "steer"
    interrupted = client.post(url, json={"content": "/interrupt"})
    assert interrupted.status_code == 200
    assert client.get(f"/api/bots/{bot['id']}").json()["busy_input"] == "interrupt"
    client.close()


def test_a_finished_owner_is_not_busy_and_resume_drops_a_stale_stop(tmp_path, monkeypatch):
    app, client, bot, chat = _client(tmp_path)
    _quiet(client, bot["id"])
    store = app.state.store

    async def check():
        async def work():
            return None

        task = asyncio.create_task(work())
        await task
        slot = turn_mod.slot_for(store, chat["id"])
        slot.busy = True
        slot.worker = task
        assert turn_mod.is_busy(store, chat["id"]) is False
        assert slot.busy is False

    asyncio.run(check())
    slot = turn_mod.slot_for(store, chat["id"])
    slot.user_stop = True
    slot.busy = False
    slot.worker = None
    doc = store.get_chat(bot["id"], chat["id"])
    doc["run"] = {"id": "run-old", "status": "paused", "current_step": "", "reason": ""}
    doc["messages"].append({
        "id": "offer",
        "role": "assistant",
        "kind": "resume-offer",
        "content": "This job stopped. Resume it, or dismiss it.",
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    store.save_chat(doc)

    async def complete(**_kwargs):
        assert turn_mod.cancelled() is False
        return "Back on the checklist."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    resumed = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    saved = body.get("messages") or body.get("chat", {}).get("messages") or []
    assert any(item.get("content") == "Back on the checklist." for item in saved)
    assert turn_mod.slot_for(store, chat["id"]).user_stop is False
    client.close()


def test_boot_drains_an_acknowledged_message(tmp_path, monkeypatch):
    async def complete(**_kwargs):
        return "Picked up after restart."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app, client, bot, chat = _client(tmp_path)
    _quiet(client, bot["id"])
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    posted = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "remember this"},
    )
    assert posted.status_code == 200
    client.close()
    slot = turn_mod.slot_for(store, chat["id"])
    slot.busy = False
    slot.worker = None
    slot.user_stop = False
    app2 = create_app(tmp_path)
    with TestClient(app2) as client2:
        saved = None
        for _ in range(40):
            saved = client2.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
            if any(item.get("content") == "Picked up after restart." for item in saved["messages"]):
                break
        assert any(item.get("content") == "Picked up after restart." for item in saved["messages"])
        remembered = next(item for item in saved["messages"] if item.get("content") == "remember this")
        assert remembered["chip"] == "answered"


def test_kick_drains_a_queue_the_cap_left_behind(tmp_path, monkeypatch):
    async def complete(**_kwargs):
        return "From the supervisor."

    monkeypatch.setattr("easyagent.llm.complete", complete)

    async def scenario():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            endpoint = (await client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"})).json()
            bot = (await client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]})).json()
            await client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False, "busy_input": "follow"})
            chat = (await client.post(f"/api/bots/{bot['id']}/chats")).json()
            store = app.state.store
            enqueue(store, bot["id"], chat["id"], "stranded")
            slot = turn_mod.slot_for(store, chat["id"])
            slot.busy = False
            slot.worker = None
            slot.user_stop = False
            slot.drain_chains = 99
            assert await kick(store) == 1
            worker = turn_mod.current_worker(store, chat["id"])
            assert worker is not None
            await worker
            saved = store.get_chat(bot["id"], chat["id"])
            row = next(item for item in saved["messages"] if item.get("content") == "stranded")
            assert row["chip"] == "answered"
            assert queued_items(store, bot["id"], chat["id"]) == []

    asyncio.run(scenario())


def test_kick_answers_the_line_an_error_left_queued(tmp_path, monkeypatch):
    async def complete(**_kwargs):
        return "Recovered."

    monkeypatch.setattr("easyagent.llm.complete", complete)

    async def scenario():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            endpoint = (await client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"})).json()
            bot = (await client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]})).json()
            await client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False, "busy_input": "follow"})
            chat = (await client.post(f"/api/bots/{bot['id']}/chats")).json()
            store = app.state.store
            _running(store, bot["id"], chat["id"])
            enqueue(store, bot["id"], chat["id"], "one")
            enqueue(store, bot["id"], chat["id"], "two")
            saved = store.get_chat(bot["id"], chat["id"])
            for item in saved["messages"]:
                if item.get("content") == "one":
                    item["chip"] = "seen"
            saved["messages"].append({
                "id": "err",
                "role": "assistant",
                "content": "Stopped: 500 from the model",
                "error": True,
                "created_at": "2026-01-01T00:00:00+00:00",
            })
            saved["run"]["status"] = "error"
            from easyagent.plan import drop_queued_ids

            drop_queued_ids(store, bot["id"], chat["id"], mark_held(saved))
            store.save_chat(saved)
            slot = turn_mod.slot_for(store, chat["id"])
            slot.busy = False
            slot.worker = None
            slot.user_stop = False
            slot.drain_chains = 99
            one = next(item for item in store.get_chat(bot["id"], chat["id"])["messages"] if item.get("content") == "one")
            assert one["chip"] == "held"
            assert "Retry" in one.get("refuse_reason", "")
            assert [item["content"] for item in queued_items(store, bot["id"], chat["id"])] == ["two"]
            assert await kick(store) == 1
            worker = turn_mod.current_worker(store, chat["id"])
            assert worker is not None
            await worker
            saved = store.get_chat(bot["id"], chat["id"])
            assert next(item for item in saved["messages"] if item.get("content") == "two")["chip"] == "answered"
            assert next(item for item in saved["messages"] if item.get("content") == "one")["chip"] == "held"

    asyncio.run(scenario())


def test_a_burst_keeps_every_message_visible(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    total = 390
    for number in range(total):
        response = client.post(url, json={"content": f"burst-{number}"})
        assert response.status_code == 200, response.text
    saved = store.get_chat(bot["id"], chat["id"])
    rows = [item for item in saved["messages"] if str(item.get("content") or "").startswith("burst-")]
    assert len(rows) == total
    assert {item["content"] for item in rows} == {f"burst-{number}" for number in range(total)}
    assert all(item.get("chip") in {"queued", "held"} for item in rows)
    assert sum(1 for item in rows if item.get("chip") == "queued") == QUEUE_CAP
    held = [item for item in rows if item.get("chip") == "held"]
    assert held
    assert all(item.get("refuse_reason") for item in held)
    assert "Retry" in held[0]["refuse_reason"]
    sent = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/{held[0]['id']}/send-now")
    assert sent.status_code == 200, sent.text
    again = next(item for item in sent.json()["messages"] if item["id"] == held[0]["id"])
    assert again["chip"] == "queued"
    client.close()


def test_past_the_waiting_cap_returns_429_and_writes_nothing(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    for number in range(WAITING_CAP):
        outcome = enqueue(store, bot["id"], chat["id"], f"wait-{number}")
        assert outcome.get("refused") is not True
    before = [item.get("id") for item in store.get_chat(bot["id"], chat["id"])["messages"]]
    extra = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "not-stored"},
    )
    assert extra.status_code == 429, extra.text
    after = store.get_chat(bot["id"], chat["id"])["messages"]
    assert [item.get("id") for item in after] == before
    assert all(item.get("content") != "not-stored" for item in after)
    assert any(
        item.get("kind") == "status" and item.get("content") == "The waiting list is full. That message was not added."
        for item in after
    )
    assert len(queued_items(store, bot["id"], chat["id"])) == QUEUE_CAP
    client.close()
