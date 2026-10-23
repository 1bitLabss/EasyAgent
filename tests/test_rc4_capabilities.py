"""The capability list stays in the prompt, and a schedule ask proposes a routine."""

import asyncio
import inspect
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.features import CAPABILITY_RULE, capability_block, ensure_capabilities, steer_reply
from easyagent.llm import native_tools, only_tools, reset_only_tools
from easyagent.registry import disable

DENIAL = (
    "I don't have a built-in scheduler or cron daemon. "
    "Use schtasks or Windows Task Scheduler, or an ad-hoc loop. "
    "I can't trigger myself on a schedule."
)

SCHEDULES = (
    "how about cron jobs?",
    "Can we use a schedule?",
    "Set up a recurring check.",
    "every morning, look at the pier",
    "remind me to check the kiln",
    "check daily",
    "keep an eye on the harbor",
)

SELVES = (
    ("can you use a browser?", "I can't browse. There is no browser.", "browser"),
    ("can you use MCP?", "I don't have MCP connectors.", "mcp"),
    ("can you use a helper bot?", "I can't start a helper.", "helper"),
    ("do you have a chat bridge?", "I don't have Telegram or Discord.", "bridge"),
    ("can you save a skill?", "I can't save a skill.", "skill"),
    ("do you have memory?", "I have no memory file.", "memory"),
    ("can you search the web?", "I can't search the web.", "web"),
    ("do you have files?", "I don't have files.", "file"),
    ("can you use the shell?", "I can't run a shell.", "shell"),
    ("can you ask another bot?", "I can't ask another bot.", "another bot"),
)


def _system(store, bot_id, chat_id) -> str:
    from easyagent.app import _turn_messages

    _endpoint, messages = _turn_messages(store, bot_id, store.get_chat(bot_id, chat_id))
    return messages[0]["content"]


def test_the_capability_list_reaches_a_normal_chat_on_windows(tmp_path, monkeypatch):
    """Default and YOLO, on Windows. Routines and the capability list are at the top.

    The native routine tool is create, edit, delete, and list. Context fitting, a
    disabled tool, and a one-tool child list do not remove the capability list.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post(
        "/api/bots",
        json={"name": "Ada", "endpoint_id": endpoint["id"], "context_tokens": 512},
    ).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    store = app.state.store
    default = _system(store, bot["id"], chat["id"])
    from easyagent.yolo import apply_command

    turned = apply_command(store, bot["id"], chat["id"], "/yolo on", source="user-message")
    assert turned.ok
    yolo = _system(store, bot["id"], chat["id"])
    block = capability_block()
    for system in (default, yolo):
        assert "PowerShell" in system
        assert "# Routines" in system
        assert block in system
        assert system.index("# What you can do") < system.index("# Routines") < system.index("# Direction")
        assert "routines (cron, reminders, recurring tasks, and every morning)" in system
        assert "every morning" in system.lower()
    tool_names = [tool["function"]["name"] for tool in native_tools()]
    assert "routine" in tool_names
    assert "cron" not in tool_names
    terminal = next(tool for tool in native_tools() if tool["function"]["name"] == "terminal")
    assert "PowerShell" in terminal["function"]["description"]
    disable("browser")
    disable("terminal")
    token = only_tools(["list_dir"])
    try:
        assert [tool["function"]["name"] for tool in native_tools()] == ["list_dir"]
        filtered = _system(store, bot["id"], chat["id"])
    finally:
        reset_only_tools(token)
    assert block in filtered
    assert "# Routines" in filtered
    from easyagent.turnctx import fit_context

    _summary, rows, earlier, _used = fit_context("summary", [{"role": "user", "content": "hi"}], block, 30, latest="hi")
    assert block not in earlier
    assert rows[-1]["content"] == "hi"
    chopped = "You are Ada, running inside EasyAgent.\n\n# This computer\nonly the tail"
    restored = ensure_capabilities(chopped)
    assert CAPABILITY_RULE in restored
    assert restored.index(CAPABILITY_RULE) < restored.index("# This computer")
    prompt = (Path(__file__).resolve().parents[1] / "easyagent" / "prompt.py").read_text(encoding="utf-8")
    assert "recurring or scheduled jobs" not in prompt
    assert CAPABILITY_RULE not in prompt
    client.close()


def test_a_schedule_ask_proposes_a_routine_card(tmp_path, monkeypatch):
    class Recorder:
        def __init__(self):
            self.reply = DENIAL

        async def __call__(self, *, base_url, api_key, model, messages, timeout=120, **_extra):
            return self.reply

    monkeypatch.setattr("easyagent.llm.complete", Recorder())
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    for phrase in SCHEDULES:
        chat = client.post(f"/api/bots/{bot['id']}/chats").json()
        sent = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": phrase},
        )
        assert sent.status_code == 200, sent.text
        answer = sent.json()["chat"]["messages"][-1]
        text = answer["content"]
        assert "routine" in text.lower(), phrase
        assert "schtasks" not in text.lower()
        assert "task scheduler" not in text.lower()
        assert "cron daemon" not in text.lower()
        assert answer["proposal"]["kind"] == "routine"
        cards = client.get(f"/api/bots/{bot['id']}/approvals").json()
        assert cards and cards[0]["rule"] == "routine"
        assert cards[0]["proposal"]["kind"] == "routine"
        assert "Save this routine?" in cards[0]["why"]
        denied = client.post(
            f"/api/bots/{bot['id']}/approvals/{cards[0]['id']}",
            json={"decision": "deny"},
        )
        assert denied.status_code == 200, denied.text
        assert client.get(f"/api/bots/{bot['id']}/approvals").json() == []
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "how about cron jobs?"},
    )
    assert "routine" in sent.json()["reply"].lower()
    cards = client.get(f"/api/bots/{bot['id']}/approvals").json()
    approved = client.post(
        f"/api/bots/{bot['id']}/approvals/{cards[0]['id']}",
        json={"decision": "approve"},
    )
    assert approved.status_code == 200, approved.text
    saved = client.get(f"/api/bots/{bot['id']}/schedules").json()
    assert saved and saved[0]["name"]
    assert client.get(f"/api/bots/{bot['id']}/approvals").json() == []
    client.close()


def test_the_bot_does_not_deny_a_feature_it_has(tmp_path, monkeypatch):
    class Recorder:
        def __init__(self):
            self.reply = "ack"

        async def __call__(self, *, base_url, api_key, model, messages, timeout=120, **_extra):
            return self.reply

    recorder = Recorder()
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    for question, denial, needle in SELVES:
        recorder.reply = denial
        chat = client.post(f"/api/bots/{bot['id']}/chats").json()
        sent = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": question},
        )
        assert sent.status_code == 200, sent.text
        text = sent.json()["chat"]["messages"][-1]["content"].lower()
        assert needle in text, question
        assert "don't have" not in text
        assert "do not have" not in text
        assert "can't" not in text
        assert "cannot" not in text
        assert "i have no" not in text
        assert "there is no" not in text
    kept = steer_reply(None, None, "what is the headline?", "I don't have a live headline for that.")
    assert kept == "I don't have a live headline for that."
    blocked = steer_reply(None, None, "can you use the shell?", "That command is blocked.")
    assert blocked == "That command is blocked."
    client.close()


def test_windows_without_tzdata_confirms_runs_and_catches_up(tmp_path, monkeypatch):
    """A Windows desktop whose zone database is missing.

    The registry module is absent, ZoneInfo cannot load, and the process clock is a
    fixed Central offset. The Routines section stays in the prompt. A daily check
    shows a confirm card, saves, and does not run while the app is closed. The next
    start posts that one missed slot and does not post it again.
    """
    import easyagent.schedule as schedule
    from easyagent.tools import _routine_from_body

    central = timezone(timedelta(hours=-5), "Central Standard Time")

    def missing(_name):
        raise ZoneInfoNotFoundError(str(_name))

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(schedule, "ZoneInfo", missing)
    monkeypatch.setattr(schedule, "_local_tzinfo", lambda: central)
    monkeypatch.setattr(schedule, "_localtime_zone", lambda: "")
    # The registry is closed, and the host's tzname is not a zone. A UTC
    # Windows runner must not leak its own zone into this case.
    closed = types.ModuleType("winreg")
    closed.HKEY_LOCAL_MACHINE = None

    def _registry_closed(*_args, **_kwargs):
        raise OSError("registry closed")

    closed.OpenKey = _registry_closed
    monkeypatch.setitem(sys.modules, "winreg", closed)
    monkeypatch.setattr(time, "tzname", ("", ""), raising=False)
    monkeypatch.setenv("EASYAGENT_SCHEDULE_TICK", "3600")
    assert schedule.windows_zone_name() == ""
    assert schedule.local_zone_name() == "America/Chicago"
    assert "sys.platform" not in inspect.getsource(schedule.schedule_loop)
    assert "sys.platform" not in inspect.getsource(schedule.run_due_schedules)
    assert "start_at_login" not in inspect.getsource(schedule.schedule_loop)
    assert "win32" not in inspect.getsource(_routine_from_body)
    drafted = schedule.compile_routine({
        "name": "Clock",
        "prompt": "Say the time.",
        "daily": "8:00 AM",
        "timezone": "Central Standard Time",
        "created_at": "2026-10-01T00:00:00+00:00",
    })
    assert drafted["timezone"] == "America/Chicago"
    assert drafted["next_run"]
    try:
        schedule.compile_routine({
            "name": "Clock",
            "prompt": "Say the time.",
            "daily": "8:00 AM",
            "timezone": "Not A Real Zone",
            "created_at": "2026-10-01T00:00:00+00:00",
        })
    except schedule.ScheduleError as exc:
        assert "not recognized" in str(exc)
    else:
        raise AssertionError("an unknown zone was accepted")

    class Recorder:
        async def __call__(self, *, base_url, api_key, model, messages, timeout=120, **_extra):
            return "The pier is quiet."

    monkeypatch.setattr("easyagent.llm.complete", Recorder())
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    store = app.state.store
    assert "# Routines" in _system(store, bot["id"], chat["id"])
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "check daily"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]
    assert "routine" in answer["content"].lower()
    assert answer["proposal"]["kind"] == "routine"
    cards = client.get(f"/api/bots/{bot['id']}/approvals").json()
    assert cards and cards[0]["rule"] == "routine"
    assert "Save this routine?" in cards[0]["why"]
    approved = client.post(
        f"/api/bots/{bot['id']}/approvals/{cards[0]['id']}",
        json={"decision": "approve"},
    )
    assert approved.status_code == 200, approved.text
    saved = store.list_schedules(bot["id"])
    assert len(saved) == 1
    assert saved[0]["name"] == "Daily check"
    assert saved[0]["timezone"] == "America/Chicago"
    assert saved[0]["cron"] == "0 8 * * *"
    assert not saved[0].get("last_slot")
    client.close()

    def routine_rows():
        rows = []
        for item in store.list_chats(bot["id"]):
            full = store.get_chat(bot["id"], item["id"])
            rows.extend(message for message in full.get("messages") or [] if message.get("routine_name"))
        return rows

    assert routine_rows() == []
    created = datetime.fromisoformat(saved[0]["created_at"])
    day = (created.astimezone(central) + timedelta(days=1)).date()
    missed = datetime(day.year, day.month, day.day, 10, 0, tzinfo=central)
    fired = asyncio.run(schedule.run_due_schedules(store, missed))
    assert len(fired) == 1
    assert fired[0]["status"] == "ok"
    posted = routine_rows()
    assert len(posted) == 1
    assert posted[0]["routine_name"] == "Daily check"
    assert posted[0]["content"] == "The pier is quiet."
    assert posted[0]["missed"] is True
    stored = store.list_schedules(bot["id"])[0]
    assert stored["last_slot"] == f"cron:{day.isoformat()}T08:00"
    assert asyncio.run(schedule.run_due_schedules(store, missed)) == []
    assert routine_rows() == posted
