"""Durable queue for routine results.

The result is stored before it is sent. A claim marks the row delivering.
A finished send marks it delivered, so a retry does not send it again.
If the process dies after the claim, the next start puts the row back and
sends it once. The chat message carries the delivery id, so that retry does
not add a second copy.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

_LOCK = threading.RLock()
_MAX_ATTEMPTS = 3


def queue_path(store) -> Path:
    return Path(store.root) / "deliveries.sqlite"


def _connect(store) -> sqlite3.Connection:
    path = queue_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deliveries (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            owner_pid INTEGER,
            token TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    return conn


def enqueue(store, payload: dict) -> str:
    """Persist one result before anyone tries to send it."""
    from easyagent.store import now_iso

    delivery_id = str(uuid.uuid4())
    body = dict(payload)
    body["delivery_id"] = delivery_id
    with _LOCK:
        conn = _connect(store)
        try:
            conn.execute(
                "INSERT INTO deliveries (id, payload, status, owner_pid, token, attempts, created_at) VALUES (?, ?, 'pending', NULL, NULL, 0, ?)",
                (delivery_id, json.dumps(body), now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
    return delivery_id


def _requeue_orphans(conn: sqlite3.Connection) -> None:
    """A delivering row whose owner is gone goes back to pending, once per death."""
    rows = conn.execute("SELECT id, owner_pid, attempts FROM deliveries WHERE status = 'delivering'").fetchall()
    for row in rows:
        pid = row["owner_pid"]
        if pid and _pid_alive(int(pid)):
            continue
        attempts = int(row["attempts"] or 0)
        if attempts >= _MAX_ATTEMPTS:
            conn.execute("UPDATE deliveries SET status = 'failed', token = NULL, owner_pid = NULL WHERE id = ?", (row["id"],))
            continue
        conn.execute(
            "UPDATE deliveries SET status = 'pending', token = NULL, owner_pid = NULL WHERE id = ?",
            (row["id"],),
        )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def abandon(store, delivery_id: str) -> None:
    """The owner died after claiming. Used when a send is killed mid-flight."""
    with _LOCK:
        conn = _connect(store)
        try:
            conn.execute(
                "UPDATE deliveries SET owner_pid = -1 WHERE id = ? AND status = 'delivering'",
                (delivery_id,),
            )
            conn.commit()
        finally:
            conn.close()


def claim(store) -> dict | None:
    """Claim one pending row. Orphans are returned to pending first."""
    token = uuid.uuid4().hex
    with _LOCK:
        conn = _connect(store)
        try:
            _requeue_orphans(conn)
            row = conn.execute(
                "SELECT id, payload, attempts FROM deliveries WHERE status = 'pending' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            attempts = int(row["attempts"] or 0) + 1
            conn.execute(
                "UPDATE deliveries SET status = 'delivering', owner_pid = ?, token = ?, attempts = ? WHERE id = ? AND status = 'pending'",
                (os.getpid(), token, attempts, row["id"]),
            )
            conn.commit()
            payload = json.loads(row["payload"])
            payload["delivery_id"] = row["id"]
            payload["token"] = token
            payload["attempts"] = attempts
            return payload
        finally:
            conn.close()


def mark_delivered(store, delivery_id: str, token: str) -> bool:
    """Only the claimer can finish the row. A second call is a no-op."""
    with _LOCK:
        conn = _connect(store)
        try:
            cur = conn.execute(
                "UPDATE deliveries SET status = 'delivered', token = NULL, owner_pid = NULL WHERE id = ? AND token = ? AND status = 'delivering'",
                (delivery_id, token),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()


def status_of(store, delivery_id: str) -> str:
    with _LOCK:
        conn = _connect(store)
        try:
            row = conn.execute("SELECT status FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            return "" if row is None else str(row["status"])
        finally:
            conn.close()


def apply_one(store, payload: dict) -> None:
    """Put the result in the chat once, then post outside. A repeat sees the same id."""
    bot_id = str(payload.get("bot_id") or "")
    text = str(payload.get("text") or "")
    delivery_id = str(payload.get("delivery_id") or "")
    if bot_id and text:
        store.append_routine_message(
            bot_id,
            text,
            routine_id=str(payload.get("routine_id") or ""),
            routine_name=str(payload.get("routine_name") or ""),
            delivery_id=delivery_id,
            missed=bool(payload.get("missed")),
        )


async def flush(store) -> int:
    """Send every pending row. Each row is claimed before the send."""
    sent = 0
    while True:
        payload = claim(store)
        if payload is None:
            return sent
        apply_one(store, payload)
        await _post_outside_once(store, payload)
        if mark_delivered(store, payload["delivery_id"], payload["token"]):
            sent += 1


async def _post_outside_once(store, payload: dict) -> None:
    kind = str(payload.get("deliver") or "").strip().lower()
    chat_id = str(payload.get("deliver_chat") or "").strip()
    if kind not in {"telegram", "discord"} or not chat_id:
        return
    from easyagent.schedule import _post_outside

    bot = {"id": str(payload.get("bot_id") or "")}
    schedule = {
        "id": payload.get("routine_id") or "",
        "name": payload.get("routine_name") or "",
        "deliver": kind,
        "deliver_chat": chat_id,
    }
    await _post_outside(store, bot, schedule, str(payload.get("text") or ""), str(payload.get("routine_name") or "Routine"))
