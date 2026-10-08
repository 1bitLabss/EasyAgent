"""A mock model and a temporary EasyAgent for the browser smoke tests.

Nothing here writes the checkout's data folder.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SLOW = b"take your time"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        slow = SLOW in raw
        words = (
            ["Here", "is", "a", "slow", "reply", "from", "the", "mock", "model", "today."]
            if slow
            else ["Mock", "says", "hello."]
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for word in words:
            if slow:
                time.sleep(0.3)
            piece = f"data: {json.dumps({'choices': [{'delta': {'content': word + ' '}}]})}\n\n".encode()
            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.flush()
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):x}\r\n".encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    data = Path(sys.argv[1])
    port = sys.argv[2]
    if data.exists():
        import shutil

        shutil.rmtree(data)
    data.mkdir(parents=True)
    model = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    model_port = model.server_address[1]
    threading.Thread(target=model.serve_forever, daemon=True).start()
    (data / "mock.url").write_text(f"http://127.0.0.1:{model_port}/v1", encoding="utf-8")
    os.environ["EASYAGENT_DATA"] = str(data)
    os.environ["EASYAGENT_PORT"] = port
    os.environ["EASYAGENT_HOST"] = "127.0.0.1"
    os.environ["EASYAGENT_TRAY"] = "0"
    os.environ["EASYAGENT_CHECK"] = "0"
    os.environ["EASYAGENT_LEARN"] = "0"
    os.chdir(ROOT)
    from easyagent.__main__ import main as run

    run()


if __name__ == "__main__":
    main()
