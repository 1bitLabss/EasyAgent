"""Find model servers already running on this computer.

Nothing here runs at boot. The first-run wizard calls it when it opens.
All probes run in parallel under one hard budget: a server that answers
late is skipped, not an error, and a closed port costs one refused socket.
The dialect parsing follows what ``llm.py`` and ``nctx.py`` already speak:
Ollama answers ``GET /api/tags``, llama.cpp answers ``GET /props`` (the
fingerprint keys are the same ones ``llm.server_is_llama`` checks), and
anything OpenAI-compatible answers ``GET /v1/models``.
"""

from __future__ import annotations

import asyncio

import httpx

from easyagent import llm, retry

BUDGET_SECONDS = 1.5
PROBE_TIMEOUT = 1.2

# The same fingerprint keys llm.server_is_llama (llm.py) checks on /props.
_LLAMA_PROPS_KEYS = ("default_generation_settings", "total_slots", "model_path")

# (server root, dialects to try in order). localhost only: the probe runs on
# the server host, which is where a local model server lives.
CANDIDATES: list[tuple[str, tuple[str, ...]]] = [
    ("http://127.0.0.1:11434", ("ollama", "openai")),
    ("http://127.0.0.1:8080", ("llama", "openai")),
    ("http://127.0.0.1:8000", ("llama", "openai")),
    ("http://127.0.0.1:1234", ("openai", "llama")),
]


def _port_of(url: str) -> str:
    try:
        return str(url.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return "?"


def _openai_ids(data) -> list[str] | None:
    """Model ids from an OpenAI-style ``{"data": [{"id": ...}]}`` body. None = not that."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    return [
        str(row.get("id"))
        for row in data["data"]
        if isinstance(row, dict) and row.get("id")
    ]


def _ollama_names(data) -> list[str] | None:
    """Model names from Ollama's ``{"models": [{"name": ...}]}`` body. None = not that."""
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    names = []
    for row in data["models"]:
        if isinstance(row, dict):
            name = str(row.get("name") or row.get("model") or "").strip()
            if name:
                names.append(name)
    return names


def _llama_fingerprint(data) -> str | None:
    """The model path from a llama.cpp ``/props`` body. None = not llama.cpp."""
    if not isinstance(data, dict):
        return None
    if not any(key in data for key in _LLAMA_PROPS_KEYS):
        return None
    return str(data.get("model_path") or "").strip() or None


async def _get_json(client, url: str, headers: dict | None = None):
    """Parsed JSON body, or None for any miss. A miss is never an error here."""
    try:
        response = await client.get(url, headers=headers or {})
        if response.status_code >= 400:
            return None
        return response.json()
    except Exception:
        return None


async def _try_dialect(client, root: str, dialect: str, headers: dict | None = None):
    """(kind, models) when this root answers this dialect. None when it does not."""
    if dialect == "ollama":
        names = _ollama_names(await _get_json(client, root + "/api/tags", headers))
        if names is not None:
            return ("ollama", names)
        return None
    if dialect == "llama":
        model_path = _llama_fingerprint(await _get_json(client, root + "/props", headers))
        if model_path is None:
            return None
        ids = _openai_ids(await _get_json(client, root + "/v1/models", headers)) or []
        if not ids and model_path:
            ids = [model_path.replace("\\", "/").rsplit("/", 1)[-1]]
        return ("llama.cpp", ids)
    ids = _openai_ids(await _get_json(client, root + "/v1/models", headers))
    if ids is not None:
        return ("openai", ids)
    return None


def _row(kind: str, root: str, models: list[str]) -> dict:
    port = _port_of(root)
    if kind == "ollama":
        label = "Ollama"
    elif kind == "llama.cpp":
        label = f"llama.cpp :{port}"
    elif port == "1234":
        kind, label = "lmstudio", "LM Studio"
    else:
        label = f"OpenAI-compatible :{port}"
    return {
        "kind": kind,
        "base_url": root.rstrip("/") + "/v1",
        "label": label,
        "models": models,
    }


async def _probe_candidate(root: str, dialects: tuple[str, ...]) -> dict | None:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(PROBE_TIMEOUT, connect=0.8)
        ) as client:
            for dialect in dialects:
                found = await _try_dialect(client, root, dialect)
                if found is not None:
                    return _row(found[0], root, found[1])
    except (httpx.HTTPError, OSError):
        return None
    return None


async def detect_local_servers(budget: float = BUDGET_SECONDS) -> list[dict]:
    """Probe the known local ports in parallel. Hard total budget, silent misses."""
    tasks = [
        asyncio.create_task(_probe_candidate(root, dialects))
        for root, dialects in CANDIDATES
    ]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=budget)
    for task in pending:
        task.cancel()
    rows: list[dict] = []
    for task in done:
        if task.cancelled():
            continue
        try:
            row = task.result()
        except Exception:
            continue
        if row is not None:
            rows.append(row)
    order = {root: index for index, (root, _) in enumerate(CANDIDATES)}
    rows.sort(key=lambda row: order.get(str(row.get("base_url", "")).rsplit("/v1", 1)[0], 99))
    return rows


async def probe_address(base_url: str, api_key: str | None = None, timeout: float = 8.0) -> dict:
    """Ask one arbitrary address if it is a model server. Never raises.

    Tries the OpenAI ``/v1/models`` shape first (the address usually already
    ends in ``/v1``), then Ollama and llama.cpp at the server root.
    """
    url = (base_url or "").strip().rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "detail": "The address must start with http:// or https://."}
    try:
        headers = llm._auth_headers(url, api_key)
    except Exception as exc:
        return {"ok": False, "detail": str(exc) or "That key was refused for this address."}
    root = llm._server_root(url)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for dialect in ("openai", "ollama", "llama"):
                found = await _try_dialect(client, root, dialect, headers)
                if found is not None:
                    return {
                        "ok": True,
                        "detail": "The server answered.",
                        "models": found[1],
                    }
            try:
                answered = await client.get(root + "/v1/models", headers=headers)
                status = answered.status_code
            except (httpx.HTTPError, OSError):
                return {"ok": False, "detail": retry.unreachable_text(url)}
            if status != 200:
                return {"ok": False, "detail": f"The server answered with {status}."}
    except (httpx.HTTPError, OSError):
        return {"ok": False, "detail": retry.unreachable_text(url)}
    return {"ok": False, "detail": "The server answered, but not like a model server."}
