"""0.3.7 encrypted secrets. A timeout does not mint a key, and a bot shell never sees one."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.store import Store

_KEY = "ea-" + "key-" + "0137-" + "fixture"
_OTHER = "ea-" + "key-" + "0137-" + "other"
_ENDPOINT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _plaintext(root, *, bridges=True):
    root.mkdir(parents=True, exist_ok=True)
    (root / "endpoints.json").write_text(
        json.dumps([{
            "id": _ENDPOINT,
            "name": "Local model",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": _KEY,
            "model": "your-model",
        }]),
        encoding="utf-8",
    )
    if not bridges:
        return
    (root / "bridges.json").write_text(
        json.dumps([{"id": "bridge-1", "token": _KEY}]),
        encoding="utf-8",
    )
    (root / "connectors.json").write_text(
        json.dumps([{"id": "conn-1", "api_key": _KEY, "secrets": {"Authorization": _KEY}}]),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps([{"id": "mcp-1", "token": _KEY}]),
        encoding="utf-8",
    )


def _grep(root) -> bytes:
    blob = b""
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            blob += path.read_bytes()
    return blob


def test_a_keychain_timeout_does_not_mint_a_key(tmp_path, monkeypatch):
    from easyagent.secrets import SecretError, put_secret

    monkeypatch.delenv("EASYAGENT_KEYRING", raising=False)
    calls = []

    def fake(op, account, value):
        calls.append((op, account))
        if account == "easyagent-probe":
            return "missing", ""
        return "error", "timed out"

    monkeypatch.setattr("easyagent.secrets._keyring", fake)
    with pytest.raises(SecretError, match="No new master key"):
        put_secret("endpoint:one:api_key", _KEY, Store(tmp_path / "data"))
    assert ("set", "secrets-master") not in calls
    assert not (tmp_path / "data" / "secrets.db").exists()


def test_a_backend_switch_is_refused(tmp_path, monkeypatch):
    from easyagent.secrets import SecretError, get_secret, put_secret

    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    store = Store(tmp_path / "data")
    store.ensure()
    put_secret("endpoint:one:api_key", _KEY, store)
    monkeypatch.setenv("EASYAGENT_KEYRING", "passphrase")
    monkeypatch.setenv("EASYAGENT_SECRETS_PASSPHRASE", "correct-horse-battery")
    with pytest.raises(SecretError, match="sealed with memory"):
        get_secret("endpoint:one:api_key", store)


def test_a_verifier_mismatch_refuses_and_keeps_the_file(tmp_path, monkeypatch):
    from easyagent.secrets import SecretError, get_secret

    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    monkeypatch.setenv("EASYAGENT_MIGRATION_BACKUP", str(tmp_path / "backup"))
    root = tmp_path / "data"
    _plaintext(root, bridges=False)
    store = Store(root)
    store.ensure()
    assert _KEY.encode("utf-8") not in (root / "endpoints.json").read_bytes()
    (root / "endpoints.json").write_text(
        json.dumps([{
            "id": _ENDPOINT,
            "name": "Local model",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": _KEY,
            "model": "your-model",
        }]),
        encoding="utf-8",
    )
    conn = sqlite3.connect(root / "secrets.db")
    conn.execute("UPDATE meta SET value = ? WHERE key = 'verifier'", (b"not-a-verifier",))
    conn.commit()
    conn.close()
    with pytest.raises(SecretError, match="does not match"):
        store.ensure()
    assert _KEY.encode("utf-8") in (root / "endpoints.json").read_bytes()
    with pytest.raises(SecretError, match="does not match"):
        get_secret(f"endpoint:{_ENDPOINT}:api_key", store)


def test_a_user_passphrase_uses_600k_rounds(tmp_path, monkeypatch):
    from easyagent.secrets import _meta_get, put_secret

    monkeypatch.setenv("EASYAGENT_KEYRING", "passphrase")
    monkeypatch.setenv("EASYAGENT_SECRETS_PASSPHRASE", "correct-horse-battery")
    monkeypatch.setenv("EASYAGENT_SECRETS_PASSFILE", str(tmp_path / "unused.pass"))
    store = Store(tmp_path / "data")
    put_secret("endpoint:one:api_key", _KEY, store)
    assert _meta_get(store.root, "kdf_rounds") == b"600000"
    assert _KEY.encode("utf-8") not in (store.root / "secrets.db").read_bytes()
    assert not (tmp_path / "unused.pass").exists()


def test_a_swapped_secret_does_not_open(tmp_path, monkeypatch):
    from easyagent.secrets import get_secret, put_secret

    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    store = Store(tmp_path / "data")
    put_secret("endpoint:one:api_key", _KEY, store)
    put_secret("endpoint:two:api_key", _OTHER, store)
    conn = sqlite3.connect(store.root / "secrets.db")
    rows = conn.execute("SELECT account, nonce, ciphertext FROM secrets").fetchall()
    by_name = {row[0]: row for row in rows}
    first = by_name["endpoint:one:api_key"]
    second = by_name["endpoint:two:api_key"]
    conn.execute(
        "UPDATE secrets SET nonce = ?, ciphertext = ? WHERE account = ?",
        (second[1], second[2], first[0]),
    )
    conn.commit()
    conn.close()
    opened = get_secret("endpoint:one:api_key", store)
    assert opened != _OTHER
    assert opened != _KEY


def test_a_failed_round_trip_restores_the_json(tmp_path, monkeypatch):
    from easyagent.secrets import SecretError, migrate_store

    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    monkeypatch.setenv("EASYAGENT_MIGRATION_BACKUP", str(tmp_path / "backup"))
    root = tmp_path / "data"
    _plaintext(root, bridges=False)
    store = Store(root)

    def boom(_store, _jobs):
        raise SecretError("round trip failed")

    monkeypatch.setattr("easyagent.secrets._round_trip", boom)
    with pytest.raises(SecretError, match="round trip"):
        migrate_store(store)
    assert _KEY.encode("utf-8") in (root / "endpoints.json").read_bytes()
    assert any((tmp_path / "backup").rglob("endpoints.json"))
    backup = next((tmp_path / "backup").rglob("endpoints.json"))
    assert store.root.resolve() not in backup.resolve().parents


def test_migrated_files_hold_no_plaintext_key(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    monkeypatch.setenv("EASYAGENT_MIGRATION_BACKUP", str(tmp_path / "backup"))
    root = tmp_path / "data"
    _plaintext(root)
    store = Store(root)
    store.ensure()
    assert _KEY.encode("utf-8") not in _grep(store.root)
    assert (store.root / "secrets.db").is_file()
    opened = store.get_endpoint(_ENDPOINT)
    assert opened is not None
    assert opened["api_key"] == _KEY
    assert not any((tmp_path / "backup").rglob("endpoints.json"))
    for name in ("bridges.json", "connectors.json", "mcp.json", "endpoints.json"):
        assert _KEY.encode("utf-8") not in (store.root / name).read_bytes()


def test_a_newline_byte_in_the_passphrase_file_is_repaired(tmp_path, monkeypatch):
    import os

    from easyagent import secrets
    from easyagent.secrets import get_secret, put_secret
    from easyagent.vault import open_vault, seal

    monkeypatch.setenv("EASYAGENT_KEYRING", "passphrase")
    monkeypatch.delenv("EASYAGENT_SECRETS_PASSPHRASE", raising=False)
    phrase = tmp_path / "outside" / "secrets.passphrase"
    monkeypatch.setenv("EASYAGENT_SECRETS_PASSFILE", str(phrase))
    real = os.urandom

    def urandom(n):
        data = bytearray(real(n))
        data[0] = 0x0A
        return bytes(data)

    monkeypatch.setattr(secrets.os, "urandom", urandom)
    store = Store(tmp_path / "data")
    store.ensure()
    put_secret("endpoint:one:api_key", _KEY, store)
    original = phrase.read_bytes()
    assert original[0] == 0x0A
    assert len(original) == 32
    phrase.write_bytes(original.replace(b"\n", b"\r\n"))
    assert len(phrase.read_bytes()) >= 33
    secrets._DERIVED.clear()
    assert get_secret("endpoint:one:api_key", store) == _KEY
    assert phrase.read_bytes() == original

    key_path = tmp_path / "vault.key"
    monkeypatch.setenv("EASYAGENT_VAULT_KEY_FILE", str(key_path))
    key = bytes(range(32))
    assert 0x0A in key
    key_path.write_bytes(key)
    sealed = seal({"user": "ada", "secret": _OTHER})
    key_path.write_bytes(key.replace(b"\n", b"\r\n"))
    assert len(key_path.read_bytes()) >= 33
    assert open_vault(sealed)["secret"] == _OTHER
    assert key_path.read_bytes() == key


def test_passphrase_store_is_ciphertext_outside_data(tmp_path, monkeypatch):
    from easyagent.secrets import put_secret

    monkeypatch.setenv("EASYAGENT_KEYRING", "passphrase")
    monkeypatch.delenv("EASYAGENT_SECRETS_PASSPHRASE", raising=False)
    phrase = tmp_path / "outside" / "secrets.passphrase"
    monkeypatch.setenv("EASYAGENT_SECRETS_PASSFILE", str(phrase))
    monkeypatch.setenv("EASYAGENT_MIGRATION_BACKUP", str(tmp_path / "backup"))
    root = tmp_path / "data"
    _plaintext(root, bridges=False)
    store = Store(root)
    store.ensure()
    assert phrase.is_file()
    assert len(phrase.read_bytes()) == 32
    assert store.root.resolve() not in phrase.resolve().parents
    assert not (store.root / "secrets.passphrase").exists()
    assert _KEY.encode("utf-8") not in (store.root / "secrets.db").read_bytes()
    assert store.get_endpoint(_ENDPOINT)["api_key"] == _KEY
    put_secret("endpoint:again:api_key", _OTHER, store)
    assert get_round_trip(store) == _OTHER


def get_round_trip(store):
    from easyagent.secrets import get_secret

    return get_secret("endpoint:again:api_key", store)


def test_passphrase_file_inside_data_is_refused(tmp_path, monkeypatch):
    from easyagent.secrets import SecretError, put_secret

    monkeypatch.setenv("EASYAGENT_KEYRING", "passphrase")
    monkeypatch.delenv("EASYAGENT_SECRETS_PASSPHRASE", raising=False)
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("EASYAGENT_SECRETS_PASSFILE", str(root / "secrets.passphrase"))
    with pytest.raises(SecretError, match="outside the data directory"):
        put_secret("endpoint:one:api_key", _KEY, Store(root))
    assert not (root / "secrets.passphrase").exists()
    assert not (root / "secrets.db").exists()


def test_the_bot_shell_env_has_no_secret(tmp_path, monkeypatch):
    from easyagent import turn as turn_mod
    from easyagent.secrets import put_secret
    from easyagent.tools import _run_shell

    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    monkeypatch.setenv("EASYAGENT_SECRETS_PASSPHRASE", "correct-horse-battery")
    monkeypatch.setenv("EASYAGENT_SECRETS_PASSFILE", str(tmp_path / "outside.pass"))
    monkeypatch.setenv("NOTES", _KEY)
    store = Store(tmp_path / "data")
    store.ensure()
    put_secret("endpoint:one:api_key", _KEY, store)
    slot = turn_mod.slot_for(store, "chat-shell")
    slot.bot_id = "bot"
    slot.store = store
    slot.cwd = str(tmp_path)
    slot.env = dict(**{key: value for key, value in __import__("os").environ.items()})
    captured = {}
    real_popen = __import__("subprocess").Popen

    def wrapped(*args, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("easyagent.tools.subprocess.Popen", wrapped)
    token = turn_mod._slot.set(slot)
    try:
        import sys

        py = f'"{sys.executable}"' if " " in sys.executable else sys.executable
        heard = _run_shell(
            store,
            py + " -c \"import os; print('CLEAN' if 'EASYAGENT_SECRETS_PASSPHRASE' not in os.environ else 'DIRTY')\"",
        )
    finally:
        turn_mod._slot.reset(token)
    assert "CLEAN" in heard
    assert "DIRTY" not in heard
    env = captured["env"]
    assert "EASYAGENT_SECRETS_PASSPHRASE" not in env
    assert "EASYAGENT_SECRETS_PASSFILE" not in env
    assert "EASYAGENT_KEYRING" not in env
    assert "NOTES" not in env
    assert all(_KEY not in str(value) for value in env.values())
    assert _KEY not in heard


def test_data_guard_covers_secrets_db_and_the_passphrase(tmp_path, monkeypatch):
    from easyagent import turn as turn_mod
    from easyagent.psast import data_decision
    from easyagent.tools import ToolError, _run_shell, _user_path

    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    phrase = tmp_path / "outside" / "secrets.passphrase"
    phrase.parent.mkdir()
    phrase.write_bytes(b"y" * 32)
    monkeypatch.setenv("EASYAGENT_SECRETS_PASSFILE", str(phrase))
    root = tmp_path / "data"
    root.mkdir()
    store = Store(root)
    store.ensure()
    bot = store.add_bot(name="Ada", endpoint_id=_endpoint(store), model="your-model")
    workspace = root / "bots" / bot["id"] / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    secret_db = root / "secrets.db"
    assert data_decision(store, f"Get-Content {secret_db}", workspace, bot["id"]) == "block"
    assert data_decision(store, f"Get-Content {phrase}", workspace, bot["id"]) == "block"
    slot = turn_mod.slot_for(store, "chat-guard")
    slot.bot_id = bot["id"]
    slot.store = store
    slot.cwd = str(workspace)
    slot.env = {}
    turn_mod._slot.set(slot)
    try:
        with pytest.raises(ToolError, match="saved secret|saved chats"):
            _run_shell(store, f"cat {secret_db}")
        with pytest.raises(ToolError, match="saved secret|saved chats"):
            _run_shell(store, f"cat {phrase}")
        with pytest.raises(ToolError, match="saved secret|saved chats"):
            _user_path(store, str(phrase))
        with pytest.raises(ToolError, match="saved secret|saved chats"):
            _user_path(store, str(secret_db))
    finally:
        turn_mod._slot.set(None)


def _endpoint(store: Store) -> str:
    record = store.add_endpoint(
        name="local",
        base_url="http://127.0.0.1:9/v1",
        api_key=None,
        endpoint_id=_ENDPOINT,
    )
    return record["id"]


def test_existing_endpoint_calls_still_authenticate(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    monkeypatch.setenv("EASYAGENT_MIGRATION_BACKUP", str(tmp_path / "backup"))
    root = tmp_path / "data"
    _plaintext(root, bridges=False)
    seen = {}

    async def complete(*, base_url, api_key, model, messages, timeout=120, **_extra):
        seen["api_key"] = api_key
        seen["messages"] = messages
        return "ack"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    client = TestClient(create_app(root))
    assert _KEY.encode("utf-8") not in _grep(root)
    listed = client.get("/api/endpoints")
    assert listed.status_code == 200
    assert _KEY not in listed.text
    assert listed.json()[0]["has_api_key"] is True
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": _ENDPOINT}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello"},
    )
    assert sent.status_code == 200, sent.text
    assert seen["api_key"] == _KEY
    assert _KEY not in json.dumps(seen["messages"])
    assert _KEY not in sent.text
