"""0.3.54: the python tool keeps one live session per chat.

The audited gap: every python call ran a fresh process, so variables and
imports never carried from one call to the next. The kernel now keeps a
process per chat alive. Stop, a timeout, or a reset clears it, and the next
call says the state is gone. Under the contained sandbox the kernel stands
down and the old one-shot path runs.
"""

import pytest

from easyagent import kernel
from easyagent import turn as turn_mod
from easyagent.features import reset_routine_turn
from easyagent.store import Store
from easyagent.tools import ToolError


def fresh_turn_context() -> None:
    """Start this test the way a real turn starts. A direct kernel call binds no
    slot, so without a clear stop flag an earlier test's Stop cancels it."""
    turn_mod.renew_cancel()
    reset_routine_turn()


@pytest.fixture(autouse=True)
def _no_leftover_sessions():
    yield
    kernel.shutdown_all()


def _store(tmp_path) -> Store:
    store = Store(tmp_path)
    store.ensure()
    return store


def test_variables_survive_between_python_calls(tmp_path):
    fresh_turn_context()
    store = _store(tmp_path)
    kernel.execute(store, "import math\nx = 41 + 1")
    out = kernel.execute(store, "print(x, math.floor(2.9))")
    assert "42 2" in out


def test_two_chats_get_two_sessions(tmp_path):
    fresh_turn_context()
    store = _store(tmp_path)
    turn_mod.bind(store, "chat-a")
    kernel.execute(store, "who = 'a'")
    turn_mod.bind(store, "chat-b")
    with pytest.raises(ToolError) as boom:
        kernel.execute(store, "print(who)")
    assert "NameError" in str(boom.value)
    # The first chat still has its own state.
    turn_mod.bind(store, "chat-a")
    assert "a" in kernel.execute(store, "print(who)")


def test_reset_clears_the_session(tmp_path):
    fresh_turn_context()
    store = _store(tmp_path)
    kernel.execute(store, "x = 1")
    kernel.reset(store, "")
    with pytest.raises(ToolError) as boom:
        kernel.execute(store, "print(x)")
    assert "NameError" in str(boom.value)


def test_a_run_past_its_time_is_killed_and_says_the_state_is_gone(tmp_path):
    fresh_turn_context()
    store = _store(tmp_path)
    kernel.execute(store, "x = 7")
    with pytest.raises(ToolError) as boom:
        kernel.execute(store, "import time\ntime.sleep(30)", timeout=2)
    assert "timed out" in str(boom.value)
    out = kernel.execute(store, "print(1)")
    assert "restarted" in out
    assert "1" in out


def test_stop_clears_the_session(tmp_path):
    fresh_turn_context()
    store = _store(tmp_path)
    turn_mod.bind(store, "chat-s")
    kernel.execute(store, "x = 5")
    kernel.interrupt(store, "chat-s")
    out = kernel.execute(store, "print('back')")
    assert "restarted" in out
    assert "back" in out


def test_contained_sandbox_falls_back_to_the_one_shot_path(tmp_path, monkeypatch):
    fresh_turn_context()
    store = _store(tmp_path)
    monkeypatch.setattr(
        "easyagent.sandbox.shell_mode",
        lambda store: {
            "contained": True,
            "status": "active",
            "label": "active",
            "notice": "",
            "reason": "",
            "mechanism": "test",
        },
    )
    assert kernel.execute(store, "print(1)") is None


def test_status_label_names_python():
    from easyagent.tools import ToolRequest, status_label

    assert status_label(ToolRequest(kind="python", body="print(1)")) == "Running Python"
