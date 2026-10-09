"""Questions, memory, chat files, one-shot notices, and an uninstalled night proposal."""

import asyncio
import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.llm import ProviderError
from easyagent.schedule import run_due_schedules
from easyagent.store import Store

ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
QUESTION = (
    "<function_calls>\n"
    "<invoke name=\"clarify\">\n"
    "<parameter name=\"question\">Which folder?</parameter>\n"
    "<parameter name=\"choices\">[\"src\", \"tests\"]</parameter>\n"
    "</invoke>\n"
    "</function_calls>"
)


def changelog() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def chat_file(root: Path, bot_id: str, chat_id: str) -> Path:
    return root / "bots" / bot_id / "chats" / f"{chat_id}.json"


def memory_bytes(root: Path, bot_id: str) -> dict[str, bytes]:
    base = root / "bots" / bot_id
    found = {}
    legacy = base / "memory.json"
    if legacy.is_file():
        found["memory.json"] = legacy.read_bytes()
    folder = base / "memory"
    if folder.is_dir():
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.is_symlink():
                found[str(path.relative_to(base))] = path.read_bytes()
    return found


def skill_bytes(root: Path) -> dict[str, bytes]:
    folder = root / "skills"
    if not folder.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in sorted(folder.glob("*.md"))}


def open_client(tmp_path, monkeypatch, reply):
    monkeypatch.setattr("easyagent.llm.complete", reply)
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    return client, bot, chat


class Script:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.seen.append(messages)
        if not self.replies:
            return "ack"
        item = self.replies.pop(0)
        return item(messages) if callable(item) else item


def test_question_is_tappable_and_the_reply_is_stored(tmp_path, monkeypatch):
    script = Script([QUESTION, "noted", "heard"])
    client, bot, chat = open_client(tmp_path, monkeypatch, script)
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Where should this go?"},
    )
    assert sent.status_code == 200, sent.text
    asked = sent.json()["chat"]["messages"]
    assert asked[1]["role"] == "assistant"
    assert asked[1]["content"] == "Which folder?"
    assert asked[1]["choices"] == ["src", "tests"]
    path = chat_file(tmp_path, bot["id"], chat["id"])
    raw = path.read_text(encoding="utf-8")
    assert "function_calls" not in raw
    assert "<invoke" not in raw

    picked = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "src"},
    )
    assert picked.status_code == 200, picked.text
    after_pick = picked.json()["chat"]["messages"]
    assert after_pick[2]["role"] == "user"
    assert after_pick[2]["content"] == "src"
    assert "choices" not in after_pick[2]

    typed = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "the attic"},
    )
    assert typed.status_code == 200, typed.text
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert [item["content"] for item in stored["messages"]] == [
        "Where should this go?",
        "Which folder?",
        "src",
        "noted",
        "the attic",
        "heard",
    ]
    assert stored["messages"][1]["choices"] == ["src", "tests"]
    assert "function_calls" not in path.read_text(encoding="utf-8")

    page = client.get("/static/app.js").text
    assert 'class: "choice"' in page
    assert "pickChoice" in page
    assert "Or type an answer" in page
    assert "choices to tap" in changelog()


def test_memory_edit_and_drop_leave_the_other_line_and_the_skill(tmp_path, monkeypatch):
    script = Script(
        [
            "Noted.\n\n```memory\nprefers a wide margin\n```",
            "Still noted.\n\n```memory\nlikes paper notes\n```",
        ]
    )
    client, bot, chat = open_client(tmp_path, monkeypatch, script)
    saved = client.post(
        "/api/skills",
        json={"name": "desk-notes", "description": "notes", "body": "Keep the desk note."},
    )
    assert saved.status_code == 200, saved.text
    skills = skill_bytes(tmp_path)
    transcript = chat_file(tmp_path, bot["id"], chat["id"]).read_bytes()

    first = client.post(f"/api/bots/{bot['id']}/memory", json={"text": "likes paper notes"}).json()
    second = client.post(f"/api/bots/{bot['id']}/memory", json={"text": "uses two fonts"}).json()
    patched = client.patch(
        f"/api/bots/{bot['id']}/memory/{second['id']}",
        json={"text": "uses two fonts on the page"},
    )
    assert patched.status_code == 200, patched.text
    listed = client.get(f"/api/bots/{bot['id']}/memory").json()
    example = listed[0]
    assert "example" in example["text"].lower()
    assert [(item["id"], item["text"]) for item in listed] == [
        (example["id"], example["text"]),
        (first["id"], "likes paper notes"),
        (second["id"], "uses two fonts on the page"),
    ]
    assert chat_file(tmp_path, bot["id"], chat["id"]).read_bytes() == transcript
    assert skill_bytes(tmp_path) == skills

    dropped = client.delete(f"/api/bots/{bot['id']}/memory/{second['id']}")
    assert dropped.status_code == 200, dropped.text
    left = client.get(f"/api/bots/{bot['id']}/memory").json()
    assert left == [example, first]
    assert chat_file(tmp_path, bot["id"], chat["id"]).read_bytes() == transcript
    assert skill_bytes(tmp_path) == skills

    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "remember the margin"},
    )
    assert sent.status_code == 200, sent.text
    grown = client.get(f"/api/bots/{bot['id']}/memory").json()
    assert grown[0] == example
    assert grown[1] == first
    assert grown[2]["text"] == "prefers a wide margin"
    assert "```memory" not in sent.json()["chat"]["messages"][1]["content"]
    assert skill_bytes(tmp_path) == skills

    before = memory_bytes(tmp_path, bot["id"])
    again = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "say it again"},
    )
    assert again.status_code == 200, again.text
    assert memory_bytes(tmp_path, bot["id"]) == before
    assert client.get(f"/api/bots/{bot['id']}/memory").json()[0] == example
    assert skill_bytes(tmp_path) == skills

    page = client.get("/static/app.js").text
    assert "Change" in page
    assert "Drop" in page
    assert "memory-form" in client.get("/static/index.html").text
    assert "does not rewrite a skill" in changelog()


def test_attached_file_is_shown_to_the_bot_and_a_handed_file_stays(tmp_path, monkeypatch):
    handoff = "Here is the copy.\n\n```file\nback.txt\nhanded-back-mark\n```"
    script = Script(["I can see the notes.", handoff])
    client, bot, chat = open_client(tmp_path, monkeypatch, script)
    uploaded = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/files",
        files={"file": ("notes.txt", b"visible-file-mark", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    file_id = uploaded.json()["id"]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Look at the notes.", "attachment_id": file_id},
    )
    assert sent.status_code == 200, sent.text
    user = sent.json()["chat"]["messages"][0]
    assert user["content"] == "Look at the notes."
    assert "/" not in user["content"]
    assert user["attachment"]["id"] == file_id
    assert user["attachment"]["name"] == "notes.txt"
    kept = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{file_id}")
    assert kept.status_code == 200, kept.text
    assert kept.content == b"visible-file-mark"
    shown = script.seen[0][-1]["content"]
    assert "visible-file-mark" in shown
    assert "Attached file notes.txt" in shown

    picture = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/files",
        files={"file": ("shot.png", PNG, "image/png")},
    )
    assert picture.status_code == 200, picture.text
    shot = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Look at the picture.", "attachment_id": picture.json()["id"]},
    )
    assert shot.status_code == 200, shot.text
    image = script.seen[1][-1]["content"]
    assert isinstance(image, list)
    assert any(
        part.get("type") == "image_url" and str(part.get("image_url", {}).get("url", "")).startswith("data:image/png;base64,")
        for part in image
    )
    reply = shot.json()["chat"]["messages"][-1]
    assert reply["content"] == "Here is the copy."
    assert reply["attachment"]["name"] == "back.txt"
    handed = client.get(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/files/{reply['attachment']['id']}"
    )
    assert handed.content == b"handed-back-mark"
    raw = chat_file(tmp_path, bot["id"], chat["id"]).read_text(encoding="utf-8")
    assert "```file" not in raw
    assert "visible-file-mark" not in raw
    on_disk = tmp_path / "bots" / bot["id"] / "chats" / chat["id"] / "files" / file_id
    assert on_disk.read_bytes() == b"visible-file-mark"
    assert "attach-file" in client.get("/static/index.html").text
    assert "hand a file back" in changelog()


def test_tell_me_when_fires_once_for_a_message_and_a_failed_job(tmp_path, monkeypatch):
    notices = []
    mode = {"fail": False}

    async def complete(*, base_url, api_key, model, messages, timeout=120, **_extra):
        if mode["fail"]:
            raise ProviderError("the job missed its slot")
        return "ack"

    def deliver(title, body):
        notices.append((title, body))

    monkeypatch.setattr("easyagent.notify.deliver", deliver)
    client, bot, chat = open_client(tmp_path, monkeypatch, complete)
    message = client.post("/api/watches", json={"kind": "message"})
    failed = client.post("/api/watches", json={"kind": "job_failed"})
    assert message.status_code == 200, message.text
    assert failed.status_code == 200, failed.text
    assert message.json()["armed"] is True
    assert failed.json()["armed"] is True

    for text in ("quiet-line", "second-line"):
        sent = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": text},
        )
        assert sent.status_code == 200, sent.text
    assert notices == [("EasyAgent", "A new message is waiting.")]
    watches = client.get("/api/watches").json()
    assert next(item for item in watches if item["kind"] == "message")["armed"] is False
    assert next(item for item in watches if item["kind"] == "job_failed")["armed"] is True

    schedule = client.post(
        f"/api/bots/{bot['id']}/schedules",
        json={"prompt": "check the lane", "kind": "interval", "every_minutes": 1},
    )
    assert schedule.status_code == 200, schedule.text
    store = Store(tmp_path)
    rows = store.list_schedules(bot["id"])
    rows[0]["created_at"] = "2020-01-01T00:00:00+00:00"
    store.save_schedules(bot["id"], rows)
    mode["fail"] = True
    from datetime import datetime, timezone

    first = asyncio.run(run_due_schedules(store, datetime(2020, 1, 1, 0, 2, tzinfo=timezone.utc)))
    second = asyncio.run(run_due_schedules(store, datetime(2020, 1, 1, 0, 4, tzinfo=timezone.utc)))
    assert first[0]["status"] == "error"
    assert second[0]["status"] == "error"
    assert notices == [
        ("EasyAgent", "A new message is waiting."),
        ("EasyAgent", "A job failed."),
    ]
    assert "quiet-line" not in notices[0][1]
    assert "the job missed its slot" not in notices[1][1]
    spent = client.get("/api/watches").json()
    assert all(item["armed"] is False for item in spent)
    page = client.get("/static/index.html").text
    assert "Tell me when" in page
    assert "job_failed" in page
    assert "fires once" in changelog()


def test_night_pass_drops_a_proposal_without_a_counterexample_and_does_not_install(tmp_path, monkeypatch):
    phase = {"propose": 0, "break": 0}

    def propose(messages):
        phase["propose"] += 1
        transcript = messages[-1]["content"]
        quote = ""
        for line in transcript.splitlines():
            if "lane-marker" in line:
                quote = line.split(" | ", 1)[0].strip()
                break
        if phase["propose"] == 1:
            return f"kind: memory\nreplaces: older-line\nquote: {quote}\ntext: overwrite the older line"
        return f"kind: skill\nname: margin-note\nquote: {quote}\ntext: keep a wide margin on notes"

    def breaker(_messages):
        phase["break"] += 1
        if phase["break"] == 1:
            return "NONE"
        return "variant: the person asks for a long design note here"

    async def complete(*, base_url, api_key, model, messages, timeout=120, **_extra):
        system = messages[0]["content"]
        if system.startswith("Propose one skill"):
            return propose(messages)
        if system.startswith("You did not write"):
            return breaker(messages)
        return "ack"

    client, bot, chat = open_client(tmp_path, monkeypatch, complete)
    skill = client.post(
        "/api/skills",
        json={"name": "desk-notes", "description": "notes", "body": "Keep the desk note."},
    )
    assert skill.status_code == 200, skill.text
    remembered = client.post(f"/api/bots/{bot['id']}/memory", json={"text": "likes paper notes"})
    assert remembered.status_code == 200, remembered.text
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "lane-marker for the night pass"},
    )
    assert sent.status_code == 200, sent.text
    skills = skill_bytes(tmp_path)
    memory = memory_bytes(tmp_path, bot["id"])
    transcript = chat_file(tmp_path, bot["id"], chat["id"]).read_bytes()

    replaced = client.post("/api/night")
    assert replaced.status_code == 200, replaced.text
    assert replaced.json() == {"kept": [], "dropped": 1}
    assert phase["break"] == 0

    shrugged = client.post("/api/night")
    assert shrugged.status_code == 200, shrugged.text
    assert shrugged.json() == {"kept": [], "dropped": 1}
    assert phase["break"] == 1
    assert client.get("/api/proposals").json() == []
    assert not (tmp_path / "skills" / "margin-note.md").exists()

    kept = client.post("/api/night")
    assert kept.status_code == 200, kept.text
    body = kept.json()
    assert body["dropped"] == 0
    assert len(body["kept"]) == 1
    proposal = body["kept"][0]
    assert proposal["kind"] == "skill"
    assert proposal["name"] == "margin-note"
    assert proposal["installed"] is False
    assert proposal["counterexample"] == "the person asks for a long design note here"
    listed = client.get("/api/proposals").json()
    assert listed[0]["installed"] is False
    on_disk = json.loads((tmp_path / "proposals.json").read_text(encoding="utf-8"))
    assert on_disk[0]["installed"] is False
    remembered_text = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "bots" / bot["id"] / "memory").rglob("*.txt"))
    assert "overwrite the older line" not in remembered_text
    assert "likes paper notes" not in (tmp_path / "bots" / bot["id"] / "memory" / "index.txt").read_text(encoding="utf-8")
    assert skill_bytes(tmp_path) == skills
    assert memory_bytes(tmp_path, bot["id"]) == memory
    assert chat_file(tmp_path, bot["id"], chat["id"]).read_bytes() == transcript
    assert not (tmp_path / "skills" / "margin-note.md").exists()
    page = client.get("/static/index.html").text
    assert "Run night pass" in page
    assert "does not install" in page
    assert "Not installed" in client.get("/static/app.js").text
    assert "concrete counterexample is dropped" in changelog()


def test_a_playbook_edit_is_dropped_without_a_counterexample(tmp_path, monkeypatch):
    from easyagent.loop import PLAYBOOK_DIR

    before = {path.name: path.read_bytes() for path in sorted(PLAYBOOK_DIR.glob("*.md"))}
    phase = {"break": 0}

    async def complete(*, base_url, api_key, model, messages, timeout=120, **_extra):
        system = messages[0]["content"]
        if system.startswith("Propose one skill"):
            transcript = messages[-1]["content"]
            quote = ""
            for line in transcript.splitlines():
                if "playbook-marker" in line:
                    quote = line.split(" | ", 1)[0].strip()
                    break
            return (
                "kind: playbook\nname: build\n"
                f"quote: {quote}\ntext: skip the re-read on a build"
            )
        if system.startswith("You did not write"):
            phase["break"] += 1
            if phase["break"] == 1:
                return "NONE"
            return "variant: the page is written and never opened"
        return "ack"

    client, bot, chat = open_client(tmp_path, monkeypatch, complete)
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "playbook-marker for a build"},
    )
    assert sent.status_code == 200, sent.text
    dropped = client.post("/api/night")
    assert dropped.status_code == 200, dropped.text
    assert dropped.json() == {"kept": [], "dropped": 1}
    assert client.get("/api/proposals").json() == []
    kept = client.post("/api/night")
    assert kept.status_code == 200, kept.text
    body = kept.json()
    assert body["dropped"] == 0
    assert body["kept"][0]["kind"] == "playbook"
    assert body["kept"][0]["installed"] is False
    assert "never opened" in body["kept"][0]["counterexample"]
    assert {path.name: path.read_bytes() for path in sorted(PLAYBOOK_DIR.glob("*.md"))} == before
    assert "skip the re-read" not in (PLAYBOOK_DIR / "build.md").read_text(encoding="utf-8")
