"""Resolve a shell command before it runs. Unresolved paths fail closed.

PowerShell's real parser is used when powershell or pwsh is on PATH. The
result is cached. Without that parser, statements are still walked: ``cd``
and Set-Location move the working directory, ``${env:}`` / ``$HOME`` /
``$env:`` expand, wildcards and Join-Path are resolved. A path that cannot
be resolved, and could name the data directory, is not allowed.

Inline interpreter code is scanned the same way: python -c, node -e,
ruby -e, perl -e, deno eval, powershell -Command, and cmd /c. A path
literal, an env var, expanduser, os.path.join, or Path() is resolved.
A path built dynamically, or a reference to the data folder or another
bot's folder that cannot be resolved, is not allowed. A script this turn
just wrote is scanned when the interpreter runs it.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from pathlib import Path


_CACHE: dict[str, dict] = {}
_PATH_CMDS = {
    "get-content", "gc", "type", "cat", "get-item", "gi", "test-path",
    "copy-item", "cpi", "cp", "move-item", "mi", "mv", "remove-item", "ri",
    "del", "erase", "set-content", "sc", "out-file", "select-string",
    "get-childitem", "gci", "dir", "ls",
}
_LOCATION = {"cd", "set-location", "sl", "chdir", "push-location", "pushd"}
_POP = {"pop-location", "popd"}
_LISTING = {"get-childitem", "gci", "dir", "ls"}


def data_decision(store, command: str, cwd: Path, bot_id: str | None) -> str:
    """``block`` when a resolved path is in the data folder outside this bot's work.

    ``ask`` when a path cannot be resolved and might name that folder.
    ``allow`` when every path stays outside it, or inside this bot's own
    workspace, workbench, or tmp.
    """
    from easyagent.safety import decode_shell, _unglue

    text, status = decode_shell(command or "")
    if status == "ask":
        return "ask"
    text = _unglue(text)
    if not text.strip():
        return "allow"
    from easyagent.secrets import command_targets_secret, is_protected_secret

    if command_targets_secret(store, text):
        return "block"
    env = dict(os.environ)
    if store is not None:
        env.setdefault("EASYAGENT_DATA", str(store.root))
    touched, unresolved, cwd_after = _walk(text, Path(cwd), env, store, bot_id)
    if _blocked(store, touched, bot_id) or any(is_protected_secret(store, path) for path in touched):
        return "block"
    if _inside_data(store, cwd_after, bot_id) and _lists_here(text):
        return "block"
    if getattr(_walk, "inline_ask", False) or (unresolved and _could_name_data(text)):
        return "ask"
    return "allow"


def _blocked(store, paths: list[Path], bot_id: str | None) -> bool:
    from easyagent.tools import _data_path_blocked

    for path in paths:
        if _data_path_blocked(store, path, bot_id):
            return True
    return False


def _inside_data(store, path: Path, bot_id: str | None) -> bool:
    from easyagent.tools import _data_path_blocked

    return _data_path_blocked(store, path, bot_id)


def _could_name_data(command: str) -> bool:
    text = command or ""
    if re.search(r"[\$%*?]|~|\.\.|join-path|set-location|\bcd\b|push-location|pop-location", text, re.IGNORECASE):
        return True
    return False


def _lists_here(command: str) -> bool:
    for statement in _statements(command):
        words = _words(statement)
        if words and words[0].lower() in _LISTING and not any(not word.startswith("-") for word in words[1:]):
            return True
    return False


def _walk(
    command: str,
    cwd: Path,
    env: dict,
    store=None,
    bot_id: str | None = None,
    depth: int = 0,
) -> tuple[list[Path], bool, Path]:
    if depth == 0:
        _walk.inline_ask = False
    parsed = _powershell_ast(command)
    raw_statements = parsed.get("statements") if isinstance(parsed, dict) else None
    pieces = _statements(command)
    unresolved = False
    if isinstance(raw_statements, list) and raw_statements:
        for statement in raw_statements:
            text = statement.get("text") if isinstance(statement, dict) else str(statement)
            pieces.extend(_statements(str(text or "")))
        unresolved = bool(parsed.get("unresolved")) or unresolved
    statements = [{"text": item} for item in pieces]
    location = Path(cwd)
    stack: list[Path] = []
    found: list[Path] = []
    for statement in statements:
        if isinstance(statement, dict) and statement.get("kind") == "location":
            target = str(statement.get("path") or "")
            pushed = bool(statement.get("push"))
            if pushed:
                stack.append(location)
            resolved = _resolve_one(target, location, env)
            if not resolved:
                unresolved = True
            else:
                location = resolved[0]
            continue
        if isinstance(statement, dict) and statement.get("kind") == "pop":
            if stack:
                location = stack.pop()
            continue
        text = statement.get("text") if isinstance(statement, dict) else str(statement)
        words = _words(str(text or ""))
        if not words:
            continue
        head = words[0].lower()
        if head in _POP:
            if stack:
                location = stack.pop()
            continue
        if head in _LOCATION:
            target = _location_arg(words[1:])
            if head in {"push-location", "pushd"}:
                stack.append(location)
            resolved = _resolve_many(target, location, env, store, bot_id) if target else None
            if target and not resolved:
                unresolved = True
            elif resolved:
                location = resolved[0]
            continue
        if head == "join-path":
            joined, ok = _join_path(words[1:], location, env, store, bot_id)
            if not ok:
                unresolved = True
            found.extend(joined)
            continue
        if _pathish(words[0]):
            resolved = _resolve_many(words[0], location, env, store, bot_id)
            if resolved:
                from easyagent.safety import _exempt_interpreter

                if not any(_exempt_interpreter(item) for item in resolved):
                    found.extend(resolved)
        consumed, nested_unresolved = _take_inline(
            str(text or ""), words, location, env, store, bot_id, found, depth
        )
        if consumed:
            unresolved = unresolved or nested_unresolved
            continue
        args = words[1:]
        if head in _PATH_CMDS:
            for word in args:
                if word.startswith("-"):
                    continue
                resolved = _resolve_many(word, location, env, store, bot_id)
                if resolved is None:
                    unresolved = True
                else:
                    found.extend(resolved)
            continue
        for word in args:
            if word.startswith("-"):
                continue
            if not _pathish(word):
                continue
            resolved = _resolve_many(word, location, env, store, bot_id)
            if resolved is None:
                unresolved = True
            else:
                found.extend(resolved)
    return found, unresolved, location


_INLINE_DEPTH = 4
_SCRIPT_CAP = 2_000_000
_SCRIPT_PROGS = {"python", "python3", "py", "node", "ruby", "perl", "deno", "powershell", "pwsh"}
_JOIN_CALLS = {"os.path.join", "path.join", "path.resolve", "File.join", "Path", "pathlib.Path"}
_HOME_CALLS = {"Path.home", "pathlib.Path.home"}
_EXPAND_USER_CALLS = {"os.path.expanduser", "File.expand_path"}
_PATH_CALLS = _JOIN_CALLS | _HOME_CALLS | _EXPAND_USER_CALLS | {
    "open",
    "os.path.expandvars",
    "os.path.abspath",
    "os.path.realpath",
    "os.path.normpath",
    "fs.readFileSync",
    "fs.readFile",
    "require('fs').readFileSync",
    "require('fs').readFile",
    "Deno.readTextFileSync",
    "Deno.readTextFile",
    "Deno.readFileSync",
    "Deno.readFile",
    "File.read",
    "File.binread",
    "File.open",
    "IO.read",
    "IO.binread",
}
_VALUE_CALLS = _JOIN_CALLS | _HOME_CALLS | _EXPAND_USER_CALLS | {
    "os.path.expandvars",
    "os.path.abspath",
    "os.path.realpath",
    "os.path.normpath",
    "os.getenv",
    "os.environ.get",
}
_CALL_RE = re.compile(
    r"(?<![A-Za-z0-9_.])("
    r"require\(\s*['\"]fs['\"]\s*\)\.readFileSync"
    r"|require\(\s*['\"]fs['\"]\s*\)\.readFile"
    r"|os\.path\.join|os\.path\.expanduser|os\.path\.expandvars|os\.path\.abspath"
    r"|os\.path\.realpath|os\.path\.normpath"
    r"|pathlib\.Path\.home|pathlib\.Path|Path\.home|Path"
    r"|fs\.readFileSync|fs\.readFile"
    r"|Deno\.readTextFileSync|Deno\.readTextFile|Deno\.readFileSync|Deno\.readFile"
    r"|File\.expand_path|File\.binread|File\.join|File\.read|File\.open"
    r"|IO\.binread|IO\.read|path\.resolve|path\.join|os\.environ\.get|os\.getenv|open"
    r")\s*\("
)
_DATA_MARK = re.compile(r"EASYAGENT_DATA|[/\\]bots[/\\]|[/\\]chats[/\\]")


def _take_inline(statement: str, words, location, env, store, bot_id, found: list[Path], depth: int) -> tuple[bool, bool]:
    """Scan interpreter code and this-turn scripts. True when the statement is consumed."""
    if not words:
        return False, False
    prog = _program_name(words[0])
    rest = _shell_command_rest(prog, statement)
    if rest is not None:
        if depth >= _INLINE_DEPTH:
            _walk.inline_ask = True
            return True, False
        nested, nested_unresolved, _cwd = _walk(rest, location, env, store, bot_id, depth + 1)
        found.extend(nested)
        return True, nested_unresolved
    blobs = _inline_code_args(prog, words)
    if blobs is not None:
        for blob in blobs:
            _apply_code(blob, location, env, store, bot_id, found)
        return True, False
    token = _script_arg(prog, words)
    if token:
        _scan_created_script(token, location, env, store, bot_id, found)
    return False, False


def _program_name(word: str) -> str:
    name = (word or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _shell_command_rest(prog: str, statement: str) -> str | None:
    """The raw text after powershell -Command or cmd /c, quotes included."""
    if prog in {"powershell", "pwsh"}:
        flags = {"-command", "-c"}
    elif prog == "cmd":
        flags = {"/c"}
    else:
        return None
    for token, _start, end in _word_spans(statement):
        if token.lower() in flags:
            return statement[end:].strip()
    return None


def _word_spans(statement: str) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    for match in re.finditer(r"'([^']*)'|\"([^\"]*)\"|(\S+)", statement or ""):
        token = next(group for group in match.groups() if group is not None)
        if token:
            spans.append((token, match.start(), match.end()))
    return spans


def _inline_code_args(prog: str, words: list[str]) -> list[str] | None:
    if prog in {"python", "python3", "py"}:
        return _values_after(words, {"-c"})
    if prog == "node":
        return _values_after(words, {"-e", "-p", "--eval", "--print"})
    if prog in {"ruby", "perl"}:
        return _values_after(words, {"-e"})
    if prog == "deno" and len(words) > 1 and words[1].lower() == "eval":
        body = [word for word in words[2:] if not word.startswith("-")]
        return [" ".join(body)] if body else [""]
    return None


def _values_after(words: list[str], flags: set[str]) -> list[str] | None:
    found: list[str] = []
    seen = False
    index = 1
    while index < len(words):
        if words[index].lower() in flags:
            seen = True
            if index + 1 < len(words) and not words[index + 1].startswith("-"):
                found.append(words[index + 1])
                index += 2
                continue
        index += 1
    if not seen:
        return None
    return found


def _script_arg(prog: str, words: list[str]) -> str | None:
    if prog not in _SCRIPT_PROGS:
        return None
    start = 1
    if prog == "deno" and len(words) > 1 and words[1].lower() in {"run", "task"}:
        start = 2
    value_flags = {"-m", "-w", "-x", "--eval", "--print"}
    index = start
    while index < len(words):
        word = words[index]
        low = word.lower()
        if low in {"-file", "-f"} and prog in {"powershell", "pwsh"}:
            return words[index + 1] if index + 1 < len(words) else None
        if low in value_flags or low in {"-c", "-e", "-p"}:
            index += 2
            continue
        if word.startswith("-") or word.startswith("/"):
            index += 1
            continue
        return word
    return None


def _scan_created_script(token, location, env, store, bot_id, found: list[Path]) -> None:
    path = _created_script(token, location, env, store, bot_id)
    if path is None:
        return
    try:
        if not path.is_file():
            _walk.inline_ask = True
            return
        if path.stat().st_size > _SCRIPT_CAP:
            _walk.inline_ask = True
        text = path.read_text(encoding="utf-8", errors="replace")[:_SCRIPT_CAP]
    except OSError:
        _walk.inline_ask = True
        return
    _apply_code(text, location, env, store, bot_id, found)


def _created_script(token, location, env, store, bot_id) -> Path | None:
    from easyagent import turn as turn_mod

    slot = turn_mod.current_slot()
    created = getattr(slot, "safety_created", None) if slot is not None else None
    if not created:
        return None
    known: set[str] = set()
    for item in created:
        known.add(str(item))
        try:
            known.add(str(Path(item).resolve()))
        except OSError:
            pass
    candidates: list[Path] = []
    resolved = _resolve_many(token, location, env, store, bot_id) or []
    candidates.extend(resolved)
    bare = (token or "").strip().strip('"').strip("'")
    if (
        store is not None
        and bot_id
        and bare
        and "/" not in bare
        and "\\" not in bare
        and not bare.startswith("~")
        and not bare.startswith("-")
    ):
        from easyagent.workspace import bot_workspace

        candidates.append(bot_workspace(store, bot_id) / Path(bare).name)
    for path in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if str(path) in known or key in known:
            return path
    return None


def _apply_code(source: str, location, env, store, bot_id, found: list[Path]) -> None:
    tokens, ask = _scan_code(source or "", env)
    if ask:
        _walk.inline_ask = True
    for token in tokens:
        if not token or token in {".", ".."}:
            continue
        resolved = _resolve_many(token, location, env, store, bot_id)
        if not resolved:
            _walk.inline_ask = True
            continue
        found.extend(resolved)


def _scan_code(source: str, env: dict) -> tuple[list[str], bool]:
    tokens: list[str] = []
    ask = False
    spans: list[tuple[int, int]] = []
    for prefix, body, start, end in _iter_strings(source):
        spans.append((start, end))
        lowered = prefix.lower()
        if "f" in lowered and "{" in body:
            value = _eval_fstring(body, env)
            if value is None:
                if _DATA_MARK.search(body) or _literal_pathish(body):
                    ask = True
            elif _literal_pathish(value):
                tokens.append(value)
            continue
        if prefix == "`" and "${" in body:
            value = _eval_template(body, env)
            if value is None:
                if _DATA_MARK.search(body) or _literal_pathish(body):
                    ask = True
            elif _literal_pathish(value):
                tokens.append(value)
            continue
        if _literal_pathish(body):
            tokens.append(body)
    for name, inner, _start in _iter_calls(source, spans):
        key = _call_key(name)
        if key not in _PATH_CALLS:
            continue
        value, failed = _consume_call(key, inner, env)
        if failed:
            ask = True
        elif value:
            tokens.append(value)
    for expr in _plus_chains(source):
        if not _DATA_MARK.search(expr):
            continue
        value = _eval_expr(expr, env)
        if value is None:
            ask = True
        elif value:
            tokens.append(value)
    return tokens, ask


def _call_key(name: str) -> str:
    return re.sub(r"\s+", "", name).replace('"', "'")


def _iter_calls(source: str, spans: list[tuple[int, int]]):
    for match in _CALL_RE.finditer(source or ""):
        if any(start <= match.start() < end for start, end in spans):
            continue
        open_at = match.end() - 1
        close_at = _balanced_end(source, open_at)
        if close_at is None:
            yield match.group(1), source[match.end() :], match.start()
            continue
        yield match.group(1), source[match.end() : close_at], match.start()


def _iter_strings(source: str):
    text = source or ""
    index = 0
    size = len(text)
    while index < size:
        ch = text[index]
        if ch == "`":
            start = index
            index += 1
            body: list[str] = []
            while index < size and text[index] != "`":
                if text[index] == "\\" and index + 1 < size:
                    body.append(text[index + 1])
                    index += 2
                    continue
                body.append(text[index])
                index += 1
            if index < size and text[index] == "`":
                index += 1
            yield "`", "".join(body), start, index
            continue
        prefix = ""
        if ch in "rRuUbBfF":
            cursor = index
            while cursor < size and cursor < index + 3 and text[cursor] in "rRuUbBfF":
                cursor += 1
            if cursor < size and text[cursor] in {"'", '"'}:
                prefix = text[index:cursor]
                index = cursor
                ch = text[index]
            else:
                index += 1
                continue
        if ch not in {"'", '"'}:
            index += 1
            continue
        triple = text[index : index + 3] in {"'''", '"""'}
        quote = text[index : index + 3] if triple else ch
        start = index - len(prefix)
        index += len(quote)
        body = []
        raw = "r" in prefix.lower()
        closed = False
        while index < size:
            if text.startswith(quote, index):
                index += len(quote)
                closed = True
                break
            if text[index] == "\\" and not raw:
                if index + 1 < size:
                    nxt = text[index + 1]
                    body.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                    index += 2
                    continue
            body.append(text[index])
            index += 1
        if closed or body:
            yield prefix, "".join(body), start, index


def _literal_pathish(body: str) -> bool:
    text = (body or "").strip()
    if not text or text in {".", ".."}:
        return False
    if text.startswith("~"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    return "/" in text or "\\" in text


def _consume_call(name: str, inner: str, env: dict) -> tuple[str | None, bool]:
    args = _split_args(inner)
    if name in _HOME_CALLS:
        return env.get("HOME") or env.get("USERPROFILE") or str(Path.home()), False
    if name in {"os.getenv", "os.environ.get"}:
        if not args:
            return None, True
        key = _eval_expr(args[0], env)
        if key is None or key not in env:
            if key is not None and len(args) >= 2:
                fallback = _eval_expr(args[1], env)
                return (fallback, False) if fallback is not None else (None, True)
            return None, True
        return env[key], False
    if name in _JOIN_CALLS:
        if not args:
            return None, True
        parts: list[str] = []
        for arg in args:
            value = _eval_expr(arg, env)
            if value is None:
                return None, True
            parts.append(value)
        built = parts[0]
        for part in parts[1:]:
            built = str(Path(built) / part)
        return built, False
    if not args:
        return None, True
    value = _eval_expr(args[0], env)
    if value is None:
        return None, True
    if name in _EXPAND_USER_CALLS:
        value = _expand_user(value, env)
        if len(args) >= 2 and name == "File.expand_path" and not Path(value).is_absolute():
            base = _eval_expr(args[1], env)
            if base is None:
                return None, True
            value = str(Path(base) / value)
    elif name == "os.path.expandvars":
        expanded = _expand(value, env)
        if expanded is None:
            return None, True
        value = expanded
    return value, False


def _expand_user(text: str, env: dict) -> str:
    if not text.startswith("~"):
        return text
    home = env.get("HOME") or env.get("USERPROFILE") or str(Path.home())
    if text == "~" or text.startswith("~/") or text.startswith("~\\"):
        rest = text[2:] if len(text) > 1 else ""
        return str(Path(home) / rest) if rest else home
    return text


def _eval_expr(expr: str, env: dict) -> str | None:
    text = (expr or "").strip()
    while text.startswith("(") and _balanced_end(text, 0) == len(text) - 1:
        text = text[1:-1].strip()
        if not text:
            return None
    plus = _split_op(text, "+")
    if plus is not None:
        bits: list[str] = []
        for part in plus:
            value = _eval_expr(part, env)
            if value is None:
                return None
            bits.append(value)
        return "".join(bits)
    div = _split_div(text)
    if div is not None:
        bits = []
        for part in div:
            value = _eval_expr(part, env)
            if value is None:
                return None
            bits.append(value)
        built = bits[0]
        for bit in bits[1:]:
            built = str(Path(built) / bit)
        return built
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        return _eval_template(text[1:-1], env)
    literal = _whole_string(text)
    if literal is not None:
        prefix, body = literal
        if "f" in prefix.lower():
            return _eval_fstring(body, env)
        return body
    named = (
        re.fullmatch(r"os\.environ\[\s*(['\"])([A-Za-z_][A-Za-z0-9_]*)\1\s*\]", text)
        or re.fullmatch(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)", text)
        or re.fullmatch(r"process\.env\[\s*(['\"])([A-Za-z_][A-Za-z0-9_]*)\1\s*\]", text)
        or re.fullmatch(r"ENV\[\s*(['\"])([A-Za-z_][A-Za-z0-9_]*)\1\s*\]", text)
        or re.fullmatch(r"\$ENV\{([A-Za-z_][A-Za-z0-9_]*)\}", text)
    )
    if named:
        groups = [group for group in named.groups() if group and group not in {"'", '"'}]
        key = groups[-1] if groups else ""
        if key not in env:
            return None
        return env[key]
    match = re.match(r"^([A-Za-z_][\w.]*)\s*\(", text)
    if match:
        close_at = _balanced_end(text, match.end() - 1)
        if close_at == len(text) - 1:
            name = match.group(1)
            inner = text[match.end() : close_at]
            if name not in _VALUE_CALLS and name not in {"os.getenv", "os.environ.get"}:
                return None
            value, failed = _consume_call(name, inner, env)
            if failed:
                return None
            return value
    return None


def _eval_fstring(body: str, env: dict) -> str | None:
    out: list[str] = []
    index = 0
    while index < len(body):
        if body.startswith("{{", index):
            out.append("{")
            index += 2
            continue
        if body.startswith("}}", index):
            out.append("}")
            index += 2
            continue
        if body[index] == "{":
            end = body.find("}", index)
            if end < 0:
                return None
            inner = body[index + 1 : end]
            value = _eval_expr(inner, env)
            if value is None:
                head = inner.split("!", 1)[0].split(":", 1)[0]
                value = _eval_expr(head, env)
            if value is None:
                return None
            out.append(value)
            index = end + 1
            continue
        out.append(body[index])
        index += 1
    return "".join(out)


def _eval_template(body: str, env: dict) -> str | None:
    out: list[str] = []
    index = 0
    while index < len(body):
        if body.startswith("${", index):
            end = body.find("}", index)
            if end < 0:
                return None
            value = _eval_expr(body[index + 2 : end], env)
            if value is None:
                return None
            out.append(value)
            index = end + 1
            continue
        out.append(body[index])
        index += 1
    return "".join(out)


def _whole_string(text: str) -> tuple[str, str] | None:
    items = list(_iter_strings(text))
    if len(items) != 1:
        return None
    prefix, body, start, end = items[0]
    if start == 0 and end == len(text):
        return prefix, body
    return None


def _balanced_end(text: str, open_index: int, open_ch: str = "(", close_ch: str = ")") -> int | None:
    depth = 0
    quote = ""
    index = open_index
    while index < len(text):
        ch = text[index]
        if quote:
            if ch == "\\" and quote != "`":
                index += 2
                continue
            if ch == quote:
                quote = ""
            index += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            index += 1
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _split_args(inner: str) -> list[str]:
    return _split_op(inner, ",") or ([inner.strip()] if inner.strip() else [])


def _split_op(text: str, sep: str) -> list[str] | None:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    found = False
    index = 0
    while index < len(text):
        ch = text[index]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote != "`":
                if index + 1 < len(text):
                    buf.append(text[index + 1])
                    index += 2
                    continue
            if ch == quote:
                quote = ""
            index += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            buf.append(ch)
            index += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0 and not (sep == "+" and (text.startswith("+=", index) or (index and text[index - 1] == "+"))):
            parts.append("".join(buf).strip())
            buf = []
            found = True
            index += 1
            continue
        buf.append(ch)
        index += 1
    if not found:
        return None
    parts.append("".join(buf).strip())
    return [part for part in parts if part]


def _split_div(text: str) -> list[str] | None:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    found = False
    index = 0
    while index < len(text):
        ch = text[index]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote != "`":
                if index + 1 < len(text):
                    buf.append(text[index + 1])
                    index += 2
                    continue
            if ch == quote:
                quote = ""
            index += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            buf.append(ch)
            index += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "/" and depth == 0 and not text.startswith("//", index):
            prev = text[index - 1] if index else ""
            nxt = text[index + 1] if index + 1 < len(text) else ""
            if prev in {")", " ", "\t", "'", '"', "`"} or nxt in {" ", "\t", "'", '"', "`", "("}:
                parts.append("".join(buf).strip())
                buf = []
                found = True
                index += 1
                continue
        buf.append(ch)
        index += 1
    if not found:
        return None
    parts.append("".join(buf).strip())
    return [part for part in parts if part] or None


def _plus_chains(source: str) -> list[str]:
    positions = []
    depth = 0
    quote = ""
    index = 0
    text = source or ""
    while index < len(text):
        ch = text[index]
        if quote:
            if ch == "\\" and quote != "`":
                index += 2
                continue
            if ch == quote:
                quote = ""
            index += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            index += 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth = max(0, depth - 1)
        elif ch == "+" and depth == 0 and not text.startswith("+=", index) and (index == 0 or text[index - 1] != "+"):
            positions.append(index)
        index += 1
    chains: list[str] = []
    seen: set[tuple[int, int]] = set()
    for pos in positions:
        left = _term_left(text, pos)
        right = _term_right(text, pos)
        grew = True
        while grew:
            grew = False
            cursor = right
            while cursor < len(text) and text[cursor] in " \t":
                cursor += 1
            if cursor < len(text) and text[cursor] == "+" and not text.startswith("+=", cursor):
                nxt = _term_right(text, cursor)
                if nxt > right:
                    right = nxt
                    grew = True
            cursor = left - 1
            while cursor >= 0 and text[cursor] in " \t":
                cursor -= 1
            if cursor >= 0 and text[cursor] == "+" and (cursor == 0 or text[cursor - 1] != "+"):
                nxt = _term_left(text, cursor)
                if nxt < left:
                    left = nxt
                    grew = True
        key = (left, right)
        if key in seen:
            continue
        seen.add(key)
        chains.append(text[left:right].strip())
    return [chain for chain in chains if chain]


def _term_left(text: str, plus_at: int) -> int:
    index = plus_at - 1
    while index >= 0 and text[index] in " \t":
        index -= 1
    if index < 0:
        return plus_at
    return _atom_start(text, index)


def _term_right(text: str, plus_at: int) -> int:
    index = plus_at + 1
    while index < len(text) and text[index] in " \t":
        index += 1
    return _atom_end(text, index)


def _atom_start(text: str, index: int) -> int:
    if text[index] in {"'", '"', "`"}:
        quote = text[index]
        cursor = index - 1
        while cursor >= 0:
            if text[cursor] == quote and (cursor == 0 or text[cursor - 1] != "\\"):
                start = cursor
                while start > 0 and text[start - 1] in "rRuUbBfF":
                    start -= 1
                return start
            cursor -= 1
        return 0
    if text[index] in {")", "]", "}"}:
        close_ch = text[index]
        open_ch = {")": "(", "]": "[", "}": "{"}[close_ch]
        cursor = index
        depth = 0
        quote = ""
        while cursor >= 0:
            ch = text[cursor]
            if quote:
                if ch == quote and (cursor == 0 or text[cursor - 1] != "\\"):
                    quote = ""
                cursor -= 1
                continue
            if ch in {"'", '"', "`"}:
                quote = ch
                cursor -= 1
                continue
            if ch == close_ch:
                depth += 1
            elif ch == open_ch:
                depth -= 1
                if depth == 0:
                    if open_ch in {"(", "["}:
                        return _name_start(text, cursor)
                    return cursor
            cursor -= 1
        return 0
    cursor = index
    while cursor >= 0 and (text[cursor].isalnum() or text[cursor] in "._$"):
        cursor -= 1
    return cursor + 1


def _name_start(text: str, paren_at: int) -> int:
    cursor = paren_at - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    while cursor >= 0 and (text[cursor].isalnum() or text[cursor] in "._"):
        cursor -= 1
    return cursor + 1


def _atom_end(text: str, index: int) -> int:
    if index >= len(text):
        return index
    if text[index] in "rRuUbBfF":
        cursor = index
        while cursor < len(text) and cursor < index + 3 and text[cursor] in "rRuUbBfF":
            cursor += 1
        if cursor < len(text) and text[cursor] in {"'", '"'}:
            index = cursor
    if text[index] in {"'", '"', "`"}:
        quote = text[index : index + 3] if text[index : index + 3] in {"'''", '"""'} else text[index]
        cursor = index + len(quote)
        while cursor < len(text):
            if text.startswith(quote, cursor):
                return cursor + len(quote)
            if text[cursor] == "\\" and quote != "`":
                cursor += 2
                continue
            cursor += 1
        return len(text)
    if text[index] in {"$", "@"} or text[index].isalpha() or text[index] == "_":
        cursor = index + 1
        while cursor < len(text) and (text[cursor].isalnum() or text[cursor] in "._$"):
            cursor += 1
        while True:
            while cursor < len(text) and text[cursor] in " \t":
                look = cursor
                while look < len(text) and text[look] in " \t":
                    look += 1
                if look < len(text) and text[look] in "([":
                    cursor = look
                else:
                    break
            if cursor < len(text) and text[cursor] in "([":
                close_at = _balanced_end(text, cursor, text[cursor], ")" if text[cursor] == "(" else "]")
                if close_at is None:
                    return len(text)
                cursor = close_at + 1
                continue
            break
        return cursor
    return index + 1


def _location_arg(words: list[str]) -> str:
    for word in words:
        if word.startswith("-"):
            continue
        return word
    return ""


def _join_path(words: list[str], cwd: Path, env: dict, store=None, bot_id: str | None = None) -> tuple[list[Path], bool]:
    parts: list[str] = []
    skip = False
    for word in words:
        if skip:
            parts.append(word)
            skip = False
            continue
        lower = word.lower()
        if lower in {"-path", "-childpath", "-additionalchildpath"}:
            skip = True
            continue
        if word.startswith("-"):
            continue
        parts.append(word)
    if len(parts) < 2:
        return [], False
    built = parts[0]
    ok = True
    resolved = _resolve_many(built, cwd, env, store, bot_id)
    if not resolved:
        return [], False
    current = str(resolved[0])
    for part in parts[1:]:
        expanded = _expand(part, env)
        if expanded is None:
            ok = False
            break
        current = str(Path(current) / expanded)
    if not ok:
        return [], False
    return _resolve_many(current, cwd, env, store, bot_id) or [], True


def _bases(cwd: Path, token: str, store, bot_id: str | None) -> list[Path]:
    """A workspace-prefixed climb is also resolved from that bot's folder."""
    bases = [Path(cwd)]
    text = (token or "").strip().replace("\\", "/")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    first = text.split("/", 1)[0].lower() if text else ""
    if store is not None and bot_id and first in {"workspace", "workbench", "tmp"}:
        bases.append(Path(store.root) / "bots" / str(bot_id))
    return bases


def _resolve_many(token: str, cwd: Path, env: dict, store=None, bot_id: str | None = None) -> list[Path] | None:
    found: list[Path] = []
    for base in _bases(cwd, token, store, bot_id):
        got = _resolve_one(token, base, env)
        if got:
            found.extend(got)
    return found or None


def _resolve_one(token: str, cwd: Path, env: dict) -> list[Path] | None:
    expanded = _expand(token, env)
    if expanded is None or not expanded:
        return None
    if any(ch in expanded for ch in "*?"):
        pattern = expanded
        if not Path(pattern).is_absolute():
            pattern = str(Path(cwd) / pattern)
        matches = [Path(item) for item in glob.glob(pattern)]
        if not matches:
            return None
        return matches
    try:
        return [resolve_path_token(expanded, cwd)]
    except OSError:
        return None


def _expand(token: str, env: dict) -> str | None:
    text = (token or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    if "$(" in text or "`" in text or text.startswith("&"):
        return None

    def env_brace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise KeyError(name)
        return env[name]

    def env_dollar(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise KeyError(name)
        return env[name]

    try:
        text = re.sub(r"(?i)\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}", env_brace, text)
        text = re.sub(r"(?i)\$env:([A-Za-z_][A-Za-z0-9_]*)", env_dollar, text)
        text = re.sub(r"(?i)\$\{HOME\}|\$HOME\b", lambda _m: env.get("HOME") or env.get("USERPROFILE") or str(Path.home()), text)
        text = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", env_dollar, text)
        text = re.sub(r"(?<![\w{])\$([A-Za-z_][A-Za-z0-9_]*)", env_dollar, text)
    except KeyError:
        return None
    def percent(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise KeyError(name)
        return env[name]

    try:
        text = re.sub(r"%([^%]+)%", percent, text)
    except KeyError:
        return None
    if text.startswith("~"):
        home = env.get("HOME") or env.get("USERPROFILE") or str(Path.home())
        text = str(Path(home) / text[2:]) if text.startswith("~/") or text.startswith("~\\") else home
    text = text.replace("\\", "/")
    if re.search(r"(?i)\$\{?env:|\$[A-Za-z_]|%[^%]+%", text):
        return None
    return text


def _pathish(token: str) -> bool:
    text = token or ""
    if text.startswith("-"):
        return False
    return bool(re.search(r"[\\/]|^\.\.|\$|%|~|\*|\?", text))


def _words(statement: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"'([^']*)'|\"([^\"]*)\"|(\S+)", statement or ""):
        token = next(group for group in match.groups() if group is not None)
        if token:
            found.append(token)
    return found


def _statements(command: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    text = command or ""
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in {";", "\n"} or text.startswith("&&", i) or text.startswith("||", i):
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
            i += 2 if text.startswith("&&", i) or text.startswith("||", i) else 1
            continue
        if ch == "|":
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts



def resolve_path_token(raw: str, cwd: Path) -> Path:
    """normpath then realpath. ``..`` is resolved."""
    token = (raw or "").strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        token = token[1:-1]
    token = token.replace("\\", "/")
    if token in {"", "."}:
        path = Path(cwd)
    else:
        path = Path(token)
        if not path.is_absolute():
            path = Path(cwd) / path
    path = Path(os.path.normpath(str(path)))
    try:
        return path.resolve()
    except OSError:
        return path


def _which(name: str) -> str | None:
    """shutil.which crashes when tests mark this process Windows on Linux."""
    try:
        return shutil.which(name)
    except Exception:
        return None


def _on_windows() -> bool:
    """The platform this module should treat as Windows. Tests patch this, not os.name."""
    return os.name == "nt"


def _parser_program() -> str | None:
    """Windows PowerShell 5.1. PowerShell 7 is only a fallback, and only on Windows.

    On Linux and macOS the shell is not PowerShell. Calling pwsh there changes
    the data guard and stalls short commands, so those platforms use the
    built-in walk.
    """
    if not _on_windows():
        return None
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    system = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(system):
        return system
    return _which("powershell.exe") or _which("powershell") or _which("pwsh.exe") or _which("pwsh")


def _powershell_ast(command: str) -> dict:
    """The real parser, when it is installed. Cached. Empty when it is not."""
    key = command or ""
    if key in _CACHE:
        return _CACHE[key]
    program = _parser_program()
    if not program:
        _CACHE[key] = {}
        return {}
    script = r"""
$ErrorActionPreference = 'Stop'
$errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($env:EASYAGENT_PS_COMMAND, [ref]$null, [ref]$errs)
$rows = @()
foreach ($item in $ast.FindAll({$true}, $true)) {
  $name = $item.GetType().Name
  if ($name -eq 'CommandAst') {
    $text = $item.Extent.Text
    $rows += @{ kind = 'text'; text = $text }
  }
}
if ($errs -and $errs.Count -gt 0) {
  @{ statements = @(); unresolved = $true } | ConvertTo-Json -Compress
  exit 0
}
@{ statements = $rows; unresolved = $false } | ConvertTo-Json -Compress -Depth 4
"""
    try:
        proc = subprocess.Popen(
            [program, "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "EASYAGENT_PS_COMMAND": key},
            start_new_session=True,
        )
    except (OSError, ValueError, TypeError):
        _CACHE[key] = {}
        return {}
    from easyagent import turn as turn_mod

    turn_mod.attach_proc(proc)
    try:
        try:
            stdout, _stderr = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            turn_mod.stop_process(proc)
            try:
                proc.communicate(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _CACHE[key] = {}
            return {}
        except (OSError, ValueError, TypeError):
            _CACHE[key] = {}
            return {}
    finally:
        turn_mod.detach_proc(proc)
    if turn_mod.cancelled():
        raise turn_mod.TurnCancelled()
    raw = (stdout or "").strip()
    try:
        import json

        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    _CACHE[key] = data
    return data
