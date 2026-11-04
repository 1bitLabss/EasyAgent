"""Routine runs cannot overlap, Run now returns at once, and Stop stops a run.

Regression cover for the audited overlap guard, the stop route, and the
fire-and-return Run now route.
"""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.schedule import (
    ScheduleError,
    claim_routine_now,
    routine_running,
    run_due_schedules,
    run_routine_now,
)
from tests.test_schedule import at, put_schedule, seed


def wait_for_state(client, schedule_id, wanted, timeout=10.0):
    """Poll /api/routines until this routine's state is one of wanted."""
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        rows = client.get("/api/routines").json()
        row = next((item for item in rows if item["id"] == schedule_id), None)
        if row is not None and row["state"] in wanted:
            return row
        time.sleep(0.05)
    raise AssertionError(f"routine never reached {wanted}, last row: {row}")


def test_a_second_trigger_does_not_start_a_concurrent_run(tmp_path, monkeypatch):
    release = threading.Event()
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        await asyncio.to_thread(release.wait)
        return "ack"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    schedule = put_schedule(store, bot, kind="interval", every_minutes=1, name="Watch")

    async def scenario():
        first = asyncio.create_task(run_routine_now(store, bot, schedule))
        await asyncio.sleep(0.2)  # let the run claim and block inside the model call
        assert routine_running(schedule["id"])
        with pytest.raises(ScheduleError):
            await run_routine_now(store, bot, schedule)
        # The scheduler tick also skips the routine while its run is in flight.
        assert await run_due_schedules(store, at(1020)) == []
        assert len(calls) == 1
        release.set()
        entry = await first
        assert entry["status"] == "ok"
        assert not routine_running(schedule["id"])
        # The skipped slot is not lost. It runs once the routine is free again.
        fired = await run_due_schedules(store, at(1020))
        assert len(fired) == 1
        assert len(calls) == 2

    asyncio.run(scenario())


def test_claim_refuses_a_second_holder(tmp_path):
    _store, bot, _endpoint = seed(tmp_path)
    schedule = {"id": "aaaaaaaa-1111-1111-1111-111111111111", "name": "Guard"}
    assert claim_routine_now(bot, schedule) is True
    assert claim_routine_now(bot, schedule) is False
    assert routine_running(schedule["id"])
    from easyagent import schedule as schedule_mod

    schedule_mod._RUNNING.pop(schedule["id"], None)
    schedule_mod._CANCEL.pop(schedule["id"], None)
    assert not routine_running(schedule["id"])


def test_run_now_returns_promptly_and_refuses_a_second_click(tmp_path, monkeypatch):
    hold = threading.Event()

    async def slow(**kwargs):
        await asyncio.to_thread(hold.wait)
        return "ack"

    monkeypatch.setattr("easyagent.llm.complete", slow)
    store, bot, _endpoint = seed(tmp_path)
    schedule = put_schedule(store, bot, kind="once", at="2099-01-01T00:00:00+00:00", name="Slow")
    app = create_app(tmp_path)
    try:
        with TestClient(app) as client:
            started = time.monotonic()
            ran = client.post(f"/api/bots/{bot['id']}/schedules/{schedule['id']}/run")
            elapsed = time.monotonic() - started
            assert ran.status_code == 200, ran.text
            assert ran.json()["started"] is True
            assert elapsed < 1.0, "Run now waited for the whole model call"
            again = client.post(f"/api/bots/{bot['id']}/schedules/{schedule['id']}/run")
            assert again.status_code == 409
            hold.set()
            wait_for_state(client, schedule["id"], ("ok", "done"), timeout=15)
            jobs = client.get(f"/api/bots/{bot['id']}/jobs").json()
            assert any(job["status"] == "ok" and job["output"] == "ack" for job in jobs)
    finally:
        hold.set()


def test_stop_route_stops_a_running_routine(tmp_path, monkeypatch):
    hold = threading.Event()
    inside = threading.Event()

    async def hang(**kwargs):
        inside.set()
        await asyncio.to_thread(hold.wait)
        return "late"

    monkeypatch.setattr("easyagent.llm.complete", hang)
    store, bot, _endpoint = seed(tmp_path)
    schedule = put_schedule(store, bot, kind="once", at="2099-01-01T00:00:00+00:00", name="Stuck")
    app = create_app(tmp_path)
    try:
        with TestClient(app) as client:
            missing = client.post(f"/api/bots/{bot['id']}/schedules/deadbeef0000/stop")
            assert missing.status_code == 404
            idle = client.post(f"/api/bots/{bot['id']}/schedules/{schedule['id']}/stop")
            assert idle.status_code == 409
            ran = client.post(f"/api/bots/{bot['id']}/schedules/{schedule['id']}/run")
            assert ran.status_code == 200, ran.text
            assert inside.wait(10)
            wait_for_state(client, schedule["id"], ("running", "queued"))
            stopped = client.post(f"/api/bots/{bot['id']}/schedules/{schedule['id']}/stop")
            assert stopped.status_code == 200, stopped.text
            hold.set()
            wait_for_state(client, schedule["id"], ("stopped", "done"), timeout=15)
            jobs = client.get(f"/api/bots/{bot['id']}/jobs").json()
            assert any(job["status"] == "stopped" for job in jobs)
            assert not routine_running(schedule["id"])
    finally:
        hold.set()
