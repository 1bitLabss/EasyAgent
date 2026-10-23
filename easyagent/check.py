"""A second pass on the bot's own model, after the draft and before the reply is kept.

The model only grades. It does not promote a file, a command, or a lesson.
The call uses the connection already bound for this turn, so it waits in that
At once line. A short reply with no tools is skipped.
"""

from __future__ import annotations

import json
import re

from easyagent import llm
from easyagent import turn as turn_mod
from easyagent.judge import checks_enabled, parse_grade
from easyagent.store import Store, StoreError

# A review is for work that changed something. A lookup is not that work.
_LOOKUP_KINDS = frozenset({"search", "fetch", "research", "browser", "history"})
_FILE_KINDS = frozenset({"files", "file", "project"})
_WRITE_ACTIONS = frozenset({
    "write", "edit", "create", "append", "patch", "delete", "remove",
    "mkdir", "move", "rename", "trash",
})
_TEST_COMMAND = re.compile(
    r"(?i)(?:^|[;&|`]\s*)"
    r"(?:python(?:3)?\s+-m\s+(?:pytest|unittest)|pytest|py\.test|"
    r"npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|"
    r"cargo\s+test|go\s+test|vitest|jest|ctest)\b"
)
_READ_SHELL = re.compile(
    r"(?i)^\s*(?:sudo\s+)?"
    r"(?:ls|dir|cat|type|head|tail|pwd|wc|stat|file|which|whoami|date|"
    r"true|false|echo|printf|rg|grep|find|git\s+(?:status|log|diff|show|blame))\b"
)
_MUTATING_SHELL = re.compile(
    r"(?i)(?:>>?|\btee\b|\brm\b|\bmv\b|\bcp\b|\bmkdir\b|\btouch\b|"
    r"\bpip3?\s+install\b|\bnpm\s+install\b|\bapt(?:-get)?\s+install\b)"
)

CHECK_PROMPT = (
    "EASYAGENT_CHECK_V1\n"
    "You check one draft reply. You did not write it. You cannot call a tool.\n"
    "Answer only these questions. Did it answer the request? "
    "If a tool ran, did that tool succeed? "
    "Does the reply claim a file line, a command output, or a search result that is not in the tool results?\n"
    "Reply with JSON only, no markdown:\n"
    '{"pass": true, "problems": [], "fix_hint": ""}\n'
    "pass is true or false. problems is a short list. "
    "fix_hint is one sentence, or empty when it passed. Do not add other keys."
)

_SKIP_CHARS = 280
_force_check = False


def force_check(enabled: bool) -> None:
    """Eval tasks that exist to exercise this pass. The app does not call this."""
    global _force_check
    _force_check = bool(enabled)


def _bot(store: Store, bot_id: str | None) -> dict | None:
    if not bot_id:
        return None
    try:
        return store.get_bot(bot_id)
    except StoreError:
        return None


def _step(item) -> dict:
    return item if isinstance(item, dict) else {}


def _command_text(step: dict) -> str:
    return " ".join(str(step.get("command") or "").split())


def _edited_or_tested(step: dict) -> bool:
    """A file was written or a test command ran. A failed attempt did not."""
    if step.get("ok") is False:
        return False
    kind = str(step.get("kind") or "")
    action = str(step.get("action") or "").lower()
    if kind in _FILE_KINDS and action in _WRITE_ACTIONS:
        return True
    if kind in {"shell", "ssh", "windows"} and _TEST_COMMAND.search(_command_text(step)):
        return True
    return False


def _changed_state(step: dict) -> bool:
    """A tool that changed the machine. A read, a search, and a fetch do not."""
    if step.get("ok") is False:
        return False
    if _edited_or_tested(step):
        return True
    kind = str(step.get("kind") or "")
    action = str(step.get("action") or "").lower()
    if kind in _LOOKUP_KINDS:
        return False
    if kind in _FILE_KINDS:
        return action in _WRITE_ACTIONS
    if kind == "memory" and action not in {"", "read", "list"}:
        return True
    if kind in {"shell", "ssh", "windows"}:
        command = _command_text(step)
        if not command:
            return False
        if _MUTATING_SHELL.search(command):
            return True
        if _READ_SHELL.search(command):
            return False
        return True
    return False


def review_budget(steps) -> int:
    """How many review passes this turn gets. The answer is 0 or 1.

    A chat question, a web lookup, a search-and-answer, and a single file
    read get none. Coding gets one: the turn edited or created a file, or
    ran tests. A multi-step task gets one when three or more steps changed
    state. The result is never more than one, and a later pass cannot ask again.
    """
    rows = [_step(item) for item in steps or []]
    if any(_edited_or_tested(row) for row in rows):
        return 1
    if sum(1 for row in rows if _changed_state(row)) >= 3:
        return 1
    return 0


def should_skip(reply: str, tools: list) -> bool:
    """A short reply that did not use a tool. Anything else is checked."""
    if tools:
        return False
    text = " ".join((reply or "").split())
    return len(text) <= _SKIP_CHARS


def _note(grade: dict, revisions: int) -> str:
    problems = grade.get("problems") or []
    if grade.get("pass") and not revisions:
        return "Checked. No problem found."
    lines = []
    if revisions:
        lines.append("Revised after check.")
    if problems:
        lines.extend(f"- {item}" for item in problems)
    hint = (grade.get("fix_hint") or "").strip()
    if hint and not grade.get("pass"):
        lines.append(hint)
    return "\n".join(lines).strip()


def _revision_note(grade: dict) -> str:
    problems = grade.get("problems") or ["The reply does not match the request."]
    lines = ["The check found problems. Correct the reply. Do not invent a result."]
    for item in problems:
        lines.append(f"- {item}")
    hint = (grade.get("fix_hint") or "").strip()
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def _tool_text(raw: str) -> tuple[list, str]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        data = []
    if not isinstance(data, list):
        data = []
    chunks = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind") or "tool"
        action = item.get("action") or ""
        ok = "ok" if item.get("ok") else "failed"
        result = (item.get("result") or "").strip()
        chunks.append(f"{kind} {action} {ok}: {result}".strip())
    return data, "\n".join(chunks)


async def _grade(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    request: str,
    reply: str,
    tool_text: str,
) -> dict | None:
    """One grade on the connection this turn already holds. None when the call failed."""
    turn_mod.raise_if_cancelled()
    user = (
        f"Request:\n{(request or '').strip()}\n\n"
        f"Tool results:\n{tool_text or '(no tools ran)'}\n\n"
        f"Reply:\n{(reply or '').strip() or '(empty reply)'}"
    )
    try:
        text = await llm.complete(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=[
                {"role": "system", "content": CHECK_PROMPT},
                {"role": "user", "content": user},
            ],
            tools=False,
            timeout=llm.grade_timeout(base_url),
            long_wait=True,
        )
    except turn_mod.TurnCancelled:
        raise
    except llm.ProviderError:
        print("heuristic check: the grade did not return, so the draft was kept", flush=True)
        return None
    return parse_grade(text)


ANSWER_NOW = (
    "Answer the user now with what you have. "
    "Do not call a tool. Do not ask another bot. "
    "Write the answer in your own words."
)
NO_ANSWER = "The turn ended before an answer was written."
_FENCE = re.compile(r"```.*?```", re.DOTALL)


class _Held:
    """What one run_turn produced. An async generator cannot return this."""

    def __init__(self) -> None:
        self.stopped = False
        self.loop = False
        self.loop_text = ""
        self.final = ""
        self.ledger = "[]"


def _plain_answer(text: str) -> str:
    """Words with fences removed, for a comparison. Not the saved reply."""
    _hidden, answer = llm.peel_thinking(text or "")
    return _FENCE.sub("", answer or "").strip()


def _published_answer(text: str) -> str:
    """The salvage the person sees. Code fences, indent, and blank lines stay.

    Thinking and tool fences come off. A ```python or ```yaml fence does not.
    """
    from easyagent import search as search_mod
    from easyagent.subagent import strip_subagent_fences
    from easyagent.tools import _strip_lesson_fences, strip_tool_markup

    _hidden, answer = llm.peel_thinking(text or "", keep_shape=True)
    cleaned = strip_subagent_fences(answer, keep_shape=True)
    cleaned = search_mod.strip_search_fences(cleaned, keep_shape=True)
    cleaned = strip_tool_markup(cleaned, keep_shape=True)
    return _strip_lesson_fences(cleaned, keep_shape=True)


def _child_reply(text: str, tools: list) -> bool:
    """True when the words are another bot's reply, not this bot's own answer.

    A parent answer may quote that reply and add its own words. Those words
    stay. An answer that is only the other bot's text does not.
    """
    spoken = " ".join(_plain_answer(text).split())
    if not spoken:
        return False
    for item in tools or []:
        if not isinstance(item, dict) or item.get("kind") != "delegate":
            continue
        result = " ".join(str(item.get("result") or "").split())
        if result and (spoken == result or spoken in result):
            return True
    return False


def _usable(text: str, tools: list) -> str:
    """A reply the user can be shown. Empty and a copied child reply are not."""
    raw = text or ""
    if not raw.strip():
        return ""
    if _child_reply(raw, tools):
        return ""
    return raw


async def _drive(run_turn, call, held: _Held):
    async for kind, text in run_turn(**call):
        if kind == "loop":
            # A repeat stop already has a plain sentence. It is not an empty turn.
            held.stopped = True
            held.loop = True
            held.loop_text = text or ""
            return
        if kind == "ledger":
            held.ledger = text or "[]"
            continue
        if kind == "final":
            held.final = text
            continue
        yield kind, text


async def _salvage(kwargs, messages, draft: str, tool_text: str) -> str:
    """One last call, with no tools. The draft and the tool results are context."""
    turn_mod.raise_if_cancelled()
    prior = list(messages or [])
    kept = _published_answer(draft)
    if kept.strip():
        prior = [*prior, {"role": "assistant", "content": kept}]
    note = ANSWER_NOW
    if (tool_text or "").strip():
        note = f"{ANSWER_NOW}\n\nTool results:\n{tool_text}"
    prior = [*prior, {"role": "user", "content": note}]
    try:
        text = await llm.complete(
            base_url=kwargs.get("base_url") or "",
            api_key=kwargs.get("api_key"),
            model=kwargs.get("model"),
            messages=prior,
            tools=False,
        )
    except turn_mod.TurnCancelled:
        raise
    except llm.ProviderError:
        return ""
    return _published_answer(text or "")


async def _emit_answer(kwargs, messages, final: str, tools: list, tool_text: str, draft: str = ""):
    """Yield a visible answer. Another bot's reply is not that answer."""
    spoken = _usable(final, tools)
    if spoken:
        yield "final", final
        return
    recovered = await _salvage(kwargs, messages, draft or "", tool_text)
    if _child_reply(recovered, tools):
        recovered = ""
    text = recovered or _usable(draft, tools) or NO_ANSWER
    yield "delta", text
    yield "final", text


async def review_turn(run_turn, **kwargs):
    """Run the turn. Review at most once, and only after complex work.

    A chat question, a lookup, and a search-and-answer are not reviewed.
    Coding and a multi-step change get one grade. If that grade fails, one
    revision runs and the turn finishes. The done-gate does not start another.
    A turn that stops with no words of its own gets one last answer, with no tools.
    """
    store: Store = kwargs["store"]
    bot = _bot(store, kwargs.get("bot_id"))
    enabled = _force_check or checks_enabled(bot)
    current = list(kwargs.get("messages") or [])
    call = dict(kwargs)
    call["messages"] = current
    held = _Held()
    turn_mod.raise_if_cancelled()
    async for kind, text in _drive(run_turn, call, held):
        yield kind, text
    tools, tool_text = _tool_text(held.ledger)
    request = _request_text(current)
    if held.loop:
        yield "loop", held.loop_text
        return
    if not enabled or held.stopped or review_budget(tools) < 1 or should_skip(held.final, tools):
        if _usable(held.final, tools) and not held.stopped:
            lesson = _close_learning(
                store, kwargs.get("bot_id"), kwargs.get("chat_id"), request, held.final, tools, None, 0
            )
            if str(lesson).startswith("Learned:"):
                yield "lesson", lesson
            yield "final", held.final
            return
        async for kind, text in _emit_answer(kwargs, current, "" if held.stopped else held.final, tools, tool_text):
            yield kind, text
        return
    grade = await _grade(
        base_url=kwargs.get("base_url") or "",
        api_key=kwargs.get("api_key"),
        model=kwargs.get("model"),
        request=request,
        reply=held.final,
        tool_text=tool_text,
    )
    turn_mod.raise_if_cancelled()
    if grade is None or not grade.get("parsed") or grade.get("pass"):
        if grade is not None and not grade.get("parsed"):
            yield "thinking", "The check did not return a grade.\n"
        badge = "checked" if grade and grade.get("parsed") and grade.get("pass") else ""
        note = _note(grade, 0) if grade and grade.get("parsed") else ""
        if note:
            yield "thinking", note if note.endswith("\n") else note + "\n"
        if badge:
            yield "check", json.dumps({"badge": badge, "problems": grade.get("problems") or []})
            yield "face", "glad"
        lesson = _close_learning(
            store, kwargs.get("bot_id"), kwargs.get("chat_id"), request, held.final, tools, grade, 0
        )
        if lesson:
            yield "lesson", lesson
        async for kind, text in _emit_answer(kwargs, current, held.final, tools, tool_text):
            yield kind, text
        return
    # One revision. It is not graded again, and it cannot throw the draft away.
    draft = held.final
    print("heuristic check: the draft failed, so the reply is revised", flush=True)
    yield "face", "sad"
    from easyagent.tools import open_tool_step

    open_tool_step("Revising")
    yield "status", "Revising"
    note = _note(grade, 1)
    if note:
        yield "thinking", note if note.endswith("\n") else note + "\n"
    call["messages"] = [
        *current,
        {"role": "assistant", "content": draft or "(empty reply)"},
        {"role": "user", "content": _revision_note(grade)},
    ]
    revised = _Held()
    turn_mod.raise_if_cancelled()
    async for kind, text in _drive(run_turn, call, revised):
        yield kind, text
    revised_tools, revised_text = _tool_text(revised.ledger)
    if _usable(revised.final, revised_tools) and not revised.stopped:
        await _offer_revision(store, bot, request, revised.final, 1)
        yield "check", json.dumps({"badge": "revised", "problems": grade.get("problems") or []})
        yield "face", "sad"
        lesson = _close_learning(
            store, kwargs.get("bot_id"), kwargs.get("chat_id"), request, revised.final, revised_tools, grade, 1
        )
        if lesson:
            yield "lesson", lesson
        yield "final", revised.final
        return
    await _offer_revision(store, bot, request, draft, 1)
    yield "check", json.dumps({"badge": "revised", "problems": grade.get("problems") or []})
    yield "face", "sad"
    async for kind, text in _emit_answer(
        kwargs, current, "" if revised.stopped else revised.final, revised_tools, revised_text or tool_text, draft
    ):
        yield kind, text


async def _offer_revision(store, bot, request: str, final: str, revisions: int) -> None:
    """A revised reply can become a candidate. The replay gate still has to pass."""
    if not revisions or not isinstance(bot, dict) or not bot.get("id"):
        return
    try:
        from easyagent.learn import propose_from_signal

        await propose_from_signal(
            store,
            bot,
            f"The check revised the reply. Request: {(request or '')[:400]} Reply: {(final or '')[:400]}",
            reason="checker",
            task=request or "",
        )
    except Exception:
        return


def _close_learning(store, bot_id, chat_id, request, reply, tools, grade, revisions, closing: bool = True) -> str:
    """Record a lesson or an inbox note. A failure here must not change the reply."""
    if not bot_id:
        return ""
    try:
        from easyagent.learn import credit_named, note_check_passed, note_failed_check, observe
    except Exception:
        return ""
    quarantine = False
    if chat_id:
        try:
            store.get_chat(bot_id, chat_id)
        except StoreError:
            quarantine = True
    spoken = ""
    try:
        parsed = bool(grade and grade.get("parsed"))
        if parsed and grade.get("pass"):
            spoken = note_check_passed(store, bot_id, request)
        elif parsed:
            problems = grade.get("problems") or []
            problem = str(problems[0]) if problems else "The check failed."
            spoken = note_failed_check(store, bot_id, request, problem, quarantine=quarantine)
        if closing and parsed:
            credit_named(store, reply, bool(grade.get("pass")), "checker")
        failed_tool = any(isinstance(item, dict) and item.get("ok") is False for item in tools or [])
        if closing and failed_tool:
            credit_named(store, reply, False, "tool")
        if closing and not quarantine and (revisions or len(tools or []) >= 6):
            reason = "recovery" if revisions else "steps"
            text = (
                "The reply was revised after a failed check."
                if revisions
                else "The reply took many steps."
            )
            observe(store, bot_id, text, reason=reason, task=request or "", quarantine=quarantine)
    except Exception:
        return spoken if str(spoken).startswith("Learned:") else ""
    if not str(spoken).startswith("Learned:"):
        return ""
    return spoken


def _request_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""
