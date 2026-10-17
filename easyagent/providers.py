"""Ordered provider fallback. A key is pinned to the connection that stored it.

A later request may use that key only with that connection's host. The previous
provider's error is not added to the next request, and a known key is removed
from an error before it is shown.
"""

from __future__ import annotations

import re
import threading
from contextvars import ContextVar
from urllib.parse import urlparse

_lock = threading.Lock()
_hosts: dict[str, tuple[str, str, int]] = {}
_owner_of: dict[str, str] = {}
_chain: ContextVar[tuple[dict, ...] | None] = ContextVar("easyagent_provider_chain", default=None)

_FAILOVER_STATUS = {401, 403, 408, 409, 429, 500, 502, 503, 504}
_STATUS = re.compile(r"^(\d{3}) from ")


class ProviderKeyError(Exception):
    """This key is pinned to a different provider."""


def reset_registry() -> None:
    """Tests start with no pinned keys and no fallback chain."""
    with _lock:
        _hosts.clear()
        _owner_of.clear()
    _chain.set(None)


def host_of(url: str) -> tuple[str, str, int]:
    """Scheme, hostname, and port. https without a port is 443, http is 80."""
    parsed = urlparse((url or "").strip())
    scheme = (parsed.scheme or "http").lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.port is not None:
        port = int(parsed.port)
    elif scheme == "https":
        port = 443
    else:
        port = 80
    return scheme, hostname, port


def _drop_owner(endpoint_id: str) -> None:
    for old, owner in list(_owner_of.items()):
        if owner == endpoint_id:
            _hosts.pop(old, None)
            _owner_of.pop(old, None)


def register(endpoint_id: str, base_url: str, api_key: str | None) -> None:
    """Pin this key to this connection's host. The first connection to store it wins."""
    endpoint_id = (endpoint_id or "").strip()
    key = (api_key or "").strip()
    if not endpoint_id:
        return
    with _lock:
        if len(key) < 6:
            _drop_owner(endpoint_id)
            return
        host = host_of(base_url)
        if not host[1]:
            return
        for old, owner in list(_owner_of.items()):
            if owner == endpoint_id and old != key:
                _hosts.pop(old, None)
                _owner_of.pop(old, None)
        owner = _owner_of.get(key)
        if owner is None or owner == endpoint_id:
            _hosts[key] = host
            _owner_of[key] = endpoint_id


def forget(endpoint_id: str) -> None:
    """Drop the pin when the connection is removed."""
    endpoint_id = (endpoint_id or "").strip()
    if not endpoint_id:
        return
    with _lock:
        _drop_owner(endpoint_id)


def authorize(base_url: str, api_key: str | None, *, content_type: str | None = "application/json") -> dict:
    """Headers for one request. A pinned key is not attached to a different host."""
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    key = (api_key or "").strip()
    if not key:
        return headers
    host = host_of(base_url)
    with _lock:
        pinned = _hosts.get(key)
    if pinned is not None and pinned != host:
        raise ProviderKeyError("Refusing to send a key to a different provider.")
    headers["Authorization"] = f"Bearer {key}"
    return headers


def refused(detail: str) -> bool:
    return "refusing to send a key" in (detail or "").lower()


def failover(detail: str) -> bool:
    """True for a rate limit, a gateway error, or a dropped connection. A 400 is not."""
    if refused(detail):
        return False
    text = (detail or "").strip()
    match = _STATUS.match(text)
    if match:
        return int(match.group(1)) in _FAILOVER_STATUS
    from easyagent import retry

    return retry.retryable_before_token(text)


def scrub(text: str, keys: list[str]) -> str:
    """Remove known keys from an error. The next provider never sees that text."""
    out = text or ""
    for key in sorted({item for item in keys if item and len(item) >= 6}, key=len, reverse=True):
        if key in out:
            out = out.replace(key, "[redacted]")
    from easyagent.journal import redact_leaks

    return redact_leaks(out)


def secret_values(rows: list[dict]) -> list[str]:
    found: list[str] = []
    for row in rows:
        key = str(row.get("api_key") or "").strip()
        if len(key) >= 6 and key not in found:
            found.append(key)
    with _lock:
        for key in _hosts:
            if len(key) >= 6 and key not in found:
                found.append(key)
    return found


def _row(base_url, api_key, model) -> dict:
    return {"base_url": str(base_url or ""), "api_key": str(api_key or ""), "model": model}


def bind_chain(rows: list[dict]):
    """This turn tries these connections, in order, each with its own key."""
    cleaned = tuple(
        _row(row.get("base_url"), row.get("api_key"), row.get("model"))
        for row in rows
        if str(row.get("base_url") or "").strip()
    )
    return _chain.set(cleaned)


def reset_chain(token) -> None:
    _chain.reset(token)


def current_chain() -> list[dict] | None:
    rows = _chain.get()
    if not rows:
        return None
    return [dict(row) for row in rows]


def chain_for(store, endpoint: dict) -> list[dict]:
    """The primary connection, then each saved fallback, hydrated with its own key."""
    primary = _row(endpoint.get("base_url"), endpoint.get("api_key"), endpoint.get("model"))
    rows = [primary]
    seen = {str(endpoint.get("id") or "")}
    for raw in list(endpoint.get("fallback_ids") or [])[:4]:
        fid = str(raw or "").strip().lower()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        try:
            other = store.get_endpoint(fid)
        except Exception:
            other = None
        if not other:
            continue
        rows.append(_row(other.get("base_url"), other.get("api_key"), other.get("model")))
    return rows


def activation(base_url: str, api_key: str | None, model, fallbacks) -> list[dict]:
    """The connections this call may try. Fallback keys stay on their own rows."""
    if fallbacks is not None:
        rows = [_row(base_url, api_key, model)]
        for item in list(fallbacks)[:4]:
            if not isinstance(item, dict):
                continue
            rows.append(_row(item.get("base_url"), item.get("api_key"), item.get("model") or model))
        return rows
    current = current_chain()
    if current and host_of(current[0]["base_url"]) == host_of(base_url):
        rows = [dict(item) for item in current]
        rows[0]["base_url"] = base_url
        rows[0]["api_key"] = str(api_key or "")
        rows[0]["model"] = model
        return rows
    return [_row(base_url, api_key, model)]
