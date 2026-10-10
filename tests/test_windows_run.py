"""Live run state, a locked chat file, thinking gaps, and Windows shell advice."""

import json
import os
import subprocess
import sys
import time

import pytest
from fastapi import HTTPException

from easyagent.app import (
    LIVE_SAVE_SECONDS,
    _finish_reply,
    _save_live_thinking,
    _live_saved_at,
)
from easyagent.llm import native_tools
from easyagent.prompt import build_system
from easyagent.store import Store, StoreError, _extended_win, _io_path, atomic_write_text, read_json
from easyagent.tools import ToolError, ToolRequest, _run_shell, status_label, thought_gap


def _store(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    return store, bot, chat


def test_replace_retries_a_windows_lock_then_writes(tmp_path, monkeypatch):
    path = tmp_path / "chat.json"
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            err = PermissionError(13, "Access is denied")
            err.winerror = 5
            raise err
        return real(src, dst)

    monkeypatch.setattr("easyagent.store.os.replace", flaky)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    atomic_write_text(path, "hello")
    assert path.read_text(encoding="utf-8") == "hello"
    assert calls["n"] == 3


def test_replace_retries_winerror_32_and_gives_up(tmp_path, monkeypatch):
    path = tmp_path / "chat.json"
    calls = {"n": 0}

    def locked(src, dst):
        calls["n"] += 1
        err = OSError(13, "The process cannot access the file")
        err.winerror = 32
        raise err

    monkeypatch.setattr("easyagent.store.os.replace", locked)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(OSError):
        atomic_write_text(path, "nope")
    assert calls["n"] == 10
    assert not path.exists()


def test_read_json_retries_a_windows_permission_error(tmp_path, monkeypatch):
    path = tmp_path / "chat.json"
    path.write_text('{"ok": true}\n', encoding="utf-8")
    calls = {"n": 0}
    original = type(path).read_text

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        wanted = os.path.normcase(os.fspath(_io_path(path)))
        if os.path.normcase(os.fspath(self)) == wanted and calls["n"] < 3:
            err = PermissionError(13, "Access is denied")
            err.winerror = 5
            raise err
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", flaky)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    assert read_json(path) == {"ok": True}
    assert calls["n"] == 3


def test_read_json_gives_up_after_a_lasting_windows_lock(tmp_path, monkeypatch):
    path = tmp_path / "chat.json"
    path.write_text("{}\n", encoding="utf-8")
    calls = {"n": 0}

    def locked(*args, **kwargs):
        calls["n"] += 1
        err = OSError(13, "The process cannot access the file")
        err.winerror = 32
        raise err

    monkeypatch.setattr(type(path), "read_text", locked)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(OSError):
        read_json(path)
    assert calls["n"] == 10


def test_a_different_oserror_is_not_retried(tmp_path, monkeypatch):
    path = tmp_path / "chat.json"
    calls = {"n": 0}

    def missing(src, dst):
        calls["n"] += 1
        raise FileNotFoundError(2, "missing")

    monkeypatch.setattr("easyagent.store.os.replace", missing)
    with pytest.raises(FileNotFoundError):
        atomic_write_text(path, "nope")
    assert calls["n"] == 1


def test_live_thinking_is_throttled_and_a_lock_does_not_stop_the_reply(tmp_path, monkeypatch):
    store, bot, chat = _store(tmp_path)
    _live_saved_at.clear()
    monkeypatch.setattr("easyagent.app.LIVE_SAVE_SECONDS", 30)
    _save_live_thinking(store, bot["id"], chat["id"], "first thought")
    _save_live_thinking(store, bot["id"], chat["id"], "second thought")
    saved = store.get_chat(bot["id"], chat["id"])
    assert saved["messages"][-1]["thinking"] == "first thought"
    assert LIVE_SAVE_SECONDS == 1.5

    def deny(self, document):
        raise PermissionError(13, "Access is denied")

    _live_saved_at.clear()
    monkeypatch.setattr(Store, "save_chat", deny)
    _save_live_thinking(store, bot["id"], chat["id"], "third thought")


def test_the_final_save_says_when_the_chat_file_stays_locked(tmp_path, monkeypatch):
    store, bot, chat = _store(tmp_path)
    chat["messages"].append({"id": "u1", "role": "user", "content": "hello", "created_at": "2026-10-07T00:00:00+00:00"})
    store.save_chat(chat)

    def deny(self, document):
        err = PermissionError(13, "Access is denied")
        err.winerror = 5
        raise err

    monkeypatch.setattr(Store, "save_chat", deny)
    with pytest.raises(HTTPException) as caught:
        _finish_reply(store, bot["id"], chat["id"], "the answer", [])
    assert str(caught.value.detail).startswith("Stopped:")
    assert "locked" in str(caught.value.detail)


def test_thought_chunks_and_steps_do_not_run_together():
    from easyagent.tools import join_segments

    assert thought_gap("check the X account first", "Let me first check") == ""
    assert thought_gap("see what is going on.", "Let me start") == ""
    assert thought_gap("Hel", "lo") == ""
    assert thought_gap("FA", "KE123") == ""
    assert thought_gap("step 0. ", "step 1. ") == ""
    assert thought_gap("see what is going on.", "Let me start", new_step=True) == "\n\n"
    parts = ["facts:", "\n", "1", ". key sk-test-FA", "KE123", "\n", "2", ". a PROM", "ISE", "S.md"]  # fake-key-fixture
    assert join_segments(parts) == "".join(parts)
    stepped = "see what is going on." + thought_gap("see what is going on.", "Let me start", new_step=True) + "Let me start"
    assert stepped == "see what is going on.\n\nLet me start"


def test_windows_shell_is_powershell_and_empty_output_names_the_exit_code(tmp_path, monkeypatch):
    store, _bot, _chat = _store(tmp_path)
    seen = {}

    class Proc:
        def __init__(self, code, out):
            self.returncode = code
            self._out = out

        def communicate(self, timeout=None):
            return self._out, ""

    def fake_popen(args, **kwargs):
        seen["args"] = args
        seen["shell"] = kwargs.get("shell")
        return Proc(0, "")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert _run_shell(store, "Invoke-RestMethod http://127.0.0.1:8188/prompt") == (
        "command produced no output, exit code 0"
    )
    assert seen["shell"] is False
    assert seen["args"][0] == "powershell.exe"
    assert seen["args"][-1] == "Invoke-RestMethod http://127.0.0.1:8188/prompt"

    def fail_popen(args, **kwargs):
        return Proc(3, "")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    with pytest.raises(ToolError) as caught:
        _run_shell(store, "Invoke-RestMethod http://127.0.0.1:8188/prompt")
    assert str(caught.value) == "command produced no output, exit code 3"

    label = status_label(ToolRequest(kind="shell", command="Invoke-RestMethod http://127.0.0.1:8188/prompt"))
    assert label.startswith("Running PowerShell: Invoke-RestMethod")
    tools = native_tools()
    terminal = next(item for item in tools if item["function"]["name"] == "terminal")
    assert "Invoke-RestMethod" in terminal["function"]["description"]
    assert "python -c" in terminal["function"]["description"]
    prompt = build_system(bot_name="Ada", direction="", summary="", skills_text="")
    assert "Invoke-RestMethod" in prompt
    assert "ConvertFrom-Json" in prompt
    assert ".ps1" in prompt


def test_linux_shell_still_says_when_a_command_prints_nothing(tmp_path, monkeypatch):
    store, _bot, _chat = _store(tmp_path)

    class Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return "   ", ""

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Proc())
    assert _run_shell(store, "true") == "command produced no output, exit code 0"
    assert "Running PowerShell" not in status_label(ToolRequest(kind="shell", command="echo hi"))


def test_windows_file_io_uses_the_extended_length_prefix():
    assert _extended_win(r"C:\data\bots\note") == "\\\\?\\C:\\data\\bots\\note"
    assert _extended_win("\\\\?\\C:\\data\\note") == "\\\\?\\C:\\data\\note"
    assert _extended_win("\\\\server\\share\\a") == "\\\\?\\UNC\\server\\share\\a"


def test_a_chat_attachment_saves_under_a_deep_data_dir(tmp_path):
    folder = tmp_path
    while len(str(folder)) < 180:
        folder = folder / ("nest" * 8)
    folder.mkdir(parents=True)
    assert len(str(folder)) > 130
    store, bot, chat = _store(folder)
    meta = store.save_chat_file(
        bot["id"],
        chat["id"],
        name="note.txt",
        media_type="text/plain",
        data=b"deep-note",
    )
    found, blob = store.read_chat_file(bot["id"], chat["id"], meta["id"])
    leaf = folder / "bots" / bot["id"] / "chats" / chat["id"] / "files" / meta["id"]
    assert len(str(leaf)) > 260
    assert blob == b"deep-note"
    assert found["name"] == "note.txt"
    assert _io_path(leaf).is_file()
    assert store.get_chat(bot["id"], chat["id"])["id"] == chat["id"]


def test_a_failed_attachment_write_says_so(tmp_path, monkeypatch):
    from pathlib import Path

    store, bot, chat = _store(tmp_path)

    def denied(self, _data):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_bytes", denied)
    with pytest.raises(StoreError, match="Could not save that file") as denied_err:
        store.save_chat_file(bot["id"], chat["id"], name="a.txt", media_type="text/plain", data=b"a")
    assert "too long" not in str(denied_err.value).lower()

    def too_long(self, _data):
        err = OSError(206, "The filename or extension is too long")
        err.winerror = 206
        raise err

    monkeypatch.setattr(Path, "write_bytes", too_long)
    with pytest.raises(StoreError, match="too long for Windows"):
        store.save_chat_file(bot["id"], chat["id"], name="b.txt", media_type="text/plain", data=b"b")
