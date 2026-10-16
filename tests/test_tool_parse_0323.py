"""A llama.cpp 500 for two stuck-together tool calls is retried, then left resumable."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.llm import (
    MALFORMED_TOOL_TEXT,
    TOOL_PARSE_NUDGE,
    reset_parallel_tool_calls,
    tool_parse_failure,
)
from easyagent.tools import _requests_from_native, parse_tools

PARSE = json.dumps({
    "error": {
        "code": 500,
        "message": (
            "Failed to parse tool call arguments as JSON: "
            '{"command":"ls"}{"command":"pwd"} unexpected \'{\'; expected end of input'
        ),
        "type": "server_error",
    }
}).encode()
OK = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Noted."}}]}).encode()
DISK = b'{"error":{"code":500,"message":"disk blew up"}}'
UNKNOWN_FLAG = b'{"error":{"message":"unknown field parallel_tool_calls"}}'


class Handler(BaseHTTPRequestHandler):
    payloads: list[dict] = []
    mode = "recover"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        path = self.path.split("?", 1)[0]
        if not path.endswith("/chat/completions"):
            self._send(404, b'{"error":"not a completion"}')
            return
        payload = json.loads(raw.decode("utf-8"))
        Handler.payloads.append(payload)
        count = len(Handler.payloads)
        if Handler.mode == "plain500":
            self._send(500, DISK)
            return
        if Handler.mode == "dropflag":
            if count == 1:
                self._send(500, PARSE)
                return
            if count == 2 and payload.get("parallel_tool_calls") is False:
                self._send(400, UNKNOWN_FLAG)
                return
            self._send(200, OK)
            return
        if Handler.mode == "exhaust" and count <= 3:
            self._send(500, PARSE)
            return
        if Handler.mode == "recover" and count < 3:
            self._send(500, PARSE)
            return
        self._send(200, OK)

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


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


def _messages(body: dict) -> list[dict]:
    chat = body.get("chat") or body
    return list(chat.get("messages") or [])


def setup_function():
    Handler.payloads = []
    Handler.mode = "recover"
    reset_parallel_tool_calls()


def test_detector_requires_a_5xx_and_the_parse_phrase():
    assert tool_parse_failure(500, '{"error":{"message":"parse_error: unexpected \'{\'"}}') is False
    assert tool_parse_failure(500, "parse_error while reading a tool call")
    assert tool_parse_failure(500, PARSE.decode())
    assert tool_parse_failure(400, "Failed to parse tool call") is False
    assert tool_parse_failure(500, "disk blew up") is False


def test_concatenated_tool_json_splits_into_two_calls():
    found = _requests_from_native([{
        "id": "call_1",
        "name": "terminal",
        "arguments": '{"command":"ls"}{"command":"pwd"}',
    }])
    assert [item.command for item in found] == ["ls", "pwd"]
    assert json.loads(found[0].call_arguments) == {"command": "ls"}
    assert json.loads(found[1].call_arguments) == {"command": "pwd"}
    assert found[0].call_id == "call_1"
    assert found[1].call_id != "call_1"
    parsed = parse_tools(
        '{"name":"terminal","arguments":{"command":"ls"}}'
        '{"name":"read_file","arguments":{"path":"notes.txt"}}'
    )
    assert [(item.kind, item.command or item.path) for item in parsed] == [("shell", "ls"), ("files", "notes.txt")]
    single = parse_tools('{"name":"terminal","arguments":{"command":"ls"}}')
    assert len(single) == 1
    assert single[0].command == "ls"


def test_stub_retries_a_tool_parse_500_then_answers(tmp_path):
    Handler.mode = "recover"
    server = _serve()
    port = server.server_address[1]
    client, bot, chat = _chat(tmp_path, port)
    try:
        response = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": "list the folder"},
        )
        assert response.status_code == 200, response.text
        assert len(Handler.payloads) == 3
        assert "parallel_tool_calls" not in Handler.payloads[0]
        assert Handler.payloads[1]["parallel_tool_calls"] is False
        assert Handler.payloads[2]["parallel_tool_calls"] is False
        assert [item["temperature"] for item in Handler.payloads] == [0.6, 0.3, 0.1]
        assert all("tools" in item for item in Handler.payloads)
        first = json.dumps(Handler.payloads[0])
        second = json.dumps(Handler.payloads[1])
        assert TOOL_PARSE_NUDGE not in first
        assert TOOL_PARSE_NUDGE in second
        assert any(item.get("content") == "Noted." for item in _messages(response.json()))
    finally:
        client.close()
        server.shutdown()


def test_stub_gives_a_resumable_message_when_the_parse_error_persists(tmp_path):
    Handler.mode = "exhaust"
    server = _serve()
    port = server.server_address[1]
    client, bot, chat = _chat(tmp_path, port)
    try:
        response = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": "list the folder"},
        )
        assert response.status_code == 200, response.text
        assert len(Handler.payloads) == 3
        body = response.json()
        chat_body = body.get("chat") or body
        assert MALFORMED_TOOL_TEXT in (body.get("reply") or "")
        assert "Failed to parse tool call" in (body.get("reply") or "")
        assert chat_body["run"]["status"] == "paused"
        assert chat_body["run"]["status"] != "stopped"
        offer = [item for item in chat_body["messages"] if item.get("kind") == "resume-offer"]
        assert MALFORMED_TOOL_TEXT in offer[-1]["content"]
        assert "Failed to parse tool call" in offer[-1]["content"]
        blob = json.dumps(offer[-1])
        assert "Stopped" not in blob
        assert "127.0.0.1" not in blob
        assert all("tools" in item for item in Handler.payloads)
        assert [item["temperature"] for item in Handler.payloads] == [0.6, 0.3, 0.1]
        saved = (Path(os.environ["EASYAGENT_DATA"]) / "debug-raw.txt").read_text(encoding="utf-8")
        assert "retry=1" in saved and "retry=2" in saved and "retry=3" in saved
        assert "Failed to parse tool call" in saved
        resumed = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/resume")
        assert resumed.status_code == 200, resumed.text
        assert any(item.get("content") == "Noted." for item in _messages(resumed.json()))
    finally:
        client.close()
        server.shutdown()


def test_other_500_stays_stopped_and_is_not_retried(tmp_path):
    Handler.mode = "plain500"
    server = _serve()
    port = server.server_address[1]
    client, bot, chat = _chat(tmp_path, port)
    try:
        response = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": "list the folder"},
        )
        assert response.status_code == 200, response.text
        assert len(Handler.payloads) == 1
        body = response.json()
        chat_body = body.get("chat") or body
        assert str(body.get("reply") or "").startswith("Stopped:")
        assert "disk blew up" in body["reply"]
        assert chat_body["run"]["status"] == "error"
    finally:
        client.close()
        server.shutdown()


def test_unsupported_parallel_flag_is_dropped_and_the_nudge_remains(tmp_path):
    Handler.mode = "dropflag"
    server = _serve()
    port = server.server_address[1]
    client, bot, chat = _chat(tmp_path, port)
    try:
        response = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": "list the folder"},
        )
        assert response.status_code == 200, response.text
        assert len(Handler.payloads) == 3
        assert Handler.payloads[1]["parallel_tool_calls"] is False
        assert "parallel_tool_calls" not in Handler.payloads[2]
        assert all("tools" in item for item in Handler.payloads)
        assert TOOL_PARSE_NUDGE in json.dumps(Handler.payloads[2])
        assert any(item.get("content") == "Noted." for item in _messages(response.json()))
    finally:
        client.close()
        server.shutdown()
