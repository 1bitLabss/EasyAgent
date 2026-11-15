"""Keep-going: a finish that leaves steps on the plan continues the chat on its own.

The person turns keep-going on once. After that, when a turn ends on `finish`
while the plan still has todo steps, the harness runs one continuation turn
with a plain-voice nudge, up to the budget. Stop, a new message during a
continuation, the budget, or a finished plan turn it back off.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from easyagent import turn as turn_mod
from easyagent.app import _finish_turn, create_app
from easyagent.plan import KEEP_GOING_BUDGET, budget_left, load_plan
from easyagent.tools import ToolRequest, execute, parse_tool

NUDGE = "Keep going: the plan still has 2 steps. Next: Write the summary."
CLOSE = "Stopped at the budget: 2 steps left. Say continue to keep going."
FINISH = "The first step is done.\n\n```finish\nproven\nGathered the sources.\n```"


class Recorder:
    """Fake model at the llm.complete seam, same tape pattern as test_0361."""

    def __init__(self, tape):
        self.tape = list(tape)
        self.seen = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.seen.append(messages)
        if self.tape:
            reply = self.tape.pop(0)
            return reply(messages) if callable(reply) else reply
        return "Nothing else is owed."


def _checklist_reply(m):
    return "The summary is drafted and sent.\n\n```finish\nproven\nWrote and sent it.\n```"


DONE = "The summary is drafted and sent."


async def _settle(store, chat_id, timeout=10.0):
    """Wait until the chat is idle again: every continuation has finished."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not turn_mod.slot_for(store, chat_id).busy:
            await asyncio.sleep(0.05)
            if not turn_mod.slot_for(store, chat_id).busy:
                return
        await asyncio.sleep(0.05)
    raise AssertionError("the chat never went idle")


async def _world(tmp_path, monkeypatch, tape, *, keep=None, content="do the job",
                 plan_body="Notes\n- Gather sources\n- Write the summary\n- Send it"):
    """A chat with a 3-step plan, step 1 done, and keep-going set as asked.

    plan_body=None skips the pre-made checklist, for tests where the bot builds
    the plan inside the turn.
    """
    recorder = Recorder(tape)
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    app = create_app(tmp_path)
    store = app.state.store
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        endpoint = (
            await client.post(
                "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
            )
        ).json()
        bot = (await client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]})).json()
        chat = (await client.post(f"/api/bots/{bot['id']}/chats")).json()
        turn_mod.bind(store, chat["id"], bot_id=bot["id"])
        if plan_body:
            await execute(
                store,
                ToolRequest(
                    kind="plan",
                    action="set",
                    body=plan_body,
                ),
                bot["id"],
            )
            await execute(store, ToolRequest(kind="plan", action="step", path="1", command="done"), bot["id"])
        if keep is not None:
            await execute(store, ToolRequest(kind="plan", action="keep", command=str(keep)), bot["id"])
        # bind() above claimed this task as the chat's worker. The send must own the next turn.
        turn_mod.slot_for(store, chat["id"]).worker = None
        sent = await client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages", json={"content": content}
        )
        assert sent.status_code == 200, sent.text
        await _settle(store, chat["id"])
        return store, bot, chat, recorder


def _nudges(store, bot_id, chat_id):
    chat = store.get_chat(bot_id, chat_id)
    return [
        item
        for item in chat.get("messages") or []
        if item.get("role") == "user" and str(item.get("content") or "").startswith("Keep going:")
    ]


def _lines(store, bot_id, chat_id):
    chat = store.get_chat(bot_id, chat_id)
    return [str(item.get("content") or "") for item in chat.get("messages") or []]


def test_finish_with_steps_left_schedules_one_continuation(tmp_path, monkeypatch):
    """(a) keep-going on: exactly one continuation turn, the nudge line, continued incremented."""

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH, DONE], keep=6
        )
        nudges = _nudges(store, bot["id"], chat["id"])
        assert len(nudges) == 1
        assert nudges[0]["content"] == NUDGE
        # The continuation really ran: the model was asked a second time with the nudge.
        assert len(recorder.seen) >= 2
        assert any(NUDGE in str(m.get("content") or "") for m in recorder.seen[-1])
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["continued"] == 1
        assert plan["budget"] == 6
        assert budget_left(plan) == 5
        # The first turn's answer is unchanged: no budget line while budget remains.
        assert not any(CLOSE in line for line in _lines(store, bot["id"], chat["id"]))

    asyncio.run(scenario())


def test_budget_exhausted_closes_honestly_once(tmp_path, monkeypatch):
    """(b) budget spent: no further continuation, the honest close line exactly once."""

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH, _checklist_reply], keep=1
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert sum(1 for line in lines if CLOSE in line) == 1
        # One continuation ran (the one that hit the wall), and no nudge followed it.
        assert len(_nudges(store, bot["id"], chat["id"])) == 1
        assert len(recorder.seen) == 2
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is False
        assert plan["continued"] == 1

    asyncio.run(scenario())


def test_keep_going_off_is_today_behavior(tmp_path, monkeypatch):
    """(c) opt-out: keep off (by the person or the bot) makes a finish go idle."""

    async def scenario():
        store, bot, chat, recorder = await _world(tmp_path, monkeypatch, [FINISH], keep="off")
        assert len(recorder.seen) == 1
        assert not _nudges(store, bot["id"], chat["id"])
        assert not any(CLOSE in line for line in _lines(store, bot["id"], chat["id"]))
        plan = load_plan(store, bot["id"], chat["id"])
        assert not plan.get("keep_going")
        assert int(plan.get("continued") or 0) == 0
        assert budget_left(plan) == 0

    asyncio.run(scenario())


def test_stop_during_a_continuation_cancels_further_continuations(tmp_path, monkeypatch):
    """(d) Stop wins: the pending continuation is dropped and keep-going turns off."""

    async def scenario():
        recorder = Recorder([FINISH])
        monkeypatch.setattr("easyagent.llm.complete", recorder)
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
            turn_mod.bind(store, chat["id"], bot_id=bot["id"])
            await execute(
                store,
                ToolRequest(kind="plan", action="set", body="Notes\n- Write the summary\n- Send it"),
                bot["id"],
            )
            await execute(store, ToolRequest(kind="plan", action="keep", command="4"), bot["id"])
            # The finish stamped a continuation. Then the person pressed Stop.
            turn_mod.slot_for(store, chat["id"]).worker = None
            turn_mod.try_begin(store, chat["id"])
            slot = turn_mod.slot_for(store, chat["id"])
            slot.keep_going_nudge = NUDGE
            slot.user_stop = True
            await _finish_turn(store, bot["id"], chat["id"])
            assert not _nudges(store, bot["id"], chat["id"])
            assert load_plan(store, bot["id"], chat["id"])["keep_going"] is False
            assert slot.busy is False
            assert not recorder.seen

    asyncio.run(scenario())


def test_no_progress_continuation_costs_two(tmp_path, monkeypatch):
    """(e) a continuation that ends with the same todo count costs the budget double."""

    async def scenario():
        # Turn 1 finishes with 2 steps left. The continuation finishes with the
        # same 2 left: no progress, so it costs 2 of the budget of 4.
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH, FINISH, "Nothing else is owed."], keep=4
        )
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["continued"] == 3
        assert budget_left(plan) == 1
        assert len(_nudges(store, bot["id"], chat["id"])) == 2
        assert len(recorder.seen) == 3

    asyncio.run(scenario())


def test_plan_fence_toggles_keep_going(tmp_path):
    """(f) the plan fence gains a sibling action: keep / keep off / keep <number>."""
    from fastapi.testclient import TestClient

    fastapi_app = create_app(tmp_path)
    app = TestClient(fastapi_app)
    store = fastapi_app.state.store
    endpoint = app.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    bot = app.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = app.post(f"/api/bots/{bot['id']}/chats").json()
    turn_mod.bind(store, chat["id"], bot_id=bot["id"])

    request = parse_tool("```plan\nkeep\n```")
    assert request is not None and request.kind == "plan" and request.action == "keep"
    assert parse_tool("```plan\nkeep off\n```").command == "off"
    assert parse_tool("```plan\nkeep\n3\n```").command == "3"

    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Write the summary\n- Send it"),
        bot["id"],
    ))
    said = asyncio.run(execute(store, ToolRequest(kind="plan", action="keep", command="3"), bot["id"]))
    plan = load_plan(store, bot["id"], chat["id"])
    assert plan["keep_going"] is True and plan["budget"] == 3 and plan["continued"] == 0
    assert "up to 3 times" in said
    asyncio.run(execute(store, ToolRequest(kind="plan", action="keep", command="off"), bot["id"]))
    assert load_plan(store, bot["id"], chat["id"])["keep_going"] is False
    app.close()


def test_plan_payload_exposes_keep_going_fields(tmp_path):
    """The chat payload carries keep_going/continued/budget additively."""
    from fastapi.testclient import TestClient

    fastapi_app = create_app(tmp_path)
    app = TestClient(fastapi_app)
    store = fastapi_app.state.store
    endpoint = app.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    bot = app.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = app.post(f"/api/bots/{bot['id']}/chats").json()
    turn_mod.bind(store, chat["id"], bot_id=bot["id"])
    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Write the summary\n- Send it"),
        bot["id"],
    ))
    public = app.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    plan = public["plan"]
    # A 2-step plan arms keep-going by default at the default budget.
    assert plan["keep_going"] is True
    assert plan["continued"] == 0
    assert plan["budget"] == KEEP_GOING_BUDGET
    asyncio.run(execute(store, ToolRequest(kind="plan", action="keep", command="5"), bot["id"]))
    public = app.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert public["plan"]["keep_going"] is True
    assert public["plan"]["budget"] == 5
    app.close()


def test_plan_keep_route_toggles(tmp_path):
    """POST /plan/keep turns keep-going on and off from the UI."""
    from fastapi.testclient import TestClient

    fastapi_app = create_app(tmp_path)
    app = TestClient(fastapi_app)
    store = fastapi_app.state.store
    endpoint = app.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    bot = app.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = app.post(f"/api/bots/{bot['id']}/chats").json()
    turn_mod.bind(store, chat["id"], bot_id=bot["id"])

    missing = app.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/plan/keep", json={"on": True})
    assert missing.status_code == 404

    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Write the summary\n- Send it"),
        bot["id"],
    ))
    turned_on = app.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/plan/keep", json={"on": True})
    assert turned_on.status_code == 200
    plan = turned_on.json()["plan"]
    assert plan["keep_going"] is True
    assert plan["budget"] >= 1
    assert plan["continued"] == 0

    with_budget = app.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/plan/keep", json={"on": True, "budget": 3}
    )
    assert with_budget.json()["plan"]["budget"] == 3

    turned_off = app.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/plan/keep", json={"on": False})
    assert turned_off.json()["plan"]["keep_going"] is False
    app.close()


def _bare_world(tmp_path):
    """An app with one chat, no plan yet."""
    from fastapi.testclient import TestClient

    fastapi_app = create_app(tmp_path)
    app = TestClient(fastapi_app)
    store = fastapi_app.state.store
    endpoint = app.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    bot = app.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = app.post(f"/api/bots/{bot['id']}/chats").json()
    turn_mod.bind(store, chat["id"], bot_id=bot["id"])
    return app, store, bot, chat


def test_multi_step_plan_autoarms_without_a_tap(tmp_path):
    """(g) a 3-step plan created during a task arms keep-going by itself."""
    app, store, bot, chat = _bare_world(tmp_path)
    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Gather sources\n- Write the summary\n- Send it"),
        bot["id"],
    ))
    plan = load_plan(store, bot["id"], chat["id"])
    assert plan["keep_going"] is True
    assert plan["budget"] == KEEP_GOING_BUDGET
    assert plan["continued"] == 0
    assert not plan.get("keep_off_by_user")
    app.close()


def test_single_step_plan_does_not_autoarm(tmp_path):
    """(h) one step is not multi-step work: no auto-arm."""
    app, store, bot, chat = _bare_world(tmp_path)
    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Gather sources"),
        bot["id"],
    ))
    plan = load_plan(store, bot["id"], chat["id"])
    assert not plan.get("keep_going")
    assert int(plan.get("budget") or 0) == 0
    app.close()


def test_stop_blocks_rearming_on_later_plan_edits(tmp_path):
    """(i) the person tapped stop in this plan: later plan edits do not re-arm."""
    app, store, bot, chat = _bare_world(tmp_path)
    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Write the summary\n- Send it"),
        bot["id"],
    ))
    assert load_plan(store, bot["id"], chat["id"])["keep_going"] is True
    turned_off = app.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/plan/keep", json={"on": False})
    assert turned_off.status_code == 200
    plan = load_plan(store, bot["id"], chat["id"])
    assert plan["keep_going"] is False
    assert plan.get("keep_off_by_user") is True
    # A later plan edit (steps gained) must not re-arm over the person's stop.
    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Write the summary\n- Send it\n- Archive it"),
        bot["id"],
    ))
    plan = load_plan(store, bot["id"], chat["id"])
    assert plan["keep_going"] is False
    assert plan.get("keep_off_by_user") is True
    app.close()


def test_keep_on_again_clears_the_off_flag_and_rearms(tmp_path):
    """(j) the person taps keep on: the off-flag clears and later edits re-arm."""
    app, store, bot, chat = _bare_world(tmp_path)
    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Write the summary\n- Send it"),
        bot["id"],
    ))
    app.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/plan/keep", json={"on": False})
    assert load_plan(store, bot["id"], chat["id"]).get("keep_off_by_user") is True
    turned_on = app.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/plan/keep", json={"on": True})
    plan = turned_on.json()["plan"]
    assert plan["keep_going"] is True
    assert not plan.get("keep_off_by_user")
    # Re-arming is back: a fresh multi-step set arms at the default budget.
    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Write the summary\n- Send it\n- Archive it"),
        bot["id"],
    ))
    plan = load_plan(store, bot["id"], chat["id"])
    assert plan["keep_going"] is True
    assert plan["budget"] == KEEP_GOING_BUDGET
    app.close()


def test_plan_created_during_a_task_continues_without_a_tap(tmp_path, monkeypatch):
    """(k) end to end: the bot makes a 3-step plan mid-task and keeps going on its own."""
    fence = "```plan\nNotes\n- Gather sources\n- Write the summary\n- Send it\n```"
    first = fence + "\nStep one is done.\n\n```finish\nproven\nGathered the sources.\n```"

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [first, DONE], plan_body=None
        )
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is True
        assert plan["budget"] == KEEP_GOING_BUDGET
        assert plan["continued"] == 1
        nudges = _nudges(store, bot["id"], chat["id"])
        assert len(nudges) == 1
        assert "3 steps" in nudges[0]["content"]

    asyncio.run(scenario())
