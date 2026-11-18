"""Cron scan performance: jump-walk must equal the old minute-by-minute walk, and be fast.

The brute-force references below are the ORIGINAL algorithms (minute-by-minute walk
calling cron_matches / _wall_exists per minute), kept inline so the optimized
latest_cron_slot / next_cron_time can be diffed against them.
"""

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from easyagent.schedule import (
    ScheduleError,
    _wall_exists,
    cron_matches,
    latest_cron_slot,
    next_cron_time,
    parse_cron,
)

CHICAGO = ZoneInfo("America/Chicago")
YEAR_MINUTES = 366 * 24 * 60

EXPRESSIONS = [
    "0 8 * * *",              # daily
    "0 9 * * 1",              # weekly (Monday)
    "0 0 15 * *",             # monthly
    "0 0 1 1 *",              # yearly
    "0 0 30 2 *",             # never matches (Feb 30)
    "0 12 13 * 5",            # day-of-month OR day-of-week combo
    "*/17 9-17 * * 1-5",      # ranges and steps
    "30 6,18 * * *",          # hour list
]

# now values: a plain minute, the spring-forward gap and just after it,
# and both occurrences of the ambiguous fall-back hour in America/Chicago.
NOWS = [
    datetime(2026, 11, 17, 12, 0, tzinfo=CHICAGO),
    datetime(2026, 3, 8, 2, 30, tzinfo=CHICAGO),                    # gap wall time
    datetime(2026, 3, 8, 3, 30, tzinfo=CHICAGO),                    # just after spring-forward
    datetime(2026, 11, 1, 1, 30, tzinfo=CHICAGO),                   # fall-back, first occurrence
    datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=CHICAGO),           # fall-back, second occurrence
    datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc),               # plain UTC
]


def ref_latest_cron_slot(expression: str, now: datetime, created_at: datetime):
    """Original backward minute-by-minute walk (verbatim semantics)."""
    parse_cron(expression)
    cursor = now.replace(second=0, microsecond=0)
    created_minute = created_at.astimezone(cursor.tzinfo).replace(second=0, microsecond=0)
    earliest = created_minute + timedelta(minutes=1)
    if cursor < earliest:
        return None
    for _ in range(YEAR_MINUTES):
        if cursor < earliest:
            return None
        if cron_matches(expression, cursor):
            return cursor
        cursor -= timedelta(minutes=1)
    return None


def ref_next_cron_time(expression: str, after: datetime, zone):
    """Original forward minute-by-minute wall walk (verbatim semantics)."""
    parse_cron(expression)
    local = after.astimezone(zone)
    cursor = datetime(local.year, local.month, local.day, local.hour, local.minute) + timedelta(minutes=1)
    for _ in range(YEAR_MINUTES):
        y, m, d, h, mi = cursor.year, cursor.month, cursor.day, cursor.hour, cursor.minute
        if _wall_exists(y, m, d, h, mi, zone):
            aware = datetime(y, m, d, h, mi, tzinfo=zone)
            if cron_matches(expression, aware):
                return aware
        cursor += timedelta(minutes=1)
    return None


@pytest.mark.parametrize("expression", EXPRESSIONS)
@pytest.mark.parametrize("now", NOWS)
def test_latest_cron_slot_matches_brute_force(expression, now):
    created_far = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # A created_at bound that excludes the latest match for the daily/hourly crons.
    created_tight = now - timedelta(minutes=90)
    for created in (created_far, created_tight):
        expected = ref_latest_cron_slot(expression, now, created)
        got = latest_cron_slot(expression, now, created)
        assert got == expected, f"{expression!r} now={now} created={created}: {got} != {expected}"
        if expected is not None and got is not None:
            assert got.utcoffset() == expected.utcoffset()


@pytest.mark.parametrize("expression", EXPRESSIONS)
@pytest.mark.parametrize("now", NOWS)
def test_next_cron_time_matches_brute_force(expression, now):
    zone = now.tzinfo
    expected = ref_next_cron_time(expression, now, zone)
    got = next_cron_time(expression, now, zone)
    assert got == expected, f"{expression!r} after={now}: {got} != {expected}"
    if expected is not None and got is not None:
        assert got.utcoffset() == expected.utcoffset()


def test_created_at_bound_excludes_latest_match():
    now = datetime(2026, 11, 17, 12, 0, tzinfo=CHICAGO)
    created = datetime(2026, 11, 17, 9, 0, tzinfo=CHICAGO)  # 08:00 today is before creation
    assert latest_cron_slot("0 8 * * *", now, created) is None


@pytest.mark.parametrize("expression", ["not cron", "0 8 * *", ""])
def test_bad_expression_raises(expression):
    now = datetime(2026, 11, 17, 12, 0, tzinfo=CHICAGO)
    with pytest.raises(ScheduleError):
        latest_cron_slot(expression, now, now - timedelta(days=1))
    with pytest.raises(ScheduleError):
        next_cron_time(expression, now, CHICAGO)


def test_rare_crons_are_fast():
    """Budget test: the old minute-walk took 0.21 s / 4.09 s here; jumps must take milliseconds."""
    now = datetime(2026, 11, 17, 12, 0, tzinfo=CHICAGO)
    created = datetime(2026, 1, 1, tzinfo=CHICAGO)
    for expression in ("0 0 30 2 *", "0 0 1 1 *"):
        start = time.perf_counter()
        latest_cron_slot(expression, now, created)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.05, f"latest_cron_slot({expression!r}) took {elapsed:.3f}s"
        start = time.perf_counter()
        next_cron_time(expression, now, CHICAGO)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.05, f"next_cron_time({expression!r}) took {elapsed:.3f}s"
