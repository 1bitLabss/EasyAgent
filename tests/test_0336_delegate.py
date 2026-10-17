"""A child prompt names only the tools it was given, and three children can run."""

from easyagent.delegate import MAX_CONCURRENT, isolated_messages
from easyagent.helpers import _system


def test_the_child_prompt_lists_only_the_allowed_tools():
    assert MAX_CONCURRENT == 3
    messages = isolated_messages("count notes", frozenset({"files"}), ["read_file"])
    text = messages[0]["content"]
    assert "read_file" in text
    assert "write_file" not in text
    assert "terminal" not in text
    running = _system("Child", frozenset({"files"}), ("read_file",))
    assert "read_file" in running
    assert "write_file" not in running
    assert "list_dir" not in running
