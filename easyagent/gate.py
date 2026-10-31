"""How many chats may use one connection at the same time.

A single-slot server drops a second request. Extra chats wait in line and
start when a slot is free. Stop while waiting leaves the line.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextvars import ContextVar

from easyagent import turn as turn_mod

RETRY_PAUSE = 0.4

_conn: ContextVar[dict | None] = ContextVar("easyagent_connection", default=None)
_inside: ContextVar[bool] = ContextVar("easyagent_connection_hold", default=False)
_inner_retry: ContextVar[bool] = ContextVar("easyagent_inner_retry", default=True)
_lanes: dict[str, "_Lane"] = {}
_lanes_guard = threading.Lock()
_wait_note: ContextVar = ContextVar("easyagent_wait_note", default=None)
_on_lane: ContextVar = ContextVar("easyagent_on_lane", default=None)


def max_yield_seconds() -> float:
    """How long a routine stays jumpable. `EASYAGENT_ROUTINE_MAX_YIELD_SECONDS` overrides 60."""
    raw = os.environ.get("EASYAGENT_ROUTINE_MAX_YIELD_SECONDS", "").strip()
    if not raw:
        return 60.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


def bind_wait_note(callback):
    """This task hears the queue sentence when it is waiting for the connection."""
    return _wait_note.set(callback)


def reset_wait_note(token) -> None:
    _wait_note.reset(token)


def bind_on_lane(callback):
    """Called once this task holds the connection. A routine starts its budget here."""
    return _on_lane.set(callback)


def reset_on_lane(token) -> None:
    _on_lane.reset(token)


def _tell_wait(label: str) -> None:
    callback = _wait_note.get()
    if callback is None or not label:
        return
    try:
        callback(label)
    except Exception:
        return


def _tell_lane() -> None:
    callback = _on_lane.get()
    if callback is None:
        return
    try:
        callback()
    except Exception:
        return


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


def bind_connection(endpoint: dict, bot_name: str, store=None):
    """This task's model calls use this connection's line.

    When `store` is passed, a rate limit or a dropped connection can try the
    saved fallbacks. Each fallback keeps the key stored for that connection.
    """
    conn_token = _conn.set(
        {
            "id": str(endpoint.get("id") or endpoint.get("base_url") or ""),
            "name": (endpoint.get("name") or "the connection").strip() or "the connection",
            "max_parallel": clamp_parallel(endpoint.get("max_parallel")),
            "bot": (bot_name or "a bot").strip() or "a bot",
        }
    )
    if store is None:
        return conn_token
    from easyagent.providers import bind_chain, chain_for

    return (conn_token, bind_chain(chain_for(store, endpoint)))


def reset_connection(token) -> None:
    if isinstance(token, tuple):
        from easyagent.providers import reset_chain

        _conn.reset(token[0])
        reset_chain(token[1])
        return
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


def lease_seconds() -> float:
    """How long one call may hold the line. A finished turn releases sooner.

    ``EASYAGENT_LANE_LEASE_SECONDS`` overrides 900, the local hard cap.
    """
    raw = os.environ.get("EASYAGENT_LANE_LEASE_SECONDS", "").strip()
    if not raw:
        return 900.0
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 900.0


def reset_lanes() -> None:
    """Drop every line. Tests start from an empty queue."""
    with _lanes_guard:
        _lanes.clear()


class _Seat:
    def __init__(self, bot_name: str, *, background: bool = False) -> None:
        self.bot_name = bot_name
        self.background = background
        self.overtaken = False
        self.queued_at = time.monotonic()
        self.ready = asyncio.Event()
        self.holding = False
        self.waiting = False
        self.label = ""
        self.lane: _Lane | None = None
        self._inside = None
        self.held_at = 0.0
        self.task: asyncio.Task | None = None
        self.slot = turn_mod.current_slot()
        try:
            self.task = asyncio.current_task()
        except RuntimeError:
            self.task = None

    async def acquire(self) -> None:
        self._inside = _inside.set(True)
        try:
            if not self.holding and self.lane is not None:
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
        if self.holding:
            if not self.held_at:
                self.held_at = time.monotonic()
            _tell_lane()

    def abandon(self) -> None:
        """Free the slot now. A stuck reader must not keep the line.

        Safe to call twice, and safe from a task that is not the one that
        acquired the seat. A queued job can start as soon as this returns.
        """
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
            try:
                _inside.reset(self._inside)
            except ValueError:
                pass
            self._inside = None

    async def release(self) -> None:
        self.abandon()


class _Open:
    """No connection is bound, or this call is already inside a held slot."""

    waiting = False
    label = ""

    async def acquire(self) -> None:
        _tell_lane()
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
        seat.held_at = time.monotonic()
        lane.holders.append(seat)
        seat.ready.set()


def _yieldable(seat: _Seat, now: float) -> bool:
    """A background seat a new chat may pass, once, during the first minute."""
    if not seat.background or seat.overtaken:
        return False
    return (now - seat.queued_at) < max_yield_seconds()


def _place(lane: _Lane, seat: _Seat) -> None:
    if seat.background:
        lane.queue.append(seat)
        return
    now = time.monotonic()
    insert_at = len(lane.queue)
    for index, existing in enumerate(lane.queue):
        if _yieldable(existing, now):
            existing.overtaken = True
            insert_at = index
            break
    lane.queue.insert(insert_at, seat)


class BusyLane(Exception):
    """This connection is already in use. A background call should try later."""


def lane_busy(endpoint_id: str) -> bool:
    """True when this connection already holds a model call."""
    if not endpoint_id:
        return False
    lane = _lane(endpoint_id)
    with lane.lock:
        return bool(lane.holders)


def user_waiting() -> bool:
    """True when a chat is lined up behind the call that holds this connection."""
    info = _conn.get()
    if not info or not info.get("id"):
        return False
    lane = _lane(info["id"])
    with lane.lock:
        return any(seat.waiting for seat in lane.queue)


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


def _chat_running(seat: _Seat) -> bool | None:
    """True when this seat's chat is still in a run. None when that cannot be read."""
    slot = seat.slot
    if slot is None or getattr(slot, "store", None) is None or not getattr(slot, "chat_id", "") or not getattr(slot, "bot_id", ""):
        return None
    try:
        chat = slot.store.get_chat(slot.bot_id, slot.chat_id)
    except Exception:
        return None
    run = chat.get("run") if isinstance(chat.get("run"), dict) else {}
    return run.get("status") == "running"


def _stale(seat: _Seat, now: float) -> bool:
    """A finished task, a chat that already went idle, or a lease that ran out."""
    if not seat.holding:
        return False
    if seat.task is not None and seat.task.done():
        return True
    age = now - (seat.held_at or seat.queued_at)
    if age >= lease_seconds():
        return True
    running = _chat_running(seat)
    if running is False and age >= 1.0:
        return True
    return False


def sweep() -> int:
    """Free seats a finished turn left behind. A queued job can start."""
    now = time.monotonic()
    with _lanes_guard:
        lanes = list(_lanes.values())
    stale: list[_Seat] = []
    for lane in lanes:
        with lane.lock:
            stale.extend(seat for seat in list(lane.holders) if _stale(seat, now))
    for seat in stale:
        seat.abandon()
    return len(stale)


def release_bot(bot_name: str) -> int:
    """Drop every seat this bot still holds or is waiting on.

    A turn that saved its reply must not leave the line busy with its name.
    """
    name = (bot_name or "").strip()
    if not name:
        return 0
    with _lanes_guard:
        lanes = list(_lanes.values())
    found: list[_Seat] = []
    for lane in lanes:
        with lane.lock:
            found.extend(seat for seat in list(lane.holders) if seat.bot_name == name)
            found.extend(seat for seat in list(lane.queue) if seat.bot_name == name)
    for seat in found:
        seat.abandon()
    return len(found)


async def reserve(*, step_aside: bool = False, background: bool = False):
    """Take a slot, or return a seat that is waiting at the back of the line.

    A background call passes step_aside so it leaves the line when a chat is
    already using the connection, instead of holding the only slot.
    A seat left behind by a finished turn is dropped first.
    """
    sweep()
    info = _conn.get()
    if not info or _inside.get() or not info.get("id"):
        return _Open()
    lane = _lane(info["id"])
    seat = _Seat(info["bot"], background=background)
    seat.lane = lane
    announced = ""
    with lane.lock:
        lane.limit = clamp_parallel(info["max_parallel"])
        busy = [item.bot_name for item in lane.holders]
        if step_aside and not background and (lane.holders or lane.queue):
            raise BusyLane()
        if len(lane.holders) < lane.limit and not lane.queue:
            seat.holding = True
            seat.held_at = time.monotonic()
            lane.holders.append(seat)
            seat.ready.set()
        else:
            seat.waiting = True
            _place(lane, seat)
            seat.label = queued_label(info["name"], busy)
            print(f"heuristic queue: {seat.label}", flush=True)
            if len(lane.holders) < lane.limit:
                _grant(lane)
            if seat.waiting:
                announced = seat.label
    if announced:
        _tell_wait(announced)
    return seat
