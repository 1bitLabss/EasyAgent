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


_GUARD = threading.Lock()
_SLOTS: dict[str, Slot] = {}
_WORKERS: set[asyncio.Task] = set()
_cancel: ContextVar[asyncio.Event | None] = ContextVar("easyagent_turn_cancel", default=None)
_slot: ContextVar[Slot | None] = ContextVar("easyagent_turn_slot", default=None)


def _key(store, chat_id: str) -> str:
    return f"{getattr(store, 'root', '')}:{chat_id}"


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


def bind(store, chat_id: str, bot_id: str = "") -> Slot:
    """Start a turn on this chat. The previous turn's cancel flag stays set on that task."""
    slot = slot_for(store, chat_id)
    stopped = slot.user_stop
    slot.user_stop = False
    slot.cancel = asyncio.Event()
    slot.client = None
    slot.proc = None
    slot.reason = ""
    slot.run_id = str(uuid.uuid4())
    slot.bot_id = bot_id or slot.bot_id
    slot.chat_id = chat_id
    slot.store = store
    slot.cwd = str(Path.home())
    slot.env = os.environ.copy()
    slot.worker = asyncio.current_task()
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
