"""Load eval tasks from evals/tasks/*.json."""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2] / "evals" / "tasks"


def tasks_dir() -> Path:
    return _ROOT


def load_tasks(ids: list[str] | None = None) -> list[dict]:
    """Every task file, sorted by id. Unknown ids are an error."""
    folder = tasks_dir()
    if not folder.is_dir():
        raise FileNotFoundError(f"No task folder at {folder}")
    found: list[dict] = []
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("id"):
            raise ValueError(f"{path.name} needs an id.")
        data["_path"] = str(path)
        found.append(data)
    if not found:
        raise FileNotFoundError(f"No tasks in {folder}")
    wanted = [item.strip() for item in (ids or []) if item and item.strip()]
    if not wanted:
        return [item for item in found if item.get("suite", True) is not False]
    by_id = {item["id"]: item for item in found}
    missing = [item for item in wanted if item not in by_id]
    if missing:
        raise ValueError("Unknown task: " + ", ".join(missing))
    return [by_id[item] for item in wanted]


def substitute(value, workspace: str):
    """Fill {workspace} in strings, lists, and dicts."""
    if isinstance(value, str):
        return value.replace("{workspace}", workspace)
    if isinstance(value, list):
        return [substitute(item, workspace) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, workspace) for key, item in value.items()}
    return value
