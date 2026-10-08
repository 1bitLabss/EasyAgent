"""Run eval tasks in a temporary store. The saved data folder is not written."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from easyagent import gate
from easyagent import llm
from easyagent import search as search_mod
from easyagent import tools as tools_mod
from easyagent.app import create_app
from easyagent.check import force_check
from easyagent.evals.score import score_checks
from easyagent.evals.tasks import load_tasks, substitute
from easyagent.judge import grade_text, grading_model, load_judge_prompt
from easyagent.store import ID_RE, Store, new_id, now_iso

class _Script:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies) or ["Done."]
        self.i = 0

    def next(self) -> str:
        if self.i < len(self.replies):
            reply = self.replies[self.i]
            self.i += 1
            return reply
        return self.replies[-1]


def _fake_search_text(query: str) -> str:
    folded = (query or "").lower()
    if "london" in folded or "thames" in folded or "river" in folded:
        return "1. The Thames runs through London."
    if "france" in folded or "paris" in folded or "capital" in folded:
        return "1. Paris is the capital of France."
    return "1. A short public note."


def _estimate(messages: list[dict], reply: str) -> int:
    blob = reply or ""
    for item in messages or []:
        blob += str(item.get("content") or "")
    return max(1, len(blob) // 4)


def _read_connection(data_dir: Path, name: str) -> dict:
    path = data_dir / "endpoints.json"
    if not path.is_file():
        raise SystemExit(f"No connections file at {path}.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path.name} is not a list.")
    wanted = " ".join(name.split()).casefold()
    for item in data:
        if " ".join(str(item.get("name") or "").split()).casefold() == wanted:
            return item
    raise SystemExit(f"No connection named {name}.")


def _memory_blob(store: Store, bot_id: str) -> str:
    folder = store.root / "bots" / bot_id / "memory"
    if not folder.is_dir():
        return ""
    chunks = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not path.is_symlink():
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "\n".join(chunks)


def _install_trace():
    trace: list[dict] = []
    original = tools_mod.execute

    async def wrapped(store, request, bot_id=None):
        try:
            result = await original(store, request, bot_id)
        except Exception as exc:
            trace.append(
                {
                    "kind": request.kind,
                    "action": request.action,
                    "path": request.path,
                    "command": request.command,
                    "success": False,
                    "result": "",
                    "error": str(exc),
                }
            )
            raise
        trace.append(
            {
                "kind": request.kind,
                "action": request.action,
                "path": request.path,
                "command": request.command,
                "success": True,
                "result": result or "",
                "error": "",
            }
        )
        return result

    tools_mod.execute = wrapped
    return trace, original


def _seed_chat(store: Store, bot_id: str, chat_id: str, prelude: list[dict]) -> None:
    if not prelude:
        return
    chat = store.get_chat(bot_id, chat_id)
    for item in prelude:
        role = item.get("role") or "user"
        chat["messages"].append(
            {
                "id": new_id(),
                "role": role,
                "content": item.get("content") or "",
                "created_at": now_iso(),
            }
        )
    chat["updated_at"] = now_iso()
    store.save_chat(chat)


def _pad(prelude: list[dict], count: int) -> list[dict]:
    messages = list(prelude or [])
    for index in range(count):
        messages.append({"role": "user", "content": f"Filler note {index}: the weather was ordinary."})
        messages.append({"role": "assistant", "content": f"Noted filler {index}."})
    return messages


def _turns_of(task: dict) -> list[dict]:
    turns = task.get("turns")
    if isinstance(turns, list) and turns:
        return turns
    return [{"prompt": task.get("prompt") or "", "mock": task.get("mock") or ["Done."]}]


async def _grade_task(
    store: Store,
    bot: dict,
    endpoint: dict,
    task: dict,
    reply: str,
    trace: list[dict],
    judge_model: str | None,
) -> list[dict]:
    rubrics = [item for item in task.get("criteria") or [] if item.get("kind") == "llm_judge"]
    if not rubrics:
        return []
    model = grading_model(bot, endpoint, judge_model)
    if (judge_model or "").strip():
        print(
            f"heuristic judge: the rubric uses this bot's connection and model {model}",
            flush=True,
        )
    tool_text = "\n".join(
        (item.get("result") or item.get("error") or "") for item in trace
    )
    grades = []
    request = task.get("prompt") or ""
    if isinstance(task.get("turns"), list) and task["turns"]:
        request = task["turns"][-1].get("prompt") or request
    for rubric in rubrics:
        grades.append(
            await grade_text(
                endpoint=endpoint,
                bot=bot,
                rubric=rubric.get("rubric") or "",
                request=request,
                reply=reply,
                tool_text=tool_text,
                judge_model=judge_model,
            )
        )
    return grades


def run_suite(
    *,
    connection: str = "",
    task_ids: list[str] | None = None,
    judge_model: str = "",
    mock: bool = False,
    data_dir: str | Path | None = None,
    out_dir: str | Path | None = None,
    preload_skill: dict | None = None,
    tasks: list | None = None,
) -> dict:
    """Run tasks. Bots and chats are created in a temp folder and deleted after."""
    tasks = list(tasks) if tasks is not None else load_tasks(task_ids)
    source = Path(data_dir) if data_dir else Path(os.environ.get("EASYAGENT_DATA") or Path.cwd() / "data")
    connection_record = None
    if not mock:
        connection_record = _read_connection(source, connection)
    version, _prompt = load_judge_prompt()
    started = time.perf_counter()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = []
    with tempfile.TemporaryDirectory(prefix="easyagent-eval-") as temp_name:
        temp = Path(temp_name)
        app = create_app(temp)
        store: Store = app.state.store
        if mock:
            connection_record = {
                "name": "mock",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "",
                "model": "mock",
                "max_parallel": 1,
            }
        raw_id = str(connection_record.get("id") or "")
        saved = store.add_endpoint(
            name=connection_record["name"],
            base_url=connection_record["base_url"],
            api_key=connection_record.get("api_key") or None,
            model=connection_record.get("model"),
            max_parallel=int(connection_record.get("max_parallel") or 1),
            endpoint_id=raw_id if ID_RE.fullmatch(raw_id) else None,
        )
        if preload_skill:
            store.save_skill(preload_skill)
        meter = {"tokens": 0}
        script_box: dict[str, _Script] = {}
        original_complete = llm.complete

        async def wrapped_complete(**kwargs):
            messages = kwargs.get("messages") or []
            system = ""
            if messages and isinstance(messages[0], dict):
                system = str(messages[0].get("content") or "")
            if mock and "EASYAGENT_CHECK_V1" in system:
                user = ""
                if messages:
                    user = str(messages[-1].get("content") or "")
                if "LOOP-CHECK" in user:
                    passed = False
                    problems = ["The reply still has the marker."]
                    hint = "Remove the marker."
                elif "FABRICATED-CLAIM" in user:
                    passed = False
                    problems = ["The reply quotes a line that was not returned."]
                    hint = "Say the attempt failed."
                else:
                    passed = True
                    problems = []
                    hint = ""
                text = json.dumps({"pass": passed, "problems": problems, "fix_hint": hint})
                meter["tokens"] += _estimate(messages, text)
                return text
            if mock and "EASYAGENT_JUDGE_V1" in system:
                user = ""
                if messages:
                    user = str(messages[-1].get("content") or "")
                passed = "FAIL-THIS-RUBRIC" not in user
                text = json.dumps(
                    {
                        "pass": passed,
                        "problems": [] if passed else ["The rubric was not met."],
                        "fix_hint": "" if passed else "Meet the rubric.",
                    }
                )
                meter["tokens"] += _estimate(messages, text)
                return text
            if mock:
                blob = "\n".join(
                    str(item.get("content") or "")
                    for item in messages
                    if isinstance(item, dict)
                )
                revising = "The check found problems." in blob
                script = script_box.get("revision" if revising else "current")
                if script is None:
                    script = script_box.get("current")
                text = script.next() if script is not None else "Done."
                meter["tokens"] += _estimate(messages, text)
                return text
            text = await original_complete(**kwargs)
            meter["tokens"] += _estimate(messages, text)
            return text

        original_search = search_mod.web_search
        original_execute = tools_mod.execute

        async def fake_search(query: str) -> str:
            return _fake_search_text(query)

        async def traced_search(query: str) -> str:
            try:
                result = await (fake_search(query) if mock else original_search(query))
            except Exception as exc:
                trace.append(
                    {
                        "kind": "search",
                        "action": "",
                        "path": "",
                        "command": query,
                        "success": False,
                        "result": "",
                        "error": str(exc),
                    }
                )
                raise
            trace.append(
                {
                    "kind": "search",
                    "action": "",
                    "path": "",
                    "command": query,
                    "success": True,
                    "result": result or "",
                    "error": "",
                }
            )
            return result

        async def assets_ok(_url: str) -> int:
            return 200

        llm.complete = wrapped_complete
        search_mod.web_search = traced_search
        if mock:
            tools_mod._asset_status = assets_ok
        trace, original_execute = _install_trace()
        try:
            with TestClient(app) as client:
                for task in tasks:
                    trace.clear()
                    gate.reset_lanes()
                    workspace = Path(tempfile.mkdtemp(prefix="easyagent-task-"))
                    started_task = time.perf_counter()
                    tokens_before = meter["tokens"]
                    bot = store.add_bot(name="Eval", endpoint_id=saved["id"], model=saved.get("model"))
                    chat = store.create_chat(bot["id"])
                    setup = task.get("setup") or {}
                    for name, body in (setup.get("files") or {}).items():
                        target = workspace / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(body, encoding="utf-8")
                    skill = setup.get("skill")
                    if isinstance(skill, dict) and skill.get("name"):
                        store.save_skill(skill)
                    prelude = _pad(task.get("prelude") or [], int(task.get("prelude_pad") or 0))
                    _seed_chat(store, bot["id"], chat["id"], substitute(prelude, str(workspace)))
                    reply = ""
                    errored = False
                    prompts = []
                    force_check(bool(task.get("needs_check")))
                    prepared_task = substitute(task, str(workspace))
                    try:
                        for turn in _turns_of(prepared_task):
                            script_box["current"] = _Script(list(turn.get("mock") or ["Done."]))
                            revision = turn.get("revision_mock") or prepared_task.get("revision_mock") or ["Done."]
                            script_box["revision"] = _Script(list(revision))
                            prompts.append(turn.get("prompt") or "")
                            sent = client.post(
                                f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
                                json={"content": turn.get("prompt") or ""},
                            )
                            if sent.status_code != 200:
                                reply = sent.text
                                errored = True
                                break
                            payload = sent.json()
                            messages = payload.get("chat", {}).get("messages") or []
                            last = messages[-1] if messages else {}
                            reply = last.get("content") or ""
                            if last.get("choices"):
                                trace.append(
                                    {
                                        "kind": "question",
                                        "action": "",
                                        "path": "",
                                        "command": "",
                                        "success": True,
                                        "result": reply,
                                        "error": "",
                                    }
                                )
                            if last.get("error"):
                                errored = True
                    finally:
                        force_check(False)
                    filled = substitute(task, str(workspace))
                    filled["prompt"] = prompts[-1] if prompts else filled.get("prompt") or ""
                    grades = []
                    if not errored:
                        grades = asyncio.run(
                            _grade_task(store, bot, saved, filled, reply, list(trace), judge_model or None)
                        )
                    checks = score_checks(
                        filled.get("criteria") or [],
                        reply=reply,
                        trace=list(trace),
                        workspace=workspace,
                        memory_text=_memory_blob(store, bot["id"]),
                        grades=grades,
                    )
                    passed = (not errored) and bool(checks) and all(item["passed"] for item in checks)
                    if errored and not any(item["passed"] is False for item in checks):
                        passed = False
                    results.append(
                        {
                            "id": task["id"],
                            "category": task.get("category") or "general",
                            "passed": passed,
                            "checks": checks,
                            "steps": len(trace),
                            "tokens": meter["tokens"] - tokens_before,
                            "seconds": round(time.perf_counter() - started_task, 3),
                            "reply": reply,
                            "error": errored,
                        }
                    )
        finally:
            force_check(False)
            llm.complete = original_complete
            search_mod.web_search = original_search
            tools_mod.execute = original_execute

    summary = _summarize(results)
    report = {
        "version": 1,
        "judge_prompt_version": version,
        "connection": "mock" if mock else connection,
        "judge_model": (judge_model or "").strip() or (connection_record or {}).get("model") or "",
        "mock": bool(mock),
        "started_at": stamp,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "summary": summary,
        "tasks": results,
    }
    folder = Path(out_dir) if out_dir else Path.cwd() / "evals" / "results"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report["path"] = str(path)
    return report


def _summarize(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for item in results if item.get("passed"))
    by_category: dict[str, dict] = {}
    for item in results:
        bucket = by_category.setdefault(item.get("category") or "general", {"passed": 0, "total": 0})
        bucket["total"] += 1
        if item.get("passed"):
            bucket["passed"] += 1
    def _avg(key: str) -> float:
        if not results:
            return 0.0
        return round(sum(float(item.get(key) or 0) for item in results) / len(results), 2)

    return {
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "by_category": by_category,
        "avg_steps": _avg("steps"),
        "avg_tokens": _avg("tokens"),
        "avg_seconds": _avg("seconds"),
    }


def format_summary(report: dict) -> str:
    summary = report.get("summary") or {}
    total = summary.get("total") or 0
    passed = summary.get("passed") or 0
    rate = (summary.get("pass_rate") or 0) * 100
    lines = [
        f"Eval  {passed}/{total} passed  {rate:.1f}%",
        f"connection {report.get('connection')}   model {report.get('judge_model') or 'the bot'}   prompt v{report.get('judge_prompt_version')}",
        f"avg steps {summary.get('avg_steps')}   avg tokens {summary.get('avg_tokens')}   avg time {summary.get('avg_seconds')}s",
        "",
        f"{'category':<16} {'passed':>6} {'total':>6}",
    ]
    for name, bucket in sorted((summary.get("by_category") or {}).items()):
        lines.append(f"{name:<16} {bucket.get('passed', 0):>6} {bucket.get('total', 0):>6}")
    lines.append("")
    for item in report.get("tasks") or []:
        mark = "pass" if item.get("passed") else "fail"
        lines.append(f"{mark:<4} {item.get('id')}  steps {item.get('steps')}  {item.get('seconds')}s")
        if not item.get("passed"):
            for check in item.get("checks") or []:
                if not check.get("passed"):
                    lines.append(f"     {check.get('kind')}: {check.get('detail')}")
    return "\n".join(lines)


def compare_results(before: dict, after: dict) -> dict:
    """Tasks that passed in before and fail in after are regressions."""
    left = {item["id"]: item for item in before.get("tasks") or []}
    right = {item["id"]: item for item in after.get("tasks") or []}
    regressions = []
    fixed = []
    for task_id, earlier in left.items():
        later = right.get(task_id)
        if later is None:
            continue
        if earlier.get("passed") and not later.get("passed"):
            regressions.append(task_id)
        elif not earlier.get("passed") and later.get("passed"):
            fixed.append(task_id)
    return {
        "regressions": regressions,
        "fixed": fixed,
        "before_passed": (before.get("summary") or {}).get("passed"),
        "after_passed": (after.get("summary") or {}).get("passed"),
    }


def format_compare(report: dict) -> str:
    lines = [
        f"before {report.get('before_passed')} passed    after {report.get('after_passed')} passed",
    ]
    if report.get("regressions"):
        lines.append("regressions:")
        lines.extend(f"  {item}" for item in report["regressions"])
    else:
        lines.append("regressions: none")
    if report.get("fixed"):
        lines.append("fixed:")
        lines.extend(f"  {item}" for item in report["fixed"])
    return "\n".join(lines)
