"""OpenAI-compatible chat completions. An endpoint is a base URL."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

import httpx

from easyagent import gate
from easyagent import retry

# Idle notes and the rolling summary run at night. A local 27B often needs
# minutes, not a few seconds, before the first token.
IDLE_MODEL_TIMEOUT = 180.0
from easyagent import turn as turn_mod


class ProviderError(Exception):
    """The endpoint did not return a usable chat completion."""


class YieldLater(Exception):
    """A background model call stepped aside so a chat can use the connection."""


_LLAMA_CACHE: dict[str, bool] = {}
_LLAMA_LOCK = threading.Lock()
_THINK_BLOCK = re.compile(r"<think\b[^>]*>[\s\S]*?</think>", re.IGNORECASE)
_PLAIN_JSON = (
    "Reply with one JSON object and no other text. "
    "Do not call a tool. Do not run a shell command. Do not use markdown fences."
)


def _llama_key(base_url: str) -> str:
    return (base_url or "").rstrip("/").lower()


def remember_llama(base_url: str, found: bool) -> None:
    with _LLAMA_LOCK:
        _LLAMA_CACHE[_llama_key(base_url)] = bool(found)


def cached_llama(base_url: str) -> bool | None:
    with _LLAMA_LOCK:
        key = _llama_key(base_url)
        if key in _LLAMA_CACHE:
            return _LLAMA_CACHE[key]
    return None


def clear_llama_cache() -> None:
    with _LLAMA_LOCK:
        _LLAMA_CACHE.clear()


def grammar_rejected(detail: str) -> bool:
    """llama.cpp answers 400 when a JSON schema is combined with the tool list."""
    lowered = (detail or "").lower()
    return "failed to parse grammar" in lowered or ("grammar" in lowered and "400" in lowered)


def _server_root(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


async def server_is_llama(base_url: str, api_key: str | None = None) -> bool:
    """True when /props looks like llama.cpp. A later grammar 400 can still mark it."""
    cached = cached_llama(base_url)
    if cached is not None:
        return cached
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = _server_root(base_url).rstrip("/") + "/props"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
            response = await client.get(url, headers=headers)
        if response.status_code < 400:
            data = response.json()
            found = isinstance(data, dict) and any(
                key in data for key in ("default_generation_settings", "total_slots", "model_path")
            )
            remember_llama(base_url, found)
            return found
    except Exception:
        pass
    remember_llama(base_url, False)
    return False


def extract_json_text(text: str) -> str:
    """The JSON object in a reply. Think blocks and code fences are not part of it."""
    raw = _THINK_BLOCK.sub("", text or "")
    raw = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return ""
    return raw[start : end + 1].strip()


def _plain_json_messages(messages: list[dict]) -> list[dict]:
    copied = [dict(item) for item in messages]
    if copied and copied[0].get("role") == "system":
        content = str(copied[0].get("content") or "")
        if _PLAIN_JSON not in content:
            copied[0]["content"] = content.rstrip() + "\n" + _PLAIN_JSON
        return copied
    return [{"role": "system", "content": _PLAIN_JSON}, *copied]


def connection_error_text(detail: str, endpoint: dict | None, model: str | None = None, bot_name: str = "") -> str:
    """Name the connection and the exact address this request used.

    The address comes from the connection saved on the bot at the time of the
    request. Nothing here falls back to an older or default address.
    """
    text = (detail or "").strip() or "The model server did not answer."
    if not endpoint:
        return text
    url = (endpoint.get("base_url") or "").strip() or "(no address saved)"
    name = (endpoint.get("name") or "").strip() or "unnamed"
    who = f"{bot_name} used" if bot_name else "This request used"
    shown_model = model or "the server's default model"
    return (
        f"{text}\n{who} the connection \u201c{name}\u201d at {url} (model {shown_model}). "
        "If that address is old, change the connection in Settings. The next message uses the saved connection."
    )


def _call_args(call: dict) -> dict:
    raw = call.get("arguments") if isinstance(call, dict) else None
    if raw is None and isinstance(call, dict):
        raw = call.get("parameters") or {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _string_arg(args: dict, *keys: str) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_STRING = {"type": "string"}

# Sent on every completion. A model that only writes a fence or a tag still works;
# the fence parser remains the fallback.
NATIVE_TOOLS = [
    _tool("list_dir", "List a folder on this computer.", {"path": _STRING}, ["path"]),
    _tool("read_file", "Read a file on this computer.", {"path": _STRING}, ["path"]),
    _tool(
        "write_file",
        "Write a file on this computer. The path is the folder and the filename. The content is the file's text.",
        {"path": _STRING, "content": _STRING},
        ["path", "content"],
    ),
    _tool("terminal", "Run a command on this computer.", {"command": _STRING}, ["command"]),
    _tool("web_search", "Search the public web from this computer.", {"query": _STRING}, ["query"]),
    _tool(
        "ssh",
        "Run a command on a saved Linux computer.",
        {"computer": _STRING, "command": _STRING},
        ["computer", "command"],
    ),
    _tool(
        "windows",
        "Run a command on a saved Windows computer.",
        {"computer": _STRING, "command": _STRING},
        ["computer", "command"],
    ),
    _tool(
        "project",
        "List a project or read one of its files.",
        {"action": _STRING, "project": _STRING, "file": _STRING},
        ["action", "project"],
    ),
    _tool(
        "memory",
        "Read a memory topic, or file, move, or point at a line.",
        {"action": _STRING, "topic": _STRING, "line": _STRING, "other": _STRING},
        ["action"],
    ),
    _tool(
        "history",
        "Read or search your own saved chats and memory files. action is list, search, read, or memory. "
        "query is the words for search; chat is this, a chat id, or part of a title for read.",
        {"action": _STRING, "query": _STRING, "chat": _STRING},
        ["action"],
    ),
    _tool(
        "question",
        "Ask the person to pick one of the choices.",
        {"question": _STRING, "choices": {"type": "array", "items": _STRING}},
        ["question", "choices"],
    ),
    _tool(
        "finish",
        "End the turn. status is proven, unproven, or blocked. A sentence is not a finish.",
        {"status": _STRING, "note": _STRING},
        ["status"],
    ),
    _tool(
        "react",
        "React to one of the person's messages. emoji is 👍, 👎, ❤️, or 👀. message_id is their message id.",
        {"emoji": _STRING, "message_id": _STRING},
        ["emoji", "message_id"],
    ),
]


def _fence_for_call(call: dict) -> str:
    """One native tool call, as a fence. Empty when this computer cannot run it."""
    name = str(call.get("name") or "").strip().lower().replace("-", "_")
    args = _call_args(call)
    path = _string_arg(args, "path", "file", "target_file", "filename")
    body = args.get("content")
    if body is None:
        body = args.get("text")
    if body is None:
        body = args.get("body") or ""
    if not isinstance(body, str):
        body = str(body)
    command = _string_arg(args, "command", "cmd")
    query = _string_arg(args, "query", "q")
    action = str(args.get("action") or "").strip().lower()
    if name in {"write", "write_file", "save_file", "create_file"} or (
        name in {"files", "file"} and action in {"", "write"}
    ):
        if path and body.strip():
            return f"```files\nwrite\n{path}\n{body.rstrip()}\n```"
    if name in {"read", "read_file", "cat"} or (name in {"files", "file"} and action == "read"):
        if path:
            return f"```files\nread\n{path}\n```"
    if name in {"list", "list_dir", "list_directory", "ls"} or (
        name in {"files", "file"} and action in {"list", "ls"}
    ):
        if path:
            return f"```files\nlist\n{path}\n```"
    if name in {"shell", "terminal", "bash", "execute", "execute_command", "run_command", "exec"}:
        if command:
            return f"```shell\n{command}\n```"
    if name in {"web_search", "search", "duckduckgo"} and query:
        return f"```search\n{query}\n```"
    computer = _string_arg(args, "computer", "host")
    if name in {"ssh", "run_ssh"} and computer and command:
        return f"```ssh\n{computer}\n{command}\n```"
    if name in {"windows", "run_windows"} and computer and command:
        return f"```windows\n{computer}\n{command}\n```"
    project = _string_arg(args, "project", "name")
    filename = _string_arg(args, "file", "filename")
    if name in {"project", "project_file"}:
        if action in {"read", "cat"} and project and filename:
            return f"```project\nread\n{project}\n{filename}\n```"
        if project and action in {"", "list", "ls"}:
            return f"```project\nlist\n{project}\n```"
    topic = _string_arg(args, "topic")
    line = _string_arg(args, "line", "text")
    other = _string_arg(args, "other", "other_topic")
    if name in {"memory", "memory_topic"} and action in {"read", "file", "new", "move", "also"}:
        rows = [action]
        if action == "read":
            if topic:
                rows.append(topic)
        elif action in {"file", "new"}:
            if topic:
                rows.append(topic)
            if line:
                rows.append(line)
        elif action == "move":
            if line:
                rows.append(line)
            if topic:
                rows.append(topic)
            if other:
                rows.append(other)
        elif action == "also":
            if line:
                rows.append(line)
            if other or topic:
                rows.append(other or topic)
        return "```memory\n" + "\n".join(rows) + "\n```"
    if name in {"history", "chat_history", "recall", "search_history"}:
        act = action or ("search" if query else "list")
        if name == "search_history":
            act = "search"
        target = query or _string_arg(args, "chat", "chat_id", "title", "words")
        if act in {"search", "find", "grep"} and not target:
            return ""
        rows = [act] + ([target] if target else [])
        return "```history\n" + "\n".join(rows) + "\n```"
    question = _string_arg(args, "question", "prompt")
    choices = args.get("choices") if isinstance(args.get("choices"), list) else args.get("options")
    if name in {"question", "ask"} and question and isinstance(choices, list):
        labels = [str(item).strip() for item in choices if str(item).strip()]
        if len(labels) >= 2:
            return "```question\n" + "\n".join([question, *labels]) + "\n```"
    if name in {"react", "reaction", "tapback"}:
        emoji = _string_arg(args, "emoji", "reaction")
        mid = _string_arg(args, "message_id", "message", "id")
        if emoji and mid:
            return f"```react\n{emoji}\n{mid}\n```"
    if name == "finish":
        status = _string_arg(args, "status", "action").lower() or "proven"
        if status not in {"proven", "unproven", "blocked"}:
            status = "proven"
        note = _string_arg(args, "note", "reason", "content")
        return f"```finish\n{status}\n{note}\n```"
    return ""


def calls_to_fence(calls: list[dict]) -> str:
    """Every native tool call, in order. The first call is not the only one."""
    parts = []
    for call in calls:
        fence = _fence_for_call(call)
        if fence:
            parts.append(fence)
    return "\n".join(parts)


def calls_to_fences(calls: list[dict]) -> str:
    """Every native tool call, in order."""
    return calls_to_fence(calls)


_TERMINAL_WINDOWS = (
    "Run a PowerShell command on this computer. Prefer Invoke-RestMethod for HTTP, "
    "and ConvertTo-Json or ConvertFrom-Json for JSON. For anything multi-line, write a temporary "
    ".py or .ps1 script and run that file. Do not use python -c or curl one-liners."
)


def native_tools() -> list[dict]:
    """Tool list for this computer. On Windows the shell tool is PowerShell."""
    if sys.platform != "win32":
        return NATIVE_TOOLS
    tools = []
    for tool in NATIVE_TOOLS:
        if tool.get("function", {}).get("name") == "terminal":
            tools.append({**tool, "function": {**tool["function"], "description": _TERMINAL_WINDOWS}})
        else:
            tools.append(tool)
    return tools


def _completion_payload(
    messages: list[dict],
    model: str | None,
    *,
    stream: bool,
    response_schema: dict | None = None,
    grammar: str | None = None,
    tools: bool = True,
) -> dict:
    """A schema or a grammar never rides along with the tool list. llama.cpp rejects that pair."""
    payload: dict = {"messages": messages}
    if tools and not response_schema and not grammar:
        payload["tools"] = native_tools()
    if stream:
        payload["stream"] = True
    if model:
        payload["model"] = model
    if response_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "easyagent_json", "strict": True, "schema": response_schema},
        }
    if grammar:
        payload["grammar"] = grammar
    return payload


def _collect_calls(bucket: dict[int, dict], choice: dict) -> None:
    source = None
    if isinstance(choice.get("delta"), dict):
        source = choice["delta"].get("tool_calls")
    if source is None and isinstance(choice.get("message"), dict):
        source = choice["message"].get("tool_calls")
    if not isinstance(source, list):
        return
    for call in source:
        if not isinstance(call, dict):
            continue
        try:
            index = int(call.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        slot = bucket.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call.get("id"):
            slot["id"] = str(call["id"])
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        if function.get("name"):
            slot["name"] += str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["arguments"] += arguments
        elif isinstance(arguments, dict):
            slot["arguments"] = json.dumps(arguments)


_reasoning_sink: ContextVar[Callable[[str], None] | None] = ContextVar("easyagent_reasoning_sink", default=None)
_native_calls: ContextVar[list[dict] | None] = ContextVar("easyagent_native_calls", default=None)


def take_native_calls() -> list[dict]:
    """The tool calls from the model reply that just finished, then clear them.

    Each item is id, name, and the arguments string. The next request has to
    send those ids back on the tool messages.
    """
    calls = _native_calls.get()
    _native_calls.set(None)
    return list(calls or [])


def restore_native_calls(calls: list[dict]) -> None:
    """Put tool calls onto this task.

    The stream reader runs in its own task, and a ContextVar set there does
    not come back with the text. The ids have to be restored on the loop's task
    or the next request invents call_0 and llama.cpp drops the turn.
    """
    _native_calls.set(list(calls or []))


def _store_native_calls(bucket: dict[int, dict]) -> None:
    calls: list[dict] = []
    for index in sorted(bucket):
        slot = bucket[index]
        name = str(slot.get("name") or "").strip()
        if not name:
            continue
        cid = str(slot.get("id") or "").strip() or f"call_{index}"
        arguments = slot.get("arguments") or ""
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        calls.append({"id": cid, "name": name, "arguments": arguments})
    _native_calls.set(calls)

_OPENER = re.compile(r"(?is)<think(?:ing)?>|```think[ \t]*\r?\n")
_OPEN_MARKERS = ("<think>", "<thinking>", "```think\n", "```think\r\n")


def attach_reasoning_sink(sink: Callable[[str], None]):
    """Receive reasoning while a completion runs. The answer string stays separate."""
    return _reasoning_sink.set(sink)


def detach_reasoning_sink(token) -> None:
    _reasoning_sink.reset(token)


def note_reasoning(text: str) -> None:
    if not text:
        return
    sink = _reasoning_sink.get()
    if sink is not None:
        sink(text)


def _suffix_hold(text: str, markers: tuple[str, ...]) -> int:
    """How many trailing characters could still grow into a marker."""
    if not text or not markers:
        return 0
    lower = text.lower()
    limit = min(len(text), max(len(marker) for marker in markers))
    hold = 0
    for size in range(1, limit + 1):
        tail = lower[-size:]
        if any(marker.startswith(tail) for marker in markers):
            hold = size
    return hold


class _ThinkSplitter:
    """Split answer text from a think block without waiting for the whole reply."""

    def __init__(self) -> None:
        self.buf = ""
        self.in_think = False
        self.closer = "</think>"

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        if not chunk:
            return []
        self.buf += chunk
        out: list[tuple[str, str]] = []
        while True:
            if not self.in_think:
                match = _OPENER.search(self.buf)
                if not match:
                    hold = _suffix_hold(self.buf, _OPEN_MARKERS)
                    emit = self.buf[:-hold] if hold else self.buf
                    self.buf = self.buf[-hold:] if hold else ""
                    if emit:
                        out.append(("text", emit))
                    break
                if match.start():
                    out.append(("text", self.buf[: match.start()]))
                token = match.group(0).lower()
                self.buf = self.buf[match.end() :]
                if token.startswith("```"):
                    self.closer = "```"
                elif token.startswith("<thinking"):
                    self.closer = "</thinking>"
                else:
                    self.closer = "</think>"
                self.in_think = True
                continue
            lowered = self.buf.lower()
            idx = lowered.find(self.closer)
            if idx >= 0 and self.closer == "</think>" and lowered.startswith("</thinking>", idx):
                idx = lowered.find("</thinking>")
                closer = "</thinking>"
            else:
                closer = self.closer
            if idx < 0:
                hold = _suffix_hold(self.buf, (self.closer, "</thinking>"))
                emit = self.buf[:-hold] if hold else self.buf
                self.buf = self.buf[-hold:] if hold else ""
                if emit:
                    out.append(("reasoning", emit))
                break
            if idx:
                out.append(("reasoning", self.buf[:idx]))
            self.buf = self.buf[idx + len(closer) :]
            self.in_think = False
        return out

    def finish(self) -> list[tuple[str, str]]:
        if not self.buf:
            return []
        kind = "reasoning" if self.in_think else "text"
        piece = self.buf
        self.buf = ""
        self.in_think = False
        return [(kind, piece)]


def peel_thinking(text: str) -> tuple[str, str]:
    """Return (reasoning, answer). The answer does not contain the think block."""
    splitter = _ThinkSplitter()
    parts = splitter.feed(text or "")
    parts.extend(splitter.finish())
    reasoning = "".join(piece for kind, piece in parts if kind == "reasoning").strip()
    answer = "".join(piece for kind, piece in parts if kind == "text")
    answer = re.sub(r"\n{3,}", "\n\n", answer).strip()
    return reasoning, answer


def _reasoning_field(source: dict) -> str:
    """A separate reasoning channel. A think block in the answer is the other one."""
    if not isinstance(source, dict):
        return ""
    for key in ("reasoning_content", "reasoning"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    details = source.get("reasoning_details")
    if isinstance(details, str) and details:
        return details
    if isinstance(details, list):
        chunks: list[str] = []
        for item in details:
            if isinstance(item, str) and item:
                chunks.append(item)
            elif isinstance(item, dict):
                bit = item.get("text") or item.get("content") or ""
                if isinstance(bit, str) and bit:
                    chunks.append(bit)
        return "".join(chunks)
    return ""


def _reasoning_from_choice(choice: dict) -> str:
    if not isinstance(choice, dict):
        return ""
    for source in (choice.get("delta"), choice.get("message"), choice):
        found = _reasoning_field(source if isinstance(source, dict) else {})
        if found:
            return found
    return ""


def _note_split(field: str, hidden: str) -> None:
    if field:
        note_reasoning(field)
        extra = (hidden or "").strip()
        if extra and extra not in field:
            note_reasoning(extra)
    elif hidden:
        note_reasoning(hidden)


def _message_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
        return "".join(parts)
    return str(content)


def _http_timeout(timeout: float, *, stream: bool) -> httpx.Timeout:
    """Connect fails fast. A stream that has started may think quietly. A whole reply still has a limit."""
    return httpx.Timeout(
        connect=retry.CONNECT_TIMEOUT,
        read=None if stream else timeout,
        write=timeout,
        pool=retry.CONNECT_TIMEOUT,
    )


def _raise_transport(exc: BaseException, base_url: str) -> None:
    if isinstance(exc, turn_mod.TurnCancelled):
        raise exc
    if turn_mod.cancelled():
        raise turn_mod.TurnCancelled() from exc
    if isinstance(exc, asyncio.TimeoutError) or isinstance(exc, httpx.TimeoutException):
        raise ProviderError(f"Timed out calling {base_url}") from exc
    if isinstance(exc, httpx.HTTPError):
        raise ProviderError(f"Could not reach {base_url}: {exc}") from exc
    raise exc


async def _watch_for_chat(client: httpx.AsyncClient) -> None:
    """Close this request when a user chat is waiting on the only slot."""
    while True:
        await asyncio.sleep(0.2)
        if gate.user_waiting():
            await client.aclose()
            return


async def _complete_once(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
    timeout: float,
    response_schema: dict | None = None,
    grammar: str | None = None,
    tools: bool = True,
    yield_to_chats: bool = False,
) -> str:
    """One non-streaming completion. The caller decides whether to retry."""
    _native_calls.set(None)
    url = base_url.rstrip("/") + "/chat/completions"
    payload = _completion_payload(
        messages,
        model,
        stream=False,
        response_schema=response_schema,
        grammar=grammar,
        tools=tools,
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client = httpx.AsyncClient(timeout=_http_timeout(timeout, stream=False))
    turn_mod.attach_client(client)
    watcher = asyncio.create_task(_watch_for_chat(client)) if yield_to_chats else None
    try:
        turn_mod.raise_if_cancelled()
        try:
            response = await client.post(url, json=payload, headers=headers)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            if yield_to_chats and gate.user_waiting():
                raise YieldLater() from exc
            _raise_transport(exc, base_url)
    finally:
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
        turn_mod.detach_client(client)
        await client.aclose()
    if response.status_code >= 400:
        detail = " ".join(response.text.split())[:300]
        raise ProviderError(f"{response.status_code} from {base_url}: {detail}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError(f"Endpoint did not return JSON: {response.text[:200]}") from exc
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("Endpoint returned no choices.") from exc
    field = _reasoning_field(message if isinstance(message, dict) else {})
    body = _message_text(message.get("content") if isinstance(message, dict) else None)
    hidden, answer = peel_thinking(body)
    _note_split(field, hidden)
    calls: dict[int, dict] = {}
    if isinstance(message, dict):
        _collect_calls(calls, {"message": message})
    _store_native_calls(calls)
    fence = calls_to_fences(list(calls.values()))
    prose = answer.strip()
    if fence and prose:
        return prose + "\n" + fence
    if fence:
        return fence
    if not prose:
        raise ProviderError("Endpoint returned an empty message.")
    return answer


async def _dispatch_complete(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
    timeout: float,
    response_schema: dict | None,
    grammar: str | None,
    tools: bool,
    yield_to_chats: bool,
) -> str:
    if not gate.inner_retry_allowed():
        try:
            permit = await gate.reserve(step_aside=yield_to_chats)
        except gate.BusyLane as exc:
            raise YieldLater() from exc
        try:
            await permit.acquire()
            return await _complete_once(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                timeout=timeout,
                response_schema=response_schema,
                grammar=grammar,
                tools=tools,
                yield_to_chats=yield_to_chats,
            )
        finally:
            await permit.release()
    return await _complete_window(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        timeout=timeout,
        response_schema=response_schema,
        grammar=grammar,
        tools=tools,
        yield_to_chats=yield_to_chats,
    )


async def _complete_window(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
    timeout: float,
    response_schema: dict | None = None,
    grammar: str | None = None,
    tools: bool = True,
    yield_to_chats: bool = False,
) -> str:
    """Retry a completion that never returned, and free the slot during the pause."""
    window = retry.Window()
    while True:
        try:
            permit = await gate.reserve(step_aside=yield_to_chats)
        except gate.BusyLane as exc:
            raise YieldLater() from exc
        hold = True
        try:
            await permit.acquire()
            try:
                return await _complete_once(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    timeout=timeout,
                    response_schema=response_schema,
                    grammar=grammar,
                    tools=tools,
                    yield_to_chats=yield_to_chats,
                )
            except turn_mod.TurnCancelled:
                raise
            except YieldLater:
                raise
            except ProviderError as exc:
                kind = window.plan(str(exc), started=False)
                if kind is None:
                    message = window.failure_message(str(exc))
                    if message:
                        raise ProviderError(message) from exc
                    raise
                _label, delay = window.arm(kind, str(exc))
                await permit.release()
                hold = False
                await retry.pause(delay)
        finally:
            if hold:
                await permit.release()


async def complete(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
    timeout: float = 120.0,
    response_schema: dict | None = None,
    grammar: str | None = None,
    tools: bool | None = None,
    yield_to_chats: bool = False,
) -> str:
    """One completion. Structured and internal calls omit tools. llama.cpp gets plain JSON."""
    structured = bool(response_schema or grammar)
    send_tools = False if tools is False or structured else True
    schema = response_schema
    gram = grammar
    outgoing = messages
    if structured and await server_is_llama(base_url, api_key):
        schema = None
        gram = None
        send_tools = False
        outgoing = _plain_json_messages(messages)
    try:
        return await _dispatch_complete(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=outgoing,
            timeout=timeout,
            response_schema=schema,
            grammar=gram,
            tools=send_tools,
            yield_to_chats=yield_to_chats,
        )
    except ProviderError as exc:
        if structured and grammar_rejected(str(exc)):
            remember_llama(base_url, True)
            return await _dispatch_complete(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=_plain_json_messages(messages),
                timeout=timeout,
                response_schema=None,
                grammar=None,
                tools=False,
                yield_to_chats=yield_to_chats,
            )
        raise


def _choice_text(choice: dict) -> str:
    if not isinstance(choice, dict):
        return ""
    delta = choice.get("delta")
    if isinstance(delta, dict):
        text = _message_text(delta.get("content"))
        if text:
            return text
    message = choice.get("message")
    if isinstance(message, dict):
        return _message_text(message.get("content"))
    return ""


def _answer_pieces(splitter: _ThinkSplitter, chunk: str, *, saw_field: bool) -> list[str]:
    """Answer fragments. A think block is noted, and it is not one of them."""
    pieces: list[str] = []
    for kind, piece in splitter.feed(chunk):
        if kind == "reasoning":
            if not saw_field and piece:
                note_reasoning(piece)
        elif piece:
            pieces.append(piece)
    return pieces


_raw_lock = threading.Lock()
_RAW_LINES = 6
_RAW_BYTES = 48_000


def _debug_raw_path() -> Path | None:
    """Where a raw model chunk goes. Only when EASYAGENT_DEBUG_RAW=1."""
    if os.environ.get("EASYAGENT_DEBUG_RAW", "").strip() != "1":
        return None
    env = os.environ.get("EASYAGENT_DATA", "").strip()
    root = Path(env) if env else Path.cwd() / "data"
    return root / "debug-raw.txt"


def _remember_raw(payload: str) -> None:
    """Keep a raw chunk so a live model can be inspected. Nothing is invented."""
    path = _debug_raw_path()
    if path is None:
        return
    text = (payload or "").strip()
    if not text:
        return
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    with _raw_lock:
        try:
            if path.is_file() and path.stat().st_size >= _RAW_BYTES:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError:
            return


def _mark_raw_call() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _remember_raw(f"--- {stamp}")


def _finish_pieces(splitter: _ThinkSplitter, *, saw_field: bool) -> list[str]:
    pieces: list[str] = []
    for kind, piece in splitter.finish():
        if kind == "reasoning":
            if not saw_field and piece:
                note_reasoning(piece)
        elif piece:
            pieces.append(piece)
    return pieces


async def _iter_completion_body(response: httpx.Response) -> AsyncIterator[str]:
    _native_calls.set(None)
    ctype = (response.headers.get("content-type") or "").lower()
    calls: dict[int, dict] = {}
    held: list[str] = []
    splitter = _ThinkSplitter()
    saw_field = False
    if _debug_raw_path() is not None:
        _mark_raw_call()

    def take_field(choice: dict) -> None:
        nonlocal saw_field
        found = _reasoning_from_choice(choice)
        if not found:
            return
        saw_field = True
        note_reasoning(found)

    if "application/json" in ctype and "text/event-stream" not in ctype:
        try:
            raw = (await response.aread()).decode("utf-8", "replace")
            _remember_raw(raw)
            data = json.loads(raw)
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("Endpoint returned no choices.") from exc
        if isinstance(choice, dict):
            take_field(choice)
            _collect_calls(calls, choice)
        text = _choice_text(choice if isinstance(choice, dict) else {})
        if text:
            held.extend(_answer_pieces(splitter, text, saw_field=saw_field))
        held.extend(_finish_pieces(splitter, saw_field=saw_field))
        _store_native_calls(calls)
        fence = calls_to_fences(list(calls.values()))
        prose = "".join(held).strip()
        if fence and prose:
            yield prose + "\n" + fence
            return
        if fence:
            yield fence
            return
        if prose:
            yield prose
        return
    raw_kept = 0
    async for line in response.aiter_lines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line.split(":", 1)[1].strip()
        if data == "[DONE]":
            break
        if raw_kept < _RAW_LINES:
            _remember_raw(data)
            raw_kept += 1
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        try:
            choice = obj["choices"][0]
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(choice, dict):
            take_field(choice)
            _collect_calls(calls, choice)
        text = _choice_text(choice if isinstance(choice, dict) else {})
        if not text:
            continue
        pieces = _answer_pieces(splitter, text, saw_field=saw_field)
        if calls:
            held.extend(pieces)
        else:
            for piece in pieces:
                yield piece
    for piece in _finish_pieces(splitter, saw_field=saw_field):
        if calls:
            held.append(piece)
        else:
            yield piece
    _store_native_calls(calls)
    fence = calls_to_fences(list(calls.values()))
    prose = "".join(held).strip()
    if fence and prose:
        yield prose + "\n"
    if fence:
        yield fence
        return
    if held:
        yield "".join(held)


async def _stream_once(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
    timeout: float,
) -> AsyncIterator[str]:
    """One streaming attempt. A pause after the headers is not a failure. No headers in time is."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = _completion_payload(messages, model, stream=True)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client = httpx.AsyncClient(timeout=_http_timeout(timeout, stream=True))
    turn_mod.attach_client(client)
    yielded = False
    opened = None
    try:
        turn_mod.raise_if_cancelled()
        opened = client.stream("POST", url, json=payload, headers=headers)
        try:
            response = await asyncio.wait_for(opened.__aenter__(), timeout)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            _raise_transport(exc, base_url)
        try:
            if response.status_code >= 400:
                detail = " ".join((await response.aread()).decode("utf-8", "replace").split())[:300]
                raise ProviderError(f"{response.status_code} from {base_url}: {detail}")
            async for piece in _iter_completion_body(response):
                turn_mod.raise_if_cancelled()
                if piece:
                    yielded = True
                    yield piece
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            _raise_transport(exc, base_url)
        finally:
            await opened.__aexit__(None, None, None)
            opened = None
    finally:
        turn_mod.detach_client(client)
        await client.aclose()
    if not yielded:
        raise ProviderError("Endpoint returned an empty message.")


async def _stream_window(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
    timeout: float,
) -> AsyncIterator[str]:
    """Retry until the first token, then let a later drop surface to the caller."""
    window = retry.Window()
    while True:
        permit = await gate.reserve()
        hold = True
        started = False
        try:
            await permit.acquire()
            try:
                async for piece in _stream_once(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    timeout=timeout,
                ):
                    started = True
                    yield piece
                return
            except turn_mod.TurnCancelled:
                raise
            except ProviderError as exc:
                if started or window.plan(str(exc), started=False) is None:
                    message = None if started else window.failure_message(str(exc))
                    if message:
                        raise ProviderError(message) from exc
                    raise
                _label, delay = window.arm("again", str(exc))
                await permit.release()
                hold = False
                await retry.pause(delay)
        finally:
            if hold:
                await permit.release()


async def stream_complete(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
    timeout: float = 120.0,
) -> AsyncIterator[str]:
    """Yield the reply as it arrives. Waiting on an open socket is not a failure.

    A caller that already retries passes one attempt through. On its own, this
    keeps trying until the first token or the retry window runs out.
    """
    producer = _stream_window if gate.inner_retry_allowed() else _stream_once
    async for piece in producer(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        timeout=timeout,
    ):
        yield piece
