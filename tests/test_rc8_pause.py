"""0.3.46 rc8: one pause row, and a finished answer is not resumable."""

import json

from fastapi.testclient import TestClient

from easyagent.app import (
    FINISHED_BEFORE_PAUSING,
    PAUSED_SAY_CONTINUE,
    PAUSED_YOU_SENT,
    _drop_pause_rows,
    create_app,
)


def _events(body: str) -> list[dict]:
    rows = []
    for block in body.split("\n\n"):
        data = "\n".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
        if data:
            rows.append(json.loads(data))
    return rows


def _visible(messages: list) -> list[str]:
    rows = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        if item.get("about") in {"activity", "queue"} or item.get("id") in {"activity", "live-status"}:
            continue
        kind = str(item.get("kind") or "")
        content = str(item.get("content") or "")
        if kind in {"status", "pickup", "activity"}:
            rows.append(content)
            continue
        if item.get("role") not in {"user", "assistant"}:
            continue
        if item.get("role") == "assistant" and not content.strip() and not item.get("attachment") and not item.get("choices"):
            raise AssertionError(f"empty assistant row saved: {item}")
        rows.append(content)
    return rows


def _world(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return app, client, bot, chat


def _answer(length: int = 336) -> str:
    body = "The east pier is listed. " + ("kiln " * 80)
    return body[:length]


def test_a_finished_answer_drops_say_continue_on_screen_and_after_reload(tmp_path, monkeypatch):
    """336 of 336 chars were saved. Nothing is left to run, so the continue row is not kept."""
    import easyagent.llm as llm
    from easyagent import turn as turn_mod
    from easyagent.hermes_busy import _hm_handle_running_session_message
    from easyagent.plan import enqueue

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    calls = {"model": 0}
    full = _answer()

    async def stream_complete(**_kwargs):
        calls["model"] += 1
        if calls["model"] == 1:
            yield full
            enqueue(store, bot["id"], chat["id"], "and the kiln", client_id="c-late")
            _hm_handle_running_session_message(store, bot["id"], chat["id"], "and the kiln")
            raise turn_mod.TurnCancelled()
        yield "The kiln is warm."

    async def complete(**_kwargs):
        return "The kiln is warm."

    monkeypatch.setattr(llm, "stream_complete", stream_complete)
    monkeypatch.setattr(llm, "complete", complete)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "check the east pier"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    done = [item for item in _events(body) if item.get("type") == "done"]
    assert done, body[-800:]
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    screen = _visible(done[-1]["chat"]["messages"])
    stored = _visible(saved["messages"])
    assert stored == screen
    assert full in stored
    assert "and the kiln" in stored
    assert "The kiln is warm." in stored
    assert PAUSED_SAY_CONTINUE not in stored
    assert PAUSED_YOU_SENT not in stored
    assert saved["run"]["status"] != "paused"
    again = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert _visible(again["messages"]) == stored
    assert PAUSED_SAY_CONTINUE not in _visible(again["messages"])
    client.close()


def test_a_mid_text_cut_still_says_continue_after_reload(tmp_path, monkeypatch):
    """Text that stopped before the model finished is still resumable."""
    import easyagent.llm as llm
    from easyagent import turn as turn_mod
    from easyagent.hermes_busy import _hm_handle_running_session_message
    from easyagent.plan import enqueue

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    calls = {"model": 0}
    partial = "The east pier is half listed."

    async def stream_complete(**_kwargs):
        calls["model"] += 1
        if calls["model"] == 1:
            yield partial
            enqueue(store, bot["id"], chat["id"], "and the kiln", client_id="c-cut")
            _hm_handle_running_session_message(store, bot["id"], chat["id"], "and the kiln")
            turn_mod.slot_for(store, chat["id"]).cut_unfinished = True
            raise turn_mod.TurnCancelled()
        yield "The kiln is warm."

    async def complete(**_kwargs):
        return "The kiln is warm."

    monkeypatch.setattr(llm, "stream_complete", stream_complete)
    monkeypatch.setattr(llm, "complete", complete)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "check the east pier"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    done = [item for item in _events(body) if item.get("type") == "done"]
    assert done, body[-800:]
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    screen = _visible(done[-1]["chat"]["messages"])
    stored = _visible(saved["messages"])
    assert stored == screen
    assert partial in stored
    assert PAUSED_SAY_CONTINUE in stored
    assert PAUSED_YOU_SENT not in stored
    assert stored.count(PAUSED_SAY_CONTINUE) == 1
    assert saved["run"]["status"] == "paused"
    again = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert _visible(again["messages"]) == stored
    client.close()


def test_one_finished_tool_does_not_leave_say_continue(tmp_path, monkeypatch):
    """A side question during one tool step is the next turn. Nothing is left to resume."""
    import easyagent.llm as llm

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    calls = {"model": 0, "tools": 0}

    async def execute(_store, request, bot_id=None):
        del bot_id
        calls["tools"] += 1
        from easyagent import turn as turn_mod
        from easyagent.hermes_busy import _hm_handle_running_session_message
        from easyagent.plan import enqueue

        slot = turn_mod.slot_for(_store, chat["id"])
        slot._executing_tools = True
        slot.sealing = False
        enqueue(_store, bot["id"], chat["id"], "and the kiln", client_id="c-kiln")
        _hm_handle_running_session_message(_store, bot["id"], chat["id"], "and the kiln")
        return f"listed {request.path}"

    async def stream_complete(**kwargs):
        calls["model"] += 1
        if calls["model"] == 1:
            llm._store_native_calls({
                0: {"id": "call_1", "name": "list_dir", "arguments": '{"path": "pier-1"}'},
            })
            if False:
                yield ""
            return
        yield "The kiln is warm."

    async def complete(**_kwargs):
        return "The kiln is warm."

    monkeypatch.setattr("easyagent.tools.execute", execute)
    monkeypatch.setattr("easyagent.llm.stream_complete", stream_complete)
    monkeypatch.setattr("easyagent.llm.complete", complete)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "check the east pier"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    done = [item for item in _events(body) if item.get("type") == "done"]
    assert done, body[-800:]
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    stored = _visible(saved["messages"])
    screen = _visible(done[-1]["chat"]["messages"])
    assert calls["tools"] == 1, calls
    assert stored == screen
    assert stored == [
        "check the east pier",
        "and the kiln",
        "The kiln is warm.",
    ]
    assert PAUSED_SAY_CONTINUE not in stored
    assert PAUSED_YOU_SENT not in stored
    assert FINISHED_BEFORE_PAUSING not in stored
    assert saved["run"]["status"] != "paused"
    client.close()


def test_a_pause_row_above_a_message_is_not_deleted(tmp_path):
    """Retiring the row keeps its place, so the message under it does not jump."""
    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "check the east pier", "created_at": "2026-10-22T18:00:00Z"},
        {"id": "a1", "role": "assistant", "content": "The east pier is listed.", "created_at": "2026-10-22T18:00:30Z"},
        {"id": "pause", "role": "assistant", "kind": "status", "content": PAUSED_YOU_SENT, "created_at": "2026-10-22T18:00:33Z"},
        {"id": "u2", "role": "user", "content": "and the kiln", "created_at": "2026-10-22T18:00:34Z"},
    ]
    store.save_chat(doc)
    _drop_pause_rows(store, bot["id"], chat["id"])
    kept = store.get_chat(bot["id"], chat["id"])
    rows = _visible(kept["messages"])
    assert rows == [
        "check the east pier",
        "The east pier is listed.",
        FINISHED_BEFORE_PAUSING,
        "and the kiln",
    ]
    assert next(item["id"] for item in kept["messages"] if item.get("content") == FINISHED_BEFORE_PAUSING) == "pause"
    client.close()


def test_an_unfinished_tool_chain_stays_say_continue_and_continue_answers(tmp_path, monkeypatch):
    """A second tool step and the report are still owed. Continue answers the first question."""
    import easyagent.llm as llm

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    calls = {"model": 0, "tools": 0}
    seen: list[list] = []
    partial = "The east pier is listed."

    async def execute(_store, request, bot_id=None):
        del bot_id
        calls["tools"] += 1
        from easyagent import turn as turn_mod
        from easyagent.hermes_busy import _hm_handle_running_session_message
        from easyagent.plan import enqueue

        slot = turn_mod.slot_for(_store, chat["id"])
        slot._executing_tools = True
        slot.sealing = False
        enqueue(_store, bot["id"], chat["id"], "and the kiln", client_id="c-kiln")
        _hm_handle_running_session_message(_store, bot["id"], chat["id"], "and the kiln")
        return f"listed {request.path}"

    async def stream_complete(**kwargs):
        calls["model"] += 1
        seen.append(list(kwargs.get("messages") or []))
        number = calls["model"]
        if number == 1:
            llm._store_native_calls({
                0: {"id": "call_1", "name": "list_dir", "arguments": '{"path": "pier-1"}'},
            })
            yield partial
            return
        if number == 2:
            yield "The kiln is warm."
            return
        yield "The pier is clear."

    async def complete(**kwargs):
        seen.append(list(kwargs.get("messages") or []))
        return "The pier is clear."

    monkeypatch.setattr("easyagent.tools.execute", execute)
    monkeypatch.setattr("easyagent.llm.stream_complete", stream_complete)
    monkeypatch.setattr("easyagent.llm.complete", complete)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "check the east pier"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    done = [item for item in _events(body) if item.get("type") == "done"]
    assert done, body[-800:]
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    screen = _visible(done[-1]["chat"]["messages"])
    stored = _visible(saved["messages"])
    assert calls["tools"] == 1, calls
    assert stored == screen
    assert partial in stored
    assert "and the kiln" in stored
    assert "The kiln is warm." in stored
    assert PAUSED_SAY_CONTINUE in stored
    assert stored.count(PAUSED_SAY_CONTINUE) == 1
    assert PAUSED_YOU_SENT not in stored
    assert FINISHED_BEFORE_PAUSING not in stored
    assert stored.index(PAUSED_SAY_CONTINUE) < stored.index("and the kiln")
    assert saved["run"]["status"] == "paused"
    again = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert _visible(again["messages"]) == stored

    with client.stream(
        "POST",
        url,
        json={"content": "continue", "client_id": "c-continue"},
        headers={"Accept": "text/event-stream"},
    ) as resumed:
        assert resumed.status_code == 200, resumed.read()
        resumed_body = resumed.read().decode()
    resumed_events = _events(resumed_body)
    done_resume = [item for item in resumed_events if item.get("type") == "done"]
    assert done_resume, resumed_body[-800:]
    reloaded = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    resumed_rows = _visible(done_resume[-1]["chat"]["messages"])
    stored_rows = _visible(reloaded["messages"])
    assert stored_rows == resumed_rows
    assert stored_rows == [
        "check the east pier",
        partial,
        "and the kiln",
        "The kiln is warm.",
        "continue",
        "The pier is clear.",
    ]
    assert [item.get("content") for item in reloaded["messages"] if item.get("role") == "user"] == [
        "check the east pier",
        "and the kiln",
        "continue",
    ]
    typed = next(item for item in reloaded["messages"] if item.get("client_id") == "c-continue")
    assert typed["role"] == "user"
    assert typed["content"] == "continue"
    last_user = ""
    for item in reversed(seen[-1]):
        if item.get("role") == "user":
            last_user = item.get("content") or ""
            break
    assert last_user == "Resume the checklist where it stopped. Do not start over."
    client.close()


def test_a_finish_call_beside_the_answer_is_complete(tmp_path, monkeypatch):
    """A finish tool in the same step as the text is the answer. It does not say continue."""
    import easyagent.llm as llm
    from easyagent import turn as turn_mod
    from easyagent.hermes_busy import _hm_handle_running_session_message
    from easyagent.plan import enqueue

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    calls = {"model": 0}
    full = _answer(344)

    async def stream_complete(**_kwargs):
        calls["model"] += 1
        if calls["model"] == 1:
            yield full
            llm._store_native_calls({
                0: {"id": "call_finish", "name": "finish", "arguments": '{"status":"proven"}'},
            })
            enqueue(store, bot["id"], chat["id"], "and the kiln", client_id="c-late")
            _hm_handle_running_session_message(store, bot["id"], chat["id"], "and the kiln")
            raise turn_mod.TurnCancelled()
        yield "The kiln is warm."

    async def complete(**_kwargs):
        return "The kiln is warm."

    monkeypatch.setattr(llm, "stream_complete", stream_complete)
    monkeypatch.setattr(llm, "complete", complete)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "check the east pier"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    done = [item for item in _events(body) if item.get("type") == "done"]
    assert done, body[-800:]
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    screen = _visible(done[-1]["chat"]["messages"])
    stored = _visible(saved["messages"])
    assert stored == screen
    assert full in stored
    assert "and the kiln" in stored
    assert "The kiln is warm." in stored
    assert PAUSED_SAY_CONTINUE not in stored
    assert PAUSED_YOU_SENT not in stored
    pause_rows = [row for row in stored if row in {PAUSED_SAY_CONTINUE, PAUSED_YOU_SENT, FINISHED_BEFORE_PAUSING}]
    assert pause_rows == [] or pause_rows == [FINISHED_BEFORE_PAUSING]
    assert saved["run"]["status"] != "paused"
    again = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert _visible(again["messages"]) == stored
    assert PAUSED_SAY_CONTINUE not in _visible(again["messages"])
    client.close()


def test_a_cut_stays_say_continue_until_a_later_message_clears_it(tmp_path):
    """A reply cut mid-text is not labeled finished. A later message removes the row."""
    from easyagent import turn as turn_mod

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "check the east pier", "created_at": "2026-10-22T18:00:00Z"},
        {"id": "a1", "role": "assistant", "content": "The east pier is half listed.", "created_at": "2026-10-22T18:00:30Z"},
        {"id": "pause", "role": "assistant", "kind": "status", "content": PAUSED_SAY_CONTINUE, "created_at": "2026-10-22T18:00:33Z"},
        {"id": "u2", "role": "user", "content": "and the kiln", "created_at": "2026-10-22T18:00:34Z"},
    ]
    store.save_chat(doc)
    slot = turn_mod.slot_for(store, chat["id"])
    slot.cut_unfinished = True
    _drop_pause_rows(store, bot["id"], chat["id"])
    kept = _visible(store.get_chat(bot["id"], chat["id"])["messages"])
    assert kept == [
        "check the east pier",
        "The east pier is half listed.",
        PAUSED_SAY_CONTINUE,
        "and the kiln",
    ]
    assert FINISHED_BEFORE_PAUSING not in kept
    slot.cut_unfinished = False
    _drop_pause_rows(store, bot["id"], chat["id"])
    still = _visible(store.get_chat(bot["id"], chat["id"])["messages"])
    assert PAUSED_SAY_CONTINUE in still
    assert FINISHED_BEFORE_PAUSING not in still
    _drop_pause_rows(store, bot["id"], chat["id"], force=True)
    cleared = _visible(store.get_chat(bot["id"], chat["id"])["messages"])
    assert cleared == [
        "check the east pier",
        "The east pier is half listed.",
        "and the kiln",
    ]
    client.close()


def test_stop_removes_every_finished_pause_row(tmp_path):
    """Stop leaves no pause row, including two that already say finished."""
    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["run"] = {
        "id": "run-paused",
        "status": "paused",
        "started_at": "2026-10-22T18:00:00Z",
        "last_activity_at": "2026-10-22T18:00:40Z",
        "current_step": "",
        "reason": "",
    }
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "check the east pier", "created_at": "2026-10-22T18:00:00Z"},
        {"id": "f1", "role": "assistant", "kind": "status", "content": FINISHED_BEFORE_PAUSING, "created_at": "2026-10-22T18:00:33Z"},
        {"id": "u2", "role": "user", "content": "and the kiln", "created_at": "2026-10-22T18:00:34Z"},
        {"id": "f2", "role": "assistant", "kind": "status", "content": FINISHED_BEFORE_PAUSING, "created_at": "2026-10-22T18:00:40Z"},
    ]
    store.save_chat(doc)
    stopped = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/stop")
    assert stopped.status_code == 200, stopped.text
    texts = _visible(stopped.json()["messages"])
    assert texts == ["check the east pier", "and the kiln"]
    assert FINISHED_BEFORE_PAUSING not in texts
    assert PAUSED_SAY_CONTINUE not in texts
    client.close()
