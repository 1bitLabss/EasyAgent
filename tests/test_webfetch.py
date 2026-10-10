"""Search providers, page fetch, and research citations. Fixtures only. No network."""

import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.prompt import build_system
from easyagent.search import (
    SearchError,
    citation_problems,
    fetch_page,
    format_results,
    load_settings,
    readable_text,
    research,
    save_settings,
    stories_from_brave,
    stories_from_duckduckgo_html,
    stories_from_searxng,
    stories_from_tavily,
    web_search,
)
from easyagent.secrets import delete_secret
from easyagent.store import Store
from easyagent.tools import parse_tools


def _clear_keys():
    delete_secret("search:brave")
    delete_secret("search:tavily")


def test_duckduckgo_html_keeps_title_url_snippet_and_date():
    html = """
    <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fparis">Paris guide</a>
    <a class="result__snippet">Paris is the capital of France.</a>
    <span class="result__timestamp">2026-10-01</span>
    """
    items = stories_from_duckduckgo_html(html)
    assert items[0]["title"] == "Paris guide"
    assert items[0]["url"] == "https://example.com/paris"
    assert items[0]["snippet"] == "Paris is the capital of France."
    assert items[0]["date"] == "2026-10-01"
    text = format_results(items)
    assert "2026-10-01" in text
    assert "https://example.com/paris" in text


def test_provider_parsers_keep_the_same_fields():
    searx = stories_from_searxng({
        "results": [{
            "title": "Paris",
            "url": "https://example.com/paris",
            "content": "A city.",
            "publishedDate": "2026-01-02",
        }]
    })
    brave = stories_from_brave({
        "web": {"results": [{
            "title": "Paris",
            "url": "https://example.com/paris",
            "description": "A city.",
            "age": "2 days ago",
        }]}
    })
    tavily = stories_from_tavily({
        "results": [{
            "title": "Paris",
            "url": "https://example.com/paris",
            "content": "A city.",
            "published_date": "2026-01-02",
        }]
    })
    for items, date in ((searx, "2026-01-02"), (brave, "2 days ago"), (tavily, "2026-01-02")):
        assert items[0]["title"] == "Paris"
        assert items[0]["url"] == "https://example.com/paris"
        assert items[0]["snippet"] == "A city."
        assert items[0]["date"] == date


def test_saved_provider_is_used_and_the_key_stays_out_of_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    _clear_keys()
    store = Store(tmp_path)
    saved = save_settings(store, provider="searxng", searxng_url="http://127.0.0.1:9")
    assert saved["provider"] == "searxng"
    assert "brave" not in (tmp_path / "search.json").read_text(encoding="utf-8")

    async def get_text(url, params=None, headers=None):
        assert url == "http://127.0.0.1:9/search"
        assert params["q"] == "paris"
        return json.dumps({
            "results": [{
                "title": "Paris",
                "url": "https://example.com/paris",
                "content": "Capital city.",
                "publishedDate": "2026-04-01",
            }]
        })

    monkeypatch.setattr("easyagent.search._get_text", get_text)
    findings = asyncio.run(web_search("paris", store=store))
    assert "Capital city." in findings
    assert "2026-04-01" in findings
    monkeypatch.setenv("EASYAGENT_SEARCH_PROVIDER", "duckduckgo")
    assert load_settings(store)["provider"] == "duckduckgo"
    monkeypatch.delenv("EASYAGENT_SEARCH_PROVIDER")


def test_a_missing_key_or_address_searches_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    _clear_keys()
    store = Store(tmp_path)
    save_settings(store, provider="brave")

    async def fail(*_args, **_kwargs):
        raise AssertionError("the network was called")

    monkeypatch.setattr("easyagent.search._get_text", fail)
    monkeypatch.setattr("easyagent.search._post_json", fail)
    try:
        asyncio.run(web_search("paris", store=store))
        raise AssertionError("brave should have refused")
    except SearchError as exc:
        assert "Brave" in str(exc)
        assert "Nothing was searched" in str(exc)
    save_settings(store, provider="tavily")
    try:
        asyncio.run(web_search("paris", store=store))
        raise AssertionError("tavily should have refused")
    except SearchError as exc:
        assert "Tavily" in str(exc)
    (tmp_path / "search.json").write_text('{"provider": "searxng", "searxng_url": ""}', encoding="utf-8")
    try:
        asyncio.run(web_search("paris", store=store))
        raise AssertionError("searxng should have refused")
    except SearchError as exc:
        assert "SearXNG" in str(exc)


def test_keyed_errors_do_not_contain_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    _clear_keys()
    store = Store(tmp_path)
    secret = "SECRET-BRAVE-KEY-991"
    save_settings(store, provider="brave", brave_key=secret)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            token = (headers or {}).get("X-Subscription-Token", "")
            raise httpx.ConnectError(f"down {token}")

        async def post(self, url, json=None, headers=None):
            key = (json or {}).get("api_key", "")
            raise httpx.ConnectError(f"down {key}")

    monkeypatch.setattr("easyagent.search.httpx.AsyncClient", Client)
    try:
        asyncio.run(web_search("paris", store=store))
        raise AssertionError("brave should have failed")
    except SearchError as exc:
        assert secret not in str(exc)
    assert secret not in (tmp_path / "search.json").read_text(encoding="utf-8")
    other = "SECRET-TAVILY-KEY-992"
    save_settings(store, provider="tavily", tavily_key=other)
    try:
        asyncio.run(web_search("paris", store=store))
        raise AssertionError("tavily should have failed")
    except SearchError as exc:
        assert other not in str(exc)
        assert secret not in str(exc)
    assert other not in (tmp_path / "search.json").read_text(encoding="utf-8")
    _clear_keys()


def test_readable_text_drops_scripts_and_a_thin_page_uses_the_browser():
    html = (
        "<html><head><script>ignore this secret</script><style>.x{}</style></head>"
        "<body><nav>Home</nav><article><p>Lighthouses mark the coast.</p></article>"
        "<footer>Copyright</footer></body></html>"
    )
    text = readable_text(html)
    assert "Lighthouses mark the coast." in text
    assert "ignore this secret" not in text
    assert "Home" not in text
    assert "Copyright" not in text

    thin = "<html><script></script><script></script><script>app()</script><div id='root'></div></html>"
    assert len(readable_text(thin)) < 200

    async def opener(url):
        assert url == "https://example.com/app"
        return "<article><p>The rendered lighthouse page is long enough to keep.</p></article>"

    async def get_text(url, params=None, headers=None):
        return thin

    import easyagent.search as search_mod

    original = search_mod._get_text
    search_mod._get_text = get_text
    try:
        found = asyncio.run(fetch_page("https://example.com/app", opener=opener))
    finally:
        search_mod._get_text = original
    assert "rendered lighthouse" in found
    assert "UNTRUSTED" in found
    assert "app()" not in found


def test_a_fetched_page_is_cached_for_that_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ann", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    calls = []

    async def get_text(url, params=None, headers=None):
        calls.append(url)
        return "<article><p>Readable article about lighthouses along the coast.</p></article>"

    monkeypatch.setattr("easyagent.search._get_text", get_text)

    async def go():
        from easyagent import turn

        turn.bind(store, chat["id"], bot["id"])
        try:
            first = await fetch_page("https://example.com/light", store=store, bot_id=bot["id"])
            second = await fetch_page("https://example.com/light", store=store, bot_id=bot["id"])
            return first, second
        finally:
            turn._cancel.set(None)
            turn._slot.set(None)

    first, second = asyncio.run(go())
    assert calls == ["https://example.com/light"]
    assert "lighthouses" in first
    assert "UNTRUSTED" in first
    assert first == second
    other = store.create_chat(bot["id"])

    async def again():
        from easyagent import turn

        turn.bind(store, other["id"], bot["id"])
        try:
            return await fetch_page("https://example.com/light", store=store, bot_id=bot["id"])
        finally:
            turn._cancel.set(None)
            turn._slot.set(None)

    asyncio.run(again())
    assert calls == ["https://example.com/light", "https://example.com/light"]


def test_fetch_rejects_a_non_http_url():
    try:
        asyncio.run(fetch_page("file:///etc/passwd"))
        raise AssertionError("file urls must fail")
    except SearchError as exc:
        assert "http" in str(exc).lower()


def test_research_reads_the_top_results_and_the_checker_maps_claims(tmp_path, monkeypatch):
    store = Store(tmp_path)
    html = """
    <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fparis">Paris guide</a>
    <a class="result__snippet">Paris is the capital of France.</a>
    <span class="result__timestamp">2026-10-01</span>
    """
    page = "<article><p>Paris is the capital of France and holds the Louvre museum.</p></article>"

    async def get_text(url, params=None, headers=None):
        if "api.duckduckgo.com" in url:
            return "{}"
        if "html.duckduckgo.com" in url:
            return html
        if url == "https://example.com/paris":
            return page
        raise AssertionError(url)

    monkeypatch.setattr("easyagent.search._get_text", get_text)
    packet = asyncio.run(research("capital of France", store=store))
    assert "https://example.com/paris" in packet
    assert "Louvre" in packet
    assert "UNTRUSTED" in packet
    sources = [
        {
            "title": "Paris guide",
            "url": "https://example.com/paris",
            "snippet": "Paris is the capital of France.",
            "text": "Paris is the capital of France and holds the Louvre museum.",
        }
    ]
    assert citation_problems("It is nice.", sources) == ["The answer does not cite a source."]
    assert any("not a source" in item for item in citation_problems("See [9].", sources))
    assert citation_problems("Bananas are yellow [1].", sources)
    assert citation_problems("Paris is the capital of France [1].", sources) == []
    from easyagent.search import sources_from_text

    parsed = sources_from_text(packet)
    assert parsed[0]["url"] == "https://example.com/paris"
    assert citation_problems("The Louvre museum is in Paris [1].", parsed) == []


def test_prompt_samples_are_not_tool_calls():
    system = build_system(
        bot_name="Ann",
        direction="",
        summary="",
        skills_text="",
    )
    kinds = {item.kind for item in parse_tools(system)}
    assert "search" not in kinds
    assert "fetch" not in kinds
    assert "research" not in kinds
    assert parse_tools("```fetch\nhttps://example.com\n```") == []
    assert parse_tools("```research\nthe question\n```") == []
    fetched = parse_tools("```fetch\nhttps://example.com/real\n```")
    assert fetched[0].kind == "fetch"
    assert fetched[0].body == "https://example.com/real"
    asked = parse_tools("```research\ncapital of France\n```")
    assert asked[0].kind == "research"


def test_search_setup_does_not_echo_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    _clear_keys()
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/search-setup").json()["provider"] == "duckduckgo"
    saved = client.put(
        "/api/search-setup",
        json={"provider": "brave", "brave_key": "SECRET-BRAVE-KEY-991"},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["has_brave_key"] is True
    assert "SECRET" not in saved.text
    again = client.get("/api/search-setup")
    assert again.json()["has_brave_key"] is True
    assert "SECRET" not in again.text
    assert "SECRET" not in (tmp_path / "search.json").read_text(encoding="utf-8")
    refused = client.put("/api/search-setup", json={"provider": "searxng", "searxng_url": "not-a-url"})
    assert refused.status_code == 400
    cleared = client.put("/api/search-setup", json={"provider": "duckduckgo", "clear_brave": True})
    assert cleared.json()["has_brave_key"] is False
    _clear_keys()


def test_a_research_answer_must_cite_a_fetched_page(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    html = """
    <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fparis">Paris guide</a>
    <a class="result__snippet">Paris is the capital of France.</a>
    <span class="result__timestamp">2026-10-01</span>
    """
    page = "<article><p>Paris is the capital of France and holds the Louvre museum.</p></article>"

    async def get_text(url, params=None, headers=None):
        if "api.duckduckgo.com" in url:
            return "{}"
        if "html.duckduckgo.com" in url:
            return html
        return page

    monkeypatch.setattr("easyagent.search._get_text", get_text)
    calls = []

    async def complete(**kwargs):
        blob = "\n".join(str(item.get("content") or "") for item in kwargs["messages"])
        calls.append(blob)
        if "citation problem" in blob:
            return "Paris is the capital of France [1]."
        if "example.com/paris" in blob:
            return "Paris is lovely."
        return "```research\ncapital of France\n```"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ann", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    response = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Where is the Louvre?"},
    )
    assert response.status_code == 200, response.text
    answer = response.json()["chat"]["messages"][-1]["content"]
    assert "Paris is the capital of France [1]." in answer
    assert any("citation problem" in blob for blob in calls)
