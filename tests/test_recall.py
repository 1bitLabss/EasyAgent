"""An older line comes back only when the new message shares its words."""

import json

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.context import prepare_context
from easyagent.store import Store


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.calls.append(messages)
        return "ack"


MARKER = "The kiln manual lives behind the workshop door."
OVERLAP = "Where is the kiln manual"
NONE = "purple zebra quota"


def _outside_the_tail(messages: list[dict]) -> list[dict]:
    """Grow filler until the marker is in neither the summary nor the recent tail."""
    fillers: list[str] = []
    while len(fillers) <= 80:
        rows = [{"role": "user", "content": MARKER}]
        for index, filler in enumerate(fillers):
            role = "assistant" if index % 2 else "user"
            rows.append({"role": role, "content": filler})
        prepared = prepare_context([*rows, {"role": "user", "content": OVERLAP}], "", 0)
        in_tail = any(MARKER in item["content"] for item in prepared.tail)
        if prepared.summarized_through > 0 and MARKER not in prepared.summary and not in_tail:
            return rows
        fillers.append(f"padding item {len(fillers):04d} " + ("x" * 4000))
    raise AssertionError("the older line never left the summary and the tail")


def test_shared_words_bring_an_older_line_back_and_no_overlap_adds_nothing(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    seeded = _outside_the_tail([])
    store = Store(tmp_path)
    saved = store.get_chat(bot["id"], chat["id"])
    saved["messages"] = [
        {"role": item["role"], "content": item["content"]}
        for item in seeded
    ]
    saved["summary"] = ""
    saved["summarized_through"] = 0
    store.save_chat(saved)
    path = tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json"
    before = json.loads(path.read_text(encoding="utf-8"))
    assert before["messages"][0]["content"] == MARKER

    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": OVERLAP},
    )
    assert sent.status_code == 200, sent.text
    prompt = recorder.calls[-1]
    system = prompt[0]["content"]
    tail = [item for item in prompt if item["role"] != "system"]
    assert "Earlier conversation" in system
    assert MARKER in system
    assert "Earlier lines that share words" in system
    assert any(OVERLAP in (item.get("content") or "") for item in tail)
    assert all(MARKER not in (item.get("content") or "") for item in tail)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["messages"][0]["content"] == MARKER
    assert len(on_disk["messages"]) == len(seeded) + 2

    quiet = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": NONE},
    )
    assert quiet.status_code == 200, quiet.text
    quiet_prompt = json.dumps(recorder.calls[-1])
    assert MARKER not in quiet_prompt
    assert "Earlier lines that share words" not in quiet_prompt
    assert "Earlier conversation" in recorder.calls[-1][0]["content"]
    assert any(NONE in (item.get("content") or "") for item in recorder.calls[-1] if item["role"] != "system")
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["messages"][0]["content"] == MARKER
    assert [item["content"] for item in final["messages"][: len(seeded)]] == [
        item["content"] for item in seeded
    ]


def test_changelog_names_the_scan():
    from pathlib import Path

    text = Path(__file__).resolve().parents[1].joinpath("CHANGELOG.md").read_text(encoding="utf-8")
    assert "share its words" in text
    assert "no overlap adds nothing" in text
