"""Scheduled runs fire once, time out, stay off /yolo, and keep a delivery queue."""

import asyncio

from easyagent.delivery import abandon, claim, enqueue, mark_delivered, status_of
from easyagent.safety import is_unattended
from easyagent.schedule import run_due_schedules
from tests.test_schedule import at, put_schedule, seed


def test_a_missed_interval_runs_once_and_not_as_a_burst(tmp_path, monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        assert is_unattended()
        return "caught up"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=5, name="Watch", quiet=False)
    # Twenty missed buckets. Only the current slot runs, and a second pass is empty.
    moment = at(1000 + 5 * 60 * 20)
    fired = asyncio.run(run_due_schedules(store, moment))
    assert len(fired) == 1
    assert len(calls) == 1
    assert asyncio.run(run_due_schedules(store, moment)) == []
    chat = store.ongoing_chat(bot["id"])
    posted = [item for item in chat["messages"] if item.get("content") == "caught up"]
    assert len(posted) == 1


def test_a_routine_stops_when_it_passes_its_time_limit(tmp_path, monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return "too late"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    monkeypatch.setattr("easyagent.schedule.routine_second_budget", lambda: -1.0)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=5, name="Slow", quiet=False)
    fired = asyncio.run(run_due_schedules(store, at(4000)))
    assert fired[0]["status"] == "stopped"
    assert calls == []
    assert "time limit" in (fired[0]["error"] or "")


def test_a_killed_delivery_is_sent_once_on_the_next_start(tmp_path):
    store, bot, _endpoint = seed(tmp_path)
    delivery_id = enqueue(store, {
        "bot_id": bot["id"],
        "text": "queued-result",
        "routine_id": "sched",
        "routine_name": "Watch",
    })
    first = claim(store)
    assert first["delivery_id"] == delivery_id
    assert status_of(store, delivery_id) == "delivering"
    from easyagent.delivery import apply_one

    apply_one(store, first)
    abandon(store, delivery_id)
    second = claim(store)
    assert second is not None
    assert second["delivery_id"] == delivery_id
    apply_one(store, second)
    assert mark_delivered(store, delivery_id, second["token"]) is True
    assert claim(store) is None
    assert status_of(store, delivery_id) == "delivered"
    assert mark_delivered(store, delivery_id, second["token"]) is False
    chat = store.ongoing_chat(bot["id"])
    posted = [item for item in chat["messages"] if item.get("content") == "queued-result"]
    assert len(posted) == 1
    assert posted[0]["delivery_id"] == delivery_id
