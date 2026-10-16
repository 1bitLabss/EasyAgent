"""Chat-scoped YOLO. One chat can skip approval cards for its own files.

A person turns it on with ``/yolo`` or the header control. While it is on, every
ask-tier action inside this bot's folders skips the card. It expires when the
chat sits idle, on a hard deadline, and when the app starts again. It never
covers another chat, a routine, a helper, a schedule, or a resumed run. A hard
block, a download-and-run, a disguised command, a git hook, and a ``.npmrc``
injection are refused with no card. The access layer is what decides the actor.
This module does not trust a source string that the access check refused.
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

# Asks YOLO never runs, even inside the folder. A block never reaches this set.
_REFUSE_RULES = frozenset({
    "download-exec",
    "remote-script",
    "remote-change",
    "install",
    "git-hooks",
    "injection",
    "wrapped-command",
    "inline-code",
    "exec-flag",
    "persistence",
    "admin",
    "git-destructive",
    "off-limits",
    "credential-dump",
    "firewall-off",
    "fork-bomb",
    "disk-format",
    "root-delete",
    "guardrail-edit",
    "browser-store",
    "data-dir",
    "own-server",
    "bot-isolation",
    "contain-setup",
    "already-denied",
    "unreviewed",
    "mcp-ask",
    "mcp-install",
    "browser-pay",
    "browser-login",
    "browser-post",
    "browser-settings",
    "browser-submit",
    "browser-secret",
})

_FETCH = re.compile(
    r"(?i)\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|start-bitstransfer|"
    r"certutil|bitsadmin|mshta|msiexec)\b"
)
_DISGUISE = re.compile(
    r"(?i)(\$\(|`|@'|@\"|"
    r"\b(iex|invoke-expression|start-process|set-alias|new-alias|sal|nal|eval|exec)\b|"
    r"\b(powershell|pwsh|cmd|bash|sh)(?:\.exe)?\b[^\n]*\s-(?:command|c|encodedcommand|enc|ec|e)\b|"
    r"\s-(?:encodedcommand|enc)\s|"
    r"(?:^|[;&|])\s*&\s*[{$])"
)
_INJECT_NAMES = frozenset({".npmrc", ".pnpmfile.cjs", ".yarnrc", ".yarnrc.yml", ".pypirc"})

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


def active(store: Store, bot_id: str, *, now: datetime | None = None) -> bool:
    """True when this turn's chat is the one YOLO was turned on for, and it is still on."""
    from easyagent.helpers import in_helper
    from easyagent.safety import is_unattended
    from easyagent import turn as turn_mod

    if is_unattended() or in_helper() or not bot_id:
        return False
    slot = turn_mod.current_slot()
    if slot is None or slot.bot_id != bot_id or not slot.chat_id:
        return False
    try:
        chat = store.get_chat(bot_id, slot.chat_id)
    except Exception:
        return False
    return bool(public_view(chat, now=now)["on"])


def refuses(store: Store, bot_id: str, request, verdict) -> bool:
    """True when this ask is a hard block YOLO must refuse with no card.

    A path outside the folder is not this. That still waits on a card. A drive
    wipe, a download, a disguise, a git hook, and a ``.npmrc`` injection are.
    """
    rule = str(getattr(verdict, "rule", "") or "")
    if rule in _REFUSE_RULES:
        return True
    kind = getattr(request, "kind", "")
    if kind == "files":
        return _files_refused(store, bot_id, request, rule)
    if kind != "shell":
        return False
    command = getattr(request, "command", "") or ""
    if _shape_hard(command):
        return True
    if rule == "bot-changed" and _implicated_hard(store, bot_id, command):
        return True
    try:
        paths = _touched_paths(store, bot_id, request, verdict)
    except Exception:
        return False
    return bool(paths) and any(_hard_path(path) for path in paths)


def covers(store: Store, bot_id: str, request, verdict, *, now: datetime | None = None) -> bool:
    """True when this ask-tier action may skip its card.

    Every ask whose paths stay in this bot's folders, or a folder that already
    has a grant, skips the card. A hard block, a secret, another bot's files,
    and a path that cannot be proved to stay there do not.
    """
    if getattr(verdict, "tier", "") != "ask" or not active(store, bot_id, now=now):
        return False
    if refuses(store, bot_id, request, verdict):
        return False
    try:
        ruled = _inside_scope(store, bot_id, request, verdict)
    except Exception:
        return False
    if not ruled:
        return False
    # The rule match is not the scope check. bot-changed only looks at the
    # files the bot edited, so a later rm of another folder used to ride it.
    try:
        return targets_confined(store, bot_id, request)
    except Exception:
        return False


def forget_process(store: Store) -> None:
    """A restart does not keep YOLO. Each chat loses it when the app starts."""
    try:
        bots = store.list_bots()
    except Exception:
        return
    for bot in bots:
        bot_id = str(bot.get("id") or "")
        if not bot_id:
            continue
        try:
            chats = store.list_chats(bot_id)
        except Exception:
            continue
        for item in chats:
            chat_id = str(item.get("id") or "")
            if chat_id:
                clear(store, bot_id, chat_id)


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
        f"Actions in this workspace skip the approval card for {_span(hard)}, "
        f"or after {idle} with nothing running. "
        "Drive wipes, downloads, disguised commands, git hooks, secrets, and anything outside still stop."
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
        _file_request_path,
        _path_key,
        _wide_token,
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
            # The shebang names the interpreter. It is not a path this write
            # touches. The body is proved statement by statement. None means
            # the body leaves the folder or cannot be proved, so the card stays.
            from easyagent.scriptgate import script_body_proved

            body = getattr(request, "body", "") or ""
            if not script_body_proved(store, bot_id, body, path.parent, path):
                return None
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


def _without_shebang(body: str) -> str:
    """Drop #! lines so the interpreter is not treated as a path the write touches."""
    kept = []
    for line in (body or "").splitlines():
        if line.lstrip().startswith("#!"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _shape_hard(command: str) -> bool:
    text = command or ""
    return bool(_FETCH.search(text) or _DISGUISE.search(text))


def _hard_path(path) -> bool:
    from easyagent.safety import _persistence_path

    return _inject_path(path) or _persistence_path(path)


def _inject_path(path) -> bool:
    name = path.name.lower()
    if name in _INJECT_NAMES or name.startswith(".yarnrc"):
        return True
    parts = [part.lower() for part in path.parts]
    return ".git" in parts and "hooks" in parts


def _files_refused(store: Store, bot_id: str, request, rule: str) -> bool:
    from pathlib import Path

    from easyagent import turn as turn_mod
    from easyagent.safety import _file_request_path, _mode, _workspace_roots, judge_command

    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        return False
    path = _file_request_path(store, getattr(request, "path", "") or ".", cwd, bot_id)
    if _hard_path(path):
        return True
    if rule != "script-body":
        return False
    # A shebang is not the command. python3 on that line is not python -c.
    body = _without_shebang(getattr(request, "body", "") or "")
    if not body.strip():
        return False
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
    return inner.tier == "block" or inner.rule in _REFUSE_RULES


def _implicated(store: Store, bot_id: str, command: str):
    from pathlib import Path

    from easyagent import turn as turn_mod
    from easyagent.safety import _workspace_roots
    from easyagent.strictshell import _records, _review_paths

    try:
        cwd = Path(turn_mod.tool_cwd())
        _bot, records = _records(store, bot_id)
        return list(_review_paths(command, cwd, _workspace_roots(store, bot_id, ""), records))
    except Exception:
        return None


def _exists(path) -> bool:
    try:
        return path.exists()
    except OSError:
        return True


def _implicated_hard(store: Store, bot_id: str, command: str) -> bool:
    paths = _implicated(store, bot_id, command)
    if not paths:
        return False
    return any(_hard_path(path) and _exists(path) for path in paths)


def _touched_paths(store: Store, bot_id: str, request, verdict) -> list | None:
    from pathlib import Path

    from easyagent import turn as turn_mod
    from easyagent.safety import _file_request_path

    kind = getattr(request, "kind", "")
    if kind == "files":
        try:
            cwd = Path(turn_mod.tool_cwd())
        except OSError:
            return None
        return [_file_request_path(store, getattr(request, "path", "") or ".", cwd, bot_id)]
    if kind != "shell":
        return None
    command = getattr(request, "command", "") or ""
    proved = _readonly_paths(request)
    if proved is not None:
        return proved
    paths = _prove_paths(store, bot_id, request, verdict)
    if paths:
        return paths
    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        return None
    return _operand_paths(command, cwd)


def _inside_scope(store: Store, bot_id: str, request, verdict) -> bool:
    from pathlib import Path

    from easyagent import turn as turn_mod

    kind = getattr(request, "kind", "")
    roots = _scope_roots(store, bot_id)
    if not roots:
        return False
    if kind == "files":
        rule = str(getattr(verdict, "rule", "") or "")
        paths = _prove_paths(store, bot_id, request, verdict) if rule == "script-body" else _touched_paths(
            store, bot_id, request, verdict
        )
        if not paths or any(_hard_path(path) for path in paths):
            return False
        return all(_safe_inside(store, bot_id, path, roots) for path in paths)
    if kind != "shell":
        return False
    command = getattr(request, "command", "") or ""
    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        return False
    from easyagent.scriptgate import command_reads_secrets, script_run_unproved

    # A script is read again here. A folder-wide read that would print a
    # secret file is not an ordinary in-folder read.
    if script_run_unproved(store, bot_id, command, cwd) or command_reads_secrets(command, cwd):
        return False
    if _command_is_wide(command, _wide()):
        return False
    rule = str(getattr(verdict, "rule", "") or "")
    if rule == "bot-changed":
        found = _implicated(store, bot_id, command)
        if not found:
            return False
        paths = []
        for path in found:
            if _hard_path(path):
                return False
            if not _exists(path):
                continue
            paths.append(path)
        if not paths:
            return False
        return all(_read_inside(store, bot_id, path, roots) for path in paths)
    proved = _readonly_paths(request)
    if proved is not None:
        if any(_hard_path(path) for path in proved):
            return False
        return _covers_readonly(store, bot_id, request)
    paths = _prove_paths(store, bot_id, request, verdict)
    if not paths:
        try:
            cwd = Path(turn_mod.tool_cwd())
        except OSError:
            return False
        paths = _operand_paths(command, cwd)
    if not paths or any(_hard_path(path) for path in paths):
        return False
    writes = rule in {"delete", "move", "overwrite", "redirect-overwrite", "write-outside", "script-body"} or bool(
        _REDIRECT.search(command)
    )
    check = _safe_inside if writes else _read_inside
    return all(check(store, bot_id, path, roots) for path in paths)


def targets_confined(store: Store, bot_id: str, request) -> bool:
    """True only when every real target of this command stays in the bot's folders.

    This is the last gate. It runs after the rule has matched, so a rule cannot
    skip it. A path that does not resolve, a symlink or junction that leaves
    the folder, a glob, an environment variable, a redirect, a cd, a chain, a
    subshell, xargs, or find -delete that cannot be proved stays a card.
    A folder grant is one of those folders. Membership uses the same resolved
    key as every other root: realpath, then the platform's case and slash fold.
    """
    kind = getattr(request, "kind", "")
    roots = _scope_roots(store, bot_id)
    if not roots:
        return False
    if kind == "files":
        paths = _touched_paths(store, bot_id, request, None)
        if not paths:
            return False
        return all(_safe_inside(store, bot_id, path, roots) for path in paths)
    if kind != "shell":
        return False
    command = getattr(request, "command", "") or ""
    proved = _readonly_paths(request)
    if proved is not None:
        return _covers_readonly(store, bot_id, request)
    targets = _shell_targets(command)
    if targets is None:
        return False
    if _mutates(command) and not targets:
        return False
    check = _safe_inside if _mutates(command) else _read_inside
    return all(check(store, bot_id, path, roots) for path in targets)


def _mutates(command: str) -> bool:
    text = command or ""
    if _REDIRECT.search(text):
        return True
    return bool(
        re.search(
            r"(?i)\b(rm|unlink|del|erase|rd|ri|rmdir|remove-item|mv|move|move-item|"
            r"cp|copy|copy-item|tee|dd|ln|link|set-content|out-file|add-content|new-item)\b",
            text,
        )
    )


def _piece_unanalyzable(text: str) -> bool:
    """find -delete and find -exec name a search, not the files they will touch."""
    if re.search(r"(?i)\bfind\b", text or "") and re.search(
        r"(?i)(?:^|\s)-(?:delete|exec|execdir|ok|okdir)\b", text or ""
    ):
        return True
    return bool(re.search(r"(?i)(?:^|[\s;&|])xargs\b", text or ""))


def _shell_targets(command: str):
    """Resolved targets, or None when the command cannot be fully read."""
    import os
    from pathlib import Path

    from easyagent import turn as turn_mod
    from easyagent.shellexpand import prepared_command, still_unresolved
    from easyagent.strictshell import _sites

    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        return None
    slot = turn_mod.current_slot()
    env = dict(slot.env) if slot is not None and slot.env else dict(os.environ)
    expanded = prepared_command(command or "", env)
    if still_unresolved(expanded) or _shape_hard(expanded):
        return None
    try:
        sites = _sites(expanded, cwd)
    except Exception:
        return None
    if not sites or any(site.get("unknown") or site.get("dir_unknown") for site in sites):
        return None
    found = []
    for site in sites:
        text = str(site.get("text") or "")
        if _piece_unanalyzable(text):
            return None
        piece_cwd = site.get("cwd") or cwd
        paths = _piece_targets(text, piece_cwd)
        if paths is None:
            return None
        found.extend(paths)
    return found


def _piece_targets(text: str, cwd):
    """Paths this one piece names. None when one of them cannot be proved."""
    from easyagent.safety import _resolve, _wide_token

    paths = _operand_paths(text, cwd)
    if paths is None:
        return None
    from easyagent.strictshell import _strip_extended

    extra = []
    for match in re.finditer(r"(?i)\b(?:of|if)=(\S+)", text or ""):
        token = _strip_extended(match.group(1).strip("'\""))
        if not token or token in {".", "..", "*", "~"} or _wide_token(token):
            return None
        if any(mark in token for mark in "*?$%"):
            return None
        extra.append(_resolve(token, cwd))
    return list(paths) + extra


def _wide():
    from easyagent.safety import _wide_token

    return _wide_token


def _readonly_paths(request):
    import os
    from pathlib import Path

    from easyagent import turn as turn_mod
    from easyagent.psread import prove_readonly

    if getattr(request, "kind", "") != "shell":
        return None
    command = getattr(request, "command", "") or ""
    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        return None
    slot = turn_mod.current_slot()
    env = dict(slot.env) if slot is not None and slot.env else dict(os.environ)
    return prove_readonly(command, cwd, env)


def _drive_token(token: str) -> bool:
    """A Windows absolute path whose only colon is the drive letter.

    ``C:\\a\\b``, ``c:/a/b``, and ``\\\\?\\C:\\a\\b`` can be resolved. An
    alternate stream (``notes.txt:stream``), a drive-relative ``C:notes.txt``,
    and a UNC path cannot. Those stay a card. ``\\\\?\\UNC\\`` is a UNC path
    after the prefix is removed, so it stays a card too.
    """
    from easyagent.strictshell import _strip_extended

    text = _strip_extended((token or "").strip().strip("'\""))
    if text.count(":") != 1:
        return False
    return bool(re.match(r"(?i)^[A-Za-z]:[\\/]", text))


def _command_unreadable(raw: str) -> bool:
    """True when a path in this command cannot be proved.

    A variable, ``..``, or an alternate stream is not expanded here. On
    Windows a drive letter is a real path, and confinement then resolves it
    with the same key a folder grant uses.
    """
    import os

    text = raw or ""
    if re.search(r"[`$%]|\.\.", text):
        return True
    if os.name != "nt":
        return ":" in text or "\\\\" in text
    for token in re.findall(r"[^\s|&;<>]+", text):
        cleaned = token.strip("'\"")
        if not cleaned:
            continue
        if ":" in cleaned or cleaned.startswith("\\\\") or cleaned.startswith("//"):
            if not _drive_token(cleaned):
                return True
    return False


def _operand_paths(command: str, cwd) -> list | None:
    """Files a command names, after expansion. None when a path cannot be proved."""
    from pathlib import Path

    from easyagent.safety import _command_program, _content_target, _path_key, _paths_in, _resolve, _wide_token
    from easyagent.shellexpand import prepared_command, still_unresolved

    raw = command or ""
    # A variable, an alternate stream, or .. is proved by the read parser or not at all.
    # Expanding it here can turn an empty variable into a path inside the folder.
    if _command_unreadable(raw):
        return None
    expanded = prepared_command(raw)
    if still_unresolved(expanded) or _shape_hard(expanded) or _command_unreadable(expanded):
        return None
    if re.search(r"(?i)[a-z][a-z0-9+.-]*://", expanded):
        return None
    from easyagent.strictshell import _strip_extended

    paths = []
    for match in _REDIRECT.finditer(expanded):
        token = _strip_extended(match.group(2))
        if any(mark in token for mark in ("*", "?", "$", "%")):
            return None
        if _wide_token(token) or token in {".", "..", "*", "~"}:
            return None
        paths.append(_resolve(token, cwd))
    target = _content_target(expanded, cwd)
    if target is not None:
        paths.append(target)
    paths.extend(_paths_in(expanded, cwd))
    for token in re.findall(r"[^\s|&;<>]+", expanded):
        cleaned = token.strip("'\"")
        if not cleaned or cleaned.startswith("-"):
            continue
        # \\?\ is an extended-length prefix. Its question mark is not a glob.
        bare = _strip_extended(cleaned)
        if bare in {".", "..", "*", "~"} or _wide_token(bare):
            return None
        if any(mark in bare for mark in ("*", "?", "$", "%")):
            return None
        looks = "/" in bare or "\\" in bare or bare.startswith(".") or "." in Path(bare).name
        if not looks:
            try:
                looks = (cwd / cleaned).exists()
            except OSError:
                looks = False
        if not looks:
            continue
        paths.append(_resolve(bare, cwd))
    program = _command_program(expanded, cwd)
    if program is not None:
        key = _path_key(program)
        paths = [path for path in paths if _path_key(path) != key]
    return _unique(paths)


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
        secret_file,
        saved_data_blocked,
    )
    from easyagent.secrets import is_protected_secret

    if secret_file(path) or _is_profile_root(path) or _is_guardrail(path):
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
        secret_file,
        saved_data_blocked,
    )

    if secret_file(path) or _is_profile_root(path) or _is_guardrail(path):
        return False
    if saved_data_blocked(store, path, bot_id) or _foreign_bot_data(store, path, bot_id):
        return False
    if not _inside(path, roots):
        return False
    from easyagent.strictshell import _canonical_key

    path_key = _canonical_key(path)
    if not path_key:
        return False
    for root in roots:
        if _canonical_key(root) == path_key:
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
