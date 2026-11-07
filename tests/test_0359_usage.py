"""Real token counts flow from the model server into saved message rows.

The audited gap: llm.py parsed usage but dropped it, and the context meter read
a field nothing ever wrote. Now streams ask for usage in the final chunk
(stream_options include_usage), the finished assistant row carries
prompt_tokens, completion_tokens and elapsed_ms, the meter shows the latest
real prompt size, and a small Today counter feeds the jobs panel rollup.
"""

import asyncio
import json

from fastapi.testclient import TestClient

from easyagent import llm
from easyagent import turn as turn_mod
from easyagent.app import create_app
from easyagent.features import reset_routine_turn

BASE = "http://127.0.0.1:9/v1"


def fresh_turn_context() -> None:
    """Direct stream calls bind no slot. Clear what an earlier test left behind."""
    turn_mod.renew_cancel()
    reset_routine_turn()


def _chunk(delta=None, finish=None):
    return "data: " + json.dumps(
        {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    )


def _usage_line(prompt, completion):
    return "data: " + json.dumps(
        {"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}
    )


USAGE_LINES = [
    _chunk({"content": "Twelve "}),
    _chunk({"content": "boats."}, finish="stop"),
    _usage_line(1234, 56),
    "data: [DONE]",
]

NO_USAGE_LINES = [
    _chunk({"content": "Plain."}, finish="stop"),
    "data: [DONE]",
]


class _NotFound:
    status_code = 404
    headers = {"content-type": "application/json"}

    async def aread(self):
        return b"{}"

    def json(self):
        return {}


class _SseResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines):
        self._lines = lines

    async def aread(self):
        return "\n".join(self._lines).encode()

    def aiter_lines(self):
        async def walk():
            for line in self._lines:
                yield line

        return walk()


class _StatusResponse:
    def __init__(self, code, text):
        self.status_code = code
        self.headers = {"content-type": "application/json"}
        self._text = text
        self.text = text

    async def aread(self):
        return self._text.encode()

    def json(self):
        return {"error": self._text}


class _StreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


class FakeServer:
    """Stands in for httpx.AsyncClient. Each stream call replays one script."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.payloads = []
        self.calls = 0

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aclose(self):
        return None

    async def get(self, url, headers=None):
        return _NotFound()

    async def post(self, url, json=None, headers=None):
        return _NotFound()

    def stream(self, method, url, json=None, headers=None):
        self.payloads.append(json)
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        if script[0] == "sse":
            return _StreamCtx(_SseResponse(script[1]))
        return _StreamCtx(_StatusResponse(script[1], script[2]))


async def _ack(**_kwargs):
    return "ack"


def _drain_stream(base_url):
    async def collect():
        parts = []
        async for piece in llm.stream_complete(
            base_url=base_url, api_key=None, model="m", messages=[{"role": "user", "content": "a"}]
        ):
            parts.append(piece)
        return "".join(parts)

    return asyncio.run(collect())


def _world(tmp_path, monkeypatch, server):
    monkeypatch.setattr(llm.httpx, "AsyncClient", server)
    monkeypatch.setattr("easyagent.llm.complete", _ack)
    llm.reset_stream_options_cache()
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": BASE}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat


def _send(client, bot, chat):
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST", url, json={"content": "how many boats"}, headers={"Accept": "text/event-stream"}
    ) as response:
        assert response.status_code == 200, response.read()
        response.read()


def test_stream_asks_for_usage_and_records_the_final_chunk(monkeypatch):
    """stream_options goes out; usage in the final chunk lands in the turn tally."""
    server = FakeServer([("sse", USAGE_LINES)])
    monkeypatch.setattr(llm.httpx, "AsyncClient", server)
    llm.reset_stream_options_cache()
    fresh_turn_context()
    llm.begin_turn_usage()
    token = llm.bind_purpose("chat")
    try:
        text = _drain_stream(BASE)
    finally:
        llm.reset_purpose(token)
    assert "Twelve boats." in text
    assert server.payloads[0]["stream_options"] == {"include_usage": True}
    assert server.payloads[0]["stream"] is True
    usage = llm.turn_usage()
    assert usage is not None
    assert usage["prompt"] == 1234
    assert usage["completion"] == 56


def test_server_that_rejects_stream_options_is_retried_without_it(monkeypatch):
    """One 400 with the field on the request, and the server is remembered."""
    server = FakeServer(
        [("status", 400, "Unknown field: stream_options"), ("sse", USAGE_LINES)]
    )
    monkeypatch.setattr(llm.httpx, "AsyncClient", server)
    llm.reset_stream_options_cache()
    fresh_turn_context()
    llm.begin_turn_usage()
    token = llm.bind_purpose("chat")
    try:
        text = _drain_stream(BASE)
    finally:
        llm.reset_purpose(token)
    assert "Twelve boats." in text
    assert len(server.payloads) == 2
    assert "stream_options" in server.payloads[0]
    assert "stream_options" not in server.payloads[1]
    assert llm.stream_options_supported(BASE) is False
    llm.reset_stream_options_cache()


def test_row_is_stamped_meter_shows_real_usage_and_today_counts(tmp_path, monkeypatch):
    """The saved assistant row carries the counts; the meter reads the real size."""
    server = FakeServer([("sse", USAGE_LINES)])
    client, bot, chat = _world(tmp_path, monkeypatch, server)
    _send(client, bot, chat)
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    row = stored["messages"][-1]
    assert row["role"] == "assistant"
    assert row["content"] == "Twelve boats."
    assert row["prompt_tokens"] == 1234
    assert row["completion_tokens"] == 56
    assert row["elapsed_ms"] > 0
    # The dead meter is alive: it shows the latest real prompt size, not an estimate.
    assert stored["context"]["prompt_tokens"] == 1234
    assert stored["context"]["prompt_estimated"] == 0
    today = client.get("/api/usage/today").json()
    assert today["prompt_tokens"] == 1234
    assert today["completion_tokens"] == 56


def test_no_usage_server_stamps_time_only(tmp_path, monkeypatch):
    """A server that never sends usage still gets a time on the row."""
    server = FakeServer([("sse", NO_USAGE_LINES)])
    client, bot, chat = _world(tmp_path, monkeypatch, server)
    _send(client, bot, chat)
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    row = stored["messages"][-1]
    assert row["content"] == "Plain."
    assert row["elapsed_ms"] > 0
    assert "prompt_tokens" not in row
    assert "completion_tokens" not in row
    # The meter falls back to the estimate when the server gave nothing.
    assert stored["context"]["prompt_estimated"] == 1
    today = client.get("/api/usage/today").json()
    assert today["prompt_tokens"] == 0
    assert today["completion_tokens"] == 0
