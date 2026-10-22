"""A finish after a visible answer rejects once, then the turn ends."""

import json
import time

from fastapi.testclient import TestClient

from tests.test_tools import _world

CHILD = "The harbor ledger lists twelve boats at the east pier this week."
ANSWER = CHILD + " That is the whole count."


def _events(body: str) -> list[dict]:
    rows = []
    for block in body.split("\n\n"):
        data = "\n".join(
            line[5:].strip() for line in block.splitlines() if line.startswith("data:")
        )
        if data:
            rows.append(json.loads(data))
    return rows


class _Script:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.answered_at = 0.0

    async def __call__(self, **_kwargs):
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else None
        if reply is None:
            await _sleep()
            yield "still going"
            return
        for piece in reply:
            if piece == "ANSWER":
                self.answered_at = time.monotonic()
                yield ANSWER
            else:
                yield piece


async def _sleep():
    import asyncio

    await asyncio.sleep(30)


def _post(client: TestClient, bot, chat, content: str) -> tuple[str, dict]:
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    started = time.monotonic()
    with client.stream(
        "POST",
        url,
        json={"content": content},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    elapsed = time.monotonic() - started
    assert elapsed < 2, body[-500:]
    return body, client.get(url.replace("/messages", "")).json()


def test_a_visible_answer_ends_after_one_finish_rejection(tmp_path, monkeypatch, capsys):
    """Ask another bot, write the answer, then finish. The gate rejects once and the turn ends."""

    async def child(_store, _bot_id, _request):
        return CHILD

    monkeypatch.setattr("easyagent.delegate.run_delegate", child)
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    script = _Script([
        ["```delegate\nCount the boats in the harbor ledger.\n```"],
        ["ANSWER", "\n```finish\nproven\n```"],
    ])
    monkeypatch.setattr("easyagent.llm.stream_complete", script)
    body, stored = _post(client, bot, chat, "What does the harbor ledger say?")
    assert script.calls == 2
    assert script.answered_at and time.monotonic() - script.answered_at < 2
    assert capsys.readouterr().out.count(
        "a finish was rejected once because the answer has to be in the model's own words"
    ) == 1
    answer = stored["messages"][-1]["content"]
    assert "twelve boats" in answer
    assert stored["messages"][-1].get("error") is not True
    assert stored["run"]["status"] != "running"
    assert not any(item.get("live") for item in stored["messages"])
    events = _events(body)
    done_at = next(index for index, event in enumerate(events) if event.get("type") == "done")
    assert not [event for event in events[done_at + 1:] if event.get("type") == "status"]
    assert events[done_at]["chat"]["run"]["status"] != "running"
    assert "twelve boats" in events[done_at]["chat"]["messages"][-1]["content"]


def test_an_answer_without_finish_still_ends_the_turn(tmp_path, monkeypatch):
    """The model writes the answer and never calls finish. The turn still ends."""

    async def child(_store, _bot_id, _request):
        return CHILD

    monkeypatch.setattr("easyagent.delegate.run_delegate", child)
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    script = _Script([
        ["```delegate\nCount the boats in the harbor ledger.\n```"],
        ["The east pier is quiet and the count is already in."],
    ])
    monkeypatch.setattr("easyagent.llm.stream_complete", script)
    body, stored = _post(client, bot, chat, "What does the harbor ledger say?")
    assert script.calls == 2
    answer = stored["messages"][-1]["content"]
    assert "east pier is quiet" in answer
    assert "still going" not in answer
    assert stored["run"]["status"] != "running"
    assert not any(item.get("live") for item in stored["messages"])
    events = _events(body)
    done_at = next(index for index, event in enumerate(events) if event.get("type") == "done")
    assert not [event for event in events[done_at + 1:] if event.get("type") == "status"]


def test_silence_after_a_visible_answer_ends_the_turn(tmp_path, monkeypatch):
    """No finish, and the model call stays open. Twenty seconds of silence is enough to end it."""
    import easyagent.tools as tools

    monkeypatch.setattr(tools, "ANSWER_IDLE_SECONDS", 0.3)
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)

    class Hang:
        def __init__(self):
            self.calls = 0
            self.answered_at = 0.0

        async def __call__(self, **_kwargs):
            self.calls += 1
            self.answered_at = time.monotonic()
            yield "The east pier is quiet and the count is already in."
            await _sleep()
            yield "still going"

    hang = Hang()
    monkeypatch.setattr("easyagent.llm.stream_complete", hang)
    body, stored = _post(client, bot, chat, "What does the harbor ledger say?")
    assert hang.calls == 1
    assert time.monotonic() - hang.answered_at < 2
    answer = stored["messages"][-1]["content"]
    assert "east pier is quiet" in answer
    assert "still going" not in answer
    assert stored["run"]["status"] != "running"
    events = _events(body)
    assert any(event.get("type") == "done" for event in events)
