"""Voice notes turned into words through one OpenAI-compatible endpoint.

Browsers that ship no speech recognition (Safari, Firefox on some platforms)
still get the microphone: the recording is sent to a transcription service the
person runs or subscribes to, the same way picture making talks to one image
endpoint. One JSON file in the data folder holds the settings. The key is read
only when a recording is transcribed; every read for display leaves it out.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from easyagent.store import Store, atomic_write_json, read_json

DEFAULT_MODEL = "whisper-1"
MAX_AUDIO_BYTES = 25_000_000
_TIMEOUT = 120.0
_NOT_SET_UP = (
    "Voice transcription is not set up yet. Add the transcription address and"
    " the model name first."
)


class SttError(Exception):
    """A refusal in plain words. The person reads this text."""


@dataclass(frozen=True)
class SttConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = DEFAULT_MODEL


def config_path(store: Store) -> Path:
    return store.root / "stt.json"


def load_config(store: Store) -> SttConfig:
    """The saved settings. A missing or broken file means not set up."""
    path = config_path(store)
    data = read_json(path) if path.is_file() else None
    if not isinstance(data, dict):
        return SttConfig()
    return SttConfig(
        enabled=bool(data.get("enabled")),
        base_url=str(data.get("base_url") or "").strip(),
        api_key=str(data.get("api_key") or ""),
        model=str(data.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
    )


def validate_base_url(url: str) -> str:
    """The address must be http or https, the same rule the bridges use."""
    text = (url or "").strip()
    if text and not text.lower().startswith(("http://", "https://")):
        raise ValueError("The transcription address must start with http:// or https://.")
    return text


def save_config(
    store: Store,
    *,
    enabled: bool | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> SttConfig:
    """Save the settings. An empty api_key keeps the key already saved."""
    old = load_config(store)
    url = old.base_url if base_url is None else validate_base_url(base_url)
    key = old.api_key if api_key is None or not api_key.strip() else api_key.strip()
    config = SttConfig(
        enabled=old.enabled if enabled is None else bool(enabled),
        base_url=url,
        api_key=key,
        model=old.model if model is None else ((model or "").strip() or DEFAULT_MODEL),
    )
    atomic_write_json(
        config_path(store),
        {
            "enabled": config.enabled,
            "base_url": config.base_url,
            "api_key": config.api_key,
            "model": config.model,
        },
    )
    return config


def public_config(config: SttConfig) -> dict:
    """The settings for display. The key is never in this shape."""
    return {
        "enabled": config.enabled,
        "base_url": config.base_url,
        "model": config.model,
        "has_key": bool(config.api_key),
    }


def transcriptions_url(base_url: str) -> str:
    """Where the recording goes. The address may or may not carry /v1 already."""
    root = (base_url or "").strip().rstrip("/")
    if root.lower().endswith("/v1"):
        return root + "/audio/transcriptions"
    return root + "/v1/audio/transcriptions"


async def post_multipart(url: str, data: dict, files: dict, headers: dict | None = None) -> dict:
    """POST a recording as multipart and answer with JSON. No answer is a failure."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, data=data, files=files, headers=headers or {})
    except httpx.HTTPError as exc:
        raise SttError("The transcription service did not answer.") from exc
    if response.status_code >= 400:
        raise SttError("The transcription service did not answer.")
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def transcribe(store: Store, data: bytes, filename: str = "voice-note.webm") -> str:
    """Turn one recording into words. Returns the transcript text."""
    config = load_config(store)
    if not config.enabled or not config.base_url or not config.model:
        raise SttError(_NOT_SET_UP)
    if not data:
        raise SttError("The recording was empty. Nothing to transcribe.")
    if len(data) > MAX_AUDIO_BYTES:
        raise SttError("The recording was too large to send. Keep it under 25 MB.")
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    payload = await post_multipart(
        transcriptions_url(config.base_url),
        {"model": config.model},
        {"file": (filename, data, "application/octet-stream")},
        headers,
    )
    text = str(payload.get("text") or "").strip()
    if not text:
        raise SttError("The transcription service answered without any words.")
    return text
