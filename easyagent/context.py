"""Build the bounded view the model sees.

The stored message list is never trimmed. `prepare_context` only chooses a
suffix (the tail) and folds everything before that suffix into a short summary.
"""

from __future__ import annotations

from dataclasses import dataclass

from easyagent.limits import (
    CHARS_PER_TOKEN,
    EXCERPT_CHARS,
    MAX_CONTEXT_TOKENS,
    MAX_SUMMARY_CHARS,
    MIN_CONTEXT_TOKENS,
    MIN_MESSAGE_PAYLOAD_CHARS,
    MIN_SUMMARY_CHARS,
    default_context_tokens,
)

TRUNCATION_NOTICE = "\n[truncated in context; full message is on disk]"
ERROR_PLACEHOLDER = (
    "(An earlier request to the model server failed here. That was a connection error, "
    "not something said in this chat. The current connection is used for this turn.)"
)


@dataclass(frozen=True)
class PreparedContext:
    summary: str
    summarized_through: int
    tail: list[dict]
    stats: dict


def estimate_tokens(chars: int) -> int:
    """Rough token count for a number of characters."""
    if chars <= 0:
        return 0
    return -(-int(chars) // CHARS_PER_TOKEN)


def tokens_to_chars(tokens: int) -> int:
    return int(tokens) * CHARS_PER_TOKEN


def clean_tokens(value) -> int | None:
    """A whole number of tokens inside the allowed range, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < MIN_CONTEXT_TOKENS or number > MAX_CONTEXT_TOKENS:
        return None
    return number


def context_window(context_chars: int | None) -> tuple[int, int, int]:
    """Split a character budget into (budget, summary cap, per-message cap).

    The budget is the chat budget in characters (tokens * CHARS_PER_TOKEN).
    The summary cap only matters once the chat is over budget. A single
    message is clipped only when it would take more than half the room left
    for the recent chat.
    """
    if context_chars is None:
        budget = tokens_to_chars(default_context_tokens())
    else:
        budget = int(context_chars)
    low = tokens_to_chars(MIN_CONTEXT_TOKENS)
    high = tokens_to_chars(MAX_CONTEXT_TOKENS)
    if budget < low:
        budget = low
    if budget > high:
        budget = high
    summary_cap = min(MAX_SUMMARY_CHARS, max(MIN_SUMMARY_CHARS, budget // 10))
    if summary_cap >= budget // 2:
        summary_cap = max(80, budget // 5)
    tail_budget = max(1, budget - summary_cap)
    per_message = max(MIN_MESSAGE_PAYLOAD_CHARS, tail_budget // 2)
    per_message = min(per_message, tail_budget)
    return budget, summary_cap, per_message


def bot_context_tokens(bot: dict | None) -> int:
    """The token budget saved on a bot, or the default.

    A bot saved before budgets were counted in tokens has only context_chars,
    capped at 7,200 characters. That old value is ignored, so the bot gets the
    default token budget instead of a tiny window.
    """
    if bot:
        value = clean_tokens(bot.get("context_tokens"))
        if value is not None:
            return value
    return default_context_tokens()


def bot_context_chars(bot: dict | None) -> int:
    """The bot's chat budget in characters, for the character-based fold."""
    budget, _, _ = context_window(tokens_to_chars(bot_context_tokens(bot)))
    return budget


def clip_message(content: str, limit: int | None = None) -> str:
    cap = MIN_MESSAGE_PAYLOAD_CHARS if limit is None else int(limit)
    if cap < 1:
        cap = 1
    text = content or ""
    if len(text) <= cap:
        return text
    keep = cap - len(TRUNCATION_NOTICE)
    if keep < 1:
        return text[:cap]
    return text[:keep] + TRUNCATION_NOTICE


def model_content(message: dict) -> str:
    """What the model sees for one stored message.

    A stored error line (for example a connection error that names an old
    server address) is not replayed. The model gets a short neutral note.
    """
    if message.get("error"):
        return ERROR_PLACEHOLDER
    return message.get("content") or ""


def _payload_len(content: str, limit: int | None = None) -> int:
    return len(clip_message(content, limit))


def _fold_line(message: dict) -> str:
    line = f"- {message.get('role', 'user')}: {_excerpt(model_content(message))}"
    emoji = message.get("reaction")
    if isinstance(emoji, str) and emoji:
        line += f" [person reacted {emoji}]"
    return line


def _excerpt(content: str) -> str:
    text = " ".join((content or "").split())
    if len(text) <= EXCERPT_CHARS:
        return text
    return text[: EXCERPT_CHARS - 1] + "…"


def clamp_summary(existing: str, block: str, summary_cap: int = MAX_SUMMARY_CHARS) -> str:
    parts = [part.strip() for part in (existing, block) if part and part.strip()]
    combined = "\n".join(parts)
    if len(combined) <= summary_cap:
        return combined
    lines = combined.splitlines()
    reserve = min(80, max(20, summary_cap // 4))
    budget = summary_cap - reserve
    limit = max(budget, 0)
    lengths = [len(line) for line in lines]
    prefix = [0]
    for length in lengths:
        prefix.append(prefix[-1] + length)

    def joined_from(start: int) -> int:
        count = len(lines) - start
        if count <= 0:
            return 0
        return (prefix[-1] - prefix[start]) + (count - 1)

    dropped = 0
    if lines and joined_from(0) > limit:
        low = 0
        high = len(lines)
        while low < high:
            mid = (low + high) // 2
            if joined_from(mid) > limit:
                low = mid + 1
            else:
                high = mid
        dropped = low
        lines = lines[dropped:]
    prefix = f"[Earlier summary compacted; {dropped} lines dropped.]"
    body = "\n".join(lines).strip()
    result = prefix if not body else f"{prefix}\n{body}"
    if len(result) > summary_cap:
        result = result[: summary_cap - 1] + "…"
    return result


def prepare_context(
    messages: list[dict],
    summary: str,
    summarized_through: int,
    context_chars: int | None = None,
) -> PreparedContext:
    """Return the summary + recent chat for one request. Does not mutate `messages`.

    `context_chars` is the bot's chat budget in characters (see
    `bot_context_chars`). Omit it to use the default. Every recent message
    goes in, newest first, until the next older one would not fit. Only the
    messages before that point are folded into the summary. A chat that fits
    is sent whole and nothing is compacted. The stored list is not trimmed.
    """
    budget, summary_cap, per_message = context_window(context_chars)
    tail_budget = max(1, budget - summary_cap)
    summary = summary or ""
    count = len(messages)
    through = summarized_through or 0
    if through < 0:
        through = 0
    if through > count:
        through = count

    if count == 0:
        if len(summary) > summary_cap:
            summary = clamp_summary("", summary, summary_cap)
        stats = _stats(messages, summary, through, [], budget, summary_cap)
        return PreparedContext(summary, through, [], stats)

    start = count
    used = 0
    for index in range(count - 1, -1, -1):
        cost = _payload_len(model_content(messages[index]), per_message)
        if index < count - 1 and used + cost > tail_budget:
            break
        used += cost
        start = index

    if start == 0:
        summary = ""
    elif start > through:
        folded = "\n".join(_fold_line(m) for m in messages[through:start])
        summary = clamp_summary(summary, folded, summary_cap)
    elif start < through:
        folded = "\n".join(_fold_line(m) for m in messages[:start])
        summary = clamp_summary("", folded, summary_cap)
    elif len(summary) > summary_cap:
        summary = clamp_summary("", summary, summary_cap)
    through = start

    tail = [
        {"role": m.get("role", "user"), "content": clip_message(model_content(m), per_message)}
        for m in messages[through:]
    ]
    stats = _stats(messages, summary, through, tail, budget, summary_cap)
    return PreparedContext(summary, through, tail, stats)


def _stats(
    messages: list[dict],
    summary: str,
    through: int,
    tail: list[dict],
    budget: int | None = None,
    summary_cap: int = MAX_SUMMARY_CHARS,
) -> dict:
    if budget is None:
        budget = tokens_to_chars(default_context_tokens())
    tail_chars = sum(len(m["content"]) for m in tail)
    summary_chars = len(summary)
    transcript_chars = sum(len(m.get("content") or "") for m in messages)
    context_chars = summary_chars + tail_chars
    return {
        "transcript_messages": len(messages),
        "transcript_chars": transcript_chars,
        "transcript_tokens": estimate_tokens(transcript_chars),
        "model_messages": len(tail),
        "summary_chars": summary_chars,
        "tail_chars": tail_chars,
        "context_chars": context_chars,
        "context_tokens": estimate_tokens(context_chars),
        "summarized_through": through,
        "compacted_messages": through,
        "compacted": through > 0,
        "max_context_chars": budget,
        "max_context_tokens": budget // CHARS_PER_TOKEN,
        "chars_per_token": CHARS_PER_TOKEN,
        "bounded": context_chars <= budget and summary_chars <= summary_cap,
    }
