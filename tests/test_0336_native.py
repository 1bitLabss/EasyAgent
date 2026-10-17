"""A native tool call is fenced through the registry, or the model hears the error."""

import json

import pytest

from easyagent.llm import _fence_for_call, native_tools
from easyagent.tools import ToolError, parse_tools


def test_native_calls_are_fenced_or_return_the_registry_error():
    names = [tool["function"]["name"] for tool in native_tools()]
    assert "skill" in names
    delegated = _fence_for_call(
        {"name": "delegate_task", "arguments": {"task": "count notes", "tools": ["read_file"]}}
    )
    child = parse_tools(delegated)[0]
    assert child.kind == "delegate"
    assert child.body == "count notes"
    assert json.loads(child.command) == ["read_file"]

    read = parse_tools(_fence_for_call({"name": "readFile", "arguments": {"path": "notes.txt"}}))[0]
    assert read.kind == "files" and read.action == "read" and read.path == "notes.txt"
    typo = parse_tools(_fence_for_call({"name": "read_flie", "arguments": {"path": "notes.txt"}}))[0]
    assert typo.action == "read" and typo.path == "notes.txt"
    listed = parse_tools(_fence_for_call({"name": "read_file", "arguments": {"path": ["notes.txt"]}}))[0]
    assert listed.path == "notes.txt"
    command = _fence_for_call({"name": "terminal", "arguments": {"command": 123}})
    assert command == "```shell\n123\n```"
    with pytest.raises(ToolError, match="Name a path"):
        parse_tools(_fence_for_call({"name": "read_file", "arguments": {}}))

    skill = parse_tools(_fence_for_call({"name": "skill", "arguments": {"action": "read", "name": "notes"}}))[0]
    assert skill.kind == "skill" and skill.action == "read" and skill.path == "notes"
