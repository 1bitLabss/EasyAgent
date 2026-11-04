"""Audit fixes: an unexpected crash still logs a job, and the job log stays capped."""

from test_schedule import asyncio_run, at, put_schedule, seed

from easyagent.schedule import run_due_schedules
from easyagent.store import Store


def test_unexpected_exception_still_logs_an_error_job(tmp_path, monkeypatch):
    async def boom(**kwargs):
        raise ValueError("sqlite is locked")

    monkeypatch.setattr("easyagent.llm.complete", boom)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=1)
    fired = asyncio_run(run_due_schedules(store, at(1020)))
    assert len(fired) == 1
    assert fired[0]["status"] == "error"
    assert "sqlite is locked" in fired[0]["error"]
    jobs = store.list_jobs(bot["id"])
    assert len(jobs) == 1
    assert jobs[0]["status"] == "error"
    assert "sqlite is locked" in jobs[0]["error"]
    stored = store.list_schedules(bot["id"])[0]
    assert stored["last_status"] == "error"
    assert "sqlite is locked" in stored["last_error"]
    # The crashed slot is consumed like any other.
    assert asyncio_run(run_due_schedules(store, at(1020))) == []


def test_append_job_caps_the_log_and_keeps_the_newest(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None, model="grid")
    bot = store.add_bot(name="Ann", endpoint_id=endpoint["id"], model=None)
    total = 520
    for number in range(total):
        store.append_job(bot["id"], {"id": str(number), "status": "ok", "result": f"run {number}"})
    jobs = store.list_jobs(bot["id"])
    assert len(jobs) == 500
    assert jobs[0]["result"] == f"run {total - 500}"
    assert jobs[-1]["result"] == f"run {total - 1}"
