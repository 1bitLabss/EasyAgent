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


def test_a_proposal_does_not_send_tools_with_the_schema(monkeypatch):
    seen = {}

    async def fake_complete(**kwargs):
        seen.update(kwargs)
        return '{"name":"marker-note","trigger":"when asked","steps":["Say it."],"pitfalls":[],"scope":"this bot","check":{"kind":"command","command":"true","exit_code":0}}'

    monkeypatch.setattr("easyagent.llm.complete", fake_complete)
    data = asyncio.run(
        propose_json(
            base_url="http://localhost:8080/v1",
            api_key=None,
            model="your-model",
            notes="The marker was missing.",
        )
    )
    assert data["name"] == "marker-note"
    assert seen["tools"] is False
    assert seen["response_schema"]["required"] == ["name", "trigger", "steps", "pitfalls", "scope", "check"]
    assert "grammar" not in seen or not seen.get("grammar")


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


_REJECTED = (
    '{"name":"missed-reply","trigger":"when a reply is marked down","steps":["Say what was wrong."],'
    '"pitfalls":[],"scope":"this bot","check":{"kind":"command","command":"false","exit_code":0}}'
)


def test_a_thumbs_down_proposes_a_candidate_and_the_gate_rejects_it(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from easyagent.app import create_app

    monkeypatch.setenv("EASYAGENT_LEARN", "1")
    clear_stop()

    async def complete(**kwargs):
        messages = kwargs.get("messages") or []
        system = ""
        if messages and isinstance(messages[0], dict):
            system = str(messages[0].get("content") or "")
        if "Propose one skill" in system:
            return _REJECTED
        return "The kiln cooled evenly."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "home", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"], "model": "bot-model"}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "how did the firing go"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]
    reacted = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/{answer['id']}/reaction",
        json={"emoji": "👎"},
    )
    assert reacted.status_code == 200, reacted.text
    panel = client.get(f"/api/bots/{bot['id']}/learning")
    assert panel.status_code == 200
    waiting = panel.json()["waiting"]
    assert [item["name"] for item in waiting] == ["missed-reply"]
    assert waiting[0]["status"] == "candidate"
    assert not (tmp_path / "skills" / "missed-reply.md").is_file()
    slept = client.post(f"/api/bots/{bot['id']}/learning/sleep")
    assert slept.status_code == 200, slept.text
    body = slept.json()
    assert body["status"] == "done"
    assert any(item.get("name") == "missed-reply" for item in body["rejected"])
    again = client.get(f"/api/bots/{bot['id']}/learning").json()
    assert all(item.get("name") != "missed-reply" for item in again["waiting"])
    assert any(item.get("name") == "missed-reply" for item in again["rejected"])
    assert not (tmp_path / "skills" / "missed-reply.md").is_file()


def test_a_user_correction_proposes_a_candidate(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from easyagent.app import create_app

    monkeypatch.setenv("EASYAGENT_LEARN", "1")
    clear_stop()

    async def complete(**kwargs):
        messages = kwargs.get("messages") or []
        system = str(messages[0].get("content") or "") if messages else ""
        if "Propose one skill" in system:
            return (
                '{"name":"corrected-reply","trigger":"when the person corrects a reply","steps":["Use the correction."],'
                '"pitfalls":[],"scope":"this bot","check":{"kind":"command","command":"false","exit_code":0}}'
            )
        return "The sky is green."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "home", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"], "model": "bot-model"}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    first = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what color is the sky"},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "that's not right, I meant blue"},
    )
    assert second.status_code == 200, second.text
    waiting = client.get(f"/api/bots/{bot['id']}/learning").json()["waiting"]
    assert [item["name"] for item in waiting] == ["corrected-reply"]
    assert waiting[0]["source"]["reason"] == "correction"
    assert not (tmp_path / "skills" / "corrected-reply.md").is_file()


def test_a_checker_revision_and_the_nightly_pass_propose_candidates(tmp_path, monkeypatch):
    import json

    from easyagent.check import force_check, review_turn
    from easyagent.journal import run_pass
    from datetime import datetime, timezone

    monkeypatch.setenv("EASYAGENT_LEARN", "1")
    store, _endpoint, bot = _world(tmp_path)
    observe(store, bot["id"], "The kiln reply was wrong and had to be redone.", reason="correction", task="Check the kiln")

    async def complete(**kwargs):
        messages = kwargs.get("messages") or []
        system = str(messages[0].get("content") or "") if messages else ""
        if "Propose one skill" in system:
            assert kwargs.get("timeout") in {None, 180.0} or (kwargs.get("timeout") or 0) >= 180
            return _REJECTED
        user = str(messages[-1].get("content") or "") if messages else ""
        passed = "FABRICATED-CLAIM" not in user
        return json.dumps(
            {
                "pass": passed,
                "problems": [] if passed else ["The reply quotes a line that was not returned."],
                "fix_hint": "" if passed else "Say the attempt failed.",
            }
        )

    monkeypatch.setattr("easyagent.llm.complete", complete)

    async def run_turn(**kwargs):
        if not getattr(run_turn, "done", False):
            run_turn.done = True
            yield "ledger", json.dumps([{"kind": "files", "action": "read", "ok": False, "result": "No such file"}])
            yield "final", "The first line is FABRICATED-CLAIM."
            return
        yield "ledger", "[]"
        yield "final", "The read failed. There is no line to quote."

    force_check(True)
    try:
        events = []

        async def collect():
            async for kind, text in review_turn(
                run_turn,
                base_url="http://127.0.0.1:9/v1",
                api_key=None,
                model="bot-model",
                messages=[{"role": "user", "content": "Read the note."}],
                store=store,
                bot_id=bot["id"],
            ):
                events.append((kind, text))

        asyncio.run(collect())
    finally:
        force_check(False)
    assert any(kind == "check" and "revised" in text for kind, text in events)
    waiting = list_candidates(store, bot["id"], status="candidate")
    assert any(item["name"] == "missed-reply" and (item.get("source") or {}).get("reason") == "checker" for item in waiting)
    assert not (store.skills_dir / "missed-reply.md").is_file()

    async def nightly(**kwargs):
        messages = kwargs.get("messages") or []
        system = str(messages[0].get("content") or "") if messages else ""
        if "Propose one skill" in system:
            assert kwargs.get("timeout") == 180.0
            return (
                '{"name":"night-note","trigger":"after the idle pass","steps":["Keep the note."],'
                '"pitfalls":[],"scope":"this bot","check":{"kind":"command","command":"false","exit_code":0}}'
            )
        return ""

    monkeypatch.setattr("easyagent.llm.complete", nightly)
    payload = asyncio.run(run_pass(store, bot, now=datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc), force=True))
    assert payload["result"] == "updated"
    names = {item["name"] for item in list_candidates(store, bot["id"], status="candidate")}
    assert "night-note" in names
    assert not (store.skills_dir / "night-note.md").is_file()
