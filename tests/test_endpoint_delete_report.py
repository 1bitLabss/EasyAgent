"""Deleting a saved connection must say which bots were using it.

The delete response and the connections list carry a used_by report so the
user is never left with silently dangling bots. The scan must never write
bot.json (the chat-touching upgrade in list_bots is the trap).
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app


@pytest.fixture
def world(tmp_path, monkeypatch):
    async def ok_reply(**_extra):
        return "ack"

    monkeypatch.setattr("easyagent.llm.complete", ok_reply)
    app = create_app(tmp_path)
    client = TestClient(app)
    return SimpleNamespace(client=client, path=tmp_path, app=app)


def add_endpoint(world, name, url="http://127.0.0.1:9/v1"):
    response = world.client.post("/api/endpoints", json={"name": name, "base_url": url})
    assert response.status_code == 200, response.text
    return response.json()


def add_bot(world, name, endpoint_id):
    response = world.client.post(
        "/api/bots", json={"name": name, "endpoint_id": endpoint_id, "model": None}
    )
    assert response.status_code == 200, response.text
    return response.json()


def bot_files(root):
    return {path.name: path.read_bytes() for path in sorted(root.glob("bots/*/bot.json"))}


def test_delete_reports_the_bots_using_the_endpoint(world):
    shared = add_endpoint(world, "Shared")
    zoe = add_bot(world, "Zoe", shared["id"])
    bob = add_bot(world, "Bob", shared["id"])
    before = bot_files(world.path)

    response = world.client.request("DELETE", f"/api/endpoints/{shared['id']}", json={"confirm_name": "Shared"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == shared["id"]
    # Exactly the two bots, each with id and name, sorted by name.
    assert body["used_by"] == [
        {"id": bob["id"], "name": "Bob"},
        {"id": zoe["id"], "name": "Zoe"},
    ]
    # The usage scan must not rewrite any bot file.
    assert bot_files(world.path) == before


def test_delete_unused_endpoint_reports_empty_list(world):
    lonely = add_endpoint(world, "Lonely")
    response = world.client.request("DELETE", f"/api/endpoints/{lonely['id']}", json={"confirm_name": "Lonely"})
    assert response.status_code == 200, response.text
    assert response.json()["used_by"] == []


def test_delete_wrong_confirm_name_still_refuses_and_reports_nothing(world):
    shared = add_endpoint(world, "Shared")
    add_bot(world, "Bob", shared["id"])
    response = world.client.request("DELETE", f"/api/endpoints/{shared['id']}", json={"confirm_name": "Wrong"})
    assert response.status_code == 400
    assert "used_by" not in response.json()
    # The connection is still there.
    assert any(item["id"] == shared["id"] for item in world.client.get("/api/endpoints").json())


def test_list_endpoints_rows_carry_used_by_names(world):
    shared = add_endpoint(world, "Shared")
    unused = add_endpoint(world, "Unused")
    add_bot(world, "Bob", shared["id"])
    add_bot(world, "Ada", shared["id"])

    rows = {item["id"]: item for item in world.client.get("/api/endpoints").json()}
    assert sorted(rows[shared["id"]]["used_by"]) == ["Ada", "Bob"]
    assert rows[unused["id"]]["used_by"] == []
    # Existing fields are untouched.
    assert rows[shared["id"]]["name"] == "Shared"
    assert rows[shared["id"]]["base_url"] == "http://127.0.0.1:9/v1"


def test_delete_does_not_list_bots_on_a_different_endpoint(world):
    shared = add_endpoint(world, "Shared")
    other = add_endpoint(world, "Other")
    add_bot(world, "Bob", shared["id"])
    add_bot(world, "Ada", other["id"])

    response = world.client.request("DELETE", f"/api/endpoints/{other['id']}", json={"confirm_name": "Other"})
    assert response.status_code == 200, response.text
    # Only Ada, who used Other, is named. Bob used Shared and stays out.
    names = [user["name"] for user in response.json()["used_by"]]
    assert names == ["Ada"]
