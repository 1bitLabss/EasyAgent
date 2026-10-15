"""Chat-scoped YOLO. One chat can skip approval cards for its own files.

A person turns it on with ``/yolo`` or the header control. It expires when the
chat sits idle, and on a hard deadline. It never covers another chat, a routine,
a helper, a schedule, or a resumed run. The access layer is what decides the
actor. This module does not trust a source string that the access check refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from easyagent.access import yolo_actor_allowed
from easyagent.store import Store, now_iso

DEFAULT_IDLE_MINUTES = 30
DEFAULT_HOURS = 2
MAX_HOURS = 8
_MAX_SECONDS = MAX_HOURS * 60 * 60
_DEFAULT_SECONDS = DEFAULT_HOURS * 60 * 60
_MIN_SECONDS = 60

# Approval-card rules whose targets are files. Anything else still waits.
_ELIGIBLE = frozenset({
    "delete",
    "overwrite",
    "redirect-overwrite",
    "move",
    "script-body",
    "write-outside",
})

# Ask rules a proved read-only pipeline may skip. A block never moves.
_READ_RULES = frozenset({
    "unlisted-command",
    "dynamic-command",
})

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([hm])$", re.IGNORECASE)
_REDIRECT = re.compile(r"(?i)(?<![0-9&])>{1,2}\s*(['\"]?)([^'\"\s|&;]+)\1")


@dataclass(frozen=True)
class YoloResult:
    ok: bool
    message: str


def is_command(text: str) -> bool:
    return parse_command(text) is not None


def parse_command(text: str) -> dict | None:
    """A whole-message slash command, or None when the line is ordinary text.

    ``/yolo``, ``/yolo on``, ``/yolo off``, ``/yolo 4h``, ``/yolo 90m``, and
    ``/yolo 4h idle 15m``. Any other word means this is not the command.
    """
    raw = " ".join((text or "").split())
    if not raw.lower().startswith("/yolo"):
        return None
    if not re.fullmatch(r"/yolo(?:\s+\S+)*", raw, flags=re.IGNORECASE):
        return None
    parts = raw.split()
    if parts[0].lower() != "/yolo":
        return None
    tokens = parts[1:]
    if not tokens:
        return {"off": False, "seconds": None, "idle": None}
    if len(tokens) == 1 and tokens[0].lower() == "off":
        return {"off": True, "seconds": None, "idle": None}
    seconds = None
    idle = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        low = token.lower()
        if low == "off":
            return None
        if low == "on":
            index += 1
            continue
        if low == "idle":
            if idle is not None or index + 1 >= len(tokens):
                return None
            idle = _duration_seconds(tokens[index + 1])
            if idle is None:
                return None
            index += 2
            continue
        if seconds is not None:
            return None
        seconds = _duration_seconds(token)
        if seconds is None:
            return None
        index += 1
    return {"off": False, "seconds": seconds, "idle": idle}


def apply_command(store: Store, bot_id: str, chat_id: str, text: str, *, source: str) -> YoloResult:
    """Turn YOLO on or off from a slash command. A refused actor changes nothing."""
    parsed = parse_command(text)
    if parsed is None:
        return YoloResult(False, "That is not a YOLO command. Nothing was changed.")
    if not yolo_actor_allowed(source):
        return YoloResult(False, "Only you can turn YOLO on.")
    if parsed["off"]:
        clear(store, bot_id, chat_id)
        return YoloResult(True, "YOLO is off for this chat.")
    return _enable(
        store,
        bot_id,
        chat_id,
        seconds=parsed["seconds"],
        idle_seconds=parsed["idle"],
    )


def apply_toggle(
    store: Store,
    bot_id: str,
    chat_id: str,
    *,
    on: bool,
    hours: float | None = None,
    idle_minutes: int | None = None,
    source: str,
) -> YoloResult:
    """The header control. ``source`` is chosen by the route, not by the body."""
    if not yolo_actor_allowed(source):
        return YoloResult(False, "Only you can turn YOLO on.")
    if not on:
        clear(store, bot_id, chat_id)
        return YoloResult(True, "YOLO is off for this chat.")
    seconds = None if hours is None else int(float(hours) * 3600)
    idle_seconds = None if idle_minutes is None else int(idle_minutes) * 60
    return _enable(store, bot_id, chat_id, seconds=seconds, idle_seconds=idle_seconds)


def clear(store: Store, bot_id: str, chat_id: str) -> None:
    """Drop YOLO for this chat. A resumed run calls this before it starts."""
    try:
        chat = store.get_chat(bot_id, chat_id)
    except Exception:
        return
    if "yolo" not in chat:
        return
    chat.pop("yolo", None)
    chat["updated_at"] = now_iso()
    store.save_chat(chat)


def note_run(chat: dict, status: str) -> None:
    """Pause the idle clock while a job is running. Start it again when the job stops.

    The chat dict is updated in place. The caller saves it. Reading a chat does not.
    """
    state = chat.get("yolo")
    if not isinstance(state, dict) or not state.get("on"):
        return
    if status == "running":
        if state.get("idle_from"):
            state["idle_from"] = None
            chat["yolo"] = state
        return
    if not state.get("idle_from"):
        state["idle_from"] = now_iso()
        chat["yolo"] = state


def public_view(chat: dict, now: datetime | None = None) -> dict:
    """What the header shows. This does not write the chat."""
    state = chat.get("yolo") if isinstance(chat.get("yolo"), dict) else None
    empty = {"on": False, "until": None, "idle_until": None, "remaining_seconds": 0}
    if not state or not state.get("on"):
        return empty
    moment = now or _now()
    until = _parse_time(state.get("until"))
    if until is None or moment >= until:
        return {"on": False, "until": state.get("until"), "idle_until": None, "remaining_seconds": 0}
    running = _running(chat)
    idle_until = None
    if not running:
        start = _parse_time(state.get("idle_from")) or _parse_time(state.get("since"))
        if start is None:
            return empty
        idle_until = start + timedelta(minutes=_idle_minutes(state))
        if moment >= idle_until:
            return {
                "on": False,
                "until": state.get("until"),
                "idle_until": _stamp(idle_until),
                "remaining_seconds": 0,
            }
    deadline = until
    if idle_until is not None and idle_until < deadline:
        deadline = idle_until
    remaining = int((deadline - moment).total_seconds())
    if remaining <= 0:
        return empty
    return {
        "on": True,
        "until": state.get("until"),
        "idle_until": _stamp(idle_until) if idle_until is not None else None,
        "remaining_seconds": remaining,
    }


def covers(store: Store, bot_id: str, request, verdict, *, now: datetime | None = None) -> bool:
    """True when this ask-tier action may skip its card.

    The current turn's chat must be the one YOLO was turned on for. Paths that
    cannot be proved to sit inside the workspace, or a folder that already has
    a grant, stay on the card. A read-only PowerShell pipeline can skip its
    card on the same terms. A block is never this function's job.
    """
    from easyagent.helpers import in_helper
    from easyagent.safety import is_unattended
    from easyagent import turn as turn_mod

    if is_unattended() or in_helper():
        return False
    rule = getattr(verdict, "rule", "")
    if getattr(verdict, "tier", "") != "ask" or rule not in _ELIGIBLE | _READ_RULES:
        return False
    slot = turn_mod.current_slot()
    if slot is None or slot.bot_id != bot_id or not slot.chat_id:
        return False
    try:
        chat = store.get_chat(bot_id, slot.chat_id)
    except Exception:
        return False
    if not public_view(chat, now=now)["on"]:
        return False
    if rule in _READ_RULES:
        try:
            return _covers_readonly(store, bot_id, request)
        except Exception:
            return False
    try:
        paths = _prove_paths(store, bot_id, request, verdict)
    except Exception:
        return False
    if not paths:
        return False
    roots = _scope_roots(store, bot_id)
    if not roots:
        return False
    return all(_safe_inside(store, bot_id, path, roots) for path in paths)


def _enable(
    store: Store,
    bot_id: str,
    chat_id: str,
    *,
    seconds: int | None,
    idle_seconds: int | None,
) -> YoloResult:
    notes: list[str] = []
    if seconds is None:
        hard = _DEFAULT_SECONDS
    elif seconds < _MIN_SECONDS:
        return YoloResult(False, "YOLO needs at least 1 minute. Nothing was changed.")
    elif seconds > _MAX_SECONDS:
        hard = _MAX_SECONDS
        notes.append(f"{_span(seconds)} is above the 8 hour maximum, so it stops at 8 hours")
    else:
        hard = seconds
    if idle_seconds is None:
        idle_minutes = DEFAULT_IDLE_MINUTES
    elif idle_seconds < _MIN_SECONDS:
        return YoloResult(False, "The idle time needs at least 1 minute. Nothing was changed.")
    elif idle_seconds > _MAX_SECONDS:
        idle_minutes = MAX_HOURS * 60
        notes.append("the idle time stops at 8 hours")
    else:
        idle_minutes = max(1, idle_seconds // 60)
    moment = _now()
    chat = store.get_chat(bot_id, chat_id)
    running = _running(chat)
    chat["yolo"] = {
        "on": True,
        "since": _stamp(moment),
        "until": _stamp(moment + timedelta(seconds=hard)),
        "idle_minutes": idle_minutes,
        "hours": hard / 3600,
        "idle_from": None if running else _stamp(moment),
    }
    chat["updated_at"] = now_iso()
    store.save_chat(chat)
    return YoloResult(True, _on_sentence(hard, idle_minutes, notes))


def _on_sentence(hard: int, idle_minutes: int, notes: list[str]) -> str:
    extra = ""
    if notes:
        clause = ", and ".join(notes)
        clause = clause[:1].upper() + clause[1:]
        extra = " " + clause + "."
    idle = "1 minute" if idle_minutes == 1 else f"{idle_minutes} minutes"
    return (
        f"YOLO is on for this chat.{extra} "
        f"File actions in this workspace skip the approval card for {_span(hard)}, "
        f"or after {idle} with nothing running. "
        "Drive wipes, secrets, messages, and anything outside still wait."
    )


def _span(seconds: int) -> str:
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return "1 hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} seconds"


def _duration_seconds(token: str) -> int | None:
    match = _DURATION.fullmatch(token.strip())
    if match is None:
        return None
    value = float(match.group(1))
    if value < 0:
        return None
    if match.group(2).lower() == "h":
        return int(value * 3600)
    return int(value * 60)


def _scope_roots(store: Store, bot_id: str) -> list:
    from pathlib import Path

    from easyagent.workspace import bot_workspace

    roots = [bot_workspace(store, bot_id)]
    try:
        bot = store.get_bot(bot_id)
    except Exception:
        bot = {}
    for item in (bot or {}).get("safety_exec_always") or []:
        if isinstance(item, dict) and item.get("root"):
            roots.append(Path(str(item["root"])))
    return roots


def _prove_paths(store: Store, bot_id: str, request, verdict) -> list | None:
    from pathlib import Path

    from easyagent import turn as turn_mod
    from easyagent.safety import (
        _command_program,
        _content_target,
        _file_request_path,
        _mode,
        _paths_in,
        _path_key,
        _resolve,
        _targets_after_verb,
        _wide_token,
        _workspace_roots,
        judge_command,
        shell_text_as_run,
    )

    try:
        cwd = Path(turn_mod.tool_cwd()).resolve()
    except OSError:
        cwd = Path(turn_mod.tool_cwd())
    kind = getattr(request, "kind", "")
    rule = getattr(verdict, "rule", "")
    if kind == "files":
        path = _file_request_path(store, getattr(request, "path", "") or ".", cwd, bot_id)
        paths = [path]
        if rule == "script-body":
            body = getattr(request, "body", "") or ""
            try:
                bot = store.get_bot(bot_id)
            except Exception:
                bot = None
            inner = judge_command(
                body,
                remote=False,
                cwd=path.parent,
                roots=_workspace_roots(store, bot_id, ""),
                created=set(),
                mode=_mode(bot),
            )
            if inner.tier == "block" or inner.rule not in _ELIGIBLE:
                return None
            inner_paths = _shell_paths(body, path.parent, inner.rule)
            if not inner_paths:
                return None
            paths.extend(inner_paths)
        return _unique(paths)
    if kind != "shell":
        return None
    command = shell_text_as_run(getattr(request, "command", "") or "")
    if _command_is_wide(command, _wide_token):
        return None
    paths = _shell_paths(command, cwd, rule)
    if not paths:
        return None
    program = _command_program(command, cwd)
    if program is not None:
        program_key = _path_key(program)
        paths = [path for path in paths if _path_key(path) != program_key]
    return paths or None


def _shell_paths(command: str, cwd, rule: str) -> list | None:
    from easyagent.safety import _content_target, _paths_in, _resolve, _targets_after_verb

    if rule in {"delete", "move"}:
        paths = list(_targets_after_verb(command, cwd))
        paths.extend(_paths_in(command, cwd))
        return _unique(paths) or None
    if rule in {"redirect-overwrite", "overwrite", "write-outside"}:
        paths = []
        for match in _REDIRECT.finditer(command or ""):
            paths.append(_resolve(match.group(2), cwd))
        target = _content_target(command, cwd)
        if target is not None:
            paths.append(target)
        paths.extend(_paths_in(command, cwd))
        return _unique(paths) or None
    return None


def _command_is_wide(command: str, wide_token) -> bool:
    for token in re.findall(r"[^\s]+", command or ""):
        cleaned = token.strip("'\"").strip()
        if cleaned in {".", "..", "*", "~", ">", ">>"}:
            if cleaned in {".", "..", "*", "~"}:
                return True
            continue
        if wide_token(cleaned):
            return True
    return False


def _unique(paths: list) -> list:
    from easyagent.safety import _path_key

    found = []
    seen = set()
    for path in paths:
        key = _path_key(path)
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return found


def _covers_readonly(store: Store, bot_id: str, request) -> bool:
    """A parsed read whose paths stay in this bot's folders. Anything else waits."""
    import os
    from pathlib import Path

    from easyagent import turn as turn_mod
    from easyagent.psread import prove_readonly
    from easyagent.secrets import command_targets_secret

    if getattr(request, "kind", "") != "shell":
        return False
    command = getattr(request, "command", "") or ""
    if command_targets_secret(store, command):
        return False
    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        return False
    slot = turn_mod.current_slot()
    env = dict(slot.env) if slot is not None and slot.env else dict(os.environ)
    proved = prove_readonly(command, cwd, env)
    if proved is None:
        return False
    if not proved:
        return True
    roots = _scope_roots(store, bot_id)
    if not roots:
        return False
    return all(_read_inside(store, bot_id, path, roots) for path in proved)


def _read_inside(store: Store, bot_id: str, path, roots: list) -> bool:
    """A read may name the workspace itself. A secret or another bot's file may not."""
    from easyagent.safety import (
        _foreign_bot_data,
        _inside,
        _is_guardrail,
        _is_profile_root,
        _secret_kind,
        saved_data_blocked,
    )
    from easyagent.secrets import is_protected_secret

    if _secret_kind(path) or _is_profile_root(path) or _is_guardrail(path):
        return False
    if is_protected_secret(store, path):
        return False
    if saved_data_blocked(store, path, bot_id) or _foreign_bot_data(store, path, bot_id):
        return False
    return _inside(path, roots)


def _safe_inside(store: Store, bot_id: str, path, roots: list) -> bool:
    from easyagent.safety import (
        _foreign_bot_data,
        _inside,
        _is_guardrail,
        _is_profile_root,
        _secret_kind,
        saved_data_blocked,
    )

    if _secret_kind(path) or _is_profile_root(path) or _is_guardrail(path):
        return False
    if saved_data_blocked(store, path, bot_id) or _foreign_bot_data(store, path, bot_id):
        return False
    if not _inside(path, roots):
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            if resolved == root.resolve():
                return False
        except OSError:
            return False
    return True


def _running(chat: dict) -> bool:
    run = chat.get("run") if isinstance(chat.get("run"), dict) else {}
    return run.get("status") == "running"


def _idle_minutes(state: dict) -> int:
    try:
        minutes = int(state.get("idle_minutes") or DEFAULT_IDLE_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_IDLE_MINUTES
    if minutes < 1:
        return 1
    if minutes > MAX_HOURS * 60:
        return MAX_HOURS * 60
    return minutes


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
