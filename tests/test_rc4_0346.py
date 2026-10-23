"""0.3.46 rc4: a tool fence stays out of the reply, and a slow tool keeps its status."""

import asyncio
import json

import pytest

from easyagent.botskills import _ACTION_FENCES
from easyagent.plan import tool_status_line
from easyagent.tools import (
    _keep_partial_reply,
    _next_visible_delta,
    _partial_stream,
    _visible_stream_text,
    strip_tool_markup,
    tool_fence_names,
)
from tests.test_plan_0320 import _client
from tests.test_tools import _world

CODE = "Look.\n```python\nprint(1)\n```\nDone.\n"


def _deltas(chunks: list[str]) -> str:
    sent = ""
    held = ""
    parts: list[str] = []
    for bit in chunks:
        held += bit
        extra, sent = _next_visible_delta(held, sent)
        if extra:
            parts.append(extra)
    return "".join(parts)


def _pieces(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def test_the_stream_uses_every_tool_fence_name():
    assert tool_fence_names() == _ACTION_FENCES
    assert "search_files" in tool_fence_names()


@pytest.mark.parametrize("name", _ACTION_FENCES)
def test_a_complete_tool_fence_is_not_visible(name):
    prose = "The kiln is fine.\n"
    fence = f"```{name}\nrefunded\naudit\n```"
    raw = prose + fence + "\n"
    visible = _visible_stream_text(raw)
    assert f"```{name}" not in visible
    assert "refunded" not in visible
    assert "audit" not in visible
    assert "The kiln is fine." in visible
    shown = _deltas([raw])
    assert f"```{name}" not in shown
    assert "refunded" not in shown
    assert "audit" not in shown
    saved = strip_tool_markup(raw, keep_shape=True)
    # ```skill and ```helper stay until the reply is saved. extract_skills and
    # the helper parser lift them out, and the stored reply does not keep the fence.
    if name not in {"skill", "helper"}:
        assert f"```{name}" not in saved
        assert "refunded" not in saved
    assert "The kiln is fine." in saved


@pytest.mark.parametrize("name", _ACTION_FENCES)
def test_a_tool_fence_split_across_chunks_is_not_visible(name):
    prose = "The kiln is fine.\n"
    fence = f"```{name}\nrefunded\naudit\n```\n"
    for size in (1, 3, 7):
        shown = _deltas(_pieces(prose + fence, size))
        assert f"```{name}" not in shown
        assert "refunded" not in shown
        assert "audit" not in shown
        assert "```" not in shown


@pytest.mark.parametrize("name", _ACTION_FENCES)
def test_a_tool_fence_cut_off_by_a_pause_is_not_saved(name):
    prose = "The pier is clear.\n"
    events = list(_partial_stream([prose, f"```{name}\nrefunded\naudit\n"]))
    blob = "\n".join(text for _kind, text in events)
    assert f"```{name}" not in blob
    assert "refunded" not in blob
    assert "audit" not in blob
    assert "The pier is clear." in blob
    saved = strip_tool_markup(prose + f"```{name}\nrefunded\naudit\n", keep_shape=True)
    assert f"```{name}" not in saved
    assert "The pier is clear." in saved


def test_a_python_fence_still_streams_byte_for_byte():
    assert _visible_stream_text(CODE) == CODE
    assert _deltas(_pieces(CODE, 1)) == CODE
    assert _deltas(_pieces(CODE, 4)) == CODE
    assert strip_tool_markup(CODE, keep_shape=True) == CODE


def test_a_paused_search_files_fence_is_not_the_saved_reply(tmp_path):
    app, _client_obj, bot, chat = _client(tmp_path)
    store = app.state.store
    doc = store.get_chat(bot["id"], chat["id"])
    doc["messages"].append(
        {
            "id": "live-1",
            "role": "assistant",
            "content": "```search_files\nrefunded\naudit\n",
            "live": True,
            "created_at": "2026-10-23T00:00:00Z",
        }
    )
    store.save_chat(doc)
    _keep_partial_reply(
        store,
        bot["id"],
        chat["id"],
        ["The pier is clear.\n", "```search_files\nrefunded\naudit\n"],
    )
    again = store.get_chat(bot["id"], chat["id"])
    live = next(item for item in reversed(again["messages"]) if item.get("role") == "assistant" and item.get("live"))
    assert live["content"] == "The pier is clear."
    assert "```" not in live["content"]
    raw = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert "```search_files" not in raw
    assert "refunded" not in raw


def test_tool_status_survives_a_writing_label():
    assert tool_status_line("Running python3 -c sleep")
    assert tool_status_line("Searching files")
    assert tool_status_line("Writing harbor.py")
    assert not tool_status_line("Writing, 4s")
    assert not tool_status_line("Waiting on model, 3s")
    assert not tool_status_line("Thinking")
    assert not tool_status_line("Paused, reading your message")


def test_a_slow_python_tool_keeps_its_status(tmp_path, monkeypatch):
    seen: dict[str, list[str]] = {"steps": []}

    class Pieces:
        def __init__(self):
            self.n = 0

        async def __call__(self, **_kwargs):
            self.n += 1
            if self.n == 1:
                text = "Before the search.\n```search_files\nrefunded\naudit\n```"
                for index in range(0, len(text), 4):
                    yield text[index : index + 4]
                return
            if self.n == 2:
                text = '```shell\npython3 -c "import time; time.sleep(3)"\n```'
                for index in range(0, len(text), 5):
                    yield text[index : index + 5]
                return
            yield "The east pier is settled."

    client, bot, chat, _recorder = _world(tmp_path, monkeypatch)
    chat_id = chat["id"]

    async def slow(store, request, bot_id=None):
        doc = store.get_chat(bot_id, chat_id)
        step = str((doc.get("run") or {}).get("current_step") or "")
        seen["steps"].append(step)
        live = ""
        for message in reversed(doc.get("messages") or []):
            if isinstance(message, dict) and message.get("role") == "assistant" and message.get("live"):
                live = str(message.get("content") or "")
                break
        assert "```search_files" not in live
        assert "refunded" not in live
        assert "```shell" not in live
        if request.kind == "shell":
            assert step.startswith("Running"), step
            await asyncio.sleep(0.4)
            later = str((store.get_chat(bot_id, chat_id).get("run") or {}).get("current_step") or "")
            seen["steps"].append(later)
            assert later.startswith("Running"), later
            assert not later.startswith("Writing"), later
        return "ok"

    monkeypatch.setattr("easyagent.llm.stream_complete", Pieces())
    monkeypatch.setattr("easyagent.tools.execute", slow)
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "Look through the audit, then run the slow check."},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    deltas = []
    statuses = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        event = json.loads(line.split(":", 1)[1].strip())
        if event.get("type") == "delta":
            deltas.append(str(event.get("text") or ""))
        elif event.get("type") == "status":
            statuses.append(str(event.get("text") or ""))
    blob = "\n".join(deltas)
    assert "```search_files" not in blob
    assert "refunded" not in blob
    assert "```shell" not in blob
    assert "python3 -c" not in blob
    assert any(item.startswith("Running") for item in statuses), statuses
    assert any(item == "Searching files" for item in statuses), statuses
    assert any(item.startswith("Running") for item in seen["steps"]), seen
    stored = client.get(url.replace("/messages", "")).json()
    answer = stored["messages"][-1]["content"]
    assert "The east pier is settled." in answer
    assert "```search_files" not in answer
    assert "refunded" not in answer
    raw = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text(encoding="utf-8")
    assert "```search_files" not in raw
    assert "```shell" not in raw
    client.close()
