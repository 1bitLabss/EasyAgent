"""Windows spellings of a secret path still card. Ordinary commands do not.

These run on Linux and on the Windows CI job. An 8.3 short name can be
created only on Windows.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from easyagent.safety import secret_class
from easyagent.scriptgate import command_reads_secrets
from easyagent.strictshell import secret_basename


def test_secret_names_fold_before_the_file_is_opened():
    assert secret_basename(r"..\.env") == ".env"
    assert secret_basename(r".env::$DATA") == ".env"
    assert secret_basename(r".env.") == ".env"
    assert secret_basename(r".ENV") == ".env"
    assert secret_basename(r"\\?\C:\work\.ENV") == ".env"
    assert secret_basename(r"C:\work\.env ") == ".env"
    assert secret_basename(" .env.example").startswith(" ")
    assert secret_class(Path(".ENV")) == "name"
    assert secret_class(Path(".env.")) == "name"
    assert secret_class(Path(".env::$DATA")) == "name"
    assert secret_class(Path(r"\\?\C:\work\.ENV")) == "name"
    assert secret_class(Path(" .env.example")) == "name"
    assert secret_class(Path("id_rsa")) == "name"
    assert secret_class(Path("notes.pem")) == "name"
    assert secret_class(Path("token.key")) == "name"


def test_windows_secret_commands_card_and_harmless_ones_do_not(tmp_path):
    folder = tmp_path / "tree"
    nested = folder / "sub"
    nested.mkdir(parents=True)
    (folder / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    (folder / "id_rsa").write_text("KEYBODYXYZ\n", encoding="utf-8")
    (folder / "notes.pem").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (folder / "token.key").write_text("not a real key\n", encoding="utf-8")
    (nested / "notes.txt").write_text("hello\n", encoding="utf-8")
    (nested / "file.txt").write_text("plain\n", encoding="utf-8")
    cards = (
        "type .env",
        r"cat ..\.env",
        "type .ENV",
        "type .env.",
        "type .env::$DATA",
        "cat .env",
        "cat id_rsa",
        "cat notes.pem",
        "cat token.key",
        r"type C:\work\.env",
        r"cat ..\.ENV",
    )
    for command in cards:
        assert command_reads_secrets(command, nested) is True, command
    windows_cwd = Path(r"C:\Users\runner\proj")
    for command in ("echo hi", "git status", "cat notes.txt", "type file.txt", "ls", "cat file.txt"):
        assert command_reads_secrets(command, nested) is False, command
    assert command_reads_secrets("echo hi", windows_cwd) is False
    assert command_reads_secrets("cat notes.txt", windows_cwd) is False
    assert command_reads_secrets("git status", windows_cwd) is False


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names are a Windows path")
def test_eight_three_short_name_of_env_is_secret(tmp_path):
    import ctypes

    env = tmp_path / ".env"
    env.write_text("SECRETENV=1\n", encoding="utf-8")
    buf = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetShortPathNameW(str(env), buf, len(buf))
    if not size or "~" not in buf.value:
        pytest.skip("this volume has no 8.3 short name")
    assert secret_class(Path(buf.value)) == "name"
    assert command_reads_secrets(f"type {buf.value}", tmp_path) is True
