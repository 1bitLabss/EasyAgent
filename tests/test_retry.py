"""A flaky model server is retried. Stop still stops. A tool that already ran stays run."""

import asyncio
import random
import time
from pathlib import Path

import pytest

from easyagent import gate
from easyagent import llm
from easyagent import retry
from easyagent import turn as turn_mod
from easyagent.app import create_app
from easyagent.llm import ProviderError
from tests.test_gate import _pair, _who


def test_backoff_grows_then_caps_at_15_seconds():
    rng = random.Random(0)
    delays = [retry.delay_for(attempt, rng) for attempt in range(1, 8)]
    bases = (1, 2, 4, 8, 15, 15, 15)
    for delay, base in zip(delays, bases):
        assert base <= delay <= 15
        assert delay <= base * 1.25 + 1e-9
    assert delays[-1] == 15
    assert retry.gave_up("Could not reach x", 180) == "Could not reach x Retried for 3m."
    assert retry.gave_up("down", 12) == "down Retried for 12s."
    assert retry.gave_up("down", 75) == "down Retried for 1m 15s."


def test_retry_budget_comes_from_the_environment(monkeypatch):
    assert retry.budget_seconds() == 120
    monkeypatch.setenv("EASYAGENT_MODEL_RETRY_SECONDS", "9")
    assert retry.budget_seconds() == 9
    monkeypatch.setenv("EASYAGENT_MODEL_RETRY_SECONDS", "nope")
    assert retry.budget_seconds() == 120
    monkeypatch.setenv("EASYAGENT_MODEL_RETRY_SECONDS", "-4")
    assert retry.budget_seconds() == 0


@pytest.mark.parametrize(
    ("text", "again"),
    [
        ("Could not reach http://127.0.0.1:9/v1: All connection attempts failed", True),
        ("Could not reach http://127.0.0.1:9/v1: Connection refused", True),
        ("Timed out calling http://127.0.0.1:9/v1", True),
        ("502 from http://127.0.0.1:9/v1: bad gateway", True),
        ("503 from http://127.0.0.1:9/v1: unavailable", True),
        ("504 from http://127.0.0.1:9/v1: gateway timeout", True),
        ("500 from http://127.0.0.1:9/v1: server busy", True),
        ("429 from http://127.0.0.1:9/v1: slot unavailable", True),
        ("500 from http://127.0.0.1:9/v1: exploded", False),
        ("400 from http://127.0.0.1:9/v1: bad", False),
        ("Endpoint returned an empty message.", False),
        (
            "Could not reach http://127.0.0.1:9/v1: peer closed connection (incomplete chunked read)",
            False,
        ),
    ],
)
def test_which_failures_are_retried_before_the_first_token(text, again):
    assert retry.retryable_before_token(text) is again


def test_a_mid_stream_abort_is_not_replayed():
    detail = "Could not reach http://127.0.0.1:9/v1: incomplete chunked read"
    assert retry.midstream_drop(detail) is False
    assert retry.midstream_drop("Could not reach http://127.0.0.1:9/v1: Connection reset by peer") is True


def test_connect_timeout_is_separate_from_the_long_read():
    streaming = llm._http_timeout(120, stream=True)
    assert streaming.connect == retry.CONNECT_TIMEOUT
    assert streaming.read is None
    assert streaming.write == 120
    whole = llm._http_timeout(120, stream=False)
    assert whole.connect == retry.CONNECT_TIMEOUT
    assert whole.read == 120


def test_pause_notices_stop_without_waiting_out_the_backoff():
    async def run():
        event = asyncio.Event()
        token = turn_mod._cancel.set(event)

        async def trip():
            await asyncio.sleep(0.08)
            event.set()

        task = asyncio.create_task(trip())
        try:
            started = time.monotonic()
            with pytest.raises(turn_mod.TurnCancelled):
                await retry._sleep(30)
            assert time.monotonic() - started < 1
        finally:
            turn_mod._cancel.reset(token)
            task.cancel()

    asyncio.run(run())


def test_headers_that_never_arrive_time_out_before_the_body():
    """Connect is allowed. Silence before the first byte is a timeout, and it is one attempt when the caller retries."""

    async def run():
        handlers: list[asyncio.Task] = []

        async def handle(reader, writer):
            handlers.append(asyncio.current_task())
            try:
                await reader.read(64)
                await asyncio.sleep(30)
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        token = gate.suppress_inner_retry()
        try:
            async for _piece in llm.stream_complete(
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key=None,
                model=None,
                messages=[{"role": "user", "content": "hi"}],
                timeout=0.4,
            ):
                pass
        finally:
            gate.restore_inner_retry(token)
            for task in handlers:
                task.cancel()
            server.close()
            await server.wait_closed()

    with pytest.raises(ProviderError) as caught:
        asyncio.run(run())
    assert "Timed out calling" in str(caught.value)
    assert "Retried for" not in str(caught.value)


def test_a_flaky_connect_is_shown_as_reconnecting_and_then_answers(tmp_path, monkeypatch):
    gate.reset_lanes()
    calls = {"n": 0}

    async def reply(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("Could not reach http://127.0.0.1:9/v1: All connection attempts failed")
        yield "back"

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, _endpoint, bot_a, _bot_b, chat_a, _chat_b = await _pair(tmp_path, parallel=1)
        try:
            sent = await client.post(
                f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages",
                json={"content": "PING-A"},
                headers={"Accept": "text/event-stream"},
            )
            stored = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
        finally:
            await client.aclose()
        assert sent.status_code == 200, sent.text
        assert "Model not answering, retrying (attempt 2)" in sent.text
        assert "Model not answering, retrying (attempt 3)" in sent.text
        assert stored["messages"][-1]["content"] == "back"
        assert calls["n"] == 3

    asyncio.run(scenario())


def test_stop_during_the_retry_pause_does_not_call_the_model_again(tmp_path, monkeypatch):
    gate.reset_lanes()
    calls = {"n": 0}

    async def reply(**kwargs):
        calls["n"] += 1
        raise ProviderError("Could not reach http://127.0.0.1:9/v1: All connection attempts failed")
        yield ""

    async def pause(_seconds):
        slot = turn_mod.current_slot()
        assert slot is not None
        slot.reason = "you pressed Stop"
        slot.user_stop = True
        slot.cancel.set()
        turn_mod.raise_if_cancelled()

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)
    monkeypatch.setattr("easyagent.retry.pause", pause)

    async def scenario():
        client, _endpoint, bot_a, _bot_b, chat_a, _chat_b = await _pair(tmp_path, parallel=1)
        try:
            sent = await client.post(
                f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages",
                json={"content": "PING-A"},
                headers={"Accept": "text/event-stream"},
            )
            stored = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
        finally:
            await client.aclose()
        assert sent.status_code == 200, sent.text
        assert calls["n"] == 1
        assert stored["messages"][-1]["content"] == "Stopped: you pressed Stop"
        assert stored["run"]["status"] == "stopped"
        assert stored["messages"][-1].get("error") is not True

    asyncio.run(scenario())


def test_retry_releases_the_slot_so_another_chat_can_run(tmp_path, monkeypatch):
    gate.reset_lanes()
    released = asyncio.Event()
    continue_a = asyncio.Event()
    a_calls = {"n": 0}

    async def reply(**kwargs):
        who = _who(kwargs.get("messages"))
        if who == "A":
            a_calls["n"] += 1
            if a_calls["n"] == 1:
                raise ProviderError(
                    "Could not reach http://127.0.0.1:9/v1: All connection attempts failed"
                )
            yield "Answer A"
            return
        yield "Answer B"

    async def pause(_seconds):
        released.set()
        await continue_a.wait()

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)
    monkeypatch.setattr("easyagent.retry.pause", pause)

    async def scenario():
        client, endpoint, bot_a, bot_b, chat_a, chat_b = await _pair(tmp_path, parallel=1)
        headers = {"Accept": "text/event-stream"}

        async def collect(bot, chat, content):
            url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
            async with client.stream("POST", url, json={"content": content}, headers=headers) as response:
                body = (await response.aread()).decode()
                return response.status_code, body

        try:
            first = asyncio.create_task(collect(bot_a, chat_a, "PING-A"))
            await asyncio.wait_for(released.wait(), timeout=5)
            lane = gate._lane(endpoint["id"])
            assert lane.holders == []
            status_b, body_b = await asyncio.wait_for(collect(bot_b, chat_b, "PING-B"), timeout=5)
            continue_a.set()
            status_a, body_a = await asyncio.wait_for(first, timeout=5)
        finally:
            continue_a.set()
            await client.aclose()
        assert status_b == 200 and "Answer B" in body_b
        assert status_a == 200 and "Answer A" in body_a
        assert a_calls["n"] == 2

    asyncio.run(scenario())


def test_a_dropped_stream_is_replaced_and_the_tool_is_not_run_again(tmp_path, monkeypatch):
    gate.reset_lanes()
    folder = tmp_path.parent / f"ea-retry-{tmp_path.name}"
    folder.mkdir()
    path = folder / "note.txt"
    fence = f"```files\nwrite\n{path}\nhello-once\n```"
    calls = {"n": 0}
    wrote = []

    async def reply(**kwargs):
        calls["n"] += 1
        messages = kwargs.get("messages") or []
        blob = "\n".join(item.get("content") or "" for item in messages if item.get("role") == "tool")
        wrote.append(blob.count("Wrote "))
        if calls["n"] == 1:
            yield fence
            return
        if calls["n"] == 2:
            yield "partial answer that must not stick"
            raise ProviderError("Could not reach http://127.0.0.1:9/v1: All connection attempts failed")
        yield "The note is written."

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    from fastapi.testclient import TestClient

    with TestClient(create_app(tmp_path)) as client:
        endpoint = client.post(
            "/api/endpoints",
            json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "max_parallel": 1},
        ).json()
        bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
        chat = client.post(f"/api/bots/{bot['id']}/chats").json()
        sent = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": "hello there"},
            headers={"Accept": "text/event-stream"},
        )
        assert sent.status_code == 200, sent.text
        stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
        body = sent.text
    assert path.read_text(encoding="utf-8") == "hello-once"
    assert wrote[0] == 0
    assert wrote[1:]
    assert all(count == 1 for count in wrote[1:])
    assert calls["n"] >= 3
    text = stored["messages"][-1]["content"]
    transcript = "\n".join(item.get("content") or "" for item in stored["messages"])
    assert "partial answer" not in text
    assert "partial answer" not in transcript
    assert "note.txt" in text
    assert "Model not answering, retrying (attempt" in body
    assert '"type": "replay"' in body


def test_the_host_in_the_harness_sentence_has_no_secret():
    assert retry.host_of("http://127.0.0.1:9/v1") == "127.0.0.1:9"
    assert retry.host_of("https://example.com/v1") == "example.com"
    assert retry.host_of("http://token:secret@127.0.0.1:9/v1") == "127.0.0.1:9"
    assert (
        retry.unreachable_text("http://127.0.0.1:9/v1")
        == "Can't reach the model at 127.0.0.1:9. Check the connection or address."
    )
    assert retry.model_explained_harness_failure("The tool calls timed out and were not run.")
    assert not retry.model_explained_harness_failure(retry.unreachable_text("http://127.0.0.1:9/v1"))
    assert not retry.model_explained_harness_failure("The page is ready.")


def test_unreachable_time_is_shared_across_model_calls(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(retry, "now", lambda: clock["t"])
    retry.reset_unreachable()
    detail = "Timed out calling http://127.0.0.1:9/v1"
    first = retry.Window(budget=10, base_url="http://127.0.0.1:9/v1")
    first.begin_attempt()
    clock["t"] = 6
    assert first.plan(detail, started=False) == "again"
    first.arm("again", detail)
    clock["t"] = 10
    assert first.plan(detail, started=False) is None
    assert first.failure_message(detail) == "Can't reach the model at 127.0.0.1:9. Check the connection or address."
    second = retry.Window(budget=10, base_url="http://127.0.0.1:9/v1")
    assert second.attempt_limit(120) == 0
    assert second.plan(detail, started=False) is None
    retry.reset_unreachable()


def test_a_dead_model_stops_in_the_budget_and_the_model_does_not_explain_it(tmp_path, monkeypatch):
    """The invented 'timed out and not run' line is not the reply. The harness is."""
    gate.reset_lanes()
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    async def pause(seconds):
        clock["t"] += float(seconds or 0)

    monkeypatch.setattr("easyagent.retry.now", now)
    monkeypatch.setattr("easyagent.retry.pause", pause)
    monkeypatch.setenv("EASYAGENT_MODEL_RETRY_SECONDS", "10")
    calls = {"n": 0}
    seen: list = []

    async def reply(**kwargs):
        calls["n"] += 1
        seen.append(kwargs.get("messages") or [])
        clock["t"] += 6
        if calls["n"] == 1:
            yield "The tool calls timed out and were not run."
        raise ProviderError("Timed out calling http://127.0.0.1:9/v1")

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, _endpoint, bot_a, _bot_b, chat_a, _chat_b = await _pair(tmp_path, parallel=1)
        try:
            sent = await client.post(
                f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages",
                json={"content": "PING-A"},
                headers={"Accept": "text/event-stream"},
            )
            body = sent.text
            stored = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
        finally:
            await client.aclose()
        return sent.status_code, body, stored

    status, body, stored = asyncio.run(scenario())
    assert status == 200, body
    assert calls["n"] <= 3
    last = stored["messages"][-1]
    assert last["content"] == "Can't reach the model at 127.0.0.1:9. Check the connection or address."
    assert last["kind"] == "connection"
    assert last["error"] is True
    assert "not run" not in last["content"].lower()
    assert "timed out" not in last["content"].lower()
    assert stored["run"]["status"] == "error"
    assert "Model not answering, retrying" in body
    for messages in seen:
        for item in messages:
            if item.get("role") != "user":
                continue
            text = item.get("content") or ""
            assert "not run" not in text.lower()
            assert "explain" not in text.lower()
    root = Path(__file__).resolve().parents[1]
    react = (root / "web/src/components/ChatPane.tsx").read_text(encoding="utf-8")
    classic = (root / "easyagent/static/app.js").read_text(encoding="utf-8")
    assert 'data-testid="open-connection-settings"' in react
    assert 'setScreen("connections")' in react
    assert '"open-connection-settings"' in classic
    assert 'openPlainScreen("connections")' in classic


def test_a_model_excuse_after_a_retry_is_replaced_by_the_harness(tmp_path, monkeypatch):
    gate.reset_lanes()
    calls = {"n": 0}

    async def reply(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("Timed out calling http://127.0.0.1:9/v1")
        yield "The tool calls timed out and were not run."

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)

    async def scenario():
        client, _endpoint, bot_a, _bot_b, chat_a, _chat_b = await _pair(tmp_path, parallel=1)
        try:
            sent = await client.post(
                f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}/messages",
                json={"content": "PING-A"},
                headers={"Accept": "text/event-stream"},
            )
            stored = (await client.get(f"/api/bots/{bot_a['id']}/chats/{chat_a['id']}")).json()
        finally:
            await client.aclose()
        return sent.status_code, sent.text, stored

    status, body, stored = asyncio.run(scenario())
    assert status == 200, body
    assert calls["n"] == 2
    last = stored["messages"][-1]
    assert last["content"] == "Can't reach the model at 127.0.0.1:9. Check the connection or address."
    assert last["kind"] == "connection"
    assert "not run" not in last["content"].lower()
