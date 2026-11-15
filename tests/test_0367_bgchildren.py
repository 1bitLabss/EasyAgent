"""0.3.67: background children.

A delegated child must not block the parent's turn: fire it detached, end the
turn with a plain line, and wake the parent when it lands through the existing
between-turns queue (plan.enqueue + the supervisor kick / _spawn_drain). A
restart settles children still recorded as running.

The bug-sensitive core: before this feature a delegate call always awaited its
child, so the parent turn could not end early; there was no .children.json, no
queue line on completion, and nothing to settle after a restart.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from easyagent import delegate, helpers, llm, plan
from easyagent import turn as turn_mod
from easyagent.app import create_app
from easyagent.delegate import (
    MAX_CONCURRENT,
    RESTART_NOTE,
    _acquire,
    children_path,
    inflight,
    reset_for_tests,
    run_delegate,
    settle_orphan_children,
)
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest


def _store_world(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(
        name="local", base_url="http://127.0.0.1:9/v1", api_key=None, max_parallel=3
    )
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="m")
    chat = store.create_chat(bot["id"])
    return store, bot["id"], chat["id"]


def _queued_lines(store, bot_id, chat_id):
    return [str(item.get("content") or "") for item in plan._read_queue(store, bot_id, chat_id)["items"]]


def _noop_kick(monkeypatch):
    async def kick(_store):
        return 0

    monkeypatch.setattr("easyagent.app.kick", kick)


def test_background_delegate_returns_now_and_lands_in_the_queue(tmp_path, monkeypatch):
    """The delegate call returns before the child finishes; the finished line
    arrives in the parent chat's queue afterwards, as one plain wrapped line."""
    _noop_kick(monkeypatch)
    reset_for_tests()
    store, bot_id, chat_id = _store_world(tmp_path)

    async def fake_run(store_, bot, specs, *, parent_chat_id=None):
        await asyncio.sleep(0.2)
        return [
            {"name": s.name, "status": "done", "result": "found 3 notes", "child_id": s.child_id}
            for s in specs
        ]

    monkeypatch.setattr(helpers, "run_helpers", fake_run)

    async def scene():
        turn_mod.bind(store, chat_id, bot_id)
        request = ToolRequest(
            kind="delegate",
            body="count the notes",
            command=json.dumps(["read_file"]),
            background=True,
        )
        started = time.monotonic()
        out = await run_delegate(store, bot_id, request)
        elapsed = time.monotonic() - started
        assert elapsed < 0.1, "the delegate call must not wait for the child"
        assert "Started a background worker" in out and "count the notes" in out

        # The child is recorded as running next to the chat, and it holds a slot.
        path = children_path(store, bot_id, chat_id)
        rows = json.loads(path.read_text(encoding="utf-8"))
        assert len(rows) == 1 and rows[0]["state"] == "running"
        assert rows[0]["task"] == "count the notes"
        assert inflight(chat_id) == 1
        assert _queued_lines(store, bot_id, chat_id) == []

        # The parent turn is over; the detached child keeps working and lands.
        await asyncio.sleep(0.5)
        assert _queued_lines(store, bot_id, chat_id) == [
            "Background worker finished: count the notes. Result: found 3 notes"
        ]
        rows = json.loads(path.read_text(encoding="utf-8"))
        assert rows[0]["state"] == "done"
        assert inflight(chat_id) == 0

    asyncio.run(scene())
    reset_for_tests()


def test_background_failure_delivers_the_failed_line(tmp_path, monkeypatch):
    """A failed child delivers the plain failed line, not a silent drop."""
    _noop_kick(monkeypatch)
    reset_for_tests()
    store, bot_id, chat_id = _store_world(tmp_path)

    async def fake_run(store_, bot, specs, *, parent_chat_id=None):
        await asyncio.sleep(0.05)
        return [
            {"name": s.name, "status": "error", "result": "the endpoint went away", "child_id": s.child_id}
            for s in specs
        ]

    monkeypatch.setattr(helpers, "run_helpers", fake_run)

    async def scene():
        turn_mod.bind(store, chat_id, bot_id)
        request = ToolRequest(
            kind="delegate",
            body="count the notes",
            command=json.dumps(["read_file"]),
            background=True,
        )
        await run_delegate(store, bot_id, request)
        await asyncio.sleep(0.3)
        assert _queued_lines(store, bot_id, chat_id) == [
            "Background worker failed: count the notes. the endpoint went away"
        ]
        rows = json.loads(children_path(store, bot_id, chat_id).read_text(encoding="utf-8"))
        assert rows[0]["state"] == "failed"
        assert inflight(chat_id) == 0

    asyncio.run(scene())
    reset_for_tests()


def test_startup_settles_children_left_running(tmp_path):
    """A record still running after a restart cannot be running: the startup
    hook settles it with the plain restart line and clears the file."""
    store, bot_id, chat_id = _store_world(tmp_path)
    path = children_path(store, bot_id, chat_id)
    path.write_text(
        json.dumps(
            [
                {
                    "child_id": "c1",
                    "name": "Child",
                    "task": "count the notes",
                    "started_at": time.time(),
                    "state": "running",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert settle_orphan_children(store) == 1
    assert _queued_lines(store, bot_id, chat_id) == [
        f"Background worker failed: count the notes. {RESTART_NOTE}"
    ]
    assert not path.exists(), "settled records leave the file behind empty"
    # A second pass finds nothing left to settle.
    assert settle_orphan_children(store) == 0


def test_background_children_count_toward_the_cap(tmp_path, monkeypatch):
    """The same 3-concurrent cap counts background children: while three hold
    slots a fourth grab fails, and the slots come back when they land."""
    _noop_kick(monkeypatch)
    reset_for_tests()
    store, bot_id, chat_id = _store_world(tmp_path)

    async def fake_run(store_, bot, specs, *, parent_chat_id=None):
        await asyncio.sleep(0.3)
        return [
            {"name": s.name, "status": "done", "result": "ok", "child_id": s.child_id} for s in specs
        ]

    monkeypatch.setattr(helpers, "run_helpers", fake_run)

    async def scene():
        turn_mod.bind(store, chat_id, bot_id)
        request = ToolRequest(
            kind="delegate",
            body="count the notes",
            command=json.dumps(["read_file"]),
            background=True,
        )
        for _ in range(MAX_CONCURRENT):
            await run_delegate(store, bot_id, request)
        assert inflight(chat_id) == MAX_CONCURRENT
        with pytest.raises(ToolError, match="Too many child agents"):
            await _acquire(chat_id, timeout=0.1)
        await asyncio.sleep(0.6)
        assert inflight(chat_id) == 0
        await _acquire(chat_id, timeout=0.5)
        delegate._release(chat_id)

    asyncio.run(scene())
    reset_for_tests()


def test_foreground_delegate_is_unchanged(tmp_path, monkeypatch):
    """Regression: without the background flag the parent still waits for the
    summary, and nothing is written to .children.json or the queue."""
    _noop_kick(monkeypatch)
    reset_for_tests()
    store, bot_id, chat_id = _store_world(tmp_path)

    async def fake_run(store_, bot, specs, *, parent_chat_id=None):
        return [
            {"name": s.name, "status": "done", "result": "3 notes", "child_id": s.child_id} for s in specs
        ]

    monkeypatch.setattr(helpers, "run_helpers", fake_run)

    async def scene():
        turn_mod.bind(store, chat_id, bot_id)
        request = ToolRequest(kind="delegate", body="count the notes", command=json.dumps(["read_file"]))
        out = await run_delegate(store, bot_id, request)
        assert "3 notes" in out
        assert not children_path(store, bot_id, chat_id).exists()
        assert _queued_lines(store, bot_id, chat_id) == []
        assert inflight(chat_id) == 0

    asyncio.run(scene())
    reset_for_tests()


class BgTape:
    """Scripted model for the full flow: the parent fires one background
    delegate and ends its turn; the child finishes late; the drained queue line
    wakes the parent for a second turn."""

    FENCE = (
        "Working on it.\n\n```delegate\ncount the notes\n"
        'tools: ["read_file"]\nbackground: true\n```'
    )

    def __init__(self):
        self.parent_calls = 0
        self.child_calls = 0

    def _reply(self, messages):
        system = messages[0].get("content") or ""
        blob = "\n".join(item.get("content") or "" for item in messages)
        if "a helper with one task" in system:
            return "CHILD"
        if "Background worker finished" in blob:
            return "Thanks — the notes are counted."
        self.parent_calls += 1
        if "Started a background worker" in blob:
            return "Started 1 background worker. I will report when they land."
        return self.FENCE

    async def complete(self, *, messages, **kwargs):
        reply = self._reply(messages)
        if reply == "CHILD":
            self.child_calls += 1
            await asyncio.sleep(0.2)
            return "found 3 notes"
        return reply

    async def stream_complete(self, *, messages, **kwargs):
        reply = self._reply(messages)
        if reply == "CHILD":
            self.child_calls += 1
            await asyncio.sleep(0.2)
            yield "found 3 notes"
            return
        yield reply


def test_background_result_wakes_the_parent_through_the_queue(tmp_path, monkeypatch):
    """End to end: the parent turn ends with the started line while the child
    is still running; the finished line then arrives in the parent chat as a
    normal user-shaped turn through the existing queue machinery.

    The client runs with its lifespan (a `with` block): the detached worker
    needs one event loop that lives across requests, exactly like the real
    server, and the supervisor kick is the real wake path."""
    reset_for_tests()
    tape = BgTape()
    monkeypatch.setattr("easyagent.llm.complete", tape.complete)
    monkeypatch.setattr("easyagent.llm.stream_complete", tape.stream_complete)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        endpoint = client.post(
            "/api/endpoints",
            json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "max_parallel": 3},
        ).json()
        bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
        chat = client.post(f"/api/bots/{bot['id']}/chats").json()

        with client.stream(
            "POST",
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": "count the notes in the background"},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            body = response.read().decode()
        assert "Started 1 background worker. I will report when they land." in body

        # The wake lands on its own: the child finishes, the line queues, the
        # kick drains it as a new turn. Poll the transcript for both halves.
        deadline = time.time() + 10
        messages = []
        blob = ""
        while time.time() < deadline:
            messages = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()["messages"]
            blob = json.dumps(messages, ensure_ascii=False)
            if "Background worker finished: count the notes" in blob and "Thanks — the notes are counted." in blob:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"the background result never woke the parent: {blob[:2000]}")

        wrapped = [m for m in messages if "Background worker finished: count the notes" in str(m.get("content"))]
        assert wrapped and wrapped[0]["role"] == "user", "the drained line is a user-shaped turn"
        assert "Result: found 3 notes" in wrapped[0]["content"]

        # The Jobs payload lists the finished background child additively.
        children = client.get("/api/children").json()
        bg = [item for item in children if item.get("background")]
        assert bg and bg[0]["state"] == "done" and bg[0]["task_excerpt"] == "count the notes"
    reset_for_tests()


def test_keep_going_parks_while_a_background_child_runs(tmp_path, monkeypatch):
    """Keep-going parks while a background worker is in flight: a finish with
    the plan still open spends no continuation and writes no close line — it
    just goes idle. The child landing wakes the chat through the queue as
    normal, and the run is parked, not stopped."""
    import httpx

    from easyagent.delegate import (
        _settle_bg_child,
        deliver_bg_result,
        register_bg_child,
        running_children,
    )
    from easyagent.tools import execute

    reset_for_tests()
    tape = [
        "The first step is done.\n\n```finish\nproven\nGathered the sources.\n```",
        "Thanks — the notes are counted.",
    ]
    calls = []

    async def fake_complete(*, messages, **kwargs):
        calls.append(messages)
        return tape.pop(0) if tape else "Nothing else is owed."

    monkeypatch.setattr("easyagent.llm.complete", fake_complete)

    async def scene():
        app = create_app(tmp_path)
        store = app.state.store
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            endpoint = (
                await client.post(
                    "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
                )
            ).json()
            bot = (
                await client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]})
            ).json()
            chat = (await client.post(f"/api/bots/{bot['id']}/chats")).json()
            bot_id, chat_id = bot["id"], chat["id"]
            turn_mod.bind(store, chat_id, bot_id=bot_id)
            await execute(
                store,
                ToolRequest(
                    kind="plan",
                    action="set",
                    body="Notes\n- Gather sources\n- Write the summary\n- Send it",
                ),
                bot_id,
            )
            await execute(store, ToolRequest(kind="plan", action="step", path="1", command="done"), bot_id)
            turn_mod.slot_for(store, chat_id).worker = None

            # A background worker is in flight for this chat.
            register_bg_child(store, bot_id, chat_id, "bg1", "count the notes")
            assert running_children(store, bot_id, chat_id) == 1

            sent = await client.post(
                f"/api/bots/{bot_id}/chats/{chat_id}/messages", json={"content": "do the job"}
            )
            assert sent.status_code == 200, sent.text
            deadline = time.time() + 10
            while time.time() < deadline and turn_mod.slot_for(store, chat_id).busy:
                await asyncio.sleep(0.05)
            assert not turn_mod.slot_for(store, chat_id).busy, "the parked turn never went idle"

            # Parked: no continuation fired, no close line, keep-going still armed.
            plan_data = plan.load_plan(store, bot_id, chat_id)
            assert plan_data["keep_going"] is True
            assert int(plan_data.get("continued") or 0) == 0
            assert int(plan_data.get("stuck") or 0) == 0
            blob = " | ".join(
                str(m.get("content") or "")
                for m in (store.get_chat(bot_id, chat_id).get("messages") or [])
            )
            assert "Keep going:" not in blob
            assert "no progress" not in blob and "Say continue" not in blob

            # The child lands: the real delivery path wakes the chat.
            _settle_bg_child(store, bot_id, chat_id, "bg1", "done", "found 3 notes")
            assert running_children(store, bot_id, chat_id) == 0
            deliver_bg_result(
                store, bot_id, chat_id, "Background worker finished: count the notes. Result: found 3 notes"
            )
            deadline = time.time() + 10
            woke = ""
            while time.time() < deadline:
                woke = " | ".join(
                    str(m.get("content") or "")
                    for m in (store.get_chat(bot_id, chat_id).get("messages") or [])
                )
                if "Thanks — the notes are counted." in woke:
                    break
                await asyncio.sleep(0.05)
            assert "Background worker finished: count the notes" in woke, "the child result never woke the chat"
            assert "Thanks — the notes are counted." in woke

            # The run was parked, not stopped: keep-going is still armed and
            # nothing was consumed while the worker ran.
            plan_data = plan.load_plan(store, bot_id, chat_id)
            assert plan_data["keep_going"] is True
            assert int(plan_data.get("continued") or 0) == 0

    asyncio.run(scene())
    reset_for_tests()
