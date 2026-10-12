"""Machine checks for one task. A rubric check is graded by the caller."""

from __future__ import annotations

import re
from pathlib import Path


def _trace_text(trace: list[dict], *, success: bool | None = None) -> str:
    chunks = []
    for item in trace:
        if success is True and not item.get("success"):
            continue
        if success is False and item.get("success"):
            continue
        chunks.append(str(item.get("result") or ""))
        chunks.append(str(item.get("error") or ""))
    return "\n".join(chunks)


_FAIL_WORD = re.compile(r"(?i)\b(failed|traceback|assertionerror|exception)\b")
_EXIT_CODE = re.compile(r"exit code (\d+)")
_WRITE_ACTIONS = {"write", "delete", "move", "append", "edit", "mkdir", "create", "remove", "patch"}


def _blob(item: dict) -> str:
    return f"{item.get('result') or ''}\n{item.get('error') or ''}"


def _run_failed(item: dict) -> bool:
    blob = _blob(item)
    if not item.get("success"):
        return True
    if _FAIL_WORD.search(blob):
        return True
    match = None
    for match in _EXIT_CODE.finditer(blob):
        pass
    return match is not None and int(match.group(1)) != 0


def _debug_sequence(trace: list[dict], check: dict) -> tuple[bool, str]:
    """A failing command before the first edit, then that same command passing after it."""
    want_proved = bool(check.get("proved", True))
    edits = [
        index
        for index, item in enumerate(trace)
        if item.get("kind") == "files"
        and (item.get("action") or "") in _WRITE_ACTIONS
        and item.get("success")
    ]
    shells = [
        (index, item)
        for index, item in enumerate(trace)
        if item.get("kind") in {"shell", "windows"}
    ]
    if not edits:
        return False, "no edit"
    first = edits[0]
    prior = [item for index, item in shells if index < first and _run_failed(item)]
    if not prior:
        return False, "no failing run before the edit"
    command = " ".join(str(prior[-1].get("command") or "").split())
    proved = any(
        index > first
        and " ".join(str(item.get("command") or "").split()) == command
        and item.get("success")
        and not _run_failed(item)
        for index, item in shells
    )
    if want_proved:
        return proved, command or "passing rerun"
    return (not proved), "the same command did not pass" if not proved else "the same command passed"


def _tool_ok(trace: list[dict], check: dict) -> tuple[bool, str]:
    kind = (check.get("tool") or "").strip()
    action = (check.get("action") or "").strip()
    want_success = check.get("success", True)
    for item in trace:
        if kind and item.get("kind") != kind:
            continue
        if action and item.get("action") != action:
            continue
        if bool(item.get("success")) == bool(want_success):
            return True, f"{kind or 'tool'} {action}".strip()
    label = f"{kind or 'tool'} {action}".strip()
    return False, f"no matching {label} call"


def score_checks(
    criteria: list[dict],
    *,
    reply: str,
    trace: list[dict],
    workspace: Path,
    memory_text: str,
    grades: list[dict],
    unverified: bool = False,
) -> list[dict]:
    """One result per criterion, in order. llm_judge uses the next grade."""
    results = []
    grade_at = 0
    text = reply or ""
    for check in criteria:
        kind = check.get("kind") or ""
        passed = False
        detail = ""
        if kind == "file_exists":
            path = Path(check.get("path") or "")
            passed = path.is_file()
            detail = str(path)
        elif kind == "file_contains":
            path = Path(check.get("path") or "")
            needle = check.get("text") or ""
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = ""
            passed = needle in body
            detail = needle
        elif kind == "file_excludes":
            path = Path(check.get("path") or "")
            needle = check.get("text") or ""
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = ""
            passed = needle not in body and path.is_file()
            detail = needle
        elif kind == "reply_regex":
            pattern = check.get("pattern") or ""
            passed = bool(re.search(pattern, text, re.DOTALL))
            detail = pattern
        elif kind == "reply_excludes":
            pattern = check.get("pattern") or ""
            passed = not bool(re.search(pattern, text, re.DOTALL))
            detail = pattern
        elif kind == "tool_called":
            passed, detail = _tool_ok(trace, check)
        elif kind == "no_fabricated":
            needle = check.get("text") or ""
            in_reply = needle in text
            in_tools = needle in _trace_text(trace, success=True)
            passed = (not in_reply) or in_tools
            detail = needle
        elif kind == "grounded":
            needle = check.get("text") or ""
            passed = needle in text and needle in _trace_text(trace, success=True)
            detail = needle
        elif kind == "debug_sequence":
            passed, detail = _debug_sequence(trace, check)
        elif kind == "trace_contains":
            needle = check.get("text") or ""
            blob = "\n".join(
                str(item.get("result") or "") + "\n" + str(item.get("error") or "") for item in trace
            )
            passed = needle in blob
            detail = needle
        elif kind == "unverified":
            passed = bool(unverified) is bool(check.get("value", True))
            detail = "unverified" if unverified else "verified"
        elif kind == "max_steps":
            limit = int(check.get("limit") or 0)
            passed = len(trace) <= limit
            detail = f"{len(trace)} step(s), limit {limit}"
        elif kind == "memory_contains":
            needle = check.get("text") or ""
            passed = needle in (memory_text or "")
            detail = needle
        elif kind == "llm_judge":
            grade = grades[grade_at] if grade_at < len(grades) else {"pass": False, "problems": ["No grade."]}
            grade_at += 1
            passed = bool(grade.get("pass"))
            problems = grade.get("problems") or []
            detail = "; ".join(problems) if problems else "passed the rubric"
        else:
            detail = f"unknown check {kind}"
        results.append({"kind": kind, "passed": passed, "detail": detail})
    return results
