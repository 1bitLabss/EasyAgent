"""A short queue between a producer and a slow reader.

Pieces of the same kind merge. The last signal is kept. The producer never waits.
"""

from __future__ import annotations

import asyncio

_CONTENT = {"text", "thinking", "chunk"}
_SIGNAL = {"end", "error", "start", "done"}


def _kind(item) -> str:
    if isinstance(item, tuple) and item:
        return str(item[0] or "")
    return ""


def _join(left, right):
    if isinstance(left, (bytes, bytearray)) or isinstance(right, (bytes, bytearray)):
        return (left or b"") + (right or b"")
    return (left or "") + (right or "")


def _protected(item) -> bool:
    if item is None:
        return True
    if _kind(item) in _SIGNAL:
        return True
    if isinstance(item, str) and any(
        marker in item for marker in ('"type": "done"', '"type": "error"', '"type": "stopped"', '"type":"done"')
    ):
        return True
    return False


def _merge(held: list, item) -> list:
    """Merge model text and relay bytes. Viewer events stay separate so old ones can be dropped."""
    kind = _kind(item)
    if kind in _CONTENT:
        for index in range(len(held) - 1, -1, -1):
            if _kind(held[index]) == kind:
                held[index] = (kind, _join(held[index][1], item[1]))
                return held
    held.append(item)
    return held


def _trim(held: list, limit: int) -> list:
    """Drop the oldest droppable piece until the list fits. The tail stays."""
    if limit < 1:
        limit = 1
    while len(held) > limit:
        drop_at = None
        for index, item in enumerate(held[:-1]):
            if not _protected(item):
                drop_at = index
                break
        if drop_at is None and len(held) > 1 and not _protected(held[0]):
            drop_at = 0
        if drop_at is None:
            break
        del held[drop_at]
    return held


def offer(queue: asyncio.Queue, item, *, final: bool = False) -> None:
    """Add item without waiting. A full queue merges or drops older pieces.

    ``final`` is the closing signal. It is not dropped.
    """
    held: list = []
    while True:
        try:
            held.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    if final or _kind(item) in _SIGNAL or item is None:
        held.append(item)
    else:
        held = _merge(held, item)
    limit = queue.maxsize or 8
    held = _trim(held, limit)
    for old in held:
        try:
            queue.put_nowait(old)
        except asyncio.QueueFull:
            return
