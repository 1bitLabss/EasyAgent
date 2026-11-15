"""Image generation through one OpenAI-compatible endpoint.

The HTTP layer is faked: no test here touches the network. The picture reaches
the person the same way a written file does: the tool result names the file it
wrote, the turn's made-payload previews it, and _finish_reply attaches it to
the reply (see _preview_attachment in app.py). These tests pin that whole
chain, the config file's key hygiene, and the plain refusal when nothing is
set up yet.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from easyagent import imagegen
from easyagent import turn as turn_mod
from easyagent.app import _finish_reply
from easyagent.features import reset_routine_turn
from easyagent.imagegen import (
    MAX_IMAGE_BYTES,
    ImageGenError,
    extract_image,
    generate_image,
    load_config,
    public_config,
    save_config,
)
from easyagent.store import Store
from easyagent.tools import (
    ToolError,
    ToolRequest,
    _draw_png,
    _made_payload,
    _repair_tool_name,
    execute,
    happened_line,
    parse_tools,
)
from easyagent.workspace import assign_workspace
from tests.test_plan_0320 import _client

PNG = _draw_png()
B64 = base64.b64encode(PNG).decode("ascii")
KEY = "sk-imgsecret-9f3a"


def fresh_turn_context() -> None:
    """A direct execute() call binds no slot, so clear what an earlier test left."""
    turn_mod.renew_cancel()
    reset_routine_turn()


def _store(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    return store, bot


def _setup(store, **fields):
    config = {
        "enabled": True,
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "",
        "model": "flux-local",
    }
    config.update(fields)
    return save_config(store, **config)


def _fake_http(monkeypatch, payload=None, picture=PNG, get_picture=None):
    """Fake imagegen's HTTP helpers; return the list of (url, payload, headers) posts."""
    posts = []

    async def fake_post(url, body, headers=None):
        posts.append({"url": url, "payload": body, "headers": headers or {}})
        return payload if payload is not None else {"data": [{"b64_json": B64}]}

    async def fake_get(url):
        return get_picture if get_picture is not None else picture

    monkeypatch.setattr(imagegen, "post_json", fake_post)
    monkeypatch.setattr(imagegen, "get_bytes", fake_get)
    return posts


# --- config ----------------------------------------------------------------


def test_config_round_trips_and_the_key_stays_out_of_the_public_view(tmp_path):
    store, _bot = _store(tmp_path)
    _setup(store, api_key=KEY, size="512x512")
    loaded = load_config(store)
    assert loaded.enabled and loaded.base_url == "http://127.0.0.1:9/v1"
    assert loaded.model == "flux-local" and loaded.size == "512x512"
    shown = public_config(loaded)
    assert shown["has_key"] is True
    assert "api_key" not in shown
    assert KEY not in json.dumps(shown)


def test_empty_api_key_on_update_keeps_the_saved_key(tmp_path):
    store, _bot = _store(tmp_path)
    _setup(store, api_key=KEY)
    save_config(store, base_url="http://127.0.0.1:9/v1", api_key="")
    assert load_config(store).api_key == KEY


def test_a_base_url_that_is_not_http_is_refused(tmp_path):
    store, _bot = _store(tmp_path)
    with pytest.raises(ValueError) as exc:
        save_config(store, base_url="ftp://server/v1")
    assert exc.value.args[0] == "The image address must start with http:// or https://."
    assert not (tmp_path / "imagegen.json").is_file()


def test_api_round_trips_without_ever_showing_the_key(tmp_path):
    app, client, _bot, _chat = _client(tmp_path)
    store = app.state.store
    blank = client.get("/api/imagegen").json()
    assert blank["enabled"] is False and blank["has_key"] is False
    made = client.put(
        "/api/imagegen",
        json={"enabled": True, "base_url": "http://127.0.0.1:9/v1", "api_key": KEY, "model": "flux"},
    ).json()
    assert made["has_key"] is True and made["model"] == "flux"
    assert KEY not in json.dumps(made)
    kept = client.put("/api/imagegen", json={"api_key": ""}).json()
    assert kept["has_key"] is True
    assert load_config(store).api_key == KEY
    bad = client.put("/api/imagegen", json={"base_url": "ftp://server/v1"})
    assert bad.status_code == 400


# --- response parsing --------------------------------------------------------


def test_parser_reads_b64_json():
    data, url = extract_image({"data": [{"b64_json": B64}]})
    assert data == PNG and url == ""


def test_parser_hands_back_a_url_when_the_server_sent_no_bytes():
    data, url = extract_image({"data": [{"url": "http://server/out.png"}]})
    assert data is None and url == "http://server/out.png"


def test_parser_refuses_an_answer_without_a_picture():
    for payload in ({}, {"data": []}, {"data": [{}]}, {"data": [{"b64_json": "!!!"}]}):
        with pytest.raises(ImageGenError):
            extract_image(payload)


# --- generate_image ----------------------------------------------------------


def test_generate_posts_the_openai_shape_and_returns_the_picture(tmp_path, monkeypatch):
    store, _bot = _store(tmp_path)
    _setup(store, api_key=KEY)
    posts = _fake_http(monkeypatch)
    data = asyncio.run(generate_image(store, "a red fox"))
    assert data == PNG
    assert posts[0]["url"] == "http://127.0.0.1:9/v1/images/generations"
    assert posts[0]["payload"] == {
        "model": "flux-local",
        "prompt": "a red fox",
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }
    assert posts[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_generate_fetches_a_url_only_answer(tmp_path, monkeypatch):
    store, _bot = _store(tmp_path)
    _setup(store)
    _fake_http(monkeypatch, payload={"data": [{"url": "http://server/out.png"}]})
    assert asyncio.run(generate_image(store, "a red fox")) == PNG


def test_generate_refuses_a_picture_too_large_to_keep(tmp_path, monkeypatch):
    store, _bot = _store(tmp_path)
    _setup(store)
    _fake_http(
        monkeypatch,
        payload={"data": [{"url": "http://server/big.png"}]},
        get_picture=b"x" * (MAX_IMAGE_BYTES + 1),
    )
    with pytest.raises(ImageGenError) as exc:
        asyncio.run(generate_image(store, "a red fox"))
    assert "too large" in str(exc.value)


def test_unset_config_gives_the_plain_message(tmp_path):
    store, _bot = _store(tmp_path)
    with pytest.raises(ImageGenError) as exc:
        asyncio.run(generate_image(store, "a red fox"))
    assert "not set up yet" in str(exc.value)


def test_disabled_config_gives_the_plain_message(tmp_path):
    store, _bot = _store(tmp_path)
    _setup(store, enabled=False)
    with pytest.raises(ImageGenError) as exc:
        asyncio.run(generate_image(store, "a red fox"))
    assert "not set up yet" in str(exc.value)


# --- the tool ----------------------------------------------------------------


def test_image_fence_parses_and_the_sample_line_is_not_a_call():
    found = parse_tools("Sure.\n```image\na red fox in the snow\n```")
    assert len(found) == 1
    assert found[0].kind == "image" and found[0].body == "a red fox in the snow"
    assert parse_tools("```image\nthe picture you want\n```") == []
    assert _repair_tool_name("GenerateImage") == "generate_image"


def test_the_tool_saves_the_picture_and_the_reply_attaches_it(tmp_path, monkeypatch):
    app, _client_, bot, chat = _client(tmp_path)
    store = app.state.store
    fresh_turn_context()
    workspace = tmp_path.parent / f"ws-{tmp_path.name}"
    assign_workspace(store, store.get_bot(bot["id"]), str(workspace))
    _setup(store)
    _fake_http(monkeypatch)
    result = asyncio.run(execute(store, ToolRequest(kind="image", body="a red fox"), bot["id"]))
    assert "Made a picture" in result and "a red fox" in result
    made_line = happened_line(ToolRequest(kind="image", body="a red fox"), [], result)
    assert made_line.startswith("Wrote ") and made_line.endswith(".")
    path = made_line[len("Wrote ") : -1]
    picture = Path(path)
    assert picture.is_file() and picture.read_bytes() == PNG
    payload = _made_payload([made_line])
    assert payload and json.loads(payload)["media_type"] == "image/png"
    done = _finish_reply(store, bot["id"], chat["id"], "Here is your fox.", [], made=payload)
    messages = (done.get("chat") or {}).get("messages") or []
    last = messages[-1] if messages else {}
    attachment = last.get("attachment") or {}
    assert attachment.get("media_type") == "image/png"
    assert attachment.get("name", "").endswith(".png")
    _meta, saved = store.read_chat_file(bot["id"], chat["id"], attachment["id"])
    assert saved == PNG


def test_the_tool_refuses_in_plain_words_when_not_set_up(tmp_path):
    store, bot = _store(tmp_path)
    fresh_turn_context()
    with pytest.raises(ToolError) as exc:
        asyncio.run(execute(store, ToolRequest(kind="image", body="a red fox"), bot["id"]))
    assert "not set up yet" in str(exc.value)
