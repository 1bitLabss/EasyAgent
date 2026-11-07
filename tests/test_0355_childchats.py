"""0.3.55: a fan-out child gets its own transcript chat.

The audited gap: delegate/helper children kept their whole conversation in
memory and threw it away. The child_status event carried child_chat_id=None,
so the "Open child chat" button had nothing to open.

Now: each child gets a real chat under the parent's bot (task row written
first, tool batches and the final result appended as they happen), the
child_status event carries that id, the sidebar list stays clean, and
deleting the parent chat takes its children with it.
"""

from tests.test_0354_children import FENCES, Tape, _stream_message, _world


def _child_ids(events: list[dict]) -> dict[str, str]:
    """name -> child_chat_id from the finished child_status events."""
    return {
        item["name"]: item["child_chat_id"]
        for item in events
        if item.get("type") == "child_status" and item["state"] != "running"
    }


def test_fan_out_child_chats_hold_task_and_result_rows(tmp_path, monkeypatch):
    """Each child's own chat opens with the task it was given and ends with
    what it found. The failed child's transcript shows its failure."""
    tape = Tape()
    client, bot, chat = _world(tmp_path, monkeypatch, tape)
    events = _stream_message(client, bot, chat, "look over the marina")
    ids = _child_ids(events)
    assert set(ids) == {"Scout", "Sailor", "Skipper"}
    assert all(ids.values()), "every finished child carries a real chat id"

    scout = client.get(f"/api/bots/{bot['id']}/chats/{ids['Scout']}")
    assert scout.status_code == 200, scout.text
    messages = scout.json()["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "count the boats"
    assert messages[-1]["role"] == "assistant"
    assert "count the boats-MARKER" in messages[-1]["content"]

    skipper = client.get(f"/api/bots/{bot['id']}/chats/{ids['Skipper']}")
    assert skipper.status_code == 200, skipper.text
    rows = skipper.json()["messages"]
    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "look at the fuel"
    assert rows[-1].get("error") is True

    # The child chats are filed under the parent's bot and tagged as children.
    # _public_chat does not carry the tag, so read the stored document.
    from easyagent.store import Store

    store = Store(tmp_path)
    scout_doc = store.get_chat(bot["id"], ids["Scout"])
    assert scout_doc["bot_id"] == bot["id"]
    assert scout_doc["child"] is True
    assert scout_doc["child_of"] == {"chat_id": chat["id"]}
    client.close()


def test_child_status_events_carry_the_same_real_chat_id(tmp_path, monkeypatch):
    """Running and finished events for one child ride one real chat id, so
    the frontend button works from the first event onward."""
    tape = Tape()
    client, bot, chat = _world(tmp_path, monkeypatch, tape)
    events = _stream_message(client, bot, chat, "look over the marina")
    children = [item for item in events if item.get("type") == "child_status"]
    by_child: dict[str, list[dict]] = {}
    for item in children:
        by_child.setdefault(item["child_id"], []).append(item)
    assert len(by_child) == 3
    for items in by_child.values():
        assert all(item["child_chat_id"] for item in items)
        assert len({item["child_chat_id"] for item in items}) == 1
    # The ids are real chats, not invented strings.
    for name, chat_id in _child_ids(events).items():
        fetched = client.get(f"/api/bots/{bot['id']}/chats/{chat_id}")
        assert fetched.status_code == 200, f"{name}: {fetched.text}"
    client.close()


def test_child_chats_stay_out_of_the_sidebar(tmp_path, monkeypatch):
    """The chat list is the person's chats. A fan-out child is not one."""
    tape = Tape()
    client, bot, chat = _world(tmp_path, monkeypatch, tape)
    events = _stream_message(client, bot, chat, "look over the marina")
    ids = set(_child_ids(events).values())
    listed = client.get(f"/api/bots/{bot['id']}/chats").json()
    client.close()
    assert chat["id"] in [item["id"] for item in listed]
    assert not ids & {item["id"] for item in listed}


def test_deleting_the_parent_chat_deletes_the_child_chats(tmp_path, monkeypatch):
    """No trash bin for child chats: they live and die with their parent."""
    tape = Tape()
    client, bot, chat = _world(tmp_path, monkeypatch, tape)
    events = _stream_message(client, bot, chat, "look over the marina")
    ids = _child_ids(events)

    deleted = client.delete(f"/api/bots/{bot['id']}/chats/{chat['id']}")
    assert deleted.status_code == 200, deleted.text
    for chat_id in ids.values():
        gone = client.get(f"/api/bots/{bot['id']}/chats/{chat_id}")
        assert gone.status_code == 404
    # A sibling chat of the same bot is untouched.
    other = client.post(f"/api/bots/{bot['id']}/chats").json()
    assert client.get(f"/api/bots/{bot['id']}/chats/{other['id']}").status_code == 200
    client.close()
