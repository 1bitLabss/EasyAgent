"""Several questions in one stop (batched clarify). A questions array of 1-5
objects parses, validates, and stamps message["questions"]; the legacy single
question keeps its exact old payload."""

import json

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.tools import ToolError, ToolRequest, _ask, _function_arguments, parse_tool


class Recorder:
    def __init__(self, reply):
        self.reply = reply
        self.seen = []

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.seen.append(messages)
        reply = self.reply
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else "ack"
        return reply(messages) if callable(reply) else reply


def _world(tmp_path, monkeypatch, reply="ack"):
    recorder = Recorder(reply)
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "keep-this-line"},
    )
    assert sent.status_code == 200, sent.text
    return client, bot, chat, recorder


BATCH3 = [
    {"question": "Which bakery is the page for?", "choices": ["Oven Street", "Flour Power"]},
    {"question": "Which day should it open?", "choices": ["Friday", "Saturday"]},
    {"question": "Which hero picture?", "choices": ["Bread", "Storefront"]},
]


def _fence(payload) -> str:
    return "```question\n" + json.dumps(payload) + "\n```"


def test_three_questions_in_the_fence_parse_to_a_three_item_batch():
    request = parse_tool(_fence({"questions": BATCH3}))
    assert request.kind == "question"
    assert len(request.questions) == 3
    assert request.questions[2]["question"] == "Which hero picture?"
    assert request.questions[2]["choices"] == ("Bread", "Storefront")
    # body/choices carry the first question so old readers keep working.
    assert request.body == "Which bakery is the page for?"
    assert request.choices == ("Oven Street", "Flour Power")


def test_three_questions_in_the_invoke_form_parse_to_a_batch():
    body = "<parameter name=\"questions\">" + json.dumps(BATCH3) + "</parameter>"
    request = parse_tool(f"<invoke name=\"question\">{body}</invoke>")
    assert request.kind == "question"
    assert len(request.questions) == 3


def test_native_args_questions_array_becomes_a_batch():
    from easyagent.llm import _fence_for_call

    fence = _fence_for_call({"name": "question", "arguments": {"questions": BATCH3}})
    assert fence.startswith("```question")
    request = parse_tool(fence)
    assert len(request.questions) == 3
    assert request.body == BATCH3[0]["question"]


def test_one_entry_array_is_the_plain_single_question():
    request = parse_tool(_fence({"questions": [BATCH3[0]]}))
    assert request.kind == "question"
    assert request.body == BATCH3[0]["question"]
    assert request.choices == ("Oven Street", "Flour Power")
    assert request.questions == ()


def test_legacy_single_question_payload_is_unchanged():
    request = parse_tool("```question\nWhich folder?\nsrc\ntests\n```")
    assert request.kind == "question"
    assert request.body == "Which folder?"
    assert request.choices == ("src", "tests")
    assert request.questions == ()
    assert json.loads(_function_arguments(request)) == {"question": "Which folder?", "choices": ["src", "tests"]}


def test_more_than_five_questions_is_refused_with_a_hint():
    six = [{"question": f"q{i}", "choices": ["a", "b"]} for i in range(6)]
    with pytest.raises(ToolError) as caught:
        parse_tool(_fence({"questions": six}))
    assert "5" in str(caught.value)


def test_a_question_with_one_choice_is_refused():
    with pytest.raises(ToolError):
        parse_tool(_fence({"questions": [{"question": "q", "choices": ["only"]}, BATCH3[1]]}))


class _NoComputers:
    def list_computers(self):
        return []


def test_ask_caps_and_dedupes_each_question(tmp_path):
    from easyagent.store import Store

    store = Store(tmp_path)
    request = ToolRequest(
        kind="question",
        body="Pick",
        choices=("a", "b"),
        questions=(
            {"question": "Pick", "choices": ("a", "b", "a", "c", "d", "e", "f", "g")},
            {"question": "Second", "choices": ("x", "y")},
        ),
    )
    settled = _ask(store, request)
    first = settled.questions[0]
    assert first["choices"] == ("a", "b", "c", "d", "e", "f")  # deduped, capped at 6
    assert settled.text == "Pick"
    assert settled.choices == ("a", "b", "c", "d", "e", "f")


def test_ask_refuses_a_batched_question_with_one_choice(tmp_path):
    from easyagent.store import Store

    store = Store(tmp_path)
    request = ToolRequest(
        kind="question",
        body="Pick",
        choices=("a", "b"),
        questions=({"question": "Pick", "choices": ("a", "b")}, {"question": "Alone", "choices": ("x",)}),
    )
    with pytest.raises(ToolError):
        _ask(store, request)


def test_ask_scrubs_secrets_from_batched_text(tmp_path, monkeypatch):
    from easyagent import tools
    from easyagent.store import Store

    monkeypatch.setattr(tools, "secret_strings", lambda store: ["hunter2"])
    store = Store(tmp_path)
    request = ToolRequest(
        kind="question",
        body="Pick",
        choices=("a", "b"),
        questions=(
            {"question": "Use hunter2 for the login?", "choices": ["yes", "no"]},
            {"question": "Where is hunter2 stored?", "choices": ["vault", "file"]},
        ),
    )
    settled = _ask(store, request)
    blob = json.dumps([[item["question"], list(item["choices"])] for item in settled.questions])
    assert "hunter2" not in blob
    assert "hunter2" not in settled.text


def test_function_arguments_carries_the_batch():
    request = parse_tool(_fence({"questions": BATCH3}))
    payload = json.loads(_function_arguments(request))
    assert payload["question"] == BATCH3[0]["question"]
    assert payload["questions"] == [
        {"question": item["question"], "choices": list(item["choices"])} for item in BATCH3
    ]


def test_a_batched_ask_stamps_questions_on_the_reply(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = [_fence({"questions": BATCH3})]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "set up the bakery page but you need things from me"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    assert message["questions"] == [
        {"question": item["question"], "choices": list(item["choices"])} for item in BATCH3
    ]
    # The first question still rides the legacy fields for old clients.
    assert message["choices"] == ["Oven Street", "Flour Power"]
    assert BATCH3[0]["question"] in message["content"]


def test_a_single_question_reply_stamps_only_choices(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    recorder.reply = ["```question\nWhich folder?\nsrc\ntests\n```"]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "write the notes into a file"},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["chat"]["messages"][-1]
    assert message["choices"] == ["src", "tests"]
    assert "questions" not in message


def test_a_streamed_batched_ask_stamps_questions_on_the_saved_reply(tmp_path, monkeypatch):
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    fence = _fence({"questions": BATCH3})

    async def fake_stream(**_kwargs):
        yield fence[:20]
        yield fence[20:]

    monkeypatch.setattr("easyagent.llm.stream_complete", fake_stream)
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "set up the bakery page but you need things from me"},
        headers={"Accept": "text/event-stream"},
    )
    assert sent.status_code == 200, sent.text
    assert "choices" in sent.text
    stored = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    message = stored["messages"][-1]
    assert message["questions"] == [
        {"question": item["question"], "choices": list(item["choices"])} for item in BATCH3
    ]
    assert message["choices"] == ["Oven Street", "Flour Power"]
