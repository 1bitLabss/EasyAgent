"""The webhook HTTP route checks the token and does not write it to disk."""

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.bridges import add_bridge

TOKEN = "webhook-token-value"


def test_the_webhook_route_checks_the_token_and_stores_no_copy(tmp_path):
    app = create_app(tmp_path / "data")
    store = app.state.store
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    row = add_bridge(store, kind="webhook", name="Hook", bot_id=bot["id"], allow_ids=["ada"], token=TOKEN)
    client = TestClient(app)
    url = f"/api/bridges/{row['id']}/webhook"
    bad = client.post(url, json={"user_id": "ada", "text": "hello", "token": TOKEN}, headers={"X-Webhook-Token": "nope"})
    assert bad.status_code == 200
    assert bad.json() == {"ok": False}
    assert TOKEN not in bad.text
    assert store.existing_ongoing(bot["id"]) is None
    good = client.post(url, json={"user_id": "ada", "text": "hello there"}, headers={"X-Webhook-Token": TOKEN})
    assert good.status_code == 200
    assert good.json() == {"ok": True}
    assert TOKEN not in good.text
    chat = store.ongoing_chat(bot["id"])
    body = "\n".join(str(item.get("content") or "") for item in chat.get("messages") or [])
    assert "hello there" in body
    for path in store.root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".txt"}:
            continue
        assert TOKEN not in path.read_text(encoding="utf-8")
