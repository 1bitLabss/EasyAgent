"""Picture making through one OpenAI-compatible image endpoint.

EasyAgent is local-first, so the endpoint is whatever the person runs: OpenAI
itself or a local DALL-E-compatible server (FastSDXL, SD.Next). One JSON file
in the data folder holds the settings. The key is read only when a picture is
made; every read of the settings for display leaves it out.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

import httpx

from easyagent.store import Store, atomic_write_json, read_json

DEFAULT_SIZE = "1024x1024"
MAX_IMAGE_BYTES = 9_500_000
_TIMEOUT = 120.0
_NOT_SET_UP = (
    "Image generation is not set up yet. Add the image address and the model name first."
)


class ImageGenError(Exception):
    """A refusal in plain words. The person reads this text."""


@dataclass(frozen=True)
class ImageGenConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    size: str = DEFAULT_SIZE


def config_path(store: Store) -> Path:
    return store.root / "imagegen.json"


def load_config(store: Store) -> ImageGenConfig:
    """The saved settings. A missing or broken file means not set up."""
    path = config_path(store)
    data = read_json(path) if path.is_file() else None
    if not isinstance(data, dict):
        return ImageGenConfig()
    return ImageGenConfig(
        enabled=bool(data.get("enabled")),
        base_url=str(data.get("base_url") or "").strip(),
        api_key=str(data.get("api_key") or ""),
        model=str(data.get("model") or "").strip(),
        size=str(data.get("size") or DEFAULT_SIZE).strip() or DEFAULT_SIZE,
    )


def validate_base_url(url: str) -> str:
    """The address must be http or https, the same rule the bridges use."""
    text = (url or "").strip()
    if text and not text.lower().startswith(("http://", "https://")):
        raise ValueError("The image address must start with http:// or https://.")
    return text


def save_config(
    store: Store,
    *,
    enabled: bool | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    size: str | None = None,
) -> ImageGenConfig:
    """Save the settings. An empty api_key keeps the key already saved."""
    old = load_config(store)
    url = old.base_url if base_url is None else validate_base_url(base_url)
    key = old.api_key if api_key is None or not api_key.strip() else api_key.strip()
    config = ImageGenConfig(
        enabled=old.enabled if enabled is None else bool(enabled),
        base_url=url,
        api_key=key,
        model=old.model if model is None else (model or "").strip(),
        size=(size or "").strip() or old.size or DEFAULT_SIZE,
    )
    atomic_write_json(
        config_path(store),
        {
            "enabled": config.enabled,
            "base_url": config.base_url,
            "api_key": config.api_key,
            "model": config.model,
            "size": config.size,
        },
    )
    return config


def public_config(config: ImageGenConfig) -> dict:
    """The settings for display. The key is never in this shape."""
    return {
        "enabled": config.enabled,
        "base_url": config.base_url,
        "model": config.model,
        "size": config.size,
        "has_key": bool(config.api_key),
    }


def extract_image(payload: object) -> tuple[bytes | None, str]:
    """The picture in an OpenAI-style answer: bytes, or a url to fetch.

    A server that ignores response_format answers with a url instead.
    """
    items = payload.get("data") if isinstance(payload, dict) else None
    first = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    b64 = str(first.get("b64_json") or "").strip()
    if b64:
        try:
            return base64.b64decode(b64, validate=True), ""
        except Exception as exc:
            raise ImageGenError("The image service sent a picture that was not readable.") from exc
    url = str(first.get("url") or "").strip()
    if url:
        return None, url
    raise ImageGenError("The image service answered without a picture.")


async def post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    """POST for JSON and answer with JSON. No answer is a failure."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=headers or {})
    except httpx.HTTPError as exc:
        raise ImageGenError("The image service did not answer.") from exc
    if response.status_code >= 400:
        raise ImageGenError("The image service did not answer.")
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def get_bytes(url: str) -> bytes:
    """Fetch a picture a server answered by url instead of bytes."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ImageGenError("The picture link did not answer.") from exc
    if response.status_code >= 400 or not response.content:
        raise ImageGenError("The picture link did not answer.")
    return response.content


async def generate_image(store: Store, prompt: str) -> bytes:
    """Make one picture from the prompt. Returns the image bytes."""
    config = load_config(store)
    if not config.enabled or not config.base_url or not config.model:
        raise ImageGenError(_NOT_SET_UP)
    text = (prompt or "").strip()
    if not text:
        raise ImageGenError("Name the picture you want. Nothing was made.")
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    body = {
        "model": config.model,
        "prompt": text,
        "n": 1,
        "size": config.size,
        "response_format": "b64_json",
    }
    data, image_url = extract_image(
        await post_json(config.base_url.rstrip("/") + "/images/generations", body, headers)
    )
    if data is None:
        data = await get_bytes(image_url)
    if not data:
        raise ImageGenError("The image service answered without a picture.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageGenError("The picture was too large to keep. Nothing was saved.")
    return data
