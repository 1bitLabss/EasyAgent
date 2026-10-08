"""One existing bot asked to do one task.

The child sees the task and the direction file. It does not see the
parent's transcript. Nothing here creates a bot or runs more than one child.
"""

from __future__ import annotations

import re

from easyagent.limits import DIRECTION_CAP

FENCE_RE = re.compile(r"```subagent[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_IGNORED_BOTS = {"the bot", "bot"}
_IGNORED_TASKS = {"the task", "task"}
SHORT_RESULT_CHARS = 1200


def parse_subagent(reply: str) -> tuple[str, str] | None:
    """The first real ask in a reply, or nothing.

    The prompt's sample (`bot: the bot` / `the task`) is not an ask.
    Later fences are ignored so one reply cannot start a swarm.
    """
    match = FENCE_RE.search(reply or "")
    if not match:
        return None
    lines = [line.rstrip() for line in match.group(1).strip().splitlines()]
    if not lines or not lines[0].lower().startswith("bot:"):
        return None
    name = lines[0].split(":", 1)[1].strip()
    task = "\n".join(lines[1:]).strip()
    if not name or name.lower() in _IGNORED_BOTS:
        return None
    if not task or task.lower() in _IGNORED_TASKS:
        return None
    return name, task


def strip_subagent_fences(reply: str) -> str:
    text = FENCE_RE.sub("", reply or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def child_messages(bot_name: str, direction: str, task: str) -> list[dict]:
    """The only messages the child model sees."""
    direction_text = (direction or "").strip()
    if len(direction_text) > DIRECTION_CAP:
        direction_text = direction_text[: DIRECTION_CAP - 40].rstrip() + "\n[direction truncated]"
    name = " ".join((bot_name or "EasyAgent").split()) or "EasyAgent"
    system = (
        f"You are {name}.\n\n"
        "Follow the direction file below. It outranks habit.\n\n"
        f"# Direction\n{direction_text}\n\n"
        "You were given one task. You do not have another bot's transcript. "
        "Reply with the result of the task only. Do not ask for a new bot."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]


def short_result(task: str, reply: str) -> str:
    """What is appended to the parent. Not the child's other chats."""
    task_line = " ".join((task or "").split())
    if len(task_line) > 180:
        task_line = task_line[:179] + "…"
    body = (reply or "").strip() or "(empty reply)"
    if len(body) > SHORT_RESULT_CHARS:
        body = body[: SHORT_RESULT_CHARS - 48].rstrip() + "\n[shortened; full reply is on that bot's chat]"
    return f"Task: {task_line}\n\n{body}"


def error_line(name: str, detail: str) -> str:
    text = " ".join((detail or "The endpoint failed.").split())
    return f"{name} could not finish the task. {text}"
