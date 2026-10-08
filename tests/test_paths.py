"""Default file location, shell advice, and first-run examples."""

import sys
from pathlib import Path

from easyagent.paths import default_deliverable_dir, deliverable_file
from easyagent.prompt import build_system
from easyagent.store import EXAMPLE_MEMORY, Store


def _prompt(**kwargs) -> str:
    return build_system(
        bot_name="Ada",
        direction="Stay.",
        summary="",
        skills_text="",
        **kwargs,
    )


def test_linux_uses_xdg_data_home(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/ea-xdg-home")
    assert default_deliverable_dir() == Path("/tmp/ea-xdg-home/EasyAgent")
    assert deliverable_file("landing.html") == "/tmp/ea-xdg-home/EasyAgent/landing.html"


def test_a_hidden_data_directory_is_a_folder(monkeypatch):
    """~/.local is a directory. A filename used as a parent is still blocked."""
    from easyagent.tools import _file_parent_in

    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert _file_parent_in("/home/ada/.local/share/EasyAgent/landing.html") == ""
    assert _file_parent_in("/Users/ada/Library/Application Support/EasyAgent/landing.html") == ""
    assert _file_parent_in(r"C:\work\summary.txt\note-10.txt") == r"C:\work\summary.txt"


def test_linux_uses_local_share_when_xdg_is_unset(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/home/ada")))
    assert default_deliverable_dir() == Path("/home/ada/.local/share/EasyAgent")


def test_windows_uses_local_app_data(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\ada\AppData\Local")
    assert deliverable_file("landing.html") == r"C:\Users\ada\AppData\Local\EasyAgent\landing.html"


def test_windows_falls_back_under_the_user_profile(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path(r"C:\Users\ada")))
    assert deliverable_file("landing.html") == r"C:\Users\ada\AppData\Local\EasyAgent\landing.html"


def test_macos_uses_application_support(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/Users/ada")))
    assert default_deliverable_dir() == Path("/Users/ada/Library/Application Support/EasyAgent")
    assert " " in deliverable_file("landing.html")


def test_prompt_follows_the_running_system(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/ea-xdg-prompt")
    linux = _prompt()
    assert "/tmp/ea-xdg-prompt/EasyAgent" in linux
    assert "On Linux, ls and pwd are normal commands." in linux
    assert "ls and pwd are not commands" not in linux
    assert "C:\\work" not in linux

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/Users/ada")))
    mac = _prompt()
    assert "/Users/ada/Library/Application Support/EasyAgent" in mac
    assert "On macOS, ls and pwd are normal commands." in mac
    assert "ls and pwd are not commands" not in mac

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\ada\AppData\Local")
    windows = _prompt()
    assert r"C:\Users\ada\AppData\Local\EasyAgent" in windows
    assert "On Windows, ls and pwd are not commands." in windows
    assert "Invoke-RestMethod" in windows
    assert "ConvertTo-Json" in windows
    assert "ConvertFrom-Json" in windows
    assert ".ps1" in windows
    assert "python -c" in windows
    assert "C:\\work" not in windows


def test_a_new_bot_gets_one_example_memory_line_and_one_example_skill(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    lines = store.list_memory(bot["id"])
    assert [item["text"] for item in lines] == [EXAMPLE_MEMORY]
    assert lines[0]["topic"] == "examples"
    assert "example" in lines[0]["text"].lower()
    skills = store.list_skills()
    assert [item["name"] for item in skills] == ["example-note"]
    assert "example" in skills[0]["description"].lower()
    other = store.add_bot(name="Bea", endpoint_id=endpoint["id"], model=None)
    assert [item["text"] for item in store.list_memory(other["id"])] == [EXAMPLE_MEMORY]
    assert [item["name"] for item in store.list_skills()] == ["example-note"]
