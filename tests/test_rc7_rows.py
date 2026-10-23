"""0.3.46 rc7: one true pause row, and a last-moment message is not left Queued."""

import json

from fastapi.testclient import TestClient

from easyagent.app import (
    PAUSED_SAY_CONTINUE,
    PAUSED_YOU_SENT,
    create_app,
)
from easyagent.store import now_iso


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


def _queued(messages: list) -> list[str]:
    return [
        str(item.get("content") or "")
        for item in messages or []
        if isinstance(item, dict) and item.get("role") == "user" and item.get("chip") == "queued"
    ]


def test_a_message_in_the_last_moment_is_answered_before_idle(tmp_path, monkeypatch):
    """The final 0.5s is the save, after the model call has sealed. The answer stays, and Queued does not outlive the turn."""
    import easyagent.llm as llm
    import easyagent.tools as tools
    from easyagent import turn as turn_mod
    from easyagent.hermes_busy import _hm_handle_running_session_message
    from easyagent.plan import enqueue

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    calls = {"model": 0}
    hooked = {"late": False}

    async def stream_complete(**kwargs):
        calls["model"] += 1
        if calls["model"] == 1:
            yield "The east pier is listed."
            return
        yield "The kiln is warm."

    async def complete(**_kwargs):
        return "The kiln is warm."

    real_honesty = tools.finish_honesty

    async def finish_honesty(final, **kwargs):
        if not hooked["late"]:
            hooked["late"] = True
            slot = turn_mod.slot_for(store, chat["id"])
            assert slot.sealing is True
            enqueue(store, bot["id"], chat["id"], "and the kiln", client_id="c-late")
            _hm_handle_running_session_message(store, bot["id"], chat["id"], "and the kiln")
        return await real_honesty(final, **kwargs)

    monkeypatch.setattr(llm, "stream_complete", stream_complete)
    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(tools, "finish_honesty", finish_honesty)

    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "check the east pier"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    events = _events(body)
    done = [item for item in events if item.get("type") == "done"]
    assert done, body[-800:]
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    screen = _visible(done[-1]["chat"]["messages"])
    stored = _visible(saved["messages"])
    assert stored == screen
    assert "The east pier is listed." in stored
    assert "The kiln is warm." in stored
    assert "and the kiln" in stored
    assert _queued(done[-1]["chat"]["messages"]) == []
    assert _queued(saved["messages"]) == []
    assert not (saved["run"]["status"] == "idle" and _queued(saved["messages"]))
    assert saved["run"]["status"] != "idle" or _queued(saved["messages"]) == []
    client.close()


def test_a_message_after_the_drain_looked_is_still_answered(tmp_path, monkeypatch):
    """Enqueue in the save, after the first drain already found an empty queue."""
    import easyagent.app as app_mod
    import easyagent.llm as llm
    from easyagent.hermes_busy import _hm_handle_running_session_message
    from easyagent.plan import enqueue

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    calls = {"model": 0}
    hooked = {"gap": False}
    real_park = app_mod._park_side_answer

    async def stream_complete(**kwargs):
        calls["model"] += 1
        if calls["model"] == 1:
            yield "The east pier is listed."
            return
        yield "The kiln is warm."

    async def complete(**_kwargs):
        return "The kiln is warm."

    def park(store_obj, bot_id, chat_id):
        if not hooked["gap"]:
            hooked["gap"] = True
            enqueue(store_obj, bot_id, chat_id, "and the kiln", client_id="c-gap")
            _hm_handle_running_session_message(store_obj, bot_id, chat_id, "and the kiln")
        return real_park(store_obj, bot_id, chat_id)

    monkeypatch.setattr(llm, "stream_complete", stream_complete)
    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(app_mod, "_park_side_answer", park)

    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "check the east pier"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    events = _events(body)
    done = [item for item in events if item.get("type") == "done"]
    assert done, body[-800:]
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    screen = _visible(done[-1]["chat"]["messages"])
    stored = _visible(saved["messages"])
    assert stored == screen
    assert stored == [
        "check the east pier",
        "The east pier is listed.",
        "and the kiln",
        "The kiln is warm.",
    ]
    assert _queued(saved["messages"]) == []
    assert saved["run"]["status"] != "idle" or _queued(saved["messages"]) == []
    assert PAUSED_YOU_SENT not in stored
    assert PAUSED_SAY_CONTINUE not in stored
    client.close()


def test_stop_and_a_follow_up_clear_a_pause_row(tmp_path, monkeypatch):
    import easyagent.llm as llm

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["run"] = {
        "id": "run-paused",
        "status": "paused",
        "started_at": now_iso(),
        "last_activity_at": now_iso(),
        "current_step": "",
        "reason": "",
    }
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "check the east pier", "created_at": now_iso()},
        {"id": "a1", "role": "assistant", "content": "The east pier is half listed.", "created_at": now_iso()},
        {"id": "sent", "role": "assistant", "kind": "status", "content": PAUSED_YOU_SENT, "created_at": now_iso()},
        {"id": "paused", "role": "assistant", "kind": "status", "content": PAUSED_SAY_CONTINUE, "created_at": now_iso()},
    ]
    store.save_chat(doc)
    stopped = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/stop")
    assert stopped.status_code == 200, stopped.text
    texts = _visible(stopped.json()["messages"])
    assert PAUSED_YOU_SENT not in texts
    assert PAUSED_SAY_CONTINUE not in texts
    assert "Resumed" not in texts

    doc = store.get_chat(bot["id"], chat["id"])
    doc["run"] = {
        "id": "run-paused-2",
        "status": "paused",
        "started_at": now_iso(),
        "last_activity_at": now_iso(),
        "current_step": "",
        "reason": "",
    }
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "check the east pier", "created_at": now_iso()},
        {"id": "a1", "role": "assistant", "content": "The east pier is half listed.", "created_at": now_iso()},
        {"id": "paused", "role": "assistant", "kind": "status", "content": PAUSED_SAY_CONTINUE, "created_at": now_iso()},
    ]
    store.save_chat(doc)

    async def stream_complete(**_kwargs):
        yield "The kiln is warm."

    async def complete(**_kwargs):
        return "The kiln is warm."

    monkeypatch.setattr(llm, "stream_complete", stream_complete)
    monkeypatch.setattr(llm, "complete", complete)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "thanks"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    done = [item for item in _events(body) if item.get("type") == "done"]
    assert done, body[-800:]
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert _visible(done[-1]["chat"]["messages"]) == _visible(saved["messages"])
    assert PAUSED_SAY_CONTINUE not in _visible(saved["messages"])
    assert PAUSED_YOU_SENT not in _visible(saved["messages"])
    assert "thanks" in _visible(saved["messages"])
    assert "The kiln is warm." in _visible(saved["messages"])
    client.close()
