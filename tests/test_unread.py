"""Unread replies stay counted until that chat is opened. Transcripts are not rewritten."""

import builtins
import os
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.store import Store
from easyagent.tray import (
    WINDOWS_TRAY_LIMIT,
    _drop_pystray_modules,
    _session_block,
    dock_warning,
    draw_icon,
    load_pystray,
    run_tray,
    start_tray,
    sync_icon,
)


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.calls.append(messages)
        return "ack"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr("easyagent.llm.complete", Recorder())
    app = create_app(tmp_path)
    return type("World", (), {"client": TestClient(app), "path": tmp_path, "app": app})()


def _stamp(path: Path):
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns


def test_unread_clears_only_the_opened_chat_and_leaves_transcripts(world):
    client = world.client
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"})
    assert endpoint.status_code == 200, endpoint.text
    endpoint_id = endpoint.json()["id"]
    ann = client.post("/api/bots", json={"name": "Ann", "endpoint_id": endpoint_id}).json()
    ben = client.post("/api/bots", json={"name": "Ben", "endpoint_id": endpoint_id}).json()
    ann_chat = client.post(f"/api/bots/{ann['id']}/chats").json()
    ben_chat = client.post(f"/api/bots/{ben['id']}/chats").json()
    room = client.post("/api/rooms", json={"name": "Desk"}).json()
    assert client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": ann["id"]}).status_code == 200
    assert client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": ben["id"]}).status_code == 200

    store = world.app.state.store
    solo = store.create_chat(ann["id"])
    solo["messages"].append({"id": "u1", "role": "user", "content": "only-the-user", "created_at": "t"})
    store.save_chat(solo)
    quiet = client.get("/api/unread")
    assert quiet.status_code == 200, quiet.text
    assert quiet.json()["total"] == 0
    assert not (world.path / "unread.json").exists()

    ann_sent = client.post(
        f"/api/bots/{ann['id']}/chats/{ann_chat['id']}/messages",
        json={"content": "ann-private-line"},
    )
    assert ann_sent.status_code == 200, ann_sent.text
    ben_sent = client.post(
        f"/api/bots/{ben['id']}/chats/{ben_chat['id']}/messages",
        json={"content": "ben-private-line"},
    )
    assert ben_sent.status_code == 200, ben_sent.text
    room_sent = client.post(f"/api/rooms/{room['id']}/messages", json={"content": "hello-room"})
    assert room_sent.status_code == 200, room_sent.text

    err = store.create_chat(ben["id"])
    err["messages"].append(
        {"id": "e1", "role": "assistant", "content": "Endpoint error. down", "error": True, "created_at": "t"}
    )
    store.save_chat(err)

    ann_path = world.path / "bots" / ann["id"] / "chats" / f"{ann_chat['id']}.json"
    ben_path = world.path / "bots" / ben["id"] / "chats" / f"{ben_chat['id']}.json"
    err_path = world.path / "bots" / ben["id"] / "chats" / f"{err['id']}.json"
    solo_path = world.path / "bots" / ann["id"] / "chats" / f"{solo['id']}.json"
    room_path = world.path / "rooms" / f"{room['id']}.json"
    before = {path: _stamp(path) for path in (ann_path, ben_path, err_path, solo_path, room_path)}

    unread = client.get("/api/unread").json()
    assert unread["total"] == 5
    assert {"bot_id": ann["id"], "chat_id": ann_chat["id"], "unread": 1} in unread["chats"]
    assert {"bot_id": ben["id"], "chat_id": ben_chat["id"], "unread": 1} in unread["chats"]
    assert {"bot_id": ben["id"], "chat_id": err["id"], "unread": 1} in unread["chats"]
    assert {"room_id": room["id"], "unread": 2} in unread["rooms"]
    assert not any(item["chat_id"] == solo["id"] for item in unread["chats"])
    assert not (world.path / "unread.json").exists()
    assert {path: _stamp(path) for path in before} == before

    opened = client.get(f"/api/bots/{ann['id']}/chats/{ann_chat['id']}")
    assert opened.status_code == 200
    assert client.get("/api/unread").json()["total"] == 5
    assert {path: _stamp(path) for path in before} == before

    shown = len(opened.json()["messages"])
    cleared = client.post(
        f"/api/bots/{ann['id']}/chats/{ann_chat['id']}/read",
        json={"through": shown},
    )
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["total"] == 4
    assert not any(item["chat_id"] == ann_chat["id"] for item in body["chats"])
    assert {"bot_id": ben["id"], "chat_id": ben_chat["id"], "unread": 1} in body["chats"]
    assert {"room_id": room["id"], "unread": 2} in body["rooms"]
    assert {path: _stamp(path) for path in before} == before
    cursor = (world.path / "unread.json").read_text(encoding="utf-8")
    assert "ann-private-line" not in cursor
    assert "hello-room" not in cursor
    assert "ack" not in cursor

    stale = client.post(f"/api/bots/{ann['id']}/chats/{ann_chat['id']}/read", json={"through": 0})
    assert not any(item["chat_id"] == ann_chat["id"] for item in stale.json()["chats"])
    assert {path: _stamp(path) for path in before} == before

    again = client.post(
        f"/api/bots/{ann['id']}/chats/{ann_chat['id']}/messages",
        json={"content": "ann-second-line"},
    )
    assert again.status_code == 200, again.text
    grown = ann_path.read_text(encoding="utf-8")
    assert "ann-private-line" in grown
    assert "ann-second-line" in grown
    assert grown.count("ack") >= 2
    after = client.get("/api/unread").json()
    assert {"bot_id": ann["id"], "chat_id": ann_chat["id"], "unread": 1} in after["chats"]
    assert {"bot_id": ben["id"], "chat_id": ben_chat["id"], "unread": 1} in after["chats"]
    assert {"room_id": room["id"], "unread": 2} in after["rooms"]
    assert _stamp(ben_path) == before[ben_path]
    assert _stamp(room_path) == before[room_path]
    assert _stamp(solo_path) == before[solo_path]
    assert _stamp(err_path) == before[err_path]

    room_cleared = client.post(f"/api/rooms/{room['id']}/read", json={"through": 3})
    assert room_cleared.status_code == 200, room_cleared.text
    assert not any(item["room_id"] == room["id"] for item in room_cleared.json()["rooms"])
    assert {"bot_id": ann["id"], "chat_id": ann_chat["id"], "unread": 1} in room_cleared.json()["chats"]
    assert _stamp(room_path) == before[room_path]
    assert "hello-room" in room_path.read_text(encoding="utf-8")


def test_unread_names_a_bot_with_a_running_chat(world):
    client = world.client
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ann", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    store = world.app.state.store
    saved = store.get_chat(bot["id"], chat["id"])
    saved["run"] = {
        "id": "run-1",
        "status": "running",
        "started_at": "2026-01-01T00:00:00Z",
        "last_activity_at": "2026-01-01T00:00:01Z",
        "current_step": "Thinking",
        "reason": "",
    }
    store.save_chat(saved)
    body = client.get("/api/unread").json()
    assert body["busy"] == [bot["id"]]
    saved = store.get_chat(bot["id"], chat["id"])
    saved["run"]["status"] = "idle"
    store.save_chat(saved)
    assert client.get("/api/unread").json()["busy"] == []


def test_tray_icon_updates_without_a_new_process(world):
    client = world.client
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ann", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    path = world.path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json"

    class Icon:
        pass

    icon = Icon()
    store = world.app.state.store
    assert sync_icon(icon, store) == 0
    assert icon.title == "EasyAgent"
    quiet = icon.icon.tobytes()
    sent = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages", json={"content": "ping"})
    assert sent.status_code == 200, sent.text
    stamped = _stamp(path)
    assert sync_icon(icon, store) == 1
    assert icon.title == "(1) EasyAgent"
    marked = icon.icon.tobytes()
    assert marked != quiet
    assert _stamp(path) == stamped
    assert sync_icon(icon, store) == 1
    assert "ping" in path.read_text(encoding="utf-8")
    shown = len(client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()["messages"])
    cleared = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/read",
        json={"through": shown},
    )
    assert cleared.status_code == 200, cleared.text
    assert sync_icon(icon, store) == 0
    assert icon.title == "EasyAgent"
    assert icon.icon.tobytes() == quiet
    assert icon.icon.tobytes() != marked


def test_icon_bitmap_changes_with_the_count():
    zero = draw_icon(0).tobytes()
    one = draw_icon(1).tobytes()
    twelve = draw_icon(12).tobytes()
    lots = draw_icon(100).tobytes()
    assert zero != one
    assert one != twelve
    assert twelve != lots
    assert draw_icon(140).tobytes() == lots


def test_windows_tray_does_not_need_display(monkeypatch):
    """A Windows desktop has a notification area and does not set DISPLAY."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("EASYAGENT_TRAY", raising=False)
    assert _session_block() is None


def test_headless_session_reports_the_windows_limit(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("EASYAGENT_TRAY", raising=False)
    store = Store(tmp_path)
    store.ensure()
    assert start_tray(store, 44721) is None
    text = capsys.readouterr().out
    assert "no DISPLAY" in text
    assert WINDOWS_TRAY_LIMIT in text
    assert "pywin32" in text
    assert "(N) EasyAgent" in text


def test_missing_pystray_reports_the_windows_limit(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("EASYAGENT_TRAY", "1")
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "pystray" or name.startswith("pystray."):
            raise ImportError("No module named pystray")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    store = Store(tmp_path)
    store.ensure()
    run_tray(store, 44721)
    text = capsys.readouterr().out
    assert "No module named pystray" in text
    assert "notification area" in text
    assert "pywin32" in text


def test_dock_warning_names_the_missing_tray_owner():
    class Icon:
        _systray_manager = None

    text = dock_warning(Icon())
    assert text is not None
    assert "Failed to dock icon" in text
    assert "assert self._systray_manager" in text

    class Docked:
        _systray_manager = object()

    assert dock_warning(Docked()) is None
    assert dock_warning(object()) is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses the pywin32 backend")
def test_gtk_namespace_error_retries_the_xorg_backend(monkeypatch):
    monkeypatch.delenv("PYSTRAY_BACKEND", raising=False)
    real_import = builtins.__import__
    seen = []

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pystray" or name.startswith("pystray."):
            seen.append(os.environ.get("PYSTRAY_BACKEND"))
            if os.environ.get("PYSTRAY_BACKEND") != "xorg":
                raise ValueError("Namespace Gtk not available")
            module = types.ModuleType("pystray")
            sys.modules["pystray"] = module
            return module
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake)
    try:
        module = load_pystray()
        assert isinstance(module, types.ModuleType)
        assert seen[0] in (None, "")
        assert seen[-1] == "xorg"
    finally:
        os.environ.pop("PYSTRAY_BACKEND", None)
        _drop_pystray_modules()


def test_tray_can_be_turned_off(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("EASYAGENT_TRAY", "0")
    monkeypatch.setenv("DISPLAY", ":1")
    store = Store(tmp_path)
    store.ensure()
    assert start_tray(store, 44721) is None
    text = capsys.readouterr().out
    assert "EASYAGENT_TRAY=0" in text
    assert "(N) EasyAgent" in text
