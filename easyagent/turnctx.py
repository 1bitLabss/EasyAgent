"""The bounded view for one private-chat turn.

`prepare_context` still decides the summary and the verbatim tail. Retrieval
is added only when it fits in the same budget, and it is trimmed before any
recent turn is dropped. A short chat with nothing to retrieve is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from easyagent.context import (
    PreparedContext,
    bot_context_chars,
    estimate_tokens,
    is_harness_message,
    prepare_context,
)
from easyagent.recall import scan_chats
from easyagent.retrieve import earlier_block
from easyagent.rolling import grounded_summary
from easyagent.store import Store

RETRIEVAL_RESERVE = 1600


def _fresh(chat: dict, count: int) -> int:
    fresh = int(chat.get("fresh_from") or 0)
    if fresh < 0:
        fresh = 0
    if fresh > count:
        fresh = count
    return fresh


def message_cap_chars(n_ctx: int, system_tokens: int = 0, kept: int = 2) -> int:
    """Characters one message may take. About 40% of the window, tighter when the system prompt is large."""
    from easyagent.limits import CHARS_PER_TOKEN

    try:
        window = int(n_ctx or 0)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        return 0
    single = int(window * 0.4 * CHARS_PER_TOKEN)
    try:
        overhead = int(system_tokens or 0)
    except (TypeError, ValueError):
        overhead = 0
    if overhead < 0:
        overhead = 0
    if overhead <= 0:
        return max(240, single)
    room = int(window * 0.85) - overhead
    if room < 128:
        room = 128
    shares = kept if isinstance(kept, int) and kept > 0 else 1
    shared = room * CHARS_PER_TOKEN // shares
    return max(240, min(single, shared))


def visible_prepare(chat: dict, budget: int, message_cap: int = 0) -> PreparedContext:
    """Same fold as `prepare_context`, ignoring turns hidden by Start fresh.

    With no fresh floor this is `prepare_context` itself, stats included.
    """
    messages = list(chat.get("messages") or [])
    fresh = _fresh(chat, len(messages))
    through = int(chat.get("summarized_through") or 0) - fresh
    if through < 0:
        through = 0
    try:
        floor = int(chat.get("compact_through") or 0) - fresh
    except (TypeError, ValueError):
        floor = 0
    if floor < 0:
        floor = 0
    prepared = prepare_context(
        messages[fresh:],
        chat.get("summary") or "",
        through,
        budget,
        floor,
        message_cap,
    )
    if fresh == 0:
        return prepared
    stats = dict(prepared.stats)
    transcript_chars = sum(len(m.get("content") or "") for m in messages)
    absolute = fresh + int(prepared.summarized_through or 0)
    stats["transcript_messages"] = len(messages)
    stats["transcript_chars"] = transcript_chars
    stats["transcript_tokens"] = estimate_tokens(transcript_chars)
    stats["summarized_through"] = absolute
    stats["compacted_messages"] = absolute
    stats["compacted"] = absolute > 0
    return PreparedContext(prepared.summary, absolute, prepared.tail, stats)


def prompt_summary(chat: dict, prepared: PreparedContext, messages: list[dict]) -> str:
    """The extractive summary, or a grounded rolling summary when it already covers the fold."""
    summary = prepared.summary or ""
    through = int(prepared.summarized_through or 0)
    rolling = (chat.get("rolling_summary") or "").strip()
    rolled = int(chat.get("rolling_through") or 0)
    if through <= 0 or not rolling or rolled < through:
        return summary
    cap = max(len(summary), 400)
    visible = [
        item
        for item in messages[:through]
        if isinstance(item, dict) and not is_harness_message(item)
    ]
    grounded = grounded_summary(rolling, visible, cap)
    return grounded or summary


def _trim_block(block: str, room: int) -> str:
    if room < 24 or not block:
        return ""
    if len(block) <= room:
        return block
    lines = block.splitlines()
    kept: list[str] = []
    for line in lines:
        trial = line if not kept else "\n".join([*kept, line])
        if len(trial) > room:
            break
        kept.append(line)
    if len(kept) <= 1:
        return ""
    return "\n".join(kept)


def _row_protected(row: dict, latest: str) -> bool:
    """The latest request, the open task list, and pending approvals stay in the tail."""
    if not isinstance(row, dict):
        return False
    if row.get("pin") in {"request", "tasks", "approvals"}:
        return True
    if latest and row.get("role") == "user" and str(row.get("content") or "").strip() == latest:
        return True
    return False


def fit_context(
    summary: str,
    tail: list[dict],
    earlier: str,
    budget: int,
    latest: str = "",
) -> tuple[str, list[dict], str, int]:
    """Keep summary + tail + earlier inside `budget`. Trim retrieval before the tail.

    Rows are dropped from the front, and only when that row is not the latest
    request, the task list, or a pending approval. A protected row can leave
    the budget over the cap. A call with no protected row behaves as before.
    """
    summary = summary or ""
    rows = list(tail or [])
    block = earlier or ""
    kept = (latest or "").strip()

    def cost(body: str, items: list[dict]) -> int:
        return len(summary) + sum(len(item.get("content") or "") for item in items) + len(body)

    def drop_front() -> bool:
        if len(rows) <= 1 or _row_protected(rows[0], kept):
            return False
        rows.pop(0)
        return True

    if block:
        reserve = min(len(block), RETRIEVAL_RESERVE)
        while len(rows) > 1 and cost("", rows) > max(0, budget - reserve):
            if not drop_front():
                break
        room = budget - cost("", rows)
        block = _trim_block(block, room)
    used = cost(block, rows)
    if used > budget and rows:
        # The summary alone can meet the cap. Drop retrieval rather than exceed it.
        block = _trim_block(block, max(0, budget - cost("", rows)))
        used = cost(block, rows)
    return summary, rows, block, used


def _latest_request(messages: list) -> str:
    from easyagent.context import is_harness_message

    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if is_harness_message(message):
            continue
        return str(message.get("content") or "").strip()
    return ""


def _context_pins(store: Store, bot: dict, chat: dict, tail: list[dict], latest: str) -> list[dict]:
    """Rows the compressor must keep. Empty when the tail already holds the request and there is no list."""
    pins: list[dict] = []
    if latest and not any(
        item.get("role") == "user" and str(item.get("content") or "").strip() == latest for item in tail
    ):
        pins.append({"role": "user", "content": latest, "pin": "request"})
    try:
        from easyagent.plan import describe, load_plan

        plan = load_plan(store, str(bot.get("id") or ""), str(chat.get("id") or ""))
        steps = [step for step in (plan or {}).get("steps") or [] if isinstance(step, dict)]
        if steps and not (plan or {}).get("dismissed"):
            pins.append({"role": "user", "content": "Open tasks:\n" + describe(plan), "pin": "tasks"})
    except Exception:
        pass
    try:
        from easyagent.safety import list_pending

        lines = []
        for card in list_pending(str(bot.get("id") or "")):
            rule = str(card.get("rule") or "")
            detail = str(card.get("detail") or card.get("why") or "")
            if rule or detail:
                lines.append(f"{rule}: {detail}".strip())
        if lines:
            pins.append({"role": "user", "content": "Pending approvals:\n" + "\n".join(lines), "pin": "approvals"})
    except Exception:
        pass
    return pins


@dataclass
class ModelTurn:
    summary: str
    tail: list[dict]
    earlier: str
    recalled: str
    summarized_through: int
    stats: dict
    used_chars: int


def model_turn(store: Store, bot: dict, chat: dict, endpoint: dict | None = None) -> ModelTurn:
    """Direction stays outside this budget. Summary, tail, and retrieval stay inside it."""
    budget = bot_context_chars(bot)
    cap = 0
    if endpoint:
        from easyagent.nctx import resolve_n_ctx

        bot_model = (bot.get("model") or "").strip()
        endpoint_model = (endpoint.get("model") or "").strip()
        model = bot_model or endpoint_model or None
        try:
            overhead = int(chat.get("system_tokens") or 0)
        except (TypeError, ValueError):
            overhead = 0
        cap = message_cap_chars(resolve_n_ctx(endpoint, None, model), overhead, 2)
    prepared = visible_prepare(chat, budget, cap)
    messages = list(chat.get("messages") or [])
    earlier = earlier_block(store, bot["id"], chat, int(prepared.summarized_through or 0), endpoint)
    try:
        from easyagent.journal import note_block

        notes = note_block(store, str(bot.get("id") or ""), messages)
    except Exception:
        notes = ""
    if notes:
        earlier = notes if not earlier else notes + "\n\n" + earlier
    summary = prompt_summary(chat, prepared, messages)
    latest = _latest_request(messages)
    tail_rows = list(prepared.tail)
    tail_rows.extend(_context_pins(store, bot, chat, tail_rows, latest))
    summary, tail, earlier, used = fit_context(summary, tail_rows, earlier, budget, latest=latest)
    recalled = scan_chats(store, bot["id"], chat, int(prepared.summarized_through or 0))
    return ModelTurn(
        summary=summary,
        tail=tail,
        earlier=earlier,
        recalled=recalled,
        summarized_through=int(prepared.summarized_through or 0),
        stats=prepared.stats,
        used_chars=used,
    )
