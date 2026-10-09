"""A second pass on the bot's own model, after the draft and before the reply is kept.

The model only grades. It does not promote a file, a command, or a lesson.
The call uses the connection already bound for this turn, so it waits in that
At once line. A short reply with no tools is skipped.
"""

from __future__ import annotations

import json

from easyagent import llm
from easyagent import turn as turn_mod
from easyagent.judge import checks_enabled, check_revision_limit, parse_grade
from easyagent.store import Store, StoreError

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
        )
    except turn_mod.TurnCancelled:
        raise
    except llm.ProviderError:
        print("heuristic check: the grade did not return, so the draft was kept", flush=True)
        return None
    return parse_grade(text)


async def review_turn(run_turn, **kwargs):
    """Run the turn, then grade the draft with this bot's model. At most two revisions."""
    store: Store = kwargs["store"]
    bot = _bot(store, kwargs.get("bot_id"))
    enabled = _force_check or checks_enabled(bot)
    if not enabled:
        async for kind, text in run_turn(**kwargs):
            if kind == "ledger":
                continue
            yield kind, text
        return
    limit = check_revision_limit(bot)
    current = list(kwargs.get("messages") or [])
    revisions = 0
    pending = ""
    while True:
        turn_mod.raise_if_cancelled()
        final = ""
        ledger_raw = "[]"
        call = dict(kwargs)
        call["messages"] = current
        async for kind, text in run_turn(**call):
            if kind == "ledger":
                ledger_raw = text or "[]"
                continue
            if kind == "final":
                final = text
                continue
            yield kind, text
        tools, tool_text = _tool_text(ledger_raw)
        request = _request_text(list(kwargs.get("messages") or []))
        if should_skip(final, tools):
            if revisions:
                await _offer_revision(store, bot, request, final, revisions)
                yield "check", json.dumps({"badge": "revised", "problems": []})
                yield "face", "sad"
            lesson = _close_learning(
                store, kwargs.get("bot_id"), kwargs.get("chat_id"), request, final, tools, None, revisions
            ) or pending
            if lesson.startswith("Learned:"):
                yield "lesson", lesson
            yield "final", final
            return
        grade = await _grade(
            base_url=kwargs.get("base_url") or "",
            api_key=kwargs.get("api_key"),
            model=kwargs.get("model"),
            request=request,
            reply=final,
            tool_text=tool_text,
        )
        if grade is None or not grade.get("parsed"):
            if grade is not None:
                yield "thinking", "The check did not return a grade.\n"
            if revisions:
                await _offer_revision(store, bot, request, final, revisions)
            lesson = pending if pending.startswith("Learned:") else ""
            if lesson.startswith("Learned:"):
                yield "lesson", lesson
            yield "final", final
            return
        if grade.get("pass") or revisions >= limit:
            if revisions:
                await _offer_revision(store, bot, request, final, revisions)
            badge = "revised" if revisions else ("checked" if grade.get("pass") else "")
            note = _note(grade, revisions)
            if note:
                yield "thinking", note if note.endswith("\n") else note + "\n"
            if badge:
                yield "check", json.dumps({"badge": badge, "problems": grade.get("problems") or []})
                yield "face", "glad" if badge == "checked" else "sad"
            lesson = _close_learning(
                store, kwargs.get("bot_id"), kwargs.get("chat_id"), request, final, tools, grade, revisions
            )
            if lesson:
                yield "lesson", lesson
            yield "final", final
            return
        pending = _close_learning(
            store,
            kwargs.get("bot_id"),
            kwargs.get("chat_id"),
            request,
            final,
            tools,
            grade,
            revisions,
            closing=False,
        )
        revisions += 1
        print("heuristic check: the draft failed, so the reply is revised", flush=True)
        yield "face", "sad"
        yield "status", "Revising"
        note = _note(grade, revisions)
        if note:
            yield "thinking", note if note.endswith("\n") else note + "\n"
        current = [
            *current,
            {"role": "assistant", "content": final or "(empty reply)"},
            {"role": "user", "content": _revision_note(grade)},
        ]


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
