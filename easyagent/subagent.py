"""One existing bot asked to do one task.

The child sees the task and the direction file. It does not see the
parent's transcript. Nothing here creates a bot or runs more than one child.
"""

from __future__ import annotations

import re

from easyagent.direction import direction_body
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


def _tidy_removed_fence(text: str) -> str:
    """Close the gap a removed fence left. A remaining ``` fence stays as written."""
    parts = re.split(r"(```[\s\S]*?```)", text or "")
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            out.append(part)
        else:
            out.append(re.sub(r"\n{3,}", "\n\n", part))
    return "".join(out).strip()


def strip_subagent_fences(reply: str, *, keep_shape: bool = False) -> str:
    """Remove an ask-another-bot fence. A reply with none is returned as written."""
    source = reply or ""
    text = FENCE_RE.sub("", source)
    if text == source:
        return text
    if keep_shape:
        return _tidy_removed_fence(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_CONSULT = re.compile(
    r"(?i)\bask\s+another\s+bot\b"
    r"|\banother\s+(?:bot|agent|helper)\b"
    r"|\bconsult\s+(?:another\s+)?(?:bot|agent)\b"
    r"|\bdelegate\s+to\b"
    r"|\bask\s+(?:the\s+)?(?!me\b|you\b|us\b|him\b|her\b|them\b|someone\b|anybody\b|anyone\b|another\b)"
    r"([A-Za-z][\w-]{1,40})(?:\s+bot)?\s+to\b"
)


def _roster(names) -> list[str]:
    """Bot names this bot can actually ask. Longer names win over a shorter prefix."""
    found: list[str] = []
    seen: set[str] = set()
    for name in names or ():
        cleaned = " ".join(str(name or "").split())
        key = cleaned.casefold()
        if not cleaned or key in _IGNORED_BOTS or key in seen:
            continue
        seen.add(key)
        found.append(cleaned)
    found.sort(key=len, reverse=True)
    return found


def _named_consult(text: str, names) -> bool:
    """An asking verb or a colon, tied to a bot that is actually available."""
    roster = _roster(names)
    if not roster:
        return False
    alt = "|".join(re.escape(name) for name in roster)
    name = rf"(?:{alt})"
    patterns = (
        rf"(?i)\b(?:ask|consult|tell|ping|query|message|delegate)\s+(?:the\s+)?(?:bot\s+)?{name}(?:\s+bot)?\b",
        rf"(?i)\bwhat\s+does\s+(?:the\s+)?{name}\s+think\b",
        rf"(?i)\bcheck\s+with\s+(?:the\s+)?{name}(?:\s+bot)?\b",
        rf"(?i)\bget\s+(?:the\s+)?{name}(?:'s|’s)\s+opinion\b",
        rf"(?i)\brun\s+this\s+by\s+(?:the\s+)?{name}(?:\s+bot)?\b",
        rf"(?i)\bhave\s+(?:the\s+)?{name}(?:\s+bot)?\s+[A-Za-z]",
        rf"(?i)(?:\bask\s+(?:the\s+)?)?{name}(?:\s+bot)?\s*:",
    )
    raw = text or ""
    return any(re.search(pattern, raw) for pattern in patterns)


def user_asked_to_consult(text: str, names=None) -> bool:
    """The person asked to send work to another bot. A question aimed at them is not that.

    A name counts when it is one of the bots available here, and the sentence
    asks that bot (a common asking verb, or a colon). Naming a bot in passing
    does not. 'another bot', 'another agent', and 'another helper' do.
    """
    raw = text or ""
    if _CONSULT.search(raw):
        return True
    return _named_consult(raw, names)


def plain_consult_turn(text: str) -> bool:
    """Chat, a lookup, or a search-and-answer. A coding task is not one of these."""
    from easyagent.loop import is_quick, pick_playbook

    raw = text or ""
    if is_quick(raw) or pick_playbook(raw) == "research":
        return True
    if re.search(r"(?i)\b(search(?:\s+the\s+web)?|look\s*up|lookup)\b", raw):
        if re.search(r"(?i)\b(write|create|save|fix|code|file|script|bug|patch)\b", raw):
            return False
        return True
    return False


def automatic_ask_allowed(text: str, names=None) -> bool:
    """A subagent fence sends on its own only outside a plain question.

    Chat, a lookup, and a search-and-answer ignore that fence unless the
    person asked to consult the other bot. names are the bots this bot can ask.
    """
    if user_asked_to_consult(text, names):
        return True
    return not plain_consult_turn(text)


def child_messages(bot_name: str, direction: str, task: str) -> list[dict]:
    """The only messages the child model sees."""
    direction_text = direction_body(direction or "")
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
