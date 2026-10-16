"""Stop a model request that keeps generating.

A local server can ignore max_tokens and hold the socket open while tokens
arrive, so the read timeout never fires. This watch is the client-side cap:
length, silence, wall clock, and a chunk that repeats.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass

RUNAWAY_TEXT = "The model kept generating without finishing; stopped it. Try again or lower the length."

DEFAULT_MAX_TOKENS = 4096
DEFAULT_STALL_SECONDS = 45.0
DEFAULT_FIRST_TOKEN_SECONDS = 120.0
DEFAULT_WALL_SECONDS = 180.0
DEFAULT_HARD_SECONDS = 480.0
DEFAULT_TOOL_TEMPERATURE = 0.6
MIN_MAX_TOKENS = 256
MAX_MAX_TOKENS = 32768
REPEAT_CHUNK = 6
REPEAT_TAIL = 48
REPEAT_TAIL_TIMES = 3


class GenerationLimit(Exception):
    """This request was stopped. The chat can resume, and the queue is kept."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(RUNAWAY_TEXT)


@dataclass(frozen=True)
class Limits:
    max_tokens: int = DEFAULT_MAX_TOKENS
    stall_seconds: float = DEFAULT_STALL_SECONDS
    first_token_seconds: float = DEFAULT_FIRST_TOKEN_SECONDS
    wall_seconds: float = DEFAULT_WALL_SECONDS
    hard_seconds: float = DEFAULT_HARD_SECONDS
    tool_temperature: float = DEFAULT_TOOL_TEMPERATURE


def _whole(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < low:
        return low
    if number > high:
        return high
    return number


def effective_max_tokens(endpoint: dict | None) -> int:
    raw = None if not isinstance(endpoint, dict) else endpoint.get("max_tokens")
    if raw in (None, ""):
        return DEFAULT_MAX_TOKENS
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOKENS
    if number < MIN_MAX_TOKENS:
        return MIN_MAX_TOKENS
    if number > MAX_MAX_TOKENS:
        return MAX_MAX_TOKENS
    return number


def effective_tool_temperature(endpoint: dict | None) -> float:
    """Sampling for a request that includes tools. Blank keeps 0.6."""
    raw = None if not isinstance(endpoint, dict) else endpoint.get("tool_temperature")
    if raw in (None, ""):
        return DEFAULT_TOOL_TEMPERATURE
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TOOL_TEMPERATURE
    if number < 0:
        return 0.0
    if number > 2:
        return 2.0
    return number


def tool_try_temperature(base: float, index: int) -> float:
    """First tool request uses the connection value. The next try is half. Later tries are 0.1."""
    number = effective_tool_temperature({"tool_temperature": base})
    if index <= 0:
        return round(number, 4)
    if index == 1:
        return round(number / 2.0, 4)
    return 0.1


def limits_from_endpoint(endpoint: dict | None) -> Limits:
    """Connection settings, or the defaults. A missing length is 4096."""
    source = endpoint if isinstance(endpoint, dict) else {}
    stall = _whole(source.get("stall_seconds"), DEFAULT_STALL_SECONDS, 5.0, 600.0)
    first = _whole(source.get("first_token_seconds"), DEFAULT_FIRST_TOKEN_SECONDS, 5.0, 600.0)
    if first < stall:
        first = stall
    wall = _whole(source.get("wall_seconds"), DEFAULT_WALL_SECONDS, 5.0, 3600.0)
    hard = _whole(source.get("hard_seconds"), DEFAULT_HARD_SECONDS, wall, 7200.0)
    return Limits(
        max_tokens=effective_max_tokens(source),
        stall_seconds=stall,
        first_token_seconds=first,
        wall_seconds=wall,
        hard_seconds=hard,
        tool_temperature=effective_tool_temperature(source),
    )


_limits: ContextVar[Limits | None] = ContextVar("easyagent_generation_limits", default=None)


def bind_limits(endpoint: dict | None):
    """This task's model calls use this connection's length cap."""
    return _limits.set(limits_from_endpoint(endpoint))


def reset_limits(token) -> None:
    _limits.reset(token)


def current_limits() -> Limits:
    found = _limits.get()
    if found is None:
        return limits_from_endpoint(None)
    return found


class GenerationWatch:
    """One request. `clock` returns seconds and is injectable so tests do not sleep."""

    def __init__(self, limits: Limits | None = None, clock=None) -> None:
        self.limits = limits or current_limits()
        self.clock = clock or time.monotonic
        now = float(self.clock())
        self.started = now
        self.last_token_at = now
        self.tokens = 0
        self.saw_token = False
        self._last = ""
        self._same = 0
        self._buf = ""

    def observe(self, piece: str) -> None:
        now = float(self.clock())
        if piece:
            self.tokens += max(1, len(piece) // 4)
            self.last_token_at = now
            self.saw_token = True
            self._repeat(piece)
        self._check(now)

    def poll(self) -> None:
        """No new chunk. Silence and the wall clock still count."""
        self._check(float(self.clock()))

    def _check(self, now: float) -> None:
        limits = self.limits
        if self.tokens >= limits.max_tokens:
            raise GenerationLimit("max_tokens")
        elapsed = now - self.started
        if elapsed >= limits.hard_seconds:
            raise GenerationLimit("hard")
        silent = now - self.last_token_at
        quiet = limits.stall_seconds if self.saw_token else limits.first_token_seconds
        if silent >= quiet:
            raise GenerationLimit("stall" if self.saw_token else "first_token")
        if elapsed >= limits.wall_seconds and silent >= limits.stall_seconds and self.saw_token:
            raise GenerationLimit("wall")

    def _repeat(self, piece: str) -> None:
        if not piece.strip():
            return
        if piece == self._last:
            self._same += 1
        else:
            self._last = piece
            self._same = 1
        if self._same >= REPEAT_CHUNK:
            raise GenerationLimit("repeat")
        self._buf = (self._buf + piece)[-REPEAT_TAIL * REPEAT_TAIL_TIMES :]
        tail = self._buf[-REPEAT_TAIL:]
        if len(tail) >= 16 and self._buf.endswith(tail * REPEAT_TAIL_TIMES):
            raise GenerationLimit("repeat")
