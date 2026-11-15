"""Keep-going: a finish that leaves steps on the plan continues the chat on its own.

Keep-going runs until the plan is done or it clearly stalls. After a finish
while the plan still has todo steps, the harness runs one continuation turn
with a plain-voice nudge. Two continuations in a row that end with the plan
truly unchanged stop the run with an honest close line; any progress resets
that counter. Progress is a smaller open count OR any change to the plan
itself: reopening a done step, adding a subtask, or reordering is productive
churn, witnessed by a digest of the steps list (ids, states, texts). A hard
cap of KEEP_GOING_HARD_CAP continuations is a runaway backstop.
Stop, a new message during a continuation, or a finished plan turns it off.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from easyagent import plan as plan_mod
from easyagent import turn as turn_mod
from easyagent.app import _finish_turn, create_app
from easyagent.plan import KEEP_GOING_HARD_CAP, _write_plan, budget_left, load_plan, set_keep_going
from easyagent.tools import ToolRequest, execute, parse_tool

NUDGE = "Keep going: the plan still has 2 steps. Next: Write the summary."
STALL = "Stopped: two tries in a row made no progress. 2 steps left. Say continue to keep going."
CAP = "Stopped after 40 goes: 2 steps left. Say continue."
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


def _progress_reply(n: int) -> str:
    """A continuation that marks step n done, then finishes: real progress."""
    return (
        f"```plan\nstep\n{n}\ndone\n```\nStep {n} is done.\n\n"
        f"```finish\nproven\nFinished step {n}.\n```"
    )


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
                 plan_body="Notes\n- Gather sources\n- Write the summary\n- Send it",
                 continued=0):
    """A chat with a 3-step plan, step 1 done, and keep-going set as asked.

    plan_body=None skips the pre-made checklist, for tests where the bot builds
    the plan inside the turn. continued pre-stamps the counter before the send.
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
        if continued:
            stamped = load_plan(store, bot["id"], chat["id"])
            stamped["continued"] = continued
            _write_plan(store, bot["id"], chat["id"], stamped)
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
        # The number on the fence is ignored: the budget field carries the hard cap.
        assert plan["budget"] == KEEP_GOING_HARD_CAP
        assert budget_left(plan) == KEEP_GOING_HARD_CAP - 1
        # The first turn's answer is unchanged: no close line while the run continues.
        assert not any(STALL in line for line in _lines(store, bot["id"], chat["id"]))

    asyncio.run(scenario())


def test_ten_progressing_continuations_all_fire(tmp_path, monkeypatch):
    """(b) progress-based stopping: 10 progressing continuations all fire (old code stopped at 8)."""

    async def scenario():
        steps = "\n".join(f"- Step {n}" for n in range(1, 13))
        tape = [FINISH] + [_progress_reply(n) for n in range(2, 12)]
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, tape + ["All done."],
            plan_body="Notes\n" + steps,
        )
        assert len(_nudges(store, bot["id"], chat["id"])) == 11
        assert len(recorder.seen) == 12
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is True
        # 10 progressing continuations fired (plus the first turn's stamp).
        assert plan["continued"] == 11
        assert int(plan.get("stuck") or 0) == 0
        assert not any(STALL in line for line in _lines(store, bot["id"], chat["id"]))
        assert not any(CAP in line for line in _lines(store, bot["id"], chat["id"]))

    asyncio.run(scenario())


def test_two_no_progress_tries_stop_with_the_stall_line(tmp_path, monkeypatch):
    """(c) two consecutive no-progress continuations: the stall line once, no third continuation."""

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH, FINISH, FINISH, "Idle."], keep=6
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert sum(1 for line in lines if STALL in line) == 1
        # Two continuations ran; the second no-progress try closed the run, no nudge after it.
        assert len(_nudges(store, bot["id"], chat["id"])) == 2
        assert len(recorder.seen) == 3
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is False
        assert plan["continued"] == 2

    asyncio.run(scenario())


def test_progress_between_no_progress_tries_resets_the_counter(tmp_path, monkeypatch):
    """(d) no-progress, progress, no-progress: the stuck counter resets, the run keeps going."""

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH, FINISH, _progress_reply(2), FINISH, "Idle."], keep=6
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert not any(STALL in line for line in lines)
        assert len(_nudges(store, bot["id"], chat["id"])) == 4
        assert len(recorder.seen) == 5
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is True
        assert plan["continued"] == 4
        # The last try was a no-progress one, but not in a row: stuck sits at 1.
        assert int(plan.get("stuck") or 0) == 1

    asyncio.run(scenario())


def _churn_reply(done_n: int, reopen_n: int) -> str:
    """A continuation that marks one step done and reopens another: the open
    count does not move, but the plan changed — productive churn, not a stall."""
    return (
        f"```plan\nstep\n{done_n}\ndone\n```\n"
        f"```plan\nstep\n{reopen_n}\ntodo\n```\n"
        f"Step {done_n} is done; step {reopen_n} needs another pass.\n\n"
        f"```finish\nproven\nRevised the plan.\n```"
    )


def _grow_reply() -> str:
    """A continuation that adds a subtask and finishes two steps: the open
    count stays 2, but the plan grew and moved."""
    return (
        "```plan\nNotes\n- Gather sources\n- Verify the numbers\n- Write the summary\n- Send it\n```\n"
        "```plan\nstep\n1\ndone\n```\n"
        "```plan\nstep\n2\ndone\n```\n"
        "Added a subtask and finished two steps.\n\n"
        "```finish\nproven\nGrew the plan.\n```"
    )


def test_reopening_a_step_counts_as_progress(tmp_path, monkeypatch):
    """(o) plan mutation is progress: two continuations that reopen a done step
    while the open count holds at 2 keep going; the old count-only rule stalled."""

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch,
            [FINISH, _churn_reply(2, 1), _churn_reply(3, 2), "Idle."], keep=6,
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert not any(STALL in line for line in lines)
        assert len(_nudges(store, bot["id"], chat["id"])) == 3
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is True
        assert plan["continued"] == 3
        assert int(plan.get("stuck") or 0) == 0
        # The mark stored is the plan digest, not the bare open count.
        assert plan["keep_going_mark"] == plan_mod.plan_digest(plan)

    asyncio.run(scenario())


def test_adding_a_subtask_counts_as_progress(tmp_path, monkeypatch):
    """(p) a continuation that grows the plan (new subtask, steps finished,
    open count unchanged) is progress, not a stall."""

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch,
            [FINISH, _grow_reply(), _churn_reply(3, 1), "Idle."], keep=6,
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert not any(STALL in line for line in lines)
        assert len(_nudges(store, bot["id"], chat["id"])) == 3
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is True
        # The set inside the first continuation re-armed the run (a plan set
        # resets the counters), so two stamps remain from that point on.
        assert plan["continued"] == 2
        assert int(plan.get("stuck") or 0) == 0
        assert plan_mod.unfinished(plan) == 2

    asyncio.run(scenario())


def test_hard_cap_closes_honestly_once(tmp_path, monkeypatch):
    """(e) the hard cap is a backstop: at 40 goes the run closes with the cap line."""

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH, "Idle."], continued=KEEP_GOING_HARD_CAP
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert sum(1 for line in lines if CAP in line) == 1
        assert not _nudges(store, bot["id"], chat["id"])
        assert len(recorder.seen) == 1
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is False
        assert plan["continued"] == KEEP_GOING_HARD_CAP

    asyncio.run(scenario())


def test_continue_after_a_stall_rearms_with_counters_reset(tmp_path, monkeypatch):
    """(f) after a stall, turning keep-going back on resets continued and the stuck counter."""

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH, FINISH, FINISH, "Idle."], keep=6
        )
        stalled = load_plan(store, bot["id"], chat["id"])
        assert stalled["keep_going"] is False
        assert stalled["continued"] == 2
        # The person says continue: the same re-arm the chip and the fence use.
        set_keep_going(store, bot["id"], chat["id"], on=True)
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is True
        assert plan["continued"] == 0
        assert int(plan.get("stuck") or 0) == 0
        assert budget_left(plan) == KEEP_GOING_HARD_CAP

    asyncio.run(scenario())


def test_keep_going_off_is_today_behavior(tmp_path, monkeypatch):
    """(g) opt-out: keep off (by the person or the bot) makes a finish go idle."""

    async def scenario():
        store, bot, chat, recorder = await _world(tmp_path, monkeypatch, [FINISH], keep="off")
        assert len(recorder.seen) == 1
        assert not _nudges(store, bot["id"], chat["id"])
        assert not any(STALL in line for line in _lines(store, bot["id"], chat["id"]))
        plan = load_plan(store, bot["id"], chat["id"])
        assert not plan.get("keep_going")
        assert int(plan.get("continued") or 0) == 0
        assert budget_left(plan) == 0

    asyncio.run(scenario())


def test_stop_during_a_continuation_cancels_further_continuations(tmp_path, monkeypatch):
    """(h) Stop wins: the pending continuation is dropped and keep-going turns off."""

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


def test_plan_fence_toggles_keep_going(tmp_path):
    """(i) the plan fence gains a sibling action: keep / keep off / keep <number>."""
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
    # A number is accepted but ignored: the run ends on progress, not on a count.
    assert plan["keep_going"] is True and plan["budget"] == KEEP_GOING_HARD_CAP and plan["continued"] == 0
    assert "stalls twice in a row" in said
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
    # A 2-step plan arms keep-going by default at the hard cap.
    assert plan["keep_going"] is True
    assert plan["continued"] == 0
    assert plan["budget"] == KEEP_GOING_HARD_CAP
    asyncio.run(execute(store, ToolRequest(kind="plan", action="keep", command="5"), bot["id"]))
    public = app.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert public["plan"]["keep_going"] is True
    assert public["plan"]["budget"] == KEEP_GOING_HARD_CAP
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
    assert plan["budget"] == KEEP_GOING_HARD_CAP
    assert plan["continued"] == 0

    # A budget number is accepted and ignored: the cap is the cap.
    with_budget = app.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/plan/keep", json={"on": True, "budget": 3}
    )
    assert with_budget.json()["plan"]["budget"] == KEEP_GOING_HARD_CAP

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
    """(j) a 3-step plan created during a task arms keep-going by itself."""
    app, store, bot, chat = _bare_world(tmp_path)
    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Gather sources\n- Write the summary\n- Send it"),
        bot["id"],
    ))
    plan = load_plan(store, bot["id"], chat["id"])
    assert plan["keep_going"] is True
    assert plan["budget"] == KEEP_GOING_HARD_CAP
    assert plan["continued"] == 0
    assert not plan.get("keep_off_by_user")
    app.close()


def test_single_step_plan_does_not_autoarm(tmp_path):
    """(k) one step is not multi-step work: no auto-arm."""
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
    """(l) the person tapped stop in this plan: later plan edits do not re-arm."""
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
    """(m) the person taps keep on: the off-flag clears and later edits re-arm."""
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
    assert plan["budget"] == KEEP_GOING_HARD_CAP
    app.close()


def test_plan_created_during_a_task_continues_without_a_tap(tmp_path, monkeypatch):
    """(n) end to end: the bot makes a 3-step plan mid-task and keeps going on its own."""
    fence = "```plan\nNotes\n- Gather sources\n- Write the summary\n- Send it\n```"
    first = fence + "\nStep one is done.\n\n```finish\nproven\nGathered the sources.\n```"

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [first, DONE], plan_body=None
        )
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is True
        assert plan["budget"] == KEEP_GOING_HARD_CAP
        assert plan["continued"] == 1
        nudges = _nudges(store, bot["id"], chat["id"])
        assert len(nudges) == 1
        assert "3 steps" in nudges[0]["content"]

    asyncio.run(scenario())
