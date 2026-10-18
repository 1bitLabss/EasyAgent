"""Messages sent while a job is running stay in the transcript."""

from __future__ import annotations

from easyagent.app import _write_live
from easyagent.plan import is_stuck, steer_into, waiting_activity
from easyagent.store import now_iso
from easyagent.tools import ToolRequest, status_label
from tests.test_plan_0320 import _checklist, _client, _running


def test_two_busy_messages_stay_after_a_refresh(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _checklist(store, bot["id"], chat["id"])
    _running(store, bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    for text in ("what are you doing?", "and the second?"):
        response = client.post(url, json={"content": text})
        assert response.status_code == 200
        assert response.json()["reply"].startswith("Still on this job.")
    first = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    again = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    for public in (first, again):
        users = [item for item in public["messages"] if item.get("role") == "user"]
        notes = [item for item in public["messages"] if item.get("kind") == "status"]
        assert [item["content"] for item in users] == ["what are you doing?", "and the second?"]
        assert [item["chip"] for item in users] == ["queued", "queued"]
        assert len(notes) == 2
        assert all(item["content"].startswith("Still on this job.") for item in notes)
        assert notes[0]["about"] == users[0]["id"]
        assert notes[1]["about"] == users[1]["id"]
    client.close()


def test_stale_live_save_keeps_messages_sent_while_busy(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"].append({
        "id": "live-1",
        "role": "assistant",
        "content": "",
        "live": True,
        "created_at": now_iso(),
    })
    store.save_chat(doc)
    stale = store.get_chat(bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    assert client.post(url, json={"content": "one"}).status_code == 200
    assert client.post(url, json={"content": "two"}).status_code == 200
    stale["messages"][-1]["thinking"] = "tokens are arriving"
    stale["run"]["current_step"] = "Waiting on model, 1s"
    stale["run"]["last_activity_at"] = now_iso()
    store.save_chat(stale)
    saved = store.get_chat(bot["id"], chat["id"])
    contents = [item.get("content") for item in saved["messages"]]
    assert "one" in contents
    assert "two" in contents
    assert any(item.get("thinking") == "tokens are arriving" for item in saved["messages"])
    assert saved["run"]["id"] == "run-keep"
    client.close()


def test_steer_marks_seen_and_a_finished_reply_marks_answered(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _checklist(store, bot["id"], chat["id"])
    _running(store, bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    client.post(url, json={"content": "what are you doing?"})
    working = []
    assert steer_into(store, bot["id"], chat["id"], working) == 1
    stored = store.get_chat(bot["id"], chat["id"])
    user = next(item for item in stored["messages"] if item.get("content") == "what are you doing?")
    assert user["chip"] == "seen"
    assert user["seen_step"] == 2
    pickups = [item for item in stored["messages"] if item.get("kind") == "pickup"]
    assert len(pickups) == 1
    assert pickups[0]["content"] == "Picked up your message"
    assert pickups[0]["about"] == user["id"]
    assert [item for item in stored["messages"] if item.get("role") == "user" and item.get("content") == "what are you doing?"]
    from easyagent.plan import mark_answered

    stored["messages"].append({"id": "reply-1", "role": "assistant", "content": "Still working on the summary."})
    assert mark_answered(stored) is True
    assert user["chip"] == "answered"
    client.close()


def test_send_now_queues_a_refused_message(tmp_path):
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    from easyagent.plan import QUEUE_CAP

    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    for number in range(QUEUE_CAP):
        assert client.post(url, json={"content": f"note {number}"}).status_code == 200
    refused = client.post(url, json={"content": "past the cap"})
    assert "queue is full" in refused.json()["reply"]
    message_id = next(
        item["id"]
        for item in store.get_chat(bot["id"], chat["id"])["messages"]
        if item.get("content") == "past the cap"
    )
    sent = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/{message_id}/send-now")
    assert sent.status_code == 200
    again = next(item for item in sent.json()["messages"] if item.get("id") == message_id)
    assert again["chip"] == "queued"
    assert again.get("refuse_reason") in {None, ""}
    client.close()


def test_stop_marks_queued_lines_not_sent_and_send_now_sends_one(tmp_path, monkeypatch):
    async def complete(**_kwargs):
        return "Sent it."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"])
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    assert client.post(url, json={"content": "first note"}).status_code == 200
    assert client.post(url, json={"content": "second note"}).status_code == 200
    stopped = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/stop")
    assert stopped.status_code == 200, stopped.text
    users = [item for item in stopped.json()["messages"] if item.get("role") == "user"]
    assert [item["content"] for item in users] == ["first note", "second note"]
    assert [item["chip"] for item in users] == ["answered", "answered"]
    assert all(not item.get("refuse_reason") for item in users)
    from easyagent.plan import queued_items

    assert queued_items(store, bot["id"], chat["id"]) == []
    again = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert [item["chip"] for item in again["messages"] if item.get("role") == "user"] == ["answered", "answered"]
    assert any(item.get("content") == "Sent it." for item in again["messages"])

    sent = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages/{users[0]['id']}/send-now")
    assert sent.status_code == 200, sent.text
    body = sent.json()
    after = [item for item in body["messages"] if item.get("role") == "user"]
    assert after[0]["chip"] == "answered"
    assert after[0].get("refuse_reason") in {None, ""}
    assert after[1]["chip"] != "unsent"
    assert after[1].get("refuse_reason") in {None, ""}
    assert any(item.get("content") == "Sent it." for item in body["messages"])

    other = _client(tmp_path)
    other_app, other_client, other_bot, other_chat = other
    other_store = other_app.state.store
    _running(other_store, other_bot["id"], other_chat["id"])
    other_url = f"/api/bots/{other_bot['id']}/chats/{other_chat['id']}/messages"
    assert other_client.post(other_url, json={"content": "keep this"}).status_code == 200
    assert other_client.post(other_url, json={"content": "send this"}).status_code == 200
    target = next(
        item["id"]
        for item in other_store.get_chat(other_bot["id"], other_chat["id"])["messages"]
        if item.get("content") == "send this"
    )
    hurried = other_client.post(
        f"/api/bots/{other_bot['id']}/chats/{other_chat['id']}/messages/{target}/stop-and-send"
    )
    assert hurried.status_code == 200, hurried.text
    hurried_users = [item for item in hurried.json()["chat"]["messages"] if item.get("role") == "user"]
    by_text = {item["content"]: item for item in hurried_users}
    assert by_text["keep this"]["chip"] != "unsent"
    assert by_text["keep this"].get("refuse_reason") in {None, ""}
    assert by_text["send this"]["chip"] == "answered"
    assert any(item.get("content") == "Sent it." for item in hurried.json()["chat"]["messages"])
    client.close()
    other_client.close()


def test_waiting_activity_names_tokens_and_clears_the_stuck_flag(tmp_path):
    assert waiting_activity(190, 4800) == "Waiting on model, 3m 10s, 1.2k tokens so far"
    assert status_label(ToolRequest(kind="files", action="write", path="index.html")) == "Writing index.html"
    assert status_label(ToolRequest(kind="shell", command="npm install")).startswith("Running")
    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    _running(store, bot["id"], chat["id"], step="Thinking")
    quiet = store.get_chat(bot["id"], chat["id"])
    quiet["run"]["last_activity_at"] = "2020-01-01T00:00:00+00:00"
    store.save_chat(quiet)
    assert is_stuck(store.get_chat(bot["id"], chat["id"])["run"]) is True
    saved = _write_live(
        store,
        bot["id"],
        chat["id"],
        step=waiting_activity(4, 80),
        thinking="looking at the page",
        force=True,
    )
    assert saved is not None
    public = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert public["run"]["stuck"] is False
    assert public["run"]["current_step"] == "Waiting on model, 4s, 20 tokens so far"
    client.close()
