"""Voice notes through one OpenAI-compatible transcription endpoint.

The HTTP layer is faked: no test here touches the network. The recording
arrives as a multipart upload on POST /api/stt, goes to the person's own
service, and the words come back as {"text": ...}. These tests pin the config
file's key hygiene, the plain refusals when nothing is set up or the answer is
empty, and the size cap on the upload.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from easyagent import stt
from easyagent.stt import (
    MAX_AUDIO_BYTES,
    SttError,
    load_config,
    public_config,
    save_config,
    transcribe,
    transcriptions_url,
)
from easyagent.store import Store
from tests.test_plan_0320 import _client

KEY = "***"
AUDIO = b"\x1a\x45\xdf\xa3fake-webm-bytes"


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
        "model": "whisper-1",
    }
    config.update(fields)
    return save_config(store, **config)


def _fake_http(monkeypatch, payload=None):
    """Fake stt's multipart POST; return the list of (url, data, files, headers)."""
    posts = []

    async def fake_post(url, data, files, headers=None):
        posts.append({"url": url, "data": data, "files": files, "headers": headers or {}})
        return payload if payload is not None else {"text": "hello from the mic"}

    monkeypatch.setattr(stt, "post_multipart", fake_post)
    return posts


# --- config ----------------------------------------------------------------


def test_config_round_trips_and_the_key_stays_out_of_the_public_view(tmp_path):
    store, _bot = _store(tmp_path)
    _setup(store, api_key=KEY, model="whisper-large")
    loaded = load_config(store)
    assert loaded.enabled and loaded.base_url == "http://127.0.0.1:9/v1"
    assert loaded.model == "whisper-large"
    shown = public_config(loaded)
    assert shown["has_key"] is True
    assert "api_key" not in shown
    assert KEY not in json.dumps(shown)


def test_model_defaults_to_whisper_1(tmp_path):
    store, _bot = _store(tmp_path)
    save_config(store, enabled=True, base_url="http://127.0.0.1:9/v1")
    assert load_config(store).model == "whisper-1"


def test_empty_api_key_on_update_keeps_the_saved_key(tmp_path):
    store, _bot = _store(tmp_path)
    _setup(store, api_key=KEY)
    save_config(store, base_url="http://127.0.0.1:9/v1", api_key="")
    assert load_config(store).api_key == KEY


def test_a_base_url_that_is_not_http_is_refused(tmp_path):
    store, _bot = _store(tmp_path)
    with pytest.raises(ValueError) as exc:
        save_config(store, base_url="ftp://server/v1")
    assert exc.value.args[0] == "The transcription address must start with http:// or https://."
    assert not (tmp_path / "stt.json").is_file()


def test_api_round_trips_without_ever_showing_the_key(tmp_path):
    app, client, _bot, _chat = _client(tmp_path)
    store = app.state.store
    blank = client.get("/api/stt").json()
    assert blank["enabled"] is False and blank["has_key"] is False
    made = client.put(
        "/api/stt",
        json={"enabled": True, "base_url": "http://127.0.0.1:9/v1", "api_key": KEY, "model": "whisper-1"},
    ).json()
    assert made["has_key"] is True and made["model"] == "whisper-1"
    assert KEY not in json.dumps(made)
    kept = client.put("/api/stt", json={"api_key": ""}).json()
    assert kept["has_key"] is True
    assert load_config(store).api_key == KEY
    bad = client.put("/api/stt", json={"base_url": "ftp://server/v1"})
    assert bad.status_code == 400


# --- url shape ---------------------------------------------------------------


def test_the_url_carries_v1_once_whether_or_not_the_address_does():
    assert transcriptions_url("http://host:8000/v1") == "http://host:8000/v1/audio/transcriptions"
    assert transcriptions_url("http://host:8000") == "http://host:8000/v1/audio/transcriptions"
    assert transcriptions_url("http://host:8000/") == "http://host:8000/v1/audio/transcriptions"


# --- transcribe --------------------------------------------------------------


def test_transcribe_posts_the_multipart_shape_and_returns_the_words(tmp_path, monkeypatch):
    store, _bot = _store(tmp_path)
    _setup(store, api_key=KEY)
    posts = _fake_http(monkeypatch)
    text = asyncio.run(transcribe(store, AUDIO))
    assert text == "hello from the mic"
    post = posts[0]
    assert post["url"] == "http://127.0.0.1:9/v1/audio/transcriptions"
    assert post["data"] == {"model": "whisper-1"}
    assert post["files"]["file"][1] == AUDIO
    assert post["headers"] == {"Authorization": f"Bearer {KEY}"}


def test_transcribe_refuses_when_not_set_up(tmp_path, monkeypatch):
    store, _bot = _store(tmp_path)
    _fake_http(monkeypatch)
    with pytest.raises(SttError) as exc:
        asyncio.run(transcribe(store, AUDIO))
    assert "not set up" in str(exc.value)


def test_transcribe_refuses_an_empty_recording(tmp_path, monkeypatch):
    store, _bot = _store(tmp_path)
    _setup(store)
    _fake_http(monkeypatch)
    with pytest.raises(SttError) as exc:
        asyncio.run(transcribe(store, b""))
    assert "empty" in str(exc.value)


def test_transcribe_refuses_an_oversize_recording(tmp_path, monkeypatch):
    store, _bot = _store(tmp_path)
    _setup(store)
    posts = _fake_http(monkeypatch)
    with pytest.raises(SttError) as exc:
        asyncio.run(transcribe(store, b"x" * (MAX_AUDIO_BYTES + 1)))
    assert "too large" in str(exc.value)
    assert posts == []


def test_transcribe_refuses_an_answer_without_words(tmp_path, monkeypatch):
    store, _bot = _store(tmp_path)
    _setup(store)
    for payload in ({}, {"text": ""}, {"text": "   "}, {"nope": 1}):
        _fake_http(monkeypatch, payload=payload)
        with pytest.raises(SttError):
            asyncio.run(transcribe(store, AUDIO))


# --- the upload endpoint ------------------------------------------------------


def test_upload_returns_the_words(tmp_path, monkeypatch):
    app, client, _bot, _chat = _client(tmp_path)
    _setup(app.state.store)
    posts = _fake_http(monkeypatch)
    reply = client.post("/api/stt", files={"file": ("voice.webm", AUDIO, "audio/webm")})
    assert reply.status_code == 200
    assert reply.json() == {"text": "hello from the mic"}
    assert posts[0]["files"]["file"][1] == AUDIO


def test_upload_rejects_an_empty_file(tmp_path, monkeypatch):
    app, client, _bot, _chat = _client(tmp_path)
    _setup(app.state.store)
    _fake_http(monkeypatch)
    reply = client.post("/api/stt", files={"file": ("voice.webm", b"", "audio/webm")})
    assert reply.status_code == 400
    assert "empty" in reply.json()["detail"]


def test_upload_rejects_an_oversize_file(tmp_path, monkeypatch):
    app, client, _bot, _chat = _client(tmp_path)
    _setup(app.state.store)
    posts = _fake_http(monkeypatch)
    reply = client.post(
        "/api/stt", files={"file": ("voice.webm", b"x" * (MAX_AUDIO_BYTES + 1), "audio/webm")}
    )
    assert reply.status_code == 400
    assert "too large" in reply.json()["detail"]
    assert posts == []


def test_upload_refuses_when_stt_is_disabled(tmp_path, monkeypatch):
    app, client, _bot, _chat = _client(tmp_path)
    _setup(app.state.store, enabled=False)
    _fake_http(monkeypatch)
    reply = client.post("/api/stt", files={"file": ("voice.webm", AUDIO, "audio/webm")})
    assert reply.status_code == 400
    assert "not set up" in reply.json()["detail"]
