"""Regressions from the v0.2.3 real-model QA."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from easyagent.app import _apply_run, create_app
from easyagent.journal import Entry, _prediction_entries, _promise_entries, commit_entries, run_pass
from easyagent.learn import note_ledger, observe, reject_candidate, rollback_latest, save_candidate
from easyagent.llm import YieldLater, clear_llama_cache, extract_json_text
from easyagent.mascot import PALETTE
from easyagent.store import Store, StoreError
from easyagent.tools import ToolRequest, _run_memory

WHEN = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)
CANDIDATE = (
    '{"name":"missed-reply","trigger":"when a reply is marked down","steps":["Say what was wrong."],'
    '"pitfalls":[],"scope":"this bot","check":{"kind":"command","command":"false","exit_code":0}}'
)
STATE_COLORS = {"#2a6fdb", "#5c4d9a", "#8f2d28", "#d0892a", "#3d7ea6"}


def _msg(mid, role, content, stamp="2026-10-08T03:00:00+00:00"):
    return {"id": mid, "role": role, "content": content, "created_at": stamp}


def test_bot_colors_avoid_the_state_colors():
    assert len(PALETTE) == len(set(PALETTE))
    assert not set(PALETTE) & STATE_COLORS
    assert "#d0892a" != "#3d7ea6"


def test_a_new_run_clears_the_old_stop_reason():
    chat = {
        "run": {
            "id": "old",
            "status": "stopped",
            "started_at": "2026-10-08T00:00:00+00:00",
            "last_activity_at": "2026-10-08T00:00:00+00:00",
            "current_step": "Thinking",
            "reason": "a new message was sent in this chat",
        }
    }
    run = _apply_run(chat, status="running", step="Waiting on model", start=True)
    assert run["status"] == "running"
    assert run["reason"] == ""
    assert run["current_step"] == "Waiting on model"


def test_json_inside_a_think_block_and_a_fence_still_parses():
    raw = "<think>planning\n{not this}</think>\n```json\n" + CANDIDATE + "\n```"
    data = json.loads(extract_json_text(raw))
    assert data["name"] == "missed-reply"


def test_a_background_call_yields_instead_of_holding_the_slot():
    from easyagent import gate
    from easyagent import llm

    async def run():
        gate.reset_lanes()
        token = gate.bind_connection(
            {"id": "only", "name": "home", "max_parallel": 1, "base_url": "http://127.0.0.1:9/v1"},
            "Ada",
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold():
            permit = await gate.reserve()
            await permit.acquire()
            started.set()
            await release.wait()
            await permit.release()

        holder = asyncio.create_task(hold())
        await started.wait()
        try:
            with pytest.raises(YieldLater):
                await llm.complete(
                    base_url="http://127.0.0.1:9/v1",
                    api_key=None,
                    model="your-model",
                    messages=[{"role": "user", "content": "notes"}],
                    timeout=30,
                    tools=False,
                    yield_to_chats=True,
                )
        finally:
            release.set()
            await holder
            gate.reset_connection(token)
            gate.reset_lanes()

    asyncio.run(run())


def test_the_nightly_pass_yields_and_retries_later(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="home", base_url="http://127.0.0.1:9/v1", api_key=None, model="your-model")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="your-model")
    chat = store.create_chat(bot["id"])
    chat["messages"] = [_msg("m1", "assistant", "I will check the boiler tomorrow")]
    store.save_chat(chat)

    async def complete(**kwargs):
        raise YieldLater()

    monkeypatch.setattr("easyagent.llm.complete", complete)
    result = asyncio.run(run_pass(store, bot, now=WHEN, force=True))
    assert result["result"] == "skipped"
    assert "connection" in result["reason"]


def test_an_explicit_promise_is_kept(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="home", base_url="http://127.0.0.1:9/v1", api_key=None, model="your-model")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="your-model")
    chat = store.create_chat(bot["id"])
    chat["messages"] = [_msg("boiler", "assistant", "I will check the boiler tomorrow")]
    store.save_chat(chat)
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        assert kwargs.get("tools") is False
        prompt = kwargs["messages"][0]["content"]
        assert "Today is 2026-10-08" in prompt
        assert "timezone" in prompt
        assert "I will check the boiler tomorrow" in prompt
        assert "Do not call a tool" in prompt
        return "PROMISE|[m:boiler]|2026-10-08|open|I will check the boiler tomorrow"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    asyncio.run(run_pass(store, bot, now=WHEN, force=True))
    text = (store.root / "bots" / bot["id"] / "notes" / "PROMISES.md").read_text(encoding="utf-8")
    assert "I will check the boiler tomorrow" in text
    assert "status=open" in text
    assert calls


def test_a_title_is_scrubbed_before_it_is_cut(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="home", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="your-model")
    key = "sk-proj-abc_def123456"  # fake-key-fixture
    title = ("x" * 70) + " " + key
    messages = [_msg("m1", "user", "ceramic drawer note")]
    commit_entries(
        store,
        bot["id"],
        [Entry(kind="unknown", title=title, body=title, cites=["m1"], dates=["2026-10-08"], source="derived")],
        messages,
    )
    folder = store.root / "bots" / bot["id"] / "notes"
    blob = "\n".join(path.read_text(encoding="utf-8") for path in folder.glob("*.md"))
    assert key not in blob
    assert "sk-proj" not in blob
    assert "[redacted]" in blob


def test_a_prediction_is_the_bots_words_and_skips_an_empty_actual():
    rows = _prediction_entries(
        [
            _msg("u", "user", "please alphabetize the ceramic drawer labels tonight"),
            _msg("a", "assistant", ""),
            _msg("b", "assistant", "I expect the kiln to cool overnight"),
        ],
        0.5,
    )
    assert rows == []
    kept = _prediction_entries(
        [
            _msg("u", "user", "please alphabetize the ceramic drawer labels tonight"),
            _msg("b", "assistant", "I expect the kiln to cool overnight"),
            _msg("c", "user", "the kiln cooled"),
        ],
        0.5,
    )
    assert len(kept) == 1
    assert kept[0].body.startswith("Expected: I expect the kiln")
    assert "alphabetize" not in kept[0].body.split("Actual:")[0]


def test_memory_saves_are_scrubbed(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="home", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="your-model")
    key = "sk-test-FAKE123"  # fake-key-fixture
    _run_memory(
        store,
        ToolRequest(kind="memory", action="new", path="keys", body=f"my staging API key is {key}, remember it"),
        bot["id"],
    )
    blob = "\n".join(path.read_text(encoding="utf-8") for path in (store.root / "bots" / bot["id"] / "memory").glob("*.txt"))
    assert key not in blob
    assert "[redacted]" in blob


def test_a_second_rollback_says_there_is_nothing_left(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="home", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="your-model")
    from easyagent.learn import ledger_rows, _mark_rolled

    for row in ledger_rows(store, bot["id"]):
        _mark_rolled(store, row["id"])
    note_ledger(store, kind="skill", key="kiln-note", previous="Before.\n", bot_id=bot["id"])
    first = rollback_latest(store, bot["id"])
    assert first["restored"] is True
    assert "Before." in (store.skills_dir / "kiln-note.md").read_text(encoding="utf-8")
    with pytest.raises(StoreError) as caught:
        rollback_latest(store, bot["id"])
    assert caught.value.status == 404
    assert "nothing" in str(caught.value).lower() or "nothing" in caught.value.message.lower()


def test_a_waiting_candidate_can_be_rejected(tmp_path):
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "home", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    store = client.app.state.store
    saved = save_candidate(
        store,
        bot["id"],
        {
            "name": "missed-reply",
            "trigger": "when a reply is marked down",
            "steps": ["Say what was wrong."],
            "pitfalls": [],
            "scope": "this bot",
            "check": {"kind": "command", "command": "false", "exit_code": 0},
        },
    )
    rejected = client.post(f"/api/bots/{bot['id']}/learning/reject/{saved['id']}")
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert all(item.get("id") != saved["id"] for item in body["waiting"])
    assert any(item.get("id") == saved["id"] and "rejected" in (item.get("reason") or "").lower() for item in body["rejected"])
    assert not (tmp_path / "skills" / "missed-reply.md").is_file()
    with pytest.raises(StoreError):
        reject_candidate(store, store.get_bot(bot["id"]), saved["id"])


class _Llama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bodies: list[dict] = []

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/props":
            self._json({"default_generation_settings": {"temp": 0.8}, "total_slots": 1, "model_path": "your-model.gguf"})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        body = json.loads(raw.decode() or "{}")
        _Llama.bodies.append(body)
        blob = json.dumps(body)
        if body.get("tools") and (body.get("response_format") or body.get("grammar")):
            self._error("failed to parse grammar")
            return
        if "Propose one skill" in blob:
            if body.get("tools") or body.get("response_format") or body.get("grammar"):
                self._error("failed to parse grammar")
                return
            wrapped = "<think>planning the skill</think>\n```json\n" + CANDIDATE + "\n```"
            self._json({"choices": [{"message": {"content": wrapped}}]})
            return
        if "Today is" in blob:
            if "tools" in body:
                self._error("failed to parse grammar")
                return
            self._json({"choices": [{"message": {"content": ""}}]})
            return
        self._sse([{"choices": [{"delta": {"content": "The sky is green."}}]}])

    def _error(self, detail: str) -> None:
        payload = json.dumps({"error": {"message": detail}}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self, events: list[dict]) -> None:
        chunks = [f"data: {json.dumps(event)}\n\n".encode() for event in events]
        chunks.append(b"data: [DONE]\n\n")
        raw = b"".join(chunks)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:
        return


def test_llama_cpp_corrections_thumbs_and_nightly_each_make_a_gated_candidate(tmp_path, monkeypatch):
    from easyagent.learn import clear_stop

    monkeypatch.setenv("EASYAGENT_LEARN", "1")
    clear_stop()
    clear_llama_cache()
    _Llama.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Llama)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = TestClient(create_app(tmp_path))
        endpoint = client.post(
            "/api/endpoints",
            json={"name": "Local model", "base_url": f"http://127.0.0.1:{port}/v1", "model": "your-model", "max_parallel": 1},
        ).json()
        bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"], "model": "your-model"}).json()
        chat = client.post(f"/api/bots/{bot['id']}/chats").json()
        first = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages", json={"content": "what color is the sky"})
        assert first.status_code == 200, first.text
        second = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": "that's not right, I meant blue"},
        )
        assert second.status_code == 200, second.text
        answer = first.json()["chat"]["messages"][-1]
        thumb = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/{answer['id']}/reaction",
            json={"emoji": "👎"},
        )
        assert thumb.status_code == 200, thumb.text
        store = client.app.state.store
        observe(store, bot["id"], "The kiln reply was wrong and had to be redone.", reason="nightly", task="Check the kiln")
        passed = asyncio.run(run_pass(store, store.get_bot(bot["id"]), now=WHEN, force=True))
        assert passed.get("result") != "skipped"
        waiting = client.get(f"/api/bots/{bot['id']}/learning").json()["waiting"]
        reasons = sorted((item.get("source") or {}).get("reason") for item in waiting)
        assert reasons == ["correction", "nightly", "thumb"]
        assert all(item["status"] == "candidate" for item in waiting)
        proposals = [body for body in _Llama.bodies if "Propose one skill" in json.dumps(body)]
        assert len(proposals) >= 3
        for body in proposals:
            assert "tools" not in body
            assert "response_format" not in body
            assert "grammar" not in body
        assert not any(body.get("tools") and (body.get("response_format") or body.get("grammar")) for body in _Llama.bodies)
        slept = client.post(f"/api/bots/{bot['id']}/learning/sleep")
        assert slept.status_code == 200, slept.text
        assert len(slept.json()["rejected"]) == 3
        assert client.get(f"/api/bots/{bot['id']}/learning").json()["waiting"] == []
        assert not (tmp_path / "skills" / "missed-reply.md").is_file()
    finally:
        server.shutdown()
        clear_llama_cache()


class _Slow(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    release = threading.Event()
    late = False

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if b"EASYAGENT_CHECK_V1" in raw:
            payload = json.dumps({"choices": [{"message": {"content": '{"pass": true, "problems": [], "fix_hint": ""}'}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(obj: dict) -> None:
            piece = f"data: {json.dumps(obj)}\n\n".encode()
            self.wfile.write(f"{len(piece):X}\r\n".encode() + piece + b"\r\n")
            self.wfile.flush()

        parts = ["facts:", "\n", "1", ". key sk-test-FA", "KE123", "\n", "2", ". a PROM", "ISE", "S.md"]  # fake-key-fixture
        for part in parts:
            send({"choices": [{"delta": {"reasoning_content": part}}]})
        send({"choices": [{"delta": {"content": "Hello "}}]})
        if not _Slow.release.wait(2):
            _Slow.late = True
        send({"choices": [{"delta": {"content": "there."}}]})
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, fmt: str, *args) -> None:
        return


def _events(body: str) -> list[dict]:
    found = []
    for block in body.split("\n\n"):
        data = "\n".join(line[5:].strip() for line in block.split("\n") if line.startswith("data:"))
        if not data:
            continue
        found.append(json.loads(data))
    return found


def test_thinking_stays_byte_exact_and_answer_deltas_arrive_live(tmp_path, monkeypatch):
    import easyagent.app as appmod

    monkeypatch.setenv("EASYAGENT_CHECK", "0")
    _Slow.release.clear()
    original = appmod._sse

    def _sse(payload):
        if payload.get("type") == "delta" and str(payload.get("text") or "").startswith("Hello"):
            _Slow.release.set()
        return original(payload)

    monkeypatch.setattr(appmod, "_sse", _sse)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = TestClient(create_app(tmp_path))
        endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": f"http://127.0.0.1:{port}/v1"}).json()
        bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
        chat = client.get(f"/api/bots/{bot['id']}/ongoing").json()
        url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
        seen = ""
        with client.stream("POST", url, json={"content": "say hello"}, headers={"Accept": "text/event-stream"}) as response:
            assert response.status_code == 200
            for piece in response.iter_text():
                seen += piece
        # The model held "there." until the first answer delta was written.
        # The test client may still deliver both chunks in one read.
        assert _Slow.late is False
        events = _events(seen)
        deltas = [event.get("text") for event in events if event.get("type") == "delta"]
        assert "Hello " in deltas
        assert "there." in deltas
        thought = "".join(str(event.get("text") or "") for event in events if event.get("type") == "thinking")
        exact = "facts:\n1. key sk-test-FAKE123\n2. a PROMISES.md"  # fake-key-fixture
        assert thought == exact
        assert "FA KE" not in thought
        assert "PROM ISE" not in thought
        stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
        assert stored["messages"][-1]["thinking"] == exact
    finally:
        _Slow.release.set()
        server.shutdown()


def test_promises_match_the_boiler_sentence():
    rows = _promise_entries([_msg("boiler", "assistant", "I will check the boiler tomorrow")])
    assert len(rows) == 1
    assert rows[0].status == "open"
    assert "boiler" in rows[0].body
