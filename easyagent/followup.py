"""Card-free follow-ups: a bot schedules its own next step into the same chat.

A follow-up is one plain line the bot asks the chat to wake it with later. It
is not a routine: no confirm card, no foreign run context. It fires back into
the same chat transcript as a user-shaped turn, the same way a background
worker result lands (plan.enqueue plus the kick). The scope is what removes
the card: same chat, at most a few pending, one to two minutes to twenty-four
hours out, and a fired follow-up may schedule only one more.

Records live next to the chat, so a restart keeps them. One whose time passed
while the app was closed fires on the next sweep, inside the catch-up window.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MIN_DELAY_MINUTES = 1
MAX_HORIZON_MINUTES = 24 * 60
PENDING_CAP = 3
# A follow-up this overdue when the app comes back still fires. Older ones are
# too stale to wake the chat with; they are dropped with a plain line instead.
CATCHUP_HOURS = 24
FIRE_PREFIX = "Follow-up due: "
EXPIRE_PREFIX = "Follow-up expired before it fired: "
_MAX_TEXT = 200


class FollowupError(ValueError):
    pass


def followups_path(store, bot_id: str, chat_id: str) -> Path:
    """The per-chat file recording pending follow-ups across restarts."""
    chat = store._chat_path(bot_id, chat_id)
    return chat.with_name(chat.stem + ".followups.json")


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    rows = []
    for row in data:
        if isinstance(row, dict) and row.get("id") and row.get("due_at"):
            rows.append(row)
    return rows


def _write(path: Path, rows: list[dict]) -> None:
    try:
        if not rows:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _short_id(rows: list[dict]) -> str:
    taken = {str(row.get("id") or "") for row in rows}
    while True:
        candidate = uuid.uuid4().hex[:6]
        if candidate not in taken:
            return candidate


def _fire_key(store, bot_id: str, chat_id: str) -> str:
    """The id of the last user message when it is a fired follow-up line.

    A turn woken by a fired follow-up may schedule one more follow-up, not fill
    the cap. The fired line is the last user message for the whole of that turn.
    """
    try:
        chat = store.get_chat(bot_id, chat_id)
    except Exception:
        return ""
    for message in reversed(list(chat.get("messages") or [])):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        if content.startswith(FIRE_PREFIX):
            return str(message.get("id") or "") or content
        return ""
    return ""


def pending(store, bot_id: str, chat_id: str) -> list[dict]:
    """Pending follow-ups for this chat, soonest first."""
    rows = _read(followups_path(store, bot_id, chat_id))
    rows.sort(key=lambda row: float(row.get("due_at") or 0))
    return rows


def human_phrase(minutes: float) -> str:
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    hours, rest = divmod(minutes, 60)
    if not rest:
        return f"{hours} hour" + ("s" if hours != 1 else "")
    return f"{hours} hour{'s' if hours != 1 else ''} {rest} minutes"


def schedule_followup(
    store, bot_id: str, chat_id: str, text: str, minutes: float
) -> dict:
    """Add one follow-up to this chat's own pending list. No card, no confirm.

    Raises FollowupError with the plain reason when the scope rules refuse it.
    """
    text = " ".join(str(text or "").split())
    if not text:
        raise FollowupError("Write what the follow-up should do.")
    if len(text) > _MAX_TEXT:
        raise FollowupError(f"A follow-up line is {_MAX_TEXT} characters at most.")
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        raise FollowupError("Say how many minutes to wait.") from None
    if minutes < MIN_DELAY_MINUTES:
        raise FollowupError("A follow-up waits at least 1 minute.")
    if minutes > MAX_HORIZON_MINUTES:
        raise FollowupError("A follow-up waits at most 24 hours.")
    path = followups_path(store, bot_id, chat_id)
    rows = _read(path)
    if len(rows) >= PENDING_CAP:
        raise FollowupError(
            f"Only {PENDING_CAP} follow-ups can wait in this chat. Cancel one first."
        )
    fire_key = _fire_key(store, bot_id, chat_id)
    if fire_key and any(str(row.get("fire_key") or "") == fire_key for row in rows):
        raise FollowupError("A fired follow-up may schedule only one more. It was not scheduled.")
    now = time.time()
    record = {
        "id": _short_id(rows),
        "text": text,
        "minutes": int(minutes),
        "due_at": now + minutes * 60,
        "created_at": now,
    }
    if fire_key:
        record["fire_key"] = fire_key
    rows.append(record)
    _write(path, rows)
    return record


def cancel_followup(store, bot_id: str, chat_id: str, short_id: str) -> str:
    """Drop one pending follow-up by its short id (or a unique prefix of it)."""
    wanted = str(short_id or "").strip().lower()
    if not wanted:
        raise FollowupError("Name the follow-up id to cancel.")
    path = followups_path(store, bot_id, chat_id)
    rows = _read(path)
    matches = [row for row in rows if str(row.get("id") or "").lower().startswith(wanted)]
    if not matches:
        raise FollowupError("No follow-up with that id is waiting in this chat.")
    if len(matches) > 1:
        listing = ", ".join(str(row.get("id")) for row in matches)
        raise FollowupError(f"More than one follow-up starts with {wanted}: {listing}.")
    gone = matches[0]
    _write(path, [row for row in rows if row is not gone])
    return f"Cancelled the follow-up: {gone.get('text') or 'follow-up'}."


def list_text(store, bot_id: str, chat_id: str) -> str:
    rows = pending(store, bot_id, chat_id)
    if not rows:
        return "No follow-ups are waiting in this chat."
    now = time.time()
    lines = ["Follow-ups waiting in this chat"]
    for row in rows:
        left = max(0.0, float(row.get("due_at") or 0) - now) / 60.0
        lines.append(
            f"- [{row.get('id')}] {row.get('text') or 'follow-up'} in {human_phrase(left)}"
        )
    return "\n".join(lines)


def pending_view(store, bot_id: str, chat_id: str) -> list[dict]:
    """What the chat payload shows for pending follow-ups."""
    out = []
    for row in pending(store, bot_id, chat_id):
        due = datetime.fromtimestamp(float(row.get("due_at") or 0), tz=timezone.utc)
        out.append(
            {
                "id": row.get("id"),
                "text": row.get("text") or "",
                "due_at": due.isoformat(),
            }
        )
    return out


def _deliver_line(store, bot_id: str, chat_id: str, line: str) -> None:
    """Land one plain line in the chat and wake the bot. Same path a background
    worker result rides: plan.enqueue plus the kick."""
    from easyagent.plan import enqueue

    try:
        enqueue(store, bot_id, chat_id, line)
    except Exception:
        return
    try:
        import asyncio

        from easyagent import app as app_mod

        asyncio.get_running_loop().create_task(app_mod.kick(store))
    except Exception:
        pass


def sweep_followups(store, now: float | None = None) -> int:
    """Fire every follow-up whose time has come. Returns how many fired.

    Called from the scheduler tick and once at startup, so one whose time
    passed while the app was closed fires on start inside the catch-up window.
    """
    moment = time.time() if now is None else float(now)
    fired = 0
    try:
        pairs = store.iter_chat_ids()
    except Exception:
        return 0
    for bot_id, chat_id in pairs:
        path = followups_path(store, bot_id, chat_id)
        rows = _read(path)
        if not rows:
            continue
        keep: list[dict] = []
        due: list[tuple[float, dict]] = []
        for row in rows:
            try:
                due_at = float(row.get("due_at") or 0)
            except (TypeError, ValueError):
                continue
            if due_at <= moment:
                due.append((due_at, row))
            else:
                keep.append(row)
        if not due:
            continue
        _write(path, keep)
        due.sort(key=lambda item: item[0])
        for due_at, row in due:
            text = str(row.get("text") or "").strip() or "follow-up"
            if moment - due_at > CATCHUP_HOURS * 3600:
                line = EXPIRE_PREFIX + text
            else:
                line = FIRE_PREFIX + text
            _deliver_line(store, bot_id, chat_id, line)
            fired += 1
    return fired
