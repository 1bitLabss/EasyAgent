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
    # The window opens on its own startup page and moves to the server only after
    # /api/health answers. Opening the server address first showed a blank page
    # whenever nothing was listening yet.
    assert window["url"] == "index.html"
    lib = (ROOT / "desktop" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "open_server_page" in lib
    assert "wait_for_child" in lib
    assert "Phase::Failed" in lib
    assert "host::UNREAD_POLL" in lib
    assert "from_secs(2)" not in lib
    supervisor = (ROOT / "desktop" / "supervisor" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "UNREAD_POLL: Duration = Duration::from_secs(15)" in supervisor
    splash = (ROOT / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "location.replace" in splash
    assert "/api/unread" not in splash
    assert "startup_state" in splash
    assert "retry_start" in splash
    assert 'id="log"' in splash


def test_every_installer_carries_the_python_runtime():
    for name, triples in {
        "windows": ["x86_64-pc-windows-msvc"],
        "macos": ["aarch64-apple-darwin", "x86_64-apple-darwin"],
        "linux": ["x86_64-unknown-linux-gnu"],
    }.items():
        conf = json.loads((ROOT / "desktop" / "src-tauri" / f"tauri.{name}.conf.json").read_text(encoding="utf-8"))
        resources = conf["bundle"]["resources"]
        for triple in triples:
            assert f"runtime/python-{triple}.tar.gz" in resources
    workflow = (ROOT / ".github" / "workflows" / "desktop-installers.yml").read_text(encoding="utf-8")
    assert workflow.count("scripts/build_desktop_runtime.py") >= 4
    assert "--smoke-test" in workflow
    assert "needs: [windows, macos, linux, smoke-windows, smoke-macos, smoke-linux]" in workflow
