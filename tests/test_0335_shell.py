"""Shell easing and the night pass read one transcript."""

import json
import os
import subprocess
import time
from pathlib import Path

from easyagent.scriptgate import command_reads_secrets
from easyagent.tools import ToolRequest


def _git(folder: Path, *args: str) -> None:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Ada"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "ada@example.com"
    subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True, env=env)


def _classify(tmp_path: Path, folder: Path, command: str):
    from easyagent import turn as turn_mod
    from easyagent.safety import classify, set_mode
    from easyagent.store import Store

    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    set_mode(store, bot["id"], "strict")
    slot = turn_mod.bind(store, "shell", bot["id"])
    slot.cwd = str(folder)
    slot.store = store
    try:
        return classify(store, ToolRequest(kind="shell", command=command), bot["id"])
    finally:
        turn_mod._slot.set(None)


def test_version_queries_tree_sleep_and_file_list_run(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    (folder / "notes.txt").write_text("hello\n", encoding="utf-8")
    (folder / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    allows = (
        "python3 --version",
        "python3 -V",
        "node --version",
        "node -v",
        "npm --version",
        "pip --version",
        "pip -V",
        "cargo --version",
        "curl --version",
        "curl -V",
        "tsc --version",
        "sleep 1",
        "command -v python3",
        "tree",
        "rg --files",
        "sed -n '$p' notes.txt",
        "sed 's/a/b/;s/c/d/' notes.txt",
    )
    for command in allows:
        assert command_reads_secrets(command, folder) is False, command
        verdict = _classify(tmp_path, folder, command)
        assert verdict.tier == "allow", (command, verdict.tier, verdict.rule)


def test_version_flags_do_not_hide_a_read(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    (folder / "notes.txt").write_text("hello\n", encoding="utf-8")
    (folder / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    cards = (
        "python3 -c 'print(open(\".env\").read())'",
        "python3 --version -c 'print(1)'",
        "python3 -v",
        "node --version -e 'console.log(1)'",
        "npm install",
        "npm --version --prefix .",
        "pip install -r requirements.txt",
        "curl http://127.0.0.1/secret",
        "curl -v http://127.0.0.1/",
        "command cat .env",
        "rg TODO",
        "rg --files --pre cat",
        "tree /",
        "sed 'r.env' notes.txt",
        "sed 's/a/b/e' notes.txt",
        "sed 'R .env' notes.txt",
    )
    for command in cards:
        verdict = _classify(tmp_path, folder, command)
        assert verdict.tier == "ask", (command, verdict.tier, verdict.rule)


def test_git_diff_cards_only_the_paths_it_would_print(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "notes.txt").write_text("hello\n", encoding="utf-8")
    (folder / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    _git(folder, "init")
    _git(folder, "add", "notes.txt")
    _git(folder, "commit", "-m", "notes")
    assert command_reads_secrets("git diff", folder) is False
    assert command_reads_secrets("git log -p -1", folder) is False
    assert command_reads_secrets("git diff -- notes.txt", folder) is False
    (folder / "notes.txt").write_text("hello\nmore\n", encoding="utf-8")
    assert command_reads_secrets("git diff", folder) is False
    assert command_reads_secrets("git log -p -1", folder) is False
    _git(folder, "add", "-A")
    _git(folder, "commit", "-m", "secret")
    assert command_reads_secrets("git log -p -1", folder)
    assert command_reads_secrets("git diff", folder) is False
    (folder / ".env").write_text("SECRETENV=2\n", encoding="utf-8")
    assert command_reads_secrets("git diff", folder)
    assert command_reads_secrets("git diff | head", folder)
    (folder / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    assert command_reads_secrets("git diff", bare)
    assert command_reads_secrets("git diff missing-branch", folder)


def test_night_opens_only_the_newest_transcript(tmp_path):
    from easyagent.night import _newest_chat
    from easyagent.store import Store

    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    winner_id = ""
    body = "x" * 4000
    for index in range(1500):
        chat = store.create_chat(bot["id"])
        path = store._chat_path(bot["id"], chat["id"])
        stamp = f"2020-01-{(index // 1440) + 1:02d}T{(index // 60) % 24:02d}:{index % 60:02d}:00+00:00"
        secret = "SECRETENV=1" if index == 0 else "plain"
        if index == 1499:
            stamp = "2026-10-17T00:00:00+00:00"
            secret = "please summarize the notes"
            winner_id = chat["id"]
        record = {
            "id": chat["id"],
            "bot_id": bot["id"],
            "title": "n",
            "created_at": "2020-01-01T00:00:00+00:00",
            "updated_at": stamp,
            "summary": "",
            "messages": [{"id": "m", "role": "user", "content": secret + body, "created_at": stamp}],
        }
        path.write_text(json.dumps(record), encoding="utf-8")
    calls: list[str] = []
    real = store.get_chat

    def wrapped(bot_id, chat_id):
        calls.append(chat_id)
        return real(bot_id, chat_id)

    store.get_chat = wrapped
    started = time.perf_counter()
    found = _newest_chat(store)
    elapsed = time.perf_counter() - started
    assert found is not None
    bot_found, chat = found
    assert bot_found["id"] == bot["id"]
    assert chat["id"] == winner_id
    assert calls == [winner_id]
    assert "SECRETENV=1" not in json.dumps(chat)
    assert elapsed < 8, elapsed
