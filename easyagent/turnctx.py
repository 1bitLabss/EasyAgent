"""The bounded view for one private-chat turn.

`prepare_context` still decides the summary and the verbatim tail. Retrieval
is added only when it fits in the same budget, and it is trimmed before any
recent turn is dropped. A short chat with nothing to retrieve is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from easyagent.context import PreparedContext, bot_context_chars, estimate_tokens, prepare_context
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


def visible_prepare(chat: dict, budget: int) -> PreparedContext:
    """Same fold as `prepare_context`, ignoring turns hidden by Start fresh.

    With no fresh floor this is `prepare_context` itself, stats included.
    """
    messages = list(chat.get("messages") or [])
    fresh = _fresh(chat, len(messages))
    through = int(chat.get("summarized_through") or 0) - fresh
    if through < 0:
        through = 0
    prepared = prepare_context(messages[fresh:], chat.get("summary") or "", through, budget)
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
    grounded = grounded_summary(rolling, messages[:through], cap)
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


def fit_context(summary: str, tail: list[dict], earlier: str, budget: int) -> tuple[str, list[dict], str, int]:
    """Keep summary + tail + earlier inside `budget`. Trim retrieval before the tail."""
    summary = summary or ""
    rows = list(tail or [])
    block = earlier or ""

    def cost(body: str, items: list[dict]) -> int:
        return len(summary) + sum(len(item.get("content") or "") for item in items) + len(body)

    if block:
        reserve = min(len(block), RETRIEVAL_RESERVE)
        while len(rows) > 1 and cost("", rows) > max(0, budget - reserve):
            rows.pop(0)
        room = budget - cost("", rows)
        block = _trim_block(block, room)
    used = cost(block, rows)
    if used > budget and rows:
        # The summary alone can meet the cap. Drop retrieval rather than exceed it.
        block = _trim_block(block, max(0, budget - cost("", rows)))
        used = cost(block, rows)
    return summary, rows, block, used


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
    prepared = visible_prepare(chat, budget)
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
    summary, tail, earlier, used = fit_context(summary, prepared.tail, earlier, budget)
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
