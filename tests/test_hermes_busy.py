"""Hermes busy-input: steer, redirect, ordered queue, and the shutdown spool."""

from __future__ import annotations

from easyagent.hermes_busy import (
    SlotAgent,
    flush_overflow_to_file,
    flush_pending_to_file,
    recover_pending_spool,
)
from easyagent.plan import drop_queue, queued_items
from tests.test_plan_0320 import _client, _running


def test_steer_concatenates_and_a_soft_clear_keeps_it():
    agent = SlotAgent(_Bare())
    assert agent.steer("  ") is False
    assert agent.steer("hello?") is True
    assert agent.steer("you there?") is True
    assert agent._pending_steer == "hello?\nyou there?"
    assert agent.clear_interrupt() is True
    assert agent._pending_steer == "hello?\nyou there?"
    assert agent.clear_interrupt(hard_cancel=True) is True
    assert agent._pending_steer is None


def test_redirect_during_a_command_does_not_cancel(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    from easyagent import turn as turn_mod

    slot = turn_mod.slot_for(store, chat["id"])

    class Proc:
        def poll(self):
            return None

    slot.proc = Proc()
    slot.client = object()
    agent = SlotAgent(slot)
    assert agent.redirect("change the task") is True
    assert slot.cancel.is_set() is False
    assert slot.user_stop is False
    assert "change the task" in (slot._pending_steer or "")
    assert slot._pending_redirect is None
    client.close()


def test_redirect_aborts_only_the_model_request(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    from easyagent import turn as turn_mod

    slot = turn_mod.slot_for(store, chat["id"])
    slot.client = object()
    slot._model_request_active.set()
    agent = SlotAgent(slot)
    assert agent.redirect("hello?") is True
    assert slot.cancel.is_set() is True
    assert slot.redirect is True
    assert slot.user_stop is False
    assert slot.proc is None
    assert slot._pending_redirect == "hello?"
    client.close()


def test_spool_replays_two_messages_in_order(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    assert client.post(url, json={"content": "hello?"}).status_code == 200
    assert client.post(url, json={"content": "you there?"}).status_code == 200
    assert [item["content"] for item in queued_items(store, bot["id"], chat["id"])] == [
        "hello?",
        "you there?",
    ]
    assert flush_pending_to_file(store, reason="shutdown") == 1
    assert flush_overflow_to_file(store, reason="shutdown") == 1
    drop_queue(store, bot["id"], chat["id"])
    assert queued_items(store, bot["id"], chat["id"]) == []
    recovered, held = recover_pending_spool(store)
    assert held == set()
    assert recovered == 2
    assert [item["content"] for item in queued_items(store, bot["id"], chat["id"])] == [
        "hello?",
        "you there?",
    ]
    again, _held = recover_pending_spool(store)
    assert again == 0
    client.close()


class _Bare:
    """Enough of a slot for the steer buffer. No model, no command."""

    def __init__(self) -> None:
        import threading

        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()
        self._pending_redirect = None
        self._pending_redirect_lock = threading.Lock()
        self._interrupt_requested = False
        self._interrupt_message = None
        self._executing_tools = False
        self._turn_user_intervened = False
        self.proc = None
        self.client = None
        self.redirect = False
        self.user_stop = False
        self.reason = ""

        class _Flag:
            def is_set(self):
                return False

            def set(self):
                return None

        self._model_request_active = _Flag()
        self.cancel = _Flag()
