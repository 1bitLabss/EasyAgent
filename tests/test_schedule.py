"""Schedules: cron and every-N-minutes, one slot, no double fire, chats stay."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.llm import ProviderError
from easyagent.schedule import cron_matches, due_slot, interval_slot, latest_cron_slot, parse_cron, run_due_schedules
from easyagent.store import Store


def at(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def seed(tmp_path: Path) -> tuple[Store, dict, dict]:
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None, model="grid")
    bot = store.add_bot(name="Ann", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    chat["title"] = "kept"
    chat["messages"] = [{"role": "user", "content": "keep-me"}]
    store.save_chat(chat)
    room = store.create_room(name="Desk")
    room["bot_ids"] = [bot["id"]]
    room["messages"] = [{"speaker": "user", "speaker_name": "You", "content": "room-line"}]
    store.save_room(room)
    return store, bot, endpoint


def put_schedule(store: Store, bot: dict, **fields) -> dict:
    schedule = {
        "id": "11111111-1111-1111-1111-111111111111",
        "prompt": "schedule-ping",
        "paused": False,
        "last_slot": None,
        "created_at": at(1000.5).isoformat(),
        **fields,
    }
    store.add_schedule(bot["id"], schedule)
    return schedule


def chat_blobs(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted((root / "bots").glob("*/chats/*.json"))}


def room_blobs(root: Path) -> dict[str, bytes]:
    folder = root / "rooms"
    if not folder.exists():
        return {}
    return {path.name: path.read_bytes() for path in sorted(folder.glob("*.json"))}


def test_cron_matches_steps_ranges_and_sunday():
    assert cron_matches("*/5 * * * *", datetime(2026, 10, 3, 22, 0))
    assert cron_matches("*/5 * * * *", datetime(2026, 10, 3, 22, 5))
    assert not cron_matches("*/5 * * * *", datetime(2026, 10, 3, 22, 1))
    assert cron_matches("10-12 0 * * *", datetime(2026, 10, 3, 0, 11))
    assert not cron_matches("10-12 0 * * *", datetime(2026, 10, 3, 0, 13))
    # 2026-10-05 is a Monday. Cron weekday 1 is Monday.
    assert cron_matches("0 9 * * 1", datetime(2026, 10, 5, 9, 0))
    assert not cron_matches("0 9 * * 1", datetime(2026, 10, 6, 9, 0))
    # 2026-10-04 is a Sunday. Both 0 and 7 match.
    sunday = datetime(2026, 10, 4, 8, 0)
    assert cron_matches("0 8 * * 0", sunday)
    assert cron_matches("0 8 * * 7", sunday)
    assert not cron_matches("0 8 * * 1", sunday)
    # Day-of-month and weekday both set means either one (vixie).
    assert cron_matches("0 0 1 * 1", datetime(2026, 10, 1, 0, 0))
    assert cron_matches("0 0 1 * 1", datetime(2026, 10, 5, 0, 0))
    with pytest.raises(Exception):
        parse_cron("* * *")
    with pytest.raises(Exception):
        parse_cron("*/0 * * * *")


def test_interval_waits_for_the_next_bucket_and_fires_once(tmp_path, monkeypatch):
    async def complete(**kwargs):
        return "ack"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=1)
    chats = chat_blobs(tmp_path)
    rooms = room_blobs(tmp_path)

    assert interval_slot(1, at(1000), at(1000.5)) is None
    assert asyncio_run(run_due_schedules(store, at(1000))) == []
    assert store.list_jobs(bot["id"]) == []

    first = asyncio_run(run_due_schedules(store, at(1020)))
    assert len(first) == 1
    assert first[0]["status"] == "ok"
    assert first[0]["slot"] == "every:1:17"
    assert first[0]["output"] == "ack"
    again = asyncio_run(run_due_schedules(store, at(1020)))
    assert again == []
    assert len(store.list_jobs(bot["id"])) == 1
    assert chat_blobs(tmp_path) == chats
    assert room_blobs(tmp_path) == rooms


def test_catch_up_runs_only_the_latest_slot(tmp_path, monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return "once"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=1)
    fired = asyncio_run(run_due_schedules(store, at(1000.5 + 60 * 10)))
    assert len(fired) == 1
    assert len(calls) == 1
    assert fired[0]["slot"] == interval_slot(1, at(1000.5 + 600), at(1000.5))


def test_paused_consumes_the_slot_without_a_call(tmp_path, monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return "ran"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    schedule = put_schedule(store, bot, kind="interval", every_minutes=1, paused=True)
    assert asyncio_run(run_due_schedules(store, at(1020))) == []
    assert calls == []
    stored = store.list_schedules(bot["id"])[0]
    assert stored["last_slot"] == "every:1:17"
    stored["paused"] = False
    store.save_schedules(bot["id"], [stored])
    assert asyncio_run(run_due_schedules(store, at(1020))) == []
    nxt = asyncio_run(run_due_schedules(store, at(1080)))
    assert len(nxt) == 1
    assert nxt[0]["slot"] == "every:1:18"
    assert schedule["id"] == nxt[0]["schedule_id"]


def test_restart_does_not_double_fire_the_same_slot(tmp_path, monkeypatch):
    async def complete(**kwargs):
        return "ran"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=1)
    asyncio_run(run_due_schedules(store, at(1020)))
    restarted = Store(tmp_path)
    asyncio_run(run_due_schedules(restarted, at(1020)))
    assert len(restarted.list_jobs(bot["id"])) == 1


def test_provider_error_is_logged_and_chats_stay(tmp_path, monkeypatch):
    async def complete(**kwargs):
        raise ProviderError("Could not reach http://127.0.0.1:9/v1: down")

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=1)
    chats = chat_blobs(tmp_path)
    rooms = room_blobs(tmp_path)
    fired = asyncio_run(run_due_schedules(store, at(1020)))
    assert fired[0]["status"] == "error"
    assert "down" in fired[0]["error"]
    assert chat_blobs(tmp_path) == chats
    assert room_blobs(tmp_path) == rooms
    # The failed slot is consumed.
    assert asyncio_run(run_due_schedules(store, at(1020))) == []


def test_cron_slot_is_the_latest_local_minute_after_creation():
    created = datetime(2026, 10, 3, 22, 13, 40).astimezone()
    now = datetime(2026, 10, 3, 22, 13, 50).astimezone()
    assert latest_cron_slot("* * * * *", now, created) is None
    later = datetime(2026, 10, 3, 22, 16, 10).astimezone()
    slot = latest_cron_slot("*/5 * * * *", later, created)
    assert slot is not None
    assert slot.minute == 15
    assert due_slot(
        {"kind": "cron", "cron": "*/5 * * * *", "created_at": created.isoformat()},
        later,
    ) == "cron:" + slot.strftime("%Y-%m-%dT%H:%M")


def test_delete_schedule_leaves_bot_chats_and_job_log(tmp_path, monkeypatch):
    async def complete(**kwargs):
        return "ack"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=1)
    asyncio_run(run_due_schedules(store, at(1020)))
    bot_bytes = (tmp_path / "bots" / bot["id"] / "bot.json").read_bytes()
    chats = chat_blobs(tmp_path)
    log_bytes = (tmp_path / "bots" / bot["id"] / "job-log.json").read_bytes()
    deleted = store.delete_schedule(bot["id"], "11111111-1111-1111-1111-111111111111")
    assert deleted["prompt"] == "schedule-ping"
    assert store.list_schedules(bot["id"]) == []
    assert (tmp_path / "bots" / bot["id"] / "bot.json").read_bytes() == bot_bytes
    assert chat_blobs(tmp_path) == chats
    assert (tmp_path / "bots" / bot["id"] / "job-log.json").read_bytes() == log_bytes
    assert store.get_bot(bot["id"])["name"] == "Ann"


def test_api_add_pause_delete_does_not_touch_chats(tmp_path, monkeypatch):
    async def complete(**kwargs):
        return "ack"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ann", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    client_store = Store(tmp_path)
    full = client_store.get_chat(bot["id"], chat["id"])
    full["messages"] = [{"role": "user", "content": "keep-me"}]
    client_store.save_chat(full)
    before = chat_blobs(tmp_path)
    created = client.post(
        f"/api/bots/{bot['id']}/schedules",
        json={"prompt": "ping", "kind": "interval", "every_minutes": 2},
    )
    assert created.status_code == 200, created.text
    assert created.json()["label"] == "Every 2 minutes"
    bad = client.post(
        f"/api/bots/{bot['id']}/schedules",
        json={"prompt": "ping", "kind": "cron", "cron": "not cron"},
    )
    assert bad.status_code == 400
    paused = client.post(
        f"/api/bots/{bot['id']}/schedules/{created.json()['id']}/pause",
        json={"paused": True},
    )
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    removed = client.delete(f"/api/bots/{bot['id']}/schedules/{created.json()['id']}")
    assert removed.status_code == 200
    assert client.get(f"/api/bots/{bot['id']}/schedules").json() == []
    assert chat_blobs(tmp_path) == before
    assert client.get(f"/api/bots/{bot['id']}").json()["name"] == "Ann"
    listed = json.loads((tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text())
    assert listed["messages"][0]["content"] == "keep-me"


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
