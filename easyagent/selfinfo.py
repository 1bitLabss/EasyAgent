"""Where a bot's own files live, and a read-only look back at them.

The bot is told these paths on every turn, so it never has to ask the
person where its chat log or memory is. The history tool reads and searches
the same files. Nothing here rewrites a chat.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from easyagent.paths import default_deliverable_dir, display_path
from easyagent.store import Store, StoreError, atomic_write_text

MEMORY_FILE = "MEMORY.md"
USER_FILE = "USER.md"
_NOTE_TITLES = ((MEMORY_FILE, "Memory"), (USER_FILE, "User"))

_SEARCH_HITS = 12
_SNIPPET = 240
_READ_MESSAGES = 40
_READ_CHARS = 12000
_MEMORY_CHARS = 12000
_LIST_CHATS = 30


def _show(path: Path) -> str:
    return display_path(path)


def _workspace(store: Store, bot_id: str) -> Path:
    from easyagent.workspace import bot_workspace

    return bot_workspace(store, bot_id)


def app_folder_note() -> str:
    """The per-account app folder on each operating system, and this one."""
    here = _show(default_deliverable_dir())
    return (
        f"{here} (Windows: %LOCALAPPDATA%\\EasyAgent, macOS: ~/Library/Application Support/EasyAgent, "
        "Linux: ~/.local/share/EasyAgent)"
    )


def bot_paths(store: Store, bot_id: str, chat_id: str | None = None) -> dict[str, Path]:
    bot_dir = store._bot_dir(bot_id)
    found = {
        "data": store.root,
        "bot": bot_dir,
        "chats": bot_dir / "chats",
        "notes": bot_dir / "notes",
        "memory_md": bot_dir / "notes" / MEMORY_FILE,
        "user_md": bot_dir / "notes" / USER_FILE,
        "memory_dir": bot_dir / "memory",
        "memory_index": bot_dir / "memory" / "index.txt",
        "skills": store.skills_dir,
        "direction": store.direction_path,
        "app": default_deliverable_dir(),
    }
    cleaned = (chat_id or "").strip().lower()
    if cleaned and re.fullmatch(r"[a-z0-9-]{8,80}", cleaned):
        found["chat"] = bot_dir / "chats" / f"{cleaned}.json"
    return found


def ensure_own_files(store: Store, bot_id: str) -> None:
    """Create MEMORY.md, USER.md, and the memory index when they are missing.

    An existing file is never overwritten. A failure to write is not fatal.
    """
    try:
        paths = bot_paths(store, bot_id)
    except StoreError:
        return
    try:
        paths["notes"].mkdir(parents=True, exist_ok=True)
        for name, title in _NOTE_TITLES:
            path = paths["notes"] / name
            if not path.exists():
                atomic_write_text(path, f"# {title}\n\n")
        paths["memory_dir"].mkdir(parents=True, exist_ok=True)
        if not paths["memory_index"].exists():
            atomic_write_text(paths["memory_index"], "")
    except OSError:
        return


def own_files_prompt(store: Store, bot_id: str, chat_id: str | None = None) -> str:
    """The system-prompt block that names the bot's own files."""
    try:
        paths = bot_paths(store, bot_id, chat_id)
    except StoreError:
        return ""
    rows = [
        f"- EasyAgent data folder: {_show(paths['data'])}",
        f"- Your folder: {_show(paths['bot'])}",
        f"- Your chat transcripts: {_show(paths['chats'])} (one .json file per chat, every message kept)",
    ]
    if "chat" in paths:
        rows.append(f"- This chat's transcript: {_show(paths['chat'])}")
    rows += [
        f"- Your memory file: {_show(paths['memory_md'])} (lessons and facts you keep)",
        f"- Notes about the person: {_show(paths['user_md'])}",
        "- Notes you keep while idle, in that same notes folder: MISTAKES.md, PROMISES.md, UNKNOWNS.md, "
        "PREDICTIONS.md, HABITS.md, PLAYBOOK.md, WORLD.md, and DREAMS.md",
        f"- Your memory topics: {_show(paths['memory_index'])} names the topic files in {_show(paths['memory_dir'])}",
        f"- Skills (shared by every bot): {_show(paths['skills'])} (one .md file per skill)",
        f"- Direction: {_show(paths['direction'])}",
        f"- This bot's workspace, where a file goes when no folder is named: {_show(_workspace(store, bot_id))}",
    ]
    return "\n".join(rows)


# --- history tool --------------------------------------------------------


def _chat_files(store: Store, bot_id: str) -> list[Path]:
    folder = store._bot_dir(bot_id) / "chats"
    if not folder.is_dir():
        return []
    files = [
        path
        for path in folder.iterdir()
        if path.is_file() and not path.is_symlink() and path.suffix == ".json"
    ]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _line(message: dict) -> str:
    text = message.get("content") or ""
    if not isinstance(text, str):
        text = str(text)
    if message.get("error"):
        text = "(failed request) " + text
    return text


def _snippet(text: str, needle: str) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= _SNIPPET:
        return flat
    at = flat.lower().find(needle.lower()) if needle else -1
    if at < 0:
        return flat[: _SNIPPET - 1] + "…"
    start = max(0, at - _SNIPPET // 3)
    piece = flat[start : start + _SNIPPET]
    return ("…" if start else "") + piece + ("…" if start + _SNIPPET < len(flat) else "")


def list_chats(store: Store, bot_id: str) -> str:
    rows = []
    for path in _chat_files(store, bot_id)[:_LIST_CHATS]:
        chat = _load(path)
        if not chat:
            continue
        count = len(chat.get("messages") or [])
        rows.append(
            f"- {chat.get('title') or 'New chat'} | id {chat.get('id')} | {count} messages | "
            f"updated {chat.get('updated_at') or '?'} | {_show(path)}"
        )
    if not rows:
        return "No saved chats yet."
    return "Your chats, newest first:\n" + "\n".join(rows)


def search_history(store: Store, bot_id: str, query: str) -> str:
    """Case-insensitive search over this bot's chats and memory files."""
    needle = " ".join((query or "").split())
    if not needle:
        raise StoreError("Name the words to search for.", 400)
    terms = [term for term in needle.lower().split() if term]
    hits: list[str] = []

    def matches(text: str) -> bool:
        low = (text or "").lower()
        if needle.lower() in low:
            return True
        return all(term in low for term in terms)

    for path in _chat_files(store, bot_id):
        chat = _load(path)
        if not chat:
            continue
        title = chat.get("title") or "New chat"
        for index, message in enumerate(chat.get("messages") or []):
            text = _line(message)
            if matches(text):
                hits.append(
                    f"- chat \"{title}\" (id {chat.get('id')}) message {index + 1}, {message.get('role', 'user')}, "
                    f"{message.get('created_at') or '?'}: {_snippet(text, terms[0] if terms else needle)}"
                )
                if len(hits) >= _SEARCH_HITS:
                    break
        if len(hits) >= _SEARCH_HITS:
            break
    try:
        from easyagent.retrieve import search_passages

        seen = "\n".join(hits)
        for passage in search_passages(store, bot_id, needle, limit=_SEARCH_HITS):
            if passage.text and passage.text[:80] in seen:
                continue
            hits.append(
                f"- chat \"{passage.title}\" (id {passage.chat_id}) message {passage.index + 1}, "
                f"{passage.role}, {passage.created_at or '?'}: {_snippet(passage.text, terms[0] if terms else needle)}"
            )
            if len(hits) >= _SEARCH_HITS:
                break
    except Exception:
        pass
    for label, text in _memory_sources(store, bot_id):
        for row in text.splitlines():
            if row.strip() and matches(row):
                hits.append(f"- {label}: {_snippet(row, terms[0] if terms else needle)}")
                if len(hits) >= _SEARCH_HITS * 2:
                    break
    if not hits:
        return f"Nothing in your chats or memory matches \"{needle}\"."
    return f"Matches for \"{needle}\":\n" + "\n".join(hits)


def _find_chat(store: Store, bot_id: str, key: str) -> tuple[Path, dict] | None:
    files = _chat_files(store, bot_id)
    wanted = " ".join((key or "").split()).lower()
    if wanted in {"", "this", "current", "this chat", "latest", "last"}:
        for path in files:
            chat = _load(path)
            if chat:
                return path, chat
        return None
    for path in files:
        if path.stem == wanted or (len(wanted) >= 6 and path.stem.startswith(wanted)):
            chat = _load(path)
            if chat:
                return path, chat
    for path in files:
        chat = _load(path)
        if chat and wanted in (chat.get("title") or "").lower():
            return path, chat
    return None


def read_chat(store: Store, bot_id: str, key: str) -> str:
    found = _find_chat(store, bot_id, key)
    if found is None:
        raise StoreError(f"No chat of yours matches \"{key}\". List your chats first.", 404)
    path, chat = found
    messages = chat.get("messages") or []
    shown = messages[-_READ_MESSAGES:]
    skipped = len(messages) - len(shown)
    rows = []
    for offset, message in enumerate(shown, start=skipped + 1):
        rows.append(f"[{offset}] {message.get('role', 'user')} {message.get('created_at') or ''}: {_line(message)}")
    body = "\n".join(rows)
    if len(body) > _READ_CHARS:
        body = "…" + body[-_READ_CHARS:]
    head = f"Chat \"{chat.get('title') or 'New chat'}\" ({len(messages)} messages) at {_show(path)}"
    if skipped:
        head += f". Showing the last {len(shown)}; search to find older lines."
    return f"{head}\n{body}"


def _memory_sources(store: Store, bot_id: str) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    paths = bot_paths(store, bot_id)
    for key in ("memory_md", "user_md"):
        path = paths[key]
        if path.is_file():
            try:
                sources.append((_show(path), path.read_text(encoding="utf-8")))
            except OSError:
                continue
    try:
        slugs = store.memory_slugs(bot_id)
    except StoreError:
        slugs = []
    for slug in slugs:
        try:
            sources.append((f"memory topic {slug}", store.topic_text(bot_id, slug)))
        except StoreError:
            continue
    return sources


def read_memory(store: Store, bot_id: str) -> str:
    ensure_own_files(store, bot_id)
    parts = []
    for label, text in _memory_sources(store, bot_id):
        body = text.strip() or "(empty)"
        parts.append(f"== {label}\n{body}")
    joined = "\n\n".join(parts) if parts else "(no memory yet)"
    if len(joined) > _MEMORY_CHARS:
        joined = joined[:_MEMORY_CHARS].rstrip() + "\n[more memory not shown; search for a word]"
    return joined


def run_history(store: Store, bot_id: str, action: str, argument: str = "") -> str:
    act = (action or "").strip().lower()
    if act in {"", "list", "ls", "chats"}:
        return list_chats(store, bot_id)
    if act in {"search", "find", "grep"}:
        return search_history(store, bot_id, argument)
    if act in {"read", "open", "chat"}:
        return read_chat(store, bot_id, argument)
    if act in {"memory", "notes"}:
        return read_memory(store, bot_id)
    raise StoreError("History can list your chats, search them, read one, or read your memory.", 400)


__all__ = [
    "app_folder_note",
    "bot_paths",
    "ensure_own_files",
    "own_files_prompt",
    "run_history",
]
