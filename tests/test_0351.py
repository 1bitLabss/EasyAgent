"""0.3.51: user-facing chat search. One query finds passages across every bot's saved chats."""

from fastapi.testclient import TestClient

from easyagent.app import create_app


def open_client(tmp_path, monkeypatch, reply):
    monkeypatch.setattr("easyagent.llm.complete", reply)
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat


class Script:
    def __init__(self, replies):
        self.replies = list(replies)

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        return self.replies.pop(0) if self.replies else "ack"


def test_chat_search_finds_words_from_saved_messages(tmp_path, monkeypatch):
    client, bot, chat = open_client(tmp_path, monkeypatch, Script(["the kessel run totals nine hundred credits"]))
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "log the kessel run cargo manifest"},
    )
    assert sent.status_code == 200, sent.text
    found = client.get("/api/chat-search", params={"q": "kessel"})
    assert found.status_code == 200
    results = found.json()["results"]
    assert results
    hit = results[0]
    assert hit["bot_id"] == bot["id"]
    assert hit["bot_name"] == "Ada"
    assert hit["chat_id"] == chat["id"]
    assert "kessel" in hit["text"].lower()
    assert hit["message_id"]


def test_chat_search_covers_every_bot(tmp_path, monkeypatch):
    script = Script(["noted", "recorded"])
    client, bot_a, chat_a = open_client(tmp_path, monkeypatch, script)
    endpoint = client.post("/api/endpoints", json={"name": "two", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot_b = client.post("/api/bots", json={"name": "Bo", "endpoint_id": endpoint["id"]}).json()
    chat_b = client.post(f"/api/bots/{bot_b['id']}/chats").json()
    first = client.post(
        f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages", json={"content": "orchid pruning schedule"}
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/bots/{bot_b['id']}/chats/{chat_b['id']}/messages", json={"content": "orchid watering schedule"}
    )
    assert second.status_code == 200, second.text
    results = client.get("/api/chat-search", params={"q": "orchid schedule"}).json()["results"]
    assert {hit["bot_name"] for hit in results} == {"Ada", "Bo"}


def test_chat_search_short_query_returns_nothing(tmp_path, monkeypatch):
    client, *_ = open_client(tmp_path, monkeypatch, Script([]))
    assert client.get("/api/chat-search", params={"q": "a"}).json()["results"] == []
    assert client.get("/api/chat-search").json()["results"] == []


def test_chat_search_ignores_harness_rows(tmp_path, monkeypatch):
    client, bot, chat = open_client(tmp_path, monkeypatch, Script(["ack"]))
    client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages", json={"content": "ship the ledger"})
    results = client.get("/api/chat-search", params={"q": "typo transient failure"}).json()["results"]
    assert all("typo" not in hit["text"].lower() for hit in results)
