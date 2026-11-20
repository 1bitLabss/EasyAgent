"""Goal judge: a second model decides each keep-going continuation.

When a plan arms the judge (plan fence: judge on), the digest witness steps
aside at the continuation decision. A judge call reads the goal, the agent's
last answer, the checklist state, and the running background children, and
returns one verdict on one JSON line: done closes the run, blocked pauses it
(not done), continue runs one more go with the reason on the line, wait parks
without spending a budget go. Garbage fails open to continue. Three parse
failures or five transport failures in a row pause the run with an honest
line; any usable reply resets both counters. The numbers are ported from
Hermes' goals.py.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from easyagent import goaljudge
from easyagent import turn as turn_mod
from easyagent.app import create_app
from easyagent.goaljudge import (
    MAX_WAIT_SECONDS,
    apply_health,
    build_user_prompt,
    parse_judge_response,
)
from easyagent.plan import KEEP_GOING_HARD_CAP, load_plan, set_keep_going
from easyagent.tools import ToolRequest, execute, parse_tool

FINISH = "The first step is done.\n\n```finish\nproven\nGathered the sources.\n```"
DONE = "The summary is drafted and sent."


# ── verdict parsing (goals.py 798-845) ────────────────────────────────


def test_parse_verdict_shapes():
    assert parse_judge_response('{"verdict": "done", "reason": "sent"}')[:3] == (
        "done",
        "sent",
        False,
    )
    assert parse_judge_response('{"verdict": "BLOCKED", "reason": "no path"}')[:2] == (
        "blocked",
        "no path",
    )
    assert parse_judge_response('{"verdict": "continue", "reason": "next step"}')[0] == "continue"
    # wait targets: seconds (capped at 1800), then pid, then session.
    v, _, pf, wait = parse_judge_response('{"verdict": "wait", "wait_for_seconds": 60}')
    assert (v, pf, wait) == ("wait", False, {"seconds": 60})
    v, _, _, wait = parse_judge_response('{"verdict": "wait", "wait_for_seconds": 99999}')
    assert wait == {"seconds": MAX_WAIT_SECONDS}
    v, _, _, wait = parse_judge_response('{"verdict": "wait", "wait_on_pid": 42}')
    assert v == "wait" and wait["seconds"] > 0  # no pid registry: parks on the clock
    v, _, _, wait = parse_judge_response('{"verdict": "wait", "wait_on_session": "s1"}')
    assert v == "wait" and wait["seconds"] > 0
    # A wait with no target downgrades to continue.
    v, reason, pf, wait = parse_judge_response('{"verdict": "wait", "reason": "hmm"}')
    assert v == "continue" and wait is None and not pf and "no target" in reason
    # Legacy shape: {"done": bool}, string or real bool.
    assert parse_judge_response('{"done": true, "reason": "finished"}')[0] == "done"
    assert parse_judge_response('{"done": "yes", "reason": "finished"}')[0] == "done"
    assert parse_judge_response('{"done": false, "reason": "more to do"}')[0] == "continue"
    # Fenced JSON still parses.
    assert parse_judge_response('```json\n{"verdict": "done", "reason": "ok"}\n```')[0] == "done"
    # Unknown verdict and garbage fail open to continue.
    assert parse_judge_response('{"verdict": "banana", "reason": "?"}')[0] == "continue"
    v, reason, pf, _ = parse_judge_response("the agent seems done to me")
    assert v == "continue" and pf and "not JSON" in reason
    v, _, pf, _ = parse_judge_response("")
    assert v == "continue" and pf


# ── evidence truncation (goals.py 39, 928-929) ───────────────────────


def test_evidence_is_truncated():
    plan = {"steps": [{"id": "1", "text": "x" * 50, "state": "done"}]}
    prompt = build_user_prompt("G" * 3000, "R" * 5000, plan, 0, current_time="now")
    assert "G" * 2000 in prompt and "G" * 2001 not in prompt
    assert "R" * 4000 in prompt and "R" * 4001 not in prompt
    assert prompt.count("… [truncated]") == 2
    assert "Background workers running right now: 0" in prompt
    assert "Current time: now" in prompt
    assert "x" * 50 in prompt  # the plan block carries the step text


# ── judge health (goals.py 43-46, 1520-1559) ─────────────────────────


def test_health_counters_pause_and_reset():
    plan = {}
    assert apply_health(plan, True, False) == ""
    assert apply_health(plan, True, False) == ""
    line = apply_health(plan, True, False)
    assert "judge" in line and "3 times" in line  # three parse failures in a row
    assert plan["judge_parse_fails"] == 3
    # Any usable reply resets both counters.
    assert apply_health(plan, False, False) == ""
    assert plan["judge_parse_fails"] == 0 and plan["judge_transport_fails"] == 0
    plan = {}
    for _ in range(4):
        assert apply_health(plan, False, True) == ""
    line = apply_health(plan, False, True)
    assert "connection" in line and "5 times" in line  # five transport failures
    # A transport failure resets the parse counter and vice versa.
    plan = {"judge_parse_fails": 2}
    apply_health(plan, False, True)
    assert plan["judge_parse_fails"] == 0 and plan["judge_transport_fails"] == 1
    plan = {"judge_transport_fails": 4}
    apply_health(plan, True, False)
    assert plan["judge_transport_fails"] == 0 and plan["judge_parse_fails"] == 1


def test_judge_call_uses_the_connection_path(tmp_path):
    """The judge rides llm.complete with temperature 0, 4096 tokens, 30 s."""
    from easyagent.store import Store

    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="m")
    seen = {}

    async def fake_complete(**kwargs):
        seen.update(kwargs)
        return '{"verdict": "done", "reason": "the file exists"}'

    import easyagent.llm as llm_mod

    original = llm_mod.complete
    llm_mod.complete = fake_complete
    try:
        out = asyncio.run(
            goaljudge.call_judge(
                store,
                bot["id"],
                goal="send the summary",
                answer="I sent it.",
                plan={"steps": []},
                children=0,
            )
        )
    finally:
        llm_mod.complete = original
    assert out[0] == "done" and out[1] == "the file exists"
    assert seen["temperature"] == 0
    assert seen["max_tokens"] == 4096
    assert seen["timeout"] == 30.0
    assert seen["messages"][0]["content"] == goaljudge.JUDGE_SYSTEM_PROMPT
    assert "send the summary" in seen["messages"][1]["content"]


# ── wiring: the judge replaces the digest witness ────────────────────


class Recorder:
    """Fake model at the llm.complete seam, same tape pattern as test_keepgoing."""

    def __init__(self, tape):
        self.tape = list(tape)
        self.seen = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.seen.append(messages)
        if self.tape:
            reply = self.tape.pop(0)
            return reply(messages) if callable(reply) else reply
        return "Nothing else is owed."


def fake_judge(tape):
    """Stand-in for goaljudge.call_judge: pops (verdict, reason, wait) tuples."""
    calls = []

    async def _call(store, bot_id, *, goal, answer, plan, children):
        calls.append({"goal": goal, "answer": answer, "plan": plan, "children": children})
        if not tape:
            return "done", "nothing else is owed", None, False, False
        verdict, reason, wait = tape.pop(0)
        return verdict, reason, False, wait, False

    _call.calls = calls
    return _call


async def _settle(store, chat_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not turn_mod.slot_for(store, chat_id).busy:
            await asyncio.sleep(0.05)
            if not turn_mod.slot_for(store, chat_id).busy:
                return
        await asyncio.sleep(0.05)
    raise AssertionError("the chat never went idle")


async def _world(tmp_path, monkeypatch, tape, judge_tape, *, judge=True):
    """A chat with a 3-step plan (step 1 done), keep-going on, judge as asked."""
    turn_mod.renew_cancel()
    recorder = Recorder(tape)
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    if judge_tape is not None:
        monkeypatch.setattr("easyagent.goaljudge.call_judge", fake_judge(judge_tape))
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
            ToolRequest(
                kind="plan", action="set", body="Notes\n- Gather sources\n- Write the summary\n- Send it"
            ),
            bot["id"],
        )
        await execute(store, ToolRequest(kind="plan", action="step", path="1", command="done"), bot["id"])
        await execute(store, ToolRequest(kind="plan", action="keep", command="on"), bot["id"])
        if judge:
            await execute(store, ToolRequest(kind="plan", action="judge", command="on"), bot["id"])
        turn_mod.slot_for(store, chat["id"]).worker = None
        sent = await client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages", json={"content": "do the job"}
        )
        assert sent.status_code == 200, sent.text
        await _settle(store, chat["id"])
        return store, bot, chat, recorder


def _lines(store, bot_id, chat_id):
    chat = store.get_chat(bot_id, chat_id)
    return [str(item.get("content") or "") for item in chat.get("messages") or []]


def _user_lines(store, bot_id, chat_id):
    chat = store.get_chat(bot_id, chat_id)
    return [
        str(item.get("content") or "")
        for item in chat.get("messages") or []
        if item.get("role") == "user"
    ]


def test_judge_done_closes_the_run(tmp_path, monkeypatch):
    """done closes with the achieved line; no continuation runs."""

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH], [("done", "the summary was sent", None)]
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert any("Goal done: the summary was sent" in line for line in lines)
        assert not any(line.startswith("Keep going") for line in _user_lines(store, bot["id"], chat["id"]))
        assert len(recorder.seen) == 1  # only the first turn ran
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is False
        # The judge saw the goal: the person's task line, captured at the decision.
        assert plan.get("goal") == "do the job"

    asyncio.run(scenario())


def test_judge_blocked_pauses_without_calling_it_done(tmp_path, monkeypatch):
    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH], [("blocked", "the file path is unknown", None)]
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert any("Stuck: the file path is unknown" in line for line in lines)
        assert not any("Goal done" in line for line in lines)
        assert not any(line.startswith("Keep going") for line in _user_lines(store, bot["id"], chat["id"]))
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is False  # paused, not done
        assert len(recorder.seen) == 1

    asyncio.run(scenario())


def test_judge_continue_runs_one_more_go_with_the_reason(tmp_path, monkeypatch):
    async def scenario():
        progress = (
            "```plan\nstep\n2\ndone\n```\nStep 2 is done.\n\n"
            "```finish\nproven\nFinished step 2.\n```"
        )
        store, bot, chat, recorder = await _world(
            tmp_path,
            monkeypatch,
            [FINISH, progress],
            [
                ("continue", "write the summary next", None),
                ("done", "it is all sent", None),
            ],
        )
        nudges = [line for line in _user_lines(store, bot["id"], chat["id"]) if line.startswith("Keep going")]
        assert nudges == [f"Keep going 1/{KEEP_GOING_HARD_CAP}: write the summary next"]
        lines = _lines(store, bot["id"], chat["id"])
        assert any("Goal done: it is all sent" in line for line in lines)
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["continued"] == 1  # only the continue verdict spent a go
        assert plan["keep_going"] is False

    asyncio.run(scenario())


def test_judge_wait_parks_without_burning_budget(tmp_path, monkeypatch):
    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path,
            monkeypatch,
            [FINISH],
            [("wait", "the worker is still running", {"seconds": 1800})],
        )
        assert not any(line.startswith("Keep going") for line in _user_lines(store, bot["id"], chat["id"]))
        assert len(recorder.seen) == 1
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is True  # parked, not stopped
        assert int(plan.get("continued") or 0) == 0  # the wait spent no go
        slot = turn_mod.slot_for(store, chat["id"])
        task = getattr(slot, "judge_wait_task", None)
        assert task is not None and not task.done()
        task.cancel()

    asyncio.run(scenario())


def test_judge_off_keeps_the_digest_witness(tmp_path, monkeypatch):
    """With the judge off the deterministic path is untouched: the digest mark
    is stamped and the two-no-progress stall still applies."""
    from easyagent import plan as plan_mod

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH, FINISH, FINISH, "Idle."], None, judge=False
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert any("two tries in a row made no progress" in line for line in lines)
        plan = load_plan(store, bot["id"], chat["id"])
        assert not plan.get("judge")
        assert plan["keep_going_mark"] == plan_mod.plan_digest(plan)

    asyncio.run(scenario())


def test_judge_fence_parses_and_arms(tmp_path):
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

    request = parse_tool("```plan\njudge\non\n```")
    assert request is not None and request.kind == "plan" and request.action == "judge"
    assert request.command == "on"
    assert parse_tool("```plan\njudge off\n```").command == "off"

    asyncio.run(execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Write the summary\n- Send it"),
        bot["id"],
    ))
    # The person's task line is in the chat: arming the judge captures it.
    full = store.get_chat(bot["id"], chat["id"])
    full.setdefault("messages", []).append({"role": "user", "content": "Write me a summary of Q3"})
    store.save_chat(full)
    said = asyncio.run(execute(store, ToolRequest(kind="plan", action="judge", command="on"), bot["id"]))
    plan = load_plan(store, bot["id"], chat["id"])
    assert plan["judge"] is True
    assert plan["keep_going"] is True  # the judge rides the keep-going engine
    assert plan["goal"] == "Write me a summary of Q3"
    assert "judge is on" in said
    asyncio.run(execute(store, ToolRequest(kind="plan", action="judge", command="off"), bot["id"]))
    assert load_plan(store, bot["id"], chat["id"])["judge"] is False
    app.close()


def test_resume_after_a_judge_pause_resets_the_counters(tmp_path, monkeypatch):
    """Sending continue after a judge pause resets the budget and the health."""

    async def scenario():
        store, bot, chat, recorder = await _world(
            tmp_path, monkeypatch, [FINISH], [("blocked", "needs the path", None)]
        )
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is False
        plan["continued"] = 3
        plan["judge_parse_fails"] = 2
        from easyagent.plan import _write_plan

        _write_plan(store, bot["id"], chat["id"], plan)
        set_keep_going(store, bot["id"], chat["id"], on=True)
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is True
        assert plan["continued"] == 0
        assert int(plan.get("judge_parse_fails") or 0) == 0
        assert int(plan.get("judge_transport_fails") or 0) == 0

    asyncio.run(scenario())
