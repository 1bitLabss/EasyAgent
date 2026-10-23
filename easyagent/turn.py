"""One running turn per chat. A different chat does not share its cancel flag."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

import httpx


class TurnCancelled(Exception):
    """This chat's turn was stopped. Another chat's turn is left alone."""


class UserPause(Exception):
    """A message arrived. This reply stops so the next one can answer it."""


class Slot:
    """Mutable state for one chat. A second chat has a different slot."""

    def __init__(self) -> None:
        self.cancel = asyncio.Event()
        self.client: httpx.AsyncClient | None = None
        self.proc: subprocess.Popen | None = None
        self.reason = ""
        self.user_stop = False
        self.run_id = ""
        self.bot_id = ""
        self.chat_id = ""
        self.cwd = str(Path.home())
        self.env: dict[str, str] | None = None
        self.worker: asyncio.Task | None = None
        self.store = None
        self.busy = False
        self.job_started = ""
        self.follow_chain = 0
        self.resend_id = ""
        self.redirect = False
        self.drain_chains = 0
        self.deliver_queue = False
        self.sealed = False
        self.claimed_at = 0.0
        self.child_tasks: list = []
        self.child_procs: list = []
        # Hermes interrupt_control fields. The queue file is the durable FIFO;
        # these hold only the in-flight steer and redirect for this turn.
        self._pending_steer: str | None = None
        self._pending_steer_lock = threading.Lock()
        self._pending_redirect: str | None = None
        self._pending_redirect_lock = threading.Lock()
        self._interrupt_requested = False
        self._interrupt_message: str | None = None
        self._executing_tools = False
        self._model_request_active = threading.Event()
        self._turn_user_intervened = False
        self._supports_active_turn_redirect = True
        self.pause_for_user = False
        self.side_answer = False
        # True only while the model read is waiting on the next line, not while a token is being saved.
        self.in_model_read = False
        # True after this reply's tokens are in hand, until the next model call or tool.
        # A message in that window is the next turn. It does not pause a finished reply.
        self.sealing = False
        self.late_arrival = False
        # A pause cut the model or a tool. Say-continue is only for that.
        # A sealed final answer sets sealed_answer and leaves this false.
        self.cut_unfinished = False
        self.sealed_answer = False
        self.listeners: list = []
        self.notify = None


_GUARD = threading.Lock()
_SLOTS: dict[str, Slot] = {}
_WORKERS: set[asyncio.Task] = set()
_BOUNDARIES: dict[str, asyncio.Lock] = {}
_cancel: ContextVar[asyncio.Event | None] = ContextVar("easyagent_turn_cancel", default=None)
_slot: ContextVar[Slot | None] = ContextVar("easyagent_turn_slot", default=None)


def _key(store, chat_id: str) -> str:
    return f"{getattr(store, 'root', '')}:{chat_id}"


def boundary(store, chat_id: str) -> asyncio.Lock:
    """Serializes the busy claim and the idle handoff. The model call does not hold it."""
    key = _key(store, chat_id)
    lock = _BOUNDARIES.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _BOUNDARIES[key] = lock
    return lock


def slot_for(store, chat_id: str) -> Slot:
    key = _key(store, chat_id)
    with _GUARD:
        slot = _SLOTS.get(key)
        if slot is None:
            slot = Slot()
            _SLOTS[key] = slot
        return slot


def cancelled() -> bool:
    """True when this turn was stopped. A finished turn's flag does not cancel a later one."""
    event = _cancel.get()
    if event is None or not event.is_set():
        return False
    slot = _slot.get()
    if slot is not None and slot.cancel is event:
        worker = slot.worker
        if worker is None or worker.done():
            return False
    return True


def raise_if_cancelled() -> None:
    if cancelled():
        raise TurnCancelled()


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
        if proc.poll() is None:
            proc.kill()
        return
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        kill = getattr(proc, "kill", None)
        if callable(kill):
            try:
                kill()
            except Exception:
                return
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        proc.kill()


async def _close_client(client: httpx.AsyncClient) -> None:
    try:
        await client.aclose()
    except Exception:
        return


# Two short peeks (50ms each) decide whether the reply already ended.
# The socket drops after that, still inside the 200ms pause budget.
_PAUSE_CLOSE_DELAY = 0.16


async def _close_client_after(client: httpx.AsyncClient, delay: float) -> None:
    """Drop the socket after a finished reply has had a moment to notice the pause."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    await _close_client(client)


def interrupt(store, chat_id: str, reason: str = "") -> None:
    """Abort the model request and any command for this chat. Nothing running is fine.

    A different chat's slot is not touched. Closing the browser view is not an interrupt.
    """
    slot = slot_for(store, chat_id)
    cleaned = " ".join((reason or "").split())
    slot.reason = cleaned or "you pressed Stop"
    slot.user_stop = slot.reason == "you pressed Stop"
    slot.cancel.set()
    stop_process(slot.proc)
    from easyagent.delegate import cancel_children

    cancel_children(slot)
    client = slot.client
    if client is None:
        return
    try:
        asyncio.get_running_loop().create_task(_close_client(client))
    except RuntimeError:
        return


def cancel_reason() -> str:
    slot = _slot.get()
    if slot is None:
        return ""
    return slot.reason or ""


def current_run_id() -> str:
    slot = _slot.get()
    if slot is None:
        return ""
    return slot.run_id or ""


def current_slot() -> Slot | None:
    return _slot.get()


def push_payload(payload: dict) -> None:
    """Publish one SSE event on the open turn. A missing listener is a no-op."""
    slot = _slot.get()
    if slot is None:
        return
    notify = getattr(slot, "notify", None)
    if notify is None:
        return
    body = dict(payload)
    if slot.run_id:
        body.setdefault("run_id", slot.run_id)
    if slot.bot_id:
        body.setdefault("bot_id", slot.bot_id)
    if slot.chat_id:
        body.setdefault("chat_id", slot.chat_id)
    import json

    chunk = "data: " + json.dumps(body, ensure_ascii=False) + "\n\n"
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def send() -> None:
        try:
            await notify(chunk)
        except Exception:
            return

    loop.create_task(send())


def tool_cwd() -> str:
    """The working folder for this run. Another run does not share the string."""
    slot = _slot.get()
    if slot is not None and slot.cwd:
        return slot.cwd
    return str(Path.home())


def tool_env() -> dict[str, str]:
    """A private environment for this run. Callers must not replace os.environ."""
    slot = _slot.get()
    if slot is not None and slot.env is not None:
        return slot.env
    return os.environ.copy()


def bind(store, chat_id: str, bot_id: str = "", *, resume: bool = False) -> Slot:
    """Start a turn on this chat. The previous turn's cancel flag stays set on that task.

    A resume drops a leftover Stop. That flag belongs to the turn that already ended.
    """
    slot = slot_for(store, chat_id)
    # A leftover Stop belongs to the turn that already ended. It must not cancel this one.
    slot.user_stop = False
    slot.redirect = False
    slot.pause_for_user = False
    slot.side_answer = False
    slot.sealing = False
    slot.late_arrival = False
    slot.in_model_read = False
    slot.sealed = False
    slot.cut_unfinished = False
    slot.sealed_answer = False
    slot.cancel = asyncio.Event()
    slot.client = None
    slot.proc = None
    slot.child_tasks = []
    slot.child_procs = []
    slot.reason = ""
    slot.run_id = str(uuid.uuid4())
    slot.job_started = ""
    slot.follow_chain = 0
    slot.bot_id = bot_id or slot.bot_id
    slot.chat_id = chat_id
    slot.store = store
    slot.cwd = str(Path.home())
    if bot_id:
        try:
            from easyagent.workspace import bot_workspace

            slot.cwd = str(bot_workspace(store, bot_id, create=True))
        except Exception:
            pass
    slot.env = os.environ.copy()
    try:
        slot.worker = asyncio.current_task()
    except RuntimeError:
        slot.worker = None
    _cancel.set(slot.cancel)
    _slot.set(slot)
    return slot


def track_worker(task: asyncio.Task) -> None:
    _WORKERS.add(task)
    task.add_done_callback(_WORKERS.discard)


def current_worker(store, chat_id: str) -> asyncio.Task | None:
    return slot_for(store, chat_id).worker


def _forget_finished(slot: Slot) -> asyncio.Task | None:
    """A finished owner is not still working. A claim with no task is reaped once it is stale."""
    worker = slot.worker
    if worker is not None and worker.done():
        slot.worker = None
        slot.busy = False
        slot.user_stop = False
        return None
    if slot.busy and worker is None and not _slot_in_flight(slot):
        claimed = float(getattr(slot, "claimed_at", 0.0) or 0.0)
        if claimed and time.monotonic() - claimed > 15:
            slot.busy = False
            slot.user_stop = False
            slot.claimed_at = 0.0
            return None
    return worker


def _slot_in_flight(slot: Slot) -> bool:
    if slot.client is not None:
        return True
    proc = slot.proc
    return proc is not None and proc.poll() is None


def request_in_flight(store, chat_id: str) -> bool:
    """A model socket or a command is open. A chat marked running is not that."""
    slot = slot_for(store, chat_id)
    if slot.client is not None:
        return True
    proc = slot.proc
    return proc is not None and proc.poll() is None


def is_busy(store, chat_id: str) -> bool:
    """True while this chat's turn is still the one that was started."""
    slot = slot_for(store, chat_id)
    with _GUARD:
        worker = _forget_finished(slot)
        return bool(slot.busy or worker is not None)


def try_begin(store, chat_id: str) -> bool:
    """Claim this chat for a new turn. A turn that is already running stays as it is."""
    slot = slot_for(store, chat_id)
    with _GUARD:
        worker = _forget_finished(slot)
        if slot.busy or worker is not None:
            return False
        slot.busy = True
        slot.claimed_at = time.monotonic()
        return True


def clear_busy(store, chat_id: str) -> None:
    """Drop busy when this task owns the turn, or when the owner has already finished."""
    slot = slot_for(store, chat_id)
    with _GUARD:
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        owner = slot.worker
        if owner is not None and not owner.done() and current is not None and owner is not current:
            return
        slot.busy = False
        if owner is None or owner.done() or owner is current:
            slot.worker = None


def redirect_model(store, chat_id: str) -> str:
    """Abort only the in-flight model request. A running command is left alone.

    Returns ``redirect`` when the model call was told to stop, ``hold`` when a
    command is still running, and ``later`` when nothing is in flight to abort.
    This is not Stop. ``user_stop`` stays clear.
    """
    slot = slot_for(store, chat_id)
    proc = slot.proc
    if proc is not None and proc.poll() is None:
        return "hold"
    if slot.client is None:
        return "later"
    slot.redirect = True
    slot.user_stop = False
    slot.reason = "a newer message arrived"
    slot.cancel.set()
    client = slot.client
    try:
        asyncio.get_running_loop().create_task(_close_client(client))
    except RuntimeError:
        return "redirect"
    return "redirect"


def take_redirect() -> bool:
    """The in-flight model call stopped because a newer message arrived. Keep the turn."""
    slot = _slot.get()
    if slot is None or not slot.redirect or slot.user_stop:
        return False
    slot.redirect = False
    slot.reason = ""
    slot.cancel = asyncio.Event()
    _cancel.set(slot.cancel)
    return True


def _tool_in_progress(slot: Slot) -> bool:
    """A command or a tool batch is still finishing. The model socket is not that."""
    proc = slot.proc
    if proc is not None and proc.poll() is None:
        return True
    return bool(getattr(slot, "_executing_tools", False))


PAUSE_TOOL_TEXT = "Paused, reading your message — finishing current step"


def _command_running(slot: Slot) -> bool:
    proc = slot.proc
    return proc is not None and proc.poll() is None


def request_user_pause(store, chat_id: str) -> None:
    """A message arrived. Finish the current token or tool step, then stop that reply.

    The text already streamed stays. A read blocked on the next chunk is closed so
    the pause can happen, after a short wait so a reply that has already ended can
    finish. A command is left alone, and the screen says so at once.
    Stop is a different flag. A reply that is already being saved is not paused:
    the message stays queued and is the next turn.
    """
    slot = slot_for(store, chat_id)
    if slot.user_stop:
        return
    if slot.sealing and not _command_running(slot) and not slot._executing_tools:
        slot.late_arrival = True
        return
    slot.pause_for_user = True
    if _tool_in_progress(slot):
        _announce_tool_pause(slot)
        return
    # No model socket yet: the step finishes, and the next model call sees the pause.
    client = slot.client
    if client is None:
        return
    slot.cancel.set()
    # Between tokens the read loop can still see a reply that already ended.
    # Closing here would drop that last line. A read blocked on the socket
    # cannot see the flag, so that one is closed.
    if not slot.in_model_read:
        return
    try:
        asyncio.get_running_loop().create_task(_close_client_after(client, _PAUSE_CLOSE_DELAY))
    except RuntimeError:
        return


def _announce_tool_pause(slot: Slot) -> None:
    """The tool keeps running. The viewer still hears the pause on this turn."""
    notify = getattr(slot, "notify", None)
    if notify is None:
        return
    import json

    chunk = "data: " + json.dumps({"type": "status", "text": PAUSE_TOOL_TEXT}, ensure_ascii=False) + "\n\n"
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def send() -> None:
        try:
            await notify(chunk)
        except Exception:
            return

    loop.create_task(send())


def mark_pause_cut() -> None:
    """This pause stopped a reply that still had text or a tool left to run."""
    slot = _slot.get()
    if slot is None:
        return
    slot.cut_unfinished = True
    slot.sealed_answer = False


def clear_pause_cut() -> None:
    """The step that just finished is the answer. It is not waiting to be resumed."""
    slot = _slot.get()
    if slot is None:
        return
    slot.cut_unfinished = False


def pause_requested() -> bool:
    """True when a user message is waiting and this turn should stop after the safe point."""
    slot = _slot.get()
    return bool(slot is not None and slot.pause_for_user and not slot.user_stop)


def take_user_pause() -> bool:
    """Accept the pause and clear it so the next reply can start."""
    slot = _slot.get()
    if slot is None or not slot.pause_for_user or slot.user_stop:
        return False
    slot.pause_for_user = False
    slot.reason = ""
    slot.cancel = asyncio.Event()
    _cancel.set(slot.cancel)
    return True


def stop_all(reason: str) -> list[asyncio.Task]:
    """Mark every chat stopped. Used when this process is going away."""
    cleaned = " ".join((reason or "").split()) or "the server restarted"
    with _GUARD:
        slots = list(_SLOTS.values())
        tasks = [task for task in _WORKERS if not task.done()]
    for slot in slots:
        slot.reason = cleaned
        slot.user_stop = False
        slot.cancel.set()
        stop_process(slot.proc)
    return tasks


def attach_client(client: httpx.AsyncClient) -> None:
    slot = _slot.get()
    if slot is not None:
        slot.client = client


def detach_client(client: httpx.AsyncClient) -> None:
    slot = _slot.get()
    if slot is not None and slot.client is client:
        slot.client = None


def attach_proc(proc: subprocess.Popen) -> None:
    slot = _slot.get()
    if slot is not None:
        slot.proc = proc


def detach_proc(proc: subprocess.Popen) -> None:
    slot = _slot.get()
    if slot is not None and slot.proc is proc:
        slot.proc = None
