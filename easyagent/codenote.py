"""Undo copies and spilled command output for a coding turn.

A write keeps the previous bytes beside the file, under .ea-undo. A long
command result keeps its head and tail for the model and the full text in
.ea-spill. A secret is not copied into either folder.
"""

from __future__ import annotations

import re
from pathlib import Path

_SECRET_MARK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|sk_live_[A-Za-z0-9]|ghp_[A-Za-z0-9]|_authToken="
)


def _secret(text: str) -> bool:
    return bool(_SECRET_MARK.search(text or ""))


def _scrubbed(text: str) -> str:
    from easyagent.journal import redact_leaks

    return redact_leaks(text or "")


def remember_undo(path: Path, previous: str) -> Path | None:
    """Copy the previous text next to the file. A secret is not copied."""
    body = previous or ""
    if not body.strip() or _secret(body):
        return None
    body = _scrubbed(body)
    folder = path.parent / ".ea-undo"
    folder.mkdir(parents=True, exist_ok=True)
    dest = (folder / path.name).resolve()
    if dest.parent != folder.resolve():
        return None
    dest.write_text(body, encoding="utf-8")
    return dest


def spill_output(text: str, folder: Path, *, label: str = "output") -> Path | None:
    """Write the full text. The caller still shows head and tail."""
    body = text or ""
    if _secret(body):
        return None
    body = _scrubbed(body)
    folder.mkdir(parents=True, exist_ok=True)
    dest = (folder / f"{label}.txt").resolve()
    if dest.parent != folder.resolve():
        return None
    dest.write_text(body, encoding="utf-8")
    return dest


CODING_BLOCK = (
    "# Coding\n"
    "Before you stop, verify the change by reading it back or running the check. "
    "A replaced file keeps its previous bytes in .ea-undo next to it. "
    "A long command keeps its head and tail here and spills the full text under .ea-spill."
)
