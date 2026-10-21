"""A runaway reply stops, a queued line stays Seen, Resume survives a restart, and a routine can post out."""

from __future__ import annotations

import asyncio

import pytest

from easyagent.app import _claim_for_resume, _pause_for_resume, _pause_for_runaway
from easyagent.bridges import add_bridge
from easyagent.generation import (
    RUNAWAY_TEXT,
    GenerationLimit,
    GenerationWatch,
    Limits,
    limits_from_endpoint,
)
from easyagent.llm import _completion_payload
from easyagent.plan import mark_answered, queued_items, steer_into
from easyagent.schedule import ScheduleError, _deliver, compile_routine
from easyagent.store import now_iso
from easyagent import turn as turn_mod
from tests.test_plan_0320 import _client, _running


def _clock(box: dict):
    return lambda: box["t"]


def test_watch_stops_for_length_silence_wall_and_a_loop():
    box = {"t": 0.0}
    short = Limits(max_tokens=4, stall_seconds=10, wall_seconds=30, hard_seconds=50)
    watch = GenerationWatch(short, clock=_clock(box))
    watch.observe("abcd")
    watch.observe("efgh")
    watch.observe("ijkl")
    with pytest.raises(GenerationLimit) as limited:
        watch.observe("mnop")
    assert str(limited.value) == RUNAWAY_TEXT
    assert limited.value.reason == "max_tokens"

    box["t"] = 0.0
    silent = GenerationWatch(Limits(max_tokens=100, stall_seconds=10, wall_seconds=100, hard_seconds=200), clock=_clock(box))
    silent.observe("hello")
    box["t"] = 10
    with pytest.raises(GenerationLimit) as stalled:
        silent.poll()
    assert stalled.value.reason == "stall"

    box["t"] = 0.0
    moving = GenerationWatch(Limits(max_tokens=1000, stall_seconds=10, wall_seconds=30, hard_seconds=50), clock=_clock(box))
    moving.observe("one")
    box["t"] = 40
    moving.observe("two")
    box["t"] = 50
    with pytest.raises(GenerationLimit) as hard:
        moving.poll()
    assert hard.value.reason == "hard"

    box["t"] = 0.0
    loop = GenerationWatch(Limits(max_tokens=1000, stall_seconds=10, wall_seconds=30, hard_seconds=50), clock=_clock(box))
    with pytest.raises(GenerationLimit) as repeated:
        for _ in range(6):
            loop.observe(" same")
    assert repeated.value.reason == "repeat"

    phrase = "x" * 48
    box["t"] = 0.0
    tail = GenerationWatch(Limits(max_tokens=5000, stall_seconds=30, wall_seconds=100, hard_seconds=200), clock=_clock(box))
    with pytest.raises(GenerationLimit) as looped:
        for _ in range(3):
            tail.observe(phrase)
    assert looped.value.reason == "repeat"


def test_connection_length_is_sent_and_defaults(tmp_path):
    assert limits_from_endpoint(None).max_tokens == 4096
    assert limits_from_endpoint({"max_tokens": 1024}).max_tokens == 1024
    payload = _completion_payload([{"role": "user", "content": "hi"}], "stub", stream=True, max_tokens=1024)
    assert payload["max_tokens"] == 1024
    app, client, bot, _chat = _client(tmp_path)
    endpoint_id = bot["endpoint_id"]
    saved = client.patch(f"/api/endpoints/{endpoint_id}", json={"max_tokens": 2048}).json()
    assert saved["max_tokens"] == 2048
    again = client.get("/api/endpoints").json()
    assert again[0]["max_tokens"] == 2048
    client.close()
    assert app.state.store.get_endpoint(endpoint_id)["max_tokens"] == 2048


def test_runaway_stops_the_request_and_keeps_the_queue(tmp_path, monkeypatch):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    assert client.post(url, json={"content": "hold this"}).status_code == 200
    turn_mod.clear_busy(store, chat["id"])

    async def explode(**_kwargs):
        raise GenerationLimit("repeat")

    monkeypatch.setattr("easyagent.app.complete_with_tools", explode)
    sent = client.post(url, json={"content": "go on"})
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert RUNAWAY_TEXT in (body.get("reply") or "")
    saved = store.get_chat(bot["id"], chat["id"])
    assert saved["run"]["status"] == "paused"
    assert any(item.get("kind") == "resume-offer" and item.get("content") == RUNAWAY_TEXT for item in saved["messages"])
    held = next(item for item in saved["messages"] if item.get("content") == "hold this")
    assert held["chip"] != "answered"
    assert "hold this" in [item["content"] for item in queued_items(store, bot["id"], chat["id"])]
    client.close()


def test_stop_shows_seen_until_a_real_reply(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"].append({
        "id": "live-old",
        "role": "assistant",
        "content": "still writing the earlier answer",
        "live": True,
        "created_at": now_iso(),
    })
    store.save_chat(doc)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    assert client.post(url, json={"content": "and this too"}).status_code == 200
    assert steer_into(store, bot["id"], chat["id"], []) == 1
    stored = store.get_chat(bot["id"], chat["id"])
    assert mark_answered(stored) is False
    user = next(item for item in stored["messages"] if item.get("content") == "and this too")
    assert user["chip"] == "seen"
    user["chip"] = "answered"
    store.save_chat(stored)
    stopped = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/stop")
    assert stopped.status_code == 200
    after = next(item for item in stopped.json()["messages"] if item.get("content") == "and this too")
    assert after["chip"] == "seen"
    saved = store.get_chat(bot["id"], chat["id"])
    saved["messages"].append({
        "id": "reply-new",
        "role": "assistant",
        "content": "Here is the answer.",
        "created_at": now_iso(),
    })
    assert mark_answered(saved) is True
    assert next(item for item in saved["messages"] if item.get("content") == "and this too")["chip"] == "answered"
    client.close()


def test_resume_clears_a_stale_running_flag(tmp_path, monkeypatch):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    doc = store.get_chat(bot["id"], chat["id"])
    doc["run"] = {
        "id": "run-stale",
        "status": "running",
        "started_at": now_iso(),
        "last_activity_at": now_iso(),
        "current_step": "Reading notes",
        "reason": "",
    }
    doc["messages"].append({
        "id": "offer-1",
        "role": "assistant",
        "kind": "resume-offer",
        "content": "This job stopped during Reading notes. Resume it, or dismiss it.",
        "created_at": now_iso(),
    })
    store.save_chat(doc)
    slot = turn_mod.slot_for(store, chat["id"])
    slot.busy = True
    slot.worker = None

    async def reply(**_kwargs):
        return "Continuing."

    monkeypatch.setattr("easyagent.llm.complete", reply)
    resumed = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.status_code != 409
    client.close()


def test_claim_resume_does_not_steal_a_live_worker(tmp_path):
    app, client, _bot, chat = _client(tmp_path)
    store = app.state.store

    async def go():
        slot = turn_mod.slot_for(store, chat["id"])

        async def linger():
            await asyncio.sleep(30)

        task = asyncio.create_task(linger())
        slot.busy = True
        slot.worker = task
        try:
            assert await _claim_for_resume(store, chat["id"]) == "owned"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(go())
    client.close()


def test_pickup_card_is_the_default_after_restart(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["run"] = {"id": "run-boot", "status": "running", "started_at": now_iso(), "last_activity_at": now_iso(), "current_step": "Reading notes", "reason": ""}
    store.save_chat(doc)
    _pause_for_resume(store, bot["id"], chat["id"])
    paused = store.get_chat(bot["id"], chat["id"])
    assert paused["run"]["status"] == "paused"
    assert any(item.get("content") == "Picked up where it stopped." for item in paused["messages"])
    off = client.patch(f"/api/bots/{bot['id']}", json={"auto_resume": False}).json()
    assert off["auto_resume"] is False
    client.close()


def test_routine_posts_only_to_an_allowlisted_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    app, client, bot, _chat = _client(tmp_path)
    store = app.state.store
    add_bridge(store, kind="telegram", name="Phone", bot_id=bot["id"], allow_ids=["42"], token="token-value")
    calls = []

    async def send(token, chat_id, text, keyboard_rows=None):
        calls.append((token, chat_id, text))
        if len(calls) == 1:
            raise RuntimeError("telegram down")

    monkeypatch.setattr("easyagent.bridges.telegram_send", send)
    record = compile_routine({
        "name": "Brief",
        "prompt": "Say good morning.",
        "daily": "8:00 AM",
        "deliver": "telegram",
        "deliver_chat": "42",
    })
    entry = {"status": "ok", "output": "Good morning.", "name": "Brief"}
    asyncio.run(_deliver(store, bot, record, entry))
    assert [item[1] for item in calls] == ["42", "42"]
    assert calls[0][0] == "token-value"
    texts = [item.get("content") for item in store.get_chat(bot["id"], store.ongoing_chat(bot["id"])["id"])["messages"]]
    assert "Good morning." in texts
    assert not any("could not post" in str(item) for item in texts)

    calls.clear()
    refused = compile_routine({
        "name": "Elsewhere",
        "prompt": "Say hello.",
        "daily": "9:00 AM",
        "deliver": "telegram",
        "deliver_chat": "999",
    })
    asyncio.run(_deliver(store, bot, refused, {"status": "ok", "output": "Hello.", "name": "Elsewhere"}))
    assert calls == []
    texts = [item.get("content") for item in store.get_chat(bot["id"], store.ongoing_chat(bot["id"])["id"])["messages"]]
    assert any("not on the allow list" in str(item) for item in texts)

    async def always_down(token, chat_id, text, keyboard_rows=None):
        calls.append(chat_id)
        raise RuntimeError("still down")

    monkeypatch.setattr("easyagent.bridges.telegram_send", always_down)
    asyncio.run(_deliver(store, bot, record, {"status": "ok", "output": "Again.", "name": "Brief"}))
    assert calls == ["42", "42"]
    texts = [item.get("content") for item in store.get_chat(bot["id"], store.ongoing_chat(bot["id"])["id"])["messages"]]
    assert any("could not post to Telegram" in str(item) for item in texts)
    with pytest.raises(ScheduleError):
        compile_routine({"name": "Bad", "prompt": "x", "daily": "8:00 AM", "deliver": "email", "deliver_chat": "42"})
    client.close()


def test_stall_guard_keeps_native_tool_call_ids(monkeypatch):
    """The stall cap reads the body in this task, on 3.11 and 3.12.

    wait_for on 3.11 runs the read in another task and drops the ContextVar,
    so the next request invents call_0. The id from the stream has to survive.
    """
    import easyagent.llm as llm
    from easyagent.generation import Limits, _limits

    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_pwd","type":"function","function":{"name":"terminal","arguments":""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"command\\": \\"pwd\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            for line in lines:
                yield line

        async def aread(self):
            return b""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            return None

        def stream(self, method, url, json=None, headers=None):
            class Context:
                async def __aenter__(self_inner):
                    return FakeResponse()

                async def __aexit__(self_inner, *args):
                    return False

            return Context()

    monkeypatch.setattr(llm.httpx, "AsyncClient", FakeClient)

    async def read():
        pieces = []
        async for piece in llm._stream_attempt(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="stub",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        ):
            pieces.append(piece)
        return pieces, llm.take_native_calls()

    pieces, calls = asyncio.run(read())
    assert calls and calls[0]["id"] == "call_pwd"
    assert calls[0]["name"] == "terminal"
    assert "pwd" in calls[0]["arguments"]
    assert pieces

    class SilentResponse(FakeResponse):
        async def aiter_lines(self):
            await asyncio.sleep(30)
            yield "data: [DONE]"

    class SilentClient(FakeClient):
        def stream(self, method, url, json=None, headers=None):
            class Context:
                async def __aenter__(self_inner):
                    return SilentResponse()

                async def __aexit__(self_inner, *args):
                    return False

            return Context()

    monkeypatch.setattr(llm.httpx, "AsyncClient", SilentClient)
    token = _limits.set(Limits(
        max_tokens=256,
        stall_seconds=0.05,
        first_token_seconds=0.05,
        wall_seconds=10,
        hard_seconds=20,
    ))

    async def stall():
        async for _piece in llm._stream_attempt(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="stub",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        ):
            pass

    try:
        with pytest.raises(GenerationLimit) as stalled:
            asyncio.run(stall())
        assert stalled.value.reason == "first_token"
    finally:
        _limits.reset(token)


def test_finish_reason_ends_the_read_without_done(monkeypatch):
    """A terminal finish_reason is the end of the completion. [DONE] is not required."""
    import easyagent.llm as llm

    lines = [
        'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}',
        'data: {"choices":[{"delta":{"content":"Section 1 already written."}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    ]
    hung = {"n": 0}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            for line in lines:
                yield line
            hung["n"] += 1
            await asyncio.sleep(30)
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            return None

        def stream(self, method, url, json=None, headers=None):
            class Context:
                async def __aenter__(self_inner):
                    return FakeResponse()

                async def __aexit__(self_inner, *args):
                    return False

            return Context()

    monkeypatch.setattr(llm.httpx, "AsyncClient", FakeClient)

    async def read():
        pieces = []
        async for piece in llm._stream_attempt(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="stub",
            messages=[{"role": "user", "content": "MEDIUM please"}],
            timeout=5,
        ):
            pieces.append(piece)
        return pieces

    assert asyncio.run(read()) == ["Section 1 already written."]
    assert hung["n"] == 0


def _stream_client(lines_fn):
    import easyagent.llm as llm

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def aiter_lines(self):
            return lines_fn()

        async def aread(self):
            return b""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            return None

        def stream(self, method, url, json=None, headers=None):
            class Context:
                async def __aenter__(self_inner):
                    return FakeResponse()

                async def __aexit__(self_inner, *args):
                    return False

            return Context()

    return llm, FakeClient


def test_a_pause_on_the_last_token_keeps_the_finished_reply(monkeypatch):
    """Cancel after the last words is the next turn. The finished reply is not cut."""
    import easyagent.llm as llm

    async def lines():
        yield 'data: {"choices":[{"delta":{"content":"Section 1 already written."}}]}'
        await asyncio.sleep(0.02)
        yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'

    llm_mod, FakeClient = _stream_client(lines)
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

    async def read():
        slot = turn_mod.Slot()
        slot.cancel = asyncio.Event()
        slot.worker = asyncio.current_task()
        turn_mod._cancel.set(slot.cancel)
        turn_mod._slot.set(slot)
        pieces = []
        async for piece in llm._stream_attempt(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="stub",
            messages=[{"role": "user", "content": "MEDIUM please"}],
            timeout=5,
        ):
            pieces.append(piece)
            slot.cancel.set()
            slot.pause_for_user = True
        return pieces

    assert asyncio.run(read()) == ["Section 1 already written."]


def test_a_pause_with_more_tokens_still_stops(monkeypatch):
    """A later token is a real pause. The read does not wait the rest of the reply out."""
    import time

    import easyagent.llm as llm

    async def lines():
        yield 'data: {"choices":[{"delta":{"content":"Hello"}}]}'
        yield 'data: {"choices":[{"delta":{"content":" there"}}]}'
        await asyncio.sleep(0.4)
        yield 'data: {"choices":[{"delta":{"content":" friend"}}]}'
        yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'

    llm_mod, FakeClient = _stream_client(lines)
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

    async def read():
        slot = turn_mod.Slot()
        slot.cancel = asyncio.Event()
        slot.worker = asyncio.current_task()
        turn_mod._cancel.set(slot.cancel)
        turn_mod._slot.set(slot)
        pieces = []
        started = time.perf_counter()
        try:
            async for piece in llm._stream_attempt(
                base_url="http://127.0.0.1:9/v1",
                api_key=None,
                model="stub",
                messages=[{"role": "user", "content": "hi"}],
                timeout=5,
            ):
                pieces.append(piece)
                slot.cancel.set()
                slot.pause_for_user = True
        except turn_mod.TurnCancelled:
            return pieces, time.perf_counter() - started
        return pieces, time.perf_counter() - started

    pieces, elapsed = asyncio.run(read())
    assert pieces == ["Hello", " there"]
    assert elapsed < 0.25


def test_the_last_buffered_tokens_finish_the_reply(monkeypatch):
    """Two tokens already in hand, then the end, are not a cut reply."""
    import easyagent.llm as llm

    async def lines():
        yield 'data: {"choices":[{"delta":{"content":"One"}}]}'
        yield 'data: {"choices":[{"delta":{"content":" two"}}]}'
        yield 'data: {"choices":[{"delta":{"content":" three"}}]}'
        yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'

    llm_mod, FakeClient = _stream_client(lines)
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

    async def read():
        slot = turn_mod.Slot()
        slot.cancel = asyncio.Event()
        slot.worker = asyncio.current_task()
        turn_mod._cancel.set(slot.cancel)
        turn_mod._slot.set(slot)
        pieces = []
        async for piece in llm._stream_attempt(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="stub",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        ):
            pieces.append(piece)
            slot.cancel.set()
            slot.pause_for_user = True
        return pieces

    assert asyncio.run(read()) == ["One", " two", " three"]


def test_buffered_tokens_at_the_end_are_kept_whole(monkeypatch):
    """A pause while the last lines are already buffered does not cut them."""
    import easyagent.llm as llm

    async def lines():
        yield 'data: {"choices":[{"delta":{"content":"A"}}]}'
        for i in range(30):
            yield 'data: {"choices":[{"delta":{"content":"' + f"n{i}-" + '"}}]}'
        yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'

    llm_mod, FakeClient = _stream_client(lines)
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

    async def read():
        slot = turn_mod.Slot()
        slot.cancel = asyncio.Event()
        slot.worker = asyncio.current_task()
        turn_mod._cancel.set(slot.cancel)
        turn_mod._slot.set(slot)
        pieces = []
        async for piece in llm._stream_attempt(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="stub",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        ):
            pieces.append(piece)
            slot.cancel.set()
        return "".join(pieces)

    assert asyncio.run(read()) == "A" + "".join(f"n{i}-" for i in range(30))


def test_a_pause_between_tokens_leaves_the_socket_open():
    """Closing between tokens drops the last line of a reply that already ended."""
    closed = []

    class Client:
        async def aclose(self):
            closed.append("closed")

    slot = turn_mod.Slot()
    slot.client = Client()
    slot.in_model_read = False

    async def run():
        turn_mod.request_user_pause(object(), "chat")
        await asyncio.sleep(0.25)

    import easyagent.turn as turn

    # request_user_pause looks the slot up. This chat is not on disk.
    original = turn.slot_for
    turn.slot_for = lambda store, chat_id: slot
    try:
        asyncio.run(run())
    finally:
        turn.slot_for = original
    assert slot.pause_for_user is True
    assert slot.cancel.is_set()
    assert closed == []


def test_a_pause_during_the_read_closes_the_socket():
    closed = []

    class Client:
        async def aclose(self):
            closed.append("closed")

    slot = turn_mod.Slot()
    slot.client = Client()
    slot.in_model_read = True

    async def run():
        turn_mod.request_user_pause(object(), "chat")
        await asyncio.sleep(0.05)
        assert closed == []
        await asyncio.sleep(0.2)

    import easyagent.turn as turn

    original = turn.slot_for
    turn.slot_for = lambda store, chat_id: slot
    try:
        asyncio.run(run())
    finally:
        turn.slot_for = original
    assert closed == ["closed"]
