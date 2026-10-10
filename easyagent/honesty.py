"""Harness checks for a turn. Deterministic first. At most one extra model call.

A missing setting stays on. The scripted runner drives the same functions the
live turn uses, with a list of replies standing in for the model.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "receipts": True,
    "pushback": True,
    "excuse": True,
    "loop": True,
    "tripwires": True,
    "stall": True,
    "stall_minutes": 5,
}

_CLEAR_PUSHBACK = (
    "still broken",
    "still wrong",
    "didn't work",
    "did not work",
    "doesn't work",
    "does not work",
    "not working",
    "not fixed",
    "same error",
    "still fails",
    "still failed",
    "failed again",
    "didn't fix",
    "did not fix",
    "still doesn't",
    "still does not",
    "it's still",
    "it is still broken",
)
_AMBIGUOUS = re.compile(r"(?i)^\s*(no|nope|wrong|again|still|what\?|huh)\s*[.!?]*\s*$")
_CLAIM = re.compile(r"(?i)\b(works now|tests pass|fixed|done|created|deleted)\b")
_NEG_BEFORE = re.compile(
    r"(?i)\b(not|never|unable|cannot|can't|didn't|did not|wasn't|isn't|without|no)\s*$"
)
_HEDGE = re.compile(r"(?i)\b(?:this should fix it|should work now)\b")
_EXCUSE = re.compile(
    r"(?i)\b(pre-existing|preexisting|unrelated|not caused by (?:my|this) change)\b"
)
_APOLOGY = re.compile(r"(?i)\b(i'm sorry|i am sorry|sorry|apologize|apologies|my mistake|my bad)\b")
_ACTION = re.compile(r"(?i)\b(run|test|read|check|open|reproduce|edit|inspect|retry|look)\b")
_SHELL_MUTATE = re.compile(
    r"(?i)(?:>>?|[|]>)|\b(?:rm|del|erase|rmdir|mkdir|mv|move|cp|copy|tee|chmod|touch|unlink|sed|"
    r"set-content|add-content|out-file|new-item|remove-item)\b"
)
_MCP_MUTATE = re.compile(r"(?i)\b(write|delete|remove|send|update|create|put|post|edit|unlink)\b")
_WRITE_ACTIONS = {"write", "delete", "move", "append", "edit", "mkdir", "create", "remove", "patch"}
_EXIT = re.compile(r"exit code (\d+)")
_TOOL_LINE = re.compile(r"^TOOL\s+(\S+)(?:\s+(.*))?$")
_KV = re.compile(r"(\w+)=((?:\"[^\"]*\")|(?:'[^']*')|\S+)")
_BULLET = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])\s+\S")
_SNAPSHOT_CAP = 2_000_000

_CURRENT: ContextVar["Trace | None"] = ContextVar("easyagent_honesty", default=None)


def live_model_allowed() -> bool:
    """The suite disables the reply checker, and with it this extra model call.

    Production leaves EASYAGENT_CHECK unset, so one ambiguous classification or
    one verification call can still run. The scripted runner spends its own call.
    """
    return os.environ.get("EASYAGENT_CHECK", "1") != "0"


def _clamp_minutes(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 5
    if number < 1:
        return 1
    if number > 120:
        return 120
    return number


def load_settings(store, bot_id: str | None) -> dict:
    settings = dict(DEFAULTS)
    if store is None or not bot_id:
        return settings
    path = store._bot_dir(bot_id) / "honesty.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if not isinstance(raw, dict):
        return settings
    for key in ("receipts", "pushback", "excuse", "loop", "tripwires", "stall"):
        if key in raw:
            settings[key] = bool(raw[key])
    if "stall_minutes" in raw:
        settings["stall_minutes"] = _clamp_minutes(raw.get("stall_minutes"))
    return settings


def save_settings(store, bot_id: str, patch: dict) -> dict:
    store.get_bot(bot_id)
    current = load_settings(store, bot_id)
    for key in ("receipts", "pushback", "excuse", "loop", "tripwires", "stall"):
        if key in patch and patch[key] is not None:
            current[key] = bool(patch[key])
    if "stall_minutes" in patch and patch["stall_minutes"] is not None:
        current["stall_minutes"] = _clamp_minutes(patch.get("stall_minutes"))
    folder = store._bot_dir(bot_id)
    folder.mkdir(parents=True, exist_ok=True)
    from easyagent.store import atomic_write_text

    atomic_write_text(folder / "honesty.json", json.dumps(current, indent=2) + "\n")
    return current


@dataclass
class Trace:
    settings: dict
    store: object = None
    bot_id: str = ""
    events: list[dict] = field(default_factory=list)
    last_change_index: int = -1
    fail_counts: dict[str, int] = field(default_factory=dict)
    investigate: bool = False
    reproduced: bool = False
    fix_attempts: int = 0
    pending_fix: bool = False
    snapshot: dict[str, bytes | None] = field(default_factory=dict)
    last_progress: float = 0.0
    model_calls: int = 0
    last_fail_command: str = ""
    last_fail_cwd: str = ""
    lesson_prefix: str = ""
    mistakes: list | None = None
    clock: object = time.monotonic
    outcome: "Outcome | None" = None
    outcome_taken: bool = False
    excuse_note: str | None = None
    excuse_checked: bool = False


@dataclass
class Outcome:
    text: str
    receipts: list[dict] = field(default_factory=list)
    unverified: bool = False
    needs_verify: bool = False


@dataclass
class Call:
    kind: str
    action: str = ""
    path: str = ""
    command: str = ""
    body: str = ""


def current() -> Trace | None:
    return _CURRENT.get()


def begin(store=None, bot_id: str | None = None, *, settings: dict | None = None, clock=None) -> Trace:
    merged = load_settings(store, bot_id)
    if settings:
        merged.update(settings)
        if "stall_minutes" in settings:
            merged["stall_minutes"] = _clamp_minutes(settings.get("stall_minutes"))
    trace = Trace(settings=merged, store=store, bot_id=bot_id or "")
    if clock is not None:
        trace.clock = clock
    trace.last_progress = float(trace.clock())
    _CURRENT.set(trace)
    return trace


def touch() -> None:
    trace = current()
    if trace is None:
        return
    trace.last_progress = float(trace.clock())


def latest_user(messages: list[dict] | None) -> str:
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def classify_pushback(text: str) -> str:
    """'yes', 'no', or 'maybe'. 'maybe' is the only case that may call the model."""
    folded = " ".join((text or "").lower().split())
    if any(phrase in folded for phrase in _CLEAR_PUSHBACK):
        return "yes"
    if _AMBIGUOUS.match((text or "").strip()):
        return "maybe"
    return "no"


def pushback_json(raw: str) -> bool:
    try:
        data = json.loads(raw or "")
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "pushback" in data:
        return bool(data["pushback"])
    lowered = (raw or "").lower()
    return "true" in lowered and "false" not in lowered


def mark_pushback(yes: bool) -> None:
    trace = current()
    if trace is None or not yes or not trace.settings.get("pushback", True):
        return
    trace.investigate = True


def working_messages(messages: list[dict] | None) -> list[dict]:
    """Drop earlier assistant explanations. The stored chat is left as it is."""
    trace = current()
    source = list(messages or [])
    if trace is None or not trace.investigate:
        return source
    return [item for item in source if item.get("role") not in {"assistant", "tool"}]


def poll() -> str | None:
    trace = current()
    if trace is None or not trace.settings.get("stall", True):
        return None
    line = _stall_line(trace)
    if not line:
        return None
    return line


def _stall_line(trace: Trace) -> str:
    if not trace.settings.get("stall", True):
        return ""
    limit = float(trace.settings.get("stall_minutes") or 5) * 60.0
    if float(trace.clock()) - trace.last_progress < limit:
        return ""
    minutes = int(trace.settings.get("stall_minutes") or 5)
    return f"Blocked: no progress for {minutes} minutes."


def _signature(request) -> str:
    parts = [
        getattr(request, "kind", "") or "",
        getattr(request, "action", "") or "",
        getattr(request, "path", "") or "",
        getattr(request, "command", "") or "",
        (getattr(request, "body", "") or "")[:180],
    ]
    return "|".join(" ".join(part.lower().split()) for part in parts)


def is_edit(request) -> bool:
    kind = (getattr(request, "kind", "") or "").lower()
    action = (getattr(request, "action", "") or "").lower()
    command = getattr(request, "command", "") or ""
    blob = " ".join(
        part
        for part in (
            action,
            command,
            getattr(request, "path", "") or "",
            getattr(request, "body", "") or "",
            getattr(request, "call_name", "") or "",
        )
        if part
    )
    if kind == "files" and action in _WRITE_ACTIONS:
        return True
    if kind == "files":
        return False
    if kind == "shell":
        return bool(_SHELL_MUTATE.search(command))
    if kind == "mcp":
        return bool(_MCP_MUTATE.search(blob))
    return False


def output_failed(text: str) -> bool:
    match = None
    for match in _EXIT.finditer(text or ""):
        pass
    if match is None:
        return False
    return int(match.group(1)) != 0


def _cwd() -> str:
    try:
        from easyagent import turn as turn_mod

        return turn_mod.tool_cwd()
    except Exception:
        return ""


def _resolve(path: str) -> Path:
    raw = Path(path)
    if raw.is_absolute():
        return raw
    cwd = _cwd() or "."
    return Path(cwd) / raw


def _snapshot(trace: Trace, path: str) -> None:
    if not path or not trace.settings.get("excuse", True):
        return
    try:
        target = _resolve(path)
    except OSError:
        return
    key = str(target)
    if key in trace.snapshot:
        return
    try:
        if target.is_symlink():
            return
        if target.is_file():
            if target.stat().st_size > _SNAPSHOT_CAP:
                return
            trace.snapshot[key] = target.read_bytes()
            return
        if target.exists():
            return
        trace.snapshot[key] = None
    except OSError:
        return


def _lesson_body(body: str) -> str:
    kept = []
    for line in (body or "").splitlines():
        folded = line.strip().lower()
        if folded.startswith(("trigger:", "pattern:", "check:")):
            continue
        kept.append(line)
    return " ".join(" ".join(kept).split())


def _tripwire_fields(body: str) -> dict | None:
    trigger = ""
    pattern = ""
    check = ""
    for line in (body or "").splitlines():
        folded = line.strip()
        lower = folded.lower()
        if lower.startswith("trigger:"):
            trigger = folded.split(":", 1)[1].strip().lower()
        elif lower.startswith("pattern:"):
            pattern = folded.split(":", 1)[1].strip()
        elif lower.startswith("check:"):
            check = folded.split(":", 1)[1].strip()
    if not trigger:
        return None
    return {"tool": trigger, "pattern": pattern, "check": check or "ok"}


def _tool_matches(trigger: str, request) -> bool:
    parts = (trigger or "").split()
    if not parts:
        return False
    kind = (getattr(request, "kind", "") or "").lower()
    action = (getattr(request, "action", "") or "").lower()
    if parts[0] not in {kind, "*"}:
        return False
    if len(parts) > 1 and parts[1] not in {action, "*"}:
        return False
    return True


def _blob(request) -> str:
    return "\n".join(
        [
            getattr(request, "kind", "") or "",
            getattr(request, "action", "") or "",
            getattr(request, "command", "") or "",
            getattr(request, "path", "") or "",
            getattr(request, "body", "") or "",
        ]
    )


def _pattern_hits(pattern: str, blob: str) -> bool:
    if not pattern:
        return True
    try:
        return re.search(pattern, blob, re.I) is not None
    except re.error:
        return pattern.lower() in blob.lower()


def _check_passes(check: str, blob: str) -> bool:
    rule = (check or "ok").strip()
    if rule == "block":
        return False
    if rule in {"", "ok"}:
        return True
    if rule.lower().startswith("absent:"):
        try:
            return re.search(rule.split(":", 1)[1], blob, re.I) is None
        except re.error:
            return True
    if rule.lower().startswith("present:"):
        try:
            return re.search(rule.split(":", 1)[1], blob, re.I) is not None
        except re.error:
            return False
    return True


def _load_mistakes(trace: Trace) -> list:
    if trace.mistakes is not None:
        return trace.mistakes
    trace.mistakes = []
    if not trace.store or not trace.bot_id or not trace.settings.get("tripwires", True):
        return trace.mistakes
    try:
        from easyagent.journal import notes_dir, read_entries

        path = notes_dir(trace.store, trace.bot_id) / "MISTAKES.md"
        text = path.read_text(encoding="utf-8")
    except (OSError, ImportError):
        return trace.mistakes
    found = []
    for entry in read_entries(text):
        fields = _tripwire_fields(entry.body)
        if fields:
            found.append((entry, fields))
    trace.mistakes = found
    return found


def _matching_lessons(trace: Trace, request) -> list[tuple]:
    blob = _blob(request)
    matched = []
    for entry, fields in _load_mistakes(trace):
        if not _tool_matches(fields["tool"], request):
            continue
        if not _pattern_hits(fields["pattern"], blob):
            continue
        matched.append((entry, fields))
        if len(matched) >= 3:
            break
    return matched


def before_tool(request) -> str | None:
    """Block a tool, or return None so it runs. A block is the tool result."""
    trace = current()
    if trace is None:
        return None
    if trace.settings.get("tripwires", True):
        matched = _matching_lessons(trace, request)
        if matched:
            lessons = []
            failed = False
            blob = _blob(request)
            for entry, fields in matched:
                title = (entry.title or "Mistake").strip()
                detail = _lesson_body(entry.body)
                lessons.append(f"Lesson: {title}. {detail}".strip())
                if not _check_passes(fields["check"], blob):
                    failed = True
            lesson = "\n".join(lessons)
            trace.last_progress = float(trace.clock())
            if failed:
                return f"{lesson}\nThe check failed, so this was not run."
            trace.lesson_prefix = lesson + "\n\n"
    if trace.settings.get("loop", True):
        sig = _signature(request)
        if trace.fail_counts.get(sig, 0) >= 2:
            trace.fail_counts[sig] = trace.fail_counts.get(sig, 0) + 1
            trace.last_progress = float(trace.clock())
            return "That call already failed twice. Change approach. It was not run again."
    if trace.investigate and trace.settings.get("pushback", True) and is_edit(request):
        trace.last_progress = float(trace.clock())
        if not trace.reproduced:
            return "Investigation first: reproduce the failure with a command, a log, or a test before editing."
        if trace.fix_attempts >= 2:
            return "Two fixes already failed. Switch approach before another edit."
    if is_edit(request) and (getattr(request, "kind", "") or "") == "files":
        _snapshot(trace, getattr(request, "path", "") or "")
    return None


def after_tool(request, output: str, *, ok: bool) -> str:
    trace = current()
    raw = output or ""
    if trace is None:
        return raw
    text = raw
    if trace.lesson_prefix:
        text = trace.lesson_prefix + raw
        trace.lesson_prefix = ""
    failed = (not ok) or output_failed(raw)
    kind = getattr(request, "kind", "") or ""
    action = getattr(request, "action", "") or ""
    command = getattr(request, "command", "") or ""
    path = getattr(request, "path", "") or ""
    trace.events.append(
        {
            "kind": kind,
            "action": action,
            "command": command,
            "path": path,
            "ok": not failed,
            "output": raw[:240],
        }
    )
    trace.last_progress = float(trace.clock())
    if failed:
        sig = _signature(request)
        trace.fail_counts[sig] = trace.fail_counts.get(sig, 0) + 1
        if kind == "shell" and command:
            trace.last_fail_command = command
            trace.last_fail_cwd = _cwd()
    edit = is_edit(request)
    if edit:
        trace.last_change_index = len(trace.events) - 1
        if trace.investigate:
            if failed:
                trace.fix_attempts += 1
                trace.pending_fix = False
            else:
                trace.pending_fix = True
    elif trace.investigate and trace.pending_fix:
        if failed:
            trace.fix_attempts += 1
        trace.pending_fix = False
    if trace.investigate and not edit and text.strip():
        trace.reproduced = True
    return text if trace.events else raw


def _claims(text: str) -> list[str]:
    found: list[str] = []
    for match in _CLAIM.finditer(text or ""):
        window = (text or "")[max(0, match.start() - 24) : match.start()]
        if _NEG_BEFORE.search(window):
            continue
        word = match.group(1).lower()
        if word not in found:
            found.append(word)
    return found


def _backing(trace: Trace) -> dict | None:
    for index in range(len(trace.events) - 1, trace.last_change_index, -1):
        event = trace.events[index]
        if event.get("ok") and str(event.get("output") or "").strip():
            return event
    return None


def _label(event: dict) -> str:
    bits = [str(event.get("kind") or "")]
    if event.get("action"):
        bits.append(str(event["action"]))
    if event.get("command"):
        bits.append(str(event["command"]))
    elif event.get("path"):
        bits.append(str(event["path"]))
    return " ".join(bit for bit in bits if bit)[:180]


def _has_causes(text: str) -> bool:
    if "my last change caused it" not in (text or "").lower():
        return False
    if len(_BULLET.findall(text or "")) >= 2:
        return True
    chunks = [part.strip() for part in re.split(r"[.\n;]|\bor\b", text or "") if len(part.split()) >= 3]
    return len(chunks) >= 2


def _open_promises(trace: Trace) -> list[str]:
    if not trace.store or not trace.bot_id:
        return []
    try:
        from easyagent.journal import notes_dir, read_entries

        path = notes_dir(trace.store, trace.bot_id) / "PROMISES.md"
        text = path.read_text(encoding="utf-8")
    except (OSError, ImportError):
        return []
    titles = []
    for entry in read_entries(text):
        if entry.kind != "promise":
            continue
        if (entry.status or "open") not in {"open", ""}:
            continue
        title = (entry.title or "").strip()
        if title:
            titles.append(title)
    return titles[:8]


def _swap_snapshot(snapshot: dict[str, bytes | None]) -> dict[str, bytes | None]:
    current_bytes: dict[str, bytes | None] = {}
    for key, old in snapshot.items():
        path = Path(key)
        try:
            if path.is_symlink():
                continue
            current_bytes[key] = path.read_bytes() if path.is_file() else None
            if old is None:
                if path.is_file():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(old)
        except OSError:
            continue
    return current_bytes


def _restore_bytes(saved: dict[str, bytes | None]) -> None:
    for key, data in saved.items():
        path = Path(key)
        try:
            if data is None:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        except OSError:
            continue


def _rerun(command: str, cwd: str) -> str:
    from easyagent.tools import _shell_invocation

    argv, use_shell = _shell_invocation(command)
    try:
        proc = subprocess.run(
            argv,
            shell=use_shell,
            capture_output=True,
            text=True,
            cwd=cwd or None,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"the command could not be run again ({exc})"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if not out:
        out = f"exit code {proc.returncode}"
    else:
        out = f"{out}\nexit code {proc.returncode}"
    return out[:500]


def _excuse_note(trace: Trace, text: str) -> str:
    if trace.excuse_checked:
        return trace.excuse_note or ""
    trace.excuse_checked = True
    trace.excuse_note = ""
    if not trace.settings.get("excuse", True):
        return ""
    if not _EXCUSE.search(text or ""):
        return ""
    if not trace.last_fail_command or not trace.snapshot:
        return ""
    saved = _swap_snapshot(trace.snapshot)
    try:
        result = _rerun(trace.last_fail_command, trace.last_fail_cwd)
    finally:
        _restore_bytes(saved)
    note = f"Checked the state before this turn's changes: {result}"
    trace.excuse_note = note
    return note


def settle(text: str, *, allow_model: bool = False) -> Outcome:
    trace = current()
    raw = text or ""
    if trace is None:
        return Outcome(raw)
    if trace.settings.get("receipts", True):
        rewritten, hedged = _rewrite_hedges(raw)
    else:
        rewritten, hedged = raw, False
    claims = _claims(rewritten) if trace.settings.get("receipts", True) else []
    backing = _backing(trace)
    receipts = []
    unverified = False
    needs_verify = False
    if claims:
        if backing:
            for claim in claims:
                receipts.append(
                    {
                        "claim": claim,
                        "tool": _label(backing),
                        "output": str(backing.get("output") or "")[:240],
                    }
                )
        else:
            for claim in claims:
                receipts.append({"claim": claim, "tool": "", "output": ""})
            if allow_model and trace.model_calls == 0:
                needs_verify = True
            else:
                unverified = True
    if hedged and not backing and not needs_verify:
        unverified = True
    pieces = [rewritten.rstrip()]
    excuse = _excuse_note(trace, rewritten)
    if excuse:
        pieces.append(excuse)
    if trace.investigate and trace.settings.get("pushback", True):
        if not _has_causes(rewritten):
            pieces.append(
                "Investigation is incomplete: list at least two causes, including that your last change caused it."
            )
        if trace.fix_attempts >= 2 and ("switch" not in rewritten.lower() or "approach" not in rewritten.lower()):
            pieces.append("Switching approach. Two fixes already failed.")
    if _APOLOGY.search(rewritten) and not trace.events and not _ACTION.search(rewritten):
        pieces.append("An apology needs a new action.")
    if trace.settings.get("stall", True):
        stall = _stall_line(trace)
        joined = "\n".join(pieces)
        if stall and "Blocked: no progress" not in joined:
            pieces.append(stall)
        promises = _open_promises(trace)
        if promises and "Blocked: still open" not in joined:
            pieces.append("Blocked: still open — " + "; ".join(promises) + ".")
    body = "\n\n".join(part for part in pieces if part).strip()
    if not body:
        stall = _stall_line(trace) if trace.settings.get("stall", True) else ""
        body = stall
    return Outcome(body, receipts, unverified, needs_verify)


def _rewrite_hedges(text: str) -> tuple[str, bool]:
    rewritten, count = _HEDGE.subn("This is not verified.", text or "")
    return rewritten, count > 0


def take_outcome() -> Outcome | None:
    trace = current()
    if trace is None or trace.outcome is None or trace.outcome_taken:
        return None
    trace.outcome_taken = True
    return trace.outcome


def parse_script(text: str) -> tuple[list[Call], str]:
    calls: list[Call] = []
    prose: list[str] = []
    for line in (text or "").splitlines():
        match = _TOOL_LINE.match(line.strip())
        if not match:
            prose.append(line)
            continue
        fields = {}
        for key, value in _KV.findall(match.group(2) or ""):
            fields[key] = value.strip("\"'")
        calls.append(
            Call(
                kind=match.group(1),
                action=fields.get("action", ""),
                path=fields.get("path", ""),
                command=fields.get("command", ""),
                body=fields.get("body", ""),
            )
        )
    return calls, "\n".join(prose).strip()


async def _invoke(tools, call: Call) -> str:
    result = tools(call)
    if hasattr(result, "__await__"):
        result = await result
    return "" if result is None else str(result)


async def _run_call(tools, call: Call, outputs: list[str]) -> None:
    blocked = before_tool(call)
    if blocked:
        outputs.append(blocked)
        return
    try:
        output = await _invoke(tools, call)
        ok = not output_failed(output)
    except Exception as exc:
        output = str(exc)
        ok = False
    outputs.append(after_tool(call, output, ok=ok))


async def run_scripted(
    replies: list[str],
    user: str,
    tools,
    *,
    prior: list[dict] | None = None,
    settings: dict | None = None,
    store=None,
    bot_id: str | None = None,
    jump_seconds: float = 0,
) -> dict:
    """One turn. `replies` is the fake model, in order, including at most one extra call."""
    queue = list(replies)
    trace = begin(store, bot_id, settings=settings)
    messages = list(prior or [])
    messages.append({"role": "user", "content": user})
    kind = classify_pushback(user)
    if (
        kind == "maybe"
        and queue
        and trace.settings.get("pushback", True)
        and trace.model_calls == 0
    ):
        raw = queue.pop(0)
        trace.model_calls += 1
        kind = "yes" if pushback_json(raw) else "no"
    if kind == "yes":
        mark_pushback(True)
    context = working_messages(messages)
    outputs: list[str] = []
    prose = ""
    try:
        while queue:
            reply = queue.pop(0)
            calls, prose = parse_script(reply)
            for call in calls:
                await _run_call(tools, call, outputs)
            if prose or not calls:
                break
        if prose:
            touch()
        if jump_seconds:
            trace.last_progress -= float(jump_seconds)
        outcome = settle(prose, allow_model=trace.model_calls == 0 and bool(queue))
        if outcome.needs_verify and queue:
            trace.model_calls += 1
            extra = queue.pop(0)
            calls, _extra_prose = parse_script(extra)
            for call in calls:
                await _run_call(tools, call, outputs)
            outcome = settle(prose, allow_model=False)
        trace.outcome = outcome
        return {
            "text": outcome.text,
            "receipts": outcome.receipts,
            "unverified": outcome.unverified,
            "model_calls": trace.model_calls,
            "investigate": trace.investigate,
            "outputs": outputs,
            "context": context,
        }
    finally:
        _CURRENT.set(None)
