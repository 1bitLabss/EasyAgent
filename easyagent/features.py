"""What a normal chat bot can do.

The system prompt prints this list. It is not a second paragraph kept in the
prompt template. A tool filter, a small context window, or a child tool list
does not remove it: those paths change the native tool array or the chat
tail, and this list is neither.
"""

from __future__ import annotations

import importlib
import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass

CAPABILITY_RULE = "Never say you can't do something on this list; propose it."
_MARK = "# What you can do"
_OFFER: ContextVar[dict | None] = ContextVar("easyagent_routine_offer", default=None)


class _RoutineTurn:
    """One user request. The first routine card is the only one.

    Tool calls from one reply run together, in tasks that do not share a
    ContextVar write. The object lives on the chat's turn slot so those tasks
    still see the same card.
    """

    def __init__(self) -> None:
        self.opened = False
        self.refused = ""
        self.seen: set[tuple[str, str]] = set()
        self.lock = threading.Lock()


_TURN: ContextVar[_RoutineTurn | None] = ContextVar("easyagent_routine_turn", default=None)


def reset_routine_turn() -> None:
    """A new model run starts with no routine card and no refusal."""
    fresh = _RoutineTurn()
    _TURN.set(fresh)
    slot = _current_slot()
    if slot is not None:
        slot.routine_turn = fresh


def _current_slot():
    try:
        from easyagent.turn import current_slot
    except Exception:
        return None
    return current_slot()


def _turn() -> _RoutineTurn:
    slot = _current_slot()
    if slot is not None:
        current = getattr(slot, "routine_turn", None)
        if not isinstance(current, _RoutineTurn):
            current = _RoutineTurn()
            slot.routine_turn = current
        return current
    current = _TURN.get()
    if current is None:
        current = _RoutineTurn()
        _TURN.set(current)
    return current


def _signature(schedule: str, prompt: str) -> tuple[str, str]:
    return (" ".join((schedule or "").split()), " ".join((prompt or "").split()))


def claim_routine_card(schedule: str, prompt: str) -> bool:
    """True when this proposal may open a card.

    A later proposal in the same request does not, and neither does a second
    copy of the same schedule and prompt.
    """
    turn = _turn()
    key = _signature(schedule, prompt)
    with turn.lock:
        duplicate = key in turn.seen
        turn.seen.add(key)
        if turn.refused or turn.opened or duplicate:
            return False
        turn.opened = True
        return True


def note_routine_refused(reason: str) -> None:
    """The routine was not saved. This request does not get a card."""
    turn = _turn()
    text = " ".join((reason or "").split())
    with turn.lock:
        if text and not turn.refused:
            turn.refused = text


def routine_refusal() -> str:
    turn = _turn()
    with turn.lock:
        return turn.refused


def routine_opened() -> bool:
    turn = _turn()
    with turn.lock:
        return turn.opened

_ROUTINE_ASK = re.compile(
    r"(?i)(\bcron\b|\bschedules?\b|\bscheduled\b|\bscheduling\b|\brecurring\b|"
    r"\bevery morning\b|\bremind me\b|\bcheck daily\b|\bkeep an eye on\b)"
)
_QUESTION = re.compile(
    r"(?i)(\bcan you\b|\bcould you\b|\bdo you\b|\bhow about\b|\bwhat about\b|\bare you able\b)"
)
_OS_SCHEDULER = re.compile(
    r"(?i)(schtasks|task scheduler|cron daemon|crontab|\blaunchd\b|systemd timer|ad-hoc loop|ad hoc loop)"
)
_ABSENT = re.compile(
    r"(?i)(\bdon'?t have\b|\bdo not have\b|\bno built-in\b|\bi have no\b|"
    r"\bthere is no\b|\bthere isn't\b|\bcannot\b|\bcan't\b|\bunable\b)"
)
_SAFETY_REPLY = re.compile(
    r"(?i)(\bblocked\b|\bdenied\b|\bnot allowed\b|\bcatastrophic\b|\bapproval\b|\bguardrail\b)"
)
_TASK_NOT_QUESTION = re.compile(
    r"(?i)(https?://|[A-Za-z]:\\|[/\\][\w.-]+\.\w{1,8}\b|\b(rm|del|format|sudo|mkfs)\b)"
)


@dataclass(frozen=True)
class Feature:
    """One product capability. tools and module are how the list stays real."""

    key: str
    phrase: str
    tools: tuple[str, ...] = ()
    module: str = ""
    symbol: str = ""
    ask: str = ""


# Phrases the prompt is allowed to say. Each row is backed by a native tool
# that exists on NATIVE_TOOLS, or by a module symbol the product actually calls.
_FEATURES: tuple[Feature, ...] = (
    Feature("files", "files", tools=("list_dir", "read_file", "write_file", "search_files"), ask=r"\bfiles\b"),
    Feature("shell", "shell", tools=("terminal",), ask=r"\b(shell|terminal)\b"),
    Feature("web", "web search", tools=("web_search",), ask=r"\b(web search|search the web)\b"),
    Feature("browser", "browser", tools=("browser",), ask=r"\b(browser|browse)\b"),
    Feature(
        "routines",
        "routines (cron, reminders, recurring tasks, and every morning)",
        tools=("routine",),
    ),
    Feature("helpers", "helper bots", module="easyagent.helpers", symbol="parse_helpers", ask=r"\bhelpers?\b"),
    Feature("bots", "other bots", module="easyagent.subagent", symbol="user_asked_to_consult", ask=r"\b(other bots?|another bot)\b"),
    Feature("mcp", "MCP connectors", tools=("mcp",), ask=r"\b(mcp|connectors?)\b"),
    Feature("skills", "skills", tools=("skill",), ask=r"\bskills?\b"),
    Feature("memory", "memory", tools=("memory",), ask=r"\bmemory\b"),
    Feature("bridges", "chat bridges", module="easyagent.bridges", symbol="list_records", ask=r"\b(bridges?|telegram|discord)\b"),
    Feature(
        "yolo",
        "YOLO (while /yolo is on, approval gates allow the action and show no card; a catastrophic command still blocks)",
        module="easyagent.yolo",
        symbol="passes",
        ask=r"\byolo\b",
    ),
    Feature("learn", "nightly learning", module="easyagent.learn", symbol="list_candidates", ask=r"\bnightly\b"),
)


def features() -> tuple[Feature, ...]:
    """Every capability, after checking the tool or module behind it still exists.

    A missing tool or symbol raises. The prompt does not quietly omit one.
    Disabling a tool, or limiting a child to a short tool list, does not change
    this. Those filters read the native tool array, not this list.
    """
    from easyagent.llm import NATIVE_TOOLS

    names = {(tool.get("function") or {}).get("name") or "" for tool in NATIVE_TOOLS}
    ready: list[Feature] = []
    for feature in _FEATURES:
        missing = [tool for tool in feature.tools if tool not in names]
        if missing:
            raise RuntimeError(f"{feature.key} is missing native tools: {', '.join(missing)}")
        if feature.module:
            module = importlib.import_module(feature.module)
            if feature.symbol and not hasattr(module, feature.symbol):
                raise RuntimeError(f"{feature.key} is missing {feature.module}.{feature.symbol}")
        ready.append(feature)
    return tuple(ready)


def capability_summary() -> str:
    """One sentence built from features(). Not a hand-written copy of that list."""
    phrases = [item.phrase for item in features()]
    if not phrases:
        return ""
    if len(phrases) == 1:
        return phrases[0]
    return ", ".join(phrases[:-1]) + ", and " + phrases[-1]


def capability_block() -> str:
    summary = capability_summary()
    return f"{_MARK}\nYou can: {summary}.\n{CAPABILITY_RULE}"


def ensure_capabilities(text: str) -> str:
    """Put the capability list back when a copy of the prompt lost it.

    The block stays near the top, after the identity line and before Direction.
    A prompt that already has the rule is returned unchanged.
    """
    body = text or ""
    if CAPABILITY_RULE in body and _MARK in body:
        return body
    block = capability_block()
    parts = body.split("\n\n", 1)
    if parts and parts[0].startswith("You are "):
        rest = parts[1] if len(parts) == 2 else ""
        return parts[0] + "\n\n" + block + ("\n\n" + rest if rest else "")
    if not body.strip():
        return block
    return block + "\n\n" + body


def asks_for_routine(text: str) -> bool:
    return bool(_ROUTINE_ASK.search(text or ""))


def _asked_feature(text: str) -> Feature | None:
    """A short question about a capability. A task that names a file or a URL is not one."""
    body = " ".join((text or "").split())
    if asks_for_routine(body):
        for feature in features():
            if feature.key == "routines":
                return feature
        return None
    if len(body) > 180 or not _QUESTION.search(body) or _TASK_NOT_QUESTION.search(body):
        return None
    for feature in features():
        if feature.ask and re.search(feature.ask, body, flags=re.IGNORECASE):
            return feature
    return None


def _denies(reply: str) -> bool:
    if _SAFETY_REPLY.search(reply or ""):
        return False
    return bool(_ABSENT.search(reply or ""))


def _proposal_sentence(feature: Feature, user_text: str) -> str:
    del user_text
    if feature.key == "routines":
        return (
            "A routine runs on a schedule, the same idea as cron. "
            "Confirm the routine card to save it."
        )
    if feature.key == "yolo":
        return (
            "YOLO means while /yolo is on, approval gates allow the action and show no card. "
            "A catastrophic command still blocks."
        )
    sentences = {
        "files": "You have files. I can list, read, and write them on this computer.",
        "shell": "You have a shell. I can run a command on this computer.",
        "web": "You have web search. I can search the public web from this computer.",
        "browser": "You have a browser on this bot. I can open a page in it.",
        "helpers": "You have helper bots. I can hand one task to a helper.",
        "bots": "You can ask another bot that already exists.",
        "mcp": "You have MCP connectors. I can call a saved connector.",
        "skills": "You have skills. I can save one and reuse it.",
        "memory": "You have memory. It is a file on this computer, and I can read it and file a line.",
        "bridges": "You have chat bridges for Telegram and Discord.",
        "learn": "You have nightly learning. It can propose a skill from what this bot has done.",
    }
    return sentences.get(feature.key, f"You can use {feature.phrase}.")


def _routine_spec(text: str) -> dict:
    body = " ".join((text or "").split())
    lowered = body.lower()
    if "every morning" in lowered:
        when = {"weekdays": "8:00 AM"}
        name = "Morning check"
    elif "keep an eye on" in lowered:
        when = {"every_minutes": 60}
        name = "Watch"
    elif "check daily" in lowered or "daily" in lowered:
        when = {"daily": "8:00 AM"}
        name = "Daily check"
    else:
        when = {"daily": "8:00 AM"}
        name = "Scheduled job"
    prompt = body[:500] if body else "Run this check."
    return {"action": "create", "name": name, "prompt": prompt, **when}


def _offer_routine(store, bot_id: str, user_text: str) -> str:
    """Open a routine card, or save it when YOLO is already on. The reply names a routine."""
    from easyagent.helpers import in_helper
    from easyagent.safety import Verdict, _PENDING, _make_card, _notify, is_unattended, list_pending
    from easyagent.schedule import ScheduleError, apply_confirmed, compile_routine, confirm_detail, preview
    from easyagent.yolo import passes

    if in_helper() or is_unattended():
        return ""
    if routine_refusal() or routine_opened():
        return ""
    if any((card.get("proposal") or {}).get("kind") == "routine" for card in list_pending(bot_id or "")):
        return ""
    spec = _routine_spec(user_text)
    try:
        drafted = compile_routine(spec)
    except ScheduleError:
        return ""
    name = drafted.get("name") or spec["name"]
    shown = preview(drafted)
    prompt = drafted.get("prompt") or spec["prompt"]
    spec = {
        **spec,
        **{key: drafted.get(key) for key in ("name", "prompt", "kind", "cron", "every_minutes", "at", "timezone", "quiet") if drafted.get(key) is not None},
    }
    spec["action"] = "create"
    if not claim_routine_card(shown, prompt):
        return ""
    if bot_id and passes(store, bot_id):
        try:
            apply_confirmed(store, bot_id, spec)
        except ScheduleError:
            return ""
        return f"Saved the routine {name}. A routine runs on a schedule, the same idea as cron."
    detail = confirm_detail(name, shown, prompt)
    card = _make_card(
        bot_id,
        Verdict("ask", "routine", "Save this routine?", detail, ""),
        "",
        proposal={
            "kind": "routine",
            "action": "create",
            "name": name,
            "schedule": shown,
            "prompt": prompt,
            "spec": spec,
            "apply_on_approve": True,
        },
    )
    _PENDING[card.id] = card
    _notify("Save this routine?")
    from easyagent.safety import announce_approvals

    announce_approvals(bot_id)
    _OFFER.set(
        {
            "kind": "routine",
            "action": "create",
            "name": name,
            "schedule": shown,
            "prompt": prompt,
        }
    )
    return ""


def steer_reply(store, bot_id: str | None, user_text: str, reply: str) -> str:
    """A schedule ask proposes a routine. A denial of a listed capability does not stand."""
    text = reply or ""
    feature = _asked_feature(user_text)
    if feature is None:
        return text
    if feature.key == "routines":
        reason = routine_refusal()
        if reason:
            if reason.lower() not in text.lower():
                text = f"{text.rstrip()}\n\n{reason}".strip() if text.strip() else reason
            return text
        note = "" if routine_opened() else _offer_routine(store, bot_id or "", user_text)
        sentence = note or _proposal_sentence(feature, user_text)
        if _OS_SCHEDULER.search(text) or _denies(text) or not re.search(r"(?i)\broutine", text):
            if _OS_SCHEDULER.search(text) or _denies(text):
                return sentence
            if text.strip():
                return text.rstrip() + "\n\n" + sentence
            return sentence
        return text
    if _denies(text):
        return _proposal_sentence(feature, user_text)
    return text


def take_routine_offer() -> dict | None:
    """The routine card for the reply being saved. One take clears it."""
    offer = _OFFER.get()
    if offer is not None:
        _OFFER.set(None)
    return offer
