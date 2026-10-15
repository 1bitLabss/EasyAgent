"""Chat-scoped approval by action class, not by one path or one argument.

A person can allow a class from the card, for this chat only. The grant expires,
and it can be revoked. Every later command is parsed and its paths are resolved
again. A symlink, a .. segment, an environment variable, a UNC path, or a
wildcard that leaves this bot's folders does not ride along.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from easyagent.yolo import DEFAULT_HOURS, DEFAULT_IDLE_MINUTES

READ = "read-in-workspace"
WRITE = "write-in-workspace"
DELETE = "delete-in-workspace"
MOVE = "move-in-workspace"
RUN = "run-listed-program"

_LABELS = {
    READ: "Allow all: read files in this bot's folder for this chat",
    WRITE: "Allow all: write files in this bot's folder for this chat",
    DELETE: "Allow all: delete files in this bot's folder for this chat",
    MOVE: "Allow all: move files in this bot's folder for this chat",
}

# These asks are one command at a time. A class grant never covers them.
_CLOSED = frozenset({
    "read-secret",
    "write-secret",
    "write-outside",
    "persistence",
    "download-exec",
    "remote-script",
    "remote-change",
    "install",
    "admin",
    "send-or-pay",
    "outbound",
    "git-destructive",
    "injection",
    "data-unresolved",
    "data-dir",
    "own-server",
    "bot-isolation",
    "credential-dump",
    "firewall-off",
    "fork-bomb",
    "disk-format",
    "root-delete",
    "browser-store",
    "guardrail-edit",
    "script-body",
    "exec-flag",
    "inline-code",
    "wrapped-command",
    "mcp-ask",
    "mcp-install",
    "bridge-send",
    "contain-setup",
    "already-denied",
    "unreviewed",
})

_RUN_RULES = frozenset({"bot-changed", "git-hooks", "unlisted-command", "dynamic-command"})
_RUN_HEADS = frozenset({"pytest", "unittest", "make", "cargo", "go", "npm", "yarn", "pnpm"})
_HARD_SECONDS = DEFAULT_HOURS * 60 * 60

_OBFUSCATED = re.compile(
    r"(?i)(\$\(|`|@'|@\"|"
    r"\b(iex|invoke-expression|start-process|set-alias|new-alias|sal|nal)\b|"
    r"\b(powershell|pwsh|cmd)(?:\.exe)?\b[^\n]*\s-(?:command|c|encodedcommand|enc|ec|e)\b|"
    r"\s-(?:encodedcommand|enc)\s|"
    r"(?:^|[;&|])\s*&\s*[{$])"
)
_FETCH = re.compile(
    r"(?i)\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|start-bitstransfer|"
    r"certutil|bitsadmin|mshta|msiexec)\b"
)
_REDIRECT = re.compile(r"(?i)(?<![0-9&])>{1,2}")


@dataclass(frozen=True)
class ActionClass:
    name: str
    tool: str
    label: str


def match(store, bot_id: str, request, verdict) -> ActionClass | None:
    """The class this ask belongs to, after this command's paths are resolved.

    None means the card stays about this one command. A path outside the bot's
    folders, a secret, or a command we cannot prove is not a class.
    """
    if getattr(verdict, "tier", "") != "ask":
        return None
    rule = str(getattr(verdict, "rule", "") or "")
    if rule in _CLOSED:
        return None
    kind = getattr(request, "kind", "")
    if kind == "files":
        return _files(store, bot_id, request, rule)
    if kind != "shell":
        return None
    command = getattr(request, "command", "") or ""
    if _FETCH.search(command):
        return None
    read = _read(store, bot_id, command)
    if read is not None:
        return read
    if _OBFUSCATED.search(command):
        return None
    listed = _listed(store, bot_id, command, rule)
    if listed is not None:
        return listed
    if rule in {"overwrite", "redirect-overwrite"}:
        return _mutation(store, bot_id, request, verdict, WRITE, redirect_ok=True)
    if rule == "delete":
        return _mutation(store, bot_id, request, verdict, DELETE, redirect_ok=False)
    if rule == "move":
        return _mutation(store, bot_id, request, verdict, MOVE, redirect_ok=False)
    return None


def covers(store, bot_id: str, request, verdict, *, now: datetime | None = None) -> bool:
    """True when this chat already allowed this class and this command still fits it."""
    from easyagent.helpers import in_helper
    from easyagent.safety import is_unattended
    from easyagent import turn as turn_mod

    if is_unattended() or in_helper():
        return False
    slot = turn_mod.current_slot()
    if slot is None or slot.bot_id != bot_id or not slot.chat_id:
        return False
    found = match(store, bot_id, request, verdict)
    if found is None:
        return False
    from easyagent.yolo import targets_confined

    try:
        if not targets_confined(store, bot_id, request):
            return False
    except Exception:
        return False
    try:
        chat = store.get_chat(bot_id, slot.chat_id)
    except Exception:
        return False
    return _live(store, bot_id, chat, found, now or _now()) is not None


def remember(store, bot_id: str, request, verdict) -> ActionClass | None:
    """Store the class for this chat. The command must still match, or nothing is stored."""
    from easyagent.helpers import in_helper
    from easyagent.safety import _audit, is_unattended
    from easyagent import turn as turn_mod
    from easyagent.store import now_iso
    from easyagent.yolo import public_view

    if is_unattended() or in_helper():
        return None
    found = match(store, bot_id, request, verdict)
    if found is None:
        return None
    slot = turn_mod.current_slot()
    if slot is None or slot.bot_id != bot_id or not slot.chat_id:
        return None
    try:
        chat = store.get_chat(bot_id, slot.chat_id)
    except Exception:
        return None
    moment = _now()
    row = {
        "class": found.name,
        "tool": found.tool,
        "label": found.label,
        "since": _stamp(moment),
        "until": _stamp(moment + timedelta(seconds=_HARD_SECONDS)),
        "idle_minutes": DEFAULT_IDLE_MINUTES,
        "idle_from": None if _running(chat) else _stamp(moment),
        "while_yolo": bool(public_view(chat, now=moment)["on"]),
    }
    rows = [item for item in _rows(chat) if not _same(item, found)]
    rows.append(row)
    chat["class_grants"] = rows
    chat["updated_at"] = now_iso()
    store.save_chat(chat)
    _audit(store, bot_id, {
        "at": now_iso(),
        "decision": "grant",
        "tag": "class",
        "rule": found.name,
        "class": found.name,
        "tool": found.tool,
        "why": found.label,
        "detail": found.label,
    })
    return found


def revoke(store, bot_id: str, chat_id: str, name: str = "", tool: str = "") -> int:
    """Remove one class, or every class when name is empty. Returns how many dropped."""
    from easyagent.safety import _audit
    from easyagent.store import now_iso

    try:
        chat = store.get_chat(bot_id, chat_id)
    except Exception:
        return 0
    rows = _rows(chat)
    if not name:
        dropped = rows
        kept: list[dict] = []
    else:
        dropped = [item for item in rows if item.get("class") == name and (not tool or item.get("tool") == tool)]
        kept = [item for item in rows if item not in dropped]
    if not dropped:
        return 0
    if kept:
        chat["class_grants"] = kept
    else:
        chat.pop("class_grants", None)
    chat["updated_at"] = now_iso()
    store.save_chat(chat)
    for item in dropped:
        _audit(store, bot_id, {
            "at": now_iso(),
            "decision": "revoke",
            "tag": "class",
            "rule": item.get("class") or "",
            "class": item.get("class") or "",
            "tool": item.get("tool") or "",
            "why": "The class grant was revoked.",
            "detail": item.get("label") or "",
        })
    return len(dropped)


def note_run(chat: dict, status: str) -> None:
    """Pause the idle clock while a job runs. The caller saves the chat."""
    rows = _rows(chat)
    if not rows:
        return
    changed = False
    for row in rows:
        if status == "running":
            if row.get("idle_from"):
                row["idle_from"] = None
                changed = True
            continue
        if not row.get("idle_from"):
            row["idle_from"] = _stamp(_now())
            changed = True
    if changed:
        chat["class_grants"] = rows


def public_rows(chat: dict, now: datetime | None = None) -> list[dict]:
    """Grants the header can show. This does not write the chat."""
    moment = now or _now()
    shown = []
    yolo_on = _yolo_on(chat, moment)
    for row in _rows(chat):
        if row.get("while_yolo") and not yolo_on:
            continue
        if not _clock_open(row, chat, moment):
            continue
        shown.append({
            "class": row.get("class") or "",
            "tool": row.get("tool") or "",
            "label": row.get("label") or "",
            "until": row.get("until"),
        })
    return shown


def _files(store, bot_id: str, request, rule: str) -> ActionClass | None:
    from pathlib import Path

    from easyagent import turn as turn_mod
    from easyagent.safety import _file_request_path, _persistence_path

    action = getattr(request, "action", "") or ""
    if action != "write" or rule != "overwrite":
        return None
    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        return None
    path = _file_request_path(store, getattr(request, "path", "") or ".", cwd, bot_id)
    if _persistence_path(path):
        return None
    if not _contained(store, bot_id, [path], writes=True):
        return None
    return _named(WRITE)


def _read(store, bot_id: str, command: str) -> ActionClass | None:
    import os
    from pathlib import Path

    from easyagent import turn as turn_mod
    from easyagent.psread import prove_readonly
    from easyagent.secrets import command_targets_secret

    if command_targets_secret(store, command):
        return None
    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        return None
    slot = turn_mod.current_slot()
    env = dict(slot.env) if slot is not None and slot.env else dict(os.environ)
    proved = prove_readonly(command, cwd, env)
    if proved is None:
        return None
    if not _contained(store, bot_id, proved, writes=False):
        return None
    return _named(READ)


def _listed(store, bot_id: str, command: str, rule: str) -> ActionClass | None:
    from pathlib import Path

    from easyagent import turn as turn_mod
    from easyagent.strictshell import _grant_tool

    if rule not in _RUN_RULES:
        return None
    if _REDIRECT.search(command):
        return None
    tool = _grant_tool(command) or ""
    head = tool.split()[0] if tool else ""
    if head not in _RUN_HEADS:
        return None
    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        return None
    if not _contained(store, bot_id, [cwd], writes=False):
        return None
    paths = _argument_paths(command, cwd)
    if not _contained(store, bot_id, paths, writes=False):
        return None
    label = f"Allow all: run {tool} in this bot's folder for this chat"
    return ActionClass(RUN, tool, label)


def _mutation(store, bot_id: str, request, verdict, name: str, *, redirect_ok: bool) -> ActionClass | None:
    from easyagent.safety import _persistence_path
    from easyagent.yolo import _prove_paths

    command = getattr(request, "command", "") or ""
    if not redirect_ok and _REDIRECT.search(command):
        return None
    try:
        paths = _prove_paths(store, bot_id, request, verdict)
    except Exception:
        return None
    if not paths:
        return None
    if any(_persistence_path(path) for path in paths):
        return None
    if not _contained(store, bot_id, paths, writes=True):
        return None
    return _named(name)


def _contained(store, bot_id: str, paths: list, *, writes: bool) -> bool:
    from easyagent.yolo import _read_inside, _safe_inside, _scope_roots

    roots = _scope_roots(store, bot_id)
    if not roots:
        return False
    check = _safe_inside if writes else _read_inside
    try:
        return all(check(store, bot_id, path, roots) for path in paths)
    except Exception:
        return False


def _argument_paths(command: str, cwd):
    from easyagent.safety import _command_program, _path_key, _paths_in

    paths = list(_paths_in(command, cwd))
    program = _command_program(command, cwd)
    if program is None:
        return paths
    key = _path_key(program)
    return [path for path in paths if _path_key(path) != key]


def _named(name: str) -> ActionClass:
    return ActionClass(name, "", _LABELS[name])


def _live(store, bot_id: str, chat: dict, found: ActionClass, moment: datetime) -> dict | None:
    from easyagent.safety import _audit
    from easyagent.store import now_iso

    rows = _rows(chat)
    kept: list[dict] = []
    chosen = None
    expired: list[dict] = []
    yolo_on = _yolo_on(chat, moment)
    for row in rows:
        if row.get("while_yolo") and not yolo_on:
            expired.append(row)
            continue
        if not _clock_open(row, chat, moment):
            expired.append(row)
            continue
        kept.append(row)
        if chosen is None and _same(row, found):
            chosen = row
    if expired:
        if kept:
            chat["class_grants"] = kept
        else:
            chat.pop("class_grants", None)
        chat["updated_at"] = now_iso()
        try:
            store.save_chat(chat)
        except Exception:
            return None
        for row in expired:
            _audit(store, bot_id, {
                "at": now_iso(),
                "decision": "expired",
                "tag": "class",
                "rule": row.get("class") or "",
                "class": row.get("class") or "",
                "tool": row.get("tool") or "",
                "why": "The class grant expired.",
                "detail": row.get("label") or "",
            })
    return chosen


def _clock_open(row: dict, chat: dict, moment: datetime) -> bool:
    until = _parse_time(row.get("until"))
    if until is None or moment >= until:
        return False
    if _running(chat):
        return True
    start = _parse_time(row.get("idle_from")) or _parse_time(row.get("since"))
    if start is None:
        return False
    try:
        minutes = int(row.get("idle_minutes") or DEFAULT_IDLE_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_IDLE_MINUTES
    if minutes < 1:
        minutes = 1
    return moment < start + timedelta(minutes=minutes)


def _yolo_on(chat: dict, moment: datetime) -> bool:
    from easyagent.yolo import public_view

    return bool(public_view(chat, now=moment)["on"])


def _rows(chat: dict) -> list[dict]:
    raw = chat.get("class_grants")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("class")]


def _same(row: dict, found: ActionClass) -> bool:
    return row.get("class") == found.name and (row.get("tool") or "") == (found.tool or "")


def _running(chat: dict) -> bool:
    run = chat.get("run") if isinstance(chat.get("run"), dict) else {}
    return run.get("status") == "running"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
