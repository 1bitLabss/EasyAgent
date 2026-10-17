"""One registry for the native tools.

Every tool has a name, a JSON schema, and a description. Disabling a tool
removes it from the prompt and blocks the call, including an alias. Argument
coercion follows the schema: a string ``"20"`` for an integer ``limit`` becomes
``20``, and an ambiguous value stays as the model sent it.
"""

from __future__ import annotations

import json
import threading
from typing import Any

_LOCK = threading.Lock()
_DISABLED: set[str] = set()

# Aliases the dispatcher already accepts, mapped to the registered name.
_ALIASES = {
    "read": "read_file",
    "cat": "read_file",
    "write": "write_file",
    "save_file": "write_file",
    "create_file": "write_file",
    "list": "list_dir",
    "list_directory": "list_dir",
    "ls": "list_dir",
    "apply_patch": "patch",
    "search_replace": "patch",
    "grep": "search_files",
    "rg": "search_files",
    "shell": "terminal",
    "bash": "terminal",
    "execute": "terminal",
    "execute_command": "terminal",
    "run_command": "terminal",
    "exec": "terminal",
    "search": "web_search",
    "duckduckgo": "web_search",
    "fetch": "fetch_page",
    "browse": "browser",
    "run_ssh": "ssh",
    "run_windows": "windows",
    "project_file": "project",
    "memory_topic": "memory",
    "chat_history": "history",
    "recall": "history",
    "search_history": "history",
    "ask": "question",
    "reaction": "react",
    "tapback": "react",
    "connector": "mcp",
}

# kind (+ action for files) of a ToolRequest, mapped to the registered name.
_KIND_TOOLS = {
    "search_files": "search_files",
    "shell": "terminal",
    "ssh": "ssh",
    "windows": "windows",
    "project": "project",
    "memory": "memory",
    "history": "history",
    "react": "react",
    "plan": "plan",
    "browser": "browser",
    "mcp": "mcp",
    "fetch": "fetch_page",
    "research": "research",
    "search": "web_search",
    "question": "question",
    "finish": "finish",
    "delegate": "delegate_task",
    "skill": "skill",
}


def canonical(name: str) -> str:
    """The registered name for a call name or an alias."""
    text = (name or "").strip()
    if not text:
        return ""
    folded = text.lower().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(folded, folded)


def tool_name_for(kind: str, action: str = "") -> str:
    """The registered tool a request would run. Empty when it is not a native tool."""
    if kind == "files":
        act = (action or "").strip().lower()
        if act == "write":
            return "write_file"
        if act == "patch":
            return "patch"
        if act == "read":
            return "read_file"
        return "list_dir"
    return _KIND_TOOLS.get(kind or "", "")


def entries() -> list[dict]:
    """Every native tool: name, schema, description."""
    from easyagent.llm import NATIVE_TOOLS

    found = []
    for tool in NATIVE_TOOLS:
        function = tool.get("function") or {}
        found.append(
            {
                "name": function.get("name") or "",
                "schema": function.get("parameters") or {},
                "description": function.get("description") or "",
            }
        )
    return found


def get_entry(name: str) -> dict | None:
    canon = canonical(name)
    for item in entries():
        if item["name"] == canon:
            return item
    return None


def get_schema(name: str) -> dict | None:
    item = get_entry(name)
    if item is None:
        return None
    schema = item.get("schema")
    return schema if isinstance(schema, dict) else None


def names() -> list[str]:
    return [item["name"] for item in entries() if item["name"]]


def is_disabled(name: str) -> bool:
    canon = canonical(name)
    if not canon:
        return False
    with _LOCK:
        return canon in _DISABLED


def disable(name: str) -> None:
    """Remove one tool from the prompt and refuse later calls."""
    canon = canonical(name)
    if get_entry(canon) is None:
        raise ValueError("Unknown tool. Valid tools: " + ", ".join(names()[:12]) + ".")
    with _LOCK:
        _DISABLED.add(canon)
    _invalidate_schema_cache()


def enable(name: str) -> None:
    canon = canonical(name)
    with _LOCK:
        _DISABLED.discard(canon)
    _invalidate_schema_cache()


def reset() -> None:
    """Enable every tool again. Tests start from this."""
    with _LOCK:
        _DISABLED.clear()
    _invalidate_schema_cache()


def disabled_names() -> list[str]:
    with _LOCK:
        return sorted(_DISABLED)


def block_reason(name: str) -> str | None:
    """Why a call must not run. None when the tool is enabled or not registered."""
    canon = canonical(name)
    if not canon or not is_disabled(canon):
        return None
    return f"{canon} is disabled. It was removed from the prompt and it was not run."


def visible_tools(tools: list[dict]) -> list[dict]:
    """The prompt list, without disabled tools."""
    with _LOCK:
        blocked = set(_DISABLED)
    if not blocked:
        return tools
    return [tool for tool in tools if (tool.get("function") or {}).get("name") not in blocked]


def _invalidate_schema_cache() -> None:
    try:
        import easyagent.nctx as nctx
    except Exception:
        return
    nctx._TOOL_SCHEMA_TOKENS = None


def coerce_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Coerce string-typed args to their JSON-Schema types. Originals stay on failure."""
    if not args or not isinstance(args, dict):
        return args
    schema = get_schema(tool_name) or {}
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return args
    args = dict(args)
    for key, value in list(args.items()):
        prop_schema = properties.get(key)
        if not isinstance(prop_schema, dict):
            continue
        expected = prop_schema.get("type")
        is_container = isinstance(value, (list, tuple))
        if expected == "array" and value is not None and not is_container:
            if isinstance(value, str):
                coerced = _coerce_value(value, expected, schema=prop_schema)
                if coerced is not value:
                    args[key] = coerced
                    continue
            args[key] = [value]
            continue
        if not isinstance(value, str):
            wants_string = expected == "string" or (
                _schema_accepts_kind(prop_schema, "string") and not _schema_accepts_kind(prop_schema, "array")
            )
            if wants_string:
                text = _string_from_value(value)
                if text is not None:
                    args[key] = text
                continue
            if (expected == "array" and is_container) or (expected == "object" and isinstance(value, dict)):
                args[key] = _normalize_json_strings(value, prop_schema)
            continue
        if not expected and not _schema_allows_null(prop_schema):
            continue
        coerced = _coerce_value(value, expected, schema=prop_schema)
        if coerced is not value:
            args[key] = coerced
            if isinstance(coerced, (list, tuple, dict)):
                args[key] = _normalize_json_strings(coerced, prop_schema)
    return args


def trim_result(text: str, max_chars: int, *, label: str = "OUTPUT") -> str:
    """Head and tail of a long tool result. Short text is unchanged."""
    from easyagent.truncate import truncate_head_tail

    return truncate_head_tail(text or "", max_chars, label=label)


def _schema_accepts_kind(schema: Any, kind: str, seen: frozenset[int] = frozenset()) -> bool:
    if not isinstance(schema, dict) or id(schema) in seen:
        return False
    seen = seen | {id(schema)}
    typed = schema.get("type")
    if typed == kind or (isinstance(typed, list) and kind in typed):
        return True
    for union_key in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(union_key)
        if isinstance(branches, list) and any(_schema_accepts_kind(branch, kind, seen) for branch in branches):
            return True
    return False


def _normalize_json_strings(value: Any, schema: Any) -> Any:
    if not isinstance(schema, dict):
        return value
    if isinstance(value, str):
        trimmed = value.strip()
        expects_array = _schema_accepts_kind(schema, "array")
        expects_object = _schema_accepts_kind(schema, "object")
        if not ((expects_array and trimmed.startswith("[")) or (expects_object and trimmed.startswith("{"))):
            return value
        try:
            parsed = json.loads(trimmed)
        except (ValueError, TypeError):
            return value
        if not ((isinstance(parsed, list) and expects_array) or (isinstance(parsed, dict) and expects_object)):
            return value
        value = parsed
    if isinstance(value, list):
        items_schema = schema.get("items")
        if not isinstance(items_schema, dict):
            return value
        out = [_normalize_json_strings(item, items_schema) for item in value]
        return out if any(new is not old for new, old in zip(out, value)) else value
    if isinstance(value, dict):
        props = schema.get("properties")
        if not isinstance(props, dict):
            return value
        out = dict(value)
        for key, prop_schema in props.items():
            if key in value and isinstance(prop_schema, dict):
                out[key] = _normalize_json_strings(value[key], prop_schema)
        return out if any(out[key] is not old for key, old in value.items()) else value
    return value


def _string_from_value(value: Any) -> str | None:
    """A string field sent as a number or a one-item list. Anything else stays put."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if value == int(value):
            return str(int(value))
        return str(value)
    if isinstance(value, list) and len(value) == 1:
        item = value[0]
        if isinstance(item, str):
            return item.strip() or None
        return _string_from_value(item)
    return None


def _coerce_value(value: str, expected_type, schema: dict | None = None):
    if _schema_allows_null(schema) and value.strip().lower() == "null":
        return None
    if isinstance(expected_type, list):
        for kind in expected_type:
            coerced = _coerce_value(value, kind, schema=schema)
            if coerced is not value:
                return coerced
        return value
    coercer = _SCALAR_COERCERS.get(expected_type)
    if coercer is not None:
        return coercer(value)
    if expected_type == "null" and value.strip().lower() == "null":
        return None
    return value


def _schema_allows_null(schema: dict | None) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == "null" or (isinstance(schema_type, list) and "null" in schema_type):
        return True
    if schema.get("nullable") is True:
        return True
    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if isinstance(variants, list) and any(isinstance(item, dict) and item.get("type") == "null" for item in variants):
            return True
    return False


def _coerce_json(value: str, expected_python_type: type):
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return value
    if isinstance(parsed, expected_python_type):
        return parsed
    return value


def _coerce_number(value: str, integer_only: bool = False):
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return value
    if number != number or number in (float("inf"), float("-inf")):
        return value
    if number == int(number):
        return int(number)
    return value if not integer_only else value


def _coerce_boolean(value: str):
    return {"true": True, "false": False}.get(value.strip().lower(), value)


_SCALAR_COERCERS = {
    "integer": lambda value: _coerce_number(value, integer_only=True),
    "number": _coerce_number,
    "boolean": _coerce_boolean,
    "array": lambda value: _coerce_json(value, list),
    "object": lambda value: _coerce_json(value, dict),
}
