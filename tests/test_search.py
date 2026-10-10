"""Search runs on this computer. The stored chat gets the answer or the error."""

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.search import SearchError, results_from_json, search_query
from easyagent.store import Store


def test_duckduckgo_json_and_ignore_the_prompt_example():
    items = results_from_json(
        {
            "Heading": "Paris",
            "AbstractText": "Paris is the capital of France.",
            "AbstractURL": "https://en.wikipedia.org/wiki/Paris",
            "RelatedTopics": [{"Text": "Example - a second hit", "FirstURL": "https://example.com/paris"}],
        }
    )
    assert items[0]["url"] == "https://en.wikipedia.org/wiki/Paris"
    assert items[0]["snippet"] == "Paris is the capital of France."
    assert items[1]["url"] == "https://example.com/paris"
    assert search_query("```search\nthe query\n```") is None
    assert search_query("```search\nParis France\n```") == "Paris France"


def _client(tmp_path, monkeypatch, complete, search):
    monkeypatch.setattr("easyagent.search.llm.complete", complete)
    monkeypatch.setattr("easyagent.search.web_search", search)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ann", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat


def test_phone_sees_the_answer_not_the_search_page(tmp_path, monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs["messages"])
        if len(calls) == 1:
            return "```search\nParis France\n```"
        return "Paris is the capital of France."

    async def search(query):
        assert query == "Paris France"
        return "1. Paris - Wikipedia\nhttps://en.wikipedia.org/wiki/Paris\nSNIPPET-ONLY-ON-THE-MACHINE"

    client, bot, chat = _client(tmp_path, monkeypatch, complete, search)
    response = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "What is the capital of France?"},
    )
    assert response.status_code == 200, response.text
    messages = response.json()["chat"]["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "What is the capital of France?"
    assert messages[1]["content"] == "Paris is the capital of France."
    assert "Searched the web." not in messages[1]["content"]
    assert "SNIPPET-ONLY-ON-THE-MACHINE" not in response.text
    tool_msgs = [item for item in calls[1] if item.get("role") == "tool"]
    assert any("SNIPPET-ONLY-ON-THE-MACHINE" in (item.get("content") or "") for item in tool_msgs)
    paired = [item for item in calls[1] if item.get("role") == "assistant" and item.get("tool_calls")]
    assert paired
    assert tool_msgs[-1].get("tool_call_id") == paired[-1]["tool_calls"][0]["id"]
    stored = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert "SNIPPET-ONLY-ON-THE-MACHINE" not in stored
    assert "Paris is the capital of France." in stored


def test_search_failure_stays_in_the_chat(tmp_path, monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs["messages"])
        return "```search\nParis France\n```"

    async def search(query):
        raise SearchError("Search failed: could not reach the search provider. down")

    client, bot, chat = _client(tmp_path, monkeypatch, complete, search)
    before = Store(tmp_path).get_chat(bot["id"], chat["id"])
    response = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "keep-this-question"},
    )
    assert response.status_code == 200, response.text
    messages = response.json()["chat"]["messages"]
    assert messages[0]["content"] == "keep-this-question"
    assert messages[1].get("error") is not True
    assert "Search failed" not in messages[1]["content"]
    fed = [item for item in calls[-1] if item.get("role") == "tool"]
    assert fed and "Search failed" in fed[-1]["content"]
    again = Store(tmp_path).get_chat(bot["id"], chat["id"])
    assert len(again["messages"]) == len(before["messages"]) + 2
    assert again["messages"][0]["content"] == "keep-this-question"


def test_an_empty_news_search_fetches_the_cnn_story(monkeypatch):
    import asyncio

    from easyagent.search import stories_from_cnn_lite, web_search

    page = (
        '<a href="/">CNN</a>'
        '<a href="/2026/10/04/world/bridge-kyiv">'
        "Russian drone slams into a major bridge in Kyiv during the visit"
        "</a>"
    )
    parsed = stories_from_cnn_lite(page)
    assert parsed[0]["title"].startswith("Russian drone slams")
    assert parsed[0]["url"] == "https://www.cnn.com/2026/10/04/world/bridge-kyiv"

    async def get_text(url, params=None):
        if "lite.cnn.com" in url:
            return page
        return '{"AbstractText":"","RelatedTopics":[]}'

    monkeypatch.setattr("easyagent.search._get_text", get_text)
    findings = asyncio.run(web_search("top story on CNN right now"))
    assert "Russian drone slams into a major bridge in Kyiv" in findings
    assert "https://www.cnn.com/2026/10/04/world/bridge-kyiv" in findings


def test_news_with_no_story_says_the_search_found_nothing(monkeypatch):
    import asyncio

    from easyagent.search import web_search

    async def get_text(url, params=None):
        if "lite.cnn.com" in url:
            return "<html><a href='/'>CNN</a></html>"
        return '{"AbstractText":"","RelatedTopics":[]}'

    monkeypatch.setattr("easyagent.search._get_text", get_text)
    findings = asyncio.run(web_search("what is the top story on CNN right now"))
    assert findings == "The search found nothing."
