"""End-to-end chat gate for an installed EasyAgent.

Starts a stub OpenAI-compatible server, adds a connection pointed at it,
creates a bot, sends one chat message the way the window does (SSE), and
requires a real assistant reply. A 'Stopped:' line or an error event fails.

Stdlib only, so it runs with any Python on a clean CI runner:

    python scripts/chat_smoke.py --app http://127.0.0.1:45101
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The CI step exports an unwritable SSLKEYLOGFILE for the app under test. This
# harness must not inherit it, or urllib's own ssl context fails the same way.
os.environ.pop("SSLKEYLOGFILE", None)

REPLY = "Yes, I am connected. (chat-smoke stub)"
MODEL = "stub-model"
HOLD = threading.Event()
_COMPLETIONS = {"n": 0}
_COMPLETION_LOCK = threading.Lock()


class Stub(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        sys.stderr.write("stub: " + (fmt % args) + "\n")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "stub"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            req = {}
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        with _COMPLETION_LOCK:
            _COMPLETIONS["n"] += 1
            hold = _COMPLETIONS["n"] == 1
        if hold and not HOLD.wait(45):
            self._json(500, {"error": {"message": "the first completion was not released"}})
            return
        created = int(time.time())
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            chunks = [
                {"role": "assistant", "content": ""},
                {"content": REPLY},
            ]
            for delta in chunks:
                event = {"id": "stub", "object": "chat.completion.chunk", "created": created, "model": MODEL,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            done = {"id": "stub", "object": "chat.completion.chunk", "created": created, "model": MODEL,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(b"data: " + json.dumps(done).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        self._json(200, {
            "id": "stub", "object": "chat.completion", "created": created, "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


def call(app: str, method: str, path: str, payload: dict | None = None, accept: str = "application/json",
         timeout: float = 30.0) -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(app + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_up(app: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            status, _ = call(app, "GET", "/api/bots", timeout=5)
            if status == 200:
                return
            last = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(2)
    raise SystemExit(f"FAIL: EasyAgent at {app} did not answer in {seconds:.0f}s ({last})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True, help="EasyAgent base URL, e.g. http://127.0.0.1:45101")
    parser.add_argument("--wait", type=float, default=240.0)
    args = parser.parse_args()
    app = args.app.rstrip("/")

    stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{stub.server_address[1]}/v1"
    print(f"stub model server: {base_url}", flush=True)

    wait_up(app, args.wait)
    status, text = call(app, "POST", "/api/endpoints", {"name": "chat-smoke", "base_url": base_url, "model": MODEL})
    if status != 200:
        print(f"FAIL: create connection HTTP {status}: {text}")
        return 1
    endpoint = json.loads(text)
    status, text = call(app, "POST", "/api/bots", {"name": "SMOKE1", "endpoint_id": endpoint["id"]})
    if status != 200:
        print(f"FAIL: create bot HTTP {status}: {text}")
        return 1
    bot = json.loads(text)
    status, text = call(app, "POST", f"/api/bots/{bot['id']}/chats")
    if status != 200:
        print(f"FAIL: create chat HTTP {status}: {text}")
        return 1
    chat = json.loads(text)

    box: dict = {}

    def first_turn() -> None:
        box["result"] = call(
            app,
            "POST",
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            {"content": "are you connected?"},
            accept="text/event-stream",
            timeout=180,
        )

    worker = threading.Thread(target=first_turn)
    worker.start()
    run_id = ""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        _status, stored = call(app, "GET", f"/api/bots/{bot['id']}/chats/{chat['id']}?window=80", timeout=5)
        try:
            running = json.loads(stored).get("run") or {}
        except json.JSONDecodeError:
            running = {}
        if running.get("status") == "running" and running.get("id"):
            run_id = running["id"]
            break
        time.sleep(0.2)
    if not run_id:
        HOLD.set()
        worker.join(timeout=20)
        print("FAIL: the chat never showed a running turn")
        return 1
    sent = []
    for text in ("what are you doing?", "and the second question?"):
        status_code, status_body = call(
            app,
            "POST",
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            {"content": text},
            timeout=20,
        )
        sent.append((text, status_code, status_body))
    _status, during = call(app, "GET", f"/api/bots/{bot['id']}/chats/{chat['id']}?window=80", timeout=5)
    try:
        during_doc = json.loads(during)
    except json.JSONDecodeError:
        during_doc = {}
    during_run = during_doc.get("run") or {}
    during_messages = during_doc.get("messages") or []
    notes = [
        item for item in during_messages
        if item.get("kind") == "status" and "Still on this job" in str(item.get("content") or "")
    ]
    both_present = all(any(item.get("content") == text for item in during_messages) for text, _code, _body in sent)
    both_ok = all(code == 200 and "Still on this job" in body for _text, code, body in sent)
    if not both_ok or during_run.get("id") != run_id or not both_present or len(notes) < 2:
        HOLD.set()
        worker.join(timeout=20)
        print(f"FAIL: status during the turn: {sent!r}")
        print("run during question:", during_run.get("id"), "expected", run_id)
        print("messages:", [item.get("content") for item in during_messages])
        return 1
    print("status during turn: two messages kept, both with a status note, same run", flush=True)
    HOLD.set()
    worker.join(timeout=60)
    if "result" not in box:
        print("FAIL: the first turn did not finish")
        return 1
    status, body = box["result"]
    print(f"messages HTTP {status}", flush=True)
    events = []
    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    kinds = [e.get("type") for e in events]
    print("events:", kinds, flush=True)
    problems = [e for e in events if e.get("type") in {"error", "stopped"}]
    for e in events:
        for key in ("text", "detail", "reason"):
            if isinstance(e.get(key), str) and e[key].startswith("Stopped:"):
                problems.append(e)
    # The stored chat is the source of truth: the last message must be the stub's reply.
    _, stored = call(app, "GET", f"/api/bots/{bot['id']}/chats/{chat['id']}?window=80")
    try:
        messages = json.loads(stored).get("messages") or []
    except json.JSONDecodeError:
        messages = []
    last = messages[-1] if messages else {}
    print("last message:", json.dumps({k: last.get(k) for k in ("role", "content")})[:400], flush=True)
    folded = []
    for text, _code, _body in sent:
        row = next((item for item in messages if item.get("role") == "user" and item.get("content") == text), None)
        pickup = any(
            item.get("kind") == "pickup" and item.get("about") == (row or {}).get("id")
            for item in messages
        )
        folded.append(bool(row) and row.get("chip") in {"seen", "answered"} and pickup)
    ok = (
        status == 200
        and not problems
        and "done" in kinds
        and last.get("role") == "assistant"
        and REPLY in str(last.get("content") or "")
        and all(folded)
    )
    if not ok:
        for p in problems:
            print("problem event:", json.dumps(p)[:600])
        print("FAIL: no real assistant reply")
        return 1
    print("PASS: real assistant reply received")
    return 0


if __name__ == "__main__":
    sys.exit(main())
