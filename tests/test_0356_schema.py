"""0.3.56: schema-validated child output for delegate_task.

The parent can hand a child a JSON schema with its task. The child's final
answer must then be JSON matching it. One bounded correction retry with a
plain-language note, then an honest fallback: the raw text plus a marker
line saying it did not match. No schema set is the old path, unchanged.
"""

import asyncio
import json

from easyagent import helpers, llm
from easyagent import turn as turn_mod
from easyagent.childschema import validate
from easyagent.tools import ToolRequest, _request_from_call, execute, parse_tools
from tests.test_schedule import seed

SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}, "note": {"type": "string"}},
    "required": ["count"],
}


def fresh_turn_context() -> None:
    """Start the way a real turn starts (see tests/test_0353h.py). A direct
    call binds no slot, so without this it inherits whatever an earlier test
    left in this thread's context (a stopped turn's flag cancels by default)."""
    turn_mod.renew_cancel()


class Tape:
    """Scripted child answers, one per model call. The last answer repeats."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120, tools=None, **_kwargs):
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.answers) - 1)
        return self.answers[index]


def _run_one(tmp_path, monkeypatch, answers, schema=SCHEMA):
    store, bot, endpoint = seed(tmp_path)
    fresh_turn_context()
    tape = Tape(answers)
    monkeypatch.setattr(llm, "complete", tape)
    spec = helpers.HelperSpec(
        name="Child",
        task="count the boats",
        tools=frozenset({"files"}),
        steps=4,
        seconds=60,
        schema=schema,
    )
    row = asyncio.run(helpers._run_one(store, bot, endpoint, spec, yield_to_chats=False))
    return row, tape


# --- childschema.validate ----------------------------------------------------


def test_valid_json_passes_and_comes_back_clean():
    ok, parsed = validate(SCHEMA, '{"count": 3, "note": "three at the pier"}')
    assert ok
    assert json.loads(parsed) == {"count": 3, "note": "three at the pier"}


def test_json_wrapped_in_a_code_fence_still_counts():
    ok, parsed = validate(SCHEMA, "```json\n{\"count\": 2}\n```")
    assert ok
    assert json.loads(parsed) == {"count": 2}


def test_missing_required_field_names_the_field():
    ok, error = validate(SCHEMA, '{"note": "no count here"}')
    assert not ok
    assert "count" in error


def test_wrong_type_is_named_in_plain_words():
    ok, error = validate(SCHEMA, '{"count": "many"}')
    assert not ok
    assert "whole number" in error


def test_an_answer_that_is_not_json_is_named_as_such():
    ok, error = validate(SCHEMA, "I think there are three boats.")
    assert not ok
    assert "JSON" in error


def test_array_of_string_items_are_checked():
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
        "required": ["tags"],
    }
    assert validate(schema, '{"tags": ["a", "b"]}')[0]
    ok, error = validate(schema, '{"tags": [1]}')
    assert not ok
    assert "tags" in error


def test_extra_fields_are_allowed_unless_the_schema_forbids_them():
    assert validate(SCHEMA, '{"count": 1, "extra": "x"}')[0]
    strict = dict(SCHEMA, additionalProperties=False)
    ok, error = validate(strict, '{"count": 1, "extra": "x"}')
    assert not ok
    assert "extra" in error


# --- the child loop: one bounded correction, then an honest fallback ---------


def test_a_valid_first_answer_passes_straight_through(tmp_path, monkeypatch):
    row, tape = _run_one(tmp_path, monkeypatch, ['{"count": 3, "note": "three at the pier"}'])
    assert row["status"] == "done"
    assert json.loads(row["result"]) == {"count": 3, "note": "three at the pier"}
    assert len(tape.calls) == 1


def test_an_invalid_answer_gets_exactly_one_correction_try(tmp_path, monkeypatch):
    row, tape = _run_one(tmp_path, monkeypatch, ['{"note": "three boats"}', '{"count": 3}'])
    assert row["status"] == "done"
    assert json.loads(row["result"]) == {"count": 3}
    assert len(tape.calls) == 2
    # The retry note is a user-role message naming the problem.
    retry = tape.calls[1]
    assert retry[-1]["role"] == "user"
    assert "did not match the schema" in retry[-1]["content"]
    assert "count" in retry[-1]["content"]
    # The child saw its own bad answer before being asked to fix it.
    assert retry[-2]["role"] == "assistant"
    assert '"note": "three boats"' in retry[-2]["content"]


def test_two_invalid_answers_return_the_raw_text_with_a_marker(tmp_path, monkeypatch):
    row, tape = _run_one(tmp_path, monkeypatch, ["nope", "still nope"])
    assert len(tape.calls) == 2
    assert row["status"] == "done"
    assert row["result"].startswith("still nope")
    assert "The answer did not match the requested schema:" in row["result"]


def test_no_schema_leaves_the_child_path_unchanged(tmp_path, monkeypatch):
    row, tape = _run_one(tmp_path, monkeypatch, ["Three boats at the pier."], schema=None)
    assert row["status"] == "done"
    assert row["result"] == "Three boats at the pier."
    assert len(tape.calls) == 1
    # The child was never told about JSON.
    assert "Answer with only JSON" not in tape.calls[0][0]["content"]


def test_a_schema_is_written_into_the_child_system_prompt(tmp_path, monkeypatch):
    _, tape = _run_one(tmp_path, monkeypatch, ['{"count": 1}'])
    assert "Answer with only JSON that matches this schema" in tape.calls[0][0]["content"]


# --- the tool path: parse, thread, run ----------------------------------------


def test_request_from_call_carries_the_schema():
    request = _request_from_call(
        {"name": "delegate_task", "arguments": {"task": "count", "schema": SCHEMA}}
    )
    assert json.loads(request.schema_json) == SCHEMA
    plain = _request_from_call({"name": "delegate_task", "arguments": {"task": "count"}})
    assert plain.schema_json == ""


def test_the_delegate_fence_carries_the_schema_both_ways():
    fence = llm.calls_to_fence(
        [{"name": "delegate_task", "arguments": {"task": "count the boats", "schema": SCHEMA}}]
    )
    assert "schema:" in fence
    requests = parse_tools(fence)
    assert len(requests) == 1
    assert requests[0].body == "count the boats"
    assert json.loads(requests[0].schema_json) == SCHEMA


def test_a_helper_fence_can_carry_a_schema_line():
    specs = helpers.parse_helpers(
        "```helper\nname: Scout\nschema: {\"type\": \"object\"}\n---\ncount the boats\n```"
    )
    assert specs and specs[0].schema == {"type": "object"}
    plain = helpers.parse_helpers("```helper\nname: Scout\n---\ncount the boats\n```")
    assert plain and plain[0].schema is None


def test_execute_runs_the_child_with_the_schema(tmp_path, monkeypatch):
    store, bot, _endpoint = seed(tmp_path)
    fresh_turn_context()
    tape = Tape(['{"count": 12}'])
    monkeypatch.setattr(llm, "complete", tape)
    request = ToolRequest(
        kind="delegate",
        body="count the boats",
        command='["read_file"]',
        schema_json=json.dumps(SCHEMA),
    )
    text = asyncio.run(execute(store, request, bot["id"]))
    assert '"count": 12' in text
    assert len(tape.calls) == 1
