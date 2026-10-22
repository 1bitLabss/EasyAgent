"""A turn that would end with no words still leaves an answer."""

import json
import time

from fastapi.testclient import TestClient

from easyagent.check import force_check, review_budget, review_turn
from tests.test_check import _collect, _store
from tests.test_tools import _world

PIER = "The east pier has twelve boats."
MARK = "The pier mark is set."
KILN = "The kiln note is the one that was written."
CHILD = "CHILD-ONLY harbor note."
PAUSE = "Paused, reading your message"


def _events(body: str) -> list[dict]:
    rows = []
    for block in body.split("\n\n"):
        data = "\n".join(
            line[5:].strip() for line in block.splitlines() if line.startswith("data:")
        )
        if data:
            rows.append(json.loads(data))
    return rows


def _visible(chat: dict) -> list[tuple[str, str]]:
    rows = []
    for item in chat.get("messages") or []:
        if item.get("kind") in {"status", "pickup", "activity"}:
            continue
        if item.get("role") in {"user", "assistant"}:
            rows.append((item.get("role"), item.get("content") or ""))
    return rows


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
    assert time.monotonic() - started < 5, body[-500:]
    stored = client.get(url.replace("/messages", "")).json()
    return body, stored


def _install_answer(monkeypatch, sentence: str):
    seen = []
    stamp = {"at": 0.0}

    async def complete(**kwargs):
        seen.append(kwargs)
        messages = kwargs.get("messages") or []
        user = str(messages[-1].get("content") or "") if messages else ""
        system = str(messages[0].get("content") or "") if messages else ""
        if "Answer the user now" in user:
            stamp["at"] = time.monotonic()
            assert kwargs.get("tools") is False
            return sentence
        if "EASYAGENT_CHECK_V1" in system:
            return json.dumps(
                {"pass": False, "problems": ["The reply does not match the file."], "fix_hint": "Say what was written."}
            )
        return sentence

    monkeypatch.setattr("easyagent.llm.complete", complete)
    return seen, stamp


def _assert_saved(body: str, stored: dict, sentence: str):
    events = _events(body)
    done = next(event for event in events if event.get("type") == "done")
    assert _visible(done["chat"]) == _visible(stored)
    answers = [text for role, text in _visible(stored) if role == "assistant" and text.strip()]
    assert answers
    assert sentence in answers[-1]
    assert stored["run"]["status"] != "running"
    assert stored["messages"][-1]["content"] == done["chat"]["messages"][-1]["content"]
    assert stored["messages"][-1]["content"].strip()


def test_review_budget_is_zero_or_one():
    assert review_budget([]) == 0
    assert review_budget([{"kind": "search", "ok": True, "result": "boats at the pier"}]) == 0
    assert review_budget([{"kind": "fetch", "ok": True, "result": "a page"}]) == 0
    assert review_budget([{"kind": "files", "action": "read", "ok": True, "result": "pine"}]) == 0
    assert review_budget([{"kind": "files", "action": "write", "ok": False, "path": "kiln.py"}]) == 0
    assert review_budget([
        {"kind": "shell", "command": "mkdir kiln", "ok": True, "result": ""},
        {"kind": "memory", "action": "write", "ok": True, "result": "noted"},
    ]) == 0
    coded = review_budget([
        {"kind": "files", "action": "write", "ok": True, "path": "kiln.py", "result": "wrote"},
        {"kind": "shell", "command": "python3 -m pytest", "ok": True, "result": "passed"},
    ])
    assert coded == 1
    five = [
        {"kind": "files", "action": "write", "ok": True, "path": f"n{index}.txt", "result": "wrote"}
        for index in range(5)
    ]
    assert review_budget(five) == 1
    assert review_budget(five * 3) == 1
    assert review_budget([
        {"kind": "shell", "command": "mkdir kiln", "ok": True, "result": ""},
        {"kind": "memory", "action": "write", "ok": True, "result": "noted"},
        {"kind": "shell", "command": "touch kiln/note", "ok": True, "result": ""},
        {"kind": "shell", "command": "cp kiln/note kiln/copy", "ok": True, "result": ""},
        {"kind": "files", "action": "read", "ok": True, "result": "pine"},
    ]) == 1


def test_a_discarded_revise_still_answers(tmp_path, monkeypatch):
    """The revision returns nothing. The draft is not thrown away into silence."""
    store, _endpoint, bot = _store(tmp_path)
    grades = {"n": 0}

    async def run_turn(**kwargs):
        if grades["n"] == 0:
            yield "ledger", json.dumps(
                [{"kind": "files", "action": "write", "ok": True, "path": "kiln.py", "result": "wrote kiln.py"}]
            )
            yield "final", "The first line is FABRICATED-CLAIM."
            return
        yield "ledger", "[]"
        yield "final", ""

    async def complete(**kwargs):
        messages = kwargs.get("messages") or []
        user = str(messages[-1].get("content") or "") if messages else ""
        system = str(messages[0].get("content") or "") if messages else ""
        if "Answer the user now" in user:
            assert kwargs.get("tools") is False
            return KILN
        assert "EASYAGENT_CHECK_V1" in system
        grades["n"] += 1
        return json.dumps(
            {
                "pass": False,
                "problems": ["The reply quotes a line that was not written."],
                "fix_hint": "Say what was written.",
            }
        )

    monkeypatch.setattr("easyagent.llm.complete", complete)
    force_check(True)
    try:
        events = _collect(store, bot, run_turn, [{"role": "user", "content": "Write the kiln note."}])
    finally:
        force_check(False)
    finals = [text for kind, text in events if kind == "final"]
    assert finals == [KILN]
    assert "FABRICATED-CLAIM" not in finals[-1]
    assert [text for kind, text in events if kind == "status"] == ["Revising"]
    assert grades["n"] == 1
    assert any(kind == "delta" and text == KILN for kind, text in events)


def test_an_ask_another_bot_end_still_answers(tmp_path, monkeypatch):
    """The model only asks another bot. That reply is not the answer on screen."""

    async def child(_store, _bot_id, _request):
        return CHILD

    monkeypatch.setattr("easyagent.delegate.run_delegate", child)
    monkeypatch.setattr("easyagent.tools.TURN_STEP_CAP", 2)
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    seen, _stamp = _install_answer(monkeypatch, PIER)

    class Script:
        def __init__(self):
            self.calls = 0

        async def __call__(self, **_kwargs):
            self.calls += 1
            if self.calls > 6:
                yield PIER
                return
            yield f"```delegate\nCount the boats, pass {self.calls}.\n```"

    script = Script()
    monkeypatch.setattr("easyagent.llm.stream_complete", script)
    body, stored = _post(client, bot, chat, "How many boats are at the east pier?")
    assert script.calls == 2
    assert seen and "Answer the user now" in seen[-1]["messages"][-1]["content"]
    _assert_saved(body, stored, PIER)
    assert CHILD not in stored["messages"][-1]["content"]
    assert stored["run"]["status"] != "paused"


def test_a_step_cap_still_answers(tmp_path, monkeypatch):
    """The model never writes prose. The step cap still leaves an answer."""
    monkeypatch.setattr("easyagent.tools.TURN_STEP_CAP", 2)
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    _install_answer(monkeypatch, MARK)

    class Script:
        def __init__(self):
            self.calls = 0

        async def __call__(self, **_kwargs):
            self.calls += 1
            if self.calls > 6:
                yield MARK
                return
            yield f"```shell\necho harbor-{self.calls}\n```"

    script = Script()
    monkeypatch.setattr("easyagent.llm.stream_complete", script)
    body, stored = _post(client, bot, chat, "Mark the pier.")
    assert script.calls == 2
    _assert_saved(body, stored, MARK)
    assert "The bot repeated itself" not in stored["messages"][-1]["content"]
    assert stored["run"]["status"] == "idle"


def test_a_search_answers_after_two_lookups(tmp_path, monkeypatch):
    """A question that only searches stops at two lookups and answers, with no review."""
    monkeypatch.setenv("EASYAGENT_CHECK", "1")
    searched = []

    async def search(query):
        searched.append((query, time.monotonic()))
        return "Twelve boats are tied at the east pier."

    async def child(*_args, **_kwargs):
        raise AssertionError("another bot was asked")

    monkeypatch.setattr("easyagent.search.web_search", search)
    monkeypatch.setattr("easyagent.delegate.run_delegate", child)
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    _seen, stamp = _install_answer(monkeypatch, PIER)
    mode = {"search": True}

    class Script:
        def __init__(self):
            self.calls = 0

        async def __call__(self, **_kwargs):
            self.calls += 1
            if not mode["search"]:
                yield "The west pier is empty."
                return
            if self.calls > 6:
                yield PIER
                return
            yield f"```search\neast pier boats {self.calls}\n```"

    script = Script()
    monkeypatch.setattr("easyagent.llm.stream_complete", script)
    body, stored = _post(client, bot, chat, "How many boats are at the east pier?")
    assert len(searched) == 2
    assert stamp["at"] > 0
    assert stamp["at"] - searched[-1][1] < 3
    assert "Revising" not in body
    assert "EASYAGENT_CHECK_V1" not in json.dumps([call.get("messages") for call in _seen])
    _assert_saved(body, stored, PIER)
    assert stored["run"]["status"] == "idle"

    mode["search"] = False
    again, stored_again = _post(client, bot, chat, "What about the west pier?")
    assert PAUSE not in body
    assert PAUSE not in again
    assert "The west pier is empty." in stored_again["messages"][-1]["content"]
    assert len(searched) == 2
    assert stored_again["run"]["status"] != "running"
    _assert_saved(again, stored_again, "The west pier is empty.")
