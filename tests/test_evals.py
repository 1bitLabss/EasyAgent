"""The eval runner scores tasks in a temp store. It does not write the saved data folder."""

import asyncio
import json
from pathlib import Path

from easyagent import gate
from easyagent.evals.__main__ import main
from easyagent.evals.runner import compare_results, run_suite
from easyagent.evals.tasks import load_tasks
from easyagent.judge import grade_text, grading_model


def test_there_are_at_least_thirty_tasks():
    tasks = load_tasks()
    assert len(tasks) >= 30
    ids = [item["id"] for item in tasks]
    assert len(ids) == len(set(ids))
    categories = {item["category"] for item in tasks}
    for name in ("search", "files", "shell", "page", "memory", "honesty", "question", "skill", "recall", "judge"):
        assert name in categories


def test_mock_run_passes_and_leaves_the_data_dir_alone(tmp_path):
    source = tmp_path / "saved"
    source.mkdir()
    (source / "endpoints.json").write_text("[]\n", encoding="utf-8")
    before = {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    report = run_suite(mock=True, data_dir=source, out_dir=tmp_path / "out")
    after = {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    assert after == before
    assert not (source / "bots").exists()
    assert report["summary"]["passed"] == report["summary"]["total"]
    assert report["summary"]["total"] >= 30
    assert report["judge_model"] == "mock"
    assert report["judge_prompt_version"] == "1"
    assert Path(report["path"]).is_file()
    saved = json.loads(Path(report["path"]).read_text(encoding="utf-8"))
    assert saved["summary"]["pass_rate"] == 1.0


def test_compare_flags_a_regression(tmp_path):
    report = run_suite(mock=True, task_ids=["quick-earth", "write-exact"], out_dir=tmp_path)
    earlier = json.loads(Path(report["path"]).read_text(encoding="utf-8"))
    later = json.loads(json.dumps(earlier))
    later["tasks"][0]["passed"] = False
    later["summary"]["passed"] = 1
    diff = compare_results(earlier, later)
    assert diff["regressions"] == [later["tasks"][0]["id"]]
    assert diff["fixed"] == []


def test_unknown_task_is_an_error():
    try:
        load_tasks(["not-a-task"])
    except ValueError as exc:
        assert "not-a-task" in str(exc)
    else:
        raise AssertionError("missing task was accepted")


def test_cli_mock_run(tmp_path, capsys):
    code = main(["run", "--mock", "--tasks", "quick-earth", "--out", str(tmp_path)])
    assert code == 0
    text = capsys.readouterr().out
    assert "quick-earth" in text
    assert "1/1 passed" in text


def test_grading_uses_the_bots_model_unless_a_flag_names_one():
    endpoint = {"model": "connection-model"}
    bot = {"model": "bot-model"}
    assert grading_model(bot, endpoint) == "bot-model"
    assert grading_model({"model": ""}, endpoint) == "connection-model"
    assert grading_model(bot, endpoint, "offline-grader") == "offline-grader"


def test_rubric_grade_stays_on_the_bots_connection(tmp_path, monkeypatch):
    seen = {}

    async def fake_complete(**kwargs):
        seen["url"] = kwargs["base_url"]
        seen["model"] = kwargs["model"]
        seen["conn"] = (gate._conn.get() or {}).get("id")
        seen["parallel"] = (gate._conn.get() or {}).get("max_parallel")
        return '{"pass": true, "problems": [], "fix_hint": ""}'

    monkeypatch.setattr("easyagent.llm.complete", fake_complete)
    endpoint = {
        "id": "endpoint-1",
        "name": "home",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "",
        "model": "your-model",
        "max_parallel": 2,
    }
    bot = {"name": "Ada", "model": "bot-model", "endpoint_id": "endpoint-1"}

    async def once(judge_model=None):
        return await grade_text(
            endpoint=endpoint,
            bot=bot,
            rubric="Plain sentences.",
            request="Explain a checklist.",
            reply="A checklist is a short list.",
            tool_text="",
            judge_model=judge_model,
        )

    grade = asyncio.run(once())
    assert grade["pass"] is True
    assert seen["url"] == "http://127.0.0.1:9/v1"
    assert seen["model"] == "bot-model"
    assert seen["conn"] == "endpoint-1"
    assert seen["parallel"] == 2
    asyncio.run(once("offline-grader"))
    assert seen["model"] == "offline-grader"
    assert seen["url"] == "http://127.0.0.1:9/v1"
    report = run_suite(
        mock=True,
        task_ids=["judge-checklist"],
        judge_model="offline-grader",
        out_dir=tmp_path,
    )
    assert report["judge_model"] == "offline-grader"
    assert report["tasks"][0]["passed"] is True


def test_a_grade_waits_on_that_connections_line(monkeypatch):
    gate.reset_lanes()

    async def fake_complete(**kwargs):
        permit = await gate.reserve()
        try:
            await permit.acquire()
            return '{"pass": true, "problems": [], "fix_hint": ""}'
        finally:
            await permit.release()

    monkeypatch.setattr("easyagent.llm.complete", fake_complete)
    endpoint = {
        "id": "lane-1",
        "name": "home",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "",
        "model": "your-model",
        "max_parallel": 1,
    }

    async def scenario():
        holding = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            token = gate.bind_connection(endpoint, "Busy")
            try:
                seat = await gate.reserve()
                await seat.acquire()
                holding.set()
                await release.wait()
                await seat.release()
            finally:
                gate.reset_connection(token)

        async def grader():
            await holding.wait()
            return await grade_text(
                endpoint=endpoint,
                bot={"name": "Ada", "model": "your-model"},
                rubric="Plain sentences.",
                request="Say hello.",
                reply="Hello.",
                tool_text="",
            )

        held = asyncio.create_task(holder())
        graded = asyncio.create_task(grader())
        await holding.wait()
        await asyncio.sleep(0.15)
        assert not graded.done()
        release.set()
        result = await graded
        await held
        return result

    result = asyncio.run(scenario())
    assert result["pass"] is True
    gate.reset_lanes()
