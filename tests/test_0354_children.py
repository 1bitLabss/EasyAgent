"""0.3.54: a parallel fan-out shows each child live in the chat stream.

The audited gaps: a three-child fan-out showed one generic "Running a child
agent" step; a helper batch folded into one parent row where one failure
painted everything red; the fourth concurrent child was refused outright and
lost its work; and the helpers parallel gather path (endpoint max_parallel>1)
had no test at all — every existing test monkeypatched run_helpers away.

Now: child_status SSE events per child (start, finish, failure), one activity
step per delegate call, per-child result rows, and a bounded queue instead of
a refusal.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from easyagent import llm
from easyagent.app import create_app
from easyagent.delegate import (
    MAX_CONCURRENT,
    _acquire,
    _release,
    inflight,
    reset_for_tests,
)
from easyagent.tools import ToolError

FENCES = (
    "Sending three helpers now.\n\n"
    "```helper\nname: Scout\ncount the boats\n```\n\n"
    "```helper\nname: Sailor\ncheck the tide\n```\n\n"
    "```helper\nname: Skipper\nlook at the fuel\n```"
)

DELEGATE_CALLS = (
    "Fanning out now.\n\n"
    "<tool_call>"
    '{"name": "delegate_task", "arguments": {"task": "count the boats", "tools": ["read_file"]}}\n'
    '{"name": "delegate_task", "arguments": {"task": "check the tide", "tools": ["read_file"]}}'
    "</tool_call>"
)


class Tape:
    """Scripted model for a three-way fan-out: Scout and Sailor finish, Skipper's
    endpoint fails. The chat stream reads the parent reply through
    stream_complete; helpers and the synthesis call use complete. No
    monkeypatching of run_helpers — the real gather path runs."""

    def __init__(self, reply=FENCES, final="Both children reported."):
        self.reply = reply
        self.final = final
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.parent_calls = 0

    async def complete(self, *, base_url, api_key, model, messages, timeout=120, tools=None, **_kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        system = messages[0].get("content") or ""
        blob = "\n".join(item.get("content") or "" for item in messages)
        if "The helper reported:" in blob:
            return "All three looked. The boats were counted."
        if "a helper with one task" in system:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.15)
                task = messages[1].get("content") or ""
                if "fuel" in task:
                    raise llm.ProviderError("the endpoint went away")
                return f"Done: {' '.join(task.split())}-MARKER"
            finally:
                self.active -= 1
        self.parent_calls += 1
        if self.parent_calls > 1 and self.reply is DELEGATE_CALLS:
            return self.final
        return self.reply

    async def stream_complete(self, **kwargs):
        messages = kwargs["messages"]
        self.calls.append({"messages": messages, "tools": None})
        self.parent_calls += 1
        text = self.final if self.parent_calls > 1 else self.reply
        yield text

    def synthesis_calls(self):
        return [c for c in self.calls if c["tools"] is False]


def _world(tmp_path, monkeypatch, tape, max_parallel=3):
    monkeypatch.setattr("easyagent.llm.complete", tape.complete)
    monkeypatch.setattr("easyagent.llm.stream_complete", tape.stream_complete)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "max_parallel": max_parallel},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat


def _events(body: str) -> list[dict]:
    rows = []
    for block in body.split("\n\n"):
        data = "\n".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
        if data:
            rows.append(json.loads(data))
    return rows


def _stream_message(client: TestClient, bot, chat, content: str) -> list[dict]:
    with client.stream(
        "POST",
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": content},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    return _events(body)


def test_a_three_way_fan_out_reports_each_child_live(tmp_path, monkeypatch):
    """The real gather path (max_parallel=3, three specs, no run_helpers stub):
    every child announces itself on the stream, running first, then its own
    end state, and a failure is only that child's failure."""
    tape = Tape()
    client, bot, chat = _world(tmp_path, monkeypatch, tape)
    events = _stream_message(client, bot, chat, "look over the marina")
    client.close()

    # The gather path really ran: all three children were in flight at once.
    assert tape.max_active == 3

    children = [item for item in events if item.get("type") == "child_status"]
    by_name = {}
    for item in children:
        by_name.setdefault(item["name"], []).append(item)
    assert set(by_name) == {"Scout", "Sailor", "Skipper"}

    done_at = next(i for i, item in enumerate(events) if item.get("type") == "done")
    for name, items in by_name.items():
        states = [item["state"] for item in items]
        assert states[0] == "running", f"{name} announced itself before finishing"
        assert states[-1] in {"done", "error", "stopped"}
        # Running came before the end state, and both ride the same child_id.
        assert len({item["child_id"] for item in items}) == 1
        assert all(item["task_excerpt"] for item in items)
        # Every event carries the child's own transcript chat, running first.
        assert all(item["child_chat_id"] for item in items)
        assert len({item["child_chat_id"] for item in items}) == 1
        assert all(item["elapsed"] >= 0 for item in items)
        first = next(i for i, item in enumerate(events) if item is items[0])
        assert first < done_at

    finished = {item["name"]: item["state"] for item in children if item["state"] != "running"}
    assert finished == {"Scout": "done", "Sailor": "done", "Skipper": "error"}
    assert any(item.get("type") == "done" for item in events)


def test_partial_failure_leaves_per_child_rows(tmp_path, monkeypatch):
    """Two children finish and one fails: three result rows, each under its
    own name, and only the failed one is flagged. The synthesis call still
    runs over all of them."""
    tape = Tape()
    client, bot, chat = _world(tmp_path, monkeypatch, tape)
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "look over the marina"},
    )
    assert sent.status_code == 200, sent.text
    messages = sent.json()["chat"]["messages"]
    client.close()

    rows = [item for item in messages if item.get("subagent")]
    speakers = [item.get("speaker_name") for item in rows]
    assert speakers == ["Scout", "Sailor", "Skipper"]
    by_speaker = {item["speaker_name"]: item for item in rows}
    assert by_speaker["Scout"].get("error") is not True
    assert by_speaker["Sailor"].get("error") is not True
    assert by_speaker["Skipper"].get("error") is True
    assert "count the boats-MARKER" in by_speaker["Scout"]["content"]
    assert "check the tide-MARKER" in by_speaker["Sailor"]["content"]

    # Not all errored, so the synthesis call still folds the findings into
    # the parent's own words, and it was fed every child's result.
    followups = tape.synthesis_calls()
    assert len(followups) == 1
    fed = "\n".join(item.get("content") or "" for item in followups[0]["messages"])
    assert "count the boats-MARKER" in fed
    assert "check the tide-MARKER" in fed


def test_delegate_task_children_emit_child_status(tmp_path, monkeypatch):
    """delegate_task calls share the same live reporting: two children in one
    batch each get their own activity step and their own child_status pair."""
    tape = Tape(reply=DELEGATE_CALLS)
    client, bot, chat = _world(tmp_path, monkeypatch, tape)
    events = _stream_message(client, bot, chat, "count and check, both at once")
    client.close()

    children = [item for item in events if item.get("type") == "child_status"]
    running = [item for item in children if item["state"] == "running"]
    finished = [item for item in children if item["state"] != "running"]
    assert len(running) == 2 and len(finished) == 2
    assert {item["name"] for item in children} == {"Child"}
    assert {item["task_excerpt"] for item in running} == {"count the boats", "check the tide"}
    assert all(item["state"] == "done" for item in finished)
    assert all(item["child_chat_id"] for item in children)

    # One activity step per child call, not one shared batch step.
    steps = {
        item.get("step_id")
        for item in events
        if item.get("type") == "status" and item.get("text") == "Running a child agent"
    }
    assert None not in steps
    assert len(steps) == 2


def test_a_fourth_child_queues_instead_of_being_refused():
    """The cap still holds, but a fourth call waits its turn instead of
    losing its work. It only refuses after the wait budget runs out."""
    reset_for_tests()

    async def scene():
        for _ in range(MAX_CONCURRENT):
            await _acquire("chat")
        waiting = asyncio.create_task(_acquire("chat", timeout=5))
        await asyncio.sleep(0.05)
        assert not waiting.done(), "the fourth child is queued, not refused"
        assert inflight("chat") == MAX_CONCURRENT
        _release("chat")
        await waiting
        assert inflight("chat") == MAX_CONCURRENT
        with pytest.raises(ToolError, match="Too many child agents"):
            await _acquire("chat", timeout=0.1)

    asyncio.run(scene())
    reset_for_tests()
    assert inflight() == 0
