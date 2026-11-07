"""The child budget cuts off a hung model call, and the concurrency cap is per chat.

W5: a helper's seconds budget was only checked between steps, so a hung model
call could sit for the llm default timeout. W6: the in-flight child counter was
process-global, so one chat's children starved every other chat.
"""

import asyncio
import time

import pytest

from easyagent import delegate
from easyagent import helpers
from easyagent import llm
from easyagent import turn as turn_mod
from easyagent.delegate import (
    MAX_CONCURRENT,
    _acquire,
    _release,
    inflight,
    reset_for_tests,
    run_delegate,
)
from easyagent.helpers import HelperSpec, _run_one
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest


def _world(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="stub")
    return store, bot, endpoint


def test_the_counter_is_per_chat_not_process_global():
    reset_for_tests()

    async def scene():
        for _ in range(MAX_CONCURRENT):
            await _acquire("chat-a")
        # A fourth child waits its turn instead of losing its work.
        waiting = asyncio.create_task(_acquire("chat-a", timeout=5))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        _release("chat-a")
        await waiting
        assert inflight("chat-a") == MAX_CONCURRENT
        # A full chat still gives up with the plain refusal after the wait.
        with pytest.raises(ToolError, match="Too many child agents"):
            await _acquire("chat-a", timeout=0.1)
        # A different chat is not starved by chat-a's full set of children.
        await _acquire("chat-b")
        assert inflight("chat-a") == MAX_CONCURRENT
        assert inflight("chat-b") == 1
        assert inflight() == MAX_CONCURRENT + 1

    asyncio.run(scene())
    reset_for_tests()
    assert inflight() == 0


def test_two_chats_each_run_children_without_starving_each_other(tmp_path, monkeypatch):
    store, bot, _endpoint = _world(tmp_path)
    chat_one = store.create_chat(bot["id"])
    chat_two = store.create_chat(bot["id"])

    release = asyncio.Event()
    running: list[str] = []

    async def fake_run(store_, bot_id, specs):
        running.append(specs[0].task)
        if len(running) == MAX_CONCURRENT + 1:
            release.set()
        await release.wait()
        return [{"task": specs[0].task, "result": "ok", "status": "done"}]

    monkeypatch.setattr(helpers, "run_helpers", fake_run)

    async def scene():
        async def child(chat, name):
            turn_mod.bind(store, chat["id"], bot_id=bot["id"])
            request = ToolRequest(kind="delegate", body=name, command='["read_file"]')
            return await run_delegate(store, bot["id"], request)

        busy_one = [asyncio.create_task(child(chat_one, f"a{i}")) for i in range(MAX_CONCURRENT)]
        # chat_two starts while chat_one already holds its full per-chat set.
        also_two = asyncio.create_task(child(chat_two, "b0"))
        # chat_one's fourth child queues at its own cap, not by chat_two,
        # and runs when a slot frees instead of losing its work.
        fourth = asyncio.create_task(child(chat_one, "a3"))
        results = await asyncio.gather(*busy_one, also_two, fourth)
        assert "a3" in running
        assert "b0" in running
        assert all("ok" in text for text in results)

    asyncio.run(scene())
    assert delegate.inflight() == 0


def test_a_hung_model_call_is_cut_off_by_the_seconds_budget(tmp_path, monkeypatch):
    store, bot, endpoint = _world(tmp_path)
    seen: dict = {}

    async def hung_complete(**kwargs):
        seen["timeout"] = kwargs.get("timeout")
        await asyncio.sleep(30)
        return "never"

    monkeypatch.setattr(llm, "complete", hung_complete)
    spec = HelperSpec(
        name="Child",
        task="count the notes",
        tools=frozenset({"files"}),
        steps=4,
        seconds=1,
        listed=("read_file",),
    )
    started = time.monotonic()
    row = asyncio.run(_run_one(store, bot, endpoint, spec, yield_to_chats=False))
    elapsed = time.monotonic() - started
    assert elapsed < 5
    assert row["status"] == "error"
    assert "time" in row["result"].lower()
    # The remaining budget also reaches the provider call itself.
    assert seen.get("timeout") is not None and seen["timeout"] <= 1


def test_a_failure_before_the_child_starts_does_not_leak_a_slot(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("setup failed")

    monkeypatch.setattr(delegate, "isolated_messages", boom)
    request = ToolRequest(kind="delegate", body="look", command="[]")
    with pytest.raises(RuntimeError, match="setup failed"):
        asyncio.run(run_delegate(None, "bot", request))
    assert delegate.inflight() == 0
    # The slot is usable again, so nothing leaked.
    asyncio.run(_acquire("bot"))
    assert inflight("bot") == 1
    _release("bot")
    reset_for_tests()
