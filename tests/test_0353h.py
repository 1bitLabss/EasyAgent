"""Helper results are folded into a parent reply in the parent's own words.

The audited gap: a reply that launches a helper saved its answer before the
helper finished, so the person saw a reply that ignored what the helper found.
The ask-bot path already fixes this with one synthesis call
(_answer_in_parent_words). The helper path now mirrors it: after the raw helper
row lands, one no-tools call restates the finding in the parent's words. The
raw row stays as the audit trail. A failed synthesis call keeps the raw row.
"""

import json

from fastapi.testclient import TestClient

from easyagent import helpers, llm
from easyagent.app import create_app

MARKER = "MARKER-7731"
FENCE = (
    "Checking with a helper now.\n\n"
    "```helper\n"
    "name: Scout\n"
    "count the boats at the east pier\n"
    "```"
)


class Tape:
    """Scripted llm.complete: parent reply, helper result, then synthesis."""

    def __init__(self, synthesis="The east pier has twelve boats.", fail_synthesis=False):
        self.calls = []
        self.synthesis = synthesis
        self.fail_synthesis = fail_synthesis

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120, tools=None, **_kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        system = messages[0].get("content") or ""
        blob = "\n".join(item.get("content") or "" for item in messages)
        if "The helper reported:" in blob:
            if self.fail_synthesis:
                raise llm.ProviderError("the endpoint went away")
            return self.synthesis
        if "a helper with one task" in system:
            return f"Twelve boats are tied at the east pier. {MARKER}"
        return FENCE

    def synthesis_calls(self):
        return [c for c in self.calls if c["tools"] is False]


def _world(tmp_path, monkeypatch, tape):
    monkeypatch.setattr("easyagent.llm.complete", tape)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat


def test_a_helper_result_is_folded_into_a_parent_reply(tmp_path, monkeypatch):
    tape = Tape()
    client, bot, chat = _world(tmp_path, monkeypatch, tape)
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "How many boats are at the east pier?"},
    )
    assert sent.status_code == 200, sent.text
    messages = sent.json()["chat"]["messages"]
    contents = [item.get("content") or "" for item in messages]
    assert any("Checking with a helper now." in item for item in contents)
    # The raw helper row is still stored, marker and all, as the audit trail.
    helper_rows = [
        item
        for item in messages
        if item.get("subagent") and item.get("speaker_name") == "Helper"
    ]
    assert helper_rows and MARKER in helper_rows[-1]["content"]
    # The follow-up parent reply is the last message, in the parent's words.
    answer = messages[-1]
    assert answer.get("subagent") is not True
    assert answer["role"] == "assistant"
    assert answer["content"] == "The east pier has twelve boats."
    # Exactly one extra no-tools call, and it was fed the helper's finding.
    followups = tape.synthesis_calls()
    assert len(followups) == 1
    fed = "\n".join(item.get("content") or "" for item in followups[0]["messages"])
    assert MARKER in fed
    assert "The helper reported:" in fed
    helper_at = next(
        index
        for index, call in enumerate(tape.calls)
        if "a helper with one task" in call["messages"][0]["content"]
    )
    follow_at = next(index for index, call in enumerate(tape.calls) if call["tools"] is False)
    assert follow_at > helper_at
    # The stored chat matches the response: synthesis landed after the raw row.
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()["messages"]
    assert stored[-1]["content"] == "The east pier has twelve boats."
    assert MARKER in json.dumps(stored)
    client.close()


def test_a_failed_synthesis_call_keeps_the_raw_helper_row(tmp_path, monkeypatch):
    tape = Tape(fail_synthesis=True)
    client, bot, chat = _world(tmp_path, monkeypatch, tape)
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "How many boats are at the east pier?"},
    )
    assert sent.status_code == 200, sent.text
    messages = sent.json()["chat"]["messages"]
    assert tape.synthesis_calls(), "the synthesis call was attempted"
    # Nothing was appended on top; the raw row is the last word. Degrade, never lose.
    assert messages[-1].get("speaker_name") == "Helper"
    assert MARKER in messages[-1]["content"]
    client.close()


def test_all_helpers_errored_skips_the_synthesis_call(tmp_path, monkeypatch):
    tape = Tape()
    client, bot, chat = _world(tmp_path, monkeypatch, tape)

    async def fake_run(store, bot_id, specs):
        return [
            {"id": "r1", "name": spec.name, "task": spec.task, "status": "error", "result": "boom", "steps": 0}
            for spec in specs
        ]

    monkeypatch.setattr(helpers, "run_helpers", fake_run)
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "How many boats are at the east pier?"},
    )
    assert sent.status_code == 200, sent.text
    messages = sent.json()["chat"]["messages"]
    assert not tape.synthesis_calls()
    assert messages[-1].get("speaker_name") == "Helper"
    assert messages[-1].get("error") is True
    assert "boom" in messages[-1]["content"]
    client.close()
