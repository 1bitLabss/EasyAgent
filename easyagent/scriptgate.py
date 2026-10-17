"""Prove a script stays inside this bot's folder, or refuse to treat it as safe.

A shebang is only the interpreter. The body is read the way a shell command is:
each statement, each redirect, rm/cp/mv/dd/chmod, a pipe into a shell, a
variable, a substitution, an absolute path, ``..``, ``cd``, a symlink, and a
call to another script. Anything that cannot be proved is not safe. Running
``sh``, ``./``, ``python``, ``source``, or ``bash -c`` reads the file again,
because the bytes may have changed after the write.
"""

from __future__ import annotations

import ast
import os
import re
import time
from pathlib import Path

_SKIP_DIRS = frozenset({".git", "node_modules", "venv", ".venv", "__pycache__"})
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash"})
_PYTHONS = frozenset({"python", "python2", "python3", "py"})
_OTHERS = frozenset({"perl", "ruby", "node", "nodejs", "deno", "php", "bun", "lua"})
_READERS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "cat", "head", "tail", "strings", "awk", "sed", "find",
})
_INERT = re.compile(
    r"(?i)^(?:echo|printf|true|false|:|pwd|whoami|date|uname|hostname|id|"
    r"which|where|exit|return|export|readonly|local|unset|umask|hash|type|"
    r"shift|break|continue|test|\[|set|sleep)\b"
)
_SAFE_IMPORTS = frozenset({
    "os", "sys", "pathlib", "shutil", "subprocess", "socket", "ctypes",
    "time", "math", "json", "re", "random", "datetime",
})
_MODULE_CALLS = {
    "time": frozenset({"sleep", "time", "monotonic", "perf_counter", "gmtime", "localtime", "strftime"}),
    "math": None,
    "re": frozenset({"search", "match", "fullmatch", "findall", "finditer", "sub", "split", "compile", "escape"}),
    "random": frozenset({"random", "randint", "choice", "randrange", "uniform", "seed"}),
    "json": frozenset({"loads", "dumps"}),
    "datetime": frozenset({"now", "utcnow", "today", "fromtimestamp", "datetime", "date", "time", "timedelta"}),
}
_DYNAMIC = re.compile(r"\$\(|`|<\(|<<|\beval\b|\bexec\b")
_PIPE_SHELL = re.compile(
    r"(?i)\|\s*(?:sudo\s+)?(?:(?:ba|z|da|k)?sh|bash|zsh|dash|ksh|pwsh|"
    r"powershell|python3?|perl|ruby|node|deno)\b"
)
_DECODE = re.compile(
    r"(?i)\b(?:base64\b[^\n|]*\s(?:-d|--decode)\b|base64\s+(?:-d|--decode)\b)"
)
_CD = re.compile(r"(?i)^(?:cd|pushd)\b")
_RECURSIVE = re.compile(r"(?:^|\s)-(?:[A-Za-z]*[rR]|-recursive)\b")
_SAFE_FUNCS = frozenset({
    "print", "len", "repr", "str", "int", "float", "bool", "abs", "min", "max",
    "sum", "range", "enumerate", "zip", "list", "dict", "tuple", "set", "round",
    "isinstance", "sorted", "reversed", "any", "all", "ord", "chr", "hex", "oct",
    "bin", "format", "ascii", "map", "filter",
})
_SAFE_METHODS = frozenset({
    "read", "readline", "readlines", "write", "writelines", "close", "flush",
    "strip", "split", "join", "format", "lower", "upper", "replace",
    "startswith", "endswith", "append", "extend", "items", "keys", "values",
    "get", "update",
})
_OS_PATH = frozenset({
    "remove", "unlink", "rmdir", "mkdir", "makedirs", "rename", "replace",
    "chmod", "chown", "link", "symlink", "truncate", "chdir",
})
_MAX_DEPTH = 6
_SCAN_CAP = 4000


def script_body_proved(store, bot_id: str, body: str, cwd: Path, script_path: Path | None = None) -> bool:
    """True only when every part of ``body`` is proved to stay in this bot's folders."""
    from easyagent.yolo import _scope_roots

    try:
        roots = _scope_roots(store, bot_id)
    except Exception:
        return False
    if not roots:
        return False
    try:
        return _prove(body or "", Path(cwd), roots, store, bot_id, script_path, set(), 0)
    except Exception:
        return False


def script_run_unproved(store, bot_id: str, command: str, cwd: Path) -> bool:
    """True when this command runs a script whose current bytes are not proved."""
    from easyagent.safety import _split_chain

    try:
        statements = _split_chain(command or "")
    except Exception:
        return True
    for statement in statements:
        try:
            if _statement_runs_unproved(store, bot_id, statement, Path(cwd), set(), 0):
                return True
        except Exception:
            return True
    return False


def awk_plain_read(command: str, cwd: Path) -> bool:
    """True when awk only prints fields of files that are already non-secret.

    ``$1`` inside the program is awk's field, including after quote
    normalization turns the quotes double. A shell variable, a command
    substitution, or a file that is secret or missing is not this form.
    """
    from easyagent.safety import _quoted_words

    line = (command or "").strip()
    if not line or _awk_meta(line):
        return False
    words = _quoted_words(line)
    if not words:
        return False
    program = words[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if program not in {"awk", "gawk", "mawk", "nawk"}:
        return False
    codes, files, ok = _awk_pieces(words[1:])
    if not ok or not codes:
        return False
    if any(_RISKY_INLINE.search(code) or re.search(r"\$[A-Za-z_{]", code) for code in codes):
        return False
    for token in files:
        if _token_unresolved(token) or "$" in token or _operand_secret(token, cwd, wide=True):
            return False
        path = _try_resolve(token, cwd)
        if path is None or not path.is_file() or _path_secret(path):
            return False
    return True


def _awk_meta(line: str) -> bool:
    """Shell syntax outside single quotes. ``$`` plus digits inside quotes stays."""
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            if ch == '"':
                quote = ""
            elif ch in {"`", "$"} and not (ch == "$" and i + 1 < len(line) and line[i + 1].isdigit()):
                return True
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            i += 1
            continue
        if ch in "$`|&;<>":
            return True
        i += 1
    return False


def _awk_pieces(args: list[str]) -> tuple[list[str], list[str], bool]:
    codes: list[str] = []
    files: list[str] = []
    saw = False
    index = 0
    valued = {"-F", "-v", "--field-separator", "--assign"}
    while index < len(args):
        arg = args[index]
        if arg in {"-e", "--source"}:
            if index + 1 >= len(args):
                return codes, files, False
            codes.append(args[index + 1])
            saw = True
            index += 2
            continue
        if arg in valued:
            index += 2
            continue
        if arg.startswith(("-F", "-v", "--field-separator=", "--assign=")):
            index += 1
            continue
        if arg.startswith("-"):
            return codes, files, False
        if not saw:
            codes.append(arg)
            saw = True
        else:
            files.append(arg)
        index += 1
    return codes, files, True


def command_reads_secrets(command: str, cwd: Path) -> bool:
    """True when a command is not on the reader allow-list, or a proved operand is secret-class.

    A command runs only when every program has known option semantics and every
    file operand is a literal path ``secret_class`` clears, or a recursive search
    can exclude those paths. Anything else waits. Classification does not walk
    the workspace to sniff every file.
    """
    folder = Path(cwd)
    text = command or ""
    if not text.strip():
        return False
    try:
        # PowerShell assignments and pipelines are a known grammar. Trust the
        # paths that grammar proves, instead of treating ``$s`` as an unknown
        # program. A command the grammar rejects falls through to the allow-list.
        if _powershell_shape(text):
            from easyagent.psread import prove_readonly
            from easyagent.safety import secret_file

            proved = prove_readonly(text, folder, os.environ)
            if proved is not None:
                return any(secret_file(path) for path in proved)
        if _wants_search_exclude(text):
            text = shield_secrets(text, folder)
        return _command_reads(text, folder)
    except Exception:
        return True


def _powershell_shape(text: str) -> bool:
    """A Verb-Noun cmdlet or a ``$name`` use. Unix ``$(...)`` does not match."""
    return bool(re.search(r"(?i)(?:^|[|;&\n(])\s*(?:\$[A-Za-z_]|[A-Za-z]+-[A-Za-z]+)", text or ""))


def _prove(body: str, cwd: Path, roots, store, bot_id, script_path, seen: set, depth: int) -> bool:
    if depth > _MAX_DEPTH:
        return False
    language = _language(body, script_path)
    if language == "python":
        return _python_proved(_without_shebang(body), cwd, roots, store, bot_id)
    return _shell_proved(body, cwd, roots, store, bot_id, seen, depth)


def _language(body: str, script_path: Path | None) -> str:
    for line in (body or "").splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("#!"):
            low = line.lower()
            if "python" in low or re.search(r"\bpy\b", low):
                return "python"
            return "shell"
        break
    if script_path is not None and script_path.suffix.lower() == ".py":
        return "python"
    return "shell"


def _without_shebang(body: str) -> str:
    kept = []
    for line in (body or "").splitlines():
        if line.lstrip().startswith("#!"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _shell_proved(body: str, cwd: Path, roots, store, bot_id, seen: set, depth: int) -> bool:
    from easyagent.safety import _split_chain

    text = _strip_comments(_without_shebang(body))
    if _DYNAMIC.search(text) or _PIPE_SHELL.search(text) or _DECODE.search(text):
        return False
    location = cwd
    for statement in _split_chain(text):
        if not statement.strip() or statement.lstrip().startswith("#"):
            continue
        moved = _shell_statement(statement, location, roots, store, bot_id, seen, depth)
        if moved is None:
            return False
        if moved is not location and moved is not True:
            location = moved
    return True


def _strip_comments(text: str) -> str:
    kept = []
    for line in (text or "").splitlines():
        if line.lstrip().startswith("#"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _shell_statement(statement: str, cwd: Path, roots, store, bot_id, seen: set, depth: int):
    """True, or the new cwd after ``cd``. None when the statement is not proved."""
    from easyagent.safety import _mode, judge_command
    from easyagent.yolo import _REFUSE_RULES, _command_unreadable, _operand_paths, _shape_hard

    if _shape_hard(statement) or _command_unreadable(statement):
        return None
    folded = statement.strip()
    if re.match(r"(?i)^for\s+\w+\s+in\b", folded):
        if re.search(r"[`$]|\.\.", folded):
            return None
        return True
    if re.match(r"(?i)^(while|until|do|done|then|else|elif|fi|esac)\b", folded):
        rest = re.sub(r"(?i)^(while|until|do|then|else|elif)\s+", "", folded, count=1)
        if rest == folded or not rest or rest.lower() in {"true", "false", ":"}:
            return True
        return _shell_statement(rest, cwd, roots, store, bot_id, seen, depth)
    kind, target = _cd_of(statement, cwd, roots, store, bot_id)
    if kind == "bad":
        return None
    if kind == "ok":
        return target
    invoked = _invoked_in_statement(statement, cwd)
    if invoked is False:
        return None
    if invoked:
        for path in invoked:
            if not _nested_proved(path, cwd, roots, store, bot_id, seen, depth):
                return None
        return True
    try:
        bot = store.get_bot(bot_id)
    except Exception:
        bot = None
    inner = judge_command(
        statement,
        remote=False,
        cwd=cwd,
        roots=list(roots),
        created=set(),
        mode=_mode(bot),
    )
    if inner.tier == "block" or inner.rule in _REFUSE_RULES or inner.rule == "read-secret":
        return None
    paths = _operand_paths(statement, cwd)
    if paths is None:
        return None
    if not all(_inside_scope(path, roots, store, bot_id) for path in paths):
        return None
    if _mutates(statement):
        names = list(paths) or _plain_targets(statement, cwd)
        if not names or not all(_inside_scope(path, roots, store, bot_id) for path in names):
            return None
        return True
    if _INERT.search(statement.strip()) or inner.tier == "allow":
        return True
    if inner.tier == "ask" and inner.rule in {"delete", "move", "overwrite", "redirect-overwrite"} and paths:
        return True
    return None


def _cd_of(statement: str, cwd: Path, roots, store, bot_id):
    from easyagent.safety import _quoted_words, _resolve

    if not _CD.match(statement.strip()):
        return "no", None
    words = _quoted_words(statement)
    args = [word for word in words[1:] if not word.startswith("-")]
    if len(args) != 1:
        return "bad", None
    token = args[0]
    if token in {"-", "~"} or token.startswith("~") or ".." in token.split("/"):
        return "bad", None
    if any(mark in token for mark in "*?$`"):
        return "bad", None
    try:
        path = _resolve(token, cwd)
    except Exception:
        return "bad", None
    if not _stays(path, roots, store, bot_id):
        return "bad", None
    return "ok", path


def _invoked_in_statement(statement: str, cwd: Path):
    """A list of script files, False when the invocation cannot be proved, or None."""
    from easyagent.safety import _quoted_words, _resolve

    words = _quoted_words(statement)
    if not words:
        return None
    program = _program_name(words[0])
    raw = words[0]
    if raw in {".", "source"} or program in {".", "source"}:
        args = [word for word in words[1:] if word not in {".", "source"}]
        if len(args) != 1 or args[0].startswith("-"):
            return False
        return [_resolve(args[0], cwd)]
    if program in _SHELLS or program in _PYTHONS or program in _OTHERS:
        files, inline = _runner_args(program, words[1:])
        if inline:
            return False
        if not files:
            return None
        return [_resolve(item, cwd) for item in files]
    if raw.startswith("./") or raw.endswith((".sh", ".bash", ".py", ".pl", ".rb")):
        return [_resolve(raw, cwd)]
    return None


def _shell_inline_pieces(args: list[str]) -> tuple[str | None, list[str]]:
    """Inline code and script files for sh, bash, zsh, dash, ksh, and ash.

    Any short-option cluster that contains ``c`` is ``-c``. The command is the
    text attached after that ``c``, or the next argument. ``--command`` and
    ``--command=`` are the same. ``-o`` / ``-O`` and ``--rcfile`` / ``--init-file``
    take a value and do not hide a later ``-c``.
    """
    code = None
    files: list[str] = []
    index = 0
    long_values = {"--rcfile", "--init-file"}
    while index < len(args):
        arg = args[index]
        if arg == "--":
            files.extend(args[index + 1 :])
            break
        if arg == "--command":
            code = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue
        if arg.startswith("--command="):
            code = arg.split("=", 1)[1]
            index += 1
            continue
        head = arg.split("=", 1)[0]
        if head in long_values:
            index += 1 if "=" in arg else 2
            continue
        if arg.startswith("-") and not arg.startswith("--"):
            body = arg[1:]
            cursor = 0
            consumed = 1
            while cursor < len(body):
                ch = body[cursor]
                if ch == "c":
                    attached = body[cursor + 1 :]
                    if attached:
                        code = attached
                    else:
                        code = args[index + 1] if index + 1 < len(args) else ""
                        consumed = 2
                    break
                if ch in {"o", "O"}:
                    if not body[cursor + 1 :]:
                        consumed = 2
                    break
                cursor += 1
            index += consumed
            continue
        if arg.startswith("-"):
            index += 1
            continue
        files.append(arg)
        index += 1
    return code, files


def _runner_args(program: str, args: list[str]) -> tuple[list[str], bool]:
    """(script files, inline code present)."""
    if program in _SHELLS:
        code, files = _shell_inline_pieces(args)
        return files[:1], code is not None
    files: list[str] = []
    inline = False
    skip = False
    valued = {"-o", "-O"}
    for arg in args:
        if skip:
            skip = False
            continue
        low = arg.lower()
        if low in {"-c", "-e", "--command", "-m", "--module", "-lc"}:
            inline = True
            skip = True
            continue
        if low in valued:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        files.append(arg)
        if program in _SHELLS or program in _PYTHONS:
            break
    return files, inline


def _nested_proved(path: Path, cwd: Path, roots, store, bot_id, seen: set, depth: int) -> bool:
    key = _key(path)
    if key in seen:
        return False
    if not _stays(path, roots, store, bot_id):
        return False
    try:
        if not path.is_file() or path.is_symlink() and not _stays(path.resolve(), roots, store, bot_id):
            return False
    except OSError:
        return False
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    seen.add(key)
    try:
        return _prove(body, cwd, roots, store, bot_id, path, seen, depth + 1)
    finally:
        seen.discard(key)


def _mutates(statement: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b(rm|unlink|del|erase|rd|ri|rmdir|remove-item|mv|move|cp|copy|"
            r"dd|chmod|chown|ln|tee|truncate|mkdir|touch|install)\b",
            statement or "",
        )
        or re.search(r"(?i)(?:^|\s)>{1,2}\s*\S+", statement or "")
    )


def _plain_targets(statement: str, cwd: Path) -> list:
    from easyagent.safety import _quoted_words, _resolve

    words = _quoted_words(statement)
    found = []
    for token in words[1:]:
        if token.startswith("-") or token in {">", ">>", "<", "|"}:
            continue
        if any(mark in token for mark in "*?$`") or ".." in token.split("/"):
            return []
        if "/" in token or "\\" in token or token.startswith("~"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", token):
            continue
        found.append(_resolve(token, cwd))
    return found


def _inside_scope(path: Path, roots, store, bot_id) -> bool:
    from easyagent.yolo import _safe_inside

    try:
        return bool(_safe_inside(store, bot_id, path, roots))
    except Exception:
        return False


def _stays(path: Path, roots, store, bot_id) -> bool:
    """True when ``path`` is this bot's folder or a file inside it."""
    from easyagent.safety import _inside, _is_guardrail, _is_profile_root, secret_file

    try:
        if secret_file(path) or _is_profile_root(path) or _is_guardrail(path):
            return False
        return bool(_inside(path, roots))
    except Exception:
        return False


def _key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _python_proved(body: str, cwd: Path, roots, store, bot_id) -> bool:
    try:
        tree = ast.parse(body or "")
    except SyntaxError:
        # Nothing runs. The write is allowed and lint names the line.
        return True
    checker = _PythonCheck(cwd, roots, store, bot_id)
    try:
        checker.visit(tree)
    except _Unsafe:
        return False
    return True


class _Unsafe(Exception):
    pass


class _PythonCheck(ast.NodeVisitor):
    def __init__(self, cwd: Path, roots, store, bot_id: str) -> None:
        self.cwd = cwd
        self.roots = roots
        self.store = store
        self.bot_id = bot_id

    def visit_Import(self, node):
        return

    def visit_ImportFrom(self, node):
        if node.module in _SAFE_IMPORTS:
            return
        raise _Unsafe()

    def visit_Call(self, node):
        self._call(node)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        # A bare attribute is checked when it is the function of a call.
        self.generic_visit(node)

    def visit_Lambda(self, node):
        raise _Unsafe()

    def visit_FunctionDef(self, node):
        raise _Unsafe()

    def visit_AsyncFunctionDef(self, node):
        raise _Unsafe()

    def visit_ClassDef(self, node):
        raise _Unsafe()

    def visit_With(self, node):
        raise _Unsafe()

    def visit_AsyncWith(self, node):
        raise _Unsafe()

    def visit_Try(self, node):
        raise _Unsafe()

    def visit_Raise(self, node):
        raise _Unsafe()

    def visit_Delete(self, node):
        raise _Unsafe()

    def visit_Global(self, node):
        raise _Unsafe()

    def visit_Nonlocal(self, node):
        raise _Unsafe()

    def visit_JoinedStr(self, node):
        raise _Unsafe()

    def _call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
            if name in {"eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals", "vars", "input"}:
                raise _Unsafe()
            if name == "open":
                self._need_inside(node.args[0] if node.args else None)
                return
            if name not in _SAFE_FUNCS:
                raise _Unsafe()
            return
        if isinstance(func, ast.Attribute):
            self._attr_call(func, node)
            return
        raise _Unsafe()

    def _attr_call(self, func: ast.Attribute, node: ast.Call) -> None:
        name = func.attr
        if name in _SAFE_METHODS and isinstance(func.value, ast.Call):
            return
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id == "os" and name in _OS_PATH:
            self._need_inside(node.args[0] if node.args else None)
            if name in {"rename", "replace", "link", "symlink"} and len(node.args) > 1:
                self._need_inside(node.args[1])
            return
        if isinstance(owner, ast.Name) and owner.id in _MODULE_CALLS:
            allowed = _MODULE_CALLS[owner.id]
            if allowed is None or name in allowed:
                return
        if isinstance(owner, ast.Name) and owner.id == "shutil" and name in {"rmtree", "move", "copy", "copy2", "copytree"}:
            self._need_inside(node.args[0] if node.args else None)
            if name in {"move", "copy", "copy2"} and len(node.args) > 1:
                self._need_inside(node.args[1])
            return
        raise _Unsafe()

    def _need_inside(self, node) -> None:
        from easyagent.safety import _resolve

        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            raise _Unsafe()
        text = node.value
        if any(mark in text for mark in ("$", "`", "*", "?", "~")):
            raise _Unsafe()
        try:
            path = _resolve(text, self.cwd)
        except Exception as exc:
            raise _Unsafe() from exc
        if not _inside_scope(path, self.roots, self.store, self.bot_id):
            raise _Unsafe()


def _statement_runs_unproved(store, bot_id, statement: str, cwd: Path, seen: set, depth: int) -> bool:
    from easyagent.safety import _quoted_words, _resolve

    words = _quoted_words(statement)
    if not words:
        return False
    program = _program_name(words[0])
    raw = words[0]
    files: list[str] = []
    codes: list[str] = []
    if raw in {".", "source"} or program in {".", "source"} or program in _SHELLS:
        files, codes = _shell_launch(words if program in _SHELLS else [raw, *words[1:]])
    elif program in _PYTHONS or program in _OTHERS:
        if any(arg.lower() in {"-c", "-e", "-m", "--module"} for arg in words[1:]):
            return False
        files = [arg for arg in words[1:] if not arg.startswith("-")][:1]
    elif raw.startswith("./") or raw.endswith((".sh", ".bash", ".py", ".pl", ".rb")):
        files = [raw]
    else:
        return False
    for code in codes:
        if not script_body_proved(store, bot_id, code, cwd, None):
            return True
    for token in files:
        try:
            path = _resolve(token, cwd)
        except Exception:
            return True
        if not path.is_file():
            return False
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True
        if not _is_script(path, body):
            return False
        if not script_body_proved(store, bot_id, body, cwd, path):
            return True
    return False


def _shell_launch(words: list[str]) -> tuple[list[str], list[str]]:
    code, files = _shell_inline_pieces(words[1:])
    codes = [code] if code is not None else []
    return files[:1], codes


def _is_script(path: Path, body: str) -> bool:
    if path.suffix.lower() in {".sh", ".bash", ".py", ".pl", ".rb", ".ps1", ".bat", ".cmd"}:
        return True
    first = ""
    for line in (body or "").splitlines():
        if line.strip():
            first = line
            break
    return first.lstrip().startswith("#!")


def _program_name(token: str) -> str:
    text = (token or "").replace("\\", "/").rstrip("/")
    name = text.rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    # python3.11 and python3.12 are the same interpreter as python3.
    if re.fullmatch(r"python3\.\d+(?:\.\d+)?", name):
        return "python3"
    if re.fullmatch(r"python2\.\d+(?:\.\d+)?", name):
        return "python2"
    if name in _ALLOW_PROGRAMS or name in _NON_READERS or name in _PEEL:
        return name
    # A Windows path keeps its backslashes only until the shell tokenizer
    # treats them as escapes, so ``...\git.exe`` arrives glued to the parent.
    if ":" in name or " " in name:
        for known in ("python3", "python", "powershell", "pwsh", "bash", "node", "git", "cmd"):
            if name.endswith(known):
                return known
    return name


def _statement_reads_secrets(statement: str, cwd: Path) -> bool:
    from easyagent.safety import _quoted_words, _resolve, secret_file

    words = _quoted_words(statement)
    if not words:
        return False
    program = _program_name(words[0])
    if program not in _READERS:
        return False
    args = words[1:]
    if any(_glob(arg) for arg in args):
        return _tree_has_secret(cwd, recursive=False) or any(
            _tree_has_secret(path, recursive=False) for path in _dirs(args, cwd)
        )
    if program == "find" and re.search(r"(?i)(?:^|\s)-(?:exec|execdir|ok|okdir)\b", statement):
        roots = _dirs(args, cwd) or [cwd]
        return any(_tree_has_secret(path, recursive=True) for path in roots)
    if program in {"cat", "head", "tail", "strings"}:
        files = [arg for arg in args if not arg.startswith("-")]
        if len(files) > 1:
            return _named_secret(files, cwd) or _tree_has_secret(cwd, recursive=False)
        if len(files) == 1:
            path = _resolve(files[0], cwd)
            if path.is_dir():
                return _tree_has_secret(path, recursive=False)
            return _file_secret(path)
        return False
    if program in {"awk", "sed"}:
        files = _program_files(args)
        if len(files) > 1 or any(_glob(item) for item in files):
            return _named_secret(files, cwd) or _tree_has_secret(cwd, recursive=False)
        if len(files) == 1:
            path = _resolve(files[0], cwd)
            if path.is_dir():
                return _tree_has_secret(path, recursive=True)
            return _file_secret(path)
        return False
    recursive = program in {"rg", "ag", "ack"} or bool(_RECURSIVE.search(statement))
    targets = _search_targets(program, args, cwd)
    if any(_file_secret(path) or secret_file(path) for path in targets):
        return True
    dirs = [path for path in targets if path.is_dir()]
    if recursive or dirs or (program in {"rg", "ag", "ack"} and not targets):
        scan = dirs or [cwd]
        return any(_tree_has_secret(path, recursive=True) for path in scan)
    return False


def _glob(token: str) -> bool:
    return any(mark in (token or "") for mark in "*?[")


def _dirs(args: list[str], cwd: Path) -> list[Path]:
    from easyagent.safety import _resolve

    found = []
    for arg in args:
        if arg.startswith("-") or _glob(arg):
            continue
        try:
            path = _resolve(arg, cwd)
        except Exception:
            continue
        if path.is_dir():
            found.append(path)
    return found


def _program_files(args: list[str]) -> list[str]:
    files = []
    skip = False
    valued = {"-e", "-f", "-F", "--file", "--field-separator", "-n"}
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in valued:
            skip = True
            continue
        if arg.startswith("-") or arg.startswith("{") or arg in {"p", "d", "l"}:
            continue
        files.append(arg)
    return files


def _search_targets(program: str, args: list[str], cwd: Path) -> list[Path]:
    from easyagent.safety import _resolve

    skip = False
    valued = {
        "-e", "-f", "-m", "--regexp", "--file", "--glob", "-g",
        "--include", "--exclude", "--exclude-dir", "-A", "-B", "-C",
    }
    positional = []
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in valued:
            skip = True
            continue
        if arg.startswith("--") and "=" in arg:
            continue
        if arg.startswith("-"):
            continue
        positional.append(arg)
    paths = positional[1:] if len(positional) > 1 or program in {"rg", "ag", "ack"} else []
    if program in {"rg", "ag", "ack"}:
        paths = positional[1:] if positional else []
    return [_resolve(item, cwd) for item in paths]


def _named_secret(tokens: list[str], cwd: Path) -> bool:
    from easyagent.safety import _resolve

    for token in tokens:
        if token.startswith("-") or _glob(token):
            continue
        try:
            if _file_secret(_resolve(token, cwd)):
                return True
        except Exception:
            return True
    return False


def _file_secret(path: Path) -> bool:
    from easyagent.safety import secret_file

    if secret_file(path):
        return True
    if path.is_symlink():
        try:
            return secret_file(path.resolve())
        except OSError:
            return True
    return False


def _tree_has_secret(root: Path, *, recursive: bool) -> bool:
    from easyagent.safety import secret_file

    try:
        if secret_file(root):
            return True
        if root.is_symlink():
            return secret_file(root.resolve())
        if root.is_file():
            return False
        if not root.is_dir():
            return False
    except OSError:
        return True
    if not recursive:
        try:
            entries = list(root.iterdir())
        except OSError:
            return True
        if len(entries) > 500:
            return True
        for entry in entries:
            if secret_file(entry):
                return True
            if entry.is_symlink():
                try:
                    if secret_file(entry.resolve()):
                        return True
                except OSError:
                    return True
        return False
    overflow, entries = _name_index(root)
    return bool(overflow or entries)


_INDEX_SKIP = frozenset({
    ".git", "node_modules", "venv", ".venv", "__pycache__", "build", "dist",
    ".tox", "target", ".next", "coverage",
})
_INDEX_MAX = 2500
_INDEX_DEPTH = 8
_INDEX_BUDGET = 0.20
_CONTENT_CAP = 80
# Not secret by name. The name index still records one when its head sniffs as a key.
# A pathless recursive search does not use that list; it waits.
# .p12, .pfx, .jks, and .kdbx are already secret by suffix before this sample runs.
_KEY_SNIFF_SUFFIXES = frozenset({".ppk", ".bin", ".der"})
_INDEX_CACHE: dict[tuple, tuple[bool, list[tuple[str, str]]]] = {}


def _index_fingerprint(root: Path) -> tuple | None:
    """Root mtime plus child-directory mtimes. Adding a file invalidates the cache."""
    try:
        st = root.stat()
        resolved = os.path.realpath(root)
    except OSError:
        return None
    kids: list[tuple[str, int]] = []
    seen = 0
    try:
        for entry in root.iterdir():
            seen += 1
            if seen > 8000:
                break
            if entry.name in _INDEX_SKIP:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    kids.append((entry.name, entry.stat().st_mtime_ns))
            except OSError:
                kids.append((entry.name, 0))
    except OSError:
        return None
    return (resolved, st.st_mtime_ns, seen, tuple(sorted(kids)))


def _name_index(root: Path) -> tuple[bool, list[tuple[str, str]]]:
    """``(overflow, secret entries)``. Names only, plus a small content sample when the walk finishes.

    The walk is depth-, count-, and time-bounded. An unfinished walk is an overflow:
    a pathless recursive command cards instead of sniffing the rest of the tree.
    """
    try:
        if not root.is_dir():
            return False, []
    except OSError:
        return True, []
    key = _index_fingerprint(root)
    if key is None:
        return True, []
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    if len(_INDEX_CACHE) > 8:
        _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)), None)
    result = _build_name_index(root)
    _INDEX_CACHE[key] = result
    return result


def _head_sniffs_secret(path: Path) -> bool:
    """First 8 KB of a key-suffix file. Unreadable bytes count as secret."""
    from easyagent.safety import _sniff_blob

    try:
        with path.open("rb") as handle:
            blob = handle.read(8192)
    except OSError:
        return True
    return _sniff_blob(blob) is not None


def _build_name_index(root: Path) -> tuple[bool, list[tuple[str, str]]]:
    from easyagent.safety import _classify_names, secret_class

    deadline = time.monotonic() + _INDEX_BUDGET
    found: list[tuple[str, str]] = []
    candidates: list[Path] = []
    seen = 0
    overflow = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if time.monotonic() > deadline or seen > _INDEX_MAX:
            overflow = True
            break
        try:
            depth = len(Path(dirpath).relative_to(root).parts)
        except ValueError:
            depth = 0
        if depth > _INDEX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name not in _INDEX_SKIP]
        here = Path(dirpath)
        for name in filenames:
            seen += 1
            if seen > _INDEX_MAX or time.monotonic() > deadline:
                overflow = True
                break
            candidate = here / name
            try:
                rel = candidate.relative_to(root).as_posix()
            except ValueError:
                rel = name
            try:
                named = _classify_names(candidate)
            except Exception:
                named = "name"
            if named:
                found.append((rel, name))
                continue
            candidates.append(candidate)
        if overflow:
            break
    if overflow:
        return True, found
    textish: list[Path] = []
    keyish: list[Path] = []
    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            size = candidate.stat().st_size
        except OSError:
            return True, found
        if size <= 0:
            continue
        suffix = candidate.suffix.lower()
        if suffix in _KEY_SNIFF_SUFFIXES:
            keyish.append(candidate)
            continue
        if size > 262144:
            continue
        if suffix in {"", ".txt", ".text", ".json", ".yml", ".yaml", ".env", ".conf", ".cfg", ".ini", ".toml", ".csv", ".log", ".md"} or "env" in candidate.name.lower():
            textish.append(candidate)
    if len(textish) > _CONTENT_CAP or len(keyish) > _CONTENT_CAP:
        return True, found

    def remember(candidate: Path) -> None:
        try:
            rel = candidate.relative_to(root).as_posix()
        except ValueError:
            rel = candidate.name
        found.append((rel, candidate.name))

    for candidate in textish:
        try:
            secret = secret_class(candidate) is not None
        except Exception:
            secret = True
        if secret:
            remember(candidate)
    for candidate in keyish:
        try:
            secret = _head_sniffs_secret(candidate)
        except Exception:
            secret = True
        if secret:
            remember(candidate)
    return False, found


def _secret_entries(root: Path) -> list[tuple[str, str]]:
    """Relative path and basename of each secret file under ``root``.

    An overflow is reported as more than 400 entries so a recursive form cards
    instead of pretending the tree was fully classified.
    """
    overflow, found = _name_index(Path(root))
    if overflow:
        return found + [("__overflow__", "__overflow__")] * 401
    return found


def _tree_has_unexcluded_secret(root: Path, excluded: set[str]) -> bool:
    names = {item for item in excluded if item}
    for rel, name in _secret_entries(root):
        if name in names or rel in names:
            continue
        return True
    return False


def _search_excludes(program: str, args: list[str]) -> set[str]:
    found: set[str] = set()
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("--exclude="):
            found.add(arg.split("=", 1)[1].strip("'\""))
        elif arg == "--exclude" and index + 1 < len(args):
            found.add(args[index + 1].strip("'\""))
            index += 1
        elif arg.startswith("--glob=") or arg.startswith("-g"):
            value = arg.split("=", 1)[1] if "=" in arg else ""
            if arg in {"-g", "--glob"} and index + 1 < len(args):
                value = args[index + 1]
                index += 1
            value = value.strip("'\"")
            if value.startswith("!"):
                found.add(value[1:].lstrip("/"))
        index += 1
    if program == "git":
        found |= _git_excludes(args)
    return {item for item in found if item}


def _expand_pwd(text: str, cwd: Path, old: Path | None) -> str:
    """``$PWD`` is the cwd after ``cd``. ``$OLDPWD`` is the folder before it.

    The folder is substituted as literal text. A Windows path contains
    backslashes, and a replacement string would treat ``\\U`` as an escape.
    """
    folder = str(cwd)
    out = re.sub(r"\$\{PWD\}|\$PWD\b", lambda _match: folder, text or "")
    if old is not None:
        previous = str(old)
        out = re.sub(r"\$\{OLDPWD\}|\$OLDPWD\b", lambda _match: previous, out)
    return out


_RAW_NONREADERS = frozenset({
    "echo", "printf", "true", "false", "pwd", "whoami", "date", "uname",
    "hostname", "id", "ls", "dir", "cd", "pushd", "popd", "mkdir", "sleep",
    "which", "where",
})


def _raw_words(statement: str) -> list[str]:
    """Words with backslashes kept. The bash tokenizer eats them later."""
    words: list[str] = []
    buf: list[str] = []
    quote = ""
    text = statement or ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            i += 1
            continue
        if ch.isspace() or ch in {"|", ";", "&"}:
            if buf:
                words.append("".join(buf))
                buf = []
            i += 1
            continue
        if ch in {"<", ">"}:
            if buf:
                words.append("".join(buf))
                buf = []
            words.append(ch)
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        words.append("".join(buf))
    return words


def _raw_token_secret(token: str, cwd: Path) -> bool:
    from easyagent.safety import _basename_is_secret
    from easyagent.strictshell import secret_basename

    base = secret_basename(token)
    if base and _basename_is_secret(base):
        return True
    name = token.replace("/", "\\").rsplit("\\", 1)[-1]
    if name != name.strip() and _basename_is_secret(name):
        return True
    path = _try_resolve(token, cwd)
    return bool(path is not None and _path_secret(path))


def _raw_stage_secret(stage: str, cwd: Path) -> bool:
    words = _raw_words(stage)
    if not words:
        return False
    program = _program_name(words[0])
    redirect = False
    operands: list[str] = []
    for word in words[1:]:
        if word in {"<", ">"}:
            redirect = True
            continue
        if word.startswith("-") and not re.match(r"^[A-Za-z]:", word):
            continue
        operands.append(word)
    if program in _RAW_NONREADERS and not redirect:
        return False
    return any(_raw_token_secret(token, cwd) for token in operands)


def _raw_operand_secret(segment: str, cwd: Path) -> bool:
    """A Windows path, before bash treats a backslash as an escape.

    ``cat ..\\.env`` and ``type .env`` card. ``echo hi``, ``cat notes.txt``,
    and ``git status`` do not. ``c\\at .env`` still cards because the operand
    is ``.env`` before the escape is applied. Each side of a pipe is its own
    command, so ``echo a | cat ..\\.env`` still cards.
    """
    return any(_raw_stage_secret(stage, cwd) for stage in _pipe_parts(segment))


_UNPROVED_SCRIPT = re.compile(
    r"(?is)(?:"
    r"child_process|Bun\.file|\bARGV\b|\bgetline\b|"
    r"Buffer\.from\s*\(|"
    r"\bos\s*\.\s*(?:open|read|pread|readv|system|popen)\b|"
    r"require\s*\(\s*['\"]fs['\"]\s*\)\s*\[|"
    r"import\s*\(\s*['\"]fs['\"]|"
    r"\[\s*['\"]read['\"]\s*\+|"
    r"read['\"]\s*\+\s*['\"]File|"
    r"from\s+['\"]fs['\"]"
    r")"
)


def _script_text_unproved(body: str) -> bool:
    """True when a script file or heredoc is not statically free of a file read.

    ``console.log(1)`` and ``print(1)`` and ``{print $1}`` are free.
    ``require('fs')['read'+'FileSync']``, ``import('fs')``, ``Buffer.from``,
    ``os.open`` / ``os.read``, and ``ARGV`` are not.
    """
    return bool(_UNPROVED_SCRIPT.search(body or ""))


def _code_body_reads(body: str, cwd: Path, depth: int = 0) -> bool:
    """An inline or on-disk program that reads a file, or names a secret path."""
    text = body or ""
    if not text.strip():
        return False
    if _script_text_unproved(text):
        return True
    if _inline_code_reads(text, cwd, depth):
        return True
    if _foreign_code_reads(text, cwd):
        return True
    try:
        if _python_code_reads(text, cwd, depth):
            return True
    except Exception:
        pass
    for _quote, literal in re.findall(r"""(['"])(.*?)\1""", text):
        if literal and _operand_secret(literal, cwd, wide=True):
            return True
    return False


def _staged_code_reads(text: str, cwd: Path) -> bool:
    """A script written and then run in the same command, including a heredoc."""
    bodies: list[str] = []
    for match in re.finditer(r"(?s)\b(?:echo|printf)\s+(['\"])(.*?)\1\s*>{1,2}\s*\S+", text or ""):
        bodies.append(match.group(2))
    for match in re.finditer(r"(?s)>{1,2}\s*\S+\s*<<-?['\"]?(\w+)['\"]?\n(.*?)\n\1\b", text or ""):
        bodies.append(match.group(2))
    for match in re.finditer(
        r"(?s)\b(?:python3?|python|perl|ruby|node|bun|deno|php|awk|gawk)\b[^\n;|&]*<<-?['\"]?(\w+)['\"]?\n(.*?)\n\1\b",
        text or "",
    ):
        bodies.append(match.group(2))
    for match in re.finditer(
        r"(?s)\b(?:echo|printf)\s+(['\"])(.*?)\1\s*\|\s*(?:python3?|python|perl|ruby|node|bun|awk|gawk)\b",
        text or "",
    ):
        bodies.append(match.group(2))
    return any(_code_body_reads(body, cwd, 0) for body in bodies)


def _wants_search_exclude(text: str) -> bool:
    """Recursive grep or rg. Git diff is not excluded: a content diff cards instead."""
    body = text or ""
    if re.search(r"(?i)\b(?:grep|egrep|fgrep)\b[^\n;|&]*(?:--recursive|(?:^|\s)-[A-Za-z]*[rR][A-Za-z]*)", body):
        return True
    return bool(re.search(r"(?i)(?:^|[;&|\n]\s*)rg\b", body))


# A recursive search may run only when --include/-g/--type limits it to these
# source suffixes. md, css, toml, hpp, cs, rb, php, and sh are not on the list.
_SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".c", ".h", ".cpp",
})
_TYPE_SUFFIXES = {
    "py": frozenset({".py"}),
    "python": frozenset({".py"}),
    "js": frozenset({".js", ".jsx"}),
    "javascript": frozenset({".js", ".jsx"}),
    "jsx": frozenset({".jsx"}),
    "ts": frozenset({".ts", ".tsx"}),
    "typescript": frozenset({".ts", ".tsx"}),
    "tsx": frozenset({".tsx"}),
    "rs": frozenset({".rs"}),
    "rust": frozenset({".rs"}),
    "go": frozenset({".go"}),
    "java": frozenset({".java"}),
    "c": frozenset({".c", ".h"}),
    "cpp": frozenset({".cpp"}),
    "c++": frozenset({".cpp"}),
}
_STREAM_CAP = 16_000_000
_SUFFIX_GLOB = re.compile(r"^(?:(?:[A-Za-z0-9_.-]+/)+)?(?:\*\*/)?\*\.([A-Za-z0-9]+)$")
_INCLUDE_CACHE: dict[tuple, list[str] | None] = {}


def _suffix_glob(token: str) -> str | None:
    """``.py`` for ``*.py`` / ``src/**/*.py``. ``''`` for a negation. None if it is not an allow-list suffix."""
    text = (token or "").strip().strip("'\"")
    if text.startswith("!"):
        return ""
    match = _SUFFIX_GLOB.fullmatch(text)
    if match is None:
        return None
    suffix = "." + match.group(1).lower()
    if suffix not in _SOURCE_SUFFIXES:
        return None
    return suffix


def _allow_suffixes(program: str, args: list[str]) -> frozenset[str] | None:
    """Suffixes the search is limited to, or None when the file set is not on the allow-list."""
    found: set[str] = set()
    saw = False
    index = 0
    while index < len(args):
        arg = args[index]
        low = arg.lower()
        value = ""
        consume = 0
        if low.startswith("--include=") or low.startswith("--glob="):
            value = arg.split("=", 1)[1]
            consume = 1
        elif low in {"--include", "--glob", "-g"} and index + 1 < len(args):
            value = args[index + 1]
            consume = 2
        elif arg.startswith("-g") and len(arg) > 2 and not arg.startswith("--"):
            value = arg[2:]
            consume = 1
        elif program in _SEARCH_NAMES and (
            low == "--type"
            or arg == "-t"
            or low.startswith("--type=")
            or (arg.startswith("-t") and not arg.startswith("--") and len(arg) > 2)
        ):
            if low == "--type" or arg == "-t":
                if index + 1 >= len(args):
                    return None
                name = args[index + 1]
                step = 2
            elif "=" in arg:
                name = arg.split("=", 1)[1]
                step = 1
            else:
                name = arg[2:]
                step = 1
            mapped = _TYPE_SUFFIXES.get(name.lower())
            if not mapped:
                return None
            found |= set(mapped)
            saw = True
            index += step
            continue
        if consume:
            suffix = _suffix_glob(value)
            if suffix is None:
                return None
            if suffix:
                found.add(suffix)
                saw = True
            index += consume
            continue
        index += 1
    if not saw or not found:
        return None
    return frozenset(found)


def _search_unsafe(program: str, args: list[str]) -> bool:
    """Binary, unrestricted, or preprocessor modes read past a suffix include."""
    for arg in args:
        low = arg.lower()
        if low in {"--pre", "--pre-glob", "--text", "--binary", "--unrestricted"}:
            return True
        if low.startswith("--pre=") or low.startswith("--pre-glob=") or low.startswith("--binary-files") or low.startswith("--type-add"):
            return True
        if arg.startswith("-") and not arg.startswith("--") and arg[1:].isalpha():
            body = arg[1:]
            if program in _GREP_NAMES and "a" in body:
                return True
            if program in _SEARCH_NAMES and ("a" in body or "u" in body):
                return True
    return False


def _stream_secret(path: Path) -> bool:
    """Whole file up to 16 MB. Larger, unreadable, or a symlink is excluded."""
    from easyagent.safety import _sniff_blob

    try:
        if path.is_symlink() or not path.is_file():
            return True
        size = path.stat().st_size
    except OSError:
        return True
    if size > _STREAM_CAP:
        return True
    if size <= 0:
        return False
    try:
        with path.open("rb") as handle:
            blob = handle.read(size)
    except OSError:
        return True
    return _sniff_blob(blob) is not None


def _included_secret_names(root: Path, suffixes: frozenset[str]) -> list[str] | None:
    """Basenames to exclude. None means the walk did not finish, so the search cards."""
    try:
        fingerprint = _index_fingerprint(root)
    except Exception:
        return None
    key = (os.path.realpath(root) if fingerprint is not None else str(root), suffixes, fingerprint)
    if key in _INCLUDE_CACHE:
        return _INCLUDE_CACHE[key]
    if len(_INCLUDE_CACHE) > 8:
        _INCLUDE_CACHE.pop(next(iter(_INCLUDE_CACHE)), None)
    result = _scan_included(root, suffixes)
    _INCLUDE_CACHE[key] = result
    return result


def _scan_included(root: Path, suffixes: frozenset[str]) -> list[str] | None:
    deadline = time.monotonic() + _INDEX_BUDGET
    found: list[str] = []
    seen = 0
    try:
        if not root.is_dir():
            return found
    except OSError:
        return None
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if time.monotonic() > deadline or seen > _INDEX_MAX:
            return None
        try:
            depth = len(Path(dirpath).relative_to(root).parts)
        except ValueError:
            depth = 0
        if depth > _INDEX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name not in _INDEX_SKIP]
        for name in filenames:
            seen += 1
            if seen > _INDEX_MAX or time.monotonic() > deadline:
                return None
            if Path(name).suffix.lower() not in suffixes:
                continue
            if _stream_secret(Path(dirpath) / name):
                found.append(name)
    return found


def _literal_search_files(operands: list[str], cwd: Path) -> bool:
    """Every operand is one existing file, not a directory and not a glob."""
    if not operands:
        return False
    for token in operands:
        if token in {".", "./", ".."} or _glob(token) or _unresolved_words([token]):
            return False
        path = _try_resolve(token, cwd)
        if path is None:
            return False
        try:
            if path.is_dir() or not path.is_file():
                return False
        except OSError:
            return False
    return True


def _recursive_stage_reads(program: str, args: list[str], operands: list[str], cwd: Path) -> bool:
    """True when this recursive grep, rg, or find-grep must card."""
    if _search_unsafe(program, args):
        return True
    suffixes = _allow_suffixes(program, args)
    if suffixes is not None:
        bases: list[Path] = []
        for token in operands:
            if _unresolved_words([token]) or _glob(token):
                return True
            if _operand_secret(token, cwd, wide=True):
                path = _try_resolve(token, cwd)
                if path is None or not path.is_dir():
                    return True
            path = _try_resolve(token, cwd)
            if path is not None and path.is_dir():
                bases.append(path)
            elif token not in {".", "./"} and path is not None and path.is_file():
                if _stream_secret(path):
                    return True
        if not bases:
            if operands and not any(token in {".", "./"} for token in operands):
                return False
            bases = [cwd]
        excluded = _search_excludes(program, args)
        for base in bases:
            names = _included_secret_names(base, suffixes)
            if names is None:
                return True
            if any(name not in excluded for name in names):
                return True
        return False
    if _literal_search_files(operands, cwd):
        for token in operands:
            if _operand_secret(token, cwd, wide=True):
                return True
            path = _try_resolve(token, cwd)
            if path is not None and _stream_secret(path):
                return True
        return False
    return True


def shield_secrets(command: str, cwd: Path) -> str:
    """Exclude sniffed source files from a suffix-limited recursive grep or rg.

    A pathless search, ``--include='*'``, an unknown type, or a binary mode is
    left unchanged. Those card. No walk happens unless the include list is on
    the source allow-list.
    """
    text = command or ""
    if not _wants_search_exclude(text):
        return text
    folder = Path(cwd)
    for segment in _and_parts(text):
        for stage in _pipe_parts(segment):
            words, _redirects = _tokenize(stage)
            program, args = _split_program(words)
            recursive = program in _SEARCH_NAMES or (program in _GREP_NAMES and _has_recursive(args))
            if not recursive or _search_unsafe(program, args):
                continue
            allowed = _allow_suffixes(program, args)
            if allowed is None:
                continue
            operands = _positionals(program, args)
            bases = [folder]
            dirs = []
            for token in operands:
                path = _try_resolve(token, folder)
                if path is not None and path.is_dir():
                    dirs.append(path)
            if dirs:
                bases = dirs
            names: set[str] = set()
            for base in bases:
                found = _included_secret_names(base, allowed)
                if found is None:
                    return command or ""
                names.update(found)
            # GNU grep ignores --exclude when it appears before --include.
            # ripgrep ignores a negative glob that appears before a positive one.
            # Both flags go at the end of this stage, positives first.
            extra: list[str] = []
            if _stage_needs_type_globs(program, args):
                for suffix in sorted(allowed):
                    token = f"*.{suffix[1:]}"
                    if token not in stage:
                        extra.append(f"-g '{token}'")
            for name in sorted(names):
                if program in _GREP_NAMES:
                    if f"--exclude={name}" not in stage:
                        extra.append(f"--exclude={_quote_name(name)}")
                elif f"!{name}" not in stage:
                    extra.append(f"-g '!{_quote_name(name)}'")
            if extra:
                text = text.replace(stage, stage.rstrip() + " " + " ".join(extra), 1)
    return text


def _stage_needs_type_globs(program: str, args: list[str]) -> bool:
    """rg --type also matches suffixes outside the allow-list, so add explicit globs."""
    if program not in _SEARCH_NAMES:
        return False
    for arg in args:
        low = arg.lower()
        if arg == "-t" or low == "--type" or low.startswith("--type="):
            return True
        if arg.startswith("-t") and not arg.startswith("--") and len(arg) > 2:
            return True
    return False


def program_needs_globs(text: str) -> bool:
    """rg --type also matches suffixes outside the allow-list, so add explicit globs."""
    return bool(re.search(r"(?i)(?:^|[;&|\n]\s*)rg\b", text or "")) and bool(
        re.search(r"(?i)(?:--type(?:=|\s+)|(?:^|\s)-t(?:=|\s+)?)[A-Za-z0-9+]+", text or "")
    )


def _shield_rg_globs(text: str, suffixes: list[str]) -> str:
    missing = [suffix for suffix in suffixes if f"*.{suffix[1:]}" not in text]
    if not missing:
        return text
    flags = " ".join(f"-g '*.{suffix[1:]}'" for suffix in missing)
    return re.sub(r"(?i)\brg\b", "rg " + flags, text, count=1)


def _explicit_secret_operand(command: str, cwd: Path) -> bool:
    for segment in _and_parts(command):
        for stage in _pipe_parts(segment):
            words, _redirects = _tokenize(stage)
            program, args = _split_program(words)
            if program not in _GREP_NAMES and program not in _SEARCH_NAMES and program != "git":
                continue
            for token in _positionals(program, args):
                if token in {".", "./", ".."}:
                    continue
                path = _try_resolve(token, cwd)
                if path is not None and path.is_dir():
                    continue
                if _operand_secret(token, cwd, wide=True):
                    return True
    return False


def _shield_grep(text: str, names: list[str]) -> str:
    def repl(match: re.Match) -> str:
        program = match.group(1)
        flags = " ".join(f"--exclude={_quote_name(name)}" for name in names if f"--exclude={name}" not in text)
        if not flags:
            return match.group(0)
        return f"{program} {flags}"

    if not re.search(r"(?i)\b(?:grep|egrep|fgrep)\b[^\n;|&]*(?:--recursive|(?:^|\s)-[A-Za-z]*[rR][A-Za-z]*)", text):
        return text
    return re.sub(r"(?i)\b(grep|egrep|fgrep)\b", repl, text, count=1)


def _shield_rg(text: str, names: list[str]) -> str:
    if not re.search(r"(?i)(?:^|[;&|]\s*)rg\b", text):
        return text
    missing = [name for name in names if f"!{name}" not in text]
    if not missing:
        return text
    flags = " ".join(f"-g '!{_quote_name(name)}'" for name in missing)
    return re.sub(r"(?i)\brg\b", "rg " + flags, text, count=1)


def _stage_end(text: str, origin: int) -> int:
    """Index of the next top-level pipe or separator at or after ``origin``."""
    quote = ""
    i = origin
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            i += 1
            continue
        if text.startswith("&&", i) or text.startswith("||", i):
            return i
        if ch in {"|", ";", "\n"}:
            return i
        if ch == "&":
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if prev in {">", "<"} or nxt in {">", "<", "|"}:
                i += 1
                continue
            return i
        i += 1
    return len(text)


def _shield_git_stage(stage: str, rels: list[str], cwd: Path) -> str:
    words, _redirects = _tokenize(stage)
    program, args = _split_program(words)
    if program != "git":
        return stage
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-C", "--git-dir", "--work-tree", "--namespace", "-c"}:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        break
    if index >= len(args):
        return stage
    sub = args[index]
    rest = args[index + 1 :]
    # Pathless git grep, show, log, and blame keep their card. diff can exclude and run.
    if sub != "diff":
        return stage
    if _git_metadata_only(rest):
        return stage
    paths, excludes = _git_body_paths(rest, cwd)
    if paths and not any(token in {".", "./"} for token in paths):
        return stage
    needed = [rel for rel in rels if rel not in excludes and Path(rel).name not in excludes]
    if not needed:
        return stage
    extra = " ".join(_quote_pathspec(rel) for rel in needed)
    if "--" in stage:
        return stage.rstrip() + " " + extra
    return stage.rstrip() + " -- . " + extra


def _shield_git(text: str, rels: list[str], cwd: Path) -> str:
    """Exclude secret paths on the git stage itself, before a pipe or ``&&``."""
    if not re.search(r"(?i)(?:^|[;&|]\s*)git\b", text):
        return text
    cursor = 0
    pieces: list[str] = []
    while cursor < len(text):
        match = re.search(r"(?i)(?:^|[;&|]\s*)git\b", text[cursor:])
        if match is None:
            pieces.append(text[cursor:])
            break
        git_at = cursor + match.end() - 3
        end = _stage_end(text, git_at)
        pieces.append(text[cursor:git_at])
        stage = _shield_git_stage(text[git_at:end], rels, cwd)
        if end < len(text) and text[end] in {"|", "&", ";", "\n"} and not stage.endswith((" ", "\t")):
            stage += " "
        pieces.append(stage)
        cursor = end
    return "".join(pieces)


def _quote_pathspec(rel: str) -> str:
    """A git pathspec the shell will not treat as a subshell."""
    text = ":(exclude)" + (rel or "")
    return "'" + text.replace("'", "'\\''") + "'"


def _quote_name(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./@+-]+", name or ""):
        return name
    return "'" + name.replace("'", "'\\''") + "'"


_NON_READERS = frozenset({
    "echo", "printf", "true", "false", ":", "pwd", "whoami", "date", "uname",
    "hostname", "id", "which", "where", "exit", "return", "export", "readonly",
    "local", "unset", "umask", "hash", "type", "shift", "break", "continue",
    "test", "[", "[[", "set", "cd", "pushd", "popd", "mkdir", "rmdir", "rm",
    "if", "then", "else", "elif", "fi", "do", "done", "case", "esac", "in",
    "get-childitem", "gci", "format-table", "format-list",
    "set-location", "push-location", "pop-location", "sl", "gradlew", "mvn",
    "unlink", "del", "erase", "remove-item", "ri", "out-file", "move-item",
    "copy-item", "rename-item", "set-content", "add-content", "new-item",
    "clear-content", "clear-item", "touch", "chmod", "chown", "chgrp", "ls", "dir",
    "stat", "sleep", "wait", "kill", "jobs", "fg", "bg", "ulimit", "alias",
    "unalias", "builtin", "command", "times", "trap", "getopts", "git", "npm",
    "npx", "yarn", "pnpm", "pip", "pip3", "pytest", "cargo", "make", "true",
})
_GREP_NAMES = frozenset({"grep", "egrep", "fgrep"})
_SEARCH_NAMES = frozenset({"rg", "ag", "ack"})
_VALUE_FLAGS = frozenset({
    "-d", "-f", "-n", "-m", "-A", "-B", "-C", "-g", "-t", "-o", "-T", "-a",
    "-b", "-c", "-e", "-F", "-I", "-L", "-S", "-W", "-k", "--glob", "--file",
    "--regexp", "--include", "--exclude", "--exclude-dir", "--max-count",
    "--delimiter", "--fields",
})
_WIDE_GLOBS = frozenset({"*", ".*", "**", "**/*", "./*", "./.*"})
_PEEL = frozenset({
    "command", "builtin", "exec", "env", "busybox", "nohup", "timeout",
    "nice", "stdbuf", "time", "sudo", "doas", "chrt", "ionice", "setsid", "watch",
})
_WRAPPER_VALUES = {
    "exec": {"-a"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
    "nice": {"-n", "--adjustment"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "--user", "--group"},
    "doas": {"-u"},
    "watch": {"-n", "--interval"},
    "chrt": {"-p"},
    "ionice": {"-c", "-n", "-p"},
}


def _command_reads(text: str, cwd: Path, depth: int = 0, root: Path | None = None) -> bool:
    """Walk one command line. ``cd`` changes the cwd used for later operands."""
    if depth == 0:
        try:
            from easyagent.strictshell import expand_literal_assignments

            expanded = expand_literal_assignments(text)
        except Exception:
            expanded = None
        if isinstance(expanded, str) and expanded.strip() and expanded.strip() != (text or "").strip():
            text = expanded
    if plain_loop(text, cwd):
        return False
    base = root or cwd
    if depth > 6:
        return _tree_has_secret(base, recursive=True)
    if depth == 0 and _staged_code_reads(text, base):
        return True
    if _for_loop_reads(text, cwd):
        return True
    location: Path | None = cwd
    previous: Path | None = None
    stack: list[Path] = []
    lost = False
    for segment in _and_parts(text):
        nested = _subshell_body(segment)
        if nested is not None:
            if _command_reads(nested, location or base, depth + 1, root=base):
                return True
            continue
        here = location or base
        expanded = _expand_pwd(segment, here, previous)
        if _raw_operand_secret(expanded, here):
            return True
        handled, moved, stack = _directory_update(expanded, here, stack)
        if handled:
            if moved is not None and moved != here:
                previous = here
            if moved is None:
                lost = True
                location = None
            else:
                lost = False
                location = moved
            continue
        if lost:
            if _uncertain_reads(expanded, base):
                return True
            continue
        if _pipeline_reads(expanded, location or base, depth, root=base):
            return True
    return False


def _and_parts(command: str) -> list[str]:
    """Split on ``&&``, ``||``, and ``;`` outside quotes and parentheses."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    paren = 0
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
        if ch == "(":
            paren += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")" and paren:
            paren -= 1
            buf.append(ch)
            i += 1
            continue
        if paren == 0 and (text.startswith("&&", i) or text.startswith("||", i)):
            parts.append("".join(buf).strip())
            buf = []
            i += 2
            continue
        if paren == 0 and ch == "&":
            prev = buf[-1] if buf else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            # 2>&1, >&1, &>, <&1, and &| are redirects or pipes, not separators.
            if prev in {">", "<"} or nxt in {">", "<", "|"}:
                buf.append(ch)
                i += 1
                continue
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        if paren == 0 and ch in {";", "\n"}:
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part] or [text.strip()]


def _pipe_parts(segment: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    text = segment or ""
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
        if text.startswith("|&", i) or text.startswith("&|", i):
            parts.append("".join(buf).strip())
            buf = []
            i += 2
            continue
        if ch == "|" and not text.startswith("||", i):
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part] or [text.strip()]


def _tokenize(statement: str) -> tuple[list[str], list[str]]:
    words: list[str] = []
    redirects: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    text = statement or ""

    def flush() -> None:
        if buf:
            words.append("".join(buf))
            buf.clear()

    while i < len(text):
        ch = text[i]
        if quote:
            if quote == '"' and ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 2
                    continue
                quote = ""
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "\\":
            if i + 1 < len(text):
                buf.append(text[i + 1])
            i += 2
            continue
        if ch in {"'", '"'}:
            # A quoted empty word is an argument (`grep "" .npmrc`). `""file` stays one word.
            if not quote and not buf and i + 1 < len(text) and text[i + 1] == ch:
                nxt = text[i + 2] if i + 2 < len(text) else ""
                if nxt == "" or nxt.isspace() or nxt in {"<", ">", "|", "&", ";"}:
                    words.append("")
                    i += 2
                    continue
            quote = ch
            i += 1
            continue
        if ch == "$" and i + 1 < len(text) and text[i + 1] in {"(", "{"}:
            opener = text[i + 1]
            closer = ")" if opener == "(" else "}"
            buf.append(ch)
            buf.append(opener)
            i += 2
            nest = 1
            while i < len(text) and nest:
                buf.append(text[i])
                if text[i] == opener:
                    nest += 1
                elif text[i] == closer:
                    nest -= 1
                i += 1
            continue
        if ch == "`":
            buf.append(ch)
            i += 1
            while i < len(text) and text[i] != "`":
                buf.append(text[i])
                i += 1
            if i < len(text):
                buf.append(text[i])
                i += 1
            continue
        if ch.isspace():
            flush()
            i += 1
            continue
        if ch in {"<", ">"}:
            flush()
            op = ch
            if i + 1 < len(text) and text[i + 1] == ch:
                op = ch + ch
                i += 1
            i += 1
            while i < len(text) and text[i].isspace():
                i += 1
            if op == "<" and i < len(text) and text[i] == "(":
                i += 1
                continue
            target: list[str] = []
            while i < len(text) and not text[i].isspace():
                target.append(text[i])
                i += 1
            if op == "<" and target:
                redirects.append("".join(target))
            continue
        buf.append(ch)
        i += 1
    flush()
    return words, redirects


def _pipeline_reads(segment: str, cwd: Path, depth: int, root: Path | None = None) -> bool:
    stages = _pipe_parts(segment)
    produced: list[Path] | None = None
    known = False
    location: Path | None = cwd
    stack: list[Path] = []
    lost = False
    for index, stage in enumerate(stages):
        handled, moved, stack = _directory_update(stage, location or cwd, stack)
        if handled:
            if moved is None:
                lost = True
                location = None
            else:
                lost = False
                location = moved
            produced = None
            known = False
            continue
        here = location or cwd
        if lost:
            if _uncertain_reads(stage, cwd):
                return True
            produced = None
            known = False
            continue
        words, redirects = _tokenize(stage)
        if any(_operand_secret(item, here, wide=True) for item in redirects):
            return True
        if _process_sub_reads(stage, here, depth):
            return True
        program, args = _split_program(words)
        if not program and not args:
            if any(mark in " ".join(words) for mark in ("$(", "${", "`")) and _tree_has_secret(here, recursive=True):
                return True
            produced = None
            known = False
            continue
        if program == "find":
            if _find_exec(stage) or _find_option_blocks(args):
                return True
            produced = None
            known = False
            continue
        if program == "xargs":
            return True
        if program in {"ls", "dir"} and index + 1 < len(stages):
            nxt = _split_program(_tokenize(stages[index + 1])[0])[0]
            produced = _ls_names(args, here)
            known = produced is not None
            if nxt in {"xargs", "while"} and produced is None and _tree_has_secret(here, recursive=True):
                return True
            if nxt == "while" and produced is not None and any(_path_secret(path) for path in produced):
                return True
            continue
        if _simple_reads(program, args, here, depth, root=root or cwd):
            return True
        produced = None
        known = False
    return False


def _split_program(words: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(words) and "=" in words[index] and not words[index].startswith(("'", '"', "$", "/")):
        head = words[index].split("=", 1)[0]
        if head.replace("_", "").isalnum() and index + 1 < len(words):
            index += 1
            continue
        break
    if index >= len(words):
        return "", []
    token = words[index]
    if token.startswith("$(`") or token.startswith("$(") or token.startswith("`") or token.startswith("${"):
        return "", words[index + 1 :]
    return _program_name(token), words[index + 1 :]


def _wrapper_tail(program: str, args: list[str]) -> list[str]:
    """Arguments after a wrapper's own flags. `timeout` also drops the duration."""
    valued = _WRAPPER_VALUES.get(program, set())
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            rest = args[index + 1 :]
            if program == "timeout" and rest:
                return rest[1:]
            return rest
        head = arg.split("=", 1)[0]
        if head in valued:
            index += 1 if "=" in arg else 2
            continue
        if arg.startswith("-") and arg != "-":
            index += 1
            continue
        if program == "env" and "=" in arg:
            index += 1
            continue
        if program == "timeout":
            return args[index + 1 :]
        return args[index:]
    return []


def _subshell_body(part: str) -> str | None:
    text = (part or "").strip()
    if len(text) < 2 or not text.startswith("(") or not text.endswith(")"):
        return None
    depth = 0
    for index, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return None
    if depth != 0:
        return None
    return text[1:-1].strip()


def _token_unresolved(token: str) -> bool:
    return any(mark in (token or "") for mark in ("$(", "${", "`", "$"))


def _directory_update(part: str, cwd: Path, stack: list[Path]) -> tuple[bool, Path | None, list[Path]]:
    """A bare ``cd``, ``pushd``, or ``popd``. The new cwd is None when it cannot be proved."""
    words, _redirects = _tokenize(part)
    program, args = _split_program(words)
    if program not in {"cd", "pushd", "popd"}:
        return False, cwd, stack
    if "|" in part:
        return False, cwd, stack
    if program == "popd":
        if stack:
            return True, stack.pop(), stack
        return True, None, stack
    if program == "pushd" and not args:
        return True, None, stack
    dest = args[0] if args else ""
    if program == "cd" and not dest:
        dest = str(Path.home())
    if not dest or dest == "-" or _token_unresolved(dest):
        return True, None, stack
    dest = _rewrite_proc(dest, cwd)
    if str(dest).replace("\\", "/").startswith("/proc/"):
        return True, None, stack
    if program == "pushd":
        stack = [*stack, cwd]
    path = _try_resolve(dest, cwd)
    if path is None or not path.is_dir():
        return True, cwd, stack[:-1] if program == "pushd" else stack
    return True, path, stack


def _uncertain_reads(part: str, root: Path) -> bool:
    """A later read after a cd whose destination cannot be proved."""
    words, redirects = _tokenize(part)
    if any(_token_unresolved(item) or not os.path.isabs(item) for item in redirects if item):
        return _tree_has_secret(root, recursive=True)
    program, args = _split_program(words)
    if program in {"echo", "printf", "true", "false", ":", "pwd", "whoami", "date", "uname"}:
        return False
    if _all_proved_nonsecret(program, args, root):
        return False
    return _tree_has_secret(root, recursive=True)


def _all_proved_nonsecret(program: str, args: list[str], cwd: Path) -> bool:
    if program in {"eval", "source", "."} or program in _SHELLS or program == "git":
        return False
    if any(_token_unresolved(arg) for arg in args):
        return False
    operands = _positionals(program, args)
    if not operands:
        return program in _NON_READERS
    for token in operands:
        if not os.path.isabs(token):
            return False
        if _operand_secret(token, cwd, wide=True):
            return False
        path = _try_resolve(token, cwd)
        if path is None or not path.is_file():
            return False
    return True


_RISKY_INLINE = re.compile(
    r"(?i)(?:\b(?:system|getline|open|readFileSync|readFile|exec(?:Sync|File|FileSync)?|"
    r"spawn(?:Sync)?|popen|execute|eval|subprocess)\b|child_process|fs\.|os\.popen|os\.system|`|\$\()"
)
_INLINE_COMMAND = re.compile(
    r"(?i)^(?:cat|head|tail|sort|uniq|grep|egrep|fgrep|sed|awk|gawk|cut|nl|fold|tac|od|xxd|"
    r"strings|base64|more|less|paste|rev|sh|bash|dash|zsh)\b"
)


def _inline_code_reads(code: str, cwd: Path, depth: int) -> bool:
    """An inline program that can read a file. Safe only when every operand is a non-secret file."""
    if not code or not _RISKY_INLINE.search(code):
        return False
    strings = [text for _quote, text in re.findall(r"""(['"])(.*?)\1""", code)]
    if not strings:
        return _tree_has_secret(cwd, recursive=True)
    proved = True
    for text in strings:
        if not text or _token_unresolved(text):
            proved = False
            continue
        if _INLINE_COMMAND.search(text) or re.search(r"\s", text):
            if _command_reads(text, cwd, depth + 1):
                return True
            continue
        if _operand_secret(text, cwd, wide=True):
            return True
        path = _try_resolve(text, cwd)
        if path is None or not path.is_file():
            if "/" in text or text.startswith("."):
                proved = False
    if proved:
        return False
    return _tree_has_secret(cwd, recursive=True)


def _eval_or_source_reads(program: str, args: list[str], cwd: Path, depth: int) -> bool:
    if program in {"source", "."}:
        if not args:
            return False
        token = args[0]
        if _token_unresolved(token):
            return _tree_has_secret(cwd, recursive=True)
        path = _try_resolve(token, cwd)
        if path is None or not path.is_file():
            return _tree_has_secret(cwd, recursive=True)
        if _path_secret(path):
            return True
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return _tree_has_secret(cwd, recursive=True)
        return _command_reads(body, path.parent, depth + 1)
    if not args:
        return False
    text = " ".join(args)
    if any(_token_unresolved(arg) for arg in args):
        if _command_reads(text, cwd, depth + 1):
            return True
        return _tree_has_secret(cwd, recursive=True)
    return _command_reads(text, cwd, depth + 1)


def _script_dash_c(args: list[str], cwd: Path, depth: int) -> bool:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-c", "--command"}:
            command = args[index + 1] if index + 1 < len(args) else ""
            if not command or _token_unresolved(command):
                return _tree_has_secret(cwd, recursive=True)
            return _command_reads(command, cwd, depth + 1)
        index += 1
    return False


def _ssh_reads(args: list[str], cwd: Path, depth: int) -> bool:
    valued = {"-o", "-i", "-p", "-l", "-F", "-J", "-b", "-c", "-D", "-L", "-R", "-S", "-E", "-W"}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in valued:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        remote = " ".join(args[index + 1 :])
        if not remote.strip():
            return False
        if _token_unresolved(remote):
            return _tree_has_secret(cwd, recursive=True)
        return _command_reads(remote, cwd, depth + 1)
    return False


_DIFF_REFUSE = {"--ext-diff", "--textconv", "--output", "-c", "--exec"}


def _git_name_lines(cwd: Path, argv: list[str]) -> list[str] | None:
    """Names from a git name-only command. None means the list failed."""
    import subprocess

    for item in argv:
        if any(mark in item for mark in ("$(", "`", "\n", "\x00")):
            return None
    env = os.environ.copy()
    env["GIT_EXTERNAL_DIFF"] = ""
    env["GIT_PAGER"] = "cat"
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "-c", "diff.external=", "-c", "core.pager=cat", *argv],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _git_diff_names(cwd: Path, rest: list[str]) -> list[str] | None:
    argv: list[str] = []
    for item in rest:
        flag = item.split("=", 1)[0]
        if flag in _DIFF_REFUSE or flag.startswith("--output"):
            return None
        argv.append(item)
    return _git_name_lines(cwd, ["diff", "--name-only", "--no-ext-diff", "--no-textconv", *argv])


def _git_log_names(cwd: Path, rest: list[str]) -> list[str] | None:
    argv: list[str] = []
    for item in rest:
        if item in {"-p", "--patch", "-u", "--raw"} or item.startswith("--patch"):
            continue
        if item.startswith("-") and not item.startswith("--") and "p" in item[1:] and item[1:].isalpha():
            return None
        flag = item.split("=", 1)[0]
        if flag in _DIFF_REFUSE:
            return None
        argv.append(item)
    return _git_name_lines(cwd, ["log", "--name-only", "--pretty=format:", *argv])


def _listed_names_secret(names: list[str], cwd: Path) -> bool:
    """True when a diff path is secret, or the name list is too large to prove."""
    if len(names) > 400:
        return True
    for name in names:
        text = name.strip().strip("'\"")
        if not text or text in {".", "./"} or _token_unresolved(text) or _glob(text):
            return True
        path = _try_resolve(text, cwd)
        if path is None:
            return True
        try:
            if _file_secret(path):
                return True
        except Exception:
            return True
    return False


def _git_reads(args: list[str], cwd: Path) -> bool:
    """git grep, show, cat-file, diff, log -p, blame, and archive read file bodies."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-C", "--git-dir", "--work-tree", "--namespace", "-c"}:
            if arg == "-C" and index + 1 < len(args):
                path = _try_resolve(args[index + 1], cwd)
                if path is not None and path.is_dir():
                    cwd = path
            index += 2
            continue
        if arg.startswith("--git-dir=") or arg.startswith("--work-tree="):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        break
    if index >= len(args):
        return False
    sub = args[index]
    rest = args[index + 1 :]
    if _git_metadata_only(rest) and sub in {"diff", "show", "log"}:
        return False
    if sub == "grep":
        paths = _git_paths(rest, pattern=True)
        revs = [item for item in paths if _git_rev_token(item, cwd)]
        files = [item for item in paths if item not in revs and not _git_exclude_token(item)]
        if files and _paths_secret_or_uncertain(files, cwd):
            return True
        if revs:
            return any(_revision_has_secret(cwd, rev) for rev in revs)
        if not files:
            return _tree_has_secret(cwd, recursive=True)
        return False
    if sub == "diff":
        if _git_metadata_only(rest):
            return False
        paths, _excludes = _git_body_paths(rest, cwd)
        specific = [item for item in paths if item not in {".", "./"}]
        if specific:
            return _paths_secret_or_uncertain(specific, cwd)
        names = _git_diff_names(cwd, rest)
        if names is None:
            # Not a repository, or the name list failed. A tree with no secret
            # cannot print one. A tree that has one still cards.
            return _tree_has_secret(cwd, recursive=True)
        return _listed_names_secret(names, cwd)
    if sub == "log":
        shows = any(
            item in {"-p", "--patch", "-u", "--raw"} or item.startswith("-p") or item.startswith("--patch")
            for item in rest
        )
        if not shows:
            return False
        paths, _excludes = _git_body_paths(rest, cwd)
        specific = [item for item in paths if item not in {".", "./"}]
        if specific:
            return _paths_secret_or_uncertain(specific, cwd)
        names = _git_log_names(cwd, rest)
        if names is None:
            return _tree_has_secret(cwd, recursive=True)
        return _listed_names_secret(names, cwd)
    if sub == "blame":
        paths = [item for item in rest if item != "--" and not item.startswith("-") and not _git_exclude_token(item) and not _git_rev_token(item, cwd)]
        if not paths:
            return _tree_has_secret(cwd, recursive=True)
        return _paths_secret_or_uncertain(paths, cwd)
    if sub in {"show", "cat-file", "archive"}:
        paths = [
            item
            for item in _git_paths(rest, pattern=False)
            if _pathish(item, cwd) and not _git_exclude_token(item) and not _git_rev_token(item, cwd)
        ]
        revs = [item for item in rest if _git_rev_token(item, cwd) and ":" not in item]
        if paths and _paths_secret_or_uncertain(paths, cwd):
            return True
        if revs:
            return any(_revision_has_secret(cwd, rev) for rev in revs)
        if paths:
            return False
        return _tree_has_secret(cwd, recursive=True)
    if sub == "stash" and any(item in {"show", "-p"} or item.startswith("-p") for item in rest):
        return _tree_has_secret(cwd, recursive=True) or any(
            _revision_has_secret(cwd, rev) for rev in _git_rev_args(rest, cwd)
        )
    return False


def _git_metadata_only(rest: list[str]) -> bool:
    """``--stat`` and name-only forms print no file body."""
    saw = False
    for item in rest:
        if item in {"-p", "-u", "--patch", "--raw"} or item.startswith("--patch"):
            return False
        if item.startswith("-") and not item.startswith("--") and "p" in item[1:] and item[1:].isalpha():
            return False
        if item in {"--stat", "--name-only", "--name-status", "--numstat", "--shortstat", "--dirstat"} or item.startswith("--stat"):
            saw = True
    return saw


def _git_rev_token(token: str, cwd: Path | None = None) -> bool:
    """A branch, tag, stash, ref, SHA, or range. An existing worktree file stays a path."""
    text = (token or "").strip().strip("'\"")
    if not text or text.startswith("-") or text.startswith(".") or _git_exclude_token(text):
        return False
    if ":" in text and not text.startswith("refs/"):
        return False
    if text in {"HEAD", "@"} or text.startswith("HEAD") or (text.startswith("@") and text != "@"):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", text):
        return True
    if text.startswith("refs/"):
        return True
    if re.fullmatch(r"stash@\{\d+\}", text):
        return True
    if ".." in text and not text.startswith("./"):
        return True
    if "/" in text:
        return False
    if cwd is None:
        return False
    path = _try_resolve(text, cwd)
    if path is not None and path.exists():
        return False
    return True


def _git_rev_args(rest: list[str], cwd: Path) -> list[str]:
    found: list[str] = []
    for item in rest:
        if item == "--":
            break
        if item.startswith("-") or _git_exclude_token(item):
            continue
        if _git_rev_token(item, cwd):
            found.append(item.strip().strip("'\""))
    return found


def _git_exclude_token(token: str) -> bool:
    text = (token or "").strip()
    return text.startswith(":(exclude)") or text.startswith(":!") or text.startswith(":(exclude,")


def _git_excludes(args: list[str]) -> set[str]:
    found: set[str] = set()
    for item in args:
        text = (item or "").strip().strip("'\"")
        if text.startswith(":(exclude)"):
            found.add(text[len(":(exclude)") :].lstrip("/"))
        elif text.startswith(":!"):
            found.add(text[2:].lstrip("/"))
    return {item for item in found if item}


def _git_body_paths(rest: list[str], cwd: Path) -> tuple[list[str], set[str]]:
    excludes = _git_excludes(rest)
    if "--" in rest:
        tail = rest[rest.index("--") + 1 :]
    else:
        tail = [item for item in rest if not item.startswith("-")]
    paths = [
        item
        for item in tail
        if item and not _git_exclude_token(item) and not _git_rev_token(item, cwd) and (_pathish(item, cwd) or item in {".", "./"})
    ]
    return paths, excludes


def _secrets_remain(cwd: Path, excludes: set[str]) -> bool:
    entries = _secret_entries(cwd)
    if not entries:
        return False
    if len(entries) > 400:
        return True
    covered = {item.lstrip("./") for item in excludes}
    for rel, _name in entries:
        if rel not in covered and Path(rel).name not in covered:
            return True
    return False


def _revision_has_secret(cwd: Path, rev: str) -> bool:
    """A tracked path at ``rev`` whose name is secret-class, or a small blob that sniffs secret.

    More than 80 names cannot be proved in this budget, so that revision cards.
    Blob sniffing stays on the first 8 KB of at most 40 files.
    """
    import subprocess

    from easyagent.safety import _classify_names, _sniff_blob

    if not re.fullmatch(r"[A-Za-z0-9_./~^@{}-]+", rev or ""):
        return True
    try:
        listed = subprocess.run(
            ["git", "-C", str(cwd), "ls-tree", "-r", "--name-only", rev],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if listed.returncode != 0:
        # Outside a repository the command cannot print a blob, so it cards
        # only when this folder already holds a secret. A bad revision inside
        # a repository still cards, including ``git show refs/stash``.
        err = (listed.stderr or "").lower()
        if "not a git repository" in err:
            return _tree_has_secret(cwd, recursive=True)
        return True
    names = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if len(names) > 80:
        return True
    sniff_names: list[str] = []
    for name in names:
        try:
            named = _classify_names(Path(name))
        except Exception:
            return True
        if named:
            return True
        sniff_names.append(name)
    sniff_names = sniff_names[:40]
    if not sniff_names:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "cat-file", "--batch"],
            input="".join(f"{rev}:{name}\n" for name in sniff_names).encode(),
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    blob = proc.stdout or b""
    cursor = 0
    for _name in sniff_names:
        end = blob.find(b"\n", cursor)
        if end < 0:
            return True
        header = blob[cursor:end]
        cursor = end + 1
        parts = header.split()
        if len(parts) < 3 or parts[1] != b"blob":
            continue
        try:
            size = int(parts[2])
        except ValueError:
            return True
        chunk = blob[cursor : cursor + min(size, 8192)]
        cursor += size + 1
        if _sniff_blob(chunk):
            return True
    return False


def _pathish(token: str, cwd: Path) -> bool:
    """A path operand, not a revision name such as ``HEAD``."""
    if not token:
        return False
    if "/" in token or token.startswith("."):
        return True
    path = _try_resolve(token, cwd)
    return path is not None and path.exists()


def _git_paths(args: list[str], *, pattern: bool) -> list[str]:
    paths: list[str] = []
    saw_pattern = not pattern
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            paths.extend(args[index + 1 :])
            break
        if arg.startswith("-"):
            index += 1
            continue
        if ":" in arg and not arg.startswith("./") and not arg.startswith("/"):
            paths.append(arg.split(":", 1)[1])
            index += 1
            continue
        if pattern and not saw_pattern:
            saw_pattern = True
            index += 1
            continue
        paths.append(arg)
        index += 1
    return [item for item in paths if item]


def _paths_secret_or_uncertain(paths: list[str], cwd: Path) -> bool:
    uncertain = False
    for token in paths:
        if _token_unresolved(token):
            uncertain = True
            continue
        if _operand_secret(token, cwd, wide=True):
            return True
    if uncertain:
        return _tree_has_secret(cwd, recursive=True)
    return False


_PROC_CWD = re.compile(r"^/proc/(?:self|thread-self|\d+)/(cwd|root|exe)(?:/(.*))?$")


def _rewrite_proc(raw: str, cwd: Path) -> str:
    """`/proc/self/cwd` is this command's folder, not the Python process folder."""
    text = (raw or "").replace("\\", "/")
    match = _PROC_CWD.match(text)
    if not match:
        return raw
    kind = match.group(1)
    base = cwd if kind == "cwd" else Path("/")
    rest = match.group(2) or ""
    return str(base / rest) if rest else str(base)


def _env_split_command(args: list[str]) -> str | None:
    """The command string of ``env -S`` / ``--split-string``, if this invocation has one."""
    valued = {"-u", "--unset", "-C", "--chdir"}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-S", "--split-string"}:
            return args[index + 1] if index + 1 < len(args) else ""
        if arg.startswith("--split-string="):
            return arg.split("=", 1)[1]
        if arg.startswith("-S") and not arg.startswith("--"):
            return arg[2:]
        head = arg.split("=", 1)[0]
        if head in valued:
            index += 1 if "=" in arg else 2
            continue
        if arg.startswith("-") and arg != "-":
            index += 1
            continue
        if "=" in arg:
            index += 1
            continue
        return None
    return None


# Programs whose options this gate understands. Anything else can read a file we cannot see.
_ALLOW_PROGRAMS = frozenset({
    "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "sed", "sort", "uniq", "cut", "ls", "dir", "find", "nl", "tac", "less", "more",
    "diff", "od", "strings", "hexdump", "md5sum", "sha1sum", "sha256sum", "cksum",
    "file", "stat", "du", "df", "free", "ps", "which", "whereis", "type",
    "sh", "bash", "dash", "zsh", "ksh", "ash",
    "python", "python2", "python3", "py", "pytest",
    "node", "nodejs", "bun", "deno", "ruby", "perl", "php", "lua", "tclsh",
    "awk", "gawk", "mawk", "nawk",
    "git", "npm", "npx", "yarn", "pnpm", "pip", "pip3", "make", "gmake",
    "go", "cargo", "docker", "tsc",
    "echo", "printf", "true", "false", "pwd", "whoami", "date", "uname", "hostname", "id",
    "if", "then", "else", "elif", "fi", "do", "done", "while", "for", "case", "esac",
    "select", "until", "in",
    "get-content", "gc", "get-childitem", "gci", "format-table", "format-list",
    "set-location", "push-location", "pop-location", "sl",
    "gradlew", "mvn", "cmake", "ninja", "poetry", "tox", "just", "task",
    "mkdir", "rmdir", "touch", "cp", "mv", "rm", "chmod", "ln", "cd", "pushd", "popd",
    "sleep", "test", "[", "[[",
    "env", "timeout", "time", "nice", "command", "nohup", "sudo", "doas", "exec", "builtin",
    "stdbuf", "watch", "setsid", "ionice", "chrt", "busybox",
    "tar", "zip", "unzip", "curl", "wget", "base64", "xxd", "jq", "yq", "tee", "tree",
    "rsync", "robocopy",
    "ssh", "eval", "source", "script",
})


def _env_blocks(args: list[str]) -> bool:
    """``env -C``, ``--chdir``, and ``-S`` change the directory or hide a command string."""
    for arg in args:
        if arg in {"-C", "--chdir", "-S", "--split-string"}:
            return True
        if arg.startswith(("--chdir=", "--split-string=")):
            return True
        if arg.startswith("-S") and not arg.startswith("--"):
            return True
        if arg.startswith("-C") and not arg.startswith("--"):
            return True
    return False


def _shell_has_command_flag(args: list[str]) -> bool:
    """Any short cluster containing ``c``, or ``--command``. The inner text is not trusted."""
    code, _files = _shell_inline_pieces(args)
    return code is not None


def _find_option_blocks(args: list[str]) -> bool:
    blocked = {"-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls", "-delete"}
    return any(arg in blocked for arg in args)


def _local_script(program: str, cwd: Path) -> Path | None:
    for token in (program, "./" + program):
        path = _try_resolve(token, cwd)
        if path is not None and path.is_file():
            return path
    return None


def _file_body_reads(path: Path, cwd: Path, depth: int) -> bool:
    if _path_secret(path):
        return True
    try:
        body = path.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return True
    if path.suffix.lower() in {".py"}:
        return bool(_python_code_reads(body, cwd, depth) or _code_body_reads(body, cwd, depth))
    if path.suffix.lower() in {".sh", ".bash"} or True:
        if _code_body_reads(body, cwd, depth):
            return True
        return _command_reads(body, path.parent, depth + 1)
    return True


def _simple_reads(program: str, args: list[str], cwd: Path, depth: int, root: Path | None = None) -> bool:
    # On Windows ``type`` reads a file. On Linux it is a builtin. A secret
    # operand cards in both places. ``type file.txt`` still does not.
    if program == "type":
        operands = [arg for arg in args if arg != "--" and not (arg.startswith("-") and arg != "-")]
        return any(_operand_secret(token, cwd, wide=True) for token in operands)
    if program == "xargs":
        return True
    if program == "find" and _find_option_blocks(args):
        return True
    if program == "env" and _env_blocks(args):
        return True
    if program in _SHELLS and _shell_has_command_flag(args):
        return True
    if program in {"if", "elif", "while", "until", "then", "else"} and args:
        return _simple_reads(_program_name(args[0]), args[1:], cwd, depth, root)
    if program in {"fi", "done", "esac", "do", "in"}:
        return False
    # Wrappers such as ``command`` and ``builtin`` are also listed as non-readers.
    # They still have to peel, or ``command cat .npmrc`` would skip the card.
    if program in _NON_READERS and program not in _PEEL and program != "git":
        return False
    if program in {"seq", "yes", "clear", "reset", "tty"}:
        return False
    if program and program not in _ALLOW_PROGRAMS and program not in _PEEL and program not in {".", "source"}:
        path = _local_script(program, cwd)
        if path is None:
            # ``./run.sh`` before the file exists cannot read a secret.
            return not program.endswith((".sh", ".bash", ".py", ".js", ".ts", ".mjs", ".rb", ".pl", ".php", ".lua"))
        return _file_body_reads(path, cwd, depth)
    if program == "env":
        split = _env_split_command(args)
        if split is not None:
            return _command_reads(split, cwd, depth + 1)
    if program in _PEEL:
        rest = _wrapper_tail(program, args)
        if not rest:
            return False
        if len(rest) == 1 and re.search(r"\s", rest[0]):
            return _command_reads(rest[0], cwd, depth + 1)
        return _simple_reads(_program_name(rest[0]), rest[1:], cwd, depth)
    if program in {"eval", "source", "."}:
        return _eval_or_source_reads(program, args, cwd, depth)
    if program == "script":
        return _script_dash_c(args, cwd, depth)
    if program == "ssh":
        return _ssh_reads(args, cwd, depth)
    if program == "git":
        return _git_reads(args, cwd)
    if not program or any(mark in program for mark in ("$(", "${", "`")):
        if any(_operand_secret(arg, cwd, wide=True) for arg in args):
            return True
        return _tree_has_secret(cwd, recursive=True)
    if program in _PYTHONS or program in _SHELLS or program in _OTHERS or program in {"lua", "tclsh"}:
        if _interpreter_reads(program, args, cwd, depth):
            return True
        code, _files = _interpreter_pieces(program, args)
        if code and _inline_code_reads(code, cwd, depth):
            return True
        if program in _PYTHONS or program in _SHELLS or program in _OTHERS or program in {"lua", "php", "tclsh"}:
            return False
    if program in {"awk", "gawk", "mawk", "nawk", "sed", "jq"}:
        return _script_files_read(program, args, cwd, depth)
    if program in _NON_READERS:
        return False
    operands = _positionals(program, args)
    if program in _GREP_NAMES or program in _SEARCH_NAMES:
        if program in _SEARCH_NAMES and _lists_files(args) and not _search_unsafe(program, args):
            return False
        if program in _SEARCH_NAMES or _has_recursive(args):
            return _recursive_stage_reads(program, args, operands, cwd)
    for token in operands:
        if _operand_secret(token, cwd, wide=True):
            return True
    scope = root or cwd
    if _unresolved_words(operands) and _tree_has_secret(scope, recursive=True):
        return True
    return False


def _lists_files(args: list[str]) -> bool:
    """``rg --files`` before ``--``. That mode prints paths, not file bodies."""
    for arg in args:
        if arg == "--":
            return False
        if arg == "--files":
            return True
    return False


def _has_recursive(args: list[str]) -> bool:
    for arg in args:
        if arg in {"-r", "-R", "--recursive", "--dereference-recursive"}:
            return True
        if arg.startswith("--") and "recursive" in arg.lower():
            return True
        if arg.startswith("-") and not arg.startswith("--") and "r" in arg[1:].lower() and arg[1:].isalpha():
            return True
    return False


def _positionals(program: str, args: list[str]) -> list[str]:
    """File operands. A flag does not hide the next word, except a grep pattern.

    `-e` / `--regexp` introduce a pattern, not a path. `-f` / `--file` introduce
    a pattern file, which is a read. Every other non-flag token is an operand.
    For grep and rg the first operand is the pattern when no `-e` or `-f` was given,
    including an empty `""`.
    """
    pattern_flags = {"-e", "--regexp"}
    file_flags = {"-f", "--file"}
    search = program in _GREP_NAMES or program in _SEARCH_NAMES
    found: list[str] = []
    forced: list[str] = []
    pattern_given = False
    index = 0
    while index < len(args):
        arg = args[index]
        flag = arg.split("=", 1)[0]
        if search and (flag in pattern_flags or (arg.startswith("-e") and len(arg) > 2)):
            pattern_given = True
            if flag in pattern_flags and "=" not in arg and not (arg.startswith("-e") and len(arg) > 2):
                index += 2
            else:
                index += 1
            continue
        if search and (flag in file_flags or arg.startswith("--file=")):
            pattern_given = True
            if arg.startswith("--file="):
                forced.append(arg.split("=", 1)[1])
                index += 1
                continue
            if index + 1 < len(args):
                forced.append(args[index + 1])
            index += 2
            continue
        if search and flag in {"--include", "--glob", "-g", "--exclude", "--exclude-dir", "--type", "-t"}:
            index += 2
            continue
        if search and any(
            arg.startswith(prefix)
            for prefix in ("--include=", "--glob=", "--exclude=", "--exclude-dir=", "--type=")
        ):
            index += 1
            continue
        if arg.startswith("-") and arg != "-":
            index += 1
            continue
        found.append(arg)
        index += 1
    if search and not pattern_given and found:
        found = found[1:]
    return forced + found


def _script_files_read(program: str, args: list[str], cwd: Path, depth: int = 0) -> bool:
    if program == "sed":
        return _sed_invocation_reads(args, cwd)
    files: list[str] = []
    codes: list[str] = []
    saw_script = program == "jq"
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-e", "--source"}:
            codes.append(args[index + 1] if index + 1 < len(args) else "")
            saw_script = True
            index += 2
            continue
        if arg in {"-f", "--file"}:
            files.append(args[index + 1] if index + 1 < len(args) else "")
            saw_script = True
            index += 2
            continue
        if arg in {"-v", "-F"} or arg.startswith("-v") and len(arg) > 2:
            value = arg.split("=", 1)[1] if "=" in arg else (args[index + 1] if index + 1 < len(args) else "")
            if "=" not in arg and not (arg.startswith("-v") and len(arg) > 2):
                index += 1
            if value and (_operand_secret(value, cwd, wide=True) or _code_body_reads(value, cwd, depth)):
                return True
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if not saw_script:
            codes.append(arg)
            saw_script = True
        else:
            files.append(arg)
        index += 1
    if program == "sed":
        for code in codes:
            if _sed_script_reads(code):
                return True
    for code in codes:
        if _code_body_reads(code, cwd, depth):
            return True
    for item in files:
        if item == "-":
            continue
        if _operand_secret(item, cwd, wide=True):
            return True
        path = _try_resolve(item, cwd)
        if path is not None and path.is_file():
            try:
                body = path.read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError:
                if _tree_has_secret(cwd, recursive=True):
                    return True
                continue
            if _code_body_reads(body, cwd, depth):
                return True
    return bool(files) and _unresolved_words(files) and _tree_has_secret(cwd, recursive=True)


_SED_LONG_OK = frozenset({
    "posix", "quiet", "silent", "regexp-extended", "unbuffered",
    "null-data", "zero-terminated", "sandbox", "follow-symlinks",
    "binary", "help", "version",
})


def sed_plain_read(line: str, cwd: Path) -> bool:
    """True when this is one sed command and the script does not read or write a file.

    ``$p`` and ``s/a/b/;s/c/d/`` are addresses and substitutions. ``r`` and ``w`` are not.
    """
    words, redirects = _tokenize(line or "")
    if redirects:
        return False
    program, args = _split_program(words)
    if program != "sed" or not args:
        return False
    return not _sed_invocation_reads(args, cwd)


def _sed_invocation_reads(args: list[str], cwd: Path) -> bool:
    """A sed run waits unless every script is pure and no input file is secret.

    ``-f``, ``-i``, and ``-s`` always wait. ``-e``, ``--expression``, and
    ``--posix`` still go through the script scan.
    """
    scripts: list[str] = []
    files: list[str] = []
    saw_script = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            files.extend(args[index + 1 :])
            break
        if arg.startswith("--"):
            name, eq, val = arg[2:].partition("=")
            low = name.lower()
            if low == "expression":
                if eq:
                    scripts.append(val)
                elif index + 1 < len(args):
                    index += 1
                    scripts.append(args[index])
                else:
                    return True
                saw_script = True
            elif low in {"file", "in-place", "separate"}:
                return True
            elif low == "line-length":
                if not eq:
                    if index + 1 >= len(args):
                        return True
                    index += 1
            elif low not in _SED_LONG_OK:
                return True
            index += 1
            continue
        if arg.startswith("-") and arg != "-":
            cluster = arg[1:]
            point = 0
            while point < len(cluster):
                ch = cluster[point]
                if ch in {"i", "s", "f"}:
                    return True
                if ch == "e":
                    rest = cluster[point + 1 :]
                    if rest:
                        scripts.append(rest)
                    elif index + 1 < len(args):
                        index += 1
                        scripts.append(args[index])
                    else:
                        return True
                    saw_script = True
                    break
                if ch == "l":
                    if point + 1 >= len(cluster):
                        if index + 1 >= len(args):
                            return True
                        index += 1
                    break
                if ch in "nEruzb":
                    point += 1
                    continue
                return True
            index += 1
            continue
        if not saw_script:
            scripts.append(arg)
            saw_script = True
        else:
            files.append(arg)
        index += 1
    for script in scripts:
        if _sed_script_reads(script):
            return True
    for item in files:
        if item == "-":
            continue
        if _operand_secret(item, cwd, wide=True):
            return True
    return bool(files) and _unresolved_words(files) and _tree_has_secret(cwd, recursive=True)


def _sed_script_reads(script: str) -> bool:
    """True when ``r R w W e E`` is a command, or an ``s///`` flag, not text inside ``s`` or ``y``."""
    text = script or ""
    n = len(text)
    i = 0

    def closed(start: int, delim: str) -> int | None:
        point = start
        while point < n:
            ch = text[point]
            if ch == "\\":
                if point + 1 >= n:
                    return None
                point += 2
                continue
            if ch == "\n" or ch == delim:
                return point + 1 if ch == delim else None
            point += 1
        return None

    def address(start: int) -> int | None:
        if start >= n:
            return start
        ch = text[start]
        if ch.isdigit():
            point = start + 1
            while point < n and text[point].isdigit():
                point += 1
            if point < n and text[point] in {"~", "+"}:
                point += 1
                if point >= n or not text[point].isdigit():
                    return None
                while point < n and text[point].isdigit():
                    point += 1
            return point
        if ch == "$":
            return start + 1
        if ch == "/":
            return closed(start + 1, "/")
        if ch == "\\" and start + 1 < n and text[start + 1] not in {"\\", "\n"}:
            return closed(start + 2, text[start + 1])
        return start

    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            return False
        if text[i] == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text[i] in {";", "}"}:
            i += 1
            continue
        nxt = address(i)
        if nxt is None:
            return True
        if nxt != i:
            i = nxt
            if i < n and text[i] == ",":
                i += 1
                while i < n and text[i] in " \t":
                    i += 1
                nxt = address(i)
                if nxt is None or nxt == i:
                    return True
                i = nxt
            if i < n and text[i] == "!":
                i += 1
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            return True
        if text[i] in {";", "\n", "}"}:
            continue
        if text[i] == "{":
            i += 1
            continue
        cmd = text[i]
        if cmd in "rRwWeE":
            return True
        if cmd in {"s", "y"}:
            if i + 1 >= n or text[i + 1] in {"\\", "\n"}:
                return True
            delim = text[i + 1]
            point = closed(i + 2, delim)
            if point is None:
                return True
            point = closed(point, delim)
            if point is None:
                return True
            if cmd == "s":
                while point < n and text[point] not in ";\n}":
                    flag = text[point]
                    if flag in " \t":
                        point += 1
                        continue
                    if flag in "eEwW":
                        return True
                    if flag.isdigit() or flag in "gpiImM":
                        point += 1
                        continue
                    return True
            i = point
            continue
        if cmd == ":":
            i += 1
            while i < n and text[i] not in " \t;\n}":
                i += 1
            continue
        if cmd in {"b", "t", "T"}:
            i += 1
            while i < n and text[i] not in " \t;\n}":
                i += 1
            continue
        if cmd in {"q", "Q", "l"}:
            i += 1
            while i < n and text[i] in " \t":
                i += 1
            while i < n and text[i].isdigit():
                i += 1
            continue
        if cmd in "pPdDnN=gGhHx":
            i += 1
            continue
        return True
    return False


def _interpreter_reads(program: str, args: list[str], cwd: Path, depth: int) -> bool:
    code, files = _interpreter_pieces(program, args)
    if code is not None:
        if program in _PYTHONS and _python_code_reads(code, cwd, depth):
            return True
        if program in _SHELLS and _command_reads(code, cwd, depth + 1):
            return True
        if program not in _PYTHONS and program not in _SHELLS and _foreign_code_reads(code, cwd):
            return True
    for token in files:
        if _operand_secret(token, cwd, wide=True):
            return True
        path = _try_resolve(token, cwd)
        if path is None or not path.is_file():
            continue
        if _name_only(path):
            return True
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if program in _PYTHONS or path.suffix.lower() == ".py":
            if _python_code_reads(body, cwd, depth) or _code_body_reads(body, cwd, depth):
                return True
        elif program in _SHELLS or path.suffix.lower() in {".sh", ".bash"}:
            if _command_reads(body, path.parent, depth + 1):
                return True
        elif _code_body_reads(body, cwd, depth):
            return True
    return False


def _interpreter_pieces(program: str, args: list[str]) -> tuple[str | None, list[str]]:
    if program in _SHELLS:
        return _shell_inline_pieces(args)
    code = None
    files: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        perl_e = program == "perl" and re.fullmatch(r"-[0-9A-Za-z]*e", arg) is not None
        if arg in {"-c", "-e", "-E", "--command", "-lc"} or perl_e:
            code = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        files.append(arg)
        index += 1
    return code, files


def _python_code_reads(code: str, cwd: Path, depth: int) -> bool:
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return False
    unresolved = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        verdict = _py_call_reads(node, cwd, depth)
        if verdict is True:
            return True
        if verdict is None:
            unresolved = True
    return bool(unresolved and _tree_has_secret(cwd, recursive=True))


def _py_call_reads(node: ast.Call, cwd: Path, depth: int) -> bool | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id in {"eval", "exec", "compile"}:
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            return True if _python_code_reads(node.args[0].value, cwd, depth + 1) else False
        return None
    if isinstance(func, ast.Name) and func.id in {"open"}:
        return _literal_secret(node.args[0] if node.args else None, cwd)
    if isinstance(func, ast.Attribute):
        name = func.attr
        owner = _receiver_name(func.value)
        if name in {"read_text", "read_bytes"}:
            return _receiver_secret(func.value, cwd)
        if owner == "os" and name in {"read", "pread", "readv"}:
            return True
        if name in {"open"} and owner in {"io", "codecs", "os", "pathlib", "Path"}:
            return _literal_secret(node.args[0] if node.args else None, cwd)
        if name in {"listdir", "scandir"}:
            target = _const_path(node.args[0], cwd) if node.args else cwd
            if target is None:
                return None
            return True if _tree_has_secret(target, recursive=False) else False
        if name == "walk":
            target = _const_path(node.args[0], cwd) if node.args else cwd
            if target is None:
                return None
            return True if _tree_has_secret(target, recursive=True) else False
        if name in {"system", "popen"} and owner in {"os", "subprocess"}:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                return True if _command_reads(node.args[0].value, cwd, depth + 1) else False
            return None
        if name in {"glob", "iglob"}:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                return True if _operand_secret(str(node.args[0].value), cwd, wide=True) else False
            return None
    return False


def _receiver_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _receiver_secret(node, cwd: Path) -> bool | None:
    if isinstance(node, ast.Call) and node.args:
        return _literal_secret(node.args[0], cwd)
    if isinstance(node, ast.Name):
        return None
    return False


def _literal_secret(node, cwd: Path) -> bool | None:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    return True if _operand_secret(node.value, cwd, wide=True) else False


def _const_path(node, cwd: Path) -> Path | None:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    return _try_resolve(node.value, cwd)


def _foreign_code_reads(code: str, cwd: Path) -> bool:
    body = code or ""
    if _script_text_unproved(body) or _inline_code_reads(body, cwd, 0):
        return True
    reads = bool(re.search(r"(?i)readFileSync|readFile|\bopen\s*\(|IO\.read|File\.read|\.read\(|read_text|Path\(", body))
    for _quote, text in re.findall(r"""(['"])(.*?)\1""", body):
        if text and _operand_secret(text, cwd, wide=True):
            return True
    if reads and re.search(r"[+$`]|%s|\{[A-Za-z_]", body) and _tree_has_secret(cwd, recursive=True):
        return True
    return False


def _operand_secret(token: str, cwd: Path, *, wide: bool) -> bool:
    raw = token or ""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1]
    # A leading space, tab, or newline is part of the name. Path resolution
    # strips it, which would turn ' .env.example' into a template exemption.
    if Path(raw).name != Path(raw).name.strip():
        return True
    if not raw or raw == "-" or raw.startswith("-"):
        return False
    folded = raw.replace("\\", "/")
    # A drive letter or a UNC path is not the Linux /proc tree.
    windows_spelling = bool(re.match(r"^[A-Za-z]:", folded)) or folded.startswith("//")
    if re.match(r"^/dev/fd/\d+$", folded):
        path = _try_resolve(raw, cwd)
        if path is not None and path.is_file():
            return _path_secret(path)
        return _tree_has_secret(cwd, recursive=True)
    if not windows_spelling and (folded.startswith("/proc/") or "/proc/" in folded):
        if _PROC_CWD.match(folded):
            rewritten = _rewrite_proc(raw, cwd)
            if rewritten != raw:
                return _operand_secret(rewritten, cwd, wide=wide)
        return _tree_has_secret(cwd, recursive=True)
    if raw.startswith("$(") or raw.startswith("${") or raw.startswith("`") or (raw.startswith("$") and len(raw) > 1):
        return False
    if _glob(raw):
        for match in _expand_glob(raw, cwd):
            if _path_secret(Path(match)):
                return True
        if wide and raw in _WIDE_GLOBS:
            base = cwd if not os.path.isabs(raw) else Path(raw).parent
            return _tree_has_secret(base, recursive=("**" in raw))
        return False
    path = _try_resolve(raw, cwd)
    if path is None:
        return False
    return _path_secret(path)


def _expand_glob(token: str, cwd: Path) -> list[str]:
    import glob as globmod

    pattern = token if os.path.isabs(token) else str(Path(cwd) / token)
    found = globmod.glob(pattern, recursive=True)
    if token in {"*", "./*"}:
        found.extend(globmod.glob(str(Path(cwd) / ".*")))
    return found


def _unresolved_words(tokens: list[str]) -> bool:
    for token in tokens:
        if any(mark in (token or "") for mark in ("$(", "${", "`", "$")):
            return True
    return False


def _try_resolve(raw: str, cwd: Path) -> Path | None:
    from easyagent.safety import _resolve

    try:
        return _resolve(raw, cwd)
    except Exception:
        return None


def _path_secret(path: Path) -> bool:
    try:
        return _file_secret(path)
    except Exception:
        return False


def _name_only(path: Path) -> bool:
    from easyagent.safety import _name_secret

    try:
        return _name_secret(path) is not None
    except Exception:
        return False


def _process_sub_reads(statement: str, cwd: Path, depth: int) -> bool:
    for match in re.finditer(r"<\((.*)\)", statement or ""):
        inner = match.group(1).strip()
        if inner and _command_reads(inner, cwd, depth + 1):
            return True
    return False


def _for_loop_reads(text: str, cwd: Path) -> bool:
    match = re.search(r"(?is)\bfor\s+\w+\s+in\s+([^;]+)", text or "")
    if not match:
        return False
    tokens = [part for part in re.findall(r"'[^']*'|\"[^\"]*\"|\S+", match.group(1)) if part]
    if any(_operand_secret(token, cwd, wide=True) for token in tokens):
        return True
    wide = any(token.strip("'\"") in _WIDE_GLOBS or "$" in token for token in tokens)
    return bool(wide and _tree_has_secret(cwd, recursive=False))


def _find_exec(statement: str) -> bool:
    return bool(re.search(r"(?i)(?:^|\s)-(?:exec|execdir|ok|okdir)\b", statement or ""))


def _find_root(args: list[str], cwd: Path) -> Path:
    for arg in args:
        if not arg.startswith("-"):
            path = _try_resolve(arg, cwd)
            if path is not None:
                return path
    return cwd


def _simulate_find(args: list[str], cwd: Path) -> list[Path] | None:
    import fnmatch

    roots: list[str] = []
    patterns: list[str] = []
    inames: list[str] = []
    type_char = ""
    unresolved = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-name", "-iname", "-path", "-ipath"} and index + 1 < len(args):
            (inames if "iname" in arg or arg.startswith("-ipath") else patterns).append(args[index + 1])
            index += 2
            continue
        if arg == "-type" and index + 1 < len(args):
            type_char = args[index + 1]
            index += 2
            continue
        if arg in {"-print", "-print0", "-ls"}:
            index += 1
            continue
        if arg in {"-exec", "-execdir", "-ok", "-okdir"}:
            break
        if arg.startswith("-"):
            unresolved = True
            index += 1
            continue
        if arg in {";", "+", "{}", "\\;"}:
            index += 1
            continue
        roots.append(arg)
        index += 1
    if unresolved and not patterns and not inames:
        return None
    root_paths = [path for path in (_try_resolve(item, cwd) for item in roots) if path is not None] or [cwd]
    found: list[Path] = []
    seen = 0
    for root in root_paths:
        if root.is_file():
            if _find_name_ok(root.name, patterns, inames):
                found.append(root)
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
            names = filenames if type_char in {"", "f"} else list(filenames) + list(dirnames)
            if type_char == "d":
                names = list(dirnames)
            for name in names:
                seen += 1
                if seen > _SCAN_CAP:
                    return None
                if not _find_name_ok(name, patterns, inames):
                    continue
                found.append(Path(dirpath) / name)
    return found


def _find_name_ok(name: str, patterns: list[str], inames: list[str]) -> bool:
    import fnmatch

    if patterns and not any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
        return False
    if inames and not any(fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in inames):
        return False
    return True


def _xargs_reads(args: list[str], produced: list[Path] | None, cwd: Path, depth: int = 0) -> bool:
    list_file = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-a" and index + 1 < len(args):
            list_file = args[index + 1]
            index += 2
            continue
        if arg in {"-I", "-i", "-n", "-L", "-P", "-s", "-J"} and index + 1 < len(args):
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        break
    rest = args[index:]
    if rest and _simple_reads(_program_name(rest[0]), rest[1:], cwd, depth):
        return True
    if list_file:
        path = _try_resolve(list_file, cwd)
        if path is not None and _path_secret(path):
            return True
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path is not None else []
        except OSError:
            return _tree_has_secret(cwd, recursive=True)
        return any(_operand_secret(line.strip(), cwd, wide=False) for line in lines if line.strip())
    if produced is None:
        return _tree_has_secret(cwd, recursive=True)
    return any(_path_secret(path) for path in produced)


def _ls_names(args: list[str], cwd: Path) -> list[Path] | None:
    """Names ``ls`` would print. None when a producer cannot be enumerated.

    Globs are expanded, including ``.e*`` onto ``.env``. ``-a`` / ``-A`` keep
    dotfiles. A resolved-but-missing glob is not an empty known list: an empty
    expansion is a real empty list, and an unresolved operand is unknown.
    """
    show_hidden = False
    positionals: list[str] = []
    for arg in args:
        if arg in {"-a", "-A", "--all", "--almost-all"}:
            show_hidden = True
            continue
        if arg.startswith("--"):
            continue
        if arg.startswith("-") and arg != "-":
            if "a" in arg[1:] or "A" in arg[1:]:
                show_hidden = True
            continue
        positionals.append(arg)
    if not positionals:
        positionals = ["."]
    found: list[Path] = []
    for item in positionals:
        if _token_unresolved(item):
            return None
        if _glob(item):
            found.extend(Path(match) for match in _expand_glob(item, cwd))
            continue
        path = _try_resolve(item, cwd)
        if path is None:
            return None
        if path.is_file():
            found.append(path)
            continue
        if not path.is_dir():
            continue
        try:
            for entry in path.iterdir():
                if entry.name in {".", ".."}:
                    continue
                if entry.name.startswith(".") and not show_hidden:
                    continue
                found.append(entry)
        except OSError:
            return None
    return found


_LOOP_BODY = frozenset({
    "echo", "printf", "wc", "cat", "ls", "head", "tail", "nl", "true", ":", "pwd",
})


def _loop_item_safe(token: str, cwd: Path) -> bool:
    raw = (token or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1]
    if not raw or _token_unresolved(raw):
        return False
    return not _operand_secret(raw, cwd, wide=True)


def _var_use(token: str, variables: set[str]) -> bool:
    if token.startswith("${") and token.endswith("}") and token[2:-1] in variables:
        return True
    if token.startswith("$"):
        name = token[1:].split("/", 1)[0]
        return bool(name) and name in variables
    return False


def _loop_body_safe(body: str, variables: set[str], cwd: Path) -> bool:
    parts = [part.strip() for part in (body or "").split(";") if part.strip()]
    if not parts:
        return False
    for part in parts:
        words = [item for item in re.findall(r"'[^']*'|\"[^\"]*\"|\S+", part) if item]
        if not words:
            return False
        program = _program_name(words[0].strip("'\""))
        if program not in _LOOP_BODY:
            return False
        for word in words[1:]:
            token = word.strip("'\"")
            if token.startswith("-"):
                continue
            if _var_use(token, variables):
                continue
            if not _loop_item_safe(word, cwd):
                return False
    return True


def plain_loop(text: str, cwd: Path) -> bool:
    """A for/while whose operands are literals or a non-secret glob."""
    raw = (text or "").strip()
    found = re.fullmatch(
        r"for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.+?);\s*do\s+(.+);\s*done",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if found:
        var, items, body = found.group(1), found.group(2), found.group(3)
        tokens = [part for part in re.findall(r"'[^']*'|\"[^\"]*\"|\S+", items) if part]
        if not tokens or any(not _loop_item_safe(token, cwd) for token in tokens):
            return False
        return _loop_body_safe(body, {var}, cwd)
    reading = re.fullmatch(
        r"while\s+read\s+(?:-r\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*;\s*do\s+(.+);\s*done(?:\s*<\s*(\S+))?",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if not reading:
        return False
    var, body, source = reading.group(1), reading.group(2), reading.group(3)
    if source:
        if not _loop_item_safe(source, cwd):
            return False
        path = _try_resolve(source.strip("'\""), cwd)
        if path is None or not path.is_file() or _path_secret(path):
            return False
    return _loop_body_safe(body, {var}, cwd)
