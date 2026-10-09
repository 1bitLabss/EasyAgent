"""The desktop window reads these two routes and loads the page the server is serving."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import easyagent
from easyagent.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_health_and_unread_are_what_the_desktop_window_reads(tmp_path):
    client = TestClient(create_app(tmp_path))
    health = client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["ok"] is True
    assert "data_dir" in body
    assert body["version"] == easyagent.__version__

    unread = client.get("/api/unread")
    assert unread.status_code == 200
    snapshot = unread.json()
    assert snapshot["total"] == 0
    assert snapshot["chats"] == []
    assert snapshot["rooms"] == []


def test_desktop_shell_loads_the_server_and_polls_unread_slowly():
    conf = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    window = conf["app"]["windows"][0]
    assert window["url"] == "http://127.0.0.1:44721/"
    lib = (ROOT / "desktop" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "open_server_page" in lib
    assert "host::UNREAD_POLL" in lib
    assert "from_secs(2)" not in lib
    supervisor = (ROOT / "desktop" / "supervisor" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "UNREAD_POLL: Duration = Duration::from_secs(15)" in supervisor
    splash = (ROOT / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "location.replace" in splash
    assert "/api/unread" not in splash
