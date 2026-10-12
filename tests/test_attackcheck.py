"""Attack checks feed the classifier. They do not run a destructive command."""

from easyagent.attackcheck import (
    debug_attack,
    helper_isolation,
    mcp_injection,
    message_injection,
    routine_attack,
    web_injection,
)


def test_a_routine_cannot_run_a_wipe_or_auto_approve():
    status, detail = routine_attack()
    assert status == "PASS", detail
    assert "wipe blocked" in detail


def test_web_content_cannot_grant_a_command():
    status, detail = web_injection()
    assert status == "PASS", detail


def test_connector_output_cannot_grant_or_install():
    status, detail = mcp_injection()
    assert status == "PASS", detail


def test_a_helper_cannot_escape_its_tool_list_or_its_bot():
    status, detail = helper_isolation()
    assert status == "PASS", detail


def test_an_inbound_message_cannot_grant_or_cross_a_bot():
    status, detail = message_injection()
    assert status == "PASS", detail


def test_debug_mode_holds_an_edit_and_still_blocks_a_wipe():
    status, detail = debug_attack()
    assert status == "PASS", detail
