"""0.3.34: secret reads are classified, and the P0 tool bugs have a live path."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.fuzzy import fuzzy_find_and_replace
from easyagent.safety import secret_class, secret_file
from easyagent.scriptgate import command_reads_secrets, script_body_proved
from easyagent.tools import ToolRequest, _request_from_call, _tool_signature


class Handler(BaseHTTPRequestHandler):
    scripts: list = []
    payloads: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        path = self.path.split("?", 1)[0]
        if not path.endswith("/chat/completions"):
            self._send(200, b'{"data":[]}', "application/json")
            return
        Handler.payloads.append(json.loads(raw.decode("utf-8")))
        lines = Handler.scripts.pop(0) if Handler.scripts else [
            'data: {"choices":[{"delta":{"content":"Done."}}]}',
            "data: [DONE]",
        ]
        body = ("\n".join(lines) + "\n").encode()
        self._send(200, body, "text/event-stream")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def setup_function():
    Handler.scripts = []
    Handler.payloads = []


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _chat(tmp_path, port):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": f"http://127.0.0.1:{port}/v1", "model": "qwen"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat


def _tool_sse(name: str, arguments: dict, call_id: str) -> list[str]:
    blob = json.dumps(arguments)
    encoded = json.dumps(blob)
    return [
        f'data: {{"choices":[{{"delta":{{"tool_calls":[{{"index":0,"id":"{call_id}","type":"function","function":{{"name":"{name}","arguments":""}}}}]}}}}]}}',
        f'data: {{"choices":[{{"delta":{{"tool_calls":[{{"index":0,"function":{{"arguments":{encoded}}}}}]}}}}]}}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]


def _tool_text(payloads: list) -> str:
    return "\n".join(_tool_rows(payloads))


def _tool_rows(payloads: list) -> list[str]:
    """Tool results from the request that carried the most of them."""
    best: list[str] = []
    for payload in payloads:
        rows = [
            str(message.get("content") or "")
            for message in payload.get("messages") or []
            if message.get("role") == "tool"
        ]
        if len(rows) >= len(best):
            best = rows
    return best


def _plant(folder: Path) -> None:
    (folder / "id_rsa").write_text("comment\nKEYBODYXYZ\n", encoding="utf-8")
    (folder / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    (folder / ".npmrc").write_text("//registry.npmjs.org/:_authToken=npm_secret\n", encoding="utf-8")
    (folder / "token.txt").write_text("token=plain\n", encoding="utf-8")
    (folder / "key_material.txt").write_text("-----BEGIN PRIVATE KEY-----\nABC\n", encoding="utf-8")
    (folder / "creds.json").write_text('{"password":"hunter2PASS"}\n', encoding="utf-8")
    (folder / "notes.txt").write_text("visible-note hunter2\n", encoding="utf-8")
    (folder / ".envrc").write_text("export PATH\n", encoding="utf-8")
    (folder / "bland.txt").write_text("-----BEGIN PRIVATE KEY-----\nABC\n", encoding="utf-8")
    (folder / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (folder / "README.md").write_text("hello\n", encoding="utf-8")
    src = folder / "src"
    src.mkdir()
    (src / "app.py").write_text("print('ok')\n", encoding="utf-8")
    sub = folder / "sub"
    sub.mkdir()
    # Windows can refuse a symlink without developer mode. A hard link is a
    # separate try: NTFS allows it, and `cat hard` has to card when the name exists.
    try:
        (folder / "link").symlink_to(folder / ".env")
    except OSError:
        pass
    try:
        os.link(folder / ".env", folder / "hard")
    except OSError:
        pass


READERS = [
    "more .env",
    "tac .env",
    "nl .env",
    "cut -c1- .env",
    "sort .env",
    "uniq .env",
    "paste .env",
    "more id_rsa",
    "tac id_rsa",
    "nl id_rsa",
    "cut -c1- id_rsa",
    "sort id_rsa",
    "uniq id_rsa",
    "paste id_rsa",
    "python3 -c \"print(open('id_rsa').read())\"",
    "find . -type f | xargs grep -n hunter2",
    "find . -type f | xargs cat",
    "find . -type f -print0 | xargs -0 cat",
    "cat .npmrc",
    "cat token.txt",
    "cat key_material.txt",
    "cat .env",
    "cat id_rsa",
]

OBFUSCATED = [
    "c\\at .env",
    '"ca""t" .env',
    "$(echo cat) .env",
    "cat < .env",
    "cat ./sub/../.env",
    "cat link",
    "cat hard",
    "cat .e*",
    "cp .env x.txt && cat x.txt",
    "tar cf - .env | tar xO",
    "base64 .env",
    "python3 -c \"from pathlib import Path; print(Path('.env').read_text())\"",
    "python3 -c \"exec('print(open(\\\"id_rsa\\\").read())')\"",
    "node -e \"console.log(require('fs').readFileSync('.env','utf8'))\"",
    "perl -ne 'print' .env",
    "awk 1 .env",
    "sed -n p .env",
    "find . -name '*.py' | xargs wc -l",
    "find . -name '*.env' -exec cat {} +",
    "ls | xargs -I{} cat {}",
    "for f in *; do cat $f; done",
    "xxd .env",
    "od .env",
    "jq . creds.json",
    "strings id_rsa",
    "head .env",
    "${IFS}",
    "xargs -a list cat",
    "cat${IFS}.env",
]

CONTROLS = [
    "cat notes.txt",
    "cat src/app.py",
    "grep -n foo src/app.py",
    "grep -rn TODO --include='*.py' src",
    "find . -name '*.py'",
    "python3 -c \"print(open('notes.txt').read())\"",
    "sort notes.txt",
    "cut -d, -f1 data.csv",
    "head -n 5 README.md",
]


def test_secret_class_names_content_and_envrc(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    _plant(folder)
    (folder / ".envrc").write_text("export PATH\n", encoding="utf-8")
    assert secret_file(folder / ".envrc") is False
    assert secret_class(folder / ".env") == "name"
    assert secret_class(folder / ".npmrc") == "name"
    assert secret_class(folder / "token.txt") == "name"
    assert secret_class(folder / "key_material.txt") == "content"
    assert secret_file(folder / "notes.txt") is False
    if (folder / "link").is_symlink():
        assert secret_class(folder / "link") == "symlink"
    if (folder / "hard").exists():
        assert secret_class(folder / "hard") == "hardlink"


FLAG_LEAKS = [
    "command cat .npmrc",
    "builtin cat .npmrc",
    "env cat .npmrc",
    "busybox cat .npmrc",
    "nohup cat .npmrc",
    "timeout 2 cat .npmrc",
    "cat -n .npmrc",
    "cat -A .npmrc",
    "uniq -c .npmrc",
    "sort -n .npmrc",
    "sort -m .npmrc",
    "sort -m creds.json",
    'grep "" .npmrc',
    "grep '' creds.json",
    "grep '' bland.txt",
    "grep -e . .npmrc",
    'cat ".npmrc"',
    "cat '.npmrc'",
    "cat .npmr*",
    "cat${IFS}.npmrc",
    "$(echo cat) .npmrc",
    "f=.npmrc; cat $f",
    "head -n 1 .npmrc",
    "head -c 5 .npmrc",
    "tail -n 1 .npmrc",
    "cut -d: -f1 .npmrc",
    "nl -ba .npmrc",
    "fold -w 5 .npmrc",
    "tac -s x .npmrc",
    "sort -r .npmrc",
    "uniq -d .npmrc",
    "grep -F . .npmrc",
    "sed -n p .npmrc",
    "sed -f .npmrc",
    "awk -F: '{print $1}' .npmrc",
    "od -c .npmrc",
    "xxd -p .npmrc",
    "strings -a .npmrc",
    "base64 -w 0 .npmrc",
    "xargs cat .npmrc",
]

FLAG_CONTROLS = [
    "cat notes.txt",
    "cat -n notes.txt",
    "cat .envrc",
    "grep hunter2 notes.txt",
    'grep "" notes.txt',
    "grep -e hunter2 notes.txt",
    "sort notes.txt",
    "sort -n notes.txt",
    "uniq notes.txt",
    "uniq -c notes.txt",
    "head -n 5 README.md",
    "echo .npmrc",
    "command echo .npmrc",
    "grep -e . notes.txt",
    "cat -n .envrc",
]


def test_flags_wrappers_and_empty_patterns_card_secret_operands(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    _plant(folder)
    for command in FLAG_LEAKS:
        assert command_reads_secrets(command, folder), command
    for command in FLAG_CONTROLS:
        assert command_reads_secrets(command, folder) is False, command


EXEC_LEAKS = [
    "eval 'cat .npmrc'",
    'eval "cat creds.json"',
    "eval 'cat bland.txt'",
    "eval \"eval 'cat .npmrc'\"",
    'f=.npmrc; eval "cat $f"',
    "awk 'BEGIN{system(\"cat .npmrc\")}'",
    "awk 'BEGIN{while((getline l < \"bland.txt\")>0) print l}'",
    "cd sub && cat ../bland.txt",
    "cd sub; cat ../bland.txt",
    "pushd sub && cat ../bland.txt",
    "(cd sub; cat ../bland.txt)",
    "cat sub/../bland.txt",
    "cat ./bland.txt",
    "cat /proc/self/cwd/bland.txt",
    "sh -c 'cat .npmrc'",
    "bash -c 'cat creds.json'",
    "xargs sh -c 'cat bland.txt'",
    "watch 'cat .npmrc'",
    "script -c 'cat .npmrc' typescript",
    "ssh localhost 'cat .npmrc'",
]

EXEC_CONTROLS = [
    "cat notes.txt",
    "cat .envrc",
    "eval echo hi",
    "awk '{print $1}' notes.txt",
    "cd sub && cat ok.txt",
    "echo .npmrc",
]


def _prepare_exec_tree(folder: Path) -> None:
    _plant(folder)
    (folder / "sub" / "ok.txt").write_text("ok-note\n", encoding="utf-8")


def test_eval_inline_cd_and_proc_card_secret_operands(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    _prepare_exec_tree(folder)
    for command in EXEC_LEAKS:
        assert command_reads_secrets(command, folder), command
    for command in EXEC_CONTROLS:
        assert command_reads_secrets(command, folder) is False, command


def test_git_readers_card_in_a_real_repository(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    _plant(folder)
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Ada"
    env["GIT_AUTHOR_EMAIL"] = "ada@example.com"
    env["GIT_COMMITTER_NAME"] = "Ada"
    env["GIT_COMMITTER_EMAIL"] = "ada@example.com"
    subprocess.run(["git", "init"], cwd=folder, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=folder, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixtures"], cwd=folder, check=True, capture_output=True, env=env)
    leaks = [
        "git grep hunter2",
        "git grep -n hunter2 -- .npmrc",
        "git grep hunter2 -- bland.txt",
        "git show HEAD:.npmrc",
        "git log -p",
        "git blame bland.txt",
        "git archive HEAD",
        "git cat-file -p HEAD:creds.json",
        "git diff HEAD~1",
        "git diff HEAD~1 HEAD",
        "git grep hunter2 feat",
        "git show refs/stash",
    ]
    controls = [
        "git status",
        "git log",
        "git grep hunter2 -- notes.txt",
        "git diff -- notes.txt",
        "git blame notes.txt",
        "git diff",
        "git diff HEAD",
    ]
    for command in leaks:
        assert command_reads_secrets(command, folder), command
    for command in controls:
        assert command_reads_secrets(command, folder) is False, command


_LIVE_FLAG_CONTROLS = (
    "cat notes.txt",
    "cat -n notes.txt",
    'grep "" notes.txt',
    "cat .envrc",
)


@pytest.mark.parametrize("yolo", [False, True])
def test_live_flag_wrapper_and_empty_pattern_reads_do_not_print(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    calls = [("terminal", {"command": command}) for command in FLAG_LEAKS]
    calls.extend(("terminal", {"command": command}) for command in _LIVE_FLAG_CONTROLS)
    heard, stored, body, _folder = _live(
        tmp_path,
        calls,
        yolo=yolo,
        ask="Read the project file.",
    )
    blob = heard + "\n" + body + "\n" + json.dumps(stored.get("messages") or [])
    for marker in ("_authToken=", "hunter2PASS", "BEGIN PRIVATE KEY", "KEYBODYXYZ", "SECRETENV="):
        assert marker not in blob, (yolo, marker)
    rows = _tool_rows(Handler.payloads)
    assert len(rows) >= len(calls), (yolo, len(rows), len(calls))
    for command, row in zip(FLAG_LEAKS, rows):
        assert "was not run" in row, (yolo, command, row)
        assert "_authToken=" not in row, (yolo, command)
    notes, numbered, empty_grep, envrc = rows[len(FLAG_LEAKS) : len(FLAG_LEAKS) + 4]
    assert "visible-note" in notes and "was not run" not in notes
    assert "export PATH" in envrc and "was not run" not in envrc
    # cat -n is GNU cat. PowerShell's cat is Get-Content and has no -n.
    # grep is not a Windows command. Both are still sent, and neither may print a secret.
    if os.name != "nt":
        assert "visible-note" in numbered and "was not run" not in numbered
        assert "visible-note" in empty_grep and "was not run" not in empty_grep


_LIVE_EXEC_CONTROLS = (
    "eval echo hi",
    "awk '{print $1}' notes.txt",
    "cat .envrc",
    "cat notes.txt",
    "cd sub && cat ok.txt",
)


def _exec_setup(folder: Path) -> None:
    (folder / "sub" / "ok.txt").write_text("ok-note\n", encoding="utf-8")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Ada"
    env["GIT_AUTHOR_EMAIL"] = "ada@example.com"
    env["GIT_COMMITTER_NAME"] = "Ada"
    env["GIT_COMMITTER_EMAIL"] = "ada@example.com"
    subprocess.run(["git", "init"], cwd=folder, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=folder, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixtures"], cwd=folder, check=True, capture_output=True, env=env)


@pytest.mark.parametrize("yolo", [False, True])
def test_live_eval_awk_cd_proc_and_git_do_not_print(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    commands = [
        "eval 'cat .npmrc'",
        'eval "cat creds.json"',
        "eval 'cat bland.txt'",
        "eval \"eval 'cat .npmrc'\"",
        'f=.npmrc; eval "cat $f"',
        "awk 'BEGIN{system(\"cat .npmrc\")}'",
        "awk 'BEGIN{while((getline l < \"bland.txt\")>0) print l}'",
        "cd sub && cat ../bland.txt",
        "cat /proc/self/cwd/bland.txt",
        "cd sub; cat ../bland.txt",
        "pushd sub && cat ../bland.txt",
        "(cd sub; cat ../bland.txt)",
        "cat sub/../bland.txt",
        "cat ./bland.txt",
        "git grep hunter2",
        "git grep -n hunter2 -- .npmrc",
    ]
    calls = [("terminal", {"command": command}) for command in commands]
    calls.extend(("terminal", {"command": command}) for command in _LIVE_EXEC_CONTROLS)
    heard, stored, body, _folder = _live(
        tmp_path,
        calls,
        yolo=yolo,
        ask="Read the project file.",
        setup=_exec_setup,
    )
    blob = heard + "\n" + body + "\n" + json.dumps(stored.get("messages") or [])
    for marker in ("_authToken=", "hunter2PASS", "BEGIN PRIVATE KEY", "KEYBODYXYZ", "SECRETENV="):
        assert marker not in blob, (yolo, marker)
    rows = _tool_rows(Handler.payloads)
    assert len(rows) >= len(calls), (yolo, len(rows), len(calls))
    for command, row in zip(commands, rows):
        assert "was not run" in row, (yolo, command, row)
    echo, awk_notes, envrc, notes, moved = rows[len(commands) : len(commands) + 5]
    assert "export PATH" in envrc and "was not run" not in envrc
    assert "visible-note" in notes and "was not run" not in notes
    # eval, awk, and `&&` are Unix shell. Windows PowerShell 5.1 runs none of them.
    if os.name != "nt":
        assert "hi" in echo and "was not run" not in echo
        assert "visible-note" in awk_notes and "was not run" not in awk_notes
        assert "ok-note" in moved and "was not run" not in moved


def test_listing_1500_chats_does_not_reread_every_transcript(tmp_path):
    """A data directory of many idle chats stays listable. The supervisor does not parse each one."""
    import time

    from easyagent.app import _cached_run_status, _queued_chat_ids, _running_chat_ids
    from easyagent.store import Store

    store = Store(tmp_path)
    store.ensure()
    bot = store.add_bot(name="Ada", endpoint_id="", model="stub")
    for _ in range(1500):
        store.create_chat(bot["id"])
    started = time.perf_counter()
    listed = store.list_chats(bot["id"])
    queued = _queued_chat_ids(store)
    running = _running_chat_ids(store)
    again = _running_chat_ids(store)
    elapsed = time.perf_counter() - started
    assert len(listed) == 1500
    assert queued == []
    assert running == []
    assert again == []
    assert elapsed < 8, elapsed
    sample = listed[0]["id"]
    assert _cached_run_status(store, bot["id"], sample) == ""


def test_live_unknown_tool_lists_valid_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    heard, _stored, body, _folder = _live(
        tmp_path,
        [("not_a_real_tool", {})],
        yolo=False,
        ask="Use a tool.",
    )
    assert "Valid tools" in heard
    assert "read_file" in heard
    assert "rules did not recognize" not in heard
    assert "Valid tools" in body or "Valid tools" in heard


def test_readers_and_obfuscation_card_and_controls_do_not(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    _plant(folder)
    obfuscated = list(OBFUSCATED)
    # A Windows runner without developer mode cannot create the symlink or the
    # hardlink. Those names are not planted, so they are not secret reads.
    if not (folder / "link").exists():
        obfuscated = [command for command in obfuscated if command != "cat link"]
    if not (folder / "hard").exists():
        obfuscated = [command for command in obfuscated if command != "cat hard"]
    for command in READERS + obfuscated:
        assert command_reads_secrets(command, folder), command
    for command in CONTROLS:
        assert command_reads_secrets(command, folder) is False, command
    assert command_reads_secrets("grep -rn KEYBODY .", folder)
    assert command_reads_secrets("grep -rn foo src", folder)
    clean = folder / "empty-tree"
    clean.mkdir()
    (clean / "notes.txt").write_text("visible-note\n", encoding="utf-8")
    assert command_reads_secrets(f"grep -rn visible-note {clean}", folder)
    assert command_reads_secrets("head <(echo hi)", folder) is False


def test_script_prove_list_allows_sleep_and_blocks_escape(tmp_path):
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    store = Store(tmp_path)
    folder = bot_workspace(store, bot["id"], create=True)
    try:
        assert script_body_proved(store, bot["id"], "#!/bin/sh\nsleep 2\necho ok\n", folder)
        assert script_body_proved(
            store,
            bot["id"],
            "#!/usr/bin/env python3\nimport time\ntime.sleep(0)\nprint(1)\n",
            folder,
        )
        assert script_body_proved(store, bot["id"], "def (\n", folder, folder / "bad.py")
        assert script_body_proved(store, bot["id"], "import os\nos.remove('../outside/x')\n", folder) is False
    finally:
        client.close()


def test_native_args_and_name_repair_keep_offset_and_limit():
    read = _request_from_call({"name": "read_file", "arguments": {"path": "notes.txt", "offset": 1500, "limit": "50"}})
    assert read.action == "read" and read.offset == 1500 and read.limit == 50
    camel = _request_from_call({"name": "readFile", "arguments": {"path": "notes.txt", "limit": "20"}})
    assert camel.action == "read" and camel.limit == 20
    suffixed = _request_from_call({"name": "read_file_tool", "arguments": {"path": "notes.txt", "limit": "20"}})
    assert suffixed.action == "read" and suffixed.limit == 20
    shell = _request_from_call({"name": "terminal", "arguments": {"command": "pwd", "timeout": "2", "workdir": "sub"}})
    assert shell.kind == "shell" and shell.timeout == 2 and shell.workdir == "sub"
    with pytest.raises(Exception) as exc:
        _request_from_call({"name": "not_a_real_tool", "arguments": {}})
    assert "Valid tools" in str(exc.value)
    first = ToolRequest(kind="files", action="read", path="a.py", offset=1, limit=50)
    second = ToolRequest(kind="files", action="read", path="a.py", offset=51, limit=50)
    assert _tool_signature(first) != _tool_signature(second)


def test_a_one_letter_typo_does_not_patch(tmp_path):
    path = tmp_path / "word.txt"
    path.write_text("retries\nalpha\n", encoding="utf-8")
    original = path.read_bytes()
    _content, count, _strategy, error = fuzzy_find_and_replace("retries\n", "retrys\n", "fixed\n", False)
    assert count == 0
    assert error
    _content, count, strategy, error = fuzzy_find_and_replace("alpha\n", "alpah\n", "beta\n", False)
    assert count == 0 and error
    drifted, count, strategy, error = fuzzy_find_and_replace("    alpha\n", "alpha\n", "beta\n", False)
    assert error is None and count == 1 and "beta" in drifted
    assert strategy in {"indentation_flexible", "whitespace_normalized", "line_trimmed", "exact"}
    spaced, count, strategy, error = fuzzy_find_and_replace("alpha  \n", "alpha\n", "beta\n", False)
    assert error is None and count == 1
    assert path.read_bytes() == original


def test_rg_never_opens_a_named_secret(tmp_path, monkeypatch):
    from easyagent.coding import search_tree

    folder = tmp_path.parent / f"ea-rg-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    (folder / "id_rsa").write_text("KEYBODYXYZ\n", encoding="utf-8")
    (folder / "ok.txt").write_text("NEEDLE line\n", encoding="utf-8")
    nested = folder / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    (nested / "index.js").write_text("NEEDLE hidden\n", encoding="utf-8")
    found = search_tree(folder, "NEEDLE")
    used_python = False
    if "NEEDLE line" not in found:
        import easyagent.coding as coding

        # rg is absent, or the program on PATH is not ripgrep. The Python walk
        # is the result that has to find the ordinary line and skip the key.
        monkeypatch.setattr(coding.shutil, "which", lambda _name: None)
        found = search_tree(folder, "NEEDLE")
        used_python = True
    assert "NEEDLE line" in found
    assert "KEYBODYXYZ" not in found
    assert "NEEDLE hidden" not in found
    if not used_python and shutil.which("rg") is not None:
        assert "via rg" in found
    # openat is a Linux strace check. It still runs on Linux and on macOS when
    # both strace and rg are installed. Windows has no strace, so it stops here.
    if sys.platform not in {"linux", "darwin"}:
        return
    if shutil.which("strace") is None or shutil.which("rg") is None:
        return
    runner = tmp_path / "search_once.py"
    runner.write_text(
        "import sys\nfrom pathlib import Path\nfrom easyagent.coding import search_tree\n"
        "print(search_tree(Path(sys.argv[1]), 'NEEDLE'))\n",
        encoding="utf-8",
    )
    import site

    env = os.environ.copy()
    env.pop("PYTHONSAFEPATH", None)
    env.pop("PYTHONNOUSERSITE", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path("/tmp/ea-0324")), site.getusersitepackages(), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        ["strace", "-f", "-e", "trace=openat", sys_executable(), str(runner), str(folder)],
        cwd=str(folder),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert "NEEDLE line" in proc.stdout
    assert "KEYBODYXYZ" not in proc.stdout
    assert "NEEDLE hidden" not in proc.stdout
    assert "via rg" in proc.stdout
    opened = [
        line for line in proc.stderr.splitlines()
        if "openat" in line and "id_rsa" in line and "O_RDONLY" in line
    ]
    assert opened == [], opened


def sys_executable() -> str:
    return os.environ.get("EASYAGENT_PYTHON") or __import__("sys").executable


def _live(tmp_path, calls: list[tuple[str, dict]], *, yolo: bool, ask: str, mode: str = "", setup=None):
    from easyagent.safety import set_mode
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    if mode:
        set_mode(Store(tmp_path), bot["id"], mode)
    _plant(folder)
    if setup is not None:
        setup(folder)
    Handler.scripts = [_tool_sse(name, args, f"call_{index}") for index, (name, args) in enumerate(calls)]
    Handler.scripts.append([
        'data: {"choices":[{"delta":{"content":"The distinctive summary sentence is kept with the file."}}]}',
        "data: [DONE]",
    ])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        if yolo:
            toggled = client.post(f"{url}/yolo", json={"on": True})
            assert toggled.status_code == 200, toggled.text
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": ask},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            body = response.read().decode("utf-8", errors="replace")
        stored = client.get(url).json()
        return _tool_text(Handler.payloads), stored, body, folder
    finally:
        client.close()
        server.shutdown()


@pytest.mark.parametrize("yolo", [False, True])
def test_live_secret_readers_do_not_print(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    commands = [
        "more .env",
        "tac id_rsa",
        "nl .env",
        "python3 -c \"print(open('id_rsa').read())\"",
        "find . -type f | xargs cat",
        "cat .npmrc",
        "cat token.txt",
        "cat key_material.txt",
        "cat < .env",
        "base64 id_rsa",
    ]
    for command in commands:
        heard, stored, body, _folder = _live(
            tmp_path,
            [("terminal", {"command": command})],
            yolo=yolo,
            ask="Read the project file.",
        )
        blob = heard + "\n" + body + "\n" + json.dumps(stored.get("messages") or [])
        assert "KEYBODYXYZ" not in blob, (command, yolo, heard)
        assert "SECRETENV=1" not in blob, (command, yolo)
        assert "hunter2PASS" not in blob, (command, yolo)
        assert "_authToken=" not in blob, (command, yolo)
        assert "BEGIN PRIVATE KEY" not in blob, (command, yolo)


@pytest.mark.parametrize("yolo", [False, True])
def test_live_ordinary_reads_still_run(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    heard, _stored, _body, _folder = _live(
        tmp_path,
        [("terminal", {"command": "cat notes.txt"})],
        yolo=yolo,
        ask="Show the notes.",
    )
    assert "visible-note" in heard
    assert "KEYBODYXYZ" not in heard
    assert "waits for you" not in heard


def test_live_search_hides_sniffed_and_named_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    for yolo in (False, True):
        heard, _stored, _body, _folder = _live(
            tmp_path,
            [("search_files", {"pattern": "visible-note|KEYBODY|hunter2|SECRETENV|BEGIN|authToken|token=", "path": "."})],
            yolo=yolo,
            ask="Search the project notes.",
        )
        assert "visible-note" in heard, (yolo, heard)
        for marker in ("KEYBODYXYZ", "hunter2PASS", "SECRETENV=", "BEGIN PRIVATE KEY", "_authToken="):
            assert marker not in heard, (yolo, marker, heard)


def _path_shown(heard: str, folder: Path) -> bool:
    """True when heard contains this folder in either slash, case, or \\\\?\\ spelling."""
    from easyagent.strictshell import _canonical_key, _fold_text, _strip_extended, windows_key

    folded = os.path.normcase(_strip_extended(heard)).replace("\\", "/")
    raw = os.fspath(folder)
    candidates = [raw, _fold_text(raw)]
    key = _canonical_key(folder)
    if key:
        candidates.append(key)
    for item in candidates:
        text = os.path.normcase(item).replace("\\", "/")
        if text and text in folded:
            return True
    wanted = windows_key(raw)
    for line in heard.splitlines():
        piece = line.strip()
        if piece.lower().startswith("cwd "):
            piece = piece[4:].strip()
        if not piece or len(piece) > 400:
            continue
        if windows_key(piece) == wanted:
            return True
        if key and _canonical_key(piece) == key:
            return True
    return False


def test_live_native_read_offset_timeout_and_workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    from easyagent.safety import set_mode
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    # seq, sleep, and sh -c are unlisted in strict mode. Careful is the mode
    # that already runs them; this test does not add them to the strict list.
    set_mode(Store(tmp_path), bot["id"], "careful")
    lines = "\n".join(f"row {number}" for number in range(1, 3001)) + "\n"
    (folder / "long.txt").write_text(lines, encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "here.txt").write_text("in-sub\n", encoding="utf-8")
    (folder / "slow.sh").write_text("#!/bin/sh\nsleep 5\necho late\n", encoding="utf-8")
    (folder / "bye.sh").write_text("#!/bin/sh\necho bye\nexit 3\n", encoding="utf-8")
    # sh and pwd are not Windows commands. sleep and echo run in PowerShell.
    # The workdir is reported even when that command prints nothing.
    if os.name == "nt":
        shell_scripts = [
            _tool_sse("terminal", {"command": "sleep 5", "timeout": 2}, "call_to"),
            _tool_sse("terminal", {"command": "echo bye; exit 3"}, "call_ex"),
            _tool_sse("terminal", {"command": "echo ok"}, "call_ok"),
            _tool_sse("terminal", {"command": "echo ok", "workdir": "sub"}, "call_wd"),
        ]
    else:
        shell_scripts = [
            _tool_sse("terminal", {"command": "sh slow.sh", "timeout": 2}, "call_to"),
            _tool_sse("terminal", {"command": "sh bye.sh"}, "call_script"),
            _tool_sse("terminal", {"command": "sh -c 'echo bye; exit 3'"}, "call_ex"),
            _tool_sse("terminal", {"command": "echo ok"}, "call_ok"),
            _tool_sse("terminal", {"command": "pwd", "workdir": "sub"}, "call_wd"),
        ]
    Handler.scripts = [
        _tool_sse("read_file", {"path": "long.txt", "offset": 1500, "limit": 50}, "call_read"),
        *shell_scripts,
        [
            'data: {"choices":[{"delta":{"content":"The distinctive summary sentence is kept with the file."}}]}',
            "data: [DONE]",
        ],
    ]
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": "Read the middle of long.txt, then run the checks."},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        heard = _tool_text(Handler.payloads)
        assert "1500|row 1500" in heard or "row 1500" in heard
        assert "row 1549" in heard
        assert "next_offset" in heard
        assert _path_shown(heard, folder / "sub"), heard
        assert "timed out after 2 s" in heard
        assert heard.count("exit code 3") >= 1 and "bye" in heard
        assert "exit code 0" in heard
    finally:
        client.close()
        server.shutdown()


def test_live_cd_persists_inside_the_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    heard, _stored, _body, folder = _live(
        tmp_path,
        [
            ("terminal", {"command": "cd .."}),
            ("terminal", {"command": "cd sub"}),
            ("terminal", {"command": "pwd"}),
        ],
        yolo=False,
        ask="Move into sub and print the folder.",
    )
    assert _path_shown(heard, folder / "sub"), heard
    assert "outside" in heard or "not run" in heard


def test_live_long_command_keeps_the_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    heard, _stored, _body, _folder = _live(
        tmp_path,
        [("terminal", {"command": "seq 1 200000"})],
        yolo=False,
        ask="Print the numbers.",
        mode="careful",
    )
    assert "OUTPUT TRUNCATED" in heard
    assert heard.rstrip().endswith("200000") or "\n200000\n" in heard or heard.rstrip().endswith("200000\n")
    assert "200000" in heard.split("TRUNCATED")[-1]


def test_live_summary_survives_a_write(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    target = folder / "note.txt"
    sentence = "The distinctive summary sentence is kept with the file."
    Handler.scripts = [
        _tool_sse("write_file", {"path": str(target), "content": "agenda\n"}, "call_w"),
        [
            f'data: {{"choices":[{{"delta":{{"content":"{sentence}"}}}}]}}',
            "data: [DONE]",
        ],
    ]
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": "Write the note."},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        stored = client.get(url).json()
        text = "\n".join(message.get("content") or "" for message in stored["messages"])
        assert sentence in text
        assert "in ." not in text
        assert target.read_text(encoding="utf-8") == "agenda\n"
    finally:
        client.close()
        server.shutdown()


def test_three_windows_and_three_patches_are_not_stuck(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    path = folder / "edit.txt"
    path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    big = folder / "rows.txt"
    big.write_text("\n".join(f"row {n}" for n in range(1, 160)) + "\n", encoding="utf-8")
    Handler.scripts = [
        _tool_sse("read_file", {"path": str(big), "offset": 1, "limit": 50}, "r1"),
        _tool_sse("read_file", {"path": str(big), "offset": 51, "limit": 50}, "r2"),
        _tool_sse("read_file", {"path": str(big), "offset": 101, "limit": 50}, "r3"),
        _tool_sse("read_file", {"path": str(path)}, "r4"),
        _tool_sse("patch", {"path": str(path), "old_string": "alpha", "new_string": "ALPHA"}, "p1"),
        _tool_sse("patch", {"path": str(path), "old_string": "beta", "new_string": "BETA"}, "p2"),
        _tool_sse("patch", {"path": str(path), "old_string": "gamma", "new_string": "GAMMA"}, "p3"),
        [
            'data: {"choices":[{"delta":{"content":"The distinctive summary sentence is kept with the file."}}]}',
            "data: [DONE]",
        ],
    ]
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": f"Update {path} after you read {big}."},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        stored = client.get(url).json()
        text = "\n".join(message.get("content") or "" for message in stored["messages"])
        heard = _tool_text(Handler.payloads)
        assert "Stuck" not in text, text
        assert "row 1" in heard and "row 51" in heard and "row 101" in heard
        assert path.read_text(encoding="utf-8") == "ALPHA\nBETA\nGAMMA\n", heard
    finally:
        client.close()
        server.shutdown()


def test_live_yolo_proved_scripts_skip_the_card_and_a_breakout_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    outside = folder.parent / "outside"
    outside.mkdir()
    canary = outside / "x"
    canary.write_text("keep\n", encoding="utf-8")
    bodies = {
        "sleep.sh": "#!/bin/sh\nsleep 2\necho ok\n",
        "bash.sh": "#!/usr/bin/env bash\necho hi\n",
        "py.sh": "#!/usr/bin/env python3\nimport time\ntime.sleep(0)\nprint(1)\n",
        "pybin.sh": "#!/usr/bin/python3\nprint(1)\n",
        "plain.sh": "echo ok\n",
        "syn.py": "def ok(\n    return 1\n",
        "bad.py": "#!/usr/bin/env python3\nimport os\nos.remove('../outside/x')\n",
    }
    Handler.scripts = [
        _tool_sse("write_file", {"path": str(folder / name), "content": body}, f"w{index}")
        for index, (name, body) in enumerate(bodies.items())
    ]
    Handler.scripts.append(_tool_sse("terminal", {"command": "sh sleep.sh"}, "run"))
    Handler.scripts.append([
        'data: {"choices":[{"delta":{"content":"The scripts are in the folder."}}]}',
        "data: [DONE]",
    ])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        toggled = client.post(f"{url}/yolo", json={"on": True})
        assert toggled.status_code == 200, toggled.text
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": f"Write the helper scripts in {folder}."},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        heard = _tool_text(Handler.payloads)
        for name in ("sleep.sh", "bash.sh", "py.sh", "pybin.sh", "plain.sh", "syn.py"):
            assert (folder / name).is_file(), name
        assert "SyntaxError" in heard and "line 1" in heard
        assert not (folder / "bad.py").exists()
        assert canary.read_text(encoding="utf-8") == "keep\n"
        assert "ok" in heard
        assert "waits for you" not in (folder / "sleep.sh").read_text(encoding="utf-8")
    finally:
        client.close()
        server.shutdown()


def test_live_summary_survives_a_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    path = folder / "note.txt"
    path.write_text("alpha\n", encoding="utf-8")
    sentence = "The distinctive summary sentence is kept with the file."
    Handler.scripts = [
        _tool_sse("read_file", {"path": str(path)}, "r1"),
        _tool_sse("patch", {"path": str(path), "old_string": "alpha", "new_string": "beta"}, "p1"),
        [
            f'data: {{"choices":[{{"delta":{{"content":"{sentence}"}}}}]}}',
            "data: [DONE]",
        ],
    ]
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": f"Update {path} after you read it."},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        stored = client.get(url).json()
        text = "\n".join(message.get("content") or "" for message in stored["messages"])
        assert sentence in text
        assert "in ." not in text
        assert path.read_text(encoding="utf-8") == "beta\n"
    finally:
        client.close()
        server.shutdown()


@pytest.mark.skipif(os.environ.get("EASYAGENT_SLOW") != "1", reason="wall-clock 20s search")
def test_python_search_hits_the_twenty_second_cap(tmp_path, monkeypatch):
    import time

    import easyagent.coding as coding
    from easyagent.coding import search_tree

    monkeypatch.setattr(coding.shutil, "which", lambda _name: None)
    folder = tmp_path / "slowtree"
    folder.mkdir()
    for number in range(80):
        (folder / f"n{number}.txt").write_text("plain\n", encoding="utf-8")
    original = Path.read_text

    def slow_read(self, *args, **kwargs):
        time.sleep(0.3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", slow_read)
    started = time.monotonic()
    found = search_tree(folder, "NOMATCH")
    elapsed = time.monotonic() - started
    assert "stopped" in found and "20" in found
    assert 15 <= elapsed <= 35, elapsed


def test_search_still_skips_node_modules_from_tmp(tmp_path):
    from easyagent.coding import search_tree

    folder = tmp_path.parent / f"ea-cwd-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    (folder / "ok.txt").write_text("NEEDLE visible\n", encoding="utf-8")
    hidden = folder / "node_modules" / "pkg"
    hidden.mkdir(parents=True)
    (hidden / "index.js").write_text("NEEDLE hidden\n", encoding="utf-8")
    found = search_tree(folder, "NEEDLE")
    assert "NEEDLE visible" in found
    assert "NEEDLE hidden" not in found
    if shutil.which("rg") is not None:
        assert "via rg" in found


SHELL_CLUSTER_LEAKS = [
    "sh -ec 'cat .npmrc'",
    "sh -xc 'cat .npmrc'",
    "bash -ec 'cat .npmrc'",
    "bash -xc 'cat creds.json'",
    "bash -elc 'cat bland.txt'",
    "bash -ic 'grep . .npmrc'",
    "bash -uc 'cat -n .npmrc'",
    "zsh -ec 'cat .npmrc'",
    "dash -ec 'cat .npmrc'",
    "ksh -ec 'cat .npmrc'",
    "ash -ec 'cat .npmrc'",
    "busybox sh -ec 'cat .npmrc'",
    "bash -o pipefail -c 'cat .npmrc'",
    "bash --rcfile /dev/null -c 'cat .npmrc'",
    'echo a & cat .npmrc',
    'echo a & echo b & cat bland.txt',
    'echo a & echo b & cat creds.json',
    'echo a & echo b & cat key_material.txt',
    "env -S 'cat .npmrc'",
    "env -S 'sort -n creds.json'",
    "env --split-string 'cat .npmrc'",
    "env -S'cat .npmrc'",
    'ls .e* | xargs cat',
    'ls .e* | while read f; do cat $f; done',
    "sh -c 'echo hi'",
    "sh -ce 'echo hi'",
    "bash -cx 'echo hi'",
    "bash -ec 'echo hi'",
    "bash -lc 'echo hi'",
    "bash -xec 'echo hi'",
    "sh -o errexit -c 'echo hi'",
    "bash -o errexit -c 'echo hi'",
    "env -C sub echo hi",
    "env --chdir=sub echo hi",
    "env -S 'echo hi'",
    "env -S 'echo\\_hi'",
]

DEV_COMMANDS = (
    'git status',
    'git status --short',
    'git diff',
    'git diff --stat',
    'git log --oneline',
    'git log -p -1',
    'git branch',
    'git show HEAD',
    'git blame src/app.py',
    'git add -A',
    'git commit -qm wip',
    'git stash',
    'git checkout -b feat',
    'git grep TODO',
    'git ls-files',
    'ls',
    'ls -la',
    'ls -R',
    'ls src',
    'pwd',
    'tree',
    "find . -name '*.py'",
    'find src -type f',
    "find . -name '*.py' | xargs wc -l",
    'wc -l src/app.py',
    'du -sh .',
    'which python3',
    'cat src/app.py',
    'cat -n src/app.py',
    'head -n 5 src/app.py',
    'tail -n 5 src/app.py',
    'less src/app.py',
    'cat README.md',
    'cat package.json',
    'cat config.yaml',
    'cat .env.example',
    'cat Makefile',
    'cat .gitignore',
    'cat src/*.py',
    'cat data.csv',
    'grep -rn TODO src',
    'grep -rn add .',
    'grep -n def src/app.py',
    'grep -r TODO .',
    "grep -rn 'TODO' --include='*.py' .",
    'grep -c . notes.txt',
    'rg TODO',
    'rg -n add src',
    'rg --files',
    'grep -i hello notes.txt',
    'grep -e . notes.txt',
    "grep '' notes.txt",
    'sort -n data.csv',
    'sort notes.txt',
    'uniq -c notes.txt',
    'cut -d, -f1 data.csv',
    'wc -l data.csv',
    'diff notes.txt README.md',
    'nl src/app.py',
    'tac notes.txt',
    'python3 -m pytest -q',
    'python3 -m pytest tests -q',
    'pytest -q',
    'python3 script.py',
    "python3 -c 'print(1+1)'",
    'python3 --version',
    'python3 -m unittest discover -s tests',
    'python3 -m py_compile src/app.py',
    'python3 -m json.tool package.json',
    'pip --version',
    'pip list',
    'npm test',
    'npm --version',
    'node --version',
    "node -e 'console.log(1)'",
    'make',
    'make test',
    'cd sub && make',
    'cd sub && ls',
    'cd src && cat app.py',
    'cd src; python3 ../script.py',
    './run.sh',
    'sh run.sh',
    'bash run.sh',
    "sed -n '1,5p' src/app.py",
    "sed 's/add/plus/' src/app.py",
    "sed -i 's/TODO/DONE/' src/app.py",
    "awk '{print $1}' notes.txt",
    "awk -F, '{print $1}' data.csv",
    "awk 'NR>1' data.csv",
    "awk '{s+=$1} END{print s}' data.csv",
    "perl -pe 's/a/b/' notes.txt",
    'python3 -c "print(open(\'notes.txt\').read())"',
    'python3 -c "print(open(\'src/app.py\').read())"',
    'for f in src/*.py; do echo $f; done',
    'for f in src/*.py; do wc -l $f; done',
    'for f in *.md; do cat $f; done',
    'for i in 1 2 3; do echo $i; done',
    'while read l; do echo $l; done < notes.txt',
    'if [ -f README.md ]; then echo yes; fi',
    'F=src/app.py; cat $F',
    'D=src; ls $D',
    'D=src; cat $D/app.py',
    'echo $HOME',
    'echo $PWD',
    'cat $PWD/src/app.py',
    'cat ./src/app.py',
    'cat src/../README.md',
    'cat "src/app.py"',
    "cat 'src/app.py'",
    'X=1 python3 script.py',
    'env | head -3',
    'env FOO=1 python3 script.py',
    'timeout 10 python3 script.py',
    'time python3 script.py',
    'nice python3 script.py',
    'command -v python3',
    'cat src/app.py | grep def',
    'cat src/app.py | head -2',
    'grep -rn def src | sort',
    'ls | wc -l',
    'ls src | head',
    "find . -name '*.py' | head -3",
    'git log --oneline | head -3',
    'git diff | head',
    'echo hi > out.txt',
    'echo hi >> out.txt',
    'cat out.txt',
    'mkdir -p build',
    'touch build/x',
    'cp README.md build/README.md',
    'mv build/README.md build/R2.md',
    'rm build/x',
    'rm -f out.txt',
    'tar czf b.tgz src',
    'tar tzf b.tgz',
    'zip -q b.zip README.md',
    'unzip -l b.zip',
    'curl --version',
    'chmod +x run.sh',
    'ln -s README.md rl',
    'cp notes.txt notes2.txt',
    'tee t.txt < notes.txt',
    'cat notes.txt > n3.txt',
    'git diff --no-index notes.txt notes2.txt',
    'xargs -a notes.txt echo',
    'sleep 1',
    'date',
    'uname -a',
    'whoami',
    'id',
    'df -h .',
    'free -m',
    'ps aux | head -3',
    'git remote -v',
    'git config user.name',
    'git tag',
    'git fetch',
    'git pull',
    'git push',
    'docker ps',
    'pip install -r requirements.txt',
    'npm install',
    'npm run test',
    'yarn test',
    'cargo --version',
    'go version',
    'tsc --version',
    'pytest tests/test_app.py::test_add',
    'python3 -m pytest -x -q 2>&1 | tail -3',
    'make 2>&1 | tail -1',
    'cat src/app.py src/util.py',
    'head -c 50 src/app.py',
    'od -c src/app.py | head -2',
    'file src/app.py',
    'stat src/app.py',
    'md5sum src/app.py',
    'sha256sum README.md',
    'base64 README.md',
    'strings README.md',
    'xxd README.md | head -1',
    'hexdump -C README.md | head -1',
    'jq . package.json',
    'yq . config.yaml',
    'git grep -n add -- src',
    'git grep -e TODO',
    'git show HEAD:README.md',
    'git cat-file -p HEAD:src/app.py',
)

DEV_CARDS = {
    'git checkout -b feat': 'unlisted-command',
    'python3 -m py_compile src/app.py': 'inline-code',
    'python3 -m json.tool package.json': 'inline-code',
    "awk 'NR>1' data.csv": 'unmatched-shell',
    "perl -pe 's/a/b/' notes.txt": 'exec-flag',
    'if [ -f README.md ]; then echo yes; fi': 'unlisted-command',
    'env | head -3': 'unlisted-command',
    'nice python3 script.py': 'unlisted-command',
    'echo hi > out.txt': 'redirect-overwrite',
    'echo hi >> out.txt': 'redirect-overwrite',
    'mv build/README.md build/R2.md': 'move',
    'rm build/x': 'delete',
    'rm -f out.txt': 'delete',
    'tar czf b.tgz src': 'unlisted-command',
    'tar tzf b.tgz': 'unlisted-command',
    'zip -q b.zip README.md': 'unlisted-command',
    'unzip -l b.zip': 'unlisted-command',
    'chmod +x run.sh': 'unlisted-command',
    'ln -s README.md rl': 'unlisted-command',
    'tee t.txt < notes.txt': 'unlisted-command',
    'cat notes.txt > n3.txt': 'redirect-overwrite',
    'git diff --no-index notes.txt notes2.txt': 'exec-flag',
    'git config user.name': 'exec-flag',
    'git push': 'git-destructive',
    'pip install -r requirements.txt': 'install',
    'npm install': 'install',
    'base64 README.md': 'unlisted-command',
    'xxd README.md | head -1': 'unlisted-command',
    'jq . package.json': 'unlisted-command',
    'yq . config.yaml': 'unlisted-command',
    'git grep -e TODO': 'exec-flag',
    'git cat-file -p HEAD:src/app.py': 'unlisted-command',
    "find . -name '*.py' | xargs wc -l": 'read-secret',
    'xargs -a notes.txt echo': 'read-secret',
    'grep -rn TODO src': 'read-secret',
    'grep -rn add .': 'read-secret',
    'grep -r TODO .': 'read-secret',
    'rg TODO': 'read-secret',
    'rg -n add src': 'read-secret',
    'grep -rn def src | sort': 'read-secret',
    "sed -i 's/TODO/DONE/' src/app.py": 'read-secret',
}

DEV_YOLO_CARDS = frozenset({
    "awk 'NR>1' data.csv",
    'env | head -3',
    'git cat-file -p HEAD:src/app.py',
    'git checkout -b feat',
    'git config user.name',
    'git diff --no-index notes.txt notes2.txt',
    'git grep -e TODO',
    'git push',
    'jq . package.json',
    'mv build/README.md build/R2.md',
    'npm install',
    "perl -pe 's/a/b/' notes.txt",
    'pip install -r requirements.txt',
    'python3 -m json.tool package.json',
    'python3 -m py_compile src/app.py',
    'rm build/x',
    'yq . config.yaml',
    "find . -name '*.py' | xargs wc -l",
    'xargs -a notes.txt echo',
    'grep -rn TODO src',
    'grep -rn add .',
    'grep -r TODO .',
    'rg TODO',
    'rg -n add src',
    'grep -rn def src | sort',
    "sed -i 's/TODO/DONE/' src/app.py",
})


def test_here_string_into_a_shell_cards(tmp_path):
    """A shell that reads stdin runs that text. ``grep x <<< foo`` does not."""
    folder = tmp_path / "tree"
    folder.mkdir()
    _plant(folder)
    scripts = (
        "cat .env",
        "cat id_rsa",
        "cat key_material.txt",
        "head id_rsa",
        "cat .npmrc",
    )
    shells = ("sh", "bash", "dash", "zsh")
    for shell in shells:
        for script in scripts:
            assert command_reads_secrets(f"{shell} <<< '{script}'", folder), (shell, script)
            assert command_reads_secrets(f'{shell} <<< "{script}"', folder), (shell, script)
            assert command_reads_secrets(f"{shell} -s <<< '{script}'", folder), (shell, script)
            assert command_reads_secrets(f"echo '{script}' | {shell}", folder), (shell, script)
            assert command_reads_secrets(f"{shell} <<'EOF'\n{script}\nEOF", folder), (shell, script)
            assert command_reads_secrets(f"{shell} -s < <(echo '{script}')", folder), (shell, script)
    assert command_reads_secrets("sh <<< 'cat .env'", folder)
    assert command_reads_secrets("bash -s <<< 'cat .env'", folder)
    assert command_reads_secrets("""sh -s <<< "cat ."'env'""", folder)
    assert command_reads_secrets("sh -s <<< 'cat id_rsa'", folder)
    assert command_reads_secrets("bash -s -- <<< 'cat .env'", folder)
    assert command_reads_secrets("command sh <<< 'cat .env'", folder)
    assert command_reads_secrets("printf '%s\\n' 'cat .env' | dash", folder)
    assert command_reads_secrets("sh <<< 'echo hi'", folder) is False
    assert command_reads_secrets("sh <<< 'cat notes.txt'", folder) is False
    assert command_reads_secrets("grep x <<< foo", folder) is False
    assert command_reads_secrets("grep x <<< 'hello'", folder) is False
    assert command_reads_secrets("grep -e x <<< foo", folder) is False
    assert command_reads_secrets("echo 'echo hi' | sh", folder) is False
    assert command_reads_secrets("bash -s < <(echo 'echo hi')", folder) is False
    assert command_reads_secrets("head <(echo hi)", folder) is False
    assert command_reads_secrets("cat < .env", folder)
    assert command_reads_secrets("sh <<'EOF'\ncat .env\nEOF", folder)


def test_process_substitution_into_a_shell_stays_a_card_under_yolo(tmp_path):
    """``/yolo`` does not approve a script built by ``<(echo 'cat .env')``."""
    from easyagent import turn as turn_mod
    from easyagent.safety import classify, set_mode
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace
    from easyagent.yolo import _enable, covers

    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    set_mode(store, bot["id"], "strict")
    folder = bot_workspace(store, bot["id"], create=True)
    _plant(folder)
    chat = store.create_chat(bot["id"])
    _enable(store, bot["id"], chat["id"], seconds=3600, idle_seconds=3600)
    slot = turn_mod.bind(store, chat["id"], bot["id"])
    slot.cwd = str(folder)
    slot.store = store
    command = "bash -s < <(echo 'cat .env')"
    request = ToolRequest(kind="shell", command=command)
    try:
        assert command_reads_secrets(command, folder)
        verdict = classify(store, request, bot["id"])
        assert verdict.tier == "ask", (verdict.tier, verdict.rule, verdict.why)
        assert covers(store, bot["id"], request, verdict) is False
    finally:
        turn_mod._slot.set(None)


def test_interpreter_stdin_program_cards(tmp_path):
    """A program on stdin is the script. ``python - <<< 'print(1)'`` still runs."""
    folder = tmp_path / "tree"
    folder.mkdir()
    _plant(folder)
    secret_py = 'print(open(".env").read())'
    node_secret = 'console.log(require("fs").readFileSync(".env","utf8"))'
    ruby_secret = 'puts File.read(".env")'
    forms = (
        "python - <<< '{body}'",
        "python3 - <<< '{body}'",
        "python - <<'EOF'\n{body}\nEOF",
        "python - < <(echo '{body}')",
        "echo '{body}' | python -",
    )
    for form in forms:
        assert command_reads_secrets(form.format(body=secret_py), folder), form
    assert command_reads_secrets(f"node - <<< '{node_secret}'", folder)
    assert command_reads_secrets(f"node - <<'EOF'\n{node_secret}\nEOF", folder)
    assert command_reads_secrets(f"node - < <(echo '{node_secret}')", folder)
    assert command_reads_secrets(f"echo '{node_secret}' | node -", folder)
    assert command_reads_secrets(f"ruby <<< '{ruby_secret}'", folder)
    assert command_reads_secrets(f"ruby <<'EOF'\n{ruby_secret}\nEOF", folder)
    assert command_reads_secrets(f"ruby < <(echo '{ruby_secret}')", folder)
    assert command_reads_secrets(f"echo '{ruby_secret}' | ruby", folder)
    assert command_reads_secrets("perl - <<< 'open F, \".env\"; print <F>'", folder)
    assert command_reads_secrets("php <<< '<?php echo file_get_contents(\".env\");'", folder)
    assert command_reads_secrets("deno run - <<< 'Deno.readTextFileSync(\".env\")'", folder)
    assert command_reads_secrets("bun - <<< 'await Bun.file(\".env\").text()'", folder)
    # Windows spelling: .exe, the py launcher, and a backslash inside the script.
    assert command_reads_secrets("python.exe - <<< 'print(open(\".env\").read())'", folder)
    assert command_reads_secrets("py -3 - <<< 'print(open(\".env\").read())'", folder)
    assert command_reads_secrets(f"node.exe - <<< '{node_secret}'", folder)
    assert command_reads_secrets("ruby.exe <<< 'puts File.read(\".env\")'", folder)
    assert command_reads_secrets(r"""python - <<< 'print(open(".\\.env").read())'""", folder)
    # Default-deny: a syntax error, a variable path, and a call that is not on the list.
    assert command_reads_secrets("python - <<< 'print('", folder)
    assert command_reads_secrets("python - <<< 'print(open(path).read())'", folder)
    assert command_reads_secrets("node - <<< 'require(\"fs\").readFileSync(name)'", folder)
    assert command_reads_secrets("python - <<< 'import shutil; shutil.rmtree(\"/\")'", folder)
    for command in (
        "python - <<< 'print(1)'",
        "node - <<< 'console.log(1)'",
        "ruby <<< 'puts 1'",
        "perl - <<< 'print 1'",
        "php <<< '<?php echo 1;'",
        "deno run - <<< 'console.log(1)'",
        "bun - <<< 'console.log(1)'",
        "python - <<< 'print(open(\"notes.txt\").read())'",
        "node - <<< 'require(\"fs\").readFileSync(\"notes.txt\",\"utf8\")'",
        "ruby <<< 'puts File.read(\"notes.txt\")'",
        "python -c 'print(1)'",
        "python --version",
        "node --version",
        "echo 'print(1)' | python -",
        "python - < <(echo 'print(1)')",
        "python script.py <<< 'print(open(\".env\").read())'",
        "grep x <<< foo",
        "head <(echo hi)",
    ):
        assert command_reads_secrets(command, folder) is False, command


def test_interpreter_stdin_stays_a_card_under_yolo(tmp_path):
    """``/yolo`` does not approve a program on stdin that reads a secret."""
    from easyagent import turn as turn_mod
    from easyagent.safety import classify, set_mode
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace
    from easyagent.yolo import _enable, covers

    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    set_mode(store, bot["id"], "strict")
    folder = bot_workspace(store, bot["id"], create=True)
    _plant(folder)
    chat = store.create_chat(bot["id"])
    _enable(store, bot["id"], chat["id"], seconds=3600, idle_seconds=3600)
    slot = turn_mod.bind(store, chat["id"], bot["id"])
    slot.cwd = str(folder)
    slot.store = store
    secret = (
        "python - <<< 'print(open(\".env\").read())'",
        "python - < <(echo 'print(open(\".env\").read())')",
        "node - <<< 'console.log(require(\"fs\").readFileSync(\".env\",\"utf8\"))'",
        "ruby <<< 'puts File.read(\".env\")'",
    )
    harmless = (
        "python - <<< 'print(1)'",
        "node - <<< 'console.log(1)'",
        "ruby <<< 'puts 1'",
    )
    try:
        for command in secret:
            request = ToolRequest(kind="shell", command=command)
            assert command_reads_secrets(command, folder), command
            verdict = classify(store, request, bot["id"])
            assert verdict.tier == "ask", (command, verdict.tier, verdict.rule)
            assert covers(store, bot["id"], request, verdict) is False, command
        for command in harmless:
            request = ToolRequest(kind="shell", command=command)
            assert command_reads_secrets(command, folder) is False, command
            verdict = classify(store, request, bot["id"])
            assert verdict.tier == "allow", (command, verdict.tier, verdict.rule)
    finally:
        turn_mod._slot.set(None)


def test_redirected_interpreter_file_cards(tmp_path):
    """``python3 < script.py`` is the same program as a here-string. A harmless file runs."""
    folder = tmp_path / "tree"
    folder.mkdir()
    _plant(folder)
    (folder / "leak.py").write_text('print(open(".env").read())\n', encoding="utf-8")
    (folder / "ok.py").write_text("print(1)\n", encoding="utf-8")
    (folder / "leak.js").write_text('console.log(require("fs").readFileSync(".env","utf8"))\n', encoding="utf-8")
    (folder / "ok.js").write_text("console.log(1)\n", encoding="utf-8")
    (folder / "leak.rb").write_text('puts File.read(".env")\n', encoding="utf-8")
    (folder / "ok.rb").write_text("puts 1\n", encoding="utf-8")
    (folder / "leak.pl").write_text('open F, ".env"; print <F>\n', encoding="utf-8")
    (folder / "ok.pl").write_text("print 1\n", encoding="utf-8")
    (folder / "leak.php").write_text('<?php echo file_get_contents(".env");\n', encoding="utf-8")
    (folder / "ok.php").write_text("<?php echo 1;\n", encoding="utf-8")
    (folder / "leak.sh").write_text("cat .env\n", encoding="utf-8")
    (folder / "ok.sh").write_text("echo hi\n", encoding="utf-8")
    (folder / "win.py").write_text('print(open(".\\\\.env").read())\n', encoding="utf-8")
    secret = (
        "python3 < leak.py",
        "python < leak.py",
        "python - < leak.py",
        "node < leak.js",
        "ruby < leak.rb",
        "perl < leak.pl",
        "php < leak.php",
        "sh < leak.sh",
        "bash < leak.sh",
        "bash -s < leak.sh",
        "python.exe < leak.py",
        "py -3 < leak.py",
        "node.exe < leak.js",
        "ruby.exe < leak.rb",
        "python3 < win.py",
        "python3 < missing.py",
        "sh < missing.sh",
        "python3 < \"$leak\"",
    )
    for command in secret:
        assert command_reads_secrets(command, folder), command
    harmless = (
        "python3 < ok.py",
        "python - < ok.py",
        "node < ok.js",
        "ruby < ok.rb",
        "perl < ok.pl",
        "php < ok.php",
        "sh < ok.sh",
        "bash < ok.sh",
        "python.exe < ok.py",
        "py -3 < ok.py",
        "node.exe < ok.js",
        "grep x < notes.txt",
        "python ok.py < leak.py",
    )
    for command in harmless:
        assert command_reads_secrets(command, folder) is False, command


def test_redirected_interpreter_file_stays_a_card_under_yolo(tmp_path):
    """``/yolo`` does not approve ``python3 < script.py`` when that file reads a secret."""
    from easyagent import turn as turn_mod
    from easyagent.safety import classify, set_mode
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace
    from easyagent.yolo import _enable, covers

    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    folder = bot_workspace(store, bot["id"], create=True)
    _plant(folder)
    (folder / "leak.py").write_text('print(open(".env").read())\n', encoding="utf-8")
    (folder / "ok.py").write_text("print(1)\n", encoding="utf-8")
    (folder / "leak.sh").write_text("cat .env\n", encoding="utf-8")
    chat = store.create_chat(bot["id"])
    _enable(store, bot["id"], chat["id"], seconds=3600, idle_seconds=3600)
    slot = turn_mod.bind(store, chat["id"], bot["id"])
    slot.cwd = str(folder)
    slot.store = store
    secret = (
        "python3 < leak.py",
        "python.exe < leak.py",
        "node < leak.py",
        "sh < leak.sh",
        "bash < leak.sh",
    )
    try:
        for mode in ("careful", "strict"):
            set_mode(store, bot["id"], mode)
            for command in secret:
                request = ToolRequest(kind="shell", command=command)
                assert command_reads_secrets(command, folder), (mode, command)
                verdict = classify(store, request, bot["id"])
                assert verdict.tier == "ask", (mode, command, verdict.tier, verdict.rule)
                assert covers(store, bot["id"], request, verdict) is False, (mode, command)
            request = ToolRequest(kind="shell", command="python3 < ok.py")
            assert command_reads_secrets("python3 < ok.py", folder) is False
            if mode == "careful":
                verdict = classify(store, request, bot["id"])
                assert verdict.tier == "allow", (mode, verdict.tier, verdict.rule)
    finally:
        turn_mod._slot.set(None)


def test_redirected_file_still_cards_beside_other_redirections(tmp_path):
    """``2>&1``, ``2>/dev/null``, and ``>out`` do not hide ``< script.py``."""
    from easyagent import turn as turn_mod
    from easyagent.safety import classify, set_mode
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace
    from easyagent.yolo import _enable, covers

    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    folder = bot_workspace(store, bot["id"], create=True)
    _plant(folder)
    (folder / "leak.py").write_text('print(open(".env").read())\n', encoding="utf-8")
    (folder / "ok.py").write_text("print(1)\n", encoding="utf-8")
    (folder / "leak.js").write_text('console.log(require("fs").readFileSync(".env","utf8"))\n', encoding="utf-8")
    (folder / "ok.js").write_text("console.log(1)\n", encoding="utf-8")
    (folder / "leak.rb").write_text('puts File.read(".env")\n', encoding="utf-8")
    (folder / "ok.rb").write_text("puts 1\n", encoding="utf-8")
    (folder / "leak.pl").write_text('open F, ".env"; print <F>\n', encoding="utf-8")
    (folder / "ok.pl").write_text("print 1\n", encoding="utf-8")
    (folder / "leak.php").write_text('<?php echo file_get_contents(".env");\n', encoding="utf-8")
    (folder / "ok.php").write_text("<?php echo 1;\n", encoding="utf-8")
    (folder / "leak.sh").write_text("cat .env\n", encoding="utf-8")
    (folder / "ok.sh").write_text("echo hi\n", encoding="utf-8")
    secret = (
        "python3 < leak.py 2>&1",
        "python3 < leak.py 2>/dev/null",
        "python3 < leak.py >out",
        "python < leak.py 2>&1",
        "node < leak.js 2>&1",
        "node < leak.js 2>/dev/null",
        "ruby < leak.rb 2>&1",
        "perl < leak.pl 2>&1",
        "php < leak.php 2>&1",
        "sh < leak.sh 2>&1",
        "bash < leak.sh 2>/dev/null",
        "dash < leak.sh >out",
        "bash -s < leak.sh 2>&1",
    )
    harmless = (
        "python3 < ok.py 2>&1",
        "node < ok.js 2>/dev/null",
        "ruby < ok.rb >out",
        "perl < ok.pl 2>&1",
        "php < ok.php 2>&1",
        "sh < ok.sh 2>&1",
        "bash < ok.sh 2>/dev/null",
        "dash < ok.sh >out",
        "grep x < notes.txt 2>&1",
    )
    for command in secret:
        assert command_reads_secrets(command, folder), command
    for command in harmless:
        assert command_reads_secrets(command, folder) is False, command
    chat = store.create_chat(bot["id"])
    _enable(store, bot["id"], chat["id"], seconds=3600, idle_seconds=3600)
    slot = turn_mod.bind(store, chat["id"], bot["id"])
    slot.cwd = str(folder)
    slot.store = store
    try:
        for mode in ("careful", "strict"):
            set_mode(store, bot["id"], mode)
            for command in ("python3 < leak.py 2>&1", "sh < leak.sh 2>/dev/null", "dash < leak.sh >out"):
                request = ToolRequest(kind="shell", command=command)
                verdict = classify(store, request, bot["id"])
                assert verdict.tier == "ask", (mode, command, verdict.tier, verdict.rule)
                assert covers(store, bot["id"], request, verdict) is False, (mode, command)
            if mode == "careful":
                request = ToolRequest(kind="shell", command="python3 < ok.py 2>&1")
                verdict = classify(store, request, bot["id"])
                assert verdict.tier == "allow", (mode, verdict.tier, verdict.rule)
    finally:
        turn_mod._slot.set(None)


def test_shell_clusters_ampersand_env_split_and_globs_card(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    _plant(folder)
    for command in SHELL_CLUSTER_LEAKS:
        assert command_reads_secrets(command, folder), command
    for command in (
        "cat notes.txt",
        "cat .envrc",
        "eval echo hi",
        "echo a & echo b",
        "awk '{print $1}' notes.txt",
        "cd sub && cat ok.txt",
        "grep -e . notes.txt",
    ):
        assert command_reads_secrets(command, folder) is False, command


def test_env_templates_are_not_secret_by_name_or_content(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    token = "API_KEY=" + ("a" * 40) + "\n"
    key = "-----BEGIN PRIVATE KEY-----\nABC\n"
    for name in (".env.example", ".env.sample", ".env.template", ".env.dist", "example.env", "sample.env"):
        path = folder / name
        path.write_text("API_KEY=your_key_here\nCOLOR=blue\n", encoding="utf-8")
        assert secret_class(path) is None, name
        path.write_text(token + key, encoding="utf-8")
        assert secret_class(path) == "content", name
    for name in (".env", ".env.local", ".env.production", ".env.development", ".env.test"):
        path = folder / name
        path.write_text("SECRETENV=1\n", encoding="utf-8")
        assert secret_class(path) == "name", name
    envrc = folder / ".envrc"
    envrc.write_text("export PATH\n", encoding="utf-8")
    assert secret_class(envrc) is None


def test_string_limit_offset_and_timeout_are_integers(tmp_path):
    from easyagent.coding import read_window

    path = tmp_path / "long.txt"
    path.write_text("".join(f"row {number}\n" for number in range(1, 401)), encoding="utf-8")
    text, partial = read_window(path, offset="1", limit="200", task_id="string-limit")
    assert partial is True
    assert "row 200" in text
    assert "row 201" not in text
    read = _request_from_call({"name": "read_file", "arguments": {"path": "long.txt", "offset": "2", "limit": "200"}})
    assert read.offset == 2 and read.limit == 200
    quoted = _request_from_call({"name": "read_file", "arguments": {"path": "long.txt", "limit": '"200"'}})
    assert quoted.limit == 200
    shell = _request_from_call({"name": "terminal", "arguments": {"command": "pwd", "timeout": "200"}})
    assert shell.timeout == 200


def _dev_tree(folder: Path) -> None:
    (folder / "src").mkdir()
    (folder / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n# TODO\n", encoding="utf-8")
    (folder / "src" / "util.py").write_text("x = 1\n", encoding="utf-8")
    (folder / "tests").mkdir()
    (folder / "tests" / "test_app.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "Makefile").write_text("all:\n\ttrue\n", encoding="utf-8")
    (folder / "notes.txt").write_text("hello\n", encoding="utf-8")
    (folder / "README.md").write_text("# hi\n", encoding="utf-8")
    (folder / "package.json").write_text('{"name":"x","scripts":{"test":"true"}}\n', encoding="utf-8")
    (folder / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    (folder / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    (folder / ".gitignore").write_text("build\n", encoding="utf-8")
    (folder / "data.csv").write_text("1,2\n", encoding="utf-8")
    (folder / "script.py").write_text("print(1)\n", encoding="utf-8")
    (folder / "run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    os.chmod(folder / "run.sh", 0o755)
    (folder / ".env.example").write_text("COLOR=blue\n", encoding="utf-8")
    (folder / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    subprocess.check_call(["git", "init", "-q"], cwd=folder)
    subprocess.check_call(["git", "config", "user.email", "a@b.c"], cwd=folder)
    subprocess.check_call(["git", "config", "user.name", "A"], cwd=folder)
    subprocess.check_call(["git", "add", "-A"], cwd=folder)
    subprocess.check_call(["git", "commit", "-qm", "init"], cwd=folder)


def test_missing_pwd_keeps_an_ordinary_read_and_still_cards_a_secret(tmp_path, monkeypatch):
    """Windows does not set PWD. ``echo $PWD`` stays allowed. ``cat $PWD/.env`` still waits."""
    from easyagent import turn as turn_mod
    from easyagent.safety import classify, set_mode
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    monkeypatch.delenv("PWD", raising=False)
    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    set_mode(store, bot["id"], "strict")
    folder = bot_workspace(store, bot["id"], create=True)
    _dev_tree(folder)
    (folder / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    chat = store.create_chat(bot["id"])
    slot = turn_mod.bind(store, chat["id"], bot["id"])
    slot.cwd = str(folder)
    slot.store = store
    try:
        for command in ("echo $PWD", "cat $PWD/src/app.py"):
            verdict = classify(store, ToolRequest(kind="shell", command=command), bot["id"])
            assert verdict.tier == "allow", (command, verdict.tier, verdict.rule)
        secret = classify(store, ToolRequest(kind="shell", command="cat $PWD/.env"), bot["id"])
        assert secret.tier == "ask" and secret.rule == "read-secret", (secret.tier, secret.rule)
        assert command_reads_secrets("cat $PWD/.env", folder)
    finally:
        turn_mod._slot.set(None)


def test_ordinary_dev_commands_card_only_the_unresolved_ones(tmp_path):
    from easyagent import turn as turn_mod
    from easyagent.safety import classify, set_mode
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace
    from easyagent.yolo import _enable, covers

    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    set_mode(store, bot["id"], "strict")
    folder = bot_workspace(store, bot["id"], create=True)
    _dev_tree(folder)
    chat = store.create_chat(bot["id"])
    _enable(store, bot["id"], chat["id"], seconds=3600, idle_seconds=3600)
    slot = turn_mod.bind(store, chat["id"], bot["id"])
    slot.cwd = str(folder)
    slot.store = store
    normal = {}
    yolo = set()
    try:
        for command in DEV_COMMANDS:
            request = ToolRequest(kind="shell", command=command)
            verdict = classify(store, request, bot["id"])
            if verdict.tier == "allow":
                continue
            normal[command] = verdict.rule
            if not covers(store, bot["id"], request, verdict):
                yolo.add(command)
    finally:
        turn_mod._slot.set(None)
    assert normal == DEV_CARDS
    assert yolo == set(DEV_YOLO_CARDS)
    assert len(DEV_COMMANDS) == 188
    assert len(normal) == 41
    assert len(yolo) == 26


@pytest.mark.parametrize("yolo", [False, True])
def test_live_clusters_ampersand_env_split_and_glob_do_not_print(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    commands = [
        "sh -ec 'cat .npmrc'",
        "bash -xc 'cat creds.json'",
        "bash -elc 'cat bland.txt'",
        "echo a & cat .npmrc",
        "echo a & echo b & cat bland.txt",
        "env -S 'cat .npmrc'",
        "env -S 'sort -n creds.json'",
        "ls .e* | xargs cat",
        "cat notes.txt",
        "cat .envrc",
    ]
    heard, stored, body, _folder = _live(
        tmp_path,
        [("terminal", {"command": command}) for command in commands],
        yolo=yolo,
        ask="Read the project notes.",
    )
    blob = heard + "\n" + body + "\n" + json.dumps(stored.get("messages") or [])
    for marker in ("_authToken=", "hunter2PASS", "BEGIN PRIVATE KEY", "KEYBODYXYZ", "SECRETENV="):
        assert marker not in blob, (yolo, marker)
    assert "was not run" in heard
    assert "visible-note" in heard
    assert "export PATH" in heard


def test_live_ordinary_sed_loop_copy_and_template_run(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    from easyagent.safety import set_mode
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    set_mode(Store(tmp_path), bot["id"], "strict")
    _dev_tree(folder)
    lines = "".join(f"row {number}\n" for number in range(1, 401))
    (folder / "long.txt").write_text(lines, encoding="utf-8")
    commands = [
        "sed -n '1p' src/app.py",
        "for i in 1 2 3; do echo $i; done",
        "F=src/app.py; cat $F",
        "mkdir -p build",
        "touch build/x",
        "cp README.md build/README.md",
        "timeout 10 python3 script.py",
        "cat .env.example",
        "git diff",
        "python3 -m pytest -x -q 2>&1 | tail -3",
    ]
    Handler.scripts = [_tool_sse("terminal", {"command": command}, f"c{index}") for index, command in enumerate(commands)]
    Handler.scripts.append(_tool_sse("read_file", {"path": "long.txt", "offset": "1", "limit": "200"}, "window"))
    Handler.scripts.append([
        'data: {"choices":[{"delta":{"content":"The distinctive summary sentence is kept with the file."}}]}',
        "data: [DONE]",
    ])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        with client.stream("POST", f"{url}/messages", json={"content": "Check the project."}, headers={"Accept": "text/event-stream"}) as response:
            assert response.status_code == 200, response.read()
            response.read()
        heard = _tool_text(Handler.payloads)
        assert "was not run" not in heard, heard
        assert "def add" in heard
        assert "COLOR=blue" in heard
        assert "row 200" in heard
        assert "row 201" not in heard
    finally:
        client.close()
        server.shutdown()


_SECRET_MARKERS = ("_authToken=", "hunter2PASS", "BEGIN PRIVATE KEY", "KEYBODYXYZ", "SECRETENV=", "ghp_", "sk_live_")

_TEMPLATE_BODIES = {
    ".env.example": "-----BEGIN PRIVATE KEY-----\nABC\n",
    ".env.sample": "GITHUB_TOKEN=ghp_" + ("a" * 20) + "\n",
    ".env.template": "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n",
    "example.env": "STRIPE=sk_live_" + ("b" * 20) + "\n",
    ".env.example.local": "GITHUB_TOKEN=ghp_" + ("c" * 20) + "\n",
    ".env.example.production": "-----BEGIN PRIVATE KEY-----\nABC\n",
    ".env.local.example": "GITHUB_TOKEN=ghp_" + ("d" * 20) + "\n",
    ".env.example.pem": "-----BEGIN PRIVATE KEY-----\nABC\n",
    ".env.example.key": "-----BEGIN PRIVATE KEY-----\nABC\n",
    ".env.example.bak": "GITHUB_TOKEN=ghp_" + ("e" * 20) + "\n",
    ".env.example.swp": "GITHUB_TOKEN=ghp_" + ("f" * 20) + "\n",
    ".env.example.json": "GITHUB_TOKEN=ghp_" + ("g" * 20) + "\n",
    ".env.sample.local": "GITHUB_TOKEN=ghp_" + ("h" * 20) + "\n",
    ".env.dist.local": "GITHUB_TOKEN=ghp_" + ("i" * 20) + "\n",
    ".env.default.local": "GITHUB_TOKEN=ghp_" + ("j" * 20) + "\n",
    ".ENV.EXAMPLE": "GITHUB_TOKEN=ghp_" + ("k" * 20) + "\n",
    ".env.example.": "-----BEGIN PRIVATE KEY-----\nABC\n",
}


def _write_template_secrets(folder: Path) -> None:
    for name, body in _TEMPLATE_BODIES.items():
        (folder / name).write_text(body, encoding="utf-8")
    nested = folder / "sd"
    nested.mkdir(exist_ok=True)
    if os.name != "nt":
        link = nested / ".env.example"
        if not link.exists():
            link.symlink_to(folder / ".env")


def test_template_names_need_a_clean_sniff_and_follow_links(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    _plant(folder)
    placeholder = folder / ".env.example"
    placeholder.write_text("API_KEY=your_key_here\nCOLOR=blue\n", encoding="utf-8")
    assert secret_class(placeholder) is None
    for name in (".env.examples", ".env.example~", ".env.exampl"):
        path = folder / name
        path.write_text("COLOR=blue\n", encoding="utf-8")
        assert secret_class(path) == "name", name
    _write_template_secrets(folder)
    for name in _TEMPLATE_BODIES:
        reason = secret_class(folder / name)
        assert reason in {"name", "content"}, (name, reason)
    if os.name != "nt":
        assert secret_class(folder / "sd" / ".env.example") == "symlink"
        assert secret_class(folder / "hard") == "hardlink"
    okex = folder / "okex.txt"
    okex.write_text("hello\n", encoding="utf-8")
    assert secret_class(okex) is None


def test_revision_grep_cards_and_stat_commit_and_literal_reads_do_not(tmp_path):
    from easyagent.safety import classify, set_mode
    from easyagent.store import Store
    from easyagent import turn as turn_mod
    from easyagent.workspace import bot_workspace

    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    set_mode(store, bot["id"], "strict")
    folder = bot_workspace(store, bot["id"], create=True)
    _plant(folder)
    (folder / "x.txt").write_text("plain\n", encoding="utf-8")
    (folder / "ok.py").write_text("print(open('notes.txt').read())\n", encoding="utf-8")
    _exec_setup(folder)
    notes = folder / "notes.txt"
    notes.write_text(notes.read_text(encoding="utf-8") + "second-line\n", encoding="utf-8")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Ada"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "ada@example.com"
    subprocess.run(["git", "add", "notes.txt"], cwd=folder, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=folder, check=True, capture_output=True, env=env)
    slot = turn_mod.bind(store, "rev", bot["id"])
    slot.cwd = str(folder)
    try:
        leaks = [
            "git grep hunter2 HEAD",
            "git grep hunter2 HEAD~1",
            "cat /proc/thread-self/cwd/bland.txt",
            "cd /proc/self/cwd/sub && cat ../bland.txt",
            'cd sub && cat "$PWD/../bland.txt"',
            "cd sub && cat $OLDPWD/bland.txt",
            "node -e \"console.log(require('child_process').execSync('cat bland.txt').toString())\"",
            "awk -v f=bland.txt 'BEGIN{while((getline l < f)>0) print l}'",
            "awk 'BEGIN{ARGV[1]=\"bland.txt\"} 1' x.txt",
            "echo 'BEGIN{system(\"cat bland.txt\")}' > p.awk; awk -f p.awk",
            "echo 'BEGIN{system(\"cat bland.txt\")}' | awk -f -",
            "echo \"print(open('bland.txt').read())\" > rd.py && python3 rd.py",
            "printf 'print(open(\"bland.txt\").read())' > rd2.py; python3 rd2.py",
            "python3 - <<'EOF'\nprint(open('bland.txt').read())\nEOF",
            "cat > rd3.py <<'EOF'\nprint(open('bland.txt').read())\nEOF\npython3 rd3.py",
            "bun -e \"console.log(require('fs').readFileSync('bland.txt','utf8'))\"",
            "head -c 200 bland.txt",
            "grep foo bland.txt",
            "grep -rn .",
        ]
        for command in leaks:
            assert command_reads_secrets(command, folder), command
        allows = [
            "cat notes.txt",
            "head notes.txt",
            "tail notes.txt",
            "wc -l notes.txt",
            "grep hunter2 notes.txt",
            "sed -n '1p' notes.txt",
            "sort notes.txt",
            "awk '{print $1}' notes.txt",
            "python3 -m pytest",
            "python3 ok.py",
            "grep -rn TODO --include='*.py' .",
            "git diff -- notes.txt",
            "git diff --stat",
            "git show --stat",
            "git diff",
            "git diff | head",
            "git diff HEAD",
            "git diff HEAD~1",
            "git diff HEAD~1 HEAD",
        ]
        for command in allows:
            assert command_reads_secrets(command, folder) is False, command
        for command in ("git diff --stat", "git show --stat", "git commit -qam wip", "git commit -m 'wip'"):
            verdict = classify(store, ToolRequest(kind="shell", command=command), bot["id"])
            assert verdict.tier == "allow", (command, verdict.tier, verdict.rule)
        for command in ("git grep hunter2 HEAD", "git grep hunter2 HEAD~1"):
            verdict = classify(store, ToolRequest(kind="shell", command=command), bot["id"])
            assert verdict.tier == "ask" and verdict.rule == "read-secret", (command, verdict.tier, verdict.rule)
        asked = classify(store, ToolRequest(kind="shell", command="git commit -qam '$(cat .env)'"), bot["id"])
        assert asked.tier == "ask", asked
    finally:
        turn_mod._slot.set(None)


def test_planted_secrets_keep_the_same_dev_cards(tmp_path):
    """Literal reads stay allowed. The 188-list cards do not grow when a secret is in the tree."""
    from easyagent import turn as turn_mod
    from easyagent.safety import classify, set_mode
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace
    from easyagent.yolo import _enable, covers

    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    set_mode(store, bot["id"], "strict")
    folder = bot_workspace(store, bot["id"], create=True)
    _dev_tree(folder)
    (folder / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    (folder / ".npmrc").write_text("_authToken=npm_secret\n", encoding="utf-8")
    (folder / "creds.json").write_text('{"password":"hunter2PASS"}\n', encoding="utf-8")
    (folder / "bland.txt").write_text("-----BEGIN PRIVATE KEY-----\nABC\n", encoding="utf-8")
    chat = store.create_chat(bot["id"])
    _enable(store, bot["id"], chat["id"], seconds=3600, idle_seconds=3600)
    slot = turn_mod.bind(store, chat["id"], bot["id"])
    slot.cwd = str(folder)
    slot.store = store
    normal = {}
    yolo = set()
    try:
        for command in DEV_COMMANDS:
            request = ToolRequest(kind="shell", command=command)
            verdict = classify(store, request, bot["id"])
            if verdict.tier == "allow":
                continue
            normal[command] = verdict.rule
            if not covers(store, bot["id"], request, verdict):
                yolo.add(command)
        for command in (
            "cat notes.txt",
            "head -n 5 notes.txt",
            "tail notes.txt",
            "wc -l notes.txt",
            "grep hunter2 notes.txt",
            "sed -n '1p' notes.txt",
            "sort notes.txt",
            "awk '{print $1}' notes.txt",
            "git diff -- notes.txt",
            "git diff --stat",
            "grep -rn 'TODO' --include='*.py' .",
        ):
            verdict = classify(store, ToolRequest(kind="shell", command=command), bot["id"])
            assert verdict.tier == "allow", (command, verdict.tier, verdict.rule)
        named = classify(store, ToolRequest(kind="shell", command="grep foo bland.txt"), bot["id"])
        assert named.tier == "ask" and named.rule == "read-secret"
    finally:
        turn_mod._slot.set(None)
    planted_extra = {
        "git grep TODO": "read-secret",
    }
    assert normal == {**DEV_CARDS, **planted_extra}
    assert yolo == set(DEV_YOLO_CARDS) | set(planted_extra)
    assert len(normal) == 42
    assert len(yolo) == 27


def _assert_quiet(blob: str, yolo: bool) -> None:
    for marker in _SECRET_MARKERS:
        assert marker not in blob, (yolo, marker)


@pytest.mark.parametrize("yolo", [False, True])
def test_live_template_secret_content_does_not_print(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "0.25")
    commands = [f"cat {name}" for name in _TEMPLATE_BODIES]
    commands.extend([
        "head -c 200 .env.example",
        "cp .env.example copy.txt && cat copy.txt",
        "tar cf - .env.example | tar xO",
        "cat notes.txt",
    ])
    calls = [("terminal", {"command": command}) for command in commands]
    if os.name != "nt":
        calls.append(("read_file", {"path": "sd/.env.example"}))
    heard, stored, body, _folder = _live(
        tmp_path,
        calls,
        yolo=yolo,
        ask="Read the env template.",
        setup=_write_template_secrets,
    )
    blob = heard + "\n" + body + "\n" + json.dumps(stored.get("messages") or [])
    _assert_quiet(blob, yolo)
    rows = _tool_rows(Handler.payloads)
    assert len(rows) >= len(calls)
    for command, row in zip(commands, rows):
        if command == "cat notes.txt":
            assert "visible-note" in row and "was not run" not in row
        else:
            assert "was not run" in row, (yolo, command, row)
    if os.name != "nt":
        assert "was not run" in rows[len(commands)]


def test_live_placeholder_template_reads_without_a_card(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "0.25")

    def setup(folder: Path) -> None:
        (folder / ".env.example").write_text("API_KEY=your_key_here\nCOLOR=blue\n", encoding="utf-8")
        (folder / "okex.txt").write_text("okex-line\n", encoding="utf-8")

    heard, _stored, _body, _folder = _live(
        tmp_path,
        [
            ("terminal", {"command": "cat .env.example"}),
            ("read_file", {"path": ".env.example"}),
            ("terminal", {"command": "cat okex.txt"}),
        ],
        yolo=False,
        ask="Read the placeholder template.",
        setup=setup,
    )
    assert "COLOR=blue" in heard
    assert "okex-line" in heard
    assert "was not run" not in heard


@pytest.mark.parametrize("yolo", [False, True])
def test_live_proc_pwd_git_revision_and_scripts_do_not_print(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "0.25")
    files = ("bland.txt", ".npmrc", "creds.json")
    leaks = [
        "cat /proc/thread-self/cwd/bland.txt",
        "cd /proc/self/cwd/sub && cat ../bland.txt",
        'cd sub && cat "$PWD/../bland.txt"',
        "cd sub && cat $OLDPWD/bland.txt",
    ]
    for name in files:
        leaks.append(f"node -e \"console.log(require('child_process').execSync('cat {name}').toString())\"")
        leaks.append(f"awk -v f={name} 'BEGIN{{while((getline l < f)>0) print l}}'")
        leaks.append(f"awk 'BEGIN{{ARGV[1]=\"{name}\"}} 1' x.txt")
        leaks.append(f"echo 'BEGIN{{system(\"cat {name}\")}}' > p.awk; awk -f p.awk")
        leaks.append(f"echo 'BEGIN{{system(\"cat {name}\")}}' | awk -f -")
        leaks.append(f"echo \"print(open('{name}').read())\" > rd.py && python3 rd.py")
        leaks.append(f"bun -e \"console.log(require('fs').readFileSync('{name}','utf8'))\"")
    leaks.extend([
        "printf 'print(open(\"bland.txt\").read())' > rd2.py; python3 rd2.py",
        "python3 - <<'EOF'\nprint(open('bland.txt').read())\nEOF",
        "cat > rd3.py <<'EOF'\nprint(open('bland.txt').read())\nEOF\npython3 rd3.py",
        "git grep hunter2 HEAD",
        "git grep hunter2 HEAD~1",
    ])
    controls = [
        "cat notes.txt",
        "head notes.txt",
        "tail notes.txt",
        "wc -l notes.txt",
        "grep hunter2 notes.txt",
        "sed -n '1p' notes.txt",
        "sort notes.txt",
        "awk '{print $1}' notes.txt",
        "grep -n visible-note notes.txt",
        "git diff --stat",
        "git show --stat",
        "git commit -qam wip",
        "git diff -- notes.txt",
        "git diff",
        "git diff HEAD",
    ]
    leaks.append("git diff feat")

    def setup(folder: Path) -> None:
        (folder / "x.txt").write_text("plain\n", encoding="utf-8")
        (folder / "sub").mkdir(exist_ok=True)
        _exec_setup(folder)
        notes = folder / "notes.txt"
        notes.write_text("visible-note hunter2\nsecond-line\n", encoding="utf-8")
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Ada"
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "ada@example.com"
        subprocess.run(["git", "add", "notes.txt", "x.txt"], cwd=folder, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "second"], cwd=folder, check=True, capture_output=True, env=env)
        notes.write_text("visible-note hunter2\nsecond-line\nstill-editing\n", encoding="utf-8")

    calls = [("terminal", {"command": command}) for command in controls + leaks]
    heard, stored, body, _folder = _live(
        tmp_path,
        calls,
        yolo=yolo,
        ask="Read the notes and search the tree.",
        setup=setup,
    )
    blob = heard + "\n" + body + "\n" + json.dumps(stored.get("messages") or [])
    _assert_quiet(blob, yolo)
    rows = _tool_rows(Handler.payloads)
    assert len(rows) >= len(calls), (yolo, len(rows), len(calls))
    for command, row in zip(controls, rows):
        assert "was not run" not in row, (yolo, command, row)
    assert "visible-note" in rows[0]
    assert "Syntax error" not in rows[8]
    assert "was not run" not in rows[9]
    for command, row in zip(leaks, rows[len(controls):]):
        assert "was not run" in row, (yolo, command, row)


def test_template_bytes_past_the_head_and_padded_names_are_secret(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    placeholder = folder / ".env.example"
    placeholder.write_text("API_KEY=your_key_here\nCOLOR=blue\n", encoding="utf-8")
    assert secret_class(placeholder) is None
    plain = folder / ".env.default"
    plain.write_text("DB_PASSWORD=hunter2realpass\nCOLOR=blue\n", encoding="utf-8")
    assert secret_class(plain) == "content"
    short = folder / ".env.dist"
    short.write_text("PASSWORD=hunter2\nCOLOR=blue\nPORT=3000\n", encoding="utf-8")
    assert secret_class(short) == "content"
    buried = folder / "big"
    buried.mkdir()
    body = "API_KEY=your_key_here\n" + ("# filler\n" * 1500) + "-----BEGIN OPENSSH PRIVATE KEY-----\nABC\n"
    (buried / ".env.example").write_text(body, encoding="utf-8")
    assert secret_class(buried / ".env.example") == "content"
    huge = folder / ".env.sample"
    huge.write_bytes(b"COLOR=blue\n" + (b"#" * 1_300_000) + b"\n-----BEGIN PRIVATE KEY-----\nABC\n")
    assert secret_class(huge) == "content"
    over = folder / ".env.template"
    over.write_bytes(b"COLOR=blue\n" + (b"x" * 16_000_001))
    assert secret_class(over) == "content"
    mid = folder / "mid"
    mid.mkdir()
    (mid / ".env.example").write_bytes(b"COLOR=blue\n" + (b"#" * 3_000_000) + b"\nDB_PASSWORD=hunter2\n")
    assert secret_class(mid / ".env.example") == "content"
    entropy = folder / ".env.dist"
    entropy.write_text("SECRET_KEY=Zk9vQ2mN8pL4sT6w\n", encoding="utf-8")
    assert secret_class(entropy) == "content"
    local = folder / "env.example.local"
    local.write_text("COLOR=blue\n", encoding="utf-8")
    assert secret_class(local) == "name"
    ssh = folder / ".ssh"
    ssh.mkdir()
    nested = ssh / ".env.example"
    nested.write_text("COLOR=blue\n", encoding="utf-8")
    assert secret_class(nested) == "name"
    for name in (" .env.example", "\t.env.example", "\n.env.example"):
        path = folder / name
        try:
            path.write_text("API_KEY=your_key_here\nCOLOR=blue\n", encoding="utf-8")
        except OSError as exc:
            # Windows rejects a trailing space, a tab, or a newline in a file name.
            assert exc.errno in {22, 123}, exc
        assert secret_class(path) == "name", repr(name)
    if os.name != "nt":
        real = folder / ".env"
        real.write_text("SECRETENV=1\n", encoding="utf-8")
        placeholder.unlink()
        os.link(real, placeholder)
        assert secret_class(placeholder) == "hardlink"


def test_reader_allow_list_cards_the_extra_leak_classes(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    _plant(folder)
    (folder / "run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (folder / "r.js").write_text("console.log(require('fs').readFileSync('bland.txt','utf8'))\n", encoding="utf-8")
    (folder / "r.mjs").write_text("import {readFileSync} from 'fs'; console.log(readFileSync('bland.txt','utf8'))\n", encoding="utf-8")
    (folder / "r.ts").write_text("console.log(require('fs').readFileSync('bland.txt','utf8'))\n", encoding="utf-8")
    (folder / "r.rb").write_text("puts File.read('bland.txt')\n", encoding="utf-8")
    (folder / "r.pl").write_text("open(F, 'bland.txt'); print <F>\n", encoding="utf-8")
    (folder / "r.php").write_text("<?php echo file_get_contents('bland.txt');\n", encoding="utf-8")
    (folder / "r.lua").write_text("print(io.open('bland.txt'):read('*a'))\n", encoding="utf-8")
    (folder / "ok.py").write_text("print(1)\n", encoding="utf-8")
    leaks = [
        "sh -ce 'echo hi'",
        "sh -cx 'cat bland.txt'",
        "sh -ec 'cat bland.txt'",
        "bash -lc 'cat bland.txt'",
        "bash -xec 'cat bland.txt'",
        "sh -x -c 'echo hi'",
        "bash -o errexit -c 'echo hi'",
        "env -C sub cat ../bland.txt",
        "env --chdir=sub cat ../bland.txt",
        "env --chdir=sub echo hi",
        "env -S 'cat bland.txt'",
        "env -S 'cat\\_bland.txt'",
        "env --split-string='cat bland.txt'",
        "sed 'r bland.txt' notes.txt",
        "sed -n 'w /tmp/ea-copy.txt' notes.txt",
        "sed 's/a/b/e' notes.txt",
        "sed e notes.txt",
        "find . -exec cat {} +",
        "xargs cat",
        "node r.js",
        "node r.mjs",
        "bun r.ts",
        "deno run r.ts",
        "ruby r.rb",
        "perl r.pl",
        "php r.php",
        "lua r.lua",
        "git grep hunter2 master",
        "git grep hunter2 feat",
        "git grep hunter2 v1",
        "git grep hunter2 v2",
        "git grep hunter2 'stash@{0}'",
        "git grep hunter2 refs/stash",
        "git grep hunter2 refs/heads/feat",
        "git grep hunter2 refs/tags/v1",
        "git grep -i KEYBODY feat",
        "git show refs/stash",
        "git diff",
        "git diff HEAD",
        "git diff HEAD~1",
        "git diff HEAD~1 HEAD",
        "git diff feat",
        "git diff 'stash@{0}'",
        "cat ' .env.example'",
    ]
    for command in leaks:
        assert command_reads_secrets(command, folder), command
    for command in (
        "cat notes.txt",
        "ls",
        "sed -n '1p' notes.txt",
        "sed 's/add/plus/' notes.txt",
        "find . -name '*.py'",
        "sh run.sh",
        "python3 ok.py",
        "python3 -c 'print(1+1)'",
        "git diff -- notes.txt",
        "git diff --stat",
        "git status",
        "grep -n visible notes.txt",
    ):
        assert command_reads_secrets(command, folder) is False, command


def test_a_wide_tree_classifies_ordinary_commands_quickly(tmp_path):
    """60,000 files. ls, cat of one note, and a recursive grep each classify in under a second."""
    import time

    from easyagent import turn as turn_mod
    from easyagent.safety import classify, set_mode
    from easyagent.store import Store

    folder = tmp_path / "wide"
    folder.mkdir()
    for bucket in range(60):
        dest = folder / f"b{bucket:02d}"
        dest.mkdir()
        for number in range(1000):
            fd = os.open(dest / f"f{number:04d}.txt", os.O_CREAT | os.O_WRONLY, 0o644)
            os.write(fd, b"x\n")
            os.close(fd)
    (folder / "notes.txt").write_text("hello\n", encoding="utf-8")
    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    set_mode(store, bot["id"], "strict")
    slot = turn_mod.bind(store, "wide", bot["id"])
    slot.cwd = str(folder)
    try:
        for command in ("ls", "cat notes.txt", "grep -rn foo ."):
            started = time.perf_counter()
            verdict = classify(store, ToolRequest(kind="shell", command=command), bot["id"])
            elapsed = time.perf_counter() - started
            assert elapsed < 1.0, (command, elapsed, verdict.tier, verdict.rule)
        assert classify(store, ToolRequest(kind="shell", command="ls"), bot["id"]).tier == "allow"
        assert classify(store, ToolRequest(kind="shell", command="cat notes.txt"), bot["id"]).tier == "allow"
        assert classify(store, ToolRequest(kind="shell", command="grep -rn foo ."), bot["id"]).tier == "ask"
    finally:
        turn_mod._slot.set(None)


@pytest.mark.parametrize("yolo", [False, True])
def test_live_allow_list_leaks_do_not_print(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "0.25")
    leaks = [
        "sh -ce 'cat bland.txt'",
        "bash -ec 'cat bland.txt'",
        "bash -lc 'cat .npmrc'",
        "bash -xec 'cat creds.json'",
        "bash -o errexit -c 'cat bland.txt'",
        "sh -x -c 'cat bland.txt'",
        "env -C sub cat ../bland.txt",
        "env --chdir=sub cat ../bland.txt",
        "env -S 'cat bland.txt'",
        "env -S 'cat\\_bland.txt'",
        "sed 'r bland.txt' notes.txt",
        "sed -n 'w /tmp/ea-w.txt' notes.txt",
        "sed 's/a/b/e' notes.txt",
        "node r.js",
        "node r.mjs",
        "bun r.ts",
        "deno run r.ts",
        "ruby r.rb",
        "perl r.pl",
        "php r.php",
        "lua r.lua",
        "git grep hunter2 feat",
        "git grep -i KEYBODY feat",
        "git show refs/stash",
        "cat ' .env.example'",
    ]
    controls = [
        "cat notes.txt",
        "sed -n '1p' notes.txt",
        "git status",
        "git diff --stat",
        "git diff -- notes.txt",
        "find . -name '*.py'",
        "git diff",
        "git diff HEAD",
        "git diff feat",
        "git diff 'stash@{0}'",
    ]

    def setup(folder: Path) -> None:
        (folder / "sub").mkdir(exist_ok=True)
        (folder / " .env.example").write_text("COLOR=blue\n", encoding="utf-8")
        scripts = {
            "r.js": "console.log(require('fs').readFileSync('bland.txt','utf8'))\n",
            "r.mjs": "import {readFileSync} from 'fs'; console.log(readFileSync('bland.txt','utf8'))\n",
            "r.ts": "console.log(require('fs').readFileSync('bland.txt','utf8'))\n",
            "r.rb": "puts File.read('bland.txt')\n",
            "r.pl": "open(F, 'bland.txt'); print <F>\n",
            "r.php": "<?php echo file_get_contents('bland.txt');\n",
            "r.lua": "print(io.open('bland.txt'):read('*a'))\n",
        }
        for name, body in scripts.items():
            (folder / name).write_text(body, encoding="utf-8")
        _exec_setup(folder)
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Ada"
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "ada@example.com"
        subprocess.run(["git", "branch", "feat"], cwd=folder, check=True, capture_output=True)
        subprocess.run(["git", "tag", "v1"], cwd=folder, check=True, capture_output=True)
        notes = folder / "notes.txt"
        notes.write_text("visible-note\nstaged\n", encoding="utf-8")
        subprocess.run(["git", "stash", "push", "-m", "hold", "--", "notes.txt"], cwd=folder, check=False, capture_output=True, env=env)

    calls = [("terminal", {"command": command}) for command in controls + leaks]
    heard, stored, body, _folder = _live(
        tmp_path,
        calls,
        yolo=yolo,
        ask="Read the project.",
        setup=setup,
    )
    blob = heard + "\n" + body + "\n" + json.dumps(stored.get("messages") or [])
    _assert_quiet(blob, yolo)
    rows = _tool_rows(Handler.payloads)
    assert len(rows) >= len(calls), (yolo, len(rows), len(calls))
    for command, row in zip(controls, rows):
        assert "was not run" not in row, (yolo, command, row)
    assert "visible-note" in rows[0]
    for command, row in zip(leaks, rows[len(controls):]):
        assert "was not run" in row, (yolo, command, row)


def test_ordinary_reads_stay_allowed_beside_each_planted_secret(tmp_path):
    """cat/head/tail/wc/grep/sort/awk of notes.txt stay allowed no matter which secret sits beside them."""
    from easyagent.scriptgate import command_reads_secrets

    folder = tmp_path / "s24"
    folder.mkdir()
    (folder / "notes.txt").write_text("visible-note hunter2\n", encoding="utf-8")
    readers = [
        "cat notes.txt",
        "head -n 5 notes.txt",
        "tail notes.txt",
        "wc -l notes.txt",
        "grep hunter2 notes.txt",
        "sort notes.txt",
        "awk '{print $1}' notes.txt",
    ]
    plants = {
        ".env": "SECRETENV=1\n",
        ".npmrc": "_authToken=npm_secret\n",
        "creds.json": '{"password":"hunter2PASS"}\n',
        "id_rsa": "KEYBODYXYZ\n",
        "token.txt": "token=plain\n",
        "bland.txt": "-----BEGIN PRIVATE KEY-----\nABC\n",
        "key_material.txt": "-----BEGIN PRIVATE KEY-----\nABC\n",
        ".netrc": "machine x login a password b\n",
        "secrets.json": '{"token":"abc"}\n',
    }
    for name, body in plants.items():
        path = folder / name
        path.write_text(body, encoding="utf-8")
        for command in readers:
            assert command_reads_secrets(command, folder) is False, (name, command)
        path.unlink()
    nested = folder / ".aws"
    nested.mkdir()
    (nested / "credentials").write_text("aws_secret_access_key=abc\n", encoding="utf-8")
    for command in readers:
        assert command_reads_secrets(command, folder) is False, command


@pytest.mark.parametrize("yolo", [False, True])
def test_live_script_writes_and_ordinary_notes_do_not_print_secrets(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "0.25")
    controls = [
        "cat notes.txt",
        "head -n 1 notes.txt",
        "tail -n 1 notes.txt",
        "wc -l notes.txt",
        "grep visible notes.txt",
        "sort notes.txt",
        "awk '{print $1}' notes.txt",
        "node ok.js",
        "python3 ok.py",
        "git diff",
        "git diff HEAD",
    ]
    leaks = [
        "node r.js",
        "bun r.ts",
        "python3 r.py",
        "awk -f r.awk notes.txt",
        "python3 - <<'EOF'\nimport os\nos.read(0, 4)\nEOF",
        "awk 'BEGIN{ARGV[1]=\"bland.txt\"} {print}' notes.txt",
        "node -e \"console.log(require('fs')['read'+'FileSync']('bland.txt','utf8'))\"",
        "node -e \"import('fs').then(fs=>fs.readFileSync('bland.txt'))\"",
        "git diff HEAD~1",
        "git grep hunter2 feat",
        "git show refs/stash",
    ]

    def setup(folder: Path) -> None:
        (folder / "notes.txt").write_text("visible-note\n", encoding="utf-8")
        (folder / "ok.js").write_text("console.log(1)\n", encoding="utf-8")
        (folder / "ok.py").write_text("print(1)\n", encoding="utf-8")
        (folder / "r.js").write_text(
            "console.log(require('fs')['read'+'FileSync']('bland.txt','utf8'))\n",
            encoding="utf-8",
        )
        (folder / "r.ts").write_text(
            "const fs = Buffer.from('ZnM=', 'base64').toString(); console.log(require(fs).readFileSync('bland.txt','utf8'))\n",
            encoding="utf-8",
        )
        (folder / "r.py").write_text("import os\nprint(os.read(os.open('bland.txt', os.O_RDONLY), 64))\n", encoding="utf-8")
        (folder / "r.awk").write_text("BEGIN{ARGV[1]=\"bland.txt\"} {print}\n", encoding="utf-8")
        _exec_setup(folder)
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Ada"
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "ada@example.com"
        subprocess.run(["git", "branch", "feat"], cwd=folder, check=True, capture_output=True)
        subprocess.run(
            ["git", "stash", "push", "-m", "hold", "--", "notes.txt"],
            cwd=folder,
            check=False,
            capture_output=True,
            env=env,
        )

    calls = [("terminal", {"command": command}) for command in controls + leaks]
    heard, stored, body, _folder = _live(
        tmp_path,
        calls,
        yolo=yolo,
        ask="Read notes, then the scripts.",
        setup=setup,
    )
    blob = heard + "\n" + body + "\n" + json.dumps(stored.get("messages") or [])
    _assert_quiet(blob, yolo)
    rows = _tool_rows(Handler.payloads)
    assert len(rows) >= len(calls), (yolo, len(rows), len(calls))
    for command, row in zip(controls, rows):
        assert "was not run" not in row, (yolo, command, row)
    assert "visible-note" in rows[0]
    for command, row in zip(leaks, rows[len(controls):]):
        assert "was not run" in row, (yolo, command, row)


def _openssh(marker: str) -> str:
    return (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + marker
        + "\n-----END OPENSSH PRIVATE KEY-----\n"
    )


_ODD_SUFFIXES = (
    ".dat", ".crt", ".cer", ".p8", ".pk8", ".gpg", ".asc", ".db", ".bak", ".old",
    ".orig", ".tmp", ".swp", ".1", ".sec", ".enc", ".blob", ".data", ".pub", ".sig",
    ".dump", ".out", ".sh", ".py", ".js", ".html", ".xml", ".lock", ".properties",
    ".sql", ".key2", ".id", ".rsa", ".pem2", ".kdb",
)


def test_recursive_search_cards_unless_the_suffix_list_is_explicit(tmp_path):
    """A pathless recursive search waits. An allow-list include sniffs the matched files and runs."""
    from easyagent.safety import secret_class
    from easyagent.scriptgate import command_reads_secrets, shield_secrets

    folder = tmp_path / "keys"
    folder.mkdir()
    (folder / "notes.txt").write_text("alpha\nNOTES\nline3\nline4\nline5\n", encoding="utf-8")
    (folder / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    (folder / ".npmrc").write_text("_authToken=npm_secret\n", encoding="utf-8")
    (folder / "plain.bin").write_bytes(b"plain-bin-ok\n")
    (folder / "data.bin").write_bytes(_openssh("KEYBODYBIN").encode())
    (folder / "putty.ppk").write_bytes(b"PuTTY-User-Key-File-3: ssh-ed25519\nComment: KEYBODYPPK\n")
    (folder / "token.der").write_bytes(_openssh("KEYBODYDER").encode())
    src = folder / "src"
    src.mkdir()
    (src / "app.py").write_text("# TODO-app\n", encoding="utf-8")
    (src / "secret.py").write_text("# TODO-secret\n" + _openssh("KEYBODY_PY"), encoding="utf-8")
    (folder / "run.sh").write_text(_openssh("KEYBODY_SH"), encoding="utf-8")
    (folder / "note.txt").write_text(_openssh("KEYBODY_TXT"), encoding="utf-8")
    (folder / "extensionless").write_text(_openssh("KEYBODY_BARE"), encoding="utf-8")
    (folder / "big.txt").write_bytes(b"x" * 300_000 + b"\n" + _openssh("KEYBODY_BIG").encode())
    (folder / "big.bin").write_bytes(b"x" * 9_000 + _openssh("KEYBODY_BIN").encode())
    for suffix in _ODD_SUFFIXES:
        (folder / f"k{suffix}").write_text(_openssh("KEYBODY_ODD"), encoding="utf-8")
    assert secret_class(folder / "data.bin") == "content"
    assert secret_class(folder / "putty.ppk") == "content"
    assert secret_class(folder / "token.der") == "content"
    assert secret_class(folder / "plain.bin") is None
    assert secret_class(folder / "big.bin") is None
    leaks = [
        "grep -r KEYBODY .",
        "grep -R KEYBODY .",
        "grep -rn KEYBODY .",
        "grep -ra KEYBODY .",
        "rg KEYBODY",
        "rg KEYBODY .",
        "grep --include='*' -r KEYBODY .",
        "grep --include='*.txt' -r KEYBODY .",
        "grep --include='*.sh' -r KEYBODY .",
        "rg --type sh KEYBODY",
        "grep -r KEYBODY big.txt",
        "grep -r KEYBODY big.bin",
        "grep --dereference-recursive KEYBODY .",
        "sed 'r.env' notes.txt",
        "sed '1r .npmrc' notes.txt",
        "sed -e 'r.env' -n notes.txt",
        "sed --expression='r .env' notes.txt",
        "sed 'R .env' notes.txt",
        "sed '1R.npmrc' notes.txt",
        "sed '1e cat .env' notes.txt",
        "sed '/NOTES/r .env' notes.txt",
        "sed -n '/./r .env' notes.txt",
        "sed 'p; r .env' notes.txt",
        "sed '{r.env}' notes.txt",
        "sed '$!r .env' notes.txt",
        "sed --posix 'r .env' notes.txt",
        "sed -ne 'r .env' notes.txt",
        "sed -f x.sed notes.txt",
        "sed -i 's/a/b/' notes.txt",
        "sed --in-place 's/a/b/' notes.txt",
        "sed -s 'p' notes.txt",
        "sed 's/a/b/e' notes.txt",
        "sed 's/a/b/w out.txt' notes.txt",
        "sed 'w .env' notes.txt",
        "sed 'W.env' notes.txt",
        "sed 'E cat .env' notes.txt",
    ]
    for command in leaks:
        assert command_reads_secrets(command, folder), command
    allows = [
        "cat notes.txt",
        "cat plain.bin",
        "grep plain-bin plain.bin",
        "grep -n TODO src/app.py",
        "grep -rn TODO --include='*.py' src",
        "grep -rn TODO --include=*.py src",
        "rg -g '*.py' TODO src",
        "rg --type py TODO src",
        "rg -tpy TODO src",
        "sed -n '1,5p' notes.txt",
        "sed 's/a/b/' notes.txt",
        "sed 's/add/plus/' notes.txt",
        "sed 's/a/r.env/' notes.txt",
        "sed 'y/rReE/abcd/' notes.txt",
        "sed --posix -n '1,5p' notes.txt",
        "sed -ne 's/a/b/' notes.txt",
        "sed -e's/a/b/' notes.txt",
    ]
    for command in allows:
        assert command_reads_secrets(command, folder) is False, command
    shielded = shield_secrets("grep -rn TODO --include='*.py' src", folder)
    assert shielded.index("--include") < shielded.index("--exclude=secret.py")
    assert "TODO-secret" not in shielded
    typed = shield_secrets("rg --type py TODO src", folder)
    assert typed.index("-g '*.py'") < typed.index("-g '!secret.py'")
    assert command_reads_secrets("cat data.bin", folder)
    assert shield_secrets("grep -r KEYBODY .", folder) == "grep -r KEYBODY ."


def _search_tree(folder: Path) -> None:
    (folder / "notes.txt").write_text("visible-note\n", encoding="utf-8")
    (folder / "plain.bin").write_bytes(b"plain-bin-ok\n")
    (folder / "data.bin").write_bytes(_openssh("KEYBODYBIN").encode())
    (folder / "putty.ppk").write_bytes(b"PuTTY-User-Key-File-3: ssh-ed25519\nComment: KEYBODYPPK\n")
    (folder / "token.der").write_bytes(_openssh("KEYBODYDER").encode())
    src = folder / "src"
    src.mkdir(exist_ok=True)
    (src / "app.py").write_text("# TODO-app\n", encoding="utf-8")
    (src / "secret.py").write_text("# TODO-secret\n" + _openssh("KEYBODY_PY"), encoding="utf-8")
    (folder / "run.sh").write_text(_openssh("KEYBODY_SH"), encoding="utf-8")
    (folder / "note.txt").write_text(_openssh("KEYBODY_TXT"), encoding="utf-8")
    (folder / "extensionless").write_text(_openssh("KEYBODY_BARE"), encoding="utf-8")
    (folder / "big.txt").write_bytes(b"x" * 300_000 + b"\n" + _openssh("KEYBODY_BIG").encode())
    (folder / "big.bin").write_bytes(b"x" * 9_000 + _openssh("KEYBODY_BIN").encode())
    for suffix in _ODD_SUFFIXES:
        (folder / f"k{suffix}").write_text(_openssh("KEYBODY_ODD"), encoding="utf-8")


_KEY_MARKERS = (
    "KEYBODYBIN", "KEYBODYPPK", "KEYBODYDER", "KEYBODY_PY", "KEYBODY_SH", "KEYBODY_TXT",
    "KEYBODY_BARE", "KEYBODY_BIG", "KEYBODY_BIN", "KEYBODY_ODD", "TODO-secret",
    "PuTTY-User-Key-File", "BEGIN OPENSSH PRIVATE KEY",
)


@pytest.mark.parametrize("yolo", [False, True])
def test_live_pathless_search_does_not_print_a_key(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "0.25")
    # grep -R casefolds onto grep -r, so it has its own chat.
    batches = [
        [
            "cat notes.txt",
            "cat plain.bin",
            "grep -rn TODO --include='*.py' src",
            "grep -r KEYBODY .",
            "grep -rn KEYBODY .",
            "grep -ra KEYBODY .",
            "rg KEYBODY",
            "grep --include='*' -r KEYBODY .",
            "grep -r KEYBODY big.txt",
            "grep -r KEYBODY big.bin",
        ],
        [
            "cat notes.txt",
            "grep -R KEYBODY .",
        ],
    ]
    runs = {
        "cat notes.txt": "visible-note",
        "cat plain.bin": "plain-bin-ok",
        "grep -rn TODO --include='*.py' src": "TODO-app",
    }
    for commands in batches:
        Handler.payloads = []
        heard, stored, body, _folder = _live(
            tmp_path,
            [("terminal", {"command": command}) for command in commands],
            yolo=yolo,
            ask="Search the project.",
            setup=_search_tree,
        )
        blob = heard + "\n" + body + "\n" + json.dumps(stored.get("messages") or [])
        _assert_quiet(blob, yolo)
        for marker in _KEY_MARKERS:
            assert marker not in blob, (yolo, marker)
        rows = _tool_rows(Handler.payloads)
        assert len(rows) >= len(commands), (yolo, commands, len(rows), len(commands))
        for command, row in zip(commands, rows):
            if command in runs:
                assert "was not run" not in row, (yolo, command, row)
                assert runs[command] in row, (yolo, command, row)
            else:
                assert "was not run" in row, (yolo, command, row)


def _sed_notes(folder: Path) -> None:
    (folder / "notes.txt").write_text("alpha\nNOTES\nline3\nline4\nline5\n", encoding="utf-8")


@pytest.mark.parametrize("yolo", [False, True])
def test_live_sed_read_commands_do_not_print(tmp_path, monkeypatch, yolo):
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "0.25")
    # Space-stripping makes r.env and R .env the same repeat, and likewise 1r and 1R.
    batches = [
        [
            "sed -n '1,5p' notes.txt",
            "sed 's/a/b/' notes.txt",
            "sed 'r.env' notes.txt",
            "sed '1r .npmrc' notes.txt",
            "sed -e 'r.env' -n notes.txt",
            "sed --expression='r .env' notes.txt",
            "sed '1e cat .env' notes.txt",
            "sed '/NOTES/r .env' notes.txt",
            "sed -n '/./r .env' notes.txt",
        ],
        [
            "sed -n '1p' notes.txt",
            "sed 'R .env' notes.txt",
            "sed '1R.npmrc' notes.txt",
        ],
    ]
    runs = {
        "sed -n '1,5p' notes.txt": "alpha",
        "sed 's/a/b/' notes.txt": "blpha",
        "sed -n '1p' notes.txt": "alpha",
    }
    for commands in batches:
        Handler.payloads = []
        heard, stored, body, _folder = _live(
            tmp_path,
            [("terminal", {"command": command}) for command in commands],
            yolo=yolo,
            ask="Edit the notes.",
            setup=_sed_notes,
        )
        blob = heard + "\n" + body + "\n" + json.dumps(stored.get("messages") or [])
        _assert_quiet(blob, yolo)
        assert "SECRETENV" not in blob, yolo
        assert "_authToken" not in blob, yolo
        rows = _tool_rows(Handler.payloads)
        assert len(rows) >= len(commands), (yolo, commands, len(rows), len(commands))
        for command, row in zip(commands, rows):
            if command in runs:
                assert "was not run" not in row, (yolo, command, row)
                assert runs[command] in row, (yolo, command, row)
            else:
                assert "was not run" in row, (yolo, command, row)
