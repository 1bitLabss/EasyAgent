"""0.3.46 rc8: one pause row, and a finished answer is not resumable."""

import json

from fastapi.testclient import TestClient

from easyagent.app import (
    PAUSED_SAY_CONTINUE,
    PAUSED_YOU_SENT,
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
