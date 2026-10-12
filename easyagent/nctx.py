"""How full the model's context window is, without an extra completion.

llama.cpp reports ``n_ctx`` on ``GET /props`` (and sometimes ``GET /slots``).
Ollama reports it on ``POST /api/show``. A miss is remembered briefly so a
quiet server is not asked again on every turn. Measuring does not send a chat.
"""

from __future__ import annotations

import re
import threading
import time
from contextvars import ContextVar

import httpx

from easyagent.context import clean_tokens

HIT_SECONDS = 600.0
MISS_SECONDS = 30.0
WARN_RATIO = 0.8
FULL_RATIO = 0.9

_NUM_CTX = re.compile(r"(?i)\bnum_ctx\s+(\d+)\b")
_CACHE: dict[str, tuple[int | None, float]] = {}
_RUNTIME: dict[str, int] = {}
_LOCK = threading.Lock()
_SYSTEM_TOKENS: ContextVar[int] = ContextVar("easyagent_system_tokens", default=0)
_TOOL_SCHEMA_TOKENS: int | None = None
_OVERFLOW = (
    "context length",
    "context size",
    "available context",
    "maximum context",
    "context window exceeded",
    "context overflow",
    "too many tokens",
    "prompt is too long",
    "n_ctx_exceed",
)


def server_root(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def cache_key(base_url: str, model: str | None) -> str:
    return f"{server_root(base_url).lower()}|{(model or '').strip().lower()}"


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
        _RUNTIME.clear()


def note_system_tokens(count: int) -> None:
    """Remember the system prompt size for this turn. It is not a model call."""
    try:
        number = int(count or 0)
    except (TypeError, ValueError):
        return
    if number > 0:
        _SYSTEM_TOKENS.set(number)


def take_system_tokens() -> int:
    value = _SYSTEM_TOKENS.get()
    _SYSTEM_TOKENS.set(0)
    return value if value > 0 else 0


def context_overflow(text: str) -> bool:
    """True when the server refused the turn because the prompt did not fit."""
    lowered = (text or "").lower()
    return any(item in lowered for item in _OVERFLOW)


def remember_runtime(base_url: str, model: str | None, value: int | None) -> None:
    """Keep a detected window in this process. It is not written to endpoints.json."""
    number = clean_tokens(value)
    if not number or not (base_url or "").strip():
        return
    with _LOCK:
        _RUNTIME[cache_key(base_url, model)] = number
        _RUNTIME[server_root(base_url).lower()] = number


def runtime_n_ctx(base_url: str, model: str | None = None) -> int | None:
    if not (base_url or "").strip():
        return None
    with _LOCK:
        found = _RUNTIME.get(cache_key(base_url, model))
        if found is None:
            found = _RUNTIME.get(server_root(base_url).lower())
    return found


def remember(base_url: str, model: str | None, value: int | None, *, ttl: float | None = None) -> None:
    seconds = HIT_SECONDS if value else MISS_SECONDS
    if ttl is not None:
        seconds = ttl
    with _LOCK:
        _CACHE[cache_key(base_url, model)] = (value, time.monotonic() + seconds)


def cached(base_url: str, model: str | None) -> int | None | object:
    """The remembered window, None for a remembered miss, or a sentinel when nothing is stored."""
    with _LOCK:
        entry = _CACHE.get(cache_key(base_url, model))
    if entry is None:
        return _MISSING
    value, expires = entry
    if expires <= time.monotonic():
        return _MISSING
    return value


_MISSING = object()


def parse_llama_props(data: object) -> int | None:
    if not isinstance(data, dict):
        return None
    settings = data.get("default_generation_settings")
    if isinstance(settings, dict):
        found = clean_tokens(settings.get("n_ctx"))
        if found:
            return found
    return clean_tokens(data.get("n_ctx"))


def parse_llama_slots(data: object) -> int | None:
    rows = data if isinstance(data, list) else None
    if rows is None and isinstance(data, dict) and isinstance(data.get("slots"), list):
        rows = data.get("slots")
    if not isinstance(rows, list):
        return None
    best: int | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        found = clean_tokens(row.get("n_ctx"))
        if found and (best is None or found > best):
            best = found
    return best


def parse_ollama_show(data: object) -> int | None:
    """Prefer the running ``num_ctx``. Otherwise the architecture context length."""
    if not isinstance(data, dict):
        return None
    params = data.get("parameters")
    if isinstance(params, str):
        match = _NUM_CTX.search(params)
        if match:
            found = clean_tokens(int(match.group(1)))
            if found:
                return found
    info = data.get("model_info")
    if isinstance(info, dict):
        best: int | None = None
        for key, value in info.items():
            if not str(key).endswith("context_length"):
                continue
            found = clean_tokens(value)
            if found and (best is None or found > best):
                best = found
        return best
    return None


def resolve_n_ctx(endpoint: dict | None, detected: int | None = None, model: str | None = None) -> int:
    """Fresh detection, then this process's cache, then the endpoint setting."""
    fresh = clean_tokens(detected)
    if fresh:
        return fresh
    if not isinstance(endpoint, dict):
        return 0
    wanted = model if model is not None else endpoint.get("model")
    cached_window = runtime_n_ctx(endpoint.get("base_url") or "", wanted if isinstance(wanted, str) else None)
    if cached_window:
        return cached_window
    fallback = clean_tokens(endpoint.get("n_ctx"))
    if fallback:
        return fallback
    return 0


def tool_schema_tokens() -> int:
    """The native tool list, in tokens. A quiet server does not report this, and the estimate was short without it."""
    global _TOOL_SCHEMA_TOKENS
    if _TOOL_SCHEMA_TOKENS is not None:
        return _TOOL_SCHEMA_TOKENS
    import json

    from easyagent.llm import native_tools

    raw = json.dumps(native_tools())
    count = -(-len(raw) // 3)
    if count < 0:
        count = 0
    _TOOL_SCHEMA_TOKENS = count
    return count


def apply_meter(stats: dict, n_ctx: int, usage_prompt: int) -> dict:
    """Add the header numbers. A real prompt count wins; otherwise the estimate."""
    out = dict(stats or {})
    try:
        real = int(usage_prompt or 0)
    except (TypeError, ValueError):
        real = 0
    if real < 0:
        real = 0
    try:
        estimated_used = int(out.get("context_tokens") or 0)
    except (TypeError, ValueError):
        estimated_used = 0
    try:
        system_tokens = int(out.get("system_tokens") or 0)
    except (TypeError, ValueError):
        system_tokens = 0
    if system_tokens < 0:
        system_tokens = 0
    if real > 0:
        used = real
        estimated = 0
    else:
        used = estimated_used if estimated_used > 0 else 0
        used += system_tokens
        used += tool_schema_tokens()
        estimated = 1
    try:
        window = int(n_ctx or 0)
    except (TypeError, ValueError):
        window = 0
    if window < 0:
        window = 0
    level = 0
    if window > 0 and used > 0:
        ratio = used / window
        if ratio >= FULL_RATIO:
            level = 2
        elif ratio >= WARN_RATIO:
            level = 1
    overflow = False
    try:
        overflow = int(out.get("context_overflow") or 0) == 1
    except (TypeError, ValueError):
        overflow = False
    out["prompt_tokens"] = used
    out["prompt_estimated"] = estimated
    out["n_ctx"] = window
    out["context_level"] = level
    out["system_tokens"] = system_tokens
    out["offer_compact"] = 1 if level >= 1 or overflow else 0
    return out


async def _read_json(client: httpx.AsyncClient, method: str, url: str, headers: dict, body: dict | None) -> object | None:
    try:
        if method == "POST":
            response = await client.post(url, headers=headers, json=body or {})
        else:
            response = await client.get(url, headers=headers)
    except Exception:
        return None
    if response.status_code >= 400:
        return None
    try:
        return response.json()
    except Exception:
        return None


async def probe_n_ctx(base_url: str, model: str | None = None, api_key: str | None = None) -> int | None:
    """Ask the server how large its window is. No chat completion."""
    url = (base_url or "").strip()
    if not url:
        return None
    stored = cached(url, model)
    if stored is not _MISSING:
        return stored if isinstance(stored, int) else None
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    root = server_root(url).rstrip("/")
    timeout = httpx.Timeout(0.8, connect=0.4)
    found: int | None = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            props = await _read_json(client, "GET", root + "/props", headers, None)
            found = parse_llama_props(props)
            if found is None:
                slots = await _read_json(client, "GET", root + "/slots", headers, None)
                found = parse_llama_slots(slots)
            if found is None and (model or "").strip():
                shown = await _read_json(client, "POST", root + "/api/show", headers, {"name": model})
                found = parse_ollama_show(shown)
    except Exception:
        found = None
    remember(url, model, found)
    if found:
        remember_runtime(url, model, found)
    return found
