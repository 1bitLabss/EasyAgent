"""Telegram and Discord, for the person's own accounts.

A stranger is ignored and logged. A reply goes back to the person who wrote.
Messaging anyone else waits for a card. Tokens stay in the keychain.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

from easyagent.secrets import delete_secret, get_secret, put_secret
from easyagent.store import Store, StoreError, atomic_write_json, new_id, now_iso, read_json

_KINDS = ("telegram", "discord")
_IGNORED_TO = {"the person", "person", "the user", "user", ""}
_IGNORED_TEXT = {"the message", "message", ""}
_LOG_CAP = 200
_NOTIFIED: set[str] = set()


def bridge_path(store: Store) -> Path:
    return store.root / "bridges.json"


def log_path(store: Store) -> Path:
    return store.root / "bridge-log.json"


def list_records(store: Store) -> list[dict]:
    path = bridge_path(store)
    if not path.is_file():
        return []
    rows = read_json(path)
    return [row for row in rows if isinstance(row, dict) and row.get("id")] if isinstance(rows, list) else []


def _save(store: Store, rows: list[dict]) -> None:
    with store._lock:
        atomic_write_json(bridge_path(store), rows)


def secret_account(bridge_id: str) -> str:
    return f"bridge:{bridge_id}:token"


def public_record(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "kind": row.get("kind") or "telegram",
        "name": row.get("name") or "Bridge",
        "bot_id": row.get("bot_id") or "",
        "enabled": row.get("enabled") is not False,
        "allow_ids": [str(item) for item in (row.get("allow_ids") or [])],
        "has_token": bool(row.get("has_token")),
        "last_user_id": str(row.get("last_user_id") or ""),
    }


def token_values(store: Store) -> list[str]:
    found = []
    for row in list_records(store):
        value = get_secret(secret_account(row["id"]), store)
        if value and value not in found:
            found.append(value)
    return found


def scrub(text: str, store: Store) -> str:
    cleaned = text or ""
    for secret in token_values(store):
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def add_bridge(
    store: Store,
    *,
    kind: str,
    name: str,
    bot_id: str,
    allow_ids: list[str],
    token: str,
) -> dict:
    platform = kind if kind in _KINDS else ""
    if not platform:
        raise StoreError("A bridge is Telegram or Discord.", 400)
    label = " ".join((name or platform).split())[:80]
    ids = _ids(allow_ids)
    if not ids:
        raise StoreError("Add your own user id. An empty list would ignore you too.", 400)
    secret = (token or "").strip()
    if not secret:
        raise StoreError("The token goes in the keychain field.", 400)
    if secret in label or any(secret in item for item in ids):
        raise StoreError("Put the token in the keychain field, not in the name or the user id.", 400)
    row = {
        "id": new_id(),
        "kind": platform,
        "name": label,
        "bot_id": (bot_id or "").strip(),
        "enabled": True,
        "allow_ids": ids,
        "has_token": True,
        "update_offset": 0,
        "last_user_id": "",
        "last_chat_id": "",
    }
    try:
        put_secret(secret_account(row["id"]), secret, store)
    except RuntimeError as exc:
        raise StoreError(str(exc), 400) from exc
    rows = list_records(store)
    rows.append(row)
    _save(store, rows)
    return row


def update_bridge(store: Store, bridge_id: str, *, enabled: bool | None, allow_ids: list[str] | None, token: str | None) -> dict:
    rows = list_records(store)
    row = next((item for item in rows if item.get("id") == bridge_id), None)
    if row is None:
        raise StoreError("That bridge is not saved.", 404)
    if enabled is not None:
        row["enabled"] = bool(enabled)
    if allow_ids is not None:
        ids = _ids(allow_ids)
        if not ids:
            raise StoreError("Add your own user id. An empty list would ignore you too.", 400)
        row["allow_ids"] = ids
    if token is not None and token.strip():
        try:
            put_secret(secret_account(bridge_id), token.strip(), store)
        except RuntimeError as exc:
            raise StoreError(str(exc), 400) from exc
        row["has_token"] = True
    _save(store, rows)
    return row


def delete_bridge(store: Store, bridge_id: str) -> None:
    rows = list_records(store)
    if not any(item.get("id") == bridge_id for item in rows):
        raise StoreError("That bridge is not saved.", 404)
    delete_secret(secret_account(bridge_id), store)
    _save(store, [item for item in rows if item.get("id") != bridge_id])


def _ids(values: list[str] | None) -> list[str]:
    found = []
    for item in values or []:
        text = str(item).strip()
        if text and text not in found and len(text) <= 40:
            found.append(text)
    return found[:20]


def allow_set(row: dict) -> set[str]:
    return {str(item) for item in (row.get("allow_ids") or []) if str(item).strip()}


def admitted(row: dict, user_id: str) -> bool:
    return str(user_id or "").strip() in allow_set(row)


def find(store: Store, name_or_id: str, bot_id: str | None = None) -> dict | None:
    wanted = (name_or_id or "").strip().casefold()
    for row in list_records(store):
        if row.get("enabled") is False:
            continue
        if bot_id and row.get("bot_id") != bot_id:
            continue
        if row.get("id") == name_or_id or str(row.get("name") or "").casefold() == wanted:
            return row
    return None


def log_event(store: Store, kind: str, user_id: str, decision: str) -> None:
    path = log_path(store)
    rows = read_json(path) if path.is_file() else []
    if not isinstance(rows, list):
        rows = []
    rows.append({
        "at": now_iso(),
        "kind": kind,
        "user_id": str(user_id or ""),
        "decision": decision,
    })
    atomic_write_json(path, rows[-_LOG_CAP:])


def remember_sender(store: Store, bridge_id: str, user_id: str, chat_id: str) -> None:
    rows = list_records(store)
    for row in rows:
        if row.get("id") == bridge_id:
            row["last_user_id"] = str(user_id)
            row["last_chat_id"] = str(chat_id)
    _save(store, rows)


def take_inbound(store: Store, row: dict, user_id: str, text: str, chat_id: str) -> bool:
    """Append the person's message. A stranger is logged and does not touch the chat."""
    if not admitted(row, user_id):
        log_event(store, row.get("kind") or "", user_id, "ignored")
        return False
    body = " ".join(scrub(text or "", store).split())
    if not body:
        return False
    if len(body) > 8000:
        body = body[:7980].rstrip() + "…"
    from easyagent.safety import note_untrusted

    # Store the plain sentence. note_untrusted only records it for the injection
    # check. The model sees a fenced copy later; that fence is not written here.
    note_untrusted(body)
    bot_id = row.get("bot_id") or ""
    if not bot_id:
        log_event(store, row.get("kind") or "", user_id, "ignored")
        return False
    chat = store.ongoing_chat(bot_id)
    messages = list(chat.get("messages") or [])
    messages.append({
        "id": new_id(),
        "role": "user",
        "content": body,
        "created_at": now_iso(),
        "bridge": row.get("kind") or "",
    })
    chat["messages"] = messages
    chat["updated_at"] = now_iso()
    store.save_chat(chat)
    remember_sender(store, row["id"], user_id, chat_id or user_id)
    log_event(store, row.get("kind") or "", user_id, "accepted")
    return True


def append_reply(store: Store, bot_id: str, text: str) -> None:
    chat = store.ongoing_chat(bot_id)
    body = (text or "").strip() or "(empty reply)"
    if len(body) > 8000:
        body = body[:7980].rstrip() + "…"
    chat["messages"] = list(chat.get("messages") or []) + [{
        "id": new_id(),
        "role": "assistant",
        "content": body,
        "created_at": now_iso(),
    }]
    chat["updated_at"] = now_iso()
    run = chat.get("run") if isinstance(chat.get("run"), dict) else {}
    if run.get("status") == "running":
        chat["run"] = {**run, "status": "idle"}
    store.save_chat(chat)


def judge_bridge(request, store: Store, bot_id: str | None = None):
    from easyagent.safety import ASK, BLOCK, ALLOW, _fp, _verdict

    name = (getattr(request, "path", "") or "").strip()
    recipient = (getattr(request, "command", "") or "").strip()
    text = scrub(getattr(request, "body", "") or "", store)
    row = find(store, name, bot_id)
    if row is None:
        return _verdict(BLOCK, "bridge-missing", "That bridge is not saved or it is turned off.", name, _fp("bridge", name, recipient))
    detail = f"{row.get('kind')}\nto: {recipient}\n{text[:500]}".strip()
    if recipient.lower() in _IGNORED_TO or not recipient:
        return _verdict(BLOCK, "bridge-missing", "Name who should receive it.", detail, _fp("bridge", row.get("id") or "", recipient))
    if admitted(row, recipient):
        return _verdict(ALLOW, "bridge", "That message goes to you.", detail, _fp("bridge", row.get("id") or "", recipient, text[:80]))
    return _verdict(
        ASK,
        "bridge-send",
        "That message goes to someone other than you. It waits for you.",
        detail,
        _fp("bridge", row.get("id") or "", recipient, text[:80]),
    )


def card_proposal(request) -> dict:
    return {
        "kind": "bridge",
        "platform": "",
        "recipient": getattr(request, "command", "") or "",
        "text": getattr(request, "body", "") or "",
        "bridge": getattr(request, "path", "") or "",
    }


def keyboard(card_id: str, kind: str) -> dict:
    approve = f"ea:{card_id}:approve"
    deny = f"ea:{card_id}:deny"
    if kind == "discord":
        return {
            "components": [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": approve},
                    {"type": 2, "style": 4, "label": "Deny", "custom_id": deny},
                ],
            }]
        }
    return {
        "inline_keyboard": [[
            {"text": "Approve", "callback_data": approve},
            {"text": "Deny", "callback_data": deny},
        ]]
    }


def parse_callback(data: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"ea:([0-9a-fA-F-]{36}):(approve|deny)", (data or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def apply_callback(store: Store, row: dict, user_id: str, data: str) -> str:
    """A stranger's tap does nothing. The person's tap answers the card."""
    parsed = parse_callback(data)
    if parsed is None:
        return ""
    if not admitted(row, user_id):
        log_event(store, row.get("kind") or "", user_id, "ignored")
        return ""
    card_id, decision = parsed
    from easyagent import safety as safety_mod

    waiting = safety_mod._PENDING.get(card_id)
    owner = str(getattr(waiting, "bot_id", "") or "") if waiting is not None else ""
    bridge_bot = str(row.get("bot_id") or "")
    if waiting is not None and owner != bridge_bot:
        log_event(store, row.get("kind") or "", user_id, "ignored")
        return ""
    card = safety_mod.resolve_card(card_id, decision)
    if card is None:
        return ""
    log_event(store, row.get("kind") or "", user_id, decision)
    return decision


def notice_text(card) -> str:
    why = getattr(card, "why", "") or "EasyAgent is waiting for you."
    detail = getattr(card, "detail", "") or ""
    text = f"{why}\n{detail}".strip()
    return text[:500]


def schedule_card(store: Store, bot_id: str, card) -> None:
    card_id = getattr(card, "id", "") or ""
    if not card_id or card_id in _NOTIFIED:
        return
    _NOTIFIED.add(card_id)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_send_card(store, bot_id, card))


async def _send_card(store: Store, bot_id: str, card) -> None:
    for row in list_records(store):
        if row.get("enabled") is False or row.get("bot_id") != bot_id:
            continue
        user_id = str(row.get("last_user_id") or "")
        chat_id = str(row.get("last_chat_id") or "")
        if not user_id or not admitted(row, user_id) or not chat_id:
            continue
        token = get_secret(secret_account(row["id"]), store)
        if not token:
            continue
        text = notice_text(card)
        buttons = keyboard(card.id, row.get("kind") or "telegram")
        try:
            if row.get("kind") == "discord":
                await discord_send(token, chat_id, text, buttons)
            else:
                await telegram_send(token, chat_id, text, buttons.get("inline_keyboard"))
        except Exception:
            return


async def post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, json=payload, headers=headers or {})
    except httpx.HTTPError as exc:
        raise RuntimeError("The bridge did not answer.") from exc
    if response.status_code >= 400:
        raise RuntimeError("The bridge did not answer.")
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def get_json(url: str, headers: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=headers or {})
    except httpx.HTTPError as exc:
        raise RuntimeError("The bridge did not answer.") from exc
    if response.status_code >= 400:
        raise RuntimeError("The bridge did not answer.")
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def telegram_send(token: str, chat_id: str, text: str, keyboard_rows: list | None = None) -> None:
    payload: dict = {"chat_id": chat_id, "text": (text or "")[:4000]}
    if keyboard_rows:
        payload["reply_markup"] = {"inline_keyboard": keyboard_rows}
    await post_json(f"https://api.telegram.org/bot{token}/sendMessage", payload)


async def telegram_updates(token: str, offset: int) -> list[dict]:
    data = await get_json(f"https://api.telegram.org/bot{token}/getUpdates?offset={int(offset)}&timeout=0")
    result = data.get("result") if isinstance(data, dict) else None
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


async def discord_send(token: str, channel_id: str, text: str, extra: dict | None = None) -> None:
    payload: dict = {"content": (text or "")[:2000]}
    if extra:
        payload.update(extra)
    await post_json(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        payload,
        {"Authorization": f"Bot {token}"},
    )


def bump_offset(store: Store, bridge_id: str, offset: int) -> None:
    rows = list_records(store)
    for row in rows:
        if row.get("id") == bridge_id:
            row["update_offset"] = int(offset)
    _save(store, rows)


async def handle_telegram_update(store: Store, row: dict, update: dict, reply) -> int | None:
    """Return the next offset, or None when this update should be tried again."""
    update_id = int(update.get("update_id") or 0)
    callback = update.get("callback_query") if isinstance(update.get("callback_query"), dict) else None
    if callback:
        user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        apply_callback(store, row, str(user.get("id") or ""), str(callback.get("data") or ""))
        return update_id + 1
    message = update.get("message") if isinstance(update.get("message"), dict) else None
    if not message:
        return update_id + 1
    user = message.get("from") if isinstance(message.get("from"), dict) else {}
    user_id = str(user.get("id") or "")
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or user_id)
    text = str(message.get("text") or "")
    if not admitted(row, user_id):
        log_event(store, "telegram", user_id, "ignored")
        return update_id + 1
    if await _lane_holding(store, row.get("bot_id") or ""):
        return None
    if not take_inbound(store, row, user_id, text, chat_id):
        return update_id + 1
    reply_text = await reply(store, row.get("bot_id") or "")
    token = get_secret(secret_account(row["id"]), store)
    if token and reply_text:
        await telegram_send(token, chat_id, scrub(reply_text, store))
        log_event(store, "telegram", user_id, "sent")
    return update_id + 1


def handle_discord_event(store: Store, row: dict, event: dict) -> tuple[str, str, str]:
    """Return (action, user_id, channel). action is ignored, accepted, callback, or skip."""
    name = str(event.get("t") or "")
    data = event.get("d") if isinstance(event.get("d"), dict) else {}
    if name == "INTERACTION_CREATE":
        user = data.get("member", {}).get("user") if isinstance(data.get("member"), dict) else data.get("user")
        if not isinstance(user, dict):
            user = {}
        custom = ""
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        custom = str(inner.get("custom_id") or "")
        apply_callback(store, row, str(user.get("id") or ""), custom)
        return "callback", str(user.get("id") or ""), str(data.get("channel_id") or "")
    if name != "MESSAGE_CREATE":
        return "skip", "", ""
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    if author.get("bot"):
        return "skip", str(author.get("id") or ""), ""
    user_id = str(author.get("id") or "")
    channel_id = str(data.get("channel_id") or "")
    text = str(data.get("content") or "")
    if not take_inbound(store, row, user_id, text, channel_id):
        return "ignored", user_id, channel_id
    return "accepted", user_id, channel_id


async def _lane_holding(store: Store, bot_id: str) -> bool:
    from easyagent.gate import lane_busy

    try:
        bot = store.get_bot(bot_id)
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except Exception:
        return False
    return lane_busy(str(endpoint.get("id") or ""))


async def deliver(store: Store, request, bot_id: str | None = None) -> str:
    row = find(store, getattr(request, "path", "") or "", bot_id)
    if row is None:
        from easyagent.tools import ToolError

        raise ToolError("That bridge is not saved.")
    recipient = str(getattr(request, "command", "") or "").strip()
    text = scrub(getattr(request, "body", "") or "", store).strip()
    if not text:
        from easyagent.tools import ToolError

        raise ToolError("The message was empty. It was not sent.")
    token = get_secret(secret_account(row["id"]), store)
    if not token:
        from easyagent.tools import ToolError

        raise ToolError("That bridge has no token in the keychain. It was not sent.")
    try:
        if row.get("kind") == "discord":
            await discord_send(token, recipient, text)
        else:
            await telegram_send(token, recipient, text)
    except RuntimeError as exc:
        from easyagent.tools import ToolError

        raise ToolError("The bridge did not answer. It was not sent.")
    log_event(store, row.get("kind") or "", recipient, "sent")
    who = "you" if admitted(row, recipient) else recipient
    return f"Sent on {row.get('kind')} to {who}."


def prompt_block(store: Store) -> str:
    rows = [public_record(row) for row in list_records(store) if row.get("enabled") is not False]
    if not rows:
        return "No Telegram or Discord bridge is set up. Do not invent one."
    lines = ["A bridge reaches you. Do not message anyone else unless a card says you may."]
    for row in rows:
        ids = ", ".join(row["allow_ids"]) or "(nobody)"
        lines.append(f"- {row['name']} ({row['kind']}): your ids {ids}")
    return "\n".join(lines)


async def run_bridges(store: Store, stop: asyncio.Event, reply) -> None:
    while not stop.is_set():
        for row in list_records(store):
            if stop.is_set() or row.get("enabled") is False:
                continue
            token = get_secret(secret_account(row["id"]), store)
            if not token or not row.get("bot_id"):
                continue
            try:
                if row.get("kind") == "telegram":
                    await _poll_telegram(store, row, token, reply)
                elif row.get("kind") == "discord":
                    _ensure_discord(store, row, token, reply)
            except Exception:
                continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            continue


async def _poll_telegram(store: Store, row: dict, token: str, reply) -> None:
    offset = int(row.get("update_offset") or 0)
    updates = await telegram_updates(token, offset)
    for update in updates:
        nxt = await handle_telegram_update(store, row, update, reply)
        if nxt is None:
            return
        offset = nxt
        bump_offset(store, row["id"], offset)
        row["update_offset"] = offset


_DISCORD: dict[str, asyncio.Task] = {}


def _ensure_discord(store: Store, row: dict, token: str, reply) -> None:
    current = _DISCORD.get(row["id"])
    if current is not None and not current.done():
        return
    _DISCORD[row["id"]] = asyncio.create_task(_discord_session(store, dict(row), token, reply))


async def _discord_session(store: Store, row: dict, token: str, reply) -> None:
    """One gateway connection. A failure waits for the next loop to try again."""
    import websockets

    info = await get_json("https://discord.com/api/v10/gateway/bot", {"Authorization": f"Bot {token}"})
    url = str(info.get("url") or "")
    if not url.startswith("wss://"):
        return
    async with websockets.connect(url + "/?v=10&encoding=json", open_timeout=10) as socket:
        raw = await asyncio.wait_for(socket.recv(), timeout=10)
        await socket.send(json.dumps({
            "op": 2,
            "d": {
                "token": token,
                "intents": 37376,
                "properties": {"os": "linux", "browser": "easyagent", "device": "easyagent"},
            },
        }))
        del raw
        while True:
            incoming = json.loads(await socket.recv())
            if not isinstance(incoming, dict):
                continue
            if str(incoming.get("t") or "") == "MESSAGE_CREATE":
                while await _lane_holding(store, row.get("bot_id") or ""):
                    await asyncio.sleep(0.2)
            action, user_id, channel_id = handle_discord_event(store, row, incoming)
            if action != "accepted":
                continue
            if await _lane_holding(store, row.get("bot_id") or ""):
                continue
            text = await reply(store, row.get("bot_id") or "")
            if text and channel_id:
                await discord_send(token, channel_id, scrub(text, store))
                log_event(store, "discord", user_id, "sent")
