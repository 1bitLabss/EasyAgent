"""A normal chat streams reasoning to the React client, including a check note."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from easyagent.app import create_app

ANSWER = "The field reply is ready. " * 20
TAGGED = "The tagged reply is ready. " * 20


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if b"EASYAGENT_CHECK_V1" in raw:
            self._json({"choices": [{"message": {"content": '{"pass": true, "problems": [], "fix_hint": ""}'}}]})
            return
        if b"reason-tags" in raw:
            self._sse([{"choices": [{"delta": {"content": f"<think>tag plan from the model</think>{TAGGED}"}}]}])
            return
        self._sse(
            [
                {"choices": [{"delta": {"reasoning_content": "field plan one. "}}]},
                {"choices": [{"delta": {"reasoning_content": "field plan two."}}]},
                {"choices": [{"delta": {"content": ANSWER}}]},
            ]
        )

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, events: list[dict]) -> None:
        chunks = [f"data: {json.dumps(event)}\n\n".encode() for event in events]
        chunks.append(b"data: [DONE]\n\n")
        body = b"".join(chunks)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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


def test_a_normal_chat_streams_reasoning_and_the_check_note(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_CHECK", "1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = TestClient(create_app(tmp_path))
        endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": f"http://127.0.0.1:{port}/v1"}).json()
        bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
        chat = client.get(f"/api/bots/{bot['id']}/ongoing").json()
        url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"

        with client.stream("POST", url, json={"content": "reason-field please"}, headers={"Accept": "text/event-stream"}) as response:
            assert response.status_code == 200, response.read()
            body = response.read().decode()
        events = _events(body)
        kinds = [event.get("type") for event in events]
        thinking = [event for event in events if event.get("type") == "thinking"]
        assert thinking, body
        assert kinds.index("thinking") < kinds.index("delta")
        joined = "".join(str(event.get("text") or "") for event in thinking)
        assert "field plan one." in joined
        assert "field plan two." in joined
        assert "Checked. No problem found." in joined
        answer_at = body.index("The field reply is ready.")
        note_at = body.index("Checked. No problem found.")
        assert body.index("field plan one.") < answer_at < note_at
        stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
        assert len(client.get(f"/api/bots/{bot['id']}/chats").json()) == 1
        reply = stored["messages"][-1]
        assert "field plan one." in reply["thinking"]
        assert "Checked. No problem found." in reply["thinking"]
        assert "field plan" not in reply["content"]
        assert reply["thought_seconds"] >= 1
        assert reply.get("check") == "checked"

        with client.stream("POST", url, json={"content": "reason-tags please"}, headers={"Accept": "text/event-stream"}) as response:
            assert response.status_code == 200, response.read()
            tagged = response.read().decode()
        tag_events = _events(tagged)
        tag_thought = "".join(str(event.get("text") or "") for event in tag_events if event.get("type") == "thinking")
        assert "tag plan from the model" in tag_thought
        tag_kinds = [event.get("type") for event in tag_events]
        assert tag_kinds.index("thinking") < tag_kinds.index("delta")
        follow = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
        assert follow["id"] == chat["id"]
        last = follow["messages"][-1]
        assert last["content"].startswith("The tagged reply is ready.")
        assert "<think>" not in last["content"]
        assert "tag plan from the model" in last["thinking"]
    finally:
        server.shutdown()
