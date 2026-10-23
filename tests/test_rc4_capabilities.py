"""The capability list stays in the prompt, and a schedule ask proposes a routine."""

import sys
from pathlib import Path

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
    """Default and YOLO, on Windows. The late Routines section is present, and so is the list at the top.

    The native tool array has no scheduler. Context fitting, a disabled tool, and a
    one-tool child list do not remove the capability list from the system prompt.
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
        assert system.index("# What you can do") < system.index("# Direction") < system.index("# Routines")
        assert "routines (recurring or scheduled jobs, run on a schedule, cron-like)" in system
    tool_names = [tool["function"]["name"] for tool in native_tools()]
    assert "routine" not in tool_names
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
