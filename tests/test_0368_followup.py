"""0.3.68: card-free scoped follow-ups (audit wave 3).

A bot in a chat can schedule its own next step into that same chat without a
confirm card, because the scope is bounded: same chat, at most 3 pending, 1
minute to 24 hours out, and a fired follow-up may schedule only one more.
Records live in a per-chat .followups.json, so a restart keeps them and one
whose time passed while the app was closed fires on the next sweep.

The bug-sensitive core: before this feature `action: followup` was refused
("A routine action is create, edit, delete, or list."), there was no
.followups.json, nothing fired into the chat transcript, and a routine run
got a foreign context with no way to schedule its own next slice.
"""

import asyncio
import json
import time

import pytest

from easyagent import followup, plan, tools
from easyagent import turn as turn_mod
from easyagent.app import _public_followups
from easyagent.followup import (
    EXPIRE_PREFIX,
    FIRE_PREFIX,
    FollowupError,
    PENDING_CAP,
    followups_path,
    schedule_followup,
    sweep_followups,
)
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest, parse_tools, execute


def _store_world(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(
        name="local", base_url="http://127.0.0.1:9/v1", api_key=None, max_parallel=3
    )
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="m")
    chat = store.create_chat(bot["id"])
    return store, bot["id"], chat["id"]


def _noop_kick(monkeypatch):
    async def kick(_store):
        return 0

    monkeypatch.setattr("easyagent.app.kick", kick)


def _followup_request(text: str, in_text: str) -> ToolRequest:
    spec = {"action": "followup", "prompt": text, "in": in_text}
    return ToolRequest(
        kind="routine", action="followup", path="", body=text, call_arguments=json.dumps(spec)
    )


def _queued_lines(store, bot_id, chat_id):
    return [
        str(item.get("content") or "")
        for item in plan._read_queue(store, bot_id, chat_id)["items"]
    ]


def test_schedule_writes_a_disk_record_and_a_visible_line(tmp_path, monkeypatch):
    """The tool returns the visible promise line and the record is on disk."""
    _noop_kick(monkeypatch)
    store, bot_id, chat_id = _store_world(tmp_path)
    tools._turn_notes.set([])

    async def scene():
        turn_mod.bind(store, chat_id, bot_id)
        return await execute(store, _followup_request("check the build", "10"), bot_id)

    line = asyncio.run(scene())
    assert line == "I will check the build in 10 minutes."
    notes = tools._turn_notes.get() or []
    assert ("followup", "I will check the build in 10 minutes.") in notes
    path = followups_path(store, bot_id, chat_id)
    assert path.is_file()
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["text"] == "check the build"
    assert rows[0]["minutes"] == 10
    assert rows[0]["due_at"] == pytest.approx(time.time() + 600, abs=5)


def test_fence_grammar_parses_a_followup():
    """```routine action: followup / in: 10 / --- / text parses to the request."""
    text = "```routine\naction: followup\nin: 10\n---\ncheck the build\n```"
    request = parse_tools(text)
    assert len(request) == 1
    assert request[0].kind == "routine"
    assert request[0].action == "followup"
    spec = json.loads(request[0].call_arguments)
    assert spec["in"] == "10"
    assert spec["prompt"] == "check the build"


def test_fire_lands_in_the_chat_as_a_user_shaped_turn(tmp_path, monkeypatch):
    """At its time the follow-up becomes a user message in the same chat."""
    _noop_kick(monkeypatch)
    store, bot_id, chat_id = _store_world(tmp_path)
    record = schedule_followup(store, bot_id, chat_id, "check the build", 10)
    fired = sweep_followups(store, now=record["due_at"] + 1)
    assert fired == 1
    assert _queued_lines(store, bot_id, chat_id) == ["Follow-up due: check the build"]
    chat = store.get_chat(bot_id, chat_id)
    users = [m for m in chat["messages"] if m.get("role") == "user"]
    assert users and users[-1]["content"] == "Follow-up due: check the build"
    assert not followups_path(store, bot_id, chat_id).exists()


def test_cancel_by_short_id(tmp_path):
    store, bot_id, chat_id = _store_world(tmp_path)
    record = schedule_followup(store, bot_id, chat_id, "check the build", 10)
    short = record["id"]
    assert len(short) == 6
    assert followup.cancel_followup(store, bot_id, chat_id, short) == (
        "Cancelled the follow-up: check the build."
    )
    assert followup.pending(store, bot_id, chat_id) == []
    with pytest.raises(FollowupError, match="No follow-up with that id"):
        followup.cancel_followup(store, bot_id, chat_id, short)


def test_cap_three_pending_the_fourth_is_refused_plainly(tmp_path, monkeypatch):
    _noop_kick(monkeypatch)
    store, bot_id, chat_id = _store_world(tmp_path)
    tools._turn_notes.set([])

    async def scene():
        turn_mod.bind(store, chat_id, bot_id)
        for i in range(PENDING_CAP):
            await execute(store, _followup_request(f"step {i}", "5"), bot_id)
        with pytest.raises(ToolError) as exc:
            await execute(store, _followup_request("one too many", "5"), bot_id)
        return str(exc.value)

    message = asyncio.run(scene())
    assert message == "Only 3 follow-ups can wait in this chat. Cancel one first."
    assert len(followup.pending(store, bot_id, chat_id)) == 3


def test_min_delay_and_horizon_are_validated(tmp_path):
    store, bot_id, chat_id = _store_world(tmp_path)
    with pytest.raises(FollowupError, match="at least 1 minute"):
        schedule_followup(store, bot_id, chat_id, "soon", 0)
    with pytest.raises(FollowupError, match="at most 24 hours"):
        schedule_followup(store, bot_id, chat_id, "far", 24 * 60 + 1)
    assert followup.pending(store, bot_id, chat_id) == []


def test_restart_catchup_a_followup_due_while_down_fires_on_start(tmp_path, monkeypatch):
    """The record survived on disk; the startup sweep fires it, no backlog replay."""
    _noop_kick(monkeypatch)
    store, bot_id, chat_id = _store_world(tmp_path)
    record = schedule_followup(store, bot_id, chat_id, "check the build", 5)
    # The app closes; five more minutes pass. The file is the only survivor.
    later = record["due_at"] + 300
    assert followups_path(store, bot_id, chat_id).is_file()
    assert sweep_followups(store, now=later) == 1
    assert _queued_lines(store, bot_id, chat_id) == ["Follow-up due: check the build"]
    # It does not fire twice.
    assert sweep_followups(store, now=later + 60) == 0


def test_stale_beyond_the_catchup_window_is_dropped_not_fired(tmp_path, monkeypatch):
    _noop_kick(monkeypatch)
    store, bot_id, chat_id = _store_world(tmp_path)
    record = schedule_followup(store, bot_id, chat_id, "old news", 5)
    sweep_followups(store, now=record["due_at"] + 25 * 3600)
    assert _queued_lines(store, bot_id, chat_id) == [EXPIRE_PREFIX + "old news"]


def test_a_fired_followup_may_schedule_only_one_more(tmp_path, monkeypatch):
    """No runaway chains: the turn a fired follow-up woke can queue one more, not three."""
    _noop_kick(monkeypatch)
    store, bot_id, chat_id = _store_world(tmp_path)
    tools._turn_notes.set([])
    record = schedule_followup(store, bot_id, chat_id, "check the build", 1)
    sweep_followups(store, now=record["due_at"] + 1)

    async def scene():
        turn_mod.bind(store, chat_id, bot_id)
        first = await execute(store, _followup_request("check again", "5"), bot_id)
        assert first == "I will check again in 5 minutes."
        with pytest.raises(ToolError) as exc:
            await execute(store, _followup_request("and again", "5"), bot_id)
        return str(exc.value)

    message = asyncio.run(scene())
    assert message == "A fired follow-up may schedule only one more. It was not scheduled."
    assert [row["text"] for row in followup.pending(store, bot_id, chat_id)] == ["check again"]


def test_a_normal_turn_is_not_limited_by_the_fire_rule(tmp_path, monkeypatch):
    """Once a human line lands after the fired line, the one-more rule is off."""
    _noop_kick(monkeypatch)
    store, bot_id, chat_id = _store_world(tmp_path)
    tools._turn_notes.set([])
    record = schedule_followup(store, bot_id, chat_id, "check the build", 1)
    sweep_followups(store, now=record["due_at"] + 1)
    chat = store.get_chat(bot_id, chat_id)
    chat["messages"].append(
        {"id": "human-1", "role": "user", "content": "thanks", "created_at": "now"}
    )
    store.save_chat(chat)

    async def scene():
        turn_mod.bind(store, chat_id, bot_id)
        return await execute(store, _followup_request("check again", "5"), bot_id)

    assert asyncio.run(scene()) == "I will check again in 5 minutes."


def test_schedule_loop_sweeps_followups(tmp_path, monkeypatch):
    """The scheduler tick hosts the per-chat follow-up sweep."""
    _noop_kick(monkeypatch)
    store, bot_id, chat_id = _store_world(tmp_path)
    calls = []
    real = followup.sweep_followups

    def counting(_store, now=None):
        calls.append(1)
        return real(_store, now)

    monkeypatch.setattr(followup, "sweep_followups", counting)
    monkeypatch.setenv("EASYAGENT_SCHEDULE_TICK", "1")

    async def scene():
        from easyagent.schedule import schedule_loop

        stop = asyncio.Event()
        task = asyncio.create_task(schedule_loop(store, stop))
        await asyncio.sleep(1.5)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scene())
    assert calls


def test_public_chat_payload_lists_pending_followups(tmp_path):
    store, bot_id, chat_id = _store_world(tmp_path)
    record = schedule_followup(store, bot_id, chat_id, "check the build", 10)
    chat = store.get_chat(bot_id, chat_id)
    rows = _public_followups(store, chat)
    assert len(rows) == 1
    assert rows[0]["id"] == record["id"]
    assert rows[0]["text"] == "check the build"
    assert rows[0]["due_at"]
