"""Compression keeps the latest request, the open tasks, and pending approvals."""

from easyagent.turnctx import fit_context


def test_a_short_tail_without_pins_is_unchanged():
    tail = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    _summary, rows, _earlier, _used = fit_context("", tail, "", 1000)
    assert rows == tail


def test_pins_survive_a_tight_budget():
    latest = "Ship the notes"
    tail = [
        {"role": "user", "content": "old " * 400},
        {"role": "assistant", "content": "x" * 4000},
        {"role": "user", "content": latest},
        {"role": "user", "content": "Open tasks:\n1. [doing] Write", "pin": "tasks"},
        {"role": "user", "content": "Pending approvals:\nread-secret: cat .env", "pin": "approvals"},
    ]
    _summary, rows, _earlier, _used = fit_context("sum", tail, "earlier " * 80, 700, latest=latest)
    blob = "\n".join(str(item.get("content") or "") for item in rows)
    assert latest in blob
    assert "Open tasks:" in blob
    assert "Pending approvals:" in blob
    assert "old " * 40 not in blob
