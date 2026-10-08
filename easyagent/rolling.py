"""A capped rolling summary, written by the bot's own model while idle.

A sentence is kept only when it cites a real message id and does not name
a fact that is absent from those messages. The reply path does not wait for this.
"""

from __future__ import annotations

import os
import re

from easyagent import llm
from easyagent.store import Store, StoreError

CAP = 2000
_CITE = re.compile(r"\[m:([A-Za-z0-9_-]+)\]")
_LONG = re.compile(r"[a-z0-9]{6,}")


def grounded_summary(text: str, messages: list[dict], cap: int = CAP) -> str:
    """Drop any line that invents a fact or cites an id that is not in `messages`."""
    sources: dict[str, str] = {}
    for message in messages or []:
        mid = str(message.get("id") or "")
        if mid:
            sources[mid] = (message.get("content") or "").lower()
    kept: list[str] = []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        cites = _CITE.findall(line)
        if not cites or any(cite not in sources for cite in cites):
            continue
        blob = "\n".join(sources[cite] for cite in cites)
        body = _CITE.sub(" ", line).lower()
        if any(word not in blob for word in _LONG.findall(body)):
            continue
        kept.append(line)
    while kept and len("\n".join(kept)) > cap:
        kept.pop(0)
    result = "\n".join(kept).strip()
    if len(result) > cap:
        return ""
    return result


def _span(chat: dict) -> tuple[int, int]:
    messages = list(chat.get("messages") or [])
    fresh = int(chat.get("fresh_from") or 0)
    if fresh < 0:
        fresh = 0
    if fresh > len(messages):
        fresh = len(messages)
    through = int(chat.get("summarized_through") or 0)
    if through < fresh:
        through = fresh
    if through > len(messages):
        through = len(messages)
    return fresh, through


def _prompt(chat: dict) -> str | None:
    messages = list(chat.get("messages") or [])
    start, end = _span(chat)
    if end - start < 1:
        return None
    lines = []
    for message in messages[start:end]:
        mid = str(message.get("id") or "")
        if not mid:
            continue
        text = " ".join((message.get("content") or "").split())
        if len(text) > 240:
            text = text[:239].rstrip() + "…"
        lines.append(f"[m:{mid}] {message.get('role') or 'user'}: {text}")
    if not lines:
        return None
    blob = "\n".join(lines)
    if len(blob) > 8000:
        blob = blob[-8000:]
    prior = (chat.get("rolling_summary") or "").strip() or "(none)"
    return (
        "Summarize only the lines below for a later turn. "
        "Every sentence must end with a message id copied from those lines, written as [m:id]. "
        "Do not add facts that are not in the lines. Do not invent ids. "
        f"Stay under {CAP} characters.\n\n"
        f"Previous summary:\n{prior}\n\n"
        f"Lines:\n{blob}"
    )


async def refresh_rolling_summary(store: Store, bot: dict, chat: dict, *, timeout: float = 8.0) -> str:
    """Ask this bot's connection to refresh the summary. Messages are not rewritten."""
    prompt = _prompt(chat)
    if not prompt:
        return str(chat.get("rolling_summary") or "")
    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except StoreError:
        endpoint = None
    if not endpoint:
        return str(chat.get("rolling_summary") or "")
    start, end = _span(chat)
    messages = list(chat.get("messages") or [])
    try:
        text = await llm.complete(
            base_url=endpoint["base_url"],
            api_key=endpoint.get("api_key") or None,
            model=(bot.get("model") or endpoint.get("model") or None),
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout,
        )
    except Exception:
        return str(chat.get("rolling_summary") or "")
    accepted = grounded_summary(text, messages[start:end], CAP)
    if not accepted:
        return str(chat.get("rolling_summary") or "")
    store.save_rolling_summary(bot["id"], chat["id"], accepted, end)
    return accepted


def _running(chat: dict) -> bool:
    run = chat.get("run") if isinstance(chat.get("run"), dict) else {}
    if run.get("status") == "running":
        return True
    return any(isinstance(item, dict) and item.get("live") for item in chat.get("messages") or [])


async def refresh_idle(store: Store) -> None:
    """One due summary, and only when no chat is running. Tests set EASYAGENT_ROLLING=0."""
    if os.environ.get("EASYAGENT_ROLLING") == "0":
        return
    try:
        bots = store.list_bots()
    except StoreError:
        return
    for bot in bots:
        chat = store.existing_ongoing(bot["id"])
        if chat is None or _running(chat):
            continue
        start, end = _span(chat)
        if end - start < 2:
            continue
        if int(chat.get("rolling_through") or 0) >= end and (chat.get("rolling_summary") or "").strip():
            continue
        try:
            await refresh_rolling_summary(store, bot, chat)
        except Exception:
            continue
        return
