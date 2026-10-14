"""A repeated Looking line is one note, then the run stops."""

from __future__ import annotations

import asyncio

from easyagent.app import _drain_queued_json, _write_live
from easyagent.plan import FOLLOW_CAP, enqueue
from easyagent.tools import LOOP_TEXT, _near_line, status_only_narration
from easyagent import turn as turn_mod
from tests.test_plan_0320 import _client, _running
from tests.test_tools import _world


def test_near_lines_and_status_words():
    assert _near_line("Looking", "Looking.")
    assert _near_line("looking", "Looking")
    assert not _near_line("Looking", "The file says hello.")
    assert status_only_narration("Looking")
    assert status_only_narration("Looking.")
    assert not status_only_narration("Looking at the notes, the file is empty.")


def test_activity_updates_one_row(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    steps = ["Waiting on model", "Looking", "Looking", "Looking", "Reading notes.txt"]
    for step in steps:
        _write_live(store, bot["id"], chat["id"], step=step, force=True)
    first = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    again = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    for public in (first, again):
        notes = [item for item in public["messages"] if item.get("about") == "activity"]
        bubbles = [
            item for item in public["messages"]
            if item.get("role") == "assistant" and item.get("about") != "activity" and item.get("kind") != "status"
        ]
        assert len(notes) == 1
        assert notes[0]["id"] == "activity"
        assert notes[0]["content"] == "Reading notes.txt"
        assert notes[0]["kind"] == "status"
        assert [item.get("content") for item in bubbles] == []
    client.close()


def test_looking_three_times_stops_and_offers_resume(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = "Looking"
    before = len(recorder.seen)
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Look at the notes."},
    )
    assert sent.status_code == 200, sent.text
    stored = sent.json()["chat"]
    messages = stored["messages"]
    looking = [
        item for item in messages
        if str(item.get("content") or "").strip() in {"Looking", "Looking."}
        and item.get("kind") != "status"
    ]
    assert len(recorder.seen) == before + 3
    assert looking == []
    assert messages[-1]["content"] == LOOP_TEXT
    assert messages[-1]["kind"] == "resume-offer"
    assert stored["run"]["status"] == "paused"
    assert sum(1 for item in messages if item.get("content") == LOOP_TEXT) == 1


def test_the_same_tool_stops_when_the_repeat_limit_is_hit(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-loop-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    (folder / "notes.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setattr("easyagent.tools.REPEAT_LIMIT", 2)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = f"```files\nlist\n{folder}\n```"
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Please list the folder and then tell me if it is empty."},
    )
    assert sent.status_code == 200, sent.text
    stored = sent.json()["chat"]
    messages = stored["messages"]
    looking = [
        item for item in messages
        if item.get("role") == "assistant" and str(item.get("content") or "").strip() == "Looking"
    ]
    assert len(recorder.seen) == before + 2
    assert looking == []
    assert messages[-1]["content"] == LOOP_TEXT
    assert messages[-1]["kind"] == "resume-offer"
    assert stored["run"]["status"] == "paused"
    assert "notes.txt" not in messages[-1]["content"]


def test_steered_followups_stop_at_the_cap(tmp_path, monkeypatch):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    turn_mod.bind(store, chat["id"], bot_id=bot["id"])
    calls = {"n": 0}

    async def complete(**kwargs):
        calls["n"] += 1
        enqueue(store, bot["id"], chat["id"], f"again {calls['n']}")
        return "Still working on it."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    enqueue(store, bot["id"], chat["id"], "first steer")
    asyncio.run(_drain_queued_json(store, bot["id"], chat["id"]))
    assert calls["n"] == FOLLOW_CAP
    assert calls["n"] < 10
    client.close()
