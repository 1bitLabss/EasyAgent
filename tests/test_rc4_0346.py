"""0.3.46 rc4: a tool fence stays out of the reply, and a slow tool keeps its status."""

import asyncio
import json

import pytest

from easyagent.botskills import _ACTION_FENCES
from easyagent.plan import tool_status_line
from easyagent.subagent import automatic_ask_allowed, user_asked_to_consult
from easyagent.tools import (
    _drop_result_wrappers,
    _ensure_shown,
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


_BOTS = ("Helper", "Helper2", "Helper3", "Helper4", "Kiln")

_CONSULT_YES = (
    "Ask the Helper2 bot to say hello",
    "Ask Helper4: what is your favourite colour?",
    "ask Helper what the tide is",
    "what does Kiln think about the east pier",
    "what does the Kiln think about the harbor",
    "check with Helper",
    "check with the Helper4 bot",
    "get Helper's opinion",
    "get the Helper's opinion on the pier",
    "run this by Helper2",
    "have Helper say hello",
    "have the Helper2 bot look at the kiln",
    "please ask another bot to count the boats",
    "consult another agent about the kiln",
    "please use another helper",
    "delegate to Helper3",
    "consult the Helper bot",
    "Helper4: what is your favourite colour?",
    "Ask the Helper2 bot: say hello",
)

_CONSULT_NO = (
    "How many boats are at the east pier?",
    "Look up how many boats are at the east pier.",
    "who is Helper3's author",
    "The notes mention Helper3.",
    "ask me to explain the pier",
    "what does the author think about Helper3",
    "I have Helper3's notes on my desk",
)


@pytest.mark.parametrize("phrase", _CONSULT_YES)
def test_a_named_consult_is_allowed(phrase):
    assert user_asked_to_consult(phrase, _BOTS) is True
    assert automatic_ask_allowed(phrase, _BOTS) is True


@pytest.mark.parametrize("phrase", _CONSULT_NO)
def test_a_plain_or_incidental_mention_is_not_a_consult(phrase):
    assert user_asked_to_consult(phrase, _BOTS) is False
    assert automatic_ask_allowed(phrase, _BOTS) is False


def test_a_natural_ask_is_still_answered_in_the_parents_words(tmp_path, monkeypatch):
    """The child reply is context. One no-tools call writes the parent's answer."""
    from fastapi.testclient import TestClient

    from easyagent.app import create_app

    class Tape:
        def __init__(self):
            self.calls = []

        async def __call__(self, *, base_url, api_key, model, messages, timeout=120, tools=None, **_kwargs):
            self.calls.append({"messages": messages, "tools": tools})
            system = messages[0]["content"]
            blob = "\n".join(item.get("content") or "" for item in messages)
            if "You were given one task" in system:
                return "Blue, like the east pier."
            if "The other bot replied:" in blob:
                return "Helper4's favourite colour is blue."
            return (
                "Asking Helper4.\n\n"
                "```subagent\n"
                "bot: Helper4\n"
                "say your favourite colour\n"
                "```"
            )

    tape = Tape()
    monkeypatch.setattr("easyagent.llm.complete", tape)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints",
        json={"name": "local", "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    parent = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    client.post("/api/bots", json={"name": "Helper4", "endpoint_id": endpoint["id"]})
    chat = client.post(f"/api/bots/{parent['id']}/chats").json()
    sent = client.post(
        f"/api/bots/{parent['id']}/chats/{chat['id']}/messages",
        json={"content": "Ask Helper4: what is your favourite colour?"},
    )
    assert sent.status_code == 200, sent.text
    messages = sent.json()["chat"]["messages"]
    answer = messages[-1]
    assert answer.get("subagent") is not True
    assert answer["role"] == "assistant"
    assert answer["content"] == "Helper4's favourite colour is blue."
    followups = [call for call in tape.calls if call["tools"] is False]
    assert len(followups) == 1
    client.close()


def test_a_bare_done_after_a_shell_step_stays_done(tmp_path, monkeypatch):
    wrapped = '<<<UNTRUSTED source="tool">>>\nSHELL-MARK-91\n<<<END UNTRUSTED>>>'
    shown = _ensure_shown(
        "Done.",
        ["Ran a command."],
        [{"kind": "shell", "action": "", "command": "echo SHELL-MARK-91", "raw": wrapped}],
    )
    assert shown == "Done."
    assert "UNTRUSTED" not in shown
    kept = _ensure_shown(
        "The command finished.",
        ["Ran a command."],
        [{"kind": "shell", "action": "", "command": "echo SHELL-MARK-91", "raw": wrapped}],
    )
    assert "SHELL-MARK-91" in kept
    assert "UNTRUSTED" not in kept
    assert "<<<" not in kept
    echoed = _drop_result_wrappers("The pier is clear.\n\n" + wrapped)
    assert "UNTRUSTED" not in echoed
    assert "The pier is clear." in echoed

    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = ["```shell\necho SHELL-MARK-91\n```", "Done."]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "run it"},
    )
    assert sent.status_code == 200, sent.text
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    answer = stored["messages"][-1]["content"]
    assert answer.strip() == "Done."
    assert "UNTRUSTED" not in answer
    assert "SHELL-MARK-91" not in answer
    for message in stored["messages"]:
        if message.get("role") == "assistant":
            assert "UNTRUSTED" not in (message.get("content") or "")
    client.close()
