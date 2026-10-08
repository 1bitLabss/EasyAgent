"""The React page is the default. The earlier page stays at /classic."""

from fastapi.testclient import TestClient

from easyagent.app import create_app


def test_the_react_page_is_served_at_the_root(tmp_path):
    client = TestClient(create_app(tmp_path))
    page = client.get("/")
    assert page.status_code == 200
    assert "AI agents, made easy." in page.text
    assert 'id="token-gate"' in page.text
    assert 'id="root"' in page.text
    assert "/ui/assets/" in page.text
    assert page.headers["cache-control"] == "no-store"
    script = page.text.split('src="', 1)[1].split('"', 1)[0]
    asset = client.get(script)
    assert asset.status_code == 200
    assert "Add a bot." in asset.text or "AI agents, made easy." in asset.text


def test_the_earlier_page_stays_at_classic(tmp_path):
    client = TestClient(create_app(tmp_path))
    page = client.get("/classic")
    assert page.status_code == 200
    assert 'id="screen-chat"' in page.text
    assert "AI agents, made easy." in page.text


def test_classic_flag_serves_the_earlier_page(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_UI", "classic")
    page = TestClient(create_app(tmp_path)).get("/")
    assert 'id="screen-chat"' in page.text
    assert "/ui/assets/" not in page.text


def test_a_ui_asset_cannot_leave_its_folder(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.get("/ui/../app.py").status_code == 404
    assert client.get("/ui/assets/../../app.py").status_code == 404
