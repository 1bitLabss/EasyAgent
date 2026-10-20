"""Busy input, ported from Hermes Agent (Nous Research).

Hermes Agent is released under the MIT License. Copyright (c) Nous Research.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Function names match the Hermes sources so a later diff stays readable:

* ``agent/interrupt_control.py`` — ``steer``, ``redirect``, ``clear_interrupt``,
  ``_drain_pending_steer``, ``_drain_pending_redirect``
* ``gateway/run_busy.py`` — ``_enqueue_fifo``, ``_promote_queued_event``,
  ``_queue_or_replace_pending_event``, ``_rescue_orphaned_overflow``
* ``gateway/run_inbound.py`` — ``_hm_busy_steer``, ``_hm_busy_interrupt``,
  ``_hm_handle_running_session_message``
* ``agent/turn_iteration_prep.py`` — ``_inject_steer_after_newest_tool_result``
* ``agent/turn_finalizer.py`` — leftover steer becomes the next turn
* ``gateway/run_turn.py`` — ``_run_agent_drain_pending``
* ``gateway/platforms/base.py`` — ``_finish_session_task``, ``_spawn_drain_task``
* ``gateway/shutdown_flush.py`` — ``flush_pending_to_file``,
  ``flush_overflow_to_file``, ``recover_pending_spool``

Storage is EasyAgent's. The chat queue file is the ordered FIFO (slot head
plus overflow tail). The turn slot holds only the in-flight steer and
redirect. The spool directory is ``<data>/pending_messages/``. A message is
written to the transcript before any of this runs, so a crash cannot lose it.
Stop does not discard that queue: the next turn still runs it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from easyagent.store import Store, atomic_write_json

logger = logging.getLogger("easyagent.hermes_busy")

BUSY_QUEUE_MAX_PENDING = 32


def _ic_lock(agent, attr: str):
    """``with`` the lock stored at ``attr`` when present."""
    import contextlib

    lock = getattr(agent, attr, None)
    return contextlib.nullcontext() if lock is None else lock


def _ic_slot(agent, lock_attr: str, slot: str):
    if getattr(agent, lock_attr, None) is None:
        return getattr(agent, slot, None)
    return getattr(agent, slot)


class SlotAgent:
    """``steer`` / ``redirect`` / ``clear_interrupt`` on one chat's turn slot.

    The bodies follow ``InterruptControlMixin`` without Codex, subagents, or
    the compression fence. A running command is a tool: redirect degrades to
    steer and does not kill it.
    """

    def __init__(self, slot) -> None:
        object.__setattr__(self, "_slot", slot)

    def __getattr__(self, name: str):
        return getattr(self._slot, name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._slot, name, value)

    def steer(self, text: str) -> bool:
        """Queue user text for delivery as its own user row after the current tool batch.

        Multiple calls concatenate with newlines. Empty text returns False.
        """
        if not text or not text.strip():
            return False
        cleaned = text.strip()
        with _ic_lock(self, "_pending_steer_lock"):
            existing = _ic_slot(self, "_pending_steer_lock", "_pending_steer")
            self._pending_steer = (existing + "\n" + cleaned) if existing else cleaned
        self._turn_user_intervened = True
        return True

    def redirect(self, text: str) -> bool:
        """Redirect the active turn. Only the in-flight model request is aborted.

        During tool execution (or a live command) this degrades to ``steer``
        and the command keeps running. False when no model request is live.
        """
        if not text or not text.strip():
            return False
        cleaned = text.strip()
        proc = getattr(self._slot, "proc", None)
        tool_running = bool(getattr(self, "_executing_tools", False)) or (
            proc is not None and proc.poll() is None
        )
        if tool_running:
            return self.steer(cleaned)

        active = getattr(self, "_model_request_active", None)
        client = getattr(self._slot, "client", None)
        model_live = (active is not None and active.is_set()) or client is not None
        with _ic_lock(self, "_pending_redirect_lock"):
            if not model_live:
                return False
            existing = _ic_slot(self, "_pending_redirect_lock", "_pending_redirect")
            if self._interrupt_requested and not existing:
                return False
            self._pending_redirect = (
                f"{existing}\n\n[Additional user correction]\n{cleaned}" if existing else cleaned
            )
            self._interrupt_requested = True
            self._interrupt_message = None
            self._turn_user_intervened = True
        self._slot.redirect = True
        self._slot.user_stop = False
        self._slot.reason = "a newer message arrived"
        self._slot.cancel.set()
        _abort_model_client(self._slot)
        return True

    def clear_interrupt(self, *, preserve_redirect: bool = False, hard_cancel: bool = False) -> bool:
        """Clear the interrupt flag. A soft clear keeps an accepted steer.

        ``hard_cancel`` drops an unconsumed steer. EasyAgent's Stop does not
        use that path: a queued message still runs as the next turn.
        """
        with _ic_lock(self, "_pending_redirect_lock"):
            if preserve_redirect and not _ic_slot(self, "_pending_redirect_lock", "_pending_redirect"):
                return False
            self._interrupt_requested = False
            self._interrupt_message = None
            if not preserve_redirect:
                self._pending_redirect = None
        if hard_cancel:
            with _ic_lock(self, "_pending_steer_lock"):
                self._pending_steer = None
        return True

    def _drain_pending_redirect(self) -> Optional[str]:
        with _ic_lock(self, "_pending_redirect_lock"):
            text = _ic_slot(self, "_pending_redirect_lock", "_pending_redirect")
            self._pending_redirect = None
        return text

    def _drain_pending_steer(self) -> Optional[str]:
        with _ic_lock(self, "_pending_steer_lock"):
            text = _ic_slot(self, "_pending_steer_lock", "_pending_steer")
            self._pending_steer = None
        return text


def _abort_model_client(slot) -> None:
    """Shut the in-flight model socket. A command process is not touched."""
    client = getattr(slot, "client", None)
    if client is None:
        return
    import asyncio

    from easyagent.turn import _close_client

    try:
        asyncio.get_running_loop().create_task(_close_client(client))
    except RuntimeError:
        return


def _session_key(bot_id: str, chat_id: str) -> str:
    return f"{bot_id}:{chat_id}"


def _split_session_key(session_key: str) -> tuple[str, str] | None:
    bot_id, sep, chat_id = str(session_key or "").partition(":")
    if not sep or not bot_id or not chat_id:
        return None
    return bot_id, chat_id


def _queue_items(store: Store, bot_id: str, chat_id: str) -> list[dict]:
    from easyagent.plan import queued_items

    return list(queued_items(store, bot_id, chat_id))


def _enqueue_fifo(store: Store, bot_id: str, chat_id: str, event: dict) -> None:
    """Append one follow-up. The first item is the pending head; the rest are overflow.

    The transcript row already exists when this is a busy send. This only
    extends the queue file, in arrival order.
    """
    from easyagent.plan import _read_queue, _write_queue

    content = str(event.get("content") or event.get("text") or "").strip()
    if not content and not event.get("id"):
        return
    payload = _read_queue(store, bot_id, chat_id)
    items = payload["items"]
    item_id = str(event.get("id") or "")
    if item_id and any(str(item.get("id") or "") == item_id for item in items):
        return
    if any((item.get("content") or "") == content and content for item in items):
        return
    items.append({
        "id": item_id,
        "content": content,
        "attachment": event.get("attachment"),
        "at": event.get("at") or "",
        "client_id": event.get("client_id") or "",
    })
    payload["items"] = items
    _write_queue(store, bot_id, chat_id, payload)


def _promote_queued_event(store: Store, bot_id: str, chat_id: str, pending_event: dict | None) -> dict | None:
    """After the head is consumed, the overflow head becomes the next pending event."""
    items = _queue_items(store, bot_id, chat_id)
    if not items:
        return pending_event
    if pending_event is None:
        return items[0]
    return pending_event


def _queue_depth(store: Store, bot_id: str, chat_id: str) -> int:
    return len(_queue_items(store, bot_id, chat_id))


def _queue_or_replace_pending_event(store: Store, bot_id: str, chat_id: str, event: dict) -> bool:
    """FIFO append. At the cap the message is already held in the transcript, not dropped quietly."""
    if _queue_depth(store, bot_id, chat_id) >= BUSY_QUEUE_MAX_PENDING:
        logger.warning(
            "Busy follow-up for %s is past the queue cap (%d). It stays held in the transcript.",
            _session_key(bot_id, chat_id),
            BUSY_QUEUE_MAX_PENDING,
        )
        return False
    _enqueue_fifo(store, bot_id, chat_id, event)
    return True


def _rescue_orphaned_overflow(store: Store, bot_id: str, chat_id: str) -> dict | None:
    """The oldest queued line on an idle chat whose drain never ran."""
    from easyagent import turn as turn_mod

    if turn_mod.is_busy(store, chat_id):
        return None
    items = _queue_items(store, bot_id, chat_id)
    if not items:
        return None
    logger.warning(
        "Rescued orphaned FIFO overflow for idle chat %s",
        _session_key(bot_id, chat_id),
    )
    return items[0]


def _hm_busy_steer(store: Store, bot_id: str, chat_id: str, text: str) -> bool:
    """Steer mode: buffer the text. The queue file already holds the durable copy."""
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    try:
        return SlotAgent(store and _slot(store, chat_id)).steer(cleaned)
    except Exception as exc:
        logger.warning("steer failed for %s: %s", _session_key(bot_id, chat_id), exc)
        return False


def _slot(store, chat_id: str):
    from easyagent import turn as turn_mod

    return turn_mod.slot_for(store, chat_id)


def _hm_busy_interrupt(store: Store, bot_id: str, chat_id: str, text: str) -> bool:
    """Pause at the next safe point. The same path runs every time.

    A live command finishes. A token chunk already in hand is kept. The old
    reply does not continue, and the text already streamed is not discarded.
    """
    del text
    from easyagent import turn as turn_mod

    turn_mod.request_user_pause(store, chat_id)
    return True


def _hm_handle_running_session_message(store: Store, bot_id: str, chat_id: str, text: str) -> None:
    """What to do with a message that arrived while this chat is busy.

    Queue and follow wait until the turn ends. Steer injects at the next
    model call. Interrupt pauses at the next safe point, every time.
    """
    from easyagent.plan import busy_mode
    from easyagent.store import StoreError

    try:
        bot = store.get_bot(bot_id)
    except StoreError:
        bot = None
    mode = busy_mode(bot)
    if mode in {"queue", "follow"}:
        return
    if mode == "steer":
        _hm_busy_steer(store, bot_id, chat_id, text)
        return
    _hm_busy_interrupt(store, bot_id, chat_id, text)


def _inject_steer_after_newest_tool_result(working: list | None, steer_text: str) -> bool:
    """Append the steer as its own user row after the newest tool message.

    With no tool row, the caller puts the text back so the end-of-turn drain
    delivers it. Returns True when a row was inserted.
    """
    if working is None or not (steer_text or "").strip():
        return False
    cleaned = steer_text.strip()
    for index in range(len(working) - 1, -1, -1):
        message = working[index]
        if isinstance(message, dict) and message.get("role") == "tool":
            working.insert(index + 1, {"role": "user", "content": cleaned})
            return True
    return False


def prepare_iteration(store: Store, bot_id: str, chat_id: str, working: list | None) -> int:
    """Before the next model call: apply a redirect, then a steer, then the queue.

    This is the pre-API drain in ``turn_iteration_prep`` plus the post-tool
    drain. Both land on the same line because the tool batch has already
    finished by the time the loop asks for another completion.
    """
    from easyagent.plan import steer_into

    agent = SlotAgent(_slot(store, chat_id))
    redirect_text = agent._drain_pending_redirect()
    if redirect_text and working is not None:
        last = ""
        for item in reversed(working):
            if isinstance(item, dict) and item.get("role") == "user":
                last = str(item.get("content") or "")
                break
        if redirect_text not in last:
            if not _inject_steer_after_newest_tool_result(working, redirect_text):
                working.append({"role": "user", "content": redirect_text})
    steer_text = agent._drain_pending_steer()
    if steer_text and not _inject_steer_after_newest_tool_result(working, steer_text):
        agent.steer(steer_text)
    taken = steer_into(store, bot_id, chat_id, working)
    if taken:
        agent._drain_pending_steer()
    return taken


def handoff_leftover_steer(store: Store, bot_id: str, chat_id: str) -> str:
    """A steer that arrived after the last model call becomes the next user turn.

    Mirrors the finalizer's ``result["pending_steer"]`` handoff. The text is
    not written twice when the queue already has it.
    """
    from easyagent.plan import enqueue

    agent = SlotAgent(_slot(store, chat_id))
    leftover = (agent._drain_pending_steer() or "").strip()
    redirect_text = (agent._drain_pending_redirect() or "").strip()
    text = leftover or redirect_text
    if not text:
        return ""
    queued = _queue_items(store, bot_id, chat_id)
    if any(text in str(item.get("content") or "") for item in queued):
        return text
    queued_ids = {str(item.get("id") or "") for item in queued}
    try:
        chat = store.get_chat(bot_id, chat_id)
    except Exception:
        chat = {}
    for message in reversed(chat.get("messages") or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if text not in str(message.get("content") or ""):
            continue
        # Seen or answered lines were already delivered. Only a queued row
        # missing from the file is put back.
        if message.get("chip") == "queued" and str(message.get("id") or "") not in queued_ids:
            _enqueue_fifo(store, bot_id, chat_id, {
                "id": message.get("id"),
                "content": message.get("content") or "",
                "attachment": message.get("attachment"),
                "at": message.get("created_at") or "",
                "client_id": message.get("client_id") or "",
            })
        return text
    enqueue(store, bot_id, chat_id, text)
    return text


def _run_agent_drain_pending(store: Store, bot_id: str, chat_id: str, result: dict | None) -> str:
    """Next turn text: interrupt message, else the queue head, else leftover steer.

    A leftover steer is delivered before a queued event that arrived behind it.
    """
    pending = ""
    pending_event = _promote_queued_event(store, bot_id, chat_id, None)
    if result and result.get("interrupted") and not pending_event and result.get("interrupt_message"):
        message = str(result.get("interrupt_message") or "")
        if message and not message.startswith("/"):
            pending = message
    elif pending_event:
        pending = str(pending_event.get("content") or "")
    leftover = str((result or {}).get("pending_steer") or "").strip()
    if leftover and not pending:
        pending = leftover
    elif leftover and pending_event:
        pending = leftover
    return pending


def _finish_session_task(store: Store, bot_id: str, chat_id: str, *, spawn) -> str:
    """Exit seam. A message that landed after the last drain starts a fresh task.

    The busy flag stays set while that task owns the chat. Nothing pending
    releases the guard only when this task still owns it. Returns ``drain`` or
    ``release``.
    """
    import asyncio

    from easyagent import turn as turn_mod

    late = _queue_items(store, bot_id, chat_id)
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None
    slot = _slot(store, chat_id)
    if late:
        existing = slot.worker
        if existing is not None and not existing.done() and current is not None and existing is not current:
            return "owned"
        spawn(store, bot_id, chat_id)
        return "drain"
    if current is None or slot.worker is None or slot.worker is current or slot.worker.done():
        turn_mod.clear_busy(store, chat_id)
    return "release"


def _spawn_drain_task(store: Store, bot_id: str, chat_id: str, spawn) -> None:
    """Hand the chat to a fresh task. Never recurse on the turn that just finished."""
    spawn(store, bot_id, chat_id)


# --- shutdown spool (gateway/shutdown_flush.py) --------------------------------


def _flush_dir(store: Store) -> Path:
    path = Path(store.root) / "pending_messages"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def _write_payload(flush_dir: Path, payload: dict) -> Path:
    final = flush_dir / f"pending-{uuid.uuid4().hex}.json"
    atomic_write_json(final, payload)
    return final


def _sort_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _spool_sort_key(payload: dict, name: str) -> tuple:
    return _sort_number(payload.get("ts")), _sort_number(payload.get("seq", -1)), name


def flush_pending_to_file(store: Store, *, reason: str = "shutdown") -> int:
    """Write each queue head to the spool. Returns how many payloads were written."""
    return _flush_queues(store, reason=reason, head_only=True)


def flush_overflow_to_file(store: Store, *, reason: str = "shutdown") -> int:
    """Write each FIFO tail (everything after the head) with ``seq`` for order."""
    return _flush_queues(store, reason=reason, head_only=False)


def _flush_queues(store: Store, *, reason: str, head_only: bool) -> int:
    from easyagent.store import StoreError

    try:
        bots = store.list_bots()
    except StoreError:
        return 0
    flush_dir = _flush_dir(store)
    ts = int(time.time())
    flushed = 0
    for bot in bots:
        bot_id = bot.get("id") or ""
        if not bot_id:
            continue
        try:
            chats = store.list_chats(bot_id)
        except StoreError:
            continue
        for summary in chats:
            chat_id = summary.get("id") or ""
            items = _queue_items(store, bot_id, chat_id)
            if not items:
                continue
            if head_only:
                pairs = [(-1, items[0])]
            else:
                pairs = [(seq, item) for seq, item in enumerate(items)]
                pairs = pairs[1:]
            for seq, item in pairs:
                if not isinstance(item, dict):
                    continue
                _write_payload(flush_dir, {
                    "session_key": _session_key(bot_id, chat_id),
                    "reason": reason,
                    "ts": ts,
                    "seq": seq,
                    "kind": "pending" if head_only else "overflow",
                    "data": {
                        "text": item.get("content") or "",
                        "id": item.get("id") or "",
                        "client_id": item.get("client_id") or "",
                        "attachment": item.get("attachment"),
                        "at": item.get("at") or "",
                    },
                })
                flushed += 1
    if flushed:
        logger.info("Flushed %d pending message(s) to %s (reason=%s)", flushed, flush_dir, reason)
    return flushed


def recover_pending_spool(store: Store) -> tuple[int, set[str]]:
    """Replay spool files into the queue, in ``seq`` order, and delete each on success.

    A line already stored is not written again. Returns ``(recovered, held_back)``.
    """
    flush_dir = _flush_dir(store)
    try:
        paths = list(flush_dir.glob("pending-*.json"))
    except OSError:
        return 0, set()
    entries = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("reason") == "shutdown-with-unpersisted-agent-history":
            continue
        entries.append((_spool_sort_key(payload, path.name), path, payload))
    entries.sort(key=lambda item: item[0])
    recovered = 0
    held: set[str] = set()
    for _key, path, payload in entries:
        session = _split_session_key(str(payload.get("session_key") or ""))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if session is None or not data:
            continue
        bot_id, chat_id = session
        if session[0] in held or _session_key(bot_id, chat_id) in held:
            continue
        try:
            if _replay_one(store, bot_id, chat_id, data):
                recovered += 1
            path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Replay of %s failed; keeping the spool file: %s", path, exc)
            held.add(_session_key(bot_id, chat_id))
    return recovered, held


def _replay_one(store: Store, bot_id: str, chat_id: str, data: dict) -> bool:
    """Put one spooled line back if it is not already queued. True when it was restored."""
    from easyagent.plan import _read_queue, _write_queue, enqueue
    from easyagent.store import StoreError

    text = str(data.get("text") or data.get("content") or "").strip()
    item_id = str(data.get("id") or "")
    try:
        chat = store.get_chat(bot_id, chat_id)
    except StoreError:
        return False
    messages = [item for item in chat.get("messages") or [] if isinstance(item, dict)]
    queued = _queue_items(store, bot_id, chat_id)
    if item_id and any(str(item.get("id") or "") == item_id for item in queued):
        return False
    if text and any(str(item.get("content") or "") == text for item in queued):
        return False
    if item_id and any(str(item.get("id") or "") == item_id for item in messages):
        payload = _read_queue(store, bot_id, chat_id)
        payload["items"].append({
            "id": item_id,
            "content": text or next(
                (str(item.get("content") or "") for item in messages if str(item.get("id") or "") == item_id),
                "",
            ),
            "attachment": data.get("attachment"),
            "at": data.get("at") or "",
            "client_id": data.get("client_id") or "",
        })
        _write_queue(store, bot_id, chat_id, payload)
        return True
    if not text:
        return False
    enqueue(store, bot_id, chat_id, text, data.get("attachment"), client_id=str(data.get("client_id") or ""))
    return True
