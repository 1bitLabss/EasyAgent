"""Find a few earlier stretches that share words with the message just sent.

No embeddings and no vector table. The match is the words in that message
against transcripts already on disk. The full transcript is not rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from easyagent.store import Store, StoreError
from easyagent.tools import redact

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    """
    that this with from have were your what when where which about just they
    them then than into over also some only been will would could should there
    their here because before after under again other another these those very
    onto does didn wasn isn aren don can not but and the for you are was his her
    its who how all any our out too
    """.split()
)
_MAX_STRETCHES = 3
_MAX_LINE = 320
_MAX_BLOCK = 1200
_MIN_SHARED = 2
_MIN_WORD = 4


def words(text: str) -> set[str]:
    found = set()
    for word in _WORD.findall((text or "").lower()):
        if len(word) < _MIN_WORD or word in _STOP:
            continue
        found.add(word)
    return found


@dataclass
class _Hit:
    title: str
    index: int
    role: str
    text: str
    shared: int


def _clip(text: str) -> str:
    body = " ".join((text or "").split())
    if len(body) <= _MAX_LINE:
        return body
    return body[: _MAX_LINE - 1].rstrip() + "…"


def _query(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


def _hits(title: str, messages: list[dict], query_words: set[str], *, end: int) -> list[_Hit]:
    found = []
    limit = min(end, len(messages))
    for index in range(limit):
        message = messages[index]
        text = message.get("content") or ""
        shared = query_words & words(text)
        if len(shared) < _MIN_SHARED:
            continue
        found.append(
            _Hit(
                title=title,
                index=index,
                role=message.get("role") or "user",
                text=text,
                shared=len(shared),
            )
        )
    return found


def _block(hits: list[_Hit]) -> str:
    if not hits:
        return ""
    ranked = sorted(hits, key=lambda hit: (hit.shared, hit.index), reverse=True)[:_MAX_STRETCHES]
    ranked.sort(key=lambda hit: (hit.title, hit.index))
    lines = [
        "Short stretches from saved chats. Not the whole history. The full transcript stays on disk.",
        "You can use these lines. They are earlier turns, not a guess.",
    ]
    for hit in ranked:
        title = " ".join((hit.title or "Chat").split()) or "Chat"
        lines.append(f"From {title}:")
        lines.append(f"{hit.role}: {_clip(hit.text)}")
    text = "\n".join(lines).strip()
    if len(text) <= _MAX_BLOCK:
        return text
    return text[: _MAX_BLOCK - 1].rstrip() + "…"


def scan_chats(store: Store, bot_id: str, chat: dict, skip_through: int) -> str:
    """Stretches outside the recent tail that share at least two words with the new message.

    An empty result means the prompt is unchanged. This does not write a chat.
    """
    messages = list(chat.get("messages") or [])
    query_words = words(_query(messages))
    if len(query_words) < _MIN_SHARED:
        return ""
    title = chat.get("title") or "New chat"
    through = skip_through if skip_through > 0 else 0
    if through > len(messages):
        through = len(messages)
    hits = _hits(title, messages, query_words, end=through)
    current_id = chat.get("id")
    try:
        listed = store.list_chats(bot_id)
    except StoreError:
        listed = []
    for item in listed:
        if item.get("id") == current_id:
            continue
        try:
            other = store.get_chat(bot_id, item["id"])
        except StoreError:
            continue
        hits.extend(
            _hits(
                other.get("title") or "New chat",
                list(other.get("messages") or []),
                query_words,
                end=len(other.get("messages") or []),
            )
        )
    block = _block(hits)
    if not block:
        return ""
    cleaned = redact(store, block).strip()
    return cleaned
