import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.limits import DEFAULT_CONTEXT_TOKENS, MAX_CONTEXT_CHARS, MIN_CONTEXT_TOKENS
from easyagent.llm import ProviderError, complete
from easyagent.store import StoreError


class Recorder:
    def __init__(self):
        self.calls = []
        self.error = None
        self.reply = None

    async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
        self.calls.append(
            {"base_url": base_url, "api_key": api_key, "model": model, "messages": messages}
        )
        if self.error:
            raise self.error
        if self.reply is not None:
            return self.reply if isinstance(self.reply, str) else self.reply(messages)
        return "ack"


@pytest.fixture
def world(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    app = create_app(tmp_path)
    client = TestClient(app)
    return SimpleNamespace(client=client, path=tmp_path, rec=recorder, app=app)


def add_endpoint(world, name, url="http://127.0.0.1:9/v1", api_key=None, **extra):
    payload = {"name": name, "base_url": url, **extra}
    if api_key is not None:
        payload["api_key"] = api_key
    response = world.client.post("/api/endpoints", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def add_bot(world, name, endpoint_id, model=None, context_tokens=None):
    payload = {"name": name, "endpoint_id": endpoint_id, "model": model}
    if context_tokens is not None:
        payload["context_tokens"] = context_tokens
    response = world.client.post("/api/bots", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def chat_files(root: Path, bot_id: str) -> dict[str, bytes]:
    folder = root / "bots" / bot_id / "chats"
    return {path.name: path.read_bytes() for path in sorted(folder.glob("*.json"))}


def all_chat_files(root: Path) -> dict[str, bytes]:
    folder = root / "bots"
    if not folder.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(folder.glob("*/chats/*.json"))
    }


def assert_switch_left_files_alone(before: dict[str, bytes], after: dict[str, bytes]) -> None:
    """A bot switch must not remove, empty, or rewrite any chat file."""
    assert set(before) == set(after), "a bot switch removed or created a chat file"
    for name, blob in before.items():
        assert after[name] == blob, f"a bot switch changed {name}"
        stored = json.loads(blob)
        assert stored.get("messages"), f"{name} is empty"
        assert len(blob) > 20


def test_endpoints_do_not_require_a_model(world):
    created = add_endpoint(world, "local")
    assert created["model"] is None
    assert created["base_url"] == "http://127.0.0.1:9/v1"
    other = add_endpoint(world, "remote", url="https://example.test/v1", model="grid-model")
    assert other["model"] == "grid-model"
    listed = world.client.get("/api/endpoints").json()
    assert [item["name"] for item in listed] == ["local", "remote"]
    assert listed[0]["model"] is None
    assert listed[1]["model"] == "grid-model"
    raw = json.loads((world.path / "endpoints.json").read_text())
    assert raw[0]["model"] is None
    assert raw[1]["model"] == "grid-model"
    assert raw[1]["id"] == other["id"]


def test_request_uses_only_the_model_the_user_set(world):
    plain = add_endpoint(world, "plain", url="http://127.0.0.1:8101/v1")
    marked = add_endpoint(world, "marked", url="http://127.0.0.1:8102/v1", model="grid-model")
    ann = add_bot(world, "Ann", plain["id"])
    cy = add_bot(world, "Cy", marked["id"])
    dee = add_bot(world, "Dee", marked["id"], model="dee-model")
    for bot, text in ((ann, "ann"), (cy, "cy"), (dee, "dee")):
        chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
        sent = world.client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": text},
        )
        assert sent.status_code == 200, sent.text
    assert [call["model"] for call in world.rec.calls] == [None, "grid-model", "dee-model"]
    assert world.rec.calls[1]["base_url"] == "http://127.0.0.1:8102/v1"
    assert world.rec.calls[2]["base_url"] == "http://127.0.0.1:8102/v1"


def test_api_key_is_not_returned(world):
    add_endpoint(world, "keyed", api_key="super-secret-key")
    listed = world.client.get("/api/endpoints")
    assert "super-secret-key" not in listed.text
    assert listed.json()[0]["has_api_key"] is True
    assert "super-secret-key" in (world.path / "endpoints.json").read_text()


def test_two_bots_keep_separate_histories_across_switches_and_restart(world):
    local = add_endpoint(world, "local", url="http://127.0.0.1:8101/v1")
    remote = add_endpoint(world, "remote", url="http://127.0.0.1:8102/v1")
    ada = add_bot(world, "Ada", local["id"])
    bea = add_bot(world, "Bea", remote["id"])
    assert ada["model"] is None and bea["model"] is None

    ada_chat = world.client.post(f"/api/bots/{ada['id']}/chats").json()
    bea_chat = world.client.post(f"/api/bots/{bea['id']}/chats").json()
    first = world.client.post(
        f"/api/bots/{ada['id']}/chats/{ada_chat['id']}/messages",
        json={"content": "alpha secret from Ada"},
    )
    assert first.status_code == 200, first.text
    second = world.client.post(
        f"/api/bots/{bea['id']}/chats/{bea_chat['id']}/messages",
        json={"content": "beta secret from Bea"},
    )
    assert second.status_code == 200, second.text

    before_switch = all_chat_files(world.path)
    assert_switch_left_files_alone(before_switch, before_switch)
    for _ in range(3):
        assert world.client.get(f"/api/bots/{bea['id']}/chats").status_code == 200
        assert world.client.get(f"/api/bots/{ada['id']}/chats").status_code == 200
        assert world.client.get(f"/api/bots/{bea['id']}/chats/{bea_chat['id']}").status_code == 200
        assert world.client.get(f"/api/bots/{ada['id']}/chats/{ada_chat['id']}").status_code == 200
        assert_switch_left_files_alone(before_switch, all_chat_files(world.path))
    ada_bytes = chat_files(world.path, ada["id"])
    listed = world.client.get(f"/api/bots/{bea['id']}/chats")
    assert listed.status_code == 200
    assert chat_files(world.path, ada["id"]) == ada_bytes
    assert "beta secret" not in world.client.get(f"/api/bots/{ada['id']}/chats/{ada_chat['id']}").text
    assert "alpha secret" not in world.client.get(f"/api/bots/{bea['id']}/chats/{bea_chat['id']}").text
    assert world.client.get(f"/api/bots/{bea['id']}/chats/{ada_chat['id']}").status_code == 404

    assert world.rec.calls[0]["base_url"] == "http://127.0.0.1:8101/v1"
    assert world.rec.calls[0]["model"] is None
    assert world.rec.calls[1]["base_url"] == "http://127.0.0.1:8102/v1"
    assert "Never delete" in world.rec.calls[0]["messages"][0]["content"]

    restarted = TestClient(create_app(world.path))
    ada_again = restarted.get(f"/api/bots/{ada['id']}/chats/{ada_chat['id']}").json()
    bea_again = restarted.get(f"/api/bots/{bea['id']}/chats/{bea_chat['id']}").json()
    assert ada_again["messages"][0]["content"] == "alpha secret from Ada"
    assert bea_again["messages"][0]["content"] == "beta secret from Bea"
    assert len(restarted.get("/api/bots").json()) == 2
    assert len(restarted.get("/api/endpoints").json()) == 2


def test_long_chat_bounds_the_request_and_keeps_the_transcript(world):
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    first = "TOKEN-FIRST-" + ("x" * 2400)
    response = None
    for index in range(20):
        content = first if index == 0 else f"turn-{index}-" + ("y" * 12000)
        response = world.client.post(
            f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
            json={"content": content},
        )
        assert response.status_code == 200, response.text
    body = response.json()
    assert body["context"]["transcript_messages"] == 40
    assert body["context"]["context_chars"] <= MAX_CONTEXT_CHARS
    assert body["context"]["bounded"] is True
    assert body["context"]["transcript_chars"] > body["context"]["context_chars"]
    assert len(body["chat"]["messages"]) == 40
    stored = (world.path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text()
    assert first in stored
    payload = json.dumps(world.rec.calls[-1]["messages"])
    assert first not in payload
    sent = [item for item in world.rec.calls[-1]["messages"] if item["role"] != "system"]
    # The budget decides, not a fixed count of 8 turns.
    assert len(sent) > 8
    assert body["context"]["compacted"] is True
    assert body["context"]["max_context_tokens"] == DEFAULT_CONTEXT_TOKENS
    system = world.rec.calls[-1]["messages"][0]["content"]
    assert "Earlier conversation" in system
    assert "older ones did not fit" in system


def test_provider_failure_keeps_the_user_turn_and_retry_does_not_duplicate_it(world):
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    world.rec.error = ProviderError("connection refused")
    failed = world.client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "please keep this"},
    )
    assert failed.status_code == 200, failed.text
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert stored["messages"][0]["content"] == "please keep this"
    assert stored["messages"][1]["content"].startswith("Stopped:")
    assert "connection refused" in stored["messages"][1]["content"]
    assert "\u201clocal\u201d at http://127.0.0.1:9/v1" in stored["messages"][1]["content"]
    assert stored["messages"][1]["error"] is True
    listed = world.client.get(f"/api/bots/{bot['id']}/chats").json()
    assert [item["id"] for item in listed] == [chat["id"]]
    assert all(item["message_count"] > 0 for item in listed)

    world.rec.error = None
    retried = world.client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/retry")
    assert retried.status_code == 200, retried.text
    messages = retried.json()["chat"]["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant", "assistant"]
    assert messages[0]["content"] == "please keep this"
    assert messages[1]["content"].startswith("Stopped:")
    assert "connection refused" in messages[1]["content"]
    assert messages[2]["content"] == "ack"
    # The stored error line names an address. It is not replayed to the model.
    assert "127.0.0.1:9" not in json.dumps(world.rec.calls[-1]["messages"][1:])
    assert messages[0]["content"] == "please keep this"


def test_delete_bot_requires_the_exact_name_and_spares_everything_else(world):
    endpoint = add_endpoint(world, "local")
    ada = add_bot(world, "Ada", endpoint["id"])
    bea = add_bot(world, "Bea", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bea['id']}/chats").json()
    assert world.client.post(
        f"/api/bots/{bea['id']}/chats/{chat['id']}/messages",
        json={"content": "bea stays"},
    ).status_code == 200
    world.client.put("/api/direction", json={"text": "Never delete chats.\n"})
    skill = world.path / "skills" / "keep-me.md"
    skill.write_text("---\nname: keep-me\ndescription: stay\n---\n\nStay.\n", encoding="utf-8")

    bea_bytes = chat_files(world.path, bea["id"])
    endpoints_bytes = (world.path / "endpoints.json").read_bytes()
    direction_bytes = (world.path / "DIRECTION.md").read_bytes()
    skill_bytes = skill.read_bytes()

    missing = world.client.request("DELETE", f"/api/bots/{ada['id']}")
    assert missing.status_code == 422
    wrong = world.client.request("DELETE", f"/api/bots/{ada['id']}", json={"confirm_name": "Ada "})
    assert wrong.status_code == 400
    assert (world.path / "bots" / ada["id"]).is_dir()
    assert chat_files(world.path, bea["id"]) == bea_bytes

    removed = world.client.request("DELETE", f"/api/bots/{ada['id']}", json={"confirm_name": "Ada"})
    assert removed.status_code == 200, removed.text
    assert not (world.path / "bots" / ada["id"]).exists()
    assert chat_files(world.path, bea["id"]) == bea_bytes
    assert (world.path / "endpoints.json").read_bytes() == endpoints_bytes
    assert (world.path / "DIRECTION.md").read_bytes() == direction_bytes
    assert skill.read_bytes() == skill_bytes
    assert world.client.get(f"/api/bots/{bea['id']}/chats/{chat['id']}").json()["messages"][0]["content"] == "bea stays"


def test_delete_endpoint_does_not_touch_chats(world):
    endpoint = add_endpoint(world, "local")
    other = add_endpoint(world, "spare", url="http://127.0.0.1:10/v1")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    assert world.client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "still here"},
    ).status_code == 200
    before = chat_files(world.path, bot["id"])
    bot_meta = (world.path / "bots" / bot["id"] / "bot.json").read_bytes()

    blocked = world.client.request(
        "DELETE", f"/api/endpoints/{endpoint['id']}", json={"confirm_name": "nope"}
    )
    assert blocked.status_code == 400
    gone = world.client.request(
        "DELETE", f"/api/endpoints/{endpoint['id']}", json={"confirm_name": "local"}
    )
    assert gone.status_code == 200, gone.text
    assert chat_files(world.path, bot["id"]) == before
    assert (world.path / "bots" / bot["id"] / "bot.json").read_bytes() == bot_meta
    refused = world.client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "another"},
    )
    assert refused.status_code == 409
    assert chat_files(world.path, bot["id"]) == before
    patched = world.client.patch(f"/api/bots/{bot['id']}", json={"endpoint_id": other["id"], "model": "tiny"})
    assert patched.status_code == 200
    assert chat_files(world.path, bot["id"]) == before
    assert patched.json()["model"] == "tiny"


def test_get_chat_does_not_write(world):
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    world.client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "stored"},
    )
    before = (world.path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_bytes()
    loaded = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}")
    assert loaded.status_code == 200
    assert loaded.json()["messages"][0]["content"] == "stored"
    after = (world.path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_bytes()
    assert before == after


def test_direction_is_reread_each_turn_and_skills_are_reused(world):
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    world.rec.reply = (
        "I will keep answers short.\n\n"
        "```skill\n"
        "---\n"
        "name: short replies\n"
        "description: Lead with the answer\n"
        "---\n"
        "Answer in one or two sentences.\n"
        "```\n"
    )
    saved = world.client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "please remember that"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["skills_saved"] == ["short-replies"]
    assert "```skill" not in saved.json()["reply"]
    skill_path = world.path / "skills" / "short-replies.md"
    assert "Answer in one or two sentences." in skill_path.read_text()

    world.client.put("/api/direction", json={"text": "Custom rule: do not wipe chats.\n"})
    world.rec.reply = "ack"
    again = world.client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "second turn"},
    )
    assert again.status_code == 200
    system = world.rec.calls[-1]["messages"][0]["content"]
    assert "Custom rule: do not wipe chats." in system
    assert "short-replies" in system
    assert "Answer in one or two sentences." in system
    assert len(again.json()["chat"]["messages"]) == 4


def test_placeholder_skill_is_not_saved(world):
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    world.rec.reply = "```skill\n---\nname: kebab-case-name\ndescription: one line\n---\nbody\n```"
    response = world.client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello"},
    )
    assert response.status_code == 200
    assert response.json()["skills_saved"] == []
    assert [path.name for path in (world.path / "skills").glob("*.md")] == ["example-note.md"]


def test_no_route_wipes_chats(world):
    spec = world.client.get("/openapi.json").json()
    for path, methods in spec["paths"].items():
        assert "wipe" not in path
        assert "reset" not in path
        if "/chats/" in path:
            assert "delete" not in methods


def test_store_refuses_to_shorten_a_transcript(world):
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    created = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    world.client.post(
        f"/api/bots/{bot['id']}/chats/{created['id']}/messages",
        json={"content": "keep me"},
    )
    from easyagent.store import Store

    store = Store(world.path)
    chat = store.get_chat(bot["id"], created["id"])
    chat["messages"] = []
    with pytest.raises(StoreError):
        store.save_chat(chat)
    assert store.get_chat(bot["id"], created["id"])["messages"][0]["content"] == "keep me"


def test_bad_ids_are_not_paths(world):
    response = world.client.request("DELETE", "/api/bots/..%2F..%2Ftmp", json={"confirm_name": "x"})
    assert response.status_code == 404
    assert (world.path / "bots").is_dir()


def test_complete_omits_model_and_sends_key_only_when_set():
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "hi"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aclose(self):
            return None

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    import easyagent.llm as llm

    original = llm.httpx.AsyncClient
    llm.httpx.AsyncClient = FakeClient
    try:
        text = asyncio.run(
            complete(
                base_url="http://example.test/v1",
                api_key=None,
                model=None,
                messages=[{"role": "user", "content": "a"}],
            )
        )
    finally:
        llm.httpx.AsyncClient = original
    assert text == "hi"
    assert captured["url"] == "http://example.test/v1/chat/completions"
    assert "model" not in captured["json"]
    assert "Authorization" not in captured["headers"]
    names = [item["function"]["name"] for item in captured["json"]["tools"]]
    assert "write_file" in names
    assert "terminal" in names
    assert "web_search" in names


def test_complete_and_stream_send_tools_and_keep_every_call():
    import easyagent.llm as llm
    from easyagent.llm import calls_to_fence, calls_to_fences, stream_complete

    calls = [
        {"index": 0, "function": {"name": "list_dir", "arguments": "{\"path\": \"/tmp/ea-a\"}"}},
        {"index": 1, "function": {"name": "terminal", "arguments": "{\"command\": \"echo NOTE-77\"}"}},
    ]
    body = {
        "choices": [{
            "message": {"content": "I will list and run.", "tool_calls": calls},
            "delta": {},
        }]
    }
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""
        headers = {"content-type": "application/json"}

        def json(self):
            return body

        async def aread(self):
            return json.dumps(body).encode()

        def aiter_lines(self):
            async def empty():
                if False:
                    yield ""
            return empty()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aclose(self):
            return None

        async def post(self, url, json=None, headers=None):
            captured["complete"] = json
            return FakeResponse()

        def stream(self, method, url, json=None, headers=None):
            captured["stream"] = json

            class Context:
                async def __aenter__(self):
                    return FakeResponse()

                async def __aexit__(self, *args):
                    return False

            return Context()

    original = llm.httpx.AsyncClient
    llm.httpx.AsyncClient = FakeClient
    try:
        text = asyncio.run(
            complete(
                base_url="http://example.test/v1",
                api_key=None,
                model=None,
                messages=[{"role": "user", "content": "a"}],
            )
        )

        async def collect():
            parts = []
            async for piece in stream_complete(
                base_url="http://example.test/v1",
                api_key=None,
                model="m",
                messages=[{"role": "user", "content": "a"}],
            ):
                parts.append(piece)
            return "".join(parts)

        streamed = asyncio.run(collect())
    finally:
        llm.httpx.AsyncClient = original
    both = calls_to_fences([
        {"name": "list_dir", "arguments": {"path": "/tmp/ea-a"}},
        {"name": "terminal", "arguments": {"command": "echo NOTE-77"}},
    ])
    assert "```files" in both and "```shell" in both
    assert both.index("```files") < both.index("```shell")
    assert calls_to_fence([
        {"name": "list_dir", "arguments": {"path": "/tmp/ea-a"}},
        {"name": "terminal", "arguments": {"command": "echo NOTE-77"}},
    ]) == both
    assert "```files" in text and "```shell" in text
    assert text.index("```files") < text.index("```shell")
    assert "I will list and run." in text
    assert "```files" in streamed and "```shell" in streamed
    assert streamed.index("```files") < streamed.index("```shell")
    assert "I will list and run." in streamed
    for payload in (captured["complete"], captured["stream"]):
        sent = [item["function"]["name"] for item in payload["tools"]]
        assert "list_dir" in sent and "terminal" in sent and "question" in sent and "finish" in sent
    assert captured["stream"]["stream"] is True
    assert captured["stream"]["model"] == "m"
    assert "model" not in captured["complete"]


def test_a_static_file_stays_inside_its_folder():
    from easyagent.access import contained_file
    from easyagent.app import STATIC_DIR

    font = contained_file(STATIC_DIR, "fonts/InterVariable.woff2")
    assert font is not None and font.is_file()
    assert font.read_bytes()[:4] == b"wOF2"
    assert contained_file(STATIC_DIR, "../app.py") is None
    assert contained_file(STATIC_DIR, "fonts/../../app.py") is None


def test_ui_is_served(world):
    page = world.client.get("/")
    assert page.status_code == 200
    assert "EasyAgent" in page.text
    assert "Connections" in page.text
    assert "How much chat it sees" in page.text
    assert "Create room" in page.text
    chat_at = page.text.index('id="screen-chat"')
    composer_at = page.text.index('id="composer"')
    settings_at = page.text.index('id="screen-settings"')
    memory_at = page.text.index('id="memory-form"')
    connections_at = page.text.index('id="screen-connections"')
    rooms_at = page.text.index('id="screen-rooms"')
    computers_at = page.text.index('id="screen-computers"')
    assert chat_at < composer_at < settings_at < memory_at
    assert settings_at < connections_at < rooms_at < computers_at
    assert world.client.get("/static/app.js").status_code == 200
    css = world.client.get("/static/app.css")
    assert css.status_code == 200
    assert "InterVariable.woff2" in css.text
    assert "cdn.jsdelivr.net/fontsource/fonts/inter:vf@5.2.8/latin-wght-normal.woff2" in css.text
    assert "JetBrainsMono-Regular.woff2" in css.text
    assert "Georgia" not in css.text
    assert "--serif" not in css.text
    face = world.client.get("/static/fonts/InterVariable.woff2")
    assert face.status_code == 200
    assert face.content[:4] == b"wOF2"


def bot_files(root: Path) -> dict[str, bytes]:
    folder = root / "bots"
    if not folder.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def test_room_replies_stay_in_the_room_and_private_chats_do_not_change(world):
    local = add_endpoint(world, "local", url="http://127.0.0.1:8101/v1")
    remote = add_endpoint(world, "remote", url="http://127.0.0.1:8102/v1", model="grid-model")
    ann = add_bot(world, "Ann", local["id"])
    ben = add_bot(world, "Ben", remote["id"], model="ben-model")
    ann_chat = world.client.post(f"/api/bots/{ann['id']}/chats").json()
    ben_chat = world.client.post(f"/api/bots/{ben['id']}/chats").json()
    assert world.client.post(
        f"/api/bots/{ann['id']}/chats/{ann_chat['id']}/messages",
        json={"content": "ann private"},
    ).status_code == 200
    assert world.client.post(
        f"/api/bots/{ben['id']}/chats/{ben_chat['id']}/messages",
        json={"content": "ben private"},
    ).status_code == 200
    private = bot_files(world.path)

    room = world.client.post("/api/rooms", json={"name": "Desk"}).json()
    assert room["messages"] == []
    assert bot_files(world.path) == private
    empty = world.client.post(f"/api/rooms/{room['id']}/messages", json={"content": "nobody here"})
    assert empty.status_code == 400
    assert world.client.get(f"/api/rooms/{room['id']}").json()["messages"] == []
    assert bot_files(world.path) == private
    added = world.client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": ann["id"]})
    assert added.status_code == 200, added.text
    added = world.client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": ben["id"]})
    assert added.status_code == 200, added.text
    assert [member["name"] for member in added.json()["members"]] == ["Ann", "Ben"]
    assert bot_files(world.path) == private

    def reply(messages):
        system = messages[0]["content"]
        if "You are Ann" in system:
            return "ann-in-room"
        if "You are Ben" in system:
            return "ben-in-room"
        return "unexpected"

    world.rec.reply = reply
    sent = world.client.post(f"/api/rooms/{room['id']}/messages", json={"content": "hello room"})
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert [(m["speaker_name"], m["content"], m.get("error")) for m in body["messages"]] == [
        ("You", "hello room", None),
        ("Ann", "ann-in-room", None),
        ("Ben", "ben-in-room", None),
    ]
    assert bot_files(world.path) == private
    assert world.rec.calls[-2]["model"] is None
    assert world.rec.calls[-1]["model"] == "ben-model"
    assert "Speak only as yourself" in world.rec.calls[-1]["messages"][0]["content"]
    seen_by_ben = [m.get("content") for m in world.rec.calls[-1]["messages"]]
    seen_by_ann = [m.get("content") for m in world.rec.calls[-2]["messages"]]
    assert "hello room" in seen_by_ann
    assert "hello room" in seen_by_ben
    assert "ann-in-room" not in seen_by_ben
    room_path = world.path / "rooms" / f"{room['id']}.json"
    assert room_path.is_file()
    assert "ann private" not in room_path.read_text()

    removed = world.client.delete(f"/api/rooms/{room['id']}/bots/{ann['id']}")
    assert removed.status_code == 200, removed.text
    assert [member["name"] for member in removed.json()["members"]] == ["Ben"]
    assert [m["content"] for m in removed.json()["messages"]] == ["hello room", "ann-in-room", "ben-in-room"]
    assert (world.path / "bots" / ann["id"] / "bot.json").is_file()
    assert bot_files(world.path) == private

    restarted = TestClient(create_app(world.path))
    again = restarted.get(f"/api/rooms/{room['id']}").json()
    assert [m["content"] for m in again["messages"]] == ["hello room", "ann-in-room", "ben-in-room"]
    assert again["members"][0]["name"] == "Ben"
    assert bot_files(world.path) == private


def test_room_keeps_going_when_one_bot_errors(world):
    local = add_endpoint(world, "local", url="http://127.0.0.1:8101/v1")
    dead = add_endpoint(world, "dead", url="http://127.0.0.1:9/v1")
    ann = add_bot(world, "Ann", dead["id"])
    ben = add_bot(world, "Ben", local["id"])
    ann_chat = world.client.post(f"/api/bots/{ann['id']}/chats").json()
    world.client.post(
        f"/api/bots/{ann['id']}/chats/{ann_chat['id']}/messages",
        json={"content": "keep ann"},
    )
    private = bot_files(world.path)
    room = world.client.post("/api/rooms", json={"name": "Desk"}).json()
    world.client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": ann["id"]})
    world.client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": ben["id"]})

    def reply(messages):
        system = messages[0]["content"]
        if "You are Ann" in system:
            raise ProviderError("ann endpoint down")
        return "ben still answered"

    world.rec.reply = reply
    sent = world.client.post(f"/api/rooms/{room['id']}/messages", json={"content": "both of you"})
    assert sent.status_code == 200, sent.text
    messages = sent.json()["messages"]
    assert messages[0]["content"] == "both of you"
    assert messages[1]["speaker_name"] == "Ann" and messages[1]["error"] is True
    assert "ann endpoint down" in messages[1]["content"]
    assert messages[2]["speaker_name"] == "Ben" and messages[2]["content"] == "ben still answered"
    assert messages[2].get("error") is not True
    assert bot_files(world.path) == private
    assert "keep ann" in (world.path / "bots" / ann["id"] / "chats" / f"{ann_chat['id']}.json").read_text()


def test_two_bots_use_different_token_budgets_and_keep_every_turn(world):
    endpoint = add_endpoint(world, "local")
    small = add_bot(world, "Small", endpoint["id"], context_tokens=MIN_CONTEXT_TOKENS)
    wide = add_bot(world, "Wide", endpoint["id"])
    assert small["context_tokens"] == MIN_CONTEXT_TOKENS
    assert small["context_chars"] == MIN_CONTEXT_TOKENS * 4
    assert wide["context_tokens"] == DEFAULT_CONTEXT_TOKENS
    assert wide["context_chars"] == MAX_CONTEXT_CHARS
    refused = world.client.post(
        "/api/bots",
        json={"name": "Huge", "endpoint_id": endpoint["id"], "context_tokens": 2_000_000},
    )
    assert refused.status_code == 400
    assert "1000000" in refused.text
    assert len(world.client.get("/api/bots").json()) == 2

    early = "EARLY-TURN-KEEP-" + ("q" * 400)
    chats = {}
    sent = {}
    for bot in (small, wide):
        chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
        chats[bot["id"]] = chat["id"]
        contents = []
        for index in range(6):
            content = early if index == 0 else f"turn-{bot['name']}-{index}-" + ("z" * 600)
            contents.append(content)
            response = world.client.post(
                f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
                json={"content": content},
            )
            assert response.status_code == 200, response.text
        sent[bot["id"]] = contents

    def chat_path(bot_id):
        return world.path / "bots" / bot_id / "chats" / f"{chats[bot_id]}.json"

    def user_texts(bot_id):
        stored = json.loads(chat_path(bot_id).read_text())
        return [item["content"] for item in stored["messages"] if item["role"] == "user"]

    assert user_texts(small["id"]) == sent[small["id"]]
    assert user_texts(wide["id"]) == sent[wide["id"]]
    assert len(json.loads(chat_path(small["id"]).read_text())["messages"]) == 12
    assert len(json.loads(chat_path(wide["id"]).read_text())["messages"]) == 12

    small_view = world.client.get(f"/api/bots/{small['id']}/chats/{chats[small['id']]}").json()
    wide_view = world.client.get(f"/api/bots/{wide['id']}/chats/{chats[wide['id']]}").json()
    small_chars = MIN_CONTEXT_TOKENS * 4
    assert small_view["context"]["max_context_chars"] == small_chars
    assert small_view["context"]["max_context_tokens"] == MIN_CONTEXT_TOKENS
    assert small_view["context"]["context_chars"] <= small_chars
    assert wide_view["context"]["compacted"] is False
    assert wide_view["context"]["model_messages"] == 12
    assert small_view["context"]["bounded"] is True
    assert small_view["context"]["summarized_through"] > wide_view["context"]["summarized_through"]
    assert small_view["context"]["model_messages"] < wide_view["context"]["model_messages"]
    assert len(small_view["messages"]) == 12
    assert len(wide_view["messages"]) == 12
    assert [item["content"] for item in small_view["messages"]] == [
        item["content"] for item in json.loads(chat_path(small["id"]).read_text())["messages"]
    ]

    def last_request(name):
        matches = [
            call["messages"]
            for call in world.rec.calls
            if call["messages"][0]["content"].startswith(f"You are {name},")
        ]
        return matches[-1]

    small_payload = json.dumps(last_request("Small"))
    wide_payload = json.dumps(last_request("Wide"))
    assert early not in small_payload
    assert "q" * 400 in wide_payload
    small_tail = [item for item in last_request("Small") if item["role"] != "system"]
    wide_tail = [item for item in last_request("Wide") if item["role"] != "system"]
    assert len(small_tail) < len(wide_tail)
    assert len(wide_tail) > 1

    raw = chat_path(wide["id"]).read_bytes()
    patched = world.client.patch(f"/api/bots/{wide['id']}", json={"context_tokens": MIN_CONTEXT_TOKENS})
    assert patched.status_code == 200, patched.text
    assert patched.json()["context_tokens"] == MIN_CONTEXT_TOKENS
    assert chat_path(wide["id"]).read_bytes() == raw
    tightened = world.client.get(f"/api/bots/{wide['id']}/chats/{chats[wide['id']]}").json()
    assert len(tightened["messages"]) == 12
    assert [item["content"] for item in tightened["messages"]] == [
        item["content"] for item in wide_view["messages"]
    ]
    assert tightened["context"]["model_messages"] < wide_view["context"]["model_messages"]
    assert tightened["context"]["max_context_chars"] == small_chars
    assert early in chat_path(wide["id"]).read_text()

    world.rec.calls.clear()
    follow = world.client.post(
        f"/api/bots/{wide['id']}/chats/{chats[wide['id']]}/messages",
        json={"content": "after the budget change"},
    )
    assert follow.status_code == 200, follow.text
    assert len(follow.json()["chat"]["messages"]) == 14
    assert early in chat_path(wide["id"]).read_text()
    follow_tail = [item for item in world.rec.calls[-1]["messages"] if item["role"] != "system"]
    assert len(follow_tail) < len(wide_tail)
    assert "q" * 400 not in json.dumps(world.rec.calls[-1]["messages"])


def test_parent_asks_one_child_and_keeps_both_transcripts(world):
    from easyagent.subagent import parse_subagent

    assert parse_subagent("```subagent\nbot: the bot\nthe task\n```") is None
    endpoint = add_endpoint(world, "local")
    parent = add_bot(world, "Parent", endpoint["id"])
    child = add_bot(world, "Child", endpoint["id"])

    def reply(messages):
        system = messages[0]["content"]
        blob = json.dumps(messages)
        if "You were given one task" in system:
            if "FAIL-THIS-TASK" in blob:
                raise ProviderError("child endpoint down")
            return "RESULT-FROM-CHILD"
        return "ack"

    world.rec.reply = reply
    child_secret = "CHILD-HISTORY-ONLY-not-for-the-parent"
    parent_secret = "PARENT-HISTORY-ONLY-not-for-the-child"
    child_chat = world.client.post(f"/api/bots/{child['id']}/chats").json()
    parent_chat = world.client.post(f"/api/bots/{parent['id']}/chats").json()
    assert world.client.post(
        f"/api/bots/{child['id']}/chats/{child_chat['id']}/messages",
        json={"content": child_secret},
    ).status_code == 200
    assert world.client.post(
        f"/api/bots/{parent['id']}/chats/{parent_chat['id']}/messages",
        json={"content": parent_secret},
    ).status_code == 200

    def chat_bytes(bot_id):
        folder = world.path / "bots" / bot_id / "chats"
        return {path.name: path.read_bytes() for path in sorted(folder.glob("*.json"))}

    child_before = chat_bytes(child["id"])
    parent_before_count = len(json.loads(next(iter(chat_bytes(parent["id"]).values())))["messages"])
    bots_before = {item["id"] for item in world.client.get("/api/bots").json()}

    asked = world.client.post(
        f"/api/bots/{parent['id']}/chats/{parent_chat['id']}/ask",
        json={"bot_id": child["id"], "task": "count the chairs"},
    )
    assert asked.status_code == 200, asked.text
    body = asked.json()
    assert "RESULT-FROM-CHILD" in body["reply"]
    assert child_secret not in body["reply"]
    assert child_secret not in json.dumps(body["chat"]["messages"])
    assert child_secret not in (body["chat"].get("summary") or "")
    assert child_secret not in json.dumps(body["context"])
    assert parent_secret in json.dumps(body["chat"]["messages"])
    assert len(body["chat"]["messages"]) > parent_before_count

    child_calls = [
        call for call in world.rec.calls if "You were given one task" in call["messages"][0]["content"]
    ]
    assert len(child_calls) == 1
    sent = json.dumps(child_calls[0]["messages"])
    assert len(child_calls[0]["messages"]) == 2
    assert child_calls[0]["messages"][1]["content"] == "count the chairs"
    assert parent_secret not in sent
    assert child_secret not in sent
    assert "Never delete" in child_calls[0]["messages"][0]["content"]

    child_after = chat_bytes(child["id"])
    assert set(child_before) < set(child_after)
    for name, raw in child_before.items():
        assert child_after[name] == raw
    new_files = [json.loads(raw) for name, raw in child_after.items() if name not in child_before]
    assert len(new_files) == 1
    assert [item["content"] for item in new_files[0]["messages"]] == ["count the chairs", "RESULT-FROM-CHILD"]
    assert parent_secret not in json.dumps(new_files[0])
    assert {item["id"] for item in world.client.get("/api/bots").json()} == bots_before

    same = world.client.post(
        f"/api/bots/{parent['id']}/chats/{parent_chat['id']}/ask",
        json={"bot_id": parent["id"], "task": "ask myself"},
    )
    assert same.status_code == 400
    missing = world.client.post(
        f"/api/bots/{parent['id']}/chats/{parent_chat['id']}/ask",
        json={"bot_id": "00000000-0000-4000-8000-000000000000", "task": "missing bot"},
    )
    assert missing.status_code == 404
    assert chat_bytes(child["id"]) == child_after

    failed = world.client.post(
        f"/api/bots/{parent['id']}/chats/{parent_chat['id']}/ask",
        json={"bot_id": child["id"], "task": "FAIL-THIS-TASK now"},
    )
    assert failed.status_code == 200, failed.text
    failed_messages = failed.json()["chat"]["messages"]
    assert any(item.get("error") and "child endpoint down" in item["content"] for item in failed_messages)
    assert parent_secret in json.dumps(failed_messages)
    assert "RESULT-FROM-CHILD" in json.dumps(failed_messages)
    assert child_secret not in json.dumps(failed_messages)
    for name, raw in child_before.items():
        assert chat_bytes(child["id"])[name] == raw
    failed_child = [
        json.loads(raw)
        for name, raw in chat_bytes(child["id"]).items()
        if name not in child_after
    ]
    assert len(failed_child) == 1
    assert failed_child[0]["messages"][0]["content"] == "FAIL-THIS-TASK now"
    assert failed_child[0]["messages"][1]["error"] is True
    assert "RESULT-FROM-CHILD" in (world.path / "bots" / child["id"] / "chats" / f"{new_files[0]['id']}.json").read_text()

    def fenced_reply(messages):
        system = messages[0]["content"]
        blob = json.dumps(messages)
        if "You were given one task" in system:
            assert parent_secret not in blob
            assert child_secret not in blob
            return "RESULT-FROM-FENCE"
        return "```subagent\nbot: Child\nfold the towels\n```"

    world.rec.reply = fenced_reply
    fenced = world.client.post(
        f"/api/bots/{parent['id']}/chats/{parent_chat['id']}/messages",
        json={"content": "please ask Child to fold the towels"},
    )
    assert fenced.status_code == 200, fenced.text
    fenced_messages = json.dumps(fenced.json()["chat"]["messages"])
    assert "RESULT-FROM-FENCE" in fenced_messages
    assert "fold the towels" in fenced_messages
    assert child_secret not in fenced_messages
    assert parent_secret in fenced_messages
    for name, raw in child_before.items():
        assert chat_bytes(child["id"])[name] == raw


def test_edit_connection_and_bot_keeps_the_original_chat(world):
    home = add_endpoint(world, "home", url="http://127.0.0.1:8101/v1", api_key="secret", model="old-model")
    other = add_endpoint(world, "other", url="http://127.0.0.1:8102/v1", model="other-model")
    ada = add_bot(world, "Ada", home["id"])
    chat = world.client.post(f"/api/bots/{ada['id']}/chats").json()
    sent = world.client.post(
        f"/api/bots/{ada['id']}/chats/{chat['id']}/messages",
        json={"content": "keep-this-line"},
    )
    assert sent.status_code == 200, sent.text
    before = chat_files(world.path, ada["id"])

    patched = world.client.patch(
        f"/api/endpoints/{home['id']}",
        json={"base_url": "http://127.0.0.1:8109/v1", "model": "your-model"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["base_url"] == "http://127.0.0.1:8109/v1"
    assert body["model"] == "your-model"
    assert body["has_api_key"] is True
    assert "secret" not in patched.text
    stored_endpoints = (world.path / "endpoints.json").read_text()
    assert "secret" in stored_endpoints
    assert chat_files(world.path, ada["id"]) == before
    shown = world.client.get(f"/api/bots/{ada['id']}/chats/{chat['id']}").json()
    assert shown["messages"][0]["content"] == "keep-this-line"
    assert shown["messages"][1]["content"] == "ack"

    renamed = world.client.patch(
        f"/api/bots/{ada['id']}",
        json={"name": "Ada Two", "endpoint_id": other["id"]},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Ada Two"
    assert renamed.json()["endpoint_base_url"] == "http://127.0.0.1:8102/v1"
    assert chat_files(world.path, ada["id"]) == before
    shown = world.client.get(f"/api/bots/{ada['id']}/chats/{chat['id']}").json()
    assert shown["messages"][0]["content"] == "keep-this-line"

    bea = add_bot(world, "Bea", other["id"])
    assert chat_files(world.path, ada["id"]) == before
    names = {item["name"] for item in world.client.get("/api/bots").json()}
    assert names == {"Ada Two", "Bea"}
    shown = world.client.get(f"/api/bots/{ada['id']}/chats/{chat['id']}").json()
    assert shown["messages"][0]["content"] == "keep-this-line"
    assert bea["id"] != ada["id"]


def test_stream_reply_arrives_in_pieces_and_a_failure_stays_in_the_chat(world, monkeypatch):
    async def pieces(**kwargs):
        yield "Hel"
        yield "lo"

    monkeypatch.setattr("easyagent.llm.stream_complete", pieces)
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with world.client.stream(
        "POST",
        url,
        json={"content": "hi there"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    hel = body.index('"text": "Hel"')
    lo = body.index('"text": "lo"')
    done = body.index('"type": "done"')
    assert hel < lo < done
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert [item["content"] for item in stored["messages"]] == ["hi there", "Hello"]
    raw = (world.path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text()
    assert "hi there" in raw and "Hello" in raw

    async def fail(**kwargs):
        raise ProviderError("connection refused")
        yield ""

    monkeypatch.setattr("easyagent.llm.stream_complete", fail)
    with world.client.stream(
        "POST",
        url,
        json={"content": "second line"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        failed = response.read().decode()
    assert '"type": "error"' in failed
    assert "connection refused" in failed
    assert "http://127.0.0.1:9/v1" in failed
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert [item["content"] for item in stored["messages"]][:3] == [
        "hi there",
        "Hello",
        "second line",
    ]
    assert stored["messages"][3]["content"].startswith("Stopped:")
    assert "connection refused" in stored["messages"][3]["content"]
    assert stored["messages"][-1]["error"] is True
    raw = (world.path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text()
    assert "hi there" in raw and "Hello" in raw and "second line" in raw


def test_a_dropped_model_connection_is_shown_and_the_next_send_works(world, monkeypatch):
    """A closed stream is Stopped, and another message can be sent."""
    detail = (
        "Could not reach http://localhost:8080/v1: "
        "peer closed connection without sending complete message body (incomplete chunked read)"
    )
    calls = {"n": 0}

    async def reply(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError(detail)
        yield "Back."

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with world.client.stream(
        "POST",
        url,
        json={"content": "hello"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        failed = response.read().decode()
    assert '"type": "error"' not in failed
    assert "incomplete chunked read" not in failed
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert stored["messages"][-1]["content"] == "Stopped: the reply stopped before it finished."
    assert "incomplete chunked read" not in stored["messages"][-1]["content"]
    assert stored["messages"][-1].get("error") is not True
    assert stored["run"]["status"] == "stopped"
    with world.client.stream(
        "POST",
        url,
        json={"content": "try again"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        followed = response.read().decode()
    assert "Back." in followed
    assert '"type": "error"' not in followed
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert [item["content"] for item in stored["messages"]][-2:] == ["try again", "Back."]


def test_a_closed_stream_keeps_partial_thinking_and_reply(world, monkeypatch):
    """Stop keeps the Thinking and reply already received. It does not show the peer-closed line."""
    detail = (
        "Could not reach http://localhost:8080/v1: "
        "peer closed connection without sending complete message body (incomplete chunked read)"
    )

    async def reply(**kwargs):
        yield "<think>half a plan</think>Partial reply"
        raise ProviderError(detail)

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with world.client.stream(
        "POST",
        url,
        json={"content": "hello"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    assert "half a plan" in body
    assert "Partial reply" in body
    assert "incomplete chunked read" not in body
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    last = stored["messages"][-1]
    assert last["content"] == "Partial reply"
    assert last["thinking"] == "half a plan"
    assert last.get("error") is not True
    raw = (world.path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_text()
    assert "incomplete chunked read" not in raw


def test_settle_marks_thinking_with_no_answer(world):
    """A dropped turn that only has Thinking is Stopped, not a blank success."""
    from easyagent.app import _settle_interrupted
    from easyagent.store import Store

    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    store = Store(world.path)
    stored = store.get_chat(bot["id"], chat["id"])
    stored["messages"] = [
        {"id": "u1", "role": "user", "content": "hello", "created_at": "2026-10-05T00:00:00Z"},
        {
            "id": "a1",
            "role": "assistant",
            "content": "",
            "thinking": "a long plan that stops mid-sentence",
            "live": True,
            "created_at": "2026-10-05T00:00:01Z",
        },
    ]
    store.save_chat(stored)
    _settle_interrupted(store, bot["id"], chat["id"])
    again = store.get_chat(bot["id"], chat["id"])
    last = again["messages"][-1]
    assert last["content"] == "Stopped: the reply stopped before it finished."
    assert last["thinking"] == "a long plan that stops mid-sentence"
    assert last.get("live") is not True
    assert last.get("error") is not True


def test_settle_closes_a_live_assistant_that_is_not_last(world):
    """An older live reply stays closed after a newer user turn."""
    from easyagent.app import _settle_interrupted
    from easyagent.store import Store

    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    store = Store(world.path)
    stored = store.get_chat(bot["id"], chat["id"])
    stored["messages"] = [
        {"id": "u1", "role": "user", "content": "first", "created_at": "2026-10-05T00:00:00Z"},
        {
            "id": "a1",
            "role": "assistant",
            "content": "",
            "thinking": "still planning the first answer",
            "live": True,
            "created_at": "2026-10-05T00:00:01Z",
        },
        {"id": "u2", "role": "user", "content": "second", "created_at": "2026-10-05T00:00:02Z"},
    ]
    store.save_chat(stored)
    _settle_interrupted(store, bot["id"], chat["id"])
    again = store.get_chat(bot["id"], chat["id"])
    messages = again["messages"]
    assert messages[1]["content"] == "Stopped: the reply stopped before it finished."
    assert messages[1]["thinking"] == "still planning the first answer"
    assert messages[1].get("live") is not True
    assert all(not item.get("live") for item in messages)
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "Stopped: the reply stopped before it finished."
    assert [item["content"] for item in messages] == [
        "first",
        "Stopped: the reply stopped before it finished.",
        "second",
        "Stopped: the reply stopped before it finished.",
    ]


def test_finish_reply_keeps_an_answer_when_only_thinking_arrived(world):
    """Thinking does not excuse a permanent empty answer."""
    from easyagent.app import _finish_reply
    from easyagent.store import Store

    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    store = Store(world.path)
    result = _finish_reply(
        store, bot["id"], chat["id"], "", [], [], "", "a plan and nothing else"
    )
    last = result["chat"]["messages"][-1]
    assert last["content"] == "(empty reply)"
    assert last["error"] is True
    assert last["thinking"] == "a plan and nothing else"
    assert last.get("live") is not True


def test_reasoning_only_asks_once_and_does_not_save_a_blank_answer(world, monkeypatch):
    """A stream of only reasoning is not a finished reply."""
    calls = {"n": 0}

    async def reply(**kwargs):
        calls["n"] += 1
        from easyagent.llm import note_reasoning

        if calls["n"] == 1:
            note_reasoning("a plan with no sentence yet")
            raise ProviderError("Endpoint returned an empty message.")
        yield "Hi, I'm ready to help."

    monkeypatch.setattr("easyagent.llm.stream_complete", reply)
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with world.client.stream(
        "POST",
        url,
        json={"content": "hello"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    assert "a plan with no sentence yet" in body
    assert "Hi, I'm ready to help." in body
    assert '"type": "error"' not in body
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    last = stored["messages"][-1]
    assert last["content"] == "Hi, I'm ready to help."
    assert last["thinking"] == "a plan with no sentence yet"
    assert last.get("live") is not True
    assert last.get("error") is not True
    assert calls["n"] == 2


def _chunked(payload: bytes) -> bytes:
    return f"{len(payload):x}\r\n".encode() + payload + b"\r\n"


async def _read_http_request(reader) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            return
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    pending = length - len(rest)
    if pending > 0:
        await reader.readexactly(pending)


def test_a_quiet_gap_between_reasoning_chunks_is_not_a_dropped_stream():
    """A few quiet seconds on an open socket is not a failure. The read wait stays open."""
    import httpx

    import easyagent.llm as llm

    seen = {}
    original = llm.httpx.AsyncClient

    class Watching(original):
        def __init__(self, *args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            super().__init__(*args, **kwargs)

    async def run():
        async def handle(reader, writer):
            await _read_http_request(reader)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
            )
            await writer.drain()
            await asyncio.sleep(4)
            events = (
                b'data: {"choices":[{"delta":{"reasoning_content":"Still thinking after the quiet gap."}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"The answer."}}]}\n\n'
                b"data: [DONE]\n\n"
            )
            writer.write(_chunked(events))
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        thoughts: list[str] = []
        token = llm.attach_reasoning_sink(thoughts.append)
        parts: list[str] = []
        try:
            async for piece in llm.stream_complete(
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key=None,
                model=None,
                messages=[{"role": "user", "content": "think"}],
            ):
                parts.append(piece)
        finally:
            llm.detach_reasoning_sink(token)
            server.close()
            await server.wait_closed()
        return "".join(parts), "".join(thoughts)

    llm.httpx.AsyncClient = Watching
    try:
        text, thought = asyncio.run(run())
    finally:
        llm.httpx.AsyncClient = original
    timeout = seen["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read is None
    assert timeout.connect == 10
    assert text == "The answer."
    assert thought == "Still thinking after the quiet gap."


def test_many_reasoning_chunks_then_content_are_both_saved(world):
    """reasoning_content chunks, then content chunks, both stay in the chat."""
    import threading

    holder: dict = {}

    async def handle(reader, writer):
        try:
            await _read_http_request(reader)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
            )
            await writer.drain()
            pieces = []
            for index in range(12):
                event = {"choices": [{"delta": {"reasoning_content": f"step {index}. "}}]}
                pieces.append(b"data: " + json.dumps(event).encode() + b"\n\n")
            for bit in ("Hi, ", "I'm ready ", "to help."):
                event = {"choices": [{"delta": {"content": bit}}]}
                pieces.append(b"data: " + json.dumps(event).encode() + b"\n\n")
            pieces.append(b"data: [DONE]\n\n")
            writer.write(_chunked(b"".join(pieces)))
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def serve():
        async def main():
            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            stop = asyncio.Event()
            holder["stop"] = stop
            holder["loop"] = asyncio.get_running_loop()
            holder["port"] = server.sockets[0].getsockname()[1]
            holder["ready"].set()
            await stop.wait()
            server.close()
            await server.wait_closed()

        asyncio.run(main())

    holder["ready"] = threading.Event()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert holder["ready"].wait(5)
    try:
        endpoint = add_endpoint(world, "local", url=f"http://127.0.0.1:{holder['port']}/v1")
        bot = add_bot(world, "Ada", endpoint["id"])
        chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
        url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
        with world.client.stream(
            "POST",
            url,
            json={"content": "hello"},
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200, response.read()
            body = response.read().decode()
    finally:
        loop = holder.get("loop")
        if loop is not None:
            loop.call_soon_threadsafe(holder["stop"].set)
        thread.join(timeout=5)
    assert "step 0." in body
    assert "step 11." in body
    assert "Hi, I'm ready to help." in body
    assert "Stopped." not in body
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    last = stored["messages"][-1]
    assert last["content"] == "Hi, I'm ready to help."
    assert "step 0." in last["thinking"]
    assert "step 11." in last["thinking"]
    assert last.get("live") is not True
    assert last.get("error") is not True


def test_a_closed_model_stream_is_still_an_error():
    """A socket that actually closes still names that failure."""
    import easyagent.llm as llm

    async def run():
        async def handle(reader, writer):
            await _read_http_request(reader)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async for _piece in llm.stream_complete(
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key=None,
                model=None,
                messages=[{"role": "user", "content": "think"}],
            ):
                pass
        finally:
            server.close()
            await server.wait_closed()

    with pytest.raises(ProviderError) as caught:
        asyncio.run(run())
    detail = str(caught.value)
    assert "Could not reach" in detail
    assert "incomplete chunked read" in detail


def test_sending_again_stops_a_quiet_model_stream(tmp_path):
    """A stream with no read timeout still stops when the person sends again."""
    import easyagent.llm as llm
    from easyagent import turn as turn_mod
    from easyagent.store import Store

    store = Store(tmp_path)
    store.ensure()

    async def run():
        handlers: list[asyncio.Task] = []

        async def handle(reader, writer):
            handlers.append(asyncio.current_task())
            try:
                await _read_http_request(reader)
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                )
                await writer.drain()
                await asyncio.sleep(30)
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        turn_mod.bind(store, "chat-1")

        async def collect():
            async for _piece in llm.stream_complete(
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key=None,
                model=None,
                messages=[{"role": "user", "content": "think"}],
            ):
                pass

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.3)
        turn_mod.interrupt(store, "chat-1")
        try:
            with pytest.raises(turn_mod.TurnCancelled):
                await asyncio.wait_for(task, timeout=3)
        finally:
            for handler in handlers:
                handler.cancel()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_a_second_send_cancels_the_model_request(tmp_path, monkeypatch):
    """Sending again aborts the turn that is still waiting on the model."""
    import httpx

    from easyagent import turn as turn_mod

    calls = {"n": 0}
    started = asyncio.Event()

    async def hang(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            while not turn_mod.cancelled():
                await asyncio.sleep(0.02)
            return
        yield "Moved on."

    monkeypatch.setattr("easyagent.llm.stream_complete", hang)

    async def scenario():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            endpoint = (
                await client.post(
                    "/api/endpoints",
                    json={"name": "local", "base_url": "http://127.0.0.1:9/v1"},
                )
            ).json()
            bot = (
                await client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]})
            ).json()
            chat = (await client.post(f"/api/bots/{bot['id']}/chats")).json()
            url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
            headers = {"Accept": "text/event-stream"}

            async def first():
                async with client.stream(
                    "POST", url, json={"content": "keep going"}, headers=headers
                ) as response:
                    body = (await response.aread()).decode()
                    return response.status_code, body

            task = asyncio.create_task(first())
            await asyncio.wait_for(started.wait(), timeout=5)
            async with client.stream(
                "POST", url, json={"content": "stop and do this"}, headers=headers
            ) as response:
                assert response.status_code == 200
                body = (await response.aread()).decode()
            status, first_body = await asyncio.wait_for(task, timeout=5)
            stored = (await client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}")).json()
        assert status == 200
        assert "Moved on." not in first_body
        assert "Moved on." in body
        assert '"type": "error"' not in first_body
        texts = [item["content"] for item in stored["messages"]]
        assert texts[0] == "keep going"
        assert "stop and do this" in texts
        assert texts[-1] == "Moved on."
        assert calls["n"] >= 2

    asyncio.run(scenario())


def test_reasoning_stays_out_of_the_answer_and_out_of_the_next_call(world, monkeypatch):
    """A think block is a Thinking section. It is not the reply, and it is not sent back."""
    from easyagent.llm import peel_thinking
    from easyagent.tools import _spoken

    assert peel_thinking("Hello") == ("", "Hello")
    assert peel_thinking("<think>quiet plan</think>The reply.") == ("quiet plan", "The reply.")
    assert peel_thinking("<thinking>quiet plan</thinking>\nThe reply.") == ("quiet plan", "The reply.")
    assert peel_thinking("```think\nquiet plan\n```\nThe reply.") == ("quiet plan", "The reply.")
    hidden, answer = peel_thinking("<think>still going")
    assert hidden == "still going"
    assert answer == ""
    spoken = _spoken(
        "<think>The title wins.</think>I found the rooms.\n\n```finish\nproven\n```\nYou can open it."
    )
    assert "The title wins" not in spoken
    assert "```" not in spoken
    assert "finish" not in spoken.lower()
    assert "proven" not in spoken.lower()
    assert "I found the rooms." in spoken
    assert "You can open it." in spoken

    seen = []

    async def pieces(**kwargs):
        seen.append(kwargs["messages"])
        if len(seen) == 1:
            yield "<think>secret-plan-99</think>Hello"
        else:
            yield "Next"

    monkeypatch.setattr("easyagent.llm.stream_complete", pieces)
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with world.client.stream(
        "POST",
        url,
        json={"content": "hi there"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    think_at = body.index('"type": "thinking"')
    plan_at = body.index("secret-plan-99")
    reply_at = body.index('"text": "Hello"')
    assert think_at < plan_at < reply_at
    assert "```" not in body
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    reply = stored["messages"][-1]
    assert reply["content"] == "Hello"
    assert reply["thinking"] == "secret-plan-99"
    assert "secret-plan-99" not in reply["content"]
    assert "<think>" not in reply["content"]

    with world.client.stream(
        "POST",
        url,
        json={"content": "and then"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        follow = response.read().decode()
    assert '"type": "thinking"' not in follow
    packed = json.dumps(seen[1])
    assert "Hello" in packed
    assert "secret-plan-99" not in packed
    assert "<think>" not in packed
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert [item["content"] for item in stored["messages"]] == ["hi there", "Hello", "and then", "Next"]
    assert "thinking" not in stored["messages"][-1]
    page = world.client.get("/")
    assert 'app.js?v=37' in page.text
    assert 'app.css?v=32' in page.text
    assert 'id="stop"' in page.text
    assert 'id="continue"' in page.text
    script = world.client.get("/static/app.js?v=37")
    assert script.status_code == 200
    assert "model-thinking" in script.text
    assert "function setProse" in script.text
    assert "createTextNode" in script.text
    assert "new AbortController()" in script.text
    assert "function stopFlight" in script.text
    assert "Stopped." in script.text
    assert "still waiting on the model" in script.text
    assert "run-pulse" in script.text
    assert "function runTone" in script.text
    assert 'startsWith("Model not answering")' in script.text
    assert '"reconnecting"' in script.text
    assert "function botIsLive" in script.text
    assert "run-dots" in script.text
    assert "is-halted" in script.text
    assert "bot-live" in script.text
    assert "function continueRun" in script.text
    send_fn = script.text.split("async function sendDraft")[1].split("async function retry")[0]
    assert '$("send").disabled' not in send_fn
    css = world.client.get("/static/app.css?v=32")
    assert css.status_code == 200
    assert ".model-thinking" in css.text
    assert ".model-thinking-p" in css.text
    assert ".run-pulse" in css.text
    assert ".run-indicator.is-waiting" in css.text
    assert ".run-indicator.is-reconnecting" in css.text
    assert ".buddy-face.is-reconnecting" in css.text
    assert ".run-indicator.is-thinking" in css.text
    assert ".run-indicator.is-tool" in css.text
    assert ".run-indicator.is-halted" in css.text
    assert "run-shimmer" in css.text
    assert "run-ellipsis" in css.text
    assert ".bot-live" in css.text
    assert "prefers-reduced-motion" in css.text
    assert "run-fade" in css.text
    assert ".message .body code" in css.text


def test_stream_keeps_reasoning_content_out_of_the_answer():
    import easyagent.llm as llm
    from easyagent.llm import complete, peel_thinking, stream_complete

    assert peel_thinking("Hello")[0] == ""
    events = [
        {"choices": [{"delta": {"reasoning_content": "Look at the title. "}}]},
        {"choices": [{"delta": {"reasoning_content": "The heading is a slogan."}}]},
        {"choices": [{"delta": {"content": "<think>duplicate</think>The reply."}}]},
    ]
    lines = ["data: " + json.dumps(event) for event in events] + ["data: [DONE]"]

    class FakeResponse:
        status_code = 200
        text = ""
        headers = {"content-type": "text/event-stream"}

        async def aread(self):
            return b""

        def aiter_lines(self):
            async def generate():
                for line in lines:
                    yield line

            return generate()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aclose(self):
            return None

        def stream(self, method, url, json=None, headers=None):
            class Context:
                async def __aenter__(self):
                    return FakeResponse()

                async def __aexit__(self, *args):
                    return False

            return Context()

    bucket = []
    original = llm.httpx.AsyncClient
    llm.httpx.AsyncClient = FakeClient
    token = llm.attach_reasoning_sink(bucket.append)
    try:
        async def collect():
            parts = []
            async for piece in stream_complete(
                base_url="http://example.test/v1",
                api_key=None,
                model="m",
                messages=[{"role": "user", "content": "a"}],
            ):
                parts.append(piece)
            return "".join(parts)

        streamed = asyncio.run(collect())
    finally:
        llm.detach_reasoning_sink(token)
        llm.httpx.AsyncClient = original
    assert streamed == "The reply."
    assert "duplicate" not in streamed
    assert "Look at the title." not in streamed
    assert "".join(bucket) == "Look at the title. The heading is a slogan."

    calls = [
        {"index": 0, "function": {"name": "list_dir", "arguments": "{\"path\": \"/tmp/ea-a\"}"}},
    ]
    body = {
        "choices": [{
            "message": {
                "content": "I will list and run.",
                "reasoning_content": "planning the list",
                "tool_calls": calls,
            },
        }]
    }

    class JsonResponse:
        status_code = 200
        text = ""
        headers = {"content-type": "application/json"}

        def json(self):
            return body

        async def aread(self):
            return json.dumps(body).encode()

        def aiter_lines(self):
            async def empty():
                if False:
                    yield ""

            return empty()

    class JsonClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aclose(self):
            return None

        async def post(self, url, json=None, headers=None):
            return JsonResponse()

        def stream(self, method, url, json=None, headers=None):
            class Context:
                async def __aenter__(self):
                    return JsonResponse()

                async def __aexit__(self, *args):
                    return False

            return Context()

    noted = []
    llm.httpx.AsyncClient = JsonClient
    token = llm.attach_reasoning_sink(noted.append)
    try:
        text = asyncio.run(
            complete(
                base_url="http://example.test/v1",
                api_key=None,
                model=None,
                messages=[{"role": "user", "content": "a"}],
            )
        )
    finally:
        llm.detach_reasoning_sink(token)
        llm.httpx.AsyncClient = original
    assert "```files" in text
    assert "I will list and run." in text
    assert "planning the list" not in text
    assert noted == ["planning the list"]


def test_debug_raw_saves_the_first_chunks_only_when_asked(tmp_path, monkeypatch):
    """Raw stream chunks are saved only when asked. They are not turned into reasoning."""
    import easyagent.llm as llm
    from easyagent.llm import stream_complete

    monkeypatch.delenv("EASYAGENT_DEBUG_RAW", raising=False)
    monkeypatch.setenv("EASYAGENT_DATA", str(tmp_path))
    events = [
        {"choices": [{"delta": {"reasoning_content": "raw-thought-77"}}]},
        {"choices": [{"delta": {"content": "The reply."}}]},
    ]
    for index in range(3, 9):
        events.append({"choices": [{"delta": {"content": f"line-{index}"}}]})
    events[-1] = {"choices": [{"delta": {"content": "UNIQUE-LATE"}}]}
    lines = ["data: " + json.dumps(event) for event in events] + ["data: [DONE]"]

    class FakeResponse:
        status_code = 200
        text = ""
        headers = {"content-type": "text/event-stream"}

        async def aread(self):
            return b""

        def aiter_lines(self):
            async def generate():
                for line in lines:
                    yield line

            return generate()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aclose(self):
            return None

        def stream(self, method, url, json=None, headers=None):
            class Context:
                async def __aenter__(self):
                    return FakeResponse()

                async def __aexit__(self, *args):
                    return False

            return Context()

    original = llm.httpx.AsyncClient
    llm.httpx.AsyncClient = FakeClient

    async def collect():
        parts = []
        async for piece in stream_complete(
            base_url="http://example.test/v1",
            api_key=None,
            model="m",
            messages=[{"role": "user", "content": "a"}],
        ):
            parts.append(piece)
        return "".join(parts)

    try:
        quiet = asyncio.run(collect())
        assert not (tmp_path / "debug-raw.txt").exists()
        monkeypatch.setenv("EASYAGENT_DEBUG_RAW", "1")
        streamed = asyncio.run(collect())
    finally:
        llm.httpx.AsyncClient = original
    assert quiet == "The reply.line-3line-4line-5line-6line-7UNIQUE-LATE"
    assert streamed == quiet
    assert "raw-thought-77" not in streamed
    saved = (tmp_path / "debug-raw.txt").read_text(encoding="utf-8")
    assert saved.startswith("--- ")
    assert "raw-thought-77" in saved
    assert "The reply." in saved
    assert "line-6" in saved
    assert "UNIQUE-LATE" not in saved
    assert "line-7" not in saved


def test_adding_a_bot_opens_its_first_chat(world):
    """A new bot is selected, its first chat is created, and the message box is focused."""
    page = world.client.get("/")
    assert 'app.js?v=37' in page.text
    script = world.client.get("/static/app.js?v=37")
    assert script.status_code == 200
    submit = script.text.split('$("bot-form").addEventListener("submit"')[1].split('$("toggle-room")')[0]
    assert 'api(`/api/bots/${bot.id}/chats`' in submit
    assert 'method: "POST"' in submit
    assert "selectBot(bot.id, chat.id)" in submit
    assert '$("draft").focus()' in submit
    assert "await selectBot(bot.id);" not in submit


def test_a_running_turn_is_visible_before_the_model_answers(world, monkeypatch):
    """Reload can see that the bot is working: status, clock, and the current step."""
    seen = {}

    async def pieces(**kwargs):
        matches = list(world.path.glob("bots/*/chats/*.json"))
        assert len(matches) == 1
        seen["doc"] = json.loads(matches[0].read_text(encoding="utf-8"))
        from easyagent.llm import note_reasoning

        note_reasoning("check the X account first")
        note_reasoning("Let me first check")
        yield "Ready."

    monkeypatch.setattr("easyagent.llm.stream_complete", pieces)
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    assert chat["run"]["status"] == "idle"
    assert "started_at" in chat["run"]
    assert "last_activity_at" in chat["run"]
    assert "current_step" in chat["run"]
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with world.client.stream(
        "POST",
        url,
        json={"content": "hello"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    assert seen["doc"]["run"]["status"] == "running"
    assert seen["doc"]["run"]["current_step"] == "Waiting on model"
    assert seen["doc"]["run"]["started_at"]
    assert seen["doc"]["run"]["last_activity_at"]
    assert '"type": "run"' in body
    assert "Waiting on model" in body
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert stored["run"]["status"] == "idle"
    assert stored["messages"][-1]["content"] == "Ready."
    assert stored["messages"][-1]["thinking"] == "check the X account first Let me first check"
    assert "firstLet" not in stored["messages"][-1]["thinking"]


def test_a_failed_run_says_stopped_and_can_be_retried(world, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("disk blew up")
        yield ""

    monkeypatch.setattr("easyagent.llm.stream_complete", boom)
    endpoint = add_endpoint(world, "local")
    bot = add_bot(world, "Ada", endpoint["id"])
    chat = world.client.post(f"/api/bots/{bot['id']}/chats").json()
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with world.client.stream(
        "POST",
        url,
        json={"content": "hello"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    assert '"type": "stopped"' in body
    assert "Stopped: disk blew up" in body
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert stored["messages"][-1]["content"].startswith("Stopped: disk blew up")
    assert stored["run"]["status"] == "stopped"
    assert stored["run"]["current_step"] == ""

    async def fine(**kwargs):
        yield "Back."

    monkeypatch.setattr("easyagent.llm.stream_complete", fine)
    with world.client.stream(
        "POST",
        f"/api/bots/{bot['id']}/chats/{chat['id']}/retry",
        json={},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        followed = response.read().decode()
    assert "Back." in followed
    stored = world.client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    assert stored["messages"][-1]["content"] == "Back."
    assert stored["run"]["status"] == "idle"
