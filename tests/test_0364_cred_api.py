"""Wave 3: the credential endpoints and the login-form hint. Values are never echoed.

The endpoints list ``{host, name}`` rows only; a POST puts the value straight into
secrets.db and no response body ever carries it. On a page with a password field,
the read snapshot names the saved sign-ins for that host so the model offers
``vault:<name>`` instead of hitting the takeover wall.
"""

import asyncio
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["EASYAGENT_BROWSER_HEADLESS"] = "1"

from easyagent.app import create_app
from easyagent.browser import reset_for_tests
from easyagent.secrets import get_web_credential, put_web_credential
from easyagent.store import Store
from easyagent.tools import ToolRequest, execute

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "browser"
SECRET = "ada-s3cret-passphrase-9182"


@pytest.fixture(autouse=True)
def _close_browsers():
    yield
    reset_for_tests()


def _world(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    client = TestClient(create_app(tmp_path))
    endpoint = client.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    bot = client.post(
        "/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"], "model": None}
    ).json()
    return client, bot


def _base(bot) -> str:
    return f"/api/bots/{bot['id']}/browser/credentials"


# endpoints: POST / GET / DELETE round-trip, names only


def test_credential_round_trip_never_echoes_the_value(tmp_path, monkeypatch):
    client, bot = _world(tmp_path, monkeypatch)
    posted = client.post(_base(bot), json={"host": "github.com", "name": "ada", "value": SECRET})
    assert posted.status_code == 200, posted.text
    assert SECRET not in posted.text

    listed = client.get(_base(bot))
    assert listed.status_code == 200, listed.text
    rows = listed.json()["credentials"]
    assert {"host": "github.com", "name": "ada"} in rows
    assert SECRET not in listed.text  # names and hosts only
    assert all("value" not in row for row in rows)

    # the value really landed where the fill path reads it
    assert get_web_credential("github.com", "ada", Store(tmp_path)) == SECRET

    deleted = client.delete(_base(bot), params={"host": "github.com", "name": "ada"})
    assert deleted.status_code == 200, deleted.text
    assert SECRET not in deleted.text

    after = client.get(_base(bot))
    assert {"host": "github.com", "name": "ada"} not in after.json()["credentials"]
    assert SECRET not in after.text


def test_post_refuses_an_uppercase_host(tmp_path, monkeypatch):
    client, bot = _world(tmp_path, monkeypatch)
    posted = client.post(_base(bot), json={"host": "GitHub.com", "name": "ada", "value": SECRET})
    assert posted.status_code == 400  # an uppercase host is refused, not stored
    assert SECRET not in posted.text


def test_malformed_posts_are_plain_errors_and_store_nothing(tmp_path, monkeypatch):
    client, bot = _world(tmp_path, monkeypatch)
    for bad in (
        {"host": "github.com", "name": "ada", "value": ""},
        {"host": "github.com", "name": "", "value": SECRET},
        {"host": "", "name": "ada", "value": SECRET},
        {"host": "GitHub.com", "name": "ada", "value": SECRET},
    ):
        response = client.post(_base(bot), json=bad)
        assert response.status_code == 400, bad
        detail = response.json()["detail"]
        assert isinstance(detail, str) and detail
        assert SECRET not in response.text
    assert client.get(_base(bot)).json()["credentials"] == []


def test_unknown_bot_is_404(tmp_path, monkeypatch):
    client, _bot = _world(tmp_path, monkeypatch)
    missing = "/api/bots/00000000-0000-0000-0000-000000000000/browser/credentials"
    assert client.get(missing).status_code == 404
    assert client.post(missing, json={"host": "a.com", "name": "n", "value": "v"}).status_code == 404
    assert client.delete(missing, params={"host": "a.com", "name": "n"}).status_code == 404


# snapshot hint: a login form names the saved sign-ins for its host


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return


def _serve() -> ThreadingHTTPServer:
    handler = partial(_Quiet, directory=str(FIXTURE))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _bot(tmp_path: Path):
    os.environ["EASYAGENT_KEYRING"] = "memory"
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    return store, bot


def test_login_form_lists_saved_sign_in_names(tmp_path):
    os.environ["EASYAGENT_KEYRING"] = "memory"
    server = _serve()
    port = server.server_address[1]
    store, bot = _bot(tmp_path)
    put_web_credential("127.0.0.1", "ada", SECRET, store)
    put_web_credential("127.0.0.1", "work", "other-value-7", store)
    put_web_credential("example.org", "elsewhere", "far-value-9", store)
    base = f"http://127.0.0.1:{port}"

    async def go(action: str, path: str = "") -> str:
        return await execute(
            store,
            ToolRequest(kind="browser", action=action, path=path, body=""),
            bot["id"],
        )

    async def once():
        await go("open", f"{base}/login.html")
        page = await go("read")
        assert "Saved sign-ins for this host: ada, work." in page
        assert SECRET not in page and "other-value-7" not in page
        assert "elsewhere" not in page  # another host's names stay out

        plain = await go("open", f"{base}/get.html")
        assert "Saved sign-ins" not in plain  # no password field, no hint
        page2 = await go("read")
        assert "Saved sign-ins" not in page2

    try:
        asyncio.run(once())
    finally:
        server.shutdown()


def test_login_form_without_saved_names_adds_no_line(tmp_path):
    os.environ["EASYAGENT_KEYRING"] = "memory"
    server = _serve()
    port = server.server_address[1]
    store, bot = _bot(tmp_path)
    put_web_credential("example.org", "elsewhere", "far-value-9", store)
    base = f"http://127.0.0.1:{port}"

    async def go(action: str, path: str = "") -> str:
        return await execute(
            store,
            ToolRequest(kind="browser", action=action, path=path, body=""),
            bot["id"],
        )

    async def once():
        await go("open", f"{base}/login.html")
        page = await go("read")
        assert "Saved sign-ins" not in page

    try:
        asyncio.run(once())
    finally:
        server.shutdown()
