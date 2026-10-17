"""The native tool registry: name, schema, description, disable, coerce, head and tail."""

import pytest

from easyagent.llm import native_tools
from easyagent.registry import (
    coerce_tool_args,
    disable,
    enable,
    entries,
    reset,
)
from easyagent.tools import ToolError, ToolRequest, _clip, _execute, _request_from_call


def test_every_tool_has_name_schema_and_description():
    found = entries()
    assert found
    names = []
    for item in found:
        assert item["name"]
        assert item["description"]
        schema = item["schema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        names.append(item["name"])
    assert names == [tool["function"]["name"] for tool in native_tools()]


def test_unknown_tool_is_rejected_and_a_misspelling_is_repaired():
    with pytest.raises(ToolError, match="Unknown tool. Valid tools:"):
        _request_from_call({"name": "not_a_real_tool_zzz", "arguments": {}})
    repaired = _request_from_call({"name": "Read_File", "arguments": {"path": "notes.txt", "limit": "20"}})
    assert repaired.kind == "files"
    assert repaired.action == "read"
    assert repaired.limit == 20
    assert isinstance(repaired.limit, int)


def test_string_limit_and_boolean_follow_the_schema():
    coerced = coerce_tool_args("read_file", {"path": "notes.txt", "limit": "20", "offset": "3"})
    assert coerced["limit"] == 20
    assert coerced["offset"] == 3
    assert coerce_tool_args("read_file", {"path": "notes.txt", "limit": "inf"})["limit"] == "inf"
    assert coerce_tool_args("read_file", {"path": "notes.txt", "limit": "20.5"})["limit"] == "20.5"
    patched = _request_from_call(
        {
            "name": "patch",
            "arguments": {
                "path": "notes.txt",
                "old_string": "a",
                "new_string": "b",
                "replace_all": "true",
            },
        }
    )
    assert patched.replace_all is True
    choices = coerce_tool_args("question", {"question": "Pick", "choices": '["yes", "no"]'})
    assert choices["choices"] == ["yes", "no"]


def test_disabling_a_tool_removes_it_from_the_prompt_and_blocks_the_call():
    disable("terminal")
    try:
        listed = [tool["function"]["name"] for tool in native_tools()]
        assert "terminal" not in listed
        assert "read_file" in listed
        with pytest.raises(ToolError, match="terminal is disabled"):
            _request_from_call({"name": "shell", "arguments": {"command": "echo hi"}})
        with pytest.raises(ToolError, match="terminal is disabled"):
            _execute(None, ToolRequest(kind="shell", command="echo hi"))
    finally:
        reset()
    assert "terminal" in [tool["function"]["name"] for tool in native_tools()]
    enable("read_file")
    disable("read_file")
    try:
        assert "read_file" not in [tool["function"]["name"] for tool in native_tools()]
        with pytest.raises(ToolError, match="read_file is disabled"):
            _execute(None, ToolRequest(kind="files", action="read", path="notes.txt"))
    finally:
        reset()


def test_long_output_keeps_head_and_tail():
    body = "START" + ("x" * 20000) + "END"
    trimmed = _clip(body)
    assert trimmed.startswith("START")
    assert trimmed.endswith("END")
    assert "OUTPUT TRUNCATED" in trimmed
    assert "[output truncated]" not in trimmed
    short = _clip("hello")
    assert short == "hello"
