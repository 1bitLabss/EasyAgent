"""A ```file fence holding only a path hands back a file already on the computer.

The fence is stripped in every case. A file is attached only when it exists,
is not empty, is under 1 MB, sits outside the data folder, and holds no secret.
"""

import base64

from fastapi.testclient import TestClient

from easyagent.app import create_app

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class Script:
    def __init__(self, replies):
        self.replies = list(replies)

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120, tools=None, **_kwargs):
        if not self.replies:
            return "ack"
        item = self.replies.pop(0)
        return item(messages) if callable(item) else item


def open_client(tmp_path, monkeypatch, replies):
    monkeypatch.setattr("easyagent.llm.complete", Script(replies))
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat


def send(client, bot, chat, content="Do it."):
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": content},
    )
    assert sent.status_code == 200, sent.text
    return sent.json()["chat"]["messages"][-1]


def outside(tmp_path):
    folder = tmp_path.parent / f"ea-handback-{tmp_path.name}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_path_only_fence_attaches_the_real_file(tmp_path, monkeypatch):
    folder = outside(tmp_path)
    picture = folder / "shot.png"
    picture.write_bytes(PNG)
    report = folder / "report.txt"
    report.write_bytes(b"quarterly numbers\n")
    client, bot, chat = open_client(
        tmp_path,
        monkeypatch,
        [
            f"Here is the picture.\n\n```file\n{picture}\n```",
            f"Report.\n\n```file\n{report}\n \n```",
            f"```file\n{picture}\n```",
        ],
    )

    reply = send(client, bot, chat)
    assert reply["content"] == "Here is the picture."
    assert "```file" not in reply["content"]
    assert reply["attachment"]["name"] == "shot.png"
    assert reply["attachment"]["media_type"] == "image/png"
    opened = client.get(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{reply['attachment']['id']}"
    )
    assert opened.status_code == 200, opened.text
    assert opened.content == PNG

    second = send(client, bot, chat, content="And the report?")
    assert second["content"] == "Report."
    assert second["attachment"]["name"] == "report.txt"
    assert second["attachment"]["media_type"] == "text/plain"
    handed = client.get(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{second['attachment']['id']}"
    )
    assert handed.status_code == 200, handed.text
    assert handed.content == b"quarterly numbers\n"

    only = send(client, bot, chat, content="Just the file.")
    assert only["content"] == "Attached shot.png."
    assert only["attachment"]["name"] == "shot.png"


def test_empty_body_fence_hands_the_path_straight_to_take_file():
    """The reply loop drops whitespace-only lines, so cover the empty-body branch here."""
    from easyagent.handoff import take_file

    visible, handed = take_file("Report.\n\n```file\nD:\\work\\report.txt\n \n```")
    assert visible == "Report."
    assert handed == {"name": "report.txt", "path": "D:\\work\\report.txt"}


def test_nonexistent_path_attaches_nothing_and_strips_the_fence(tmp_path, monkeypatch):
    folder = outside(tmp_path)
    missing = folder / "never-written.png"
    client, bot, chat = open_client(
        tmp_path,
        monkeypatch,
        [f"Look.\n\n```file\n{missing}\n```"],
    )
    reply = send(client, bot, chat)
    assert reply["content"] == "Look."
    assert "```file" not in reply["content"]
    assert reply.get("attachment") is None


def test_path_inside_the_data_folder_is_refused(tmp_path, monkeypatch):
    inside = tmp_path / "inside.txt"
    inside.write_text("store data\n", encoding="utf-8")
    client, bot, chat = open_client(
        tmp_path,
        monkeypatch,
        [f"Note.\n\n```file\n{inside}\n```"],
    )
    reply = send(client, bot, chat)
    assert reply["content"] == "Note."
    assert "```file" not in reply["content"]
    assert reply.get("attachment") is None


def test_file_over_one_megabyte_is_refused(tmp_path, monkeypatch):
    folder = outside(tmp_path)
    big = folder / "big.bin"
    big.write_bytes(b"x" * 1_000_001)
    client, bot, chat = open_client(
        tmp_path,
        monkeypatch,
        [f"Big one.\n\n```file\n{big}\n```"],
    )
    reply = send(client, bot, chat)
    assert reply["content"] == "Big one."
    assert "```file" not in reply["content"]
    assert reply.get("attachment") is None


def test_file_holding_a_secret_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("easyagent.tools.secret_strings", lambda _store: ["vault-secret-value-9133"])
    folder = outside(tmp_path)
    leaky = folder / "keys.txt"
    leaky.write_bytes(b"the key is vault-secret-value-9133\n")
    client, bot, chat = open_client(
        tmp_path,
        monkeypatch,
        [f"Keys.\n\n```file\n{leaky}\n```"],
    )
    reply = send(client, bot, chat)
    assert reply["content"] == "Keys."
    assert "vault-secret-value-9133" not in reply["content"]
    assert "```file" not in reply["content"]
    assert reply.get("attachment") is None


def test_body_fence_still_attaches_text_plain(tmp_path, monkeypatch):
    client, bot, chat = open_client(
        tmp_path,
        monkeypatch,
        ["Here is the copy.\n\n```file\nback.txt\nhanded-back-mark\n```"],
    )
    reply = send(client, bot, chat)
    assert reply["content"] == "Here is the copy."
    assert reply["attachment"]["name"] == "back.txt"
    assert reply["attachment"]["media_type"] == "text/plain"
    handed = client.get(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{reply['attachment']['id']}"
    )
    assert handed.status_code == 200, handed.text
    assert handed.content == b"handed-back-mark"


def test_bare_word_fence_is_stripped_without_attachment(tmp_path, monkeypatch):
    client, bot, chat = open_client(
        tmp_path,
        monkeypatch,
        ["Noted.\n\n```file\nnotes\n```"],
    )
    reply = send(client, bot, chat)
    assert reply["content"] == "Noted."
    assert "```file" not in reply["content"]
    assert reply.get("attachment") is None
