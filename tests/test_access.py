"""A phone on the LAN is refused until the shared token is set."""

from fastapi.testclient import TestClient

from easyagent.access import access_decision, is_local, presented_token, token_matches
from easyagent.app import create_app
from easyagent.store import Store


REMOTE = ("203.0.113.20", 40000)
LOCAL = ("127.0.0.1", 9)
TOKEN = "desk-token-not-a-transcript"


def test_loopback_and_mapped_loopback_are_local():
    assert is_local("127.0.0.1")
    assert is_local("::1")
    assert is_local("::ffff:127.0.0.1")
    assert is_local("testclient")
    assert not is_local("203.0.113.20")
    assert not is_local("::ffff:203.0.113.20")
    assert not is_local(None)
    assert access_decision("203.0.113.20", "") == "refuse"
    assert access_decision("127.0.0.1", "") == "allow"


def test_forwarded_header_does_not_count_as_the_peer():
    headers = {"x-forwarded-for": "127.0.0.1"}
    assert presented_token(headers) == ""
    assert access_decision("203.0.113.20", presented_token(headers)) == "refuse"


def test_remote_without_a_configured_token_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("EASYAGENT_TOKEN", raising=False)
    app = create_app(tmp_path)
    store = Store(tmp_path)
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ann", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    chat["messages"] = [{"role": "user", "content": "keep-me-on-the-computer"}]
    store.save_chat(chat)
    remote = TestClient(app, client=REMOTE)
    page = remote.get("/")
    assert page.status_code == 403
    assert "Remote access is off" in page.text
    assert "Georgia" not in page.text
    assert "Inter" in page.text
    assert "cdn.jsdelivr.net/fontsource/fonts/inter:vf@5.2.8/latin-wght-normal.woff2" in page.text
    assert "JetBrains Mono" in page.text
    assert "keep-me-on-the-computer" not in page.text
    assert "EASYAGENT_TOKEN" in page.text
    api = remote.get(f"/api/bots/{bot['id']}/chats/{chat['id']}")
    assert api.status_code == 403
    assert "keep-me-on-the-computer" not in api.text
    assert TestClient(app, client=LOCAL).get("/api/bots").status_code == 200


def test_remote_needs_the_token_and_then_reads_the_same_chats(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_TOKEN", TOKEN)
    app = create_app(tmp_path)
    store = Store(tmp_path)
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ann", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    chat["messages"] = [{"role": "user", "content": "keep-me-on-the-computer"}]
    store.save_chat(chat)
    remote = TestClient(app, client=REMOTE)
    page = remote.get("/")
    assert page.status_code == 200
    assert "token-gate" in page.text
    assert "keep-me-on-the-computer" not in page.text
    missed = remote.get(f"/api/bots/{bot['id']}/chats/{chat['id']}")
    assert missed.status_code == 401
    assert "keep-me-on-the-computer" not in missed.text
    query = remote.get("/api/bots", params={"token": TOKEN})
    assert query.status_code == 401
    wrong = remote.get("/api/bots", headers={"X-EasyAgent-Token": "nope"})
    assert wrong.status_code == 401
    forwarded = remote.get("/api/bots", headers={"X-Forwarded-For": "127.0.0.1"})
    assert forwarded.status_code == 401
    spoofed = remote.get("/api/bots", headers={"X-Forwarded-For": "127.0.0.1", "X-EasyAgent-Token": TOKEN})
    assert spoofed.status_code == 200
    authed = remote.get(
        f"/api/bots/{bot['id']}/chats/{chat['id']}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert authed.status_code == 200
    assert authed.json()["messages"][0]["content"] == "keep-me-on-the-computer"
    assert authed.headers["cache-control"] == "no-store"
    assert TestClient(app, client=LOCAL).get("/api/bots").status_code == 200
    assert token_matches(TOKEN, TOKEN)
    assert not token_matches("nope", TOKEN)
