"""Retry one model request when the server is flaky.

Connect failures, a timeout before the first token, 502/503/504, and a busy
slot are the same request again. The pause grows and then stops growing.
One turn shares a single budget for those retries, about two minutes unless
`EASYAGENT_MODEL_RETRY_SECONDS` says otherwise. When that time is gone the
harness stops the run. Stop during the pause ends the run. A stream that
already produced text is not this window: the caller may replay that turn once.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from contextvars import ContextVar
from urllib.parse import urlparse

from easyagent import turn as turn_mod

CONNECT_TIMEOUT = 10.0
DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0)
DEFAULT_BUDGET = 120.0
_URL = re.compile(r"https?://\S+")
_spent: ContextVar[float] = ContextVar("easyagent_unreachable_spent", default=0.0)
_open: ContextVar[float | None] = ContextVar("easyagent_unreachable_open", default=None)
_saw: ContextVar[bool] = ContextVar("easyagent_unreachable_saw", default=False)
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
    """How long one turn may spend retrying an unreachable model. `EASYAGENT_MODEL_RETRY_SECONDS` overrides the 2 minutes."""
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


def reset_unreachable() -> None:
    """A new turn starts with a full budget. Tests start from the same place."""
    _spent.set(0.0)
    _open.set(None)
    _saw.set(False)


def note_unreachable(attempt_started: float) -> None:
    """The model missed this attempt. Later attempts in the turn share this clock."""
    _saw.set(True)
    if _open.get() is None:
        _open.set(attempt_started)


def charge_unreachable() -> None:
    """The model answered. Keep the time already spent, and stop the open stretch."""
    opened = _open.get()
    if opened is None:
        return
    _spent.set(float(_spent.get()) + max(0.0, now() - opened))
    _open.set(None)


def unreachable_elapsed() -> float:
    opened = _open.get()
    extra = 0.0 if opened is None else max(0.0, now() - opened)
    return float(_spent.get()) + extra


def saw_unreachable() -> bool:
    return bool(_saw.get())


def host_of(base_url: str) -> str:
    """The host a person can check. A user name or password in the address is not included."""
    raw = (base_url or "").strip()
    if not raw:
        return "the saved address"
    try:
        parsed = urlparse(raw if "://" in raw else "//" + raw)
    except ValueError:
        return "the saved address"
    host = parsed.hostname or ""
    if not host:
        return "the saved address"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        return f"{host}:{port}"
    return host


def url_from_detail(detail: str) -> str:
    match = _URL.search(detail or "")
    if not match:
        return ""
    return match.group(0).rstrip(".,);\"'").rstrip(":")


def unreachable_text(base_url: str) -> str:
    """The harness sentence for a model that cannot be reached. The model does not write this."""
    return f"Can't reach the model at {host_of(base_url)}. Check the connection or address."


def is_unreachable_message(detail: str) -> bool:
    return (detail or "").startswith("Can't reach the model at ")


def model_explained_harness_failure(text: str) -> bool:
    """The model narrated a timeout or a skipped tool. That story is not the report."""
    lowered = " ".join((text or "").lower().split())
    if not lowered or is_unreachable_message(lowered):
        return False
    timeout = any(phrase in lowered for phrase in ("timed out", "timing out", "time out"))
    not_run = any(phrase in lowered for phrase in ("not run", "did not run", "didn't run", "was not run"))
    return timeout and not_run


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
    if is_unreachable_message(text):
        return True
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
    """One request's retry budget. Time is `now()`, so a test can move the clock.

    Every window in the turn shares `unreachable_elapsed`, so a new model call
    does not start the two minutes over.
    """

    def __init__(self, budget: float | None = None, base_url: str = "", long_wait: bool = False) -> None:
        self.budget = budget_seconds() if budget is None else max(0.0, float(budget))
        self.base_url = base_url or ""
        self.long_wait = bool(long_wait)
        self.started = now()
        self._attempt_started = self.started
        self.failures = 0
        self.replayed = False

    def elapsed(self) -> float:
        return max(0.0, now() - self.started)

    def begin_attempt(self) -> None:
        self._attempt_started = now()

    def spent(self) -> float:
        return max(self.elapsed(), unreachable_elapsed())

    def attempt_limit(self, default: float) -> float:
        """How long this attempt may wait. A later try cannot run past the budget."""
        if self.budget <= 0:
            return float(default)
        left = self.budget - self.spent()
        if left <= 0:
            return 0.0
        # A local grade may think longer than the reconnect budget. A hung
        # chat call still stops at the budget. After the budget is already
        # spent, neither kind starts another attempt.
        if self.long_wait:
            return float(default)
        return min(float(default), left)

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
        note_unreachable(self._attempt_started)
        if self.spent() >= self.budget:
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
        remaining = self.budget - self.spent()
        if remaining <= 0:
            delay = 0.0
        else:
            delay = min(delay, remaining)
        return label, delay

    def failure_message(self, detail: str) -> str | None:
        """The harness sentence after the window, or None to keep the original error."""
        if not retryable_before_token(detail):
            return None
        if not (self.failures or self.spent() >= self.budget):
            return None
        url = self.base_url or url_from_detail(detail)
        return unreachable_text(url)
