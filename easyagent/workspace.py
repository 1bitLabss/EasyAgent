"""A bot's workspace is a folder it may write in. The install is not one of them.

The install directory, the data directory, and the program package are refused.
A bot that already points at one of those is moved to its own folder on startup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_INSTALL_NAMES = {"easyagent-desktop.exe", "uninstall.exe", "easyagent-desktop"}


def package_dir() -> Path:
    """The easyagent package directory."""
    return Path(__file__).resolve().parent


def _has_installer(folder: Path) -> bool:
    try:
        names = {item.name.lower() for item in folder.iterdir()}
    except OSError:
        return False
    return bool(names & _INSTALL_NAMES)


def install_dirs() -> list[Path]:
    """Folders that hold the desktop program, next to easyagent-desktop.exe or uninstall.exe."""
    seeds: list[Path] = []
    try:
        seeds.append(Path.cwd())
    except OSError:
        pass
    if sys.argv and sys.argv[0]:
        try:
            seeds.append(Path(sys.argv[0]).resolve().parent)
        except OSError:
            seeds.append(Path(sys.argv[0]).parent)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        seeds.append(Path(local) / "EasyAgent")
    found: list[Path] = []
    seen: set[str] = set()
    for seed in seeds:
        chain = [seed, *list(seed.parents)[:3]]
        for folder in chain:
            try:
                key = str(folder.resolve())
            except OSError:
                key = str(folder)
            if key in seen:
                continue
            seen.add(key)
            if _has_installer(folder):
                found.append(Path(key))
    return found


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def overlaps(path: Path, root: Path) -> bool:
    """True when the two paths are the same, or one holds the other."""
    left = _resolve(path)
    right = _resolve(root)
    return left == right or right in left.parents or left in right.parents


def canonical_workspace(store, bot_id: str) -> Path:
    """The fresh per-bot folder under the data directory."""
    return store.root / "bots" / bot_id / "workspace"


def _is_canonical(store, path: Path) -> bool:
    resolved = _resolve(path)
    try:
        rel = resolved.relative_to(_resolve(store.root))
    except ValueError:
        return False
    return len(rel.parts) >= 3 and rel.parts[0] == "bots" and rel.parts[2] == "workspace"


def workspace_forbidden(store, path: Path) -> bool:
    """True when this folder contains the install, the data dir, or the package.

    The per-bot folder under the data directory is the safe place, so it is allowed.
    """
    if not str(path):
        return True
    resolved = _resolve(path)
    if _is_canonical(store, resolved):
        return False
    if _has_installer(resolved):
        return True
    for folder in install_dirs():
        if overlaps(resolved, folder):
            return True
    if overlaps(resolved, package_dir()):
        return True
    data = _resolve(store.root)
    if resolved == data or data in resolved.parents:
        return True
    try:
        resolved.relative_to(data)
    except ValueError:
        return False
    return True


def bot_workspace(store, bot_id: str, *, create: bool = False) -> Path:
    """The folder this bot works in. A forbidden path falls back to the per-bot folder."""
    path = canonical_workspace(store, bot_id)
    try:
        bot = store.get_bot(bot_id)
    except Exception:
        bot = None
    raw = str((bot or {}).get("workspace") or "").strip()
    if raw:
        chosen = Path(raw)
        if not workspace_forbidden(store, chosen):
            path = chosen
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def assign_workspace(store, bot: dict, raw: str) -> dict:
    """Save a workspace. The install, the data dir, and the package are refused."""
    from easyagent.store import StoreError, atomic_write_json

    text = (raw or "").strip()
    if not text:
        raise StoreError("Name the workspace folder.", 400)
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise StoreError("The workspace has to be a full path.", 400)
    if workspace_forbidden(store, path):
        raise StoreError(
            "That folder contains the EasyAgent install, its data, or the program. It was not set.",
            400,
        )
    path.mkdir(parents=True, exist_ok=True)
    bot["workspace"] = str(_resolve(path))
    atomic_write_json(store.root / "bots" / bot["id"] / "bot.json", bot)
    return bot


def migrate_workspaces(store) -> list[str]:
    """Point any bot that works inside the install, the data dir, or the package at a fresh folder."""
    import json

    from easyagent.store import StoreError, atomic_write_json

    spoken: list[str] = []
    try:
        bots = store.list_bots()
    except StoreError:
        return spoken
    for bot in bots:
        bot_id = bot.get("id") or ""
        if not bot_id:
            continue
        name = bot.get("name") or "This bot"
        fresh = canonical_workspace(store, bot_id)
        notes: list[str] = []
        changed = False
        raw = str(bot.get("workspace") or "").strip()
        if raw and workspace_forbidden(store, Path(raw)):
            fresh.mkdir(parents=True, exist_ok=True)
            notes.append(
                f"{name}'s workspace was {raw}. That folder contains the EasyAgent install, its data, or the program. "
                f"It now uses a fresh folder: {fresh}."
            )
            bot["workspace"] = str(fresh)
            changed = True
        settings_path = store.root / "bots" / bot_id / "sandbox.json"
        if settings_path.is_file():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                settings = None
            if isinstance(settings, dict):
                kept = []
                removed = []
                for grant in settings.get("grants") or []:
                    if not isinstance(grant, dict):
                        continue
                    path = str(grant.get("path") or "")
                    if path and workspace_forbidden(store, Path(path)):
                        removed.append(path)
                    else:
                        kept.append(grant)
                if removed:
                    settings["grants"] = kept
                    atomic_write_json(settings_path, settings)
                    fresh.mkdir(parents=True, exist_ok=True)
                    if not str(bot.get("workspace") or "").strip():
                        bot["workspace"] = str(fresh)
                    listed = ", ".join(removed)
                    notes.append(
                        f"{name}'s grants overlapped the EasyAgent install, its data, or the program. "
                        f"Removed: {listed}. It now uses a fresh folder: {fresh}."
                    )
                    changed = True
        if not notes:
            continue
        existing = [item for item in (bot.get("notices") or []) if isinstance(item, str)]
        for note in notes:
            if note not in existing:
                existing.append(note)
            spoken.append(note)
        bot["notices"] = existing
        if changed:
            atomic_write_json(store.root / "bots" / bot_id / "bot.json", bot)
    return spoken
