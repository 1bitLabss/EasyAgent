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
from pathlib import Path

_SKIP_DIRS = frozenset({".git", "node_modules", "venv", ".venv", "__pycache__"})
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash"})
_PYTHONS = frozenset({"python", "python2", "python3", "py"})
_OTHERS = frozenset({"perl", "ruby", "node", "nodejs", "deno", "php"})
_READERS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "cat", "head", "tail", "strings", "awk", "sed", "find",
})
_INERT = re.compile(
    r"(?i)^(?:echo|printf|true|false|:|pwd|whoami|date|uname|hostname|id|"
    r"which|where|exit|return|export|readonly|local|unset|umask|hash|type|"
    r"shift|break|continue|test|\[|set)\b"
)
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


def command_reads_secrets(command: str, cwd: Path) -> bool:
    """True when a recursive or glob read would open a secret-classified file."""
    from easyagent.safety import _split_chain

    try:
        statements = _split_chain(command or "")
    except Exception:
        return True
    for statement in statements:
        try:
            if _statement_reads_secrets(statement, Path(cwd)):
                return True
        except Exception:
            return True
    return False


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


def _runner_args(program: str, args: list[str]) -> tuple[list[str], bool]:
    """(script files, inline code present)."""
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
        return False
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
        if node.module in {"os", "sys", "pathlib", "shutil", "subprocess", "socket", "ctypes"}:
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
    files: list[str] = []
    codes: list[str] = []
    args = words[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        low = arg.lower()
        if low in {"-c", "--command", "-lc"}:
            if index + 1 < len(args):
                codes.append(args[index + 1])
            else:
                codes.append("")
            return files, codes
        if arg.startswith("-"):
            index += 1
            continue
        files.append(arg)
        break
    return files, codes


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
    name = Path(token).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
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
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        here = Path(dirpath)
        for name in list(dirnames) + list(filenames):
            seen += 1
            if seen > _SCAN_CAP:
                return True
            candidate = here / name
            if secret_file(candidate):
                return True
            if candidate.is_symlink():
                try:
                    if secret_file(candidate.resolve()):
                        return True
                except OSError:
                    return True
    return False
