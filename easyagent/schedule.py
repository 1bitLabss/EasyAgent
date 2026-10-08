"""Schedules for one bot.

A schedule is either a 5-field cron expression in local time, or an
every-N-minutes interval. Each due slot runs once. A later start of the
process runs the latest slot that is already due, and does not run that
same slot again.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from easyagent import gate
from easyagent import llm
from easyagent.limits import DIRECTION_CAP
from easyagent.search import SearchError
from easyagent.notify import poke
from easyagent.tools import ToolError, complete_with_tools, redact
from easyagent.skills import extract_skills
from easyagent.store import Store, StoreError, new_id, now_iso

_RUN_LOCKS: dict[str, asyncio.Lock] = {}


class ScheduleError(ValueError):
    pass


def parse_cron(expression: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    """Return minute, hour, day, month, weekday sets, plus whether day and weekday were `*`."""
    fields = (expression or "").split()
    if len(fields) != 5:
        raise ScheduleError("Cron needs 5 fields: minute hour day month weekday.")
    minute = _field(fields[0], 0, 59)
    hour = _field(fields[1], 0, 23)
    day, day_star = _field(fields[2], 1, 31, star=True)
    month, _month_star = _field(fields[3], 1, 12, star=True)
    weekday, weekday_star = _field(fields[4], 0, 7, star=True)
    if 7 in weekday:
        weekday = (weekday - {7}) | {0}
    return minute, hour, day, month, weekday, day_star, weekday_star


def cron_matches(expression: str, when: datetime) -> bool:
    minute, hour, day, month, weekday, day_star, weekday_star = parse_cron(expression)
    if when.minute not in minute or when.hour not in hour or when.month not in month:
        return False
    # Cron Sunday is 0. Python Monday is 0.
    cron_dow = (when.weekday() + 1) % 7
    day_ok = when.day in day
    dow_ok = cron_dow in weekday
    if day_star and weekday_star:
        return True
    if day_star:
        return dow_ok
    if weekday_star:
        return day_ok
    return day_ok or dow_ok


def latest_cron_slot(expression: str, now: datetime, created_at: datetime) -> datetime | None:
    """The latest local minute at or before `now` that matches and starts after the schedule was created."""
    parse_cron(expression)
    cursor = now.replace(second=0, microsecond=0)
    created_minute = created_at.astimezone(cursor.tzinfo).replace(second=0, microsecond=0)
    earliest = created_minute + timedelta(minutes=1)
    if cursor < earliest:
        return None
    # A year of minutes is enough for any expression that fires at least yearly.
    for _ in range(366 * 24 * 60):
        if cursor < earliest:
            return None
        if cron_matches(expression, cursor):
            return cursor
        cursor -= timedelta(minutes=1)
    return None


def interval_slot(every_minutes: int, now: datetime, created_at: datetime) -> str | None:
    """Clock bucket of N minutes. The bucket must start at or after the schedule was created."""
    if every_minutes < 1:
        raise ScheduleError("Minutes must be at least 1.")
    width = every_minutes * 60
    bucket = int(now.timestamp()) // width
    start = bucket * width
    if start < created_at.timestamp():
        return None
    return f"every:{every_minutes}:{bucket}"


def due_slot(schedule: dict, now: datetime) -> str | None:
    created_at = _parse_time(schedule.get("created_at") or "")
    if created_at is None:
        return None
    kind = schedule.get("kind")
    if kind == "cron":
        slot = latest_cron_slot(schedule.get("cron") or "", now, created_at)
        if slot is None:
            return None
        return "cron:" + slot.strftime("%Y-%m-%dT%H:%M")
    if kind == "interval":
        return interval_slot(int(schedule.get("every_minutes") or 0), now, created_at)
    return None


def describe(schedule: dict) -> str:
    if schedule.get("kind") == "cron":
        return schedule.get("cron") or "cron"
    minutes = int(schedule.get("every_minutes") or 0)
    if minutes == 1:
        return "Every 1 minute"
    return f"Every {minutes} minutes"


async def run_due_schedules(store: Store, now: datetime | None = None) -> list[dict]:
    """Fire each due slot once. Paused schedules skip the slot without calling the endpoint."""
    moment = now.astimezone() if now is not None else datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.now().astimezone().tzinfo)
    lock = _RUN_LOCKS.get(str(store.root))
    if lock is None:
        lock = asyncio.Lock()
        _RUN_LOCKS[str(store.root)] = lock
    async with lock:
        fired: list[dict] = []
        for bot in store.list_bots():
            schedules = store.list_schedules(bot["id"])
            claimed: list[tuple[dict, str]] = []
            changed = False
            for schedule in schedules:
                try:
                    slot = due_slot(schedule, moment)
                except (ScheduleError, ValueError, TypeError):
                    continue
                if not slot or schedule.get("last_slot") == slot:
                    continue
                schedule["last_slot"] = slot
                changed = True
                if not schedule.get("paused"):
                    claimed.append((schedule, slot))
            if changed:
                store.save_schedules(bot["id"], schedules)
            for schedule, slot in claimed:
                entry = await _run_one(store, bot, schedule, slot)
                store.append_job(bot["id"], entry)
                if entry.get("status") == "error":
                    poke(store, "job_failed")
                fired.append(entry)
        return fired


async def schedule_loop(store: Store, stop: asyncio.Event) -> None:
    tick = float(os.environ.get("EASYAGENT_SCHEDULE_TICK", "5"))
    while not stop.is_set():
        try:
            await run_due_schedules(store)
        except Exception:
            # A bad schedule file must not kill the loop. The next tick tries again.
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(tick, 1))
        except asyncio.TimeoutError:
            continue


async def _run_one(store: Store, bot: dict, schedule: dict, slot: str) -> dict:
    started = now_iso()
    prompt = (schedule.get("prompt") or "").strip()
    base = {
        "id": new_id(),
        "schedule_id": schedule.get("id"),
        "slot": slot,
        "prompt": prompt,
        "started_at": started,
    }
    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except StoreError:
        endpoint = None
    if endpoint is None:
        return {**base, "status": "error", "output": "", "error": "This bot's endpoint is missing.", "finished_at": now_iso()}
    direction = (store.read_direction() or "").strip()
    if len(direction) > DIRECTION_CAP:
        direction = direction[: DIRECTION_CAP - 40].rstrip() + "\n[direction truncated]"
    name = bot.get("name") or "EasyAgent"
    system = (
        f"You are {name}. This run is a scheduled job, not a chat. "
        "Answer the prompt. To look something up, include a search fence with the query. "
        "The search runs on this computer. Do not delete chats, rooms, schedules, or bots.\n\n"
        f"# Direction\n{direction}"
    )
    conn = gate.bind_connection(endpoint, name)
    try:
        try:
            settled = await complete_with_tools(
                base_url=endpoint["base_url"],
                api_key=endpoint.get("api_key") or None,
                model=_chosen_model(bot, endpoint),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                store=store,
                bot_id=bot.get("id"),
            )
        except llm.ProviderError as exc:
            return {**base, "status": "error", "output": "", "error": redact(store, str(exc)), "finished_at": now_iso()}
        except SearchError as exc:
            return {**base, "status": "error", "output": "", "error": redact(store, str(exc)), "finished_at": now_iso()}
        except ToolError as exc:
            return {**base, "status": "error", "output": "", "error": redact(store, str(exc)), "finished_at": now_iso()}
        visible, skills = extract_skills(settled.text)
        visible = redact(store, visible)
        saved = []
        for skill in skills:
            try:
                stored = store.save_skill(skill)
            except StoreError:
                continue
            saved.append(stored["name"])
        if not visible.strip():
            visible = "Saved skill: " + ", ".join(saved) if saved else "(empty reply)"
        entry = {**base, "status": "ok", "output": visible, "error": "", "finished_at": now_iso()}
        if saved:
            entry["skills_saved"] = saved
        return entry
    finally:
        gate.reset_connection(conn)


def _chosen_model(bot: dict, endpoint: dict) -> str | None:
    bot_model = (bot.get("model") or "").strip()
    if bot_model:
        return bot_model
    endpoint_model = (endpoint.get("model") or "").strip()
    return endpoint_model or None


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _field(text: str, low: int, high: int, star: bool = False):
    """Parse one cron field into a set of allowed ints. `star=True` also returns whether it was `*`."""
    raw = (text or "").strip()
    if not raw:
        raise ScheduleError("Cron has an empty field.")
    is_star = raw == "*"
    values: set[int] = set()
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            raise ScheduleError("Cron has an empty field.")
        step = 1
        if "/" in piece:
            piece, step_text = piece.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError("Cron step must be a positive number.")
            step = int(step_text)
        if piece in {"*", ""}:
            start, end = low, high
        elif "-" in piece:
            left, right = piece.split("-", 1)
            start, end = _number(left, low, high), _number(right, low, high)
            if start > end:
                raise ScheduleError("Cron range is backwards.")
        else:
            start = end = _number(piece, low, high)
        for number in range(start, end + 1, step):
            values.add(number)
    if not values:
        raise ScheduleError("Cron field matches nothing.")
    if star:
        return values, is_star
    return values


def _number(text: str, low: int, high: int) -> int:
    if not text.isdigit():
        raise ScheduleError(f"Cron value {text!r} is not a number.")
    number = int(text)
    if number < low or number > high:
        raise ScheduleError(f"Cron value {number} is outside {low}–{high}.")
    return number
