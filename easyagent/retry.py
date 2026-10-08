"""Retry one model request when the server is flaky.

Connect failures, a timeout before the first token, 502/503/504, and a busy
slot are the same request again. The pause grows and then stops growing.
Stop during the pause ends the run. A stream that already produced text is
not this window: the caller may replay that turn once.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time

from easyagent import turn as turn_mod

CONNECT_TIMEOUT = 10.0
DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0)
DEFAULT_BUDGET = 180.0
_BUSY = ("server busy", "slot unavailable", "no slot")
_DROP = (
    "disconnected without sending a response",
    "disconnected without a response",
    "connection reset",
    "connection aborted",
    "connection refused",
    "econnreset",
    "all connection attempts failed",
    "could not reach",
)

_log = logging.getLogger("easyagent.retry")


def budget_seconds() -> float:
    """How long to keep retrying one request. `EASYAGENT_MODEL_RETRY_SECONDS` overrides the 3 minutes."""
    raw = os.environ.get("EASYAGENT_MODEL_RETRY_SECONDS", "").strip()
    if not raw:
        return DEFAULT_BUDGET
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_BUDGET
    if value < 0:
        return 0.0
    return value


def now() -> float:
    return time.monotonic()


def delay_for(attempt: int, rng: random.Random | None = None) -> float:
    """Pause after failure `attempt` (1 is the first failure).

    The base is 1s, 2s, 4s, 8s, then 15s. Jitter adds up to a quarter of that
    base. The pause never goes past 15s.
    """
    index = min(max(int(attempt), 1), len(DELAYS)) - 1
    base = DELAYS[index]
    roll = (rng or random).random()
    delay = base + (base * 0.25 * roll)
    if delay > 15.0:
        return 15.0
    return delay


def retried_phrase(elapsed: float) -> str:
    seconds = max(0, int(round(elapsed)))
    if seconds < 60:
        span = f"{seconds}s"
    else:
        minutes, rest = divmod(seconds, 60)
        span = f"{minutes}m" if rest == 0 else f"{minutes}m {rest}s"
    return f"Retried for {span}."


def gave_up(detail: str, elapsed: float) -> str:
    text = (detail or "").strip() or "The model server did not answer."
    phrase = retried_phrase(elapsed)
    if phrase in text:
        return text
    return f"{text} {phrase}"


def retryable_before_token(detail: str) -> bool:
    """A failure before any token. An incomplete chunked read is a stop, not this."""
    text = detail or ""
    lowered = text.lower()
    if "incomplete chunked read" in lowered:
        return False
    if "endpoint returned an empty message" in lowered:
        return False
    if lowered.startswith("timed out") or "timed out" in lowered:
        return True
    if any(phrase in lowered for phrase in _DROP):
        return True
    if any(phrase in lowered for phrase in _BUSY):
        return True
    return bool(re.match(r"(502|503|504)\b", text.strip()))


def midstream_drop(detail: str) -> bool:
    """The socket died after text had started. One replay, and not a client abort."""
    if "incomplete chunked read" in (detail or "").lower():
        return False
    return retryable_before_token(detail)


def note_attempt(label: str, detail: str) -> None:
    print(f"heuristic reconnect: {label} {detail}", flush=True)
    _log.info("%s %s", label, detail)


async def _sleep(seconds: float) -> None:
    """Wait, but notice Stop within a short slice instead of after the whole pause."""
    turn_mod.raise_if_cancelled()
    left = max(0.0, float(seconds))
    while left > 0:
        turn_mod.raise_if_cancelled()
        step = 0.05 if left > 0.05 else left
        await asyncio.sleep(step)
        left -= step
    turn_mod.raise_if_cancelled()


async def pause(seconds: float) -> None:
    await _sleep(seconds)


class Window:
    """One request's retry budget. Time is `now()`, so a test can move the clock."""

    def __init__(self, budget: float | None = None) -> None:
        self.budget = budget_seconds() if budget is None else max(0.0, float(budget))
        self.started = now()
        self.failures = 0
        self.replayed = False

    def elapsed(self) -> float:
        return max(0.0, now() - self.started)

    def plan(self, detail: str, *, started: bool) -> str | None:
        """'again' for another try, 'replay' for the one mid-stream redo, or None to stop."""
        if "incomplete chunked read" in (detail or "").lower():
            return None
        if started:
            if self.replayed or not midstream_drop(detail):
                return None
            return "replay"
        if not retryable_before_token(detail):
            return None
        if self.elapsed() >= self.budget:
            return None
        return "again"

    def arm(self, kind: str, detail: str) -> tuple[str, float]:
        """Record this retry and return the status line plus how long to wait."""
        upcoming = self.failures + 2
        self.failures += 1
        if kind == "replay":
            self.replayed = True
        label = f"Model not answering, retrying (attempt {upcoming})…"
        note_attempt(label, detail)
        delay = delay_for(1 if kind == "replay" else self.failures)
        remaining = self.budget - self.elapsed()
        if remaining <= 0:
            delay = 0.0
        else:
            delay = min(delay, remaining)
        return label, delay

    def failure_message(self, detail: str) -> str | None:
        """The Stopped detail after the window, or None to keep the original error."""
        if self.failures and retryable_before_token(detail):
            return gave_up(detail, self.elapsed())
        return None
