"""How many chats may use one connection at the same time.

A single-slot server drops a second request. Extra chats wait in line and
start when a slot is free. Stop while waiting leaves the line.
"""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar

from easyagent import turn as turn_mod

RETRY_PAUSE = 0.4

_conn: ContextVar[dict | None] = ContextVar("easyagent_connection", default=None)
_inside: ContextVar[bool] = ContextVar("easyagent_connection_hold", default=False)
_inner_retry: ContextVar[bool] = ContextVar("easyagent_inner_retry", default=True)
_lanes: dict[str, "_Lane"] = {}
_lanes_guard = threading.Lock()


def clamp_parallel(value) -> int:
    """1 when the connection has no setting. Never below 1 or above 32."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    if number < 1:
        return 1
    if number > 32:
        return 32
    return number


def is_dropped_connection(detail: str) -> bool:
    """The server hung up, or the socket was reset."""
    lowered = (detail or "").lower()
    return (
        "disconnected without sending a response" in lowered
        or "disconnected without a response" in lowered
        or "connection reset" in lowered
        or "connection aborted" in lowered
        or "econnreset" in lowered
    )


def bind_connection(endpoint: dict, bot_name: str):
    """This task's model calls use this connection's line."""
    return _conn.set(
        {
            "id": str(endpoint.get("id") or endpoint.get("base_url") or ""),
            "name": (endpoint.get("name") or "the connection").strip() or "the connection",
            "max_parallel": clamp_parallel(endpoint.get("max_parallel")),
            "bot": (bot_name or "a bot").strip() or "a bot",
        }
    )


def reset_connection(token) -> None:
    _conn.reset(token)


def suppress_inner_retry():
    """The caller retries a dropped connection once. The HTTP helper must not."""
    return _inner_retry.set(False)


def restore_inner_retry(token) -> None:
    _inner_retry.reset(token)


def inner_retry_allowed() -> bool:
    return bool(_inner_retry.get())


async def pause_retry() -> None:
    """One short wait before the same request is tried again."""
    await asyncio.sleep(RETRY_PAUSE)
    turn_mod.raise_if_cancelled()


def reset_lanes() -> None:
    """Drop every line. Tests start from an empty queue."""
    with _lanes_guard:
        _lanes.clear()


class _Seat:
    def __init__(self, bot_name: str) -> None:
        self.bot_name = bot_name
        self.ready = asyncio.Event()
        self.holding = False
        self.waiting = False
        self.label = ""
        self.lane: _Lane | None = None
        self._inside = None

    async def acquire(self) -> None:
        self._inside = _inside.set(True)
        if self.holding or self.lane is None:
            return
        try:
            while not self.ready.is_set():
                if turn_mod.cancelled():
                    raise turn_mod.TurnCancelled()
                try:
                    await asyncio.wait_for(self.ready.wait(), 0.05)
                except asyncio.TimeoutError:
                    continue
            if turn_mod.cancelled():
                raise turn_mod.TurnCancelled()
        except BaseException:
            await self.release()
            raise

    async def release(self) -> None:
        lane = self.lane
        if lane is not None:
            with lane.lock:
                if self in lane.queue:
                    lane.queue.remove(self)
                self.waiting = False
                if self.holding:
                    self.holding = False
                    if self in lane.holders:
                        lane.holders.remove(self)
                    _grant(lane)
            self.lane = None
        if self._inside is not None:
            _inside.reset(self._inside)
            self._inside = None


class _Open:
    """No connection is bound, or this call is already inside a held slot."""

    waiting = False
    label = ""

    async def acquire(self) -> None:
        return

    async def release(self) -> None:
        return


class _Lane:
    def __init__(self) -> None:
        self.holders: list[_Seat] = []
        self.queue: list[_Seat] = []
        self.lock = threading.Lock()
        self.limit = 1


def _lane(endpoint_id: str) -> _Lane:
    with _lanes_guard:
        lane = _lanes.get(endpoint_id)
        if lane is None:
            lane = _Lane()
            _lanes[endpoint_id] = lane
        return lane


def _grant(lane: _Lane) -> None:
    while lane.queue and len(lane.holders) < lane.limit:
        seat = lane.queue.pop(0)
        seat.waiting = False
        seat.holding = True
        lane.holders.append(seat)
        seat.ready.set()


def queued_label(connection: str, holders: list[str]) -> str:
    names = [name for name in holders if name]
    if len(names) == 1:
        busy = names[0]
    elif names:
        busy = ", ".join(names)
    else:
        busy = "another bot"
    who = connection or "the connection"
    return f"Queued: waiting for {who} (busy with {busy})"


async def reserve():
    """Take a slot, or return a seat that is waiting at the back of the line."""
    info = _conn.get()
    if not info or _inside.get() or not info.get("id"):
        return _Open()
    lane = _lane(info["id"])
    seat = _Seat(info["bot"])
    seat.lane = lane
    with lane.lock:
        lane.limit = clamp_parallel(info["max_parallel"])
        busy = [item.bot_name for item in lane.holders]
        if len(lane.holders) < lane.limit and not lane.queue:
            seat.holding = True
            lane.holders.append(seat)
            seat.ready.set()
        else:
            seat.waiting = True
            lane.queue.append(seat)
            seat.label = queued_label(info["name"], busy)
            print(f"heuristic queue: {seat.label}", flush=True)
    return seat
