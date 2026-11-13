"""Server-side TTS (edge-tts): synthesis wrapper and the bot endpoints.

No test touches the network. edge-tts's Communicate is replaced with a fake
that records what it was handed, and the endpoints run with tts.synthesize
monkeypatched to a canned mp3.
"""

import time

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.tools import ToolError
from easyagent.tts import DEFAULT_VOICE, MAX_CHARS, VOICES, synthesize


class FakeCommunicate:
    """Stands in for edge_tts.Communicate. Records its arguments."""

    calls: list[dict] = []
    fail: Exception | None = None
    chunks: list[dict] = [{"type": "audio", "data": b"FAKE"}, {"type": "audio", "data": b"MP3"}]

    def __init__(self, text, voice):
        FakeCommunicate.calls.append({"text": text, "voice": voice})

    async def stream(self):
        if FakeCommunicate.fail is not None:
            raise FakeCommunicate.fail
        for chunk in FakeCommunicate.chunks:
            yield chunk


@pytest.fixture(autouse=True)
def _clear_fakes(monkeypatch):
    FakeCommunicate.calls = []
    FakeCommunicate.fail = None
    FakeCommunicate.chunks = [{"type": "audio", "data": b"FAKE"}, {"type": "audio", "data": b"MP3"}]
    monkeypatch.setattr("edge_tts.Communicate", FakeCommunicate)


def _world(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    client = TestClient(create_app(tmp_path))
    endpoint = client.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    bot = client.post(
        "/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"], "model": None}
    ).json()
    return client, bot


# --- synthesize (network faked at the edge-tts boundary) -------------------


def test_synthesize_returns_audio_and_passes_voice_through():
    audio = synthesize("Hello there.", "en-GB-RyanNeural")
    assert audio == b"FAKEMP3"
    assert FakeCommunicate.calls == [{"text": "Hello there.", "voice": "en-GB-RyanNeural"}]


def test_synthesize_defaults_the_voice():
    synthesize("Hello.")
    assert FakeCommunicate.calls[0]["voice"] == DEFAULT_VOICE


def test_synthesize_caps_the_text():
    synthesize("x" * (MAX_CHARS + 500))
    assert len(FakeCommunicate.calls[0]["text"]) == MAX_CHARS


def test_synthesize_rejects_empty_text():
    with pytest.raises(ToolError):
        synthesize("   ")
    assert FakeCommunicate.calls == []


def test_synthesize_reports_network_failure_plainly(monkeypatch):
    FakeCommunicate.fail = ConnectionError("no route to host")
    with pytest.raises(ToolError) as excinfo:
        synthesize("Hello.")
    message = str(excinfo.value)
    assert "Traceback" not in message
    assert "ConnectionError" not in message
    assert message.strip()  # one plain sentence


def test_synthesize_times_out(monkeypatch):
    def slow(_text, _voice):
        time.sleep(5)

    monkeypatch.setattr("easyagent.tts._amass", slow)
    with pytest.raises(ToolError):
        synthesize("Hello.", timeout=0.3)


# --- endpoints (synthesize monkeypatched) ----------------------------------


def test_tts_endpoint_returns_mpeg(tmp_path, monkeypatch):
    client, bot = _world(tmp_path, monkeypatch)
    seen = {}

    def fake_synthesize(text, voice=None, timeout=None):
        seen["text"] = text
        seen["voice"] = voice
        return b"FAKEMP3"

    monkeypatch.setattr("easyagent.tts.synthesize", fake_synthesize)
    response = client.post(
        f"/api/bots/{bot['id']}/tts", json={"text": "Hello.", "voice": "en-US-GuyNeural"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"FAKEMP3"
    assert seen == {"text": "Hello.", "voice": "en-US-GuyNeural"}


def test_tts_endpoint_rejects_unknown_voice(tmp_path, monkeypatch):
    client, bot = _world(tmp_path, monkeypatch)
    monkeypatch.setattr("easyagent.tts.synthesize", lambda *a, **k: b"FAKEMP3")
    response = client.post(
        f"/api/bots/{bot['id']}/tts", json={"text": "Hello.", "voice": "xx-YY-BogusNeural"}
    )
    assert response.status_code == 400
    assert "voice" in response.text.lower()


def test_tts_endpoint_rejects_empty_text(tmp_path, monkeypatch):
    client, bot = _world(tmp_path, monkeypatch)
    response = client.post(f"/api/bots/{bot['id']}/tts", json={"text": "  "})
    assert response.status_code == 400


def test_tts_endpoint_surfaces_plain_errors(tmp_path, monkeypatch):
    client, bot = _world(tmp_path, monkeypatch)

    def down(text, voice=None, timeout=None):
        raise ToolError("Speech did not come back. Check the internet connection and try again.")

    monkeypatch.setattr("easyagent.tts.synthesize", down)
    response = client.post(f"/api/bots/{bot['id']}/tts", json={"text": "Hello."})
    assert response.status_code == 400
    assert "Traceback" not in response.text


def test_voices_endpoint_lists_the_picker(tmp_path, monkeypatch):
    client, bot = _world(tmp_path, monkeypatch)
    response = client.get(f"/api/bots/{bot['id']}/tts/voices")
    assert response.status_code == 200
    data = response.json()
    ids = [voice["id"] for voice in data["voices"]]
    assert DEFAULT_VOICE in ids
    assert all(voice.get("label") for voice in data["voices"])
    assert data["default"] == DEFAULT_VOICE


def test_voice_preference_round_trip(tmp_path, monkeypatch):
    client, bot = _world(tmp_path, monkeypatch)
    saved = client.post(
        f"/api/bots/{bot['id']}/tts/voice", json={"voice": "en-AU-WilliamNeural"}
    )
    assert saved.status_code == 200
    assert saved.json()["voice"] == "en-AU-WilliamNeural"

    listed = client.get(f"/api/bots/{bot['id']}/tts/voices")
    assert listed.json()["voice"] == "en-AU-WilliamNeural"

    # a fresh Store sees the preference on disk, like other bot settings
    from easyagent.store import Store
    from easyagent.tts import get_voice

    assert get_voice(Store(tmp_path).get_bot(bot["id"])) == "en-AU-WilliamNeural"

    # and a synthesis with no voice in the body speaks with the saved one
    seen = {}

    def fake_synthesize(text, voice=None, timeout=None):
        seen["voice"] = voice
        return b"FAKEMP3"

    monkeypatch.setattr("easyagent.tts.synthesize", fake_synthesize)
    response = client.post(f"/api/bots/{bot['id']}/tts", json={"text": "Hello."})
    assert response.status_code == 200
    assert seen["voice"] == "en-AU-WilliamNeural"


def test_voice_preference_rejects_unknown(tmp_path, monkeypatch):
    client, bot = _world(tmp_path, monkeypatch)
    response = client.post(f"/api/bots/{bot['id']}/tts/voice", json={"voice": "nope"})
    assert response.status_code == 400
