"""Expand a PowerShell-shaped command the way the shell will, before it runs.

An unset variable becomes empty. Join-Path of an empty base is a drive root.
This module does not start a process and does not change the computer.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def local_assignments(text: str) -> dict[str, str]:
    """`$name='value'` in this command. A later `$name` can use it."""
    found: dict[str, str] = {}
    for match in re.finditer(r"""(?i)(?:^|[;&|\n])\s*\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'([^']*)'""", text or ""):
        found[match.group(1)] = match.group(2)
    for match in re.finditer(r'''(?i)(?:^|[;&|\n])\s*\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"''', text or ""):
        found.setdefault(match.group(1), match.group(2))
    return found


def expand_join_path(text: str, env: dict) -> str:
    """Join-Path of an empty variable is a drive root, not a path with zero files."""

    def repl(match: re.Match) -> str:
        base = env.get(match.group(1), "")
        child = match.group(2)
        if not str(base).strip():
            return r"\*" if "*" in child else "\\"
        return str(Path(str(base)) / child).replace("/", "\\")

    return re.sub(
        r"(?i)\(?\s*Join-Path\s+\$env:([A-Za-z_][A-Za-z0-9_]*)\s+['\"]([^'\"]*)['\"]\s*\)?",
        repl,
        text or "",
    )


def _env_at(text: str, index: int) -> tuple[str, int]:
    rest = text[index:]
    match = re.match(r"(?i)\$\(\s*\$env:([A-Za-z_][A-Za-z0-9_]*)\s*\)", rest)
    if match:
        return match.group(1), match.end()
    match = re.match(r"(?i)\$\(\s*\$([A-Za-z_][A-Za-z0-9_]*)\s*\)", rest)
    if match:
        return match.group(1), match.end()
    match = re.match(r"(?i)\$env:([A-Za-z_][A-Za-z0-9_]*)", rest)
    if match:
        return match.group(1), match.end()
    match = re.match(r"(?i)\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}", rest)
    if match:
        return match.group(1), match.end()
    match = re.match(r"%([A-Za-z_][A-Za-z0-9_]*)%", rest)
    if match:
        return match.group(1), match.end()
    match = re.match(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", rest)
    if match:
        return match.group(1), match.end()
    match = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)", rest)
    if match:
        return match.group(1), match.end()
    return "", 0


def expand_command(text: str, env: dict) -> str:
    """PowerShell ``$env:NAME``, cmd ``%NAME%``, and ``$NAME``. An unset name becomes empty."""
    out: list[str] = []
    i = 0
    in_single = False
    in_double = False
    source = text or ""
    while i < len(source):
        ch = source[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
            i += 1
            continue
        if not in_single:
            name, consumed = _env_at(source, i)
            if consumed:
                out.append(env.get(name, ""))
                i += consumed
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def still_unresolved(text: str) -> bool:
    """A `$` or `%NAME%` left after expansion. The root of that path is unknown."""
    return bool(re.search(r"(?i)\$(\(|env:|[A-Za-z_])|%([A-Za-z_][A-Za-z0-9_]*)%", text or ""))


def inline_code(text: str) -> str:
    """Bodies of `python -c` and `node -e`. The file they name is the target."""
    parts: list[str] = []
    pattern = re.compile(
        r"""(?is)\b(?:python3?|node|nodejs)\s+(?:-[A-Za-z]+\s+)*(-c|-e)\s+(?:\"([^\"]*)\"|'([^']*)'|(\S+))"""
    )
    for match in pattern.finditer(text or ""):
        parts.append(match.group(2) or match.group(3) or match.group(4) or "")
    return "\n".join(part for part in parts if part)


def unwrap_shell(text: str) -> str:
    """`cmd /c` and `powershell -c` hide the real command. Look at the inside."""
    current = (text or "").strip()
    for _ in range(3):
        match = re.match(
            r"(?is)^(?:cmd(?:\.exe)?\s+/c\s+|powershell(?:\.exe)?\s+(?:-noprofile\s+)?(?:-command|-c)\s+)(.+)$",
            current,
        )
        if not match:
            break
        inner = match.group(1).strip()
        if (inner.startswith('"') and inner.endswith('"')) or (inner.startswith("'") and inner.endswith("'")):
            inner = inner[1:-1]
        current = inner.strip()
    return current


def prepared_command(text: str, env: dict | None = None) -> str:
    """Join-Path, then variables, then a wrapped shell. Used by the hard block."""
    mapping = dict(env or os.environ)
    home = str(Path.home())
    mapping.setdefault("HOME", home)
    mapping.setdefault("USERPROFILE", home)
    mapping.update(local_assignments(text or ""))
    expanded = expand_command(expand_join_path(text or "", mapping), mapping)
    return unwrap_shell(expanded)
