"""The checker grades a draft with the bot's own model, then stops."""

import asyncio
import json

from fastapi.testclient import TestClient

from easyagent import gate
from easyagent import llm
from easyagent import turn as turn_mod
from easyagent.app import create_app
from easyagent.check import force_check, review_turn, should_skip
from easyagent.store import Store


def _store(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(
        name="home",
        base_url="http://127.0.0.1:9/v1",
        api_key=None,
        model="your-model",
        max_parallel=1,
    )
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="bot-model")
    return store, endpoint, bot


def _collect(store, bot, run_turn, messages=None):
    async def once():
        events = []
        async for kind, text in review_turn(
            run_turn,
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="bot-model",
            messages=messages or [{"role": "user", "content": "Read the note."}],
            store=store,
            bot_id=bot["id"],
        ):
            events.append((kind, text))
        return events

    return asyncio.run(once())


def test_a_short_reply_with_no_tools_is_not_graded():
    assert should_skip("Hello.", []) is True
    assert should_skip("Hello.", [{"kind": "files"}]) is False
    assert should_skip("word " * 80, []) is False


def test_a_fabricated_reply_is_revised(tmp_path, monkeypatch):
    store, _endpoint, bot = _store(tmp_path)
    seen = []

    async def run_turn(**kwargs):
        if not seen:
            yield "ledger", json.dumps(
                [{"kind": "files", "action": "read", "ok": False, "result": "No such file"}]
            )
            yield "final", "The first line is FABRICATED-CLAIM."
            return
        yield "ledger", "[]"
        yield "final", "The read failed. There is no line to quote."

    async def complete(**kwargs):
        seen.append(kwargs)
        user = kwargs["messages"][-1]["content"]
        passed = "FABRICATED-CLAIM" not in user
        return json.dumps(
            {
                "pass": passed,
                "problems": [] if passed else ["The reply quotes a line that was not returned."],
                "fix_hint": "" if passed else "Say the attempt failed.",
            }
        )

    monkeypatch.setattr("easyagent.llm.complete", complete)
    force_check(True)
    try:
        events = _collect(store, bot, run_turn)
    finally:
        force_check(False)
    finals = [text for kind, text in events if kind == "final"]
    assert finals == ["The read failed. There is no line to quote."]
    badge = [text for kind, text in events if kind == "check"]
    assert json.loads(badge[-1])["badge"] == "revised"
    assert len(seen) == 1
    assert seen[0]["model"] == "bot-model"
    assert seen[0]["base_url"] == "http://127.0.0.1:9/v1"
    assert "EASYAGENT_CHECK_V1" in seen[0]["messages"][0]["content"]


def test_a_failed_check_stops_after_two_revisions(tmp_path, monkeypatch):
    store, _endpoint, bot = _store(tmp_path)
    turns = {"n": 0}
    grades = {"n": 0}
    paragraph = "LOOP-CHECK " + ("ordinary note " * 30)

    async def run_turn(**kwargs):
        turns["n"] += 1
        yield "ledger", "[]"
        yield "final", paragraph

    async def complete(**kwargs):
        grades["n"] += 1
        return json.dumps(
            {"pass": False, "problems": ["The reply still has the marker."], "fix_hint": "Remove the marker."}
        )

    monkeypatch.setattr("easyagent.llm.complete", complete)
    force_check(True)
    try:
        events = _collect(store, bot, run_turn, [{"role": "user", "content": "Give a long status."}])
    finally:
        force_check(False)
    assert turns["n"] == 3
    assert grades["n"] == 3
    finals = [text for kind, text in events if kind == "final"]
    assert finals == [paragraph]
    assert json.loads([text for kind, text in events if kind == "check"][-1])["badge"] == "revised"


def test_stop_between_revisions_does_not_start_another(tmp_path, monkeypatch):
    store, _endpoint, bot = _store(tmp_path)
    chat = store.create_chat(bot["id"])
    turns = {"n": 0}

    async def run_turn(**kwargs):
        turns["n"] += 1
        yield "ledger", json.dumps([{"kind": "files", "action": "read", "ok": False, "result": "No such file"}])
        yield "final", "The first line is FABRICATED-CLAIM."

    async def complete(**kwargs):
        turn_mod.interrupt(store, chat["id"], "you pressed Stop")
        return json.dumps({"pass": False, "problems": ["Invented."], "fix_hint": "Say the attempt failed."})

    monkeypatch.setattr("easyagent.llm.complete", complete)

    async def once():
        turn_mod.bind(store, chat["id"], bot["id"])
        events = []
        async for kind, text in review_turn(
            run_turn,
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="bot-model",
            messages=[{"role": "user", "content": "Read the note."}],
            store=store,
            bot_id=bot["id"],
            chat_id=chat["id"],
        ):
            events.append((kind, text))
        return events

    force_check(True)
    try:
        try:
            asyncio.run(once())
        except turn_mod.TurnCancelled:
            stopped = True
        else:
            stopped = False
    finally:
        force_check(False)
    assert stopped
    assert turns["n"] == 1


def test_a_missed_grade_keeps_the_draft(tmp_path, monkeypatch):
    store, _endpoint, bot = _store(tmp_path)
    turns = {"n": 0}

    async def run_turn(**kwargs):
        turns["n"] += 1
        yield "ledger", json.dumps([{"kind": "files", "action": "read", "ok": True, "result": "pine"}])
        yield "final", "The note says pine."

    async def down(**kwargs):
        raise llm.ProviderError("down")

    async def nonsense(**kwargs):
        return "not json"

    force_check(True)
    try:
        monkeypatch.setattr("easyagent.llm.complete", down)
        events = _collect(store, bot, run_turn)
        assert [text for kind, text in events if kind == "final"] == ["The note says pine."]
        assert not [kind for kind, _text in events if kind == "check"]
        monkeypatch.setattr("easyagent.llm.complete", nonsense)
        events = _collect(store, bot, run_turn)
        assert [text for kind, text in events if kind == "final"] == ["The note says pine."]
        assert turns["n"] == 2
    finally:
        force_check(False)


def test_the_grade_waits_on_the_bots_connection(tmp_path, monkeypatch):
    store, endpoint, bot = _store(tmp_path)
    gate.reset_lanes()

    async def run_turn(**kwargs):
        yield "ledger", json.dumps([{"kind": "files", "action": "read", "ok": True, "result": "pine"}])
        yield "final", "The note says pine."

    async def complete(**kwargs):
        permit = await gate.reserve()
        try:
            await permit.acquire()
            return '{"pass": true, "problems": [], "fix_hint": ""}'
        finally:
            await permit.release()

    monkeypatch.setattr("easyagent.llm.complete", complete)

    async def scenario():
        holding = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            token = gate.bind_connection(endpoint, "Busy")
            try:
                seat = await gate.reserve()
                await seat.acquire()
                holding.set()
                await release.wait()
                await seat.release()
            finally:
                gate.reset_connection(token)

        async def grader():
            await holding.wait()
            token = gate.bind_connection(endpoint, bot["name"])
            try:
                events = []
                async for kind, text in review_turn(
                    run_turn,
                    base_url=endpoint["base_url"],
                    api_key=None,
                    model="bot-model",
                    messages=[{"role": "user", "content": "Read the note."}],
                    store=store,
                    bot_id=bot["id"],
                ):
                    events.append((kind, text))
                return events
            finally:
                gate.reset_connection(token)

        held = asyncio.create_task(holder())
        graded = asyncio.create_task(grader())
        await holding.wait()
        await asyncio.sleep(0.15)
        assert not graded.done()
        release.set()
        events = await graded
        await held
        return events

    force_check(True)
    try:
        events = asyncio.run(scenario())
    finally:
        force_check(False)
        gate.reset_lanes()
    assert json.loads([text for kind, text in events if kind == "check"][-1])["badge"] == "checked"


def test_the_check_is_on_unless_the_bot_turns_it_off(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        endpoint = client.post(
            "/api/endpoints",
            json={"name": "home", "base_url": "http://127.0.0.1:9/v1", "model": "your-model"},
        ).json()
        bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
        assert bot["check_enabled"] is True
        stored = json.loads((tmp_path / "bots" / bot["id"] / "bot.json").read_text(encoding="utf-8"))
        assert "check_enabled" not in stored
        off = client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
        assert off.status_code == 200
        assert off.json()["check_enabled"] is False
        stored = json.loads((tmp_path / "bots" / bot["id"] / "bot.json").read_text(encoding="utf-8"))
        assert stored["check_enabled"] is False
        on = client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": True})
        assert on.json()["check_enabled"] is True
        stored = json.loads((tmp_path / "bots" / bot["id"] / "bot.json").read_text(encoding="utf-8"))
        assert "check_enabled" not in stored
        page = client.get("/classic")
        assert "Check replies before sending" in page.text
        assert "EasyAgent uses your connected model to review and learn — no extra model needed." in page.text
        assert "Judge connection" not in page.text
        assert "judge connection" not in page.text
        assert "Pause learning while this bot is idle" in page.text
        assert "Hold candidates until I approve them" in page.text
        assert "Roll back the last change" in page.text
        script = client.get("/static/app.js?v=41").text
        assert "revised after check" in script
        assert "settings-check" in script
        assert "learn-pause" in script
        assert 'startsWith("Learned:")' in script
        assert "judge connection" not in script.lower()
        css = client.get("/static/app.css?v=36").text
        assert ".check-badge" in css
        assert ".learn-note" in css
