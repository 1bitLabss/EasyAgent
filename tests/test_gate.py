"""One connection serves a limited number of chats at once.

With max_parallel 1, the second chat waits and then runs. With 2, both run
together. Stop while waiting leaves the line. A server that hangs up is retried
for the retry window.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from easyagent import gate
from easyagent import llm
from easyagent.app import create_app
from easyagent import turn as turn_mod


def _user_text(messages) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


def _who(messages) -> str:
    text = _user_text(messages)
    if "PING-A" in text:
        return "A"
    if "PING-B" in text:
        return "B"
    return "?"


async def _pair(tmp_path, *, parallel: int):
    app = create_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    endpoint = (
        await client.post(
            "/api/endpoints",
            json={
                "name": "shared",
                "base_url": "http://127.0.0.1:9/v1",
                "max_parallel": parallel,
            },
        )
    ).json()
    bot_a = (await client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]})).json()
    bot_b = (await client.post("/api/bots", json={"name": "Bea", "endpoint_id": endpoint["id"]})).json()
    chat_a = (await client.post(f"/api/bots/{bot_a['id']}/chats")).json()
    chat_b = (await client.post(f"/api/bots/{bot_b['id']}/chats")).json()
    return client, endpoint, bot_a, bot_b, chat_a, chat_b


def test_a_new_connection_allows_one_at_a_time_and_the_setting_can_change(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        created = client.post(
            "/api/endpoints",
            json={"name": "shared", "base_url": "http://127.0.0.1:9/v1"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["max_parallel"] == 1
        stored = json.loads((tmp_path / "endpoints.json").read_text())
        assert stored[0]["max_parallel"] == 1

        changed = client.patch(f"/api/endpoints/{body['id']}", json={"max_parallel": 2})
        assert changed.status_code == 200, changed.text
        assert changed.json()["max_parallel"] == 2
        assert json.loads((tmp_path / "endpoints.json").read_text())[0]["max_parallel"] == 2

        rejected = client.patch(f"/api/endpoints/{body['id']}", json={"max_parallel": 0})
        assert rejected.status_code == 400
        assert json.loads((tmp_path / "endpoints.json").read_text())[0]["max_parallel"] == 2

        legacy = {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "name": "old",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "",
            "model": None,
            "created_at": "2026-01-01T00:00:00Z",
        }
        before = json.dumps([legacy])
        (tmp_path / "endpoints.json").write_text(before)
        listed = client.get("/api/endpoints")
        assert listed.status_code == 200
        assert listed.json()[0]["max_parallel"] == 1
        assert (tmp_path / "endpoints.json").read_text() == before

        page = client.get("/")
        assert "app.js?v=37" in page.text
        script = client.get("/static/app.js?v=37").text
    assert 'id="endpoint-parallel"' in page.text
    assert "max_parallel" in script
    assert 'startsWith("Queued:")' in script
    assert "1 at a time" in script
    assert "formatElapsed" in script.split("function describeRun")[1].split("function currentRun")[0]


def test_two_bots_on_one_slot_finish_in_order(tmp_path, monkeypatch):
    gate.reset_lanes()
    order = []
    entered = {"A": asyncio.Event(), "B": asyncio.Event()}
    release = asyncio.Event()

    async def reply(**kwargs):
        who = _who(kwargs.get("messages"))
        order.append(f"{who}-start")
        entered[who].set()
        if who == "A":
            await release.wait()
        if turn_mod.cancelled():
            return
        order.append(f"{who}-end")
        yield f"Answer {who}"

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, endpoint, bot_a, bot_b, chat_a, chat_b = await _pair(tmp_path, parallel=1)
        assert endpoint["max_parallel"] == 1
        headers = {"Accept": "text/event-stream"}
        url_a = f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages"
        url_b = f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}/messages"

        async def collect(url, content):
            async with client.stream("POST", url, json={"content": content}, headers=headers) as response:
                body = (await response.aread()).decode()
                return response.status_code, body

        try:
            first = asyncio.create_task(collect(url_a, "PING-A"))
            await asyncio.wait_for(entered["A"].wait(), timeout=5)
            second = asyncio.create_task(collect(url_b, "PING-B"))
            step = ""
            chat_b_mid = None
            for _ in range(100):
                if entered["B"].is_set():
                    pytest.fail(f"Bea reached the server while Ada still held it: {order}")
                chat_b_mid = (await client.get(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}")).json()
                step = (chat_b_mid.get("run") or {}).get("current_step") or ""
                if step.startswith("Queued:"):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail(f"Bea was not queued: {step}")
            assert step == "Queued: waiting for shared (busy with Ada)"
            assert chat_b_mid["run"]["status"] == "running"
            assert chat_b_mid["run"]["started_at"]
            lane = gate._lane(endpoint["id"])
            assert [seat.bot_name for seat in lane.queue] == ["Bea"]
            release.set()
            status_a, body_a = await asyncio.wait_for(first, timeout=5)
            status_b, body_b = await asyncio.wait_for(second, timeout=5)
            stored_a = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
            stored_b = (await client.get(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}")).json()
        finally:
            release.set()
            await client.aclose()
        assert status_a == 200 and status_b == 200
        assert order == ["A-start", "A-end", "B-start", "B-end"]
        assert "Queued: waiting for shared (busy with Ada)" in body_b
        assert stored_a["messages"][-1]["content"] == "Answer A"
        assert stored_b["messages"][-1]["content"] == "Answer B"
        assert "Answer B" not in stored_a["messages"][-1]["content"]
        assert "Answer A" not in stored_b["messages"][-1]["content"]

    asyncio.run(scenario())


def test_two_bots_run_together_when_the_connection_allows_two(tmp_path, monkeypatch):
    gate.reset_lanes()
    order = []
    entered = {"A": asyncio.Event(), "B": asyncio.Event()}
    both = asyncio.Event()

    async def reply(**kwargs):
        who = _who(kwargs.get("messages"))
        order.append(f"{who}-start")
        entered[who].set()
        if entered["A"].is_set() and entered["B"].is_set():
            both.set()
        await both.wait()
        if turn_mod.cancelled():
            return
        order.append(f"{who}-end")
        yield f"Answer {who}"

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, endpoint, bot_a, bot_b, chat_a, chat_b = await _pair(tmp_path, parallel=2)
        assert endpoint["max_parallel"] == 2
        headers = {"Accept": "text/event-stream"}

        async def collect(url, content):
            async with client.stream("POST", url, json={"content": content}, headers=headers) as response:
                return response.status_code, (await response.aread()).decode()

        try:
            first = asyncio.create_task(
                collect(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages", "PING-A")
            )
            second = asyncio.create_task(
                collect(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}/messages", "PING-B")
            )
            await asyncio.wait_for(both.wait(), timeout=5)
            assert not order[0].endswith("-end") and not order[1].endswith("-end")
            status_a, body_a = await asyncio.wait_for(first, timeout=5)
            status_b, body_b = await asyncio.wait_for(second, timeout=5)
            stored_a = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
            stored_b = (await client.get(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}")).json()
        finally:
            both.set()
            await client.aclose()
        assert status_a == 200 and status_b == 200
        assert {item for item in order if item.endswith("-start")} == {"A-start", "B-start"}
        assert order.index("A-end") > order.index("B-start")
        assert order.index("B-end") > order.index("A-start")
        assert "Queued:" not in body_a and "Queued:" not in body_b
        assert stored_a["messages"][-1]["content"] == "Answer A"
        assert stored_b["messages"][-1]["content"] == "Answer B"

    asyncio.run(scenario())


def test_stop_while_queued_leaves_the_line(tmp_path, monkeypatch):
    gate.reset_lanes()
    entered = {"A": asyncio.Event(), "B": asyncio.Event()}
    release = asyncio.Event()

    async def reply(**kwargs):
        who = _who(kwargs.get("messages"))
        entered[who].set()
        if who == "A":
            await release.wait()
        if turn_mod.cancelled():
            return
        yield f"Answer {who}"

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, endpoint, bot_a, bot_b, chat_a, chat_b = await _pair(tmp_path, parallel=1)
        headers = {"Accept": "text/event-stream"}

        async def collect(url, content):
            async with client.stream("POST", url, json={"content": content}, headers=headers) as response:
                return response.status_code, (await response.aread()).decode()

        try:
            first = asyncio.create_task(
                collect(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages", "PING-A")
            )
            await asyncio.wait_for(entered["A"].wait(), timeout=5)
            second = asyncio.create_task(
                collect(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}/messages", "PING-B")
            )
            for _ in range(100):
                chat = (await client.get(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}")).json()
                if (chat.get("run") or {}).get("current_step", "").startswith("Queued:"):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Bea never reached the queue")
            assert not entered["B"].is_set()
            stopped = await client.post(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}/stop")
            assert stopped.status_code == 200, stopped.text
            lane = gate._lane(endpoint["id"])
            assert all(seat.bot_name != "Bea" for seat in lane.queue)
            assert all(seat.bot_name != "Bea" for seat in lane.holders)
            await asyncio.wait_for(second, timeout=5)
            stored_b = (await client.get(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}")).json()
            assert stored_b["messages"][-1]["content"] == "Stopped: you pressed Stop"
            assert stored_b["run"]["status"] == "stopped"
            release.set()
            status_a, body_a = await asyncio.wait_for(first, timeout=5)
            stored_a = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
        finally:
            release.set()
            await client.aclose()
        assert status_a == 200
        assert "Answer A" in body_a
        assert stored_a["messages"][-1]["content"] == "Answer A"
        assert not entered["B"].is_set()

    asyncio.run(scenario())


def test_a_dropped_connection_is_retried_then_says_stopped(tmp_path, monkeypatch):
    gate.reset_lanes()
    calls = []

    async def reply(**kwargs):
        calls.append(_who(kwargs.get("messages")))
        raise llm.ProviderError(
            "Could not reach http://127.0.0.1:9/v1: Server disconnected without sending a response."
        )
        yield ""

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, _endpoint, bot_a, _bot_b, chat_a, _chat_b = await _pair(tmp_path, parallel=1)
        try:
            sent = await client.post(
                f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages",
                json={"content": "PING-A"},
                headers={"Accept": "text/event-stream"},
            )
            body = sent.text
            stored = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
        finally:
            await client.aclose()
        assert sent.status_code == 200, sent.text
        assert len(calls) > 2
        assert calls == ["A"] * len(calls)
        assert "Model not answering, retrying (attempt 2)" in body
        text = stored["messages"][-1]["content"]
        assert text.startswith("Stopped:")
        assert "Server disconnected without sending a response" in text
        assert "Retried for" in text
        assert stored["messages"][-1]["error"] is True

    asyncio.run(scenario())


def test_a_dropped_connection_recovers_on_the_second_try(tmp_path, monkeypatch):
    gate.reset_lanes()
    calls = {"n": 0}

    async def reply(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise llm.ProviderError(
                "Could not reach http://127.0.0.1:9/v1: Connection reset by peer"
            )
        yield "back"

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, _endpoint, bot_a, _bot_b, chat_a, _chat_b = await _pair(tmp_path, parallel=1)
        try:
            sent = await client.post(
                f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages",
                json={"content": "PING-A"},
                headers={"Accept": "text/event-stream"},
            )
            stored = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
        finally:
            await client.aclose()
        assert sent.status_code == 200, sent.text
        assert calls["n"] == 2
        assert stored["messages"][-1]["content"] == "back"

    asyncio.run(scenario())


class _Lines:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    async def aiter_lines(self):
        payload = json.dumps({"choices": [{"delta": {"content": "pong"}}]})
        yield f"data: {payload}"
        yield "data: [DONE]"


class _Hangup:
    def __init__(self, failures, message):
        self.failures = failures
        self.message = message
        self.calls = 0

    def stream(self, *args, **kwargs):
        self.calls += 1
        client = self

        class _CM:
            async def __aenter__(self):
                if client.calls <= client.failures:
                    raise httpx.RemoteProtocolError(client.message)
                return _Lines()

            async def __aexit__(self, *exc):
                return False

        return _CM()

    async def aclose(self):
        return None


def test_stream_complete_retries_a_hangup_for_the_window(monkeypatch):
    gate.reset_lanes()
    hangup = _Hangup(1, "Server disconnected without sending a response.")
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *args, **kwargs: hangup)

    async def run():
        parts = []
        async for piece in llm.stream_complete(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model=None,
            messages=[{"role": "user", "content": "hi"}],
        ):
            parts.append(piece)
        return "".join(parts)

    assert asyncio.run(run()) == "pong"
    assert hangup.calls == 2

    again = _Hangup(1000, "Connection reset by peer")
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *args, **kwargs: again)

    async def fail():
        async for _piece in llm.stream_complete(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model=None,
            messages=[{"role": "user", "content": "hi"}],
        ):
            pass

    with pytest.raises(llm.ProviderError) as caught:
        asyncio.run(fail())
    assert again.calls > 2
    assert "Connection reset by peer" in str(caught.value)
    assert "Retried for" in str(caught.value)


def test_stream_complete_does_not_retry_when_the_caller_already_will(monkeypatch):
    gate.reset_lanes()
    hangup = _Hangup(1, "Server disconnected without sending a response.")
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *args, **kwargs: hangup)

    async def run():
        token = gate.suppress_inner_retry()
        try:
            async for _piece in llm.stream_complete(
                base_url="http://127.0.0.1:9/v1",
                api_key=None,
                model=None,
                messages=[{"role": "user", "content": "hi"}],
            ):
                pass
        finally:
            gate.restore_inner_retry(token)

    with pytest.raises(llm.ProviderError):
        asyncio.run(run())
    assert hangup.calls == 1
