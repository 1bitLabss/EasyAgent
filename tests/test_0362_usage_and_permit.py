"""Three verified bugs from the flagship verification run.

1. ``permit.abandon`` crash: when ``gate.reserve()`` hands back an ``_Open``
   permit (no lane bound, or already inside a held slot), the cleanup in
   ``_pull_model``'s finally block called ``permit.abandon()`` — a method only
   ``_Seat`` had. The AttributeError killed the whole turn and the reply was
   lost. The fake model times out after acquiring to drive that path.

2. Sync-path usage: the non-SSE POST /messages path ran ``llm.complete``
   without binding purpose "chat", so the call logged as purpose=model,
   ``note_turn_usage`` was skipped, and the saved row carried only
   elapsed_ms while /api/usage/today stayed 0.

3. Over-eager usage-silent memory: a stream interrupted mid-read (line cap
   cut while lines were still arriving, or a timeout handed in from outside)
   permanently marked the server usage-silent for the process lifetime.
   Only a clean finish with the server silent about usage may mark it.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from easyagent import gate, llm, tools
from easyagent import turn as turn_mod
from easyagent.app import create_app
from easyagent.features import reset_routine_turn
from easyagent.generation import GenerationLimit, Limits, _limits

BASE = "http://127.0.0.1:9/v1"


def fresh_turn_context() -> None:
    """Direct calls bind no slot. Clear what an earlier test left behind."""
    turn_mod.renew_cancel()
    reset_routine_turn()


def _chunk(delta=None, finish=None):
    return "data: " + json.dumps(
        {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    )


# ---------------------------------------------------------------- bug 1


def test_open_permit_honors_the_permit_interface():
    """Every permit kind must answer to abandon(); _Open held nothing but had
    to carry the method so finally-block cleanup cannot crash."""

    async def go():
        permit = await gate.reserve()
        assert isinstance(permit, gate._Open)
        await permit.acquire()
        permit.abandon()  # AttributeError before the fix
        await permit.release()

    asyncio.run(go())


def test_timeout_while_open_permit_keeps_its_error(monkeypatch):
    """A model call that times out after acquiring must surface the timeout,
    not an AttributeError from permit cleanup on the _Open fast path."""
    fresh_turn_context()

    async def dying_complete(**kwargs):
        raise GenerationLimit("stall")

    monkeypatch.setattr(llm, "complete", dying_complete)

    async def go():
        events = []
        with pytest.raises(GenerationLimit):
            async for event in tools._pull_model(
                stream=False,
                base_url=BASE,
                api_key=None,
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            ):
                events.append(event)
        return events

    asyncio.run(go())


# ---------------------------------------------------------------- bug 2


class _PostResponse:
    status_code = 200
    headers = {"content-type": "application/json"}
    text = "{}"

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Nine boats.",
                        "finish_reason": "stop",
                    }
                }
            ],
            "usage": {"prompt_tokens": 777, "completion_tokens": 33},
        }


class _NotFound:
    status_code = 404
    headers = {"content-type": "application/json"}

    async def aread(self):
        return b"{}"

    def json(self):
        return {}


class _SyncServer:
    """Stands in for httpx.AsyncClient. POST answers one completion."""

    def __init__(self):
        self.posts = 0

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
        self.posts += 1
        return _PostResponse()


def test_sync_message_stamps_usage(tmp_path, monkeypatch):
    """The non-SSE send must stamp the row and the today rollup like SSE does."""
    server = _SyncServer()
    monkeypatch.setattr(llm.httpx, "AsyncClient", server)
    llm.reset_stream_options_cache()
    try:
        client = TestClient(create_app(tmp_path))
        endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": BASE}).json()
        bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
        chat = client.post(f"/api/bots/{bot['id']}/chats").json()
        response = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages", json={"content": "how many boats"}
        )
        assert response.status_code == 200, response.text
        stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
        row = stored["messages"][-1]
        assert row["role"] == "assistant"
        assert row["content"] == "Nine boats."
        assert row.get("prompt_tokens") == 777
        assert row.get("completion_tokens") == 33
        assert row.get("elapsed_ms", 0) > 0
        today = client.get("/api/usage/today").json()
        assert today["prompt_tokens"] == 777
        assert today["completion_tokens"] == 33
    finally:
        llm.reset_stream_options_cache()


# ---------------------------------------------------------------- bug 3


class _HangAfter:
    """Replays scripted lines, then hangs (the reply never closes)."""

    def __init__(self, lines):
        self.lines = list(lines)

    def client(self):
        outer = self

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def aiter_lines(self):
                for line in outer.lines:
                    yield line
                await asyncio.sleep(30)
                yield "data: [DONE]"

            async def aread(self):
                return b""

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def aclose(self):
                return None

            def stream(self, method, url, json=None, headers=None):
                class Context:
                    async def __aenter__(self_inner):
                        return FakeResponse()

                    async def __aexit__(self_inner, *args):
                        return False

                return Context()

        return FakeClient


def _drain_attempt(payload_log=None):
    async def go():
        parts = []
        async for piece in llm._stream_attempt(
            base_url=BASE, api_key=None, model="m", messages=[{"role": "user", "content": "x"}], timeout=5
        ):
            parts.append(piece)
        return "".join(parts)

    return asyncio.run(go())


def test_line_capped_mid_stream_death_does_not_silence_server(monkeypatch):
    """Lines still arriving after finish_reason mean the stream was mid-flight
    when the grace's line cap cut it. That is an interruption, not a server
    silent about usage — the server must keep being asked."""
    lines = [_chunk({"content": "Hello."}, finish="stop")]
    lines += [_chunk({"content": f"still {n} streaming."}) for n in range(20)]
    lines.append("data: [DONE]")
    monkeypatch.setattr(llm.httpx, "AsyncClient", _HangAfter(lines).client())
    fresh_turn_context()
    llm.reset_stream_options_cache()
    try:
        text = _drain_attempt()
        assert "still 0 streaming." in text
        assert llm.stream_options_supported(BASE) is True
    finally:
        llm.reset_stream_options_cache()


def test_mid_stream_hang_without_finish_does_not_silence_server(monkeypatch):
    """A stream that dies before any finish_reason is an interrupted stream."""
    lines = [_chunk({"content": "Half a"})]
    monkeypatch.setattr(llm.httpx, "AsyncClient", _HangAfter(lines).client())
    fresh_turn_context()
    token = _limits.set(
        Limits(max_tokens=256, stall_seconds=0.1, first_token_seconds=0.1, wall_seconds=10, hard_seconds=20)
    )
    llm.reset_stream_options_cache()
    try:
        with pytest.raises(GenerationLimit):
            _drain_attempt()
        assert llm.stream_options_supported(BASE) is True
    finally:
        _limits.reset(token)
        llm.reset_stream_options_cache()


def test_clean_usage_less_completion_silences_server(monkeypatch):
    """finish_reason then silence: the server completed cleanly and stayed
    silent about usage — it is remembered and never asked again."""
    lines = [_chunk({"content": "Plain."}), _chunk(finish="stop")]
    monkeypatch.setattr(llm.httpx, "AsyncClient", _HangAfter(lines).client())
    fresh_turn_context()
    token = _limits.set(
        Limits(max_tokens=256, stall_seconds=5, first_token_seconds=5, wall_seconds=10, hard_seconds=20)
    )
    llm.reset_stream_options_cache()
    try:
        text = _drain_attempt()
        assert text == "Plain."
        assert llm.stream_options_supported(BASE) is False
    finally:
        _limits.reset(token)
        llm.reset_stream_options_cache()
