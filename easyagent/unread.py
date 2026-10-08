"""Unread replies. Cursors live in unread.json. Transcripts are not opened for writing."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from easyagent.store import Store, atomic_write_json

_LOCK = threading.Lock()


def _path(root: Path) -> Path:
    return Path(root) / "unread.json"


def load_seen(root: Path) -> dict[str, int]:
    """Message-count cursors. Missing file means nothing has been opened."""
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("seen") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    seen: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            seen[key] = value
    return seen


def _save_seen(root: Path, seen: dict[str, int]) -> None:
    atomic_write_json(_path(root), {"seen": seen})


def _chat_key(bot_id: str, chat_id: str) -> str:
    return f"chat:{bot_id}:{chat_id}"


def _room_key(room_id: str) -> str:
    return f"room:{room_id}"


def chat_reply(message: object) -> bool:
    return isinstance(message, dict) and message.get("role") == "assistant"


def room_reply(message: object) -> bool:
    """A room line from a bot. The user's own line does not count."""
    if not isinstance(message, dict):
        return False
    if message.get("speaker") == "user":
        return False
    if message.get("role") == "user" and not message.get("speaker"):
        return False
    return True


def count_after(messages: list, cursor: int, qualify) -> int:
    start = cursor if cursor > 0 else 0
    total = 0
    for index, message in enumerate(messages or []):
        if index < start:
            continue
        if qualify(message):
            total += 1
    return total


def _cursor(through: int | None, length: int, current: int) -> int:
    """Opening a chat only moves the cursor forward, and never past the transcript."""
    if through is None:
        target = length
    elif through < 0:
        target = 0
    elif through > length:
        target = length
    else:
        target = through
    if target < current:
        return current
    return target


def unread_snapshot(store: Store) -> dict:
    """Counts only. Does not create unread.json and does not write a chat or room."""
    seen = load_seen(store.root)
    chats = []
    rooms = []
    busy: list[str] = []
    total = 0
    for bot in store.list_bots():
        for summary in store.list_chats(bot["id"]):
            chat = store.get_chat(bot["id"], summary["id"])
            run = chat.get("run") if isinstance(chat.get("run"), dict) else {}
            if run.get("status") == "running" and chat.get("bot_id") not in busy:
                busy.append(chat["bot_id"])
            key = _chat_key(chat["bot_id"], chat["id"])
            unread = count_after(chat.get("messages") or [], seen.get(key, 0), chat_reply)
            if unread:
                chats.append({"bot_id": chat["bot_id"], "chat_id": chat["id"], "unread": unread})
                total += unread
    for summary in store.list_rooms():
        room = store.get_room(summary["id"])
        key = _room_key(room["id"])
        unread = count_after(room.get("messages") or [], seen.get(key, 0), room_reply)
        if unread:
            rooms.append({"room_id": room["id"], "unread": unread})
            total += unread
    chats.sort(key=lambda item: (item["bot_id"], item["chat_id"]))
    rooms.sort(key=lambda item: item["room_id"])
    busy.sort()
    return {"total": total, "chats": chats, "rooms": rooms, "busy": busy}


def mark_chat_read(store: Store, bot_id: str, chat_id: str, through: int | None = None) -> dict:
    chat = store.get_chat(bot_id, chat_id)
    key = _chat_key(chat["bot_id"], chat["id"])
    length = len(chat.get("messages") or [])
    with _LOCK:
        seen = load_seen(store.root)
        seen[key] = _cursor(through, length, seen.get(key, 0))
        _save_seen(store.root, seen)
    return unread_snapshot(store)


def mark_room_read(store: Store, room_id: str, through: int | None = None) -> dict:
    room = store.get_room(room_id)
    key = _room_key(room["id"])
    length = len(room.get("messages") or [])
    with _LOCK:
        seen = load_seen(store.root)
        seen[key] = _cursor(through, length, seen.get(key, 0))
        _save_seen(store.root, seen)
    return unread_snapshot(store)
