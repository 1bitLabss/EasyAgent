"""0.3.46 rc3: a fenced reply keeps its bytes, and a restart is not an empty bubble."""

from easyagent.tools import _spoken
from tests.test_plan_0320 import _client
from tests.test_tools import _world

PROSE = (
    "The harbor counter lives in harbor.py.\n"
    "\n"
    "```python\n"
    "def fizzbuzz(n):\n"
    "    if n % 15 == 0:\n"
    '        return "FizzBuzz"\n'
    "\n"
    "\n"
    "    return str(n)\n"
    "```\n"
    "\n"
    "```yaml\n"
    "ship:\n"
    "  name: harbor\n"
    "  Searched the web.\n"
    "```\n"
    "\n"
    "Done.\n"
)

REPLY = (
    "Searched the web.\n"
    + PROSE
    + "```finish\n"
    "proven\n"
    "```"
)


def test_spoken_keeps_an_indented_fence_and_a_short_done_line():
    got = _spoken(REPLY)
    assert got == PROSE
    assert "    if n % 15 == 0:" in got
    assert '        return "FizzBuzz"\n\n\n    return str(n)' in got
    assert "  Searched the web." in got
    assert "Searched the web.\n" not in got.split("```yaml", 1)[0]
    assert got.rstrip().endswith("Done.")
    assert "```finish" not in got
    assert "proven" not in got.lower()


def test_a_finish_coding_reply_is_saved_byte_for_byte(tmp_path, monkeypatch):
    from easyagent.store import Store
    from easyagent.workspace import bot_workspace

    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    folder = bot_workspace(Store(tmp_path), bot["id"], create=True)
    target = folder / "harbor.py"
    write = f"```files\nwrite\n{target}\necho hi\n```"
    calls = {"n": 0}

    def reply(_messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return write
        return REPLY

    recorder.reply = reply
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Write the harbor counter into harbor.py and show the Python and the YAML."},
    )
    assert sent.status_code == 200, sent.text
    assert target.read_text(encoding="utf-8") == "echo hi"
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    answer = stored["messages"][-1]["content"]
    assert answer == PROSE
    assert "    if n % 15 == 0:" in answer
    assert "  name: harbor" in answer
    assert answer.rstrip().endswith("Done.")
    client.close()


def test_a_restart_mid_turn_is_a_status_row_not_an_empty_reply(tmp_path):
    from easyagent.app import _pause_for_resume
    from easyagent.store import now_iso

    app, client, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["run"] = {
        "id": "run-mid",
        "status": "running",
        "started_at": now_iso(),
        "last_activity_at": now_iso(),
        "current_step": "Writing",
        "reason": "",
    }
    doc["messages"] = [
        {"id": "u1", "role": "user", "content": "Count the boats.", "created_at": now_iso()},
        {
            "id": "a-empty",
            "role": "assistant",
            "content": "",
            "thinking": "planning the pier",
            "live": True,
            "created_at": now_iso(),
        },
        {
            "id": "a-kept",
            "role": "assistant",
            "content": "The kiln is fine.",
            "thinking": "already written",
            "live": True,
            "created_at": now_iso(),
        },
    ]
    store.save_chat(doc)
    _pause_for_resume(store, bot["id"], chat["id"])
    paused = store.get_chat(bot["id"], chat["id"])
    empty = next(item for item in paused["messages"] if item.get("id") == "a-empty")
    kept = next(item for item in paused["messages"] if item.get("id") == "a-kept")
    assert empty["kind"] == "status"
    assert empty["content"] == "Interrupted, resuming"
    assert "thinking" not in empty
    assert "live" not in empty
    assert kept["content"] == "The kiln is fine."
    assert kept.get("thinking") == "already written"
    assert any(item.get("content") == "Picked up where it stopped." for item in paused["messages"])
    bubbles = [
        item
        for item in paused["messages"]
        if item.get("role") == "assistant"
        and not str(item.get("content") or "").strip()
        and item.get("kind") not in {"status", "pickup", "resume-note", "resume-offer"}
    ]
    assert bubbles == []
    client.close()
