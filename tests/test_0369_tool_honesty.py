"""Tool-failure honesty: one failed call is not a blockage. No live endpoint."""

import asyncio

from easyagent.honesty import run_scripted
from tests.test_tools import _world


def _turn(*args, **kwargs):
    return asyncio.run(run_scripted(*args, **kwargs))


def _fail(_call):
    return "boom\nexit code 1"


def _flipped(n):
    """Fails the first call, succeeds after."""
    state = {"n": 0}

    def run(_call):
        state["n"] += 1
        return "boom\nexit code 1" if state["n"] == 1 else "fine\nexit code 0"

    return run


def _prompt(messages):
    return "\n".join(str(item.get("content") or "") for item in messages)


def test_one_failure_note_pushes_a_changed_call_not_a_blockage(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-honesty-a-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    missing = folder / "missing"
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        f"```files\nlist\n{missing}\n```",
        "The first list failed. I will try a changed call.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "List the folder."},
    )
    assert sent.status_code == 200, sent.text
    prompt = _prompt(recorder.seen[-1])
    assert "The failure is not the reply." in prompt
    assert "do not tell the user it is blocked or unavailable" in prompt
    assert "or write the answer" not in prompt


def test_two_identical_failures_warn_in_the_tool_result():
    result = _turn(
        [
            'TOOL shell command="pytest -q"',
            'TOOL shell command="pytest -q"',
            "I changed approach.",
        ],
        "run the tests",
        _fail,
    )
    assert "[Tool loop warning: repeated_exact_failure; count=2;" in result["outputs"][1]
    assert "[Tool loop warning" not in result["outputs"][0]


def test_two_identical_failures_warn_in_the_live_tool_message(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-honesty-b-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    missing = folder / "missing.txt"
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    fence = f"```files\nread\n{missing}\n```"
    recorder.reply = [fence, fence, "I changed approach."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "List the folder."},
    )
    assert sent.status_code == 200, sent.text
    prompt = _prompt(recorder.seen[-1])
    assert "[Tool loop warning: repeated_exact_failure; count=2;" in prompt


def test_a_blockage_claim_with_no_failed_call_is_held():
    result = _turn(["The site is blocked."], "check the site", _fail)
    assert "One failed call is not a blockage." in result["text"]


def test_a_blockage_claim_after_a_later_success_is_held():
    result = _turn(
        [
            'TOOL shell command="curl https://site"',
            'TOOL shell command="curl https://site"',
            "The site is unavailable.",
        ],
        "check the site",
        _flipped(1),
    )
    assert "One failed call is not a blockage." in result["text"]


def test_a_blockage_after_one_unretried_failure_is_allowed():
    result = _turn(
        ['TOOL shell command="curl https://site"', "The site is blocked."],
        "check the site",
        _fail,
    )
    assert "One failed call is not a blockage." not in result["text"]


def test_a_plain_reply_never_draws_the_blockage_note():
    result = _turn(["All good."], "check the site", _fail)
    assert "One failed call is not a blockage." not in result["text"]
    negated = _turn(["The site is not blocked."], "check the site", _fail)
    assert "One failed call is not a blockage." not in negated["text"]
