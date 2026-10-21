"""A graceful stop keeps one answer, and a picked-up line stays Seen."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from easyagent.app import _finish_reply, _pause_for_runaway
from easyagent.plan import steer_into
from easyagent.store import now_iso
from tests.test_plan_0320 import _client, _running

ANSWER = "ANSWER seen=MSG-R1,MSG-R2,MSG-job"


def test_an_interrupted_answer_is_not_stored_again(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "Look at the kiln.", "created_at": now_iso()},
        {
            "id": "a1",
            "role": "assistant",
            "content": ANSWER,
            "interrupted": True,
            "created_at": now_iso(),
        },
    ]
    store.save_chat(doc)
    done = _finish_reply(store, bot["id"], chat["id"], ANSWER, [])
    copies = [
        item.get("content")
        for item in done["chat"]["messages"]
        if item.get("role") == "assistant" and item.get("content") == ANSWER
    ]
    assert copies == [ANSWER]
    client.close()


def test_a_live_partial_without_interrupted_is_not_stored_again(tmp_path):
    """The stop hook saves a live row and can die before anything marks it interrupted."""
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "MSG-job", "created_at": now_iso()},
        {"id": "a-live", "role": "assistant", "content": ANSWER, "live": True, "created_at": now_iso()},
    ]
    store.save_chat(doc)
    done = _finish_reply(store, bot["id"], chat["id"], ANSWER, [])
    copies = [
        item
        for item in done["chat"]["messages"]
        if item.get("role") == "assistant" and item.get("content") == ANSWER
    ]
    assert [item.get("id") for item in copies] == ["a-live"]
    assert copies[0].get("live") is not True
    client.close()


def test_the_last_assistant_line_is_not_stored_again(tmp_path):
    """After a pickup note, a finished copy of this reply is not stored again."""
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"] = [
        {"id": "note", "role": "assistant", "kind": "resume-note", "content": "Picked up where it stopped.", "created_at": now_iso()},
        {"id": "a1", "role": "assistant", "content": ANSWER, "created_at": now_iso()},
        {"id": "u2", "role": "user", "content": "Resume the checklist where it stopped. Do not start over.", "created_at": now_iso()},
    ]
    store.save_chat(doc)
    done = _finish_reply(store, bot["id"], chat["id"], ANSWER, [])
    copies = [
        item.get("id")
        for item in done["chat"]["messages"]
        if item.get("role") == "assistant" and item.get("content") == ANSWER
    ]
    assert copies == ["a1"]
    client.close()


def test_an_ordinary_chat_can_say_the_same_thing_twice(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "MSG-job", "created_at": now_iso()},
        {"id": "a1", "role": "assistant", "content": ANSWER, "created_at": now_iso()},
        {"id": "u2", "role": "user", "content": "Say that again.", "created_at": now_iso()},
    ]
    store.save_chat(doc)
    done = _finish_reply(store, bot["id"], chat["id"], ANSWER, [])
    copies = [
        item.get("id")
        for item in done["chat"]["messages"]
        if item.get("role") == "assistant" and item.get("content") == ANSWER
    ]
    assert copies[0] == "a1"
    assert len(copies) == 2
    client.close()


def test_the_same_words_after_a_new_message_are_a_new_answer(tmp_path):
    """A pause keeps the partial. The next reply is its own row, even when the words match."""
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "are you connected?", "created_at": now_iso()},
        {"id": "a1", "role": "assistant", "content": ANSWER, "interrupted": True, "created_at": now_iso()},
        {"id": "u2", "role": "user", "content": "what are you doing?", "chip": "seen", "steer": True, "created_at": now_iso()},
        {"id": "u3", "role": "user", "content": "and the second question?", "chip": "seen", "steer": True, "created_at": now_iso()},
        {"id": "a-live", "role": "assistant", "content": ANSWER, "live": True, "created_at": now_iso()},
    ]
    store.save_chat(doc)
    done = _finish_reply(store, bot["id"], chat["id"], ANSWER, [])
    rows = [
        (item.get("role"), item.get("content"))
        for item in done["chat"]["messages"]
        if item.get("role") in {"user", "assistant"} and item.get("kind") not in {"status", "pickup"}
    ]
    assert rows == [
        ("user", "are you connected?"),
        ("assistant", ANSWER),
        ("user", "what are you doing?"),
        ("user", "and the second question?"),
        ("assistant", ANSWER),
    ]
    assert rows[-1][0] == "assistant"
    client.close()


def test_a_message_during_the_save_updates_the_open_reply(tmp_path):
    """The exit seam keeps one finished reply. The late message stays under it."""
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "MEDIUM please", "created_at": now_iso()},
        {"id": "a-live", "role": "assistant", "content": "Section 1 so far", "live": True, "created_at": now_iso()},
        {"id": "u2", "role": "user", "content": "SHORT busy", "chip": "queued", "created_at": now_iso()},
    ]
    store.save_chat(doc)
    done = _finish_reply(store, bot["id"], chat["id"], ANSWER, [])
    rows = [
        (item.get("id"), item.get("role"), item.get("content"), bool(item.get("interrupted")), bool(item.get("live")))
        for item in done["chat"]["messages"]
        if item.get("role") in {"user", "assistant"} and item.get("kind") not in {"status", "pickup", "resume-offer"}
    ]
    assert rows == [
        ("u1", "user", "MEDIUM please", False, False),
        ("a-live", "assistant", ANSWER, False, False),
        ("u2", "user", "SHORT busy", False, False),
    ]
    client.close()


def test_a_different_answer_after_an_interrupt_is_kept(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"] = [
        {"id": "a1", "role": "assistant", "content": "The kiln", "interrupted": True, "created_at": now_iso()},
    ]
    store.save_chat(doc)
    done = _finish_reply(store, bot["id"], chat["id"], "The kiln is fine.", [])
    texts = [item.get("content") for item in done["chat"]["messages"] if item.get("role") == "assistant"]
    assert "The kiln" in texts
    assert "The kiln is fine." in texts
    client.close()


def test_a_picked_up_line_stays_seen_after_a_guard_cut(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    assert client.post(url, json={"content": "while it runs"}).status_code == 200
    assert steer_into(store, bot["id"], chat["id"], []) == 1
    picked = next(item for item in store.get_chat(bot["id"], chat["id"])["messages"] if item.get("content") == "while it runs")
    assert picked["chip"] == "seen"
    _pause_for_runaway(store, bot["id"], chat["id"])
    again = next(item for item in store.get_chat(bot["id"], chat["id"])["messages"] if item.get("content") == "while it runs")
    assert again["chip"] == "seen"
    assert "queued" not in again
    client.close()


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _api(port: int, method: str, path: str, body: dict | None = None, stream: bool = False, timeout: float = 8):
    data = None if body is None else json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if stream:
        headers["accept"] = "text/event-stream"
    return urlopen(Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method), timeout=timeout)


def _answers(data: Path) -> list[str]:
    found = []
    for path in data.glob("bots/*/chats/*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if "messages" not in doc:
            continue
        for message in doc.get("messages") or []:
            if isinstance(message, dict) and message.get("role") == "assistant" and message.get("content") == ANSWER:
                found.append(str(message.get("id")))
    return found


def _chat_doc(data: Path) -> dict:
    for path in data.glob("bots/*/chats/*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if "messages" in doc:
            return doc
    raise AssertionError("no chat")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows ends SIGTERM without the graceful stop this checks")
def test_sigterm_restart_does_not_store_the_answer_twice(tmp_path):
    """The live stream is a real server. SIGTERM keeps the reply. Restart does not add another."""
    llm_port = _free_port()
    ea_port = _free_port()
    data = tmp_path / "data"
    data.mkdir()
    held_count = {"n": 0}
    second_held = threading.Event()
    stop_llm = threading.Event()
    root = Path(__file__).resolve().parents[1]

    def serve():
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", llm_port))
        sock.listen(20)
        sock.settimeout(0.3)
        while not stop_llm.is_set():
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue

            def handle(conn=conn):
                try:
                    raw = b""
                    while b"\r\n\r\n" not in raw:
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        raw += chunk
                    head, rest = raw.split(b"\r\n\r\n", 1)
                    length = 0
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            length = int(line.split(b":", 1)[1].strip())
                    while len(rest) < length:
                        rest += conn.recv(4096)
                    body = json.loads(rest[:length] or b"{}")
                    payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": ANSWER}}]}).encode()
                    if body.get("stream"):
                        piece = f"data: {json.dumps({'choices': [{'delta': {'content': ANSWER}}]})}\n\n".encode()
                        conn.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n"
                            + piece
                        )
                        held_count["n"] += 1
                        if held_count["n"] >= 2:
                            second_held.set()
                        stop_llm.wait(30)
                        return
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                        + str(len(payload)).encode()
                        + b"\r\nConnection: close\r\n\r\n"
                        + payload
                    )
                except Exception:
                    return
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

            threading.Thread(target=handle, daemon=True).start()
        sock.close()

    threading.Thread(target=serve, daemon=True).start()
    env = os.environ.copy()
    env["EASYAGENT_DATA"] = str(data)
    env["EASYAGENT_PORT"] = str(ea_port)
    env["EASYAGENT_HOST"] = "127.0.0.1"
    env["EASYAGENT_SUPERVISE"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    # The suite points HOME at an empty profile, so the child would not see installed packages.
    env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)
    log_handle = (tmp_path / "ea.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "easyagent"],
        cwd=str(root),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    def wait_health() -> None:
        for _ in range(50):
            try:
                _api(ea_port, "GET", "/api/health", timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise AssertionError("server did not start\n" + (tmp_path / "ea.log").read_text(encoding="utf-8")[-2000:])

    try:
        wait_health()
        endpoint = json.load(_api(ea_port, "POST", "/api/endpoints", {
            "name": "local",
            "base_url": f"http://127.0.0.1:{llm_port}/v1",
            "model": "stub",
            "stall_seconds": 120,
            "first_token_seconds": 120,
        }))
        bot = json.load(_api(ea_port, "POST", "/api/bots", {"name": "Ada", "endpoint_id": endpoint["id"]}))
        _api(ea_port, "PATCH", f"/api/bots/{bot['id']}", {"check_enabled": False})
        chat = json.load(_api(ea_port, "POST", f"/api/bots/{bot['id']}/chats", {}))
        url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"

        def send() -> None:
            try:
                response = _api(ea_port, "POST", url, {"content": "MSG-job"}, stream=True, timeout=20)
                response.close()
            except Exception:
                return

        threading.Thread(target=send, daemon=True).start()
        deadline = time.time() + 10
        while time.time() < deadline and held_count["n"] < 1:
            time.sleep(0.05)
        assert held_count["n"] >= 1, "the live stream never started"
        _api(ea_port, "POST", url, {"content": "MSG-R1"})
        _api(ea_port, "POST", url, {"content": "MSG-R2"})
        assert second_held.wait(10), "the picked-up line did not start another live stream"
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise AssertionError("SIGTERM did not stop the server")
        # The partial stays. The new reply is its own row when that chunk arrived. A restart must not add another.
        kept = _answers(data)
        assert 1 <= len(kept) <= 2
        proc = subprocess.Popen(
            [sys.executable, "-m", "easyagent"],
            cwd=str(root),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        wait_health()
        deadline = time.time() + 8
        while time.time() < deadline:
            doc = _chat_doc(data)
            if (doc.get("run") or {}).get("status") in {"idle", "paused", "stopped"} and len(_answers(data)) >= 1:
                break
            time.sleep(0.2)
        assert _answers(data) == kept
        picked = next(item for item in _chat_doc(data)["messages"] if item.get("content") == "MSG-R1")
        # A kept answer may cover the line. It must not go back to queued.
        assert picked.get("chip") in {"seen", "answered"}
    finally:
        stop_llm.set()
        log_handle.close()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
