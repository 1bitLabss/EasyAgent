"""In-process syntax checks after a write.

Only errors this edit introduced are reported. Python is reported and does
not refuse the write. JSON, YAML, and TOML are reported the same way: a
syntax note is not a card. YAML is skipped when ruamel is not installed.
"""

from __future__ import annotations

import ast
import json
import tomllib
from typing import Callable, Optional


def lint_json(content: str) -> tuple[bool, str]:
    try:
        json.loads(content)
        return True, ""
    except json.JSONDecodeError as exc:
        return False, f"JSONDecodeError: {exc.msg} (line {exc.lineno}, column {exc.colno})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def lint_yaml(content: str) -> tuple[bool, str]:
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.error import YAMLError
    except ImportError:
        return True, "__SKIP__"
    try:
        for _event in YAML(typ="safe").parse(content):
            pass
        return True, ""
    except YAMLError as exc:
        return False, f"YAMLError: {exc}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def lint_toml(content: str) -> tuple[bool, str]:
    try:
        tomllib.loads(content)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def lint_python(content: str) -> tuple[bool, str]:
    try:
        ast.parse(content.encode("utf-8", "surrogateescape"))
        return True, ""
    except SyntaxError as exc:
        loc = f" (line {exc.lineno}, column {exc.offset})" if exc.lineno else ""
        return False, f"{type(exc).__name__}: {exc.msg}{loc}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


LINTERS: dict[str, Callable[[str], tuple[bool, str]]] = {
    ".py": lint_python,
    ".json": lint_json,
    ".yaml": lint_yaml,
    ".yml": lint_yaml,
    ".toml": lint_toml,
}


def lint_text(path: str, content: str) -> tuple[bool, str, bool]:
    """(ok, message, skipped)."""
    ext = ""
    if "." in (path or ""):
        ext = "." + path.rsplit(".", 1)[-1].lower()
    checker = LINTERS.get(ext)
    if checker is None:
        return True, "", True
    ok, err = checker(content or "")
    if err == "__SKIP__":
        return True, "", True
    return ok, err, False


def check_lint_delta(path: str, pre_content: Optional[str], post_content: str) -> str:
    """New syntax errors only. Empty when the edit added none, or no linter applies.

    A clean file says nothing. A file that was already broken, and is no more
    broken, says the old errors are still there. New lines are quoted.
    """
    post_ok, post_err, skipped = lint_text(path, post_content)
    if skipped or post_ok:
        return ""
    if pre_content is None:
        return post_err
    pre_ok, pre_err, pre_skipped = lint_text(path, pre_content)
    if pre_ok or pre_skipped or not pre_err:
        return post_err
    pre_lines = {line.strip() for line in pre_err.splitlines() if line.strip()}
    post_lines = [line for line in post_err.splitlines() if line.strip() and line.strip() not in pre_lines]
    if not post_lines:
        return "Pre-existing lint errors — this edit didn't introduce new ones but the file is still broken."
    return "New lint errors introduced by this edit (pre-existing errors filtered out):\n" + "\n".join(post_lines)
