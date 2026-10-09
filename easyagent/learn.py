"""Verified learning. The model proposes. A check and a replay decide.

A candidate lives under skills/_candidates and is not a live skill.
User-written skills and memory are never rewritten by this module.
Replay uses the bot's own connection. There is no second model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from easyagent.skills import RESERVED_SLUGS, parse_skill_document, slugify
from easyagent.store import Store, StoreError, new_id, now_iso

BODY_CAP = 1200
COMMAND_CAP = 200
STEP_CAP = 8
PITFALL_CAP = 6
INBOX_CAP = 40
LEDGER_CAP = 100
FAILS_BEFORE_ARCHIVE = 3
KNOWN_TOOLS = {
    "files",
    "shell",
    "search",
    "memory",
    "history",
    "project",
    "question",
    "finish",
    "ssh",
    "windows",
    "react",
}
CANDIDATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "trigger": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "pitfalls": {"type": "array", "items": {"type": "string"}},
        "scope": {"type": "string"},
        "check": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "enum": ["command", "file", "regex"]},
                "command": {"type": "string"},
                "exit_code": {"type": "integer"},
                "path": {"type": "string"},
                "contains": {"type": "string"},
                "exists": {"type": "boolean"},
                "pattern": {"type": "string"},
            },
            "required": ["kind"],
        },
    },
    "required": ["name", "trigger", "steps", "pitfalls", "scope", "check"],
}
CANDIDATE_GRAMMAR = r"""
root ::= "{" ws "\"name\":" ws str "," ws "\"trigger\":" ws str "," ws "\"steps\":" ws arr "," ws "\"pitfalls\":" ws arr "," ws "\"scope\":" ws str "," ws "\"check\":" ws check "}"
ws ::= [ \t\n]*
str ::= "\"" ([^"\\] | "\\" .)* "\""
arr ::= "[" ws (str ("," ws str)*)? ws "]"
check ::= "{" ws "\"kind\":" ws ("\"command\"" | "\"file\"" | "\"regex\"") ("," ws "\"command\":" ws str)? ("," ws "\"exit_code\":" ws num)? ("," ws "\"path\":" ws str)? ("," ws "\"contains\":" ws str)? ("," ws "\"exists\":" ws ("true" | "false"))? ("," ws "\"pattern\":" ws str)? ws "}"
num ::= "-"? [0-9]+
""".strip()
_TOOL_REF = re.compile(r"`([a-z][a-z0-9_-]{1,24})`")
_STOP = False


class LearningStopped(Exception):
    """Stop was pressed, or a chat started, so this replay ends."""


def request_stop() -> None:
    global _STOP
    _STOP = True


def clear_stop() -> None:
    global _STOP
    _STOP = False


def stopped() -> bool:
    return _STOP


def task_type(text: str) -> str:
    skip = {"the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "please", "with"}
    words = [word for word in re.findall(r"[a-z0-9]+", (text or "").lower()) if word not in skip and len(word) > 2]
    return "-".join(words[:4]) or "general"


def _similar(left: str, right: str) -> bool:
    a = set((left or "").split("-"))
    b = set((right or "").split("-"))
    if not a or not b:
        return False
    return a == b or bool(a & b)


def _read_json(path: Path, fallback):
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _candidates_dir(store: Store, bot_id: str) -> Path:
    return store.skills_dir / "_candidates" / bot_id


def _meta_path(store: Store, slug: str) -> Path:
    return store.skills_dir / "_meta" / f"{slug}.json"


def _stats_path(store: Store, slug: str) -> Path:
    return store.skills_dir / "_stats" / f"{slug}.json"


def _inbox_path(store: Store, bot_id: str) -> Path:
    return store.root / "bots" / bot_id / "learning" / "inbox.json"


def _lessons_path(store: Store, bot_id: str) -> Path:
    return store.root / "bots" / bot_id / "learning" / "lessons.json"


def note_ledger(store: Store, *, kind: str, key: str, previous: str, bot_id: str = "") -> dict:
    """Store the previous bytes by hash. The same text is not copied twice."""
    text = previous or ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    blob = store.skills_dir / "_ledger" / "blobs" / digest
    blob.parent.mkdir(parents=True, exist_ok=True)
    if not blob.is_file():
        blob.write_text(text, encoding="utf-8")
    index_path = store.skills_dir / "_ledger" / "index.json"
    rows = _read_json(index_path, [])
    if not isinstance(rows, list):
        rows = []
    row = {
        "id": new_id(),
        "kind": kind,
        "key": key,
        "sha": digest,
        "bot_id": bot_id or "",
        "created_at": now_iso(),
    }
    rows.append(row)
    _write_json(index_path, rows[-LEDGER_CAP:])
    return row


def ledger_rows(store: Store, bot_id: str | None = None) -> list[dict]:
    rows = _read_json(store.skills_dir / "_ledger" / "index.json", [])
    if not isinstance(rows, list):
        return []
    if not bot_id:
        return list(rows)
    kept = []
    for row in rows:
        if row.get("kind") == "skill" or row.get("bot_id") == bot_id:
            kept.append(row)
    return kept


def rollback(store: Store, ledger_id: str) -> dict:
    """Put back the snapshotted skill or memory file. Nothing else is rewritten."""
    rows = _read_json(store.skills_dir / "_ledger" / "index.json", [])
    row = next((item for item in rows if item.get("id") == ledger_id), None)
    if row is None:
        raise StoreError("That backup is not there.", 404)
    blob = store.skills_dir / "_ledger" / "blobs" / str(row.get("sha") or "")
    if not blob.is_file():
        raise StoreError("That backup is not there.", 404)
    text = blob.read_text(encoding="utf-8")
    if row.get("kind") == "skill":
        slug = slugify(str(row.get("key") or ""))
        if not slug:
            raise StoreError("That backup is not a skill.", 400)
        path = store.skills_dir / f"{slug}.md"
        if text.strip():
            path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        elif path.is_file():
            path.unlink()
        _mark_rolled(store, ledger_id)
        return {"kind": "skill", "key": slug, "restored": True}
    if row.get("kind") == "memory":
        bot_id, _, slug = str(row.get("key") or "").partition("/")
        if not bot_id or not slug:
            raise StoreError("That backup is not a memory file.", 400)
        path = store.root / "bots" / bot_id / "memory" / f"{slug}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        if text.strip():
            path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        elif path.is_file():
            path.unlink()
        _mark_rolled(store, ledger_id)
        return {"kind": "memory", "key": row.get("key"), "restored": True}
    if row.get("kind") == "notes":
        bot_id = str(row.get("key") or "")
        if not bot_id:
            raise StoreError("That backup is not a note.", 400)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StoreError("That backup is not a note.", 400) from exc
        if not isinstance(payload, dict):
            raise StoreError("That backup is not a note.", 400)
        from easyagent.journal import NOTE_NAMES

        folder = store.root / "bots" / bot_id / "notes"
        folder.mkdir(parents=True, exist_ok=True)
        for name, body in payload.items():
            if name not in NOTE_NAMES or not isinstance(body, str):
                continue
            path = folder / name
            if body.strip():
                path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
            elif path.is_file():
                path.unlink()
        _mark_rolled(store, ledger_id)
        return {"kind": "notes", "key": bot_id, "restored": True}
    raise StoreError("That backup is not a skill or a memory file.", 400)


def rollback_for_bot(store: Store, bot_id: str, ledger_id: str) -> dict:
    """Roll back one snapshot that belongs to this bot's history."""
    rows = ledger_rows(store, bot_id)
    if not any(item.get("id") == ledger_id for item in rows):
        raise StoreError("That backup is not there.", 404)
    return rollback(store, ledger_id)


def rollback_latest(store: Store, bot_id: str) -> dict:
    rows = [row for row in ledger_rows(store, bot_id) if not row.get("rolled")]
    if not rows:
        raise StoreError("There is nothing to roll back.", 404)
    return rollback(store, rows[-1]["id"])


def mark_origin(
    store: Store,
    slug: str,
    origin: str,
    *,
    model: str = "",
    connection: str = "",
    bot_id: str = "",
) -> None:
    if origin not in {"user", "learned"}:
        return
    _write_json(
        _meta_path(store, slug),
        {
            "origin": origin,
            "model": model,
            "connection": connection,
            "bot_id": bot_id,
            "written_at": now_iso(),
        },
    )


def skill_meta(store: Store, slug: str) -> dict:
    data = _read_json(_meta_path(store, slug), {})
    return data if isinstance(data, dict) else {}


def is_user_skill(store: Store, slug: str) -> bool:
    """A file the person saved, or any live skill this module did not mark."""
    meta = skill_meta(store, slug)
    if meta.get("origin") == "learned":
        return False
    if meta.get("origin") == "user":
        return True
    return (store.skills_dir / f"{slug}.md").is_file()


def read_stats(store: Store, slug: str) -> dict:
    data = _read_json(_stats_path(store, slug), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("uses", 0)
    data.setdefault("passes", 0)
    data.setdefault("fails", 0)
    data.setdefault("archived", False)
    return data


def note_outcome(store: Store, slug: str, ok: bool, source: str) -> dict:
    """Count one measured result. A skill you wrote is never archived."""
    stats = read_stats(store, slug)
    stats["uses"] = int(stats.get("uses") or 0) + 1
    if ok:
        stats["passes"] = int(stats.get("passes") or 0) + 1
    else:
        stats["fails"] = int(stats.get("fails") or 0) + 1
    stats["last_source"] = source
    stats["updated_at"] = now_iso()
    _write_json(_stats_path(store, slug), stats)
    if (
        not ok
        and not is_user_skill(store, slug)
        and int(stats["fails"]) >= FAILS_BEFORE_ARCHIVE
        and int(stats["passes"]) == 0
    ):
        _archive_skill(store, slug)
        stats["archived"] = True
    return stats


def credit_named(store: Store, text: str, ok: bool, source: str) -> None:
    """Count a result only for a learned skill named in the text."""
    blob = text or ""
    for skill in store.list_skills():
        name = str(skill.get("name") or "")
        if not name or is_user_skill(store, name):
            continue
        if re.search(rf"(?<![A-Za-z0-9-]){re.escape(name)}(?![A-Za-z0-9-])", blob):
            note_outcome(store, name, ok, source)


def _archive_skill(store: Store, slug: str) -> None:
    source = store.skills_dir / f"{slug}.md"
    if not source.is_file():
        return
    dest = store.skills_dir / "_archive" / f"{slug}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    source.unlink()
    print(f"heuristic learn: {slug} kept failing, so it left the live skills", flush=True)


def rank_skills(store: Store, skills: list[dict]) -> list[dict]:
    """Skills you wrote stay first. Learned skills follow their measured pass rate."""

    def key(skill: dict) -> tuple:
        slug = skill.get("name") or ""
        stats = read_stats(store, slug)
        uses = int(stats.get("uses") or 0)
        passes = int(stats.get("passes") or 0)
        rate = (passes / uses) if uses else 0.0
        return (0 if is_user_skill(store, slug) else 1, -rate, slug)

    return sorted(skills, key=key)


def lint_candidate(data: dict) -> list[str]:
    """Tools, command shape, and the size cap. A grade is not one of these."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["The proposal was not an object."]
    slug = slugify(str(data.get("name") or ""))
    if not slug or slug in RESERVED_SLUGS:
        problems.append("The name is not a short slug.")
    trigger = " ".join(str(data.get("trigger") or "").split())
    if not trigger or len(trigger) > 200:
        problems.append("The trigger is missing or too long.")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > STEP_CAP:
        problems.append("Steps must be a short list.")
    else:
        for step in steps:
            text = " ".join(str(step).split())
            if not text or len(text) > 200:
                problems.append("A step is empty or too long.")
            for name in _TOOL_REF.findall(text):
                if name not in KNOWN_TOOLS:
                    problems.append(f"The tool `{name}` is not on this computer.")
    pitfalls = data.get("pitfalls")
    if not isinstance(pitfalls, list) or len(pitfalls) > PITFALL_CAP:
        problems.append("Pitfalls must be a short list.")
    scope = " ".join(str(data.get("scope") or "").split())
    if not scope or len(scope) > 200:
        problems.append("Scope is missing or too long.")
    check = data.get("check")
    if not isinstance(check, dict):
        problems.append("The check is missing.")
    else:
        kind = check.get("kind")
        if kind not in {"command", "file", "regex"}:
            problems.append("The check must be a command, a file, or a regex.")
        command = str(check.get("command") or "")
        if kind in {"command", "regex"}:
            if not command or len(command) > COMMAND_CAP or "\n" in command or "\x00" in command:
                problems.append("The command is missing or too long.")
            else:
                try:
                    argv = shlex.split(command)
                except ValueError:
                    argv = []
                    problems.append("The command does not parse.")
                if not argv and "does not parse" not in " ".join(problems):
                    problems.append("The command does not parse.")
        if kind == "file":
            path = str(check.get("path") or "")
            if not path or path.startswith("/") or ".." in Path(path).parts:
                problems.append("The file check needs a relative path.")
        if kind == "regex" and not str(check.get("pattern") or "").strip():
            problems.append("The regex check needs a pattern.")
    body = render_body(data) if not problems else ""
    if body and len(body) > BODY_CAP:
        problems.append("The skill is over the size cap.")
    return problems


def render_body(data: dict) -> str:
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    pitfalls = data.get("pitfalls") if isinstance(data.get("pitfalls"), list) else []
    lines = [" ".join(str(data.get("trigger") or "").split()), "", "Steps:"]
    for step in steps:
        lines.append(f"- {' '.join(str(step).split())}")
    if pitfalls:
        lines.append("")
        lines.append("Pitfalls:")
        for item in pitfalls:
            lines.append(f"- {' '.join(str(item).split())}")
    lines.append("")
    lines.append("Scope: " + " ".join(str(data.get("scope") or "").split()))
    return "\n".join(lines).strip()


def merge_text(body: str, extra: str, cap: int = BODY_CAP) -> str | None:
    """Append one line. None when the skill would grow past the cap."""
    line = " ".join((extra or "").split())
    if not line:
        return body
    current = (body or "").rstrip()
    nxt = f"{current}\n- {line}" if current else f"- {line}"
    if len(nxt) > cap:
        print("heuristic learn: the skill would pass its cap, so the edit is refused", flush=True)
        return None
    return nxt


def run_check(check: dict, *, cwd: Path) -> tuple[bool, str]:
    """Run the candidate's own check. The model's opinion is not consulted."""
    kind = (check or {}).get("kind")
    cwd.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        raw = str(check.get("path") or "")
        path = (cwd / raw).resolve()
        if cwd.resolve() not in path.parents and path != cwd.resolve():
            return False, "The file check left the workspace."
        exists = path.is_file()
        if check.get("exists") is False:
            return (not exists, "The file is still there." if exists else "The file is absent.")
        if not exists:
            return False, "The file is not there."
        contains = str(check.get("contains") or "")
        if contains and contains not in path.read_text(encoding="utf-8", errors="replace"):
            return False, "The file does not contain the expected text."
        return True, "The file check passed."
    command = str((check or {}).get("command") or "")
    try:
        argv = shlex.split(command)
    except ValueError:
        return False, "The command does not parse."
    if not argv:
        return False, "The command was empty."
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"The command did not finish: {exc}"
    output = (proc.stdout or "") + (proc.stderr or "")
    if kind == "regex":
        pattern = str(check.get("pattern") or "")
        try:
            matched = re.search(pattern, output) is not None
        except re.error:
            return False, "The pattern is not a regex."
        if not matched:
            return False, "The output did not match."
        return True, "The output matched."
    expected = check.get("exit_code")
    try:
        expected_code = int(expected if expected is not None else 0)
    except (TypeError, ValueError):
        expected_code = 0
    if proc.returncode != expected_code:
        return False, f"The command exited {proc.returncode}, expected {expected_code}."
    return True, "The command check passed."


def _mark_rolled(store: Store, ledger_id: str) -> None:
    index_path = store.skills_dir / "_ledger" / "index.json"
    rows = _read_json(index_path, [])
    if not isinstance(rows, list):
        return
    for row in rows:
        if isinstance(row, dict) and row.get("id") == ledger_id:
            row["rolled"] = True
    _write_json(index_path, rows)


def _candidate_path(store: Store, bot_id: str, candidate_id: str) -> Path:
    return _candidates_dir(store, bot_id) / f"{candidate_id}.json"


def list_candidates(store: Store, bot_id: str, status: str | None = None) -> list[dict]:
    folder = _candidates_dir(store, bot_id)
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.json")):
        data = _read_json(path, None)
        if isinstance(data, dict) and (status is None or data.get("status") == status):
            rows.append(data)
    return rows


def save_candidate(store: Store, bot_id: str, data: dict, *, source: dict | None = None) -> dict | None:
    """Lint first. A bad proposal is not stored and never becomes a live skill."""
    problems = lint_candidate(data)
    if problems:
        print("heuristic learn: the proposal failed the linter, so it stays off the live skills", flush=True)
        return None
    slug = slugify(str(data.get("name") or "")) or ""
    record = {
        "id": new_id(),
        "bot_id": bot_id,
        "name": slug,
        "trigger": " ".join(str(data.get("trigger") or "").split()),
        "steps": [" ".join(str(item).split()) for item in data.get("steps") or []],
        "pitfalls": [" ".join(str(item).split()) for item in data.get("pitfalls") or []],
        "scope": " ".join(str(data.get("scope") or "").split()),
        "check": data.get("check") or {},
        "replay_without": data.get("replay_without") or "",
        "replay_with": data.get("replay_with") or "",
        "status": "candidate",
        "reason": "",
        "quarantine": bool((source or {}).get("quarantine")),
        "source": source or {},
        "created_at": now_iso(),
    }
    record["body"] = render_body(record)
    _write_json(_candidate_path(store, bot_id, record["id"]), record)
    return record


def _update_candidate(store: Store, record: dict) -> dict:
    _write_json(_candidate_path(store, record["bot_id"], record["id"]), record)
    return record


def observe(
    store: Store,
    bot_id: str,
    text: str,
    *,
    reason: str,
    task: str = "",
    quarantine: bool = False,
) -> dict | None:
    """Append a note for later. The live skill is not opened."""
    line = " ".join((text or "").split())
    if not line:
        return None
    path = _inbox_path(store, bot_id)
    rows = _read_json(path, [])
    if not isinstance(rows, list):
        rows = []
    row = {
        "id": new_id(),
        "text": line[:300],
        "reason": reason,
        "task": task[:400],
        "task_type": task_type(task or line),
        "quarantine": quarantine,
        "status": "open",
        "created_at": now_iso(),
    }
    rows.append(row)
    _write_json(path, rows[-INBOX_CAP:])
    return row


def open_observations(store: Store, bot_id: str) -> list[dict]:
    rows = _read_json(_inbox_path(store, bot_id), [])
    if not isinstance(rows, list):
        return []
    return [item for item in rows if item.get("status") == "open"]


def merge_inbox(store: Store, bot_id: str, slug: str) -> dict:
    """Fold open notes into one learned skill, and stop at the size cap."""
    if is_user_skill(store, slug):
        return {"merged": 0, "reason": "A skill you wrote is left as it is."}
    path = store.skills_dir / f"{slug}.md"
    parsed = parse_skill_document(path.read_text(encoding="utf-8")) if path.is_file() else None
    if not parsed:
        return {"merged": 0, "reason": "There is no learned skill to merge into."}
    body = parsed.get("body") or ""
    rows = _read_json(_inbox_path(store, bot_id), [])
    merged = 0
    for item in rows:
        if not isinstance(item, dict) or item.get("status") != "open" or item.get("quarantine"):
            continue
        nxt = merge_text(body, item.get("text") or "")
        if nxt is None:
            break
        body = nxt
        item["status"] = "merged"
        merged += 1
    if merged:
        store.save_skill({"name": slug, "description": parsed.get("description") or "", "body": body})
        mark_origin(store, slug, "learned", bot_id=bot_id)
        _write_json(_inbox_path(store, bot_id), rows)
    return {"merged": merged, "body": body}


def _merge_open_notes(store: Store, bot_id: str) -> None:
    """Fold inbox notes into one learned skill. A skill you wrote is skipped."""
    slug = "verified-notes"
    path = store.skills_dir / f"{slug}.md"
    if path.is_file() and not is_user_skill(store, slug):
        merge_inbox(store, bot_id, slug)
        return
    for skill in store.list_skills():
        name = str(skill.get("name") or "")
        if name and not is_user_skill(store, name):
            merge_inbox(store, bot_id, name)
            return


def lessons(store: Store, bot_id: str) -> list[dict]:
    rows = _read_json(_lessons_path(store, bot_id), [])
    return rows if isinstance(rows, list) else []


def _write_lessons(store: Store, bot_id: str, rows: list[dict]) -> None:
    _write_json(_lessons_path(store, bot_id), rows[-40:])


def note_failed_check(store: Store, bot_id: str, request: str, problem: str, *, quarantine: bool = False) -> str:
    """A short lesson after a failed check. A second failure expires it."""
    kind = task_type(request)
    rows = lessons(store, bot_id)
    for item in rows:
        if item.get("status") == "active" and _similar(item.get("task_type") or "", kind):
            item["status"] = "expired"
            _write_lessons(store, bot_id, rows)
            print("heuristic learn: the retry failed, so the lesson expired", flush=True)
            return ""
    text = " ".join((problem or "The check failed.").split())[:180]
    from easyagent.safety import lesson_weakens

    if lesson_weakens(text):
        print("heuristic learn: a lesson that would weaken the guardrails was rejected", flush=True)
        return ""
    rows.append(
        {
            "id": new_id(),
            "task_type": kind,
            "text": text,
            "status": "active",
            "quarantine": quarantine,
            "created_at": now_iso(),
        }
    )
    _write_lessons(store, bot_id, rows)
    if quarantine:
        return ""
    return f"Learned: {text}"


def note_check_passed(store: Store, bot_id: str, request: str) -> str:
    """A lesson whose retry passed becomes a note on a learned skill, under the cap."""
    kind = task_type(request)
    rows = lessons(store, bot_id)
    hit = None
    for item in rows:
        if item.get("status") == "active" and not item.get("quarantine") and _similar(item.get("task_type") or "", kind):
            hit = item
            break
    if hit is None:
        return ""
    slug = "verified-notes"
    if is_user_skill(store, slug):
        hit["status"] = "kept"
        _write_lessons(store, bot_id, rows)
        return ""
    path = store.skills_dir / f"{slug}.md"
    if path.is_file():
        parsed = parse_skill_document(path.read_text(encoding="utf-8")) or {"description": "Notes that passed a retry.", "body": ""}
        body = parsed.get("body") or ""
        description = parsed.get("description") or "Notes that passed a retry."
    else:
        body = "Notes that a later try confirmed."
        description = "Notes that passed a retry."
    from easyagent.safety import lesson_weakens

    if lesson_weakens(hit.get("text") or ""):
        hit["status"] = "rejected"
        _write_lessons(store, bot_id, rows)
        return ""
    nxt = merge_text(body, hit.get("text") or "")
    if nxt is None:
        hit["status"] = "capped"
        _write_lessons(store, bot_id, rows)
        return ""
    store.save_skill({"name": slug, "description": description, "body": nxt})
    mark_origin(store, slug, "learned", bot_id=bot_id)
    hit["status"] = "promoted"
    _write_lessons(store, bot_id, rows)
    return f"Learned: {hit.get('text') or ''}"


def lesson_block(store: Store, bot_id: str, request: str) -> str:
    kind = task_type(request)
    lines = []
    for item in lessons(store, bot_id):
        if item.get("status") != "active" or item.get("quarantine"):
            continue
        if _similar(item.get("task_type") or "", kind):
            lines.append(f"- {item.get('text') or ''}")
    if not lines:
        return ""
    return "Lessons from a checked failure:\n" + "\n".join(lines[:4])


def approve_lesson(store: Store, bot_id: str, lesson_id: str) -> dict | None:
    rows = lessons(store, bot_id)
    for item in rows:
        if item.get("id") == lesson_id:
            item["quarantine"] = False
            _write_lessons(store, bot_id, rows)
            return item
    return None


def chats_active(store: Store) -> bool:
    """True when any saved chat is still in a run. Replay stays idle then."""
    if not store.bots_dir.is_dir():
        return False
    for bot_path in store.bots_dir.iterdir():
        chats = bot_path / "chats"
        if not chats.is_dir():
            continue
        for path in chats.glob("*.json"):
            try:
                chat = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            run = chat.get("run") if isinstance(chat.get("run"), dict) else {}
            if run.get("status") == "running":
                return True
    return False


def bot_paused(bot: dict) -> bool:
    return bot.get("learn_paused") is True


def bot_manual(bot: dict) -> bool:
    return bot.get("learn_manual") is True


def _raise_if_idle_broken(store: Store) -> None:
    if stopped():
        raise LearningStopped()
    if chats_active(store):
        print("heuristic learn: a chat is running, so replay waits", flush=True)
        raise LearningStopped()


async def replay_candidate(store: Store, bot: dict, candidate: dict, *, runs: int = 3, mock: bool = False) -> dict:
    """Run the task with and without the candidate. Promote only if it does not lose."""
    from easyagent.evals.runner import run_suite

    if candidate.get("quarantine"):
        candidate["status"] = "rejected"
        candidate["reason"] = "It came from another bot or a room, and it is not approved."
        return _update_candidate(store, candidate)
    _raise_if_idle_broken(store)
    with tempfile.TemporaryDirectory(prefix="easyagent-check-") as temp_name:
        ok, detail = run_check(candidate.get("check") or {}, cwd=Path(temp_name))
    if not ok:
        print("heuristic learn: the check failed, so the candidate stays off the live skills", flush=True)
        candidate["status"] = "rejected"
        candidate["reason"] = detail
        return _update_candidate(store, candidate)
    without_id = candidate.get("replay_without") or ""
    with_id = candidate.get("replay_with") or ""
    origin_task = None
    if not without_id or not with_id:
        prompt = " ".join(str((candidate.get("source") or {}).get("task") or candidate.get("trigger") or "").split())
        if not prompt:
            candidate["status"] = "rejected"
            candidate["reason"] = "There is no task to replay."
            return _update_candidate(store, candidate)
        origin_task = {
            "id": "origin",
            "category": "learn",
            "prompt": prompt,
            "criteria": [
                {
                    "kind": "llm_judge",
                    "rubric": "The reply follows this trigger: " + str(candidate.get("trigger") or ""),
                }
            ],
        }
    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except Exception:
        endpoint = None
    connection = (endpoint or {}).get("name") or ""
    without_passes = 0
    with_passes = 0
    skill = {
        "name": candidate["name"],
        "description": candidate.get("trigger") or "",
        "body": candidate.get("body") or render_body(candidate),
    }

    async def _once(task_ids: list[str] | None, preload: dict | None) -> dict:
        with tempfile.TemporaryDirectory(prefix="easyagent-learn-out-") as out:
            kwargs = {
                "mock": mock,
                "data_dir": store.root,
                "out_dir": Path(out),
                "connection": "" if mock else connection,
            }
            if task_ids:
                kwargs["task_ids"] = task_ids
            else:
                kwargs["tasks"] = [origin_task]
            if preload:
                kwargs["preload_skill"] = preload
            return await asyncio.to_thread(lambda: run_suite(**kwargs))

    for _ in range(max(1, runs)):
        _raise_if_idle_broken(store)
        without = await _once([without_id] if without_id else None, None)
        _raise_if_idle_broken(store)
        held = await _once([with_id] if with_id else None, skill)
        if _rate(without) > 0:
            without_passes += 1
        if _rate(held) > 0:
            with_passes += 1
    without_rate = without_passes / max(1, runs)
    with_rate = with_passes / max(1, runs)
    if with_rate < without_rate or with_rate <= 0:
        print("heuristic learn: replay did not raise the pass rate, so the candidate is dropped", flush=True)
        candidate["status"] = "rejected"
        candidate["reason"] = f"Replay pass rate {with_rate:.0%} did not beat {without_rate:.0%}."
        return _update_candidate(store, candidate)
    if is_user_skill(store, candidate["name"]):
        candidate["status"] = "rejected"
        candidate["reason"] = "A skill you wrote is left as it is."
        return _update_candidate(store, candidate)
    if bot_manual(bot):
        candidate["status"] = "ready"
        candidate["reason"] = "Replay passed. It is waiting for approval."
        candidate["with_rate"] = with_rate
        candidate["without_rate"] = without_rate
        return _update_candidate(store, candidate)
    return _promote(store, bot, candidate, with_rate, without_rate)


def _rate(report: dict) -> float:
    summary = report.get("summary") or {}
    total = int(summary.get("total") or 0)
    passed = int(summary.get("passed") or 0)
    if total <= 0:
        return 0.0
    return passed / total


def _promote(store: Store, bot: dict, candidate: dict, with_rate: float, without_rate: float) -> dict:
    source = candidate.get("source") or {}
    store.save_skill(
        {
            "name": candidate["name"],
            "description": candidate.get("trigger") or "",
            "body": candidate.get("body") or render_body(candidate),
        }
    )
    mark_origin(
        store,
        candidate["name"],
        "learned",
        model=str(source.get("model") or bot.get("model") or ""),
        connection=str(source.get("connection") or ""),
        bot_id=bot.get("id") or "",
    )
    stats = read_stats(store, candidate["name"])
    stats["model"] = str(source.get("model") or bot.get("model") or "")
    stats["connection"] = str(source.get("connection") or "")
    stats["written_at"] = now_iso()
    _write_json(_stats_path(store, candidate["name"]), stats)
    candidate["status"] = "promoted"
    candidate["reason"] = f"Replay pass rate {with_rate:.0%} held against {without_rate:.0%}, and the check passed."
    candidate["with_rate"] = with_rate
    candidate["without_rate"] = without_rate
    return _update_candidate(store, candidate)


def approve_candidate(store: Store, bot: dict, candidate_id: str) -> dict:
    found = next((item for item in list_candidates(store, bot["id"]) if item.get("id") == candidate_id), None)
    if found is None:
        raise StoreError("That candidate is not there.", 404)
    if found.get("status") != "ready":
        raise StoreError("That candidate has not passed a replay.", 400)
    if is_user_skill(store, found["name"]):
        raise StoreError("A skill you wrote is left as it is.", 400)
    return _promote(store, bot, found, float(found.get("with_rate") or 1), float(found.get("without_rate") or 0))


def reject_candidate(store: Store, bot: dict, candidate_id: str) -> dict:
    """Turn a waiting candidate down. It is not installed."""
    found = next((item for item in list_candidates(store, bot["id"]) if item.get("id") == candidate_id), None)
    if found is None:
        raise StoreError("That candidate is not there.", 404)
    if found.get("status") not in {"candidate", "ready"}:
        raise StoreError("That candidate is not waiting.", 400)
    found["status"] = "rejected"
    found["reason"] = "You rejected it."
    return _update_candidate(store, found)


async def propose_json(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    notes: str,
    timeout: float | None = None,
    yield_to_chats: bool = False,
) -> dict | None:
    """One proposal from this bot's model. Tools are never sent with the schema."""
    from easyagent import llm

    messages = [
        {
            "role": "system",
            "content": (
                "Propose one skill as JSON. Name only these tools in backticks when a step needs one: "
                + ", ".join(sorted(KNOWN_TOOLS))
                + ". The check is a command with an exit code, a relative file assertion, or a regex on command output. "
                "Do not say the skill is already saved."
            ),
        },
        {"role": "user", "content": notes[:4000]},
    ]
    call = {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "messages": messages,
        "tools": False,
        "yield_to_chats": yield_to_chats,
    }
    if timeout is not None:
        call["timeout"] = timeout
    try:
        text = await llm.complete(**call, response_schema=CANDIDATE_SCHEMA)
    except llm.YieldLater:
        return None
    except llm.ProviderError:
        try:
            text = await llm.complete(**call)
        except (llm.ProviderError, llm.YieldLater):
            return None
    return _parse_proposal(text)


async def propose_from_signal(
    store: Store,
    bot: dict,
    notes: str,
    *,
    reason: str,
    task: str = "",
    timeout: float | None = None,
    quarantine: bool = False,
) -> dict | None:
    """Ask this bot's model for one skill and save it as a candidate. Replay still decides."""
    if os.environ.get("EASYAGENT_LEARN") == "0":
        return None
    if not isinstance(bot, dict) or bot_paused(bot):
        return None
    notes = " ".join((notes or "").split())
    if len(notes) < 8:
        return None
    try:
        from easyagent.journal import scrub_text

        notes = scrub_text(store, notes)
    except Exception:
        pass
    fingerprint = notes[:180]
    for item in list_candidates(store, bot["id"], status="candidate"):
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        if source.get("reason") == reason and source.get("notes") == fingerprint:
            return item
    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except Exception:
        endpoint = None
    if not endpoint:
        return None
    data = await propose_json(
        base_url=endpoint["base_url"],
        api_key=endpoint.get("api_key") or None,
        model=bot.get("model") or endpoint.get("model"),
        notes=notes[:4000],
        timeout=timeout,
        yield_to_chats=timeout is not None,
    )
    if not isinstance(data, dict):
        return None
    return save_candidate(
        store,
        bot["id"],
        data,
        source={
            "reason": reason,
            "notes": fingerprint,
            "task": " ".join((task or "").split())[:400],
            "model": str(bot.get("model") or endpoint.get("model") or ""),
            "connection": str(endpoint.get("name") or ""),
            "quarantine": quarantine,
        },
    )


def _parse_proposal(text: str) -> dict | None:
    from easyagent.llm import extract_json_text

    raw = extract_json_text(text)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def sleep_once(store: Store, *, mock: bool = False, runs: int = 3, bot_id: str | None = None) -> dict:
    """Replay waiting candidates while no chat is running. Stop ends the pass."""
    if stopped():
        clear_stop()
        return {"status": "stopped", "promoted": [], "rejected": [], "reason": "Stop ended the pass."}
    if chats_active(store):
        print("heuristic learn: a chat is running, so replay waits", flush=True)
        return {"status": "idle", "promoted": [], "rejected": [], "reason": "A chat is running."}
    bots = []
    for bot in store.list_bots():
        if bot_id and bot.get("id") != bot_id:
            continue
        bots.append(bot)
    promoted = []
    rejected = []
    try:
        for bot in bots:
            if bot_paused(bot):
                continue
            _raise_if_idle_broken(store)
            try:
                _merge_open_notes(store, bot["id"])
            except Exception:
                pass
            for candidate in list_candidates(store, bot["id"], status="candidate"):
                updated = await replay_candidate(store, bot, candidate, runs=runs, mock=mock)
                if updated.get("status") == "promoted":
                    promoted.append(updated["name"])
                elif updated.get("status") == "rejected":
                    rejected.append({"name": updated.get("name"), "reason": updated.get("reason") or ""})
    except LearningStopped:
        status = "stopped" if stopped() else "idle"
        reason = "Stop ended the pass." if stopped() else "A chat is running."
        clear_stop()
        return {"status": status, "promoted": promoted, "rejected": rejected, "reason": reason}
    return {"status": "done", "promoted": promoted, "rejected": rejected, "reason": ""}


def panel(store: Store, bot: dict) -> dict:
    bot_id = bot["id"]
    rows = list_candidates(store, bot_id)
    skills = []
    for skill in rank_skills(store, store.list_skills()):
        stats = read_stats(store, skill["name"])
        uses = int(stats.get("uses") or 0)
        passes = int(stats.get("passes") or 0)
        skills.append(
            {
                "name": skill["name"],
                "origin": "user" if is_user_skill(store, skill["name"]) else "learned",
                "uses": uses,
                "passes": passes,
                "fails": int(stats.get("fails") or 0),
                "rate": (passes / uses) if uses else None,
                "model": stats.get("model") or skill_meta(store, skill["name"]).get("model") or "",
                "connection": stats.get("connection") or skill_meta(store, skill["name"]).get("connection") or "",
                "written_at": stats.get("written_at") or skill_meta(store, skill["name"]).get("written_at") or "",
                "archived": False,
            }
        )
    archive = store.skills_dir / "_archive"
    if archive.is_dir():
        for path in sorted(archive.glob("*.md")):
            skills.append(
                {
                    "name": path.stem,
                    "origin": "learned",
                    "uses": int(read_stats(store, path.stem).get("uses") or 0),
                    "passes": int(read_stats(store, path.stem).get("passes") or 0),
                    "fails": int(read_stats(store, path.stem).get("fails") or 0),
                    "rate": None,
                    "model": "",
                    "connection": "",
                    "written_at": "",
                    "archived": True,
                }
            )
    payload = {
        "paused": bot_paused(bot),
        "manual": bot_manual(bot),
        "waiting": [item for item in rows if item.get("status") in {"candidate", "ready"}],
        "promoted": [item for item in rows if item.get("status") == "promoted"],
        "rejected": [item for item in rows if item.get("status") == "rejected"],
        "skills": skills,
        "lessons": lessons(store, bot_id),
        "ledger": list(reversed(ledger_rows(store, bot_id)))[:12],
    }
    try:
        from easyagent.journal import learning_extra

        payload.update(learning_extra(store, bot))
    except Exception:
        pass
    return payload


async def learn_loop(store: Store, stop: asyncio.Event) -> None:
    """Wait, then replay while idle. Tests set EASYAGENT_LEARN=0 so this stays quiet."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=45)
        return
    except asyncio.TimeoutError:
        pass
    while not stop.is_set():
        if os.environ.get("EASYAGENT_LEARN") != "0":
            try:
                await sleep_once(store, mock=False, runs=3)
            except Exception:
                pass
        if os.environ.get("EASYAGENT_ROLLING") != "0":
            try:
                from easyagent.rolling import refresh_idle

                await refresh_idle(store)
            except Exception:
                pass
        if os.environ.get("EASYAGENT_NIGHTLY") != "0":
            try:
                from easyagent.journal import nightly_idle

                await nightly_idle(store)
            except Exception:
                pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=45)
        except asyncio.TimeoutError:
            continue


def set_paused(store: Store, bot_id: str, paused: bool) -> dict:
    return store.update_bot(bot_id, learn_paused=paused, learn_paused_set=True)


def set_manual(store: Store, bot_id: str, manual: bool) -> dict:
    return store.update_bot(bot_id, learn_manual=manual, learn_manual_set=True)
