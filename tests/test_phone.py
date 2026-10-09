"""A phone on the home LAN pairs with a token. Anywhere else is refused."""

import hashlib
import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image

from easyagent.access import access_decision, is_lan, lan_ip
from easyagent.app import create_app
from easyagent.icons import render_face_icon
from easyagent.phone import FIREWALL_HINT, PhoneBook, add_firewall_rule, launcher_firewall
from easyagent.store import Store
from easyagent.__main__ import attach_lan

ROOT = Path(__file__).resolve().parents[1]
LAN = ("192.168.1.40", 40000)
PUBLIC = ("203.0.113.20", 40000)
LOCAL = ("127.0.0.1", 9)
FACE_SVG_SHA = "5fd76112b544fa312b76a9c8827790210d9769137bfc32cb20781e7bc9bbd5ff"


class _Phone:
    def __init__(self, enabled, accepted):
        self.enabled = enabled
        self.accepted = accepted

    def accepts(self, presented):
        return presented in self.accepted


def test_lan_is_private_and_public_is_not():
    assert is_lan("192.168.1.40")
    assert is_lan("10.1.2.3")
    assert is_lan("172.30.0.2")
    assert is_lan("::ffff:192.168.0.10")
    assert not is_lan("127.0.0.1")
    assert not is_lan("203.0.113.20")
    assert not is_lan("8.8.8.8")
    assert not is_lan("testclient")
    assert access_decision("192.168.1.40", "") == "refuse"
    assert access_decision("192.168.1.40", "yes", _Phone(True, {"yes"})) == "allow"
    assert access_decision("192.168.1.40", "no", _Phone(True, {"yes"})) == "need_token"
    assert access_decision("192.168.1.40", "yes", _Phone(False, {"yes"})) == "refuse"
    assert access_decision("203.0.113.20", "yes", _Phone(True, {"yes"})) == "refuse"


def _seed(tmp_path):
    store = Store(tmp_path)
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ann", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    chat["messages"] = [{"role": "user", "content": "keep-me-on-the-computer"}]
    store.save_chat(chat)
    return bot, chat


def test_public_ip_is_refused_even_with_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_TOKEN", "desk-token-not-a-transcript")
    app = create_app(tmp_path)
    app.state.phone.set_enabled(True)
    _seed(tmp_path)
    remote = TestClient(app, client=PUBLIC)
    page = remote.get("/", headers={"Authorization": "Bearer desk-token-not-a-transcript"})
    assert page.status_code == 403
    assert "This network is not home." in page.text
    assert "keep-me-on-the-computer" not in page.text
    assert "Inter" in page.text
    api = remote.get("/api/bots", headers={"X-EasyAgent-Token": "desk-token-not-a-transcript"})
    assert api.status_code == 403
    assert "keep-me-on-the-computer" not in api.text


def test_lan_without_a_token_is_rejected_and_a_paired_token_works(tmp_path, monkeypatch):
    monkeypatch.delenv("EASYAGENT_TOKEN", raising=False)
    app = create_app(tmp_path)
    bot, chat = _seed(tmp_path)
    phone = TestClient(app, client=LAN)
    off = phone.get("/")
    assert off.status_code == 403
    assert "Phone access is off." in off.text
    assert "keep-me-on-the-computer" not in off.text
    assert phone.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").status_code == 403

    local = TestClient(app, client=LOCAL)
    turned = local.post("/api/phone", json={"enabled": True})
    assert turned.status_code == 200
    body = turned.json()
    assert body["enabled"] is True
    assert body["pair_url"].startswith("http://")
    assert "?pair=" in body["pair_url"]
    invite = body["pair_url"].split("?pair=", 1)[1]
    assert FIREWALL_HINT in body["firewall"]["hint"]
    qr = local.get("/api/phone/qr.svg")
    assert qr.status_code == 200
    assert qr.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in qr.text
    assert invite not in qr.text

    shell = phone.get("/")
    assert shell.status_code == 200
    assert "keep-me-on-the-computer" not in shell.text
    assert phone.get("/sw.js").status_code == 200
    manifest = phone.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"
    assert manifest.json()["start_url"] == "/"
    assert manifest.json()["icons"][0]["sizes"] == "180x180"
    paired = phone.get("/manifest.webmanifest", params={"pair": "abcDEF1234567890token"})
    assert paired.json()["start_url"] == "/?pair=abcDEF1234567890token"
    assert phone.get("/manifest.webmanifest", params={"pair": "../secret"}).json()["start_url"] == "/"
    missed = phone.get(f"/api/bots/{bot['id']}/chats/{chat['id']}")
    assert missed.status_code == 401
    assert "keep-me-on-the-computer" not in missed.text
    query = phone.get("/api/bots", params={"pair": invite, "token": invite})
    assert query.status_code == 401
    wrong = phone.get("/api/bots", headers={"X-EasyAgent-Token": "nope"})
    assert wrong.status_code == 401
    assert phone.get("/api/bots", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401

    opened = phone.get(
        f"/api/bots/{bot['id']}/chats/{chat['id']}",
        headers={"Authorization": f"Bearer {invite}", "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"},
    )
    assert opened.status_code == 200
    assert opened.json()["messages"][0]["content"] == "keep-me-on-the-computer"
    assert opened.headers["cache-control"] == "no-store"
    forwarded = phone.get("/api/bots", headers={"X-Forwarded-For": "127.0.0.1", "X-EasyAgent-Token": invite})
    assert forwarded.status_code == 200
    again = local.get("/api/phone").json()
    assert again["pair_url"].split("?pair=", 1)[1] != invite
    assert again["devices"][0]["name"] == "iPhone"
    assert "token" not in again["devices"][0]
    still = phone.get("/api/bots", headers={"X-EasyAgent-Token": invite})
    assert still.status_code == 200
    secret = phone.get("/api/phone", headers={"X-EasyAgent-Token": invite})
    assert secret.status_code == 403
    assert invite not in secret.text
    blocked = phone.post("/api/phone", json={"enabled": False}, headers={"X-EasyAgent-Token": invite})
    assert blocked.status_code == 403

    revoked = local.delete(f"/api/phone/devices/{again['devices'][0]['id']}")
    assert revoked.status_code == 200
    assert revoked.json()["devices"] == []
    assert phone.get("/api/bots", headers={"X-EasyAgent-Token": invite}).status_code == 401
    assert local.delete("/api/phone/devices/missing").status_code == 404
    assert TestClient(app, client=LOCAL).get("/api/bots").status_code == 200


def test_env_token_works_on_the_lan_only_while_phone_access_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_TOKEN", "desk-token-not-a-transcript")
    app = create_app(tmp_path)
    phone = TestClient(app, client=LAN)
    assert phone.get("/api/health", headers={"X-EasyAgent-Token": "desk-token-not-a-transcript"}).status_code == 403
    TestClient(app, client=LOCAL).post("/api/phone", json={"enabled": True})
    assert phone.get("/api/health", headers={"X-EasyAgent-Token": "desk-token-not-a-transcript"}).status_code == 200
    assert phone.get("/api/health").status_code == 401


def test_firewall_rule_needs_consent_and_windows(monkeypatch):
    calls = []

    def runner(command):
        calls.append(command)
        return type("Done", (), {"returncode": 0})()

    refused = add_firewall_rule(44721, False, platform_name="win32", runner=runner)
    assert refused["added"] is False
    assert calls == []
    other = add_firewall_rule(44721, True, platform_name="linux", runner=runner)
    assert "not Windows" in other["detail"]
    assert calls == []
    added = add_firewall_rule(44721, True, platform_name="win32", runner=runner)
    assert added["added"] is True
    assert calls[0][0] == "netsh"
    assert "localport=44721" in calls[0]
    assert "remoteip=localsubnet" in calls[0]
    monkeypatch.delenv("EASYAGENT_FIREWALL", raising=False)
    assert launcher_firewall(True, 44721) is None
    monkeypatch.setenv("EASYAGENT_FIREWALL", "1")
    assert launcher_firewall(False, 44721) is None


def test_icons_are_the_locked_face():
    svg_path = ROOT / "easyagent" / "static" / "face.svg"
    svg = svg_path.read_text(encoding="utf-8")
    assert hashlib.sha256(svg.encode("utf-8")).hexdigest() == FACE_SVG_SHA
    icon_dir = ROOT / "easyagent" / "static" / "icons"
    for size, name in ((180, "apple-touch-icon.png"), (192, "icon-192.png"), (512, "icon-512.png")):
        fresh = render_face_icon(svg, size)
        saved = Image.open(icon_dir / name).convert("RGBA")
        assert saved.size == (size, size)
        assert list(fresh.get_flattened_data()) == list(saved.get_flattened_data())
    sw = (ROOT / "easyagent" / "static" / "sw.js").read_text(encoding="utf-8")
    assert 'pathname.startsWith("/api")' in sw
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "viewport-fit=cover" in page
    assert 'rel="manifest"' in page
    assert "apple-mobile-web-app-capable" in page
    built = (ROOT / "easyagent" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "viewport-fit=cover" in built
    assert "/manifest.webmanifest" in built


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_phone_access_opens_and_closes_the_lan_socket(tmp_path, monkeypatch):
    monkeypatch.delenv("EASYAGENT_TOKEN", raising=False)
    address = lan_ip()
    assert address
    app = create_app(tmp_path)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    attach_lan(server, app)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not server.started:
            time.sleep(0.02)
        assert server.started
        assert httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2).status_code == 200
        with socket.socket() as probe:
            probe.settimeout(1)
            refused = probe.connect_ex((address, port))
        assert refused != 0
        turned = httpx.post(f"http://127.0.0.1:{port}/api/phone", json={"enabled": True}, timeout=5)
        assert turned.status_code == 200
        assert turned.json()["listening"] is True
        missed = httpx.get(f"http://{address}:{port}/api/health", timeout=2)
        assert missed.status_code == 401
        hosts = []
        for item in server.servers:
            for sock in item.sockets or []:
                hosts.append(sock.getsockname()[0])
        assert "127.0.0.1" in hosts
        assert address in hosts
        assert "0.0.0.0" not in hosts
        httpx.post(f"http://127.0.0.1:{port}/api/phone", json={"enabled": False}, timeout=5)
        with socket.socket() as probe:
            probe.settimeout(1)
            closed = probe.connect_ex((address, port))
        assert closed != 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)
