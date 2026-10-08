"""A candidate stays off the live skills until a check and a replay agree."""

import asyncio
import shlex
import sys

from easyagent.learn import (
    BODY_CAP,
    approve_candidate,
    chats_active,
    clear_stop,
    is_user_skill,
    lesson_block,
    lint_candidate,
    list_candidates,
    merge_inbox,
    merge_text,
    note_check_passed,
    note_failed_check,
    note_outcome,
    observe,
    propose_json,
    request_stop,
    rollback_latest,
    save_candidate,
    set_manual,
    set_paused,
    sleep_once,
)
from easyagent.store import Store


OK_COMMAND = shlex.quote(sys.executable) + ' -c "raise SystemExit(0)"'
BAD_COMMAND = shlex.quote(sys.executable) + ' -c "raise SystemExit(2)"'


def _world(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="home", base_url="http://127.0.0.1:9/v1", api_key=None, model="your-model")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="bot-model")
    return store, endpoint, bot


def _proposal(**extra):
    data = {
        "name": "marker-note",
        "trigger": "when asked for the marker",
        "steps": ["Say the token."],
        "pitfalls": ["Do not invent a different token."],
        "scope": "this bot",
        "check": {"kind": "command", "command": OK_COMMAND, "exit_code": 0},
        "replay_without": "learn-plain",
        "replay_with": "learn-with-skill",
    }
    data.update(extra)
    return data


def _live(store):
    return sorted(path.stem for path in store.skills_dir.glob("*.md"))


def test_a_bad_proposal_never_becomes_a_live_skill(tmp_path):
    store, _endpoint, bot = _world(tmp_path)
    before = _live(store)
    bad = _proposal(steps=["Use the `ftp` tool to fetch it."])
    assert any("ftp" in item for item in lint_candidate(bad))
    assert save_candidate(store, bot["id"], bad) is None
    assert _live(store) == before
    assert list_candidates(store, bot["id"]) == []
    broken = _proposal(check={"kind": "command", "command": "echo 'unterminated", "exit_code": 0})
    assert any("parse" in item for item in lint_candidate(broken))
    assert save_candidate(store, bot["id"], broken) is None
    assert _live(store) == before


def test_the_size_cap_refuses_another_line():
    body = "x" * (BODY_CAP - 4)
    assert merge_text(body, "yy") is None
    assert merge_text("Keep this.", "one more") == "Keep this.\n- one more"


def test_rollback_restores_a_skill_and_a_memory_line(tmp_path):
    store, _endpoint, bot = _world(tmp_path)
    store.save_skill({"name": "desk-notes", "description": "A note", "body": "First body."})
    store.save_skill({"name": "desk-notes", "description": "A note", "body": "Second body."})
    restored = rollback_latest(store, bot["id"])
    assert restored["key"] == "desk-notes"
    text = (store.skills_dir / "desk-notes.md").read_text(encoding="utf-8")
    assert "First body." in text
    assert "Second body." not in text
    remembered = store.add_memory(bot["id"], "The garage code is 1234.", topic="garage", create=True)
    store.update_memory(bot["id"], remembered["id"], "The garage code is 9999.")
    rollback_latest(store, bot["id"])
    lines = [item["text"] for item in store.list_memory(bot["id"])]
    assert "The garage code is 1234." in lines
    assert "The garage code is 9999." not in lines


def test_a_failed_check_is_rejected_and_a_replay_can_promote(tmp_path):
    store, _endpoint, bot = _world(tmp_path)
    before = set(_live(store))
    bad = save_candidate(
        store,
        bot["id"],
        _proposal(name="bad-note", check={"kind": "command", "command": BAD_COMMAND, "exit_code": 0}),
        source={"model": "bot-model", "connection": "home"},
    )
    assert bad is not None
    result = asyncio.run(sleep_once(store, mock=True, runs=1, bot_id=bot["id"]))
    assert result["status"] == "done"
    assert result["rejected"][0]["name"] == "bad-note"
    assert "bad-note" not in _live(store)
    assert set(_live(store)) == before
    good = save_candidate(
        store,
        bot["id"],
        _proposal(),
        source={"model": "bot-model", "connection": "home"},
    )
    promoted = asyncio.run(sleep_once(store, mock=True, runs=3, bot_id=bot["id"]))
    assert promoted["promoted"] == ["marker-note"]
    assert "marker-note" in _live(store)
    saved = next(item for item in list_candidates(store, bot["id"]) if item["name"] == "marker-note")
    assert saved["status"] == "promoted"
    assert saved["with_rate"] > saved["without_rate"]
    assert "bad-note" not in _live(store)
    meta_text = (store.skills_dir / "_meta" / "marker-note.json").read_text(encoding="utf-8")
    assert "bot-model" in meta_text
    assert is_user_skill(store, "marker-note") is False


def test_a_skill_you_wrote_is_not_replaced(tmp_path):
    store, _endpoint, bot = _world(tmp_path)
    store.save_skill({"name": "marker-note", "description": "Mine", "body": "Leave this."})
    assert is_user_skill(store, "marker-note") is True
    save_candidate(store, bot["id"], _proposal(), source={"model": "bot-model", "connection": "home"})
    result = asyncio.run(sleep_once(store, mock=True, runs=1, bot_id=bot["id"]))
    assert result["promoted"] == []
    assert "Leave this." in (store.skills_dir / "marker-note.md").read_text(encoding="utf-8")


def test_replay_waits_while_a_chat_is_running_and_stop_ends_it(tmp_path):
    store, _endpoint, bot = _world(tmp_path)
    save_candidate(store, bot["id"], _proposal())
    chat = store.create_chat(bot["id"])
    chat["run"] = {"status": "running", "current_step": "Waiting on model"}
    store.save_chat(chat)
    assert chats_active(store) is True
    waiting = asyncio.run(sleep_once(store, mock=True, runs=1, bot_id=bot["id"]))
    assert waiting["status"] == "idle"
    assert waiting["promoted"] == []
    assert "marker-note" not in _live(store)
    chat["run"] = {"status": "idle"}
    store.save_chat(chat)
    request_stop()
    try:
        stopped = asyncio.run(sleep_once(store, mock=True, runs=1, bot_id=bot["id"]))
    finally:
        clear_stop()
    assert stopped["status"] == "stopped"
    assert "marker-note" not in _live(store)


def test_manual_approval_holds_a_passing_candidate(tmp_path):
    store, _endpoint, bot = _world(tmp_path)
    set_manual(store, bot["id"], True)
    bot = store.get_bot(bot["id"])
    save_candidate(store, bot["id"], _proposal())
    held = asyncio.run(sleep_once(store, mock=True, runs=1, bot_id=bot["id"]))
    assert held["promoted"] == []
    assert "marker-note" not in _live(store)
    ready = next(item for item in list_candidates(store, bot["id"]) if item["status"] == "ready")
    approve_candidate(store, bot, ready["id"])
    assert "marker-note" in _live(store)
    set_paused(store, bot["id"], True)
    assert store.get_bot(bot["id"]).get("learn_paused") is True


def test_a_lesson_expires_or_becomes_a_skill_note(tmp_path):
    store, _endpoint, bot = _world(tmp_path)
    learned = note_failed_check(store, bot["id"], "Quote the marker line", "The marker was missing.")
    assert learned.startswith("Learned:")
    assert "marker was missing" in lesson_block(store, bot["id"], "Quote the marker line")
    assert note_failed_check(store, bot["id"], "Quote the marker line", "Still missing.") == ""
    assert lesson_block(store, bot["id"], "Quote the marker line") == ""
    note_failed_check(store, bot["id"], "Quote the marker line", "The marker was missing.")
    note_check_passed(store, bot["id"], "Quote the marker again")
    assert "marker was missing" in (store.skills_dir / "verified-notes.md").read_text(encoding="utf-8")
    assert is_user_skill(store, "verified-notes") is False


def test_a_room_lesson_stays_quarantined(tmp_path):
    store, _endpoint, bot = _world(tmp_path)
    assert note_failed_check(store, bot["id"], "Quote the marker line", "Heard in a room.", quarantine=True) == ""
    assert lesson_block(store, bot["id"], "Quote the marker line") == ""


def test_failing_learned_skills_are_archived_and_inbox_respects_the_cap(tmp_path):
    store, _endpoint, bot = _world(tmp_path)
    store.save_skill({"name": "shaky-note", "description": "A try", "body": "First."})
    from easyagent.learn import mark_origin

    mark_origin(store, "shaky-note", "learned", model="bot-model", connection="home", bot_id=bot["id"])
    note_outcome(store, "shaky-note", False, "checker")
    note_outcome(store, "shaky-note", False, "tool")
    note_outcome(store, "shaky-note", False, "thumb")
    assert "shaky-note" not in _live(store)
    assert (store.skills_dir / "_archive" / "shaky-note.md").is_file()
    store.save_skill({"name": "verified-notes", "description": "Notes", "body": "y" * (BODY_CAP - 3)})
    mark_origin(store, "verified-notes", "learned", bot_id=bot["id"])
    observe(store, bot["id"], "This line is too long to add now.", reason="correction", task="Quote the marker")
    merged = merge_inbox(store, bot["id"], "verified-notes")
    assert merged["merged"] == 0
    assert "too long" not in (store.skills_dir / "verified-notes.md").read_text(encoding="utf-8")
    before = _live(store)
    observe(store, bot["id"], "A separate observation.", reason="thumb")
    assert _live(store) == before


def test_llama_cpp_gets_a_schema_and_a_grammar(monkeypatch):
    seen = {}

    async def fake_complete(**kwargs):
        seen.update(kwargs)
        return '{"name":"marker-note","trigger":"when asked","steps":["Say it."],"pitfalls":[],"scope":"this bot","check":{"kind":"command","command":"true","exit_code":0}}'

    monkeypatch.setattr("easyagent.llm.complete", fake_complete)
    data = asyncio.run(
        propose_json(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="llama-3",
            notes="The marker was missing.",
        )
    )
    assert data["name"] == "marker-note"
    assert seen["response_schema"]["required"] == ["name", "trigger", "steps", "pitfalls", "scope", "check"]
    assert "root ::=" in seen["grammar"]


def test_the_panel_pauses_and_a_running_chat_blocks_the_idle_pass(tmp_path):
    import json
    from pathlib import Path

    from fastapi.testclient import TestClient

    from easyagent.app import create_app

    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "home", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"], "model": "bot-model"}).json()
    assert bot["learn_paused"] is False
    assert bot["learn_manual"] is False
    panel = client.get(f"/api/bots/{bot['id']}/learning")
    assert panel.status_code == 200
    body = panel.json()
    assert body["waiting"] == []
    assert body["paused"] is False
    paused = client.post(f"/api/bots/{bot['id']}/learning/pause", json={"on": True})
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    stored = json.loads((tmp_path / "bots" / bot["id"] / "bot.json").read_text(encoding="utf-8"))
    assert stored["learn_paused"] is True
    cleared = client.post(f"/api/bots/{bot['id']}/learning/pause", json={"on": False})
    assert cleared.json()["paused"] is False
    stored = json.loads((tmp_path / "bots" / bot["id"] / "bot.json").read_text(encoding="utf-8"))
    assert "learn_paused" not in stored
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    path = tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["run"] = {"status": "running"}
    path.write_text(json.dumps(record), encoding="utf-8")
    blocked = client.post(f"/api/bots/{bot['id']}/learning/sleep")
    assert blocked.status_code == 409
    record["run"] = {"status": "idle"}
    path.write_text(json.dumps(record), encoding="utf-8")
    stopped = client.post(f"/api/bots/{bot['id']}/learning/stop")
    assert stopped.json()["stopped"] is True
    ended = client.post(f"/api/bots/{bot['id']}/learning/sleep")
    assert ended.status_code == 200
    assert ended.json()["status"] == "stopped"
    page = client.get("/classic")
    assert "Pause learning while this bot is idle" in page.text
    assert "judge connection" not in page.text.lower()
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "## How EasyAgent learns" in readme
    assert "EasyAgent uses your connected model to review and learn — no extra model needed." in readme
    assert "Hermes" not in readme
    assert "Nous" not in readme
