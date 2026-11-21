"""Enabled connectors export as a standard mcp.json (agent-plugins.org v1 shape)."""

import json

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.connectors import add_connector, export_mcp_json, grant_server, update_connector
from tests.test_mcp import _store

SECRET_VALUE = "hunter2-super-secret"


@pytest.fixture(autouse=True)
def _memory_keyring(monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")


def _stdio(store, bot_id, name="Notes", command=None, secrets=None):
    command = command or ["python", "-m", "easyagent.mcpstd", "filesystem", "--root", "C:/ws"]
    grant_server(store, command=command)
    return add_connector(store, bot_id, name=name, transport="stdio", command=command, url="", secrets=secrets)


def test_stdio_row_splits_command_into_command_and_args(tmp_path):
    store, bot = _store(tmp_path)
    _stdio(store, bot["id"])
    doc = export_mcp_json(store, bot["id"])
    assert doc["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    entry = doc["mcpServers"]["Notes"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "python"
    assert entry["args"] == ["-m", "easyagent.mcpstd", "filesystem", "--root", "C:/ws"]


def test_secret_names_become_placeholders_and_the_value_never_appears(tmp_path):
    store, bot = _store(tmp_path)
    _stdio(store, bot["id"], name="Search", secrets={"API_KEY": SECRET_VALUE})
    doc = export_mcp_json(store, bot["id"])
    assert doc["mcpServers"]["Search"]["env"] == {"API_KEY": "${API_KEY}"}
    assert SECRET_VALUE not in json.dumps(doc)


def test_disabled_rows_are_skipped(tmp_path):
    store, bot = _store(tmp_path)
    kept = _stdio(store, bot["id"], name="Kept")
    off = _stdio(store, bot["id"], name="Off")
    update_connector(store, bot["id"], off["id"], enabled=False, tools=None)
    doc = export_mcp_json(store, bot["id"])
    assert list(doc["mcpServers"]) == ["Kept"]
    assert kept["enabled"] is True


def test_http_row_exports_as_streamable_http_with_url(tmp_path):
    store, bot = _store(tmp_path)
    address = "https://example.com/mcp"
    grant_server(store, url=address)
    add_connector(store, bot["id"], name="Remote", transport="http", command=None, url=address, secrets=None)
    entry = export_mcp_json(store, bot["id"])["mcpServers"]["Remote"]
    assert entry == {"type": "streamable-http", "url": address}


def test_duplicate_names_get_suffixed_so_every_connector_survives(tmp_path):
    store, bot = _store(tmp_path)
    _stdio(store, bot["id"], name="Notes", command=["python", "-m", "one"])
    _stdio(store, bot["id"], name="Notes", command=["python", "-m", "two"])
    _stdio(store, bot["id"], name="Notes", command=["python", "-m", "three"])
    servers = export_mcp_json(store, bot["id"])["mcpServers"]
    assert list(servers) == ["Notes", "Notes-2", "Notes-3"]
    assert servers["Notes-2"]["args"] == ["-m", "two"]


def test_rows_with_neither_command_nor_url_are_skipped(tmp_path):
    store, bot = _store(tmp_path)
    _stdio(store, bot["id"], name="Good")
    rows_path = None
    from easyagent.connectors import connector_path, list_records

    rows = list_records(store, bot["id"])
    rows.append({"id": "broken", "name": "Broken", "transport": "stdio", "command": [], "url": "", "enabled": True})
    rows_path = connector_path(store, bot["id"])
    rows_path.write_text(json.dumps(rows), encoding="utf-8")
    servers = export_mcp_json(store, bot["id"])["mcpServers"]
    assert "Broken" not in servers
    assert "Good" in servers
    assert rows_path.is_file()


def test_export_endpoint_returns_the_same_shape(tmp_path):
    monkey_client = TestClient(create_app(tmp_path))
    endpoint = monkey_client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = monkey_client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    empty = monkey_client.get(f"/api/bots/{bot['id']}/connectors/export")
    assert empty.status_code == 200
    assert empty.json() == {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {},
    }
    reviewed = monkey_client.post(
        f"/api/bots/{bot['id']}/connectors",
        json={
            "name": "Pinned",
            "transport": "stdio",
            "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem@1.2.3"],
            "package": "@modelcontextprotocol/server-filesystem",
            "version": "1.2.3",
            "secrets": {"TOKEN": SECRET_VALUE},
        },
    ).json()
    from easyagent.safety import resolve_card

    resolve_card(reviewed["card"]["id"], "approve")
    doc = monkey_client.get(f"/api/bots/{bot['id']}/connectors/export").json()
    entry = doc["mcpServers"]["Pinned"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "@modelcontextprotocol/server-filesystem@1.2.3"]
    assert entry["env"] == {"TOKEN": "${TOKEN}"}
    assert SECRET_VALUE not in monkey_client.get(f"/api/bots/{bot['id']}/connectors/export").text
