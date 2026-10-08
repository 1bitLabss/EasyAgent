"""Grade a reply with the bot's own connection.

Review, the checker, and eval rubrics use the model that bot is already
connected to. There is no second connection. An eval run can pass a model
name for offline grading; that call still uses this connection and its
At once line. A grade does not promote anything by itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from easyagent import gate
from easyagent import llm

JUDGE_VERSION = "1"
_PROMPT_PATH = Path(__file__).resolve().parent / "evals" / "judge_prompt.txt"


def load_judge_prompt() -> tuple[str, str]:
    """Version and prompt text. The version is stored with each eval result."""
    raw = _PROMPT_PATH.read_text(encoding="utf-8")
    version = JUDGE_VERSION
    body_lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("version:"):
            version = line.split(":", 1)[1].strip() or version
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return version, body


def checks_enabled(bot: dict | None) -> bool:
    """On unless this bot turned it off, or EASYAGENT_CHECK is 0."""
    import os

    flag = (os.environ.get("EASYAGENT_CHECK") or "").strip().lower()
    if flag in {"0", "off", "false", "no"}:
        return False
    if not bot:
        return True
    if "check_enabled" not in bot:
        return True
    return bool(bot.get("check_enabled"))


def check_revision_limit(bot: dict | None = None) -> int:
    """How many times a failed check may send the reply back. Default 2."""
    import os

    raw = bot.get("check_revisions") if isinstance(bot, dict) else None
    if raw is None:
        raw = os.environ.get("EASYAGENT_CHECK_REVISIONS")
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return 2
    if number < 0:
        return 0
    if number > 4:
        return 4
    return number


def grading_model(bot: dict | None, endpoint: dict, judge_model: str | None = None) -> str | None:
    """The bot's model. --judge-model overrides the name, not the connection."""
    override = " ".join((judge_model or "").split())
    if override:
        return override
    bot_model = ((bot or {}).get("model") or "").strip()
    if bot_model:
        return bot_model
    endpoint_model = (endpoint.get("model") or "").strip()
    return endpoint_model or None


def parse_grade(text: str) -> dict:
    """Strict JSON. Anything else is a failed grade, not a pass."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "pass": False,
            "problems": ["The grade was not JSON."],
            "fix_hint": "",
            "parsed": False,
        }
    if not isinstance(data, dict):
        return {
            "pass": False,
            "problems": ["The grade was not a JSON object."],
            "fix_hint": "",
            "parsed": False,
        }
    problems = data.get("problems") if isinstance(data.get("problems"), list) else []
    cleaned = [" ".join(str(item).split()) for item in problems if str(item).strip()]
    hint = " ".join(str(data.get("fix_hint") or "").split())
    return {
        "pass": bool(data.get("pass")),
        "problems": cleaned[:8],
        "fix_hint": hint[:500],
        "parsed": True,
    }


async def grade_text(
    *,
    endpoint: dict,
    bot: dict | None,
    rubric: str,
    request: str,
    reply: str,
    tool_text: str,
    judge_model: str | None = None,
) -> dict:
    """One grade on the bot's connection. The call waits in that At once line."""
    version, prompt = load_judge_prompt()
    model = grading_model(bot, endpoint, judge_model)
    user = (
        f"Request:\n{(request or '').strip()}\n\n"
        f"Tool results:\n{(tool_text or '').strip() or '(no tools ran)'}\n\n"
        f"Reply:\n{(reply or '').strip() or '(empty reply)'}\n\n"
        f"Rubric:\n{(rubric or '').strip()}"
    )
    token = gate.bind_connection(endpoint, (bot or {}).get("name") or "eval")
    try:
        text = await llm.complete(
            base_url=endpoint["base_url"],
            api_key=endpoint.get("api_key") or None,
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user},
            ],
        )
    finally:
        gate.reset_connection(token)
    grade = parse_grade(text)
    grade["version"] = version
    grade["model"] = model or ""
    return grade
