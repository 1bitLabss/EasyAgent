"""Where a file goes when the person did not name a folder.

Chats and settings stay in ``EASYAGENT_DATA`` (``./data``). This folder is only
for pages and other files the bot writes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_deliverable_dir() -> Path:
    """The account folder for files the bot writes. It is created on write."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "EasyAgent"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "EasyAgent"
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return root / "EasyAgent"


def display_path(path: Path) -> str:
    """The path as this operating system writes it."""
    text = str(path)
    if sys.platform == "win32":
        return text.replace("/", "\\")
    return text


def deliverable_file(name: str) -> str:
    """One file in the default folder, in this operating system's path form."""
    return display_path(default_deliverable_dir() / name)
