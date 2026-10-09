"""Eight notes a bot keeps for itself, written only while it is idle.

The pass uses that bot's own connection. It does not add a model, and it
does not run while a chat is active. User text outside the auto block is
copied through unchanged. A secret is scrubbed before a note is written.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from easyagent import llm
from easyagent import tools as tools_mod
from easyagent.retrieve import indexed_message_ids, tokens
from easyagent.store import Store, StoreError, atomic_write_text, new_id

NOTE_CAP = 4000
NOTE_FILES: tuple[tuple[str, str, str], ...] = (
    ("MISTAKES.md", "Mistakes", "What went wrong, the cause, and the fix that worked."),
    ("PROMISES.md", "Promises", "Commitments and steps that are still open."),
    ("UNKNOWNS.md", "Unknowns", "Questions and assumptions still open."),
    ("PREDICTIONS.md", "Predictions", "What it expected, and how that scored."),
    ("HABITS.md", "Habits", "Repeated patterns. A routine is only a suggestion until you approve it."),
    ("PLAYBOOK.md", "Playbook", "Recipes made only from steps that already worked."),
    ("WORLD.md", "World", "Machines, connections, and when they were last seen. No secrets."),
    ("DREAMS.md", "Dreams", "Ideas for later. Nothing here runs on its own."),
)
NOTE_NAMES = frozenset(name for name, _title, _blurb in NOTE_FILES)
KIND_FILE = {
    "mistake": "MISTAKES.md",
    "promise": "PROMISES.md",
    "unknown": "UNKNOWNS.md",
    "prediction": "PREDICTIONS.md",
    "habit": "HABITS.md",
    "playbook": "PLAYBOOK.md",
    "world": "WORLD.md",
    "dream": "DREAMS.md",
}
FILE_KIND = {name: kind for kind, name in ((kind, path) for kind, path in KIND_FILE.items())}

AUTO_OPEN = "<!-- ea:auto -->"
AUTO_CLOSE = "<!-- /ea:auto -->"
_ENTRY = re.compile(r"<!-- ea:entry ([^>]*?) -->(.*?)<!-- /ea:entry -->", re.S)
_USER = re.compile(r"<!-- ea:user -->.*?<!-- /ea:user -->", re.S)
_SCORE = re.compile(r"<!-- ea:score [0-9.]+ \d+ -->")
_BLOCK = re.compile(
    r"<!-- ea:user -->.*?<!-- /ea:user -->|<!-- ea:entry [^>]*?-->.*?<!-- /ea:entry -->|<!-- ea:score [0-9.]+ \d+ -->",
    re.S,
)
_CITE = re.compile(r"\[m:([A-Za-z0-9_-]+)\]")
_LONG = re.compile(r"[a-z0-9]{6,}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LEAK = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{8,}"
    r"|pk-[A-Za-z0-9_-]{8,}"
    r"|ghp_[A-Za-z0-9]{8,}"
    r"|github_pat_[A-Za-z0-9_]{8,}"
    r"|xox[abprs]-[A-Za-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|Bearer\s+[A-Za-z0-9._-]{12,}"
    r"|(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s\]]{8,}"
    r")"
)
_GENERIC = frozenset("please check later still around expected actual would could should there their about".split())
_PROMISE = re.compile(
    r"(?i)\b(?:i'll|i will)\s+(?:check|look|finish|get back|come back|do that|handle|verify|read|update)\b[^.!\n]{0,160}"
)
_EXPECT = re.compile(
    r"(?i)\b(?:i expect|i predict|my guess is|i think it will)\b[^.!\n]{0,220}"
)
_ATTR = re.compile(r"([a-z]+)=([^\s]+)")
_HHMM = re.compile(r"^(\d{2}):(\d{2})$")

DEFAULTS = {
    "window_start": "02:00",
    "window_end": "05:00",
    "idle_minutes": 30,
    "retain_days": 30,
    "keep_forever": False,
    "pruning": True,
}
LOG_SECTIONS = 40
_BLOCK_CHARS = 1200


@dataclass
class Entry:
    kind: str
    title: str
    body: str
    cites: list[str]
    dates: list[str]
    status: str = ""
    score: str = ""
    source: str = "derived"
    hour: str = ""
    entry_id: str = ""
    steps: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.entry_id:
            self.entry_id = new_id()


def notes_dir(store: Store, bot_id: str) -> Path:
    return store._bot_dir(bot_id) / "notes"


def _read(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data


def parse_when(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_retention(store: Store, bot_id: str) -> dict:
    path = notes_dir(store, bot_id) / "retention.json"
    raw = _read_json(path, {})
    settings = dict(DEFAULTS)
    if isinstance(raw, dict):
        for key in DEFAULTS:
            if key in raw:
                settings[key] = raw[key]
    settings["idle_minutes"] = _clamp_int(settings.get("idle_minutes"), 1, 1440, 30)
    settings["retain_days"] = _clamp_int(settings.get("retain_days"), 1, 3650, 30)
    settings["keep_forever"] = bool(settings.get("keep_forever"))
    settings["pruning"] = bool(settings.get("pruning"))
    if not _HHMM.fullmatch(str(settings.get("window_start") or "")):
        settings["window_start"] = DEFAULTS["window_start"]
    if not _HHMM.fullmatch(str(settings.get("window_end") or "")):
        settings["window_end"] = DEFAULTS["window_end"]
    return settings


def save_retention(store: Store, bot_id: str, patch: dict) -> dict:
    """User settings for the idle pass. The pass does not rewrite this file."""
    store.get_bot(bot_id)
    current = load_retention(store, bot_id)
    if "idle_minutes" in patch and patch["idle_minutes"] is not None:
        current["idle_minutes"] = _clamp_int(patch["idle_minutes"], 1, 1440, current["idle_minutes"])
    if "retain_days" in patch and patch["retain_days"] is not None:
        current["retain_days"] = _clamp_int(patch["retain_days"], 1, 3650, current["retain_days"])
    if "keep_forever" in patch and patch["keep_forever"] is not None:
        current["keep_forever"] = bool(patch["keep_forever"])
    if "pruning" in patch and patch["pruning"] is not None:
        current["pruning"] = bool(patch["pruning"])
    for key in ("window_start", "window_end"):
        if key in patch and patch[key]:
            if not _HHMM.fullmatch(str(patch[key])):
                raise StoreError("The idle window uses 24-hour times like 02:00.", 400)
            current[key] = str(patch[key])
    folder = notes_dir(store, bot_id)
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_text(folder / "retention.json", json.dumps(current, indent=2) + "\n")
    return current


def _clamp_int(value, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    if number < low:
        return low
    if number > high:
        return high
    return number


def _minutes(text: str) -> int:
    match = _HHMM.fullmatch(text or "")
    if not match:
        return 0
    return int(match.group(1)) * 60 + int(match.group(2))


def in_window(now: datetime, start: str, end: str) -> bool:
    minutes = now.hour * 60 + now.minute
    begin = _minutes(start)
    finish = _minutes(end)
    if begin == finish:
        return False
    if begin < finish:
        return begin <= minutes < finish
    return minutes >= begin or minutes < finish


def pass_due(now: datetime, last_activity: datetime | None, settings: dict) -> bool:
    """True during the idle window after the bot has been quiet long enough."""
    if not in_window(now, settings["window_start"], settings["window_end"]):
        return False
    if last_activity is None:
        return True
    return now - last_activity >= timedelta(minutes=int(settings["idle_minutes"]))


def find_leaks(text: str) -> list[str]:
    """Token-shaped strings that must not land in a note."""
    return [match.group(0) for match in _LEAK.finditer(text or "")]


def saved_secrets(store: Store) -> list[str]:
    found: list[str] = []
    try:
        for endpoint in store.list_endpoints():
            key = endpoint.get("api_key") or ""
            if isinstance(key, str) and len(key) >= 6:
                found.append(key)
    except Exception:
        pass
    try:
        found.extend(tools_mod.secret_strings(store))
    except Exception:
        pass
    unique: list[str] = []
    for item in sorted(found, key=len, reverse=True):
        if item and item not in unique:
            unique.append(item)
    return unique


def scrub_text(store: Store, text: str) -> str:
    cleaned = tools_mod.redact(store, text or "")
    for secret in saved_secrets(store):
        if secret in cleaned:
            cleaned = cleaned.replace(secret, "[redacted]")
    return _LEAK.sub("[redacted]", cleaned)


def _auto_clean(store: Store, text: str) -> bool:
    if find_leaks(text):
        return False
    try:
        if tools_mod.contains_secret(store, text.encode("utf-8")):
            return False
    except Exception:
        return False
    for secret in saved_secrets(store):
        if secret and secret in text:
            return False
    return True


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _attrs(raw: str) -> dict[str, str]:
    return {key: value for key, value in _ATTR.findall(raw or "")}


def _split_owned(text: str) -> tuple[str, str, str, bool]:
    start = text.find(AUTO_OPEN)
    if start < 0:
        return text, "", "", False
    end = text.find(AUTO_CLOSE, start + len(AUTO_OPEN))
    if end < 0:
        return text, "", "", False
    prefix = text[:start]
    inner = text[start + len(AUTO_OPEN) : end]
    suffix = text[end + len(AUTO_CLOSE) :]
    return prefix, inner, suffix, True


def _parse_entry(token: str) -> Entry | None:
    match = _ENTRY.search(token)
    if not match:
        return None
    attrs = _attrs(match.group(1))
    kind = attrs.get("kind") or ""
    if kind not in KIND_FILE:
        return None
    body = match.group(2).strip("\n")
    lines = body.splitlines()
    title = ""
    rest: list[str] = []
    for line in lines:
        if line.startswith("## ") and not title:
            title = line[3:].strip()
            continue
        if line.strip().startswith("[m:") or line.strip().startswith("[observed:"):
            continue
        rest.append(line)
    cites = [item for item in (attrs.get("cites") or "").split(",") if item and item != "none"]
    dates = [item for item in (attrs.get("dates") or "").split(",") if item]
    steps = [line[2:].strip() for line in rest if line.startswith("- ") and line[2:].strip()]
    return Entry(
        kind=kind,
        title=title or kind,
        body="\n".join(rest).strip(),
        cites=cites,
        dates=dates,
        status=attrs.get("status") or "",
        score=attrs.get("score") or "",
        source=attrs.get("source") or "derived",
        hour=attrs.get("hour") or "",
        entry_id=attrs.get("id") or new_id(),
        steps=steps,
    )


def read_entries(text: str) -> list[Entry]:
    _prefix, inner, _suffix, had = _split_owned(text)
    if not had:
        return []
    found = []
    for match in _ENTRY.finditer(inner):
        parsed = _parse_entry(match.group(0))
        if parsed:
            found.append(parsed)
    return found


def _preserved_inner(inner: str) -> str:
    """User blocks and any prose inside the auto section, without our entries."""
    chunks: list[str] = []
    cursor = 0
    for match in _BLOCK.finditer(inner):
        gap = inner[cursor : match.start()]
        if gap.strip():
            chunks.append(gap)
        token = match.group(0)
        if token.startswith("<!-- ea:user"):
            chunks.append(token if token.endswith("\n") else token + "\n")
        elif token.startswith("<!-- ea:entry") and _parse_entry(token) is None:
            chunks.append(token if token.endswith("\n") else token + "\n")
        cursor = match.end()
    tail = inner[cursor:]
    if tail.strip():
        chunks.append(tail)
    return "".join(chunks)


def _render_entry(entry: Entry) -> str:
    cites = ",".join(entry.cites) if entry.cites else "none"
    dates = ",".join(entry.dates)
    parts = [f"id={entry.entry_id}", f"kind={entry.kind}", f"cites={cites}", f"dates={dates}"]
    if entry.status:
        parts.append(f"status={entry.status}")
    if entry.score:
        parts.append(f"score={entry.score}")
    if entry.source and entry.source != "derived":
        parts.append(f"source={entry.source}")
    if entry.hour:
        parts.append(f"hour={entry.hour}")
    if entry.source == "observed":
        stamp = entry.dates[0] if entry.dates else "undated"
        cite_line = f"[observed:{stamp}]"
    else:
        cite_line = " ".join(f"[m:{cite}]" for cite in entry.cites)
    body = entry.body.strip()
    return (
        f"<!-- ea:entry {' '.join(parts)} -->\n"
        f"## {entry.title.strip() or entry.kind}\n"
        f"{body}\n"
        f"{cite_line}\n"
        f"<!-- /ea:entry -->\n"
    )


def _assemble(original: str, entries: list[Entry], *, predictions: bool, mean: float, count: int) -> str:
    prefix, inner, suffix, had = _split_owned(original)
    kept = _preserved_inner(inner) if had else ""
    auto = AUTO_OPEN + "\n"
    if kept:
        auto += kept if kept.endswith("\n") else kept + "\n"
    if predictions and count:
        auto += f"<!-- ea:score {mean:.2f} {count} -->\n"
    for entry in entries:
        auto += _render_entry(entry)
    auto += AUTO_CLOSE + "\n"
    if not had:
        if len(prefix) >= NOTE_CAP and prefix.strip():
            return prefix
        sep = "" if (not prefix or prefix.endswith("\n")) else "\n"
        return prefix + sep + auto
    return prefix + auto + suffix


def _fit(original: str, entries: list[Entry], *, predictions: bool, mean: float, count: int) -> str:
    current = list(entries)
    while True:
        text = _assemble(original, current, predictions=predictions, mean=mean, count=count)
        if len(text) <= NOTE_CAP or not current:
            return text
        oldest = min(
            range(len(current)),
            key=lambda index: (current[index].dates[0] if current[index].dates else "", current[index].entry_id),
        )
        current.pop(oldest)


def _same(left: Entry, right: Entry) -> bool:
    if left.kind != right.kind:
        return False
    if left.source == "observed" or right.source == "observed" or left.kind in {"world", "habit"}:
        return bool(left.title) and _norm(left.title) == _norm(right.title)
    shared = {cite for cite in set(left.cites) & set(right.cites) if cite and cite != "observed"}
    return bool(shared)


def _merge_pair(kept: Entry, new: Entry) -> Entry:
    cites: list[str] = []
    for cite in [*kept.cites, *new.cites]:
        if cite and cite not in cites:
            cites.append(cite)
    dates: list[str] = []
    for date in [*kept.dates, *new.dates]:
        if date and date not in dates:
            dates.append(date)
    status = kept.status
    if new.status == "closed" or kept.status == "closed":
        status = "closed"
    elif new.status == "verified" or kept.status == "verified":
        status = "verified"
    elif new.status == "approved" or kept.status == "approved":
        status = "approved"
    elif new.status:
        status = new.status
    body = kept.body
    if new.source == "observed" or (len(new.body) > len(kept.body) and new.source != "model"):
        body = new.body
    elif len(new.body) > len(kept.body):
        body = new.body
    score = kept.score or new.score
    steps = kept.steps or new.steps
    return Entry(
        kind=kept.kind,
        title=kept.title or new.title,
        body=body,
        cites=cites,
        dates=dates,
        status=status,
        score=score,
        source=new.source if new.source == "observed" else kept.source,
        hour=kept.hour or new.hour,
        entry_id=kept.entry_id,
        steps=steps,
    )


def merge_entries(existing: list[Entry], incoming: list[Entry]) -> list[Entry]:
    merged = list(existing)
    for entry in incoming:
        hit = next((index for index, kept in enumerate(merged) if _same(kept, entry)), None)
        if hit is None:
            merged.append(entry)
            continue
        merged[hit] = _merge_pair(merged[hit], entry)
    return merged


def _sources(messages: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    text: dict[str, str] = {}
    dates: dict[str, str] = {}
    for message in messages or []:
        mid = str(message.get("id") or "")
        if not mid:
            continue
        text[mid] = str(message.get("content") or "")
        when = parse_when(str(message.get("created_at") or ""))
        if when is not None:
            dates[mid] = when.date().isoformat()
    return text, dates


def acceptable(entry: Entry, messages: list[dict]) -> bool:
    """A note needs a date. A model line also needs real message ids and no new facts."""
    if not entry.dates or not _DATE.fullmatch(entry.dates[0]):
        return False
    if entry.source == "observed":
        return entry.kind == "world" and entry.cites == ["observed"]
    known, dates = _sources(messages)
    if not entry.cites or any(cite not in known for cite in entry.cites):
        return False
    if entry.source == "model":
        if any(dates.get(cite) != entry.dates[0] for cite in entry.cites):
            return False
        blob = "\n".join(known[cite] for cite in entry.cites).lower()
        prose = _CITE.sub(" ", f"{entry.title}\n{entry.body}").lower()
        if any(word not in blob for word in _LONG.findall(prose)):
            return False
    return True


def _cut(text: str, limit: int) -> str:
    cleaned = text or ""
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip()


def _scrub_entry(store: Store, entry: Entry) -> Entry | None:
    title = _cut(scrub_text(store, entry.title), 80)
    body = _cut(scrub_text(store, entry.body), 2000)
    steps = [_cut(scrub_text(store, step), 240) for step in entry.steps]
    cleaned = Entry(
        kind=entry.kind,
        title=title,
        body=body,
        cites=list(entry.cites),
        dates=list(entry.dates),
        status=entry.status,
        score=entry.score,
        source=entry.source,
        hour=entry.hour,
        entry_id=entry.entry_id,
        steps=steps,
    )
    rendered = _render_entry(cleaned)
    if not _auto_clean(store, rendered):
        return None
    return cleaned


def proven_steps(store: Store, bot_id: str) -> set[str]:
    """Steps from skills this bot promoted through the replay. User skills stay out."""
    from easyagent.learn import is_user_skill, list_candidates, skill_meta

    found: set[str] = set()
    try:
        rows = list_candidates(store, bot_id, status="promoted")
    except Exception:
        return found
    for cand in rows:
        name = str(cand.get("name") or "")
        if not name or is_user_skill(store, name):
            continue
        if skill_meta(store, name).get("origin") != "learned":
            continue
        if not (store.skills_dir / f"{name}.md").is_file():
            continue
        for step in cand.get("steps") or []:
            cleaned = " ".join(str(step).split())
            if cleaned:
                found.add(cleaned.lower())
    return found


def _filter_playbook(store: Store, bot_id: str, entry: Entry) -> Entry | None:
    if entry.kind != "playbook":
        return entry
    proven = proven_steps(store, bot_id)
    kept = [step for step in entry.steps if " ".join(step.split()).lower() in proven]
    if not kept:
        return None
    body = "\n".join(f"- {step}" for step in kept)
    return Entry(
        kind=entry.kind,
        title=entry.title,
        body=body,
        cites=list(entry.cites),
        dates=list(entry.dates),
        status=entry.status,
        score=entry.score,
        source=entry.source,
        hour=entry.hour,
        entry_id=entry.entry_id,
        steps=kept,
    )


def _host(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    except ValueError:
        return ""
    return parsed.netloc or parsed.path


def world_entries(store: Store, today: str, last_seen: str) -> list[Entry]:
    rows: list[Entry] = []
    try:
        endpoints = store.list_endpoints()
    except Exception:
        endpoints = []
    for endpoint in endpoints:
        name = " ".join(str(endpoint.get("name") or "connection").split()) or "connection"
        host = _host(str(endpoint.get("base_url") or ""))
        model = " ".join(str(endpoint.get("model") or "").split())
        detail = f"Host: {host or 'unknown'}. Model: {model or 'the connection default'}. Last seen: {last_seen}."
        rows.append(
            Entry(
                kind="world",
                title=name,
                body=detail,
                cites=["observed"],
                dates=[today],
                source="observed",
            )
        )
    try:
        computers = store.list_computers()
    except Exception:
        computers = []
    for computer in computers:
        if not isinstance(computer, dict):
            continue
        name = " ".join(str(computer.get("name") or "computer").split()) or "computer"
        host = " ".join(str(computer.get("host") or "").split())
        kind = " ".join(str(computer.get("kind") or "computer").split())
        rows.append(
            Entry(
                kind="world",
                title=name,
                body=f"Machine: {kind}. Address: {host or 'unknown'}. Last seen: {last_seen}.",
                cites=["observed"],
                dates=[today],
                source="observed",
            )
        )
    return rows


def _promise_entries(messages: list[dict]) -> list[Entry]:
    found = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        text = str(message.get("content") or "")
        match = _PROMISE.search(text)
        if not match:
            continue
        when = parse_when(str(message.get("created_at") or ""))
        mid = str(message.get("id") or "")
        if not mid or when is None:
            continue
        quote = " ".join(match.group(0).split())
        found.append(
            Entry(
                kind="promise",
                title=quote,
                body=quote,
                cites=[mid],
                dates=[when.date().isoformat()],
                status="open",
                source="derived",
            )
        )
    return found


def _close_promises(entries: list[Entry], messages: list[dict]) -> None:
    order = [str(message.get("id") or "") for message in messages]
    text = {str(message.get("id") or ""): str(message.get("content") or "") for message in messages}
    for entry in entries:
        if entry.kind != "promise" or entry.status == "closed" or not entry.cites:
            continue
        try:
            start = max(order.index(cite) for cite in entry.cites if cite in order)
        except ValueError:
            continue
        share = re.findall(r"[a-z0-9]{4,}", entry.body.lower())
        for mid in order[start + 1 :]:
            later = text.get(mid) or ""
            low = later.lower()
            if not any(token in low for token in ("done", "checked", "finished", "updated")):
                continue
            if share and not any(word in low for word in share):
                continue
            entry.status = "closed"
            if mid not in entry.cites:
                entry.cites.append(mid)
            break


def _prediction_entries(messages: list[dict], prior: float) -> list[Entry]:
    """Expected is the bot's own prediction. A reaction with no words is not one."""
    rows = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        text = " ".join(str(message.get("content") or "").split())
        match = _EXPECT.search(text)
        if not match:
            continue
        expected = " ".join(match.group(0).split())
        when = parse_when(str(message.get("created_at") or ""))
        mid = str(message.get("id") or "")
        if not mid or when is None or not expected:
            continue
        outcome = ""
        outcome_id = ""
        for later in messages[index + 1 :]:
            outcome = " ".join(str(later.get("content") or "").split())
            if not outcome:
                continue
            outcome_id = str(later.get("id") or "")
            break
        if not outcome_id or not outcome:
            continue
        score = _outcome_score(expected, outcome)
        rows.append(
            Entry(
                kind="prediction",
                title=expected,
                body=f"Expected: {expected}\nActual: {outcome}",
                cites=[mid, outcome_id],
                dates=[when.date().isoformat()],
                score=f"{score:.2f}",
                status=f"confidence={prior:.2f}",
                source="derived",
            )
        )
    return rows


def _outcome_score(expected: str, actual: str) -> float:
    words = _LONG.findall((expected or "").lower())
    if not words:
        return 0.5
    blob = (actual or "").lower()
    hit = sum(1 for word in words if word in blob)
    return hit / len(words)


def _unknown_entries(messages: list[dict]) -> list[Entry]:
    rows = []
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        ask = " ".join(str(message.get("content") or "").split())
        if "?" not in ask:
            continue
        when = parse_when(str(message.get("created_at") or ""))
        mid = str(message.get("id") or "")
        if not mid or when is None:
            continue
        later = ""
        for item in messages[index + 1 :]:
            later += "\n" + str(item.get("content") or "")
            tools = item.get("tools")
            if isinstance(tools, list):
                for tool in tools:
                    if isinstance(tool, dict):
                        later += "\n" + str(tool.get("result") or "")
        words = _LONG.findall(ask.lower())
        verified = bool(words) and all(word in later.lower() for word in words)
        rows.append(
            Entry(
                kind="unknown",
                title=ask,
                body=ask,
                cites=[mid],
                dates=[when.date().isoformat()],
                status="verified" if verified else "open",
                source="derived",
            )
        )
    return rows


def _habit_entries(messages: list[dict], now: datetime) -> list[Entry]:
    buckets: dict[int, list[dict]] = {}
    for message in messages:
        if message.get("role") != "user":
            continue
        when = parse_when(str(message.get("created_at") or ""))
        if when is None:
            continue
        local = when.astimezone(now.tzinfo)
        if (now.date() - local.date()).days > 14 or (now.date() - local.date()).days < 0:
            continue
        buckets.setdefault(local.hour, []).append(message)
    rows = []
    for hour, group in sorted(buckets.items()):
        days = set()
        for item in group:
            when = parse_when(str(item.get("created_at") or ""))
            if when is not None:
                days.add(when.date().isoformat())
        if len(days) < 2:
            continue
        latest = max(group, key=lambda item: str(item.get("created_at") or ""))
        mid = str(latest.get("id") or "")
        when = parse_when(str(latest.get("created_at") or ""))
        if not mid or when is None:
            continue
        rows.append(
            Entry(
                kind="habit",
                title=f"Around {hour:02d}:00",
                body=f"You wrote around {hour:02d}:00 on {len(days)} days.",
                cites=[mid],
                dates=[when.date().isoformat()],
                status="suggested",
                hour=str(hour),
                source="derived",
            )
        )
    return rows


def _cite_ids(field: str) -> list[str]:
    ids = _CITE.findall(field or "")
    if ids:
        return ids
    raw = (field or "").strip()
    if raw.startswith("m:"):
        raw = raw[2:]
    return [item for item in raw.split(",") if item and item != "observed"]


def parse_model_rows(text: str, messages: list[dict]) -> list[Entry]:
    known, dates = _sources(messages)
    rows = []
    for line in (text or "").splitlines():
        if "|" not in line:
            continue
        parts = [part.strip() for part in line.split("|")]
        kind = parts[0].lower()
        if kind not in KIND_FILE or len(parts) < 4:
            continue
        cites = [cite for cite in _cite_ids(parts[1]) if cite in known]
        date = parts[2] if _DATE.fullmatch(parts[2]) else ""
        if not cites or not date:
            continue
        if any(dates.get(cite) != date for cite in cites):
            continue
        entry = _row_entry(kind, parts, cites, date)
        if entry is None:
            continue
        if acceptable(entry, messages):
            rows.append(entry)
    return rows


def _row_entry(kind: str, parts: list[str], cites: list[str], date: str) -> Entry | None:
    if kind == "mistake" and len(parts) >= 7:
        title, wrong, cause, fix = parts[3], parts[4], parts[5], parts[6]
        return Entry(
            kind=kind,
            title=title,
            body=f"Wrong: {wrong}\nCause: {cause}\nFix: {fix}",
            cites=cites,
            dates=[date],
            source="model",
        )
    if kind == "promise" and len(parts) >= 5:
        status = "closed" if parts[3].lower() == "closed" else "open"
        return Entry(kind=kind, title=parts[4], body=parts[4], cites=cites, dates=[date], status=status, source="model")
    if kind == "unknown" and len(parts) >= 5:
        status = "verified" if parts[3].lower() == "verified" else "open"
        return Entry(kind=kind, title=parts[4], body=parts[4], cites=cites, dates=[date], status=status, source="model")
    if kind == "habit" and len(parts) >= 5:
        return Entry(
            kind=kind,
            title=parts[3],
            body=parts[4],
            cites=cites,
            dates=[date],
            status="suggested",
            source="model",
        )
    if kind == "playbook" and len(parts) >= 5:
        steps = [step.strip() for step in parts[4].split("/") if step.strip()]
        return Entry(
            kind=kind,
            title=parts[3],
            body="\n".join(f"- {step}" for step in steps),
            cites=cites,
            dates=[date],
            source="model",
            steps=steps,
        )
    if kind == "world" and len(parts) >= 5:
        return Entry(kind=kind, title=parts[3], body=parts[4], cites=cites, dates=[date], source="model")
    if kind == "dream" and len(parts) >= 5:
        try:
            score = float(parts[3])
        except ValueError:
            score = 0.0
        if score < 0:
            score = 0.0
        if score > 1:
            score = 1.0
        return Entry(
            kind=kind,
            title=parts[4],
            body=parts[4],
            cites=cites,
            dates=[date],
            score=f"{score:.2f}",
            source="model",
        )
    return None


def _day_messages(messages: list[dict], now: datetime) -> list[dict]:
    kept = []
    for message in messages:
        when = parse_when(str(message.get("created_at") or ""))
        if when is None:
            continue
        if when.astimezone(now.tzinfo).date() == now.date():
            kept.append(message)
    return kept


def _zone_label(now: datetime) -> str:
    name = now.tzname() or "local"
    offset = now.strftime("%z")
    if len(offset) == 5:
        return f"{name} (UTC{offset[:3]}:{offset[3:]})"
    if offset:
        return f"{name} ({offset})"
    return name


def _prompt(messages: list[dict], now: datetime) -> str:
    lines = []
    example_id = ""
    example_date = now.date().isoformat()
    for message in messages:
        mid = str(message.get("id") or "")
        if not mid:
            continue
        if not example_id:
            example_id = mid
            when = parse_when(str(message.get("created_at") or ""))
            if when is not None:
                example_date = when.astimezone(now.tzinfo).date().isoformat()
        text = " ".join(str(message.get("content") or "").split())
        if len(text) > 240:
            text = text[:239].rstrip() + "…"
        lines.append(f"[m:{mid}] {message.get('role') or 'user'}: {text}")
        tools = message.get("tools")
        if isinstance(tools, list):
            for tool in tools[:4]:
                if not isinstance(tool, dict):
                    continue
                bit = " ".join(str(tool.get("command") or tool.get("result") or "").split())
                if bit:
                    lines.append(f"[m:{mid}] tool: {bit[:180]}")
    blob = "\n".join(lines)
    if len(blob) > 8000:
        blob = blob[-8000:]
    today = now.date().isoformat()
    example = (
        f"PROMISE|[m:{example_id}]|{example_date}|open|I will check the boiler tomorrow"
        if example_id
        else f"PROMISE|[m:id]|{today}|open|I will check the boiler tomorrow"
    )
    return (
        f"Today is {today}. The timezone is {_zone_label(now)}. "
        "Do not call a tool. Do not run a shell command. Do not print a date command. "
        "Use only facts in the lines below. Every row cites an id copied from the lines. "
        "The date on every row is that message's date, written YYYY-MM-DD. "
        "Do not include passwords, keys, or tokens. "
        "One pipe row per fact and no other text.\n"
        "MISTAKE|[m:id]|YYYY-MM-DD|title|what went wrong|root cause|fix\n"
        "PROMISE|[m:id]|YYYY-MM-DD|open or closed|commitment\n"
        "UNKNOWN|[m:id]|YYYY-MM-DD|open or verified|question\n"
        "HABIT|[m:id]|YYYY-MM-DD|pattern|suggestion\n"
        "PLAYBOOK|[m:id]|YYYY-MM-DD|title|step / step\n"
        "WORLD|[m:id]|YYYY-MM-DD|name|what changed\n"
        "DREAM|[m:id]|YYYY-MM-DD|0.5|idea\n"
        f"Example:\n{example}\n\n"
        f"Lines:\n{blob}"
    )


def _calibration(entries: list[Entry]) -> tuple[float, int]:
    scores = []
    for entry in entries:
        if entry.kind != "prediction" or not entry.score:
            continue
        try:
            scores.append(float(entry.score))
        except ValueError:
            continue
    if not scores:
        return 0.5, 0
    return sum(scores) / len(scores), len(scores)


def _apply_calibration(store: Store, bot: dict, mean: float, count: int) -> None:
    if count < 3:
        return
    if mean < 0.45:
        revisions = 3
    elif mean > 0.8:
        revisions = 1
    else:
        revisions = 2
    if bot.get("check_revisions") == revisions:
        return
    store.update_bot(bot["id"], check_revisions=revisions, check_revisions_set=True)


def _load_all(store: Store, bot_id: str) -> dict[str, str]:
    folder = notes_dir(store, bot_id)
    return {name: _read(folder / name) for name, _title, _blurb in NOTE_FILES}


def commit_entries(store: Store, bot_id: str, incoming: list[Entry], messages: list[dict]) -> dict[str, dict]:
    """Merge, cap, and write. User text outside the auto block is not edited."""
    previous = _load_all(store, bot_id)
    existing: list[Entry] = []
    for text in previous.values():
        existing.extend(read_entries(text))
    existing_ids = {entry.entry_id for entry in existing}
    prepared = []
    for entry in incoming:
        filtered = _filter_playbook(store, bot_id, entry)
        if filtered is None or not acceptable(filtered, messages):
            continue
        cleaned = _scrub_entry(store, filtered)
        if cleaned is not None:
            prepared.append(cleaned)
    merged = merge_entries(existing, prepared)
    safe = []
    for entry in merged:
        cleaned = _scrub_entry(store, entry)
        if cleaned is None:
            continue
        # An entry already on disk keeps its cite after the raw message is pruned.
        if cleaned.entry_id in existing_ids or acceptable(cleaned, messages):
            safe.append(cleaned)
    mean, count = _calibration(safe)
    folder = notes_dir(store, bot_id)
    folder.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    changes: dict[str, dict] = {}
    for name, _title, _blurb in NOTE_FILES:
        kind = FILE_KIND[name]
        rows = [entry for entry in safe if entry.kind == kind]
        text = _fit(
            previous.get(name) or "",
            rows,
            predictions=name == "PREDICTIONS.md",
            mean=mean,
            count=count,
        )
        if not _auto_section_clean(store, text):
            text = previous.get(name) or ""
        written[name] = text
        before_ids = {entry.entry_id for entry in read_entries(previous.get(name) or "")}
        after_ids = {entry.entry_id for entry in read_entries(text)}
        changes[name] = {
            "added": len(after_ids - before_ids),
            "removed": len(before_ids - after_ids),
        }
    if written == previous:
        return changes
    from easyagent.learn import note_ledger

    note_ledger(store, kind="notes", key=bot_id, previous=json.dumps(previous), bot_id=bot_id)
    for name, text in written.items():
        if text == previous.get(name):
            continue
        path = folder / name
        if text:
            atomic_write_text(path, text if text.endswith("\n") else text + "\n")
        elif path.is_file():
            path.unlink()
    return changes


def _auto_section_clean(store: Store, text: str) -> bool:
    _prefix, inner, _suffix, had = _split_owned(text)
    if not had:
        return _auto_clean(store, text)
    return _auto_clean(store, inner)


def _busy(store: Store) -> bool:
    from easyagent.learn import chats_active

    if chats_active(store):
        return True
    return False


def _user_chatting(store: Store, now: datetime, idle_minutes: int) -> bool:
    """True when any bot was just written to. The idle pass waits for all of them."""
    limit = timedelta(minutes=int(idle_minutes))
    try:
        bots = store.list_bots()
    except StoreError:
        return False
    for bot in bots:
        last = _last_activity(store, bot["id"])
        if last is not None and now - last < limit:
            return True
    return False


def _last_activity(store: Store, bot_id: str) -> datetime | None:
    latest: datetime | None = None
    try:
        listed = store.list_chats(bot_id)
    except StoreError:
        return None
    for item in listed:
        try:
            chat = store.get_chat(bot_id, item["id"])
        except StoreError:
            continue
        for message in chat.get("messages") or []:
            when = parse_when(str(message.get("created_at") or ""))
            if when is not None and (latest is None or when > latest):
                latest = when
    return latest


def _all_messages(store: Store, bot_id: str) -> list[dict]:
    rows = []
    try:
        listed = store.list_chats(bot_id)
    except StoreError:
        return rows
    for item in listed:
        try:
            chat = store.get_chat(bot_id, item["id"])
        except StoreError:
            continue
        for message in chat.get("messages") or []:
            if isinstance(message, dict):
                stamped = dict(message)
                stamped["_chat_id"] = chat["id"]
                rows.append(stamped)
    rows.sort(key=lambda message: str(message.get("created_at") or ""))
    return rows


def _already(store: Store, bot_id: str, now: datetime) -> bool:
    data = _read_json(notes_dir(store, bot_id) / "last-night.json", {})
    if not isinstance(data, dict):
        return False
    return data.get("result") == "updated" and data.get("day") == now.date().isoformat()


def _log(store: Store, bot_id: str, now: datetime, result: str, detail: str) -> None:
    folder = notes_dir(store, bot_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    path = folder / "nightly-log.md"
    previous = _read(path)
    section = scrub_text(
        store,
        f"## {now.isoformat()}\n- result: {result}\n- {detail.strip()}\n",
    )
    text = previous
    if text and not text.endswith("\n"):
        text += "\n"
    text += section if section.endswith("\n") else section + "\n"
    parts = text.split("\n## ")
    if len(parts) > LOG_SECTIONS:
        tail = parts[-LOG_SECTIONS:]
        first = tail[0] if tail[0].startswith("## ") else "## " + tail[0]
        text = first + "".join("\n## " + part for part in tail[1:])
        if not text.endswith("\n"):
            text += "\n"
    if not _auto_clean(store, text):
        text = scrub_text(store, text)
    try:
        atomic_write_text(path, text)
    except OSError:
        return


def _mark_digested(store: Store, bot_id: str, messages: list[dict]) -> list[str]:
    """Mark a message only when a note cites it and the search index still has it."""
    folder = notes_dir(store, bot_id)
    blob = "\n".join(_read(folder / name) for name, _title, _blurb in NOTE_FILES)
    try:
        listed = store.list_chats(bot_id)
    except StoreError:
        listed = []
    for item in listed:
        try:
            chat = store.get_chat(bot_id, item["id"])
        except StoreError:
            continue
        blob += "\n" + str(chat.get("rolling_summary") or "")
    indexed = indexed_message_ids(store, bot_id)
    path = folder / "digested.json"
    current = _read_json(path, {})
    marked = current.get("messages") if isinstance(current, dict) else {}
    if not isinstance(marked, dict):
        marked = {}
    added = []
    for message in messages:
        mid = str(message.get("id") or "")
        chat_id = str(message.get("_chat_id") or "")
        if not mid or not chat_id:
            continue
        if f"[m:{mid}]" not in blob:
            continue
        if mid not in indexed:
            continue
        key = f"{chat_id}:{mid}"
        marked[key] = {
            "chat_id": chat_id,
            "message_id": mid,
            "created_at": message.get("created_at") or "",
        }
        added.append(mid)
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"messages": marked}, indent=2) + "\n")
    return added


def _digest_verified(store: Store, bot_id: str, message_id: str) -> bool:
    folder = notes_dir(store, bot_id)
    blob = "\n".join(_read(folder / name) for name, _title, _blurb in NOTE_FILES)
    try:
        for item in store.list_chats(bot_id):
            chat = store.get_chat(bot_id, item["id"])
            blob += "\n" + str(chat.get("rolling_summary") or "")
    except StoreError:
        return False
    if f"[m:{message_id}]" not in blob:
        return False
    return message_id in indexed_message_ids(store, bot_id)


def prune_plan(store: Store, bot_id: str, now: datetime) -> list[dict]:
    """Digested messages old enough to remove. This does not delete them."""
    settings = load_retention(store, bot_id)
    marked = _read_json(notes_dir(store, bot_id) / "digested.json", {})
    rows = marked.get("messages") if isinstance(marked, dict) else {}
    if not isinstance(rows, dict):
        return []
    pending = []
    for row in rows.values():
        if not isinstance(row, dict):
            continue
        mid = str(row.get("message_id") or "")
        chat_id = str(row.get("chat_id") or "")
        when = parse_when(str(row.get("created_at") or ""))
        if not mid or not chat_id or when is None:
            continue
        age = now - when.astimezone(now.tzinfo)
        if age.days < int(settings["retain_days"]):
            continue
        if not _digest_verified(store, bot_id, mid):
            continue
        try:
            chat = store.get_chat(bot_id, chat_id)
        except StoreError:
            continue
        message = next((item for item in chat.get("messages") or [] if str(item.get("id") or "") == mid), None)
        if not isinstance(message, dict):
            continue
        preview = " ".join(str(message.get("content") or "").split())
        if len(preview) > 80:
            preview = preview[:79].rstrip() + "…"
        pending.append(
            {
                "chat_id": chat_id,
                "message_id": mid,
                "created_at": row.get("created_at") or "",
                "preview": scrub_text(store, preview),
            }
        )
    pending.sort(key=lambda item: str(item.get("created_at") or ""))
    return pending


def prune_expired(store: Store, bot_id: str, now: datetime) -> list[str]:
    """Drop digested messages past the window. Undigested messages stay."""
    settings = load_retention(store, bot_id)
    pending = prune_plan(store, bot_id, now)
    if settings["keep_forever"] or not settings["pruning"]:
        return []
    dropped: list[str] = []
    by_chat: dict[str, list[str]] = {}
    for item in pending:
        if not _digest_verified(store, bot_id, item["message_id"]):
            continue
        by_chat.setdefault(item["chat_id"], []).append(item["message_id"])
    for chat_id, ids in by_chat.items():
        store.drop_messages(bot_id, chat_id, set(ids))
        dropped.extend(ids)
    if dropped:
        path = notes_dir(store, bot_id) / "digested.json"
        current = _read_json(path, {})
        rows = current.get("messages") if isinstance(current, dict) else {}
        if isinstance(rows, dict):
            gone = set(dropped)
            kept = {key: value for key, value in rows.items() if str(value.get("message_id") or "") not in gone}
            atomic_write_text(path, json.dumps({"messages": kept}, indent=2) + "\n")
    return dropped


def _write_last(store: Store, bot_id: str, payload: dict) -> None:
    folder = notes_dir(store, bot_id)
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_text(folder / "last-night.json", json.dumps(payload, indent=2) + "\n")


async def _propose_nightly(store: Store, bot: dict, summary: str) -> None:
    """Turn an open observation or a new note into a candidate. Replay still decides."""
    try:
        from easyagent.learn import open_observations, propose_from_signal
    except Exception:
        return
    observations = open_observations(store, bot["id"])
    bits = [str(item.get("text") or "").strip() for item in observations[-6:] if str(item.get("text") or "").strip()]
    folder = notes_dir(store, bot["id"])
    for name in ("MISTAKES.md", "UNKNOWNS.md", "PLAYBOOK.md"):
        text = _read(folder / name).strip()
        if text:
            bits.append(text[-400:])
    notes = "\n".join(bits).strip()
    if not notes:
        return
    try:
        await propose_from_signal(
            store,
            bot,
            notes[:2000],
            reason="nightly",
            task=summary[:400],
            timeout=llm.IDLE_MODEL_TIMEOUT,
        )
    except Exception:
        return


async def _ask(store: Store, bot: dict, prompt: str) -> str:
    from easyagent import gate

    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except StoreError:
        endpoint = None
    if not endpoint:
        return ""
    token = gate.bind_connection(endpoint, bot.get("name") or "notes")
    try:
        return await llm.complete(
            base_url=endpoint["base_url"],
            api_key=endpoint.get("api_key") or None,
            model=(bot.get("model") or endpoint.get("model") or None),
            messages=[{"role": "user", "content": scrub_text(store, prompt)}],
            timeout=llm.IDLE_MODEL_TIMEOUT,
            tools=False,
            yield_to_chats=True,
        )
    except llm.YieldLater:
        raise
    except Exception:
        return ""
    finally:
        gate.reset_connection(token)


async def run_pass(store: Store, bot: dict, *, now: datetime | None = None, force: bool = False) -> dict:
    """One idle pass for one bot. A running chat skips it even when force is set."""
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    bot_id = bot["id"]
    settings = load_retention(store, bot_id)
    if _busy(store):
        _log(store, bot_id, now, "skipped", "a chat is running")
        return {"result": "skipped", "reason": "a chat is running"}
    last = _last_activity(store, bot_id)
    chatting = _user_chatting(store, now, int(settings["idle_minutes"]))
    if not force and (chatting or not pass_due(now, last, settings)):
        reason = "still chatting" if chatting or in_window(now, settings["window_start"], settings["window_end"]) else "outside the idle window"
        _log(store, bot_id, now, "skipped", reason)
        return {"result": "skipped", "reason": reason}
    if not force and _already(store, bot_id, now):
        return {"result": "already"}
    messages = _all_messages(store, bot_id)
    today = _day_messages(messages, now)
    existing_predictions = []
    for text in _load_all(store, bot_id).values():
        existing_predictions.extend(entry for entry in read_entries(text) if entry.kind == "prediction")
    prior, prior_n = _calibration(existing_predictions)
    if prior_n:
        mean = prior
    incoming: list[Entry] = []
    incoming.extend(_promise_entries(today))
    incoming.extend(_prediction_entries(today, mean if prior_n else 0.5))
    incoming.extend(_unknown_entries(today))
    incoming.extend(_habit_entries(messages, now))
    incoming.extend(world_entries(store, now.date().isoformat(), now.date().isoformat()))
    model_text = ""
    if today:
        try:
            model_text = await _ask(store, bot, _prompt(today, now))
        except llm.YieldLater:
            _log(store, bot_id, now, "skipped", "a chat is using the connection")
            return {"result": "skipped", "reason": "a chat is using the connection"}
    incoming.extend(parse_model_rows(model_text, messages))
    _close_promises(incoming, messages)
    # Closing can also apply to promises already on disk. Fold them in before commit
    # by re-closing the merged list inside commit via a pre-pass on existing.
    previous_entries: list[Entry] = []
    for text in _load_all(store, bot_id).values():
        previous_entries.extend(read_entries(text))
    _close_promises(previous_entries, messages)
    incoming.extend(entry for entry in previous_entries if entry.kind == "promise" and entry.status == "closed")
    if _busy(store):
        _log(store, bot_id, now, "skipped", "a chat started during the pass")
        return {"result": "skipped", "reason": "a chat is running"}
    changes = commit_entries(store, bot_id, incoming, messages)
    try:
        from easyagent.rolling import _running, refresh_rolling_summary

        chat = store.existing_ongoing(bot_id)
        if chat is not None and not _running(chat):
            await refresh_rolling_summary(store, bot, chat)
    except Exception:
        pass
    if _busy(store):
        _log(store, bot_id, now, "skipped", "a chat started before prune")
        return {"result": "skipped", "reason": "a chat is running"}
    messages = _all_messages(store, bot_id)
    digested = _mark_digested(store, bot_id, messages)
    pruned = prune_expired(store, bot_id, now)
    fresh = []
    for text in _load_all(store, bot_id).values():
        fresh.extend(read_entries(text))
    score, scored = _calibration(fresh)
    _apply_calibration(store, bot, score, scored)
    bits = []
    for name, _title, _blurb in NOTE_FILES:
        added = int(changes.get(name, {}).get("added") or 0)
        removed = int(changes.get(name, {}).get("removed") or 0)
        if added or removed:
            bits.append(f"{name} +{added} -{removed}")
    summary = "Checked today's chat. No new notes." if not bits else "Updated " + ", ".join(bits) + "."
    summary += f" Digested {len(digested)}. Pruned {len(pruned)}."
    payload = {
        "at": now.isoformat(),
        "day": now.date().isoformat(),
        "result": "updated",
        "summary": summary,
        "changes": [
            {"file": name, "added": changes.get(name, {}).get("added") or 0, "removed": changes.get(name, {}).get("removed") or 0}
            for name, _title, _blurb in NOTE_FILES
        ],
        "digested": len(digested),
        "pruned": len(pruned),
        "calibration": round(score, 2) if scored else None,
    }
    _write_last(store, bot_id, payload)
    _log(store, bot_id, now, "updated", summary)
    await _propose_nightly(store, bot, summary)
    return payload


async def nightly_idle(store: Store, now: datetime | None = None) -> None:
    """One due pass per bot. Tests set EASYAGENT_NIGHTLY=0 so this stays quiet."""
    if os.environ.get("EASYAGENT_NIGHTLY") == "0":
        return
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        bots = store.list_bots()
    except StoreError:
        return
    for bot in bots:
        settings = load_retention(store, bot["id"])
        if not in_window(now, settings["window_start"], settings["window_end"]):
            continue
        try:
            await run_pass(store, bot, now=now, force=False)
        except Exception:
            continue


def note_block(store: Store, bot_id: str, messages: list[dict]) -> str:
    """Relevant note lines only. Empty when nothing overlaps, so a short chat stays the same."""
    try:
        query = ""
        for message in reversed(messages or []):
            if message.get("role") == "user":
                query = str(message.get("content") or "")
                break
        folder = notes_dir(store, bot_id)
        entries: list[Entry] = []
        for name, _title, _blurb in NOTE_FILES:
            entries.extend(read_entries(_read(folder / name)))
        if not entries:
            return ""
        wanted = {word for word in tokens(query) if len(word) >= 5 and word not in _GENERIC}
        ranked: list[tuple[int, Entry]] = []
        for entry in entries:
            if entry.source == "observed":
                continue
            overlap = {word for word in tokens(f"{entry.title} {entry.body}") if len(word) >= 5 and word not in _GENERIC}
            overlap &= wanted
            if not overlap:
                continue
            bonus = 2 if entry.kind == "mistake" else 0
            ranked.append((len(overlap) + bonus, entry))
        ranked.sort(key=lambda item: item[0], reverse=True)
        lines = ["From this bot's notes:"]
        for _score, entry in ranked[:4]:
            cite = entry.cites[0] if entry.cites else ""
            date = entry.dates[0] if entry.dates else ""
            blurb = " ".join(entry.body.split())
            if len(blurb) > 180:
                blurb = blurb[:179].rstrip() + "…"
            lines.append(f"[{entry.kind} {date} m:{cite}] {entry.title}. {blurb}")
        short = len((query or "").split()) <= 8
        if short:
            promise = next((entry for entry in entries if entry.kind == "promise" and entry.status == "open"), None)
            if promise:
                lines.append(f"[promise {promise.dates[0] if promise.dates else ''} m:{promise.cites[0] if promise.cites else ''}] Still open: {promise.title}")
            dream = _best_dream(entries)
            if dream:
                lines.append(f"[dream] One idea, only if you want it: {dream.title}")
            question = batched_question(entries)
            if question:
                lines.append(question)
        if len(lines) == 1:
            return ""
        text = scrub_text(store, "\n".join(lines)).strip()
        if len(text) > _BLOCK_CHARS:
            text = text[: _BLOCK_CHARS - 1].rstrip() + "…"
        return text
    except Exception:
        return ""


def _best_dream(entries: list[Entry]) -> Entry | None:
    dreams = [entry for entry in entries if entry.kind == "dream"]
    if not dreams:
        return None
    def score(entry: Entry) -> float:
        try:
            return float(entry.score or 0)
        except ValueError:
            return 0.0
    return max(dreams, key=score)


def batched_question(entries: list[Entry]) -> str:
    open_rows = [entry for entry in entries if entry.kind == "unknown" and entry.status == "open"]
    if not open_rows:
        return ""
    parts = [entry.title.strip() for entry in open_rows[:6] if entry.title.strip()]
    if not parts:
        return ""
    return "I am still unsure about one thing: " + "; ".join(parts)


def learning_extra(store: Store, bot: dict) -> dict:
    bot_id = bot["id"]
    folder = notes_dir(store, bot_id)
    last = _read_json(folder / "last-night.json", {})
    if not isinstance(last, dict):
        last = {}
    changed = {}
    for row in last.get("changes") or []:
        if isinstance(row, dict) and row.get("file"):
            changed[row["file"]] = row
    notes = []
    habits = []
    dreams = []
    entries: list[Entry] = []
    for name, title, blurb in NOTE_FILES:
        text = _read(folder / name)
        rows = read_entries(text)
        entries.extend(rows)
        touch = changed.get(name) or {}
        notes.append(
            {
                "name": name,
                "title": title,
                "blurb": blurb,
                "entries": len(rows),
                "chars": len(text),
                "added": int(touch.get("added") or 0),
                "removed": int(touch.get("removed") or 0),
                "changed": last.get("at") if (touch.get("added") or touch.get("removed")) else "",
            }
        )
        if name == "HABITS.md":
            for entry in rows:
                if entry.status == "suggested":
                    habits.append(
                        {
                            "id": entry.entry_id,
                            "title": entry.title,
                            "text": entry.body,
                            "hour": entry.hour,
                            "status": entry.status,
                        }
                    )
        if name == "DREAMS.md":
            for entry in rows:
                dreams.append({"id": entry.entry_id, "title": entry.title, "text": entry.body})
    now = datetime.now().astimezone()
    settings = load_retention(store, bot_id)
    mean, count = _calibration(entries)
    return {
        "notes": notes,
        "last_night": {
            "at": last.get("at") or "",
            "summary": last.get("summary") or "",
            "changes": last.get("changes") or [],
        },
        "prune": {
            "pruning": settings["pruning"],
            "keep_forever": settings["keep_forever"],
            "retain_days": settings["retain_days"],
            "window_start": settings["window_start"],
            "window_end": settings["window_end"],
            "idle_minutes": settings["idle_minutes"],
            "pending": prune_plan(store, bot_id, now),
        },
        "question": batched_question(entries),
        "habits": habits,
        "dreams": dreams[:4],
        "calibration": round(mean, 2) if count else None,
    }


def approve_habit(store: Store, bot_id: str, entry_id: str) -> dict:
    """Turn one suggested habit into a schedule. The nightly pass never does this."""
    store.get_bot(bot_id)
    path = notes_dir(store, bot_id) / "HABITS.md"
    text = _read(path)
    match = next((entry for entry in read_entries(text) if entry.entry_id == entry_id), None)
    if match is None or match.kind != "habit":
        raise StoreError("That habit is not in the notes.", 404)
    if match.status == "approved":
        raise StoreError("That routine is already approved.", 400)
    try:
        hour = int(match.hour)
    except ValueError:
        raise StoreError("That habit has no hour to schedule.", 400) from None
    if hour < 0 or hour > 23:
        raise StoreError("That habit has no hour to schedule.", 400)
    from easyagent.schedule import parse_cron

    expression = f"0 {hour} * * *"
    parse_cron(expression)
    schedule = {
        "id": new_id(),
        "prompt": match.body or match.title,
        "kind": "cron",
        "cron": expression,
        "paused": False,
        "last_slot": None,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    store.add_schedule(bot_id, schedule)
    match.status = "approved"
    messages = _all_messages(store, bot_id)
    commit_entries(store, bot_id, [match], messages)
    return schedule
