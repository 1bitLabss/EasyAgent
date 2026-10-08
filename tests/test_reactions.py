"""One emoji on a message. The same emoji again clears it. The text stays."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from easyagent.app import create_app


class Recorder:
    def __init__(self, reply):
        self.reply = reply

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        return self.reply


def test_reaction_shows_then_the_same_emoji_removes_it(tmp_path, monkeypatch):
    monkeypatch.setattr("easyagent.llm.complete", Recorder("ack"))
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "keep-this-line"},
    )
    assert sent.status_code == 200, sent.text
    stored = sent.json()["chat"]
    assert len(stored["messages"]) == 2
    original = stored["messages"][0]
    reply = stored["messages"][1]["content"]
    assert original["content"] == "keep-this-line"
    path = tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json"

    page = client.get("/static/app.js")
    assert 'class: "reaction"' in page.text

    reacted = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/{original['id']}/reaction",
        json={"emoji": "👍"},
    )
    assert reacted.status_code == 200, reacted.text
    body = reacted.json()
    assert len(body["messages"]) == 2
    assert body["messages"][0]["content"] == "keep-this-line"
    assert body["messages"][0]["reaction"] == "👍"
    assert body["messages"][1]["content"] == reply
    assert "reaction" not in body["messages"][1]
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["messages"][0]["content"] == "keep-this-line"
    assert on_disk["messages"][0]["reaction"] == "👍"
    assert len(on_disk["messages"]) == 2

    later = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "still-here"},
    )
    assert later.status_code == 200, later.text
    kept = later.json()["chat"]["messages"]
    assert kept[0]["content"] == "keep-this-line"
    assert kept[0]["reaction"] == "👍"
    assert len(kept) == 4

    cleared = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/{original['id']}/reaction",
        json={"emoji": "👍"},
    )
    assert cleared.status_code == 200, cleared.text
    gone = cleared.json()
    assert gone["messages"][0]["content"] == "keep-this-line"
    assert "reaction" not in gone["messages"][0]
    assert gone["messages"][1]["content"] == reply
    assert len(gone["messages"]) == 4
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["messages"][0]["content"] == "keep-this-line"
    assert "reaction" not in final["messages"][0]
    assert "👍" not in path.read_text(encoding="utf-8")
    assert [item["content"] for item in final["messages"]] == [
        "keep-this-line",
        reply,
        "still-here",
        "ack",
    ]


class Script:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.seen.append(messages)
        if not self.replies:
            return "ack"
        reply = self.replies.pop(0)
        if callable(reply):
            return reply(messages)
        return reply


def _world(tmp_path, monkeypatch, replies):
    script = Script(replies)
    monkeypatch.setattr("easyagent.llm.complete", script)
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat, script


def _send(client, bot, chat, content):
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": content},
    )
    assert sent.status_code == 200, sent.text
    return sent.json()["chat"]


def _react(client, bot, chat, message_id, emoji):
    reacted = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/{message_id}/reaction",
        json={"emoji": emoji},
    )
    assert reacted.status_code == 200, reacted.text
    return reacted.json()


def test_a_reaction_on_the_latest_reply_reaches_the_model(tmp_path, monkeypatch):
    client, bot, chat, script = _world(tmp_path, monkeypatch, ["Here is the answer.", "Glad that landed."])
    stored = _send(client, bot, chat, "keep-this-line")
    answer = stored["messages"][1]
    assert answer["content"] == "Here is the answer."
    body = _react(client, bot, chat, answer["id"], "👍")
    assert body["messages"][1]["content"] == "Here is the answer."
    assert body["messages"][1]["reaction"] == "👍"
    assert body["messages"][-1]["role"] == "assistant"
    assert body["messages"][-1]["content"] == "Glad that landed."
    assert len(script.seen) == 2
    heard = json.dumps(script.seen[1], ensure_ascii=False)
    assert "👍" in heard
    assert answer["id"] in heard
    assert "Here is the answer." in heard
    assert "The person reacted" in heard


def test_an_older_reaction_is_still_in_the_next_turn(tmp_path, monkeypatch):
    client, bot, chat, script = _world(
        tmp_path, monkeypatch, ["first-reply", "second-reply", "third-reply"]
    )
    first = _send(client, bot, chat, "one")
    answer_id = first["messages"][1]["id"]
    _send(client, bot, chat, "two")
    assert len(script.seen) == 2
    body = _react(client, bot, chat, answer_id, "👀")
    assert body["messages"][1]["reaction"] == "👀"
    assert len(script.seen) == 2
    _send(client, bot, chat, "three")
    assert len(script.seen) == 3
    heard = json.dumps(script.seen[2], ensure_ascii=False)
    assert "👀" in heard
    assert answer_id in heard
    assert "first-reply" in heard


def test_clearing_the_latest_reaction_does_not_ask_again(tmp_path, monkeypatch):
    client, bot, chat, script = _world(tmp_path, monkeypatch, ["answer", "thanks"])
    stored = _send(client, bot, chat, "hello")
    answer_id = stored["messages"][1]["id"]
    body = _react(client, bot, chat, answer_id, "👎")
    assert body["messages"][1]["reaction"] == "👎"
    assert body["messages"][-1]["content"] == "thanks"
    assert len(script.seen) == 2
    cleared = _react(client, bot, chat, answer_id, "👎")
    assert "reaction" not in cleared["messages"][1]
    assert cleared["messages"][1]["content"] == "answer"
    assert len(script.seen) == 2


def test_the_bot_can_react_to_the_persons_message(tmp_path, monkeypatch):
    import re

    def reply(messages):
        system = messages[0]["content"]
        match = re.search(r"- person ([0-9a-f-]{36}):", system)
        assert match, system
        return f"Okay.\n```react\n👍\n{match.group(1)}\n```"

    client, bot, chat, script = _world(tmp_path, monkeypatch, [reply])
    stored = _send(client, bot, chat, "hello there")
    assert stored["messages"][0]["content"] == "hello there"
    assert stored["messages"][0]["reaction"] == "👍"
    assert stored["messages"][1]["content"] == "Okay."
    assert "```" not in stored["messages"][1]["content"]
    page = client.get("/static/app.js")
    assert "toggleReaction" in page.text
    assert "/reaction" in page.text


def test_a_native_react_call_names_the_emoji_and_the_message():
    from easyagent.llm import calls_to_fence
    from easyagent.tools import parse_tools

    fence = calls_to_fence(
        [{"name": "react", "arguments": {"emoji": "👀", "message_id": "abc-def-ghij-klmn"}}]
    )
    found = parse_tools(fence)
    assert len(found) == 1
    assert found[0].kind == "react"
    assert found[0].body == "👀"
    assert found[0].path == "abc-def-ghij-klmn"
