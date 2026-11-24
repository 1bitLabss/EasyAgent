"""Quality gates: deterministic shell checks that run before the goal judge.

Ported from Hermes goals.py (51-57, 97-111, 343-414, 1267-1359). Gates live on
the plan (plan["gates"]), are managed with the plan fence (gate add / remove /
clear / list), and run at every judge boundary BEFORE the judge: the first
failing gate short-circuits the rest of the gates and the judge entirely, and
its bounded output tail (last 3000 chars, "(no output)" when empty) becomes the
continuation prompt. Retries are across turns: each failing boundary counts one
attempt; past max_retries the run pauses. A gate-failed continuation still
counts against the 40-continuation backstop; hitting the backstop while a gate
fails notes it on the paused line. gate add goes through the shell guard;
remove, clear, and list stay free.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from easyagent import qualitygate
from easyagent import turn as turn_mod
from easyagent.app import create_app
from easyagent.features import reset_routine_turn
from easyagent.plan import KEEP_GOING_HARD_CAP, load_plan, request_from_fence
from easyagent.tools import ToolRequest, execute, parse_tools

FINISH = "The first step is done.\n\n```finish\nproven\nGathered the sources.\n```"


# ── fence grammar ─────────────────────────────────────────────────────


def test_gate_fence_verbs():
    req = request_from_fence("gate add pytest -q")
    assert (req.kind, req.action, req.command) == ("plan", "gate", "add pytest -q")
    # The command keeps its case: only the verb is read from the lowered line.
    req = request_from_fence("gate add Run-Tests.ps1 --Strict")
    assert req.command == "add Run-Tests.ps1 --Strict"
    req = request_from_fence("gate add\npytest -q")
    assert req.command == "add pytest -q"
    assert request_from_fence("gate remove 2").command == "remove 2"
    assert request_from_fence("gate clear").command == "clear"
    assert request_from_fence("gate list").command == "list"


def test_gate_parsed_from_tools_fence():
    calls = parse_tools("Watching it.\n\n```plan\ngate add pytest -q\n```")
    assert len(calls) == 1
    assert (calls[0].kind, calls[0].action, calls[0].command) == ("plan", "gate", "add pytest -q")


def test_gate_add_command_helper():
    from easyagent.plan import gate_add_command

    assert gate_add_command(ToolRequest(kind="plan", action="gate", command="add pytest -q")) == "pytest -q"
    assert gate_add_command(ToolRequest(kind="plan", action="gate", command="remove 1")) == ""
    assert gate_add_command(ToolRequest(kind="plan", action="gate", command="list")) == ""
    assert gate_add_command(ToolRequest(kind="plan", action="judge", command="on")) == ""
    assert gate_add_command(ToolRequest(kind="shell", command="pytest")) == ""


# ── gate records on the plan dict ─────────────────────────────────────


def test_gate_record_shape_and_edits():
    plan = {}
    gate = qualitygate.add_gate(plan, "pytest -q")
    assert gate == {
        "command": "pytest -q",
        "timeout_seconds": 300,
        "max_retries": 3,
        "attempts": 0,
        "last_exit_code": None,
    }
    assert plan["gates"] == [gate]
    assert qualitygate.remove_gate(plan, 1) == "pytest -q"
    assert plan["gates"] == []
    qualitygate.add_gate(plan, "a")
    qualitygate.add_gate(plan, "b")
    assert qualitygate.clear_gates(plan) == 2
    assert plan["gates"] == []


def test_render_gates_status():
    plan = {"gates": [
        {"command": "pytest", "attempts": 0, "max_retries": 3, "last_exit_code": 0},
        {"command": "mypy .", "attempts": 2, "max_retries": 3, "last_exit_code": 1},
        {"command": "ruff check .", "attempts": 0, "max_retries": 3, "last_exit_code": None},
    ]}
    out = qualitygate.render_gates(plan)
    assert "- 1. $ pytest ✓ passing" in out
    assert "- 2. $ mypy . ✗ failing (exit 1, attempt 2/3)" in out
    assert out.endswith("- 3. $ ruff check .")
    assert "no quality gates" in qualitygate.render_gates({"gates": []})


# ── execution: order, short-circuit, attempts across turns ───────────


def fake_runner(script):
    """Stand-in for qualitygate.run_gate: pops (passed, exit_code, tail) per call."""
    calls = []

    def _run(gate, *, cwd=None):
        calls.append({"command": gate.get("command"), "cwd": cwd})
        if script:
            return script.pop(0)
        return True, 0, ""

    _run.calls = calls
    return _run


def test_first_failing_gate_short_circuits_the_rest():
    plan = {"gates": []}
    qualitygate.add_gate(plan, "one")
    qualitygate.add_gate(plan, "two")
    qualitygate.add_gate(plan, "three")
    runner = fake_runner([(True, 0, "ok"), (False, 1, "boom")])
    original = qualitygate.run_gate
    qualitygate.run_gate = runner
    try:
        decision = qualitygate.check_gates(plan, goal="ship it", cwd="C:/work")
    finally:
        qualitygate.run_gate = original
    # gate one ran and passed, gate two failed, gate three never ran.
    assert [c["command"] for c in runner.calls] == ["one", "two"]
    assert runner.calls[0]["cwd"] == "C:/work"
    assert decision["kind"] == "failed"
    assert decision["command"] == "two" and decision["exit_code"] == 1
    assert decision["attempts"] == 1 and decision["max_retries"] == 3
    # A passing gate resets its attempts; the failing gate keeps its count.
    assert plan["gates"][0]["attempts"] == 0
    assert plan["gates"][1]["attempts"] == 1
    # The prompt carries the Hermes copy: goal, command, exit, output tail.
    prompt = decision["prompt"]
    assert prompt.startswith("[Continuing toward your standing goal — a quality gate failed]")
    assert "Goal: ship it" in prompt
    assert "  $ two" in prompt and "Exit code: 1" in prompt
    assert "attempt 1/3" in prompt and "boom" in prompt
    assert "Fix the underlying problem so this gate passes" in prompt
    assert "Do not declare the goal complete while any gate fails" in prompt


def test_a_passing_gate_resets_attempts():
    plan = {"gates": []}
    qualitygate.add_gate(plan, "pytest")
    plan["gates"][0]["attempts"] = 2
    runner = fake_runner([(True, 0, "")])
    original = qualitygate.run_gate
    qualitygate.run_gate = runner
    try:
        assert qualitygate.check_gates(plan) is None
    finally:
        qualitygate.run_gate = original
    assert plan["gates"][0]["attempts"] == 0


def test_attempts_count_across_turns_then_pause():
    """Each failing boundary counts one attempt; the retry cap pauses the run."""
    plan = {"gates": []}
    qualitygate.add_gate(plan, "pytest")
    original = qualitygate.run_gate
    qualitygate.run_gate = fake_runner([(False, 7, "red")] * 4)
    try:
        for attempt in (1, 2, 3):
            decision = qualitygate.check_gates(plan)
            assert decision["kind"] == "failed"
            assert decision["attempts"] == attempt
            assert plan["gates"][0]["attempts"] == attempt
        decision = qualitygate.check_gates(plan)  # attempt 4 > 3 retries
    finally:
        qualitygate.run_gate = original
    assert decision["kind"] == "exhausted"
    assert decision["pause_line"] == (
        "Stuck: quality gate still failing after 3 retries: $ pytest (exit 7). "
        "Fix it or remove the gate, then send continue."
    )


def test_no_gates_is_no_decision():
    assert qualitygate.check_gates({}) is None
    assert qualitygate.check_gates({"gates": []}) is None


# ── the shell runner: bounded tail, empty output, timeout ────────────


class FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_gate_output_tail_is_bounded(monkeypatch):
    import subprocess

    big = "x" * 5000
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: FakeProc(1, stdout=big, stderr="E" * 10)
    )
    passed, code, tail = qualitygate.run_gate({"command": "pytest", "timeout_seconds": 30})
    assert not passed and code == 1
    assert len(tail) == qualitygate.GATE_OUTPUT_TAIL_CHARS == 3000
    assert tail.endswith("E" * 10)  # stderr follows stdout in the combined tail


def test_empty_output_shows_no_output_in_prompt():
    plan = {"gates": []}
    qualitygate.add_gate(plan, "silent")
    original = qualitygate.run_gate
    qualitygate.run_gate = fake_runner([(False, 1, "")])
    try:
        decision = qualitygate.check_gates(plan, goal="g")
    finally:
        qualitygate.run_gate = original
    assert "(no output)" in decision["prompt"]


def test_run_gate_timeout_is_exit_minus_one(monkeypatch):
    import subprocess

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="slow", timeout=30)

    monkeypatch.setattr(subprocess, "run", _boom)
    passed, code, tail = qualitygate.run_gate({"command": "slow", "timeout_seconds": 30})
    assert (passed, code) == (False, -1)
    assert "timed out after 30s" in tail


# ── wiring: fence verbs through execute() ────────────────────────────


def _fresh_turn():
    turn_mod.renew_cancel()
    reset_routine_turn()


async def _store_bot_chat(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="m")
    chat = store.create_chat(bot["id"])
    turn_mod.bind(store, chat["id"], bot_id=bot["id"])
    await execute(
        store,
        ToolRequest(kind="plan", action="set", body="Notes\n- Gather sources\n- Write the summary"),
        bot["id"],
    )
    return store, bot, chat


def test_gate_fence_add_list_remove_clear(tmp_path, monkeypatch):
    async def scenario():
        _fresh_turn()
        store, bot, chat = await _store_bot_chat(tmp_path)
        import easyagent.safety as safety_mod

        async def passthrough(store_, request, bot_id):
            return request, None

        monkeypatch.setattr(safety_mod, "guard", passthrough)
        out = await execute(
            store, ToolRequest(kind="plan", action="gate", command="add pytest -q"), bot["id"]
        )
        assert "Gate added: $ pytest -q" in out
        assert "3 retries" in out and "300 seconds" in out
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["gates"][0]["command"] == "pytest -q"
        # Persisted with the plan: reload from disk.
        assert load_plan(store, bot["id"], chat["id"])["gates"][0]["attempts"] == 0
        out = await execute(store, ToolRequest(kind="plan", action="gate", command="list"), bot["id"])
        assert "- 1. $ pytest -q" in out
        out = await execute(store, ToolRequest(kind="plan", action="gate", command="remove 1"), bot["id"])
        assert out == "Gate removed: $ pytest -q."
        out = await execute(store, ToolRequest(kind="plan", action="gate", command="clear"), bot["id"])
        assert "no quality gates" in out.lower() or "Cleared" in out
        await execute(store, ToolRequest(kind="plan", action="gate", command="add a"), bot["id"])
        await execute(store, ToolRequest(kind="plan", action="gate", command="add b"), bot["id"])
        out = await execute(store, ToolRequest(kind="plan", action="gate", command="clear"), bot["id"])
        assert out == "Cleared 2 quality gates."

    asyncio.run(scenario())


def test_gate_add_passes_the_shell_guard(tmp_path):
    """gate add runs the shell later without approval, so the guard sees it now."""
    async def scenario():
        _fresh_turn()
        store, bot, chat = await _store_bot_chat(tmp_path)
        seen = []

        async def fake_guard(store_, request, bot_id):
            seen.append((request.kind, request.command))
            return request, None

        import easyagent.safety as safety_mod

        original = safety_mod.guard
        safety_mod.guard = fake_guard
        try:
            await execute(store, ToolRequest(kind="plan", action="gate", command="add pytest -q"), bot["id"])
            assert seen == [("shell", "pytest -q")]
            # remove, clear, and list change only the checklist: no guard.
            await execute(store, ToolRequest(kind="plan", action="gate", command="list"), bot["id"])
            await execute(store, ToolRequest(kind="plan", action="gate", command="remove 1"), bot["id"])
            await execute(store, ToolRequest(kind="plan", action="gate", command="clear"), bot["id"])
            assert len(seen) == 1
            # A guard refusal stops the add: the gate is not recorded.
            async def refuse(store_, request, bot_id):
                return request, "Not approved."

            safety_mod.guard = refuse
            out = await execute(store, ToolRequest(kind="plan", action="gate", command="add rm -rf /"), bot["id"])
            assert out == "Not approved."
            assert not load_plan(store, bot["id"], chat["id"]).get("gates")
        finally:
            safety_mod.guard = original

    asyncio.run(scenario())


# ── wiring: gates run in the judge path ──────────────────────────────


class Recorder:
    """Fake model at the llm.complete seam, same tape pattern as test_goaljudge."""

    def __init__(self, tape):
        self.tape = list(tape)
        self.seen = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120, **_kwargs):
        self.seen.append(messages)
        if self.tape:
            reply = self.tape.pop(0)
            return reply(messages) if callable(reply) else reply
        return "Nothing else is owed."


def fake_judge():
    """Stand-in for goaljudge.call_judge: records that the judge was asked."""
    calls = []

    async def _call(store, bot_id, *, goal, answer, plan, children):
        calls.append({"goal": goal})
        return "done", "nothing else is owed", None, False, False

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


async def _judge_world(tmp_path, monkeypatch, *, gate_script, continued=0, attempts=0):
    """A judge-armed chat with one gate, a scripted gate runner, and a spy judge."""
    turn_mod.renew_cancel()
    recorder = Recorder([FINISH, "Nothing else is owed."])
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    judge = fake_judge()
    monkeypatch.setattr("easyagent.goaljudge.call_judge", judge)
    runner = fake_runner(list(gate_script))
    monkeypatch.setattr("easyagent.qualitygate.run_gate", runner)

    async def passthrough(store_, request, bot_id):
        return request, None

    monkeypatch.setattr("easyagent.safety.guard", passthrough)
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
        await execute(
            store,
            ToolRequest(kind="plan", action="set", body="Notes\n- Gather sources\n- Write the summary\n- Send it"),
            bot["id"],
        )
        await execute(store, ToolRequest(kind="plan", action="step", path="1", command="done"), bot["id"])
        await execute(store, ToolRequest(kind="plan", action="keep", command="on"), bot["id"])
        await execute(store, ToolRequest(kind="plan", action="judge", command="on"), bot["id"])
        await execute(store, ToolRequest(kind="plan", action="gate", command="add pytest -q"), bot["id"])
        if continued or attempts:
            plan = load_plan(store, bot["id"], chat["id"])
            plan["continued"] = continued
            plan["gates"][0]["attempts"] = attempts
            from easyagent.plan import _write_plan

            _write_plan(store, bot["id"], chat["id"], plan)
        turn_mod.slot_for(store, chat["id"]).worker = None
        sent = await client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages", json={"content": "do the job"}
        )
        assert sent.status_code == 200, sent.text
        await _settle(store, chat["id"])
        return store, bot, chat, recorder, judge, runner


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


def test_failing_gate_skips_the_judge_and_drives_the_next_turn(tmp_path, monkeypatch):
    async def scenario():
        store, bot, chat, recorder, judge, runner = await _judge_world(
            tmp_path, monkeypatch, gate_script=[(False, 1, "1 failed")]
        )
        # The judge was skipped entirely: no call, no verdict.
        assert judge.calls == []
        assert [c["command"] for c in runner.calls] == ["pytest -q"]
        lines = _lines(store, bot["id"], chat["id"])
        assert any(
            "Quality gate failed (1/40 turns, attempt 1/3): $ pytest -q" in line for line in lines
        )
        # The gate's output tail became the continuation prompt, and it ran.
        nudges = [line for line in _user_lines(store, bot["id"], chat["id"]) if "quality gate failed" in line]
        assert nudges and "1 failed" in nudges[0] and "pytest -q" in nudges[0]
        assert not any(line.startswith("Keep going") for line in _user_lines(store, bot["id"], chat["id"]))
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["gates"][0]["attempts"] == 1
        assert plan["continued"] == 1  # the gate-failed continuation spent a go
        assert plan["keep_going"] is True

    asyncio.run(scenario())


def test_gate_fail_counts_against_the_backstop(tmp_path, monkeypatch):
    async def scenario():
        store, bot, chat, recorder, judge, runner = await _judge_world(
            tmp_path, monkeypatch, gate_script=[(False, 1, "red")], continued=KEEP_GOING_HARD_CAP - 1
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert any(
            "Stopped after 40 goes" in line and "(a quality gate is still failing)" in line
            for line in lines
        )
        assert judge.calls == []
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is False

    asyncio.run(scenario())


def test_gate_retry_exhaustion_pauses_the_run(tmp_path, monkeypatch):
    async def scenario():
        store, bot, chat, recorder, judge, runner = await _judge_world(
            tmp_path, monkeypatch, gate_script=[(False, 1, "still red")], attempts=3
        )
        lines = _lines(store, bot["id"], chat["id"])
        assert any(
            "Stuck: quality gate still failing after 3 retries: $ pytest -q (exit 1). "
            "Fix it or remove the gate, then send continue." in line
            for line in lines
        )
        assert judge.calls == []
        plan = load_plan(store, bot["id"], chat["id"])
        assert plan["keep_going"] is False

    asyncio.run(scenario())


def test_passing_gates_let_the_judge_run(tmp_path, monkeypatch):
    async def scenario():
        store, bot, chat, recorder, judge, runner = await _judge_world(
            tmp_path, monkeypatch, gate_script=[(True, 0, "all green")]
        )
        assert judge.calls  # the gate passed, so the judge got its turn
        assert judge.calls[0]["goal"] == "do the job"
        assert not any("Quality gate failed" in line for line in _lines(store, bot["id"], chat["id"]))

    asyncio.run(scenario())
