"""0.3.55: a resumed answer opens its live row before the first token.

When a message interrupts a running turn, the resumed answer streams on the
same SSE. The screen switches from the frozen partial to the new reply when a
run event carries the new reply_id. If that id only appears after the first
delta, the first words are dropped and the answer starts in jumps. The first
run event after the pause must already name the new live message.
"""

import json

from fastapi.testclient import TestClient

from easyagent.app import create_app


def _events(body: str) -> list[dict]:
    rows = []
    for block in body.split("\n\n"):
        data = "\n".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
        if data:
            rows.append(json.loads(data))
    return rows


def _world(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return app, client, bot, chat


def test_the_resumed_stream_names_the_new_reply_before_the_first_delta(tmp_path, monkeypatch):
    """The first run event after the pause carries a reply_id, and it comes first."""
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
            enqueue(store, bot["id"], chat["id"], "and the kiln", client_id="c-0355")
            _hm_handle_running_session_message(store, bot["id"], chat["id"], "and the kiln")
            raise turn_mod.TurnCancelled()
        yield "The kiln is warm."

    async def complete(**_kwargs):
        return "The kiln is warm."

    monkeypatch.setattr(llm, "stream_complete", stream_complete)
    monkeypatch.setattr(llm, "complete", complete)
    with client.stream(
        "POST",
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "check the east pier"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()

    events = _events(body)
    # The partial streams first; the resumed answer's first delta must come
    # after a run event that names a NEW live reply (a second reply_id).
    first_ids: list[str] = []
    switch_at: int | None = None
    resumed_delta_at: int | None = None
    for i, item in enumerate(events):
        if item.get("type") == "run":
            rid = str((item.get("run") or {}).get("reply_id") or "")
            if rid and rid not in first_ids:
                first_ids.append(rid)
                if len(first_ids) == 2:
                    switch_at = i
        if item.get("type") == "delta" and "kiln" in str(item.get("text") or "") and resumed_delta_at is None:
            resumed_delta_at = i
    assert len(first_ids) == 2, f"expected two distinct live reply ids, got {first_ids}: {body[-800:]}"
    assert resumed_delta_at is not None, "the resumed answer never streamed a delta"
    # The live message exists before any word of the resumed answer arrives.
    assert switch_at is not None and switch_at < resumed_delta_at, (
        "the first resumed delta arrived before the run event named the new reply"
    )
    new_reply_id = first_ids[1]

    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    contents = [str(m.get("content") or "") for m in saved["messages"] if isinstance(m, dict)]
    assert partial in contents
    assert "The kiln is warm." in contents
    # The row that opened early is the row the resumed answer finished into.
    resumed = [m for m in saved["messages"] if isinstance(m, dict) and m.get("content") == "The kiln is warm."]
    assert resumed and resumed[-1].get("id") == new_reply_id


def test_the_resumed_live_row_survives_a_pause_before_any_words(tmp_path, monkeypatch):
    """A second interrupt before the resumed answer speaks leaves no empty row."""
    import easyagent.llm as llm
    from easyagent import turn as turn_mod
    from easyagent.hermes_busy import _hm_handle_running_session_message
    from easyagent.plan import enqueue

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    calls = {"model": 0}

    async def stream_complete(**_kwargs):
        calls["model"] += 1
        if calls["model"] == 1:
            yield "The east pier is half listed."
            enqueue(store, bot["id"], chat["id"], "and the kiln", client_id="c-0355b")
            _hm_handle_running_session_message(store, bot["id"], chat["id"], "and the kiln")
            raise turn_mod.TurnCancelled()
        if calls["model"] == 2:
            # The resumed answer is interrupted again before it says anything.
            enqueue(store, bot["id"], chat["id"], "actually never mind", client_id="c-0355c")
            _hm_handle_running_session_message(store, bot["id"], chat["id"], "actually never mind")
            raise turn_mod.TurnCancelled()
        yield "Both messages read."

    async def complete(**_kwargs):
        return "Both messages read."

    monkeypatch.setattr(llm, "stream_complete", stream_complete)
    monkeypatch.setattr(llm, "complete", complete)
    with client.stream(
        "POST",
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "check the east pier"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        response.read()

    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    contents = [str(m.get("content") or "") for m in saved["messages"] if isinstance(m, dict)]
    assert "Both messages read." in contents
    for message in saved["messages"]:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and message.get("live"):
            assert str(message.get("content") or "").strip(), f"empty live row left in the transcript: {message}"
