"""0.3.53b: an @mention stops at the name, and a room can be deleted."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app


class Recorder:
    def __init__(self):
        self.calls = []
        self.reply = None

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120, **_extra):
        self.calls.append({"messages": messages})
        if self.reply is not None:
            return self.reply if isinstance(self.reply, str) else self.reply(messages)
        return "ack"


@pytest.fixture
def world(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    app = create_app(tmp_path)
    client = TestClient(app)
    return SimpleNamespace(client=client, path=tmp_path, rec=recorder)


def room_with_bots(world, *names):
    endpoint = world.client.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    room = world.client.post("/api/rooms", json={"name": "Desk"}).json()
    for name in names:
        bot = world.client.post(
            "/api/bots", json={"name": name, "endpoint_id": endpoint["id"]}
        ).json()
        added = world.client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": bot["id"]})
        assert added.status_code == 200, added.text
    return room


def speak_as(messages):
    system = messages[0]["content"]
    if "You are Anna," in system:
        return "anna-said"
    if "You are Ann," in system:
        return "ann-said"
    return "unexpected"


def test_mention_matches_the_whole_name_not_a_prefix(world):
    room = room_with_bots(world, "Ann", "Anna")
    world.rec.reply = speak_as

    def replies_after(content):
        before = len(world.client.get(f"/api/rooms/{room['id']}").json()["messages"])
        sent = world.client.post(f"/api/rooms/{room['id']}/messages", json={"content": content})
        assert sent.status_code == 200, sent.text
        messages = sent.json()["messages"]
        assert messages[before]["content"] == content
        return [m["speaker_name"] for m in messages[before:] if m["role"] == "assistant"]

    assert replies_after("@Ann hi") == ["Ann"]
    assert replies_after("@Anna hi") == ["Anna"]
    assert replies_after("no mention here") == ["Ann", "Anna"]


def test_room_can_be_deleted_once(world):
    room = room_with_bots(world, "Ann")
    room_id = room["id"]
    assert any(item["id"] == room_id for item in world.client.get("/api/rooms").json())

    deleted = world.client.delete(f"/api/rooms/{room_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": room_id}
    assert all(item["id"] != room_id for item in world.client.get("/api/rooms").json())
    assert not (world.path / "rooms" / f"{room_id}.json").exists()
    assert world.client.get(f"/api/rooms/{room_id}").status_code == 404

    again = world.client.delete(f"/api/rooms/{room_id}")
    assert again.status_code == 404

    bots = world.client.get("/api/bots").json()
    assert [bot["name"] for bot in bots] == ["Ann"]
