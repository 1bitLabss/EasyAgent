"""One running turn per chat. A different chat does not share its cancel flag."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import uuid
from contextvars import ContextVar
from pathlib import Path

import httpx


class TurnCancelled(Exception):
    """This chat's turn was stopped. Another chat's turn is left alone."""


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
    event = _cancel.get()
    return bool(event is not None and event.is_set())


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
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()


async def _close_client(client: httpx.AsyncClient) -> None:
    try:
        await client.aclose()
    except Exception:
        return


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
    stopped = False if resume else slot.user_stop
    slot.user_stop = False
    slot.redirect = False
    slot.cancel = asyncio.Event()
    slot.client = None
    slot.proc = None
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
    if stopped:
        slot.reason = "you pressed Stop"
        slot.cancel.set()
    _cancel.set(slot.cancel)
    _slot.set(slot)
    return slot


def track_worker(task: asyncio.Task) -> None:
    _WORKERS.add(task)
    task.add_done_callback(_WORKERS.discard)


def current_worker(store, chat_id: str) -> asyncio.Task | None:
    return slot_for(store, chat_id).worker


def _forget_finished(slot: Slot) -> asyncio.Task | None:
    """A finished owner is not still working. Busy with no task yet is the claim window."""
    worker = slot.worker
    if worker is not None and worker.done():
        slot.worker = None
        slot.busy = False
        return None
    return worker


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
