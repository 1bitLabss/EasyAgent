"""Projects are named piles of files. A group project is not a room."""

from pathlib import Path

from fastapi.testclient import TestClient

from easyagent.app import create_app


class Recorder:
    def __init__(self, reply="ack"):
        self.reply = reply
        self.seen = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.seen.append(messages)
        reply = self.reply
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else "ack"
        return reply(messages) if callable(reply) else reply


def _chats(root: Path) -> dict[str, bytes]:
    folder = root / "bots"
    if not folder.is_dir():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(folder.glob("*/chats/*.json"))
        if path.is_file()
    }


def _rooms(root: Path) -> dict[str, bytes]:
    folder = root / "rooms"
    if not folder.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in sorted(folder.glob("*.json")) if path.is_file()}


def _world(tmp_path, monkeypatch, reply="ack"):
    recorder = Recorder(reply)
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    ada = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    bea = client.post("/api/bots", json={"name": "Bea", "endpoint_id": endpoint["id"]}).json()
    ada_chat = client.post(f"/api/bots/{ada['id']}/chats").json()
    bea_chat = client.post(f"/api/bots/{bea['id']}/chats").json()
    for bot, chat, text in ((ada, ada_chat, "ada-stays"), (bea, bea_chat, "bea-stays")):
        sent = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": text},
        )
        assert sent.status_code == 200, sent.text
    return client, ada, bea, ada_chat, bea_chat, recorder


def test_bot_project_upload_does_not_rewrite_chats_and_only_that_bot_can_read(tmp_path, monkeypatch):
    client, ada, bea, ada_chat, bea_chat, recorder = _world(tmp_path, monkeypatch)
    before = _chats(tmp_path)
    rooms_before = _rooms(tmp_path)
    created = client.post(f"/api/bots/{ada['id']}/projects", json={"name": "Kiln notes"})
    assert created.status_code == 200, created.text
    project = created.json()
    assert project["kind"] == "bot"
    assert project["bot_id"] == ada["id"]
    body = b"the kiln fires overnight and the door stays shut the whole time"
    uploaded = client.post(
        f"/api/bots/{ada['id']}/projects/{project['id']}/files",
        files=[("files", ("glaze.txt", body, "text/plain"))],
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["files"][0]["name"] == "glaze.txt"
    assert body not in uploaded.content
    assert _chats(tmp_path) == before
    assert _rooms(tmp_path) == rooms_before
    assert (tmp_path / "projects" / project["id"] / "files").is_dir()

    recorder.reply = [
        "```project\nread\nKiln notes\nglaze.txt\n```",
        "It fires overnight.",
    ]
    sent = client.post(
        f"/api/bots/{ada['id']}/chats/{ada_chat['id']}/messages",
        json={"content": "what does glaze.txt in Kiln notes say?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "Read glaze.txt" not in answer
    assert "It fires overnight." in answer
    assert b"the kiln fires overnight" not in sent.content
    assert "the kiln fires overnight" in "\n".join(
        item.get("content") or "" for item in recorder.seen[-1] if item.get("role") == "tool"
    )
    assert "```project" not in answer

    recorder.reply = ["```project\nread\nKiln notes\nglaze.txt\n```"]
    denied = client.post(
        f"/api/bots/{bea['id']}/chats/{bea_chat['id']}/messages",
        json={"content": "read glaze.txt in Kiln notes"},
    )
    assert denied.status_code == 200, denied.text
    hidden = denied.json()["chat"]["messages"][-1]
    assert hidden.get("error") is not True
    assert "not available" not in hidden["content"]
    fed = [item for item in recorder.seen[-1] if item.get("role") == "tool"]
    assert fed and "not available" in fed[-1]["content"]
    assert "the kiln fires overnight" not in hidden["content"]
    raw = (tmp_path / "bots" / bea["id"] / "chats" / f"{bea_chat['id']}.json").read_text(encoding="utf-8")
    assert "the kiln fires overnight" not in raw

    wrong = client.request(
        "DELETE",
        f"/api/bots/{ada['id']}/projects/{project['id']}",
        json={"confirm_name": "kiln"},
    )
    assert wrong.status_code == 400
    assert (tmp_path / "projects" / project["id"] / "project.json").is_file()
    after_turns = _chats(tmp_path)
    removed_file = client.delete(
        f"/api/bots/{ada['id']}/projects/{project['id']}/files/{uploaded.json()['files'][0]['id']}"
    )
    assert removed_file.status_code == 200, removed_file.text
    assert removed_file.json()["files"] == []
    assert _chats(tmp_path) == after_turns


def test_group_project_is_not_a_room_and_only_chosen_bots_can_read(tmp_path, monkeypatch):
    client, ada, bea, ada_chat, _bea_chat, recorder = _world(tmp_path, monkeypatch)
    room = client.post("/api/rooms", json={"name": "Desk"}).json()
    added = client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": ada["id"]})
    assert added.status_code == 200, added.text
    added = client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": bea["id"]})
    assert added.status_code == 200, added.text
    room_path = tmp_path / "rooms" / f"{room['id']}.json"
    room_bytes = room_path.read_bytes()
    chats = _chats(tmp_path)

    created = client.post("/api/projects", json={"name": "Shop manual"})
    assert created.status_code == 200, created.text
    project = created.json()
    assert project["kind"] == "group"
    assert project["bot_ids"] == []
    assert not str(tmp_path / "projects" / project["id"]).startswith(str(tmp_path / "rooms"))
    member = client.post(f"/api/projects/{project['id']}/bots", json={"bot_id": ada["id"]})
    assert member.status_code == 200, member.text
    assert member.json()["bot_ids"] == [ada["id"]]
    note = b"bring the blue folder from the shelf before the shift starts tonight"
    uploaded = client.post(
        f"/api/projects/{project['id']}/files",
        files=[
            ("files", ("shared-note.txt", note, "text/plain")),
            ("files", ("extra.txt", b"a second page of the same manual for the shelf", "text/plain")),
        ],
    )
    assert uploaded.status_code == 200, uploaded.text
    names = {item["name"] for item in uploaded.json()["files"]}
    assert names == {"shared-note.txt", "extra.txt"}
    assert note not in uploaded.content
    assert room_path.read_bytes() == room_bytes
    assert _chats(tmp_path) == chats
    assert b'"bot_ids"' in room_bytes and ada["id"].encode() in room_bytes

    recorder.reply = [
        "Let me look at that.",
        "Bring the blue folder.",
    ]
    calls_before = len(recorder.seen)
    sent = client.post(
        f"/api/bots/{ada['id']}/chats/{ada_chat['id']}/messages",
        json={"content": "what does shared-note.txt in Shop manual say?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "Read shared-note.txt" not in answer
    assert "Bring the blue folder." in answer
    assert "Bring the blue folder." in answer
    assert "Let me look" not in answer
    assert b"bring the blue folder" not in sent.content
    assert "bring the blue folder" in "\n".join(
        item.get("content") or "" for item in recorder.seen[-1] if item.get("role") == "tool"
    )
    assert len(recorder.seen) == calls_before + 2

    after_turns = _chats(tmp_path)
    dropped = client.delete(f"/api/projects/{project['id']}/bots/{ada['id']}")
    assert dropped.status_code == 200, dropped.text
    assert ada["id"] not in dropped.json()["bot_ids"]
    assert room_path.read_bytes() == room_bytes
    assert _chats(tmp_path) == after_turns
    room_again = client.get(f"/api/rooms/{room['id']}").json()
    assert ada["id"] in room_again["bot_ids"]
    assert bea["id"] in room_again["bot_ids"]

    room_path.unlink()
    still = client.get(f"/api/projects/{project['id']}")
    assert still.status_code == 200, still.text
    assert {item["name"] for item in still.json()["files"]} == names
    assert client.get("/api/rooms").json() == []

    page = client.get("/")
    assert page.status_code == 200
    assert 'id="screen-projects"' in page.text
    assert 'id="bot-project-form"' in page.text
    assert 'id="nav-projects"' in page.text
    assert "A group project is not a room" in page.text
    assert "Create project" in page.text
    chat_at = page.text.index('id="screen-chat"')
    settings_at = page.text.index('id="screen-settings"')
    rooms_at = page.text.index('id="screen-rooms"')
    computers_at = page.text.index('id="screen-computers"')
    assert chat_at < settings_at < rooms_at < computers_at
