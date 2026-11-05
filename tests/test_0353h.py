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


# --- Routine delete-by-name regressions (0.3.53h) ---------------------------
#
# apply_confirmed needs spec['id'], but the routine tool only ever carried
# names and the list output never showed ids. Three legacy routines shared the
# name 'every day 8:0 AM CT' and none could be deleted. The tool now shows each
# id in list, resolves a unique name to its id for delete/pause/resume/edit,
# refuses when a name matches more than one routine, and puts the schedule
# preview on the delete card so identical names look different.

import asyncio  # noqa: E402

import pytest  # noqa: E402

from easyagent.safety import list_pending, resolve_card  # noqa: E402
from easyagent.schedule import compile_routine  # noqa: E402
from easyagent.tools import ToolError, ToolRequest, execute  # noqa: E402
from tests.test_schedule import seed  # noqa: E402


def routine(name="Morning", prompt="Brief me.", **fields):
    return compile_routine({"name": name, "prompt": prompt, "daily": "8:00 AM", **fields})


def _request(spec: dict) -> ToolRequest:
    return ToolRequest(
        kind="routine",
        action=spec.get("action", "create"),
        path=spec.get("name") or "",
        body=spec.get("prompt") or "",
        call_arguments=json.dumps(spec),
    )


def _run_with_card(store, bot_id: str, spec: dict) -> str:
    """Run the tool, approve the confirm card, return the tool's reply."""
    request = _request(spec)
    seen: dict = {}

    async def go() -> str:
        task = asyncio.create_task(execute(store, request, bot_id))
        pending = []
        for _ in range(40):
            pending = list_pending(bot_id)
            if pending:
                break
            await asyncio.sleep(0.01)
        assert pending, "the confirm card was not opened"
        seen["detail"] = pending[0].get("detail") or ""
        resolve_card(pending[0]["id"], "approve")
        return await task

    text = asyncio.run(go())
    spec["_card_detail"] = seen["detail"]
    return text


def test_delete_by_name_removes_the_routine(tmp_path):
    store, bot, _endpoint = seed(tmp_path)
    store.add_schedule(bot["id"], routine(name="Morning", prompt="Brief me."))
    text = _run_with_card(store, bot["id"], {"action": "delete", "name": "Morning"})
    assert "Trash" in text
    assert store.list_schedules(bot["id"]) == []
    assert list_pending(bot["id"]) == []


def test_two_same_names_refuse_and_the_error_lists_both_ids(tmp_path):
    store, bot, _endpoint = seed(tmp_path)
    first = routine(name="every day 8:0 AM CT", prompt="First.")
    second = routine(name="every day 8:0 AM CT", prompt="Second.")
    store.add_schedule(bot["id"], first)
    store.add_schedule(bot["id"], second)
    with pytest.raises(ToolError) as exc:
        asyncio.run(
            execute(store, _request({"action": "delete", "name": "every day 8:0 AM CT"}), bot["id"])
        )
    message = str(exc.value)
    assert "2 routines are named every day 8:0 AM CT" in message
    assert first["id"] in message
    assert second["id"] in message
    # Nothing was deleted and no card was left waiting.
    assert [row["id"] for row in store.list_schedules(bot["id"])] == [first["id"], second["id"]]
    assert list_pending(bot["id"]) == []


def test_no_routine_with_that_name_is_refused(tmp_path):
    store, bot, _endpoint = seed(tmp_path)
    store.add_schedule(bot["id"], routine(name="Morning", prompt="Brief me."))
    with pytest.raises(ToolError) as exc:
        asyncio.run(execute(store, _request({"action": "delete", "name": "Nope"}), bot["id"]))
    assert "No routine is named Nope." in str(exc.value)
    assert len(store.list_schedules(bot["id"])) == 1


def test_delete_by_id_works_when_names_are_identical(tmp_path):
    store, bot, _endpoint = seed(tmp_path)
    first = routine(name="every day 8:0 AM CT", prompt="First.")
    second = routine(name="every day 8:0 AM CT", prompt="Second.")
    store.add_schedule(bot["id"], first)
    store.add_schedule(bot["id"], second)
    spec = {"action": "delete", "id": second["id"]}
    text = _run_with_card(store, bot["id"], spec)
    assert "Trash" in text
    left = store.list_schedules(bot["id"])
    assert [row["id"] for row in left] == [first["id"]]
    # The card shows the schedule preview, not the bare 'move to Trash' line.
    assert "8:00 AM" in spec["_card_detail"]


def test_delete_by_name_card_shows_the_schedule_preview(tmp_path):
    store, bot, _endpoint = seed(tmp_path)
    store.add_schedule(bot["id"], routine(name="Morning", prompt="Brief me."))
    spec = {"action": "delete", "name": "Morning"}
    _run_with_card(store, bot["id"], spec)
    assert "8:00 AM" in spec["_card_detail"]


def test_list_output_includes_each_routine_id(tmp_path):
    store, bot, _endpoint = seed(tmp_path)
    first = routine(name="Morning", prompt="Brief me.")
    second = routine(name="Evening", prompt="Wind down.", daily="9:00 PM")
    store.add_schedule(bot["id"], first)
    store.add_schedule(bot["id"], second)
    text = asyncio.run(execute(store, _request({"action": "list"}), bot["id"]))
    assert f"[{first['id']}] Morning" in text
    assert f"[{second['id']}] Evening" in text
