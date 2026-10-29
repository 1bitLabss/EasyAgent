"""Push a saved chat line to a browser that already has that chat open."""

from __future__ import annotations

import asyncio
import threading

_GUARD = threading.Lock()
_FEEDS: dict[tuple[str, str], list[asyncio.Queue]] = {}
_LOOP: asyncio.AbstractEventLoop | None = None


def publish(bot_id: str, chat_id: str, payload: dict) -> None:
    """Hand one event to every open view of this chat. No listener is fine."""
    key = (str(bot_id or ""), str(chat_id or ""))
    with _GUARD:
        queues = list(_FEEDS.get(key) or [])

    def _put(queue: asyncio.Queue) -> None:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            return

    try:
        on_loop = asyncio.get_running_loop()
    except RuntimeError:
        on_loop = None
    for queue in queues:
        if on_loop is not None:
            _put(queue)
        elif _LOOP is not None and _LOOP.is_running():
            _LOOP.call_soon_threadsafe(_put, queue)
        else:
            _put(queue)


def subscribe(bot_id: str, chat_id: str) -> asyncio.Queue:
    global _LOOP
    try:
        _LOOP = asyncio.get_running_loop()
    except RuntimeError:
        pass
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
