"""0.3.33: verify flags survive the worker thread, and the Hermes file tools run."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from easyagent.app import create_app, public_run
from easyagent.coding import read_window, search_tree, shell_read_target
from easyagent.filestate import get_registry
from easyagent.fuzzy import fuzzy_find_and_replace
from easyagent.linting import check_lint_delta
from easyagent.llm import _short_excerpt, malformed_with_excerpt
from easyagent.plan import ensure_request_checklist
from easyagent.tools import (
    ToolError,
    ToolRequest,
    _file_sentence,
    _read_path,
    _run_shell,
    _with_file_place,
    _write_path,
    apply_verify_note,
    begin_turn_checks,
    end_turn_checks,
    note_turn_effect,
    parse_tool,
)
from easyagent.truncate import truncate_head_tail


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
            'data: {"choices":[{"delta":{"content":"Done. I wrote the agenda into note.txt and that is the whole summary of this change for the record."}}]}',
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
    end_turn_checks()
    get_registry().clear()


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


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path.parent / f"ea-0333-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    return folder


def test_a_worker_thread_keeps_the_unverified_note():
    begin_turn_checks()

    async def go():
        await asyncio.to_thread(note_turn_effect)
        return apply_verify_note("Done.")

    try:
        assert "This was not verified." in asyncio.run(go())
    finally:
        end_turn_checks()


def test_excerpt_breaks_on_a_word_and_names_the_retry():
    body = "Failed to parse tool call arguments as JSON because the server stopped mid token"
    excerpt = _short_excerpt(body, limit=40)
    assert excerpt.endswith("…")
    kept = excerpt[:-1].rstrip()
    assert body.startswith(kept)
    assert not kept.endswith("mid")
    shown = malformed_with_excerpt(body, retry=1)
    assert "retry=1" in shown
    assert "Server said:" in shown


def test_the_location_follows_the_summary_and_a_dot_folder_is_named():
    assert _file_sentence("./note.txt") == "The file is note.txt in the project folder."
    assert "in ." not in _file_sentence("./note.txt")
    summary = "I wrote the agenda into note.txt and that is the whole summary of this change for the record."
    shown = _with_file_place(summary, ["Wrote /work/note.txt."])
    assert summary in shown
    assert "The file is note.txt in /work." in shown
    bare = _with_file_place("Saved it.", ["Wrote /work/note.txt."])
    assert "Saved it." not in bare
    assert "The file is note.txt in /work." in bare


def test_read_patch_write_and_a_stale_file(tmp_path):
    folder = _folder(tmp_path)
    path = folder / "note.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8")
    begin_turn_checks()
    try:
        assert "alpha" in _read_path(path)
        fresh = folder / "new.txt"
        assert "Wrote" in _write_path(fresh, "created\n")
        patched = parse_tool(
            "```patch\n"
            f"{path}\n"
            "---OLD---\nalpha\n"
            "---NEW---\ngamma\n"
            "```"
        )
        assert patched is not None and patched.action == "patch"
        from easyagent.tools import _run_patch

        report = _run_patch(path, patched)
        assert "gamma" in path.read_text(encoding="utf-8")
        assert "verified: true" in report
        assert "exact" in report
        path.write_text("external\n", encoding="utf-8")
        try:
            _write_path(path, "nope\n")
            raised = False
        except ToolError as exc:
            raised = True
            assert "changed since it was last read" in str(exc)
        assert raised
    finally:
        end_turn_checks()


def test_a_read_survives_into_the_next_turn(tmp_path):
    folder = _folder(tmp_path)
    path = folder / "keep.txt"
    path.write_text("one\n", encoding="utf-8")
    begin_turn_checks()
    _read_path(path)
    end_turn_checks()
    begin_turn_checks()
    try:
        _write_path(path, "two\n")
        assert path.read_text(encoding="utf-8") == "two\n"
    finally:
        end_turn_checks()


def test_a_shell_read_lets_the_write_through(tmp_path):
    folder = _folder(tmp_path)
    path = folder / "seen.txt"
    path.write_text("from-shell\n", encoding="utf-8")
    app = create_app(tmp_path)
    client = TestClient(app)
    client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"})
    store = app.state.store if hasattr(app.state, "store") else None
    # create_app keeps the store on the app factory closure. Use the data dir store.
    from easyagent.store import Store

    store = Store(tmp_path)
    begin_turn_checks()
    try:
        heard = _run_shell(store, f"cat {path}", timeout=30)
        assert "from-shell" in heard
        assert "exit code 0" in heard
        assert "cwd" in heard
        _write_path(path, "after-cat\n")
        assert path.read_text(encoding="utf-8") == "after-cat\n"
    finally:
        end_turn_checks()
        client.close()


def test_shell_read_target_ignores_a_pipe():
    assert shell_read_target("cat notes.txt") == ("notes.txt", False)
    assert shell_read_target("head -n 5 notes.txt")[1] is True
    assert shell_read_target("cat notes.txt | wc") is None
    assert shell_read_target("sed -n 1,2p notes.txt") == ("notes.txt", True)


def test_numbered_read_dedup_and_search(tmp_path):
    folder = _folder(tmp_path)
    path = folder / "lines.txt"
    path.write_text("\n".join(f"row {i}" for i in range(1, 6)) + "\n", encoding="utf-8")
    window, partial = read_window(path, offset=2, limit=2, task_id="chat")
    assert "2|row 2" in window
    assert "3|row 3" in window
    assert "next_offset=4" in window
    assert partial is True
    again, _ = read_window(path, offset=2, limit=2, task_id="chat")
    assert again.startswith("File unchanged")
    blocked, _ = read_window(path, offset=2, limit=2, task_id="chat")
    assert blocked.startswith("BLOCKED:")
    (folder / "other.md").write_text("needle here\n", encoding="utf-8")
    found = search_tree(folder, "needle", limit=10)
    assert "other.md" in found
    assert ":1:" in found
    new, count, strategy, error = fuzzy_find_and_replace("alpha beta\n", "beta", "gamma", False)
    assert error is None and strategy == "exact" and count == 1 and "gamma" in new
    lint = check_lint_delta("note.py", "def ok():\n    return 1\n", "def ok(\n    return 1\n")
    assert "SyntaxError" in lint
    same = check_lint_delta("note.py", "def ok(\n    return 1\n", "def ok(\n    return 1\n")
    assert "didn't introduce new ones" in same or same == ""
    notice = truncate_head_tail("a" * 50, 10)
    assert "TRUNCATED" in notice
    assert "omitted" in notice


def test_the_checklist_is_on_the_run(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    ask = "Please update the project notes and leave a short summary when the file is saved. " * 2
    from easyagent.store import Store

    store = Store(tmp_path)
    ensure_request_checklist(store, bot["id"], chat["id"], ask)
    shown = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert shown["run"]["plan"]["title"] == "Request"
    assert shown["run"]["plan"]["steps"]
    assert public_run(store.get_chat(bot["id"], chat["id"]), store)["plan"]["title"] == "Request"
    client.close()


def test_a_live_write_keeps_the_summary_and_the_unverified_note(tmp_path):
    folder = _folder(tmp_path)
    target = folder / "note.txt"
    Handler.scripts = [
        _tool_sse("write_file", {"path": str(target), "content": "agenda\n"}, "call_write"),
    ]
    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    try:
        with client.stream(
            "POST",
            url,
            json={"content": f"write a note in {folder} and name it note.txt"},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
        answer = stored["messages"][-1]["content"]
        assert target.read_text(encoding="utf-8") == "agenda\n"
        assert "whole summary of this change" in answer
        assert "This was not verified." in answer
        assert "in ." not in answer
    finally:
        client.close()
        server.shutdown()


def test_yolo_writes_a_script_in_the_folder_and_still_blocks_a_hard_body(tmp_path):
    from easyagent import turn as turn_mod
    from easyagent.safety import classify
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace
    from easyagent.yolo import covers

    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    toggled = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/yolo",
        json={"on": True},
    )
    assert toggled.status_code == 200, toggled.text
    store = Store(tmp_path)
    folder = bot_workspace(store, bot["id"], create=True)
    turn_mod.bind(store, chat["id"], bot["id"])
    try:
        harmless = ToolRequest(kind="files", action="write", path=str(folder / "hello.py"), body='print("hello")\n')
        verdict = classify(store, harmless, bot["id"])
        if verdict.tier == "ask":
            assert covers(store, bot["id"], harmless, verdict) is True
        shebang = ToolRequest(
            kind="files",
            action="write",
            path=str(folder / "run.sh"),
            body="#!/bin/sh\necho hi\n",
        )
        shebang_verdict = classify(store, shebang, bot["id"])
        assert shebang_verdict.tier == "ask" and shebang_verdict.rule == "script-body"
        assert covers(store, bot["id"], shebang, shebang_verdict) is True
        outside = ToolRequest(
            kind="files",
            action="write",
            path=str(folder / "out.sh"),
            body="#!/bin/sh\necho hi > /tmp/outside-from-script.txt\n",
        )
        outside_verdict = classify(store, outside, bot["id"])
        if outside_verdict.tier == "ask":
            assert covers(store, bot["id"], outside, outside_verdict) is False
        blocked = ToolRequest(kind="files", action="write", path=str(folder / "bad.py"), body="rm -rf /\n")
        hard = classify(store, blocked, bot["id"])
        assert hard.tier in {"block", "ask"}
        if hard.tier == "ask":
            assert covers(store, bot["id"], blocked, hard) is False
    finally:
        turn_mod._slot.set(None)
        client.close()


def _yolo_bot(tmp_path):
    from easyagent import turn as turn_mod
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    toggled = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/yolo", json={"on": True})
    assert toggled.status_code == 200, toggled.text
    store = Store(tmp_path)
    folder = bot_workspace(store, bot["id"], create=True)
    turn_mod.bind(store, chat["id"], bot["id"])
    return client, store, bot, folder


def test_yolo_does_not_cover_a_script_that_leaves_the_folder(tmp_path):
    """c1ed147 saved #!/bin/sh then rm ../outside with no card. The body has to be proved."""
    from easyagent import turn as turn_mod
    from easyagent.safety import classify
    from easyagent.yolo import covers

    client, store, bot, folder = _yolo_bot(tmp_path)
    outside = folder.parent / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    abs_outside = outside
    (folder / "other.sh").write_text("#!/bin/sh\nrm -f ../outside.txt\n", encoding="utf-8")
    link = folder / "link-out"
    try:
        link.symlink_to(outside)
    except OSError:
        link = None
    hostile = [
        "#!/bin/sh\nrm -f ../outside.txt\n",
        f"#!/bin/sh\nrm -f {abs_outside}\n",
        "#!/bin/sh\nT=../outside.txt\nrm -f \"$T\"\n",
        "#!/bin/sh\nrm -f $(echo ../outside.txt)\n",
        "#!/bin/sh\ncd ..\nrm -f outside.txt\n",
        "#!/bin/sh\neval 'rm -f ../outside.txt'\n",
        "#!/bin/sh\ncat > nested.sh <<'EOF'\nrm -f ../outside.txt\nEOF\n",
        "#!/bin/sh\necho cm0gLWYgLi4vb3V0c2lkZS50eHQ= | base64 -d | sh\n",
        "#!/bin/sh\nsh other.sh\n",
        "#!/usr/bin/env python3\nimport os\nos.remove('../outside.txt')\n",
        "#!/usr/bin/env python3\nimport os\nos.remove('/tmp/ea-d11-outside.txt')\n",
    ]
    if link is not None:
        hostile.append("#!/bin/sh\nrm -f link-out\n")
    try:
        for body in hostile:
            request = ToolRequest(kind="files", action="write", path=str(folder / "run.sh"), body=body)
            verdict = classify(store, request, bot["id"])
            assert verdict.tier in {"ask", "block"}, (body, verdict.tier, verdict.rule)
            if verdict.tier == "ask":
                assert covers(store, bot["id"], request, verdict) is False, (body, verdict.rule)
        for body, name in (
            ("#!/bin/sh\necho hi\n", "run.sh"),
            ("#!/usr/bin/env python3\nprint('hi')\n", "hi.py"),
        ):
            request = ToolRequest(kind="files", action="write", path=str(folder / name), body=body)
            verdict = classify(store, request, bot["id"])
            if verdict.tier == "ask":
                assert covers(store, bot["id"], request, verdict) is True, (name, verdict.rule)
            else:
                assert verdict.tier == "allow", (name, verdict.tier, verdict.rule)
        planted = folder / "run.sh"
        planted.write_text("#!/bin/sh\nrm -f ../outside.txt\n", encoding="utf-8")
        run = ToolRequest(kind="shell", command="sh run.sh")
        run_verdict = classify(store, run, bot["id"])
        assert run_verdict.tier in {"ask", "block"}, (run_verdict.tier, run_verdict.rule)
        if run_verdict.tier == "ask":
            assert covers(store, bot["id"], run, run_verdict) is False
        try:
            _run_shell(store, "sh run.sh")
        except ToolError:
            pass
        assert outside.read_text(encoding="utf-8") == "keep\n"
        planted.write_text("#!/bin/sh\necho hi > inside.txt\n", encoding="utf-8")
        safe = ToolRequest(kind="shell", command="sh run.sh")
        safe_run = classify(store, safe, bot["id"])
        if safe_run.tier == "ask" and safe_run.rule == "script-body":
            assert covers(store, bot["id"], safe, safe_run) is True
        heard = _run_shell(store, "sh run.sh")
        assert "hi" in (folder / "inside.txt").read_text(encoding="utf-8")
        assert "not limited" not in heard
    finally:
        turn_mod._slot.set(None)
        client.close()


def test_a_live_yolo_script_write_does_not_ask(tmp_path):
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    target = folder / "hello.py"
    Handler.scripts = [
        _tool_sse("write_file", {"path": str(target), "content": 'print("hello")\n'}, "call_py"),
        [
            'data: {"choices":[{"delta":{"content":"The script is in place."}}]}',
            "data: [DONE]",
        ],
    ]
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        toggled = client.post(f"{url}/yolo", json={"on": True})
        assert toggled.status_code == 200, toggled.text
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": f"write hello.py in {folder}"},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        stored = client.get(url).json()
        assert target.is_file(), stored["messages"]
        assert target.read_text(encoding="utf-8") == 'print("hello")\n'
        assert stored["run"]["status"] != "paused"
        blob = json.dumps(stored["messages"])
        assert "waits for you" not in blob
    finally:
        client.close()
        server.shutdown()


def test_a_listed_path_can_be_read_without_an_injection_card(tmp_path):
    from easyagent.safety import classify, mark_untrusted
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace
    from easyagent import turn as turn_mod

    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    store = Store(tmp_path)
    folder = bot_workspace(store, bot["id"], create=True)
    target = folder / "listed.txt"
    target.write_text("hello\n", encoding="utf-8")
    mark_untrusted(f"Listed {folder}\n{target}\n")
    turn_mod.bind(store, chat["id"], bot["id"])
    begin_turn_checks()
    try:
        verdict = classify(
            store,
            ToolRequest(kind="files", action="read", path=str(target)),
            bot["id"],
        )
        assert verdict.tier == "allow", verdict
        _read_path(target)
        write = classify(
            store,
            ToolRequest(kind="files", action="write", path=str(target), body="hello\nthere\n"),
            bot["id"],
        )
        assert write.rule != "injection"
    finally:
        end_turn_checks()
        turn_mod._slot.set(None)
        client.close()


_SECRET_MARKERS = ("KEYBODYXYZ", "hunter2PASS", "SECRETENV=", "-----BEGIN KEY-----")


def _plant_secrets(folder: Path) -> None:
    (folder / "id_rsa").write_text("comment\nKEYBODYXYZ\n", encoding="utf-8")
    (folder / ".env").write_text("SECRETENV=1\n", encoding="utf-8")
    ssh = folder / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("-----BEGIN KEY-----\n", encoding="utf-8")
    (folder / "creds.json").write_text('{"password":"hunter2PASS"}\n', encoding="utf-8")
    (folder / "notes.txt").write_text("visible-note\n", encoding="utf-8")


def _tool_text(payloads: list) -> str:
    chunks = []
    for payload in payloads:
        for message in payload.get("messages") or []:
            if message.get("role") == "tool":
                chunks.append(str(message.get("content") or ""))
    return "\n".join(chunks)


def test_search_skips_node_modules(tmp_path):
    folder = _folder(tmp_path)
    (folder / "src").mkdir()
    (folder / "src" / "app.js").write_text("SRC_HIT\n", encoding="utf-8")
    nested = folder / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    (nested / "index.js").write_text("NM_HIT\n", encoding="utf-8")
    found = search_tree(folder, "HIT")
    assert "SRC_HIT" in found
    assert "NM_HIT" not in found


def test_a_folder_wide_shell_read_cards_secret_files(tmp_path):
    from easyagent import turn as turn_mod
    from easyagent.safety import classify
    from easyagent.yolo import covers

    client, store, bot, folder = _yolo_bot(tmp_path)
    _plant_secrets(folder)
    clean = folder / "empty-tree"
    clean.mkdir()
    (clean / "notes.txt").write_text("visible-note\n", encoding="utf-8")
    recursive = [
        f"grep -rn KEYBODY {folder}",
        f"rg KEYBODY {folder}",
        f"egrep -rn KEYBODY {folder}",
    ]
    commands = [
        "cat *",
        "head *",
        "strings *",
        "awk '{print}' *",
        "sed -n p *",
        "find . -exec grep KEYBODY {} +",
    ]
    try:
        turn_mod.current_slot().cwd = str(folder)
        for command in recursive:
            request = ToolRequest(kind="shell", command=command)
            verdict = classify(store, request, bot["id"])
            assert verdict.tier == "ask" and verdict.rule == "read-secret", (command, verdict.tier, verdict.rule)
            assert covers(store, bot["id"], request, verdict) is False, (command, verdict.rule)
        named = classify(store, ToolRequest(kind="shell", command="grep KEYBODY id_rsa"), bot["id"])
        assert named.tier == "ask" and named.rule == "read-secret", (named.tier, named.rule)
        for command in commands:
            request = ToolRequest(kind="shell", command=command)
            verdict = classify(store, request, bot["id"])
            assert verdict.tier == "ask", (command, verdict.tier, verdict.rule)
            assert covers(store, bot["id"], request, verdict) is False, (command, verdict.rule)
        single = ToolRequest(kind="shell", command="grep visible-note notes.txt")
        one = classify(store, single, bot["id"])
        assert one.rule != "read-secret", one
        if one.tier == "allow":
            heard = _run_shell(store, "grep visible-note notes.txt")
            assert "visible-note" in heard
            for marker in _SECRET_MARKERS:
                assert marker not in heard
        quiet = ToolRequest(kind="shell", command=f"grep -rn visible-note {clean}")
        quiet_verdict = classify(store, quiet, bot["id"])
        assert quiet_verdict.tier == "ask" and quiet_verdict.rule == "read-secret", quiet_verdict
    finally:
        turn_mod._slot.set(None)
        client.close()


def test_search_drops_secret_files_and_stops(tmp_path, monkeypatch):
    from easyagent.coding import search_root_error, search_tree
    import easyagent.coding as coding

    folder = _folder(tmp_path)
    _plant_secrets(folder)
    if os.name != "nt":
        target = folder.parent / f"secret-target-{folder.name}"
        target.write_text("KEYBODYXYZ\n", encoding="utf-8")
        (folder / "alias").symlink_to(target)
    monkeypatch.setattr(coding.shutil, "which", lambda _name: None)
    found = search_tree(folder, "KEYBODY|hunter2|SECRETENV|BEGIN|visible-note")
    assert "visible-note" in found
    for marker in _SECRET_MARKERS:
        assert marker not in found
    assert "alias" not in found
    assert search_root_error(Path("/"))
    monkeypatch.setattr(coding, "SEARCH_MAX_FILES", 5)
    bulk = folder / "bulk"
    bulk.mkdir()
    for number in range(12):
        (bulk / f"n{number}.txt").write_text("bulk-hit\n", encoding="utf-8")
    stopped = search_tree(bulk, "bulk-hit")
    assert "stopped" in stopped
    assert "5 files" in stopped


def test_a_secret_read_still_cards_and_search_of_root_does_not_run(tmp_path):
    from easyagent import turn as turn_mod
    from easyagent.safety import classify
    from easyagent.store import Store
    from easyagent.tools import ToolError, _run_search_files
    from easyagent.workspace import bot_workspace
    from easyagent.yolo import covers

    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/yolo", json={"on": True})
    store = Store(tmp_path)
    folder = bot_workspace(store, bot["id"], create=True)
    _plant_secrets(folder)
    turn_mod.bind(store, chat["id"], bot["id"])
    try:
        for name in ("id_rsa", ".env", "creds.json", ".ssh/id_rsa"):
            verdict = classify(
                store,
                ToolRequest(kind="files", action="read", path=str(folder / name)),
                bot["id"],
            )
            assert verdict.tier == "ask" and verdict.rule == "read-secret", name
            assert covers(store, bot["id"], ToolRequest(kind="files", action="read", path=str(folder / name)), verdict) is False
        try:
            _run_search_files(store, ToolRequest(kind="search_files", path="/", body="BEGIN"))
            raised = False
        except ToolError as exc:
            raised = True
            assert "not run" in str(exc).lower()
            assert "BEGIN KEY" not in str(exc)
        assert raised
    finally:
        turn_mod._slot.set(None)
        client.close()


def _live_search(tmp_path, pattern: str, *, yolo: bool, use_rg: bool, monkeypatch, prepare):
    import easyagent.coding as coding
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    if not use_rg:
        monkeypatch.setattr(coding.shutil, "which", lambda _name: None)
    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    search_path = prepare(folder)
    Handler.scripts = [
        _tool_sse("search_files", {"pattern": pattern, "path": str(search_path)}, "call_search"),
        [
            'data: {"choices":[{"delta":{"content":"Search finished."}}]}',
            "data: [DONE]",
        ],
    ]
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        if yolo:
            toggled = client.post(f"{url}/yolo", json={"on": True})
            assert toggled.status_code == 200, toggled.text
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": "Search the project notes."},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        return _tool_text(Handler.payloads)
    finally:
        client.close()
        server.shutdown()


def test_live_search_hides_secrets_with_and_without_rg(tmp_path, monkeypatch):
    outside = tmp_path.parent / f"ea-outside-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    (outside / "id_rsa").write_text("-----BEGIN KEY-----\n", encoding="utf-8")
    pattern = "visible-note|KEYBODY|hunter2|SECRETENV|BEGIN"

    def plant(folder: Path) -> Path:
        _plant_secrets(folder)
        return folder

    for yolo in (False, True):
        for use_rg in (True, False):
            heard = _live_search(
                tmp_path,
                pattern,
                yolo=yolo,
                use_rg=use_rg,
                monkeypatch=monkeypatch,
                prepare=plant,
            )
            assert "visible-note" in heard, (yolo, use_rg, heard)
            for marker in _SECRET_MARKERS:
                assert marker not in heard, (yolo, use_rg, marker, heard)
    hidden = _live_search(
        tmp_path,
        "BEGIN",
        yolo=True,
        use_rg=False,
        monkeypatch=monkeypatch,
        prepare=lambda _folder: outside,
    )
    assert "-----BEGIN KEY-----" not in hidden
    assert "not run" in hidden.lower() or "outside" in hidden.lower()


def test_live_python_search_stops_on_a_large_tree(tmp_path, monkeypatch):
    import easyagent.coding as coding

    def plant(folder: Path) -> Path:
        for number in range(30):
            (folder / f"bulk-{number}.txt").write_text("bulk-hit\n", encoding="utf-8")
        return folder

    monkeypatch.setattr(coding, "SEARCH_MAX_FILES", 8)
    heard = _live_search(
        tmp_path,
        "bulk-hit",
        yolo=False,
        use_rg=False,
        monkeypatch=monkeypatch,
        prepare=plant,
    )
    assert "stopped" in heard
    assert "bulk-hit" in heard


def test_a_live_shebang_script_write_does_not_ask(tmp_path):
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    target = folder / "run.sh"
    Handler.scripts = [
        _tool_sse("write_file", {"path": str(target), "content": "#!/bin/sh\necho hi\n"}, "call_sh"),
        [
            'data: {"choices":[{"delta":{"content":"The script is in place."}}]}',
            "data: [DONE]",
        ],
    ]
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        toggled = client.post(f"{url}/yolo", json={"on": True})
        assert toggled.status_code == 200, toggled.text
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": f"write run.sh in {folder}"},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        stored = client.get(url).json()
        assert target.is_file(), stored["messages"]
        assert target.read_text(encoding="utf-8").startswith("#!/bin/sh")
        assert stored["run"]["status"] != "paused"
        assert "waits for you" not in json.dumps(stored["messages"])
    finally:
        client.close()
        server.shutdown()


def test_a_live_python_shebang_write_does_not_ask(tmp_path):
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    target = folder / "hi.py"
    Handler.scripts = [
        _tool_sse(
            "write_file",
            {"path": str(target), "content": "#!/usr/bin/env python3\nprint('hi')\n"},
            "call_py",
        ),
        [
            'data: {"choices":[{"delta":{"content":"The python script is in place."}}]}',
            "data: [DONE]",
        ],
    ]
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        toggled = client.post(f"{url}/yolo", json={"on": True})
        assert toggled.status_code == 200, toggled.text
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": f"write hi.py in {folder}"},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        stored = client.get(url).json()
        assert target.is_file(), stored["messages"]
        assert "print('hi')" in target.read_text(encoding="utf-8")
        assert stored["run"]["status"] != "paused"
        assert "waits for you" not in json.dumps(stored["messages"])
    finally:
        client.close()
        server.shutdown()


def test_a_live_yolo_hostile_script_does_not_escape(tmp_path, monkeypatch):
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    outside = folder.parent / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    target = folder / "run.sh"
    body = "#!/bin/sh\nrm -f ../outside.txt\n"
    Handler.scripts = [
        _tool_sse("write_file", {"path": str(target), "content": body}, "call_sh"),
        _tool_sse("terminal", {"command": "sh run.sh"}, "call_run"),
        [
            'data: {"choices":[{"delta":{"content":"Done."}}]}',
            "data: [DONE]",
        ],
    ]
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    try:
        toggled = client.post(f"{url}/yolo", json={"on": True})
        assert toggled.status_code == 200, toggled.text
        with client.stream(
            "POST",
            f"{url}/messages",
            json={"content": f"write run.sh in {folder} and run it"},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            response.read()
        stored = client.get(url).json()
        assert outside.read_text(encoding="utf-8") == "keep\n"
        heard = _tool_text(Handler.payloads)
        wrote = target.is_file() and "rm -f ../outside.txt" in target.read_text(encoding="utf-8")
        assert not wrote, heard
        assert "waits for you" in heard or "not written" in heard or "not run" in heard or "denied" in heard or stored["run"]["status"] == "paused", heard
    finally:
        client.close()
        server.shutdown()


def test_bwrap_cannot_delete_outside_the_folder(tmp_path):
    import shutil
    import sys

    import pytest

    from easyagent import turn as turn_mod
    from easyagent.contain import setup, undo
    from easyagent.sandbox import public_status
    from easyagent.selftest import _linux_rows, reset_probe
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    if sys.platform != "linux" or shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not installed")
    rows = _linux_rows()
    table = {name: status for name, status, _detail in rows}
    if table.get("RESULT") != "PASS":
        pytest.skip("bubblewrap selftest did not pass")
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "stub"},
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    store = Store(tmp_path)
    folder = bot_workspace(store, bot["id"], create=True)
    outside_dir = tmp_path.parent / f"ea-bwrap-{tmp_path.name}"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    script = folder / "wipe.sh"
    script.write_text(f"#!/bin/sh\nrm -f {outside}\nrm -f ../outside.txt\n", encoding="utf-8")
    setup(store, bot["id"])
    reset_probe()
    slot = turn_mod.slot_for(store, "chat-bwrap")
    slot.bot_id = bot["id"]
    slot.store = store
    slot.cwd = str(folder)
    turn_mod._slot.set(slot)
    try:
        status = public_status(store, bot["id"])
        assert status["active"] is True, status
        try:
            _run_shell(store, "sh wipe.sh")
        except ToolError:
            pass
        assert outside.read_text(encoding="utf-8") == "keep\n"
    finally:
        turn_mod._slot.set(None)
        undo(store, bot["id"])
        client.close()


def test_a_live_grep_does_not_print_a_secret(tmp_path, monkeypatch):
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "1")
    server = _serve()
    client, bot, chat = _chat(tmp_path, server.server_address[1])
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    _plant_secrets(folder)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}"
    commands = (
        f"grep -rn KEYBODY {folder}",
        f"rg KEYBODY {folder}",
        "cat *",
    )
    try:
        for yolo in (False, True):
            client.post(f"{url}/yolo", json={"on": yolo})
            for command in commands:
                Handler.payloads = []
                Handler.scripts = [
                    _tool_sse("terminal", {"command": command}, "call_read"),
                    [
                        'data: {"choices":[{"delta":{"content":"Search finished."}}]}',
                        "data: [DONE]",
                    ],
                ]
                with client.stream(
                    "POST",
                    f"{url}/messages",
                    json={"content": "Search the project for KEYBODY."},
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    assert response.status_code == 200, response.read()
                    response.read()
                stored = client.get(url).json()
                heard = _tool_text(Handler.payloads)
                blob = heard + json.dumps(stored["messages"])
                assert "KEYBODYXYZ" not in blob, (yolo, command, heard)
                assert "hunter2PASS" not in blob, (yolo, command)
                assert "was not run" in heard or "waits for you" in heard or stored["run"]["status"] == "paused", (
                    yolo,
                    command,
                    heard,
                )
    finally:
        client.close()
        server.shutdown()
