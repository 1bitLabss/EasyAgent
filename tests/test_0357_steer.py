"""0.3.57: live steering of running children.

Wave 3: while a child works, the person watching can pass it one-line notes.
The note rides the durable queue file for the child's own chat (the same
machinery the parent queue uses), the child loop folds it in before its next
step as a plain-language note, and GET /api/children shows who is working now
so the JobsPanel can list them. A steer aimed at a chat with no running child
answers honestly instead of queueing into the void.
"""

import asyncio
import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from easyagent import helpers, llm
from easyagent.app import create_app
from easyagent.delegate import (
    finish_live_child,
    list_children,
    register_live_child,
    reset_for_tests,
)
from easyagent.turn import renew_cancel

PARENT_CALL = (
    "One helper now.\n\n<tool_call>"
    '{"name": "delegate_task", "arguments": {"task": "count the boats", "tools": ["read_file"]}}'
    "</tool_call>"
)


class Tape:
    """The parent fans out one child. The child's first call sleeps so the test
    can steer mid-run; its second call must show the note was folded in."""

    def __init__(self):
        self.calls = []
        self.child_calls = 0
        self.parent_calls = 0

    async def complete(self, *, base_url, api_key, model, messages, timeout=120, tools=None, **_kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        system = messages[0].get("content") or ""
        blob = "\n".join(item.get("content") or "" for item in messages)
        if "The helper reported:" in blob:
            return "The child looked."
        if "a helper with one task" in system:
            self.child_calls += 1
            if self.child_calls == 1:
                await asyncio.sleep(0.6)
                return (
                    "<tool_call>"
                    '{"name": "read_file", "arguments": {"path": "notes.txt"}}'
                    "</tool_call>"
                )
            return "Done looking."
        return "The child looked."

    async def stream_complete(self, **kwargs):
        messages = kwargs["messages"]
        self.calls.append({"messages": messages, "tools": None})
        self.parent_calls += 1
        yield PARENT_CALL if self.parent_calls == 1 else "The child looked."

    def child_blobs(self):
        rows = []
        for call in self.calls:
            system = call["messages"][0].get("content") or ""
            if "a helper with one task" in system:
                rows.append("\n".join(str(item.get("content") or "") for item in call["messages"]))
        return rows


def _world(tmp_path, monkeypatch, tape):
    monkeypatch.setattr("easyagent.llm.complete", tape.complete)
    monkeypatch.setattr("easyagent.llm.stream_complete", tape.stream_complete)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "max_parallel": 3},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return app, client, bot, chat


def _stream_message(client: TestClient, bot, chat, content: str) -> None:
    with client.stream(
        "POST",
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": content},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        response.read()


def _wait_for_children(client: TestClient, timeout: float = 5.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = client.get("/api/children").json()
        if rows:
            return rows
        time.sleep(0.05)
    return []


def test_steering_a_running_child_reaches_its_next_step(tmp_path, monkeypatch):
    """Mid-run, the JobsPanel board shows the child, the steer endpoint queues
    the note, and the child's next model call carries it."""
    tape = Tape()
    _app, client, bot, chat = _world(tmp_path, monkeypatch, tape)

    done = {}

    def run_turn():
        try:
            _stream_message(client, bot, chat, "count the marina")
            done["ok"] = True
        except Exception as exc:  # pragma: no cover - failure detail
            done["error"] = exc

    thread = threading.Thread(target=run_turn)
    thread.start()
    try:
        rows = _wait_for_children(client)
        assert rows, "the running child never showed up on /api/children"
        row = rows[0]
        assert "count the boats" in row["task_excerpt"]
        assert row["chat_id"], "the child board row must carry the child's own chat"
        assert row["started_at"] > 0

        steer = client.post(
            f"/api/bots/{bot['id']}/chats/{row['chat_id']}/child-steer",
            json={"text": "go faster please"},
        )
        assert steer.status_code == 200
        assert steer.json() == {"answer": "The child will read it before its next step."}
    finally:
        thread.join(30)
    assert done.get("ok"), done.get("error")

    blobs = tape.child_blobs()
    assert len(blobs) >= 2, "the child never took a second step to read the note"
    assert "go faster please" not in blobs[0]
    assert "go faster please" in blobs[1]
    assert "person watching" in blobs[1]

    # The child is off the board and left no queue behind.
    assert client.get("/api/children").json() == []
    assert not list(Path(tmp_path).rglob("*.queue.json"))


def test_steer_on_a_chat_with_no_running_child_answers_honestly(tmp_path, monkeypatch):
    """A plain chat and a finished child chat both get the plain truth."""
    tape = Tape()
    app, client, bot, chat = _world(tmp_path, monkeypatch, tape)
    reset_for_tests()

    plain = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/child-steer", json={"text": "hello?"}
    )
    assert plain.status_code == 200
    assert plain.json() == {"answer": "That child is not running right now."}

    # A child chat that exists but whose child is not in flight.
    store = app.state.store
    renew_cancel()
    spec = helpers.HelperSpec(
        name="Scout", task="count the boats", tools=frozenset({"files"}), steps=2, seconds=30
    )
    child_chat = helpers._open_child_chat(store, bot["id"], chat["id"], spec)
    assert child_chat and child_chat.get("child") is True
    answer = client.post(
        f"/api/bots/{bot['id']}/chats/{child_chat['id']}/child-steer", json={"text": "hello?"}
    )
    assert answer.json() == {"answer": "That child is not running right now."}
    # Nothing was queued for a child nobody will read.
    assert not list(Path(tmp_path).rglob("*.queue.json"))


def test_steer_needs_text(tmp_path, monkeypatch):
    tape = Tape()
    _app, client, bot, chat = _world(tmp_path, monkeypatch, tape)
    blank = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/child-steer", json={"text": "   "}
    )
    assert blank.status_code == 400


def test_children_endpoint_reflects_start_and_finish(tmp_path, monkeypatch):
    tape = Tape()
    _app, client, bot, chat = _world(tmp_path, monkeypatch, tape)
    reset_for_tests()
    assert client.get("/api/children").json() == []

    register_live_child("c1", "Scout", "count the boats", bot["id"], chat["id"])
    register_live_child("c2", "Sailor", "check the tide", bot["id"], None)
    rows = client.get("/api/children").json()
    assert [row["child_id"] for row in rows] == ["c1", "c2"]
    assert rows[0]["name"] == "Scout"
    assert rows[0]["chat_id"] == chat["id"]
    assert rows[1]["chat_id"] is None
    assert rows[0]["started_at"] <= rows[1]["started_at"]

    finish_live_child("c1")
    assert [row["child_id"] for row in client.get("/api/children").json()] == ["c2"]
    finish_live_child("c2")
    assert client.get("/api/children").json() == []
    assert list_children() == []
