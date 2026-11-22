"""Each bot keeps its own MCP allow list. The shared list stays as a fallback."""

import json

import pytest

from easyagent import connectors
from easyagent.connectors import (
    add_connector,
    allow_path,
    finish_install,
    grant_server,
    list_grants,
    queue_install,
    require_allowed,
    server_allowed,
)
from easyagent.mcpclient import McpError, session_for
from easyagent.store import StoreError
from tests.test_mcp import _store

COMMAND = ["python3", "-c", "print(1)"]


@pytest.fixture(autouse=True)
def _memory_keyring(monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    connectors.reset_for_tests()


@pytest.fixture(autouse=True)
def _clear_installs():
    connectors.reset_for_tests()


def _two_bots(tmp_path):
    store, bot_a = _store(tmp_path)
    bot_b = store.add_bot(name="Bo", endpoint_id=bot_a["endpoint_id"], model=None)
    return store, bot_a, bot_b


def _record(command=None, url=""):
    return {"transport": "stdio", "command": command or COMMAND, "url": url}


def test_a_bot_grant_lands_in_the_bot_file_not_the_global_one(tmp_path):
    store, bot_a, _bot_b = _two_bots(tmp_path)
    grant_server(store, command=COMMAND, bot_id=bot_a["id"])
    bot_file = store.bot_mcp_allow_path(bot_a["id"])
    assert bot_file.is_file()
    assert json.loads(bot_file.read_text(encoding="utf-8")) == [{"command": COMMAND, "url": ""}]
    assert not allow_path(store).is_file()


def test_a_bot_grant_allows_only_that_bot(tmp_path):
    store, bot_a, bot_b = _two_bots(tmp_path)
    grant_server(store, command=COMMAND, bot_id=bot_a["id"])
    assert server_allowed(store, _record(), bot_a["id"]) is True
    assert server_allowed(store, _record(), bot_b["id"]) is False
    assert server_allowed(store, _record()) is False


def test_a_global_grant_still_allows_every_bot(tmp_path):
    store, bot_a, bot_b = _two_bots(tmp_path)
    grant_server(store, command=COMMAND)
    assert server_allowed(store, _record(), bot_a["id"]) is True
    assert server_allowed(store, _record(), bot_b["id"]) is True
    assert server_allowed(store, _record()) is True


def test_grant_without_a_bot_still_writes_the_global_file(tmp_path):
    store, bot_a, _bot_b = _two_bots(tmp_path)
    grant_server(store, command=COMMAND)
    path = allow_path(store)
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == [{"command": COMMAND, "url": ""}]
    assert not store.bot_mcp_allow_path(bot_a["id"]).is_file()


def test_bot_rows_come_first_and_the_union_dedupes(tmp_path):
    store, bot_a, _bot_b = _two_bots(tmp_path)
    other = ["python3", "-c", "print(2)"]
    grant_server(store, command=COMMAND, bot_id=bot_a["id"])
    grant_server(store, command=COMMAND)
    grant_server(store, command=other)
    rows = list_grants(store, bot_a["id"])
    assert rows == [{"command": COMMAND, "url": ""}, {"command": other, "url": ""}]


def test_add_connector_passes_for_its_bot_and_fails_for_another(tmp_path):
    store, bot_a, bot_b = _two_bots(tmp_path)
    grant_server(store, command=COMMAND, bot_id=bot_a["id"])
    row = add_connector(
        store,
        bot_a["id"],
        name="Echo",
        transport="stdio",
        command=COMMAND,
        url="",
        secrets=None,
    )
    assert row["command"] == COMMAND
    with pytest.raises(StoreError, match="allow list"):
        require_allowed(store, command=COMMAND, url="", bot_id=bot_b["id"])
    with pytest.raises(StoreError, match="allow list"):
        add_connector(
            store,
            bot_b["id"],
            name="Echo",
            transport="stdio",
            command=COMMAND,
            url="",
            secrets=None,
        )


def test_a_corrupt_bot_allow_file_degrades_to_the_global_list(tmp_path):
    store, bot_a, bot_b = _two_bots(tmp_path)
    grant_server(store, command=COMMAND)
    bot_file = store.bot_mcp_allow_path(bot_a["id"])
    bot_file.parent.mkdir(parents=True, exist_ok=True)
    bot_file.write_text("this is not json {{{", encoding="utf-8")
    assert list_grants(store, bot_a["id"]) == [{"command": COMMAND, "url": ""}]
    assert server_allowed(store, _record(), bot_a["id"]) is True
    assert server_allowed(store, _record(), bot_b["id"]) is True


def test_approving_an_install_card_grants_for_that_bot_only(tmp_path, monkeypatch):
    class _FakeProc:
        stderr = None

    monkeypatch.setattr(
        "easyagent.mcpclient.launch_stdio",
        lambda *args, **kwargs: (_FakeProc(), ""),
    )
    monkeypatch.setattr(
        connectors,
        "refresh",
        lambda *args, **kwargs: (_ for _ in ()).throw(StoreError("no tools here")),
    )
    store, bot_a, bot_b = _two_bots(tmp_path)
    command = ["npx", "-y", "@modelcontextprotocol/server-echo@1.2.3"]
    card = queue_install(
        store,
        bot_a["id"],
        name="Echo",
        transport="stdio",
        command=command,
        url="",
        secrets=None,
    )
    saved = finish_install(card["id"], "approve")
    assert saved is not None
    assert "refresh_error" in saved
    assert store.bot_mcp_allow_path(bot_a["id"]).is_file()
    assert not allow_path(store).is_file()
    record = {"transport": "stdio", "command": command, "url": ""}
    client = session_for(record, store=store, bot_id=bot_a["id"])
    assert client is not None
    with pytest.raises(McpError, match="allow list"):
        session_for(record, store=store, bot_id=bot_b["id"])
