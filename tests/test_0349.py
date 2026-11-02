"""0.3.49: a connection can be tested without sending a message to the model."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi.testclient import TestClient

from easyagent.app import create_app


class _ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[]}')

    def log_message(self, format, *args):  # noqa: A002
        pass


def _answer_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"


def test_test_connection_says_when_the_server_is_not_there(tmp_path):
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "Down", "base_url": "http://127.0.0.1:9/v1"}).json()
    result = client.post(f"/api/endpoints/{endpoint['id']}/test").json()
    assert result["ok"] is False
    assert result["detail"].startswith("Can't reach the model at ")


def test_test_connection_says_when_the_server_answers(tmp_path):
    server, base_url = _answer_server()
    try:
        client = TestClient(create_app(tmp_path))
        endpoint = client.post("/api/endpoints", json={"name": "Up", "base_url": base_url}).json()
        result = client.post(f"/api/endpoints/{endpoint['id']}/test").json()
        assert result["ok"] is True
        assert result["detail"] == "The server answered."
    finally:
        server.shutdown()


def test_test_connection_names_no_such_connection(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.post("/api/endpoints/nothing/test").status_code == 404


def test_test_connection_sends_no_message_to_the_model(tmp_path):
    """The test route must not create a bot, a chat, or a queue entry."""
    server, base_url = _answer_server()
    try:
        client = TestClient(create_app(tmp_path))
        endpoint = client.post("/api/endpoints", json={"name": "Up", "base_url": base_url}).json()
        client.post(f"/api/endpoints/{endpoint['id']}/test")
        assert client.get("/api/bots").json() == []
    finally:
        server.shutdown()
