"""Tools the bot can actually run.

A printed function call is not an answer. EasyAgent runs the request, shows
a short line that it happened, and asks the model for the reply. The raw
listing, file bytes, and command output stay out of the chat.
"""

from __future__ import annotations

import ast
import asyncio
import html
import json
import re
import shutil
import struct
import subprocess
import sys
import zlib
from datetime import date

import httpx
from dataclasses import dataclass, replace
from pathlib import Path

from easyagent import gate
from easyagent import llm
from easyagent import retry
from easyagent import search as search_mod
from easyagent import turn as turn_mod
from easyagent.paths import default_deliverable_dir, deliverable_file, display_path
from easyagent.loop import (
    BREAKER_PROMPT,
    Ledger,
    arg_key,
    inject_working,
    is_bare_not_written,
    is_quick,
    judge_breaker,
    memory_overlap,
    near_arg,
    near_keys,
    needs_stages,
    norm_arg,
    only_the_user_knows,
    page_matches,
    page_subject,
    paths_match,
    research_query,
    summary_job,
    turn_opening,
    _norm_path,
)
from easyagent.search import SearchError
from easyagent.selfinfo import run_history
from easyagent.store import Store, StoreError, atomic_write_text, canonical_emoji
from easyagent.vault import VaultError, open_vault

_OUTPUT_CAP = 8000
_READ_CAP = 12000
_WRITE_CAP = 100_000
_SCAN_CAP = 4000
_MODEL_NAMES = 24
_MODEL_CHARS = 1200
_COMMAND_TIMEOUT = 30

_FILES_FENCE = re.compile(r"```files[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_SHELL_FENCE = re.compile(r"```shell[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_SSH_FENCE = re.compile(r"```ssh[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_WINDOWS_FENCE = re.compile(r"```windows[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_QUESTION_FENCE = re.compile(r"```question[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_PROJECT_FENCE = re.compile(r"```project[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_MEMORY_FENCE = re.compile(r"```memory[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_FINISH_FENCE = re.compile(r"```finish[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_REACT_FENCE = re.compile(r"```react[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_HISTORY_FENCE = re.compile(r"```history[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_HISTORY_ACTIONS = {"list", "ls", "chats", "search", "find", "grep", "read", "open", "chat", "memory", "notes"}
_FUNCTION_BLOCK = re.compile(r"<function_calls>\s*.*?(?:</function_calls>|$)", re.DOTALL | re.IGNORECASE)
_INVOKE = re.compile(r"<invoke\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</invoke>", re.DOTALL | re.IGNORECASE)
_PARAM = re.compile(r"<parameter\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</parameter>", re.DOTALL | re.IGNORECASE)

_IGNORED_PATHS = {"the path", "path"}
_IGNORED_COMMANDS = {"the command", "command"}
_IGNORED_COMPUTERS = {"the computer", "computer"}
_IGNORED_PROJECTS = {"the project", "project"}
_IGNORED_PROJECT_FILES = {"the file", "file"}
_IGNORED_TOPICS = {"the topic", "topic", "the other topic"}
_IGNORED_MEMORY_LINES = {"the new line", "the line", "the new fact"}
_MEMORY_ACTIONS = {"read", "file", "new", "move", "also"}


def _note_heuristic(name: str, why: str) -> None:
    """One line when a guess runs. These stay until a day of real use shows they never fire."""
    print(f"heuristic {name}: {why}", flush=True)


class ToolError(Exception):
    """The action did not run. The chat keeps the error."""


@dataclass(frozen=True)
class ToolRequest:
    kind: str
    action: str = ""
    path: str = ""
    command: str = ""
    computer: str = ""
    body: str = ""
    choices: tuple[str, ...] = ()
    call_id: str = ""
    call_name: str = ""
    call_arguments: str = ""


@dataclass(frozen=True)
class Settled:
    """Visible reply text, and choices when the bot asked a question."""

    text: str
    choices: tuple[str, ...] = ()
    made: str = ""
    thinking: str = ""
    reacted: bool = False


_PNG_MARK = "[[easyagent-png]]"
_PREVIEW_CHARS = 240
_PAGE_SUFFIXES = (".html", ".htm")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _strip_memory_tools(text: str) -> str:
    """Drop a memory tool fence. Leave a one-line fact fence for the harness to file."""

    def replacer(match: re.Match[str]) -> str:
        first = ((match.group(1) or "").strip().splitlines() or [""])[0].strip().lower()
        if first in _MEMORY_ACTIONS:
            return ""
        return match.group(0)

    return _MEMORY_FENCE.sub(replacer, text or "")


_TOOL_CALL_BLOCK = re.compile(r"<tool_call>(.*?)</tool_call>", re.IGNORECASE | re.DOTALL)
_QWEN_FUNCTION = re.compile(r"<function=([^>\s]+)>(.*?)</function>", re.IGNORECASE | re.DOTALL)
_QWEN_PARAM = re.compile(r"<parameter=([^>\s]+)>(.*?)</parameter>", re.IGNORECASE | re.DOTALL)


def strip_tool_markup(text: str) -> str:
    """Remove tool fences and printed function calls. Leave ordinary prose."""
    cleaned = _TOOL_CALL_BLOCK.sub("", text or "")
    cleaned = re.sub(r"</tool_call>", "", cleaned, flags=re.IGNORECASE)
    cleaned = _FUNCTION_BLOCK.sub("", cleaned)
    cleaned = _INVOKE.sub("", cleaned)
    for pattern in (
        _FILES_FENCE,
        _SHELL_FENCE,
        _SSH_FENCE,
        _WINDOWS_FENCE,
        _QUESTION_FENCE,
        _PROJECT_FENCE,
        _HISTORY_FENCE,
        _FINISH_FENCE,
        _REACT_FENCE,
        _PLAN_FENCE,
    ):
        cleaned = pattern.sub("", cleaned)
    cleaned = _strip_memory_tools(cleaned)
    cleaned = re.sub(r"```plan[ \t]*\r?\n.*?```", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&amp;", "&")
    )


def _params(body: str) -> dict[str, str]:
    found = {}
    for match in _PARAM.finditer(body or ""):
        found[match.group(1).strip().lower()] = _unescape(match.group(2).strip())
    return found


def _param_lists(body: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for match in _PARAM.finditer(body or ""):
        found.setdefault(match.group(1).strip().lower(), []).append(_unescape(match.group(2).strip()))
    return found


def _choice_items(values: list[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        if text.startswith("["):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str) and item.strip():
                        items.append(item.strip())
                    elif isinstance(item, dict):
                        label = item.get("label") or item.get("value") or item.get("name")
                        if isinstance(label, str) and label.strip():
                            items.append(label.strip())
                continue
        if "\n" in text:
            items.extend(line.strip() for line in text.splitlines() if line.strip())
        else:
            items.append(text)
    return items


def _ignored_path(path: str) -> bool:
    return " ".join((path or "").split()).lower() in _IGNORED_PATHS


def _ignored_command(command: str) -> bool:
    return " ".join((command or "").split()).lower() in _IGNORED_COMMANDS


def _ignored_computer(name: str) -> bool:
    return " ".join((name or "").split()).lower() in _IGNORED_COMPUTERS


def _ignored_project(name: str) -> bool:
    return " ".join((name or "").split()).lower() in _IGNORED_PROJECTS


def _ignored_project_file(name: str) -> bool:
    return " ".join((name or "").split()).lower() in _IGNORED_PROJECT_FILES


def _ignored_topic(name: str) -> bool:
    return " ".join((name or "").split()).lower() in _IGNORED_TOPICS


def _ignored_memory_line(text: str) -> bool:
    return " ".join((text or "").split()).lower() in _IGNORED_MEMORY_LINES


def _memory_tool(body: str) -> ToolRequest | None:
    """A memory tool fence. A one-line fact is not a tool."""
    raw_lines = (body or "").splitlines()
    if not raw_lines:
        return None
    action = raw_lines[0].strip().lower()
    if action not in _MEMORY_ACTIONS:
        return None
    rest = [line.strip() for line in raw_lines[1:] if line.strip()]
    if action == "read":
        topic = rest[0] if rest else ""
        if topic and _ignored_topic(topic):
            return None
        return ToolRequest(kind="memory", action="read", path=topic)
    if action in {"file", "new"}:
        topic = rest[0] if rest else ""
        text = " ".join(rest[1:]) if len(rest) > 1 else ""
        if _ignored_topic(topic) or _ignored_memory_line(text):
            return None
        if not topic or not text:
            raise ToolError("Name the topic and the line.")
        return ToolRequest(kind="memory", action=action, path=topic, body=text)
    if action == "move":
        if len(rest) < 3 or _ignored_memory_line(rest[0]) or _ignored_topic(rest[1]) or _ignored_topic(rest[2]):
            if rest and (_ignored_memory_line(rest[0]) or _ignored_topic(rest[-1])):
                return None
            raise ToolError("Name the line, the topic it is in, and the topic it should move to.")
        return ToolRequest(kind="memory", action="move", body=rest[0], command=rest[1], path=rest[2])
    if len(rest) < 2 or _ignored_memory_line(rest[0]) or _ignored_topic(rest[1]):
        if rest and (_ignored_memory_line(rest[0]) or _ignored_topic(rest[-1])):
            return None
        raise ToolError("Name the line and the other topic.")
    return ToolRequest(kind="memory", action="also", body=rest[0], path=rest[1])


def _from_invoke(name: str, body: str) -> ToolRequest | None:
    params = _params(body)
    kind = (name or "").strip().lower()
    action = (params.get("action") or params.get("op") or "").strip().lower()
    path = params.get("path") or params.get("file") or ""
    command = params.get("command") or params.get("cmd") or ""
    computer = params.get("computer") or params.get("host") or params.get("name") or ""
    content = params.get("content") or params.get("text") or params.get("body") or ""
    if kind in {"computer", "files", "file", "filesystem"} and action in {"list", "read", "write", "ls", "cat"}:
        if action == "ls":
            action = "list"
        if action == "cat":
            action = "read"
        if _ignored_path(path):
            return None
        return ToolRequest(kind="files", action=action, path=path, body=content)
    if kind in {"shell", "bash", "terminal", "powershell", "cmd"} or (kind == "computer" and action in {"shell", "run"}):
        if _ignored_command(command):
            return None
        return ToolRequest(kind="shell", command=command)
    if kind in {"ssh", "linux"}:
        if _ignored_computer(computer) or _ignored_command(command):
            return None
        return ToolRequest(kind="ssh", computer=computer, command=command)
    if kind in {"windows", "winrm"}:
        if _ignored_computer(computer) or _ignored_command(command):
            return None
        return ToolRequest(kind="windows", computer=computer, command=command)
    if kind in {"project", "knowledge"}:
        project = params.get("project") or ""
        filename = params.get("file") or params.get("filename") or ""
        if not action:
            action = "read" if filename else "list"
        if action in {"ls", "list"}:
            if project and _ignored_project(project):
                return None
            return ToolRequest(kind="project", action="list", path=project)
        if action in {"cat", "read"}:
            if _ignored_project(project) or _ignored_project_file(filename):
                return None
            if not project.strip() or not filename.strip():
                raise ToolError("Name the project and the file.")
            return ToolRequest(kind="project", action="read", path=project.strip(), command=filename.strip())
        raise ToolError("A project can be listed or read.")
    if kind in {"memory", "topic"}:
        topic = params.get("topic") or ""
        text = content
        if action in {"", "read"}:
            if topic and _ignored_topic(topic):
                return None
            return ToolRequest(kind="memory", action="read", path=topic)
        if action in {"file", "new"}:
            if _ignored_topic(topic) or _ignored_memory_line(text):
                return None
            if not topic.strip() or not text.strip():
                raise ToolError("Name the topic and the line.")
            return ToolRequest(kind="memory", action=action, path=topic.strip(), body=text.strip())
        source = params.get("from") or params.get("source") or ""
        dest = params.get("to") or params.get("dest") or topic
        if action == "move":
            if _ignored_memory_line(text) or _ignored_topic(source) or _ignored_topic(dest):
                return None
            if not text.strip() or not source.strip() or not dest.strip():
                raise ToolError("Name the line, the topic it is in, and the topic it should move to.")
            return ToolRequest(kind="memory", action="move", body=text.strip(), command=source.strip(), path=dest.strip())
        if action == "also":
            if _ignored_memory_line(text) or _ignored_topic(dest):
                return None
            if not text.strip() or not dest.strip():
                raise ToolError("Name the line and the other topic.")
            return ToolRequest(kind="memory", action="also", body=text.strip(), path=dest.strip())
        raise ToolError("A memory topic can be read, filed, moved, or pointed at another topic.")
    if kind in {"history", "chat_history", "recall"}:
        argument = params.get("query") or params.get("chat") or params.get("words") or ""
        return _history_request(action or ("search" if argument else "list"), argument)
    if kind in {"clarify", "question"}:
        lists = _param_lists(body)
        prompt = (lists.get("question") or lists.get("prompt") or [""])[0]
        raw_choices = lists.get("choices") or lists.get("options") or lists.get("choice") or []
        choices = _choice_items(raw_choices)
        if not prompt.strip() or len(choices) < 2:
            raise ToolError("A question needs a short prompt and at least two choices.")
        return ToolRequest(kind="question", body=prompt.strip(), choices=tuple(choices))
    raise ToolError(
        "That printed tool call was not run. EasyAgent can list, read, and write files on this computer, "
        "list and read a project file, read or file a memory topic, run a command here, search the web, "
        "and run a command on a Linux or Windows computer you saved."
    )


def _fence_request(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text or "")
    if not match:
        return None
    return match.group(1).strip("\n")


def _load_call(raw: str) -> dict | None:
    """A JSON or literal tool object. Anything else is not a call."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?[ \t]*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    data = None
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = ast.literal_eval(text)
        except (ValueError, SyntaxError, MemoryError, TypeError):
            return None
    if not isinstance(data, dict) or not str(data.get("name") or "").strip():
        return None
    if "arguments" not in data and "parameters" not in data and "args" not in data:
        return None
    return data


def _qwen_call(body: str) -> dict | None:
    match = _QWEN_FUNCTION.search(body or "")
    if not match:
        return None
    arguments = {key.strip(): value.strip() for key, value in _QWEN_PARAM.findall(match.group(2))}
    return {"name": match.group(1).strip(), "arguments": arguments}


def _request_from_call(data: dict) -> ToolRequest:
    """One tagged or native call, as a tool this computer can run."""
    name = str(data.get("name") or "").strip().lower().replace("-", "_")
    raw_args = data.get("arguments")
    if raw_args is None:
        raw_args = data.get("parameters")
    if raw_args is None:
        raw_args = data.get("args") or {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except ValueError:
            raw_args = {}
    if not isinstance(raw_args, dict):
        raw_args = {}
    path = str(
        raw_args.get("path") or raw_args.get("file") or raw_args.get("target_file") or raw_args.get("filename") or ""
    ).strip()
    content = raw_args.get("content")
    if content is None:
        content = raw_args.get("text")
    if content is None:
        content = raw_args.get("body") or ""
    if not isinstance(content, str):
        content = str(content)
    command = str(raw_args.get("command") or raw_args.get("cmd") or "").strip()
    query = str(raw_args.get("query") or raw_args.get("q") or "").strip()
    action = str(raw_args.get("action") or "").strip().lower()
    if name in {"react", "reaction", "tapback"}:
        emoji = canonical_emoji(str(raw_args.get("emoji") or raw_args.get("reaction") or ""))
        mid = str(raw_args.get("message_id") or raw_args.get("message") or raw_args.get("id") or "").strip()
        if not emoji or not mid:
            raise ToolError("A reaction needs an emoji and the person's message id.")
        return ToolRequest(kind="react", path=mid, body=emoji)
    if name == "finish":
        status = str(raw_args.get("status") or action or "proven").strip().lower()
        if status not in {"proven", "unproven", "blocked"}:
            status = "proven"
        note = str(raw_args.get("note") or raw_args.get("reason") or content or "").strip()
        return ToolRequest(kind="finish", action=status, body=note)
    if name in {"write", "write_file", "save_file", "create_file"} or (name in {"files", "file"} and action in {"", "write"}):
        if not path or not content.strip():
            raise ToolError("Name the file and the text. The file was not written.")
        return ToolRequest(kind="files", action="write", path=path, body=content)
    if name in {"read", "read_file", "cat"} or (name in {"files", "file"} and action == "read"):
        if not path:
            raise ToolError("Name a path on this computer.")
        return ToolRequest(kind="files", action="read", path=path)
    if name in {"list", "list_dir", "list_directory", "ls"} or (name in {"files", "file"} and action in {"list", "ls"}):
        if not path:
            raise ToolError("Name a path on this computer.")
        return ToolRequest(kind="files", action="list", path=path)
    if name in {"shell", "terminal", "bash", "execute", "execute_command", "run_command", "exec"}:
        if not command:
            raise ToolError("There was no command to run.")
        return ToolRequest(kind="shell", command=command)
    if name in {"web_search", "search", "duckduckgo"}:
        if not query:
            raise ToolError("Search failed: the query was empty.")
        return ToolRequest(kind="search", body=query)
    computer = str(raw_args.get("computer") or raw_args.get("host") or "").strip()
    if name in {"ssh", "run_ssh", "windows", "run_windows"}:
        if not command:
            raise ToolError("There was no command to run.")
        kind = "ssh" if name in {"ssh", "run_ssh"} else "windows"
        return ToolRequest(kind=kind, computer=computer, command=command)
    raise ToolError(
        "That printed tool call was not run. EasyAgent can list, read, and write files on this computer, "
        "list and read a project file, read or file a memory topic, run a command here, search the web, "
        "and run a command on a Linux or Windows computer you saved."
    )


def _tagged_tool(text: str) -> ToolRequest | None:
    """A tool call written as a tag or a JSON object, not as a native tool object."""
    blocks = _TOOL_CALL_BLOCK.findall(text or "")
    for block in blocks:
        data = _load_call(block) or _qwen_call(block)
        if data is not None:
            return _request_from_call(data)
    if blocks:
        raise ToolError(
            "That printed tool call was not run. EasyAgent can list, read, and write files on this computer, "
            "list and read a project file, read or file a memory topic, run a command here, search the web, "
            "and run a command on a Linux or Windows computer you saved."
        )
    tail = re.search(r"(\{.*\})\s*</tool_call>", text or "", re.DOTALL)
    if tail:
        data = _load_call(tail.group(1))
        if data is not None:
            return _request_from_call(data)
    stripped = (text or "").strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        data = _load_call(stripped)
        if data is not None:
            return _request_from_call(data)
    return None


def _cut_off_tool(text: str) -> bool:
    """An opened tool tag with no close is not a finished reply."""
    raw = text or ""
    lowered = raw.lower()
    for open_tag, close_tag in (
        ("<tool_call", "</tool_call>"),
        ("<function_calls", "</function_calls>"),
        ("<invoke", "</invoke>"),
    ):
        if lowered.count(open_tag) > lowered.count(close_tag):
            return True
    if re.search(r"```(?:files|shell|search|ssh|windows|project|memory|history|finish|react)\b", raw, re.IGNORECASE):
        if raw.count("```") % 2 == 1:
            return True
    return False


def parse_tool(text: str) -> ToolRequest | None:
    """The first real tool request in a reply, or None when there is nothing to run."""
    if _TOOL_CALL_BLOCK.search(text or "") or (text or "").strip().startswith("{") or "</tool_call>" in (text or "").lower():
        tagged = _tagged_tool(text or "")
        if tagged is not None:
            return tagged
    invoke = _INVOKE.search(text or "")
    if invoke:
        request = _from_invoke(invoke.group(1), invoke.group(2))
        if request is not None:
            return request
    files = _fence_request(_FILES_FENCE, text)
    if files is not None:
        lines = files.splitlines()
        action = (lines[0] if lines else "").strip().lower()
        path = lines[1].strip() if len(lines) > 1 else ""
        body = "\n".join(lines[2:]) if len(lines) > 2 else ""
        if action in {"list", "read", "write"} and not _ignored_path(path):
            return ToolRequest(kind="files", action=action, path=path, body=body)
    shell = _fence_request(_SHELL_FENCE, text)
    if shell is not None and not _ignored_command(shell.strip()):
        return ToolRequest(kind="shell", command=shell.strip())
    ssh = _fence_request(_SSH_FENCE, text)
    if ssh is not None:
        lines = ssh.splitlines()
        computer = lines[0].strip() if lines else ""
        command = "\n".join(lines[1:]).strip()
        if not _ignored_computer(computer) and not _ignored_command(command):
            return ToolRequest(kind="ssh", computer=computer, command=command)
    windows = _fence_request(_WINDOWS_FENCE, text)
    if windows is not None:
        lines = windows.splitlines()
        computer = lines[0].strip() if lines else ""
        command = "\n".join(lines[1:]).strip()
        if not _ignored_computer(computer) and not _ignored_command(command):
            return ToolRequest(kind="windows", computer=computer, command=command)
    project = _fence_request(_PROJECT_FENCE, text)
    if project is not None:
        lines = [line.strip() for line in project.splitlines() if line.strip()]
        action = (lines[0] if lines else "").lower()
        name = lines[1] if len(lines) > 1 else ""
        filename = lines[2] if len(lines) > 2 else ""
        if action in {"list", "ls"} and not (name and _ignored_project(name)):
            return ToolRequest(kind="project", action="list", path=name)
        if action in {"read", "cat"} and not _ignored_project(name) and not _ignored_project_file(filename):
            if not name or not filename:
                raise ToolError("Name the project and the file.")
            return ToolRequest(kind="project", action="read", path=name, command=filename)
    memory = _fence_request(_MEMORY_FENCE, text)
    if memory is not None:
        request = _memory_tool(memory)
        if request is not None:
            return request
    history = _fence_request(_HISTORY_FENCE, text)
    if history is not None:
        request = _history_from_body(history)
        if request is not None:
            return request
    question = _fence_request(_QUESTION_FENCE, text)
    if question is not None:
        lines = [line.strip() for line in question.splitlines() if line.strip()]
        if len(lines) < 3:
            raise ToolError("A question needs a short prompt and at least two choices.")
        return ToolRequest(kind="question", body=lines[0], choices=tuple(lines[1:]))
    if _FUNCTION_BLOCK.search(text or "") or _INVOKE.search(text or ""):
        raise ToolError(
            "That printed tool call was not run. EasyAgent can list, read, and write files on this computer, "
            "list and read a project file, read or file a memory topic, run a command here, search the web, "
            "and run a command on a Linux or Windows computer you saved."
        )
    return None


def _files_from_body(body: str) -> ToolRequest | None:
    lines = (body or "").splitlines()
    action = (lines[0] if lines else "").strip().lower()
    path = lines[1].strip() if len(lines) > 1 else ""
    content = "\n".join(lines[2:]) if len(lines) > 2 else ""
    if action in {"list", "read", "write"} and not _ignored_path(path):
        return ToolRequest(kind="files", action=action, path=path, body=content)
    return None


def _shell_from_body(body: str) -> ToolRequest | None:
    command = (body or "").strip()
    if not command or _ignored_command(command):
        return None
    return ToolRequest(kind="shell", command=command)


def _remote_from_body(kind: str, body: str) -> ToolRequest | None:
    lines = (body or "").splitlines()
    computer = lines[0].strip() if lines else ""
    command = "\n".join(lines[1:]).strip()
    if _ignored_computer(computer) or _ignored_command(command):
        return None
    return ToolRequest(kind=kind, computer=computer, command=command)


def _project_from_body(body: str) -> ToolRequest | None:
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    action = (lines[0] if lines else "").lower()
    name = lines[1] if len(lines) > 1 else ""
    filename = lines[2] if len(lines) > 2 else ""
    if action in {"list", "ls"} and not (name and _ignored_project(name)):
        return ToolRequest(kind="project", action="list", path=name)
    if action in {"read", "cat"} and not _ignored_project(name) and not _ignored_project_file(filename):
        if not name or not filename:
            raise ToolError("Name the project and the file.")
        return ToolRequest(kind="project", action="read", path=name, command=filename)
    return None


def _history_request(action: str, argument: str) -> ToolRequest | None:
    act = (action or "").strip().lower()
    if act not in _HISTORY_ACTIONS:
        return None
    arg = " ".join((argument or "").split())
    if arg.lower() in {"the words", "words", "the chat", "query"}:
        arg = ""
    if act in {"search", "find", "grep"} and not arg:
        return None
    return ToolRequest(kind="history", action=act, path=arg)


def _history_from_body(body: str) -> ToolRequest | None:
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if not lines:
        return None
    return _history_request(lines[0], " ".join(lines[1:]))


def _question_from_body(body: str) -> ToolRequest | None:
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) < 3:
        raise ToolError("A question needs a short prompt and at least two choices.")
    return ToolRequest(kind="question", body=lines[0], choices=tuple(lines[1:]))


def _search_from_body(body: str) -> ToolRequest | None:
    query = " ".join((body or "").split())
    if not query or query.lower() in {"the query", "query"}:
        return None
    return ToolRequest(kind="search", body=query[:200])


def _react_from_body(body: str) -> ToolRequest | None:
    """One tapback. The sample id in the prompt is not a message."""
    emoji = ""
    mid = ""
    for line in (body or "").splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        if not emoji and canonical_emoji(cleaned):
            emoji = canonical_emoji(cleaned)
            continue
        if not mid:
            mid = cleaned.split()[0]
    if not emoji or not mid:
        return None
    if mid.lower() in {"the", "message", "id"} or mid.lower() in {"the message id", "message id"}:
        return None
    if mid.lower().startswith("the ") or "message id" in mid.lower():
        return None
    return ToolRequest(kind="react", path=mid, body=emoji)


def _finish_from_body(body: str) -> ToolRequest | None:
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if not lines:
        return ToolRequest(kind="finish", action="proven", body="")
    status = lines[0].lower()
    if status in {"proven", "unproven", "blocked"}:
        note = "\n".join(lines[1:]).strip()
    else:
        status = "blocked"
        note = "\n".join(lines).strip()
    return ToolRequest(kind="finish", action=status, body=note)


def parse_tools(text: str) -> list[ToolRequest]:
    """Every real tool request in a reply, in the order they were written."""
    raw = text or ""
    found: list[tuple[int, ToolRequest]] = []
    errors: list[ToolError] = []
    blocks = list(_TOOL_CALL_BLOCK.finditer(raw))
    parsed_block = False
    for match in blocks:
        data = _load_call(match.group(1)) or _qwen_call(match.group(1))
        if data is None:
            continue
        try:
            found.append((match.start(), _request_from_call(data)))
            parsed_block = True
        except ToolError as exc:
            errors.append(exc)
    if blocks and not parsed_block:
        raise errors[0] if errors else ToolError(
            "That printed tool call was not run. EasyAgent can list, read, and write files on this computer, "
            "list and read a project file, read or file a memory topic, run a command here, search the web, "
            "and run a command on a Linux or Windows computer you saved."
        )
    for match in _INVOKE.finditer(raw):
        try:
            request = _from_invoke(match.group(1), match.group(2))
        except ToolError as exc:
            errors.append(exc)
            continue
        if request is not None:
            found.append((match.start(), request))

    def take(pattern: re.Pattern[str], builder) -> None:
        for match in pattern.finditer(raw):
            try:
                request = builder(match.group(1).strip("\n"))
            except ToolError as exc:
                errors.append(exc)
                continue
            if request is not None:
                found.append((match.start(), request))

    take(_FILES_FENCE, _files_from_body)
    take(_SHELL_FENCE, _shell_from_body)
    take(_SSH_FENCE, lambda body: _remote_from_body("ssh", body))
    take(_WINDOWS_FENCE, lambda body: _remote_from_body("windows", body))
    take(_PROJECT_FENCE, _project_from_body)
    take(_MEMORY_FENCE, _memory_tool)
    take(_HISTORY_FENCE, _history_from_body)
    take(_QUESTION_FENCE, _question_from_body)
    take(_FINISH_FENCE, _finish_from_body)
    take(_REACT_FENCE, _react_from_body)
    take(search_mod.FENCE_RE, _search_from_body)
    if found:
        found.sort(key=lambda item: item[0])
        return [item for _start, item in found]
    if errors:
        raise errors[0]
    stripped = raw.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        data = _load_call(stripped)
        if data is not None:
            return [_request_from_call(data)]
    tail = re.search(r"(\{.*\})\s*</tool_call>", raw, re.DOTALL | re.IGNORECASE)
    if tail:
        data = _load_call(tail.group(1))
        if data is not None:
            return [_request_from_call(data)]
    if _FUNCTION_BLOCK.search(raw) or _INVOKE.search(raw):
        raise ToolError(
            "That printed tool call was not run. EasyAgent can list, read, and write files on this computer, "
            "list and read a project file, read or file a memory topic, run a command here, search the web, "
            "and run a command on a Linux or Windows computer you saved."
        )
    return []


def _step_excerpt(command: str, limit: int = 72) -> str:
    text = " ".join((command or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def status_label(request: ToolRequest) -> str:
    if request.kind == "question":
        return "Asking"
    if request.kind in {"files", "project", "history"} or (request.kind == "memory" and request.action == "read"):
        return "Looking"
    if request.kind == "memory":
        return "Remembering"
    if request.kind == "shell":
        excerpt = _step_excerpt(request.command)
        if sys.platform == "win32":
            return f"Running PowerShell: {excerpt}" if excerpt else "Running PowerShell"
        return f"Running: {excerpt}" if excerpt else "Running"
    if request.kind == "windows":
        excerpt = _step_excerpt(request.command)
        return f"Running PowerShell: {excerpt}" if excerpt else "Running PowerShell"
    return "Connecting"


def secret_strings(store: Store) -> list[str]:
    """Sign-in values that must not be written into a chat or another plain file."""
    found: list[str] = []
    try:
        computers = store.list_computers()
    except Exception:
        return found
    for computer in computers:
        vault = computer.get("vault") if isinstance(computer, dict) else None
        if not isinstance(vault, dict):
            continue
        try:
            opened = open_vault(vault)
        except VaultError:
            continue
        secret = opened.get("secret") or ""
        user = opened.get("user") or ""
        if isinstance(secret, str) and secret and secret not in found:
            found.append(secret)
        if isinstance(user, str) and len(user) >= 4 and user not in found:
            found.append(user)
    return found


def redact(store: Store, text: str) -> str:
    return _scrub(text or "", *secret_strings(store))


def contains_secret(store: Store, data: bytes) -> bool:
    for secret in secret_strings(store):
        if secret.encode("utf-8") in data:
            return True
    return False


def _ask(store: Store, request: ToolRequest) -> Settled:
    hidden = secret_strings(store)
    prompt = " ".join(_scrub(request.body, *hidden).split())
    if len(prompt) > 240:
        prompt = prompt[:237].rstrip() + "…"
    choices: list[str] = []
    for choice in request.choices:
        cleaned = " ".join(_scrub(choice, *hidden).split())
        if cleaned and cleaned not in choices and len(cleaned) <= 120:
            choices.append(cleaned)
        if len(choices) == 6:
            break
    if not prompt or len(choices) < 2:
        raise ToolError("That question was not shown.")
    return Settled(prompt, tuple(choices))


def _clip(text: str) -> str:
    body = text or ""
    if len(body) <= _OUTPUT_CAP:
        return body
    return body[: _OUTPUT_CAP - 20].rstrip() + "\n[output truncated]"


def _scrub(text: str, *hidden: str) -> str:
    cleaned = text or ""
    for secret in hidden:
        if secret:
            cleaned = cleaned.replace(secret, "")
    return cleaned


def _placed_file(raw: str) -> Path:
    """The file named by a path. A drive path stays a drive path until this computer can use it."""
    return Path((raw or "").strip().strip('"')).expanduser()


def _user_path(store: Store, raw: str) -> Path:
    text = (raw or "").strip().strip('"')
    if not text or _ignored_path(text):
        raise ToolError("Name a path on this computer.")
    path = _placed_file(text)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise ToolError(f"Could not use that path. {exc}") from exc
    root = store.root.resolve()
    if resolved == root or root in resolved.parents:
        raise ToolError("That path is EasyAgent's saved chats. It was not changed.")
    return resolved


def _list_path(path: Path) -> str:
    if not path.exists():
        raise ToolError(f"Could not list {path}: that folder is not there.")
    if not path.is_dir():
        raise ToolError(f"{path} is a file. Ask to read it.")
    names: list[str] = []
    extra = 0
    try:
        children = sorted(path.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        raise ToolError(f"Could not list {path}. {exc}") from exc
    for child in children:
        if child.is_symlink():
            label = child.name
        elif child.is_dir():
            label = child.name + "/"
        else:
            label = child.name
        if len(names) < _SCAN_CAP:
            names.append(label)
        else:
            extra += 1
    listing = "\n".join(names) if names else "(empty)"
    if extra:
        listing += f"\n({extra} more names were not scanned)"
    return f"Listed {path}\n{listing}"


def _measure_largest(path: Path) -> str:
    """One filename and its size. The other names stay out of the prompt."""
    if not path.exists() or not path.is_dir():
        return f"Could not measure files in {path}."
    best_name = ""
    best_size = -1
    count = 0
    try:
        children = sorted(path.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        return f"Could not measure files in {path}. {exc}"
    for child in children:
        if child.is_symlink() or not child.is_file():
            continue
        try:
            size = child.stat().st_size
        except OSError:
            continue
        count += 1
        if size > best_size:
            best_size = size
            best_name = child.name
    if not best_name:
        return f"No files in {path}."
    return f"The largest file in {path} is {best_name}, {best_size} bytes. {count} files were measured."


def _read_path(path: Path) -> str:
    if not path.exists():
        raise ToolError(f"Could not read {path}: that file is not there.")
    if path.is_dir():
        raise ToolError(f"{path} is a folder. Ask to list it.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ToolError(f"Could not read {path}. {exc}") from exc
    if size > 200_000:
        raise ToolError(f"{path} is too large to read into the chat.")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"Could not read {path}. {exc}") from exc
    if len(text) > _READ_CAP:
        text = text[: _READ_CAP - 20].rstrip() + "\n[file truncated]"
    return text


def _drive_path_unusable(raw: str) -> bool:
    """A Windows drive path that this computer cannot address is not a successful write."""
    text = (raw or "").strip().strip('"')
    if not re.match(r"^[A-Za-z]:[\\/]", text):
        return False
    return not _placed_file(text).is_absolute()


def _app_roots() -> list[Path]:
    roots: list[Path] = []
    for folder in (Path(__file__).resolve().parents[1], Path.cwd()):
        if folder not in roots:
            roots.append(folder)
    return roots


def _inside_app_folder(raw: str) -> bool:
    """A relative path, or a path inside EasyAgent, is the app folder."""
    text = (raw or "").strip().strip('"')
    if not text:
        return True
    if re.match(r"^[A-Za-z]:[\\/]", text):
        folded = text.replace("/", "\\").rstrip("\\").lower()
        for root in _app_roots():
            root_folded = str(root).replace("/", "\\").rstrip("\\").lower()
            if folded == root_folded or folded.startswith(root_folded + "\\"):
                return True
        return False
    if text.startswith("/"):
        try:
            resolved = Path(text).resolve()
        except OSError:
            return False
        for root in _app_roots():
            folder = root.resolve()
            if resolved == folder or folder in resolved.parents:
                return True
        return False
    return True


def _file_is_present(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _draw_png() -> bytes:
    """A small night picture. There is no separate picture tool."""
    width, height = 48, 36
    sky = (12, 18, 36)
    stone = (70, 78, 92)
    light = (255, 214, 90)
    rows: list[bytes] = []
    for y in range(height):
        pixel: list[int] = []
        for x in range(width):
            color = sky
            span = max(1, (height - y) // 6)
            if abs(x - (width // 2)) <= span and y > 6:
                color = stone
            if abs(x - (width // 2)) <= 1 and y > 8 and y % 4 == 0:
                color = light
            if (x < 8 or x > width - 9) and y > height // 2:
                color = (28, 34, 48)
                if y % 5 == 0 and x % 3 == 0:
                    color = light
            pixel.extend(color)
        rows.append(b"\x00" + bytes(pixel))
    raw = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def _landing_html(subject: str = "") -> str:
    """A page he can open. A hotel page comes from the model, not from this text."""
    title = f"{subject[:1].upper()}{subject[1:]} landing page" if subject else "Sample landing page"
    blurb = f"A short page for a {subject}." if subject else "A short page you can open in a browser."
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
</head>
<body>
<h1>{title}</h1>
<p>{blurb}</p>
<p>It has a heading, one plain description, and nothing else to install.</p>
"""
    page += "<p>" + ("A plain sentence for the page. " * 24) + "</p>\n"
    page += "<p>END-OF-PAGE-MARKER</p>\n</body>\n</html>\n"
    return page


def _file_parent_in(raw: str) -> str:
    """A parent we would create whose name is a file, not a folder."""
    text = (raw or "").strip().strip('"').rstrip("\\/")
    if not text:
        return ""
    sep = "\\" if "\\" in text else "/"
    pieces = text.split("\\") if sep == "\\" else text.split("/")
    for index, part in enumerate(pieces[:-1]):
        if not part or re.fullmatch(r"[A-Za-z]:", part):
            continue
        # .local and .config are folders. A name like page.html is a file.
        if part.startswith(".") and part.count(".") == 1:
            continue
        if re.search(r"(?i)\.[A-Za-z][A-Za-z0-9]{1,7}$", part):
            return sep.join(pieces[: index + 1])
    return ""


def _fileish_name(name: str) -> bool:
    return bool(re.search(r"(?i)\.[A-Za-z][A-Za-z0-9]{1,7}$", name or ""))


def _clear_file_posed_as_folder(path: Path) -> None:
    """A path named as a file is written as a file, even if a folder already sits there."""
    if path.is_symlink() or not path.is_dir():
        return
    if not _fileish_name(path.name):
        return
    _note_heuristic("_clear_file_posed_as_folder", f"{path} was a folder and the request named it as a file")
    shutil.rmtree(path)


def _write_path(path: Path, body: str) -> str:
    blocked = _file_parent_in(str(path))
    if blocked:
        raise ToolError(f"The file was not written. {blocked} is a file path, not a folder.")
    _clear_file_posed_as_folder(path)
    if path.exists() and path.is_dir():
        raise ToolError(f"The file was not written. {path} is a folder.")
    if len(body) > _WRITE_CAP:
        raise ToolError(f"The file was not written. {path} is too long to write.")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"The file was not written. {path}: {exc}") from exc
    if not _file_is_present(path):
        raise ToolError(f"The file was not written. {path} is not on disk.")
    return f"Wrote {len(body)} characters to {path}"


def _write_bytes(path: Path, data: bytes) -> str:
    blocked = _file_parent_in(str(path))
    if blocked:
        raise ToolError(f"The file was not written. {blocked} is a file path, not a folder.")
    _clear_file_posed_as_folder(path)
    if path.exists() and path.is_dir():
        raise ToolError(f"The file was not written. {path} is a folder.")
    if not data:
        raise ToolError(f"The file was not written. {path} had nothing to write.")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        raise ToolError(f"The file was not written. {path}: {exc}") from exc
    if not _file_is_present(path):
        raise ToolError(f"The file was not written. {path} is not on disk.")
    return f"Wrote {len(data)} bytes to {path}"


def _run_files(store: Store, request: ToolRequest) -> str:
    path_text = request.path
    body = request.body
    if request.action == "write":
        path_text, body = _clean_write_target(request.path, request.body)
        blocked = _file_parent_in(path_text)
        if blocked:
            raise ToolError(f"The file was not written. {blocked} is a file path, not a folder.")
        if _drive_path_unusable(path_text):
            raise ToolError(f"The file was not written. This computer cannot use {path_text}.")
    path = _user_path(store, path_text)
    if request.action == "list":
        return _list_path(path)
    if request.action == "read":
        return _read_path(path)
    if request.action == "write":
        if body.strip() == _PNG_MARK:
            return _write_bytes(path, _draw_png())
        if not body.strip():
            raise ToolError(f"The file was not written. {path} had nothing to write.")
        return _write_path(path, body)
    raise ToolError("Say whether to list, read, or write that path.")


def _shell_invocation(command: str) -> tuple[object, bool]:
    """(argv, shell). On Windows the command is one PowerShell -Command argument.

    cmd.exe quoting is what breaks python -c and curl one-liners. PowerShell
    receives the script as a single argument, and a long body belongs in a
    .ps1 or .py file the model writes and then runs.
    """
    if sys.platform == "win32":
        return (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], False)
    return (command, True)


def _empty_command(code: int | None) -> str:
    shown = 0 if code is None else code
    return f"command produced no output, exit code {shown}"


def _run_shell(store: Store, command: str) -> str:
    text = (command or "").strip()
    if not text:
        raise ToolError("There was no command to run.")
    if sys.platform == "win32" and _windows_rejects(text):
        raise ToolError(f"{_windows_rejects(text)} is not a Windows command. It was not run.")
    if len(text) > 4000:
        raise ToolError("That command is too long. It was not run.")
    root = str(store.root.resolve())
    if root and root in text:
        raise ToolError("That command mentions EasyAgent's saved chats. It was not run.")
    argv, use_shell = _shell_invocation(text)
    try:
        proc = subprocess.Popen(
            argv,
            shell=use_shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=turn_mod.tool_cwd(),
            env=turn_mod.tool_env(),
            start_new_session=True,
        )
    except OSError as exc:
        raise ToolError(f"Could not run that command. {exc}") from exc
    turn_mod.attach_proc(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=_COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            turn_mod.stop_process(proc)
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            raise ToolError("The command timed out. Nothing else was changed.") from exc
    finally:
        turn_mod.detach_proc(proc)
    if turn_mod.cancelled():
        turn_mod.stop_process(proc)
        raise turn_mod.TurnCancelled()
    output = _clip(((stdout or "") + (stderr or "")).strip())
    if not output:
        output = _empty_command(proc.returncode)
    if proc.returncode != 0:
        raise ToolError(output)
    return output


def _saved_computer(store: Store, name: str, kind: str) -> dict:
    wanted = " ".join((name or "").split())
    if not wanted:
        raise ToolError("Name the computer you saved. Nothing was run.")
    matches = [item for item in store.list_computers() if item.get("name") == wanted]
    if len(matches) != 1:
        raise ToolError(f"No saved computer is named {wanted}. Add it on the page. Nothing was run.")
    computer = matches[0]
    if computer.get("kind") != kind:
        label = "Linux" if kind == "linux" else "Windows"
        raise ToolError(f"{wanted} is not a {label} computer. Nothing was run.")
    if not computer.get("vault"):
        raise ToolError("That computer has no saved sign-in. Nothing was run.")
    try:
        opened = open_vault(computer["vault"])
    except VaultError as exc:
        raise ToolError("The saved sign-in could not be opened. Nothing was run.") from exc
    return {
        "name": computer.get("name"),
        "kind": computer.get("kind"),
        "host": computer.get("host"),
        "port": computer.get("port"),
        "user": opened.get("user") or "",
        "auth": opened.get("auth") or "password",
        "secret": opened.get("secret") or "",
    }


def _load_private_key(text: str):
    import io

    import paramiko

    blob = io.StringIO(text)
    last: Exception | None = None
    for loader in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        blob.seek(0)
        try:
            return loader.from_private_key(blob)
        except Exception as exc:
            last = exc
    raise ToolError("Could not read that key. The command was not run.") from last


def run_ssh(computer: dict, command: str) -> str:
    """Run one command on a saved Linux computer."""
    try:
        import paramiko
    except ImportError as exc:
        raise ToolError("SSH is not available on this computer yet.") from exc

    text = (command or "").strip()
    if not text:
        raise ToolError("There was no command to run.")
    secret = computer.get("secret") or ""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        kwargs = {
            "hostname": computer["host"],
            "port": int(computer.get("port") or 22),
            "username": computer.get("user") or "",
            "timeout": 20,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if computer.get("auth") == "key":
            kwargs["pkey"] = _load_private_key(secret)
        else:
            kwargs["password"] = secret
        client.connect(**kwargs)
        _stdin, stdout, stderr = client.exec_command(text, timeout=_COMMAND_TIMEOUT)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
    except ToolError:
        raise
    except Exception as exc:
        detail = _scrub(str(exc), secret, computer.get("user") or "")
        raise ToolError(f"Could not run that on {computer.get('name')}. {detail}") from exc
    finally:
        client.close()
    output = _clip(_scrub((out + err).strip(), secret, computer.get("user") or ""))
    if code != 0:
        raise ToolError(output or f"The command exited {code}.")
    return output or "(no output)"


def run_windows(computer: dict, command: str) -> str:
    """Run one PowerShell command on a saved Windows computer."""
    try:
        import winrm
    except ImportError as exc:
        raise ToolError("Windows control is not available on this computer yet.") from exc

    text = (command or "").strip()
    if not text:
        raise ToolError("There was no command to run.")
    secret = computer.get("secret") or ""
    host = computer["host"]
    port = int(computer.get("port") or 5985)
    scheme = "https" if port == 5986 else "http"
    target = f"{scheme}://{host}:{port}/wsman"
    try:
        session = winrm.Session(target, auth=(computer.get("user") or "", secret), transport="ntlm")
        result = session.run_ps(text)
    except Exception as exc:
        detail = _scrub(str(exc), secret, computer.get("user") or "")
        raise ToolError(f"Could not run that on {computer.get('name')}. {detail}") from exc
    out = result.std_out.decode("utf-8", "replace") if isinstance(result.std_out, bytes) else (result.std_out or "")
    err = result.std_err.decode("utf-8", "replace") if isinstance(result.std_err, bytes) else (result.std_err or "")
    output = _clip(_scrub((out + err).strip(), secret, computer.get("user") or ""))
    if getattr(result, "status_code", 1) not in (0, None):
        raise ToolError(output or f"The command exited {result.status_code}.")
    return output or "(no output)"


def _project_for_bot(store: Store, bot_id: str, name: str):
    allowed = store.projects_for_bot(bot_id)
    wanted = " ".join((name or "").split()).casefold()
    if not wanted:
        return None
    own = [item for item in allowed if item.get("kind") == "bot" and item.get("name", "").casefold() == wanted]
    shared = [item for item in allowed if item.get("kind") == "group" and item.get("name", "").casefold() == wanted]
    if len(own) == 1:
        return own[0]
    if not own and len(shared) == 1:
        return shared[0]
    if own or shared:
        raise ToolError(f"More than one project is named {name}.")
    raise ToolError(f"{name} is not available to this bot.")


def _run_project(store: Store, request: ToolRequest, bot_id: str | None) -> str:
    if not bot_id:
        raise ToolError("This turn has no bot, so a project was not opened.")
    if request.action == "list" and not (request.path or "").strip():
        names = [item.get("name") or "Project" for item in store.projects_for_bot(bot_id)]
        if not names:
            return "No projects."
        return "Projects\n" + "\n".join(names)
    project = _project_for_bot(store, bot_id, request.path)
    if request.action == "list":
        names = [item["name"] for item in store.list_project_files(project["id"])]
        listing = "\n".join(names) if names else "(empty)"
        return f"Listed {project['name']}\n{listing}"
    if request.action != "read":
        raise ToolError("A project can be listed or read.")
    try:
        meta, data = store.read_project_bytes(project["id"], request.command)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    if contains_secret(store, data):
        raise ToolError("That file was not read.")
    if b"\x00" in data[:1024]:
        return f"{meta['name']} is not text. {meta['size']} bytes."
    text = data.decode("utf-8", "replace").strip()
    return _clip(text or "(empty file)")


def _run_memory(store: Store, request: ToolRequest, bot_id: str | None) -> str:
    if not bot_id:
        raise ToolError("This turn has no bot, so memory was not opened.")
    try:
        if request.action == "read" and not (request.path or "").strip():
            slugs = store.memory_slugs(bot_id)
            if not slugs:
                return "No memory topics."
            return "Memory index\n" + "\n".join(slugs)
        if request.action == "read":
            return store.topic_text(bot_id, request.path)
        if request.action == "file":
            record = store.add_memory(bot_id, request.body, request.path, create=False)
            return f"Filed in {record['topic']}."
        if request.action == "new":
            record = store.add_memory(bot_id, request.body, request.path, create=True, fresh=True)
            return f"Filed in {record['topic']}."
        if request.action == "move":
            record = store.move_memory(bot_id, request.body, request.command, request.path)
            return f"Moved to {record['topic']}."
        if request.action == "also":
            record = store.point_memory(bot_id, request.body, request.path)
            return f"Pointed at {record.get('also')}."
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    raise ToolError("A memory topic can be read, filed, moved, or pointed at another topic.")


def _apply_bot_reaction(store: Store, bot_id: str | None, scope_id: str | None, request: ToolRequest) -> bool:
    """Put the bot's emoji on a person's message. A missing id does not fail the turn."""
    emoji = canonical_emoji(request.body or "")
    mid = (request.path or "").strip()
    if not scope_id or not emoji or not mid:
        return False
    if bot_id:
        try:
            store.set_reaction(bot_id, scope_id, mid, emoji)
            return True
        except StoreError:
            pass
    try:
        store.set_room_reaction(scope_id, mid, emoji)
        return True
    except StoreError:
        return False


def tapback_from_reply(store: Store, bot_id: str | None, scope_id: str | None, text: str) -> tuple[str, bool]:
    """Apply a react fence in one reply. Other tools in that reply are not run."""
    try:
        found = parse_tools(text or "")
    except ToolError:
        found = []
    did = False
    for item in found:
        if item.kind == "react" and _apply_bot_reaction(store, bot_id, scope_id, item):
            did = True
    return strip_tool_markup(text or ""), did


def _execute(store: Store, request: ToolRequest, bot_id: str | None = None) -> str:
    if request.kind == "react":
        return "The reaction is on that message."
    if request.kind == "files":
        return _run_files(store, request)
    if request.kind == "project":
        return _run_project(store, request, bot_id)
    if request.kind == "memory":
        return _run_memory(store, request, bot_id)
    if request.kind == "history":
        if not bot_id:
            raise ToolError("This turn has no bot, so no history was opened.")
        try:
            return redact(store, run_history(store, bot_id, request.action, request.path))
        except StoreError as exc:
            raise ToolError(str(exc)) from exc
    if request.kind == "failed":
        raise ToolError(request.body or "That call could not be run.")
    if request.kind == "shell":
        return _run_shell(store, request.command)
    if request.kind in {"ssh", "windows"}:
        kind = "linux" if request.kind == "ssh" else "windows"
        name = " ".join((request.computer or "").split())
        matches = [item for item in store.list_computers() if item.get("name") == name] if name else []
        if not name or len(matches) != 1:
            _note_heuristic(
                "_saved_computer",
                "an unknown or empty computer means this computer",
            )
            return _run_shell(store, request.command)
        computer = _saved_computer(store, request.computer, kind)
        if request.kind == "ssh":
            return run_ssh(computer, request.command)
        return run_windows(computer, request.command)
    raise ToolError("That action is not available.")


async def execute(store: Store, request: ToolRequest, bot_id: str | None = None) -> str:
    turn_mod.raise_if_cancelled()
    result = await asyncio.to_thread(_execute, store, request, bot_id)
    turn_mod.raise_if_cancelled()
    return result


async def _run_together(
    store: Store,
    requests: list[ToolRequest],
    bot_id: str | None,
) -> list[tuple[ToolRequest, str | None, Exception | None]]:
    """Calls from one reply do not depend on each other, so they run at the same time."""

    async def one(request: ToolRequest):
        try:
            if request.kind == "search":
                result = await search_mod.web_search(request.body)
            else:
                result = await execute(store, request, bot_id)
            return request, result, None
        except (ToolError, SearchError) as exc:
            return request, None, exc

    return list(await asyncio.gather(*(one(request) for request in requests)))


def happened_line(request: ToolRequest, messages: list[dict] | None = None) -> str:
    """What the person sees: the tool ran. Not the listing or the output."""
    if request.kind == "files" and request.action == "list" and messages and _wants_largest(messages):
        return f"Checked file sizes in {request.path}."
    if request.kind == "files" and request.action == "list":
        return f"Listed {request.path}."
    if request.kind == "files" and request.action == "read":
        return f"Read {request.path}."
    if request.kind == "files" and request.action == "write":
        return f"Wrote {request.path}."
    if request.kind == "project" and request.action == "read":
        return f"Read {request.command} in {request.path}."
    if request.kind == "project" and request.path:
        return f"Listed {request.path}."
    if request.kind == "project":
        return "Listed projects."
    if request.kind == "memory" and request.action == "read" and request.path:
        return f"Read memory topic {request.path}."
    if request.kind == "memory" and request.action == "read":
        return "Read the memory index."
    if request.kind == "memory" and request.action == "move":
        return f"Moved a line to {request.path}."
    if request.kind == "memory" and request.action == "also":
        return f"Pointed a line at {request.path}."
    if request.kind == "memory":
        return f"Filed a line in {request.path}."
    if request.kind == "history" and request.action in {"search", "find", "grep"}:
        return f"Searched past chats and memory for {request.path}."
    if request.kind == "history" and request.action in {"read", "open", "chat"}:
        return "Read a saved chat."
    if request.kind == "history" and request.action in {"memory", "notes"}:
        return "Read the memory files."
    if request.kind == "history":
        return "Listed saved chats."
    if request.kind == "shell":
        return "Ran a command on this computer."
    if request.kind in {"ssh", "windows"}:
        return f"Ran a command on {request.computer}."
    return "Ran a tool."


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return ""


_NAME = re.compile(r"(?i)(?<![A-Za-z0-9_])([A-Za-z0-9][\w.-]{0,80}\.[A-Za-z0-9]{1,8})(?![A-Za-z0-9])")
_ONE_FILE = re.compile(r"(?i)\b(see|seen|find|found|exist|exists|visible|there)\b")
_LISTING = re.compile(
    r"(?i)\b(list|listing|directory|everything|contents|what(?:'s| is) in|show (?:me )?(?:all|the folder|the directory))\b"
)


def _names_in(text: str) -> list[str]:
    found: list[str] = []
    for match in _NAME.finditer(text or ""):
        name = match.group(1)
        if name.lower() in {"e.g", "i.e"}:
            continue
        ext = name.rsplit(".", 1)[-1]
        if ext.isdigit() or len(ext) < 2:
            continue
        stem = name.rsplit(".", 1)[0].replace(".", "")
        if stem.isdigit():
            continue
        if name not in found:
            found.append(name)
    return found


def _is_harness(text: str) -> bool:
    raw = text or ""
    if "[[easyagent-working]]" in raw:
        return True
    stripped = raw.lstrip()
    return stripped.startswith((
        "The tool finished.",
        "The tool result is the tool message",
        "Finish is not an ending",
        "Write the answer",
        "Continue the account",
        "You are still in the middle of the account.",
        "That command failed:",
        "That only announced the work.",
        "That tool call was cut off.",
        "Web search ran on this computer",
        "You returned nothing.",
        "The write failed:",
        "That sentence is not a finish.",
        "The breaker found a defect.",
    ))


def _one_file_name(messages: list[dict]) -> str | None:
    """The file they asked about.

    None means a folder listing is the right result to summarize.
    An empty string means they asked about a file and did not name one.
    """
    latest = ""
    earlier: list[str] = []
    seen = False
    for message in reversed(messages or []):
        text = _message_text(message)
        if message.get("role") == "user" and _is_harness(text):
            continue
        if not seen:
            if message.get("role") != "user":
                continue
            latest = text
            seen = True
            continue
        if text.strip():
            earlier.append(text)
    if not latest.strip():
        return None
    named = _names_in(latest)
    about = bool(_ONE_FILE.search(latest) or re.search(r"(?i)\bfile\b", latest))
    listing = bool(_LISTING.search(latest))
    if named and about and not (listing and not _ONE_FILE.search(latest)):
        return named[-1]
    if listing and not about:
        return None
    if about and _ONE_FILE.search(latest):
        for text in earlier:
            prior = _names_in(text)
            if prior:
                return prior[-1]
        return ""
    if named and not listing:
        return named[-1]
    return None


def _listing_names(result: str) -> list[str]:
    names: list[str] = []
    for line in (result or "").splitlines():
        item = line.strip()
        if not item or item == "(empty)" or item.startswith("Listed ") or item.startswith("("):
            continue
        names.append(item)
    return names


def _has_name(names: list[str], wanted: str) -> bool:
    target = wanted.casefold()
    for name in names:
        bare = name.rstrip("/").casefold()
        if bare == target:
            return True
    return False


def _original_user_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        text = _message_text(message)
        if _is_harness(text):
            continue
        return text
    return ""


def _wants_largest(messages: list[dict]) -> bool:
    text = _original_user_text(messages)
    return bool(re.search(r"(?i)\b(largest|biggest|file size|file sizes|size of)\b", text))


def model_slice(store: Store, request: ToolRequest, result: str, messages: list[dict]) -> str:
    """What the model may see. A long folder becomes a short slice, or a yes or no."""
    if request.kind == "project" and request.action == "list" and request.path:
        names = _listing_names(result)
        shown = names[:_MODEL_NAMES]
        more = len(names) - len(shown)
        lines = "\n".join(shown)
        note = f"\n{more} more names are not included." if more else ""
        text = f"Listed {request.path}. {len(names)} names.\n{lines}{note}".strip()
        if len(text) > _MODEL_CHARS:
            text = text[: _MODEL_CHARS - 30].rstrip() + "\n(more names not included)"
        return text
    if request.kind == "files" and request.action == "list" and _wants_largest(messages):
        try:
            return _measure_largest(_user_path(store, request.path))
        except ToolError as exc:
            return str(exc)
    if request.kind == "files" and request.action == "list":
        names = _listing_names(result)
        folder = request.path
        wanted = _one_file_name(messages)
        if wanted is not None:
            if not wanted:
                return (
                    f"Listed {folder}. The person asked about one file, not for every name. "
                    "Do not recite the folder."
                )
            if _has_name(names, wanted):
                return f"{wanted} is in {folder}."
            return f"{wanted} is not in {folder}."
        shown = names[:_MODEL_NAMES]
        more = len(names) - len(shown)
        lines = "\n".join(shown)
        note = f"\n{more} more names are not included." if more else ""
        text = f"Listed {folder}. {len(names)} names.\n{lines}{note}".strip()
        if len(text) > _MODEL_CHARS:
            text = text[: _MODEL_CHARS - 30].rstrip() + "\n(more names not included)"
        return text
    body = result or ""
    if _full_page_read(request, body):
        return body
    if len(body) > _MODEL_CHARS:
        body = body[: _MODEL_CHARS - 24].rstrip() + "\n[result truncated]"
    return body


def _full_page_read(request: ToolRequest, body: str) -> bool:
    """The whole page. The first slice is the hero, and the lower half is the rest of the file."""
    if request.kind != "files" or request.action != "read":
        return False
    name = (request.path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if not name.endswith((".html", ".htm")):
        return False
    text = (body or "").lower()
    return "<html" in text or "<!doctype" in text


def _without_dump(answer: str, raw: str) -> str:
    """Drop a pasted listing or a long verbatim tool result from the reply."""
    text = (answer or "").strip()
    raw_text = (raw or "").strip()
    if raw_text and len(raw_text) >= 60 and raw_text in text:
        text = text.replace(raw_text, "")
    names = _listing_names(raw_text)
    if len(names) >= 3:
        block = "\n".join(names)
        if len(block) >= 40 and block in text:
            text = text.replace(block, "")
        kept: list[str] = []
        hits = 0
        name_set = {name.lower() for name in names}
        for line in text.splitlines():
            if line.strip().lower() in name_set:
                hits += 1
                continue
            kept.append(line)
        if hits >= 3:
            text = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _compose(line: str, answer: str) -> str:
    cleaned = (answer or "").strip()
    lead = (line or "").strip()
    if not lead:
        return cleaned
    if not cleaned or cleaned == lead:
        return lead
    return f"{lead}\n\n{cleaned}"


_EXACT_LOG = re.compile(
    r"(?i)^(?:"
    r"searched the web\.|"
    r"web search failed\.|"
    r"the search found nothing\.|"
    r"web search failed:.*|"
    r"search failed:.*|"
    r"ran a command on [^.]+\.|"
    r"ran a tool\.|"
    r"listed projects\.|"
    r"read the memory index\."
    r")$"
)
_LEADING_LOG = re.compile(
    r"(?i)^(?:searched the web\.|ran a command on [^.]+\.|ran a tool\.)\s+"
)


def _is_exact_log(line: str, known: list[str] | None = None) -> bool:
    """A harness status line. The model's own sentence is not one."""
    text = " ".join((line or "").split())
    if not text:
        return False
    if text in {item.strip() for item in (known or []) if item and item.strip()}:
        return True
    return bool(_EXACT_LOG.match(text))


def _drop_known_logs(text: str, known: list[str] | None = None) -> str:
    """The bubble is the model's words. A tool log is not the chat."""
    blocks: list[str] = []
    leads = [item.strip() for item in (known or []) if item and item.strip()]
    for block in re.split(r"\n\s*\n", text or ""):
        rows: list[str] = []
        for row in block.splitlines():
            cleaned = _LEADING_LOG.sub("", row).strip()
            for lead in leads:
                if cleaned.startswith(lead):
                    cleaned = cleaned[len(lead):].strip()
            if not cleaned or _is_exact_log(cleaned, known) or _is_harness_narration(cleaned):
                continue
            rows.append(cleaned)
        piece = "\n".join(rows).strip()
        if piece:
            blocks.append(piece)
    return "\n\n".join(blocks)


_ANNOUNCE_LEAD = re.compile(
    r"(?i)\b(let me|let's|i'll|i will|i am going to|i'm going to|going to)\b"
)
_ANNOUNCE_WORK = re.compile(
    r"(?i)\b(check|look|see|list|find|read|run|search|get|measure|size|sizes)\b"
)
_SIZE_STATED = re.compile(r"(?i)\b\d[\d,]*\s*(bytes|byte|kb|mb|gb)\b")
_WIN_PATH = re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\s]+\\)*[^\\/:*?\"<>|\s]*")
_POSIX_PATH = re.compile(r"(?<![\w])(/(?:[\w.-]+/)*[\w.-]+)")


def _asks_the_person(text: str) -> bool:
    """A question for the person. That is the reply, not a promise to run a tool."""
    cleaned = " ".join((text or "").split())
    if "?" not in cleaned:
        return False
    return bool(re.search(
        r"(?i)\b(you|your|do you|does your|would you|could you|should you|are you|have you)\b",
        cleaned,
    ))


def _is_choice(ask: str) -> bool:
    """A recommendation between options. The model's reply is the end of that turn."""
    text = ask or ""
    if re.search(r"(?i)\b(versus|\bvs\b|tradeoff)\b", text):
        return True
    return bool(re.search(r"(?i)\bshould i\b", text) and re.search(r"(?i)\bor\b", text))


def _is_harness_narration(text: str) -> bool:
    """A sentence about the turn itself. The person never sees that."""
    return bool(re.search(
        r"(?i)("
        r"the reply was already complete"
        r"|did(?:n't| not) promise to check"
        r"|promise to check anything"
        r"|that only announced the work"
        r"|you can decide that"
        r")",
        text or "",
    ))


def _is_announcement(text: str) -> bool:
    """A promise to do the work, with no tool call and no finished result."""
    cleaned = strip_tool_markup(search_mod.strip_search_fences(text or "")).strip()
    if not cleaned:
        return False
    try:
        if parse_tool(text or "") is not None:
            return False
    except ToolError:
        return False
    if search_mod.search_query(text or ""):
        return False
    if _asks_the_person(cleaned):
        return False
    if not _ANNOUNCE_LEAD.search(cleaned) or not _ANNOUNCE_WORK.search(cleaned):
        return False
    # "I'll help you look things up" is a greeting, not a step this turn still owes.
    if re.search(r"(?i)\b(?:i'll|i will|i am going to|i'm going to)\s+help\b", cleaned):
        return False
    if _OBSERVED.search(cleaned):
        return False
    if _SIZE_STATED.search(cleaned) and _names_in(cleaned):
        return False
    _note_heuristic("_is_announcement", "the reply says it will do the work and does not call a tool")
    return True


_STILL_STEP = re.compile(
    r"(?i)\b(let me|let's|i'll|i will|i am going to|i'm going to|going to)\b"
    r".{0,120}\b(look|check|read|see|compare)\b"
)
_STILL_REST = re.compile(
    r"(?i)\b(the rest of the file|lower half|not just the hero|next part|next section)\b"
)
_SETTLED = re.compile(
    r"(?i)\b(nothing else is missing|leaving the file|leaving it|i'm leaving|i am leaving|as it is)\b"
)


_STILL_READ = re.compile(
    r"(?i)\b(read it back|let me read|i'll read|i will read|let me check|i'll check|let me look|i'll look)\b"
)
_OBSERVED = re.compile(
    r"(?i)((?<!what )(?<!to )\bi see\b|\bi saw\b|\bi found\b|\ball good\b)"
)
_WRAP_UP = re.compile(
    r"(?i)("
    r"\byou can open\b|"
    r"\bopen it\b|"
    r"\bis ready\b|"
    r"\bare ready\b|"
    r"\bready at\b|"
    r"\ball set\b|"
    r"\bthat(?:'s| is) the page\b|"
    r"\bpage is at\b|"
    r"\bwritten to\b|"
    r"\bsaved (?:it |the file |the page )?at\b"
    r")"
)


def _still_thinking(text: str) -> bool:
    """A plan to read or check is the middle of the account. It is not the end of the turn.

    The same paragraph is finished when it already says what the check found.
    """
    cleaned = strip_tool_markup(search_mod.strip_search_fences(text or "")).strip()
    if not cleaned or _SETTLED.search(cleaned) or _OBSERVED.search(cleaned):
        return False
    return bool(
        _STILL_STEP.search(cleaned)
        or _STILL_REST.search(cleaned)
        or _STILL_READ.search(cleaned)
    )


def _last_paragraph(text: str) -> str:
    parts = [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]
    return parts[-1] if parts else ""


_CHECK_RESULT = re.compile(
    r"(?i)("
    r"\bfound\b|"
    r"\bshowed\b|\bshows\b|"
    r"\bconfirmed\b|\bconfirms\b|"
    r"\blanded\b|"
    r"\bno longer\b|"
    r"\bnow (?:points?|goes|go|reads?|says)\b|"
    r"\bread[- ]back\b|"
    r"\bdead links?\b|"
    r"\blinks? (?:now|are|were|all)\b"
    r")"
)


def _reports_check(text: str) -> bool:
    """What a read or check found. That is not a repeated ending."""
    return bool(_CHECK_RESULT.search(" ".join((text or "").split())))


def _is_wrap_up(text: str) -> bool:
    """A concluding sentence. A plan to read or check, or a check result, is not one."""
    cleaned = " ".join((text or "").split())
    if not cleaned or _still_thinking(cleaned) or _reports_check(cleaned):
        return False
    return bool(_WRAP_UP.search(cleaned))


def _only_open_line(text: str) -> bool:
    """A last line that only says to open the file. A check result is not this."""
    cleaned = " ".join((text or "").split())
    if not cleaned or _reports_check(cleaned) or _still_thinking(cleaned):
        return False
    return bool(re.search(r"(?i)\b(you can open|open it)\b", cleaned))


def _only_wrap(text: str) -> bool:
    parts = [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]
    return bool(parts) and all(_is_wrap_up(part) for part in parts)


def _drop_last_paragraph(text: str) -> str:
    parts = [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]
    if len(parts) <= 1:
        return ""
    return "\n\n".join(parts[:-1])


def _end_once(text: str) -> str:
    """Drop a paragraph only when it repeats an earlier one and adds nothing."""
    blocks = [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]
    if len(blocks) < 2:
        return (text or "").strip()
    kept: list[str] = []
    for block in blocks:
        if _reports_check(block) or _still_thinking(block):
            kept.append(block)
            continue
        if _is_wrap_up(block) and any(_wrap_adds_nothing(block, earlier) for earlier in kept):
            continue
        kept.append(block)
    return "\n\n".join(kept)


_WRAP_GLUE = {
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "at", "for", "from",
    "with", "it", "its", "you", "your", "can", "is", "are", "be", "was", "now",
    "this", "that", "any", "open", "page", "ready", "file", "browser", "see",
    "just", "also", "there", "here", "all", "set", "saved", "written", "disk",
}


_DONE_WORDS = {"done", "verified", "finished", "complete", "ready", "good"}


def _short_done_line(text: str) -> bool:
    """A short ending with no fact and no title. The closer takes its place."""
    cleaned = " ".join((text or "").split())
    if not cleaned or len(cleaned) > 120:
        return False
    if _still_thinking(cleaned) or _reports_check(cleaned):
        return False
    if not re.search(r"(?i)\b(done|verified|finished|complete|ready|all good|all set)\b", cleaned):
        return False
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", cleaned.casefold())
        if word not in _WRAP_GLUE and word not in _DONE_WORDS and word != "all"
    ]
    return not words


def _states_finding(text: str) -> bool:
    """A sentence after a read that says what came back. A plan or a bare done-line does not."""
    cleaned = " ".join((_spoken(text) or text or "").split())
    if not cleaned:
        return False
    if _still_thinking(cleaned):
        return False
    if _BARE_READY.match(cleaned) or _short_done_line(cleaned):
        return False
    if _is_wrap_up(cleaned) and not _reports_check(cleaned) and not _OBSERVED.search(cleaned):
        return False
    words = re.findall(r"[A-Za-z]{3,}", cleaned)
    return len(words) >= 4


def _wrap_adds_nothing(new: str, old: str) -> bool:
    """A later wrap-up adds nothing when it brings no new fact, only glue words."""

    def words(text: str) -> set[str]:
        return {
            word
            for word in re.findall(r"[a-z0-9]+", (text or "").casefold())
            if word not in _WRAP_GLUE
        }

    return words(new) <= words(old)


def _names_page(text: str, path: str) -> bool:
    """The final words already name the title and the path."""
    name = _page_name(path)
    if not name or not (path or "").strip():
        return False
    folded = " ".join((text or "").split()).casefold()
    if " ".join(name.split()).casefold() not in folded:
        return False
    wanted = path.replace("/", "\\").casefold()
    return wanted in (text or "").replace("/", "\\").casefold()


def _find_path(text: str) -> str:
    win = _WIN_PATH.search(text or "")
    if win:
        return win.group(0).rstrip("\\").rstrip(".,);]")
    posix = _POSIX_PATH.search(text or "")
    if posix:
        return posix.group(1).rstrip(".,);]")
    return ""


def _join_stored(folder: str, name: str) -> str:
    sep = "\\" if "\\" in folder else "/"
    return folder.rstrip("\\/") + sep + name


def _blank_reply(text: str) -> bool:
    """An empty model message, including the raw endpoint sentence, is not an answer."""
    cleaned = " ".join((text or "").split()).lower().rstrip(".")
    return cleaned in {"", "endpoint returned an empty message", "(empty reply)"}


def _wants_search(text: str) -> bool:
    """News, or a live fact such as a current version, release, or price."""
    blob = text or ""
    if re.search(
        r"(?i)\b(news|headline|top story|top stories|cnn|bbc|weather|look up|look it up|search the web|search for)\b",
        blob,
    ):
        return True
    if re.search(
        r"(?i)\b(?:latest|newest|current|stable|recent)\b(?:\s+\w+){0,5}\s+\b(?:version|release|releases)\b",
        blob,
    ):
        return True
    if re.search(
        r"(?i)\b(?:version|release|releases)\b(?:\s+\w+){0,4}\s+\b(?:latest|newest|current|stable|today|now)\b",
        blob,
    ):
        return True
    if re.search(r"(?i)\bwhat(?:'s| is)\s+the\s+(?:latest|newest|current|stable)\b", blob):
        return True
    if re.search(r"(?i)\b(?:price|prices|pricing)\b|\bhow much (?:does|do|is|are)\b", blob):
        return True
    return False


def _search_query_from(ask: str) -> str:
    text = " ".join((ask or "").split()).rstrip("?").strip()
    text = re.sub(r"(?i)^(please\s+)?(what(?:'s| is)|whats|tell me|can you|could you)\s+", "", text)
    text = re.sub(r"(?i)^the\s+", "", text)
    return text[:200]


def _live_release_ask(text: str) -> bool:
    """A latest or current version or release. A news item or a price is not one."""
    blob = text or ""
    if not re.search(r"(?i)\b(?:version|release|releases)\b", blob):
        return False
    return bool(re.search(r"(?i)\b(?:latest|newest|current|stable|recent)\b", blob))


def _release_query(ask: str, *, followup: bool = False) -> str:
    """Aim a latest-release search at the official current release."""
    text = _search_query_from(ask)
    year = str(date.today().year)
    if followup:
        core = re.sub(r"(?i)\s+and\s+what\b.*$", "", text).strip()
        core = re.sub(
            r"(?i)\b(?:official|latest|newest|current|stable|recent|versions?|releases?)\b",
            " ",
            core,
        )
        core = " ".join(core.split())
        return f"official latest stable {core} {year}".strip()[:200]
    if year not in text and not re.search(r"(?i)\blatest stable\b", text):
        text = f"{text} {year}".strip()
    return text[:200]


_VERSION_TOKEN = re.compile(r"\b(\d+\.\d+(?:\.\d+)*)\b")
_YEAR_TOKEN = re.compile(r"\b(20\d{2})\b")


def _version_keys(text: str) -> list[tuple[int, ...]]:
    found: list[tuple[int, ...]] = []
    for match in _VERSION_TOKEN.finditer(text or ""):
        try:
            found.append(tuple(int(part) for part in match.group(1).split(".")))
        except ValueError:
            continue
    return found


def _years_in(text: str) -> list[int]:
    return [int(match.group(1)) for match in _YEAR_TOKEN.finditer(text or "")]


def _hits_look_old(findings: str) -> bool:
    """The newest year in the hits is before this year. That is an old announcement."""
    years = _years_in(findings)
    if not years:
        return False
    return max(years) < date.today().year


def _answer_is_stale(prose: str, findings: str) -> bool:
    """The reply cites an older release than the hits, or a version the hits do not have."""
    said_versions = _version_keys(prose)
    hit_versions = _version_keys(findings)
    if said_versions and hit_versions:
        if not any(item in hit_versions for item in said_versions):
            return True
        if max(said_versions) < max(hit_versions):
            return True
    said_years = _years_in(prose)
    hit_years = _years_in(findings)
    if said_years and hit_years and max(said_years) < max(hit_years):
        return True
    return False


def _command_from_ask(ask: str) -> str:
    quoted = re.search(r"(?i)\b(?:run|execute)\s+(?:the\s+)?command\s+[`'\"]([^`'\"]+)[`'\"]", ask or "")
    if quoted:
        return " ".join(quoted.group(1).split())
    fenced = re.search(r"`([^`]+)`", ask or "")
    if fenced and re.search(r"(?i)\b(run|execute|command)\b", ask or ""):
        return " ".join(fenced.group(1).split())
    return ""


def _wants_page(text: str) -> bool:
    """A page or a one-page site. A research question does not match."""
    blob = text or ""
    if re.search(
        r"(?i)\b(landing page|web page|html page|sample page|homepage|one-page site|one page site)\b",
        blob,
    ):
        return True
    if re.search(r"(?i)\b(build|make|create|write)\b", blob) and re.search(
        r"(?i)\b(website|web site|webpage|one-page)\b",
        blob,
    ):
        return True
    return bool(
        re.search(r"(?i)\b(build|make|create)\b", blob) and re.search(r"(?i)\bsite\b", blob)
    )


def _wants_image(text: str) -> bool:
    if _wants_page(text):
        return False
    if not re.search(r"(?i)\b(picture|image|png|jpe?g|gif|webp|voxel|drawing|screenshot)\b", text or ""):
        return False
    return bool(re.search(r"(?i)\b(make|create|draw|render|build|write|save|put|generate)\b", text or ""))


def _default_folder() -> str:
    """The folder used when a request does not name one."""
    return display_path(default_deliverable_dir())


def _target_path(ask: str, default_name: str, suffixes: tuple[str, ...]) -> str:
    """The file to write. A missing folder becomes this computer's app-data folder."""
    path = _find_path(ask)
    names = [name for name in _names_in(ask) if name.lower().endswith(suffixes)]
    filename = names[-1] if names else default_name
    if not path:
        path = _default_folder()
    folded = path.replace("\\", "/").rstrip("/")
    lower = folded.lower()
    if lower.endswith(suffixes) or lower.endswith("/" + filename.lower()):
        return path
    return _join_stored(path, filename)


def _deliverable(messages: list[dict]) -> ToolRequest | None:
    """A page or a picture the person asked for. The turn is done when this file is on disk."""
    ask = _original_user_text(messages)
    if _wants_page(ask):
        return ToolRequest(
            kind="files",
            action="write",
            path=_target_path(ask, "landing.html", _PAGE_SUFFIXES),
            body=_landing_html(page_subject(ask)),
        )
    if _wants_image(ask):
        return ToolRequest(
            kind="files",
            action="write",
            path=_target_path(ask, "picture.png", _IMAGE_SUFFIXES),
            body=_PNG_MARK,
        )
    return None


def _needed_tool(messages: list[dict], announcement: str = "") -> ToolRequest | None:
    """The tool a request needs when the model did not call one."""
    ask = _original_user_text(messages)
    if _complaint_kind(ask) == "missing":
        recovered = _write_from_history(messages)
        if recovered is not None:
            _note_heuristic("_needed_tool", "the person says the file is missing, so the same write runs again")
            return recovered
    made = _deliverable(messages)
    if made is not None:
        subject = page_subject(ask)
        canned = (
            made.action == "write"
            and bool(subject)
            and not page_matches((made.body or "").encode("utf-8"), subject)
        )
        if canned:
            _note_heuristic("_needed_tool", "the page comes from the model, so a template is not written")
        else:
            kind = "picture" if _wants_image(ask) else "page"
            _note_heuristic("_needed_tool", f"the person asked for a {kind}, so that file is written")
            return made
    blob = f"{announcement}\n{ask}"
    path = _find_path(ask) or _find_path(announcement)
    if summary_job(ask) is not None:
        return None
    if re.search(r"(?i)\b(write|create|save)\b", ask) and re.search(r"(?i)\bfiles?\b", ask) and path:
        write = _asked_to_write(messages)
        if write is not None:
            _note_heuristic("_needed_tool", "the person asked to write a file and named a path")
        return write
    if path and re.search(r"(?i)\b(size|sizes|largest|biggest)\b", blob):
        _note_heuristic("_needed_tool", "the person asked for a file size, so the folder is listed")
        return ToolRequest(kind="files", action="list", path=path)
    if path and re.search(r"(?i)\bread\b", ask):
        named = _names_in(ask)
        if named:
            folded = path.replace("\\", "/").lower()
            target = path if folded.endswith(named[-1].lower()) else _join_stored(path, named[-1])
            _note_heuristic("_needed_tool", "the person asked to read a file")
            return ToolRequest(kind="files", action="read", path=target)
        if re.search(r"(?i)\.[A-Za-z0-9]{1,8}$", path):
            _note_heuristic("_needed_tool", "the person asked to read a file")
            return ToolRequest(kind="files", action="read", path=path)
    if path and re.search(r"(?i)\b(list|folder|directory|contents)\b", ask):
        _note_heuristic("_needed_tool", "the person asked to list a folder")
        return ToolRequest(kind="files", action="list", path=path)
    command = _command_from_ask(ask)
    if command:
        _note_heuristic("_needed_tool", "the person asked to run a command")
        return ToolRequest(kind="shell", command=command)
    if _wants_search(ask) or _wants_search(announcement):
        source = ask or announcement
        query = _release_query(source) if _live_release_ask(source) else _search_query_from(source)
        if query:
            _note_heuristic("_needed_tool", "the person asked for something from the web")
            return ToolRequest(kind="search", body=query)
    return None


def _implied_tool(announcement: str, messages: list[dict]) -> ToolRequest | None:
    """The tool named by an announcement, using the person's path when they gave one."""
    return _needed_tool(messages, announcement)


def _mentions(blob: str, name: str) -> bool:
    text = " ".join((name or "").split())
    if len(text) < 2:
        return False
    return bool(re.search(rf"(?i)(?<!\w){re.escape(text)}(?!\w)", blob or ""))


def _implied_project(store: Store, bot_id: str, announcement: str, messages: list[dict]) -> ToolRequest | None:
    """A project list or read named by an announcement, only among projects this bot can use."""
    try:
        projects = store.projects_for_bot(bot_id)
    except StoreError:
        return None
    if not projects:
        return None
    blob = f"{announcement}\n{_original_user_text(messages)}"
    catalog: list[tuple[dict, list[dict]]] = []
    for project in projects:
        try:
            files = store.list_project_files(project["id"])
        except StoreError:
            files = []
        catalog.append((project, files))
    project_hits = [project for project, _files in catalog if _mentions(blob, project.get("name") or "")]
    named_ids = {project["id"] for project in project_hits}
    file_hits: list[tuple[dict, str]] = []
    for project, files in catalog:
        for meta in files:
            if _mentions(blob, meta.get("name") or ""):
                file_hits.append((project, meta["name"]))
    if file_hits:
        chosen = [hit for hit in file_hits if hit[0]["id"] in named_ids] or file_hits
        names = {filename.casefold() for _project, filename in chosen}
        projects_named = {hit[0]["id"] for hit in chosen}
        if len(names) == 1 and len(projects_named) == 1:
            project, filename = chosen[0]
            _note_heuristic("_implied_project", f"the reply names {filename} in {project['name']}")
            return ToolRequest(kind="project", action="read", path=project["name"], command=filename)
    if len(project_hits) == 1:
        _note_heuristic("_implied_project", f"the reply names the project {project_hits[0]['name']}")
        return ToolRequest(kind="project", action="list", path=project_hits[0]["name"])
    ask = _original_user_text(messages)
    if re.search(r"(?i)\bprojects?\b", ask) and len(projects) == 1:
        _note_heuristic("_implied_project", "the person asked about a project and only one is available")
        return ToolRequest(kind="project", action="list", path=projects[0]["name"])
    return None


def _implied_memory(store: Store, bot_id: str, announcement: str, messages: list[dict]) -> ToolRequest | None:
    """Read one topic named by an announcement. The index is the only file opened here."""
    try:
        slugs = store.memory_slugs(bot_id)
    except StoreError:
        return None
    if not slugs:
        return None
    blob = f"{announcement}\n{_original_user_text(messages)}"
    hits = []
    for slug in slugs:
        title = slug.replace("-", " ")
        if _mentions(blob, slug) or (title != slug and _mentions(blob, title)):
            hits.append(slug)
    if len(hits) == 1:
        _note_heuristic("_implied_memory", f"the reply names the memory topic {hits[0]}")
        return ToolRequest(kind="memory", action="read", path=hits[0])
    return None


def _split_stored_path(path: str) -> tuple[str, str]:
    text = (path or "").rstrip("\\/")
    if "\\" in text:
        folder, _, name = text.rpartition("\\")
        return name, folder
    folder, _, name = text.rpartition("/")
    return name, folder


def _file_sentence(path: str) -> str:
    name, folder = _split_stored_path(path)
    if not name:
        return ""
    return f"The file is {name} in {folder}."


def _last_write_path(lines: list[str]) -> str:
    for line in reversed(lines):
        if line.startswith("Wrote ") and line.endswith("."):
            return line[len("Wrote "):-1]
    return ""


def _answer_names_file(answer: str, path: str) -> bool:
    name, folder = _split_stored_path(path)
    text = answer or ""
    if not name or name not in text:
        return False
    return (not folder) or folder in text or path in text


def _looks_like_creation(text: str) -> bool:
    """A sentence that claims a file was created. That claim is not a write."""
    if not re.search(r"(?i)\b(created|wrote|saved)\b", text or ""):
        return False
    return bool(_names_in(text or ""))


def _with_file_place(answer: str, lines: list[str]) -> str:
    path = _last_write_path(lines)
    if not path:
        lowered = (answer or "").lower()
        if lowered.startswith("the file was not written") or lowered.startswith("write failed"):
            return answer
        if _looks_like_creation(answer):
            return "The file was not written."
        return answer
    if re.search(r"(?i)\bnot written\b", answer or ""):
        answer = ""
    if _looks_like_creation(answer) and not _answer_names_file(answer, path):
        answer = ""
    if path.lower().endswith((".html", ".htm")) and _page_name(path):
        if _names_page(answer, path):
            return answer
        sentence = _page_reply(path)
        if not sentence:
            return answer
        return _compose(answer, sentence)
    if _answer_names_file(answer, path):
        return answer
    sentence = _file_sentence(path)
    if not sentence:
        return answer
    return _compose(answer, sentence)


def _filename_from_prose(request: ToolRequest, prose: str) -> ToolRequest:
    """Use the filename in a prose claim. The folder stays the one the person named."""
    if request.kind != "files" or request.action != "write":
        return request
    if not _looks_like_creation(prose or ""):
        return request
    names = _names_in(prose or "")
    if not names:
        return request
    _name, folder = _split_stored_path(request.path)
    if not folder:
        return request
    sep = "\\" if "\\" in request.path else "/"
    target = folder.rstrip("\\/") + sep + names[-1]
    _note_heuristic("_filename_from_prose", f"the reply names {names[-1]}, so the write uses that filename")
    return ToolRequest(kind="files", action="write", path=target, body=request.body or "test file\n")


def _complaint_kind(text: str) -> str:
    """A complaint is not a new task and it is not a reason to stop."""
    raw = text or ""
    if re.search(
        r"(?i)\b(don'?t see|do not see|not there|isn'?t there|is not there|no file|file is missing|missing file|where is the file)\b",
        raw,
    ):
        return "missing"
    if re.search(r"(?i)\b(i see nothing|see nothing|nothing came back|i see no output)\b", raw):
        return "nothing"
    return ""


def _text_after_path(ask: str, path: str) -> str:
    """Words after a folder are the file's text when they are not instructions."""
    if not path or path not in (ask or ""):
        return ""
    rest = (ask or "").split(path, 1)[1].strip(" \t:.-")
    rest = re.sub(r"(?i)^(and\s+)?(name it|call it|whatever you want).*$", "", rest).strip()
    if not rest:
        return ""
    if re.search(r"(?i)\b(name it|whatever you want)\b", rest) and not re.search(r"(?i)\b(sample|just|random)\b", rest):
        return ""
    if len(rest.split()) >= 4 or re.search(r"(?i)\b(sample|just|random)\b", rest):
        return rest.strip(" .")
    return ""


def _prose_tail(rest: str, filename: str) -> bool:
    text = rest or ""
    if filename:
        text = re.sub(r"[\\/]*" + re.escape(filename) + r"\s*$", "", text, flags=re.IGNORECASE)
    words = [word for word in re.split(r"[\s\\/]+", text.strip()) if word]
    return len(words) >= 4


def _with_write_body(target: str, content: str, prose: str, ask: str) -> tuple[str, str]:
    if (content or "").strip() == _PNG_MARK:
        return target, _PNG_MARK
    prose = " ".join((prose or "").split())
    if prose and (not (content or "").strip() or (content or "").strip() == "test file"):
        body = prose if prose.endswith("\n") else prose + "\n"
        return target, body
    if (content or "").strip():
        return target, content
    extra = _text_after_path(ask, target)
    if not extra:
        _name, folder = _split_stored_path(target)
        if folder:
            extra = _text_after_path(ask, folder)
    body = (extra + "\n") if extra else "test file\n"
    return target, body


def _clean_write_target(path: str, body: str, ask: str = "") -> tuple[str, str]:
    """Split a smashed path into a folder, a filename, and the file's text."""
    raw = (path or "").strip().strip('"').strip()
    content = body or ""
    if not raw:
        return _with_write_body(raw, content, "", ask)
    names = _names_in(raw)
    filename = names[-1] if names else ""
    drive = re.match(r"^([A-Za-z]:)[\\/](.*)$", raw, re.DOTALL)
    if drive:
        letter, tail = drive.group(1), drive.group(2)
        parts = [part for part in re.split(r"[\\/]", tail) if part]
        if not parts:
            return _with_write_body(raw, content, "", ask)
        clean: list[str] = []
        prose_words: list[str] = []
        for index, part in enumerate(parts):
            if prose_words:
                if filename and part.lower() == filename.lower():
                    continue
                prose_words.extend(part.split())
                continue
            if " " in part:
                words = part.split()
                following = " ".join(words[1:] + parts[index + 1:])
                if words and _prose_tail(following, filename):
                    clean.append(words[0])
                    prose_words.extend(words[1:])
                    continue
            if index == len(parts) - 1 and filename and part.lower() == filename.lower():
                continue
            clean.append(part)
        folder = "\\".join(clean).strip("\\")
        if filename:
            target = f"{letter}\\{folder}\\{filename}" if folder else f"{letter}\\{filename}"
        else:
            target = f"{letter}\\{folder}" if folder else letter + "\\"
        prose = " ".join(prose_words)
        if filename:
            prose = re.sub(
                r"[\\/]*" + re.escape(filename) + r"\s*$",
                "",
                prose,
                flags=re.IGNORECASE,
            ).strip()
        return _with_write_body(target, content, prose, ask)
    if " " in raw and "/" in raw:
        parts = raw.split("/")
        clean: list[str] = []
        prose_words: list[str] = []
        for index, part in enumerate(parts):
            if prose_words:
                if filename and part.lower() == filename.lower():
                    continue
                prose_words.extend(part.split())
                continue
            if " " in part:
                words = part.split()
                clean.append(words[0])
                prose_words.extend(words[1:])
                continue
            if index == len(parts) - 1 and filename and part.lower() == filename.lower():
                continue
            clean.append(part)
        folder = "/".join(clean).rstrip("/")
        target = f"{folder}/{filename}" if filename else folder
        prose = " ".join(prose_words)
        if filename:
            prose = re.sub(r"[\\/]*" + re.escape(filename) + r"\s*$", "", prose, flags=re.IGNORECASE).strip()
        return _with_write_body(target, content, prose, ask)
    return _with_write_body(raw, content, "", ask)


def _smashed_span(text: str) -> str:
    win = re.search(r"[A-Za-z]:\\[^\r\n`]+", text or "")
    if win:
        return win.group(0).strip().rstrip(".,);]")
    match = re.search(r"(?<![\w])(/(?:[^\r\n`])+?\.[A-Za-z0-9]{1,8})(?![\w])", text or "")
    if match:
        return match.group(1).rstrip(".,);]")
    return ""


def _write_from_history(messages: list[dict]) -> ToolRequest | None:
    """The write the person already asked for, with the folder and the filename split apart."""
    found_path = ""
    found_body = ""
    for message in messages or []:
        text = _message_text(message)
        if not text or _is_harness(text):
            continue
        span = _smashed_span(text) or _find_path(text)
        if not span:
            continue
        ask = text if message.get("role") == "user" else ""
        path, body = _clean_write_target(span, "", ask)
        if not path:
            continue
        found_path = path
        if body.strip() and body.strip() != "test file":
            found_body = body
    if not found_path:
        for message in messages or []:
            text = _message_text(message)
            if message.get("role") == "user" and re.search(r"(?i)\b(write|create|save)\b", text):
                made = _asked_to_write([message])
                if made is not None:
                    return made
        return None
    name, folder = _split_stored_path(found_path)
    if not name or "." not in name:
        parent = _join_stored(folder, name) if folder else found_path
        found_path = _join_stored(parent, "test-file.txt")
    return ToolRequest(kind="files", action="write", path=found_path, body=found_body or "test file\n")


def _status_only(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return True
    pattern = re.compile(
        r"(?i)^(ran a command(?: on [^.]*)?|listed .+|read .+|wrote .+|checked file sizes.+|"
        r"searched the web|filed a line.+|moved a line.+|pointed a line.+|"
        r"read memory topic .+|read the memory index)\.?$"
    )
    return all(pattern.match(line) for line in lines)


def _previous_visible(messages: list[dict]) -> str:
    """The last answer that already told the person what came back."""
    for message in reversed(messages or []):
        if message.get("role") != "assistant":
            continue
        text = _message_text(message).strip()
        if not text or _is_harness(text) or _status_only(text):
            continue
        return text
    return ""


def _implied_remote(store: Store, messages: list[dict], announcement: str = "") -> ToolRequest | None:
    """SSH or Windows top, when the model only wrote a sentence."""
    ask = _original_user_text(messages)
    focus = f"{announcement}\n{ask}"
    wants_ssh = bool(re.search(r"(?i)\bssh\b", focus))
    wants_top = bool(re.search(r"(?i)\btop\b", focus)) and not re.search(r"(?i)\btop story\b", focus)
    if not wants_ssh and not wants_top:
        return None
    history = "\n".join(
        _message_text(message)
        for message in messages or []
        if message.get("role") in {"user", "assistant"} and not _is_harness(_message_text(message))
    )
    blob = f"{focus}\n{history}"
    try:
        computers = store.list_computers()
    except Exception:
        computers = []
    named = [item for item in computers if _mentions(blob, item.get("name") or "")]
    if len(named) != 1:
        return None
    computer = named[0]
    command = "top -b -n 1" if wants_top else _command_from_ask(blob)
    if not command:
        return None
    kind = {"linux": "ssh", "windows": "windows"}.get(computer.get("kind") or "")
    if not kind:
        return None
    _note_heuristic("_implied_remote", f"the reply names {computer.get('name')} and a command to run there")
    return ToolRequest(kind=kind, computer=str(computer.get("name") or ""), command=command)


def _batch_top(command: str) -> str:
    text = " ".join((command or "").split())
    if re.match(r"(?i)top\b", text) and not re.search(r"(?i)(?:^|\s)-b(?:\s|$)", text):
        return "top -b -n 1"
    return (command or "").strip()


def _process_row(line: str) -> tuple[str, str, str] | None:
    """A top process row, including a row whose USER column was cut off."""
    parts = (line or "").split()
    if len(parts) < 5 or not parts[0].isdigit():
        return None
    time_at = None
    for index, part in enumerate(parts):
        if re.fullmatch(r"\d+:\d+(?:\.\d+)?", part):
            time_at = index
    if time_at is None or time_at < 2 or time_at + 1 >= len(parts):
        return None
    cpu, mem = parts[time_at - 2], parts[time_at - 1]
    if not re.fullmatch(r"\d+(?:\.\d+)?", cpu) or not re.fullmatch(r"\d+(?:\.\d+)?", mem):
        return None
    command = parts[-1]
    if command.upper() in {"COMMAND", "CMD"}:
        return None
    return command, cpu, mem


def _busy_process(text: str) -> str:
    """One sentence for top. The header and the process table stay out of the chat."""
    best_cpu = -1.0
    best: tuple[str, str, str] | None = None
    for line in (text or "").splitlines():
        parsed = _process_row(line.strip())
        if not parsed:
            continue
        command, cpu_text, mem = parsed
        try:
            cpu = float(cpu_text)
        except ValueError:
            continue
        if cpu > best_cpu:
            best_cpu = cpu
            best = (command, cpu_text, mem)
    if not best:
        return ""
    command, cpu, mem = best
    return f"{command} is the busiest process, {cpu}% CPU and {mem}% memory."


def _is_raw_command_line(line: str) -> bool:
    """A line of raw tool text, not a sentence about what the tool found."""
    stripped = (line or "").strip()
    if not stripped or stripped.startswith("|"):
        return False
    if stripped.lower().startswith("top -"):
        return True
    if re.match(r"(?i)^(tasks:|%cpu\(|mib mem|kib mem|mib swap|kib swap)\b", stripped):
        return True
    if re.search(r"(?i)\bPID\b", stripped) and re.search(r"(?i)\b(COMMAND|CMD|TIME\+)\b", stripped):
        return True
    return _process_row(stripped) is not None


def _drop_raw_command(answer: str, raw: str) -> str:
    """Take raw command text back out of a reply. Leave the summary."""
    text = answer or ""
    blob = (raw or "").strip()
    if blob and len(blob) >= 40 and blob in text:
        text = text.replace(blob, "")
    raw_lines = {line.strip() for line in blob.splitlines() if len(line.strip()) >= 24}
    dump = len(raw_lines) >= 4
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if _is_raw_command_line(stripped):
            continue
        if dump and stripped in raw_lines:
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _drop_missing_command(answer: str) -> str:
    """A command Windows could not run is not part of a finished reply."""
    kept = [line for line in (answer or "").splitlines() if not _CMD_MISSING.search(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _useful_output(command: str, output: str) -> str:
    """What the person can read. Raw command text is not part of it."""
    text = (output or "").strip()
    if _CMD_MISSING.search(text):
        return ""
    if not text or text == "(no output)" or text.startswith("command produced no output"):
        return text if text.startswith("command produced no output") else "The command returned nothing."
    if re.search(r"(?i)\btop\b", command or "") or "load average" in text.lower():
        return _busy_process(text) or "The command returned nothing."
    rows = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(rows) > 8 or len(text) > 400:
        return f"The command returned {len(rows)} lines."
    return "\n".join(rows)


def _contains_useful(answer: str, useful: str) -> bool:
    if not useful:
        return True
    if useful == "The command returned nothing.":
        return "returned nothing" in (answer or "").lower()
    for line in useful.splitlines():
        probe = line.strip()
        if len(probe) >= 12 and probe in (answer or ""):
            return True
    return False


def _excerpt(raw: str) -> str:
    text = (raw or "").strip()
    if not text or text.startswith("Listed ") or text.startswith("Could not"):
        return ""
    if len(text) > 500:
        text = text[:497].rstrip() + "…"
    return text


def _list_blurb(raw: str) -> str:
    names = _listing_names(raw)
    if not names:
        return "The folder is empty."
    if len(names) <= 6:
        return ", ".join(name.rstrip("/") for name in names)
    return f"{len(names)} names."


_THIN_ANSWER = re.compile(
    r"(?i)^(done|ok|okay|saved it|the command finished|the file is in place|"
    r"the linux computer answered|the windows computer answered|the file was read|"
    r"the folder was listed|the command ran)\.?$"
)
_YES_NO = re.compile(r"(?i)^(yes|no)[.!]?$")


def _remainder(answer: str, lines: list[str]) -> str:
    text = answer or ""
    for line in lines:
        if line:
            text = text.replace(line, "")
    return " ".join(text.split())


def _ensure_shown(answer: str, lines: list[str], steps: list[dict]) -> str:
    """A status line alone is not the answer. Raw command text is not the answer either."""
    text = _drop_missing_command(answer or "")
    for step in reversed(steps):
        if step.get("kind") in {"shell", "ssh", "windows"}:
            text = _drop_raw_command(text, step.get("raw") or "")
            rest = _remainder(text, lines)
            thin = (not rest) or bool(_THIN_ANSWER.match(rest))
            useful = _useful_output(step.get("command") or "", step.get("raw") or "")
            if thin and useful and not _contains_useful(text, useful):
                text = _compose(text, useful)
            break
    rest = _remainder(text, lines)
    if _YES_NO.match(rest):
        return text
    thin = (not rest) or bool(_THIN_ANSWER.match(rest))
    if not thin or not steps:
        return text
    last = steps[-1]
    if last.get("kind") == "files" and last.get("action") == "read":
        excerpt = _excerpt(last.get("raw") or "")
        if excerpt and excerpt not in text:
            text = _compose(text, excerpt)
    elif last.get("kind") == "project" and last.get("action") == "read":
        excerpt = _excerpt(last.get("raw") or "")
        if excerpt and excerpt not in text:
            text = _compose(text, excerpt)
    elif last.get("kind") == "files" and last.get("action") == "list":
        blurb = _list_blurb(last.get("raw") or "")
        if blurb and blurb not in text:
            text = _compose(text, blurb)
    return text


def _complaint_close(lines: list[str], steps: list[dict], messages: list[dict]) -> str:
    if lines:
        return ""
    prior = _previous_visible(messages)
    if prior:
        return prior
    if _complaint_kind(_original_user_text(messages)) == "missing":
        return "The file was not written. There was no path to write."
    return "The command returned nothing."


_LS = re.compile(r"(?i)^ls(?:\s+(.+))?$")
_PWD = re.compile(r"(?i)^pwd$")
_CMD_MISSING = re.compile(
    r"(?i)(?:is not recognized as an internal or external command|operable program or batch file|"
    r"the syntax of the command is incorrect|^\s*\"?NOT FOUND\"?\s*$)"
)


def _ls_listing(command: str) -> str | None:
    """The folder an ls command would list. None when the command is not ls."""
    match = _LS.match(" ".join((command or "").split()))
    if not match:
        return None
    return (match.group(1) or "").strip()


def _windows_rejects(command: str) -> str:
    """ls and pwd are not Windows commands. The name, or empty when the command can run."""
    text = " ".join((command or "").split())
    if _LS.match(text):
        return "ls"
    if _PWD.match(text):
        return "pwd"
    return ""


_SUBTITLE = re.compile(r"\s+(?:—|–|\|)\s+|\s+-\s+")


def _primary_name(raw: str) -> str:
    """The name on the page. A tagline after a dash is not the name."""
    name = " ".join(re.sub(r"(?s)<[^>]+>", " ", raw or "").split())
    name = html.unescape(name)
    name = _SUBTITLE.split(name, maxsplit=1)[0].strip()
    return name


_PLACEHOLDER_HREF = re.compile(
    r"""(?i)\bhref\s*=\s*(?:"\s*#?\s*"|'\s*#?\s*')"""
)
_LINK_CHECK = (
    "A placeholder link is something to fix, the same as a dead link. "
    "That is an href of \"#\", an empty href, or an href that points at an id on this page that is not there."
)
_FRAGMENT_HREF = re.compile(
    r"""(?i)\bhref\s*=\s*["']#([^"'#][^"']*)["']"""
)
_NAMED_ID = re.compile(
    r"""(?i)\b(?:id|name)\s*=\s*["']([^"']+)["']"""
)


def _dead_anchor(html: str) -> bool:
    """An in-page anchor whose id is not in the file. A matching id is a real link."""
    text = html or ""
    ids = {match.group(1).strip().casefold() for match in _NAMED_ID.finditer(text)}
    for match in _FRAGMENT_HREF.finditer(text):
        target = match.group(1).strip().casefold()
        if target and target not in ids:
            return True
    return False


def _placeholder_link(html: str) -> bool:
    """An href of \"#\", an empty href, or an anchor with no matching id."""
    return bool(_PLACEHOLDER_HREF.search(html or "")) or _dead_anchor(html or "")


_IMG_SRC = re.compile(r"""(?i)<img\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""")
_SRCSET = re.compile(r"""(?i)\bsrcset\s*=\s*["']([^"']+)["']""")
_CSS_URL = re.compile(r"""(?i)url\(\s*["']?([^"')]+)["']?\s*\)""")


def _remote_asset_urls(page: str) -> list[str]:
    """http(s) addresses in img src, srcset, and CSS url(). A data URI is not one."""
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        url = (raw or "").strip().split()[0].rstrip(".,);")
        if not url.lower().startswith(("http://", "https://")):
            return
        if url in seen or len(found) >= 12:
            return
        seen.add(url)
        found.append(url)

    text = page or ""
    for match in _IMG_SRC.finditer(text):
        add(match.group(1))
    for match in _SRCSET.finditer(text):
        for part in match.group(1).split(","):
            add(part)
    for match in _CSS_URL.finditer(text):
        add(match.group(1))
    return found


async def _asset_status(url: str) -> int | None:
    """The HTTP status, or None when this computer cannot reach the address."""
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"User-Agent": "EasyAgent"}) as response:
                return response.status_code
    except httpx.HTTPError:
        return None


_ASSET_STATUS: dict[str, int | None] = {}


async def _dead_asset_urls(page: str) -> list[str]:
    """Image and CSS addresses that answered and were not 200. A miss is not a 404."""
    urls = _remote_asset_urls(page)
    if not urls:
        return []
    statuses = await asyncio.gather(*(_asset_status(url) for url in urls))
    for url, status in zip(urls, statuses):
        _ASSET_STATUS[url] = status
    return [url for url, status in zip(urls, statuses) if status is not None and status != 200]


def _drops_dead_asset(previous: str, current: str) -> bool:
    """The new page removed an address that did not return 200. That write stays."""
    removed = set(_remote_asset_urls(previous)) - set(_remote_asset_urls(current))
    return any(_ASSET_STATUS.get(url) not in (None, 200) for url in removed)


def _page_name(path: str) -> str:
    """The name in the title, trimmed at a subtitle separator. A heading is not the name."""
    file = _placed_file(path)
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    title_match = re.search(r"(?is)<title[^>]*>\s*(.*?)\s*</title>", text)
    if not title_match:
        return ""
    title_raw = html.unescape(" ".join(re.sub(r"(?s)<[^>]+>", " ", title_match.group(1)).split()))
    return _primary_name(title_raw) if title_raw else ""


def _path_key(path: str) -> str:
    return (path or "").replace("/", "\\").casefold().rstrip("\\")


def _readback_nudge(path: str) -> str:
    """A file was written and has not been read back. The ready sentence stays hidden."""
    return (
        f"You wrote {path}. Read that file back or run it. "
        "A sentence that it is ready is not the reply."
    )


_PAGE_TAGS = {
    "header", "footer", "main", "section", "article", "div", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "p", "a", "button", "img", "svg", "picture", "nav", "figure",
}
_COPY_TAGS = {"h1", "h2", "h3", "h4", "p"}


class _PageNode:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.children: list[_PageNode] = []


def _page_tree(html: str) -> _PageNode:
    """Tags only. Words, classes, and pictures are not part of the shape."""
    cleaned = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", html or "")
    cleaned = re.sub(r"(?is)<style\b[^>]*>.*?</style>", " ", cleaned)
    root = _PageNode("root")
    stack = [root]
    for match in re.finditer(r"(?i)<(/?)([a-z0-9]+)\b[^>]*?(/?)>", cleaned):
        closing, name, void = match.group(1), match.group(2).lower(), match.group(3)
        if name not in _PAGE_TAGS:
            continue
        if closing:
            while len(stack) > 1 and stack[-1].tag != name:
                stack.pop()
            if len(stack) > 1 and stack[-1].tag == name:
                stack.pop()
            continue
        node = _PageNode(name)
        stack[-1].children.append(node)
        if not void and name not in {"img"}:
            stack.append(node)
    return root


def _node_shape(node: _PageNode) -> tuple:
    return (node.tag, tuple(_node_shape(child) for child in node.children))


def _shape_has_copy(shape: tuple) -> bool:
    if shape[0] in _COPY_TAGS:
        return True
    return any(_shape_has_copy(child) for child in shape[1])


def _contains_tag(node: _PageNode, tag: str) -> bool:
    if node.tag == tag:
        return True
    return any(_contains_tag(child, tag) for child in node.children)


def _has_topic_heading(node: _PageNode) -> bool:
    """A heading for a block under the page title. The page title itself is h1."""
    if node.tag in {"h2", "h3", "h4"}:
        return True
    return any(_has_topic_heading(child) for child in node.children)


def _is_title_block(node: _PageNode) -> bool:
    """The hero: the page title, one paragraph, and a link. Not a second topic."""
    if node.tag in {"h1", "p", "a", "button"}:
        return True
    if node.tag not in {"section", "article", "div", "main", "header"}:
        return False
    if not _contains_tag(node, "h1"):
        return False
    return not _has_topic_heading(node)


def _is_item(node: _PageNode) -> bool:
    """One block in a stack, such as one room. The inner tags do not have to match."""
    if node.tag not in {"section", "article", "div", "li", "figure"}:
        return False
    return _shape_has_copy(_node_shape(node))


def _region_count(node: _PageNode) -> int:
    """A title plus one stack of blocks is one cluster. A second topic is another.

    Three stacked blocks count as one even when their inner tags differ.
    Two topics with different shapes stay two.
    """
    total = 0
    children = node.children
    index = 0
    while index < len(children):
        child = children[index]
        if child.tag in {"footer", "nav"}:
            index += 1
            continue
        if child.tag == "header":
            total += _region_count(child)
            index += 1
            continue
        shape = _node_shape(child)
        end = index + 1
        while end < len(children) and children[end].tag not in {"header", "footer", "nav"} and _node_shape(children[end]) == shape:
            end += 1
        if _is_item(child):
            tag_end = end
            while (
                tag_end < len(children)
                and children[tag_end].tag == child.tag
                and _is_item(children[tag_end])
            ):
                tag_end += 1
            if tag_end - index >= 3:
                end = tag_end
        seen = end - index
        if seen >= 2 and (_shape_has_copy(shape) or all(_is_item(item) for item in children[index:end])):
            total += 1
            index = end
            continue
        if _is_title_block(child):
            total += _region_count(child)
            index = end
            continue
        if child.tag in {"h1", "p", "a", "button", "img", "svg", "picture"}:
            index = end
            continue
        inner = _region_count(child)
        if inner:
            total += inner
        elif child.tag in {"section", "article", "div", "main", "li", "figure"} and _shape_has_copy(shape):
            total += 1
        index = end
    return total


def _plain_page(html: str) -> str:
    visible = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", html or "")
    visible = re.sub(r"(?is)<style\b[^>]*>.*?</style>", " ", visible)
    visible = re.sub(r"(?s)<[^>]+>", " ", visible)
    return " ".join(visible.split())


def _page_parts(node: _PageNode) -> tuple[int, int]:
    """How the page is built: (welcome blocks, stacks of similar cards).

    The hero and the footer are not counted. One welcome plus one stack is one cluster.
    Two different topics are two welcome blocks.
    """
    blurbs = 0
    stacks = 0
    children = node.children
    index = 0
    while index < len(children):
        child = children[index]
        if child.tag in {"footer", "nav"}:
            index += 1
            continue
        if child.tag == "header" or _is_title_block(child):
            inner_blurbs, inner_stacks = _page_parts(child)
            blurbs += inner_blurbs
            stacks += inner_stacks
            index += 1
            continue
        end = index + 1
        while end < len(children) and children[end].tag not in {"header", "footer", "nav"} and _node_shape(children[end]) == _node_shape(child):
            end += 1
        if _is_item(child):
            tag_end = end
            while (
                tag_end < len(children)
                and children[tag_end].tag == child.tag
                and children[tag_end].tag not in {"header", "footer", "nav"}
                and _is_item(children[tag_end])
            ):
                tag_end += 1
            if tag_end - index >= 3:
                end = tag_end
        run = end - index
        if run >= 2 and all(_is_item(item) for item in children[index:end]):
            stacks += 1
            index = end
            continue
        if child.tag in {"h1", "p", "a", "button", "img", "svg", "picture"}:
            index = end
            continue
        inner_blurbs, inner_stacks = _page_parts(child)
        if inner_blurbs or inner_stacks:
            blurbs += inner_blurbs
            stacks += inner_stacks
        elif child.tag in {"section", "article", "div", "main", "li", "figure"} and _shape_has_copy(_node_shape(child)):
            blurbs += 1
        index = end
    return blurbs, stacks


def _is_short_page_draft(html: str) -> bool:
    """A title, a welcome, and one stack of similar blocks. A second topic is not this.

    A picture tag and a byte count are not what this checks. A footer is not required.
    """
    if not re.search(r"(?i)<h1\b", html or ""):
        return False
    blurbs, stacks = _page_parts(_page_tree(html))
    return stacks <= 1 and blurbs <= 1


_PICTURE_URL = re.compile(
    r"""(?ix)
    (?:src|href)\s*=\s*["'](?:https?:)?//[^"']+["']
    | url\(\s*["']?(?:https?:)?//
    | (?:src|href)\s*=\s*["']data:image/[^"']+["']
    | url\(\s*["']?data:image/
    """
)
_DRAWN = re.compile(r"(?i)<(?:rect|circle|ellipse|path|polygon|polyline|line)\b")
_RESEARCH_CACHE: dict[str, str] = {}


def page_has_picture(html: str) -> bool:
    """A real picture URL or a drawn visual in the file. The word picture is not one."""
    text = html or ""
    if _PICTURE_URL.search(text):
        return True
    for match in re.finditer(r"(?is)<svg\b[^>]*>.*?</svg>", text):
        if _DRAWN.search(match.group(0)):
            return True
    return False


def _page_has_real_picture(html: str) -> bool:
    """An inline svg, or an image address that is not a confirmed miss.

    A CSS gradient is not a picture. A url that has not been checked yet is left
    to the dead-picture check. A url that returned 200 counts.
    """
    text = html or ""
    for match in re.finditer(r"(?is)<svg\b[^>]*>.*?</svg>", text):
        if _DRAWN.search(match.group(0)):
            return True
    urls = _remote_asset_urls(text)
    if not urls:
        return False
    for url in urls:
        if url not in _ASSET_STATUS:
            return True
        status = _ASSET_STATUS[url]
        if status == 200 or status is None:
            return True
    return False


def page_has_modern_style(html: str) -> bool:
    """CSS that lays the page out and gives it color or type. A bare tag stack is not this."""
    chunks = re.findall(r"(?is)<style\b[^>]*>(.*?)</style>", html or "")
    chunks += re.findall(r"""(?i)\bstyle\s*=\s*["']([^"']+)["']""", html or "")
    styles = " ".join(chunks)
    if not styles.strip():
        return False
    layout = re.search(r"(?i)display\s*:\s*(?:flex|grid)\b|grid-template|flex-direction", styles)
    visual = re.search(r"(?i)(?:\bcolor|\bbackground|\bfont-size)\s*:", styles)
    return bool(layout and visual)


def page_is_plain(html: str) -> bool:
    """No picture in the file, or no modern styling.

    A styled page with a real picture is not plain. A header, a paragraph, and a stack
    of cards is plain when the file has neither. Mentioning a picture is not one.
    A byte count is not the check.
    """
    if not (html or "").strip():
        return True
    if page_has_picture(html) and page_has_modern_style(html):
        return False
    return True


_DRAWN_PICTURE = """<svg width="640" height="320" viewBox="0 0 640 320" aria-hidden="true">
<rect x="0" y="0" width="640" height="320" fill="#16324a"/>
<circle cx="500" cy="70" r="28" fill="#f4e1c1"/>
<path d="M40 260 L160 120 L280 260 Z" fill="#f6f1e7"/>
<rect x="300" y="160" width="180" height="110" fill="#c4a574"/>
</svg>
"""


def _ensure_drawn_picture(path: str) -> bool:
    """Put a drawn picture in the file when it has none. A gradient is not one."""
    file = _placed_file(path)
    try:
        html = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not html.strip() or page_has_picture(html):
        return False
    if re.search(r"(?i)</body>", html):
        html = re.sub(r"(?i)</body>", _DRAWN_PICTURE + "\n</body>", html, count=1)
    else:
        html = html.rstrip() + "\n" + _DRAWN_PICTURE + "\n"
    try:
        file.write_text(html, encoding="utf-8")
    except OSError:
        return False
    _note_heuristic("_picture", "the file had no picture, so a drawn picture was put in the file")
    return page_has_picture(html)


def describe_written_page(path: str) -> str:
    """What the check says, from the file on disk."""
    file = _placed_file(path)
    try:
        html = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        html = ""
    if not html.strip():
        return f"The file at {path} was checked against the goal. It is empty, so the build continues."
    if page_is_plain(html):
        missing: list[str] = []
        if not page_has_picture(html):
            missing.append("no picture in the file")
        if not page_has_modern_style(html):
            missing.append("no modern styling")
        if _is_short_page_draft(html):
            missing.append("a header, a paragraph, and a stack of cards")
        detail = ", and ".join(missing) if missing else "not the page that was asked for"
        return f"The file at {path} was checked against the goal. It has {detail}, so the build continues."
    name = _page_name(path)
    titled = f"The page is {name}. " if name else ""
    return (
        f"The file at {path} was checked against the goal. {titled}"
        "It has a picture in the file and modern styling."
    )


def _research_blurb_ok(findings: str) -> str:
    text = " ".join((findings or "").split())
    if not text or text == "The search found nothing.":
        return ""
    return (findings or "").strip()[:1500]


async def _research_findings(query: str) -> str:
    """Look up what the page contains. A failed lookup does not invent a page."""
    key = " ".join((query or "").split())
    if not key:
        return ""
    cached = _RESEARCH_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        found = await asyncio.wait_for(search_mod.web_search(key), timeout=5)
    except Exception:
        found = ""
    kept = _research_blurb_ok(found)
    _RESEARCH_CACHE[key] = kept
    return kept


def _notes_dir(store: Store, bot_id: str | None) -> Path | None:
    if not bot_id:
        return None
    try:
        folder = store._bot_dir(bot_id) / "notes"
        folder.mkdir(parents=True, exist_ok=True)
    except (OSError, StoreError):
        return None
    return folder


def _chat_notes_dir(store: Store, bot_id: str | None, chat_id: str | None) -> Path | None:
    """Goal, plan, and check for this chat. MEMORY.md stays beside them, on the bot."""
    base = _notes_dir(store, bot_id)
    if base is None:
        return None
    _ensure_standing_notes(base)
    cleaned = (chat_id or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{8,80}", cleaned):
        return None
    folder = base / "chats" / cleaned
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return folder


def _standing_lines(store: Store, bot_id: str | None) -> list[str]:
    """Memory lines, then MEMORY.md and USER.md. The files are the standing notes."""
    lines: list[str] = []
    if not bot_id:
        return lines
    try:
        for item in store.list_memory(bot_id):
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(text)
    except (StoreError, OSError):
        pass
    folder = store._bot_dir(bot_id) / "notes"
    for name in ("MEMORY.md", "USER.md"):
        path = folder / name
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            lines.append(stripped.lstrip("- ").strip())
    return lines


def _write_note(folder: Path | None, name: str, title: str, body: str) -> None:
    if folder is None:
        return
    try:
        atomic_write_text(folder / name, f"# {title}\n\n{body.strip()}\n")
    except OSError:
        return


def _ensure_standing_notes(folder: Path | None) -> None:
    """MEMORY.md and USER.md stay on disk. An existing note is not overwritten."""
    if folder is None:
        return
    for name, title in (("MEMORY.md", "Memory"), ("USER.md", "User")):
        path = folder / name
        if path.exists():
            continue
        try:
            atomic_write_text(path, f"# {title}\n\n")
        except OSError:
            return


def _lesson_from_fence(body: str) -> tuple[str, str] | None:
    """A short lesson the model marked. A tool fence is not a lesson."""
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if not lines:
        return None
    head = lines[0].lower()
    if head in _MEMORY_ACTIONS:
        return None
    if head == "replace":
        if len(lines) < 3:
            return None
        return lines[1], " ".join(lines[2:])
    return "", " ".join(lines)


def _strip_lesson_fences(text: str) -> str:
    def replacer(match: re.Match[str]) -> str:
        if _lesson_from_fence(match.group(1)) is None:
            return match.group(0)
        return ""

    cleaned = _MEMORY_FENCE.sub(replacer, text or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _worth_saving(lesson: str) -> bool:
    """A short fact that would change a later turn. Chatter is not this."""
    text = " ".join((lesson or "").split())
    if len(text) < 12 or len(text) > 240:
        return False
    if text.endswith("?"):
        return False
    if "<" in text or "```" in text:
        return False
    if re.match(r"(?i)^(noted|ack|filed a line|the page is ready|i looked at the file)\b", text):
        return False
    if re.search(r"(?i)\b(proven|unproven)\b", text):
        return False
    return True


def _memory_md_lines(raw: str) -> tuple[list[str], list[str]]:
    """Header lines, then the bullet facts. Other lines stay in the header block."""
    lines = (raw or "").splitlines()
    header: list[str] = []
    facts: list[str] = []
    started = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            started = True
            fact = " ".join(stripped[2:].split())
            if fact:
                facts.append(fact)
            continue
        if not started:
            header.append(line)
    return header, facts


def _write_memory_md(store: Store, bot_id: str, lesson: str, replaces: str) -> None:
    """Append the lesson to MEMORY.md. Replace only the named line. Leave the rest."""
    folder = _notes_dir(store, bot_id)
    _ensure_standing_notes(folder)
    if folder is None:
        return
    path = folder / "MEMORY.md"
    try:
        raw = path.read_text(encoding="utf-8") if path.is_file() else "# Memory\n\n"
    except OSError:
        return
    header, facts = _memory_md_lines(raw)
    if not header:
        header = ["# Memory", ""]
    old = " ".join((replaces or "").split()).casefold()
    kept: list[str] = []
    swapped = False
    for fact in facts:
        if old and fact.casefold() == old and not swapped:
            kept.append(lesson)
            swapped = True
            continue
        if fact.casefold() == lesson.casefold():
            if lesson.casefold() in {item.casefold() for item in kept}:
                continue
        kept.append(fact)
    if lesson.casefold() not in {item.casefold() for item in kept}:
        kept.append(lesson)
    body = list(header)
    if body and body[-1].strip():
        body.append("")
    body.extend(f"- {fact}" for fact in kept)
    try:
        atomic_write_text(path, "\n".join(body).rstrip() + "\n")
    except OSError:
        return


def keep_lessons(store: Store, bot_id: str | None, text: str) -> str:
    """Write a lesson the model marked. The chat keeps the model's words, not this fence."""
    if not bot_id:
        return text or ""
    for old, lesson in (
        pair
        for pair in (_lesson_from_fence(match.group(1)) for match in _MEMORY_FENCE.finditer(text or ""))
        if pair is not None
    ):
        lesson = " ".join(redact(store, lesson).split())
        old = " ".join(redact(store, old).split())
        if not _worth_saving(lesson):
            continue
        try:
            if old:
                matches = [
                    item for item in store.list_memory(bot_id)
                    if " ".join(str(item.get("text") or "").split()).casefold() == old.casefold()
                ]
                if len(matches) == 1:
                    already = [
                        item for item in store.list_memory(bot_id)
                        if " ".join(str(item.get("text") or "").split()).casefold() == lesson.casefold()
                    ]
                    if already and already[0].get("id") != matches[0].get("id"):
                        store.delete_memory(bot_id, str(matches[0]["id"]))
                    else:
                        store.update_memory(bot_id, str(matches[0]["id"]), lesson)
                else:
                    store.add_memory(bot_id, lesson)
            else:
                store.add_memory(bot_id, lesson)
        except StoreError:
            continue
        _write_memory_md(store, bot_id, lesson, old)
    return _strip_lesson_fences(text)


def _save_turn_notes(
    store: Store,
    bot_id: str | None,
    opening: str,
    check: str = "",
    chat_id: str | None = None,
    goal_text: str | None = None,
) -> None:
    """Goal, thinking, and plan for this chat. A later chat does not overwrite them.

    The goal is the model's own sentence when one was given. A restatement of the ask is not.
    """
    if not opening and not goal_text and not check:
        return
    folder = _chat_notes_dir(store, bot_id, chat_id)
    if folder is None:
        return
    goal, thinking, plan = _split_opening(opening)
    if goal_text is not None:
        goal = goal_text
    else:
        goal = ""
    if goal:
        _write_note(folder, "goal.md", "Goal", goal)
    if thinking:
        _write_note(folder, "thinking.md", "Thinking", thinking)
    if plan:
        _write_note(folder, "plan.md", "Plan", plan)
    if check:
        _write_note(folder, "check.md", "Check", check)


def _split_opening(opening: str) -> tuple[str, str, str]:
    text = opening or ""
    if "Goal:" not in text and "Thinking:" not in text and "\nPlan:" not in text and not text.startswith("Plan:"):
        parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        goal = parts[0] if parts else ""
        thinking = parts[1] if len(parts) > 1 else ""
        plan = "\n\n".join(parts[2:]) if len(parts) > 2 else ""
        return goal, thinking, plan
    goal = ""
    thinking = ""
    plan_lines: list[str] = []
    bucket = ""
    for line in text.splitlines():
        if line.startswith("Goal:"):
            bucket = "goal"
            goal = line[len("Goal:"):].strip()
            continue
        if line.startswith("Thinking:"):
            bucket = "thinking"
            thinking = line[len("Thinking:"):].strip()
            continue
        if line.startswith("Plan:"):
            bucket = "plan"
            continue
        if line.startswith("No question."):
            bucket = ""
            continue
        if bucket == "goal" and line.strip():
            goal = (goal + " " + line.strip()).strip()
        elif bucket == "thinking" and line.strip():
            thinking = (thinking + " " + line.strip()).strip()
        elif bucket == "plan" and line.strip():
            plan_lines.append(line.strip())
    return goal, thinking, "\n".join(plan_lines)


def _draft_nudge(path: str) -> str:
    """The ready sentence stays hidden. The model sees the file that is on disk."""
    file = _placed_file(path)
    try:
        html = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        html = ""
    visible = _plain_page(html)
    if len(visible) > 700:
        visible = visible[:700].rstrip() + "…"
    shown = visible or "(The file is empty.)"
    if not page_has_picture(html):
        gap = (
            "It has no picture. A CSS gradient is not a picture. "
            "Put an image address that returns 200, a CSS url() that returns 200, "
            "or an inline svg in the file."
        )
    elif not page_has_modern_style(html):
        gap = "It has no modern styling. A header, a paragraph, and a stack of cards is not done."
    else:
        gap = "It is not the page that was asked for."
    return (
        f"You wrote {path}. What is on disk is not the page that was asked for.\n\n"
        f"{shown}\n\n"
        f"{gap} Say in your own words what is missing and the line you will change. "
        "Do not ask what is missing. Write the page again. "
        "A sentence that it is ready is not the reply."
    )


def _page_reply(path: str) -> str:
    """Plain words from the file: its name, where it is, and that he can open it."""
    name = _page_name(path)
    if not name:
        return f"The page is at {path}, and you can open it."
    if name[-1] in ".?!":
        return f"The page is {name} It is at {path}, and you can open it."
    return f"The page is {name}. It is at {path}, and you can open it."


def _visible_status(status: str) -> str:
    """Proven, C1, and Evidence stay on the ledger. They are not the reply."""
    kept: list[str] = []
    for line in (status or "").splitlines():
        if re.search(r"(?i)\b(proven|unproven|evidence)\b", line):
            continue
        if re.search(r"(?i)\bC\d+\b", line):
            continue
        if line.strip():
            kept.append(line)
    return "\n".join(kept).strip()


def _page_on_disk(path: str, ask: str) -> bool:
    file = _placed_file(path)
    if not _file_is_present(file):
        return False
    subject = page_subject(ask)
    if not subject:
        return True
    try:
        data = file.read_bytes()
    except OSError:
        return False
    return page_matches(data, subject)


def _deliverable_ready(messages: list[dict], path: str) -> bool:
    """The page or picture is on disk. A sample title does not finish a named page."""
    if _deliverable(messages) is None:
        return False
    return _page_on_disk(path, _original_user_text(messages))


def _prepare_request(request: ToolRequest, messages: list[dict]) -> ToolRequest:
    """A smashed write path is split before it runs. Interactive top becomes one snapshot."""
    ask = _original_user_text(messages)
    if request.kind == "files" and request.action == "write":
        path, body = _clean_write_target(request.path, request.body, ask)
        if _wants_page(ask) and (not _find_path(ask) or _inside_app_folder(path)):
            forced = _target_path(ask, "landing.html", _PAGE_SUFFIXES)
            if not _find_path(ask) or _inside_app_folder(forced):
                name = _split_stored_path(forced)[0] or "landing.html"
                forced = _join_stored(_default_folder(), name)
            if path.replace("/", "\\").lower() != forced.replace("/", "\\").lower():
                folder = _default_folder()
                why = (
                    f"no folder was named, so the page is written under {folder}"
                    if not _find_path(ask)
                    else f"a page does not go in the app folder, so it is written under {folder}"
                )
                _note_heuristic("_prepare_request", why)
            path = forced
        return ToolRequest(kind="files", action="write", path=path, body=body)
    if request.kind in {"shell", "ssh", "windows"}:
        command = _batch_top(request.command)
        if command != request.command:
            return ToolRequest(
                kind=request.kind,
                action=request.action,
                path=request.path,
                command=command,
                computer=request.computer,
                body=request.body,
            )
    return request


def _asked_to_write(messages: list[dict]) -> ToolRequest | None:
    """A file write the person asked for, when the model did not name one."""
    ask = _original_user_text(messages)
    if not re.search(r"(?i)\b(write|create|save)\b", ask):
        return None
    if not re.search(r"(?i)\bfiles?\b", ask):
        return None
    path = _find_path(ask)
    if not path:
        return None
    names = _names_in(ask)
    filename = names[-1] if names else "test-file.txt"
    folded = path.replace("\\", "/").rstrip("/")
    if folded.lower().endswith("/" + filename.lower()) or folded.lower().endswith(filename.lower()):
        target = path
    else:
        sep = "\\" if "\\" in path else "/"
        target = path.rstrip("\\/") + sep + filename
    extra = _text_after_path(ask, path)
    body = (extra + "\n") if extra else "test file\n"
    target, body = _clean_write_target(target, body, ask)
    return ToolRequest(kind="files", action="write", path=target, body=body)


def _owed_search(messages: list[dict], lines: list[str]) -> str:
    """A live fact still needs one web search. A search that already ran does not."""
    if any(line == "Searched the web." for line in lines):
        return ""
    needed = _needed_tool(messages)
    if needed is None or needed.kind != "search" or _ran_needed(needed, lines):
        return ""
    return (needed.body or "").strip()


def _ran_needed(request: ToolRequest | None, lines: list[str]) -> bool:
    if request is None:
        return False
    if request.kind == "search":
        return any(line in {"Searched the web.", "Web search failed."} for line in lines)
    if request.kind == "files" and request.action == "write":
        return bool(_last_write_path(lines))
    if request.kind == "files" and request.action == "list":
        return any(line.startswith("Listed ") or line.startswith("Checked file sizes") for line in lines)
    if request.kind == "files" and request.action == "read":
        return any(line.startswith("Read ") and "memory topic" not in line for line in lines)
    if request.kind == "shell":
        return any(line.startswith("Ran a command on this computer.") for line in lines)
    if request.kind in {"ssh", "windows"}:
        return any(line.startswith(f"Ran a command on {request.computer}.") for line in lines)
    return False


def _story_from_findings(findings: str) -> str:
    """One short story. The raw page, its address, and the numbered dump stay out."""
    snippets: list[str] = []
    for line in (findings or "").splitlines():
        item = re.sub(r"^\d+\.\s*", "", line.strip())
        if not item or item.startswith("http") or item.startswith("[search"):
            continue
        if item.lower().startswith("query:"):
            continue
        snippets.append(item)
    if not snippets:
        return ""
    best = max(snippets, key=len)
    if len(best) > 320:
        best = best[:317].rstrip() + "…"
    return best


def _fallback_after_empty(lines: list[str], raws: list[str], messages: list[dict]) -> str:
    path = _last_write_path(lines)
    if path:
        if path.lower().endswith((".html", ".htm")) and _page_name(path):
            return _page_reply(path)
        return _file_sentence(path)
    if any(line == "Searched the web." for line in lines):
        story = _story_from_findings(raws[-1] if raws else "")
        if story:
            return story
    if any(line.startswith("Listed ") or line.startswith("Checked file sizes") for line in lines):
        return "The folder was listed."
    if any(line.startswith("Read ") for line in lines):
        return "The file was read."
    if any(line.startswith("Ran a command") for line in lines):
        return "The command ran."
    return f"The model returned nothing. Still undone: {_ask_left(messages)}"


def _found_nothing(findings: str) -> bool:
    text = " ".join((findings or "").split()).lower().rstrip(".")
    return text in {"", "the search found nothing"}


def _is_stock_search_reply(text: str) -> bool:
    """A canned search line. It is a tool result, not the reply."""
    folded = " ".join((text or "").split()).lower().rstrip(".")
    if not folded:
        return False
    if folded in {"the search found nothing", "searched the web"}:
        return True
    return folded.startswith("web search failed") or folded.startswith("search failed")


_SEARCH_CAVEAT = re.compile(
    r"(?i)\b("
    r"nothing|no results?|no hits?|came back empty|"
    r"couldn'?t find|could not find|didn'?t find|did not find|"
    r"don'?t have|do not have|can'?t confirm|cannot confirm|"
    r"not sure|no headline|no current|no live|"
    r"i don'?t know|i do not know|"
    r"from what i (?:know|remember)|as far as i know|"
    r"i recall|last i knew|last i checked|"
    r"timed out|could not reach|couldn'?t reach"
    r")\b"
)
_SEARCH_ANSWER_NOTE = (
    "That search result is not the reply. Do not invent a headline, a price, or a version. "
    "Answer in your own words. If nothing came back, or the search failed, say that, "
    "answer from what you know with that limit, or ask a narrower question."
)


def _alternate_search_query(query: str) -> str:
    """One different query after an empty search. It does not invent a hit."""
    text = " ".join((query or "").split())
    if not text:
        return ""
    softer = re.sub(
        r"(?i)\b(right now|today|currently|this morning|this week|at the moment)\b",
        " ",
        text,
    )
    softer = " ".join(softer.split()).strip(" ?.")
    if softer and softer.casefold() != text.casefold():
        return softer[:200]
    if not re.search(r"(?i)\bheadlines?\b", text):
        return f"{text} headlines"[:200]
    return ""


def _empty_search_answer_ok(prose: str, ask: str) -> bool:
    """A real reply after an empty or failed search. A made-up hit is not one."""
    cleaned = " ".join((prose or "").split())
    if not cleaned or _is_stock_search_reply(cleaned):
        return False
    if _asks_the_person(cleaned) or _SEARCH_CAVEAT.search(cleaned):
        return True
    if _wants_search(ask):
        return False
    return True


def _search_needs_another_answer(prose: str, search_miss: bool, ask: str) -> bool:
    if _is_stock_search_reply(prose):
        return True
    return bool(search_miss and not _empty_search_answer_ok(prose, ask))


def _repeats_result(prose: str, raws: list[str]) -> bool:
    """True when the line is a search title or another tool result, not an answer."""
    folded = " ".join((prose or "").split())
    if not folded:
        return False
    for raw in raws:
        for line in (raw or "").splitlines():
            item = re.sub(r"^\d+\.\s*", "", " ".join(line.split()))
            if len(item) > 24 and (folded == item or folded.startswith(item[:48]) or item.startswith(folded)):
                return True
    return False


def _story_or_answer(prose: str, raws: list[str]) -> str:
    """A news answer is the story. An empty reply, a raw page, or an invented headline is not.

    A real sentence that also cites an address stays. A reply that starts with the address does not.
    """
    findings = raws[-1] if raws else ""
    if _found_nothing(findings):
        cleaned = (prose or "").strip()
        if _is_stock_search_reply(cleaned):
            return ""
        return cleaned
    story = _story_from_findings(findings)
    cleaned = (prose or "").strip()
    if not cleaned:
        return story
    first = next((line.strip() for line in cleaned.splitlines() if line.strip()), "")
    if re.match(r"https?://", first):
        return story
    folded = " ".join(cleaned.split())
    if story and (folded == " ".join(story.split()) or _repeats_result(cleaned, raws)):
        return ""
    return cleaned


def _media_for_name(name: str) -> str:
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "html": "text/html",
        "htm": "text/html",
        "txt": "text/plain",
        "md": "text/markdown",
        "css": "text/css",
        "js": "text/javascript",
        "json": "application/json",
    }.get(ext, "text/plain")


def _preview_excerpt(path: Path, media: str) -> str:
    """A short stretch of a text file. Pictures are shown as the image, not as bytes."""
    if media.startswith("image/"):
        return ""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if b"\x00" in raw[:512]:
        return ""
    text = raw.decode("utf-8", errors="replace").strip()
    if media in {"text/html", "text/htm"} or path.suffix.lower() in {".html", ".htm"}:
        text = re.sub(r"(?is)<script\b.*?</script>", " ", text)
        text = re.sub(r"(?is)<style\b.*?</style>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = " ".join(text.split())
    if len(text) > _PREVIEW_CHARS:
        text = text[:_PREVIEW_CHARS].rstrip() + "…"
    return text


def _made_payload(lines: list[str]) -> str:
    """A file this turn wrote and then found on disk. Nothing else gets a preview."""
    path_text = _last_write_path(lines)
    if not path_text:
        return ""
    path = _placed_file(path_text)
    if not _file_is_present(path):
        return ""
    try:
        shown = str(path.resolve())
    except OSError:
        shown = str(path)
    media = _media_for_name(path.name)
    return json.dumps(
        {
            "path": shown,
            "name": path.name,
            "media_type": media,
            "excerpt": _preview_excerpt(path, media),
        },
        ensure_ascii=False,
    )


def _answer_is_failure(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return lowered.startswith("the file was not written") or lowered.startswith("write failed")


def _not_bare(answer: str) -> str:
    """'The file was not written.' with no path and no reason is not a finished reply."""
    text = " ".join((answer or "").split())
    if text.lower().rstrip(".") != "the file was not written":
        return answer
    return "The file was not written. There was no path to write."


def _write_failure_text(request: ToolRequest, exc: Exception) -> str:
    message = str(exc).strip()
    path = (request.path or "").strip()
    if "not written" in message.lower():
        text = message if message.endswith(".") else message + "."
    else:
        text = f"Write failed: {message}"
        if not text.endswith("."):
            text += "."
    if text.lower().rstrip(".") == "the file was not written":
        text = "The file was not written. The write did not finish."
    if path and path not in text:
        text = text[:-1] + f" Path: {path}."
    return text


def _wanted_write(messages: list[dict]) -> bool:
    if _deliverable(messages) is not None or _asked_to_write(messages) is not None:
        return True
    return _complaint_kind(_original_user_text(messages)) == "missing"


def _tool_failed(request: ToolRequest, exc: Exception) -> str:
    message = str(exc).strip()
    if request.kind == "files" and request.action == "write":
        return _write_failure_text(request, exc)
    if request.kind == "files" and request.action == "read":
        name = "Read"
    elif request.kind == "files" and request.action == "list":
        name = "List"
    elif request.kind == "shell":
        name = "Command"
    elif request.kind == "search":
        name = "Web search"
    elif request.kind == "memory":
        name = "Memory"
    elif request.kind == "history":
        name = "History"
    else:
        name = "Tool"
    return f"{name} failed: {exc}"


def _requests_from_native(calls: list[dict]) -> list[ToolRequest]:
    """One request per native call, with the id the model sent.

    A call this computer cannot run is still returned, as kind failed, so the
    next request can answer that id.
    """
    found: list[ToolRequest] = []
    for index, call in enumerate(calls or []):
        cid = str(call.get("id") or "").strip() or f"call_{index}"
        name = str(call.get("name") or "").strip()
        raw_args = call.get("arguments")
        arguments = raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {})
        data = {"name": name, "arguments": arguments}
        try:
            request = _request_from_call(data)
        except ToolError as exc:
            fence = llm.calls_to_fence([data])
            parsed: list[ToolRequest] = []
            if fence:
                try:
                    parsed = parse_tools(fence)
                except ToolError as inner:
                    exc = inner
            request = parsed[0] if parsed else ToolRequest(kind="failed", body=str(exc))
        found.append(replace(
            request,
            call_id=cid,
            call_name=name,
            call_arguments=arguments,
        ))
    return found


def _function_name(request: ToolRequest) -> str:
    if request.call_name:
        return request.call_name
    if request.kind == "files" and request.action == "write":
        return "write_file"
    if request.kind == "files" and request.action == "read":
        return "read_file"
    if request.kind == "files":
        return "list_dir"
    if request.kind == "shell":
        return "terminal"
    if request.kind == "search":
        return "web_search"
    if request.kind == "failed":
        return "tool"
    return request.kind or "tool"


def _function_arguments(request: ToolRequest) -> str:
    if request.call_arguments:
        return request.call_arguments
    if request.kind == "shell":
        payload: dict = {"command": request.command}
    elif request.kind in {"ssh", "windows"}:
        payload = {"computer": request.computer, "command": request.command}
    elif request.kind == "search":
        payload = {"query": request.body}
    elif request.kind == "files":
        payload = {"path": request.path}
        if request.action == "write":
            payload["content"] = request.body
    elif request.kind == "finish":
        payload = {"status": request.action or "proven", "note": request.body}
    elif request.kind == "question":
        payload = {"question": request.body, "choices": list(request.choices)}
    else:
        payload = {"action": request.action, "path": request.path, "command": request.command, "body": request.body}
    return json.dumps(payload)


def _tool_call_object(request: ToolRequest, index: int) -> dict:
    return {
        "id": request.call_id or f"call_{index}",
        "type": "function",
        "function": {
            "name": _function_name(request),
            "arguments": _function_arguments(request),
        },
    }


_NEXT_STEP = re.compile(
    r"(?i)^(i'll|i will|let me|let's|i'm going to|i am going to|going to)\b"
)
_PLAN_FENCE = re.compile(r"```plan[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_PROGRESS_LEAD = re.compile(
    r"(?i)^(writing|saving|putting|looking|checking|reading|running|building|making|creating)\b"
)
_PROGRESS_MID = re.compile(
    r"(?i)\b(let me|i'll|i will|going to|writing it now|writing now|now to)\b"
)
_STEP_VOICE = re.compile(r"(?i)^(i|i'm|i am|i've|i have)\b")
_PROGRESS_WORTH_NOTE = re.compile(r"(?i)\bwriting\b|\bnow to\b|[A-Za-z]:\\")
_LASTING_FACT = re.compile(
    r"(?i)("
    r"\bis not an? \w+ command\b"
    r"|\bis not recognized\b"
    r"|\b(?:do not|don't|never) (?:run|use)\b"
    r"|\bnext time\b"
    r"|\bfrom now on\b"
    r"|\bprefer(?:s|red)\b"
    r"|\bpreference\b"
    r"|\bthe bug was\b"
    r"|\bthe fix (?:is|was)\b"
    r"|\bworth remembering\b"
    r"|\bworking (?:path|folder)\b"
    r"|\b(?:write|writes|written|goes|go) (?:it |pages |files |them )?under\b"
    r")"
)


def _is_needs_list(sentence: str) -> bool:
    """A paste of the plan. The goal is one short sentence, not that list."""
    if sentence.count(",") >= 2:
        return True
    return bool(re.search(r"(?i)\b(needs|need to|should include|must have)\b", sentence) and "," in sentence)


def _is_progress_line(sentence: str) -> bool:
    """A line about the work still happening. That is not what done looks like."""
    if _NEXT_STEP.match(sentence) or _PROGRESS_LEAD.match(sentence):
        return True
    return bool(_PROGRESS_MID.search(sentence))


_DELIVERABLE = re.compile(r"(?i)\b(page|site|website|landing|homepage|webpage)\b")
_GOAL_PATH = re.compile(r"[A-Za-z]:\\[^\s\"']+")
_CHECK_FINDING = re.compile(
    r"(?i)("
    r"\breads? back\b|"
    r"\bfrom disk\b|"
    r"\bon disk\b|"
    r"\bi see\b|"
    r"\bi saw\b|"
    r"\bi found\b|"
    r"\bhref\b|"
    r"\bplaceholder\b|"
    r"\ball good\b|"
    r"\bcleanly\b|"
    r"\blooks right\b|"
    r"\bverified\b"
    r")"
)


def _names_finished_result(sentence: str) -> bool:
    """Names what was built and where it is. A filename alone is not the thing that was built."""
    path = _GOAL_PATH.search(sentence or "")
    if not path:
        return False
    outside = _GOAL_PATH.sub(" ", sentence)
    return bool(_DELIVERABLE.search(outside))


def _goal_sentences(spoken: str):
    for part in re.split(r"\n\s*\n", spoken or ""):
        text = " ".join(part.split())
        if not text:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = sentence.strip()
            if sentence:
                yield sentence


def _goal_line(spoken: str) -> str:
    """The last sentence that says what finished, including where it is.

    A promise, a needs list, or a read-back that does not name that result is not the goal.
    """
    noted = False
    chosen = ""
    for sentence in _goal_sentences(spoken):
        if _is_progress_line(sentence):
            if not noted and _PROGRESS_WORTH_NOTE.search(sentence):
                _note_heuristic("_goal", "a progress line is not the goal")
                noted = True
            continue
        if _is_exact_log(sentence) or sentence.lower() in {"ack", "noted", "noted."}:
            continue
        if _BARE_READY.match(sentence) or _is_needs_list(sentence) or len(sentence) > 180:
            continue
        if _CHECK_FINDING.search(sentence):
            continue
        if _STEP_VOICE.match(sentence) and not _names_finished_result(sentence):
            continue
        if not _names_finished_result(sentence):
            continue
        chosen = sentence
    return chosen


def _turn_marked_a_lesson(text: str) -> bool:
    """True when this reply already marked a lesson in a memory fence."""
    for match in _MEMORY_FENCE.finditer(text or ""):
        pair = _lesson_from_fence(match.group(1))
        if pair is not None and _worth_saving(" ".join(pair[1].split())):
            return True
    return False


def _is_lasting_fact(sentence: str) -> bool:
    """A fact the model stated that would change a later turn. Chatter is not this."""
    if not sentence or _is_progress_line(sentence) or _is_harness_narration(sentence):
        return False
    if not _LASTING_FACT.search(sentence):
        return False
    return _worth_saving(sentence)


def _lasting_sentence(spoken: str) -> str:
    """One sentence the model actually wrote. Empty when this reply learned nothing."""
    cleaned = _strip_lesson_fences(
        strip_tool_markup(search_mod.strip_search_fences(spoken or ""))
    ).strip()
    for part in re.split(r"\n\s*\n", cleaned):
        text = " ".join(part.split())
        if not text:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = sentence.strip()
            if _is_lasting_fact(sentence):
                return sentence
    return ""


def file_lasting_fact(store: Store, bot_id: str | None, spoken: str) -> str:
    """File one lasting sentence in the model's words. A turn that learned nothing writes nothing."""
    if not bot_id or _turn_marked_a_lesson(spoken):
        return ""
    lesson = _lasting_sentence(spoken)
    if not lesson:
        return ""
    lesson = " ".join(redact(store, lesson).split())
    if not _worth_saving(lesson):
        return ""
    _note_heuristic("_memory", "the model stated a lasting fact, so it is filed")
    try:
        store.add_memory(bot_id, lesson)
    except StoreError:
        return ""
    _write_memory_md(store, bot_id, lesson, "")
    return lesson


def _continue_messages(
    messages: list[dict],
    line: str,
    detail: str,
    *,
    page: bool = False,
    voice: str = "",
    requests: list[ToolRequest] | None = None,
    details: list[str] | None = None,
    note: str = "",
) -> list[dict]:
    """Tool results go back as tool messages matched to the assistant tool_calls.

    The order is the one llama.cpp expects: the assistant message carries
    tool_calls, and each tool message follows it with that call's id.
    """
    del line
    if not note:
        if page:
            note = (
                "Continue the account in your own words from this result. "
                "One step, not a wrap-up: what you just checked, what you expected, what you found, "
                "and the next change, including the line you will change. "
                f"{_LINK_CHECK} "
                "If you have not looked at the rest of the file, look at it before you stop. "
                "After a read comes back, say what you found before you stop. "
                "A status line is not that account. "
                "Do not repeat these instructions. Do not paste a search title or the whole file. "
                "Do not ask a question you can decide. Then make the change. "
                "If nothing lasting was learned, do not write a memory note. "
                "If you learned where the file lives, what failed, or a preference, "
                "say so in your own words and put only the short lesson in a memory fence."
            )
        else:
            note = (
                "The tool result is the tool message. The person sees your words, not a log of the tool. "
                "Say what you found. For a command, put the useful lines in the answer. For a short file, say what it says. "
                "For a write, name the file and the folder. Do not paste a long directory listing. "
                "Do not answer with a line that only says a tool ran. "
                "If this answers them, say so. If you still need a file list, a size, a read, a command, or a search, "
                "call that tool. Do not stop after saying you will check. "
                "If they asked whether one file is there, say yes or no about that file. "
                "If they asked for the largest file, answer with that one filename and its size. "
                "Do not paste a directory listing."
            )
    reqs = list(requests or [])
    pieces = list(details) if details is not None else [detail]
    if reqs and len(pieces) < len(reqs):
        pieces = pieces + [pieces[-1] if pieces else ""] * (len(reqs) - len(pieces))
    out = list(messages)
    if reqs:
        calls = [_tool_call_object(req, index) for index, req in enumerate(reqs)]
        content = (voice or "").strip() or None
        out.append({"role": "assistant", "content": content, "tool_calls": calls})
        for call, item in zip(calls, pieces):
            out.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["function"]["name"],
                "content": item if item is not None else "",
            })
    if note:
        out.append({"role": "user", "content": note})
    return out


def _join_lines(lines: list[str]) -> str:
    kept: list[str] = []
    for line in lines:
        if line and (not kept or kept[-1] != line):
            kept.append(line)
    return "\n".join(kept)


_BARE_READY = re.compile(
    r"(?i)^(the page is ready|it'?s ready|it is ready|all set|done|ready)[.!]?$"
)
_COMMAND_NOISE = re.compile(
    r"(?i)(is not recognized|syntax of the command is incorrect|operable program or batch file)"
)


def _keep_spoken_line(line: str) -> bool:
    text = " ".join((line or "").split())
    if not text or _BARE_READY.match(text):
        return False
    if text.lower() in {"ack", "noted", "noted."}:
        return False
    if re.match(r"(?i)^(wrote|read|ran a command|listed|searched the web)\b", text):
        return False
    if re.search(r"(?i)\b(proven|unproven)\b", text):
        return False
    if re.search(r"(?i)\bevidence\s*:", text):
        return False
    if re.search(r"(?i)\bC\d+\b", text):
        return False
    if re.match(r"(?i)^the file was not written\b", text):
        return False
    if _COMMAND_NOISE.search(text):
        return False
    return True


_CODE_SPAN = re.compile(r"```[\s\S]*?```|`[^`\n]+`")
_FULL_DOCUMENT = re.compile(
    r"(?is)<!doctype\s+html\b[^>]*>\s*<html\b[\s\S]{200,}?</html\s*>|<html\b[^>]*>[\s\S]{200,}?</html\s*>"
)


def _drop_page_dump(text: str) -> str:
    """A pasted document is not the account. A cited tag, in prose or in a code span, stays."""
    masks: list[str] = []

    def hide(match: re.Match[str]) -> str:
        masks.append(match.group(0))
        return f"\x00CODE{len(masks) - 1}\x00"

    protected = _CODE_SPAN.sub(hide, text or "")

    def drop_document(match: re.Match[str]) -> str:
        if len(match.group(0)) < 400:
            return match.group(0)
        return "\n"

    protected = _FULL_DOCUMENT.sub(drop_document, protected)
    for index, piece in enumerate(masks):
        protected = protected.replace(f"\x00CODE{index}\x00", piece)
    return protected


def _spoken(text: str) -> str:
    """The model's own account. A tool log, a ready line, or a command error is not the chat."""
    _hidden, source = llm.peel_thinking(text or "")
    cleaned = _strip_lesson_fences(
        _drop_page_dump(strip_tool_markup(search_mod.strip_search_fences(source)))
    ).strip()
    if not cleaned:
        return ""
    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", cleaned):
        lines = [" ".join(line.split()) for line in block.splitlines() if _keep_spoken_line(line)]
        if lines:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _visible_answer(text: str, raws: list[str]) -> str:
    cleaned = strip_tool_markup(search_mod.strip_search_fences(text or ""))
    for raw in raws:
        cleaned = _without_dump(cleaned, raw)
    return cleaned


def _ask_left(messages: list[dict]) -> str:
    ask = " ".join(_original_user_text(messages).split())
    if len(ask) > 140:
        ask = ask[:137].rstrip() + "…"
    return ask or "the request"


def _tool_signature(request: ToolRequest) -> tuple[str, str, str, str, str, str]:
    return (request.kind, request.action, request.path, request.command, request.computer, request.body)


def _write_changes_disk(request: ToolRequest) -> bool:
    """A write whose text is not already the file. A different page is progress."""
    if request.kind != "files" or request.action != "write" or not (request.path or "").strip():
        return False
    file = _placed_file(request.path)
    try:
        current = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return current != (request.body or "")


def _write_is_near(request: ToolRequest, old_sig: tuple) -> bool:
    """Two writes are the same call only when the whole page is a small edit.

    The first 180 characters of a page are the head. A shared head is not the same page.
    """
    if len(old_sig) < 6 or old_sig[0] != "files" or old_sig[1] != "write":
        return False
    if norm_arg(request.computer or "") != norm_arg(old_sig[4] or ""):
        return False
    if not near_arg(norm_arg(request.path or ""), norm_arg(old_sig[2] or "")):
        return False
    if not near_arg(norm_arg(request.command or ""), norm_arg(old_sig[3] or "")):
        return False
    return near_arg(norm_arg(request.body or ""), norm_arg(old_sig[5] or ""))


def _stuck(lines: list[str], messages: list[dict], *, reason: str) -> str:
    del lines
    ask = _ask_left(messages)
    note = f"Stuck. Still undone: {ask}"
    if reason:
        note = f"{note} {reason}"
    return note


def _read_line_path(line: str) -> str:
    if line.startswith("Read ") and line.endswith(".") and "memory topic" not in line:
        return line[len("Read "):-1]
    return ""


def _summary_body(ledger: Ledger) -> str:
    rows: list[str] = []
    for note in ledger.note_paths:
        bit = ledger.note_bits.get(_norm_path(note), "")
        name = note.replace("\\", "/").rsplit("/", 1)[-1]
        rows.append(f"{name}: {bit}".rstrip())
    return "\n".join(rows) + "\n"


def _next_summary_step(ledger: Ledger | None, lines: list[str]) -> ToolRequest | None:
    """The next read, or the summary write, while that file is still unproven."""
    if ledger is None or not ledger.summary_path or ledger.all_proven():
        return None
    for note in ledger.note_paths:
        if any(paths_match(_read_line_path(line), note) for line in lines):
            continue
        return ToolRequest(kind="files", action="read", path=note)
    if "C2" not in ledger.proven:
        return ToolRequest(
            kind="files",
            action="write",
            path=ledger.summary_path,
            body=_summary_body(ledger),
        )
    return None


def _summary_request_is_off(ledger: Ledger, request: ToolRequest | None) -> bool:
    """A stop, or a path that is not one of the named notes or the summary file."""
    if request is None or request.kind == "finish":
        return True
    if request.kind != "files":
        return False
    if request.action == "write":
        return not paths_match(request.path, ledger.summary_path)
    if request.action == "read":
        return not any(paths_match(request.path, note) for note in ledger.note_paths)
    return False


def thought_gap(previous: str, incoming: str, *, new_step: bool = False) -> str:
    """Separator between two thought pieces. Empty when they already join cleanly.

    A new step is its own paragraph. A chunk that starts a new sentence
    ('first' + 'Let', or 'on.' + 'Let') gets a space or a blank line so the
    words do not run together.
    """
    if not previous or not incoming:
        return ""
    if new_step:
        if previous.endswith("\n\n"):
            return ""
        if previous.endswith("\n"):
            return "\n"
        return "\n\n"
    if previous[-1].isspace() or incoming[0].isspace():
        return ""
    if previous[-1] in ".!?" and incoming[0].isalpha():
        return "\n\n"
    if previous[-1].isalnum() and incoming[0].isupper():
        return " "
    return ""


def join_segments(parts: list[str]) -> str:
    """Join reasoning chunks from one model call."""
    text = ""
    for part in parts:
        if not part:
            continue
        if not text:
            text = part
            continue
        text += thought_gap(text, part) + part
    return text


def _retryable_provider(exc: llm.ProviderError) -> bool:
    """A connect failure, a timeout before any token, or a busy server."""
    return retry.retryable_before_token(str(exc))


async def _iter_streamed_model(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
):
    """Yield ('thinking', chunk) as reasoning arrives, and ('text', chunk) for the answer."""
    pending: asyncio.Queue = asyncio.Queue()
    found: list[dict] = []

    async def run() -> None:
        saw_reasoning = False

        def sink(chunk: str) -> None:
            nonlocal saw_reasoning
            if chunk:
                saw_reasoning = True
                pending.put_nowait(("thinking", chunk))

        token = llm.attach_reasoning_sink(sink)
        error: BaseException | None = None
        try:
            async for piece in llm.stream_complete(
                base_url=base_url, api_key=api_key, model=model, messages=messages
            ):
                await pending.put(("text", piece or ""))
        except llm.ProviderError as exc:
            # Reasoning with no answer is not a finished call. A truly empty
            # stream still ends here so the turn can ask once more.
            if str(exc) != "Endpoint returned an empty message." or saw_reasoning:
                error = exc
        except Exception as exc:
            error = exc
        finally:
            found.extend(llm.take_native_calls())
            llm.detach_reasoning_sink(token)
        if error is not None:
            await pending.put(("error", error))
            return
        await pending.put(("end", ""))

    task = asyncio.create_task(run())
    try:
        while True:
            kind, payload = await pending.get()
            if kind == "end":
                llm.restore_native_calls(found)
                return
            if kind == "error":
                llm.restore_native_calls(found)
                raise payload
            if payload:
                yield kind, payload
    finally:
        if not task.done():
            task.cancel()


def _abandon_model_error(exc: llm.ProviderError) -> None:
    """Stop is immediate. A client abort is a stop. Anything else is the caller's choice."""
    if turn_mod.cancelled() or "incomplete chunked read" in str(exc).lower():
        raise turn_mod.TurnCancelled() from exc


async def _pull_model(
    *,
    stream: bool,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
):
    """One model call. Reasoning is its own event. An empty answer is blank.

    The connection's line is held for the request itself. A flaky server is
    retried with the slot released, so another chat can use it during the pause.
    A stream that dies after text has started is replayed once.
    """
    window = retry.Window()
    while True:
        turn_mod.raise_if_cancelled()
        permit = await gate.reserve()
        hold = True
        try:
            if permit.waiting:
                yield "status", permit.label
            await permit.acquire()
            inner = gate.suppress_inner_retry()
            started = False
            bucket: list[str] = []
            try:
                try:
                    if not stream:
                        token = llm.attach_reasoning_sink(bucket.append)
                        try:
                            text = await llm.complete(
                                base_url=base_url, api_key=api_key, model=model, messages=messages
                            )
                        finally:
                            llm.detach_reasoning_sink(token)
                        turn_mod.raise_if_cancelled()
                        if bucket:
                            started = True
                            yield "thinking", join_segments(bucket)
                        if text:
                            started = True
                            yield "text", text
                        return
                    async for kind, piece in _iter_streamed_model(
                        base_url=base_url, api_key=api_key, model=model, messages=messages
                    ):
                        turn_mod.raise_if_cancelled()
                        if kind in {"text", "thinking"} and piece:
                            started = True
                        yield kind, piece
                    turn_mod.raise_if_cancelled()
                    return
                except turn_mod.TurnCancelled:
                    raise
                except llm.ProviderError as exc:
                    _abandon_model_error(exc)
                    if str(exc) == "Endpoint returned an empty message.":
                        if bucket:
                            yield "thinking", join_segments(bucket)
                        raise
                    kind = window.plan(str(exc), started=started)
                    if kind is None:
                        message = window.failure_message(str(exc))
                        if message:
                            raise llm.ProviderError(message) from exc
                        raise
                    label, delay = window.arm(kind, str(exc))
                    yield "status", label
                    if kind == "replay":
                        yield "replay", ""
                    await permit.release()
                    hold = False
                    await retry.pause(delay)
            finally:
                gate.restore_inner_retry(inner)
        finally:
            if hold:
                await permit.release()


def _partial_stream(text_bits: list[str]):
    """Thinking and reply already in hand when the stream stops."""
    partial = "".join(text_bits)
    if not partial.strip():
        return
    hidden, answer = llm.peel_thinking(partial)
    if hidden.strip():
        yield "thinking", hidden
    body = (answer or "").strip()
    if body:
        yield "stage", body


async def run_turn(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
    store: Store,
    stream: bool = False,
    bot_id: str | None = None,
    chat_id: str | None = None,
):
    """Keep one send alive across tools until the work is checked, or the loop is stuck."""
    working = list(messages)
    lines: list[str] = []
    raws: list[str] = []
    steps: list[dict] = []
    nudged = False
    cut_retried = False
    shown = ""
    last_sig: tuple | None = None
    last_batch: tuple | None = None
    seen_results: dict[tuple, str] = {}
    empty_retried = False
    deliver_tries = 0
    page_refusals = 0
    readback_nudges = 0
    draft_holds = 0
    command_fails = 0
    thought_holds = 0
    link_holds = 0
    image_holds = 0
    finding_holds = 0
    answer_holds = 0
    release_retries = 0
    release_writes = 0
    empty_search_retries = 0
    search_speaks = 0
    search_miss = False
    bad_text = ""
    awaiting_finding = False
    stated_now = False
    model_goal_saved = False
    lesson_filed = False
    lesson_marked = False
    remembered_for_goal = ""
    checked_writes: set[str] = set()
    turn_writes: list[str] = []
    ask = _original_user_text(messages)
    ledger = None if is_quick(ask) else Ledger.from_ask(ask)
    success_keys: list[tuple] = []
    bare_held = False
    opening = ""
    notes_text = ""
    streamed = ""
    said: list[str] = []
    thought = ""
    new_thought = True

    def _remember_thought(chunk: str) -> str:
        """Keep reasoning next to the reply. A second copy of the same block is not added."""
        nonlocal thought, new_thought
        if not (chunk or "").strip():
            return ""
        incoming = chunk
        current = thought.strip()
        folded = " ".join(incoming.split())
        have = " ".join(current.split())
        if folded and (
            have == folded
            or have.startswith(folded + " ")
            or current.startswith(incoming.strip() + "\n")
        ):
            new_thought = False
            return ""
        if len(folded) > 24 and folded in have:
            new_thought = False
            return ""
        gap = thought_gap(thought, incoming, new_step=new_thought)
        new_thought = False
        addition = gap + incoming
        thought += addition
        return addition

    def _heard(sentence: str) -> None:
        """Keep the account the model actually wrote. A wrap-up that adds nothing new is not added."""
        text = _end_once((sentence or "").strip())
        if not text:
            return
        if (
            said
            and _only_wrap(text)
            and _is_wrap_up(_last_paragraph(said[-1]))
            and _wrap_adds_nothing(text, said[-1])
        ):
            return
        if not said:
            said.append(text)
            return
        last = said[-1]
        if text == last or last.endswith(text):
            return
        if text.startswith(last):
            said[-1] = text
            return
        said.append(text)

    def _page_voice() -> str:
        return said[-1] if said else ""

    def _visible_chat(tail: str = "") -> str:
        parts = list(said)
        if tail:
            parts.append(tail)
        return "\n\n".join(part for part in parts if part)

    def _show(text: str) -> str:
        if not opening:
            return text or ""
        body = text or ""
        if not body:
            return opening
        if body.startswith(opening):
            return body
        return opening + "\n\n" + body

    def emit_lines() -> str:
        nonlocal shown
        text = _join_lines(lines)
        if text == shown:
            return ""
        extra = text[len(shown):] if text.startswith(shown) else text
        shown = text
        return extra

    def _emit_progress():
        nonlocal streamed
        if not _wants_page(ask):
            return
        text = _visible_chat()
        if text and text != streamed:
            streamed = text
            yield "stage", text

    def _keep_model_goal(spoken: str) -> None:
        """The goal is the latest sentence of what finished, including where it is.

        A read-back does not write the file. A later finished sentence replaces an earlier one.
        """
        nonlocal model_goal_saved
        if not needs_stages(ask):
            return
        goal = _goal_line(spoken)
        if not goal or _is_exact_log(goal) or goal.lower() in {"ack", "noted", "noted."}:
            return
        if remembered_for_goal and remembered_for_goal.casefold() not in goal.casefold():
            note = remembered_for_goal if remembered_for_goal.endswith((".", "!", "?")) else remembered_for_goal + "."
            goal = f"{goal} I'm using this note: {note}"
        _save_turn_notes(store, bot_id, "", chat_id=chat_id, goal_text=goal)
        model_goal_saved = True

    def _keep_model_plan(raw: str) -> None:
        """A plan file exists only when the model wrote one."""
        match = _PLAN_FENCE.search(raw or "")
        if not match or not match.group(1).strip():
            return
        folder = _chat_notes_dir(store, bot_id, chat_id)
        _write_note(folder, "plan.md", "Plan", match.group(1).strip())

    def _with_status(text: str) -> str:
        if ledger is None:
            return text
        status = _visible_status(ledger.status_text())
        if not status or status in (text or ""):
            return text
        return _compose(text, status)

    def _remember(request: ToolRequest) -> None:
        success_keys.append((
            arg_key(request.kind, request.action, request.path, request.command, request.computer, request.body),
            _tool_signature(request),
        ))

    def _near_repeat(request: ToolRequest) -> bool:
        key = arg_key(request.kind, request.action, request.path, request.command, request.computer, request.body)
        sig = _tool_signature(request)
        for old_key, old_sig in success_keys:
            if old_sig == sig:
                continue
            if request.kind == "files" and request.action == "write":
                if _write_is_near(request, old_sig):
                    return True
                continue
            if near_keys(key, old_key):
                return True
        return False

    def _saw(request: ToolRequest, result: str) -> None:
        if ledger is not None:
            ledger.observe(request.kind, request.action, request.path, result or "")

    kept_pages: dict[str, str] = {}

    def _keep_fuller_page(path: str) -> None:
        """A later write that says less does not replace a fuller page from this turn.

        This is not a size cutoff. Any page can be the reply. A shorter overwrite is put back.
        """
        if not path or not _wants_page(ask):
            return
        file = _placed_file(path)
        try:
            current = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        key = _path_key(path)
        best = kept_pages.get(key)
        if best is None:
            kept_pages[key] = current
            return
        if page_subject(ask):
            current_short = page_is_plain(current)
            best_short = page_is_plain(best)
        else:
            current_short = _is_short_page_draft(current)
            best_short = _is_short_page_draft(best)
        # A page that meets the goal replaces a plain one. A plain page does not replace it.
        # Between two pages of the same kind, a shorter overwrite is put back. Length is not a cutoff.
        if best_short and not current_short:
            kept_pages[key] = current
            return
        if _drops_dead_asset(best, current):
            kept_pages[key] = current
            return
        if (current_short and not best_short) or len(current) < len(best):
            try:
                file.write_text(best, encoding="utf-8")
            except OSError:
                return
            _note_heuristic(
                "_keep_fuller",
                "a shorter page does not replace a fuller page already written",
            )
            return
        kept_pages[key] = current

    def _mark_turn(request: ToolRequest) -> None:
        """A write this turn is unchecked until that same file is read back or run."""
        nonlocal awaiting_finding
        if request.kind == "files" and request.action == "write" and request.path:
            turn_writes.append(request.path)
            _keep_fuller_page(request.path)
            return
        if request.kind == "files" and request.action == "read" and request.path:
            key = _path_key(request.path)
            for path in turn_writes:
                if _path_key(path) != key:
                    continue
                if _written_page_is_short(path):
                    continue
                checked_writes.add(_path_key(path))
                if _wants_page(ask) and not stated_now:
                    awaiting_finding = True
            return
        if request.kind not in {"shell", "ssh", "windows"}:
            return
        command = request.command or ""
        folded = command.replace("/", "\\").casefold()
        for path in turn_writes:
            key = _path_key(path)
            name = path.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            if (key and key in folded) or (name and name in command.casefold()):
                checked_writes.add(key)

    def _pending_write() -> str:
        for path in reversed(turn_writes):
            if _path_key(path) not in checked_writes:
                return path
        return ""

    def _written_page_is_short(path: str) -> bool:
        if not _wants_page(ask):
            return False
        file = _placed_file(path)
        try:
            html = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        # A styled page with only gradients is not done. A bare file with no CSS still closes.
        if (
            path.lower().endswith((".html", ".htm"))
            and not _page_has_real_picture(html)
            and (
                page_has_modern_style(html)
                or re.search(r"(?i)gradient\s*\(", html or "")
            )
        ):
            return True
        if not page_subject(ask):
            return False
        return page_is_plain(html)

    def _hold_for_draft() -> str:
        """A page that is not done stays open. A gradient does not close the turn."""
        nonlocal draft_holds, working
        path = _last_write_path(lines)
        if not path or not _written_page_is_short(path):
            return ""
        file = _placed_file(path)
        try:
            html = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            html = ""
        if (
            draft_holds >= 2
            and not page_has_picture(html)
            and (
                page_has_modern_style(html)
                or re.search(r"(?i)gradient\s*\(", html or "")
            )
            and _ensure_drawn_picture(path)
            and not _written_page_is_short(path)
        ):
            _save_turn_notes(store, bot_id, notes_text, describe_written_page(path), chat_id=chat_id)
            return ""
        if not _written_page_is_short(path):
            return ""
        draft_holds += 1
        _note_heuristic(
            "_draft",
            "the file is still missing a picture or modern styling, so the turn keeps going",
        )
        _save_turn_notes(store, bot_id, notes_text, describe_written_page(path), chat_id=chat_id)
        working = [
            *working,
            {"role": "assistant", "content": "Noted."},
            {"role": "user", "content": _draft_nudge(path)},
        ]
        return "hold"

    def _page_is_ready() -> bool:
        path = _last_write_path(lines)
        return bool(
            path
            and _wants_page(ask)
            and _file_is_present(_placed_file(path))
            and not _written_page_is_short(path)
        )

    def _plan_open() -> bool:
        """The last sentence the person can see is still a plan to read or check."""
        return _still_thinking(_page_voice()) or _still_thinking(text or "")

    def _keep_thinking() -> bool:
        """A plan to read or check stays open. The closer does not land on it."""
        nonlocal thought_holds, working
        if thought_holds >= 3 or not _wants_page(ask) or not _page_is_ready() or not _plan_open():
            return False
        thought_holds += 1
        _note_heuristic(
            "_still_thinking",
            "the model is about to read or check, so that sentence does not end the turn",
        )
        path = _last_write_path(lines)
        shown = ""
        if path:
            try:
                html = _placed_file(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                html = ""
            if len(html) > 80000:
                html = html[:80000].rstrip() + "\n[file truncated]"
            if html:
                shown = f"\n\nRead {path}.\n{html}"
        working = [
            *working,
            {"role": "assistant", "content": _page_voice() or strip_tool_markup(text or "") or "Noted."},
            {
                "role": "user",
                "content": (
                    "You are still in the middle of the account. "
                    "You said you would read or check. That sentence is not the end. "
                    "The read result is below. Say what you found and what you do next, in your own words. "
                    f"{_LINK_CHECK} "
                    "A plan is not the reply. Do not paste the whole file. "
                    "If nothing lasting was learned, do not write a memory note. "
                    "If you learned where the file lives, what failed, or a preference, "
                    "say so in your own words and put only the short lesson in a memory fence."
                    f"{shown}"
                ),
            },
        ]
        return True

    def _hold_for_placeholder() -> bool:
        """A placeholder link is still in the file. A wrap-up is not the end."""
        nonlocal link_holds, working
        if link_holds >= 2 or not _wants_page(ask) or not _page_is_ready():
            return False
        path = _last_write_path(lines)
        if not path:
            return False
        try:
            html = _placed_file(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        if not _placeholder_link(html):
            return False
        link_holds += 1
        _note_heuristic(
            "_placeholder_link",
            "a placeholder link is still in the file, so the turn keeps going",
        )
        if len(html) > 80000:
            html = html[:80000].rstrip() + "\n[file truncated]"
        working = [
            *working,
            {"role": "assistant", "content": _page_voice() or strip_tool_markup(text or "") or "Noted."},
            {
                "role": "user",
                "content": (
                    "You are still in the middle of the account. "
                    f"{_LINK_CHECK} "
                    "Say what you found and the line you will change, then change the file. "
                    "A wrap-up is not the end while that link is still there. "
                    "Do not paste the whole file. "
                    "If nothing lasting was learned, do not write a memory note.\n\n"
                    f"Read {path}.\n{html}"
                ),
            },
        ]
        return True

    def _hold_for_readback() -> bool:
        """Withhold the draft. The file was written and has not been read back."""
        nonlocal readback_nudges, working
        pending = _pending_write()
        if not pending or readback_nudges >= 2:
            return False
        readback_nudges += 1
        _note_heuristic(
            "_readback",
            "a file was written and has not been read back, so the turn keeps going",
        )
        working = [
            *working,
            {"role": "assistant", "content": "Noted."},
            {"role": "user", "content": _readback_nudge(pending)},
        ]
        return True

    def _hold_for_finding() -> bool:
        """A read came back. The turn stays open until the model says what it found."""
        nonlocal finding_holds, awaiting_finding, working
        if not awaiting_finding or finding_holds >= 2 or not _wants_page(ask):
            return False
        path = _last_write_path(lines)
        if not path:
            awaiting_finding = False
            return False
        finding_holds += 1
        _note_heuristic(
            "_readback",
            "a read came back and the model has not said what it found, so the turn keeps going",
        )
        try:
            html = _placed_file(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            html = ""
        if len(html) > 80000:
            html = html[:80000].rstrip() + "\n[file truncated]"
        shown_file = f"\n\nRead {path}.\n{html}" if html else ""
        working = [
            *working,
            {"role": "assistant", "content": _page_voice() or "Noted."},
            {
                "role": "user",
                "content": (
                    "You read the file back. Say what you found, in your own words, before you stop. "
                    "A plan is not that finding. "
                    f"{_LINK_CHECK} "
                    "Do not paste the whole file."
                    f"{shown_file}"
                ),
            },
        ]
        return True

    def _hold_for_stale_release(prose: str) -> bool:
        """An older release than the hits is not the answer. Ask for the current one."""
        nonlocal release_writes, working
        if release_writes >= 2 or not _live_release_ask(ask) or not raws:
            return False
        if not _answer_is_stale(prose, "\n".join(raws)):
            return False
        release_writes += 1
        _note_heuristic(
            "_release",
            "the answer cited an older release than the search results, so the turn stays open",
        )
        working = [
            *working,
            {"role": "assistant", "content": (prose or "").strip() or "Noted."},
            {
                "role": "user",
                "content": (
                    "That names an older release than the search results. "
                    "Write the version from those results and what changed, "
                    "and include a link from the results. "
                    "Do not use an older release you already know."
                ),
            },
        ]
        return True

    async def _hold_for_dead_asset() -> bool:
        """An img src or CSS url() that did not return 200. A wrap-up is not the end."""
        nonlocal image_holds, working
        if image_holds >= 2:
            return False
        path = _last_write_path(lines)
        if not path or not path.lower().endswith((".html", ".htm", ".css")):
            return False
        try:
            page = _placed_file(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        dead = await _dead_asset_urls(page)
        if not dead:
            return False
        image_holds += 1
        _note_heuristic(
            "_dead_asset",
            "an image or css url did not return 200, so the turn keeps going",
        )
        listed = "\n".join(dead[:4])
        if len(page) > 80000:
            page = page[:80000].rstrip() + "\n[file truncated]"
        working = [
            *working,
            {"role": "assistant", "content": _page_voice() or strip_tool_markup(text or "") or "Noted."},
            {
                "role": "user",
                "content": (
                    "An image address or a CSS url() in the file did not return 200. "
                    "That is a dead picture, the same as a dead link. "
                    "Replace it or remove it, then say what you changed. "
                    "A wrap-up is not the end while that address is still in the file.\n"
                    f"{listed}\n\nRead {path}.\n{page}"
                ),
            },
        ]
        return True

    async def _hold_for_page_gap() -> bool:
        if _hold_for_placeholder():
            return True
        return await _hold_for_dead_asset()

    async def _defect_continues(prose: str) -> bool:
        if ledger is None or ledger.playbook not in {"research", "decide"} or ledger.defect_rounds >= 2:
            return False
        try:
            verdict = await llm.complete(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=[
                    {"role": "system", "content": BREAKER_PROMPT},
                    {"role": "user", "content": ledger.render() + "\n\nAnswer:\n" + (prose or "")},
                ],
            )
        except llm.ProviderError:
            ledger.used_breaker = True
            return False
        ledger.used_breaker = True
        defect = judge_breaker(verdict)
        if defect is None:
            return False
        ledger.add_defect(defect)
        return True

    def stuck_events(reason: str):
        if _keep_thinking():
            yield "status", "Thinking"
            yield "resume", ""
            return
        if _hold_for_finding():
            yield "status", "Thinking"
            yield "resume", ""
            return
        if _page_is_ready():
            yield from close("")
            return
        final = _show(_with_status(_ensure_shown(_stuck(lines, messages, reason=reason), lines, steps)))
        extra = final[len(shown):] if shown and final.startswith(shown) else final
        if extra:
            yield "delta", extra.lstrip("\n") if shown else extra
        yield "final", final

    def _after_stuck(reason: str):
        """True when a plan to read or check kept the turn open."""
        kept = []
        for event in stuck_events(reason):
            if event[0] == "resume":
                return True, kept
            kept.append(event)
        return False, kept

    def close(answer: str):
        nonlocal streamed
        path = _last_write_path(lines)
        if (
            path
            and _wants_page(ask)
            and _file_is_present(_placed_file(path))
            and not _written_page_is_short(path)
        ):
            check = describe_written_page(path)
            _save_turn_notes(store, bot_id, notes_text, check, chat_id=chat_id)
            body = _end_once(_visible_chat())
            last = _last_paragraph(body)
            closer = _page_reply(path)
            if _names_page(last, path):
                final = body
            elif _only_open_line(last) or _short_done_line(last):
                head = _drop_last_paragraph(body)
                final = f"{head}\n\n{closer}" if head else closer
            else:
                final = f"{body}\n\n{closer}" if body else closer
        else:
            body = _drop_known_logs(answer or "", lines)
            if not body.strip():
                body = _fallback_after_empty(lines, raws, messages)
            final = _show(_with_status(_not_bare(_ensure_shown(_with_file_place(body, lines), lines, steps))))
            final = _drop_known_logs(final, lines)
        if not _answer_is_failure(final):
            payload = _made_payload(lines)
            if payload:
                yield "made", payload
        if final and final != streamed:
            streamed = final
            yield "stage", final
        yield "final", final

    if needs_stages(ask):
        remembered = memory_overlap(ask, _standing_lines(store, bot_id))
        findings = ""
        query = research_query(ask)
        if query:
            findings = await _research_findings(query)
            _note_heuristic(
                "_research",
                "a page is being built, so what that page contains is looked up before a write",
            )
        remembered_for_goal = remembered
        detail = (
            "The chat is the running account you write in your own words as you go. "
            "Say what the page needs, what you just wrote, what is missing, and the next change, "
            "including the line you will change. One step, not a wrap-up. "
            "A sentence that says you will read or check is not the end. "
            "After that read, say what you found and what you do next. "
            "Do not stop while you are still about to look at the rest of the file. "
            "A status line is not that account. "
            "Do not copy a stock paragraph. Do not repeat these instructions. "
            "Do not paste a search title or the whole file. "
            "Do not ask a question you can decide. Pick a name if one was not given. "
            "A CSS gradient is not a picture. If the file has no picture, put an image URL or a drawn svg in the file. "
            "Say what is missing, then change the file. Do not ask the person. "
            "If nothing lasting was learned, do not write a memory note. "
            "If this turn learned a fact that would change a later turn, say so in your own words "
            "and put only the short lesson in a memory fence."
        )
        if remembered:
            detail = f"A standing note: {remembered}\n\n" + detail
        if findings:
            detail = "Research for the work:\n" + findings + "\n\n" + detail
        working = [*working, {"role": "user", "content": detail}]

    while True:
        turn_mod.raise_if_cancelled()
        stated_now = False
        if (
            not lines
            and _complaint_kind(_original_user_text(messages)) == "nothing"
            and _previous_visible(messages)
        ):
            for event in close(_previous_visible(messages)):
                yield event
            return
        if lines or shown:
            yield "status", "Thinking"
        if ledger is not None:
            working = inject_working(working, ledger)
        text = ""
        parts: list[str] | None = None
        text_bits: list[str] = []
        new_thought = True
        thought_at = len(thought)
        try:
            async for kind, piece in _pull_model(
                stream=stream,
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=working,
            ):
                if kind == "thinking":
                    extra = _remember_thought(piece)
                    if extra:
                        yield "thinking", extra
                elif kind == "status":
                    if piece:
                        yield "status", piece
                elif kind == "replay":
                    text_bits.clear()
                    if len(thought) > thought_at:
                        thought = thought[:thought_at]
                    new_thought = True
                    yield "replay", thought
                else:
                    text_bits.append(piece)
        except turn_mod.TurnCancelled:
            for event in _partial_stream(text_bits):
                yield event
            raise
        except llm.ProviderError as exc:
            if str(exc) == "Endpoint returned an empty message." and not "".join(text_bits).strip():
                pass
            else:
                path = _last_write_path(lines)
                if (
                    _retryable_provider(exc)
                    and path
                    and _wants_page(ask)
                    and _file_is_present(_placed_file(path))
                    and not _written_page_is_short(path)
                ):
                    for event in close(""):
                        yield event
                    return
                for event in _partial_stream(text_bits):
                    yield event
                if turn_mod.cancelled() or "incomplete chunked read" in str(exc).lower():
                    raise turn_mod.TurnCancelled() from exc
                raise
        raw_text = "".join(text_bits)
        hidden, answer = llm.peel_thinking(raw_text)
        if hidden:
            extra = _remember_thought(hidden)
            if extra:
                yield "thinking", extra
            text = answer
            parts = ([answer] if answer else []) if stream else None
        else:
            text = raw_text
            parts = text_bits if stream else None
        if text:
            if _turn_marked_a_lesson(text):
                lesson_marked = True
            keep_lessons(store, bot_id, text)
            if not lesson_filed and not lesson_marked:
                if file_lasting_fact(store, bot_id, text):
                    lesson_filed = True
        request = None
        search_call = None
        announced = False
        query = None
        from_blank = False
        from_prose = False
        batched: list[ToolRequest] = []
        native = llm.take_native_calls()
        if _blank_reply(text) and not native:
            if _keep_thinking():
                yield "status", "Thinking"
                continue
            if _hold_for_finding():
                yield "status", "Thinking"
                continue
            from_blank = True
            needed = _needed_tool(messages) or _implied_remote(store, messages, "")
            if needed is None and ledger is not None and ledger.summary_path and not ledger.all_proven():
                needed = _next_summary_step(ledger, lines)
            summary_open = ledger is not None and bool(ledger.summary_path) and not ledger.all_proven()
            if needed is not None and (summary_open or not _ran_needed(needed, lines)):
                if needed.kind == "search":
                    query = needed.body
                else:
                    request = needed
            elif not empty_retried and needed is None:
                empty_retried = True
                working = [
                    *working,
                    {
                        "role": "user",
                        "content": (
                            "You returned nothing. That is not the end of the turn. "
                            "Call the tool this request needs, then answer."
                        ),
                    },
                ]
                yield "status", "Thinking"
                continue
            elif search_miss and search_speaks < 2:
                search_speaks += 1
                _note_heuristic(
                    "_search",
                    "an empty or failed search is not the reply, so the model is asked to answer",
                )
                working = [*working, {"role": "user", "content": _SEARCH_ANSWER_NOTE}]
                yield "status", "Thinking"
                continue
            else:
                for event in close(_fallback_after_empty(lines, raws, messages)):
                    yield event
                return
        else:
            empty_retried = False
            spoken_now = _spoken(text or "")
            if spoken_now:
                _keep_model_goal(spoken_now)
            _keep_model_plan(text or "")
            if _wants_page(ask):
                if spoken_now:
                    _heard(spoken_now)
                    if _states_finding(spoken_now):
                        stated_now = True
                        awaiting_finding = False
                    for event in _emit_progress():
                        yield event
            try:
                found = _requests_from_native(native) if native else parse_tools(text or "")
            except ToolError as exc:
                if (text or "") == bad_text:
                    resume, events = _after_stuck("The tool call could not be run.")
                    for event in events:
                        yield event
                    if resume:
                        continue
                    return
                bad_text = text or ""
                failed = ToolRequest(
                    kind="failed",
                    body=str(exc),
                    call_id="call_0",
                    call_name="tool",
                    call_arguments="{}",
                )
                working = _continue_messages(
                    working,
                    "",
                    str(exc),
                    voice=_spoken(text or ""),
                    requests=[failed],
                    note="That call could not be run. It is not the reply. Call a tool this computer has, or write the answer.",
                )
                yield "status", "Thinking"
                continue
            reacts = [item for item in found if item.kind == "react"]
            found = [item for item in found if item.kind != "react"]
            did_react = False
            for item in reacts:
                if _apply_bot_reaction(store, bot_id, chat_id, item):
                    did_react = True
            if reacts:
                text = strip_tool_markup(text or "")
                parts = None
            if did_react:
                yield "reacted", "1"
            if did_react and not found and not (text or "").strip():
                yield "final", ""
                return
            if len(found) > 1:
                batched = found
            elif len(found) == 1:
                request = found[0]
                if request.kind == "search":
                    query = request.body
                    search_call = request
                    request = None
            else:
                query = search_mod.search_query(text or "")
        if not batched and request is None and not query and _cut_off_tool(text or ""):
            needed = _needed_tool(messages, text or "") or _implied_remote(store, messages, text or "")
            if needed is not None and not _ran_needed(needed, lines):
                from_prose = True
                needed = _filename_from_prose(needed, text or "")
                if needed.kind == "search":
                    query = needed.body
                else:
                    request = needed
            elif not cut_retried:
                cut_retried = True
                working = [
                    *working,
                    {"role": "assistant", "content": "The tool call was cut off."},
                    {
                        "role": "user",
                        "content": "That tool call was cut off. Send the complete call, then continue.",
                    },
                ]
                yield "status", "Thinking"
                continue
            else:
                for event in close(f"The tool call was cut off. Still undone: {_ask_left(messages)}"):
                    yield event
                return
        if (
            not batched
            and request is None
            and not query
            and _is_announcement(text or "")
            and not _is_choice(ask)
        ):
            announced = True
            request = _needed_tool(messages, text or "")
            if request is not None and _ran_needed(request, lines):
                request = None
            if request is None and bot_id:
                request = _implied_project(store, bot_id, text or "", working)
            if request is None and bot_id:
                request = _implied_memory(store, bot_id, text or "", working)
            if request is None:
                remote = _implied_remote(store, messages, text or "")
                if remote is not None and not _ran_needed(remote, lines):
                    request = remote
            if request is None and ledger is not None and ledger.summary_path and not ledger.all_proven():
                request = _next_summary_step(ledger, lines)
            if request is None:
                if _keep_thinking():
                    yield "status", "Thinking"
                    continue
                if _complaint_kind(_original_user_text(messages)):
                    for event in close(_complaint_close(lines, steps, messages)):
                        yield event
                    return
                if nudged:
                    resume, events = _after_stuck("It announced more work and did not call a tool.")
                    for event in events:
                        yield event
                    if resume:
                        continue
                    return
                nudged = True
                working = [
                    *working,
                    {"role": "assistant", "content": strip_tool_markup(text or "")},
                    {
                        "role": "user",
                        "content": (
                            "That only announced the work. Call the tool now. "
                            "Do not stop after saying you will check."
                        ),
                    },
                ]
                yield "status", "Thinking"
                continue
        if not batched and request is None and not query and not from_blank:
            needed = _needed_tool(messages, text or "") or _implied_remote(store, messages, text or "")
            if needed is not None and not _ran_needed(needed, lines):
                needed = _filename_from_prose(needed, text or "")
                from_prose = True
                if needed.kind == "search":
                    query = needed.body
                else:
                    request = needed
        if request is not None and request.kind == "search":
            query = request.body
            request = None
        if batched:
            questions = [item for item in batched if item.kind == "question"]
            finishes = [item for item in batched if item.kind == "finish"]
            work = [
                _prepare_request(item, messages)
                for item in batched
                if item.kind not in {"question", "finish", "react"}
                and not (
                    ledger is not None
                    and ledger.summary_path
                    and item.kind == "files"
                    and item.action == "write"
                    and not paths_match(item.path, ledger.summary_path)
                )
            ]
            if work:
                batch_sig = tuple(_tool_signature(item) for item in work)
                if not any(_write_changes_disk(item) for item in work):
                    if batch_sig == last_batch or any(_near_repeat(item) for item in work):
                        held = _hold_for_draft()
                        if held:
                            yield "status", "Thinking"
                            continue
                        reason = (
                            "It repeated the same tool with the same arguments."
                            if batch_sig == last_batch
                            else "It repeated the same tool with nearly the same arguments."
                        )
                        resume, events = _after_stuck(reason)
                        for event in events:
                            yield event
                        if resume:
                            continue
                        return
                yield "status", status_label(work[0])
                results = await _run_together(store, work, bot_id)
                details: list[str] = []
                added: list[str] = []
                progress = False
                for item, result, error in results:
                    item_sig = _tool_signature(item)
                    if error is not None:
                        detail = _tool_failed(item, error)
                        if item_sig not in seen_results or seen_results[item_sig] != detail:
                            progress = True
                        seen_results[item_sig] = detail
                        details.append(detail)
                        continue
                    raws.append(result or "")
                    steps.append({
                        "kind": item.kind,
                        "action": item.action,
                        "command": item.command or item.body,
                        "raw": result or "",
                        "path": item.path,
                    })
                    if item.kind == "search":
                        line = "Searched the web."
                        detail = (
                            "Web search ran on this computer, not on the phone.\n"
                            f"Query: {item.body}\n\n{result}"
                        )
                    else:
                        line = happened_line(item, working)
                        detail = model_slice(store, item, result or "", working)
                    lines.append(line)
                    added.append(line)
                    if item_sig not in seen_results or seen_results[item_sig] != (result or ""):
                        progress = True
                    seen_results[item_sig] = result or ""
                    _remember(item)
                    _saw(item, result or "")
                    _mark_turn(item)
                    details.append(redact(store, detail))
                last_batch = batch_sig
                last_sig = None
                if not progress:
                    for event in _emit_progress():
                        yield event
                    held = _hold_for_draft()
                    if held:
                        yield "status", "Thinking"
                        continue
                    resume, events = _after_stuck("That step made no progress.")
                    for event in events:
                        yield event
                    if resume:
                        continue
                    return
                working = _continue_messages(
                    working,
                    _join_lines(added),
                    "",
                    details=details,
                    page=_wants_page(ask),
                    voice=_page_voice() if _wants_page(ask) else _spoken(text or ""),
                    requests=work,
                )
                for event in _emit_progress():
                    yield event
                if not questions and not finishes:
                    continue
            if questions:
                request = questions[0]
            elif finishes:
                request = finishes[0]
        steered = False
        if (
            ledger is not None
            and ledger.summary_path
            and not ledger.all_proven()
            and _summary_request_is_off(ledger, request)
        ):
            nxt = _next_summary_step(ledger, lines)
            if nxt is not None:
                _note_heuristic(
                    "_next_summary_step",
                    "the summary file is not proven from the notes, so the turn keeps going",
                )
                request = nxt
                steered = True
        inferred = announced or from_blank or from_prose or steered
        if request is not None and request.kind == "finish" and not query:
            owed = _owed_search(messages, lines)
            if owed:
                _note_heuristic(
                    "_needed_tool",
                    "a live fact is not answered until the web is searched",
                )
                query = owed
                search_call = ToolRequest(
                    kind="search",
                    body=owed,
                    call_id=request.call_id or "call_0",
                    call_name="web_search",
                    call_arguments=json.dumps({"query": owed}),
                )
                request = None
        if request is not None and request.kind == "finish":
            written = _drop_known_logs(_spoken(text or ""), lines).strip()
            if written and _repeats_result(written, raws):
                written = ""
            if not written and not said and answer_holds < 2:
                answer_holds += 1
                _note_heuristic(
                    "_finish",
                    "a finish with no answer is not an ending, so the model is asked to write it",
                )
                working = _continue_messages(
                    working,
                    "",
                    "Finish is not an ending until you write the answer in your own words. A search title is not the answer.",
                    voice=_spoken(text or ""),
                    requests=[request],
                    note="Write the answer in your own words. Do not paste a search title or a tool result.",
                )
                yield "status", "Thinking"
                continue
            if not written and not said and not _last_write_path(lines):
                for event in close(
                    f"The model returned nothing. Still undone: {_ask_left(messages)}"
                ):
                    yield event
                return
            if written and _hold_for_stale_release(written):
                yield "status", "Thinking"
                continue
            held = _hold_for_draft()
            if held:
                yield "status", "Thinking"
                continue
            path = _last_write_path(lines)
            if (
                _wants_page(ask)
                and path
                and _file_is_present(_placed_file(path))
                and not _written_page_is_short(path)
            ):
                if _keep_thinking():
                    yield "status", "Thinking"
                    continue
                if await _hold_for_page_gap():
                    yield "status", "Thinking"
                    continue
                if _hold_for_finding():
                    yield "status", "Thinking"
                    continue
                for event in close(_drop_known_logs(_spoken(text or ""), lines)):
                    yield event
                return
            if _wants_page(ask) and page_subject(ask) and path and not _written_page_is_short(path):
                if _keep_thinking():
                    yield "status", "Thinking"
                    continue
                if await _hold_for_page_gap():
                    yield "status", "Thinking"
                    continue
                if _hold_for_finding():
                    yield "status", "Thinking"
                    continue
                for event in close(_drop_known_logs(_spoken(text or ""), lines)):
                    yield event
                return
            if _hold_for_readback():
                yield "status", "Thinking"
                continue
            if _pending_write():
                for event in close(_drop_known_logs(_spoken(text or ""), lines)):
                    yield event
                return
            if is_bare_not_written(request.body) and not bare_held:
                bare_held = True
                working = [
                    *working,
                    {"role": "assistant", "content": "The file was not written."},
                    {
                        "role": "user",
                        "content": "That sentence is not a finish. Check the file on disk or name a real blocker.",
                    },
                ]
                yield "status", "Thinking"
                continue
            if ledger is not None:
                ledger.note_finish(request.action, request.body)
            if await _defect_continues(request.body):
                last = ledger.defects[-1]
                working = [
                    *working,
                    {"role": "assistant", "content": "Noted."},
                    {
                        "role": "user",
                        "content": (
                            f"The breaker found a defect. {last.line}: {last.detail} "
                            f"Evidence: {last.evidence}"
                        ),
                    },
                ]
                yield "status", "Thinking"
                continue
            for event in close(_drop_known_logs(_spoken(text or ""), lines)):
                yield event
            return
        if request is not None and request.kind == "question":
            path = _last_write_path(lines)
            if _wants_page(ask) and path and _file_is_present(_placed_file(path)):
                _note_heuristic(
                    "_ask",
                    "the check already names the gap, so the turn does not stop to ask",
                )
                held = _hold_for_draft()
                if _page_is_ready():
                    if await _hold_for_page_gap():
                        yield "status", "Thinking"
                        continue
                    if _hold_for_finding():
                        yield "status", "Thinking"
                        continue
                    for event in close(""):
                        yield event
                    return
                if held:
                    yield "status", "Thinking"
                    continue
            if needs_stages(ask) and not only_the_user_knows(request.body, request.choices):
                _note_heuristic("_ask", "the model can decide that, so the turn does not stop to ask")
                working = [
                    *working,
                    {"role": "assistant", "content": "Noted."},
                    {
                        "role": "user",
                        "content": (
                            "You can decide that. Pick one and say the choice in the work. "
                            "Do not ask. Then do the task."
                        ),
                    },
                ]
                yield "status", "Thinking"
                continue
            settled = _ask(store, request)
            yield "status", "Asking"
            yield "choices", json.dumps(list(settled.choices))
            final = _show(_with_status(_drop_known_logs(settled.text, lines)))
            payload = _made_payload(lines)
            if payload and not _answer_is_failure(final):
                yield "made", payload
            if opening:
                if final and final != streamed:
                    streamed = final
                    yield "stage", final
                yield "final", final
                return
            if final != shown:
                extra = final[len(shown):] if final.startswith(shown) else final
                if extra:
                    yield "delta", extra
            yield "final", final
            return
        if query:
            sig = ("search", query)
            if search_call is not None:
                search_request = search_call
            else:
                search_request = ToolRequest(
                    kind="search",
                    body=query,
                    call_id="call_0",
                    call_name="web_search",
                    call_arguments=json.dumps({"query": query}),
                )
            if sig == last_sig:
                resume, events = _after_stuck("It repeated the same tool with the same arguments.")
                for event in events:
                    yield event
                if resume:
                    continue
                return
            if _near_repeat(search_request):
                resume, events = _after_stuck("It repeated the same tool with nearly the same arguments.")
                for event in events:
                    yield event
                if resume:
                    continue
                return
            try:
                findings = await search_mod.web_search(query)
            except SearchError as exc:
                detail = f"Web search failed: {exc}"
                last_sig = sig
                seen_results[sig] = detail
                search_miss = True
                lines.append("Web search failed.")
                working = _continue_messages(
                    working,
                    "",
                    detail,
                    voice=_spoken(text or ""),
                    requests=[search_request],
                    note="The search failed. That failure is not the reply. Try another query, or write the answer from what you have.",
                )
                yield "status", "Thinking"
                continue
            if _found_nothing(findings) and empty_search_retries < 1:
                alternate = _alternate_search_query(query)
                if alternate and alternate.casefold() != query.casefold():
                    empty_search_retries += 1
                    _note_heuristic(
                        "_search",
                        "the search found nothing, so a different query runs once",
                    )
                    try:
                        newer = await search_mod.web_search(alternate)
                    except SearchError:
                        newer = ""
                    if newer and not _found_nothing(newer):
                        findings = newer
                        query = alternate
            search_miss = _found_nothing(findings)
            line = "Searched the web."
            lines.append(line)
            raws.append(findings)
            steps.append({"kind": "search", "action": "", "command": query, "raw": findings, "path": ""})
            if (
                _live_release_ask(ask)
                and release_retries < 1
                and _hits_look_old(findings)
            ):
                release_retries += 1
                follow = _release_query(ask, followup=True)
                _note_heuristic(
                    "_release",
                    "the first hits are an older release, so the official current release is searched once more",
                )
                try:
                    newer = await search_mod.web_search(follow)
                except SearchError:
                    newer = ""
                if newer and not _found_nothing(newer):
                    lines.append("Searched the web.")
                    raws.append(newer)
                    steps.append({
                        "kind": "search",
                        "action": "",
                        "command": follow,
                        "raw": newer,
                        "path": "",
                    })
                    findings = newer
                    query = follow
            if sig in seen_results and seen_results[sig] == findings:
                for event in _emit_progress():
                    yield event
                resume, events = _after_stuck("That step made no progress.")
                for event in events:
                    yield event
                if resume:
                    continue
                return
            seen_results[sig] = findings
            last_sig = sig
            _remember(search_request)
            _saw(search_request, findings)
            detail = f"Web search ran on this computer, not on the phone.\nQuery: {query}\n\n{findings}"
            note = ""
            if search_miss:
                note = (
                    "The search returned no results. That is not the reply. "
                    "Do not invent a headline, a price, or a version. "
                    "Answer in your own words: say that nothing came back, "
                    "answer from what you know with that limit, or ask a narrower question."
                )
            working = _continue_messages(
                working,
                line,
                redact(store, detail),
                page=_wants_page(ask),
                voice=_page_voice() if _wants_page(ask) else _spoken(text or ""),
                requests=[search_request],
                note=note,
            )
            for event in _emit_progress():
                yield event
            continue
        if request is None:
            if await _hold_for_dead_asset():
                yield "status", "Thinking"
                continue
            prose = _visible_answer(text or "", raws)
            if any(line == "Searched the web." for line in lines):
                prose = _story_or_answer(prose, raws)
            if _search_needs_another_answer(prose, search_miss, ask):
                if search_speaks < 2:
                    search_speaks += 1
                    _note_heuristic(
                        "_search",
                        "an empty or failed search is not the reply, so the model is asked to answer",
                    )
                    working = [
                        *working,
                        {"role": "assistant", "content": strip_tool_markup(text or "") or "Noted."},
                        {"role": "user", "content": _SEARCH_ANSWER_NOTE},
                    ]
                    yield "status", "Thinking"
                    continue
                prose = ""
            if _hold_for_stale_release(prose):
                yield "status", "Thinking"
                continue
            prose = _with_file_place(prose, lines)
            held = _hold_for_draft()
            if held:
                yield "status", "Thinking"
                continue
            path = _last_write_path(lines)
            if (
                _wants_page(ask)
                and path
                and _file_is_present(_placed_file(path))
                and not _written_page_is_short(path)
            ):
                if _keep_thinking():
                    yield "status", "Thinking"
                    continue
                if await _hold_for_page_gap():
                    yield "status", "Thinking"
                    continue
                if _hold_for_finding():
                    yield "status", "Thinking"
                    continue
                for event in close(""):
                    yield event
                return
            if _wants_page(ask) and page_subject(ask) and path and not _written_page_is_short(path):
                if _keep_thinking():
                    yield "status", "Thinking"
                    continue
                if await _hold_for_page_gap():
                    yield "status", "Thinking"
                    continue
                if _hold_for_finding():
                    yield "status", "Thinking"
                    continue
                for event in close(""):
                    yield event
                return
            if _hold_for_readback():
                yield "status", "Thinking"
                continue
            if _hold_for_finding():
                yield "status", "Thinking"
                continue
            if _pending_write():
                for event in close(""):
                    yield event
                return
            if ledger is not None and not ledger.all_proven() and is_bare_not_written(prose):
                if not bare_held:
                    bare_held = True
                    working = [
                        *working,
                        {"role": "assistant", "content": "The file was not written."},
                        {
                            "role": "user",
                            "content": "That sentence is not a finish. Check the file on disk or name a real blocker.",
                        },
                    ]
                    yield "status", "Thinking"
                    continue
                prose = "The file was not written. There was no path to write."
            elif await _defect_continues(prose):
                last = ledger.defects[-1]
                working = [
                    *working,
                    {"role": "assistant", "content": strip_tool_markup(prose or "") or "Noted."},
                    {
                        "role": "user",
                        "content": (
                            f"The breaker found a defect. {last.line}: {last.detail} "
                            f"Evidence: {last.evidence}"
                        ),
                    },
                ]
                yield "status", "Thinking"
                continue
            prose = _drop_known_logs(prose, lines)
            if not prose.strip():
                prose = _fallback_after_empty(lines, raws, messages)
            final = _show(_with_status(_not_bare(_ensure_shown(prose, lines, steps))))
            final = _drop_known_logs(final, lines)
            if not _answer_is_failure(final):
                payload = _made_payload(lines)
                if payload:
                    yield "made", payload
            if opening:
                if final and final != streamed:
                    streamed = final
                    yield "stage", final
                yield "final", final
                return
            if parts is not None and not _join_lines(lines) and final == (text or ""):
                for piece in parts:
                    if piece:
                        yield "delta", piece
            elif not shown:
                if final:
                    yield "delta", final
            elif final.startswith(shown):
                extra = final[len(shown):]
                if extra:
                    yield "delta", extra
            elif final:
                yield "delta", final
            yield "final", final
            return
        request = _prepare_request(request, messages)
        rejected = _windows_rejects(request.command) if request.kind == "shell" and sys.platform == "win32" else ""
        if rejected and _wants_page(ask) and not _last_write_path(lines):
            _note_heuristic("_prepare_request", f"{rejected} is not a Windows command, so it was not run")
            working = _continue_messages(
                working,
                "",
                f"{rejected} is not a Windows command. It was not run.",
                page=True,
                voice=_page_voice(),
                requests=[request],
                note=(
                    f"{rejected} is not a Windows command. It was not run. "
                    f"That is not the reply. Write the page to {deliverable_file('landing.html')}."
                ),
            )
            yield "status", "Thinking"
            continue
        if rejected == "ls":
            folder = _ls_listing(request.command) or _find_path(ask) or "."
            _note_heuristic("_prepare_request", "ls is not a Windows command, so the folder is listed")
            request = ToolRequest(kind="files", action="list", path=folder)
        elif rejected:
            _note_heuristic("_prepare_request", f"{rejected} is not a Windows command, so it was not run")
            working = _continue_messages(
                working,
                "",
                f"{rejected} is not a Windows command. It was not run.",
                voice=_spoken(text or ""),
                requests=[request],
                note=f"{rejected} is not a Windows command. It was not run. That is not the reply.",
            )
            yield "status", "Thinking"
            continue
        subject = page_subject(ask)
        if (
            request.kind == "files"
            and request.action == "write"
            and subject
            and (request.body or "") == _landing_html(subject)
        ):
            _note_heuristic("_prepare_request", "the page comes from the model, so a template is not written")
            if inferred:
                for event in close(""):
                    yield event
                return
            page_refusals += 1
            if page_refusals > 2:
                for event in close("The page was not written."):
                    yield event
                return
            working = [
                *working,
                {"role": "assistant", "content": "That page was not written."},
                {"role": "user", "content": "That text is a sample. Write the page yourself."},
            ]
            yield "status", "Thinking"
            continue
        sig = _tool_signature(request)
        if sig == last_sig and _complaint_kind(_original_user_text(messages)):
            for event in close(_complaint_close(lines, steps, messages)):
                yield event
            return
        if (sig == last_sig or _near_repeat(request)) and not _write_changes_disk(request):
            held = _hold_for_draft()
            if held:
                yield "status", "Thinking"
                continue
            reason = (
                "It repeated the same tool with the same arguments."
                if sig == last_sig
                else "It repeated the same tool with nearly the same arguments."
            )
            resume, events = _after_stuck(reason)
            for event in events:
                yield event
            if resume:
                continue
            return
        yield "status", status_label(request)
        try:
            result = await execute(store, request, bot_id)
        except ToolError as exc:
            if request.kind == "files" and request.action == "write" and _wanted_write(messages):
                rich = _write_failure_text(request, exc)
                deliver_tries += 1
                if deliver_tries < 2:
                    working = [
                        *working,
                        {"role": "assistant", "content": rich},
                        {
                            "role": "user",
                            "content": (
                                "The write failed: "
                                f"{rich} That is not the end of the turn. Write the file again."
                            ),
                        },
                    ]
                    yield "status", "Thinking"
                    continue
                for event in close(rich):
                    yield event
                return
            owed = _deliverable(messages)
            if (
                owed is not None
                and _wants_page(ask)
                and not _last_write_path(lines)
                and not (request.kind == "files" and request.action == "write")
            ):
                _note_heuristic(
                    "_prepare_request",
                    "a command failed and the page is not written yet, so the turn keeps going",
                )
                last_sig = sig
                seen_results[sig] = str(exc)
                working = _continue_messages(
                    working,
                    "",
                    str(exc),
                    page=True,
                    voice=_page_voice(),
                    requests=[request],
                    note=(
                        "That command did not run. It is not the reply. "
                        f"Write the page to {deliverable_file('landing.html')}."
                    ),
                )
                yield "status", "Thinking"
                continue
            if (
                owed is not None
                and not _deliverable_ready(messages, owed.path)
                and not (request.kind == "files" and request.action == "write")
            ):
                _note_heuristic(
                    "_deliverable",
                    "a command failed and the page is not the one that was asked for, so the page is written",
                )
                request = owed
                inferred = True
                try:
                    result = await execute(store, request, bot_id)
                except ToolError as write_exc:
                    rich = _write_failure_text(request, write_exc)
                    deliver_tries += 1
                    if deliver_tries < 2:
                        working = [
                            *working,
                            {"role": "assistant", "content": rich},
                            {
                                "role": "user",
                                "content": (
                                    "The write failed: "
                                    f"{rich} That is not the end of the turn. Write the file again."
                                ),
                            },
                        ]
                        yield "status", "Thinking"
                        continue
                    for event in close(rich):
                        yield event
                    return
                line = happened_line(request, working)
                lines.append(line)
                raws.append(result or "")
                steps.append({
                    "kind": request.kind,
                    "action": request.action,
                    "command": request.command or request.body,
                    "raw": result or "",
                    "path": request.path,
                })
                _remember(request)
                _saw(request, result or "")
                _mark_turn(request)
                for event in _emit_progress():
                    yield event
                working = _continue_messages(
                    working,
                    line,
                    result or "",
                    page=_wants_page(ask),
                    voice=_page_voice() if _wants_page(ask) else _spoken(text or ""),
                    requests=[request],
                )
                yield "status", "Thinking"
                continue
            failed = str(exc)
            if (
                request.kind in {"shell", "ssh", "windows"}
                and _wants_page(ask)
                and _last_write_path(lines)
                and (_COMMAND_NOISE.search(failed) or _page_is_ready())
            ):
                _note_heuristic(
                    "_command_failed",
                    "a command failed, so the failure goes back to the model and the turn keeps going",
                )
                command_fails += 1
                if command_fails > 3 and _page_is_ready():
                    for event in close(""):
                        yield event
                    return
                last_sig = sig
                seen_results[sig] = failed
                working = _continue_messages(
                    working,
                    "",
                    failed,
                    page=True,
                    voice=_page_voice(),
                    requests=[request],
                    note="That command failed. The failure is not the reply. Continue in your own words.",
                )
                yield "status", "Thinking"
                continue
            detail = _tool_failed(request, exc)
            last_sig = sig
            seen_results[sig] = detail
            working = _continue_messages(
                working,
                "",
                detail,
                page=_wants_page(ask),
                voice=_page_voice() if _wants_page(ask) else _spoken(text or ""),
                requests=[request],
                note="That tool failed. The failure is not the reply. Change the call, or write the answer.",
            )
            yield "status", "Thinking"
            continue
        raws.append(result)
        steps.append({
            "kind": request.kind,
            "action": request.action,
            "command": request.command,
            "raw": result,
            "path": request.path,
        })
        line = happened_line(request, working)
        lines.append(line)
        if sig in seen_results and seen_results[sig] == result:
            if _keep_thinking():
                yield "status", "Thinking"
                continue
            for event in _emit_progress():
                yield event
            held = _hold_for_draft()
            if held:
                yield "status", "Thinking"
                continue
            resume, events = _after_stuck("That step made no progress.")
            for event in events:
                yield event
            if resume:
                continue
            return
        seen_results[sig] = result
        last_sig = sig
        _remember(request)
        _saw(request, result)
        _mark_turn(request)
        detail = redact(store, model_slice(store, request, result, working))
        working = _continue_messages(
            working,
            line,
            detail,
            page=_wants_page(ask),
            voice=_page_voice() if _wants_page(ask) else _spoken(text or ""),
            requests=[request],
        )
        for event in _emit_progress():
            yield event
        if announced:
            nudged = False


async def complete_with_tools(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
    store: Store,
    bot_id: str | None = None,
    chat_id: str | None = None,
) -> Settled:
    """Run tools and keep calling the model until it answers, or the loop is stuck."""
    final = ""
    choices: tuple[str, ...] = ()
    made = ""
    thought = ""
    reacted = False
    async for kind, text in run_turn(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        store=store,
        bot_id=bot_id,
        chat_id=chat_id,
    ):
        if kind == "final":
            final = text
        elif kind == "made":
            made = text
        elif kind == "thinking":
            thought += text
        elif kind == "reacted":
            reacted = True
        elif kind == "choices":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                choices = tuple(item for item in parsed if isinstance(item, str))
    return Settled(final, choices, made, thought.strip(), reacted)


async def stream_with_tools(
    *,
    base_url: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict],
    store: Store,
    bot_id: str | None = None,
    chat_id: str | None = None,
):
    """Yield stream events, then ('final', text). The turn stays open across tools."""
    async for kind, text in run_turn(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        store=store,
        stream=True,
        bot_id=bot_id,
        chat_id=chat_id,
    ):
        yield kind, text
