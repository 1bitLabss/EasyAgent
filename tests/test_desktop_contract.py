"""The desktop window reads these two routes. The page itself is unchanged."""

from fastapi.testclient import TestClient

from easyagent.app import create_app


def test_health_and_unread_are_what_the_desktop_window_reads(tmp_path):
    client = TestClient(create_app(tmp_path))
    health = client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["ok"] is True
    assert "data_dir" in body

    unread = client.get("/api/unread")
    assert unread.status_code == 200
    snapshot = unread.json()
    assert snapshot["total"] == 0
    assert snapshot["chats"] == []
    assert snapshot["rooms"] == []
