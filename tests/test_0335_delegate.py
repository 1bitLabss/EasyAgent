"""delegate_task runs one child with its own context and the parent's limits."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent import helpers
from easyagent import turn as turn_mod
from easyagent.app import create_app
from easyagent.delegate import (
    MAX_CONCURRENT,
    allowed_kinds,
    isolated_messages,
    reset_for_tests,
    run_delegate,
    summary_for,
)
from easyagent.delegate import _acquire
from easyagent.registry import disable, reset
from easyagent.safety import classify, guard, list_pending, reset_for_tests as reset_safety, resolve_card
from easyagent.tools import ToolError, ToolRequest, _request_from_call
from easyagent.workspace import bot_workspace
from easyagent.yolo import covers


def _client(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return app, client, bot, chat


def _say(client, bot, chat, text):
    response = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": text},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _settle(store, bot_id, request):
    task = asyncio.create_task(guard(store, request, bot_id))
    cards = []
    for _ in range(40):
        cards = list_pending(bot_id)
        if cards or task.done():
            break
        await asyncio.sleep(0.02)
    cards = list_pending(bot_id)
    if cards:
        resolve_card(cards[0]["id"], "deny")
        if not task.done():
            with pytest.raises(ToolError):
                await task
        await asyncio.sleep(0.05)
        return "ask"
    if not task.done():
        task.cancel()
        raise AssertionError("guard did not settle")
    exc = task.exception()
    if isinstance(exc, ToolError):
        return "block"
    return "allow"


def test_child_context_is_the_task_and_the_summary_is_not_the_transcript():
    parent = "PARENT-TRANSCRIPT-SECRET"
    messages = isolated_messages("count the notes", frozenset({"files"}))
    assert parent not in json.dumps(messages)
    assert messages[1]["content"] == "count the notes"
    assert len(messages) == 2
    heard = summary_for("count the notes", "two notes")
    assert heard.startswith("Task: count the notes")
    assert "two notes" in heard
    assert "system" not in heard
    assert parent not in heard


def test_allow_list_drops_tools_the_parent_cannot_use():
    assert allowed_kinds(["terminal", "read_file"]) == frozenset({"shell", "files"})
    assert allowed_kinds(["terminal"], parent_kinds=frozenset({"files"})) == frozenset()
    assert allowed_kinds(["delegate_task", "nope"]) == frozenset()
    disable("terminal")
    try:
        assert "shell" not in allowed_kinds(["terminal", "read_file"])
    finally:
        reset()
    depth = helpers._depth.set(1)
    allowed = helpers._allowed.set(frozenset({"files"}))
    try:
        assert helpers.scope_block("shell")
        assert helpers.scope_block("delegate")
        assert helpers.scope_block("routine")
        assert helpers.scope_block("files") is None
    finally:
        helpers._allowed.reset(allowed)
        helpers._depth.reset(depth)


def test_depth_and_concurrency_caps():
    reset_for_tests()
    for _ in range(MAX_CONCURRENT):
        _acquire()
    with pytest.raises(ToolError, match="Too many child agents"):
        _acquire()
    reset_for_tests()
    request = ToolRequest(kind="delegate", body="look", command="[]")
    depth = helpers._depth.set(1)
    try:
        with pytest.raises(ToolError, match="cannot delegate again"):
            asyncio.run(run_delegate(None, "bot", request))
    finally:
        helpers._depth.reset(depth)


def test_stopping_the_parent_cancels_the_child(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])

    async def scene():
        turn_mod.bind(store, chat["id"], bot_id=bot["id"])
        started = asyncio.Event()

        async def sleeper():
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(sleeper())
        from easyagent.delegate import register_child

        register_child(task)
        await started.wait()
        turn_mod.interrupt(store, chat["id"], "you pressed Stop")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    asyncio.run(scene())


def test_delegate_call_uses_only_the_named_tools(monkeypatch):
    seen = {}

    async def fake_run(store, bot_id, specs):
        seen["tools"] = set(specs[0].tools)
        seen["task"] = specs[0].task
        return [{"task": specs[0].task, "result": "child-done", "status": "done"}]

    monkeypatch.setattr(helpers, "run_helpers", fake_run)
    request = _request_from_call(
        {"name": "delegate_task", "arguments": {"task": "count notes", "tools": ["read_file"]}}
    )
    assert request.kind == "delegate"
    text = asyncio.run(run_delegate(None, "bot", request))
    assert seen["tools"] == {"files"}
    assert seen["task"] == "count notes"
    assert text.startswith("Task: count notes")
    assert "child-done" in text
    assert "arguments" not in text


def test_child_does_not_inherit_yolo_or_read_a_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "0.25")
    reset_safety()
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _say(client, bot, chat, "/yolo")
    folder = bot_workspace(store, bot["id"], create=True)
    secret = folder / ".env"
    secret.write_text("SECRETENV=1\n", encoding="utf-8")
    notes = folder / "notes.txt"
    notes.write_text("keep", encoding="utf-8")
    turn_mod.bind(store, chat["id"], bot_id=bot["id"])
    outside = ToolRequest(kind="files", action="write", path="/etc/ea-delegate-escape.txt", body="nope")
    escaped = classify(store, outside, bot["id"])
    assert escaped.tier in {"ask", "block"}
    removal = ToolRequest(kind="shell", command="rm notes.txt")
    parent_verdict = classify(store, removal, bot["id"])
    assert covers(store, bot["id"], removal, parent_verdict) is True
    depth = helpers._depth.set(1)
    try:
        outcome = asyncio.run(_settle(
            store,
            bot["id"],
            ToolRequest(kind="files", action="read", path=str(secret)),
        ))
        write_outcome = asyncio.run(_settle(store, bot["id"], outside))
        covered = covers(store, bot["id"], removal, parent_verdict)
    finally:
        helpers._depth.reset(depth)
    assert outcome in {"ask", "block"}
    assert write_outcome in {"ask", "block"}
    assert secret.read_text(encoding="utf-8") == "SECRETENV=1\n"
    assert notes.read_text(encoding="utf-8") == "keep"
    assert not Path("/etc/ea-delegate-escape.txt").exists()
    assert covered is False
