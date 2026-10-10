"""The bot knows where its own files are, can read them, and uses the connection saved now."""

import json
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.llm import ProviderError, _fence_for_call
from easyagent.paths import display_path
from easyagent.selfinfo import app_folder_note, run_history
from easyagent.store import Store
from easyagent.tools import parse_tools


class Recorder:
    def __init__(self):
        self.calls = []
        self.reply = "ack"
        self.error = None

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.calls.append({"base_url": base_url, "model": model, "messages": messages})
        if self.error:
            raise self.error
        reply = self.reply
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else "ack"
        return reply


@pytest.fixture
def world(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    client = TestClient(create_app(tmp_path))
    endpoint = client.post(
        "/api/endpoints", json={"name": "home", "base_url": "http://localhost:8080/v1", "model": "your-model"}
    ).json()
    bot = client.post("/api/bots", json={"name": "Bot1", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return SimpleNamespace(client=client, path=tmp_path, rec=recorder, endpoint=endpoint, bot=bot, chat=chat)


def _send(world, text):
    response = world.client.post(
        f"/api/bots/{world.bot['id']}/chats/{world.chat['id']}/messages", json={"content": text}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_prompt_names_real_paths_and_the_memory_file_is_created(world):
    notes = world.path / "bots" / world.bot["id"] / "notes"
    assert not (notes / "MEMORY.md").exists()
    _send(world, "where is your memory file?")
    memory_md = notes / "MEMORY.md"
    assert memory_md.is_file()
    assert memory_md.read_text(encoding="utf-8").startswith("# Memory")
    assert (notes / "USER.md").is_file()
    system = world.rec.calls[-1]["messages"][0]["content"]
    assert "# Your own files" in system
    assert display_path(memory_md.resolve()) in system
    chat_file = world.path / "bots" / world.bot["id"] / "chats" / f"{world.chat['id']}.json"
    assert display_path(chat_file.resolve()) in system
    assert display_path((world.path / "skills").resolve()) in system
    assert "This bot's workspace, where a file goes when no folder is named:" in system
    assert "%LOCALAPPDATA%\\EasyAgent" not in system
    workspace = world.path / "bots" / world.bot["id"] / "workspace"
    assert display_path(workspace.resolve()) in system
    assert "never ask the person where your chats or memory are" in system
    assert "```history" in system


def test_an_existing_memory_file_is_not_overwritten(world):
    notes = world.path / "bots" / world.bot["id"] / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "MEMORY.md").write_text("# Memory\n\n- keep me\n", encoding="utf-8")
    _send(world, "hello")
    assert (notes / "MEMORY.md").read_text(encoding="utf-8") == "# Memory\n\n- keep me\n"


def test_app_folder_note_names_every_os():
    note = app_folder_note()
    assert "%LOCALAPPDATA%\\EasyAgent" in note
    assert "~/Library/Application Support/EasyAgent" in note
    assert "~/.local/share/EasyAgent" in note


def test_history_tool_searches_and_reads_past_chats_and_memory(world):
    _send(world, "the kiln code is BLUE-HERON-42")
    other = world.client.post(f"/api/bots/{world.bot['id']}/chats").json()
    world.client.post(
        f"/api/bots/{world.bot['id']}/chats/{other['id']}/messages", json={"content": "different topic"}
    )
    world.client.post(f"/api/bots/{world.bot['id']}/memory", json={"text": "likes PAPER-NOTES", "topic": "desk"})

    world.rec.reply = ["```history\nsearch\nBLUE-HERON\n```", "Found it."]
    found = _send(world, "what was the kiln code I told you?")
    answer = found["chat"]["messages"][-1]["content"]
    assert "Found it." in answer
    fed = "\n".join(
        item.get("content") or "" for item in world.rec.calls[-1]["messages"] if item.get("role") == "tool"
    )
    if not fed:
        fed = json.dumps(world.rec.calls[-1]["messages"])
    assert "BLUE-HERON-42" in fed

    store = Store(world.path)
    listed = run_history(store, world.bot["id"], "list")
    assert world.chat["id"] in listed and other["id"] in listed
    read = run_history(store, world.bot["id"], "read", world.chat["id"])
    assert "the kiln code is BLUE-HERON-42" in read
    memory = run_history(store, world.bot["id"], "memory")
    assert "PAPER-NOTES" in memory
    assert "MEMORY.md" in memory
    hits = run_history(store, world.bot["id"], "search", "paper-notes")
    assert "PAPER-NOTES" in hits


def test_history_is_a_native_tool_and_a_fence():
    assert _fence_for_call({"name": "history", "arguments": json.dumps({"action": "search", "query": "kiln"})}) == (
        "```history\nsearch\nkiln\n```"
    )
    assert _fence_for_call({"name": "history", "arguments": json.dumps({"action": "memory"})}) == "```history\nmemory\n```"
    found = parse_tools("```history\nread\nthis\n```")
    assert found and found[0].kind == "history" and found[0].action == "read" and found[0].path == "this"
    assert parse_tools("```history\nsearch\nthe words\n```") == []


def test_another_bot_cannot_read_this_bots_history(world):
    _send(world, "PRIVATE-LINE-77")
    other = world.client.post("/api/bots", json={"name": "Bea", "endpoint_id": world.endpoint["id"]}).json()
    store = Store(world.path)
    result = run_history(store, other["id"], "search", "PRIVATE-LINE-77")
    assert result.startswith("Nothing in your chats or memory matches")
    assert world.chat["id"] not in run_history(store, other["id"], "list")


def test_each_request_uses_the_connection_saved_now(world):
    world.rec.error = ProviderError("Could not reach http://localhost:8080/v1: timed out")
    failed = _send(world, "are you there?")
    error = failed["chat"]["messages"][-1]
    assert error["error"] is True
    assert "\u201chome\u201d at http://localhost:8080/v1" in error["content"]
    assert "Bot1 used the connection" in error["content"]
    assert world.rec.calls[-1]["base_url"] == "http://localhost:8080/v1"

    patched = world.client.patch(
        f"/api/endpoints/{world.endpoint['id']}", json={"base_url": "http://localhost:8081/v1"}
    )
    assert patched.status_code == 200, patched.text
    world.rec.error = None
    world.rec.reply = "Yes, I am here."
    ok = _send(world, "are you there now?")
    assert world.rec.calls[-1]["base_url"] == "http://localhost:8081/v1"
    assert world.rec.calls[-1]["model"] == "your-model"
    assert ok["chat"]["messages"][-1]["content"] == "Yes, I am here."
    # The old error line is still on disk, but the old address is not replayed to the model.
    sent = json.dumps(world.rec.calls[-1]["messages"][1:])
    assert "localhost:8080" not in sent
    stored = (world.path / "bots" / world.bot["id"] / "chats" / f"{world.chat['id']}.json").read_text(encoding="utf-8")
    assert "localhost:8080" in stored


def test_a_new_bot_points_at_the_connection_it_was_given(world):
    fresh = world.client.post(
        "/api/endpoints", json={"name": "spare", "base_url": "http://localhost:8081/v1"}
    ).json()
    patched = world.client.patch(f"/api/bots/{world.bot['id']}", json={"endpoint_id": fresh["id"]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["endpoint_base_url"] == "http://localhost:8081/v1"
    _send(world, "ping")
    assert world.rec.calls[-1]["base_url"] == "http://localhost:8081/v1"


def test_the_footer_numbers_are_real_and_the_old_char_budget_is_ignored(world):
    bot_file = world.path / "bots" / world.bot["id"] / "bot.json"
    record = json.loads(bot_file.read_text(encoding="utf-8"))
    record["context_chars"] = 7200
    bot_file.write_text(json.dumps(record), encoding="utf-8")
    for index in range(4):
        _send(world, f"message {index} " + ("w" * 110))
    view = world.client.get(f"/api/bots/{world.bot['id']}/chats/{world.chat['id']}").json()
    ctx = view["context"]
    assert ctx["transcript_messages"] == 8
    assert ctx["model_messages"] == 8
    assert ctx["compacted"] is False
    assert ctx["context_chars"] == ctx["transcript_chars"]
    assert ctx["max_context_tokens"] >= 24000
    bot = world.client.get(f"/api/bots/{world.bot['id']}").json()
    assert bot["context_tokens"] >= 24000
    system = world.rec.calls[-1]["messages"][0]["content"]
    assert "All of them are in the message list below, in full" in system
    assert "earlier turns have not been compacted" not in system
