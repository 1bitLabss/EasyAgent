"""One conversation per bot. History stays on disk and inside the budget."""

import json
from datetime import datetime, timedelta, timezone

import httpx

from easyagent.context import bot_context_chars
from easyagent.retrieve import EMBED_TIMEOUT, earlier_block
from easyagent.rolling import grounded_summary, refresh_rolling_summary
from easyagent.store import Store
from easyagent.turnctx import model_turn


DETAIL = "the cedar drawer code is VELVET-OTTER-50"
ASK = "What was the cedar drawer code?"


def _stamp(index: int) -> str:
    moment = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _bot(tmp_path, *, tokens: int = 512):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None, model=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None, context_tokens=tokens)
    return store, endpoint, bot


def test_five_thousand_turns_stay_in_budget_and_recall_turn_fifty(tmp_path):
    store, endpoint, bot = _bot(tmp_path)
    chat = store.create_chat(bot["id"])
    messages = []
    for index in range(5000):
        content = DETAIL if index == 49 else f"filler turn {index} about ordinary weather"
        messages.append(
            {"id": f"u{index:04d}", "role": "user", "content": content, "created_at": _stamp(index * 2)}
        )
        messages.append(
            {"id": f"a{index:04d}", "role": "assistant", "content": f"ack {index}", "created_at": _stamp(index * 2 + 1)}
        )
    messages.append({"id": "ask", "role": "user", "content": ASK, "created_at": _stamp(20000)})
    chat["messages"] = messages
    store.save_chat(chat)
    path = tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json"
    raw = path.read_bytes()
    for message in messages:
        assert message["content"].encode() in raw

    view = model_turn(store, store.get_bot(bot["id"]), store.get_chat(bot["id"], chat["id"]), endpoint)
    budget = bot_context_chars(store.get_bot(bot["id"]))
    tail = "\n".join(item.get("content") or "" for item in view.tail)
    assert view.used_chars <= budget
    assert view.stats["bounded"] is True
    assert "context full" not in view.earlier
    assert "context full" not in view.summary
    assert "From earlier:" in view.earlier
    assert DETAIL in view.earlier
    assert "2020-01-01T00:01:38Z" in view.earlier
    assert DETAIL not in tail
    assert path.read_bytes() == raw

    window = store.get_chat(bot["id"], chat["id"])
    shown = window["messages"][-10:]
    assert len(window["messages"]) == len(messages)
    assert DETAIL not in "\n".join(item["content"] for item in shown)
    page_start = len(messages) - 80
    older = messages[max(0, page_start - 80) : page_start]
    assert any(DETAIL in item["content"] for item in older) or DETAIL in raw.decode()


def test_window_and_start_fresh_keep_every_transcript_byte(tmp_path):
    from easyagent.app import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"], "context_tokens": 512}).json()
    store = Store(tmp_path)
    first = store.create_chat(bot["id"])
    first["messages"] = [
        {
            "id": "old",
            "role": "user",
            "content": "UNIQUE-OLD-PHRASE lives in the first transcript",
            "created_at": _stamp(1),
        }
    ]
    first["updated_at"] = "2020-01-01T00:00:00Z"
    store.save_chat(first)
    second = store.create_chat(bot["id"])
    second["messages"] = [
        {"id": f"n{i}", "role": "user", "content": f"hello from the newer chat {i}", "created_at": _stamp(10 + i)}
        for i in range(4)
    ]
    second["updated_at"] = "2020-06-01T00:00:00Z"
    store.save_chat(second)
    older_path = tmp_path / "bots" / bot["id"] / "chats" / f"{first['id']}.json"
    before = older_path.read_bytes()
    ongoing = client.get(f"/api/bots/{bot['id']}/ongoing").json()
    assert ongoing["id"] == second["id"]
    assert older_path.read_bytes() == before
    assert older_path.is_file()

    stored = client.get(f"/api/bots/{bot['id']}/chats/{second['id']}").json()
    assert stored["message_count"] == len(stored["messages"]) == 4
    window = client.get(f"/api/bots/{bot['id']}/chats/{second['id']}?window=1").json()
    assert window["message_count"] == 4
    assert len(window["messages"]) == 1
    assert window["window_start"] == 3
    page = client.get(
        f"/api/bots/{bot['id']}/chats/{second['id']}/messages",
        params={"before": window["window_start"], "limit": 2},
    ).json()
    assert page["end"] == 3
    assert page["start"] == 1
    assert page["total"] == 4
    assert [item["content"] for item in page["messages"]] == [
        "hello from the newer chat 1",
        "hello from the newer chat 2",
    ]

    fresh = client.post(f"/api/bots/{bot['id']}/chats/{second['id']}/fresh").json()
    assert fresh["fresh_from"] == 4
    assert [item["content"] for item in fresh["messages"]] == [item["content"] for item in stored["messages"]]
    assert older_path.read_bytes() == before

    chat = store.get_chat(bot["id"], second["id"])
    chat["messages"].append(
        {"id": "ask", "role": "user", "content": "Where did I put UNIQUE-OLD-PHRASE?", "created_at": _stamp(9)}
    )
    store.save_chat(chat)
    disk = older_path.read_bytes()
    view = model_turn(
        store,
        store.get_bot(bot["id"]),
        store.get_chat(bot["id"], second["id"]),
        store.get_endpoint(endpoint["id"]),
    )
    assert "UNIQUE-OLD-PHRASE" in view.earlier
    assert "From earlier:" in view.earlier
    assert view.used_chars <= bot_context_chars(store.get_bot(bot["id"]))
    assert "UNIQUE-OLD-PHRASE lives in the first transcript" not in "\n".join(item["content"] for item in view.tail)
    assert older_path.read_bytes() == disk
    assert store.ongoing_chat(bot["id"])["id"] == second["id"]
    touched = store.get_chat(bot["id"], first["id"])
    touched["updated_at"] = "2099-01-01T00:00:00Z"
    store.save_chat(touched)
    assert store.ongoing_chat(bot["id"])["id"] == second["id"]
    assert "UNIQUE-OLD-PHRASE" in older_path.read_text(encoding="utf-8")


def test_embeddings_miss_falls_back_to_bm25_without_waiting(tmp_path, monkeypatch):
    store, endpoint, bot = _bot(tmp_path, tokens=2048)
    chat = store.create_chat(bot["id"])
    chat["messages"] = [
        {"id": "m1", "role": "user", "content": DETAIL, "created_at": _stamp(1)},
        {"id": "m2", "role": "assistant", "content": "ack", "created_at": _stamp(2)},
        *[{"id": f"p{i}", "role": "user", "content": f"padding {i} " + ("y" * 4000), "created_at": _stamp(3 + i)} for i in range(30)],
        {"id": "ask", "role": "user", "content": ASK, "created_at": _stamp(80)},
    ]
    store.save_chat(chat)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "missing" in str(request.url):
            return httpx.Response(404, json={"error": "no"})
        raise httpx.ReadTimeout("slow")

    transport = httpx.MockTransport(handler)
    real = httpx.Client

    class _Client(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("easyagent.retrieve.httpx.Client", _Client)
    from easyagent import retrieve

    retrieve._EMBED.clear()
    retrieve._CACHE.clear()
    endpoint = dict(endpoint)
    endpoint["base_url"] = "http://embeddings.test/v1"
    block = earlier_block(store, bot["id"], store.get_chat(bot["id"], chat["id"]), 1, endpoint)
    assert DETAIL in block
    assert calls["n"] == 1
    again = earlier_block(store, bot["id"], store.get_chat(bot["id"], chat["id"]), 1, endpoint)
    assert DETAIL in again
    assert calls["n"] == 1
    assert EMBED_TIMEOUT <= 0.5

    retrieve._EMBED.clear()
    endpoint["base_url"] = "http://embeddings.test/missing"
    missed = earlier_block(store, bot["id"], store.get_chat(bot["id"], chat["id"]), 1, endpoint)
    assert DETAIL in missed
    assert calls["n"] >= 2


def test_rolling_summary_cites_real_ids_and_drops_inventions(tmp_path, monkeypatch):
    store, _endpoint, bot = _bot(tmp_path, tokens=2048)
    chat = store.create_chat(bot["id"])
    chat["messages"] = [
        {"id": "m1", "role": "user", "content": "The kiln code is BLUE-HERON-42", "created_at": _stamp(1)},
        {"id": "m2", "role": "assistant", "content": "Saved the kiln code.", "created_at": _stamp(2)},
    ]
    chat["summarized_through"] = 2
    store.save_chat(chat)
    path = tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json"
    before = [item["content"] for item in json.loads(path.read_text(encoding="utf-8"))["messages"]]

    invented = grounded_summary(
        "The kiln code is BLUE-HERON-42 [m:m1]\nThey also own a spaceship [m:m1]\nNo cite here.",
        chat["messages"],
    )
    assert "BLUE-HERON-42" in invented
    assert "spaceship" not in invented
    assert grounded_summary("They own a spaceship [m:missing]", chat["messages"]) == ""

    async def complete(**kwargs):
        assert "BLUE-HERON-42" in kwargs["messages"][0]["content"]
        return "The kiln code is BLUE-HERON-42 [m:m1]\nA secret volcano exists [m:m1]"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    import asyncio

    text = asyncio.run(refresh_rolling_summary(store, store.get_bot(bot["id"]), store.get_chat(bot["id"], chat["id"])))
    assert "BLUE-HERON-42" in text
    assert "volcano" not in text
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [item["content"] for item in saved["messages"]] == before
    assert len(saved["rolling_summary"]) <= 2000
