"""Phone access means the token. A paired device works, a revoked one does not.

Loopback stays trusted. A LAN address with Phone access on must present a
claimed device token or the shared EASYAGENT_TOKEN. Wrong tokens are counted
per address and a guesser is locked out for a while.
"""

from fastapi.testclient import TestClient

from easyagent.access import TokenGuessGuard, access_decision
from easyagent.app import create_app
from easyagent.store import Store

LAN = ("192.168.1.40", 40000)
LOCAL = ("127.0.0.1", 9)
PUBLIC = ("203.0.113.20", 40000)


def _seed(tmp_path):
    store = Store(tmp_path)
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ann", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    chat["messages"] = [{"role": "user", "content": "keep-me-on-the-computer"}]
    store.save_chat(chat)
    return bot, chat


def _enable(app) -> str:
    turned = TestClient(app, client=LOCAL).post("/api/phone", json={"enabled": True})
    assert turned.status_code == 200
    return turned.json()["pair_url"].split("?pair=", 1)[1]


def test_loopback_stays_trusted_when_phone_access_is_on(tmp_path, monkeypatch):
    monkeypatch.delenv("EASYAGENT_TOKEN", raising=False)
    app = create_app(tmp_path)
    bot, _ = _seed(tmp_path)
    _enable(app)
    assert TestClient(app, client=LOCAL).get("/api/bots").status_code == 200


def test_lan_without_a_token_sees_the_token_gate(tmp_path, monkeypatch):
    monkeypatch.delenv("EASYAGENT_TOKEN", raising=False)
    app = create_app(tmp_path)
    bot, chat = _seed(tmp_path)
    _enable(app)
    phone = TestClient(app, client=LAN)
    api = phone.get(f"/api/bots/{bot['id']}/chats/{chat['id']}")
    assert api.status_code == 401
    assert api.json()["detail"] == "This phone needs a pairing token."
    assert "keep-me-on-the-computer" not in api.text
    # The shell still loads so the token gate can ask for a token.
    assert phone.get("/").status_code == 200


def test_paired_device_token_works_and_revocation_locks_it_out(tmp_path, monkeypatch):
    monkeypatch.delenv("EASYAGENT_TOKEN", raising=False)
    app = create_app(tmp_path)
    bot, chat = _seed(tmp_path)
    invite = _enable(app)
    phone = TestClient(app, client=LAN)
    opened = phone.get(
        f"/api/bots/{bot['id']}/chats/{chat['id']}",
        headers={"Authorization": f"Bearer {invite}", "User-Agent": "Mozilla/5.0 (iPhone)"},
    )
    assert opened.status_code == 200
    assert opened.json()["messages"][0]["content"] == "keep-me-on-the-computer"
    # The invite rotated into a device token. The device keeps its token.
    again = TestClient(app, client=LOCAL).get("/api/phone").json()
    assert again["pair_url"].split("?pair=", 1)[1] != invite
    device_id = again["devices"][0]["id"]
    assert phone.get("/api/bots", headers={"X-EasyAgent-Token": invite}).status_code == 200
    # Revoking bites at once: no cache holds the old decision.
    revoked = TestClient(app, client=LOCAL).delete(f"/api/phone/devices/{device_id}")
    assert revoked.status_code == 200
    assert revoked.json()["devices"] == []
    locked = phone.get("/api/bots", headers={"X-EasyAgent-Token": invite})
    assert locked.status_code == 401
    assert "keep-me-on-the-computer" not in locked.text


def test_shared_env_token_opens_the_lan(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_TOKEN", "desk-token-not-a-transcript")
    app = create_app(tmp_path)
    _seed(tmp_path)
    phone = TestClient(app, client=LAN)
    # Phone access off: the env token opens nothing, same as before.
    assert phone.get("/api/health").status_code == 403
    _enable(app)
    ok = phone.get("/api/health", headers={"X-EasyAgent-Token": "desk-token-not-a-transcript"})
    assert ok.status_code == 200


def test_wrong_token_guessing_locks_the_address_out(tmp_path, monkeypatch):
    monkeypatch.delenv("EASYAGENT_TOKEN", raising=False)
    app = create_app(tmp_path)
    _seed(tmp_path)
    invite = _enable(app)
    app.state.token_guard = TokenGuessGuard(max_wrong=3, window_seconds=300, lockout_seconds=300)
    phone = TestClient(app, client=LAN)
    for _ in range(2):
        wrong = phone.get("/api/bots", headers={"X-EasyAgent-Token": "nope"})
        assert wrong.status_code == 401
    third = phone.get("/api/bots", headers={"X-EasyAgent-Token": "nope"})
    assert third.status_code == 429
    assert third.json()["detail"] == "Too many wrong tokens. Wait a few minutes and try again."
    # While locked, more guessing still gets the lockout message.
    fourth = phone.get("/api/bots", headers={"X-EasyAgent-Token": "still-nope"})
    assert fourth.status_code == 429
    # The gate itself stays open: no token is not a guess.
    assert phone.get("/api/bots").status_code == 401
    # A right token still opens the door and clears the counter.
    assert phone.get("/api/bots", headers={"X-EasyAgent-Token": invite}).status_code == 200


def test_guard_lockout_expires_and_window_slides():
    clock = {"t": 0.0}
    guard = TokenGuessGuard(
        max_wrong=2, window_seconds=10.0, lockout_seconds=5.0, clock=lambda: clock["t"]
    )
    assert not guard.locked("192.168.1.40")
    guard.note_wrong("192.168.1.40")
    assert not guard.locked("192.168.1.40")
    guard.note_wrong("192.168.1.40")
    assert guard.locked("192.168.1.40")
    clock["t"] += 5.0
    assert not guard.locked("192.168.1.40")
    # Sliding window: two wrongs more than a window apart never lock.
    guard.note_wrong("192.168.1.41")
    clock["t"] += 11.0
    guard.note_wrong("192.168.1.41")
    assert not guard.locked("192.168.1.41")
    # note_ok clears everything.
    guard.note_wrong("192.168.1.42")
    guard.note_wrong("192.168.1.42")
    assert guard.locked("192.168.1.42")
    guard.note_ok("192.168.1.42")
    assert not guard.locked("192.168.1.42")


def test_decision_shape_is_unchanged():
    class _Phone:
        def __init__(self, enabled, accepted):
            self.enabled = enabled
            self.accepted = accepted

        def accepts(self, presented):
            return presented in self.accepted

    on = _Phone(True, {"yes"})
    assert access_decision("127.0.0.1", "") == "allow"
    assert access_decision("192.168.1.40", "", on) == "need_token"
    assert access_decision("192.168.1.40", "yes", on) == "allow"
    assert access_decision("192.168.1.40", "no", on) == "need_token"
    assert access_decision("192.168.1.40", "yes", _Phone(False, {"yes"})) == "refuse"
    assert access_decision("203.0.113.20", "yes", on) == "refuse"
    assert access_decision("203.0.113.20", "", None) == "refuse"


def test_phone_access_off_keeps_the_old_answers(tmp_path, monkeypatch):
    monkeypatch.delenv("EASYAGENT_TOKEN", raising=False)
    app = create_app(tmp_path)
    bot, chat = _seed(tmp_path)
    phone = TestClient(app, client=LAN)
    page = phone.get("/")
    assert page.status_code == 403
    assert "Phone access is off." in page.text
    assert "keep-me-on-the-computer" not in page.text
    api = phone.get(f"/api/bots/{bot['id']}/chats/{chat['id']}")
    assert api.status_code == 403
    wrong = phone.get("/api/bots", headers={"X-EasyAgent-Token": "nope"})
    assert wrong.status_code == 403
    remote = TestClient(app, client=PUBLIC)
    assert remote.get("/").status_code == 403
    assert "This network is not home." in remote.get("/").text
    assert TestClient(app, client=LOCAL).get("/api/bots").status_code == 200
