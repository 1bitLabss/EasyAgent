"""Fences a reply can use to remember a fact or hand back one file.

A one-line memory fence is a new fact. It does not remove a line that is still true.
A skill fence is not memory.
"""

from __future__ import annotations

import re
from pathlib import Path

_MEMORY = re.compile(r"```memory[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_FILE = re.compile(r"```file[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _strip_one(pattern: re.Pattern[str], text: str) -> str:
    visible = pattern.sub("", text or "", count=1)
    return re.sub(r"\n{3,}", "\n\n", visible).strip()


def take_memory(text: str) -> tuple[str, str | None]:
    """Pull one new fact out of a reply. The caller appends it. It does not edit."""
    match = _MEMORY.search(text or "")
    if not match:
        return text or "", None
    fact = " ".join(match.group(1).split())
    if not fact:
        return _strip_one(_MEMORY, text), None
    return _strip_one(_MEMORY, text), fact[:500]


def take_file(text: str) -> tuple[str, dict | None]:
    """Pull one handed-back file. The first line is the name. The rest is the body."""
    match = _FILE.search(text or "")
    if not match:
        return text or "", None
    raw = match.group(1).strip("\n")
    lines = raw.splitlines()
    if len(lines) < 2 or not lines[0].strip():
        return text or "", None
    name = Path(lines[0].strip()).name
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "._- ").strip()
    name = (cleaned or "file.txt")[:80]
    body = "\n".join(lines[1:])
    if not body.strip():
        return _strip_one(_FILE, text), None
    return _strip_one(_FILE, text), {"name": name, "body": body}
