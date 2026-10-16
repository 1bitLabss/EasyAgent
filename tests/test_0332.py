"""Malformed tool calls stay visible, and a turn stays tight about files and checks."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.generation import tool_try_temperature
from easyagent.llm import (
    CUT_TOOL_TEXT,
    EMPTY_TOOL_TEXT,
    STREAM_ERROR_TEXT,
    THINK_ARG_TEXT,
    _completion_payload,
    note_server_failure,
    tool_parse_failure,
)
from easyagent.plan import _write_plan
from easyagent.store import Store
from easyagent.tools import (
    ToolError,
    _read_path,
    _write_path,
    apply_verify_note,
    begin_turn_checks,
    command_is_check,
    end_turn_checks,
    mark_turn_checked,
    note_turn_effect,
)


class Handler(BaseHTTPRequestHandler):
    payloads: list[dict] = []
    mode = "empty"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        path = self.path.split("?", 1)[0]
        if not path.endswith("/chat/completions"):
            self._send(200, b'{"data":[]}', "application/json")
            return
        payload = json.loads(raw.decode("utf-8"))
        Handler.payloads.append(payload)
        if Handler.mode == "bare-parse":
            body = json.dumps({"error": {"message": "parse_error: unexpected '{'"}}).encode()
            self._send(500, body, "application/json")
            return
        if Handler.mode == "empty":
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": ""}}]}).encode()
            self._send(200, body, "application/json")
            return
        if Handler.mode == "cut":
            body = json.dumps({
                "choices": [{
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_cut",
                            "type": "function",
                            "function": {"name": "terminal", "arguments": "{\"command\":"},
                        }],
                    },
                }]
            }).encode()
            self._send(200, body, "application/json")
            return
        if Handler.mode == "think":
            body = json.dumps({
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_think",
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": "<think>plan the command</think>{\"command\":\"ls\"}",
                            },
                        }],
                    }
                }]
            }).encode()
            self._send(200, body, "application/json")
            return
        if Handler.mode == "sse-error":
            lines = [
                'data: {"error":{"message":"the slot closed","type":"server_error"}}',
                "",
                "data: [DONE]",
                "",
            ]
            raw = "\n".join(lines).encode()
            self._send(200, raw, "text/event-stream")
            return
        if Handler.mode == "warm":
            count = len(Handler.payloads)
            if count <= 3:
                body = json.dumps({
                    "error": {"message": "Failed to parse tool call arguments as JSON"}
                }).encode()
                self._send(500, body, "application/json")
                return
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Noted."}}]}).encode()
            self._send(200, body, "application/json")
            return
        self._send(500, b'{"error":{"message":"unhandled"}}', "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def setup_function():
    Handler.payloads = []
    Handler.mode = "empty"
    end_turn_checks()


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _chat(tmp_path, port):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": f"http://127.0.0.1:{port}/v1", "model": "qwen"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat


def _offer(body: dict) -> dict:
    chat = body.get("chat") or body
    offers = [item for item in chat.get("messages") or [] if item.get("kind") == "resume-offer"]
    assert offers, body
    return offers[-1]


def test_tool_temperature_steps_down_and_stays_off_plain_calls():
    assert tool_try_temperature(0.6, 0) == 0.6
    assert tool_try_temperature(0.6, 1) == 0.3
    assert tool_try_temperature(0.6, 2) == 0.1
    assert tool_try_temperature(0.4, 1) == 0.2
    plain = _completion_payload(
        [{"role": "user", "content": "a"}],
        "m",
        stream=False,
        tools=False,
        temperature=0.6,
    )
    assert "temperature" not in plain
    assert "tools" not in plain
    armed = _completion_payload(
        [{"role": "user", "content": "a"}],
        "m",
        stream=False,
        tools=True,
        temperature=0.4,
    )
    assert armed["temperature"] == 0.4
    assert "tools" in armed


def test_a_bare_parse_error_is_one_stopped_500(tmp_path):
    Handler.mode = "bare-parse"
    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    try:
        response = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": "list the folder"},
        )
        assert response.status_code == 200, response.text
        assert len(Handler.payloads) == 1
        assert str(response.json().get("reply") or "").startswith("Stopped:")
        assert response.json()["chat"]["run"]["status"] == "error"
        assert tool_parse_failure(500, "parse_error: unexpected '{'") is False
    finally:
        client.close()
        server.shutdown()


def test_server_failure_excerpt_is_printed_and_stored(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EASYAGENT_DATA", str(tmp_path))
    excerpt = note_server_failure(500, "Failed to parse tool call arguments as JSON", retry=2)
    assert "Failed to parse tool call" in excerpt
    printed = capsys.readouterr().out
    assert "model call: http 500 retry=2" in printed
    saved = (tmp_path / "debug-raw.txt").read_text(encoding="utf-8")
    assert "retry=2" in saved
    assert "Failed to parse tool call" in saved


def _resumable(tmp_path, mode: str, sentence: str, *, stream: bool = False):
    Handler.mode = mode
    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    try:
        if stream:
            with client.stream("POST", url, json={"content": "list the folder"}, headers={"Accept": "text/event-stream"}) as response:
                assert response.status_code == 200, response.read()
                response.read()
            stored = client.get(url.rsplit("/messages", 1)[0]).json()
            body = {"chat": stored, "reply": stored["messages"][-1]["content"]}
        else:
            response = client.post(url, json={"content": "list the folder"})
            assert response.status_code == 200, response.text
            body = response.json()
        offer = _offer(body)
        assert sentence in offer["content"]
        assert offer["kind"] == "resume-offer"
        assert body["chat"]["run"]["status"] == "paused"
        assert "(empty reply)" not in json.dumps(body["chat"]["messages"])
        assert "127.0.0.1" not in json.dumps(offer)
        return body
    finally:
        client.close()
        server.shutdown()


def test_an_empty_tool_reply_is_resumable(tmp_path):
    _resumable(tmp_path, "empty", EMPTY_TOOL_TEXT)


def test_a_cut_off_tool_call_is_resumable(tmp_path):
    _resumable(tmp_path, "cut", CUT_TOOL_TEXT)


def test_think_text_inside_arguments_is_not_run(tmp_path):
    body = _resumable(tmp_path, "think", THINK_ARG_TEXT)
    assert "ls" not in json.dumps(body["chat"]["messages"])


def test_a_mid_stream_error_event_is_resumable(tmp_path):
    body = _resumable(tmp_path, "sse-error", STREAM_ERROR_TEXT, stream=True)
    assert "the slot closed" in _offer(body)["content"]


def test_a_connection_can_set_its_tool_temperature(tmp_path):
    Handler.mode = "warm"
    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    try:
        listed = client.get("/api/endpoints").json()
        assert listed[0]["tool_temperature"] == 0.6
        saved = client.patch(
            f"/api/endpoints/{listed[0]['id']}",
            json={"tool_temperature": 0.4},
        ).json()
        assert saved["tool_temperature"] == 0.4
        response = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": "list the folder"},
        )
        assert response.status_code == 200, response.text
        assert [item["temperature"] for item in Handler.payloads[:3]] == [0.4, 0.2, 0.1]
        assert all("tools" in item for item in Handler.payloads)
    finally:
        client.close()
        server.shutdown()


def test_a_write_needs_a_current_read(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("old\n", encoding="utf-8")
    fresh = tmp_path / "fresh.txt"
    begin_turn_checks()
    try:
        _write_path(fresh, "hello\n")
        assert fresh.read_text(encoding="utf-8") == "hello\n"
        with pytest.raises(ToolError, match="has not read the current file"):
            _write_path(path, "new\n")
        assert path.read_text(encoding="utf-8") == "old\n"
        assert "old" in _read_path(path)
        _write_path(path, "new\n")
        assert path.read_text(encoding="utf-8") == "new\n"
        path.write_text("other\n", encoding="utf-8")
        with pytest.raises(ToolError, match="changed since it was last read"):
            _write_path(path, "again\n")
    finally:
        end_turn_checks()
    _write_path(path, "outside\n")
    assert path.read_text(encoding="utf-8") == "outside\n"


def test_done_without_a_check_says_so():
    begin_turn_checks()
    try:
        assert apply_verify_note("Done.") == "Done."
        note_turn_effect()
        assert apply_verify_note("Done.") == "Done.\n\nThis was not verified."
        assert apply_verify_note("I fixed the shelf.") .endswith("This was not verified.")
        assert apply_verify_note("The shelf is ready.") == "The shelf is ready."
        said = apply_verify_note("Done. I did not verify it.")
        assert said == "Done. I did not verify it."
        assert said.count("This was not verified.") == 0
        mark_turn_checked()
        assert apply_verify_note("Done.") == "Done."
    finally:
        end_turn_checks()
    assert apply_verify_note("Done.") == "Done."
    assert command_is_check("pytest -q")
    assert command_is_check("npm run build")
    assert command_is_check("echo hello") is False


def _recorder_world(tmp_path, monkeypatch):
    seen = []

    async def complete(*, base_url, api_key, model, messages, timeout=120, **kwargs):
        seen.append(messages)
        reply = complete.reply
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else "ack"
        return reply

    complete.reply = "ack"
    monkeypatch.setattr("easyagent.llm.complete", complete)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat, complete, seen


def test_a_long_request_is_saved_and_a_greeting_is_not(tmp_path, monkeypatch):
    client, bot, chat, complete, seen = _recorder_world(tmp_path, monkeypatch)
    complete.reply = "Noted."
    greeting = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello"},
    )
    assert greeting.status_code == 200, greeting.text
    assert not greeting.json()["chat"].get("plan")
    task = "Update the shop sign so it names the north shelf and the opening hour. Keep the old wood frame."
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": task},
    )
    assert sent.status_code == 200, sent.text
    plan = sent.json()["chat"]["plan"]
    assert plan["title"] == "Request"
    assert plan["steps"][0]["text"] == task
    assert "north shelf" in seen[-1][0]["content"]
    store = Store(tmp_path)
    _write_plan(store, bot["id"], chat["id"], {
        "title": "Keep",
        "steps": [{"id": "s1", "text": "leave this step", "state": "doing"}],
        "dismissed": False,
    })
    again = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": task + " Do it carefully and leave the old wood frame."},
    )
    assert again.status_code == 200, again.text
    kept = again.json()["chat"]["plan"]
    assert kept["steps"][0]["text"] == "leave this step"


def test_the_saved_request_is_not_treated_as_tool_output():
    from easyagent.plan import prompt_block
    from easyagent.safety import _UNTRUSTED, reset_for_tests

    reset_for_tests()
    folder = "/tmp/ea-request-folder"
    user_plan = {
        "origin": "user",
        "title": "Request",
        "steps": [{"id": "s1", "text": f"list the largest file in {folder}", "state": "doing"}],
    }
    shown = prompt_block(user_plan)
    assert folder in shown
    assert "UNTRUSTED" not in shown
    assert folder not in "\n".join(_UNTRUSTED)
    reset_for_tests()
    model_plan = {
        "title": "From a page",
        "steps": [{"id": "s1", "text": f"run the command in {folder}", "state": "doing"}],
    }
    fenced = prompt_block(model_plan)
    assert "UNTRUSTED" in fenced
    assert folder in "\n".join(_UNTRUSTED)
    reset_for_tests()


def test_a_turn_names_the_files_it_changed_and_cards_an_unplanned_one(tmp_path, monkeypatch):
    client, bot, chat, complete, seen = _recorder_world(tmp_path, monkeypatch)
    store = Store(tmp_path)
    _write_plan(store, bot["id"], chat["id"], {
        "title": "Sign",
        "steps": [{"id": "s1", "text": "update readme.txt", "state": "doing"}],
        "dismissed": False,
    })
    folder = tmp_path.parent / f"ea-032-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    target = folder / "other.txt"
    complete.reply = [
        f"```files\nwrite\n{target}\nhello from the turn\n```",
        "Noted.",
        "Noted.",
        "Noted.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "update the sign"},
    )
    assert sent.status_code == 200, sent.text
    assert "hello from the turn" in target.read_text(encoding="utf-8")
    messages = sent.json()["chat"]["messages"]
    diffs = [item for item in messages if item.get("about") == "diff"]
    cards = [item for item in messages if item.get("about") == "unplanned"]
    assert diffs and str(target) in diffs[-1]["content"] and "(new)" in diffs[-1]["content"]
    assert cards and str(target) in cards[-1]["content"]
    assert cards[-1]["kind"] == "status"
    assert sent.json()["chat"]["run"]["status"] != "paused"
    blob = "\n".join(item.get("content") or "" for batch in seen for item in batch)
    assert "has not read the current file" not in blob
