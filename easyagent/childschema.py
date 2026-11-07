"""Schema-checked answers from child agents.

A parent can hand a child a JSON schema with its task. The child's final
answer must then be JSON that matches. This checks it with pydantic.

The supported subset is deliberately small and is what the tool description
promises: a JSON object with typed properties (string, number, integer,
boolean, or array of string), a required list, and optional
additionalProperties false. Any other property type is accepted as-is,
never guessed at. Extra fields are allowed unless the schema forbids them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from pydantic import ConfigDict, ValidationError, create_model

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

_SIMPLE = {"string": str, "number": float, "integer": int, "boolean": bool}


def usable(schema: Any) -> bool:
    """True when schema is a JSON object schema this module can check."""
    return isinstance(schema, dict) and bool(schema)


def validate(schema: dict, text: str) -> tuple[bool, str]:
    """Check one child answer against a JSON schema subset.

    Returns (True, the answer as clean JSON text) or (False, a plain-language
    error naming the first problem).
    """
    if not usable(schema):
        return False, "The schema was not a JSON object, so the answer could not be checked."
    body = _FENCE.sub(r"\1", str(text or "").strip())
    try:
        data = json.loads(body)
    except ValueError as exc:
        return False, f"The answer is not valid JSON: {_plain_json_error(str(exc))}"
    if not isinstance(data, dict):
        return False, f"The answer must be one JSON object. It is {_kind_of(data)}."
    try:
        model = _model(schema)
    except (TypeError, NameError) as exc:
        return False, f"The schema is not usable: {str(exc)[:160]}"
    try:
        checked = model.model_validate(data)
    except ValidationError as exc:
        first = dict((exc.errors() or [{}])[0])
        return False, _plain_error(first)
    # exclude_unset keeps exactly what the child wrote (extra fields included)
    # and does not invent nulls for fields it left out.
    return True, json.dumps(checked.model_dump(mode="json", exclude_unset=True), ensure_ascii=False)


def _model(schema: dict):
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
    required = schema.get("required")
    required = {str(item) for item in required} if isinstance(required, list) else set()
    forbid = schema.get("additionalProperties") is False
    fields: dict[str, tuple[Any, Any]] = {}
    for name, prop in props.items():
        field = _field_type(prop if isinstance(prop, dict) else {})
        if str(name) in required:
            fields[str(name)] = (field, ...)
        else:
            fields[str(name)] = (Optional[field], None)
    config = ConfigDict(extra="forbid" if forbid else "allow")
    return create_model("ChildAnswer", __config__=config, **fields)


def _field_type(prop: dict) -> Any:
    kind = str(prop.get("type") or "").lower()
    if kind in _SIMPLE:
        return _SIMPLE[kind]
    if kind == "array":
        items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
        if str(items.get("type") or "").lower() == "string":
            return list[str]
        return list
    return Any


def _plain_error(err: dict) -> str:
    loc = ".".join(str(part) for part in err.get("loc") or ()) or "the answer"
    kind = str(err.get("type") or "")
    if kind == "missing":
        return f"Missing field '{loc}'."
    if kind == "extra_forbidden":
        return f"Field '{loc}' is not in the schema."
    if kind == "string_type":
        return f"Field '{loc}' must be text."
    if kind in {"int_type", "int_parsing"}:
        return f"Field '{loc}' must be a whole number."
    if kind in {"float_type", "float_parsing"}:
        return f"Field '{loc}' must be a number."
    if kind == "bool_type":
        return f"Field '{loc}' must be true or false."
    if kind == "list_type":
        return f"Field '{loc}' must be a list."
    return f"Field '{loc}' is not right: {str(err.get('msg') or 'it does not match the schema')}."


def _plain_json_error(detail: str) -> str:
    text = " ".join(str(detail or "").split())
    if not text:
        return "it did not parse."
    return f"it did not parse ({text[:160]})."


def _kind_of(data: Any) -> str:
    if data is None:
        return "nothing"
    if isinstance(data, list):
        return "a list"
    if isinstance(data, str):
        return "plain text"
    if isinstance(data, bool):
        return "true or false"
    if isinstance(data, (int, float)):
        return "a number"
    return type(data).__name__
