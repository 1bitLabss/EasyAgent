"""Harness checks for a turn. Deterministic first. At most one extra model call.

A missing setting stays on. The scripted runner drives the same functions the
live turn uses, with a list of replies standing in for the model.
"""

from __future__ import annotations

import json
import os
import re
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
    "debug": True,
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
_BUG = re.compile(
    r"(?i)\b(bugs?|failures?|failed|fails|failing|errors?|broken|crashes?|crashed|exceptions?|traceback|regressions?)\b"
)
_CITE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]+:\d+")
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_PROPOSAL = re.compile(r"(?i)\b(?:i'll|i will)\s+(?:fix|change|edit|patch)\b|\b(?:the fix is|here is the fix|here's the fix)\b")
_PROOF = {"fixed", "done", "works", "works now"}
_DEBUG_NOTE = (
    "Debug mode. Run a command or test that shows the failure before any edit. "
    "Name the cause in one sentence with a file:line or an output line from that run. "
    "Rerun that same command before saying it is fixed."
)
_NUDGE_REPRO = "Debug: reproduce the failure with a command or test first. The edit was not applied."
_NUDGE_CAUSE = (
    "Debug: name the cause in one sentence and cite a file:line or an output line from this turn. "
    "The edit was not applied."
)
_NUDGE_REPEAT = (
    "Debug: that repeats the last fix. Name a new cause and run a check that tells it apart. "
    "The edit was not applied."
)
_FOLLOW = "Run the failing test or command now."
_NUDGE_LINE = re.compile(
    r"(?i)^(?:debug:|investigation first:|investigation is incomplete:|two fixes already failed\b|switching approach\b)"
)
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
    for key in ("receipts", "pushback", "excuse", "loop", "tripwires", "stall", "debug"):
        if key in raw:
            settings[key] = bool(raw[key])
    if "stall_minutes" in raw:
        settings["stall_minutes"] = _clamp_minutes(raw.get("stall_minutes"))
    return settings


def save_settings(store, bot_id: str, patch: dict) -> dict:
    store.get_bot(bot_id)
    current = load_settings(store, bot_id)
    for key in ("receipts", "pushback", "excuse", "loop", "tripwires", "stall", "debug"):
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
    shell_ran: str = ""
    lesson_prefix: str = ""
    mistakes: list | None = None
    clock: object = time.monotonic
    outcome: "Outcome | None" = None
    outcome_taken: bool = False
    excuse_note: str | None = None
    excuse_checked: bool = False
    debug: bool = False
    reply_note: str = ""
    prior_explanation: str = ""
    last_edit: str = ""
    repro_command: str = ""
    repro_output: str = ""
    repro_failed: bool = False
    calls: int = 0
    hold: str = ""
    followed: bool = False
    chips: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    text: str
    receipts: list[dict] = field(default_factory=list)
    unverified: bool = False
    needs_verify: bool = False
    debug: bool = False
    chips: list[str] = field(default_factory=list)


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


def classify_debug(text: str) -> bool:
    """A bug or a failure report. Pushback is handled separately."""
    return _BUG.search(text or "") is not None


def mark_debug(yes: bool) -> None:
    trace = current()
    if trace is None or not yes or not trace.settings.get("debug", True):
        return
    trace.debug = True


def remember_prior(messages: list[dict] | None) -> None:
    """The previous explanation, so a pushback cannot repeat that fix."""
    trace = current()
    if trace is None:
        return
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "assistant":
            trace.prior_explanation = str(message.get("content") or "")[:800]
            return


def note_reply(text: str) -> None:
    trace = current()
    if trace is None:
        return
    trace.reply_note = text or ""


def mark_pushback(yes: bool) -> None:
    trace = current()
    if trace is None or not yes or not trace.settings.get("pushback", True):
        return
    trace.investigate = True


def working_messages(messages: list[dict] | None) -> list[dict]:
    """Drop earlier assistant explanations. The stored chat is left as it is."""
    trace = current()
    source = list(messages or [])
    if trace is not None and trace.investigate:
        source = [item for item in source if item.get("role") not in {"assistant", "tool"}]
    if trace is not None and trace.debug:
        source = [{"role": "system", "content": _DEBUG_NOTE}, *source]
    return source


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


def note_shell_started(command: str) -> None:
    """The shell process exists. A refusal before this does not count as a run."""
    trace = current()
    if trace is None:
        return
    trace.shell_ran = command or ""


def _shell_executed(trace: Trace, command: str, output: str) -> bool:
    """True when this shell command started. A block, a hold, or a refused container did not."""
    wanted = " ".join((command or "").split())
    ran = " ".join((trace.shell_ran or "").split())
    if ran and ran == wanted:
        return True
    if re.search(r"(?i)\b(?:was not run|not run|did not run|was not started)\b", output or ""):
        return False
    return output_failed(output or "")


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


def _chip(trace: Trace, label: str) -> None:
    if label and label not in trace.chips:
        trace.chips.append(label)


def _hold(trace: Trace, text: str, chip: str) -> str:
    """The edit does not run. The note goes to the model, and the chip is what the person sees."""
    _chip(trace, chip)
    trace.hold = text
    return text


def consume_hold() -> str:
    trace = current()
    if trace is None:
        return ""
    note = trace.hold
    trace.hold = ""
    return note


def take_followup(text: str) -> str | None:
    """One ask when a debug turn claims a result, or promises a fix, and called no tool."""
    trace = current()
    if trace is None or trace.followed or trace.calls:
        return None
    if not trace.debug or not trace.settings.get("debug", True):
        return None
    body = text or ""
    claims = _with_works(body, _claims(body))
    if not _PROPOSAL.search(body) and not claims:
        return None
    trace.followed = True
    _chip(trace, "Run the check")
    return _FOLLOW


def _without_nudges(text: str) -> str:
    kept = [line for line in (text or "").splitlines() if not _NUDGE_LINE.match(line.strip())]
    return "\n".join(kept).strip()


def before_tool(request) -> str | None:
    """Block a tool, or return None so it runs. A block is the tool result."""
    trace = current()
    if trace is None:
        return None
    trace.calls += 1
    trace.hold = ""
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
            return _hold(
                trace,
                "Investigation first: reproduce the failure with a command, a log, or a test before editing.",
                "Reproduce first",
            )
        if trace.fix_attempts >= 2:
            return _hold(
                trace,
                "Two fixes already failed. Switch approach before another edit.",
                "Switch approach",
            )
    held = _debug_hold(trace, request)
    if held:
        return held
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
    executed = kind == "shell" and bool(command) and _shell_executed(trace, command, raw)
    trace.shell_ran = ""
    if failed:
        sig = _signature(request)
        trace.fail_counts[sig] = trace.fail_counts.get(sig, 0) + 1
        if executed:
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
    if (
        trace.debug
        and kind in {"shell", "windows"}
        and command
        and (failed or _shows_failure(raw))
    ):
        trace.repro_failed = True
        trace.repro_command = " ".join(command.split())
        trace.repro_output = raw[:500]
        trace.reproduced = True
    if edit and not failed:
        trace.last_edit = ((path or "") + "\n" + (getattr(request, "body", "") or "")).strip()
    return text if trace.events else raw


def _debug_hold(trace: Trace, request) -> str | None:
    """A short nudge instead of a stall. The edit does not land."""
    if not trace.debug or not trace.settings.get("debug", True) or not is_edit(request):
        return None
    trace.last_progress = float(trace.clock())
    if trace.investigate and _repeats_fix(trace, request):
        _count_hold(trace, request)
        return _hold(trace, _NUDGE_REPEAT, "New cause")
    if not trace.repro_failed:
        _count_hold(trace, request)
        return _hold(trace, _NUDGE_REPRO, "Reproduce first")
    if not _cause_ready(trace):
        _count_hold(trace, request)
        return _hold(trace, _NUDGE_CAUSE, "Name the cause")
    return None


def _count_hold(trace: Trace, request) -> None:
    sig = _signature(request)
    trace.fail_counts[sig] = trace.fail_counts.get(sig, 0) + 1


def _repeats_fix(trace: Trace, request) -> bool:
    body = (getattr(request, "body", "") or "").strip()
    current = ((getattr(request, "path", "") or "") + "\n" + (getattr(request, "body", "") or "")).strip()
    if trace.last_edit and current == trace.last_edit:
        return True
    prior = trace.prior_explanation or ""
    return bool(body) and len(body) >= 16 and body in prior


def _shows_failure(output: str) -> bool:
    return re.search(r"(?i)\b(failed|traceback|assertionerror|exception)\b", output or "") is not None


def _prose_note(text: str) -> str:
    return _FENCE.sub(" ", text or "")


def _evidence_blob(trace: Trace) -> str:
    parts = []
    for event in trace.events:
        parts.append(str(event.get("output") or ""))
        parts.append(str(event.get("path") or ""))
        parts.append(str(event.get("command") or ""))
    return "\n".join(parts)


def _cause_ready(trace: Trace) -> bool:
    prose = _prose_note(trace.reply_note)
    blob = _evidence_blob(trace)
    if not blob.strip():
        return False
    folded_blob = blob.lower()
    sentences = [part.strip() for part in re.split(r"(?<=\.)\s+|\n", prose) if len(part.split()) >= 4]
    for sentence in sentences:
        folded = sentence.lower()
        for match in _CITE.finditer(sentence):
            if match.group(0).lower() in folded_blob:
                return True
        for line in blob.splitlines():
            line = line.strip()
            if len(line) >= 11 and line.lower() in folded:
                return True
        folded_norm = folded.replace("\\", "/")
        blob_norm = folded_blob.replace("\\", "/")
        for match in re.finditer(r"[a-z0-9_./-]{12,}", folded_norm):
            token = match.group(0).strip("./-")
            if len(token) >= 12 and token in blob_norm:
                return True
    return False


def _proof_event(trace: Trace) -> dict | None:
    if not trace.repro_failed or not trace.repro_command:
        return None
    command = " ".join(trace.repro_command.split())
    marker = ""
    for line in (trace.repro_output or "").splitlines():
        stripped = line.strip()
        if len(stripped) >= 11 and _shows_failure(stripped) and "exit code" not in stripped.lower():
            marker = stripped
            break
    for index in range(len(trace.events) - 1, trace.last_change_index, -1):
        event = trace.events[index]
        if " ".join(str(event.get("command") or "").split()) != command:
            continue
        if not event.get("ok"):
            continue
        output = str(event.get("output") or "")
        if output_failed(output) or _shows_failure(output):
            continue
        if marker and marker in output:
            continue
        return event
    return None


def _with_works(text: str, claims: list[str]) -> list[str]:
    if "works" in claims or "works now" in claims:
        return claims
    for match in re.finditer(r"(?i)\bworks\b", text or ""):
        window = (text or "")[max(0, match.start() - 24) : match.start()]
        if _NEG_BEFORE.search(window):
            continue
        return [*claims, "works"]
    return claims


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


async def _rerun(trace: Trace, command: str, cwd: str) -> str:
    """Rerun through Strict, approval, and containment. Files stay put until it is allowed to run."""
    from easyagent import turn as turn_mod
    from easyagent.safety import guard
    from easyagent.tools import ToolError, ToolRequest, _run_shell

    if trace.store is None:
        return "The command was not run."
    slot = turn_mod.current_slot()
    previous_cwd = slot.cwd if slot is not None else None
    if slot is None:
        slot = turn_mod.bind(trace.store, "honesty-rerun", trace.bot_id or "")
    if cwd:
        slot.cwd = cwd
    try:
        request = ToolRequest(kind="shell", command=command)
        try:
            _request, early = await guard(trace.store, request, trace.bot_id or None)
        except ToolError as exc:
            return str(exc)
        if early is not None:
            return str(early)
        saved = _swap_snapshot(trace.snapshot)
        try:
            try:
                return _run_shell(trace.store, command)
            except ToolError as exc:
                return str(exc)
        finally:
            _restore_bytes(saved)
    finally:
        if previous_cwd is not None:
            slot.cwd = previous_cwd


async def _excuse_note(trace: Trace, text: str) -> str:
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
    result = await _rerun(trace, trace.last_fail_command, trace.last_fail_cwd)
    note = f"Checked the state before this turn's changes: {result}"
    trace.excuse_note = note
    return note


async def settle(text: str, *, allow_model: bool = False) -> Outcome:
    trace = current()
    raw = text or ""
    if trace is None:
        return Outcome(raw)
    if trace.debug or trace.investigate:
        raw = _without_nudges(raw)
    if trace.settings.get("receipts", True):
        rewritten, hedged = _rewrite_hedges(raw)
    else:
        rewritten, hedged = raw, False
    claims = _claims(rewritten) if trace.settings.get("receipts", True) else []
    if trace.debug and trace.settings.get("debug", True):
        claims = _with_works(rewritten, claims)
    backing = _backing(trace)
    proof = _proof_event(trace) if trace.debug and trace.settings.get("debug", True) else None
    receipts = []
    unverified = False
    needs_verify = False
    if claims:
        for claim in claims:
            use_proof = bool(trace.debug and claim in _PROOF)
            event = proof if use_proof else backing
            if event:
                receipts.append(
                    {
                        "claim": claim,
                        "tool": _label(event),
                        "output": str(event.get("output") or "")[:240],
                    }
                )
                continue
            receipts.append({"claim": claim, "tool": "", "output": ""})
            if use_proof and proof is None and backing is not None:
                unverified = True
                continue
            if allow_model and trace.model_calls == 0:
                needs_verify = True
            else:
                unverified = True
        if trace.debug and any(claim in _PROOF for claim in claims) and proof is None and not needs_verify:
            unverified = True
    if hedged and not backing and not proof and not needs_verify:
        unverified = True
    pieces = [rewritten.rstrip()]
    excuse = await _excuse_note(trace, rewritten)
    if excuse:
        pieces.append(excuse)
    if trace.investigate and trace.settings.get("pushback", True):
        if not _has_causes(rewritten):
            _chip(trace, "Two causes")
        if trace.fix_attempts >= 2 and ("switch" not in rewritten.lower() or "approach" not in rewritten.lower()):
            _chip(trace, "Switch approach")
        prior = (trace.prior_explanation or "").strip()
        first = prior.split("\n", 1)[0].strip()
        if len(first) >= 24 and first.lower() in rewritten.lower():
            _chip(trace, "New cause")
        if not any(event.get("kind") in {"shell", "windows"} for event in trace.events):
            _chip(trace, "Run the check")
    if (
        trace.debug
        and trace.settings.get("debug", True)
        and not trace.repro_failed
        and _PROPOSAL.search(rewritten)
    ):
        _chip(trace, "Reproduce first")
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
    return Outcome(body, receipts, unverified, needs_verify, bool(trace.debug), list(trace.chips))


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
    remember_prior(messages)
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
        mark_debug(True)
    elif classify_debug(user):
        mark_debug(True)
    context = working_messages(messages)
    outputs: list[str] = []
    hidden: list[dict] = []
    prose = ""
    claimed = ""
    try:
        while queue:
            reply = queue.pop(0)
            note_reply(reply)
            calls, prose = parse_script(reply)
            if prose:
                claimed = prose
            for call in calls:
                await _run_call(tools, call, outputs)
            if calls:
                continue
            ask = take_followup(prose)
            if ask and queue:
                hidden.append({"role": "user", "content": ask})
                continue
            break
        prose = claimed or prose
        if prose:
            touch()
        if jump_seconds:
            trace.last_progress -= float(jump_seconds)
        outcome = await settle(prose, allow_model=trace.model_calls == 0 and bool(queue))
        if outcome.needs_verify and queue:
            trace.model_calls += 1
            extra = queue.pop(0)
            calls, _extra_prose = parse_script(extra)
            for call in calls:
                await _run_call(tools, call, outputs)
            outcome = await settle(prose, allow_model=False)
        trace.outcome = outcome
        return {
            "text": outcome.text,
            "receipts": outcome.receipts,
            "unverified": outcome.unverified,
            "model_calls": trace.model_calls,
            "investigate": trace.investigate,
            "debug": trace.debug,
            "chips": list(outcome.chips),
            "hidden": hidden,
            "outputs": outputs,
            "context": [*context, *hidden],
        }
    finally:
        _CURRENT.set(None)
