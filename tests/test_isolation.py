"""Two bots can stream at once. Leaving a chat does not stop its run.

There is no browser harness. The page routing is checked from app.js:

app.js keeps `streams`, a Map keyed by bot id and chat id. Each entry has its
own AbortController, live text, and run id. applyStreamEvent writes that
entry and paints the thread only when that bot and chat are the open view.
selectBot, openChat, and startNewChat do not abort other entries. beginFlight
aborts only the previous controller for the same chat. stopFlight aborts only
the open chat and POSTs /stop for that chat. A run that is still going when
you come back is read from the chat's run object.
"""

import asyncio
from contextlib import suppress

import httpx
import pytest

from easyagent.app import create_app
from easyagent import turn as turn_mod
from easyagent.store import Store


def _user_text(messages) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


def test_two_runs_do_not_share_cancel_env_or_cwd(tmp_path):
    store = Store(tmp_path)
    store.ensure()

    async def scenario():
        ready = asyncio.Event()
        boxes = {"a": {}, "b": {}}

        async def one(chat_id, box):
            turn_mod.bind(store, chat_id, bot_id="bot")
            box["env"] = turn_mod.tool_env()
            box["cwd"] = turn_mod.tool_cwd()
            box["run"] = turn_mod.current_run_id()
            box["env"]["EASYAGENT_ISOLATION_PROBE"] = box["run"]
            if all(item.get("run") for item in boxes.values()):
                ready.set()
            await ready.wait()
            await asyncio.sleep(0.05)
            box["cancelled"] = turn_mod.cancelled()
            box["reason"] = turn_mod.cancel_reason()

        first = asyncio.create_task(one("chat-a", boxes["a"]))
        second = asyncio.create_task(one("chat-b", boxes["b"]))
        await asyncio.wait_for(ready.wait(), timeout=2)
        turn_mod.interrupt(store, "chat-a", "you pressed Stop")
        await first
        await second
        assert boxes["a"]["env"] is not boxes["b"]["env"]
        assert boxes["a"]["cwd"] == boxes["b"]["cwd"]
        assert boxes["a"]["run"] != boxes["b"]["run"]
        assert boxes["a"]["env"]["EASYAGENT_ISOLATION_PROBE"] == boxes["a"]["run"]
        assert boxes["b"]["env"].get("EASYAGENT_ISOLATION_PROBE") != boxes["a"]["run"]
        assert boxes["a"]["cancelled"] is True
        assert boxes["a"]["reason"] == "you pressed Stop"
        assert boxes["b"]["cancelled"] is False

    asyncio.run(scenario())


def test_the_page_keeps_a_stream_per_chat(tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(create_app(tmp_path)) as client:
        script = client.get("/static/app.js?v=35")
    assert script.status_code == 200
    text = script.text
    assert "const streams = new Map()" in text
    assert "function streamKey" in text
    assert "function applyStreamEvent" in text
    assert "function adoptStream" in text
    assert "you pressed Stop" in text
    assert "/stop" in text
    assert "flightControl" not in text
    begin = text.split("function beginFlight")[1].split("async function streamPost")[0]
    assert "streams.get(key)" in begin
    assert "previous.control.abort()" in begin
    select = text.split("async function selectBot")[1].split("async function refresh")[0]
    assert ".abort(" not in select
    opened = text.split("async function openChat")[1].split("function startNewChat")[0]
    assert ".abort(" not in opened
    new_chat = text.split("function startNewChat")[1].split("function renderAskChoices")[0]
    assert ".abort(" not in new_chat
    stopped = text.split("async function stopFlight")[1].split("function openConfirm")[0]
    assert "/stop" in stopped
    assert "you pressed Stop" in stopped


async def _two_bots(tmp_path, reply):
    app = create_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    endpoint = (
        await client.post(
            "/api/endpoints",
            json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "max_parallel": 2},
        )
    ).json()
    bot_a = (await client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]})).json()
    bot_b = (await client.post("/api/bots", json={"name": "Bea", "endpoint_id": endpoint["id"]})).json()
    chat_a = (await client.post(f"/api/bots/{bot_a['id']}/chats")).json()
    chat_b = (await client.post(f"/api/bots/{bot_b['id']}/chats")).json()
    return client, bot_a, bot_b, chat_a, chat_b


def test_two_bots_stream_into_their_own_chats(tmp_path, monkeypatch):
    started = {"a": asyncio.Event(), "b": asyncio.Event()}
    release = asyncio.Event()

    async def reply(**kwargs):
        text = _user_text(kwargs.get("messages"))
        if "PING-A" in text:
            started["a"].set()
            await release.wait()
            if turn_mod.cancelled():
                return
            yield "Answer A"
            return
        if "PING-B" in text:
            started["b"].set()
            await release.wait()
            if turn_mod.cancelled():
                return
            yield "Answer B"
            return
        yield "other"

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, bot_a, bot_b, chat_a, chat_b = await _two_bots(tmp_path, reply)
        headers = {"Accept": "text/event-stream"}
        url_a = f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages"
        url_b = f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}/messages"

        async def collect(url, content):
            async with client.stream("POST", url, json={"content": content}, headers=headers) as response:
                body = (await response.aread()).decode()
                return response.status_code, body

        try:
            first = asyncio.create_task(collect(url_a, "PING-A"))
            second = asyncio.create_task(collect(url_b, "PING-B"))
            await asyncio.wait_for(started["a"].wait(), timeout=5)
            await asyncio.wait_for(started["b"].wait(), timeout=5)
            mid_a = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
            mid_b = (await client.get(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}")).json()
            assert mid_a["run"]["status"] == "running"
            assert mid_b["run"]["status"] == "running"
            assert mid_a["run"]["id"] != mid_b["run"]["id"]
            release.set()
            status_a, body_a = await asyncio.wait_for(first, timeout=5)
            status_b, body_b = await asyncio.wait_for(second, timeout=5)
            stored_a = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
            stored_b = (await client.get(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}")).json()
        finally:
            await client.aclose()
        assert status_a == 200 and status_b == 200
        assert "Answer A" in body_a and "Answer B" not in body_a
        assert "Answer B" in body_b and "Answer A" not in body_b
        assert f'"bot_id": "{bot_a["id"]}"' in body_a
        assert f'"chat_id": "{chat_a["id"]}"' in body_a
        assert f'"bot_id": "{bot_b["id"]}"' in body_b
        assert stored_a["messages"][-1]["content"] == "Answer A"
        assert stored_b["messages"][-1]["content"] == "Answer B"
        assert stored_a["run"]["status"] == "idle"
        assert stored_b["run"]["status"] == "idle"
        assert all(item.get("content") != "" for item in stored_a["messages"])
        assert all(item.get("content") != "" for item in stored_b["messages"])

    asyncio.run(scenario())


def test_leaving_a_stream_does_not_cancel_it(tmp_path, monkeypatch):
    """Closing the view is not Stop. The run finishes in its own chat."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def reply(**kwargs):
        text = _user_text(kwargs.get("messages"))
        if "PING-A" in text:
            started.set()
            while not release.is_set():
                if turn_mod.cancelled():
                    return
                await asyncio.sleep(0.02)
            if turn_mod.cancelled():
                return
            yield "Answer A"
            return
        yield "Answer B"

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, bot_a, bot_b, chat_a, chat_b = await _two_bots(tmp_path, reply)
        headers = {"Accept": "text/event-stream"}
        url_a = f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages"

        async def watch_then_leave():
            async with client.stream(
                "POST", url_a, json={"content": "PING-A"}, headers=headers
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        return

        try:
            watcher = asyncio.create_task(watch_then_leave())
            await asyncio.wait_for(started.wait(), timeout=5)
            await asyncio.sleep(0.2)
            watcher.cancel()
            with suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(watcher, timeout=1)
            await asyncio.sleep(0.05)
            mid = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
            assert mid["run"]["status"] == "running"
            other = await client.post(
                f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}/messages",
                json={"content": "hello from the other bot"},
                headers=headers,
            )
            assert other.status_code == 200
            body_b = other.text
            still = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
            assert still["run"]["status"] == "running"
            assert still["messages"][-1]["content"] != ""
            release.set()
            for _ in range(50):
                stored = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
                if stored["messages"][-1]["content"] == "Answer A":
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail(f"chat A did not finish: {stored['messages'][-1]}")
            stored_b = (await client.get(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}")).json()
        finally:
            release.set()
            await client.aclose()
        assert "Answer B" in body_b
        assert "Answer A" not in body_b
        assert stored_b["messages"][-1]["content"] == "Answer B"
        assert "Stopped" not in stored["messages"][-1]["content"]

    asyncio.run(scenario())


def test_stop_in_one_chat_leaves_the_other_running(tmp_path, monkeypatch):
    started = {"a": asyncio.Event(), "b": asyncio.Event()}
    release = asyncio.Event()

    async def reply(**kwargs):
        text = _user_text(kwargs.get("messages"))
        key = "a" if "PING-A" in text else "b"
        started[key].set()
        while not release.is_set():
            if turn_mod.cancelled():
                return
            await asyncio.sleep(0.02)
        if turn_mod.cancelled():
            return
        yield "Answer A" if key == "a" else "Answer B"

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, bot_a, bot_b, chat_a, chat_b = await _two_bots(tmp_path, reply)
        headers = {"Accept": "text/event-stream"}

        async def collect(bot, chat, content):
            url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
            async with client.stream("POST", url, json={"content": content}, headers=headers) as response:
                body = (await response.aread()).decode()
                return body

        try:
            first = asyncio.create_task(collect(bot_a, chat_a, "PING-A"))
            second = asyncio.create_task(collect(bot_b, chat_b, "PING-B"))
            await asyncio.wait_for(started["a"].wait(), timeout=5)
            await asyncio.wait_for(started["b"].wait(), timeout=5)
            stopped = (
                await client.post(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/stop")
            ).json()
            assert stopped["run"]["status"] == "stopped"
            assert "you pressed Stop" in stopped["messages"][-1]["content"]
            assert stopped["messages"][-1]["content"].startswith("Stopped:")
            assert stopped["messages"][-1]["content"] != "Stopped."
            release.set()
            body_b = await asyncio.wait_for(second, timeout=5)
            body_a = await asyncio.wait_for(first, timeout=5)
            stored_b = (await client.get(f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}")).json()
        finally:
            release.set()
            await client.aclose()
        assert "Answer B" in body_b
        assert "Answer A" not in body_a
        assert stored_b["messages"][-1]["content"] == "Answer B"
        assert stored_b["run"]["status"] == "idle"

    asyncio.run(scenario())
