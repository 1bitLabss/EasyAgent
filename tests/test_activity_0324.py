"""A tool step keeps one id from running through done or failed."""

from easyagent.tools import activity_for_status, clear_activity, close_tool_step, open_tool_step


def test_one_tool_call_keeps_one_step_id():
    clear_activity()
    open_tool_step("Reading index.html")
    running = activity_for_status("Reading index.html")
    assert activity_for_status("Thinking") is None
    close_tool_step("done")
    done = activity_for_status("Reading index.html")
    assert running[1] == "running"
    assert done[0] == running[0]
    assert done[1] == "done"
    open_tool_step("Reading index.html")
    again = activity_for_status("Reading index.html")
    assert again[0] != running[0]
    assert again[1] == "running"
    close_tool_step("failed")
    failed = activity_for_status("Reading index.html")
    assert failed == (again[0], "failed")
    clear_activity()
