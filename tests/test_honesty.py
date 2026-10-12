"""Honesty checks use a scripted fake model. No live endpoint."""

import asyncio
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.honesty import run_scripted
from easyagent.store import Store


def _turn(*args, **kwargs):
    return asyncio.run(run_scripted(*args, **kwargs))


def _ok(_call):
    return "1 passed\nexit code 0"


def _fail(_call):
    return "FAILED\nexit code 1"


def test_a_backed_claim_keeps_a_receipt_and_an_unbacked_claim_is_unverified():
    backed = _turn(
        ['TOOL shell command="pytest -q"', "Done."],
        "run it",
        _ok,
    )
    assert backed["unverified"] is False
    assert backed["model_calls"] == 0
    assert backed["receipts"][0]["claim"] == "done"
    assert "1 passed" in backed["receipts"][0]["output"]
    assert "pytest -q" in backed["receipts"][0]["tool"]

    bare = _turn(["Fixed."], "repair it", _ok)
    assert bare["unverified"] is True
    assert bare["receipts"][0]["claim"] == "fixed"
    assert bare["receipts"][0]["output"] == ""
    assert bare["text"] == "Fixed."

    hedged = _turn(["This should fix it. It should work now."], "repair it", _ok)
    assert "this should fix it" not in hedged["text"].lower()
    assert "should work now" not in hedged["text"].lower()
    assert hedged["text"].count("This is not verified.") == 2
    assert hedged["unverified"] is True

    negated = _turn(["It is not done."], "status", _ok)
    assert negated["unverified"] is False
    assert negated["receipts"] == []

    off = _turn(["Fixed."], "repair it", _ok, settings={"receipts": False})
    assert off["unverified"] is False
    assert off["text"] == "Fixed."


def test_a_verification_step_uses_one_model_reply_after_the_change():
    ran = []

    def run(call):
        ran.append(call.command or call.action)
        if call.kind == "files":
            return "wrote"
        return "1 passed\nexit code 0"

    result = _turn(
        [
            'TOOL files action=write path=/tmp/ea-honesty.txt body=hi',
            "Fixed.",
            'TOOL shell command="pytest -q"',
        ],
        "repair it",
        run,
    )
    assert result["model_calls"] == 1
    assert result["unverified"] is False
    assert result["receipts"][0]["claim"] == "fixed"
    assert "1 passed" in result["receipts"][0]["output"]
    assert ran == ["write", "pytest -q"]


def test_pushback_drops_the_previous_explanation_and_blocks_edits_until_a_failure_is_reproduced():
    seen = []

    def run(call):
        seen.append((call.kind, call.body))
        if call.kind == "shell":
            return "FAILED test_one\nexit code 1"
        return "wrote"

    result = _turn(
        [
            'TOOL files action=write path=/tmp/ea-honesty.py body=one',
            'TOOL shell command="pytest -q"',
            'Cause: FAILED test_one shows the failure.\nTOOL files action=write path=/tmp/ea-honesty.py body=two',
            "- the fixture is stale\n- my last change caused it",
        ],
        "it's still broken",
        run,
        prior=[
            {"role": "system", "content": "You are Ada."},
            {"role": "user", "content": "fix the test"},
            {"role": "assistant", "content": "I fixed it by editing the helper."},
        ],
    )
    joined = "\n".join(item.get("content") or "" for item in result["context"])
    assert "You are Ada." in joined
    assert "it's still broken" in joined
    assert "I fixed it by editing the helper." not in joined
    assert result["investigate"] is True
    assert result["model_calls"] == 0
    assert "Investigation first" in result["outputs"][0]
    assert seen == [("shell", ""), ("files", "two")]
    assert "Investigation is incomplete" not in result["text"]


def test_an_ambiguous_complaint_spends_the_only_model_call():
    ran = {"n": 0}

    def run(_call):
        ran["n"] += 1
        return "1 passed\nexit code 0"

    pushed = _turn(
        ['{"pushback": true}', "Fixed.", 'TOOL shell command="pytest -q"'],
        "no",
        run,
    )
    assert pushed["investigate"] is True
    assert pushed["model_calls"] == 1
    assert pushed["unverified"] is True
    assert ran["n"] == 1
    assert "Investigation is incomplete" not in pushed["text"]
    assert pushed["text"] == "Fixed."
    assert "Two causes" in pushed["chips"]
    assert "Run the check" in pushed["chips"]

    clear = _turn(
        ["Acknowledged."],
        "please add a readme for the project tomorrow",
        run,
    )
    assert clear["investigate"] is False
    assert clear["model_calls"] == 0
    assert clear["text"] == "Acknowledged."

    denied = _turn(
        ['{"pushback": false}', "Acknowledged."],
        "wrong",
        run,
    )
    assert denied["investigate"] is False
    assert denied["model_calls"] == 1
    assert denied["text"] == "Acknowledged."


def test_two_failed_fixes_block_the_next_edit_and_the_reply_says_so():
    writes = []

    def run(call):
        if call.kind == "files":
            writes.append(call.body)
        return "FAILED\nexit code 1"

    result = _turn(
        [
            'TOOL shell command="pytest -q"',
            "The cause is exit code 1 in the output.\nTOOL files action=write path=/tmp/ea-honesty.py body=one",
            "The cause is exit code 1 in the output.\nTOOL files action=write path=/tmp/ea-honesty.py body=two",
            "TOOL files action=write path=/tmp/ea-honesty.py body=three",
            "- the fixture is stale\n- my last change caused it",
        ],
        "it's still broken",
        run,
    )
    assert writes == ["one", "two"]
    assert "Switch approach" in result["outputs"][-1]
    assert "Switching approach" not in result["text"]
    assert "Switch approach" in result["chips"]


def test_an_apology_without_a_new_action_is_flagged():
    bare = _turn(["Sorry, my mistake."], "hello", _ok)
    assert "An apology needs a new action." in bare["text"]
    acted = _turn(
        ['TOOL shell command="pytest -q"', "Sorry, my mistake."],
        "hello",
        _ok,
    )
    assert "An apology needs a new action." not in acted["text"]


def test_a_pre_existing_claim_reruns_the_failing_command_on_the_snapshot(tmp_path):
    from easyagent import turn as turn_mod
    from easyagent.workspace import bot_workspace

    store, bot = _bot(tmp_path)
    work = bot_workspace(store, bot["id"], create=True)
    target = work / "note.txt"
    target.write_text("old", encoding="utf-8")
    script = work / "check_old.py"
    script.write_text(
        "import pathlib, sys\n"
        f"text = pathlib.Path({str(target)!r}).read_text(encoding='utf-8')\n"
        "sys.exit(0 if text == 'old' else 1)\n",
        encoding="utf-8",
    )
    command = f"{Path(sys.executable).name} {script.name}"

    def run(call):
        if call.kind == "files":
            Path(call.path).write_text(call.body, encoding="utf-8")
            return "wrote"
        return "exit code 1"

    async def go():
        slot = turn_mod.bind(store, "honesty-snapshot", bot["id"])
        slot.cwd = str(work)
        return await run_scripted(
            [
                f'TOOL files action=write path="{target}" body=new',
                f'TOOL shell command="{command}"',
                "The failure is pre-existing.",
            ],
            "why did that fail",
            run,
            store=store,
            bot_id=bot["id"],
        )

    result = asyncio.run(go())
    assert "Checked the state before this turn's changes:" in result["text"]
    assert "exit code 0" in result["text"]
    assert target.read_text(encoding="utf-8") == "new"


def test_a_command_that_did_not_run_is_not_rerun(tmp_path):
    from easyagent import honesty, turn as turn_mod
    from easyagent.tools import ToolError, ToolRequest, execute
    from easyagent.workspace import bot_workspace

    store, bot = _bot(tmp_path)
    work = bot_workspace(store, bot["id"], create=True)
    target = work / "note.txt"
    marker = work / "PWNED"

    async def go():
        honesty.begin(store, bot["id"])
        slot = turn_mod.bind(store, "honesty-block", bot["id"])
        slot.cwd = str(work)
        await execute(store, ToolRequest(kind="files", action="write", path=str(target), body="new"), bot["id"])
        command = f"{sys.executable} -c \"open(r'{marker}', 'w').write('x')\""
        try:
            await execute(store, ToolRequest(kind="shell", command=command), bot["id"])
        except ToolError:
            pass
        outcome = await honesty.settle("The failure is pre-existing.")
        return outcome.text

    text = asyncio.run(go())
    assert not marker.exists()
    assert "Checked the state before this turn's changes:" not in text


def test_a_third_identical_failure_is_blocked():
    ran = {"n": 0}

    def run(_call):
        ran["n"] += 1
        return "boom\nexit code 1"

    result = _turn(
        [
            'TOOL shell command="pytest -q"',
            'TOOL shell command="pytest -q"',
            'TOOL shell command="pytest -q"',
            "I stopped.",
        ],
        "run the tests",
        run,
    )
    assert ran["n"] == 2
    assert "Change approach" in result["outputs"][-1]
    assert result["text"] == "I stopped."


def _note(store: Store, bot_id: str, name: str, text: str) -> None:
    folder = store._bot_dir(bot_id) / "notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def _bot(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="kiln", base_url="http://127.0.0.1:9/v1", api_key="k", model="m")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="m")
    return store, bot


def test_a_matching_mistake_is_injected_and_its_check_can_block(tmp_path):
    store, bot = _bot(tmp_path)
    _note(
        store,
        bot["id"],
        "MISTAKES.md",
        "\n".join(
            [
                "<!-- ea:auto -->",
                "<!-- ea:entry id=m1 kind=mistake cites=none dates=2026-01-01 -->",
                "## Do not wipe the tree",
                "A wipe is not a fix.",
                "trigger: shell",
                "pattern: rm -rf",
                "check: block",
                "<!-- /ea:entry -->",
                "<!-- ea:entry id=m2 kind=mistake cites=none dates=2026-01-01 -->",
                "## Breakfast is optional",
                "trigger: shell",
                "pattern: brunch",
                "check: block",
                "<!-- /ea:entry -->",
                "<!-- ea:entry id=m3 kind=mistake cites=none dates=2026-01-01 -->",
                "## Read the log first",
                "Look at the failure before editing.",
                "trigger: shell",
                "pattern: pytest",
                "check: ok",
                "<!-- /ea:entry -->",
                "<!-- /ea:auto -->",
                "",
            ]
        ),
    )
    ran = []

    def run(call):
        ran.append(call.command)
        return "ok\nexit code 0"

    result = _turn(
        [
            'TOOL shell command="rm -rf /tmp/scratch"',
            'TOOL shell command="pytest -q"',
            "Stopped.",
        ],
        "clean the tree",
        run,
        store=store,
        bot_id=bot["id"],
    )
    assert ran == ["pytest -q"]
    assert "Do not wipe the tree" in result["outputs"][0]
    assert "The check failed, so this was not run." in result["outputs"][0]
    assert "Breakfast" not in result["outputs"][0]
    assert result["outputs"][1].startswith("Lesson: Read the log first.")
    assert "Breakfast" not in result["outputs"][1]
    assert "Do not wipe" not in result["outputs"][1]


def test_a_stall_or_an_open_promise_posts_a_blocker(tmp_path):
    stalled = _turn([" "], "status", _ok, jump_seconds=600)
    assert stalled["text"] == "Blocked: no progress for 5 minutes."
    quiet = _turn([" "], "status", _ok, jump_seconds=600, settings={"stall": False})
    assert "Blocked:" not in quiet["text"]

    store, bot = _bot(tmp_path)
    _note(
        store,
        bot["id"],
        "PROMISES.md",
        "\n".join(
            [
                "<!-- ea:auto -->",
                "<!-- ea:entry id=p1 kind=promise cites=none dates=2026-01-01 status=open -->",
                "## Ship the notes",
                "Still open.",
                "<!-- /ea:entry -->",
                "<!-- ea:entry id=p2 kind=promise cites=none dates=2026-01-01 status=closed -->",
                "## Already finished",
                "Done earlier.",
                "<!-- /ea:entry -->",
                "<!-- /ea:auto -->",
                "",
            ]
        ),
    )
    result = _turn(["Acknowledged."], "hello", _ok, store=store, bot_id=bot["id"])
    assert "Blocked: still open — Ship the notes." in result["text"]
    assert "Already finished" not in result["text"]
    assert result["unverified"] is False


def test_honesty_settings_default_on_and_round_trip(tmp_path):
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    got = client.get(f"/api/bots/{bot['id']}/honesty")
    assert got.status_code == 200, got.text
    body = got.json()
    for key in ("receipts", "pushback", "excuse", "loop", "tripwires", "stall", "debug"):
        assert body[key] is True
    assert body["stall_minutes"] == 5
    saved = client.post(
        f"/api/bots/{bot['id']}/honesty",
        json={"loop": False, "stall_minutes": 2},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["loop"] is False
    assert saved.json()["receipts"] is True
    assert saved.json()["stall_minutes"] == 2
    again = client.get(f"/api/bots/{bot['id']}/honesty").json()
    assert again["loop"] is False
    assert again["pushback"] is True
    assert again["stall_minutes"] == 2


def test_a_bug_report_holds_a_fix_until_a_failing_command_and_a_cited_cause():
    ran = []

    def run(call):
        ran.append(call.kind)
        if call.kind == "shell":
            return "check.py:4 FAILED add\nexit code 1"
        return "wrote"

    held = _turn(
        [
            "TOOL files action=write path=/tmp/ea-debug.py body=return a + b",
            "I'll change the helper so the sum is right.",
        ],
        "There is a bug. The check fails.",
        run,
    )
    assert held["debug"] is True
    assert ran == []
    assert "reproduce the failure" in held["outputs"][0]
    assert "Debug:" not in held["text"]
    assert held["text"] == "I'll change the helper so the sum is right."
    assert "Reproduce first" in held["chips"]

    bare = _turn(
        [
            'TOOL shell command="python check.py"',
            "TOOL files action=write path=/tmp/ea-debug.py body=return-a-plus-b-now",
        ],
        "There is a bug. The check fails.",
        lambda _call: "check.py:4 FAILED add\nexit code 1",
    )
    assert "cite a file:line" in bare["outputs"][-1]


def test_debug_proof_needs_the_same_command_to_pass_after_the_fix():
    def run(call):
        if call.kind == "files":
            return "wrote"
        if call.command == "python check.py" and run.passed:
            return "ok\nexit code 0"
        run.passed = True
        return "check.py:4 FAILED add\nexit code 1"

    run.passed = False
    missing = _turn(
        [
            'TOOL shell command="python check.py"',
            "The cause is check.py:4 in the failing check.\nTOOL files action=write path=/tmp/ea-debug.py body=return a + b",
            "Fixed.",
            'TOOL shell command="echo ok"',
        ],
        "There is a bug. The check fails.",
        run,
    )
    assert missing["unverified"] is True
    assert missing["debug"] is True

    run.passed = False

    def prove(call):
        if call.kind == "files":
            return "wrote"
        if prove.seen:
            return "ok\nexit code 0"
        prove.seen = True
        return "check.py:4 FAILED add\nexit code 1"

    prove.seen = False
    shown = _turn(
        [
            'TOOL shell command="python check.py"',
            "The cause is check.py:4 in the failing check.\nTOOL files action=write path=/tmp/ea-debug.py body=return a + b",
            'TOOL shell command="python check.py"',
            "Fixed.",
        ],
        "There is a bug. The check fails.",
        prove,
    )
    assert shown["unverified"] is False
    assert shown["receipts"][0]["claim"] == "fixed"
    assert "exit code 0" in shown["receipts"][0]["output"]


def test_pushback_refuses_the_same_fix_and_accepts_a_new_cause():
    ran = []

    def run(call):
        ran.append(call.body or call.command)
        if call.kind == "shell":
            return "check.py:4 FAILED add\nexit code 1"
        return "wrote\nexit code 0"

    old = "return a - b exactly"
    result = _turn(
        [
            'TOOL shell command="python check.py"',
            'TOOL files action=write path=/tmp/ea-debug.py body="return a - b exactly"',
            "The cause is check.py:4 in the failing check.\nTOOL files action=write path=/tmp/ea-debug.py body=return-a-plus-b-now",
            "- the subtraction is the bug\n- my last change caused it",
        ],
        "it's still broken",
        run,
        prior=[
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "I changed it to return a - b exactly."},
        ],
    )
    assert result["debug"] is True
    assert result["investigate"] is True
    assert "repeats the last fix" in result["outputs"][1]
    assert ran == ["python check.py", "return-a-plus-b-now"]
    assert "Investigation is incomplete" not in result["text"]
    assert "Debug:" not in result["text"]


def test_a_promise_with_no_tool_asks_once_to_run_the_check():
    ran = []

    def run(call):
        ran.append(call.command or call.kind)
        return "FAILED\nexit code 1"

    result = _turn(
        [
            "I'll fix the helper.",
            "I'll fix the helper again.",
            'TOOL shell command="python check.py"',
        ],
        "There is a bug. The check fails.",
        run,
    )
    assert ran == []
    assert result["text"] == "I'll fix the helper again."
    assert "Debug:" not in result["text"]
    assert result["hidden"] == [{"role": "user", "content": "Run the failing test or command now."}]
    assert "Run the check" in result["chips"]
    assert "Run the failing test" not in result["text"]
