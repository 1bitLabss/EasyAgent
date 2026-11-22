"""Per-bot web credentials: a bot's saved sign-in belongs to that bot; the global
credential stays as the fallback for every bot and for no-bot callers.

The bot-scoped account key is ``web|bot|<bot_id>|<host>|<name>``; the global shape
``web|<host>|<name>`` is unchanged. A bot-scoped row never leaks into a no-bot
listing, and a no-bot delete never touches a bot's row (and the other way round).
"""

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["EASYAGENT_BROWSER_HEADLESS"] = "1"

from easyagent.app import create_app
from easyagent.secrets import (
    get_web_credential,
    list_web_credentials,
    put_web_credential,
    remove_web_credential,
    web_credential_key,
)
from easyagent.store import Store

SECRET = "ada-s3cret-passphrase-9182"
OTHER = "grace-an0ther-passphrase-4410"


def _store(tmp_path: Path):
    os.environ["EASYAGENT_KEYRING"] = "memory"
    store = Store(tmp_path)
    store.ensure()
    return store


def _bots(tmp_path: Path):
    """Two bots in one store, via the API, like test_0364 builds its world."""
    os.environ["EASYAGENT_KEYRING"] = "memory"
    client = TestClient(create_app(tmp_path))
    endpoint = client.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    a = client.post(
        "/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"], "model": None}
    ).json()
    b = client.post(
        "/api/bots", json={"name": "Grace", "endpoint_id": endpoint["id"], "model": None}
    ).json()
    return client, a, b


def _base(bot) -> str:
    return f"/api/bots/{bot['id']}/browser/credentials"


def _accounts(tmp_path: Path) -> list[str]:
    """The on-disk account names (plaintext; values stay encrypted)."""
    db = tmp_path / "secrets.db"
    conn = sqlite3.connect(db)
    try:
        return [row[0] for row in conn.execute("SELECT account FROM secrets")]
    finally:
        conn.close()


# (a) one bot's credential is invisible to another bot


def test_bot_credential_isolated_between_bots(tmp_path):
    store = _store(tmp_path)
    put_web_credential("github.com", "ada", SECRET, store, bot_id="bot-a")
    assert get_web_credential("github.com", "ada", store, bot_id="bot-a") == SECRET
    assert get_web_credential("github.com", "ada", store, bot_id="bot-b") == ""
    rows_a = list_web_credentials(store, bot_id="bot-a")
    rows_b = list_web_credentials(store, bot_id="bot-b")
    assert {"host": "github.com", "name": "ada"} in rows_a
    assert {"host": "github.com", "name": "ada"} not in rows_b


# (b) a legacy global credential is the fallback for every bot


def test_legacy_global_credential_is_fallback_for_any_bot(tmp_path):
    store = _store(tmp_path)
    put_web_credential("github.com", "ada", SECRET, store)  # no bot_id: legacy shape
    assert get_web_credential("github.com", "ada", store, bot_id="bot-a") == SECRET
    assert get_web_credential("github.com", "ada", store, bot_id="bot-b") == SECRET
    assert {"host": "github.com", "name": "ada"} in list_web_credentials(store, bot_id="bot-a")


# (c) the bot's own value wins over a global one with the same host+name


def test_bot_value_beats_global_value(tmp_path):
    store = _store(tmp_path)
    put_web_credential("github.com", "ada", SECRET, store)  # global
    put_web_credential("github.com", "ada", OTHER, store, bot_id="bot-a")  # bot's own
    assert get_web_credential("github.com", "ada", store, bot_id="bot-a") == OTHER
    assert get_web_credential("github.com", "ada", store, bot_id="bot-b") == SECRET
    rows_a = list_web_credentials(store, bot_id="bot-a")
    same = [r for r in rows_a if r == {"host": "github.com", "name": "ada"}]
    assert len(same) == 1  # deduped, the bot row wins
    # bot rows come before other global rows in the listing
    put_web_credential("example.org", "work", OTHER, store)
    rows_a = list_web_credentials(store, bot_id="bot-a")
    assert rows_a[0] == {"host": "github.com", "name": "ada"}


# (d) the no-bot path is byte-identical to before


def test_legacy_no_bot_path_unchanged(tmp_path):
    store = _store(tmp_path)
    assert web_credential_key("GitHub.COM", " ada ") == "web|github.com|ada"
    assert web_credential_key("github.com", "ada", None) == "web|github.com|ada"
    assert web_credential_key("github.com", "ada", "") == "web|github.com|ada"
    assert web_credential_key("github.com", "ada", "bot-a") == "web|bot|bot-a|github.com|ada"
    put_web_credential("github.com", "ada", SECRET, store)
    assert get_web_credential("github.com", "ada", store) == SECRET
    assert get_web_credential("evil.com", "ada", store) == ""
    assert {"host": "github.com", "name": "ada"} in list_web_credentials(store)
    with pytest.raises(RuntimeError):
        put_web_credential("github.com", "  ", SECRET, store, bot_id="bot-a")
    remove_web_credential("github.com", "ada", store)
    assert get_web_credential("github.com", "ada", store) == ""


# (e) a bot-scoped row never leaks into a no-bot listing


def test_bot_rows_do_not_leak_into_no_bot_listing(tmp_path):
    store = _store(tmp_path)
    put_web_credential("github.com", "ada", SECRET, store, bot_id="bot-a")
    put_web_credential("example.org", "work", OTHER, store)  # global
    rows = list_web_credentials(store)
    assert {"host": "example.org", "name": "work"} in rows
    assert {"host": "github.com", "name": "ada"} not in rows
    blob = str(rows)
    assert SECRET not in blob and OTHER not in blob  # names and hosts only


# (f) a bot delete removes only the bot key; the global key survives (and vice versa)


def test_delete_scopes_to_one_key(tmp_path):
    store = _store(tmp_path)
    put_web_credential("github.com", "ada", SECRET, store)  # global
    put_web_credential("github.com", "ada", OTHER, store, bot_id="bot-a")
    remove_web_credential("github.com", "ada", store, bot_id="bot-a")
    assert get_web_credential("github.com", "ada", store, bot_id="bot-a") == SECRET  # global survives
    assert get_web_credential("github.com", "ada", store) == SECRET
    remove_web_credential("github.com", "ada", store)  # no bot: global only
    assert get_web_credential("github.com", "ada", store) == ""
    assert get_web_credential("github.com", "ada", store, bot_id="bot-a") == ""


# (g) endpoint level: POST via bot A is invisible to bot B; DELETE via A removes it for A


def test_endpoints_are_bot_scoped(tmp_path):
    client, a, b = _bots(tmp_path)
    posted = client.post(_base(a), json={"host": "github.com", "name": "ada", "value": SECRET})
    assert posted.status_code == 200, posted.text
    assert SECRET not in posted.text

    rows_b = client.get(_base(b)).json()["credentials"]
    assert {"host": "github.com", "name": "ada"} not in rows_b
    rows_a = client.get(_base(a)).json()["credentials"]
    assert {"host": "github.com", "name": "ada"} in rows_a
    assert SECRET not in client.get(_base(a)).text  # names and hosts only

    deleted = client.delete(_base(a), params={"host": "github.com", "name": "ada"})
    assert deleted.status_code == 200, deleted.text
    assert {"host": "github.com", "name": "ada"} not in client.get(_base(a)).json()["credentials"]

    # a global credential written without a bot still shows for every bot (fallback)
    store = Store(tmp_path)
    put_web_credential("example.org", "shared", OTHER, store)
    assert {"host": "example.org", "name": "shared"} in client.get(_base(b)).json()["credentials"]
    assert {"host": "example.org", "name": "shared"} in client.get(_base(a)).json()["credentials"]


# (h) the on-disk account key for a bot-scoped credential


def test_on_disk_account_key_shape(tmp_path):
    store = _store(tmp_path)
    put_web_credential("github.com", "ada", SECRET, store, bot_id="bot-7")
    put_web_credential("github.com", "ada", OTHER, store)
    accounts = _accounts(tmp_path)
    assert "web|bot|bot-7|github.com|ada" in accounts
    assert "web|github.com|ada" in accounts
