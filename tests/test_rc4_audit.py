"""Feature-truth fixes for 0.3.46 rc4. One test for each audit item, in order."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.testclient import TestClient

from easyagent.features import CAPABILITY_RULE, capability_block
from easyagent.llm import _fence_for_call, native_tools
from easyagent.registry import reset
from easyagent.schedule import OPEN_NOTE, ScheduleError, compile_routine, plain_phrase, run_due_schedules, tzdata_warning
from easyagent.tools import ToolRequest, execute, parse_tools

ROOT = Path(__file__).resolve().parents[1]


def _system(store, bot_id, chat_id) -> str:
    from easyagent.app import _turn_messages

    _endpoint, messages = _turn_messages(store, bot_id, store.get_chat(bot_id, chat_id))
    return messages[0]["content"]


def _app(tmp_path):
    from easyagent.app import create_app

    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return app, client, bot, chat


def test_routines_and_the_capability_list_are_at_the_top(tmp_path, monkeypatch):
    """Cron, reminders, and the routine tool are named before the long computer paragraph."""
    reset()
    monkeypatch.setattr(sys, "platform", "win32")
    _app_obj, client, bot, chat = _app(tmp_path)
    system = _system(_app_obj.state.store, bot["id"], chat["id"])
    head = system[: system.index("# This computer")]
    for word in ("cron", "reminder", "recurring", "every morning"):
        assert word in head.lower(), word
    for field in ("name", "prompt", "cron", "daily", "weekdays", "weekly", "once", "every_minutes", "timezone", "quiet"):
        assert field in head, field
    assert OPEN_NOTE in head
    assert "never say you can't do something on this list" in head.lower()
    assert system.index("# What you can do") < system.index("# Routines") < system.index("# Direction")
    assert system.index("# Routines") < system.index("# This computer")
    block = capability_block()
    assert block in system
    assert "web search, browser" in block
    assert "skills, memory" in block
    assert "YOLO" in block
    assert "nightly learning" in block
    assert "while /yolo is on" in block
    assert CAPABILITY_RULE in block
    prompt = (ROOT / "easyagent" / "prompt.py").read_text(encoding="utf-8")
    assert CAPABILITY_RULE not in prompt
    names = [tool["function"]["name"] for tool in native_tools()]
    assert "routine" in names
    assert abs(names.index("routine") - names.index("skill")) == 1
    described = next(tool for tool in native_tools() if tool["function"]["name"] == "routine")
    text = described["function"]["description"].lower()
    assert "create" in text and "edit" in text and "delete" in text and "list" in text
    assert "cron" in text and "reminder" in text and "every morning" in text
    listed = parse_tools(_fence_for_call({"name": "routine", "arguments": {"action": "list"}}))[0]
    assert listed.kind == "routine" and listed.action == "list"
    created = parse_tools(_fence_for_call({
        "name": "routine",
        "arguments": {
            "action": "create",
            "name": "Kiln",
            "prompt": "Check the kiln.",
            "weekdays": "8:00 AM",
            "timezone": "America/Chicago",
        },
    }))[0]
    assert created.action == "create"
    assert created.path == "Kiln"
    assert "Check the kiln." in created.body
    client.close()


def test_once_is_read_in_the_routine_timezone():
    """9:00 in Chicago is 9:00 AM CT. UTC stays 9:00 UTC. A summer date keeps daylight time."""
    fields = {
        "name": "Kiln",
        "prompt": "Check the kiln.",
        "created_at": "2026-10-01T00:00:00+00:00",
    }
    chicago = compile_routine({**fields, "once": "2026-10-23 09:00", "timezone": "America/Chicago"})
    at = datetime.fromisoformat(chicago["at"]).astimezone(timezone.utc)
    assert at == datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
    phrase = plain_phrase(chicago)
    assert "9:00 AM" in phrase
    assert "CT" in phrase
    assert "4:00 AM" not in phrase

    utc = compile_routine({**fields, "once": "2026-10-23 09:00", "timezone": "UTC"})
    utc_at = datetime.fromisoformat(utc["at"]).astimezone(timezone.utc)
    assert utc_at == datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
    utc_phrase = plain_phrase(utc)
    assert "9:00 AM" in utc_phrase
    assert "UTC" in utc_phrase

    summer = compile_routine({**fields, "once": "2026-07-04 09:00", "timezone": "America/Chicago"})
    summer_at = datetime.fromisoformat(summer["at"]).astimezone(timezone.utc)
    assert summer_at == datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc)
    summer_phrase = plain_phrase(summer)
    assert "9:00 AM" in summer_phrase
    assert "CT" in summer_phrase
    winter = compile_routine({**fields, "once": "2026-01-15 09:00", "timezone": "America/Chicago"})
    winter_at = datetime.fromisoformat(winter["at"]).astimezone(timezone.utc)
    assert winter_at == datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
    assert "9:00 AM" in plain_phrase(winter)

    aware = compile_routine({**fields, "once": "2026-10-23T09:00:00+00:00", "timezone": "America/Chicago"})
    aware_at = datetime.fromisoformat(aware["at"]).astimezone(timezone.utc)
    assert aware_at == datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)

    try:
        compile_routine({**fields, "once": "2026-03-08 02:30", "timezone": "America/Chicago"})
    except ScheduleError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("the spring-forward gap was stored")


def test_missing_tzdata_warns_and_uses_the_local_clock(tmp_path, monkeypatch):
    """A missing zone database is a visible warning. The clock is the system zone, not UTC."""
    import easyagent.schedule as schedule

    assert tzdata_warning() == ""
    local = timezone(timedelta(hours=-6), "Custom Local")

    def missing(_name):
        raise ZoneInfoNotFoundError(str(_name))

    monkeypatch.setattr(schedule, "ZoneInfo", missing)
    monkeypatch.setattr(schedule, "_local_tzinfo", lambda: local)
    monkeypatch.setattr(schedule, "_posix_zone_name", lambda: "")
    monkeypatch.setattr(sys, "platform", "linux")
    assert schedule.local_zone_name() != "UTC"
    warning = schedule.tzdata_warning()
    assert "local clock" in warning
    assert "UTC" in warning
    drafted = schedule.compile_routine({
        "name": "Kiln",
        "prompt": "Check the kiln.",
        "once": "2026-10-23 09:00",
        "timezone": "America/Chicago",
        "created_at": "2026-10-01T00:00:00+00:00",
    })
    at = datetime.fromisoformat(drafted["at"]).astimezone(timezone.utc)
    assert at == datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    assert at != datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
    _app_obj, client, _bot, _chat = _app(tmp_path)
    host = client.get("/api/host").json()
    assert host["tzdata_warning"] == warning
    settings = (ROOT / "web" / "src" / "screens" / "Settings.tsx").read_text(encoding="utf-8")
    assert 'data-testid="tzdata-warning"' in settings
    client.close()


def test_routines_run_only_while_the_app_is_open_and_a_missed_run_is_marked(tmp_path, monkeypatch):
    """The card and the list say the app must be open. A late slot under 12 hours is a missed run."""

    class Recorder:
        async def __call__(self, *, base_url, api_key, model, messages, timeout=120, **_extra):
            return "The pier is quiet."

    monkeypatch.setattr("easyagent.llm.complete", Recorder())
    app, client, bot, chat = _app(tmp_path)
    store = app.state.store
    assert OPEN_NOTE in _system(store, bot["id"], chat["id"])
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "check daily"},
    )
    assert sent.status_code == 200, sent.text
    cards = client.get(f"/api/bots/{bot['id']}/approvals").json()
    assert OPEN_NOTE in cards[0]["detail"]
    approved = client.post(
        f"/api/bots/{bot['id']}/approvals/{cards[0]['id']}",
        json={"decision": "approve"},
    )
    assert approved.status_code == 200, approved.text
    host = client.get("/api/host").json()
    assert host["start_at_login"] is False
    turned = client.post("/api/host", json={"start_at_login": True})
    assert turned.status_code == 200, turned.text
    assert turned.json()["start_at_login"] is True
    assert client.get("/api/host").json()["start_at_login"] is True
    settings = (ROOT / "web" / "src" / "screens" / "Settings.tsx").read_text(encoding="utf-8")
    assert 'data-testid="routine-open-note"' in settings
    assert 'data-testid="start-at-login"' in settings
    assert OPEN_NOTE in settings
    pane = (ROOT / "web" / "src" / "components" / "ChatPane.tsx").read_text(encoding="utf-8")
    assert 'data-testid="missed-run"' in pane
    assert "Missed run" in pane
    saved = store.list_schedules(bot["id"])
    assert len(saved) == 1
    client.close()

    zone = ZoneInfo(saved[0]["timezone"])
    created = datetime.fromisoformat(saved[0]["created_at"]).astimezone(zone)
    day = (created + timedelta(days=1)).date()
    on_time = datetime(day.year, day.month, day.day, 8, 0, 30, tzinfo=zone)
    fired = asyncio.run(run_due_schedules(store, on_time))
    assert len(fired) == 1

    def routine_rows():
        rows = []
        for item in store.list_chats(bot["id"]):
            full = store.get_chat(bot["id"], item["id"])
            rows.extend(message for message in full.get("messages") or [] if message.get("routine_name"))
        return rows

    posted = routine_rows()
    assert len(posted) == 1
    assert posted[0].get("missed") is not True
    assert posted[0]["content"] == "The pier is quiet."
    stored = store.list_schedules(bot["id"])[0]
    store.save_schedules(bot["id"], [{**stored, "last_slot": None}])
    late = datetime(day.year, day.month, day.day, 10, 0, tzinfo=zone)
    again = asyncio.run(run_due_schedules(store, late))
    assert len(again) == 1
    missed = routine_rows()
    assert len(missed) == 2
    assert missed[-1]["missed"] is True
    assert missed[-1]["content"] == "The pier is quiet."
    listed = asyncio.run(execute(store, ToolRequest(kind="routine", action="list"), bot["id"]))
    assert OPEN_NOTE in listed
    assert "Daily check" in listed


def test_approving_a_custom_connector_installs_it_or_shows_the_error(tmp_path, monkeypatch):
    """Approving the card grants the server. A failure is returned, not discarded."""
    from easyagent.connectors import list_grants
    from easyagent.store import StoreError

    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")

    def refuse_refresh(*_args, **_kwargs):
        raise StoreError("The kiln server did not answer.", 400)

    monkeypatch.setattr("easyagent.connectors.refresh", refuse_refresh)
    _app_obj, client, bot, _chat = _app(tmp_path)
    bot_id = bot["id"]
    reviewed = client.post(
        f"/api/bots/{bot_id}/connectors",
        json={
            "name": "Kiln",
            "transport": "stdio",
            "command": ["npx", "-y", "kiln-tools@1.2.3"],
            "package": "kiln-tools",
            "version": "1.2.3",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    card = reviewed.json()["card"]
    approved = client.post(f"/api/bots/{bot_id}/approvals/{card['id']}", json={"decision": "approve"})
    assert approved.status_code == 200, approved.text
    assert "did not answer" in approved.json().get("warning", "")
    assert "not listed" in approved.json()["warning"]
    listed = client.get(f"/api/bots/{bot_id}/connectors").json()
    assert [row["name"] for row in listed] == ["Kiln"]
    assert any("kiln-tools@1.2.3" in " ".join(row.get("command") or []) for row in list_grants(_app_obj.state.store))

    def blocked(*_args, **_kwargs):
        raise StoreError("That server is not on the allow list. It was not saved.", 400)

    monkeypatch.setattr("easyagent.connectors.add_connector", blocked)
    again = client.post(
        f"/api/bots/{bot_id}/connectors",
        json={
            "name": "Harbor",
            "transport": "stdio",
            "command": ["npx", "-y", "harbor-tools@1.2.3"],
            "package": "harbor-tools",
            "version": "1.2.3",
        },
    )
    assert again.status_code == 200, again.text
    failed = client.post(
        f"/api/bots/{bot_id}/approvals/{again.json()['card']['id']}",
        json={"decision": "approve"},
    )
    assert failed.status_code == 400, failed.text
    assert failed.json()["detail"] == "That server is not on the allow list. It was not saved."
    names = [row["name"] for row in client.get(f"/api/bots/{bot_id}/connectors").json()]
    assert names == ["Kiln"]
    card_source = (ROOT / "web" / "src" / "components" / "ApprovalCard.tsx").read_text(encoding="utf-8")
    assert 'data-testid="install-error"' in card_source
    client.close()


def test_a_skill_the_fence_saved_can_be_read(tmp_path):
    """The prompt, the catalog, and the skill tool read the same saved skill."""
    from easyagent.app import _skills_for_prompt
    from easyagent.botskills import catalog, read_skill
    from easyagent.store import Store

    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="stub")
    store.save_skill({
        "name": "kiln-check",
        "description": "Look at the kiln",
        "body": "Check the east pier.",
    })
    listed = catalog(store, bot["id"])
    assert "kiln-check" in listed
    assert "kiln-check" in _skills_for_prompt(store, bot["id"], "")
    body = read_skill(store, bot["id"], "kiln-check")
    assert "east pier" in body
    assert "not saved" not in body.lower()
    read = asyncio.run(execute(store, ToolRequest(kind="skill", action="read", path="kiln-check"), bot["id"]))
    assert "east pier" in read
    names = asyncio.run(execute(store, ToolRequest(kind="skill", action="list"), bot["id"]))
    assert "kiln-check" in names


def test_direction_is_printed_once_and_bridges_are_this_bots(tmp_path, monkeypatch):
    """The Direction heading is not repeated, and another bot's bridge is not listed."""
    from easyagent.bridges import add_bridge

    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    app, client, ada, chat = _app(tmp_path)
    store = app.state.store
    kiln = client.post("/api/bots", json={"name": "Kiln", "endpoint_id": ada["endpoint_id"]}).json()
    add_bridge(store, kind="telegram", name="East pier", bot_id=ada["id"], allow_ids=["1001"], token="harbor-token-ada")
    add_bridge(store, kind="discord", name="Kiln wire", bot_id=kiln["id"], allow_ids=["1002"], token="pier-token-kiln")
    system = _system(store, ada["id"], chat["id"])
    assert system.count("# Direction") == 1
    assert "East pier" in system
    assert "Kiln wire" not in system
    kiln_chat = client.post(f"/api/bots/{kiln['id']}/chats").json()
    kiln_system = _system(store, kiln["id"], kiln_chat["id"])
    assert "Kiln wire" in kiln_system
    assert "East pier" not in kiln_system
    store.write_direction("Keep the kiln warm.")
    custom = _system(store, ada["id"], chat["id"])
    assert custom.count("# Direction") == 1
    assert "Keep the kiln warm." in custom
    client.close()
