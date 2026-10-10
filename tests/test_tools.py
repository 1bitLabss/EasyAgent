"""File, shell, and saved-computer tools run. A printed function call is not the answer."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app


class Recorder:
    def __init__(self, reply):
        self.reply = reply
        self.seen = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.seen.append(messages)
        reply = self.reply
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else "ack"
        return reply(messages) if callable(reply) else reply


def _written_page() -> str:
    """The page path the last turn actually used. A bot turn writes its own workspace."""
    from easyagent.paths import deliverable_file
    from easyagent.tools import _LAST_DEFAULT_FOLDER, _join_stored

    if _LAST_DEFAULT_FOLDER:
        return _join_stored(_LAST_DEFAULT_FOLDER, "landing.html")
    return deliverable_file("landing.html")


def _page_closer(name: str) -> str:
    return f"The page is {name}. It is at {_written_page()}, and you can open it."


def _redirect_writes(folder: Path):
    """Send the default deliverable folder, and a C:\\work path, into a test folder."""
    from easyagent.paths import default_deliverable_dir

    root = str(default_deliverable_dir()).replace("\\", "/").rstrip("/").lower()

    def placed(raw):
        text = (raw or "").strip().strip('"')
        folded = text.replace("/", "\\")
        lower = folded.lower()
        if lower.startswith("c:\\work\\") or lower == "c:\\work":
            name = folded.rstrip("\\").split("\\")[-1]
            return folder if name.lower() == "work" else folder / name
        norm = text.replace("\\", "/").rstrip("/")
        if norm.lower() == root or norm.lower().startswith(root + "/"):
            name = norm.split("/")[-1]
            return folder if norm.lower() == root else folder / name
        lowered = norm.lower()
        if "/bots/" in lowered and "/workspace" in lowered:
            name = norm.split("/")[-1]
            return folder if name.lower() == "workspace" else folder / name
        return Path(text)

    return placed


def _tool_blob(messages):
    return "\n".join(item.get("content") or "" for item in messages if item.get("role") == "tool")


def _world(tmp_path, monkeypatch, reply="ack"):
    recorder = Recorder(reply)
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "keep-this-line"},
    )
    assert sent.status_code == 200, sent.text
    return client, bot, chat, recorder


def _chat_bytes(root: Path, bot_id: str) -> dict[str, bytes]:
    folder = root / "bots" / bot_id / "chats"
    return {path.name: path.read_bytes() for path in sorted(folder.glob("*.json"))}


def test_printed_function_call_lists_the_folder(tmp_path, monkeypatch):
    folder = tmp_path.parent / "ea-listed"
    folder.mkdir(exist_ok=True)
    (folder / "notes.txt").write_text("hello", encoding="utf-8")
    reply = (
        "<function_calls>\n"
        "<invoke name=\"computer\">\n"
        "<parameter name=\"action\">list</parameter>\n"
        f"<parameter name=\"path\">{folder}</parameter>\n"
        "</invoke>\n"
        "</function_calls>"
    )
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [reply, "Yes. notes.txt is in that folder."]
    again = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "What is in the folder?"},
    )
    assert again.status_code == 200, again.text
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert stored["messages"][0]["content"] == "keep-this-line"
    answer = stored["messages"][-1]["content"]
    assert f"Listed {folder}." not in answer
    assert "Yes. notes.txt is in that folder." in answer
    assert f"Listed {folder}\nnotes.txt" not in answer
    assert "function_calls" not in answer
    assert "<invoke" not in answer
    raw = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert "function_calls" not in raw
    prompt = "\n".join(item.get("content") or "" for item in recorder.seen[-1])
    assert "notes.txt" in prompt
    assert "Do not paste" in prompt
    assert any(item.get("role") == "tool" for item in recorder.seen[-1])


def test_files_fence_reads_and_writes_without_touching_chats(tmp_path, monkeypatch):
    folder = tmp_path.parent / "ea-files"
    folder.mkdir(exist_ok=True)
    target = folder / "made.txt"
    if target.exists():
        target.unlink()
    before = None
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    recorder = Recorder([
        f"```files\nwrite\n{target}\nhello from the tool\n```",
        "Saved it.",
    ])
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    written = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "please write"},
    )
    assert written.status_code == 200, written.text
    assert target.read_text(encoding="utf-8") == "hello from the tool"
    assert "```files" not in written.text
    assert "hello from the tool" not in written.json()["chat"]["messages"][-1]["content"]
    assert f"Wrote {target}." not in written.json()["chat"]["messages"][-1]["content"]
    assert "made.txt" in written.json()["chat"]["messages"][-1]["content"]
    before = _chat_bytes(tmp_path, bot["id"])
    recorder.reply = [
        f"```files\nread\n{target}\n```",
        "The file is in place.",
    ]
    read = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "please read"},
    )
    assert read.status_code == 200, read.text
    last = read.json()["chat"]["messages"][-1]["content"]
    assert f"Read {target}." not in last
    assert "The file is in place." in last
    assert "hello from the tool" in last
    assert "```files" not in read.text
    assert "hello from the tool" in _tool_blob(recorder.seen[-1])


def test_missing_folder_stays_in_the_chat_as_an_error(tmp_path, monkeypatch):
    missing = tmp_path.parent / "ea-does-not-exist"
    reply = f"```files\nlist\n{missing}\n```"
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch, reply)
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert stored["messages"][0]["content"] == "keep-this-line"
    assert stored["messages"][1].get("error") is not True
    assert "not there" not in stored["messages"][1]["content"]
    assert "```files" not in stored["messages"][1]["content"]
    tool_msgs = [item for item in _recorder.seen[-1] if item.get("role") == "tool"]
    assert tool_msgs and "not there" in tool_msgs[-1]["content"]


def test_shell_output_stays_out_of_the_chat(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = ["```shell\necho SHELL-MARK-91\n```", "The command finished."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "run it"},
    )
    assert sent.status_code == 200, sent.text
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    answer = stored["messages"][-1]["content"]
    assert "Ran a command" not in answer
    assert "The command finished." in answer
    assert "SHELL-MARK-91" in answer
    assert "```shell" not in answer
    assert "SHELL-MARK-91" in _tool_blob(recorder.seen[-1])


def test_saved_computers_do_not_change_chats_and_commands_return_output(tmp_path, monkeypatch):
    calls = {}

    def fake_ssh(computer, command):
        calls["ssh"] = (computer["name"], computer["host"], command)
        return "SSH-MARK"

    def fake_windows(computer, command):
        calls["windows"] = (computer["name"], computer["host"], command)
        return "WIN-MARK"

    monkeypatch.setattr("easyagent.tools.run_ssh", fake_ssh)
    monkeypatch.setattr("easyagent.tools.run_windows", fake_windows)
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    before = _chat_bytes(tmp_path, bot["id"])
    linux = client.post(
        "/api/computers",
        json={"name": "Workshop", "kind": "linux", "host": "workshop.internal", "user": "ada", "password": "secret-pass"},
    )
    assert linux.status_code == 200, linux.text
    assert "secret-pass" not in linux.text
    assert linux.json()["has_sign_in"] is True
    assert "user" not in linux.json()
    windows = client.post(
        "/api/computers",
        json={"name": "Desk", "kind": "windows", "host": "desk.internal", "user": "ada", "password": "win-pass"},
    )
    assert windows.status_code == 200, windows.text
    assert "win-pass" not in windows.text
    assert _chat_bytes(tmp_path, bot["id"]) == before
    shown = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert shown["messages"][0]["content"] == "keep-this-line"
    raw_secret = (tmp_path / "computers.json").read_text(encoding="utf-8")
    assert "secret-pass" not in raw_secret
    assert "win-pass" not in raw_secret
    assert '"user"' not in raw_secret
    listed = client.get("/api/computers").json()
    assert {item["name"] for item in listed} == {"Workshop", "Desk"}
    assert "secret-pass" not in client.get("/api/computers").text

    recorder = Recorder(["```ssh\nWorkshop\nuname\n```", "The linux computer answered."])
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    ssh = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "ask the linux box"},
    )
    assert ssh.status_code == 200, ssh.text
    ssh_text = ssh.json()["chat"]["messages"][-1]["content"]
    assert "Ran a command" not in ssh_text
    assert "The linux computer answered." in ssh_text
    assert "SSH-MARK" in ssh_text
    assert "SSH-MARK" in _tool_blob(recorder.seen[-1])
    assert "```ssh" not in ssh.text
    assert calls["ssh"][0] == "Workshop"
    assert calls["ssh"][2] == "uname"

    recorder.reply = ["```windows\nDesk\nhostname\n```", "The windows computer answered."]
    win = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "ask the windows box"},
    )
    assert win.status_code == 200, win.text
    win_text = win.json()["chat"]["messages"][-1]["content"]
    assert "Ran a command" not in win_text
    assert "WIN-MARK" in win_text
    assert "WIN-MARK" in _tool_blob(recorder.seen[-1])
    assert calls["windows"][0] == "Desk"
    assert "keep-this-line" in client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").text


def test_page_lists_tools_and_skills_in_plain_words(tmp_path, monkeypatch):
    monkeypatch.setattr("easyagent.llm.complete", Recorder("ack"))
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "keep-this-line"},
    )
    before = _chat_bytes(tmp_path, bot["id"])
    page = client.get("/classic")
    assert page.status_code == 200
    assert "Files on this computer" in page.text
    assert "A command on this computer" in page.text
    assert "Web search on this computer" in page.text
    assert "Linux computer you saved" in page.text
    assert "Windows computer you saved" in page.text
    assert "Add one in markdown" in page.text
    saved = client.post(
        "/api/skills",
        json={"name": "desk-notes", "description": "How this desk likes answers", "body": "Keep answers short."},
    )
    assert saved.status_code == 200, saved.text
    listed = client.get("/api/skills").json()
    assert listed[0]["name"] == "desk-notes"
    assert listed[0]["description"] == "How this desk likes answers"
    assert "Keep answers short." in listed[0]["body"]
    assert _chat_bytes(tmp_path, bot["id"]) == before
    again = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert again["messages"][0]["content"] == "keep-this-line"


def test_unknown_function_call_is_an_error_not_the_block(tmp_path, monkeypatch):
    reply = "<function_calls><invoke name=\"browser\"><parameter name=\"url\">https://example.com</parameter></invoke></function_calls>"
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch, reply)
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert stored["messages"][1].get("error") is not True
    assert "function_calls" not in stored["messages"][1]["content"]
    assert stored["messages"][0]["content"] == "keep-this-line"
    assert len(_recorder.seen) >= 2


def test_long_directory_listing_does_not_appear_in_the_chat(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-long-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    (folder / "keep-me.txt").write_text("the one file", encoding="utf-8")
    for index in range(220):
        (folder / f"noise-{index:03d}.tmp").write_text("x", encoding="utf-8")
    fence = f"```files\nlist\n{folder}\n```"
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [fence, "Yes."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "ok can you see keep-me.txt now?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    raw = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert f"Listed {folder}." not in answer
    assert "Yes." in answer
    assert "noise-000.tmp" not in answer
    assert "noise-150.tmp" not in answer
    assert "noise-000.tmp" not in raw
    assert "noise-150.tmp" not in raw
    assert len(answer) < 400
    prompt = _tool_blob(recorder.seen[-1])
    assert "keep-me.txt is in" in prompt
    assert "noise-000.tmp" not in prompt
    assert "noise-150.tmp" not in prompt

    recorder.reply = [fence, "There are many temporary files."]
    listed = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "List everything in that folder."},
    )
    assert listed.status_code == 200, listed.text
    listing_answer = listed.json()["chat"]["messages"][-1]["content"]
    assert f"Listed {folder}." not in listing_answer
    assert "There are many temporary files." in listing_answer
    assert "noise-000.tmp" not in listing_answer
    assert "noise-150.tmp" not in listing_answer
    listing_prompt = _tool_blob(recorder.seen[-1])
    assert "more names are not included" in listing_prompt
    assert "noise-150.tmp" not in listing_prompt
    saved = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert "noise-150.tmp" not in saved


def test_a_finished_answer_that_mentions_a_later_check_is_kept(tmp_path, monkeypatch):
    from easyagent.tools import _is_announcement

    prose = (
        "The kiln cooled overnight and the glaze is even across the whole shelf. "
        "The batch is usable as it stands. I will check the next firing later."
    )
    assert not _is_announcement(prose)
    assert _is_announcement("I will check the next firing later.")
    assert _is_announcement("Let me check the file sizes for you.")
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [prose, "This second reply should not replace the answer."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "How did the firing go?"},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["chat"]["messages"][-1]["content"] == prose
    assert len(recorder.seen) == before + 1


def test_an_announcement_is_not_the_answer_and_the_largest_file_is(tmp_path, monkeypatch):
    from easyagent.tools import _implied_tool, _is_announcement

    assert _is_announcement("Let me check the file sizes for you.")
    assert not _is_announcement("big.bin is 50 bytes.")
    implied = _implied_tool(
        "Let me check the file sizes for you.",
        [{"role": "user", "content": "the filename and size of the largest file in C:\\work"}],
    )
    assert implied is not None
    assert implied.kind == "files"
    assert implied.action == "list"
    assert implied.path == "C:\\work"

    folder = tmp_path.parent / f"ea-sizes-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    (folder / "small-a.txt").write_bytes(b"aa")
    (folder / "small-b.txt").write_bytes(b"bbb")
    (folder / "big.bin").write_bytes(b"x" * 50)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "Let me check the file sizes for you.",
        "big.bin is 50 bytes.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"can you give me the filename and size of the largest file in {folder}?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert f"Checked file sizes in {folder}." not in answer
    assert "big.bin" in answer
    assert "50" in answer
    assert "Let me check" not in answer
    assert "small-a.txt" not in answer
    assert "small-b.txt" not in answer
    assert len(recorder.seen) == before + 2
    prompt = _tool_blob(recorder.seen[-1])
    assert "The largest file" in prompt
    assert "big.bin" in prompt
    assert "50" in prompt
    assert "small-a.txt" not in prompt
    assert "small-b.txt" not in prompt
    raw = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert "small-a.txt" not in raw
    assert "small-b.txt" not in raw
    assert "Let me check" not in raw


def test_a_turn_keeps_going_from_a_list_into_a_read(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-steps-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    target = folder / "notes.txt"
    target.write_text("hello from the tool", encoding="utf-8")
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        f"```files\nlist\n{folder}\n```",
        f"```files\nread\n{target}\n```",
        "The file says hello.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Look in that folder and then read notes.txt."},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert f"Listed {folder}." not in answer
    assert f"Read {target}." not in answer
    assert "The file says hello." in answer
    assert "hello from the tool" not in answer
    assert "```files" not in answer
    assert len(recorder.seen) == before + 3
    assert "hello from the tool" in _tool_blob(recorder.seen[-1])
    raw = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert "hello from the tool" not in raw


def test_the_same_tool_again_is_stuck_and_says_what_is_undone(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-stuck-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    (folder / "noise-cap.tmp").write_text("x", encoding="utf-8")
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = f"```files\nlist\n{folder}\n```"
    ask = "Please list the folder and then tell me if it is empty."
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": ask},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert len(recorder.seen) == before + 2
    assert "Stuck." in answer
    assert "Still undone:" in answer
    assert "Please list the folder" in answer
    assert "same tool" in answer
    assert "8 steps" not in answer
    assert "noise-cap.tmp" not in answer
    raw = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert "noise-cap.tmp" not in raw
    assert "Stuck." in raw


def test_a_long_task_keeps_going_past_eight_steps(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-many-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    fences = []
    for index in range(9):
        path = folder / f"part-{index}.txt"
        path.write_text(f"body-{index}", encoding="utf-8")
        fences.append(f"```files\nread\n{path}\n```")
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [*fences, "All nine are in place."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Read each part and then tell me they are in place."},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert len(recorder.seen) == before + 10
    assert "Read " not in answer
    assert "All nine are in place." in answer
    assert "Stuck" not in answer
    assert "8 steps" not in answer
    assert "body-0" not in answer
    assert "body-8" not in answer
    prompt = "\n".join(
        item["content"] for item in recorder.seen[-1] if item["role"] in {"user", "tool"}
    )
    assert "body-0" in prompt
    assert "body-8" in prompt


def test_a_long_task_is_not_cut_off_by_a_step_count(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-longtask-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    fences = []
    for index in range(30):
        path = folder / f"part-{index}.txt"
        path.write_text(f"body-{index}", encoding="utf-8")
        fences.append(f"```files\nread\n{path}\n```")
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [*fences, "All thirty are in place."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Read each part and then tell me they are in place."},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert len(recorder.seen) == before + 31
    assert "Read " not in answer
    assert "All thirty are in place." in answer
    assert "Stuck" not in answer
    assert "Stopped after" not in answer
    assert "8 steps" not in answer
    assert "body-0" not in answer
    assert "body-29" not in answer


def test_listing_again_after_a_write_is_progress(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-again-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    (folder / "old.txt").write_text("old", encoding="utf-8")
    target = folder / "made.txt"
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        f"```files\nlist\n{folder}\n```",
        f"```files\nwrite\n{target}\nhello from the tool\n```",
        f"```files\nlist\n{folder}\n```",
        f"```files\nread\n{target}\n```",
        "made.txt is there now.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Add a file and then look again."},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert target.read_text(encoding="utf-8") == "hello from the tool"
    assert len(recorder.seen) == before + 5
    assert f"Listed {folder}." not in answer
    assert f"Wrote {target}." not in answer
    assert "made.txt is there now." in answer
    assert "Stuck" not in answer
    assert "hello from the tool" not in answer


def test_the_same_result_again_makes_no_progress(tmp_path, monkeypatch):
    folder_a = tmp_path.parent / f"ea-same-a-{tmp_path.name}"
    folder_b = tmp_path.parent / f"ea-same-b-{tmp_path.name}"
    folder_a.mkdir(exist_ok=True)
    folder_b.mkdir(exist_ok=True)
    (folder_a / "a.txt").write_text("a", encoding="utf-8")
    (folder_b / "b.txt").write_text("b", encoding="utf-8")
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        f"```files\nlist\n{folder_a}\n```",
        f"```files\nlist\n{folder_b}\n```",
        f"```files\nlist\n{folder_a}\n```",
        "this answer must not be saved",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Compare the two folders."},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert len(recorder.seen) == before + 3
    assert "Stuck." in answer
    assert "Still undone:" in answer
    assert "Compare the two folders." in answer
    assert "no progress" in answer
    assert "this answer must not be saved" not in answer
    assert "a.txt" not in answer
    assert "b.txt" not in answer


def test_the_same_search_again_is_stuck(tmp_path, monkeypatch):
    async def search(query):
        assert query == "Paris France"
        return "SNIPPET-REPEAT"

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "```search\nParis France\n```",
        "```search\nParis France\n```",
        "should not be saved",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "What is the capital of France?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert len(recorder.seen) == before + 2
    assert "Stuck." in answer
    assert "Still undone:" in answer
    assert "same tool" in answer
    assert "SNIPPET-REPEAT" not in answer
    assert "should not be saved" not in answer


def test_a_second_announcement_with_no_tool_says_what_is_undone(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "Let me check that for you.",
        "I'll look again.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "What is still left to do?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert len(recorder.seen) == before + 2
    assert "Stuck." in answer
    assert "Still undone:" in answer
    assert "What is still left to do?" in answer
    assert "announced more work" in answer
    assert "Let me check" not in answer
    assert "8 steps" not in answer


def test_stream_stays_open_until_the_largest_file_is_answered(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-stream-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    (folder / "small-a.txt").write_bytes(b"aa")
    (folder / "big.bin").write_bytes(b"x" * 50)

    class Pieces:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = 0

        async def __call__(self, **kwargs):
            self.calls += 1
            text = self.replies.pop(0) if self.replies else "ack"
            yield text

    pieces = Pieces([
        "Let me check the file sizes for you.",
        "big.bin is 50 bytes.",
    ])
    monkeypatch.setattr("easyagent.llm.stream_complete", pieces)
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": f"filename and size of the largest file in {folder}?"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    assert pieces.calls == 2
    assert "Let me check" not in body
    assert "Checked file sizes" not in body
    assert "small-a.txt" not in body
    thinking_at = body.index('"text": "Thinking"')
    answer_at = body.index("big.bin is 50 bytes.")
    done_at = body.index('"type": "done"')
    assert thinking_at < answer_at < done_at
    stored = client.get(url.replace("/messages", "")).json()
    answer = stored["messages"][-1]["content"]
    assert f"Checked file sizes in {folder}." not in answer
    assert "big.bin is 50 bytes." in answer
    assert "Let me check" not in answer


def test_needed_tool_covers_write_read_list_command_and_news():
    from easyagent.tools import _needed_tool

    write = _needed_tool([
        {"role": "user", "content": "can you write a test file into /tmp/work-notes and name it whatever you want"},
    ])
    assert write is not None
    assert write.kind == "files"
    assert write.action == "write"
    assert write.path == "/tmp/work-notes/test-file.txt"
    assert write.body == "test file\n"

    read = _needed_tool([{"role": "user", "content": "read notes.txt in /tmp/work-notes"}])
    assert read is not None
    assert read.action == "read"
    assert read.path == "/tmp/work-notes/notes.txt"

    listed = _needed_tool([{"role": "user", "content": "list the folder /tmp/work-notes"}])
    assert listed is not None
    assert listed.action == "list"
    assert listed.path == "/tmp/work-notes"

    command = _needed_tool([{"role": "user", "content": "run the command `printf ok`"}])
    assert command is not None
    assert command.kind == "shell"
    assert command.command == "printf ok"

    news = _needed_tool([{"role": "user", "content": "what's the top story on CNN right now?"}])
    assert news is not None
    assert news.kind == "search"
    assert news.body == "top story on CNN right now"


def test_empty_model_reply_still_writes_the_file(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-empty-write-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    target = folder / "test-file.txt"
    if target.exists():
        target.unlink()
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = ["", "Saved it."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"can you write a test file into {folder} write whatever you want and name it whatever you want"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert target.read_text(encoding="utf-8") == "test file\n"
    assert f"Wrote {target}." not in answer
    assert f"The file is test-file.txt in {folder}." in answer or str(target) in answer
    assert "Saved it." not in answer
    assert "Endpoint returned an empty message" not in answer
    assert sent.json()["chat"]["messages"][-1].get("error") is not True
    assert len(recorder.seen) == before + 4
    nudge = "\n".join(item.get("content") or "" for batch in recorder.seen[before:] for item in batch)
    assert "Read that file back" in nudge
    assert str(target) in nudge


def test_empty_model_reply_on_the_news_still_searches(tmp_path, monkeypatch):
    queries = []

    async def search(query):
        queries.append(query)
        return (
            "1. Harbor storm\n"
            "https://www.cnn.com/2026/10/04/harbor-storm\n"
            "A storm closed the harbor this morning and boats stayed in.\n"
        )

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = ["Endpoint returned an empty message.", "Endpoint returned an empty message."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what's the top story on CNN right now?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert queries == ["top story on CNN right now"]
    assert "Searched the web." not in answer
    assert "A storm closed the harbor this morning and boats stayed in." in answer
    assert "https://www.cnn.com" not in answer
    assert "Endpoint returned an empty message" not in answer
    assert sent.json()["chat"]["messages"][-1].get("error") is not True
    assert len(recorder.seen) == before + 2


def test_saying_it_will_look_up_the_news_still_searches(tmp_path, monkeypatch):
    queries = []

    async def search(query):
        queries.append(query)
        return (
            "1. Harbor storm\n"
            "https://www.cnn.com/2026/10/04/harbor-storm\n"
            "A storm closed the harbor this morning and boats stayed in.\n"
            "RAW-PAGE-MARKER\n"
        )

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        "I'll look that up.",
        "https://www.cnn.com/2026/10/04/harbor-storm\nRAW-PAGE-MARKER",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what's the top story on CNN right now?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert queries == ["top story on CNN right now"]
    assert "Searched the web." not in answer
    assert "A storm closed the harbor this morning and boats stayed in." in answer
    assert "I'll look that up" not in answer
    assert "https://www.cnn.com" not in answer
    assert "RAW-PAGE-MARKER" not in answer


def test_an_empty_reply_with_no_tool_names_what_is_undone(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = ["", ""]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello there"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "The model returned nothing" in answer
    assert "hello there" in answer
    assert "Endpoint returned an empty message" not in answer
    assert answer != "Endpoint returned an empty message."
    assert len(recorder.seen) == before + 2


def test_a_claimed_save_without_a_file_is_the_write_error():
    from easyagent.tools import _prefer_write_failure

    fails = [{"kind": "files", "action": "write", "result": "Write failed: denied. Path: made.txt."}]
    assert _prefer_write_failure("Saved it.", [], fails) == fails[0]["result"]
    assert _prefer_write_failure("Done.", [], fails) == "Done."
    assert _prefer_write_failure("Saved it.", ["Wrote made.txt."], fails) == "Saved it."
    assert "not written" in _prefer_write_failure("Saved it.", [], []).lower()
    assert _prefer_write_failure("The file is in place.", [], []) == "The file is in place."


def test_an_inferred_write_that_fails_names_the_tool(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = ["", "Saved it."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"write a test file into {tmp_path} and name it whatever you want"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "Write failed:" in answer
    assert "I'll write a test file" not in answer
    assert "Goal:" not in answer
    assert "saved chats" in answer
    assert "Path:" in answer
    assert "Endpoint returned an empty message" not in answer
    assert "attachment" not in sent.json()["chat"]["messages"][-1]
    assert not (tmp_path / "test-file.txt").exists()


def test_a_prose_claim_writes_the_named_file(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-claim-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    target = folder / "bot1-easyagent-test.txt"
    if target.exists():
        target.unlink()
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [f"Created bot1-easyagent-test.txt in {folder}.", "Done."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"can you create a new random text file in {folder}"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert target.is_file()
    assert target.stat().st_size > 0
    assert target.read_text(encoding="utf-8").strip()
    assert "bot1-easyagent-test.txt" in answer
    assert str(folder) in answer
    assert "not written" not in answer.lower()
    assert "Done." not in answer
    assert len(recorder.seen) == before + 4


def test_a_prose_claim_without_a_real_file_says_it_was_not_written(tmp_path, monkeypatch):
    claim = "Created bot1-easyagent-test.txt in C:\\work."
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = claim
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you create a new random text file in C:\\work"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    target = Path(r"C:\work\bot1-easyagent-test.txt")
    if target.is_file() and target.stat().st_size > 0:
        assert "bot1-easyagent-test.txt" in answer
        assert "C:\\work" in answer or "C:/work" in answer
    else:
        assert "not written" in answer.lower()
        assert claim not in answer
        assert "Created bot1-easyagent-test.txt" not in answer


def test_a_prose_headline_searches_and_does_not_invent_one(tmp_path, monkeypatch):
    queries = []

    async def search(query):
        queries.append(query)
        return "The search found nothing."

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "The top story is a made-up hurricane.",
        "I don't have a live headline for that.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what is the top story on CNN right now?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert queries[0] == "top story on CNN right now"
    assert len(queries) == 2
    assert queries[1] != queries[0]
    assert "Searched the web." not in answer
    assert answer.strip() != "The search found nothing."
    assert "The search found nothing." not in answer
    assert "I don't have a live headline for that." in answer
    assert "made-up hurricane" not in answer
    assert "Web search failed" not in answer
    follow = recorder.seen[before + 1]
    assert any(
        item.get("role") == "tool" and "The search found nothing." in (item.get("content") or "")
        for item in follow
    )


def test_a_failed_search_is_told_to_the_model_and_not_shown_as_the_reply(tmp_path, monkeypatch):
    from easyagent.search import SearchError

    queries = []

    async def search(query):
        queries.append(query)
        raise SearchError("Search failed: the search provider timed out.")

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "The top story is a made-up hurricane.",
        "The lookup timed out, so I can't quote a headline.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what is the top story on CNN right now?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert queries == ["top story on CNN right now"]
    assert "The lookup timed out, so I can't quote a headline." in answer
    assert "made-up hurricane" not in answer
    assert "Search failed" not in answer
    assert "Web search failed" not in answer
    assert answer.strip() != "The search found nothing."
    follow = recorder.seen[before + 1]
    assert any(
        item.get("role") == "tool" and "timed out" in (item.get("content") or "")
        for item in follow
    )


def test_a_tagged_tool_call_writes_the_file_and_a_native_object_becomes_a_fence(tmp_path, monkeypatch):
    from easyagent.llm import calls_to_fence

    folder = tmp_path.parent / f"ea-tag-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    target = folder / "from-the-tag.txt"
    if target.exists():
        target.unlink()
    payload = json.dumps({"name": "write_file", "arguments": {"path": str(target), "content": "from the tag"}})
    tag = f"<tool_call>\n{payload}\n</tool_call>"
    listed = calls_to_fence([{"name": "list_dir", "arguments": {"path": str(folder)}}])
    assert listed == f"```files\nlist\n{folder}\n```"
    shell = calls_to_fence([{"name": "terminal", "arguments": {"command": "printf ok"}}])
    assert shell == "```shell\nprintf ok\n```"
    search = calls_to_fence([{"name": "web_search", "arguments": {"query": "top story on CNN"}}])
    assert search == "```search\ntop story on CNN\n```"
    native = calls_to_fence([{
        "name": "write_file",
        "arguments": {"path": str(target), "content": "from the object"},
    }])
    assert native.startswith("```files\nwrite\n")
    assert "Created" not in native

    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [tag, "Done."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "please write that file"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert target.read_text(encoding="utf-8") == "from the tag"
    assert target.stat().st_size > 0
    assert f"Wrote {target}." not in answer
    assert "<tool_call>" not in answer
    assert "from the tag" not in answer


def test_a_qwen_tag_and_a_bare_command_object_run(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-qwen-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    target = folder / "from-qwen.txt"
    tag = (
        "<tool_call>\n"
        "<function=write_file>\n"
        f"<parameter=path>\n{target}\n</parameter>\n"
        "<parameter=content>\nfrom the qwen tag\n</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [tag, "Done."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "write it"},
    )
    assert sent.status_code == 200, sent.text
    assert target.read_text(encoding="utf-8") == "from the qwen tag"
    assert "<function=" not in sent.json()["chat"]["messages"][-1]["content"]

    recorder.reply = ['{"name":"terminal","arguments":{"command":"echo NOTE-77"}}', "The command finished."]
    ran = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "run the command"},
    )
    assert ran.status_code == 200, ran.text
    answer = ran.json()["chat"]["messages"][-1]["content"]
    assert "Ran a command" not in answer
    assert "NOTE-77" in answer
    assert '"name"' not in answer


def test_a_search_tag_runs_and_a_cut_off_tag_is_not_the_answer(tmp_path, monkeypatch):
    queries = []

    async def search(query):
        queries.append(query)
        return "1. Harbor storm\nhttps://www.cnn.com/2026/10/04/harbor\nA storm closed the harbor this morning.\n"

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        '<tool_call>\n{"name":"web_search","arguments":{"query":"top story on CNN right now"}}\n</tool_call>',
        "A storm closed the harbor this morning.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what is the top story on CNN right now?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert queries == ["top story on CNN right now"]
    assert "Searched the web." not in answer
    assert "A storm closed the harbor this morning." in answer
    assert "<tool_call>" not in answer
    assert "https://www.cnn.com" not in answer

    folder = tmp_path.parent / f"ea-cut-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    target = folder / "finished.txt"
    payload = json.dumps({"name": "write_file", "arguments": {"path": str(target), "content": "finished call"}})
    recorder.reply = [
        '<tool_call>\n{"name": "write_file"',
        f"<tool_call>\n{payload}\n</tool_call>",
        "Done.",
    ]
    cut = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello there"},
    )
    assert cut.status_code == 200, cut.text
    finished = cut.json()["chat"]["messages"][-1]["content"]
    assert target.read_text(encoding="utf-8") == "finished call"
    assert "<tool_call>" not in finished
    assert "cut off" not in finished.lower()


def test_a_transient_endpoint_error_is_tried_once(tmp_path, monkeypatch):
    from easyagent.llm import ProviderError

    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    state = {"n": 0}

    async def complete(**kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise ProviderError("503 from http://127.0.0.1:9/v1: busy")
        return "Still here."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello there"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert answer == "Still here."
    assert state["n"] == 2
    assert sent.json()["chat"]["messages"][-1].get("error") is not True

    state["n"] = 0

    async def rejected(**kwargs):
        state["n"] += 1
        raise ProviderError("400 from http://127.0.0.1:9/v1: bad")

    monkeypatch.setattr("easyagent.llm.complete", rejected)
    failed = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello again"},
    )
    assert failed.status_code == 200, failed.text
    message = failed.json()["chat"]["messages"][-1]
    assert message["error"] is True
    assert "400" in message["content"]
    assert state["n"] == 1


def test_a_smashed_path_writes_a_real_file_and_a_missing_file_is_written_again(tmp_path, monkeypatch):
    from easyagent.tools import _clean_write_target

    sample = "just something random with some sample text"
    cleaned, body = _clean_write_target(
        r"C:\work just something random with some sample text/test-file.txt",
        "",
    )
    assert cleaned == r"C:\work\test-file.txt"
    assert sample in body
    long_path = (
        r"C:\Users\ada\AppData\Local\Temp\ea-winwrite\nested "
        + sample
        + "/test-file.txt"
    )
    long_cleaned, long_body = _clean_write_target(long_path, "")
    assert long_cleaned == r"C:\Users\ada\AppData\Local\Temp\ea-winwrite\nested\test-file.txt"
    assert long_body == sample + "\n"

    folder = tmp_path.parent / f"ea-winwrite-{tmp_path.name}" / "nested"
    assert not folder.exists()
    target = folder / "test-file.txt"
    smashed = f"{folder} {sample}/test-file.txt"
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [f"```files\nwrite\n{smashed}\n```", "Done."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"write a test file in {folder} {sample}"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert target.is_file()
    assert target.stat().st_size > 0
    assert target.read_text(encoding="utf-8") == sample + "\n"
    assert f"Wrote {target}." not in answer
    assert "test-file.txt" in answer
    assert str(folder) in answer
    assert smashed not in answer
    assert "not written" not in answer.lower()

    target.unlink()
    recorder.reply = ["I'll check the file.", "I'll check the file."]
    missing = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "I don't see any file there with that name"},
    )
    assert missing.status_code == 200, missing.text
    again = missing.json()["chat"]["messages"][-1]["content"]
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == sample + "\n"
    assert "Stuck" not in again
    assert "test-file.txt" in again
    assert str(folder) in again


def test_an_empty_reply_still_reads_the_file(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-empty-read-{tmp_path.name}"
    folder.mkdir()
    target = folder / "notes.txt"
    target.write_text("hello from the page\n", encoding="utf-8")
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = ["", ""]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"read notes.txt in {folder}"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert f"Read {target}." not in answer
    assert "hello from the page" in answer
    assert "Endpoint returned an empty message" not in answer


def test_ssh_top_shows_the_processes_and_a_complaint_does_not_run_it_again(tmp_path, monkeypatch):
    top = "\n".join([
        "top - 13:15:01 up 3 days,  1 user,  load average: 0.42, 0.30, 0.20",
        "Tasks: 98 total,   1 running,  97 sleeping,   0 stopped,   0 zombie",
        "%Cpu(s):  5.0 us,  1.2 sy,  0.0 ni, 93.0 id,  0.8 wa,  0.0 hi,  0.0 si,  0.0 st",
        "MiB Mem :   7950.2 total,   1024.0 free,   4096.0 used,   2830.2 buff/cache",
        "",
        "    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND",
        "   4312 ada    20   0  812344  120004  45612 S  18.7   1.5   2:14.08 python",
        "   2201 ada    20   0  221100   40112  12008 S   4.2   0.5   0:40.11 sshd",
    ])
    calls = {"n": 0, "command": ""}

    def fake_ssh(computer, command):
        calls["n"] += 1
        calls["command"] = command
        calls["name"] = computer["name"]
        return top

    monkeypatch.setattr("easyagent.tools.run_ssh", fake_ssh)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    saved = client.post(
        "/api/computers",
        json={"name": "desk", "kind": "linux", "host": "desk.example", "user": "ada", "password": "desk-pass"},
    )
    assert saved.status_code == 200, saved.text
    recorder.reply = ["", "The command finished."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you SSH to desk and run top and show me the results"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert calls["n"] == 1
    assert calls["name"] == "desk"
    assert calls["command"] == "top -b -n 1"
    assert "Ran a command" not in answer
    assert "python is the busiest process, 18.7% CPU and 1.5% memory." in answer
    assert "top -" not in answer
    assert "PID USER" not in answer
    assert "load average" not in answer
    assert "buff/cache" not in answer
    assert "TIME+" not in answer
    assert "desk-pass" not in answer
    prompt = _tool_blob(recorder.seen[-1])
    assert "load average" in prompt
    assert "18.7" in prompt
    raw = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert "desk-pass" not in raw
    assert "PID USER" not in raw

    recorder.reply = ["```ssh\ndesk\ntop\n```", "Ran a command on desk."]
    again = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "I see nothing"},
    )
    assert again.status_code == 200, again.text
    follow = again.json()["chat"]["messages"][-1]["content"]
    assert calls["n"] == 1
    assert "python is the busiest process, 18.7% CPU and 1.5% memory." in follow
    assert "top -" not in follow
    assert "PID USER" not in follow
    assert "Stuck" not in follow
    assert "desk-pass" not in follow


def test_a_top_summary_is_kept_and_the_raw_table_is_not(tmp_path, monkeypatch):
    top = "\n".join([
        "top - 14:20:19 up 24 days, 10:30,  2 users,  load average: 1.54, 1.20, 0.99",
        "Tasks: 425 total,   2 running, 423 sleeping,   0 stopped,   0 zombie",
        "%Cpu(s):  6.4 us,  0.0 sy,  0.0 ni, 93.6 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st",
        "MiB Mem : 128000.0 total,  34000.0 free,  86000.0 used,  90000.0 buff/cache",
        "",
        "    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND",
        "2617868 ada    20   0  999999  888888  11111 S 190.9  68.8   12:01.00 flash_s+",
        "   2201 ada    20   0  221100   40112  12008 S   0.4   0.2   0:40.11 sshd",
    ])
    calls = {"n": 0}

    def fake_ssh(computer, command):
        calls["n"] += 1
        return top

    monkeypatch.setattr("easyagent.tools.run_ssh", fake_ssh)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    saved = client.post(
        "/api/computers",
        json={"name": "desk", "kind": "linux", "host": "desk.example", "user": "ada", "password": "desk-pass"},
    )
    assert saved.status_code == 200, saved.text
    summary = "flash_s+ is using about 190.9% CPU and 68.8% RAM. The box is mostly idle."
    recorder.reply = ["", summary + "\n\n" + top]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "SSH to desk and run top"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert calls["n"] == 1
    assert "flash_s+" in answer
    assert "190.9" in answer
    assert "mostly idle" in answer
    assert "top -" not in answer
    assert "PID USER" not in answer
    assert "TIME+" not in answer
    assert "load average" not in answer
    assert "buff/cache" not in answer
    prompt = _tool_blob(recorder.seen[-1])
    assert "load average" in prompt
    assert "flash_s+" in prompt
    assert "PID USER" in prompt


def test_a_landing_page_without_a_folder_names_a_clear_path():
    from easyagent.tools import _deliverable

    from easyagent.paths import deliverable_file

    request = _deliverable([{"role": "user", "content": "can you make a sample landing page?"}])
    assert request is not None
    assert request.path == deliverable_file("landing.html")
    assert "<h1>Sample landing page</h1>" in request.body
    assert request.body.strip().endswith("</html>")


def test_a_bare_failure_retries_and_a_landing_page_gets_a_short_preview(tmp_path, monkeypatch):
    from easyagent.tools import ToolError, _is_raw_command_line, _write_path

    folder = tmp_path.parent / f"ea-page-{tmp_path.name}"
    target = folder / "landing.html"
    calls = {"n": 0}
    original = _write_path

    def flaky(path, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ToolError(f"The file was not written. {path}: disk full")
        return original(path, body)

    monkeypatch.setattr("easyagent.tools._write_path", flaky)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = ["The file was not written.", "The file was not written."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"can you make a sample landing page in {folder}"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    assert calls["n"] == 2
    assert target.is_file()
    assert target.stat().st_size > 0
    text = target.read_text(encoding="utf-8")
    assert "<h1>Sample landing page</h1>" in text
    assert "END-OF-PAGE-MARKER" in text
    assert answer.strip() != "The file was not written."
    assert "not written" not in answer.lower()
    assert "landing.html" in answer
    assert str(folder) in answer
    assert "you can open it" in answer.lower()
    assert "Proven" not in answer
    assert "C1" not in answer
    assert "Evidence" not in answer
    assert "disk full" not in answer.lower()
    preview = message["attachment"]
    assert preview["media_type"] == "text/html"
    assert preview["name"] == "landing.html"
    assert "Sample landing page" in preview["excerpt"]
    assert "END-OF-PAGE-MARKER" not in preview["excerpt"]
    assert len(preview["excerpt"]) < len(text)
    assert preview["path"].endswith("landing.html")
    opened = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{preview['id']}")
    assert opened.status_code == 200
    assert b"Sample landing page" in opened.content
    assert b"END-OF-PAGE-MARKER" in opened.content
    page = Path("easyagent/static/app.js").read_text(encoding="utf-8")
    assert 'class: "file-preview"' in page
    assert 'class: "file-link"' in page
    assert not _is_raw_command_line("Sample landing page")


def _designed_model_page(name: str, subject: str) -> str:
    """A page the model wrote. The harness does not supply this text."""
    if subject == "hotel":
        detail = """
<section class="stay">
<h2>Rooms</h2>
<p>A small hotel with a quiet room for a stay. One room above the water. The north room faces the morning light and has a desk by the window. The south room is for a longer stay, with a chair and a lamp. The east room is smaller and looks over the garden wall.</p>
<p><a href="mailto:stay@example">Reserve a room</a></p>
</section>
<section class="place">
<h2>The place</h2>
<p>The building sits on a short street that ends at the water. In the morning the town is quiet, and in the afternoon people walk past the door. Dinner is in the front room. Breakfast is bread, fruit, and coffee, and you can take it upstairs.</p>
</section>
"""
    else:
        detail = f"""
<section class="offer">
<h2>Today</h2>
<p>A {subject} on the corner, open from morning until the street goes quiet. The front counter faces the window. People stop in on the way to work and again in the afternoon. The back room is for sitting down, with a long table and a few chairs.</p>
<p><a href="mailto:stay@example">Reserve a seat</a></p>
</section>
<section class="place">
<h2>The place</h2>
<p>The shop is on a short street near the water. The door sticks in damp weather. On Saturdays the line reaches the corner, and on Sundays it is slower. You can stay as long as you like once you have a seat.</p>
</section>
"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name}</title>
<style>
  body {{ margin: 0; font-family: Georgia, serif; color: #1c1915; background: #f6f1e7; }}
  header.hero {{ display: grid; min-height: 70vh; padding: 48px; background: #1e3a4c; color: #fff; }}
  header.hero p {{ max-width: 36rem; font-size: 1.25rem; }}
  main {{ display: flex; gap: 24px; padding: 32px; }}
  section {{ flex: 1; padding: 24px; background: #fff; }}
  h1 {{ font-size: 3rem; margin: 0 0 12px; }}
  h2 {{ font-size: 1.4rem; }}
  a {{ display: inline-block; padding: 12px 18px; background: #1e3a4c; color: #fff; }}
  footer {{ padding: 24px 32px; }}
</style>
</head>
<body>
<header class="hero">
<svg width="160" height="100" viewBox="0 0 160 100" aria-hidden="true">
  <rect x="0" y="40" width="160" height="60" fill="#16324a"/>
  <circle cx="120" cy="28" r="14" fill="#f4e1c1"/>
  <path d="M20 70 L50 40 L80 70 Z" fill="#f6f1e7"/>
  <polygon points="30,78 40,60 50,78" fill="#c4a574"/>
</svg>
<h1>{name}</h1>
<p>On the water street, with the town at the door, and a light in the window after dark.</p>
</header>
<main>
{detail}
</main>
<footer><p>Walk in, or write ahead. The door is on the street.</p></footer>
</body>
</html>
"""


def _model_hotel_html(name: str) -> str:
    """A page the model wrote. The harness does not supply this text."""
    return _designed_model_page(name, "hotel")


def _shore_page() -> str:
    """A styled page with pictures. The sections share a tag, so a shape count can still call it one stack."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>The Meridian Bay Hotel &amp; Spa</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  nav { position: sticky; top: 0; display: flex; gap: 16px; }
  .hero { display: grid; min-height: 80vh; color: #f7f3ea;
    background: url("https://images.unsplash.com/photo-hero") center/cover; }
  .band { display: grid; grid-template-columns: 1fr 1fr; }
  .cards { display: flex; gap: 16px; }
</style>
</head>
<body>
<nav><a href="#stay">Stay</a><a href="#book">Book</a></nav>
<header class="hero" id="stay">
<h1>Where the ocean meets calm.</h1>
<p>A quiet shore, and a room that faces the water.</p>
</header>
<section>
<h2>The house</h2>
<p>The building sits above the bay. Morning light comes in off the water.</p>
<img src="https://images.unsplash.com/photo-about" alt="">
</section>
<section>
<h2>Rooms</h2>
<p>Three rooms. Each one has a window on the bay.</p>
<img src="https://images.unsplash.com/photo-room" alt="">
</section>
<section>
<h2>The day</h2>
<p>A pool, a walk, and breakfast in the front room.</p>
<img src="https://images.unsplash.com/photo-day" alt="">
</section>
<section id="book">
<h2>Reserve</h2>
<p><a href="mailto:stay@example">Book a room</a></p>
<img src="https://images.unsplash.com/photo-book" alt="">
</section>
<footer><p>The bay road.</p></footer>
</body>
</html>
"""


def _gradient_page() -> str:
    """Modern styling, and the pictures are CSS gradients. A gradient is not a picture."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Solstice Bay Hotel — A Seaside Stay in Cape Marlow</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  nav { position: sticky; top: 0; display: flex; gap: 16px; }
  .hero { display: grid; min-height: 70vh; color: #f7f3ea; background: linear-gradient(#143, #321); }
  .cards { display: flex; gap: 16px; }
  .card { background: linear-gradient(#ddd, #aaa); }
</style>
</head>
<body>
<nav><a href="#stay">Stay</a></nav>
<header class="hero" id="stay">
<h1>Where the day ends slowly, by the water.</h1>
<p>A quiet shore, and a room that faces the water.</p>
</header>
<section><h2>Rooms</h2><p>Three rooms for a stay. Each one looks over the bay.</p></section>
<section><h2>The day</h2><p>Breakfast in the front room, then a walk.</p></section>
<section><h2>Book</h2><p><a href="mailto:stay@example">Reserve a room</a></p></section>
<footer><p>The bay road.</p></footer>
</body>
</html>
"""


def test_ls_on_windows_does_not_prove_a_sample_hotel_page(tmp_path, monkeypatch):
    """The laptop run: ls failed, and a generic sample page was marked proven."""
    import sys

    from easyagent.loop import page_matches
    from easyagent.tools import ToolRequest, _landing_html, _prepare_request, _windows_rejects

    monkeypatch.setattr(sys, "platform", "win32")
    folder = tmp_path.parent / f"ea-hotel-{tmp_path.name}"
    folder.mkdir()
    target = folder / "landing.html"
    sample = _landing_html()
    target.write_text(sample, encoding="utf-8")
    assert "<h1>Sample landing page</h1>" in target.read_text(encoding="utf-8")
    assert page_matches(target.read_bytes(), "hotel") is False
    ask = f"can you build me a simple landing page for a hotel in {folder}"
    messages = [{"role": "user", "content": ask}]
    assert _windows_rejects("ls") == "ls"
    assert _windows_rejects("pwd") == "pwd"
    prepared = _prepare_request(ToolRequest(kind="shell", command="ls"), messages)
    assert prepared.kind == "shell"
    assert "Harbor Hotel" not in _landing_html("hotel")
    page = _model_hotel_html("Cedar House Hotel")
    assert page_matches(page.encode("utf-8"), "hotel") is True

    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        "```shell\nls\n```",
        f"```files\nwrite\n{target}\n{page}\n```",
        "The sample page is ready and C1 is proven.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": ask},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    text = target.read_text(encoding="utf-8")
    assert "<h1>Sample landing page</h1>" not in text
    assert page_matches(target.read_bytes(), "hotel") is True
    heading = re.search(r"(?is)<h1>\s*(.*?)\s*</h1>", text).group(1)
    heading = " ".join(re.sub(r"<[^>]+>", " ", heading).split())
    assert heading == "Cedar House Hotel"
    assert heading in text
    assert heading in answer
    assert "Harbor Hotel" not in text
    assert text != _landing_html("hotel")
    assert "is not recognized" not in answer
    assert "NOT FOUND" not in answer
    assert "Ran a command" not in answer
    assert "<!DOCTYPE" not in answer
    assert "<h1>" not in answer
    assert "Proven" not in answer
    assert "C1" not in answer
    assert "Evidence" not in answer
    assert "you can open it" in answer.lower()
    assert str(folder) in answer
    preview = message["attachment"]
    assert preview["name"] == "landing.html"
    assert "<!DOCTYPE" not in preview["excerpt"]
    assert "<html" not in preview["excerpt"].lower()
    assert "<h1" not in preview["excerpt"]
    assert "hotel" in preview["excerpt"].lower()
    assert "Sample landing page" not in preview["excerpt"]
    assert preview["path"].endswith("landing.html")
    opened = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{preview['id']}")
    assert opened.status_code == 200
    assert heading.encode("utf-8") in opened.content
    assert b"Sample landing page" not in opened.content


def test_a_failed_grep_comes_back_and_the_written_page_is_named(tmp_path, monkeypatch):
    """A Unix command on Windows is not the reply. The failure goes back, and the page sentence follows."""
    from easyagent.tools import ToolError, _DRAWN_PICTURE, _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>The Azure Bay Hotel &amp; Spa</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  .hero { display: grid; min-height: 70vh; color: #f7f3ea;
    background: url("https://images.unsplash.com/photo-bay") center/cover; }
  main { display: flex; gap: 16px; }
</style>
</head>
<body>
<header class="hero"><h1>Where the sea meets your stay.</h1><p>A room on the water.</p></header>
<main>
<section><h2>Rooms</h2><p>Three rooms for a stay, each with a window on the bay.</p></section>
<section><h2>Book</h2><p><a href="mailto:stay@example">Reserve a room</a></p></section>
</main>
</body>
</html>
"""
    folder = tmp_path.parent / f"ea-grep-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)

    def rejected(_store, _command):
        raise ToolError(
            "'grep' is not recognized as an internal or external command, "
            "operable program or batch file."
        )

    monkeypatch.setattr("easyagent.tools._run_shell", rejected)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "I'll build a single-file hotel landing page, write it to disk, "
        "then read it back to confirm it's there and looks right.\n"
        f"```files\nwrite\nindex.html\n{page}\n```",
        "Let me check whether the page has a real image in it before I call it done.\n"
        "```shell\ngrep -n unsplash C:\\work\\landing.html\n```",
        "The background addresses are in the file. I'm leaving it as it is.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    assert message.get("error") is not True
    first = answer.find("I'll build a single-file hotel landing page")
    second = answer.find("Let me check whether the page has a real image")
    third = answer.find("The background addresses are in the file.")
    close = answer.find(
        _page_closer("The Azure Bay Hotel & Spa")
    )
    assert first >= 0 and second > first and third > second and close > third
    assert answer.endswith(
        _page_closer("The Azure Bay Hotel & Spa")
    )
    assert "Where the sea meets your stay" not in answer
    spoken = answer.replace(_written_page(), "")
    assert "not recognized" not in spoken.lower()
    assert "operable program" not in spoken.lower()
    assert "grep" not in spoken.lower()
    assert "Ran a command" not in answer
    assert "I'll build a simple landing page for a hotel." not in answer
    text = (folder / "landing.html").read_text(encoding="utf-8")
    assert "The Azure Bay Hotel" in text
    assert "unsplash.com" in text
    assert _DRAWN_PICTURE not in text
    prompts = "\n".join(item.get("content") or "" for batch in recorder.seen[before:] for item in batch)
    assert "is not recognized" in prompts
    assert len(recorder.seen) == before + 3


def test_a_page_read_keeps_the_lower_half():
    """The hero is not the whole file. A page read is not cut off after 1200 characters."""
    from easyagent.tools import ToolRequest, model_slice

    html = "<!DOCTYPE html><html><body><h1>Hero</h1>" + ("<p>stay</p>" * 200) + "<p>LATE-ROOM-LINE</p></body></html>"
    assert len(html) > 1200
    shown = model_slice(None, ToolRequest(kind="files", action="read", path=r"C:\work\landing.html"), html, [])
    assert "LATE-ROOM-LINE" in shown
    assert "[result truncated]" not in shown
    notes = model_slice(None, ToolRequest(kind="files", action="read", path="notes.txt"), "x" * 2000, [])
    assert notes.endswith("[result truncated]")


def test_a_look_ahead_keeps_the_account_and_the_model_writes_the_note(tmp_path, monkeypatch):
    """Two sentences is the middle of the account. The rest of the file comes back. A fake note does not."""
    from easyagent.tools import _RESEARCH_CACHE, _still_thinking

    _RESEARCH_CACHE.clear()
    look = (
        "The page is on disk and reads back as valid HTML — a hero with a real Unsplash photo, "
        "a booking strip, room cards, and a footer. "
        "Let me look at the rest of the file to make sure the lower half is finished, not just the hero."
    )
    assert _still_thinking(look) is True
    assert _still_thinking("Nothing else is missing, so I'm leaving the file as it is.") is False

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    pad = "<!-- " + ("room " * 400) + " -->"
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Northwater Hotel — A Short Stay</title>
<style>
  body {{ margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }}
  .hero {{ display: grid; min-height: 70vh; color: #f7f3ea;
    background: url("https://images.unsplash.com/photo-bay") center/cover; }}
  main {{ display: flex; gap: 16px; }}
</style>
</head>
<body>
<header class="hero"><h1>The tide is at the door.</h1><p>A room on the water.</p></header>
{pad}
<main>
<section><h2>Rooms</h2><p>LATE-ROOM-LINE cedar loft for a stay, with a window on the bay.</p></section>
<section><h2>Book</h2><p><a href="mailto:stay@example">Reserve a room</a></p></section>
</main>
</body>
</html>
"""
    assert page.find("LATE-ROOM-LINE") > 1200
    folder = tmp_path.parent / f"ea-ahead-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "Let's build a clean hotel landing page. I'll give it a name, a hero, rooms, and a footer. Writing it now.\n"
        f"```files\nwrite\nindex.html\n{page}\n```",
        look,
        "The lower half is the cedar loft, not another hero.\n"
        "LATE-ROOM-LINE\n"
        "\n"
        "That room card is the one I wanted. I'm leaving the file as it is.\n"
        "```memory\n"
        "a page with no folder is written at C:\\work\\landing.html\n"
        "```",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    assert message.get("error") is not True
    first = answer.find("Let's build a clean hotel landing page")
    second = answer.find("Let me look at the rest of the file")
    third = answer.find("The lower half is the cedar loft")
    close = answer.find(
        _page_closer("Northwater Hotel")
    )
    assert first >= 0 and second > first and third > second and close > third
    assert answer.endswith(
        _page_closer("Northwater Hotel")
    )
    assert "The tide is at the door" not in answer
    assert "The Meridian" not in answer
    assert "```" not in answer
    assert "Filed a line" not in answer
    assert "I'll build a simple landing page for a hotel." not in answer
    text = (folder / "landing.html").read_text(encoding="utf-8")
    assert "LATE-ROOM-LINE" in text
    assert "unsplash.com" in text
    third_call = recorder.seen[before + 2]
    users = [item.get("content") or "" for item in third_call if item.get("role") == "user"]
    assert any("LATE-ROOM-LINE" in item and "still in the middle of the account" in item for item in users)
    assert len(recorder.seen) == before + 3
    memory = (tmp_path / "bots" / bot["id"] / "notes" / "MEMORY.md").read_text(encoding="utf-8")
    assert "a page with no folder is written at C:\\work\\landing.html" in memory
    assert "cedar loft" not in memory
    assert "The Meridian" not in memory


def test_a_plan_to_read_does_not_take_the_closer(tmp_path, monkeypatch):
    """The closer does not land on a plan to read. The next sentence is what the model found."""
    from easyagent.tools import _RESEARCH_CACHE, _still_thinking

    _RESEARCH_CACHE.clear()
    plan = "Let me read it back to verify it's actually there and looks right."
    assert _still_thinking(plan) is True

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>The Lantern Hotel — A Quiet Shore</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  .hero { display: grid; min-height: 70vh; }
  main { display: flex; gap: 16px; }
</style>
</head>
<body>
<header class="hero">
<svg width="80" height="40" viewBox="0 0 80 40"><rect x="0" y="0" width="80" height="40" fill="#16324a"/></svg>
<h1>The lamp is in the window.</h1>
</header>
<main>
<section><h2>Rooms</h2><p>MARK-BOOK is the booking line for a stay.</p></section>
<section><h2>Book</h2><p><a href="mailto:stay@example">Reserve a room</a></p></section>
</main>
</body>
</html>
"""
    folder = tmp_path.parent / f"ea-plan-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "Let me build a hotel landing page. I'll give it a hero and a booking line.\n"
        f"{plan}\n"
        f"```files\nwrite\nindex.html\n{page}\n```",
        "The page is ready.",
        "I read the file. MARK-BOOK is the booking line, under the hero drawing. "
        "I'm leaving the file as it is.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    assert message.get("error") is not True
    plan_at = answer.find(plan)
    found = answer.find("MARK-BOOK is the booking line")
    close = answer.find(
        _page_closer("The Lantern Hotel")
    )
    assert plan_at >= 0 and found > plan_at and close > found
    assert answer.endswith(
        _page_closer("The Lantern Hotel")
    )
    assert "The lamp is in the window" not in answer
    assert "The Harborview Hotel" not in answer
    assert "The page is ready" not in answer
    assert "Read " not in answer
    follow = recorder.seen[before + 2]
    users = [item.get("content") or "" for item in follow if item.get("role") == "user"]
    assert any("MARK-BOOK" in item and "read result is below" in item for item in users)
    assert len(recorder.seen) == before + 3
    memory = tmp_path / "bots" / bot["id"] / "notes" / "MEMORY.md"
    assert memory.read_text(encoding="utf-8").strip() == "# Memory"


def test_a_page_with_nothing_lasting_does_not_get_a_fake_note(tmp_path, monkeypatch):
    """A turn that learned nothing lasting leaves the memory file as a heading."""
    from easyagent.tools import _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Northwater Hotel — A Short Stay</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  .hero { display: grid; min-height: 70vh;
    background: url("https://images.unsplash.com/photo-bay") center/cover; }
  main { display: flex; gap: 16px; }
</style>
</head>
<body>
<header class="hero"><h1>The tide is at the door.</h1></header>
<main>
<section><h2>Rooms</h2><p>A room for a stay.</p></section>
<section><h2>Book</h2><p><a href="mailto:stay@example">Reserve a room</a></p></section>
</main>
</body>
</html>
"""
    folder = tmp_path.parent / f"ea-nomem-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        f"```files\nwrite\nindex.html\n{page}\n```",
        "Nothing else is missing, so I'm leaving the file as it is.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert answer.endswith(
        _page_closer("Northwater Hotel")
    )
    memory = tmp_path / "bots" / bot["id"] / "notes" / "MEMORY.md"
    assert memory.read_text(encoding="utf-8").strip() == "# Memory"


def test_a_rejected_windows_command_still_writes_the_hotel_page(tmp_path, monkeypatch):
    """The laptop run: one command cmd rejected, and the stored reply was only that error.

    The leftover file was the canned sample page. It was not written by this turn.
    """
    import sys

    from easyagent.loop import page_matches, page_subject
    from easyagent.tools import ToolError, _landing_html, _target_path

    monkeypatch.setattr(sys, "platform", "win32")
    ask = "can you build me a simple landing page for a hotel"
    assert page_subject(ask) == "hotel"
    from easyagent.paths import deliverable_file

    assert _target_path(ask, "landing.html", (".html", ".htm")) == deliverable_file("landing.html")
    folder = tmp_path.parent / f"ea-syntax-{tmp_path.name}"
    folder.mkdir()
    target = folder / "landing.html"
    target.write_text(_landing_html(), encoding="utf-8")
    assert "<h1>Sample landing page</h1>" in target.read_text(encoding="utf-8")
    assert "hotel" not in target.read_text(encoding="utf-8").lower()

    def rejected(_store, _command):
        raise ToolError("The syntax of the command is incorrect.")

    monkeypatch.setattr("easyagent.tools._placed_file", _redirect_writes(folder))
    monkeypatch.setattr("easyagent.tools._run_shell", rejected)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    page = _model_hotel_html("Northline Hotel")
    recorder.reply = [
        "```shell\ndir /b C:\\work &\n```",
        f"```files\nwrite\nC:\\work\\landing.html\n{page}\n```",
        "The syntax of the command is incorrect.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": ask},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    assert message.get("error") is not True
    assert answer.strip() != "The syntax of the command is incorrect."
    assert "The syntax of the command is incorrect." not in answer
    assert "is not recognized" not in answer
    assert "Ran a command" not in answer
    text = target.read_text(encoding="utf-8")
    assert "<h1>Sample landing page</h1>" not in text
    assert "hotel" in text.lower()
    assert page_matches(target.read_bytes(), "hotel") is True
    heading = re.search(r"(?is)<h1>\s*(.*?)\s*</h1>", text).group(1)
    heading = " ".join(re.sub(r"<[^>]+>", " ", heading).split())
    assert heading == "Northline Hotel"
    assert heading in text
    assert heading in answer
    assert "Harbor Hotel" not in text
    assert "Harbor Hotel" not in answer
    assert text != _landing_html("hotel")
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "landing.html" in answer
    assert "<!DOCTYPE" not in answer
    assert "<h1>" not in answer
    assert "Proven" not in answer
    assert "C1" not in answer
    assert "Evidence" not in answer
    assert "not written" not in answer.lower()
    preview = message["attachment"]
    assert preview["media_type"] == "text/html"
    assert preview["name"] == "landing.html"
    assert "hotel" in preview["excerpt"].lower()
    assert "Sample landing page" not in preview["excerpt"]
    assert "<!DOCTYPE" not in preview["excerpt"]
    assert "<html" not in preview["excerpt"].lower()
    assert "<h1" not in preview["excerpt"]
    assert preview["path"].endswith("landing.html")
    opened = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{preview['id']}")
    assert opened.status_code == 200
    assert heading.encode("utf-8") in opened.content
    assert b"Sample landing page" not in opened.content
    assert b"A short page for a hotel." not in opened.content


def test_pwd_on_windows_still_writes_the_hotel_page(tmp_path, monkeypatch):
    """The laptop run: pwd was rejected, and that error was the whole reply."""
    import sys

    from easyagent.loop import page_matches
    from easyagent.tools import _landing_html

    monkeypatch.setattr(sys, "platform", "win32")
    ask = "can you build me a simple landing page for a hotel"
    folder = tmp_path.parent / f"ea-pwd-{tmp_path.name}"
    folder.mkdir()
    target = folder / "landing.html"
    old = _model_hotel_html("Old Pier Hotel")
    target.write_text(old, encoding="utf-8")
    assert page_matches(old.encode("utf-8"), "hotel") is True
    page = _model_hotel_html("The Lantern Hotel")
    assert "Harbor Hotel" not in page
    assert "Harbor Hotel" not in _landing_html("hotel")
    ran = []

    def trap(_store, command):
        ran.append(command)
        raise AssertionError(command)

    monkeypatch.setattr("easyagent.tools._run_shell", trap)
    monkeypatch.setattr("easyagent.tools._placed_file", _redirect_writes(folder))
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        "```shell\npwd\n```",
        f"```files\nwrite\nindex.html\n{page}\n```",
        "'pwd' is not recognized as an internal or external command,\noperable program or batch file.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": ask},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    text = target.read_text(encoding="utf-8")
    assert ran == []
    assert message.get("error") is not True
    assert "The Lantern Hotel" in text
    assert "Old Pier Hotel" not in text
    assert "Harbor Hotel" not in text
    assert text != _landing_html("hotel")
    assert page_matches(text.encode("utf-8"), "hotel") is True
    assert "The Lantern Hotel" in answer
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "pwd" not in answer.replace(_written_page(), "").lower()
    assert "not recognized" not in answer.lower()
    assert "operable program" not in answer.lower()
    assert "batch file" not in answer.lower()
    assert "Proven" not in answer
    assert "C1" not in answer
    assert "Evidence" not in answer
    assert "<!DOCTYPE" not in answer
    assert "<h1>" not in answer
    assert "index.html" not in answer
    preview = message["attachment"]
    assert preview["name"] == "landing.html"
    assert "The Lantern Hotel" in preview["excerpt"]
    assert "<" not in preview["excerpt"]
    opened = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{preview['id']}")
    assert opened.status_code == 200
    assert b"The Lantern Hotel" in opened.content
    assert b"Old Pier Hotel" not in opened.content
    assert b"Harbor Hotel" not in opened.content


def test_a_decidable_question_does_not_end_the_turn(tmp_path, monkeypatch):
    """A name the model can pick does not end the turn. Which folder still does."""
    folder = tmp_path.parent / f"ea-which-{tmp_path.name}"
    folder.mkdir()
    target = folder / "landing.html"
    old = _model_hotel_html("The Harborview Hotel")
    target.write_text(old, encoding="utf-8")
    page = _model_hotel_html("The Lantern Hotel")
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    standing = tmp_path / "bots" / bot["id"] / "notes"
    standing.mkdir(parents=True, exist_ok=True)
    standing.joinpath("USER.md").write_text("# User\n\nThe hotel keeps a quiet desk.\n", encoding="utf-8")
    standing.joinpath("MEMORY.md").write_text("# Memory\n\nUNRELATED-NOTE-TOKEN stays out.\n", encoding="utf-8")
    recorder.reply = [
        "```question\nWhich hotel should the page be for?\n"
        "A made-up one (I'll pick a name and details)\n"
        "Your real hotel (tell me the name and any details)\n```",
        "A small hotel on the water. I'll name it in the page.\n"
        f"```files\nwrite\nindex.html\n{page}\n```",
        "The page is ready.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    assert message.get("error") is not True
    assert message.get("choices") in (None, [])
    assert "Which hotel should the page be for?" not in answer
    assert "A small hotel on the water. I'll name it in the page." in answer
    assert "I'll build a simple landing page for a hotel." not in answer
    assert "I looked up what a hotel landing page contains" not in answer
    assert "I wrote the page." not in answer
    assert "I looked at the file." not in answer
    assert "UNRELATED-NOTE-TOKEN" not in answer
    assert "Goal:" not in answer
    assert "Thinking:" not in answer
    assert "Plan:" not in answer
    assert "Check:" not in answer
    assert "Build:" not in answer
    assert "Ran a command" not in answer
    assert "The Lantern Hotel" in answer
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "Stuck" not in answer
    assert "Wrote " not in answer
    assert "Read " not in answer
    assert "Proven" not in answer
    text = target.read_text(encoding="utf-8")
    assert "The Lantern Hotel" in text
    assert "The Harborview Hotel" not in text
    assert "<svg" in text
    saved = standing / "chats" / chat["id"]
    assert not (saved / "goal.md").exists()
    assert "quiet desk" in (standing / "USER.md").read_text(encoding="utf-8")
    assert not (saved / "thinking.md").exists()
    assert not (saved / "plan.md").exists()
    notes_blob = "\n".join(path.read_text(encoding="utf-8") for path in saved.glob("*.md"))
    assert "I don't have a question" not in notes_blob
    assert "The request already says what to change" not in notes_blob
    assert "picture in the file" in (saved / "check.md").read_text(encoding="utf-8")
    assert "The Lantern Hotel" in (saved / "check.md").read_text(encoding="utf-8")
    assert (standing / "MEMORY.md").is_file()
    assert (standing / "USER.md").is_file()

    bakery_page = _designed_model_page("Oven Street Bakery", "bakery")
    recorder.reply = [
        "```question\nWhich bakery should the page be for?\n"
        "A made-up one\n"
        "The one on the corner\n```",
        f"```files\nwrite\nindex.html\n{bakery_page}\n```",
        "The page is ready.",
    ]
    bakery = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a landing page for a bakery"},
    )
    assert bakery.status_code == 200, bakery.text
    follow = bakery.json()["chat"]["messages"][-1]
    assert follow.get("choices") in (None, [])
    assert "Which bakery should the page be for?" not in follow["content"]
    assert "Oven Street Bakery" in follow["content"]
    assert "I'll build a landing page for a bakery." not in follow["content"]
    assert "Oven Street Bakery" in target.read_text(encoding="utf-8")

    recorder.reply = [
        "```question\nWhich folder?\nsrc\ntests\n```",
    ]
    asked = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "write the notes into a file"},
    )
    assert asked.status_code == 200, asked.text
    choice = asked.json()["chat"]["messages"][-1]
    assert "Which folder?" in choice["content"]
    assert choice["choices"] == ["src", "tests"]


def test_the_page_sentence_uses_the_title_not_the_heading(tmp_path, monkeypatch):
    """The name is the title. A slogan in the heading is not the name, even with no dash and no period."""
    from easyagent.tools import _page_reply, _spoken

    folder = tmp_path.parent / f"ea-azure-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    (folder / "landing.html").write_text(
        "<html><head><title>Azure Bay Hotel &amp; Resort</title></head>"
        "<body><h1>Where the Sea Meets Serenity</h1></body></html>",
        encoding="utf-8",
    )
    reply = _page_reply(r"C:\work\landing.html")
    assert reply == "The page is Azure Bay Hotel & Resort. It is at C:\\work\\landing.html, and you can open it."
    assert "Where the Sea Meets Serenity" not in reply
    assert "Serenity." not in reply
    spoken = _spoken(
        "I found the booking link was dead, so I fixed it.\n"
        "```finish\nproven\nThe booking link now goes to the desk.\n```\n"
        "You can open the file to see the page.\n"
        "```finish\nproven\nchecked\n```"
    )
    assert "I found the booking link was dead, so I fixed it." in spoken
    assert "You can open the file to see the page." in spoken
    assert "```" not in spoken
    assert "finish" not in spoken.lower()
    assert "proven" not in spoken.lower()


def test_a_finish_fence_stays_out_and_the_closer_uses_the_title(tmp_path, monkeypatch):
    """The chain stays. A finish fence is not the chat. The closer uses the title, not the heading."""
    from easyagent.tools import _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Azure Bay Hotel &amp; Resort</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  .hero { display: grid; min-height: 70vh;
    background: url("https://images.unsplash.com/photo-bay") center/cover; }
  main { display: flex; gap: 16px; }
</style>
</head>
<body>
<header class="hero"><h1>Where the Sea Meets Serenity</h1><p>A room on the water.</p></header>
<main>
<section><h2>Rooms</h2><p>Three rooms for a stay.</p></section>
<section><h2>Book</h2><p><a href="mailto:stay@example">Reserve a room</a></p></section>
</main>
</body>
</html>
"""
    folder = tmp_path.parent / f"ea-fence-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        "I'll write the page, then read it back.\n"
        f"```files\nwrite\nindex.html\n{page}\n```",
        "Let me read the file back and check it.",
        "I found the booking link was dead, so I fixed it.\n"
        "```finish\nproven\nThe booking link now goes to the desk.\n```\n"
        "You can open the file to see the page.\n"
        "```finish\nproven\nchecked\n```",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    found = answer.find("I found the booking link was dead, so I fixed it.")
    close = answer.find(
        _page_closer("Azure Bay Hotel & Resort")
    )
    assert found >= 0 and close > found
    assert "You can open the file to see the page." not in answer
    assert answer.endswith(
        _page_closer("Azure Bay Hotel & Resort")
    )
    assert "Where the Sea Meets Serenity" not in answer
    assert "```" not in answer
    assert "proven" not in answer.lower()
    assert "```finish" not in answer.lower()


def test_a_read_back_stays_and_an_open_line_is_replaced(tmp_path, monkeypatch):
    """What the read-back found stays. An open-it line that misses the title is replaced by the one closer."""
    from easyagent.tools import _RESEARCH_CACHE, _end_once

    kept = _end_once(
        "I rewrote the file to fix the dead links. Let me read it back to confirm the changes landed.\n\n"
        "The read-back shows the four links now point at the sections. The page is ready.\n\n"
        "You can open C:\\work\\landing.html in a browser to see it."
    )
    assert "Let me read it back to confirm the changes landed." in kept
    assert "The read-back shows the four links now point at the sections." in kept
    assert "The page is ready." in kept

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>The Meridian</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  .hero { display: grid; min-height: 70vh;
    background: url("https://images.unsplash.com/photo-bay") center/cover; }
  main { display: flex; gap: 16px; }
</style>
</head>
<body>
<header class="hero"><h1>Rest where the sea meets the sky</h1></header>
<main>
<section><h2>Rooms</h2><p>A room for a stay.</p></section>
<section><h2>Book</h2><p><a href="mailto:stay@example">Reserve a room</a></p></section>
</main>
</body>
</html>
"""
    folder = tmp_path.parent / f"ea-readback-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "I'll write the page for a stay on the water.\n"
        f"```files\nwrite\nindex.html\n{page}\n```",
        "I rewrote the file to fix the dead links. Let me read it back to confirm the changes landed.",
        "The read-back shows the four links now point at the sections. The page is ready.\n\n"
        "You can open C:\\work\\landing.html in a browser to see it.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    plan = answer.find("Let me read it back to confirm the changes landed.")
    found = answer.find("The read-back shows the four links now point at the sections.")
    close = answer.find(
        _page_closer("The Meridian")
    )
    assert plan >= 0 and found > plan and close > found
    assert "The page is ready." in answer
    assert "You can open C:\\work\\landing.html in a browser to see it." not in answer
    assert answer.endswith(
        _page_closer("The Meridian")
    )
    assert answer.count("you can open") == 1
    assert "Rest where the sea meets the sky" not in answer
    assert len(recorder.seen) == before + 3
    memory = tmp_path / "bots" / bot["id"] / "notes" / "MEMORY.md"
    assert memory.read_text(encoding="utf-8").strip() == "# Memory"


def test_a_reported_check_does_not_hold_and_a_done_line_is_replaced(tmp_path, monkeypatch):
    """A plan that already states the result is finished. A short done-line is replaced by the closer."""
    from easyagent.tools import _RESEARCH_CACHE, _short_done_line, _still_thinking

    assert _still_thinking("Let me read it back to verify it's actually there and looks right.") is True
    assert _still_thinking("Let me check for placeholder links.") is True
    reported = (
        "I read the file back. Let me check for placeholder links — "
        'I see two href="#book" links. All good.'
    )
    assert _still_thinking(reported) is False
    assert _short_done_line("The page is done and verified on disk.") is True
    assert _short_done_line("I verified the booking link goes to the desk.") is False

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>The Meridian</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  .hero { display: grid; min-height: 70vh;
    background: url("https://images.unsplash.com/photo-bay") center/cover; }
  main { display: flex; gap: 16px; }
</style>
</head>
<body>
<header class="hero"><h1>Rest where the sea meets the sky</h1></header>
<main>
<section><h2>Rooms</h2><p>A room for a stay.</p></section>
<section id="book"><h2>Book</h2><p><a href="#book">Check dates</a></p></section>
</main>
</body>
</html>
"""
    folder = tmp_path.parent / f"ea-done-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "I'll write the page for a stay on the water.\n"
        f"```files\nwrite\nindex.html\n{page}\n```",
        reported + "\n\nThe page is done and verified on disk.",
        "This extra round should not run.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert 'I see two href="#book" links.' in answer
    assert "Let me check for placeholder links" in answer
    assert "The page is done and verified on disk." not in answer
    assert "This extra round should not run." not in answer
    assert answer.endswith(
        _page_closer("The Meridian")
    )
    assert answer.count("you can open") == 1
    assert "Rest where the sea meets the sky" not in answer
    assert len(recorder.seen) == before + 2


def test_a_cited_tag_stays_in_the_reply():
    """A short HTML citation is not a pasted page. It stays, including inside a code span."""
    from easyagent.tools import _drop_page_dump, _spoken

    filler = "The bay stays quiet at the window. " * 40
    cited = _spoken(
        "It's a valid HTML document (`<!DOCTYPE html>`). " + filler
    )
    assert "<!DOCTYPE html>" in cited
    assert "(`<!DOCTYPE html>`)" in cited
    assert "The bay stays quiet at the window." in cited
    bare = _spoken("The file starts with <!DOCTYPE html> in the first line. " + filler)
    assert "<!DOCTYPE html>" in bare
    assert "The bay stays quiet at the window." in bare
    document = (
        "<!DOCTYPE html>\n<html><head><title>Pasted Hotel</title></head><body>\n"
        + ("<p>A dumped line of the page.</p>\n" * 40)
        + "</body></html>"
    )
    assert len(document) >= 400
    dumped = _spoken("I checked the file.\n\n" + document + "\n\nThe booking link works.")
    assert "I checked the file." in dumped
    assert "The booking link works." in dumped
    assert "Pasted Hotel" not in dumped
    fenced = _spoken(
        "I checked the file.\n\n```html\n" + document + "\n```\n\nThe booking link works."
    )
    assert "Pasted Hotel" in fenced
    assert "<!DOCTYPE html>" in fenced
    assert "The booking link works." in fenced
    assert _drop_page_dump(f"See `<!DOCTYPE html>` once. {filler}").count("<!DOCTYPE html>") == 1


def test_a_repeated_wrap_up_ends_once(tmp_path, monkeypatch):
    """A later paragraph is dropped only when it repeats an earlier one. A wrap-up that names another folder is replaced by where the file was written."""
    from easyagent.tools import _RESEARCH_CACHE, _end_once, _wrap_adds_nothing

    _RESEARCH_CACHE.clear()
    repeated = _end_once(
        "I'll write the rooms.\n\n"
        "Your landing page is ready at C:\\work\\landing.html for 'The Harborview Hotel'. "
        "Open it in any browser.\n\n"
        "The page is all set. It is saved at C:\\work\\landing.html for The Harborview Hotel.\n\n"
        "You can open C:\\work\\landing.html to see The Harborview Hotel."
    )
    assert repeated.count("Harborview") == 1
    assert "Your landing page is ready at C:\\work\\landing.html for 'The Harborview Hotel'." in repeated
    assert "Open it in any browser." in repeated
    assert "You can open C:\\work\\landing.html to see The Harborview Hotel." not in repeated
    assert "all set" not in repeated
    assert "I'll write the rooms." in repeated
    earlier = "Your landing page is ready at C:\\work\\landing.html for 'The Harborview Hotel'. Open it in any browser."
    restated = "You can open it at C:\\work\\landing.html for The Harborview Hotel."
    assert _wrap_adds_nothing(restated, earlier) is True
    assert _wrap_adds_nothing(
        "The dates control now goes to the booking section. You can open C:\\work\\landing.html.",
        earlier,
    ) is False

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>The Harborview Hotel — Coastal Boutique Stay</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  .hero { display: grid; min-height: 70vh;
    background: url("https://images.unsplash.com/photo-harbor") center/cover; }
  main { display: flex; gap: 16px; }
</style>
</head>
<body>
<header class="hero"><h1>Where the water stays</h1></header>
<main>
<section><h2>Rooms</h2><p>A room for a stay.</p></section>
<section><h2>Book</h2><p><a href="mailto:stay@example">Reserve a room</a></p></section>
</main>
</body>
</html>
"""
    folder = tmp_path.parent / f"ea-once-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "I'll write the rooms and the booking line.\n"
        f"```files\nwrite\nindex.html\n{page}\n```",
        "Your landing page is ready at C:\\work\\landing.html for 'The Harborview Hotel'. "
        "Open it in any browser.\n\n"
        "The page is all set. It is saved at C:\\work\\landing.html for The Harborview Hotel.\n\n"
        "You can open C:\\work\\landing.html to see The Harborview Hotel.",
        "This later summary should not be called.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "I'll write the rooms and the booking line." in answer
    assert answer.endswith(_page_closer("The Harborview Hotel"))
    assert answer.count("you can open") == 1
    assert "Your landing page is ready at C:\\work\\landing.html for 'The Harborview Hotel'." not in answer
    assert "Open it in any browser." not in answer
    assert "You can open C:\\work\\landing.html to see The Harborview Hotel." not in answer
    assert "all set" not in answer
    assert "This later summary should not be called." not in answer
    assert "The page is The Harborview Hotel. It is at C:\\work\\landing.html, and you can open it." not in answer
    assert "Where the water stays" not in answer
    assert len(recorder.seen) == before + 2


def test_the_name_is_the_title_even_when_the_heading_is_not_a_slogan(tmp_path, monkeypatch):
    """The title is the name. A heading is not, even when the title has no subtitle separator."""
    from easyagent.tools import _page_name, _page_reply, _placeholder_link, describe_written_page

    assert _placeholder_link('<a href="#">Check dates</a>') is True
    assert _placeholder_link('<a href="">Check dates</a>') is True
    assert _placeholder_link("<a href=''>Check dates</a>") is True
    assert _placeholder_link('<a href="#stay">Stay</a>') is True
    assert _placeholder_link('<a href="#stay">Stay</a><h2 id="stay">Stay</h2>') is False
    assert _placeholder_link('<a id="book" href="#book">Book</a>') is False
    assert _placeholder_link('<a href="mailto:stay@example">Reserve</a>') is False
    folder = tmp_path.parent / f"ea-title-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    page = """<!DOCTYPE html>
<html><head><title>Azure Bay Hotel &amp; Resort</title>
<style>
  body { color: #1c1915; background: #f4efe6; font-size: 18px; }
  main { display: flex; gap: 16px; }
</style>
</head>
<body>
<h1>Rest where the sea meets the sky</h1>
<img src="https://images.unsplash.com/photo-bay" alt="">
</body></html>
"""
    (folder / "landing.html").write_text(page, encoding="utf-8")
    assert _page_name(r"C:\work\landing.html") == "Azure Bay Hotel & Resort"
    reply = _page_reply(r"C:\work\landing.html")
    assert reply == "The page is Azure Bay Hotel & Resort. It is at C:\\work\\landing.html, and you can open it."
    assert "Rest where the sea meets the sky" not in reply
    check = describe_written_page(r"C:\work\landing.html")
    assert "Azure Bay Hotel & Resort" in check
    assert "Rest where the sea meets the sky" not in check


def test_a_placeholder_link_keeps_the_turn_going(tmp_path, monkeypatch):
    """A wrap-up does not end the turn while a placeholder link is in the file. The last wrap-up is the one that stays."""
    from easyagent.tools import _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)

    def page(link: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Azure Bay Hotel &amp; Resort</title>
<style>
  body {{ margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }}
  .hero {{ display: grid; min-height: 70vh;
    background: url("https://images.unsplash.com/photo-bay") center/cover; }}
  main {{ display: flex; gap: 16px; }}
</style>
</head>
<body>
<header class="hero"><h1>Rest where the sea meets the sky</h1></header>
<main>
<section><h2>Rooms</h2><p>A room for a stay.</p></section>
<section><h2>Book</h2><p><a href="{link}">Check dates</a></p></section>
</main>
</body>
</html>
"""

    folder = tmp_path.parent / f"ea-link-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "I'll write the page, then check the links.\n"
        f"```files\nwrite\nindex.html\n{page('#')}\n```",
        "The page is on disk at C:\\work\\landing.html. "
        "The name on the page is Azure Bay Hotel & Resort. You can open it in your browser.",
        "The dates control uses a placeholder link. I'll point it at the booking section.\n"
        f"```files\nwrite\nindex.html\n{page('mailto:stay@example')}\n```",
        "The dates control now goes to the booking section. "
        "You can open C:\\work\\landing.html to see Azure Bay Hotel & Resort.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "I'll write the page, then check the links." in answer
    assert "The dates control uses a placeholder link." in answer
    assert "The dates control now goes to the booking section." in answer
    assert "Rest where the sea meets the sky" not in answer
    assert "The page is Azure Bay Hotel & Resort. It is at C:\\work\\landing.html, and you can open it." not in answer
    assert "You can open C:\\work\\landing.html to see Azure Bay Hotel & Resort." in answer
    assert answer.endswith(_page_closer("Azure Bay Hotel & Resort"))
    text = (folder / "landing.html").read_text(encoding="utf-8")
    assert 'href="#"' not in text
    assert "mailto:stay@example" in text
    assert len(recorder.seen) == before + 4
    follow = recorder.seen[before + 2]
    users = [item.get("content") or "" for item in follow if item.get("role") == "user"]
    assert any("placeholder link" in item and 'href="#"' in item for item in users)
    check = (tmp_path / "bots" / bot["id"] / "notes" / "chats" / chat["id"] / "check.md").read_text(encoding="utf-8")
    assert "Azure Bay Hotel & Resort" in check
    assert "Rest where the sea meets the sky" not in check
    memory = tmp_path / "bots" / bot["id"] / "notes" / "MEMORY.md"
    assert memory.read_text(encoding="utf-8").strip() == "# Memory"


def test_the_page_sentence_uses_the_title_name(tmp_path, monkeypatch):
    """The name is the title, not the heading, and a period already in the heading is not doubled."""
    from easyagent.tools import _page_reply

    folder = tmp_path.parent / f"ea-name-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    (folder / "landing.html").write_text(
        "<html><head><title>Solace Bay Hotel — Stay Slow, Wake Well</title></head>"
        "<body><h1>Stay slow. Wake to the tide.</h1></body></html>",
        encoding="utf-8",
    )
    reply = _page_reply(r"C:\work\landing.html")
    assert reply == "The page is Solace Bay Hotel. It is at C:\\work\\landing.html, and you can open it."
    assert "Stay slow" not in reply
    assert "tide.." not in reply


def test_the_chat_uses_the_model_sentence_and_the_title_name(tmp_path, monkeypatch):
    """A pictured page keeps the model's words. The last sentence uses the title, not the heading."""
    from easyagent.tools import _DRAWN_PICTURE, _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return (
            "1. Hotels & Travel Accommodation Landing Page Examples & Guidelines\n"
            "https://example.test/hotels\n"
            "7 Examples of Amazing Hotel Landing Pages - RedAlkemi\n"
        )

    monkeypatch.setattr("easyagent.search.web_search", search)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Solace Bay Hotel — Stay Slow, Wake Well</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  nav { display: flex; gap: 16px; }
  .hero { display: grid; min-height: 70vh; color: #f7f3ea;
    background: url("https://images.unsplash.com/photo-bay") center/cover; }
</style>
</head>
<body>
<nav><a href="#stay">Stay</a><a href="#book">Book</a></nav>
<header class="hero" id="stay">
<h1>Stay slow. Wake to the tide.</h1>
<p>A quiet room, and the water at the window.</p>
</header>
<section>
<h2>Rooms</h2>
<p>Three rooms for a stay. Each one faces the bay.</p>
<img src="https://images.unsplash.com/photo-room" alt="">
</section>
<section id="book">
<h2>Book</h2>
<p><a href="mailto:stay@example">Reserve a room</a></p>
</section>
<footer><p>The bay road.</p></footer>
</body>
</html>
"""
    folder = tmp_path.parent / f"ea-solace-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        "A quiet hotel on a bay. I'll write that page, then look at the file.\n"
        f"```files\nwrite\nindex.html\n{page}\n```",
        "The page is ready.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    text = (folder / "landing.html").read_text(encoding="utf-8")
    assert "A quiet hotel on a bay. I'll write that page, then look at the file." in answer
    assert answer.endswith(
        _page_closer("Solace Bay Hotel")
    )
    assert "Stay slow" not in answer
    assert "tide.." not in answer
    assert "RedAlkemi" not in answer
    assert "Hotels & Travel" not in answer
    assert "I'll build a simple landing page for a hotel." not in answer
    assert "I looked up what a hotel landing page contains" not in answer
    assert "I wrote the page." not in answer
    assert "I looked at the file." not in answer
    assert "Goal:" not in answer
    assert "Wrote " not in answer
    assert "Read " not in answer
    assert "The page is ready" not in answer
    assert "unsplash.com" in text
    assert "Stay slow. Wake to the tide." in text
    assert _DRAWN_PICTURE not in text


def test_a_build_keeps_the_model_chain(tmp_path, monkeypatch):
    """The chat keeps the account the model writes as it goes. A status line does not replace it."""
    from easyagent.tools import _DRAWN_PICTURE, _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return "1. 7 Examples of Amazing Hotel Landing Pages - RedAlkemi\nhttps://example.test/hotels\n"

    monkeypatch.setattr("easyagent.search.web_search", search)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>North Pier Hotel — A Quiet Stay</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  .hero { display: grid; min-height: 70vh; color: #f7f3ea;
    background: url("https://images.unsplash.com/photo-bay") center/cover; }
  main { display: flex; gap: 16px; }
</style>
</head>
<body>
<header class="hero"><h1>Come in from the weather.</h1><p>A room on the pier.</p></header>
<main>
<section><h2>Rooms</h2><p>Three rooms for a stay, each with a window on the water.</p></section>
<section><h2>Book</h2><p><a href="mailto:stay@example">Reserve a room</a></p></section>
</main>
</body>
</html>
"""
    folder = tmp_path.parent / f"ea-chain-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "The page needs a name, a hero, rooms, and a picture that is actually in the file.\n"
        "\n"
        "The line I will not ship is:\n"
        "background: linear-gradient(#143, #321);\n"
        "\n"
        "That gradient is not a picture. I'll write an image address instead, then look at the file.\n"
        f"```files\nwrite\nindex.html\n{page}\n```",
        "The picture line in the file is now:\n"
        "background: url(\"https://images.unsplash.com/photo-bay\") center/cover;\n"
        "\n"
        "The rooms and the reserve link are there. Nothing else is missing, so I'm leaving the file as it is.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    first = answer.find("The page needs a name, a hero, rooms, and a picture that is actually in the file.")
    wrong = answer.find("background: linear-gradient(#143, #321);")
    fix = answer.find('background: url("https://images.unsplash.com/photo-bay") center/cover;')
    close = answer.find(_page_closer("North Pier Hotel"))
    assert first >= 0 and wrong > first and fix > wrong and close > fix
    assert answer.endswith(
        _page_closer("North Pier Hotel")
    )
    assert "Come in from the weather" not in answer
    assert "I looked at the file." not in answer
    assert "I'll build a simple landing page for a hotel." not in answer
    assert "I looked up what a hotel landing page contains" not in answer
    assert "I wrote the page." not in answer
    assert "RedAlkemi" not in answer
    assert "Goal:" not in answer
    assert "Wrote " not in answer
    assert "Read " not in answer
    text = (folder / "landing.html").read_text(encoding="utf-8")
    assert "North Pier Hotel" in text
    assert "unsplash.com" in text
    assert _DRAWN_PICTURE not in text
    prompts = "\n".join(item.get("content") or "" for batch in recorder.seen[before:] for item in batch)
    assert "including the line you will change" in prompts
    assert "I looked at the file." not in prompts
    assert len(recorder.seen) == before + 2


def test_a_styled_page_with_pictures_is_not_a_plain_page(tmp_path, monkeypatch):
    """The laptop file had pictures and modern styling. That file is not a plain page."""
    from easyagent.tools import (
        _is_short_page_draft,
        describe_written_page,
        page_has_modern_style,
        page_has_picture,
        page_is_plain,
    )

    html = _shore_page()
    assert page_has_picture(html) is True
    assert page_has_modern_style(html) is True
    assert _is_short_page_draft(html) is True
    assert page_is_plain(html) is False
    assert page_is_plain(_welcome_and_cards("Mill Road Bakery")) is True
    folder = tmp_path.parent / f"ea-shore-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    (folder / "landing.html").write_text(html, encoding="utf-8")
    sentence = describe_written_page(r"C:\work\landing.html")
    assert "The Meridian Bay Hotel & Spa" in sentence
    assert "Where the ocean meets calm" not in sentence
    assert "picture in the file" in sentence
    assert "modern styling" in sentence
    assert "plain page" not in sentence.lower()
    assert "so the build continues" not in sentence


def test_a_missing_picture_is_drawn_instead_of_asking(tmp_path, monkeypatch):
    """A styled page of gradients stays open. The gap is not a question."""
    from easyagent.tools import _RESEARCH_CACHE, page_has_modern_style, page_has_picture, page_is_plain

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    html = _gradient_page()
    assert "linear-gradient" in html
    assert page_has_picture(html) is False
    assert page_has_modern_style(html) is True
    assert page_is_plain(html) is True
    pictured = html.replace(
        "</body>",
        '<svg width="160" height="80" viewBox="0 0 160 80"><rect x="0" y="0" width="160" height="80" fill="#16324a"/></svg>\n</body>',
        1,
    )
    folder = tmp_path.parent / f"ea-gradient-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "The rooms can be blocks of color for now. I'll put the page down and look again.\n"
        f"```files\nwrite\nindex.html\n{html}\n```",
        "```question\n"
        "The page keeps reading back as a full styled hotel page, but it still isn't meeting the bar for you. "
        "What's the real gap?\n"
        "The CSS isn't applying\n"
        "The photos aren't loading\n"
        "You want a bolder look\n"
        "Something else\n```",
        "The colors are only gradients. I'll put a drawn picture in the file.\n"
        f"```files\nwrite\nindex.html\n{pictured}\n```",
        "The picture is in the file.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    assert message.get("error") is not True
    assert message.get("choices") in (None, [])
    assert len(recorder.seen) == before + 4
    assert "What's the real gap" not in answer
    assert "photos aren't loading" not in answer
    assert "The rooms can be blocks of color for now." in answer
    assert "I'll build a simple landing page for a hotel." not in answer
    assert "I looked up what a hotel landing page contains" not in answer
    assert "I wrote the page." not in answer
    assert "put one in" not in answer
    assert "Where the day ends slowly" not in answer
    assert "The page is Solstice Bay Hotel." in answer
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "Goal:" not in answer
    assert "Thinking:" not in answer
    assert "Plan:" not in answer
    assert "Check:" not in answer
    assert "Build:" not in answer
    assert "Wrote " not in answer
    assert "Read " not in answer
    assert "Ran a command" not in answer
    assert "Stuck" not in answer
    text = (folder / "landing.html").read_text(encoding="utf-8")
    assert "Where the day ends slowly, by the water." in text
    assert "linear-gradient" in text
    assert "<svg" in text
    assert "Solstice Bay Hotel" in text
    notes = tmp_path / "bots" / bot["id"] / "notes" / "chats" / chat["id"] / "check.md"
    assert "picture in the file" in notes.read_text(encoding="utf-8")


def test_a_timeout_after_the_page_is_written_answers_from_the_file(tmp_path, monkeypatch):
    """A timeout is retried for the window. If the page is already on disk, that timeout is not the reply."""
    from easyagent.llm import ProviderError
    from easyagent.tools import _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    folder = tmp_path.parent / f"ea-timeout-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    page = _shore_page()
    calls = {"n": 0}

    def reply(_messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                "A bay hotel, written as one page.\n"
                f"```files\nwrite\nindex.html\n{page}\n```"
            )
        raise ProviderError("Timed out calling http://localhost:8080/v1")

    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = reply
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a hotel"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    assert message.get("error") is not True
    assert calls["n"] > 3
    assert "Timed out" not in answer
    assert "localhost:8080" not in answer
    assert "A bay hotel, written as one page." in answer
    assert "I'll build a simple landing page for a hotel." not in answer
    assert "I looked up what a hotel landing page contains" not in answer
    assert "Goal:" not in answer
    assert "Thinking:" not in answer
    assert "Plan:" not in answer
    assert "Check:" not in answer
    assert "The page is The Meridian Bay Hotel & Spa." in answer
    assert "Where the ocean meets calm" not in answer
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "plain page" not in answer.lower()
    assert "Stuck" not in answer
    assert "Wrote " not in answer
    assert "Read " not in answer
    text = (folder / "landing.html").read_text(encoding="utf-8")
    assert "Where the ocean meets calm." in text
    assert "unsplash.com" in text
    roles = [item["role"] for item in sent.json()["chat"]["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_the_goal_is_in_the_chat_before_the_model_replies(tmp_path, monkeypatch):
    """The chat stays quiet until the model speaks. A lookup title is not pasted in."""
    from easyagent.llm import ProviderError
    from easyagent.tools import _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return "1. A hotel page\nA shore hotel shows the rooms and a way to book."

    monkeypatch.setattr("easyagent.search.web_search", search)
    folder = tmp_path.parent / f"ea-stage-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    page = _shore_page()
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    chat_path = tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json"
    seen = {"calls": 0, "goal_before_model": False}

    async def pieces(**_kwargs):
        seen["calls"] += 1
        if seen["calls"] == 1:
            stored = json.loads(chat_path.read_text(encoding="utf-8"))
            last = stored["messages"][-1]
            content = last.get("content") or ""
            blob = json.dumps(stored)
            seen["goal_before_model"] = (
                last.get("role") == "user"
                and "I'll build a simple landing page for a hotel." not in blob
                and "I looked up what a hotel landing page contains" not in blob
                and "Goal:" not in blob
                and "Thinking:" not in blob
            )
            yield (
                "A bay hotel, written as one page.\n"
                f"```files\nwrite\nindex.html\n{page}\n```"
            )
            return
        raise ProviderError("Timed out calling http://localhost:8080/v1")

    monkeypatch.setattr("easyagent.llm.stream_complete", pieces)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "can you build me a simple landing page for a hotel"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    assert seen["goal_before_model"] is True
    assert seen["calls"] > 3
    assert '"type": "error"' not in body
    assert "Timed out" not in body
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    answer = stored["messages"][-1]["content"]
    assert stored["messages"][-1].get("error") is not True
    assert stored["messages"][-1].get("live") is not True
    assert "A bay hotel, written as one page." in answer
    assert "I'll build a simple landing page for a hotel." not in answer
    assert "I looked up what a hotel landing page contains" not in answer
    assert "A shore hotel shows the rooms" not in answer
    assert "Goal:" not in answer
    assert "Thinking:" not in answer
    assert "Plan:" not in answer
    assert "Check:" not in answer
    assert "The page is The Meridian Bay Hotel & Spa." in answer
    assert "Where the ocean meets calm" not in answer
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "plain page" not in answer.lower()
    assert [item["role"] for item in stored["messages"]] == ["user", "assistant", "user", "assistant"]
    notes = tmp_path / "bots" / bot["id"] / "notes" / "chats" / chat["id"]
    assert not (notes / "goal.md").exists()


def test_a_ready_sentence_waits_until_the_written_file_is_read_back(tmp_path, monkeypatch):
    """A sentence with no write is the reply. A write is not ready until that file is read back.

    The same loop is a text file and a page. A gradient, an image tag, or a name is not the check.
    """
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = ["The notes are ready."]
    claimed = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello there"},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["chat"]["messages"][-1]["content"] == "The notes are ready."
    assert len(recorder.seen) == before + 1

    folder = tmp_path.parent / f"ea-readback-{tmp_path.name}"
    folder.mkdir()
    target = folder / "notes.txt"
    before = len(recorder.seen)
    recorder.reply = [
        f"```files\nwrite\n{target}\nhello from the file\n```",
        "It's ready.",
    ]
    written = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"write notes.txt in {folder}"},
    )
    assert written.status_code == 200, written.text
    answer = written.json()["chat"]["messages"][-1]["content"]
    assert target.read_text(encoding="utf-8") == "hello from the file"
    assert "It's ready" not in answer
    assert f"Wrote {target}." not in answer
    nudged = "\n".join(item.get("content") or "" for batch in recorder.seen[before:] for item in batch)
    assert "Read that file back" in nudged
    assert len(recorder.seen) == before + 4

    before = len(recorder.seen)
    recorder.reply = [
        f"```files\nwrite\n{target}\nhello from the file\n```",
        f"```files\nread\n{target}\n```",
        "The file says hello from the file.",
    ]
    checked = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"write notes.txt in {folder} and then read it"},
    )
    assert checked.status_code == 200, checked.text
    checked_answer = checked.json()["chat"]["messages"][-1]["content"]
    assert "The file says hello from the file." in checked_answer
    assert len(recorder.seen) == before + 3


def _hero_stack_page(name: str, rooms: tuple[str, str, str]) -> str:
    """A hero, one paragraph, a reserve link, three stacked blocks, and a footer.

    The third block has an extra line, so the blocks are not the same tag tree.
    The hero is a section, not a header.
    """
    blocks = []
    for index, room in enumerate(rooms):
        extra = "<p>From the morning.</p>" if index == 2 else ""
        blocks.append(f'<div class="room"><h2>{room}</h2><p>A quiet corner.</p>{extra}</div>')
    stacked = "\n".join(blocks)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name} — Stay a while</title>
<style>
  body {{ margin: 0; font-family: Georgia, serif; }}
  .hero {{ padding: 48px; }}
  .room {{ padding: 16px; }}
</style>
</head>
<body>
<section class="hero">
<h1>{name}</h1>
<p>One paragraph about the place, with the town at the door.</p>
<a href="#book">Reserve</a>
</section>
<section class="rooms">
{stacked}
</section>
<footer><p>12 Lake Road</p><p>555-0142</p></footer>
</body>
</html>
"""


def _card_page(name: str, rooms: tuple[str, str, str]) -> str:
    """A title, one welcome line, three similar cards, a button, and a footer."""
    cards = "\n".join(
        f'<article class="card"><h3>{room}</h3><p>A quiet corner.</p></article>'
        for room in rooms
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{name}</title></head>
<body>
<header><h1>{name}</h1><p>Welcome in. The counter is open.</p></header>
<section>
{cards}
</section>
<a href="#book">Reserve a seat</a>
<footer><p>1 Mill Road</p><p>555-0100</p><p>hello@example.com</p></footer>
</body>
</html>
"""


def _two_part_page(name: str) -> str:
    """Two different sections. No picture tag, and the size is not the check."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{name}</title></head>
<body>
<header><h1>{name}</h1><p>On the corner since morning.</p></header>
<main>
<section>
<h2>The counter</h2>
<p>The front counter faces the window. People stop in on the way to work.</p>
<p>On Saturdays the line reaches the corner.</p>
</section>
<section>
<h2>The room in back</h2>
<p>The back room has a long table, and you can stay once you have a seat.</p>
</section>
</main>
<footer><p>Walk in, or write ahead.</p></footer>
</body>
</html>
"""


def _welcome_and_cards(name: str) -> str:
    """A header, one welcome, a reserve link, and three cards. No footer and no picture."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name}</title>
<style>
  header {{ padding: 48px; background: linear-gradient(#143, #321); }}
  .card {{ padding: 16px; }}
</style>
</head>
<body>
<header>
<h1>{name}</h1>
<p>One line at the door.</p>
</header>
<section>
<p>Welcome in. The counter is open for the morning.</p>
<a href="#book">Reserve</a>
</section>
<div class="card"><h2>Rye loaf</h2><p>A quiet corner.</p></div>
<div class="card"><h2>Seed bun</h2><p>A quiet corner.</p></div>
<div class="card"><h2>Morning cake</h2><p>A quiet corner.</p></div>
</body>
</html>
"""


def test_a_ready_sentence_after_one_short_write_is_not_the_reply(tmp_path, monkeypatch):
    """A title, a welcome, and one stack of cards does not end the turn. The later page does."""
    from easyagent.tools import _is_short_page_draft

    first = _hero_stack_page("Mill Road Bakery", ("Rye loaf", "Seed bun", "Morning cake"))
    second = _card_page("Mill Road Bakehouse", ("Olive loaf", "Honey bun", "Afternoon cake"))
    cluster = _welcome_and_cards("Mill Road Bakery") + ("\n<!-- plain -->" * 800)
    final = _designed_model_page("Oven Street Bakery", "bakery")
    sibling_rooms = first.replace(
        '<div class="room"><h2>Rye loaf</h2><p>A quiet corner.</p></div>',
        "<section><h2>Rye loaf</h2><p>A quiet corner.</p></section>",
    ).replace(
        '<div class="room"><h2>Seed bun</h2><p>A quiet corner.</p></div>',
        "<section><h2>Seed bun</h2><p>A quiet corner.</p><p>A second line.</p></section>",
    ).replace(
        '<div class="room"><h2>Morning cake</h2><p>A quiet corner.</p><p>From the morning.</p></div>',
        "<section><h2>Morning cake</h2><p>A quiet corner.</p></section>",
    )
    assert _is_short_page_draft(first) is True
    assert _is_short_page_draft(sibling_rooms) is True
    assert _is_short_page_draft(second) is True
    assert _is_short_page_draft(cluster) is True
    assert _is_short_page_draft(final) is False
    assert _is_short_page_draft(_designed_model_page("The Harborview Hotel", "hotel")) is False
    assert "<footer" not in _welcome_and_cards("Mill Road Bakery").lower()
    assert "<img" not in cluster and "<svg" not in cluster
    assert "<svg" in final
    from easyagent.tools import page_is_plain

    assert page_is_plain(cluster) is True
    assert page_is_plain(final) is False
    assert page_is_plain(_two_part_page("Oven Street Bakery")) is True
    assert len(cluster) > len(final)

    page_folder = tmp_path.parent / f"ea-shape-{tmp_path.name}"
    page_folder.mkdir()
    _map_temp(monkeypatch, page_folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        f"```files\nwrite\nindex.html\n{cluster}\n```",
        "The page is ready.",
        f"```files\nwrite\nindex.html\n{final}\n```",
        "The page is ready.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a landing page for a bakery"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    text = (page_folder / "landing.html").read_text(encoding="utf-8")
    assert "Oven Street Bakery" in text
    assert "The front counter" in text
    assert "Rye loaf" not in text
    assert "Oven Street Bakery" in answer
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "Stuck" not in answer
    assert "Still undone" not in answer
    assert "Wrote " not in answer
    assert "Read " not in answer
    assert "The page is ready" not in answer
    assert "Proven" not in answer
    assert "<!DOCTYPE" not in answer
    assert "<h1>" not in answer
    assert len(recorder.seen) == before + 4
    prompts = "\n".join(item.get("content") or "" for batch in recorder.seen[before:] for item in batch)
    assert "Rye loaf" in prompts
    assert "Write the page again" in prompts
    preview = message["attachment"]
    assert preview["name"] == "landing.html"
    assert "Oven Street Bakery" in preview["excerpt"]
    assert "Rye loaf" not in preview["excerpt"]
    assert "<" not in preview["excerpt"]


def _map_temp(monkeypatch, folder: Path) -> None:
    monkeypatch.setattr("easyagent.tools._placed_file", _redirect_writes(folder))


def test_a_repeated_read_does_not_end_a_short_page(tmp_path, monkeypatch):
    """The same read again, with nothing new on disk, does not stop a short page."""
    first = _hero_stack_page("Mill Road Bakery", ("Rye loaf", "Seed bun", "Morning cake"))
    final = _designed_model_page("Oven Street Bakery", "bakery")
    assert len(final) > len(first)
    page_folder = tmp_path.parent / f"ea-reread-{tmp_path.name}"
    page_folder.mkdir()
    _map_temp(monkeypatch, page_folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        f"```files\nwrite\nindex.html\n{first}\n```",
        "```files\nread\nC:\\work\\landing.html\n```",
        "```files\nread\nC:\\work\\landing.html\n```",
        f"```files\nwrite\nindex.html\n{final}\n```",
        "The page is ready.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a landing page for a bakery"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    text = (page_folder / "landing.html").read_text(encoding="utf-8")
    assert "Oven Street Bakery" in text
    assert "Rye loaf" not in text
    assert "Oven Street Bakery" in answer
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "Stuck" not in answer
    assert "Still undone" not in answer
    assert "The page is ready" not in answer
    assert "Rye loaf" not in answer
    assert "<!DOCTYPE" not in answer
    prompts = "\n".join(item.get("content") or "" for batch in recorder.seen[before:] for item in batch)
    assert "Rye loaf" in prompts
    assert "Write the page again" in prompts
    assert len(recorder.seen) == before + 5


def test_a_second_write_with_the_same_head_is_progress(tmp_path, monkeypatch):
    """A shared head is not the same page. The second write runs, and the bubble names the title."""
    from easyagent.loop import arg_key, near_keys
    from easyagent.tools import _is_short_page_draft

    head = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Corner Bakery</title>
<style>
  body { margin: 0; font-family: Georgia, serif; color: #243126; background: #f4efe6; }
  .hero { padding: 64px 24px; }
  .room { padding: 16px 24px; border-bottom: 1px solid #ddd; }
  a { color: #1d4e39; text-decoration: none; }
  main { display: flex; gap: 24px; }
  h1 { font-size: 3rem; }
</style>
</head>
<body>
"""
    first = head + """<section class="hero">
<h1>Mill Road Bakery</h1>
<p>One paragraph about the place, with the town at the door.</p>
<a href="#book">Reserve</a>
</section>
<section class="rooms">
<div class="room"><h2>Rye loaf</h2><p>A quiet corner.</p></div>
<div class="room"><h2>Seed bun</h2><p>A quiet corner.</p></div>
<div class="room"><h2>Morning cake</h2><p>A quiet corner.</p><p>From the morning.</p></div>
</section>
<footer><p>12 Lake Road</p><p>555-0142</p></footer>
</body>
</html>
"""
    final = head + """<header><svg width="80" height="40" viewBox="0 0 80 40"><rect x="0" y="10" width="80" height="30" fill="#16324a"/><circle cx="60" cy="12" r="8" fill="#f4e1c1"/></svg><h1>Oven Street Bakery</h1><p>On the corner since morning. The counter is open, the back room has a long table, and you can stay once you have a seat. People stop in on the way to work, and on Saturdays the line reaches the corner.</p></header>
<main>
<section>
<h2>The counter</h2>
<p>The front counter faces the window. People stop in on the way to work.</p>
<p>On Saturdays the line reaches the corner.</p>
</section>
<section>
<h2>The room in back</h2>
<p>The back room has a long table, and you can stay once you have a seat.</p>
</section>
</main>
<footer><p>Walk in, or write ahead.</p></footer>
</body>
</html>
"""
    assert _is_short_page_draft(first) is True
    assert _is_short_page_draft(final) is False
    assert "<img" not in first and "<svg" not in first
    assert "<svg" in final
    from easyagent.tools import page_is_plain

    assert page_is_plain(first) is True
    assert page_is_plain(final) is False
    key_a = arg_key("files", "write", r"C:\work\landing.html", "", "", first)
    key_b = arg_key("files", "write", r"C:\work\landing.html", "", "", final)
    assert near_keys(key_a, key_b) is True
    page_folder = tmp_path.parent / f"ea-head-{tmp_path.name}"
    page_folder.mkdir()
    _map_temp(monkeypatch, page_folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    assert len(final) > len(first)
    recorder.reply = [
        f"```files\nwrite\nindex.html\n{first}\n```",
        "```files\nread\nC:\\work\\landing.html\n```",
        f"```files\nwrite\nindex.html\n{final}\n```",
        "The page is ready.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a landing page for a bakery"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    text = (page_folder / "landing.html").read_text(encoding="utf-8")
    assert "Oven Street Bakery" in text
    assert "The counter" in text
    assert "Rye loaf" not in text
    assert "The page is Corner Bakery." in answer
    assert "Oven Street Bakery" not in answer
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "Stuck" not in answer
    assert "nearly the same" not in answer
    assert "Still undone" not in answer
    assert "The page is ready" not in answer
    assert "Rye loaf" not in answer
    assert "<!DOCTYPE" not in answer
    assert "Wrote " not in answer
    assert "Read " not in answer
    assert len(recorder.seen) == before + 4


def test_a_shorter_page_does_not_replace_a_fuller_one(tmp_path, monkeypatch):
    """A later write that is shorter leaves the fuller page on disk. The reply names that page."""
    fuller = _designed_model_page("Oven Street Bakery", "bakery")
    shorter = _hero_stack_page("Mill Road Bakery", ("Rye loaf", "Seed bun", "Morning cake"))
    assert len(fuller) > len(shorter)
    assert "Oven Street Bakery" in fuller
    assert "Rye loaf" in shorter
    page_folder = tmp_path.parent / f"ea-fuller-{tmp_path.name}"
    page_folder.mkdir()
    _map_temp(monkeypatch, page_folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        f"```files\nwrite\nindex.html\n{fuller}\n```",
        f"```files\nwrite\nindex.html\n{shorter}\n```",
        "The page is ready.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a landing page for a bakery"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    text = (page_folder / "landing.html").read_text(encoding="utf-8")
    assert "Oven Street Bakery" in text
    assert "The front counter" in text
    assert "Rye loaf" not in text
    assert "Mill Road Bakery" not in text
    assert "Oven Street Bakery" in answer
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "Stuck" not in answer
    assert "Still undone" not in answer
    assert "same shape" not in answer
    assert "Wrote " not in answer
    assert "Read " not in answer
    assert "The page is ready" not in answer
    assert "<!DOCTYPE" not in answer


def test_an_unnamed_page_lands_in_app_data_and_the_bubble_is_plain(tmp_path, monkeypatch):
    """The bubble names the hotel on the file. A different hardcoded name fails."""
    from easyagent.loop import page_matches
    from easyagent.tools import ToolRequest, _prepare_request

    ask = "can you build me a simple landing page for a hotel"
    page = _designed_model_page("The Harborview Hotel", "hotel").replace(
        "A small hotel with a quiet room for a stay. One room above the water.",
        "Rooms along the waterfront, with the harbor at the door. Harbor King from $140. Garden Twin from $165. Pier Suite from $260.",
    ).replace("Reserve a room", "Reserve Your Stay")
    assert "Harbor Hotel" not in page
    assert page_matches(page.encode("utf-8"), "hotel") is True
    prepared = _prepare_request(
        ToolRequest(kind="files", action="write", path=r"C:\Users\ada\EasyAgent\index.html", body=page),
        [{"role": "user", "content": ask}],
    )
    from easyagent.paths import deliverable_file

    assert prepared.path == deliverable_file("landing.html")
    assert "The Harborview Hotel" in prepared.body
    assert "Harbor Hotel" not in prepared.body

    folder = tmp_path.parent / f"ea-page-{tmp_path.name}"
    folder.mkdir()

    monkeypatch.setattr("easyagent.tools._placed_file", _redirect_writes(folder))
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        f"```files\nwrite\nindex.html\n{page}\n```",
        "Wrote index.html.\n\nProven: C1 The page file is on disk and is a hotel landing page.\nProven: C2 The page file is HTML.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": ask},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    target = folder / "landing.html"
    text = target.read_text(encoding="utf-8")
    assert message.get("error") is not True
    assert target.is_file()
    assert not (Path.cwd() / "index.html").exists()
    assert "The Harborview Hotel" in text
    assert "Harbor King from $140" in text
    assert "Reserve Your Stay" in text
    assert "Harbor Hotel" not in text
    heading = re.search(r"(?is)<h1>\s*(.*?)\s*</h1>", text).group(1)
    heading = " ".join(re.sub(r"<[^>]+>", " ", heading).split())
    assert heading == "The Harborview Hotel"
    assert heading in answer
    assert "Harbor Hotel" not in answer
    assert _written_page() in answer
    assert "you can open it" in answer.lower()
    assert "index.html" not in answer
    assert "Proven" not in answer
    assert "C1" not in answer
    assert "C2" not in answer
    assert "Evidence" not in answer
    assert "Wrote index.html" not in answer
    assert "<!DOCTYPE" not in answer
    assert "<h1>" not in answer
    assert "The syntax of the command is incorrect." not in answer
    preview = message["attachment"]
    assert preview["name"] == "landing.html"
    assert heading in preview["excerpt"]
    assert "Harbor Hotel" not in preview["excerpt"]
    assert "<!DOCTYPE" not in preview["excerpt"]
    assert "<html" not in preview["excerpt"].lower()
    assert "<h1" not in preview["excerpt"]
    assert len(preview["excerpt"]) < len(text)
    opened = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{preview['id']}")
    assert opened.status_code == 200
    assert heading.encode("utf-8") in opened.content
    assert b"Harbor Hotel" not in opened.content
    assert b"Reserve Your Stay" in opened.content


def test_an_image_request_continues_until_the_picture_is_on_disk(tmp_path, monkeypatch):
    from easyagent.tools import _is_raw_command_line

    folder = tmp_path.parent / f"ea-img-{tmp_path.name}"
    folder.mkdir()
    target = folder / "tower.png"
    row = "2617868    20   0  155.4g  85.4g  82.6g R 181.8  69.2  97:44.88 flash_s+"
    assert _is_raw_command_line(row)
    table = "\n".join([
        "top - 14:20:19 up 1 day,  2 users,  load average: 0.52, 0.58, 0.59",
        "PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND",
        row,
        "      1 root      20   0   16716   1236    820 S   0.0   0.0   0:01.11 systemd",
    ])
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        f"```files\nlist\n{folder}\n```",
        "```shell\nprintf side-step\n```",
        table,
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"can you make a picture of a tower at night and save it as tower.png in {folder}"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    answer = message["content"]
    assert target.is_file()
    assert target.stat().st_size > 0
    assert target.read_bytes().startswith(b"\x89PNG")
    assert f"Wrote {target}." not in answer
    assert "Proven" not in answer
    assert "C2" not in answer
    assert "Evidence" not in answer
    assert "tower.png" in answer
    assert str(folder) in answer
    assert "top -" not in answer
    assert "PID USER" not in answer
    assert "155.4g" not in answer
    assert "load average" not in answer
    assert "Stuck" not in answer
    assert answer.strip() != "The file was not written."
    preview = message["attachment"]
    assert preview["media_type"] == "image/png"
    assert preview["name"] == "tower.png"
    assert "excerpt" not in preview
    assert preview["path"].endswith("tower.png")
    opened = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{preview['id']}")
    assert opened.status_code == 200
    assert opened.content.startswith(b"\x89PNG")
    assert opened.headers["content-type"].startswith("image/png")
    page = Path("easyagent/static/app.js").read_text(encoding="utf-8")
    assert 'class: "shot"' in page
    assert 'startsWith("image/")' in page


def test_a_failed_command_still_leaves_the_picture_on_disk(tmp_path, monkeypatch):
    folder = tmp_path.parent / f"ea-img-fail-{tmp_path.name}"
    folder.mkdir()
    target = folder / "tower.png"
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [
        f"```files\nlist\n{folder}\n```",
        "```shell\neasyagent-missing-command\n```",
        "I have not made the picture.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": f"can you make a picture of a tower at night and save it as tower.png in {folder}"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert target.is_file()
    assert target.stat().st_size > 0
    assert target.read_bytes().startswith(b"\x89PNG")
    assert f"Wrote {target}." not in answer
    assert "Proven" not in answer
    assert "C2" not in answer
    assert "Evidence" not in answer
    assert "Stuck" not in answer


def test_two_calls_in_one_reply_run_together(tmp_path, monkeypatch):
    import asyncio

    order = []

    async def tracked(store, request, bot_id=None):
        order.append(("start", request.command))
        await asyncio.sleep(0.05)
        order.append(("end", request.command))
        return request.command

    monkeypatch.setattr("easyagent.tools.execute", tracked)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "```shell\necho ALPHA\n```\n```shell\necho BETA\n```",
        "Both commands finished.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "run both"},
    )
    assert sent.status_code == 200, sent.text
    assert order.index(("end", "echo ALPHA")) > order.index(("start", "echo BETA"))
    assert order.index(("end", "echo BETA")) > order.index(("start", "echo ALPHA"))
    assert len(recorder.seen) == before + 2


def test_a_heuristic_logs_its_name_when_it_fires(capsys):
    from easyagent.tools import _is_announcement, _needed_tool

    found = _needed_tool([{"role": "user", "content": "what's the top story on CNN right now?"}])
    assert found is not None and found.kind == "search"
    assert _is_announcement("Let me check the folder.")
    text = capsys.readouterr().out
    assert "heuristic _needed_tool:" in text
    assert "heuristic _is_announcement:" in text


def test_a_near_same_command_stops(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "```shell\necho SAME\n```",
        "```shell\necho  SAME\n```",
        "should not run",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "run echo twice"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "Stuck." in answer
    assert "nearly the same" in answer
    assert "should not run" not in answer
    assert len(recorder.seen) == before + 2


def test_quick_chat_does_not_write_a_contract(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = "Blue."
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "What color is the sky on a clear day?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert answer == "Blue."
    assert "Goal:" not in answer
    assert not (tmp_path / "bots" / bot["id"] / "notes" / "goal.md").exists()
    blob = "\n".join(item.get("content") or "" for item in recorder.seen[-1])
    assert "CONTRACT" not in blob
    assert "[[easyagent-working]]" not in blob
    raw = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert "CONTRACT" not in raw
    assert "[[easyagent-working]]" not in raw


def test_a_bare_not_written_sentence_is_not_a_finish(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = ["The file was not written.", "The file was not written."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "write the notes"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert answer.strip() != "The file was not written."
    assert "There was no path to write." in answer
    assert "Unproven" not in answer
    assert "Proven" not in answer
    assert "C1" not in answer
    assert "[[easyagent-working]]" not in answer
    blob = "\n".join(item.get("content") or "" for item in recorder.seen[-1])
    assert "CONTRACT" in blob
    assert "# PLAN" in blob
    assert "# EVIDENCE" in blob


def test_a_breaker_shrug_is_not_approval(tmp_path, monkeypatch):
    from easyagent.loop import judge_breaker

    assert judge_breaker("Looks fine.") is None
    assert judge_breaker("consider adding more detail") is None
    assert judge_breaker("C1: consider adding more detail. Evidence: it could be longer.") is None
    assert judge_breaker("NONE") is None
    from easyagent.night import concrete_counterexample

    assert concrete_counterexample("consider adding more detail", []) is None
    assert concrete_counterexample("variant: consider adding more detail", []) is None
    found = judge_breaker("C1: the claim has one source. Evidence: the second page contradicts it.")
    assert found is not None
    assert found.line == "C1"
    assert "one source" in found.detail
    assert "second page" in found.evidence

    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = ["The harbor closed, according to one page.", "Looks fine."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "research whether the harbor closed"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "Unproven" not in answer
    assert "Proven" not in answer
    assert "C1" not in answer
    assert "Evidence" not in answer
    assert "same model" in answer
    assert "Looks fine" not in answer
    assert len(recorder.seen) == before + 2


def test_ten_notes_are_read_and_the_summary_path_stays_a_file(tmp_path, monkeypatch):
    """The live failure: one write under the summary path, then a proven claim.

    The summary path was created as a directory. The only file was a paste of
    the request paths. The notes were never read.
    """
    from easyagent.loop import summary_job
    from easyagent.tools import ToolError, _file_parent_in, _write_path

    windows_notes = [rf"C:\work\notes\note-{index}.txt" for index in range(1, 11)]
    windows_summary = r"C:\work\summary.txt"
    windows_ask = "Read these ten note files and write a summary to " + windows_summary + "\n" + "\n".join(windows_notes)
    found_summary, found_notes = summary_job(windows_ask)
    assert found_summary == windows_summary
    assert found_notes == windows_notes
    nested_windows = windows_summary + r"\note-10.txt"
    assert _file_parent_in(nested_windows) == windows_summary

    root = tmp_path.parent / f"notes-{tmp_path.name}"
    notes = root / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    bits = []
    for index in range(1, 11):
        bit = f"Lane note {index} says MARK-{index}."
        (notes / f"note-{index}.txt").write_text(bit + "\n", encoding="utf-8")
        bits.append(bit)
    summary = root / "summary.txt"
    listed = "\n".join(str(notes / f"note-{index}.txt") for index in range(1, 11))
    nested = summary / "note-10.txt"
    try:
        _write_path(nested, listed)
    except ToolError as exc:
        assert "file path, not a folder" in str(exc)
    else:
        raise AssertionError("a file path was created as a folder")
    assert not summary.exists()

    ask = f"Read these ten note files and write a summary to {summary}\n{listed}"
    summary.mkdir()
    (summary / "note-10.txt").write_text(listed, encoding="utf-8")
    assert summary.is_dir()
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        f"```files\nwrite\n{nested}\n{listed}\n```",
        f"Wrote {nested} and C1 is proven.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": ask},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert summary.is_file()
    assert not summary.is_dir()
    assert not nested.exists()
    text = summary.read_text(encoding="utf-8")
    assert sum(1 for bit in bits if bit in text) >= 2
    assert listed.strip() != text.strip()
    assert not any(line.startswith("Read ") for line in answer.splitlines())
    assert f"Wrote {summary}." not in answer
    assert f"Wrote {nested}." not in answer
    assert "Proven" not in answer
    assert "Unproven" not in answer
    assert "C1" not in answer
    assert len(recorder.seen) > before + 1
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    roles = [item["role"] for item in stored["messages"] if item["content"] != "keep-this-line" and item["content"] != "ack"]
    assert roles == ["user", "assistant"]


def test_the_answer_after_a_tool_stays_in_the_bubble(tmp_path, monkeypatch):
    """Text, then a tool call, then the final text. That text is the bubble. A log line is not."""
    async def search(query):
        assert query == "newest stable Python"
        return (
            "1. Python releases\n"
            "https://www.python.org/downloads/\n"
            "Python 3.13 is the stable release.\n"
        )

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    seen = {"n": 0, "tool": False, "log_as_assistant": False}

    async def pieces(**kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            yield "I'll look up the current release.\n"
            yield "```search\nnewest stable Python\n```"
            return
        messages = kwargs["messages"]
        seen["tool"] = any(item.get("role") == "tool" for item in messages)
        seen["log_as_assistant"] = any(
            item.get("role") == "assistant"
            and (item.get("content") or "").strip() in {
                "Searched the web.",
                "Ran a command on this computer.",
            }
            or (
                item.get("role") == "assistant"
                and (
                    (item.get("content") or "").startswith("Read ")
                    or (item.get("content") or "").startswith("Wrote ")
                    or (item.get("content") or "").startswith("Ran a command")
                )
            )
            for item in messages
        )
        yield "The newest stable release is Python 3.13. The biggest change is clearer error messages."

    monkeypatch.setattr("easyagent.llm.stream_complete", pieces)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "what is the newest stable language release worth knowing"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    final = "The newest stable release is Python 3.13. The biggest change is clearer error messages."
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    answer = stored["messages"][-1]["content"]
    assert final in answer
    assert final in body
    assert seen["n"] == 2
    assert seen["tool"] is True
    assert seen["log_as_assistant"] is False
    for banned in ("Searched the web.", "Ran a command", "Wrote ", "Read "):
        assert banned not in answer
        assert banned not in body


def test_a_failed_native_command_comes_back_and_the_loop_continues(tmp_path, monkeypatch):
    """Text, then a native tool_call that fails, then the model's answer. The error is not the bubble."""
    import easyagent.llm as llm

    captured = []
    scripts = [
        [
            'data: {"choices":[{"delta":{"content":"I will try the folder.\\n"}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_pwd","type":"function","function":{"name":"terminal","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"command\\": \\"pwd && ls\\"}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ],
        [
            'data: {"choices":[{"delta":{"content":"Windows does not have pwd, so that command could not run."}}]}',
            "data: [DONE]",
        ],
    ]

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def __init__(self, lines):
            self._lines = lines

        async def aiter_lines(self):
            for line in self._lines:
                yield line

        async def aread(self):
            return b""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aclose(self):
            return None

        def stream(self, method, url, json=None, headers=None):
            captured.append(json)

            class Context:
                async def __aenter__(self_inner):
                    lines = scripts.pop(0) if scripts else ["data: [DONE]"]
                    return FakeResponse(lines)

                async def __aexit__(self_inner, *args):
                    return False

            return Context()

    def fail(_store, command):
        from easyagent.tools import ToolError

        raise ToolError("'pwd' is not recognized as an internal or external command.")

    monkeypatch.setattr("easyagent.tools._run_shell", fail)
    original = llm.httpx.AsyncClient
    llm.httpx.AsyncClient = FakeClient
    try:
        client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
        url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
        with client.stream(
            "POST",
            url,
            json={"content": "make me a one-page site for a little coffee shop"},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            body = response.read().decode()
    finally:
        llm.httpx.AsyncClient = original
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    answer = stored["messages"][-1]["content"]
    sentence = "Windows does not have pwd, so that command could not run."
    assert sentence in answer
    assert sentence in body
    assert stored["messages"][-1].get("error") is not True
    assert "is not recognized" not in answer
    assert len(captured) == 2
    follow = captured[1]["messages"]
    assistant = [item for item in follow if item.get("role") == "assistant" and item.get("tool_calls")]
    tools = [item for item in follow if item.get("role") == "tool"]
    assert assistant and tools
    assert assistant[-1]["tool_calls"][0]["id"] == "call_pwd"
    assert tools[-1]["tool_call_id"] == "call_pwd"
    assert assistant[-1]["tool_calls"][0]["function"]["name"] == "terminal"
    assert "pwd && ls" in assistant[-1]["tool_calls"][0]["function"]["arguments"]
    assert "is not recognized" in tools[-1]["content"]
    assert not any(
        item.get("role") == "assistant" and (item.get("content") or "").startswith("Ran a command")
        for item in follow
    )


def test_an_unknown_computer_runs_on_this_computer(tmp_path, monkeypatch):
    """A made-up computer is this computer. The error is not the reply."""
    ran = []

    def shell(_store, command):
        ran.append(command)
        return "LOCAL-OK"

    monkeypatch.setattr("easyagent.tools._run_shell", shell)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "```windows\nmade-up-box\necho hi\n```",
        "It ran on this computer.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "run echo hi on the shop computer"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert sent.json()["chat"]["messages"][-1].get("error") is not True
    assert "It ran on this computer." in answer
    assert "No saved computer" not in answer
    assert ran == ["echo hi"]
    assert len(recorder.seen) == before + 2
    tools = [item for item in recorder.seen[-1] if item.get("role") == "tool"]
    calls = [item for item in recorder.seen[-1] if item.get("role") == "assistant" and item.get("tool_calls")]
    assert tools and calls
    assert tools[-1]["tool_call_id"] == calls[-1]["tool_calls"][0]["id"]
    assert "LOCAL-OK" in tools[-1]["content"]


def test_a_finish_with_no_answer_is_not_the_end(tmp_path, monkeypatch):
    """Search, then search, then finish with no text. The bubble is the model's answer, not a title."""
    title = "Node.js 24 LTS: 5 Features That Actually Change How You Write Code"
    queries = []

    async def search(query):
        queries.append(query)
        return f"1. {title}\nhttps://example.test/node\nA release note."

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    sentence = "Node 24 is the current LTS. The release changes how imports resolve."
    recorder.reply = [
        "```search\nnode lts\n```",
        "```search\nnode 24 features\n```",
        "```finish\nproven\n```",
        sentence,
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what changed in the newest node release"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert sentence in answer
    assert title not in answer
    assert "https://example.test" not in answer
    assert sent.json()["chat"]["messages"][-1].get("error") is not True
    assert queries == ["node lts", "node 24 features"]
    assert len(recorder.seen) == before + 4


def test_a_plain_chat_reply_is_one_call(tmp_path, monkeypatch):
    """A greeting with no tool call ends after one model call."""
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = "Morning! I'm EasyAgent. I'll help you look things up."
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Morning"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert answer == "Morning! I'm EasyAgent. I'll help you look things up."
    assert len(recorder.seen) == before + 1
    assert sent.json()["chat"]["messages"][-1].get("error") is not True


def test_a_guessed_version_is_searched_before_it_is_the_answer(tmp_path, monkeypatch):
    """A latest-version answer with no tool call is not the end. One search, then the model's words."""
    from datetime import date

    title = "Go Release Index"
    queries = []

    async def search(query):
        queries.append(query)
        return (
            f"1. {title}\n"
            "https://go.dev/dl\n"
            "The index lists 1.27.1 as the stable release.\n"
        )

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    guess = "The latest version of Go is 9.9.9, released yesterday."
    sentence = "Go 1.27.1 is the stable release. That is what the index lists."
    recorder.reply = [guess, sentence]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what's the latest version of Go?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert sentence in answer
    assert "9.9.9" not in answer
    assert title not in answer
    assert "https://go.dev" not in answer
    assert sent.json()["chat"]["messages"][-1].get("error") is not True
    assert queries == [f"latest version of Go {date.today().year}"]
    assert len(recorder.seen) == before + 2


def test_a_choice_question_is_the_reply(tmp_path, monkeypatch):
    """A should-I question ends on the model's question. No second call narrates the turn."""
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    reply = "Let me see — does your side project need a database?"
    recorder.reply = [
        reply,
        "The reply was already complete — it asked you a question and didn't promise to check anything on a computer.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "should I put the app on a VPS or just keep the free host for now?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert reply in answer
    assert "already complete" not in answer.lower()
    assert "promise to check" not in answer.lower()
    assert sent.json()["chat"]["messages"][-1].get("error") is not True
    assert len(recorder.seen) == before + 1


def test_a_needs_list_is_not_the_goal():
    from easyagent.tools import _goal_line

    spoken = (
        "The page needs a hero, a menu, hours, and a map. "
        "A short bike shop page is on disk."
    )
    done = r"A short shop page at C:\work\landing.html with real pictures."
    assert _goal_line(spoken) == ""
    assert _goal_line("The page needs a hero, a menu, hours, and a map.") == ""
    assert _goal_line("A small hotel on the water.") == ""
    assert _goal_line(spoken + " " + done) == done


def test_a_progress_line_is_not_the_goal():
    from easyagent.tools import _goal_line

    progress = r"Writing it now to C:\work\studio\index.html."
    done = r"A studio landing page at C:\work\landing.html with real pictures and no dead links."
    assert _goal_line(progress) == ""
    assert _goal_line(f"{progress} {done}") == done
    assert _goal_line("A bay hotel, written as one page.") == ""
    assert _goal_line("Let me write the page.") == ""
    assert _goal_line("I read the file. A short bike shop page is on disk.") == ""
    assert _goal_line("The file reads back cleanly from disk.") == ""
    assert _goal_line('I see two href="#" links.') == ""
    assert _goal_line(r"The page is Northwind Studio at C:\work\landing.html.") == (
        r"The page is Northwind Studio at C:\work\landing.html."
    )


def test_the_goal_is_the_finished_page_not_the_read_back(tmp_path, monkeypatch):
    """A progress line and a clean read-back are not the goal. The later finished sentence is."""
    from easyagent.tools import _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    folder = tmp_path.parent / f"ea-goal-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    done = r"A studio landing page at C:\work\landing.html with real pictures and no dead links."
    recorder.reply = [
        "Writing it now to C:\\work\\landing.html.\n"
        f"```files\nwrite\nindex.html\n{_gradient_page()}\n```",
        "The file reads back cleanly from disk.",
        f"{done}\n"
        f"```files\nwrite\nindex.html\n{_designed_model_page('Wildstem', 'florist')}\n```",
        "The page is ready.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "build a simple landing page for a florist"},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["chat"]["messages"][-1].get("error") is not True
    goal = (
        tmp_path / "bots" / bot["id"] / "notes" / "chats" / chat["id"] / "goal.md"
    ).read_text(encoding="utf-8")
    assert goal.strip() == "# Goal\n\n" + done
    assert "reads back" not in goal.lower()
    assert "Writing it now" not in goal


def test_a_lasting_fact_the_model_said_is_filed(tmp_path, monkeypatch):
    """A sentence the model actually wrote is one memory line. Chatter is not."""
    from easyagent.tools import _is_lasting_fact, _lasting_sentence

    assert _lasting_sentence(
        "I read the file. MARK-BOOK is the booking line, under the hero drawing."
    ) == ""
    assert _is_lasting_fact("Nothing else is missing, so I'm leaving the file as it is.") is False
    assert _is_lasting_fact("The lower half is the cedar loft, not another hero.") is False
    assert _is_lasting_fact("The dates control uses a placeholder link.") is False
    assert _is_lasting_fact(r"a page with no folder is written at C:\work\landing.html") is False

    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = "pwd is not a Windows command. Use the file tool on this computer."
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what should stay in mind from that"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "pwd is not a Windows command." in answer
    memory = (tmp_path / "bots" / bot["id"] / "notes" / "MEMORY.md").read_text(encoding="utf-8")
    assert "pwd is not a Windows command." in memory
    assert "Use the file tool" not in memory
    notes = list((tmp_path / "bots" / bot["id"] / "memory").glob("*.txt"))
    assert notes
    assert any("pwd is not a Windows command." in path.read_text(encoding="utf-8") for path in notes)


def test_a_missing_image_keeps_the_page_turn_open(tmp_path, monkeypatch):
    """An img src that does not return 200 is a dead link. The turn stays open until it is replaced."""
    from easyagent.tools import _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    async def status(url: str) -> int:
        if "photo-missing" in url:
            return 404
        return 200

    monkeypatch.setattr("easyagent.search.web_search", search)
    monkeypatch.setattr("easyagent.tools._asset_status", status)

    def page(src: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>North Ferry Cafe</title>
<style>
  body {{ margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }}
  .hero {{ display: grid; min-height: 70vh;
    background: url("https://images.example.test/photo-bay") center/cover; }}
  main {{ display: flex; gap: 16px; }}
</style>
</head>
<body>
<header class="hero"><h1>Coffee by the water</h1></header>
<main>
<section><h2>Menu</h2><p>A short list.</p></section>
<section><h2>Visit</h2><p><img src="{src}" alt=""></p></section>
</main>
</body>
</html>
"""

    folder = tmp_path.parent / f"ea-img-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "I'll write the page, then look at the pictures.\n"
        f"```files\nwrite\nindex.html\n{page('https://images.example.test/photo-missing')}\n```",
        "The page is on disk at C:\\work\\landing.html. "
        "The name on the page is North Ferry Cafe. You can open it in your browser.",
        "One picture address did not load. I'll point it at a picture that does.\n"
        f"```files\nwrite\nindex.html\n{page('https://images.example.test/photo-bay')}\n```",
        "The picture address now loads. "
        "You can open C:\\work\\landing.html to see North Ferry Cafe.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "can you build me a simple landing page for a cafe"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "One picture address did not load." in answer
    assert "photo-missing" not in answer
    text = (folder / "landing.html").read_text(encoding="utf-8")
    assert "photo-missing" not in text
    assert "photo-bay" in text
    follow = recorder.seen[before + 2]
    users = [item.get("content") or "" for item in follow if item.get("role") == "user"]
    assert any("did not return 200" in item and "photo-missing" in item for item in users)


def test_a_gradient_only_page_stays_open(tmp_path, monkeypatch):
    """A page of CSS gradients is not done. No named folder still lands in the app-data folder."""
    from easyagent.tools import _RESEARCH_CACHE

    _RESEARCH_CACHE.clear()

    async def search(_query):
        return ""

    monkeypatch.setattr("easyagent.search.web_search", search)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Hearth Lane Bakery — Morning Bread</title>
<style>
  body { margin: 0; color: #1c1915; background: #f4efe6; font-size: 18px; }
  .hero { display: grid; min-height: 70vh; color: #f7f3ea; background: linear-gradient(#143, #321); }
  main { display: flex; gap: 16px; }
</style>
</head>
<body>
<header class="hero"><h1>Bread from the corner oven</h1><p>Rye in the morning.</p></header>
<main><section><h2>Today</h2><p>A loaf and a bun.</p></section></main>
</body>
</html>
"""
    pictured = page.replace(
        "</body>",
        '<svg width="160" height="80" viewBox="0 0 160 80"><rect x="0" y="0" width="160" height="80" fill="#16324a"/></svg>\n</body>',
        1,
    )
    folder = tmp_path.parent / f"ea-bakery-{tmp_path.name}"
    folder.mkdir()
    _map_temp(monkeypatch, folder)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        "I'll put the page in one file.\n"
        f"```files\nwrite\nC:\\tmp\\bakery\\index.html\n{page}\n```",
        "The page is done. No external images or scripts.",
        "A gradient is not a picture. I'll draw one in the file.\n"
        f"```files\nwrite\nindex.html\n{pictured}\n```",
        "The ovens are on the page now.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "build a simple one-page site for a bakery"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert sent.json()["chat"]["messages"][-1].get("error") is not True
    assert len(recorder.seen) == before + 4
    nudge = "\n".join(
        item.get("content") or ""
        for item in recorder.seen[before + 2]
        if item.get("role") == "user"
    )
    assert "A CSS gradient is not a picture." in nudge
    text = (folder / "landing.html").read_text(encoding="utf-8")
    assert "<svg" in text
    assert "linear-gradient" in text
    assert answer.endswith(
        _page_closer("Hearth Lane Bakery")
    )
    assert "The file is index.html" not in answer
    assert "/tmp" not in answer.replace(_written_page(), "")
    assert "Morning Bread" not in answer.split("The page is Hearth Lane Bakery.")[-1]


def test_an_old_release_after_search_is_not_the_latest(tmp_path, monkeypatch):
    """A latest-release answer that cites an older hit stays open for the current one."""
    from datetime import date

    year = date.today().year
    old_year = year - 1
    queries = []

    async def search(query):
        queries.append(query)
        if len(queries) == 1:
            return (
                "1. Announcing Rust 1.90.0\n"
                f"https://blog.rust-lang.org/{old_year}/09/18/Rust-1.90.0.html\n"
                f"Rust 1.90.0 was released on {old_year}-09-18. Async closures.\n"
            )
        return (
            "1. Announcing Rust 1.99.0\n"
            f"https://blog.rust-lang.org/{year}/10/01/Rust-1.99.0.html\n"
            f"Rust 1.99.0 was released on {year}-10-01. Headline changes are listed on the blog.\n"
        )

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    stale = (
        f"Rust 1.90.0 ({old_year}-09-18) is the latest stable release. "
        "Async closures landed in it."
    )
    current = (
        f"Rust 1.99.0 ({year}-10-01) is the latest stable release. "
        f"https://blog.rust-lang.org/{year}/10/01/Rust-1.99.0.html "
        "The headline changes are on that page."
    )
    recorder.reply = [stale, stale, current]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what's the latest stable Rust release and what are the headline changes?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert sent.json()["chat"]["messages"][-1].get("error") is not True
    assert current in answer
    assert "1.90.0" not in answer
    assert f"https://blog.rust-lang.org/{year}/10/01/Rust-1.99.0.html" in answer
    assert len(queries) == 2
    assert "latest stable" in queries[0]
    assert str(year) in queries[1]
    assert "official" in queries[1].lower()
    nudge = "\n".join(
        item.get("content") or ""
        for item in recorder.seen[before + 2]
        if item.get("role") == "user"
    )
    assert "older release" in nudge
    assert len(recorder.seen) == before + 3


def test_a_running_command_stops_when_the_turn_is_cancelled(tmp_path):
    """A command still running is killed when the person sends again."""
    import asyncio

    from easyagent import turn as turn_mod
    from easyagent.store import Store
    from easyagent.tools import _run_shell

    store = Store(tmp_path)
    store.ensure()

    async def run():
        turn_mod.bind(store, "chat-1")
        task = asyncio.create_task(asyncio.to_thread(_run_shell, store, "sleep 30"))
        await asyncio.sleep(0.3)
        turn_mod.interrupt(store, "chat-1")
        with pytest.raises(turn_mod.TurnCancelled):
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(run())
