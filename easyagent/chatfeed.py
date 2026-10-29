"""Push a saved chat line to a browser that already has that chat open."""

from __future__ import annotations

import asyncio
import threading

_GUARD = threading.Lock()
_FEEDS: dict[tuple[str, str], list[asyncio.Queue]] = {}


def publish(bot_id: str, chat_id: str, payload: dict) -> None:
    """Hand one event to every open view of this chat. No listener is fine."""
    key = (str(bot_id or ""), str(chat_id or ""))
    with _GUARD:
        queues = list(_FEEDS.get(key) or [])
    for queue in queues:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            continue


def subscribe(bot_id: str, chat_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    key = (str(bot_id or ""), str(chat_id or ""))
    with _GUARD:
        _FEEDS.setdefault(key, []).append(queue)
    return queue


def unsubscribe(bot_id: str, chat_id: str, queue: asyncio.Queue) -> None:
    key = (str(bot_id or ""), str(chat_id or ""))
    with _GUARD:
        rows = _FEEDS.get(key) or []
        if queue in rows:
            rows.remove(queue)
        if not rows:
            _FEEDS.pop(key, None)
