"""Disk store. One directory per bot. Transcripts are rewritten atomically.

A normal save never drops a message. The nightly prune can remove a message
only after its digest and search index have been checked."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from easyagent.direction import DEFAULT_DIRECTION
from easyagent.gate import clamp_parallel
from easyagent.skills import parse_skill_document, render_skill, slugify
from easyagent.vault import seal

ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REACTIONS = ("👍", "👎", "❤️", "👀")
TOPIC_LINE_CAP = 8
TOPIC_CAP = 24
EXAMPLE_MEMORY = "Example: replace this with a real preference. You can delete this line."
EXAMPLE_SKILL = {
    "name": "example-note",
    "description": "Example only. Delete this when you save a real skill.",
    "body": (
        "This file shows the shape of a skill. It is an example, not a fact about you. "
        "Delete it when you no longer need the sample."
    ),
}
_TOPIC_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOPIC_STOP = {
    "a", "an", "the", "and", "or", "to", "of", "for", "in", "on", "with",
    "this", "that", "it", "is", "are", "be", "as", "at", "by", "from",
}


class StoreError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _seal_legacy_computer(item: dict) -> tuple[dict, bool]:
    """Move a plaintext username or secret into the vault and drop the clear fields."""
    user = item.get("user") or ""
    secret = item.get("secret") or ""
    if not user and not secret:
        return item, False
    if not item.get("vault"):
        item["vault"] = seal(
            {
                "user": user,
                "auth": item.get("auth") or "password",
                "secret": secret,
            }
        )
    item.pop("user", None)
    item.pop("secret", None)
    item.pop("auth", None)
    return item, True


def canonical_emoji(emoji: str) -> str:
    """One of the four reactions, ignoring a missing variation selector."""
    cleaned = "".join((emoji or "").split())
    if cleaned in REACTIONS:
        return cleaned
    base = cleaned.replace("\ufe0f", "")
    if not base:
        return ""
    for known in REACTIONS:
        if known.replace("\ufe0f", "") == base:
            return known
    return ""


def fold_name(value: str) -> str:
    """Trim, collapse internal whitespace, and ignore case."""
    return " ".join(str(value or "").split()).casefold()


def names_match(typed: str, stored: str) -> bool:
    """The typed name matches the stored one. An empty name never matches."""
    right = fold_name(stored)
    return bool(right) and fold_name(typed) == right


def reaction_signal(message: dict) -> str:
    """The line the model sees when an emoji is on a message, naming who placed it."""
    if not isinstance(message, dict):
        return ""
    emoji = message.get("reaction")
    if emoji not in REACTIONS:
        return ""
    mid = str(message.get("id") or "").strip()
    excerpt = " ".join(str(message.get("content") or "").split())
    if len(excerpt) > 120:
        excerpt = excerpt[:119].rstrip() + "…"
    whose = "your message" if message.get("role") == "assistant" else "the person's message"
    ident = f" (id {mid})" if mid else ""
    actor = "You" if message.get("reaction_by") == "bot" else "The person"
    if excerpt:
        return f"{actor} reacted {emoji} to {whose}{ident}. It says: {excerpt}"
    return f"{actor} reacted {emoji} to {whose}{ident}."


def message_index(messages: list, *, self_id: str | None = None) -> str:
    """Recent ids, so a tapback can name one message."""
    lines = []
    for message in (messages or [])[-12:]:
        if not isinstance(message, dict):
            continue
        mid = str(message.get("id") or "").strip()
        if not mid:
            continue
        speaker = message.get("speaker")
        if message.get("role") == "user" and speaker in {None, "", "user"}:
            who = "person"
        elif self_id and speaker == self_id:
            who = "you"
        elif message.get("role") == "assistant" and not speaker:
            who = "you"
        else:
            who = " ".join(str(message.get("speaker_name") or "someone").split()) or "someone"
        excerpt = " ".join(str(message.get("content") or "").split())
        if len(excerpt) > 80:
            excerpt = excerpt[:79].rstrip() + "…"
        lines.append(f"- {who} {mid}: {excerpt}")
    return "\n".join(lines)


def _message_by_id(messages: list, message_id: str) -> dict | None:
    wanted = (message_id or "").strip().lower()
    if not wanted:
        return None
    exact = [item for item in messages if str(item.get("id") or "").lower() == wanted]
    if exact:
        return exact[0]
    if len(wanted) < 8:
        return None
    prefix = [item for item in messages if str(item.get("id") or "").lower().startswith(wanted)]
    if len(prefix) == 1:
        return prefix[0]
    return None


def _set_message_reaction(messages: list, message_id: str, emoji: str) -> None:
    """Put one emoji on a person's message. It replaces whatever was there."""
    chosen = canonical_emoji(emoji)
    if not chosen:
        raise StoreError("Pick one reaction.", 400)
    message = _message_by_id(messages, message_id)
    if message is None:
        raise StoreError("That message is not in the transcript.", 404)
    if message.get("role") != "user":
        raise StoreError("React to one of the person's messages.", 400)
    message["reaction"] = chosen
    message["reaction_by"] = "bot"


def _toggle_message_reaction(messages: list, message_id: str, emoji: str) -> None:
    """Set or clear one reaction. Message text, ids, and count stay put."""
    chosen = canonical_emoji(emoji)
    if not chosen:
        raise StoreError("Pick one reaction.", 400)
    emoji = chosen
    wanted = (message_id or "").strip().lower()
    if not ID_RE.fullmatch(wanted):
        raise StoreError("That message is not in the transcript.", 404)
    before = [(item.get("id"), item.get("content"), item.get("role"), item.get("speaker")) for item in messages]
    found = False
    for message in messages:
        if message.get("id") != wanted:
            continue
        found = True
        if message.get("reaction") == emoji:
            message.pop("reaction", None)
            message.pop("reaction_by", None)
        else:
            message["reaction"] = emoji
            message["reaction_by"] = "person"
        break
    if not found:
        raise StoreError("That message is not in the transcript.", 404)
    after = [(item.get("id"), item.get("content"), item.get("role"), item.get("speaker")) for item in messages]
    if after != before or len(messages) != len(before):
        raise StoreError("A reaction cannot change the message.", 500)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _topic_slug(name: str) -> str:
    words = [part.lower() for part in re.split(r"[^A-Za-z0-9]+", name or "") if part]
    slug = "-".join(words).strip("-")
    if len(slug) > 40:
        slug = slug[:40].rstrip("-")
    if slug == "index" or not _TOPIC_RE.fullmatch(slug or ""):
        raise StoreError("Name the topic. Nothing was changed.", 400)
    return slug


def _topic_title(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-"))


def _topic_heading(name: str, slug: str) -> str:
    cleaned = " ".join((name or "").split())
    try:
        same = _topic_slug(cleaned) == slug
    except StoreError:
        same = False
    if cleaned and same:
        return cleaned[:80]
    return _topic_title(slug)


def _topic_from_fact(text: str) -> str:
    words = []
    for raw in re.split(r"[^A-Za-z0-9]+", text or ""):
        piece = raw.lower()
        if len(piece) < 3 or piece in _TOPIC_STOP:
            continue
        words.append(piece)
        if len(words) == 3:
            break
    if not words:
        words = ["note"]
    return _topic_slug("-".join(words))


def _topic_matches(slug: str, text: str) -> bool:
    tokens = [part for part in slug.split("-") if len(part) >= 3]
    if not tokens:
        return False
    return all(re.search(rf"(?i)(?<!\w){re.escape(token)}(?!\w)", text or "") for token in tokens)


def _same_line(item: dict, key: str) -> bool:
    wanted = (key or "").strip()
    if ID_RE.fullmatch(wanted.lower()):
        return item.get("id") == wanted.lower()
    return item.get("text") == " ".join(wanted.split())


def _line_by_key(lines: list[dict], key: str) -> dict | None:
    matches = [item for item in lines if _same_line(item, key)]
    if len(matches) > 1:
        raise StoreError("More than one line says that.", 400)
    if not matches:
        return None
    return matches[0]


def new_id() -> str:
    return str(uuid.uuid4())


# Windows readers (antivirus, the indexer, another handle) deny the replace
# for a moment. Ten tries about 0.2s apart cover roughly two seconds.
_REPLACE_TRIES = 10
_REPLACE_DELAY = 0.2


def _replace_blocked(exc: BaseException) -> bool:
    """PermissionError, WinError 5 (access denied), or WinError 32 (sharing violation)."""
    if not isinstance(exc, OSError):
        return False
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, "winerror", None) in (5, 32)


def _extended_win(absolute: str) -> str:
    """The \\\\?\\ form of an absolute Windows path. Past MAX_PATH when long paths are off."""
    text = absolute.replace("/", "\\")
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _io_path(path: Path) -> Path:
    """A path the OS can open. On Windows this is the extended-length form."""
    text = os.fspath(path)
    if os.name != "nt":
        return Path(text)
    if text.startswith("\\\\?\\"):
        return Path(text)
    return Path(_extended_win(os.path.abspath(text)))


def _plain_path(path: Path) -> str:
    text = os.fspath(path)
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


class _StorePath(type(Path())):
    """Store paths. Every filesystem check uses the extended-length form on Windows.

    The base is the concrete path class (``PosixPath`` or ``WindowsPath``).
    ``pathlib.Path`` itself cannot be subclassed before 3.12.
    """

    def exists(self):
        return _io_path(self).exists()

    def is_file(self):
        return _io_path(self).is_file()

    def is_dir(self):
        return _io_path(self).is_dir()

    def is_symlink(self):
        return _io_path(self).is_symlink()

    def iterdir(self):
        for child in _io_path(self).iterdir():
            yield self / child.name

    def stat(self, *, follow_symlinks=True):
        return _io_path(self).stat(follow_symlinks=follow_symlinks)

    def lstat(self):
        return _io_path(self).lstat()

    def resolve(self, strict=False):
        return _StorePath(_plain_path(Path(_io_path(self)).resolve(strict=strict)))

    def read_text(self, encoding=None, errors=None):
        return _io_path(self).read_text(encoding=encoding, errors=errors)

    def read_bytes(self):
        return _io_path(self).read_bytes()

    def write_text(self, data, encoding=None, errors=None, newline=None):
        return _io_path(self).write_text(data, encoding=encoding, errors=errors, newline=newline)

    def write_bytes(self, data):
        return _io_path(self).write_bytes(data)

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        return _io_path(self).mkdir(mode=mode, parents=parents, exist_ok=exist_ok)

    def unlink(self, missing_ok=False):
        return _io_path(self).unlink(missing_ok=missing_ok)

    def rmdir(self):
        return _io_path(self).rmdir()

    def glob(self, pattern):
        for found in _io_path(self).glob(pattern):
            yield _StorePath(_plain_path(found))

    def rglob(self, pattern):
        for found in _io_path(self).rglob(pattern):
            yield _StorePath(_plain_path(found))

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        return _io_path(self).open(mode, buffering, encoding, errors, newline)

    def samefile(self, other_path):
        return _io_path(self).samefile(_io_path(Path(other_path)))


def _file_save_error(exc: OSError) -> StoreError:
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    text = str(exc).lower()
    if code == 206 or "too long" in text:
        return StoreError("Could not save that file. The path is too long for Windows.", 500)
    return StoreError(f"Could not save that file ({exc}).", 500)


def atomic_write_text(path: Path, text: str) -> None:
    target = _io_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp_name, target)
                return
            except OSError as exc:
                if not _replace_blocked(exc) or attempt + 1 >= _REPLACE_TRIES:
                    raise
                time.sleep(_REPLACE_DELAY)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path):
    """Read a JSON file. A Windows lock is retried, the same way a replace is."""
    for attempt in range(_REPLACE_TRIES):
        try:
            return json.loads(_io_path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StoreError("Not found.", 404) from exc
        except json.JSONDecodeError as exc:
            raise StoreError(f"Could not read {path.name}. It was not modified.", 500) from exc
        except OSError as exc:
            if not _replace_blocked(exc) or attempt + 1 >= _REPLACE_TRIES:
                raise
            time.sleep(_REPLACE_DELAY)


class Store:
    def __init__(self, root: Path):
        self.root = _StorePath(root).resolve()
        self._lock = threading.Lock()

    @property
    def bots_dir(self) -> Path:
        return self.root / "bots"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def endpoints_path(self) -> Path:
        return self.root / "endpoints.json"

    @property
    def computers_path(self) -> Path:
        return self.root / "computers.json"

    @property
    def direction_path(self) -> Path:
        return self.root / "DIRECTION.md"

    @property
    def rooms_dir(self) -> Path:
        return self.root / "rooms"

    @property
    def projects_dir(self) -> Path:
        return self.root / "projects"

    def ensure(self) -> None:
        self.bots_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        if not self.endpoints_path.exists():
            atomic_write_json(self.endpoints_path, [])
        if not self.computers_path.exists():
            atomic_write_json(self.computers_path, [])
        if not self.direction_path.exists():
            atomic_write_text(self.direction_path, DEFAULT_DIRECTION)
        self.rooms_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        from easyagent.secrets import migrate_store
        from easyagent.workspace import migrate_workspaces

        migrate_store(self)
        migrate_workspaces(self)

    def _parse_id(self, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if not ID_RE.fullmatch(cleaned):
            raise StoreError("Not found.", 404)
        return cleaned

    def _child(self, parent: Path, name: str) -> Path:
        """A direct child of `parent`. Symlinks and `..` are refused."""
        if name != Path(name).name or name in {".", ".."}:
            raise StoreError("Not found.", 404)
        path = parent / name
        if _io_path(path).is_symlink():
            raise StoreError("Refusing to follow a symlink.", 400)
        return path

    # --- endpoints ---------------------------------------------------------

    def list_endpoints(self) -> list[dict]:
        data = read_json(self.endpoints_path)
        if not isinstance(data, list):
            raise StoreError("endpoints.json is not a list. It was not modified.", 500)
        return data

    def get_endpoint(self, endpoint_id: str) -> dict | None:
        endpoint_id = self._parse_id(endpoint_id)
        for endpoint in self.list_endpoints():
            if endpoint.get("id") == endpoint_id:
                return self._hydrate_endpoint(endpoint)
        return None

    def _hydrate_endpoint(self, record: dict) -> dict:
        """A copy whose api_key is filled from the encrypted store. The file stays empty."""
        shown = dict(record)
        from easyagent.secrets import endpoint_account, get_secret

        saved = get_secret(endpoint_account(str(shown.get("id") or "")), self)
        if saved:
            shown["api_key"] = saved
            shown["has_api_key"] = True
        else:
            shown["api_key"] = ""
            shown["has_api_key"] = bool(shown.get("has_api_key"))
        return shown

    def _store_endpoint_key(self, endpoint_id: str, api_key: str | None, *, clear: bool = False) -> bool:
        from easyagent.secrets import delete_secret, endpoint_account, put_secret

        account = endpoint_account(endpoint_id)
        key = (api_key or "").strip()
        if clear or not key:
            delete_secret(account, self)
            return False
        put_secret(account, key, self)
        return True

    def add_endpoint(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None,
        model: str | None = None,
        max_parallel: int = 1,
        endpoint_id: str | None = None,
    ) -> dict:
        record = {
            "id": self._parse_id(endpoint_id) if endpoint_id else new_id(),
            "name": name,
            "base_url": base_url,
            "api_key": "",
            "has_api_key": False,
            "model": model,
            "max_parallel": clamp_parallel(max_parallel),
            "created_at": now_iso(),
        }
        has_key = self._store_endpoint_key(record["id"], api_key)
        record["has_api_key"] = has_key
        with self._lock:
            endpoints = self.list_endpoints()
            endpoints.append(record)
            atomic_write_json(self.endpoints_path, endpoints)
        shown = dict(record)
        shown["api_key"] = (api_key or "").strip()
        return shown

    def delete_endpoint(self, endpoint_id: str, confirm_name: str) -> None:
        """Remove one endpoint record. Bots and chats are not touched."""
        endpoint_id = self._parse_id(endpoint_id)
        with self._lock:
            endpoints = self.list_endpoints()
            match = next((item for item in endpoints if item.get("id") == endpoint_id), None)
            if match is None:
                raise StoreError("Endpoint not found.", 404)
            if not names_match(confirm_name, match.get("name") or ""):
                raise StoreError("Type the connection's name to remove it.", 400)
            kept = [item for item in endpoints if item.get("id") != endpoint_id]
            atomic_write_json(self.endpoints_path, kept)
            self._store_endpoint_key(endpoint_id, "", clear=True)

    def update_endpoint(
        self,
        endpoint_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_set: bool = False,
        clear_api_key: bool = False,
        model: str | None = None,
        model_set: bool = False,
        max_parallel: int | None = None,
        max_parallel_set: bool = False,
    ) -> dict:
        """Change one saved connection. Bot directories and chats are not opened."""
        endpoint_id = self._parse_id(endpoint_id)
        with self._lock:
            endpoints = self.list_endpoints()
            match = next((item for item in endpoints if item.get("id") == endpoint_id), None)
            if match is None:
                raise StoreError("Connection not found.", 404)
            if name is not None:
                match["name"] = name
            if base_url is not None:
                match["base_url"] = base_url
            if api_key_set and api_key:
                match["has_api_key"] = self._store_endpoint_key(endpoint_id, api_key)
                match["api_key"] = ""
            elif clear_api_key:
                self._store_endpoint_key(endpoint_id, "", clear=True)
                match["api_key"] = ""
                match["has_api_key"] = False
            if model_set:
                match["model"] = model
            if max_parallel_set and max_parallel is not None:
                match["max_parallel"] = clamp_parallel(max_parallel)
            atomic_write_json(self.endpoints_path, endpoints)
        return self._hydrate_endpoint(match)

    def endpoint_by_name(self, name: str) -> dict | None:
        """One saved connection, matched on its label. Nothing is written."""
        wanted = " ".join((name or "").split()).casefold()
        if not wanted:
            return None
        for endpoint in self.list_endpoints():
            if " ".join(str(endpoint.get("name") or "").split()).casefold() == wanted:
                return self._hydrate_endpoint(endpoint)
        return None

    # --- bots --------------------------------------------------------------

    def list_bots(self) -> list[dict]:
        bots = []
        if not self.bots_dir.exists():
            return bots
        for path in self.bots_dir.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            if not ID_RE.fullmatch(path.name):
                continue
            meta = path / "bot.json"
            if not meta.is_file():
                continue
            bots.append(read_json(meta))
        bots.sort(key=lambda bot: bot.get("created_at") or "")
        return bots

    def _bot_dir(self, bot_id: str) -> Path:
        bot_id = self._parse_id(bot_id)
        return self._child(self.bots_dir, bot_id)

    def get_bot(self, bot_id: str) -> dict:
        path = self._bot_dir(bot_id) / "bot.json"
        if not path.is_file():
            raise StoreError("Bot not found.", 404)
        bot = read_json(path)
        if bot.get("id") != self._parse_id(bot_id):
            raise StoreError("Bot not found.", 404)
        return bot

    def add_bot(
        self,
        *,
        name: str,
        endpoint_id: str,
        model: str | None,
        context_tokens: int | None = None,
    ) -> dict:
        endpoint_id = self._parse_id(endpoint_id)
        if self.get_endpoint(endpoint_id) is None:
            raise StoreError("Pick a saved connection.", 400)
        bot_id = new_id()
        record = {
            "id": bot_id,
            "name": name,
            "endpoint_id": endpoint_id,
            "model": model,
            "created_at": now_iso(),
        }
        if context_tokens is not None:
            record["context_tokens"] = context_tokens
        directory = self._bot_dir(bot_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "chats").mkdir()
        atomic_write_json(directory / "bot.json", record)
        self._seed_example_memory(bot_id)
        self._seed_example_skill()
        return record

    def update_bot(
        self,
        bot_id: str,
        *,
        name: str | None = None,
        endpoint_id: str | None = None,
        model: str | None = None,
        model_set: bool = False,
        context_tokens: int | None = None,
        context_tokens_set: bool = False,
        face_color: str | None = None,
        face_color_set: bool = False,
        check_enabled: bool | None = None,
        check_enabled_set: bool = False,
        learn_paused: bool | None = None,
        learn_paused_set: bool = False,
        learn_manual: bool | None = None,
        learn_manual_set: bool = False,
        check_revisions: int | None = None,
        check_revisions_set: bool = False,
        workspace: str | None = None,
        workspace_set: bool = False,
    ) -> dict:
        """Change settings. Chat files in this bot are not opened or rewritten."""
        bot = self.get_bot(bot_id)
        if name is not None:
            bot["name"] = name
        if endpoint_id is not None:
            endpoint_id = self._parse_id(endpoint_id)
            if self.get_endpoint(endpoint_id) is None:
                raise StoreError("Pick a saved connection.", 400)
            bot["endpoint_id"] = endpoint_id
        if model_set:
            bot["model"] = model
        if context_tokens_set:
            if context_tokens is None:
                bot.pop("context_tokens", None)
            else:
                bot["context_tokens"] = context_tokens
            # The old character budget is no longer read. Drop it when the budget is saved.
            bot.pop("context_chars", None)
        if face_color_set:
            if face_color:
                bot["face_color"] = face_color
            else:
                bot.pop("face_color", None)
        if check_enabled_set:
            if check_enabled is False:
                bot["check_enabled"] = False
            else:
                bot.pop("check_enabled", None)
        if learn_paused_set:
            if learn_paused:
                bot["learn_paused"] = True
            else:
                bot.pop("learn_paused", None)
        if learn_manual_set:
            if learn_manual:
                bot["learn_manual"] = True
            else:
                bot.pop("learn_manual", None)
        if check_revisions_set:
            if check_revisions is None:
                bot.pop("check_revisions", None)
            else:
                number = int(check_revisions)
                if number < 0:
                    number = 0
                if number > 4:
                    number = 4
                bot["check_revisions"] = number
        if workspace_set and workspace is not None:
            from easyagent.workspace import assign_workspace

            return assign_workspace(self, bot, workspace)
        atomic_write_json(self._bot_dir(bot["id"]) / "bot.json", bot)
        return bot

    def save_browser_settings(self, bot_id: str, *, headless: bool, allow: list[str], deny: list[str]) -> dict:
        """This bot's browser window and the sites it may open. Chats are not rewritten."""
        with self._lock:
            bot = self.get_bot(bot_id)
            bot["browser_headless"] = bool(headless)
            bot["browser_allow"] = list(allow)
            bot["browser_deny"] = list(deny)
            atomic_write_json(self._bot_dir(bot["id"]) / "bot.json", bot)
            return bot

    def delete_bot(self, bot_id: str, confirm_name: str) -> dict:
        """Delete exactly one bot directory after the name is confirmed.

        Endpoints, skills, direction, and every other bot stay where they are.
        """
        bot = self.get_bot(bot_id)
        if not names_match(confirm_name, bot.get("name") or ""):
            raise StoreError("Type the bot's name to remove it.", 400)
        directory = self._bot_dir(bot["id"])
        if directory.parent.resolve() != self.bots_dir.resolve():
            raise StoreError("Refusing to delete that path.", 400)
        if not directory.is_dir() or directory.is_symlink():
            raise StoreError("Bot not found.", 404)
        shutil.rmtree(_io_path(directory))
        self._forget_bot_projects(bot["id"])
        return bot

    # --- schedules and job log --------------------------------------------

    def _schedules_path(self, bot_id: str) -> Path:
        return self._bot_dir(bot_id) / "schedules.json"

    def _job_log_path(self, bot_id: str) -> Path:
        return self._bot_dir(bot_id) / "job-log.json"

    def list_schedules(self, bot_id: str) -> list[dict]:
        self.get_bot(bot_id)
        path = self._schedules_path(bot_id)
        if not path.is_file():
            return []
        data = read_json(path)
        if not isinstance(data, list):
            raise StoreError("schedules.json is not a list. It was not modified.", 500)
        return data

    def save_schedules(self, bot_id: str, schedules: list[dict]) -> list[dict]:
        self.get_bot(bot_id)
        with self._lock:
            atomic_write_json(self._schedules_path(bot_id), schedules)
        return schedules

    def add_schedule(self, bot_id: str, schedule: dict) -> dict:
        with self._lock:
            schedules = self.list_schedules(bot_id)
            schedules.append(schedule)
            atomic_write_json(self._schedules_path(bot_id), schedules)
        return schedule

    def _routine_trash_path(self, bot_id: str) -> Path:
        return self._bot_dir(bot_id) / "routines-trash.json"

    def list_routine_trash(self, bot_id: str) -> list[dict]:
        self.get_bot(bot_id)
        path = self._routine_trash_path(bot_id)
        if not path.is_file():
            return []
        data = read_json(path)
        if not isinstance(data, list):
            raise StoreError("routines-trash.json is not a list. It was not modified.", 500)
        return data

    def delete_schedule(self, bot_id: str, schedule_id: str) -> dict:
        """Move one routine to Trash. The bot, its chats, and the job log stay."""
        schedule_id = self._parse_id(schedule_id)
        with self._lock:
            schedules = self.list_schedules(bot_id)
            match = next((item for item in schedules if item.get("id") == schedule_id), None)
            if match is None:
                raise StoreError("Schedule not found.", 404)
            kept = [item for item in schedules if item.get("id") != schedule_id]
            trash_path = self._routine_trash_path(bot_id)
            trash = read_json(trash_path) if trash_path.is_file() else []
            if not isinstance(trash, list):
                trash = []
            item = dict(match)
            item["deleted_at"] = now_iso()
            trash.append(item)
            atomic_write_json(trash_path, trash[-100:])
            atomic_write_json(self._schedules_path(bot_id), kept)
        return match

    def restore_schedule(self, bot_id: str, schedule_id: str) -> dict:
        """Put a trashed routine back on the bot."""
        schedule_id = self._parse_id(schedule_id)
        with self._lock:
            trash_path = self._routine_trash_path(bot_id)
            trash = read_json(trash_path) if trash_path.is_file() else []
            if not isinstance(trash, list):
                trash = []
            match = next((item for item in trash if item.get("id") == schedule_id), None)
            if match is None:
                raise StoreError("That routine is not in Trash.", 404)
            rest = [item for item in trash if item.get("id") != schedule_id]
            atomic_write_json(trash_path, rest)
            schedules = self.list_schedules(bot_id)
            item = dict(match)
            item.pop("deleted_at", None)
            schedules.append(item)
            atomic_write_json(self._schedules_path(bot_id), schedules)
        return item

    def list_jobs(self, bot_id: str) -> list[dict]:
        self.get_bot(bot_id)
        path = self._job_log_path(bot_id)
        if not path.is_file():
            return []
        data = read_json(path)
        if not isinstance(data, list):
            raise StoreError("job-log.json is not a list. It was not modified.", 500)
        return data

    def append_job(self, bot_id: str, entry: dict) -> dict:
        with self._lock:
            jobs = self.list_jobs(bot_id)
            jobs.append(entry)
            atomic_write_json(self._job_log_path(bot_id), jobs)
        return entry

    # --- chats -------------------------------------------------------------

    def _chat_path(self, bot_id: str, chat_id: str) -> Path:
        chat_id = self._parse_id(chat_id)
        chats = self._bot_dir(bot_id) / "chats"
        return self._child(chats, f"{chat_id}.json")

    def list_chats(self, bot_id: str) -> list[dict]:
        self.get_bot(bot_id)
        chats_dir = self._bot_dir(bot_id) / "chats"
        items = []
        if not chats_dir.exists():
            return items
        for path in chats_dir.iterdir():
            if not path.is_file() or path.is_symlink() or path.suffix != ".json":
                continue
            if not ID_RE.fullmatch(path.stem):
                continue
            chat = read_json(path)
            items.append(
                {
                    "id": chat["id"],
                    "bot_id": chat["bot_id"],
                    "title": chat.get("title") or "New chat",
                    "created_at": chat.get("created_at"),
                    "updated_at": chat.get("updated_at"),
                    "message_count": len(chat.get("messages") or []),
                }
            )
        items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return items

    def create_chat(self, bot_id: str) -> dict:
        bot = self.get_bot(bot_id)
        chat = {
            "id": new_id(),
            "bot_id": bot["id"],
            "title": "New chat",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "summary": "",
            "summarized_through": 0,
            "messages": [],
        }
        atomic_write_json(self._chat_path(bot["id"], chat["id"]), chat)
        return chat

    def get_chat(self, bot_id: str, chat_id: str) -> dict:
        self.get_bot(bot_id)
        path = self._chat_path(bot_id, chat_id)
        if not path.is_file():
            raise StoreError("Chat not found.", 404)
        chat = read_json(path)
        if chat.get("bot_id") != self._parse_id(bot_id) or chat.get("id") != self._parse_id(chat_id):
            raise StoreError("Chat not found.", 404)
        chat.setdefault("messages", [])
        chat.setdefault("summary", "")
        chat.setdefault("summarized_through", 0)
        chat.setdefault("fresh_from", 0)
        chat.setdefault("rolling_summary", "")
        chat.setdefault("rolling_through", 0)
        return chat

    def toggle_reaction(self, bot_id: str, chat_id: str, message_id: str, emoji: str) -> dict:
        """One emoji on one message. The same emoji again clears it. Text stays."""
        chat = self.get_chat(bot_id, chat_id)
        _toggle_message_reaction(chat.get("messages") or [], message_id, emoji)
        chat["updated_at"] = now_iso()
        self.save_chat(chat)
        return self.get_chat(bot_id, chat_id)

    def toggle_room_reaction(self, room_id: str, message_id: str, emoji: str) -> dict:
        room = self.get_room(room_id)
        _toggle_message_reaction(room.get("messages") or [], message_id, emoji)
        room["updated_at"] = now_iso()
        self.save_room(room)
        return self.get_room(room_id)

    def set_reaction(self, bot_id: str, chat_id: str, message_id: str, emoji: str) -> dict:
        """The bot's tapback. It stays until the person changes it. Text stays."""
        chat = self.get_chat(bot_id, chat_id)
        _set_message_reaction(chat.get("messages") or [], message_id, emoji)
        chat["updated_at"] = now_iso()
        self.save_chat(chat)
        return self.get_chat(bot_id, chat_id)

    def set_room_reaction(self, room_id: str, message_id: str, emoji: str) -> dict:
        room = self.get_room(room_id)
        _set_message_reaction(room.get("messages") or [], message_id, emoji)
        room["updated_at"] = now_iso()
        self.save_room(room)
        return self.get_room(room_id)

    def save_chat(self, chat: dict) -> dict:
        """Write the full chat document. Callers must pass every stored message."""
        path = self._chat_path(chat["bot_id"], chat["id"])
        if not path.is_file():
            raise StoreError("Chat not found.", 404)
        current = read_json(path)
        disk = list(current.get("messages") or [])
        incoming = list(chat.get("messages") or [])
        disk_ids = {item.get("id") for item in disk if isinstance(item, dict) and item.get("id")}
        incoming_ids = {item.get("id") for item in incoming if isinstance(item, dict) and item.get("id")}
        missing = [item for item in disk if isinstance(item, dict) and item.get("id") and item.get("id") not in incoming_ids]
        added = [item for item in incoming if isinstance(item, dict) and item.get("id") and item.get("id") not in disk_ids]
        # A live chat save can hold a stale list while a routine appends. Keep both.
        if missing and added:
            merged = list(disk)
            have = set(disk_ids)
            for item in incoming:
                mid = item.get("id") if isinstance(item, dict) else None
                if mid and mid not in have:
                    merged.append(item)
                    have.add(mid)
            chat["messages"] = merged
        elif len(incoming) < len(disk):
            if missing and all(item.get("role") == "assistant" and item.get("live") for item in missing):
                atomic_write_json(path, chat)
                return chat
            raise StoreError("Refusing to shorten a stored transcript.", 400)
        atomic_write_json(path, chat)
        return chat

    def append_routine_message(self, bot_id: str, text: str, routine_id: str = "", routine_name: str = "") -> dict:
        """Append one finished routine reply. Re-reads the chat so a live save cannot drop it.

        Does not set the chat's run. A background routine must not look like a live turn.
        """
        chat = self.ongoing_chat(bot_id)
        with self._lock:
            chat = self.get_chat(bot_id, chat["id"])
            messages = list(chat.get("messages") or [])
            messages.append({
                "id": new_id(),
                "role": "assistant",
                "content": text,
                "created_at": now_iso(),
                "routine_id": routine_id,
                "routine_name": routine_name,
            })
            chat["messages"] = messages
            chat["updated_at"] = now_iso()
            atomic_write_json(self._chat_path(chat["bot_id"], chat["id"]), chat)
        return chat

    def read_host(self) -> dict:
        path = self.root / "host.json"
        blank = {"background": False, "start_at_login": False, "explained": False, "paused_all": False}
        if not path.is_file():
            return dict(blank)
        data = read_json(path)
        if not isinstance(data, dict):
            return dict(blank)
        return {key: bool(data.get(key)) for key in blank}

    def save_host(self, host: dict) -> dict:
        current = self.read_host()
        for key in ("background", "start_at_login", "explained", "paused_all"):
            if key in host:
                current[key] = bool(host[key])
        with self._lock:
            atomic_write_json(self.root / "host.json", current)
        return current

    def note_first_routine(self) -> dict:
        """The first saved routine turns background running and start-at-login on."""
        path = self.root / "host.json"
        if path.is_file():
            return self.read_host()
        return self.save_host({"background": True, "start_at_login": True, "explained": False, "paused_all": False})

    def existing_ongoing(self, bot_id: str) -> dict | None:
        """The pinned conversation, or the newest chat. Does not create one and does not delete any."""
        bot = self.get_bot(bot_id)
        pinned = str(bot.get("ongoing_chat_id") or "")
        if pinned:
            try:
                return self.get_chat(bot_id, pinned)
            except StoreError:
                pass
        listed = self.list_chats(bot_id)
        if not listed:
            return None
        return self.get_chat(bot_id, listed[0]["id"])

    def ongoing_chat(self, bot_id: str) -> dict:
        """The one conversation this bot opens into.

        A saved pin wins. Otherwise the newest chat becomes the ongoing one.
        When the bot has no chats, one empty chat is created. Other transcripts stay.
        """
        chat = self.existing_ongoing(bot_id)
        if chat is None:
            chat = self.create_chat(bot_id)
        bot = self.get_bot(bot_id)
        if bot.get("ongoing_chat_id") != chat["id"]:
            with self._lock:
                bot = self.get_bot(bot_id)
                bot["ongoing_chat_id"] = chat["id"]
                atomic_write_json(self._bot_dir(bot["id"]) / "bot.json", bot)
        return chat

    def start_fresh(self, bot_id: str, chat_id: str) -> dict:
        """Hide prior turns from the model. The stored messages stay on disk."""
        with self._lock:
            chat = self.get_chat(bot_id, chat_id)
            count = len(chat.get("messages") or [])
            chat["fresh_from"] = count
            chat["summary"] = ""
            chat["summarized_through"] = count
            chat["rolling_summary"] = ""
            chat["rolling_through"] = count
            chat["updated_at"] = now_iso()
            return self.save_chat(chat)

    def save_rolling_summary(self, bot_id: str, chat_id: str, summary: str, through: int) -> dict:
        """Replace the rolling summary only. The message list is read again so it cannot shrink."""
        with self._lock:
            chat = self.get_chat(bot_id, chat_id)
            chat["rolling_summary"] = summary
            chat["rolling_through"] = int(through)
            chat["updated_at"] = now_iso()
            return self.save_chat(chat)

    def drop_messages(self, bot_id: str, chat_id: str, message_ids: set[str]) -> dict:
        """Remove messages by id. Every other save still refuses to shorten a transcript.

        Only the nightly prune calls this, and only for ids it has just re-checked.
        """
        drop = {str(item) for item in message_ids if str(item)}
        with self._lock:
            chat = self.get_chat(bot_id, chat_id)
            messages = list(chat.get("messages") or [])
            if not drop:
                return chat
            summary_through = int(chat.get("summarized_through") or 0)
            fresh = int(chat.get("fresh_from") or 0)
            rolling = int(chat.get("rolling_through") or 0)
            kept = []
            dropped_summary = dropped_fresh = dropped_rolling = 0
            removed = 0
            for index, message in enumerate(messages):
                mid = str(message.get("id") or "")
                if mid and mid in drop:
                    removed += 1
                    if index < summary_through:
                        dropped_summary += 1
                    if index < fresh:
                        dropped_fresh += 1
                    if index < rolling:
                        dropped_rolling += 1
                    continue
                kept.append(message)
            if removed == 0:
                return chat
            chat["messages"] = kept
            chat["summarized_through"] = max(0, min(len(kept), summary_through - dropped_summary))
            chat["fresh_from"] = max(0, min(len(kept), fresh - dropped_fresh))
            chat["rolling_through"] = max(0, min(len(kept), rolling - dropped_rolling))
            chat["updated_at"] = now_iso()
            atomic_write_json(self._chat_path(chat["bot_id"], chat["id"]), chat)
            return chat

    def delete_chat(self, bot_id: str, chat_id: str) -> dict:
        """Remove one chat transcript and its files. The bot and every other chat stay."""
        chat = self.get_chat(bot_id, chat_id)
        run = chat.get("run") if isinstance(chat.get("run"), dict) else {}
        if run.get("status") == "running":
            raise StoreError("That chat is still running. Stop it before deleting it.", 409)
        bot_id = self._parse_id(bot_id)
        chat_id = self._parse_id(chat_id)
        chats = self._child(self._bot_dir(bot_id), "chats")
        path = self._child(chats, f"{chat_id}.json")
        files = self._child(chats, chat_id)
        if files.exists() and not files.is_dir():
            raise StoreError("Refusing to delete that path.", 400)
        if path.is_file():
            path.unlink()
        if files.is_dir():
            if files.parent.resolve() != chats.resolve():
                raise StoreError("Refusing to delete that path.", 400)
            shutil.rmtree(_io_path(files))
        return chat

    # --- rooms -------------------------------------------------------------

    def _room_path(self, room_id: str) -> Path:
        room_id = self._parse_id(room_id)
        return self._child(self.rooms_dir, f"{room_id}.json")

    def list_rooms(self) -> list[dict]:
        if not self.rooms_dir.exists():
            return []
        items = []
        for path in self.rooms_dir.iterdir():
            if not path.is_file() or path.is_symlink() or path.suffix != ".json":
                continue
            if not ID_RE.fullmatch(path.stem):
                continue
            room = read_json(path)
            items.append(
                {
                    "id": room["id"],
                    "name": room.get("name") or "Room",
                    "created_at": room.get("created_at"),
                    "updated_at": room.get("updated_at"),
                    "bot_ids": list(room.get("bot_ids") or []),
                    "message_count": len(room.get("messages") or []),
                }
            )
        items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return items

    def create_room(self, *, name: str) -> dict:
        room = {
            "id": new_id(),
            "name": name,
            "bot_ids": [],
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "summary": "",
            "summarized_through": 0,
            "messages": [],
        }
        self.rooms_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._room_path(room["id"]), room)
        return room

    def get_room(self, room_id: str) -> dict:
        path = self._room_path(room_id)
        if not path.is_file():
            raise StoreError("Room not found.", 404)
        room = read_json(path)
        if room.get("id") != self._parse_id(room_id):
            raise StoreError("Room not found.", 404)
        room.setdefault("bot_ids", [])
        room.setdefault("messages", [])
        room.setdefault("summary", "")
        room.setdefault("summarized_through", 0)
        return room

    def save_room(self, room: dict) -> dict:
        """Write the room. Messages already on disk cannot be dropped."""
        path = self._room_path(room["id"])
        if not path.is_file():
            raise StoreError("Room not found.", 404)
        current = read_json(path)
        if len(room.get("messages") or []) < len(current.get("messages") or []):
            raise StoreError("Refusing to shorten a stored transcript.", 400)
        atomic_write_json(path, room)
        return room

    def add_room_bot(self, room_id: str, bot_id: str) -> dict:
        """Membership only. Does not open the bot's chat files."""
        room = self.get_room(room_id)
        bot = self.get_bot(bot_id)
        if bot["id"] not in room["bot_ids"]:
            room["bot_ids"].append(bot["id"])
            room["updated_at"] = now_iso()
            self.save_room(room)
        return self.get_room(room["id"])

    def remove_room_bot(self, room_id: str, bot_id: str) -> dict:
        """Drop a member. The transcript and the bot directory stay."""
        room = self.get_room(room_id)
        bot_id = self._parse_id(bot_id)
        if bot_id not in (room.get("bot_ids") or []):
            raise StoreError("That bot is not in this room.", 404)
        room["bot_ids"] = [item for item in room["bot_ids"] if item != bot_id]
        room["updated_at"] = now_iso()
        self.save_room(room)
        return self.get_room(room["id"])

    # --- projects ----------------------------------------------------------

    def _project_dir(self, project_id: str) -> Path:
        project_id = self._parse_id(project_id)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        return self._child(self.projects_dir, project_id)

    def _clean_project_name(self, name: str) -> str:
        cleaned = " ".join((name or "").split())
        if not cleaned or len(cleaned) > 80:
            raise StoreError("Type a project name. Nothing was saved.", 400)
        return cleaned

    def list_project_records(self) -> list[dict]:
        folder = self.projects_dir
        if not folder.is_dir():
            return []
        items = []
        for child in sorted(folder.iterdir(), key=lambda path: path.name):
            if not child.is_dir() or child.is_symlink() or not ID_RE.fullmatch(child.name):
                continue
            path = child / "project.json"
            if not path.is_file() or path.is_symlink():
                continue
            data = read_json(path)
            if not isinstance(data, dict) or data.get("id") != child.name:
                continue
            data.setdefault("bot_ids", [])
            data.setdefault("kind", "group")
            items.append(data)
        return items

    def get_project(self, project_id: str) -> dict:
        project_id = self._parse_id(project_id)
        path = self._project_dir(project_id) / "project.json"
        if not path.is_file() or path.is_symlink():
            raise StoreError("That project is not there.", 404)
        data = read_json(path)
        if not isinstance(data, dict) or data.get("id") != project_id:
            raise StoreError("That project is not there.", 404)
        data.setdefault("bot_ids", [])
        data.setdefault("kind", "group")
        return data

    def projects_for_bot(self, bot_id: str) -> list[dict]:
        bot_id = self._parse_id(bot_id)
        found = []
        for project in self.list_project_records():
            if project.get("kind") == "bot" and project.get("bot_id") == bot_id:
                found.append(project)
            elif project.get("kind") == "group" and bot_id in (project.get("bot_ids") or []):
                found.append(project)
        return found

    def _name_taken(self, *, kind: str, name: str, bot_id: str | None = None, skip: str | None = None) -> bool:
        wanted = name.casefold()
        for project in self.list_project_records():
            if skip and project.get("id") == skip:
                continue
            if project.get("name", "").casefold() != wanted or project.get("kind") != kind:
                continue
            if kind == "bot" and project.get("bot_id") != bot_id:
                continue
            return True
        return False

    def create_bot_project(self, bot_id: str, name: str) -> dict:
        bot = self.get_bot(bot_id)
        cleaned = self._clean_project_name(name)
        with self._lock:
            if self._name_taken(kind="bot", name=cleaned, bot_id=bot["id"]):
                raise StoreError("This bot already has a project with that name.", 400)
            record = {
                "id": new_id(),
                "name": cleaned,
                "kind": "bot",
                "bot_id": bot["id"],
                "bot_ids": [],
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            directory = self._project_dir(record["id"])
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write_json(directory / "project.json", record)
        return record

    def create_group_project(self, name: str) -> dict:
        cleaned = self._clean_project_name(name)
        with self._lock:
            if self._name_taken(kind="group", name=cleaned):
                raise StoreError("A group project already has that name.", 400)
            record = {
                "id": new_id(),
                "name": cleaned,
                "kind": "group",
                "bot_id": None,
                "bot_ids": [],
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            directory = self._project_dir(record["id"])
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write_json(directory / "project.json", record)
        return record

    def add_project_bot(self, project_id: str, bot_id: str) -> dict:
        """Membership on a group project only. Rooms and chats are not opened."""
        bot = self.get_bot(bot_id)
        with self._lock:
            project = self.get_project(project_id)
            if project.get("kind") != "group":
                raise StoreError("That project belongs to one bot. Nothing was changed.", 400)
            if bot["id"] not in (project.get("bot_ids") or []):
                project["bot_ids"] = [*list(project.get("bot_ids") or []), bot["id"]]
                project["updated_at"] = now_iso()
                atomic_write_json(self._project_dir(project["id"]) / "project.json", project)
        return self.get_project(project_id)

    def remove_project_bot(self, project_id: str, bot_id: str) -> dict:
        """Drop a bot from a group project. Rooms and chats stay as they were."""
        bot_id = self._parse_id(bot_id)
        with self._lock:
            project = self.get_project(project_id)
            if project.get("kind") != "group":
                raise StoreError("That project belongs to one bot. Nothing was changed.", 400)
            if bot_id not in (project.get("bot_ids") or []):
                raise StoreError("That bot is not on this project.", 404)
            project["bot_ids"] = [item for item in project["bot_ids"] if item != bot_id]
            project["updated_at"] = now_iso()
            atomic_write_json(self._project_dir(project["id"]) / "project.json", project)
        return self.get_project(project_id)

    def delete_project(self, project_id: str, confirm_name: str) -> dict:
        project = self.get_project(project_id)
        if not names_match(confirm_name, project.get("name") or ""):
            raise StoreError("Type the project's name to remove it.", 400)
        self._delete_project_dir(project["id"])
        return project

    def _delete_project_dir(self, project_id: str) -> None:
        directory = self._project_dir(project_id)
        if directory.parent.resolve() != self.projects_dir.resolve():
            raise StoreError("Refusing to delete that path.", 400)
        if not directory.is_dir() or directory.is_symlink():
            raise StoreError("That project is not there.", 404)
        shutil.rmtree(_io_path(directory))

    def _forget_bot_projects(self, bot_id: str) -> None:
        """Drop one bot's own projects, and its name from group projects. Rooms stay."""
        bot_id = self._parse_id(bot_id)
        for project in self.list_project_records():
            if project.get("kind") == "bot" and project.get("bot_id") == bot_id:
                self._delete_project_dir(project["id"])
            elif bot_id in (project.get("bot_ids") or []):
                project["bot_ids"] = [item for item in project["bot_ids"] if item != bot_id]
                project["updated_at"] = now_iso()
                atomic_write_json(self._project_dir(project["id"]) / "project.json", project)

    def _project_files_dir(self, project_id: str) -> Path:
        self.get_project(project_id)
        return self._project_dir(project_id) / "files"

    def list_project_files(self, project_id: str) -> list[dict]:
        directory = self._project_dir(self._parse_id(project_id)) / "files"
        if not directory.is_dir():
            return []
        files = []
        for path in sorted(directory.glob("*.json")):
            if not path.is_file() or path.is_symlink():
                continue
            if not ID_RE.fullmatch(path.stem):
                continue
            data_path = directory / path.stem
            if not data_path.is_file() or data_path.is_symlink():
                continue
            meta = read_json(path)
            if not isinstance(meta, dict) or meta.get("id") != path.stem:
                continue
            files.append({"id": meta["id"], "name": meta.get("name") or "file", "size": int(meta.get("size") or 0)})
        files.sort(key=lambda item: item["name"].casefold())
        return files

    def save_project_file(self, project_id: str, *, name: str, data: bytes) -> dict:
        if not data:
            raise StoreError("That file was empty. It was not kept.", 400)
        if len(data) > 1_000_000:
            raise StoreError("That file is too large. It was not kept.", 400)
        cleaned = " ".join((name or "").split())
        if not cleaned or len(cleaned) > 80 or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
            raise StoreError("That file name was not kept.", 400)
        with self._lock:
            self.get_project(project_id)
            existing = self.list_project_files(project_id)
            if any(item["name"].casefold() == cleaned.casefold() for item in existing):
                raise StoreError("That name is already in this project.", 400)
            if len(existing) >= 40:
                raise StoreError("This project already has 40 files. Nothing was added.", 400)
            directory = self._project_files_dir(project_id)
            directory.mkdir(parents=True, exist_ok=True)
            file_id = new_id()
            (directory / file_id).write_bytes(data)
            meta = {"id": file_id, "name": cleaned, "size": len(data)}
            atomic_write_json(directory / f"{file_id}.json", meta)
            project = self.get_project(project_id)
            project["updated_at"] = now_iso()
            atomic_write_json(self._project_dir(project_id) / "project.json", project)
        return meta

    def delete_project_file(self, project_id: str, file_id: str) -> None:
        file_id = self._parse_id(file_id)
        with self._lock:
            directory = self._project_files_dir(project_id)
            meta_path = self._child(directory, f"{file_id}.json")
            data_path = self._child(directory, file_id)
            if not meta_path.is_file() or not data_path.is_file():
                raise StoreError("That file is not in this project.", 404)
            meta_path.unlink()
            data_path.unlink()
            project = self.get_project(project_id)
            project["updated_at"] = now_iso()
            atomic_write_json(self._project_dir(project_id) / "project.json", project)

    def read_project_bytes(self, project_id: str, filename: str) -> tuple[dict, bytes]:
        wanted = " ".join((filename or "").split()).casefold()
        if not wanted:
            raise StoreError("Name the file.", 400)
        for meta in self.list_project_files(project_id):
            if meta["name"].casefold() != wanted:
                continue
            directory = self._project_dir(project_id) / "files"
            data_path = self._child(directory, meta["id"])
            return meta, data_path.read_bytes()
        raise StoreError("That file is not in this project.", 404)

    # --- computers ---------------------------------------------------------

    def list_computers(self) -> list[dict]:
        if not self.computers_path.exists():
            return []
        data = read_json(self.computers_path)
        if not isinstance(data, list):
            raise StoreError("computers.json is not a list. It was not modified.", 500)
        changed = False
        cleaned = []
        for item in data:
            if not isinstance(item, dict):
                cleaned.append(item)
                continue
            sealed, did = _seal_legacy_computer(item)
            changed = changed or did
            cleaned.append(sealed)
        if changed:
            atomic_write_json(self.computers_path, cleaned)
        return cleaned

    def add_computer(
        self,
        *,
        name: str,
        kind: str,
        host: str,
        port: int | None,
        vault: dict,
    ) -> dict:
        record = {
            "id": new_id(),
            "name": name,
            "kind": kind,
            "host": host,
            "port": port,
            "vault": vault,
            "created_at": now_iso(),
        }
        with self._lock:
            computers = self.list_computers()
            if any(item.get("name") == name for item in computers):
                raise StoreError("A computer with that name is already saved. Chats were not changed.", 400)
            computers.append(record)
            atomic_write_json(self.computers_path, computers)
        return record

    def update_computer(
        self,
        computer_id: str,
        *,
        name: str,
        kind: str,
        host: str,
        port: int | None,
        vault: dict | None,
    ) -> dict:
        """Change one saved computer. Bot directories and chats are not opened."""
        computer_id = self._parse_id(computer_id)
        with self._lock:
            computers = self.list_computers()
            match = next((item for item in computers if item.get("id") == computer_id), None)
            if match is None:
                raise StoreError("Computer not found.", 404)
            if any(item.get("name") == name and item.get("id") != computer_id for item in computers):
                raise StoreError("A computer with that name is already saved. Chats were not changed.", 400)
            match["name"] = name
            match["kind"] = kind
            match["host"] = host
            match["port"] = port
            match.pop("user", None)
            match.pop("secret", None)
            match.pop("auth", None)
            if vault is not None:
                match["vault"] = vault
            atomic_write_json(self.computers_path, computers)
        return match

    def delete_computer(self, computer_id: str, confirm_name: str) -> None:
        computer_id = self._parse_id(computer_id)
        with self._lock:
            computers = self.list_computers()
            match = next((item for item in computers if item.get("id") == computer_id), None)
            if match is None:
                raise StoreError("Computer not found.", 404)
            if not names_match(confirm_name, match.get("name") or ""):
                raise StoreError("Type the computer's name to remove it.", 400)
            kept = [item for item in computers if item.get("id") != computer_id]
            atomic_write_json(self.computers_path, kept)

    # --- memory ------------------------------------------------------------
    # One index file points at topic files. The lines live in the topic files.

    def _memory_dir(self, bot_id: str) -> Path:
        return self._bot_dir(bot_id) / "memory"

    def _memory_index_path(self, bot_id: str) -> Path:
        return self._memory_dir(bot_id) / "index.txt"

    def _legacy_memory_path(self, bot_id: str) -> Path:
        return self._bot_dir(bot_id) / "memory.json"

    def _topic_file(self, bot_id: str, slug: str) -> Path:
        return self._child(self._memory_dir(bot_id), f"{slug}.txt")

    def _migrate_memory(self, bot_id: str) -> None:
        """Move a flat memory.json into one topic file. Does not open a chat."""
        self.get_bot(bot_id)
        legacy = self._legacy_memory_path(bot_id)
        if not legacy.is_file():
            return
        data = read_json(legacy)
        if not isinstance(data, list):
            raise StoreError("memory.json is not a list. It was not modified.", 500)
        folder = self._memory_dir(bot_id)
        folder.mkdir(parents=True, exist_ok=True)
        if data:
            title, lines = self._read_topic_file(bot_id, "saved") if "saved" in self._read_index_slugs(bot_id) else ("Saved", [])
            seen = {item["id"] for item in lines}
            for item in data:
                if not isinstance(item, dict):
                    continue
                text = " ".join(str(item.get("text") or "").split())
                if not text:
                    continue
                line_id = str(item.get("id") or "")
                if not ID_RE.fullmatch(line_id):
                    line_id = new_id()
                if line_id in seen:
                    continue
                record = {"id": line_id, "text": text[:500]}
                if item.get("created_at"):
                    record["created_at"] = str(item["created_at"])
                if item.get("updated_at"):
                    record["updated_at"] = str(item["updated_at"])
                lines.append(record)
                seen.add(line_id)
            self._write_topic_file(bot_id, "saved", title or "Saved", lines)
            slugs = self._read_index_slugs(bot_id)
            if "saved" not in slugs:
                slugs.append("saved")
                self._write_index(bot_id, slugs)
        legacy.unlink()

    def _read_index_slugs(self, bot_id: str) -> list[str]:
        path = self._memory_index_path(bot_id)
        if not path.is_file():
            return []
        slugs: list[str] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            slug = raw.strip()
            if not slug:
                continue
            if not _TOPIC_RE.fullmatch(slug):
                raise StoreError("The memory index is not a list of topic files. It was not modified.", 500)
            if slug not in slugs:
                slugs.append(slug)
        return slugs

    def _write_index(self, bot_id: str, slugs: list[str]) -> None:
        path = self._memory_index_path(bot_id)
        body = ("\n".join(slugs) + "\n") if slugs else ""
        atomic_write_text(path, body)

    def _read_topic_file(self, bot_id: str, slug: str) -> tuple[str, list[dict]]:
        path = self._topic_file(bot_id, slug)
        title = _topic_title(slug)
        if not path.is_file():
            return title, []
        lines: list[dict] = []
        current: dict | None = None
        seen_heading = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            if raw.startswith("#") and not seen_heading and not lines and current is None:
                seen_heading = True
                heading = raw.lstrip("#").strip()
                if heading:
                    title = heading
                continue
            if raw.startswith("- "):
                if current is not None:
                    lines.append(current)
                current = {"text": raw[2:].strip()}
                continue
            stripped = raw.strip()
            if current is None:
                raise StoreError(f"{slug}.txt has a line that is not a memory. It was not modified.", 500)
            if stripped.startswith("id: "):
                current["id"] = stripped[4:].strip()
            elif stripped.startswith("also: "):
                current["also"] = stripped[6:].strip()
            elif stripped.startswith("created: "):
                current["created_at"] = stripped[9:].strip()
            elif stripped.startswith("updated: "):
                current["updated_at"] = stripped[9:].strip()
            else:
                raise StoreError(f"{slug}.txt has a line that is not a memory. It was not modified.", 500)
        if current is not None:
            lines.append(current)
        for item in lines:
            if not item.get("text") or not ID_RE.fullmatch(str(item.get("id") or "")):
                raise StoreError(f"{slug}.txt has a line that is not a memory. It was not modified.", 500)
        return title, lines

    def _write_topic_file(self, bot_id: str, slug: str, title: str, lines: list[dict]) -> None:
        parts = [f"# {title}", ""]
        for item in lines:
            parts.append(f"- {item['text']}")
            parts.append(f"  id: {item['id']}")
            if item.get("created_at"):
                parts.append(f"  created: {item['created_at']}")
            if item.get("updated_at"):
                parts.append(f"  updated: {item['updated_at']}")
            if item.get("also"):
                parts.append(f"  also: {item['also']}")
            parts.append("")
        self._memory_dir(bot_id).mkdir(parents=True, exist_ok=True)
        path = self._topic_file(bot_id, slug)
        previous = path.read_text(encoding="utf-8") if path.is_file() else ""
        atomic_write_text(path, "\n".join(parts).rstrip() + "\n")
        self._note_ledger("memory", f"{bot_id}/{slug}", previous, bot_id)

    def _ensure_pointer(self, bot_id: str, slug: str) -> None:
        slugs = self._read_index_slugs(bot_id)
        if slug in slugs:
            return
        if len(slugs) >= TOPIC_CAP:
            raise StoreError("The index is full. An older topic was not changed.", 400)
        slugs.append(slug)
        self._write_index(bot_id, slugs)

    def _public_memory(self, item: dict, topic: str) -> dict:
        record = {"id": item["id"], "text": item["text"], "topic": topic}
        if item.get("created_at"):
            record["created_at"] = item["created_at"]
        if item.get("updated_at"):
            record["updated_at"] = item["updated_at"]
        if item.get("also"):
            record["also"] = item["also"]
        return record

    def _each_line(self, bot_id: str) -> list[tuple[str, dict]]:
        found: list[tuple[str, dict]] = []
        for slug in self._read_index_slugs(bot_id):
            _title, lines = self._read_topic_file(bot_id, slug)
            for item in lines:
                found.append((slug, item))
        return found

    def memory_slugs(self, bot_id: str) -> list[str]:
        """Topic file names only. The lines stay in the topic files."""
        self._migrate_memory(bot_id)
        return self._read_index_slugs(bot_id)

    def memory_index(self, bot_id: str) -> list[dict]:
        self._migrate_memory(bot_id)
        found = []
        for slug in self._read_index_slugs(bot_id):
            title, lines = self._read_topic_file(bot_id, slug)
            found.append({"name": slug, "title": title, "count": len(lines)})
        return found

    def read_topic(self, bot_id: str, topic: str) -> dict:
        self._migrate_memory(bot_id)
        slug = _topic_slug(topic)
        if slug not in self._read_index_slugs(bot_id):
            raise StoreError("That topic file is not in the index.", 404)
        title, lines = self._read_topic_file(bot_id, slug)
        return {
            "name": slug,
            "title": title,
            "lines": [self._public_memory(item, slug) for item in lines],
        }

    def topic_text(self, bot_id: str, topic: str) -> str:
        """One topic file for the model. Other topic files are not opened."""
        opened = self.read_topic(bot_id, topic)
        rows = []
        for item in opened["lines"]:
            line = f"- {item['text']}"
            if item.get("also"):
                line += f" | also: {item['also']}"
            rows.append(line)
        body = "\n".join(rows) if rows else "(empty)"
        return f"{opened['name']}\n{body}"

    def list_memory(self, bot_id: str) -> list[dict]:
        self._migrate_memory(bot_id)
        return [self._public_memory(item, slug) for slug, item in self._each_line(bot_id)]

    def _choose_topic(self, bot_id: str, text: str, topic: str | None, *, create: bool, fresh: bool) -> str:
        index = self._read_index_slugs(bot_id)
        if topic:
            slug = _topic_slug(topic)
            if slug in index:
                if fresh:
                    raise StoreError("That topic file already exists. File the line there.", 400)
                return slug
            if not create:
                raise StoreError(
                    f"No topic file named {slug}. Make one if this line belongs in a new subject.",
                    400,
                )
            if len(index) >= TOPIC_CAP:
                raise StoreError("The index is full. An older topic was not changed.", 400)
            return slug
        matches = [slug for slug in index if _topic_matches(slug, text)]
        if len(matches) == 1:
            _title, lines = self._read_topic_file(bot_id, matches[0])
            if len(lines) < TOPIC_LINE_CAP:
                return matches[0]
        slug = _topic_from_fact(text)
        number = 2
        while slug in index:
            _title, lines = self._read_topic_file(bot_id, slug)
            if slug in matches and len(lines) < TOPIC_LINE_CAP:
                return slug
            suffix = f"-{number}"
            slug = slug[: 40 - len(suffix)].rstrip("-") + suffix
            number += 1
            if number > 9:
                raise StoreError("That topic file is full. Make a new topic. This line was not added.", 400)
        if len(index) >= TOPIC_CAP:
            raise StoreError("The index is full. An older topic was not changed.", 400)
        return slug

    def add_memory(
        self,
        bot_id: str,
        text: str,
        topic: str | None = None,
        *,
        create: bool = False,
        fresh: bool = False,
        also: str | None = None,
    ) -> dict:
        """File one line into the topic it belongs with. An identical line is left as it is."""
        cleaned = " ".join((text or "").split())
        if not cleaned or len(cleaned) > 500:
            raise StoreError("Type the fact. Nothing was changed.", 400)
        with self._lock:
            self._migrate_memory(bot_id)
            for slug, item in self._each_line(bot_id):
                if item.get("text") == cleaned:
                    return self._public_memory(item, slug)
            slug = self._choose_topic(bot_id, cleaned, topic, create=create, fresh=fresh)
            existed = slug in self._read_index_slugs(bot_id) or self._topic_file(bot_id, slug).is_file()
            title, lines = self._read_topic_file(bot_id, slug)
            if topic and not existed:
                title = _topic_heading(topic, slug)
            if len(lines) >= TOPIC_LINE_CAP:
                raise StoreError("That topic file is full. Make a new topic. This line was not added.", 400)
            record = {"id": new_id(), "text": cleaned, "created_at": now_iso()}
            if also:
                other = _topic_slug(also)
                if other == slug:
                    raise StoreError("A line already lives in that topic.", 400)
                if other not in self._read_index_slugs(bot_id):
                    raise StoreError("That other topic file is not in the index.", 400)
                record["also"] = other
            lines.append(record)
            self._write_topic_file(bot_id, slug, title, lines)
            self._ensure_pointer(bot_id, slug)
            return self._public_memory(record, slug)

    def _find_memory(self, bot_id: str, memory_id: str) -> tuple[str, list[dict], dict]:
        for slug in self._read_index_slugs(bot_id):
            title, lines = self._read_topic_file(bot_id, slug)
            match = next((item for item in lines if item.get("id") == memory_id), None)
            if match is not None:
                return slug, lines, match
        raise StoreError("That memory line is not there.", 404)

    def update_memory(self, bot_id: str, memory_id: str, text: str) -> dict:
        """Change one line. Every other line stays."""
        memory_id = self._parse_id(memory_id)
        cleaned = " ".join((text or "").split())
        if not cleaned or len(cleaned) > 500:
            raise StoreError("Type the fact. Nothing was changed.", 400)
        with self._lock:
            self._migrate_memory(bot_id)
            slug, lines, match = self._find_memory(bot_id, memory_id)
            others = [
                (item.get("id"), item.get("text"), item.get("also"))
                for item in lines
                if item.get("id") != memory_id
            ]
            match["text"] = cleaned
            match["updated_at"] = now_iso()
            others_after = [
                (item.get("id"), item.get("text"), item.get("also"))
                for item in lines
                if item.get("id") != memory_id
            ]
            if others_after != others:
                raise StoreError("Another memory line would have changed. Nothing was saved.", 500)
            title, _current = self._read_topic_file(bot_id, slug)
            self._write_topic_file(bot_id, slug, title, lines)
            return self._public_memory(match, slug)

    def delete_memory(self, bot_id: str, memory_id: str) -> None:
        memory_id = self._parse_id(memory_id)
        with self._lock:
            self._migrate_memory(bot_id)
            slug, lines, _match = self._find_memory(bot_id, memory_id)
            kept = [item for item in lines if item.get("id") != memory_id]
            title, _current = self._read_topic_file(bot_id, slug)
            self._write_topic_file(bot_id, slug, title, kept)

    def move_memory(self, bot_id: str, key: str, source: str, dest: str) -> dict:
        """Move one line from one topic file to another. The other lines stay."""
        src = _topic_slug(source)
        dst = _topic_slug(dest)
        if src == dst:
            raise StoreError("That line is already in that topic.", 400)
        with self._lock:
            self._migrate_memory(bot_id)
            index = self._read_index_slugs(bot_id)
            if src not in index:
                raise StoreError("That topic file is not in the index.", 404)
            src_title, src_lines = self._read_topic_file(bot_id, src)
            match = _line_by_key(src_lines, key)
            if match is None:
                raise StoreError("That memory line is not there.", 404)
            if dst not in index and len(index) >= TOPIC_CAP:
                raise StoreError("The index is full. An older topic was not changed.", 400)
            dst_title, dst_lines = self._read_topic_file(bot_id, dst)
            if any(item.get("text") == match.get("text") for item in dst_lines):
                raise StoreError("That line is already in that topic.", 400)
            if len(dst_lines) >= TOPIC_LINE_CAP:
                raise StoreError("That topic file is full. Make a new topic. This line was not added.", 400)
            src_lines = [item for item in src_lines if item.get("id") != match.get("id")]
            dst_lines.append(match)
            self._write_topic_file(bot_id, dst, dst_title, dst_lines)
            self._write_topic_file(bot_id, src, src_title, src_lines)
            self._ensure_pointer(bot_id, dst)
            return self._public_memory(match, dst)

    def point_memory(self, bot_id: str, key: str, other: str) -> dict:
        """Point one line at another topic file. The line stays where it is."""
        other_slug = _topic_slug(other)
        with self._lock:
            self._migrate_memory(bot_id)
            index = self._read_index_slugs(bot_id)
            if other_slug not in index:
                raise StoreError("That other topic file is not in the index.", 400)
            found: tuple[str, dict] | None = None
            for slug, item in self._each_line(bot_id):
                if _same_line(item, key):
                    if found is not None:
                        raise StoreError("More than one line says that.", 400)
                    found = (slug, item)
            if found is None:
                raise StoreError("That memory line is not there.", 404)
            slug, match = found
            if slug == other_slug:
                raise StoreError("A line already lives in that topic.", 400)
            title, lines = self._read_topic_file(bot_id, slug)
            target = next(item for item in lines if item.get("id") == match.get("id"))
            target["also"] = other_slug
            self._write_topic_file(bot_id, slug, title, lines)
            return self._public_memory(target, slug)

    # --- chat files --------------------------------------------------------

    def _chat_files_dir(self, bot_id: str, chat_id: str) -> Path:
        chat_id = self._parse_id(chat_id)
        self.get_chat(bot_id, chat_id)
        return self._bot_dir(bot_id) / "chats" / chat_id / "files"

    def save_chat_file(
        self,
        bot_id: str,
        chat_id: str,
        *,
        name: str,
        media_type: str,
        data: bytes,
        source: str | None = None,
    ) -> dict:
        if not data:
            raise StoreError("That file was empty. It was not kept.", 400)
        if len(data) > 1_000_000:
            raise StoreError("That file is too large. It was not kept.", 400)
        directory = self._chat_files_dir(bot_id, chat_id)
        file_id = new_id()
        target = directory / file_id
        meta = {"id": file_id, "name": name, "media_type": media_type, "size": len(data)}
        if source:
            meta["source"] = source
        try:
            _io_path(directory).mkdir(parents=True, exist_ok=True)
            _io_path(target).write_bytes(data)
            atomic_write_json(directory / f"{file_id}.json", meta)
        except OSError as exc:
            raise _file_save_error(exc) from exc
        return meta

    def read_chat_file(self, bot_id: str, chat_id: str, file_id: str) -> tuple[dict, bytes]:
        file_id = self._parse_id(file_id)
        directory = self._chat_files_dir(bot_id, chat_id)
        meta_path = self._child(directory, f"{file_id}.json")
        data_path = self._child(directory, file_id)
        if not _io_path(meta_path).is_file() or not _io_path(data_path).is_file():
            raise StoreError("That file is not in this chat.", 404)
        meta = read_json(meta_path)
        if not isinstance(meta, dict) or meta.get("id") != file_id:
            raise StoreError("That file is not in this chat.", 404)
        source = meta.get("source")
        if isinstance(source, str) and source:
            src = Path(source)
            try:
                resolved = src.resolve()
                root = self.root.resolve()
                outside = resolved != root and root not in resolved.parents
                opened = _io_path(src)
                if outside and opened.is_file() and 0 < opened.stat().st_size <= 1_000_000:
                    return meta, opened.read_bytes()
            except OSError:
                pass
        return meta, _io_path(data_path).read_bytes()

    # --- watches and proposals ---------------------------------------------

    def _watches_path(self) -> Path:
        return self.root / "watches.json"

    def list_watches(self) -> list[dict]:
        path = self._watches_path()
        if not path.is_file():
            return []
        data = read_json(path)
        if not isinstance(data, list):
            raise StoreError("watches.json is not a list. It was not modified.", 500)
        return data

    def arm_watch(self, kind: str) -> dict:
        if kind not in {"message", "job_failed"}:
            raise StoreError("Choose a new message or a failed job.", 400)
        with self._lock:
            watches = self.list_watches()
            for item in watches:
                if item.get("kind") == kind and item.get("armed"):
                    return item
            record = {"id": new_id(), "kind": kind, "armed": True, "fired_at": None}
            watches.append(record)
            atomic_write_json(self._watches_path(), watches)
            return record

    def claim_watch(self, kind: str) -> dict | None:
        """Mark one armed watch as fired. A second claim finds nothing."""
        with self._lock:
            watches = self.list_watches()
            for item in watches:
                if item.get("kind") == kind and item.get("armed"):
                    item["armed"] = False
                    item["fired_at"] = now_iso()
                    atomic_write_json(self._watches_path(), watches)
                    return item
        return None

    def _proposals_path(self) -> Path:
        return self.root / "proposals.json"

    def list_proposals(self) -> list[dict]:
        path = self._proposals_path()
        if not path.is_file():
            return []
        data = read_json(path)
        if not isinstance(data, list):
            raise StoreError("proposals.json is not a list. It was not modified.", 500)
        return data

    def add_proposal(self, proposal: dict) -> dict:
        """Store an uninstalled proposal. Skills and memory are not opened."""
        record = dict(proposal)
        record["installed"] = False
        with self._lock:
            proposals = self.list_proposals()
            proposals.append(record)
            atomic_write_json(self._proposals_path(), proposals)
        return record

    # --- skills and direction ----------------------------------------------

    def _seed_example_memory(self, bot_id: str) -> None:
        """One example line so a new bot shows the memory shape. It is not a user fact."""
        self.add_memory(bot_id, EXAMPLE_MEMORY, topic="examples", create=True)

    def _seed_example_skill(self) -> None:
        """One example skill the first time the skills folder is empty."""
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        if any(path.is_file() and not path.is_symlink() for path in self.skills_dir.glob("*.md")):
            return
        self.save_skill(EXAMPLE_SKILL)

    def read_direction(self) -> str:
        if not self.direction_path.is_file():
            return DEFAULT_DIRECTION
        return self.direction_path.read_text(encoding="utf-8")

    def write_direction(self, text: str) -> str:
        if not text.strip():
            raise StoreError("Direction cannot be empty. Chats were not changed.", 400)
        atomic_write_text(self.direction_path, text if text.endswith("\n") else text + "\n")
        return self.read_direction()

    def list_skills(self) -> list[dict]:
        skills = []
        if not self.skills_dir.exists():
            return skills
        files = [path for path in self.skills_dir.glob("*.md") if path.is_file() and not path.is_symlink()]
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files:
            if slugify(path.stem) != path.stem:
                continue
            parsed = parse_skill_document(path.read_text(encoding="utf-8"))
            if not parsed:
                continue
            parsed["name"] = path.stem
            skills.append(parsed)
        return skills

    def save_skill(self, skill: dict) -> dict:
        slug = slugify(skill.get("name") or "")
        if not slug:
            raise StoreError("Skill name must be a short kebab-case slug.", 400)
        document = {
            "name": slug,
            "description": skill.get("description") or "",
            "body": skill.get("body") or "",
        }
        path = self._child(self.skills_dir, f"{slug}.md")
        if path.parent.resolve() != self.skills_dir.resolve():
            raise StoreError("Bad skill name.", 400)
        previous = path.read_text(encoding="utf-8") if path.is_file() else ""
        atomic_write_text(path, render_skill(document))
        self._note_ledger("skill", slug, previous, "")
        return document

    def _note_ledger(self, kind: str, key: str, previous: str, bot_id: str) -> None:
        """A content-addressed copy of the previous text. Learning can roll it back."""
        try:
            from easyagent.learn import note_ledger

            note_ledger(self, kind=kind, key=key, previous=previous, bot_id=bot_id)
        except Exception:
            return
