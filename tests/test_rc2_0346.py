"""0.3.46 rc2: code keeps its indent, a plain question does not ask another bot."""

import json

from easyagent.check import NO_ANSWER
from easyagent.subagent import automatic_ask_allowed, user_asked_to_consult
from easyagent.tools import _drop_known_logs
from tests.test_tools import _world

MODEL = """The harbor counter lives in harbor.py.

```python
def boats():

    return 12
```

```yaml
ship:
  name: harbor
  count: 12
```"""

PYTHON_BODY = "def boats():\n\n    return 12\n"
YAML_BODY = "ship:\n  name: harbor\n  count: 12\n"


def test_a_log_line_drops_and_a_fence_keeps_every_byte():
    text = (
        "Searched the web.\n"
        "The counter lives in harbor.py.\n"
        "    kept indent\n"
        "Searched the web. The pier has twelve boats.\n"
        "```python\n"
        "def boats():\n"
        "\n"
        "    return 12\n"
        "```\n"
        "```yaml\n"
        "ship:\n"
        "  name: harbor\n"
        "  Searched the web.\n"
        "```"
    )
    got = _drop_known_logs(text, ["Searched the web.", "Wrote /tmp/harbor.py."])
    assert got == (
        "The counter lives in harbor.py.\n"
        "    kept indent\n"
        "Searched the web. The pier has twelve boats.\n"
        "```python\n"
        "def boats():\n"
        "\n"
        "    return 12\n"
        "```\n"
        "```yaml\n"
        "ship:\n"
        "  name: harbor\n"
        "  Searched the web.\n"
        "```"
    )
    assert _drop_known_logs(MODEL, ["Searched the web."]) == MODEL
    assert PYTHON_BODY in MODEL
    assert YAML_BODY in MODEL


def test_a_coding_reply_is_saved_byte_for_byte(tmp_path, monkeypatch):
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    target = folder / "harbor.py"
    # A .py body is judged as a command. echo is on the strict safe list, so the
    # write lands. The reply the person sees is the indented code, not this body.
    write = f"```files\nwrite\n{target}\necho hi\n```"
    calls = {"n": 0}

    def reply(_messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return write
        return MODEL

    recorder.reply = reply
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Write the harbor counter into harbor.py and show the Python and the YAML."},
    )
    assert sent.status_code == 200, sent.text
    assert target.read_text(encoding="utf-8") == "echo hi"
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    answer = stored["messages"][-1]["content"]
    assert answer == MODEL
    assert "    return 12" in answer
    assert "  name: harbor" in answer
    client.close()


def test_plain_turns_do_not_ask_another_bot_unless_the_person_asked():
    assert user_asked_to_consult("please ask Child to fold the towels") is True
    assert user_asked_to_consult("ask me to explain the pier") is False
    assert user_asked_to_consult("How many boats are at the east pier?") is False
    assert automatic_ask_allowed("How many boats are at the east pier?") is False
    assert automatic_ask_allowed("Look up how many boats are at the east pier.") is False
    assert automatic_ask_allowed("Search for the harbor tide and answer with one sentence.") is False
    assert automatic_ask_allowed("please ask Child to count the boats") is True
    assert automatic_ask_allowed("Write the harbor counter into harbor.py") is True


def test_a_plain_question_strips_the_ask_block(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = (
        "The east pier has twelve boats.\n\n"
        "```subagent\n"
        "bot: Child\n"
        "count the boats again\n"
        "```"
    )
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "How many boats are at the east pier?"},
    )
    assert sent.status_code == 200, sent.text
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    answer = stored["messages"][-1]["content"]
    blob = json.dumps(stored["messages"])
    assert answer == "The east pier has twelve boats."
    assert "Asked Child" not in blob
    assert "No single existing bot" not in blob
    assert "count the boats again" not in blob
    assert all("You were given one task" not in json.dumps(call) for call in recorder.seen)
    client.close()


def test_a_lookup_and_a_search_strip_the_ask_block(tmp_path, monkeypatch):
    async def search(_query):
        return (
            "1. Harbor tide\n"
            "https://example.test/tide\n"
            "The east pier tide is at noon.\n"
        )

    monkeypatch.setattr("easyagent.search.web_search", search)
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    fence = (
        "The east pier has twelve boats.\n\n"
        "```subagent\n"
        "bot: Child\n"
        "count the boats again\n"
        "```"
    )
    for content in (
        "Look up how many boats are at the east pier.",
        "Search for the harbor tide and answer with one sentence.",
    ):
        recorder.reply = fence
        sent = client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": content},
        )
        assert sent.status_code == 200, sent.text
        stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
        answer = stored["messages"][-1]["content"]
        assert answer == "The east pier has twelve boats.", content
        assert "Asked Child" not in answer
        assert all("You were given one task" not in json.dumps(call) for call in recorder.seen)
    client.close()


def test_a_fence_with_no_other_words_does_not_claim_it_asked(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = "```subagent\nbot: Child\ncount the boats\n```"
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "How many boats are at the east pier?"},
    )
    assert sent.status_code == 200, sent.text
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    blob = json.dumps(stored["messages"])
    assert "Asked Child" not in blob
    assert "No single existing bot" not in blob
    assert NO_ANSWER in blob
    client.close()


def test_an_explicit_ask_still_sends(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = "```subagent\nbot: Child\ncount the boats\n```"
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "please ask Child to count the boats"},
    )
    assert sent.status_code == 200, sent.text
    blob = json.dumps(sent.json()["chat"]["messages"])
    assert "No single existing bot is named Child" in blob
    client.close()
