"""Stop a model request that keeps generating.

A local server can ignore max_tokens and hold the socket open while tokens
arrive, so the read timeout never fires. This watch is the client-side cap:
length, silence, wall clock, and a chunk that repeats.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlparse

RUNAWAY_TEXT = "The model kept generating without finishing; stopped it. Try again or lower the length."

DEFAULT_MAX_TOKENS = 4096
DEFAULT_STALL_SECONDS = 45.0
DEFAULT_FIRST_TOKEN_SECONDS = 120.0
LOCAL_FIRST_TOKEN_SECONDS = 600.0
DEFAULT_WALL_SECONDS = 180.0
DEFAULT_HARD_SECONDS = 480.0
LOCAL_HARD_SECONDS = 900.0
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


def is_local_base(url: str) -> bool:
    """A model on this machine or the private network. Cloud hosts stay on the shorter waits."""
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw if "://" in raw else "//" + raw)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        first, second = int(parts[0]), int(parts[1])
        if first == 10 or (first == 192 and second == 168):
            return True
        if first == 172 and 16 <= second <= 31:
            return True
    return False


def still_working_text(seconds: float) -> str:
    """The socket is open. The model has not written a first word yet."""
    minutes = max(1, int(round(float(seconds) / 60.0)))
    return f"Model is busy/slow. No first word after {minutes} min. Wait longer or retry."


def is_still_working(detail: str) -> bool:
    text = detail or ""
    return text.startswith("Model is busy/slow") or text.startswith(
        "The model is connected but hasn't started answering"
    )


def limits_from_endpoint(endpoint: dict | None) -> Limits:
    """Connection settings, or the defaults. A missing length is 4096.

    A local or private-network address waits 300 s for the first word and 900 s
    for the whole reply when those fields were left blank.
    """
    source = endpoint if isinstance(endpoint, dict) else {}
    local = is_local_base(str(source.get("base_url") or ""))
    first_default = LOCAL_FIRST_TOKEN_SECONDS if local and source.get("first_token_seconds") in (None, "") else DEFAULT_FIRST_TOKEN_SECONDS
    hard_default = LOCAL_HARD_SECONDS if local and source.get("hard_seconds") in (None, "") else DEFAULT_HARD_SECONDS
    stall = _whole(source.get("stall_seconds"), DEFAULT_STALL_SECONDS, 5.0, 600.0)
    first = _whole(source.get("first_token_seconds"), first_default, 5.0, 1200.0)
    if first < stall:
        first = stall
    wall = _whole(source.get("wall_seconds"), DEFAULT_WALL_SECONDS, 5.0, 3600.0)
    hard = _whole(source.get("hard_seconds"), hard_default, wall, 7200.0)
    return Limits(
        max_tokens=effective_max_tokens(source),
        stall_seconds=stall,
        first_token_seconds=first,
        wall_seconds=wall,
        hard_seconds=hard,
        tool_temperature=effective_tool_temperature(source),
    )


_limits: ContextVar[Limits | None] = ContextVar("easyagent_generation_limits", default=None)
_first_scale: ContextVar[float] = ContextVar("easyagent_first_scale", default=1.0)


def bind_limits(endpoint: dict | None):
    """This task's model calls use this connection's length cap."""
    return _limits.set(limits_from_endpoint(endpoint))


def reset_limits(token) -> None:
    _limits.reset(token)


def bind_first_scale(scale: float):
    """This turn may wait longer for the first word. 1 keeps the connection setting."""
    try:
        number = float(scale)
    except (TypeError, ValueError):
        number = 1.0
    if number < 1:
        number = 1.0
    return _first_scale.set(number)


def reset_first_scale(token) -> None:
    _first_scale.reset(token)


def current_limits() -> Limits:
    found = _limits.get()
    if found is None:
        found = limits_from_endpoint(None)
    scale = _first_scale.get()
    if scale <= 1:
        return found
    first = min(600.0, found.first_token_seconds * scale)
    if first <= found.first_token_seconds:
        return found
    return Limits(
        max_tokens=found.max_tokens,
        stall_seconds=found.stall_seconds,
        first_token_seconds=first,
        wall_seconds=found.wall_seconds,
        hard_seconds=found.hard_seconds,
        tool_temperature=found.tool_temperature,
    )


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
