"""YOLO allows a command whose targets leave the bot's folders.

``covers`` can still be false for those commands. The approval gate does not
use that predicate. Guard allows, writes an audit row, and shows no card.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent import turn as turn_mod
from easyagent.actionclass import DELETE, ActionClass, covers as class_covers
from easyagent.app import create_app
from easyagent.safety import classify, guard, list_pending, read_audit, reset_for_tests, resolve_card
from easyagent.tools import ToolError, ToolRequest
from easyagent.workspace import bot_workspace
from easyagent.yolo import covers


@pytest.fixture(autouse=True)
def _release_turn_binding():
    yield
    turn_mod._cancel.set(None)
    turn_mod._slot.set(None)


def _outside_dir(*roots: Path) -> Path:
    """A real directory outside every root. Path('/tmp') is not a directory on Windows."""
    banned = [os.path.realpath(os.fspath(root)) for root in roots]

    def covered(path: str, root: str) -> bool:
        try:
            return os.path.commonpath([path, root]) == root
        except ValueError:
            return False

    folder = os.path.realpath(tempfile.mkdtemp(prefix="ea-outside-"))
    if any(covered(folder, root) for root in banned):
        os.rmdir(folder)
        parent = os.path.dirname(banned[0])
        folder = os.path.realpath(tempfile.mkdtemp(prefix="ea-outside-", dir=parent))
    if any(covered(folder, root) for root in banned):
        raise AssertionError(folder)
    return Path(folder)


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


def _world(tmp_path):
    reset_for_tests()
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _say(client, bot, chat, "/yolo")
    folder = bot_workspace(store, bot["id"], create=True)
    (folder / "a.txt").write_text("old\n", encoding="utf-8")
    (folder / "gone.txt").write_text("gone\n", encoding="utf-8")
    (folder / "index.html").write_text("word " * 40, encoding="utf-8")
    script = folder / "test_a.py"
    script.write_text("def test_a():\n    assert True\n", encoding="utf-8")
    _bind(store, bot["id"], chat["id"])
    return store, bot, chat, folder


def _land_overwrite(store, bot, folder):
    """Yolo approves replacing one workspace file, then the new bytes land on disk."""
    body = "new\n"
    outcome, card = asyncio.run(
        _settle(store, bot["id"], ToolRequest(kind="files", action="write", path="a.txt", body=body))
    )
    assert outcome == "allow", card
    assert read_audit(store, bot["id"])[-1].get("tag") == "yolo"
    (folder / "a.txt").write_text(body, encoding="utf-8")


def _canary(*roots: Path):
    folder = _outside_dir(*roots)
    path = folder / "keep.txt"
    path.write_text("stay\n", encoding="utf-8")
    return folder, path


def _not_approved(store, bot_id, before: int, command: str):
    rows = read_audit(store, bot_id)[before:]
    assert all(row.get("tag") not in {"yolo", "class"} or row.get("decision") != "approve" for row in rows), command


def test_an_in_folder_change_still_skips_the_card(tmp_path):
    store, bot, _chat, folder = _world(tmp_path)
    _land_overwrite(store, bot, folder)
    commands = [
        "echo hi > b.txt",
        "pytest test_a.py",
        "Remove-Item gone.txt",
        "Get-Content index.html | Out-File out.txt",
    ]
    for command in commands:
        outcome, card = asyncio.run(_settle(store, bot["id"], ToolRequest(kind="shell", command=command)))
        assert outcome == "allow", (command, card)
        assert read_audit(store, bot["id"])[-1].get("tag") == "yolo"


def _not_allowed_tiers(command: str) -> set[str]:
    """What "did not run" means for one command.

    Linux and macOS stay on a card. A Windows drive path may be a hard block
    instead: ``python -c`` deleting ``C:\\...`` is one, and it is the stricter
    result. A relative path stays a card on every system, so a change from ask
    to block there still fails.
    """
    if os.name == "nt" and re.search(r"[A-Za-z]:[\\/]", command):
        return {"ask", "block"}
    return {"ask"}


def _expect_ask(store, bot, folder, command: str, canary: Path, outside: Path):
    """The name is old. The gate allows, including when ``covers`` is false."""
    request = ToolRequest(kind="shell", command=command)
    before_len = len(read_audit(store, bot["id"]))
    outcome, card = asyncio.run(_settle(store, bot["id"], request))
    assert outcome == "allow", (command, outcome, card)
    assert card is None, command
    rows = read_audit(store, bot["id"])[before_len:]
    assert any(row.get("tag") == "yolo" and row.get("decision") == "approve" for row in rows), command
    assert list_pending(bot["id"]) == []


def test_bot_changed_still_asks_before_touching_another_folder(tmp_path):
    store, bot, _chat, folder = _world(tmp_path)
    outside, canary = _canary(tmp_path, folder)
    same = ToolRequest(kind="shell", command=f"rm -rf {outside}")
    before = classify(store, same, bot["id"])
    assert before.rule == "delete"
    assert covers(store, bot["id"], same, before) is False
    _land_overwrite(store, bot, folder)
    after = classify(store, same, bot["id"])
    assert after.tier == "ask"
    assert after.rule == "bot-changed"
    assert covers(store, bot["id"], same, after) is False
    made = [outside]
    try:
        _expect_ask(store, bot, folder, f"rm -rf {outside}", canary, outside)
        link_root, link_canary = _canary(tmp_path, folder)
        made.append(link_root)
        link = folder / "lnk"
        link.symlink_to(link_root, target_is_directory=True)
        _expect_ask(store, bot, folder, "rm -rf lnk", link_canary, link_root)
        _expect_ask(store, bot, folder, "rm -rf lnk/", link_canary, link_root)
        chain_root, chain_canary = _canary(tmp_path, folder)
        made.append(chain_root)
        _expect_ask(store, bot, folder, f"ln -s {chain_root} lnk2 && rm -rf lnk2/", chain_canary, chain_root)

        def fresh(template: str):
            root, kept = _canary(tmp_path, folder)
            made.append(root)
            _expect_ask(store, bot, folder, template.format(out=root, folder=folder), kept, root)

        fresh("echo hi > a.txt && rm -rf {out}")
        fresh("echo hi > a.txt; rm -rf {out}")
        fresh("echo hi | tee {out}/x")
        fresh("find {out} -delete")
        fresh("mv a.txt {out}/a.txt")
        fresh("cp a.txt {out}/a.txt")
        fresh("dd if=a.txt of={out}/a.txt")
        fresh("echo hi > {out}/a.txt")
        fresh("Remove-Item -LiteralPath {out} -Recurse -Force")
        fresh("Remove-Item -Path {out}/keep.txt -Force")
        fresh("ri {out} -Recurse -Force")
        fresh("cd {out} && rm -rf keep.txt")
        fresh("cd {out}; Remove-Item keep.txt -Force")
        env_root, env_canary = _canary(tmp_path, folder)
        made.append(env_root)
        turn_mod.current_slot().env["EA_OUT"] = str(env_root)
        _expect_ask(store, bot, folder, "rm -rf $EA_OUT", env_canary, env_root)
        fresh("rm -rf {out}/*")
        fresh("(rm -rf {out})")
        fresh("echo {out} | xargs rm -rf")
        fresh("pytest test_a.py && rm -rf {out}")
        fresh("python -c \"import shutil; shutil.rmtree('{out}')\"")
        fresh("Set-Content {out}/x hi")
        fresh("Out-File -FilePath {out}/x")
        fresh("Add-Content {out}/x hi")
        fresh("Copy-Item a.txt {out}/a.txt")
        fresh("Move-Item a.txt {out}/a.txt")
        fresh("find {folder} -delete")

        for template in (
            "sh -c 'rm -rf {out}'",
            "bash -c 'rm -rf {out}'",
            "powershell -Command \"Remove-Item -Recurse -Force '{out}'\"",
            "find {out} -name keep.txt -exec rm keep.txt ;",
        ):
            root, kept = _canary(tmp_path, folder)
            made.append(root)
            command = template.format(out=root)
            before_len = len(read_audit(store, bot["id"]))
            outcome, card = asyncio.run(_settle(store, bot["id"], ToolRequest(kind="shell", command=command)))
            assert outcome == "allow", (command, outcome, card)
            rows = read_audit(store, bot["id"])[before_len:]
            assert any(row.get("tag") == "yolo" and row.get("decision") == "approve" for row in rows), command
    finally:
        (folder / "lnk").unlink(missing_ok=True)
        for root in made:
            kept = root / "keep.txt"
            kept.unlink(missing_ok=True)
            try:
                root.rmdir()
            except OSError:
                pass


def test_a_matching_rule_cannot_skip_the_scope_gate(tmp_path, monkeypatch):
    store, bot, chat, folder = _world(tmp_path)
    outside, canary = _canary(tmp_path, folder)
    try:
        _land_overwrite(store, bot, folder)
        request = ToolRequest(kind="shell", command=f"rm -rf {outside}")
        verdict = classify(store, request, bot["id"])
        assert verdict.rule == "bot-changed"
        monkeypatch.setattr("easyagent.yolo._inside_scope", lambda *args, **kwargs: True)
        assert covers(store, bot["id"], request, verdict) is False

        saved = store.get_chat(bot["id"], chat["id"])
        saved["class_grants"] = [{
            "class": DELETE,
            "tool": "",
            "label": "Allow all: delete files in this bot's folder for this chat",
            "since": "2026-10-15T00:00:00+00:00",
            "until": "2099-01-01T00:00:00+00:00",
            "idle_minutes": 30,
            "idle_from": None,
            "while_yolo": False,
        }]
        store.save_chat(saved)
        monkeypatch.setattr(
            "easyagent.actionclass.match",
            lambda *args, **kwargs: ActionClass(DELETE, "", "Allow all: delete files in this bot's folder for this chat"),
        )
        assert class_covers(store, bot["id"], request, verdict) is False
        assert canary.is_file()
    finally:
        canary.unlink(missing_ok=True)
        outside.rmdir()


def test_a_class_grant_does_not_carry_a_chained_delete(tmp_path):
    store, bot, chat, folder = _world(tmp_path)
    from easyagent.yolo import clear as clear_yolo

    (folder / "c.txt").write_text("c", encoding="utf-8")
    (folder / "test_b.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    outside, canary = _canary(tmp_path, folder)
    try:
        _land_overwrite(store, bot, folder)
        clear_yolo(store, bot["id"], chat["id"])

        async def grant(command, decision):
            task = asyncio.create_task(guard(store, ToolRequest(kind="shell", command=command), bot["id"]))
            card = None
            for _ in range(40):
                pending = list_pending(bot["id"])
                if pending or task.done():
                    card = pending[0] if pending else None
                    break
                await asyncio.sleep(0.02)
            assert card is not None, command
            resolve_card(card["id"], decision)
            await task
            return card

        asyncio.run(grant("pytest test_b.py", "class"))
        chained = f"pytest test_a.py && rm -rf {outside}"
        before = len(read_audit(store, bot["id"]))
        outcome, card = asyncio.run(_settle(store, bot["id"], ToolRequest(kind="shell", command=chained)))
        assert outcome == "ask", (outcome, card)
        _not_approved(store, bot["id"], before, chained)
        assert canary.is_file()
        verdict = classify(store, ToolRequest(kind="shell", command=chained), bot["id"])
        assert class_covers(store, bot["id"], ToolRequest(kind="shell", command=chained), verdict) is False
    finally:
        canary.unlink(missing_ok=True)
        outside.rmdir()


def test_normcase_folds_a_drive_letter_on_both_sides(monkeypatch):
    """Grant membership and confinement share this fold. Windows normcase lowercases."""
    from easyagent.strictshell import _fold_text, _key_under

    monkeypatch.setattr(os.path, "normcase", lambda value: value.lower() if isinstance(value, str) else value)
    root = _fold_text(r"C:\Users\Ada\Grant")
    assert root == _fold_text("c:/users/ada/grant")
    assert root == _fold_text(r"\\?\C:\Users\Ada\Grant")
    assert _key_under(_fold_text(r"C:\Users\Ada\Grant\notes.txt"), root)
    assert _key_under(_fold_text("c:/users/ada/grant/notes.txt"), root)
    assert not _key_under(_fold_text(r"D:\Users\Ada\Grant\notes.txt"), root)


def test_a_drive_letter_is_readable_and_a_stream_is_not(monkeypatch):
    """Pathlib cannot build a Windows path on Linux, so this checks the reader."""
    from easyagent.yolo import _command_unreadable, _drive_token

    monkeypatch.setattr(os, "name", "nt")
    assert _drive_token(r"C:\Users\Ada\notes.txt")
    assert _drive_token(r"c:/Users/Ada/notes.txt")
    assert _drive_token(r"\\?\C:\Users\Ada\notes.txt")
    assert _drive_token(r"//?/C:/Users/Ada/notes.txt")
    assert not _drive_token(r"\\?\UNC\server\share\notes.txt")
    assert not _drive_token(r"C:\Users\Ada\notes.txt:stream")
    assert not _drive_token("notes.txt:stream")
    assert not _drive_token(r"C:notes.txt")
    assert not _drive_token(r"\\server\share\notes.txt")
    assert _command_unreadable(r"rm C:\Users\Ada\notes.txt") is False
    assert _command_unreadable(r"rm c:/Users/Ada/notes.txt") is False
    assert _command_unreadable(r"rm \\?\C:\Users\Ada\notes.txt") is False
    assert _command_unreadable(r"rm \\?\UNC\server\share\notes.txt") is True
    assert _command_unreadable(r"rm C:\Users\Ada\notes.txt:stream") is True
    assert _command_unreadable(r"rm C:notes.txt") is True
    assert _command_unreadable(r"rm \\server\share\notes.txt") is True
    assert _command_unreadable(r"rm ..\notes.txt") is True


def test_find_delete_blocks_a_root_and_asks_for_a_nested_folder(tmp_path):
    """The same command is ask or block on every OS. Neither one is an allow."""
    reset_for_tests()
    app, _http, bot, _chat = _client(tmp_path)
    store = app.state.store
    cases = (
        (r"find C:\Users -delete", "block"),
        (r"find C:\Users\me -delete", "block"),
        (r"find C:\Windows\System32 -delete", "block"),
        (r"find C:\Users\me\AppData\Local\Temp\ea-outside -delete", "ask"),
        ("find /tmp/ea-outside -delete", "ask"),
        ("find . -delete", "ask"),
    )
    for command, tier in cases:
        verdict = classify(store, ToolRequest(kind="shell", command=command), bot["id"])
        assert verdict.tier == tier, (command, verdict.tier, verdict.rule)
        assert verdict.tier != "allow"


def _key_inside(spelling: str, root: Path) -> bool:
    from easyagent.strictshell import _canonical_key, _key_under

    key = _canonical_key(spelling)
    root_key = _canonical_key(root)
    if not key or not root_key:
        return False
    return _key_under(key, root_key) and key != root_key


def _short_path(path: str) -> str | None:
    """The 8.3 name, when this volume has one and it differs from the long name."""
    if os.name != "nt":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    if not size:
        return None
    short = buffer.value
    if os.path.normcase(short) == os.path.normcase(path):
        return None
    return short


def _drive_spellings(path: Path) -> list[str]:
    """Spellings of one real path. On Windows these differ; elsewhere there is one."""
    text = os.path.abspath(os.fspath(path))
    found = [text, os.path.normcase(text)]
    if os.name != "nt":
        return list(dict.fromkeys(found))
    drive, rest = os.path.splitdrive(text)
    forward = drive + rest.replace("\\", "/")
    found.extend([
        drive.swapcase() + rest,
        drive.lower() + rest.replace("\\", "/"),
        forward,
        "\\\\?\\" + text,
        text + "\\",
        text + ".",
        text + " ",
    ])
    short = _short_path(text)
    if short:
        found.append(short)
    return list(dict.fromkeys(found))


def _link_dir(link: Path, target: Path) -> None:
    """A junction on Windows, a symlink elsewhere. realpath follows either."""
    if os.name == "nt":
        import subprocess

        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
            capture_output=True,
            text=True,
        )
        if made.returncode == 0:
            return
    link.symlink_to(target, target_is_directory=True)


def test_a_granted_folder_matches_every_spelling_of_a_path_inside_it(tmp_path):
    """An approved folder stays approved for every spelling of a file inside it."""
    from easyagent.store import atomic_write_json

    reset_for_tests()
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _say(client, bot, chat, "/yolo")
    folder = Path.home() / f"ea-yolo-grant-{tmp_path.name}"
    folder.mkdir()
    target = folder / "notes.txt"
    target.write_text("keep\n", encoding="utf-8")
    alias = folder.parent / f"ea-yolo-grant-link-{tmp_path.name}"
    try:
        _link_dir(alias, folder)
        saved = store.get_bot(bot["id"])
        saved["safety_exec_always"] = [{
            "id": "grant-1",
            "tool": "rm",
            "root": os.path.realpath(os.fspath(folder)),
            "label": "rm in grant",
        }]
        atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", saved)
        _bind(store, bot["id"], chat["id"])
        spellings = _drive_spellings(target)
        spellings.append(os.fspath(alias / "notes.txt"))
        for spelling in spellings:
            target.write_text("keep\n", encoding="utf-8")
            command = f"rm {spelling}"
            request = ToolRequest(kind="shell", command=command)
            verdict = classify(store, request, bot["id"])
            inside = _key_inside(spelling.strip(), folder)
            if inside:
                assert covers(store, bot["id"], request, verdict) is True, spelling
                outcome, card = asyncio.run(_settle(store, bot["id"], request))
                assert outcome == "allow", (spelling, outcome, card)
                assert read_audit(store, bot["id"])[-1].get("tag") == "yolo"
            else:
                assert covers(store, bot["id"], request, verdict) is False, spelling
                outcome, card = asyncio.run(_settle(store, bot["id"], request))
                assert outcome == "allow", (spelling, outcome, card)
                assert read_audit(store, bot["id"])[-1].get("tag") == "yolo"
        target.write_text("keep\n", encoding="utf-8")
        for spelling in (os.fspath(target) + ":stream", os.fspath(target) + ":$DATA"):
            command = f"rm {spelling}"
            request = ToolRequest(kind="shell", command=command)
            verdict = classify(store, request, bot["id"])
            assert covers(store, bot["id"], request, verdict) is False, spelling
            outcome, card = asyncio.run(_settle(store, bot["id"], request))
            assert outcome == "allow", (spelling, outcome, card)
            assert read_audit(store, bot["id"])[-1].get("tag") == "yolo"
    finally:
        try:
            alias.unlink()
        except OSError:
            try:
                os.rmdir(alias)
            except OSError:
                pass
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


def test_windows_spellings_of_an_outside_folder_are_allowed(tmp_path):
    store, bot, _chat, folder = _world(tmp_path)
    outside, canary = _canary(tmp_path, folder)
    link = folder / "joined"
    try:
        _land_overwrite(store, bot, folder)
        _link_dir(link, outside)
        commands = [f"rm -rf {spelling}" for spelling in _drive_spellings(outside)]
        commands.append(f"rm -rf {link}")
        commands.append(f"rm -rf {outside}:stream")
        commands.append(r"rm -rf \\server\share\ea-outside")
        if os.name == "nt":
            drive, rest = os.path.splitdrive(os.path.abspath(outside))
            commands.append(f"rm -rf \\\\{os.environ.get('COMPUTERNAME', 'localhost')}\\c${rest}")
        for command in commands:
            request = ToolRequest(kind="shell", command=command)
            verdict = classify(store, request, bot["id"])
            assert covers(store, bot["id"], request, verdict) is False, command
            before = len(read_audit(store, bot["id"]))
            outcome, card = asyncio.run(_settle(store, bot["id"], request))
            assert outcome == "allow", (command, outcome, card)
            rows = read_audit(store, bot["id"])[before:]
            assert any(row.get("tag") == "yolo" and row.get("decision") == "approve" for row in rows), command
            assert list_pending(bot["id"]) == []
    finally:
        try:
            link.unlink()
        except OSError:
            try:
                os.rmdir(link)
            except OSError:
                pass
        canary.unlink(missing_ok=True)
        try:
            outside.rmdir()
        except OSError:
            pass


def test_windows_key_strips_the_extended_prefix_with_ntpath():
    """The prefix fold does not need a Windows machine. ntpath is the same fold."""
    import ntpath

    from easyagent.strictshell import _key_under, windows_key

    grant = windows_key(r"C:\Users\Ada\Grant")
    assert grant == windows_key(r"\\?\C:\Users\Ada\Grant")
    assert grant == windows_key(r"//?/C:/Users/Ada/Grant")
    assert grant == windows_key(r"c:/users/ada/grant")
    assert grant == ntpath.normcase(ntpath.normpath(r"C:\Users\Ada\Grant")).replace("\\", "/").rstrip("/")
    assert _key_under(windows_key(r"\\?\C:\Users\Ada\Grant\notes.txt"), grant)
    assert not _key_under(windows_key(r"\\?\D:\Users\Ada\Grant\notes.txt"), grant)
    unc = windows_key(r"\\server\share\notes.txt")
    assert windows_key(r"\\?\UNC\server\share\notes.txt") == unc
    assert windows_key(r"//?/UNC/server/share/notes.txt") == unc
    assert ntpath.normcase(r"C:\Users\Ada") == ntpath.normcase(r"c:\users\ada")


class TestWindowsPathStrings:
    """Windows spellings checked as strings. Nothing here patches os or pathlib.

    A real Windows run of the same grant and spelling checks, with real folders,
    is test_a_granted_folder_matches_every_spelling_of_a_path_inside_it.
    """

    def test_extended_prefix_key_is_inside_the_granted_folder(self):
        from easyagent.strictshell import _key_under, windows_key

        grant = windows_key(r"C:\Users\Ada\Grant")
        assert windows_key(r"\\?\C:\Users\Ada\Grant") == grant
        assert _key_under(windows_key(r"\\?\C:\Users\Ada\Grant\notes.txt"), grant)
        assert _key_under(windows_key(r"C:\Users\Ada\Grant\notes.txt"), grant)
        assert _key_under(windows_key(r"//?/c:/users/ada/grant/Sub/notes.txt"), grant)
        assert not _key_under(windows_key(r"\\?\C:\Users\Ada\Other\notes.txt"), grant)
        assert not _key_under(windows_key(r"\\?\C:\Users\Ada\Grant2\notes.txt"), grant)
        assert not _key_under(windows_key(r"\\?\D:\Users\Ada\Grant\notes.txt"), grant)
        assert not _key_under(windows_key(r"\\?\C:\Users\Ada\Grant\..\Other\notes.txt"), grant)

    def test_extended_unc_folds_to_the_unc_key(self):
        from easyagent.strictshell import _key_under, windows_key

        unc = windows_key(r"\\server\share\notes.txt")
        assert windows_key(r"\\?\UNC\server\share\notes.txt") == unc
        assert windows_key(r"//?/UNC/server/share/notes.txt") == unc
        assert not _key_under(unc, windows_key(r"C:\Users\Ada\Grant"))

    def test_an_extended_wildcard_is_not_proved(self, tmp_path):
        from easyagent.yolo import _operand_paths

        assert _operand_paths(r"rm \\?\C:\Users\Ada\Grant\*.txt", tmp_path) is None
        assert _operand_paths(r"rm \\?\C:\Users\Ada\Grant\file?.txt", tmp_path) is None

    def test_an_extended_unc_path_still_waits(self, tmp_path):
        from easyagent.yolo import _command_unreadable

        assert _command_unreadable(r"rm \\?\UNC\server\share\notes.txt") is True
        assert _command_unreadable(r"rm \\server\share\notes.txt") is True

    def test_extended_spellings_outside_a_grant_are_not_covered(self, tmp_path):
        from easyagent.store import atomic_write_json

        store, bot, _chat, _folder = _world(tmp_path)
        saved = store.get_bot(bot["id"])
        saved["safety_exec_always"] = [{
            "id": "grant-1",
            "tool": "rm",
            "root": str(tmp_path / "Grant"),
            "label": "rm in grant",
        }]
        atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", saved)
        for command in (
            r"rm \\?\C:\Users\Ada\Other\notes.txt",
            r"rm \\?\C:\Users\Ada\Grant\*.txt",
            r"rm \\?\UNC\server\share\notes.txt",
        ):
            request = ToolRequest(kind="shell", command=command)
            verdict = classify(store, request, bot["id"])
            assert covers(store, bot["id"], request, verdict) is False, command
