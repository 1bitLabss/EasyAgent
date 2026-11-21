"""Per-bot direction: a bot's own DIRECTION.md replaces the shared note for that bot."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120, **_extra):
        self.calls.append(
            {"base_url": base_url, "api_key": api_key, "model": model, "messages": messages}
        )
        return "ack"


@pytest.fixture
def world(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    app = create_app(tmp_path)
    client = TestClient(app)
    return SimpleNamespace(client=client, path=tmp_path, rec=recorder, app=app)


def add_endpoint(world, name, url="http://127.0.0.1:9/v1"):
    response = world.client.post("/api/endpoints", json={"name": name, "base_url": url})
    assert response.status_code == 200, response.text
    return response.json()


def add_bot(world, name, endpoint_id):
    response = world.client.post("/api/bots", json={"name": name, "endpoint_id": endpoint_id})
    assert response.status_code == 200, response.text
    return response.json()


def global_file(world):
    return world.path / "DIRECTION.md"


def bot_file(world, bot_id):
    return world.path / "bots" / bot_id / "DIRECTION.md"


def test_direction_without_bot_id_keeps_the_legacy_shape(world):
    saved = world.client.put("/api/direction", json={"text": "Shared rule only."})
    assert saved.status_code == 200
    assert saved.json() == {"text": "Shared rule only.\n"}
    assert global_file(world).read_text(encoding="utf-8") == "Shared rule only.\n"
    fetched = world.client.get("/api/direction")
    assert fetched.status_code == 200
    assert fetched.json() == {"text": "Shared rule only.\n"}


def test_put_with_bot_id_writes_only_that_bots_file(world):
    endpoint = add_endpoint(world, "local")
    ada = add_bot(world, "Ada", endpoint["id"])
    bea = add_bot(world, "Bea", endpoint["id"])
    world.client.put("/api/direction", json={"text": "Shared rule."})
    global_before = global_file(world).read_bytes()

    saved = world.client.put(f"/api/direction?bot_id={ada['id']}", json={"text": "Ada only rule."})
    assert saved.status_code == 200
    assert saved.json()["text"] == "Ada only rule.\n"
    assert bot_file(world, ada["id"]).read_text(encoding="utf-8") == "Ada only rule.\n"
    assert global_file(world).read_bytes() == global_before
    assert not bot_file(world, bea["id"]).exists()

    fetched = world.client.get(f"/api/direction?bot_id={ada['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["text"] == "Ada only rule.\n"
    assert fetched.json()["own"] is True
    assert world.client.get("/api/direction").json() == {"text": "Shared rule.\n"}


def test_read_direction_falls_back_to_global_when_the_bot_has_no_file(world):
    endpoint = add_endpoint(world, "local")
    ada = add_bot(world, "Ada", endpoint["id"])
    world.client.put("/api/direction", json={"text": "Shared rule."})

    from easyagent.store import Store

    store = Store(world.path)
    assert store.read_direction(ada["id"]) == "Shared rule.\n"
    assert store.read_direction() == "Shared rule.\n"
    fetched = world.client.get(f"/api/direction?bot_id={ada['id']}")
    assert fetched.json()["text"] == "Shared rule.\n"
    assert fetched.json()["own"] is False
    assert not bot_file(world, ada["id"]).exists()


def test_a_prompt_uses_the_bots_own_note_and_other_bots_keep_the_shared_one(world):
    endpoint = add_endpoint(world, "local")
    ada = add_bot(world, "Ada", endpoint["id"])
    bea = add_bot(world, "Bea", endpoint["id"])
    world.client.put("/api/direction", json={"text": "Shared rule: keep answers short."})
    world.client.put(f"/api/direction?bot_id={ada['id']}", json={"text": "Ada rule: answer in French."})

    for bot in (ada, bea):
        chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
        sent = world.client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": "hello"},
        )
        assert sent.status_code == 200, sent.text

    ada_system = world.rec.calls[0]["messages"][0]["content"]
    assert "Ada rule: answer in French." in ada_system
    assert "Shared rule: keep answers short." not in ada_system

    bea_system = world.rec.calls[1]["messages"][0]["content"]
    assert "Shared rule: keep answers short." in bea_system
    assert "Ada rule: answer in French." not in bea_system


def test_delete_bot_removes_its_own_note_and_leaves_the_shared_one_alone(world):
    endpoint = add_endpoint(world, "local")
    ada = add_bot(world, "Ada", endpoint["id"])
    world.client.put("/api/direction", json={"text": "Shared rule."})
    world.client.put(f"/api/direction?bot_id={ada['id']}", json={"text": "Ada only rule."})
    global_before = global_file(world).read_bytes()
    assert bot_file(world, ada["id"]).is_file()

    removed = world.client.request("DELETE", f"/api/bots/{ada['id']}", json={"confirm_name": "Ada"})
    assert removed.status_code == 200, removed.text
    assert not bot_file(world, ada["id"]).exists()
    assert not (world.path / "bots" / ada["id"]).exists()
    assert global_file(world).read_bytes() == global_before


def test_direction_for_an_unknown_bot_is_not_found(world):
    # A well-formed UUID that no bot owns: only the existence check can reject it.
    ghost = "00000000-0000-4000-8000-000000000000"
    missing = world.client.put(f"/api/direction?bot_id={ghost}", json={"text": "Nobody."})
    assert missing.status_code == 404
    assert not (world.path / "bots" / ghost).exists()
    fetched = world.client.get(f"/api/direction?bot_id={ghost}")
    assert fetched.status_code == 404
    # A malformed id is rejected the same way.
    assert world.client.put("/api/direction?bot_id=ghost", json={"text": "Nobody."}).status_code == 404
