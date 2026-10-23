"""0.3.46 rc6: a mid-chain message does not leave an empty assistant row."""

import json

from fastapi.testclient import TestClient

from easyagent.app import (
    PAUSED_SAY_CONTINUE,
    PAUSED_YOU_SENT,
    _close_partial_for_pause,
    _park_side_answer,
    _pause_for_resume,
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
    """The text of each row the screen keeps. An empty assistant is not a row."""
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
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return app, client, bot, chat


def test_partial_text_stays_and_a_blank_row_is_not_saved(tmp_path):
    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["run"] = {
        "id": "run-part",
        "status": "running",
        "started_at": now_iso(),
        "last_activity_at": now_iso(),
        "current_step": "Listing pier-3",
        "reason": "",
    }
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "check the east pier", "created_at": now_iso()},
        {
            "id": "a-empty",
            "role": "assistant",
            "content": "",
            "thinking": "planning the pier",
            "live": True,
            "created_at": now_iso(),
        },
        {
            "id": "a-part",
            "role": "assistant",
            "content": "The east pier is half listed.",
            "live": True,
            "created_at": now_iso(),
        },
        {"id": "u2", "role": "user", "content": "and the kiln", "chip": "queued", "created_at": now_iso()},
    ]
    store.save_chat(doc)
    _close_partial_for_pause(store, bot["id"], chat["id"])
    paused = store.get_chat(bot["id"], chat["id"])
    ids = [item.get("id") for item in paused["messages"]]
    assert "a-empty" not in ids
    kept = next(item for item in paused["messages"] if item.get("id") == "a-part")
    assert kept["content"] == "The east pier is half listed."
    assert "live" not in kept
    assert "interrupted" not in kept
    assert _visible(paused["messages"]) == [
        "check the east pier",
        "The east pier is half listed.",
        PAUSED_YOU_SENT,
        "and the kiln",
    ]
    public = _park_side_answer(store, bot["id"], chat["id"])
    assert public is not None
    assert _visible(public["messages"]) == [
        "check the east pier",
        "The east pier is half listed.",
        PAUSED_YOU_SENT,
        "and the kiln",
        PAUSED_SAY_CONTINUE,
    ]
    assert public["run"]["status"] == "paused"
    assert "Interrupted, resuming" not in _visible(public["messages"])
    client.close()


def test_a_restart_without_auto_resume_does_not_say_interrupted(tmp_path):
    app, client, bot, chat = _world(tmp_path)
    client.patch(f"/api/bots/{bot['id']}", json={"auto_resume": False})
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["run"] = {
        "id": "run-off",
        "status": "running",
        "started_at": now_iso(),
        "last_activity_at": now_iso(),
        "current_step": "Listing",
        "reason": "",
    }
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "check the east pier", "created_at": now_iso()},
        {"id": "a-empty", "role": "assistant", "content": "", "thinking": "planning", "live": True, "created_at": now_iso()},
    ]
    store.save_chat(doc)
    _pause_for_resume(store, bot["id"], chat["id"])
    paused = store.get_chat(bot["id"], chat["id"])
    texts = [str(item.get("content") or "") for item in paused["messages"]]
    assert "Interrupted, resuming" not in texts
    assert "a-empty" not in [item.get("id") for item in paused["messages"]]
    assert any(item.get("kind") == "resume-offer" for item in paused["messages"])
    client.close()


def test_eight_tool_job_and_a_mid_chain_message_match_the_screen(tmp_path, monkeypatch):
    """Eight tool steps, then a message. Saved rows and the done event are the same text."""
    import easyagent.llm as llm

    app, client, bot, chat = _world(tmp_path)
    store = app.state.store
    calls = {"model": 0, "tools": 0}

    async def execute(_store, request, bot_id=None):
        del bot_id
        calls["tools"] += 1
        if calls["tools"] == 8:
            from easyagent import turn as turn_mod
            from easyagent.hermes_busy import _hm_handle_running_session_message
            from easyagent.plan import enqueue

            # The tool is still the current step. A message here pauses at the
            # next safe point instead of waiting for the whole answer.
            slot = turn_mod.slot_for(_store, chat["id"])
            slot._executing_tools = True
            slot.sealing = False
            enqueue(_store, bot["id"], chat["id"], "and the kiln", client_id="c-kiln")
            _hm_handle_running_session_message(_store, bot["id"], chat["id"], "and the kiln")
        return f"listed {request.path}"

    async def stream_complete(**_kwargs):
        calls["model"] += 1
        number = calls["model"]
        if number <= 8:
            if number == 1:
                llm.note_reasoning("planning the pier")
            llm._store_native_calls({
                0: {
                    "id": f"call_{number}",
                    "name": "list_dir",
                    "arguments": json.dumps({"path": f"pier-{number}"}),
                }
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
    events = _events(body)
    done = [item for item in events if item.get("type") == "done"]
    assert done, body[-800:]
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    screen = _visible(done[-1]["chat"]["messages"])
    stored = _visible(saved["messages"])
    assert calls["tools"] == 8, calls
    assert stored == screen
    assert stored == [
        "check the east pier",
        "and the kiln",
        "The kiln is warm.",
        PAUSED_SAY_CONTINUE,
    ]
    assert "Interrupted, resuming" not in stored
    assert PAUSED_YOU_SENT not in stored
    assert saved["run"]["status"] == "paused"
    blanks = [
        item
        for item in saved["messages"]
        if item.get("role") == "assistant" and not str(item.get("content") or "").strip()
    ]
    assert blanks == []

    resumed = client.post(url, json={"content": "continue"})
    assert resumed.status_code == 200, resumed.text
    payload = resumed.json()
    after = (payload.get("chat") or payload)["messages"]
    texts = _visible(after)
    assert PAUSED_SAY_CONTINUE not in texts
    assert "Interrupted, resuming" not in texts
    assert any("Do not start over" in text for text in texts)
    assert any(text == "The kiln is warm." for text in texts)
    client.close()
