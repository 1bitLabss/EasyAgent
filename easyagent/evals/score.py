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
