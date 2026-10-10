"""Routines for one bot.

A routine is a saved prompt on a schedule. The schedule is a 5-field cron
expression, an every-N-minutes interval, or one moment. Each due slot runs
once. A start of the process runs the latest slot that is already due when
that slot is still inside the catch-up window, and does not replay a backlog.
The result is posted into the bot's ongoing chat.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from easyagent import gate
from easyagent import llm
from easyagent.limits import DIRECTION_CAP
from easyagent.notify import poke
from easyagent.store import Store, StoreError, new_id, now_iso

MIN_PUBLIC_MINUTES = 5
_QUIET = re.compile(r"(?i)^(nothing new|nothing changed|no change|quiet)\.?$")
_RUNNING: dict[str, dict] = {}
_CANCEL: dict[str, asyncio.Event] = {}
_WEEKDAYS = {
    "sunday": 0, "sun": 0,
    "monday": 1, "mon": 1,
    "tuesday": 2, "tue": 2, "tues": 2,
    "wednesday": 3, "wed": 3,
    "thursday": 4, "thu": 4, "thur": 4, "thurs": 4,
    "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
}
_DOW_NAME = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

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
    moment = _in_zone(now, zone_for(schedule))
    kind = schedule.get("kind")
    if kind == "cron":
        slot = latest_cron_slot(schedule.get("cron") or "", moment, created_at)
        if slot is None:
            return None
        return "cron:" + slot.strftime("%Y-%m-%dT%H:%M")
    if kind == "interval":
        return interval_slot(int(schedule.get("every_minutes") or 0), moment, created_at)
    if kind == "once":
        when = _parse_time(schedule.get("at") or "")
        if when is None or moment < when.astimezone(moment.tzinfo):
            return None
        return "once:" + when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def describe(schedule: dict) -> str:
    """Short label. Interval text stays 'Every N minutes' so older callers still match."""
    if schedule.get("kind") == "interval":
        minutes = int(schedule.get("every_minutes") or 0)
        if minutes == 1:
            return "Every 1 minute"
        return f"Every {minutes} minutes"
    if schedule.get("kind") == "once":
        return "Once"
    return plain_phrase(schedule) or (schedule.get("cron") or "cron")


async def run_due_schedules(store: Store, now: datetime | None = None) -> list[dict]:
    """Fire each due slot once. Paused schedules skip the slot without calling the endpoint.

    A slot older than the catch-up window is marked and not run. There is no backlog.
    """
    moment = now.astimezone() if now is not None else datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.now().astimezone().tzinfo)
    lock = _RUN_LOCKS.get(str(store.root))
    if lock is None:
        lock = asyncio.Lock()
        _RUN_LOCKS[str(store.root)] = lock
    async with lock:
        fired: list[dict] = []
        window = catchup_seconds()
        for bot in store.list_bots():
            schedules = store.list_schedules(bot["id"])
            claimed: list[tuple[dict, str]] = []
            changed = migrate_schedules(schedules)
            for schedule in schedules:
                try:
                    slot = due_slot(schedule, moment)
                except (ScheduleError, ValueError, TypeError):
                    continue
                if not slot or schedule.get("last_slot") == slot:
                    continue
                schedule["last_slot"] = slot
                stamp_next(schedule, moment)
                changed = True
                if schedule.get("paused"):
                    continue
                age = slot_age(schedule, slot, moment)
                if age is not None and age > window:
                    continue
                claimed.append((dict(schedule), slot))
            if changed:
                store.save_schedules(bot["id"], schedules)
            for schedule, slot in claimed:
                entry = await _run_one(store, bot, schedule, slot)
                store.append_job(bot["id"], entry)
                _remember_run(store, bot["id"], schedule, entry, moment)
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


async def run_routine_now(store: Store, bot: dict, schedule: dict) -> dict:
    """One extra run. It does not consume the next scheduled slot."""
    entry = await _run_one(store, bot, schedule, "now:" + now_iso())
    store.append_job(bot["id"], entry)
    _remember_run(store, bot["id"], schedule, entry, datetime.now().astimezone())
    if entry.get("status") == "error":
        poke(store, "job_failed")
    return entry


async def _run_one(store: Store, bot: dict, schedule: dict, slot: str) -> dict:
    from easyagent.safety import _audit, attend, unattended
    from easyagent.skills import extract_skills
    from easyagent.tools import ToolError, parse_tools, redact, strip_tool_markup

    started = now_iso()
    prompt = (schedule.get("prompt") or "").strip()
    routine_name = (schedule.get("name") or prompt.splitlines()[0] if prompt else "") or "Routine"
    base = {
        "id": new_id(),
        "schedule_id": schedule.get("id"),
        "name": routine_name,
        "slot": slot,
        "prompt": prompt,
        "started_at": started,
    }
    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except StoreError:
        endpoint = None
    if endpoint is None:
        entry = {**base, "status": "error", "output": "", "result": "", "error": "This bot's endpoint is missing.", "finished_at": now_iso()}
        _deliver(store, bot, schedule, entry)
        return entry
    direction = (store.read_direction() or "").strip()
    if len(direction) > DIRECTION_CAP:
        direction = direction[: DIRECTION_CAP - 40].rstrip() + "\n[direction truncated]"
    name = bot.get("name") or "EasyAgent"
    quiet = bool(schedule.get("quiet"))
    system = (
        f"You are {name}. This run is a routine named {routine_name}, not a live chat. "
        "Answer the prompt. To look something up, include a search fence or a files fence. "
        "Do not create, edit, enable, or delete routines. Do not change safety settings. "
        "Do not delete chats, rooms, schedules, or bots."
    )
    if quiet:
        system += " If nothing changed and there is nothing worth saying, reply with exactly: nothing new"
    system += f"\n\n# Direction\n{direction}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt or routine_name}]
    schedule_id = str(schedule.get("id") or "")
    cancel = asyncio.Event()
    if schedule_id:
        _CANCEL[schedule_id] = cancel
        _RUNNING[schedule_id] = {"bot_id": bot.get("id"), "name": routine_name, "schedule_id": schedule_id}
    token = unattended()
    conn = gate.bind_connection(endpoint, name)
    status = "ok"
    error = ""
    visible = ""
    try:
        try:
            visible = await _routine_loop(
                store,
                bot,
                endpoint,
                messages,
                cancel,
            )
        except _Stopped as exc:
            status = "stopped"
            error = str(exc)
            visible = visible or str(exc)
        except llm.ProviderError as exc:
            status = "error"
            error = redact(store, str(exc))
        except ToolError as exc:
            status = "error"
            error = redact(store, str(exc))
        else:
            visible, skills = extract_skills(visible)
            visible = redact(store, strip_tool_markup(visible))
            saved = []
            for skill in skills:
                try:
                    stored = store.save_skill(skill)
                    from easyagent.learn import mark_origin

                    mark_origin(store, stored["name"], "user")
                except StoreError:
                    continue
                saved.append(stored["name"])
            if not visible.strip():
                visible = "Saved skill: " + ", ".join(saved) if saved else "(empty reply)"
            if quiet and is_quiet_text(visible):
                status = "nothing new"
        entry = {
            **base,
            "status": status,
            "output": visible if status != "nothing new" else "",
            "result": "nothing new" if status == "nothing new" else visible,
            "error": error,
            "finished_at": now_iso(),
        }
        _audit(store, bot.get("id"), {
            "at": now_iso(),
            "decision": "routine",
            "rule": "routine-run",
            "why": routine_name,
            "detail": status,
        })
        _deliver(store, bot, schedule, entry)
        return entry
    finally:
        attend(token)
        gate.reset_connection(conn)
        if schedule_id:
            _CANCEL.pop(schedule_id, None)
            _RUNNING.pop(schedule_id, None)


class _Stopped(Exception):
    pass


def catchup_seconds() -> float:
    raw = (os.environ.get("EASYAGENT_ROUTINE_CATCHUP_HOURS") or "12").strip()
    try:
        return max(0.0, float(raw)) * 3600
    except ValueError:
        return 12 * 3600


def routine_step_budget() -> int:
    raw = (os.environ.get("EASYAGENT_ROUTINE_STEPS") or "8").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


def routine_second_budget() -> float:
    raw = (os.environ.get("EASYAGENT_ROUTINE_SECONDS") or "600").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 600.0


# Windows TimeZoneKeyName values. The IANA name is what ZoneInfo accepts.
_WINDOWS_ZONES = {
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest",
    "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul",
    "Israel Standard Time": "Asia/Jerusalem",
    "Arabic Standard Time": "Asia/Baghdad",
    "Arab Standard Time": "Asia/Riyadh",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Pakistan Standard Time": "Asia/Karachi",
    "India Standard Time": "Asia/Kolkata",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "E. Australia Standard Time": "Australia/Brisbane",
    "W. Australia Standard Time": "Australia/Perth",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Alaskan Standard Time": "America/Anchorage",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time": "America/Denver",
    "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York",
    "SA Pacific Standard Time": "America/Bogota",
    "SA Western Standard Time": "America/La_Paz",
    "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Buenos_Aires",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    "Dateline Standard Time": "Etc/GMT+12",
}


def windows_zone_name() -> str:
    """The Windows zone key. Empty on other systems and when the registry is closed."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
            value, _kind = winreg.QueryValueEx(key, "TimeZoneKeyName")
    except OSError:
        return ""
    return str(value or "").strip()


def iana_zone(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    mapped = _WINDOWS_ZONES.get(raw)
    if mapped:
        return mapped
    return raw


def _local_tzinfo():
    return datetime.now().astimezone().tzinfo


def local_zone_name() -> str:
    tz = _local_tzinfo()
    key = getattr(tz, "key", None) if tz is not None else None
    if isinstance(key, str) and key and _zone_ok(key):
        return iana_zone(key)
    windows = windows_zone_name()
    if windows and _zone_ok(windows):
        return iana_zone(windows)
    label = ""
    if tz is not None:
        try:
            label = tz.tzname(None) or ""
        except Exception:
            label = ""
    if label and _zone_ok(label):
        return iana_zone(label)
    return "UTC"


def zone_for(schedule: dict):
    name = iana_zone((schedule.get("timezone") or "").strip())
    if not name:
        return None
    if name in {"UTC", "Etc/UTC", "GMT", "Etc/GMT"}:
        try:
            return ZoneInfo("UTC")
        except ZoneInfoNotFoundError:
            return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def _in_zone(moment: datetime, zone: ZoneInfo | None) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if zone is None:
        return moment
    return moment.astimezone(zone)


def migrate_schedules(schedules: list[dict]) -> bool:
    """Fill fields older records did not have. Returns whether anything changed."""
    changed = False
    zone = local_zone_name()
    for item in schedules:
        if not isinstance(item, dict):
            continue
        if not (item.get("name") or "").strip():
            prompt = (item.get("prompt") or "Routine").strip()
            item["name"] = (prompt.splitlines()[0] if prompt else "Routine")[:80] or "Routine"
            changed = True
        if not (item.get("timezone") or "").strip():
            item["timezone"] = zone
            changed = True
        if "quiet" not in item:
            item["quiet"] = False
            changed = True
        if "last_run_at" not in item:
            item["last_run_at"] = None
            changed = True
    return changed


def is_quiet_text(text: str) -> bool:
    return bool(_QUIET.match((text or "").strip()))


def slot_moment(schedule: dict, slot: str) -> datetime | None:
    zone = zone_for(schedule) or timezone.utc
    if slot.startswith("cron:"):
        try:
            naive = datetime.fromisoformat(slot.split(":", 1)[1])
        except ValueError:
            return None
        return naive.replace(tzinfo=zone)
    if slot.startswith("every:"):
        parts = slot.split(":")
        if len(parts) != 3:
            return None
        try:
            minutes = int(parts[1])
            bucket = int(parts[2])
        except ValueError:
            return None
        return datetime.fromtimestamp(bucket * minutes * 60, tz=timezone.utc)
    if slot.startswith("once:"):
        return _parse_time(slot.split(":", 1)[1])
    return None


def slot_age(schedule: dict, slot: str, now: datetime) -> float | None:
    moment = slot_moment(schedule, slot)
    if moment is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - moment.astimezone(now.tzinfo)).total_seconds()


def stamp_next(schedule: dict, now: datetime) -> None:
    nxt = next_run(schedule, now)
    schedule["next_run"] = nxt.isoformat() if nxt else None


def _remember_run(store: Store, bot_id: str, schedule: dict, entry: dict, now: datetime) -> None:
    rows = store.list_schedules(bot_id)
    for row in rows:
        if row.get("id") != schedule.get("id"):
            continue
        row["last_run_at"] = entry.get("finished_at")
        if schedule.get("kind") == "once":
            row["paused"] = True
        stamp_next(row, now)
    store.save_schedules(bot_id, rows)


def _deliver(store: Store, bot: dict, schedule: dict, entry: dict) -> None:
    """Post a finished routine into the ongoing chat. Quiet runs stay in the log only."""
    status = entry.get("status")
    if status == "nothing new":
        return
    name = (schedule.get("name") or entry.get("name") or "Routine").strip() or "Routine"
    if status == "error":
        text = "The routine could not run. " + (entry.get("error") or "Something went wrong.")
    elif status == "stopped":
        text = entry.get("error") or "Stopped. The routine ran past its limit."
    else:
        text = (entry.get("output") or "").strip() or "(empty reply)"
    store.append_routine_message(
        bot["id"],
        text,
        routine_id=str(schedule.get("id") or ""),
        routine_name=name,
    )


def active_routines(bot_id: str) -> list[dict]:
    return [dict(item) for item in _RUNNING.values() if item.get("bot_id") == bot_id]


def cancel_schedule(schedule_id: str) -> bool:
    event = _CANCEL.get(schedule_id)
    if event is None:
        return False
    event.set()
    return True


def cancel_bot(bot_id: str) -> int:
    stopped = 0
    for schedule_id, info in list(_RUNNING.items()):
        if info.get("bot_id") == bot_id and cancel_schedule(schedule_id):
            stopped += 1
    return stopped


def pause_all(store: Store) -> int:
    count = 0
    for bot in store.list_bots():
        rows = store.list_schedules(bot["id"])
        changed = False
        for row in rows:
            if not row.get("paused"):
                row["paused"] = True
                changed = True
                count += 1
        if changed:
            store.save_schedules(bot["id"], rows)
    host = store.read_host()
    host["paused_all"] = True
    store.save_host(host)
    return count


def _clock(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    suffix = "AM" if moment.hour < 12 else "PM"
    return f"{moment.strftime('%a %b')} {moment.day} {hour}:{moment.minute:02d} {suffix}"


def _time_of_day(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    shown = hour % 12 or 12
    return f"{shown}:{minute:02d} {suffix}"


def friendly_zone(moment: datetime) -> str:
    abbr = moment.tzname() or ""
    folded = {
        "CST": "CT", "CDT": "CT",
        "EST": "ET", "EDT": "ET",
        "PST": "PT", "PDT": "PT",
        "MST": "MT", "MDT": "MT",
    }
    if abbr in folded:
        return folded[abbr]
    if abbr:
        return abbr
    key = getattr(moment.tzinfo, "key", "") or ""
    return key or "local"


def plain_phrase(schedule: dict) -> str:
    zone = zone_for(schedule)
    sample = datetime.now(zone or timezone.utc)
    label = friendly_zone(sample) if zone else ""
    tail = f" {label}" if label else ""
    if schedule.get("kind") == "interval":
        minutes = int(schedule.get("every_minutes") or 0)
        if minutes and minutes % 60 == 0:
            hours = minutes // 60
            if hours == 1:
                return "Every hour"
            return f"Every {hours} hours"
        if minutes == 1:
            return "Every 1 minute"
        return f"Every {minutes} minutes"
    if schedule.get("kind") == "once":
        when = _parse_time(schedule.get("at") or "")
        if when is None:
            return "Once"
        local = when.astimezone(zone) if zone else when.astimezone()
        return f"Once at {_clock(local)} {friendly_zone(local)}".strip()
    expression = schedule.get("cron") or ""
    try:
        minute, hour, _day, _month, weekday, day_star, weekday_star = parse_cron(expression)
    except ScheduleError:
        return expression or "cron"
    if len(minute) == 1 and len(hour) == 1 and day_star:
        at = _time_of_day(next(iter(hour)), next(iter(minute)))
        if weekday_star:
            return f"Every day at {at}{tail}".strip()
        days = set(weekday)
        if days == {1, 2, 3, 4, 5}:
            return f"Every weekday at {at}{tail}".strip()
        if len(days) == 1:
            return f"Every {_DOW_NAME[next(iter(days))]} at {at}{tail}".strip()
    return expression or "cron"


def preview(schedule: dict, now: datetime | None = None) -> str:
    phrase = plain_phrase(schedule)
    nxt = next_run(schedule, now or datetime.now().astimezone())
    if nxt is None:
        return phrase
    zone = zone_for(schedule)
    local = nxt.astimezone(zone) if zone else nxt.astimezone()
    return f"{phrase}, next: {_clock(local)}"


def next_run(schedule: dict, now: datetime) -> datetime | None:
    if schedule.get("paused"):
        return None
    zone = zone_for(schedule) or ZoneInfo(local_zone_name()) if _zone_ok(local_zone_name()) else timezone.utc
    if not isinstance(zone, ZoneInfo) and zone_for(schedule) is None:
        current = datetime.now().astimezone().tzinfo or timezone.utc
        zone = current
    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    kind = schedule.get("kind")
    if kind == "once":
        when = _parse_time(schedule.get("at") or "")
        if when is None or when <= moment:
            return None
        return when
    if kind == "interval":
        try:
            minutes = int(schedule.get("every_minutes") or 0)
        except (TypeError, ValueError):
            return None
        if minutes < 1:
            return None
        width = minutes * 60
        created = _parse_time(schedule.get("created_at") or "") or moment
        bucket = int(moment.timestamp()) // width
        start = bucket * width
        if start < created.timestamp():
            start = (int(created.timestamp()) // width) * width
            if start < created.timestamp():
                start += width
        if start <= moment.timestamp():
            start += width
        return datetime.fromtimestamp(start, tz=timezone.utc)
    if kind == "cron":
        try:
            return next_cron_time(schedule.get("cron") or "", moment, zone)
        except ScheduleError:
            return None
    return None


def _zone_ok(name: str) -> bool:
    candidate = iana_zone(name)
    if candidate in {"UTC", "Etc/UTC", "GMT", "Etc/GMT"}:
        return True
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return False
    except Exception:
        return False
    return True


def next_cron_time(expression: str, after: datetime, zone) -> datetime | None:
    """The next wall-clock match after `after`. A skipped hour is skipped. A repeated hour fires once."""
    parse_cron(expression)
    local = after.astimezone(zone)
    y, m, d, h, mi = _add_wall_minute(local.year, local.month, local.day, local.hour, local.minute)
    for _ in range(366 * 24 * 60):
        if _wall_exists(y, m, d, h, mi, zone):
            aware = datetime(y, m, d, h, mi, tzinfo=zone)
            if cron_matches(expression, aware):
                return aware
        y, m, d, h, mi = _add_wall_minute(y, m, d, h, mi)
    return None


def _add_wall_minute(y: int, m: int, d: int, h: int, mi: int):
    cursor = datetime(y, m, d, h, mi) + timedelta(minutes=1)
    return cursor.year, cursor.month, cursor.day, cursor.hour, cursor.minute


def _wall_exists(y: int, m: int, d: int, h: int, mi: int, zone) -> bool:
    """A wall time in a spring-forward gap does not exist. A repeated hour is the first one."""
    try:
        aware = datetime(y, m, d, h, mi, tzinfo=zone)
    except ValueError:
        return False
    back = aware.astimezone(timezone.utc).astimezone(zone)
    return (back.year, back.month, back.day, back.hour, back.minute) == (y, m, d, h, mi)


def public_interval(minutes: int) -> int:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < MIN_PUBLIC_MINUTES or minutes > 7 * 24 * 60:
        raise ScheduleError("A routine waits at least 5 minutes between runs.")
    return minutes


def public_cron(expression: str) -> str:
    expression = " ".join((expression or "").split())
    minute, *_rest = parse_cron(expression)
    mins = sorted(minute)
    if len(mins) > 1:
        gaps = [right - left for left, right in zip(mins, mins[1:])]
        gaps.append(mins[0] + 60 - mins[-1])
        if min(gaps) < MIN_PUBLIC_MINUTES:
            raise ScheduleError("A routine waits at least 5 minutes between runs.")
    return expression


def _parse_hhmm(text: str) -> tuple[int, int]:
    raw = (text or "").strip().lower().replace(".", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", raw)
    if not match:
        raise ScheduleError("Use a time like 8:00 AM.")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3)
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if suffix is None and hour > 23:
        raise ScheduleError("Use a time like 8:00 AM.")
    if minute > 59 or hour > 23:
        raise ScheduleError("Use a time like 8:00 AM.")
    return hour, minute


def compile_routine(fields: dict, *, created_at: str | None = None) -> dict:
    """Turn a form, a template, or a confirmed card into a stored routine."""
    preset = (fields.get("preset") or "").strip()
    template = next((item for item in TEMPLATES if item["id"] == preset), None) if preset else None
    if preset and template is None:
        raise ScheduleError("That template is not one of the built-in routines.")
    prompt = (fields.get("prompt") or "").strip() or (template["prompt"] if template else "")
    if not prompt:
        raise ScheduleError("Write a prompt for the routine.")
    if len(prompt) > 4000:
        raise ScheduleError("Prompt is too long (4000 characters max).")
    name = (fields.get("name") or "").strip() or (template["name"] if template else "") or (prompt.splitlines()[0][:80] or "Routine")
    if len(name) > 80:
        raise ScheduleError("Name is too long (80 characters max).")
    timezone_name = iana_zone((fields.get("timezone") or "").strip() or local_zone_name())
    if not _zone_ok(timezone_name):
        raise ScheduleError("That timezone is not recognized.")
    quiet = _as_bool(fields.get("quiet"))
    paused = _as_bool(fields.get("paused"))
    record: dict = {
        "id": (fields.get("id") or "").strip() or new_id(),
        "name": name,
        "prompt": prompt,
        "timezone": timezone_name,
        "quiet": quiet,
        "paused": paused,
        "last_slot": fields.get("last_slot"),
        "last_run_at": fields.get("last_run_at"),
        "created_at": created_at or fields.get("created_at") or now_iso(),
    }
    if template is not None:
        if template.get("kind") == "cron":
            record["kind"] = "cron"
            record["cron"] = public_cron(template["cron"])
        else:
            record["kind"] = "interval"
            record["every_minutes"] = public_interval(int(template["every_minutes"]))
        if "quiet" not in fields or fields.get("quiet") is None:
            record["quiet"] = bool(template.get("quiet"))
        stamp_next(record, datetime.now().astimezone())
        return record
    once = (fields.get("once") or "").strip()
    daily = (fields.get("daily") or "").strip()
    weekdays = (fields.get("weekdays") or "").strip()
    weekly = (fields.get("weekly") or "").strip()
    weekly_day = (fields.get("weekly_day") or "").strip()
    weekly_time = (fields.get("weekly_time") or "").strip()
    cron = (fields.get("cron") or "").strip()
    every_hours = fields.get("every_hours")
    every_minutes = fields.get("every_minutes")
    kind = (fields.get("kind") or "").strip()
    if once or kind == "once":
        when = _parse_time(once or fields.get("at") or "")
        if when is None:
            raise ScheduleError("Say when this routine should run once.")
        record["kind"] = "once"
        record["at"] = when.astimezone(timezone.utc).isoformat()
    elif daily:
        hour, minute = _parse_hhmm(daily)
        record["kind"] = "cron"
        record["cron"] = public_cron(f"{minute} {hour} * * *")
    elif weekdays:
        hour, minute = _parse_hhmm(weekdays)
        record["kind"] = "cron"
        record["cron"] = public_cron(f"{minute} {hour} * * 1-5")
    elif weekly or (weekly_day and weekly_time):
        text = weekly or f"{weekly_day} {weekly_time}"
        day_name, _, time_text = text.partition(" ")
        if not time_text:
            raise ScheduleError("Name the day and the time, like Sunday 10:00 AM.")
        dow = _WEEKDAYS.get(day_name.strip().lower())
        if dow is None:
            raise ScheduleError("Name a weekday, like Sunday.")
        hour, minute = _parse_hhmm(time_text)
        record["kind"] = "cron"
        record["cron"] = public_cron(f"{minute} {hour} * * {dow}")
    elif cron or kind == "cron":
        record["kind"] = "cron"
        record["cron"] = public_cron(cron)
    elif every_hours not in (None, "") or every_minutes not in (None, "") or kind == "interval":
        if every_hours not in (None, ""):
            try:
                hours = int(every_hours)
            except (TypeError, ValueError):
                raise ScheduleError("Hours must be a whole number.") from None
            minutes = hours * 60
        else:
            try:
                minutes = int(every_minutes)
            except (TypeError, ValueError):
                raise ScheduleError("Minutes must be a whole number.") from None
        record["kind"] = "interval"
        record["every_minutes"] = public_interval(minutes)
    else:
        raise ScheduleError("Choose a schedule: every few minutes, a time of day, weekdays, a weekday, once, or a 5-field cron.")
    stamp_next(record, datetime.now().astimezone())
    return record


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


TEMPLATES = [
    {
        "id": "morning-briefing",
        "name": "Morning briefing",
        "prompt": "Give me a short morning briefing from my notes: what is due, and one useful thing to start with. A few sentences.",
        "kind": "cron",
        "cron": "0 8 * * 1-5",
        "quiet": False,
        "blurb": "Weekdays at 8:00 AM.",
    },
    {
        "id": "watch-page",
        "name": "Watch a web page for changes",
        "prompt": "Check this page and tell me only what changed:\n\nhttps://example.com\n\nIf nothing changed, reply with exactly: nothing new",
        "kind": "interval",
        "every_minutes": 60,
        "quiet": True,
        "blurb": "Every hour. Quiet unless the page changed.",
    },
    {
        "id": "daily-notes",
        "name": "Daily summary of my notes",
        "prompt": "Summarize my notes from today in a short paragraph. Mention anything I should not forget.",
        "kind": "cron",
        "cron": "0 18 * * *",
        "quiet": False,
        "blurb": "Every day at 6:00 PM.",
    },
    {
        "id": "weekly-cleanup",
        "name": "Weekly cleanup suggestions",
        "prompt": "Look at my notes and files and suggest a short cleanup list. Suggest only. Never delete anything.",
        "kind": "cron",
        "cron": "0 10 * * 0",
        "quiet": False,
        "blurb": "Sundays at 10:00 AM. Suggestions only, never a delete.",
    },
]


def template_records() -> list[dict]:
    return [dict(item) for item in TEMPLATES]


def apply_confirmed(store: Store, bot_id: str, spec: dict) -> str:
    """Save, edit, or trash a routine after the person confirms the card."""
    action = (spec.get("action") or "create").strip().lower()
    if action == "create":
        record = compile_routine(spec)
        store.add_schedule(bot_id, record)
        store.note_first_routine()
        return f"Saved the routine {record['name']}."
    schedule_id = (spec.get("id") or "").strip()
    if not schedule_id:
        raise ScheduleError("Name which routine to change.")
    if action == "delete":
        deleted = store.delete_schedule(bot_id, schedule_id)
        return f"Moved {deleted.get('name') or 'the routine'} to Trash."
    rows = store.list_schedules(bot_id)
    match = next((item for item in rows if item.get("id") == schedule_id), None)
    if match is None:
        raise ScheduleError("That routine is not on this bot.")
    if action in {"pause", "resume"}:
        match["paused"] = action == "pause"
        stamp_next(match, datetime.now().astimezone())
        store.save_schedules(bot_id, rows)
        return "Paused that routine." if match["paused"] else "Resumed that routine."
    if action == "edit":
        merged = {**match, **{key: value for key, value in spec.items() if value not in (None, "")}}
        merged["id"] = match["id"]
        merged["created_at"] = match.get("created_at")
        merged["last_slot"] = match.get("last_slot")
        compiled = compile_routine(merged, created_at=match.get("created_at"))
        compiled["id"] = match["id"]
        compiled["last_slot"] = match.get("last_slot")
        compiled["paused"] = match.get("paused") if "paused" not in spec else _as_bool(spec.get("paused"))
        for index, item in enumerate(rows):
            if item.get("id") == schedule_id:
                rows[index] = compiled
        store.save_schedules(bot_id, rows)
        return f"Updated the routine {compiled['name']}."
    raise ScheduleError("A routine action is create, edit, or delete.")


async def _routine_loop(store: Store, bot: dict, endpoint: dict, messages: list[dict], cancel: asyncio.Event) -> str:
    from easyagent.search import SearchError
    from easyagent.tools import ToolError, execute, parse_tools, redact
    import easyagent.search as search_mod

    search_mod.bind_store(store)
    started = asyncio.get_running_loop().time()
    steps = 0
    limit = routine_step_budget()
    seconds = routine_second_budget()
    visible = ""
    while steps < limit:
        if cancel.is_set():
            raise _Stopped("Stopped.")
        if asyncio.get_running_loop().time() - started > seconds:
            raise _Stopped("Stopped. The routine ran past its time limit.")
        text = await _complete_yielding(endpoint, bot, messages, cancel)
        visible = text
        try:
            requests = parse_tools(text)
        except ToolError as exc:
            return redact(store, str(exc))
        if not requests:
            return text
        steps += 1
        if steps >= limit:
            raise _Stopped("Stopped. The routine ran past its step limit.")
        for request in requests:
            if cancel.is_set():
                raise _Stopped("Stopped.")
            try:
                if request.kind == "search":
                    result = await search_mod.web_search(request.body)
                else:
                    result = await execute(store, request, bot.get("id"))
            except (ToolError, SearchError) as exc:
                result = str(exc)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": redact(store, result)})
            text = ""
    raise _Stopped("Stopped. The routine ran past its step limit.")


async def _complete_yielding(endpoint: dict, bot: dict, messages: list[dict], cancel: asyncio.Event) -> str:
    while True:
        if cancel.is_set():
            raise _Stopped("Stopped.")
        purpose = llm.bind_purpose("routine")
        try:
            task = asyncio.create_task(llm.complete(
                base_url=endpoint["base_url"],
                api_key=endpoint.get("api_key") or None,
                model=_chosen_model(bot, endpoint),
                messages=messages,
                yield_to_chats=True,
            ))
        finally:
            llm.reset_purpose(purpose)
        waiter = asyncio.create_task(cancel.wait())
        done, _pending = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if cancel.is_set() and task not in done:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise _Stopped("Stopped.")
        waiter.cancel()
        try:
            return await task
        except llm.YieldLater:
            await asyncio.sleep(0.05)
            continue


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
