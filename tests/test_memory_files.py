"""Memory is an index plus topic files. A turn reads the index, then one topic."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from easyagent.app import create_app
from tests.test_tools import Recorder, _chat_bytes, _world


def _memory_dir(root: Path, bot_id: str) -> Path:
    return root / "bots" / bot_id / "memory"


def test_a_flat_list_moves_into_a_topic_file_and_chats_stay(tmp_path, monkeypatch):
    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    legacy = tmp_path / "bots" / bot["id"] / "memory.json"
    line_id = "11111111-1111-4111-8111-111111111111"
    legacy.write_text(
        json.dumps([
            {"id": line_id, "text": "likes paper notes", "created_at": "2026-10-03T00:00:00+00:00"},
        ]),
        encoding="utf-8",
    )
    before = _chat_bytes(tmp_path, bot["id"])
    listed = client.get(f"/api/bots/{bot['id']}/memory").json()
    assert listed[0]["topic"] == "examples"
    assert "example" in listed[0]["text"].lower()
    assert listed[1] == {
        "id": line_id,
        "text": "likes paper notes",
        "topic": "saved",
        "created_at": "2026-10-03T00:00:00+00:00",
    }
    assert not legacy.exists()
    folder = _memory_dir(tmp_path, bot["id"])
    index = (folder / "index.txt").read_text(encoding="utf-8")
    assert index == "examples\nsaved\n"
    assert "likes paper notes" not in index
    topic = (folder / "saved.txt").read_text(encoding="utf-8")
    assert "likes paper notes" in topic
    assert line_id in topic
    assert _chat_bytes(tmp_path, bot["id"]) == before
    assert chat["id"]


def test_the_prompt_has_the_index_and_not_every_topic(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    first = client.post(
        f"/api/bots/{bot['id']}/memory",
        json={"text": "MARGIN-TOKEN-WIDE", "topic": "margin notes"},
    )
    second = client.post(
        f"/api/bots/{bot['id']}/memory",
        json={"text": "KILN-TOKEN-HOT", "topic": "kiln heat"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    recorder.reply = "ok"
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello"},
    )
    assert sent.status_code == 200, sent.text
    system = recorder.seen[-1][0]["content"]
    assert "margin-notes" in system
    assert "kiln-heat" in system
    blob = json.dumps(recorder.seen[-1])
    assert "MARGIN-TOKEN-WIDE" not in blob
    assert "KILN-TOKEN-HOT" not in blob


def test_reading_one_topic_does_not_open_the_other(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    client.post(f"/api/bots/{bot['id']}/memory", json={"text": "MARGIN-TOKEN-WIDE", "topic": "margin notes"})
    client.post(f"/api/bots/{bot['id']}/memory", json={"text": "KILN-TOKEN-HOT", "topic": "kiln heat"})
    recorder.reply = ["```memory\nread\nmargin-notes\n```", "The margin is wide."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what did I say about the margin?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "Read memory topic" not in answer
    assert "The margin is wide." in answer
    assert "MARGIN-TOKEN-WIDE" not in answer
    assert "KILN-TOKEN-HOT" not in answer
    follow = "\n".join(item.get("content") or "" for item in recorder.seen[-1] if item.get("role") == "tool")
    assert "MARGIN-TOKEN-WIDE" in follow
    assert "KILN-TOKEN-HOT" not in follow


def test_file_new_move_and_a_pointer_keep_the_other_lines(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    client.post(f"/api/bots/{bot['id']}/memory", json={"text": "MARGIN-TOKEN-WIDE", "topic": "margin notes"})
    client.post(f"/api/bots/{bot['id']}/memory", json={"text": "keeps the desk clear", "topic": "margin notes"})
    folder = _memory_dir(tmp_path, bot["id"])
    index_before = (folder / "index.txt").read_text(encoding="utf-8")

    recorder.reply = ["```memory\nfile\nmargin-notes\nkeeps a pencil nearby\n```", "Filed."]
    filed = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "remember the pencil"},
    )
    assert filed.status_code == 200, filed.text
    assert "Filed." in filed.json()["chat"]["messages"][-1]["content"]
    assert "Filed a line" not in filed.json()["chat"]["messages"][-1]["content"]
    assert (folder / "index.txt").read_text(encoding="utf-8") == index_before
    margin = (folder / "margin-notes.txt").read_text(encoding="utf-8")
    assert "keeps a pencil nearby" in margin
    assert "MARGIN-TOKEN-WIDE" in margin

    recorder.reply = ["```memory\nnew\nkiln heat\nKILN-TOKEN-HOT\n```", "Made it."]
    made = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "start a kiln topic"},
    )
    assert made.status_code == 200, made.text
    assert "kiln-heat" in (folder / "index.txt").read_text(encoding="utf-8")
    assert "KILN-TOKEN-HOT" in (folder / "kiln-heat.txt").read_text(encoding="utf-8")
    kiln_before = (folder / "kiln-heat.txt").read_bytes()

    recorder.reply = [
        "```memory\nalso\nkeeps a pencil nearby\nkiln-heat\n```",
        "Pointed.",
    ]
    pointed = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "that pencil line also belongs with the kiln"},
    )
    assert pointed.status_code == 200, pointed.text
    assert "Pointed." in pointed.json()["chat"]["messages"][-1]["content"]
    assert "Pointed a line" not in pointed.json()["chat"]["messages"][-1]["content"]
    assert (folder / "kiln-heat.txt").read_bytes() == kiln_before
    assert "also: kiln-heat" in (folder / "margin-notes.txt").read_text(encoding="utf-8")
    assert "keeps a pencil nearby" not in (folder / "kiln-heat.txt").read_text(encoding="utf-8")

    recorder.reply = [
        "```memory\nmove\nkeeps the desk clear\nmargin-notes\nkiln-heat\n```",
        "Moved.",
    ]
    moved = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "move the desk line"},
    )
    assert moved.status_code == 200, moved.text
    margin_after = (folder / "margin-notes.txt").read_text(encoding="utf-8")
    kiln_after = (folder / "kiln-heat.txt").read_text(encoding="utf-8")
    assert "keeps the desk clear" not in margin_after
    assert "MARGIN-TOKEN-WIDE" in margin_after
    assert "keeps a pencil nearby" in margin_after
    assert "keeps the desk clear" in kiln_after
    assert "KILN-TOKEN-HOT" in kiln_after


def test_a_full_topic_does_not_grow_and_another_bot_cannot_read_it(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    for number in range(8):
        saved = client.post(
            f"/api/bots/{bot['id']}/memory",
            json={"text": f"shelf line {number} stays", "topic": "shelf notes"},
        )
        assert saved.status_code == 200, saved.text
    folder = _memory_dir(tmp_path, bot["id"])
    topic_before = (folder / "shelf-notes.txt").read_bytes()
    index_before = (folder / "index.txt").read_bytes()
    chats_before = _chat_bytes(tmp_path, bot["id"])
    recorder.reply = "```memory\nfile\nshelf-notes\nshelf line extra stays\n```"
    full = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "file one more shelf line"},
    )
    assert full.status_code == 200, full.text
    message = full.json()["chat"]["messages"][-1]
    assert message.get("error") is not True
    assert "full" not in message["content"]
    fed = [item for item in recorder.seen[-1] if item.get("role") == "tool"]
    assert fed and "full" in fed[-1]["content"]
    assert (folder / "shelf-notes.txt").read_bytes() == topic_before
    assert (folder / "index.txt").read_bytes() == index_before
    assert "shelf line extra stays" not in (folder / "shelf-notes.txt").read_text(encoding="utf-8")

    other = client.post("/api/bots", json={"name": "Bea", "endpoint_id": client.get("/api/bots").json()[0]["endpoint_id"]}).json()
    other_chat = client.post(f"/api/bots/{other['id']}/chats").json()
    recorder.reply = "```memory\nread\nshelf-notes\n```"
    denied = client.post(
        f"/api/bots/{other['id']}/chats/{other_chat['id']}/messages",
        json={"content": "read the shelf topic"},
    )
    assert denied.status_code == 200, denied.text
    denied_message = denied.json()["chat"]["messages"][-1]
    assert denied_message.get("error") is not True
    assert "not in the index" not in denied_message["content"]
    fed = [item for item in recorder.seen[-1] if item.get("role") == "tool"]
    assert fed and "not in the index" in fed[-1]["content"]
    assert not (_memory_dir(tmp_path, other["id"]) / "shelf-notes.txt").exists()
    assert _chat_bytes(tmp_path, bot["id"]) != chats_before


def test_an_announcement_reads_the_one_named_topic(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    client.post(f"/api/bots/{bot['id']}/memory", json={"text": "MARGIN-TOKEN-WIDE", "topic": "margin notes"})
    client.post(f"/api/bots/{bot['id']}/memory", json={"text": "KILN-TOKEN-HOT", "topic": "kiln heat"})
    recorder.reply = ["Let me look at margin notes.", "The margin is wide."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what did I say about the margin?"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "Read memory topic" not in answer
    assert "The margin is wide." in answer
    assert "Let me look" not in answer
    assert "KILN-TOKEN-HOT" not in answer
    fed = "\n".join(item.get("content") or "" for item in recorder.seen[-1] if item.get("role") == "tool")
    assert "MARGIN-TOKEN-WIDE" in fed
    assert "KILN-TOKEN-HOT" not in fed


def test_a_turn_keeps_a_lesson_and_leaves_a_true_line(tmp_path, monkeypatch):
    """A fact that would change a later turn is written during the turn. Chatter is not. A true line stays."""
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    kept = client.post(
        f"/api/bots/{bot['id']}/memory",
        json={"text": "likes paper notes", "topic": "desk"},
    )
    old = client.post(
        f"/api/bots/{bot['id']}/memory",
        json={"text": "pages go in the app folder", "topic": "pages"},
    )
    assert kept.status_code == 200, kept.text
    assert old.status_code == 200, old.text
    notes = tmp_path / "bots" / bot["id"] / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "MEMORY.md").write_text(
        "# Memory\n\n- likes the window open\n- pages go in the app folder\n",
        encoding="utf-8",
    )
    recorder.reply = (
        "The old note says the page goes in the app folder. That is wrong. "
        "I'm replacing it with the lesson and keeping it short.\n\n"
        "```memory\n"
        "replace\n"
        "pages go in the app folder\n"
        "a page with no folder is written at C:\\work\\landing.html\n"
        "```"
    )
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "what should I remember about where pages are written"},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    assert "I'm replacing it with the lesson and keeping it short." in answer
    assert "```memory" not in answer
    assert "Filed a line" not in answer
    listed = client.get(f"/api/bots/{bot['id']}/memory").json()
    texts = [item["text"] for item in listed]
    assert "likes paper notes" in texts
    assert "pages go in the app folder" not in texts
    assert "a page with no folder is written at C:\\work\\landing.html" in texts
    assert "The old note says the page goes in the app folder" not in texts
    memory_md = (notes / "MEMORY.md").read_text(encoding="utf-8")
    assert "likes the window open" in memory_md
    assert "pages go in the app folder" not in memory_md
    assert "a page with no folder is written at C:\\work\\landing.html" in memory_md
    assert "I'm replacing it" not in memory_md

    before = memory_md
    topic_before = sorted((tmp_path / "bots" / bot["id"] / "memory").rglob("*.txt"))
    topic_bytes = {path.name: path.read_bytes() for path in topic_before}
    recorder.reply = (
        "The weather is fine today and nothing new was learned about the files.\n\n"
        "```memory\n"
        + ("This is chatter about the weather and the walk and the window. " * 8)
        + "\n```"
    )
    chatter = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "anything else"},
    )
    assert chatter.status_code == 200, chatter.text
    assert (notes / "MEMORY.md").read_text(encoding="utf-8") == before
    again = {path.name: path.read_bytes() for path in (tmp_path / "bots" / bot["id"] / "memory").rglob("*.txt")}
    assert again == topic_bytes
    edited = client.patch(
        f"/api/bots/{bot['id']}/memory/{[item['id'] for item in listed if item['text'].startswith('a page with no folder')][0]}",
        json={"text": "a page with no folder is written under C:\\work"},
    )
    assert edited.status_code == 200, edited.text
    final = client.get(f"/api/bots/{bot['id']}/memory").json()
    final_text = [item["text"] for item in final]
    assert "likes paper notes" in final_text
    assert "a page with no folder is written under C:\\work" in final_text
    assert "pages go in the app folder" not in final_text


def test_settings_show_the_index_and_one_topic(tmp_path, monkeypatch):
    client, bot, _chat, _recorder = _world(tmp_path, monkeypatch)
    page = client.get("/static/index.html").text
    assert "memory-index-list" in page
    assert "memory-form" in page
    assert "topic file" in page
    made = client.post(
        f"/api/bots/{bot['id']}/memory",
        json={"text": "MARGIN-TOKEN-WIDE", "topic": "margin notes"},
    )
    assert made.status_code == 200, made.text
    index = client.get(f"/api/bots/{bot['id']}/memory/index").json()
    assert {"name": "examples", "title": "examples", "count": 1} in index["topics"]
    assert {"name": "margin-notes", "title": "margin notes", "count": 1} in index["topics"]
    assert "MARGIN-TOKEN-WIDE" not in json.dumps(index)
    topic = client.get(f"/api/bots/{bot['id']}/memory/topics/margin-notes").json()
    assert topic["lines"][0]["text"] == "MARGIN-TOKEN-WIDE"
    changed = client.patch(
        f"/api/bots/{bot['id']}/memory/{topic['lines'][0]['id']}",
        json={"text": "MARGIN-TOKEN-CHANGED"},
    )
    assert changed.status_code == 200, changed.text
    again = client.get(f"/api/bots/{bot['id']}/memory/topics/margin-notes").json()
    assert again["lines"][0]["text"] == "MARGIN-TOKEN-CHANGED"
    assert len(again["lines"]) == 1
