"""A fallback provider gets its own key. A key never rides along to the other host."""

import asyncio
import json

import pytest

from easyagent import gate, retry
from easyagent.llm import ProviderError, complete, stream_complete
from easyagent.providers import chain_for, register
from easyagent.store import Store, StoreError

KEY_A = "sk-AAAA-provider-key"
KEY_B = "sk-BBBB-provider-key"
KEY_SAME = "sk-SAME-shared-provider-key"
URL_A = "https://a.example/v1"
URL_B = "https://b.example/v1"
ASK = [{"role": "user", "content": "say pong"}]


def _ok(text="pong"):
    return json.dumps({"choices": [{"message": {"content": text}}]})


class _Body:
    def __init__(self, status, body, lines=None):
        self.status_code = status
        self.text = body
        self._body = body
        self.headers = {"content-type": "text/event-stream" if lines else "application/json"}
        self._lines = lines or []

    def json(self):
        return json.loads(self._body)

    async def aread(self):
        return self._body.encode()

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Script:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def _take(self, url, payload, headers):
        self.seen.append({"url": url, "json": payload, "headers": dict(headers or {})})
        if not self.replies:
            raise AssertionError("unexpected provider call")
        return self.replies.pop(0)

    async def post(self, url, json=None, headers=None):
        return self._take(url, json, headers)

    def stream(self, _method, url, json=None, headers=None):
        reply = self._take(url, json, headers)

        class _CM:
            async def __aenter__(self):
                return reply

            async def __aexit__(self, *_exc):
                return False

        return _CM()

    async def aclose(self):
        return None


def _install(monkeypatch, script):
    gate.reset_lanes()
    retry.reset_unreachable()
    monkeypatch.setattr("easyagent.llm.httpx.AsyncClient", lambda *_a, **_k: script)


def _blob(call):
    return json.dumps({"url": call["url"], "json": call["json"], "headers": call["headers"]})


def test_a_rate_limit_uses_the_next_providers_own_key(monkeypatch):
    script = _Script([
        _Body(429, json.dumps({"error": f"slow down {KEY_A}"})),
        _Body(200, _ok()),
    ])
    _install(monkeypatch, script)
    register("ep-a", URL_A, KEY_A)
    register("ep-b", URL_B, KEY_B)

    text = asyncio.run(complete(
        base_url=URL_A,
        api_key=KEY_A,
        model="ada",
        messages=ASK,
        tools=False,
        fallbacks=[{"base_url": URL_B, "api_key": KEY_B, "model": "bee"}],
    ))

    assert text == "pong"
    assert len(script.seen) == 2
    first, second = script.seen
    assert first["url"].startswith(URL_A)
    assert first["headers"]["Authorization"] == f"Bearer {KEY_A}"
    assert second["url"].startswith(URL_B)
    assert second["headers"]["Authorization"] == f"Bearer {KEY_B}"
    assert second["json"]["model"] == "bee"
    assert second["json"]["messages"] == first["json"]["messages"] == ASK
    assert KEY_A not in _blob(second)
    assert KEY_B not in _blob(first)
    assert "slow down" not in _blob(second)


def test_a_stream_rate_limit_does_not_carry_the_first_key(monkeypatch):
    payload = json.dumps({"choices": [{"delta": {"content": "pong"}}]})
    script = _Script([
        _Body(429, json.dumps({"error": KEY_A})),
        _Body(200, "", lines=[f"data: {payload}", "data: [DONE]"]),
    ])
    _install(monkeypatch, script)
    register("ep-a", URL_A, KEY_A)
    register("ep-b", URL_B, KEY_B)

    async def run():
        parts = []
        async for piece in stream_complete(
            base_url=URL_A,
            api_key=KEY_A,
            model="ada",
            messages=ASK,
            fallbacks=[{"base_url": URL_B, "api_key": KEY_B, "model": "bee"}],
        ):
            parts.append(piece)
        return "".join(parts)

    assert asyncio.run(run()) == "pong"
    assert script.seen[1]["headers"]["Authorization"] == f"Bearer {KEY_B}"
    assert KEY_A not in _blob(script.seen[1])
    assert script.seen[1]["json"]["messages"] == ASK


def test_the_same_key_is_not_sent_to_the_second_host(monkeypatch):
    script = _Script([_Body(200, _ok())])
    _install(monkeypatch, script)
    register("ep-a", URL_A, KEY_SAME)
    register("ep-b", URL_B, KEY_SAME)

    with pytest.raises(ProviderError, match="Refusing to send a key"):
        asyncio.run(complete(
            base_url=URL_B,
            api_key=KEY_SAME,
            model="bee",
            messages=ASK,
            tools=False,
        ))
    assert script.seen == []

    script.replies.clear()
    script.replies.append(_Body(429, json.dumps({"error": KEY_SAME})))
    with pytest.raises(ProviderError, match="Refusing to send a key"):
        asyncio.run(complete(
            base_url=URL_A,
            api_key=KEY_SAME,
            model="ada",
            messages=ASK,
            tools=False,
            fallbacks=[{"base_url": URL_B, "api_key": KEY_SAME, "model": "bee"}],
        ))
    assert len(script.seen) == 1
    assert script.seen[0]["url"].startswith(URL_A)
    assert KEY_SAME not in script.seen[0]["url"]
    assert all(not call["url"].startswith(URL_B) for call in script.seen)


def test_a_client_error_does_not_fall_over_and_a_key_is_scrubbed(monkeypatch):
    script = _Script([
        _Body(400, json.dumps({"error": f"bad request {KEY_A}"})),
        _Body(200, _ok("nope")),
    ])
    _install(monkeypatch, script)
    register("ep-a", URL_A, KEY_A)

    with pytest.raises(ProviderError) as caught:
        asyncio.run(complete(
            base_url=URL_A,
            api_key=KEY_A,
            model="ada",
            messages=ASK,
            tools=False,
            fallbacks=[{"base_url": URL_B, "api_key": KEY_B, "model": "bee"}],
        ))
    assert len(script.seen) == 1
    assert KEY_A not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_saved_fallbacks_survive_a_restart_and_keep_keys_out_of_the_file(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    store.ensure()
    spare = store.add_endpoint(name="spare", base_url=URL_B, api_key=KEY_B, model="bee")
    primary = store.add_endpoint(
        name="primary",
        base_url=URL_A,
        api_key=KEY_A,
        model="ada",
        fallback_ids=[spare["id"]],
    )
    raw = (store.root / "endpoints.json").read_text(encoding="utf-8")
    assert KEY_A not in raw
    assert KEY_B not in raw
    assert spare["id"] in raw
    with pytest.raises(StoreError):
        store.add_endpoint(name="loop", base_url=URL_A, api_key=None, fallback_ids=[primary["id"], "not-an-id"])
    with pytest.raises(StoreError):
        store.update_endpoint(primary["id"], fallback_ids=[primary["id"]], fallback_ids_set=True)

    reopened = Store(store.root)
    loaded = reopened.get_endpoint(primary["id"])
    assert loaded["fallback_ids"] == [spare["id"]]
    chain = chain_for(reopened, loaded)
    assert [row["api_key"] for row in chain] == [KEY_A, KEY_B]
    assert [row["base_url"] for row in chain] == [URL_A, URL_B]

    script = _Script([
        _Body(429, json.dumps({"error": KEY_A})),
        _Body(200, _ok("from-bee")),
    ])
    _install(monkeypatch, script)
    conn = gate.bind_connection(loaded, "Ada", store=reopened)
    try:
        text = asyncio.run(complete(
            base_url=loaded["base_url"],
            api_key=loaded.get("api_key") or None,
            model="ada",
            messages=ASK,
            tools=False,
        ))
    finally:
        gate.reset_connection(conn)
    assert text == "from-bee"
    assert script.seen[0]["headers"]["Authorization"] == f"Bearer {KEY_A}"
    assert script.seen[1]["headers"]["Authorization"] == f"Bearer {KEY_B}"
    assert KEY_A not in _blob(script.seen[1])
    assert KEY_B not in _blob(script.seen[0])
