"""A night pass proposes one skill or memory change and does not install it.

A second prompt must name a concrete counterexample. If it cannot, the
proposal is dropped. Failure to break a skill is not a reason to keep it.
An older memory line is never rewritten.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from easyagent import gate
from easyagent import llm
from easyagent.loop import PLAYBOOKS
from easyagent.skills import RESERVED_SLUGS, slugify
from easyagent.store import Store, new_id, now_iso
from easyagent.tools import redact

_FIELD = re.compile(r"(?im)^(kind|name|quote|text|replaces):\s*(.+)$")
_SHRUG = re.compile(
    r"^(none|no|n/?a|nothing|no problem|looks good|looks fine|cannot|no counterexample|fine|ok|okay|no issue|nothing wrong|consider adding more detail|consider adding detail)\b",
    re.IGNORECASE,
)

_PROPOSER = """Propose one skill or one memory fact from the transcript. Do not save it and do not install it.
Reply in exactly this shape, or reply NONE.

kind: skill
name: short-slug
quote: the message id
text: one rule

or

kind: memory
quote: the message id
text: one fact

or

kind: playbook
name: build
quote: the message id
text: one proposed edit to that playbook

Do not replace an older memory line. Do not say the change is already saved. Do not install a playbook."""

_BREAKER = """You did not write the proposal. Name one concrete counterexample.
Quote a line that is already in the transcript, or write one line that starts with "variant: ".
If you cannot name a concrete counterexample, reply NONE.
Do not say the proposal looks fine. Do not install anything. A failure to find a problem is not approval."""


def parse_proposal(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw or raw.upper() == "NONE":
        return None
    fields: dict[str, str] = {}
    for match in _FIELD.finditer(raw):
        fields[match.group(1).lower()] = match.group(2).strip()
    kind = fields.get("kind") or ""
    if kind not in {"skill", "memory", "playbook"}:
        return None
    if not fields.get("quote") or not fields.get("text"):
        return None
    return fields


def concrete_counterexample(text: str, messages: list[dict]) -> str | None:
    """A quoted transcript line, or one variant line. A shrug is not a counterexample."""
    raw = (text or "").strip()
    if not raw or _SHRUG.match(raw):
        return None
    for line in raw.splitlines():
        piece = line.strip()
        if piece.lower().startswith("variant:"):
            variant = piece.split(":", 1)[1].strip()
            if len(variant) >= 12 and not _SHRUG.match(variant):
                return variant
    for message in messages:
        content = " ".join((message.get("content") or "").split())
        if len(content) >= 12 and content in raw:
            return content
        width = min(24, len(content))
        if width < 12:
            continue
        for start in range(0, len(content) - width + 1, 8):
            chunk = content[start : start + width]
            if len(chunk.strip()) >= 12 and chunk in raw:
                return chunk
    return None


def _newest_chat(store: Store) -> tuple[dict, dict] | None:
    best = None
    best_key = ""
    for bot in store.list_bots():
        try:
            chats = store.list_chats(bot["id"])
        except Exception:
            continue
        for listed in chats:
            key = listed.get("updated_at") or ""
            if key < best_key:
                continue
            try:
                chat = store.get_chat(bot["id"], listed["id"])
            except Exception:
                continue
            best_key = key
            best = (bot, chat)
    return best


def _transcript(messages: list[dict]) -> str:
    day = datetime.now(timezone.utc).date().isoformat()
    todays = [item for item in messages if str(item.get("created_at") or "").startswith(day)]
    rows = todays or list(messages)
    lines = []
    for message in rows[-30:]:
        content = " ".join((message.get("content") or "").split())
        if len(content) > 400:
            content = content[:400].rstrip()
        lines.append(f"{message.get('id')} | {message.get('role') or 'user'} | {content}")
    return "\n".join(lines)


def _public(record: dict) -> dict:
    return {
        "id": record["id"],
        "kind": record["kind"],
        "name": record.get("name") or "",
        "text": record.get("text") or "",
        "quote": record.get("quote") or "",
        "counterexample": record.get("counterexample") or "",
        "installed": False,
    }


async def run_night(store: Store) -> dict:
    """Read the newest chat, propose once, and keep the proposal only with a counterexample."""
    found = _newest_chat(store)
    if found is None:
        return {"kept": [], "dropped": 0}
    bot, chat = found
    messages = list(chat.get("messages") or [])
    if not messages:
        return {"kept": [], "dropped": 0}
    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except Exception:
        endpoint = None
    if endpoint is None:
        return {"kept": [], "dropped": 0}
    transcript = _transcript(messages)
    if not transcript.strip():
        return {"kept": [], "dropped": 0}
    conn = gate.bind_connection(endpoint, bot.get("name") or "")
    try:
        proposal_text = await llm.complete(
            base_url=endpoint["base_url"],
            api_key=endpoint.get("api_key") or None,
            model=(bot.get("model") or endpoint.get("model") or None),
            messages=[
                {"role": "system", "content": _PROPOSER},
                {"role": "user", "content": transcript},
            ],
            tools=False,
        )
    finally:
        gate.reset_connection(conn)
    parsed = parse_proposal(proposal_text)
    if parsed is None:
        return {"kept": [], "dropped": 1}
    ids = {item.get("id") for item in messages}
    if parsed.get("quote") not in ids:
        return {"kept": [], "dropped": 1}
    if (parsed.get("replaces") or "").strip():
        return {"kept": [], "dropped": 1}
    if parsed["kind"] == "skill":
        slug = slugify(parsed.get("name") or "")
        if not slug or slug in RESERVED_SLUGS:
            return {"kept": [], "dropped": 1}
    if parsed["kind"] == "playbook" and (parsed.get("name") or "") not in PLAYBOOKS:
        return {"kept": [], "dropped": 1}
    conn = gate.bind_connection(endpoint, bot.get("name") or "")
    try:
        breaker_text = await llm.complete(
            base_url=endpoint["base_url"],
            api_key=endpoint.get("api_key") or None,
            model=(bot.get("model") or endpoint.get("model") or None),
            messages=[
                {"role": "system", "content": _BREAKER},
                {"role": "user", "content": transcript + "\n\nProposal:\n" + proposal_text},
            ],
            tools=False,
        )
    finally:
        gate.reset_connection(conn)
    example = concrete_counterexample(breaker_text, messages)
    if not example:
        return {"kept": [], "dropped": 1}
    text = " ".join(redact(store, parsed["text"]).split())
    example = " ".join(redact(store, example).split())
    if not text or not example:
        return {"kept": [], "dropped": 1}
    from easyagent.safety import lesson_weakens

    if lesson_weakens(text):
        return {"kept": [], "dropped": 1}
    record = {
        "id": new_id(),
        "bot_id": bot["id"],
        "chat_id": chat["id"],
        "kind": parsed["kind"],
        "name": parsed.get("name") or "",
        "text": text,
        "quote": parsed["quote"],
        "counterexample": example,
        "installed": False,
        "created_at": now_iso(),
    }
    store.add_proposal(record)
    return {"kept": [_public(record)], "dropped": 0}
