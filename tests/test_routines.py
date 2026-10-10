"""Routines: schedules that post into the chat, yield to a live turn, and stay inside the guardrails."""

import asyncio
import json
import threading
from datetime import datetime, timezone, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from easyagent import gate
from easyagent.app import create_app
from easyagent.llm import _usage_counts, bind_purpose, reset_purpose
from easyagent.safety import is_unattended, list_pending, read_audit, resolve_card, unattended, attend
from easyagent.schedule import (
    ScheduleError,
    compile_routine,
    local_zone_name,
    next_cron_time,
    preview,
    public_cron,
    run_due_schedules,
)
from easyagent.store import Store
from easyagent.tools import ToolRequest, execute
from easyagent.unread import unread_snapshot
from tests.test_schedule import at, chat_blobs, put_schedule, room_blobs, seed


def test_weekday_preview_uses_local_words():
    zone = ZoneInfo("America/Chicago")
    now = datetime(2026, 10, 9, 7, 0, tzinfo=zone)
    record = compile_routine({
        "name": "Morning briefing",
        "prompt": "Summarize the morning.",
        "weekdays": "8:00 AM",
        "timezone": "America/Chicago",
        "created_at": "2026-10-01T00:00:00+00:00",
    })
    text = preview(record, now)
    assert text.startswith("Every weekday at 8:00 AM CT")
    assert "next: Fri Oct 9 8:00 AM" in text


def test_chicago_dst_skips_the_missing_hour_and_fires_the_repeated_hour_once():
    zone = ZoneInfo("America/Chicago")
    # 2026-03-08 springs forward at 2:00 AM. 2:30 does not exist that day.
    after = datetime(2026, 3, 7, 3, 0, tzinfo=zone)
    nxt = next_cron_time("30 2 * * *", after, zone)
    assert nxt is not None
    assert (nxt.year, nxt.month, nxt.day, nxt.hour, nxt.minute) == (2026, 3, 9, 2, 30)
    morning = next_cron_time("0 8 * * *", datetime(2026, 3, 7, 8, 0, tzinfo=zone), zone)
    assert morning is not None
    gap = morning.astimezone(timezone.utc) - datetime(2026, 3, 7, 8, 0, tzinfo=zone).astimezone(timezone.utc)
    assert gap.total_seconds() == 23 * 3600
    # 2026-11-01 falls back at 2:00 AM. 1:30 happens twice and the routine fires once.
    first = next_cron_time("30 1 * * *", datetime(2026, 11, 1, 0, 30, tzinfo=zone), zone)
    assert first is not None
    assert (first.month, first.day, first.hour, first.minute) == (11, 1, 1, 30)
    assert first.fold == 0
    second = next_cron_time("30 1 * * *", first, zone)
    assert second is not None
    assert (second.month, second.day) == (11, 2)


def test_public_schedule_refuses_a_gap_under_five_minutes():
    try:
        public_cron("* * * * *")
    except Exception as exc:
        assert "5 minutes" in str(exc)
    else:
        raise AssertionError("every minute was accepted")
    assert public_cron("0 8 * * 1-5") == "0 8 * * 1-5"


def test_missed_run_outside_twelve_hours_is_marked_and_not_run(tmp_path, monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return "late"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(
        store,
        bot,
        kind="cron",
        cron="0 0 1 1 *",
        timezone="UTC",
        created_at="2025-12-01T00:00:00+00:00",
        name="Year",
        quiet=False,
    )
    late = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
    assert asyncio.run(run_due_schedules(store, late)) == []
    assert calls == []
    stored = store.list_schedules(bot["id"])[0]
    assert stored["last_slot"] == "cron:2026-01-01T00:00"
    # Six hours after that midnight is still inside the window, so it runs once.
    store.save_schedules(bot["id"], [{**stored, "last_slot": None}])
    early = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    fired = asyncio.run(run_due_schedules(store, early))
    assert len(fired) == 1
    assert len(calls) == 1
    assert asyncio.run(run_due_schedules(store, early)) == []


def test_quiet_mode_records_nothing_new_and_does_not_post(tmp_path, monkeypatch):
    async def complete(**kwargs):
        return "Nothing new."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=1, quiet=True, name="Watch")
    chats = chat_blobs(tmp_path)
    rooms = room_blobs(tmp_path)
    fired = asyncio.run(run_due_schedules(store, at(1020)))
    assert fired[0]["status"] == "nothing new"
    assert fired[0]["result"] == "nothing new"
    assert chat_blobs(tmp_path) == chats
    assert room_blobs(tmp_path) == rooms
    assert unread_snapshot(store)["total"] == 0


def test_delete_moves_to_trash_and_restore_puts_it_back(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ann", "endpoint_id": endpoint["id"]}).json()
    created = client.post(
        f"/api/bots/{bot['id']}/schedules",
        json={"name": "Morning", "prompt": "Brief me.", "weekdays": "8:00 AM", "timezone": "America/Chicago"},
    )
    assert created.status_code == 200, created.text
    assert "Every weekday at 8:00 AM CT" in created.json()["preview"]
    schedule_id = created.json()["id"]
    removed = client.delete(f"/api/bots/{bot['id']}/schedules/{schedule_id}")
    assert removed.status_code == 200
    assert client.get(f"/api/bots/{bot['id']}/schedules").json() == []
    trash = client.get(f"/api/bots/{bot['id']}/schedules/trash").json()
    assert trash[0]["id"] == schedule_id
    restored = client.post(f"/api/bots/{bot['id']}/schedules/{schedule_id}/restore")
    assert restored.status_code == 200, restored.text
    assert client.get(f"/api/bots/{bot['id']}/schedules").json()[0]["name"] == "Morning"
    assert client.get(f"/api/bots/{bot['id']}/schedules/trash").json() == []


def test_a_routine_cannot_create_a_routine(tmp_path):
    store, bot, _endpoint = seed(tmp_path)
    spec = {
        "action": "create",
        "name": "Sneaky",
        "prompt": "Do it again.",
        "weekdays": "9:00 AM",
        "timezone": "UTC",
    }
    request = ToolRequest(kind="routine", action="create", path="Sneaky", body=spec["prompt"], call_arguments=json.dumps(spec))
    token = unattended()
    try:
        try:
            asyncio.run(execute(store, request, bot["id"]))
        except Exception as exc:
            assert "cannot create" in str(exc)
        else:
            raise AssertionError("the routine was allowed to create a routine")
    finally:
        attend(token)
    assert store.list_schedules(bot["id"]) == []
    assert any(row.get("rule") == "routine-create" and row.get("decision") == "block" for row in read_audit(store, bot["id"]))


def test_unattended_ask_becomes_a_card_and_expiry_is_deny(tmp_path, monkeypatch):
    victim = tmp_path.parent / f"{tmp_path.name}-note.txt"
    victim.write_text("keep", encoding="utf-8")
    calls = {"n": 0}

    async def complete(**kwargs):
        calls["n"] += 1
        blob = json.dumps(kwargs.get("messages") or [])
        if "needs you" in blob or calls["n"] > 1:
            return "I left that for you."
        return f"```shell\nrm {victim}\n```"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=1, name="Sweep", prompt="Tidy the note.")
    async def once():
        fired = await run_due_schedules(store, at(1020))
        # The card outlives the routine. Silence, within this same loop, is a denial.
        for _ in range(20):
            if not list_pending(bot["id"]):
                break
            await asyncio.sleep(0.05)
        return fired

    try:
        fired = asyncio.run(once())
        assert fired[0]["status"] == "ok"
        assert victim.read_text(encoding="utf-8") == "keep"
        assert list_pending(bot["id"]) == []
        bot_after = store.get_bot(bot["id"])
        assert bot_after.get("safety_denied")
        expired = [row for row in read_audit(store, bot["id"]) if row.get("decision") == "expired"]
        assert expired
        assert not is_unattended()
    finally:
        victim.unlink(missing_ok=True)


def test_background_run_does_not_mark_the_chat_running(tmp_path, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def complete(**kwargs):
        started.set()
        await release.wait()
        return "done later"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    store, bot, _endpoint = seed(tmp_path)
    chat = store.list_chats(bot["id"])[0]
    put_schedule(store, bot, kind="interval", every_minutes=1, name="Later")

    async def go():
        return await run_due_schedules(store, at(1020))

    async def watch():
        task = asyncio.create_task(go())
        await asyncio.wait_for(started.wait(), 2)
        live = store.get_chat(bot["id"], chat["id"])
        assert (live.get("run") or {}).get("status") != "running"
        # The composer can still take a message. Saving one must not be refused.
        live["messages"] = list(live.get("messages") or []) + [{"id": "22222222-2222-2222-2222-222222222222", "role": "user", "content": "still typing"}]
        store.save_chat(live)
        release.set()
        fired = await task
        return fired

    fired = asyncio.run(watch())
    assert fired[0]["output"] == "done later"
    stored = store.get_chat(bot["id"], chat["id"])
    assert any(item.get("content") == "still typing" for item in stored["messages"])
    assert any(item.get("routine_name") == "Later" for item in stored["messages"])
    assert (stored.get("run") or {}).get("status") != "running"
    snap = unread_snapshot(store)
    assert snap["total"] >= 1
    assert any(row["bot_id"] == bot["id"] and row["unread"] >= 1 for row in snap["chats"])


def test_one_slot_yields_to_a_chat(tmp_path):
    gate.reset_lanes()
    order = []
    held = threading.Event()
    release_first = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            who = "chat" if "LIVECHAT" in raw else "routine"
            order.append(("start", who))
            if who == "routine" and order.count(("start", "routine")) == 1:
                held.set()
                release_first.wait(2)
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": who}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            order.append(("end", who))

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = Store(tmp_path)
        store.ensure()
        endpoint = store.add_endpoint(name="llama", base_url=f"http://127.0.0.1:{port}/v1", api_key=None, model="grid", max_parallel=1)
        bot = store.add_bot(name="Ann", endpoint_id=endpoint["id"], model=None)
        put_schedule(store, bot, kind="interval", every_minutes=1, name="Night", prompt="routine ping")

        async def chat_call():
            await asyncio.wait_for(asyncio.to_thread(held.wait), 2)
            token = gate.bind_connection(endpoint, "Ann")
            try:
                from easyagent import llm
                return await llm.complete(
                    base_url=endpoint["base_url"],
                    api_key=None,
                    model="grid",
                    messages=[{"role": "user", "content": "LIVECHAT"}],
                    timeout=5,
                )
            finally:
                gate.reset_connection(token)

        async def both():
            routine = asyncio.create_task(run_due_schedules(store, at(1020)))
            chat = asyncio.create_task(chat_call())
            chat_text = await chat
            release_first.set()
            fired = await routine
            return chat_text, fired

        chat_text, fired = asyncio.run(both())
        assert chat_text == "chat"
        assert fired[0]["status"] == "ok"
        starts = [item for item in order if item[0] == "start"]
        # The routine's first request steps aside. The chat is not served at the same time.
        assert ("start", "chat") in starts
        assert order.count(("start", "routine")) >= 1
    finally:
        release_first.set()
        server.shutdown()
        gate.reset_lanes()


def test_windows_zone_maps_to_chicago_when_the_clock_has_no_iana_key(monkeypatch):
    class Bare(tzinfo):
        def tzname(self, _dt):
            return "Central Standard Time"

    monkeypatch.setattr("easyagent.schedule._local_tzinfo", lambda: Bare())
    monkeypatch.setattr("easyagent.schedule.windows_zone_name", lambda: "Central Standard Time")
    assert local_zone_name() == "America/Chicago"


def test_utc_and_a_windows_zone_are_recognized():
    fields = {
        "name": "Clock",
        "prompt": "Say the time.",
        "weekdays": "8:00 AM",
        "created_at": "2026-10-01T00:00:00+00:00",
    }
    utc = compile_routine({**fields, "timezone": "UTC"})
    assert utc["timezone"] == "UTC"
    pacific = compile_routine({**fields, "timezone": "Pacific Standard Time"})
    assert pacific["timezone"] == "America/Los_Angeles"
    try:
        compile_routine({**fields, "timezone": "Not A Real Zone"})
    except ScheduleError as exc:
        assert "not recognized" in str(exc)
    else:
        raise AssertionError("an unknown zone was accepted")


def test_tzdata_is_listed_for_a_user_install():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "tzdata" in requirements
    assert "tzdata" in project


def test_chat_creation_waits_for_a_confirm_card(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "5")
    store, bot, _endpoint = seed(tmp_path)
    spec = {
        "action": "create",
        "name": "Morning",
        "prompt": "Brief me.",
        "weekdays": "8:00 AM",
        "timezone": "America/Chicago",
    }
    request = ToolRequest(kind="routine", action="create", path="Morning", body=spec["prompt"], call_arguments=json.dumps(spec))

    async def go():
        task = asyncio.create_task(execute(store, request, bot["id"]))
        pending = []
        for _ in range(40):
            pending = list_pending(bot["id"])
            if pending:
                break
            await asyncio.sleep(0.01)
        assert pending, "the confirm card was not opened"
        assert pending[0]["rule"] == "routine"
        assert "Save this routine?" in pending[0]["why"]
        assert store.list_schedules(bot["id"]) == []
        resolve_card(pending[0]["id"], "approve")
        text = await task
        return text

    text = asyncio.run(go())
    assert "Saved" in text
    saved = store.list_schedules(bot["id"])
    assert saved[0]["name"] == "Morning"
    assert saved[0]["timezone"] == "America/Chicago"
    assert list_pending(bot["id"]) == []


def test_run_now_posts_without_consuming_the_next_slot(tmp_path, monkeypatch):
    async def complete(**kwargs):
        return "ran just now"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ann", "endpoint_id": endpoint["id"]}).json()
    created = client.post(
        f"/api/bots/{bot['id']}/schedules",
        json={"name": "Morning", "prompt": "Brief me.", "weekdays": "8:00 AM", "timezone": "America/Chicago"},
    )
    assert created.status_code == 200, created.text
    before = created.json().get("last_slot")
    ran = client.post(f"/api/bots/{bot['id']}/schedules/{created.json()['id']}/run")
    assert ran.status_code == 200, ran.text
    assert ran.json()["output"] == "ran just now"
    stored = client.get(f"/api/bots/{bot['id']}/schedules").json()[0]
    assert stored.get("last_slot") == before
    chat = client.get(f"/api/bots/{bot['id']}/ongoing").json()
    assert any(item.get("routine_name") == "Morning" and item.get("content") == "ran just now" for item in chat["messages"])


def test_a_routine_shell_uses_the_same_guarded_runner(tmp_path, monkeypatch):
    calls = {"n": 0}
    seen = []

    async def complete(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "```shell\necho hi\n```"
        return "The command printed hi."

    def runner(_store, command):
        seen.append(command)
        return "hi"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    monkeypatch.setattr("easyagent.tools._run_shell", runner)
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=1, name="Echo", prompt="Say hi.")
    fired = asyncio.run(run_due_schedules(store, at(1020)))
    assert fired[0]["status"] == "ok"
    assert seen == ["echo hi"]
    assert "hi" in fired[0]["output"]


def test_model_call_log_names_purpose_tokens_and_milliseconds(capsys):
    prompt, cached, generated = _usage_counts({
        "timings": {"prompt_n": 20, "predicted_n": 5, "prompt_n_cache": 7},
    })
    assert (prompt, cached, generated) == (20, 7, 5)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = json.dumps({
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 3,
                    "prompt_tokens_details": {"cached_tokens": 4},
                },
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        from easyagent import llm

        async def once():
            token = bind_purpose("routine")
            try:
                return await llm.complete(
                    base_url=f"http://127.0.0.1:{port}/v1",
                    api_key=None,
                    model="grid",
                    messages=[{"role": "user", "content": "ping"}],
                    timeout=5,
                )
            finally:
                reset_purpose(token)

        text = asyncio.run(once())
    finally:
        server.shutdown()
    assert text == "ok"
    logged = capsys.readouterr().out
    assert "model call: purpose=routine" in logged
    assert "prompt_tokens=11" in logged
    assert "cached_tokens=4" in logged
    assert "generated_tokens=3" in logged
    assert "ms=" in logged
