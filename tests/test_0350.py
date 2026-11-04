"""0.3.50: the bot can see. Attached text reaches the model; images ride as
image_url parts only when the connection says the model can see pictures."""

import base64
import uuid

from easyagent.app import _attachment_text, _model_message, create_app
from easyagent.context import model_content
from easyagent.store import Store

# 1x1 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_attachment_text_extracts_text_files():
    assert _attachment_text("notes.md", "text/markdown", b"# hello") == "# hello"
    assert _attachment_text("data.json", "application/json", b'{"a": 1}') == '{"a": 1}'
    assert _attachment_text("mystery", "", b"plain bytes here") == "plain bytes here"


def test_attachment_text_skips_binary():
    assert _attachment_text("pic.png", "image/png", PNG) is None
    assert _attachment_text("blob", "application/octet-stream", b"\x00\x01\x02\x03") is None


def test_model_content_carries_attached_text():
    message = {
        "role": "user",
        "content": "read this",
        "attachment": {"id": "f1", "name": "notes.md", "media_type": "text/markdown", "text": "# hello"},
    }
    seen = model_content(message)
    assert "read this" in seen
    assert "Attached file notes.md:" in seen
    assert "# hello" in seen


def test_model_content_notes_an_unreadable_attachment():
    message = {
        "role": "user",
        "content": "look",
        "attachment": {"id": "f1", "name": "app.zip", "media_type": "application/zip"},
    }
    seen = model_content(message)
    assert "app.zip" in seen
    assert "not shown" in seen


def test_vision_flag_rounds_through_the_api(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(create_app(tmp_path))
    created = client.post("/api/endpoints", json={"name": "Vision", "base_url": "http://x.test/v1", "vision": True}).json()
    assert created.get("vision") is True
    patched = client.patch(f"/api/endpoints/{created['id']}", json={"vision": False}).json()
    assert not patched.get("vision")


def _attachment_store(tmp_path):
    store = Store(tmp_path)
    bot = store.add_bot(name="Seer", endpoint_id=None, model=None)
    bot_id = bot["id"]
    chat_id = store.create_chat(bot_id)["id"]
    meta = store.save_chat_file(bot_id, chat_id, name="pic.png", media_type="image/png", data=PNG)
    stored = {"role": "user", "content": "what is in this picture?", "attachment": {"id": meta["id"], "name": "pic.png", "media_type": "image/png"}}
    clipped = {"role": "user", "content": "what is in this picture?"}
    return store, bot_id, chat_id, stored, clipped


def test_image_stays_text_when_the_connection_cannot_see(tmp_path):
    store, bot_id, chat_id, stored, clipped = _attachment_store(tmp_path)
    message = _model_message(store, bot_id, chat_id, stored, clipped, vision=False)
    assert isinstance(message["content"], str)
    assert "cannot see pictures" in message["content"]
    assert "pic.png" in message["content"]


def test_image_rides_as_parts_when_the_connection_can_see(tmp_path):
    store, bot_id, chat_id, stored, clipped = _attachment_store(tmp_path)
    message = _model_message(store, bot_id, chat_id, stored, clipped, vision=True)
    assert isinstance(message["content"], list)
    kinds = [part.get("type") for part in message["content"]]
    assert kinds == ["text", "image_url"]
    url = message["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_attached_text_is_not_duplicated(tmp_path):
    store = Store(tmp_path)
    bot = store.add_bot(name="Reader", endpoint_id=None, model=None)
    stored = {
        "role": "user",
        "content": "read this",
        "attachment": {"id": "f1", "name": "notes.md", "media_type": "text/markdown", "text": "# hello"},
    }
    clipped = {"role": "user", "content": model_content(stored)}
    message = _model_message(store, bot["id"], str(uuid.uuid4()), stored, clipped)
    assert message["content"].count("# hello") == 1
