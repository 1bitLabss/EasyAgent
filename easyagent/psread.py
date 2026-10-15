"""A read-only PowerShell pipeline, proved by a parser.

The check walks statements, pipelines, expressions, and script blocks. A
regular expression is not the decision. Anything the grammar does not accept
is not a read: writes, aliases, nested commands, redirection, here-strings,
backtick-obfuscated names, and encoded commands all fail closed.

Path tokens are expanded and resolved afterwards. A wildcard, ``..``, an
environment variable, a UNC path, or an alternate data stream that cannot be
shown to stay put is not a read this module will vouch for.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path


class _Reject(Exception):
    pass


_ALIASES = {
    "gc": "get-content",
    "cat": "get-content",
    "type": "get-content",
    "gci": "get-childitem",
    "dir": "get-childitem",
    "ls": "get-childitem",
    "gi": "get-item",
    "sls": "select-string",
    "measure": "measure-object",
    "select": "select-object",
    "where": "where-object",
    "?": "where-object",
    "sort": "sort-object",
    "oss": "out-string",
}

_METHODS = {"substring", "split", "trim", "trimstart", "trimend"}

_CMP = {
    "eq", "ne", "gt", "ge", "lt", "le", "like", "notlike", "match", "notmatch",
    "contains", "notcontains", "in", "notin", "ceq", "cne", "ieq", "ine",
    "clike", "cnotlike", "ilike", "inotlike", "cgt", "igt", "clt", "ilt",
    "and", "or", "xor", "band", "bor", "not",
}

_COMMON_SWITCH = {"-verbose", "-debug", "-whatif", "-confirm"}
_COMMON_VALUE = {
    "-erroraction", "-warningaction", "-informationaction", "-errorvariable",
    "-warningvariable", "-outvariable", "-outbuffer", "-pipelinevariable",
    "-informationvariable",
}

# positional: path, properties, pattern (first is text, the rest are paths), none
_SPECS = {
    "get-content": {
        "paths": {"-path", "-literalpath"},
        "switches": {"-raw", "-force", "-wait", "-asbytestream"},
        "values": {
            "-totalcount": "text", "-tail": "text", "-readcount": "text",
            "-encoding": "text", "-delimiter": "text", "-stream": "stream",
        },
        "positional": "path",
        "default_cwd": False,
    },
    "get-childitem": {
        "paths": {"-path", "-literalpath"},
        "switches": {"-recurse", "-force", "-name", "-file", "-directory", "-hidden", "-readonly", "-system"},
        "values": {"-filter": "filter", "-include": "filter", "-exclude": "filter", "-depth": "text", "-attributes": "text"},
        "positional": "path",
        "default_cwd": True,
    },
    "get-item": {
        "paths": {"-path", "-literalpath"},
        "switches": {"-force"},
        "values": {"-stream": "stream"},
        "positional": "path",
        "default_cwd": True,
    },
    "test-path": {
        "paths": {"-path", "-literalpath"},
        "switches": {"-isvalid", "-leaf", "-container"},
        "values": {"-pathtype": "text"},
        "positional": "path",
        "default_cwd": True,
    },
    "select-string": {
        "paths": {"-path", "-literalpath"},
        "switches": {"-simplematch", "-casesensitive", "-notmatch", "-quiet", "-list", "-allmatches"},
        "values": {
            "-pattern": "text", "-context": "text", "-encoding": "text",
            "-include": "filter", "-exclude": "filter",
        },
        "positional": "pattern",
        "default_cwd": False,
    },
    "measure-object": {
        "paths": set(),
        "switches": {"-line", "-word", "-character", "-ignorewhitespace", "-sum", "-average", "-maximum", "-minimum"},
        "values": {"-property": "property"},
        "positional": "properties",
        "default_cwd": False,
    },
    "select-object": {
        "paths": set(),
        "switches": {"-unique"},
        "values": {
            "-property": "property", "-expandproperty": "property",
            "-first": "text", "-last": "text", "-skip": "text", "-skiplast": "text",
            "-index": "text",
        },
        "positional": "properties",
        "default_cwd": False,
    },
    "sort-object": {
        "paths": set(),
        "switches": {"-descending", "-unique"},
        "values": {"-property": "property"},
        "positional": "properties",
        "default_cwd": False,
    },
    "out-string": {
        "paths": set(),
        "switches": {"-stream", "-nonewline"},
        "values": {"-width": "text"},
        "positional": "none",
        "default_cwd": False,
    },
    "where-object": {
        "paths": set(),
        "switches": {"-not"},
        "values": {"-property": "property", "-value": "text", "-filterscript": "script"},
        "positional": "where",
        "default_cwd": False,
    },
}


def prove_readonly(command: str, cwd: Path, env: dict | None = None) -> list[Path] | None:
    """Resolved paths when `command` is only a read. None when it is not proved.

    An empty list is a read that names no file, such as a string method on a
    value this same script just produced.
    """
    text = command or ""
    if not text.strip():
        return None
    try:
        parser = _Parser(text, Path(cwd), dict(env or os.environ))
        parser.script()
        if parser.i < parser.n and parser.text[parser.i:].strip():
            return None
        return _unique(parser.found)
    except (_Reject, ValueError, OSError):
        return None


def _unique(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return found


class _Parser:
    def __init__(self, text: str, cwd: Path, env: dict) -> None:
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.n = len(self.text)
        self.i = 0
        self.cwd = cwd
        self.env = env
        self.bound: dict[str, str] = {}
        self.computed: set[str] = set()
        self.found: list[Path] = []
        self.depth = 0

    def script(self) -> None:
        self._noise(newlines=True)
        if self._eof():
            raise _Reject("empty")
        while not self._eof():
            self._statement()
            self._space_comments()
            if self._eof():
                break
            if not self._separator():
                raise _Reject("separator")
            self._noise(newlines=True)

    def _statement(self) -> None:
        self._space_comments()
        if self._eof() or self._boundary():
            return
        if self._at(">") or self._at("<"):
            raise _Reject("redirect")
        if self._at("&"):
            raise _Reject("call")
        if self._at("$"):
            mark = self.i
            kind, name = self._variable_name()
            self._space_comments()
            if kind == "var" and self._at("=") and self._at_eq_assign():
                self.i += 1
                value = self._rhs()
                self._bind(name, value)
                return
            self.i = mark
            self._expression()
            return
        if self._at("(") or self._at("@"):
            self._expression()
            return
        self._pipeline()

    def _rhs(self) -> tuple[str, str]:
        self._space_comments()
        if self._command_ahead():
            self._pipeline()
            return ("computed", "")
        return self._expression()

    def _pipeline(self) -> None:
        first = True
        while True:
            self._command(in_pipeline=not first)
            first = False
            self._space_comments()
            if self._at("|") and not self._starts("||"):
                self.i += 1
                self._space_comments()
                continue
            break

    def _command(self, *, in_pipeline: bool) -> None:
        self._space_comments()
        if self._at(">") or self._at("<") or self._at("&"):
            raise _Reject("redirect")
        if not self._command_ahead():
            raise _Reject("command")
        raw = self._read_command_name()
        if any(ch in raw for ch in "\\/:`."):
            raise _Reject("command-name")
        name = _ALIASES.get(raw.lower(), raw.lower())
        spec = _SPECS.get(name)
        if spec is None:
            raise _Reject("cmdlet")
        got_path = self._arguments(spec)
        if spec["positional"] == "path" and not got_path:
            if spec["default_cwd"]:
                self._take_path(".", recurse="-recurse" in spec["switches"] and self._saw_recurse)
            elif not in_pipeline:
                raise _Reject("missing-path")

    def _arguments(self, spec: dict) -> bool:
        got_path = False
        self._saw_recurse = False
        pattern_used = False
        while True:
            self._space_comments()
            if self._arg_stop():
                break
            if self._at(","):
                self.i += 1
                continue
            if self._at("{") and spec["positional"] in {"where", "properties"}:
                self._script_block()
                continue
            if self._param_ahead():
                param = self._read_param()
                low = param.lower()
                if low in _COMMON_SWITCH or low in spec["switches"]:
                    if low == "-recurse":
                        self._saw_recurse = True
                    continue
                if low in _COMMON_VALUE:
                    self._value()
                    continue
                if low in spec["paths"]:
                    self._take_value_path()
                    got_path = True
                    continue
                if low in spec["values"]:
                    kind = spec["values"][low]
                    if kind == "script":
                        self._space_comments()
                        self._script_block()
                    elif kind == "stream":
                        self._take_stream()
                    elif kind == "filter":
                        self._take_filter()
                    elif kind == "property":
                        self._take_properties()
                    else:
                        self._value()
                    continue
                if low.lstrip("-") in _CMP and spec["positional"] == "where":
                    self._value()
                    continue
                raise _Reject("parameter")
            if spec["positional"] == "none":
                raise _Reject("positional")
            if spec["positional"] == "path":
                self._take_value_path()
                got_path = True
                continue
            if spec["positional"] == "pattern":
                if not pattern_used:
                    self._value()
                    pattern_used = True
                else:
                    self._take_value_path()
                    got_path = True
                continue
            if spec["positional"] == "where":
                self._take_properties()
                continue
            self._take_properties()
        if self._saw_recurse and got_path:
            self._walk_links()
        return got_path

    def _expression(self) -> tuple[str, str]:
        self._space_comments()
        left = self._term()
        while True:
            self._space_comments()
            if self._at("+"):
                self.i += 1
                right = self._term()
                left = _combine(left, right, "+")
                continue
            if self._at("-") and not self._param_ahead():
                self.i += 1
                right = self._term()
                left = ("computed", "")
                continue
            break
        return left

    def _term(self) -> tuple[str, str]:
        left = self._unary()
        while True:
            self._space_comments()
            if self.i < self.n and self.text[self.i] in "*/%":
                self.i += 1
                self._unary()
                left = ("computed", "")
                continue
            break
        return left

    def _unary(self) -> tuple[str, str]:
        self._space_comments()
        if self._at("-") and not self._param_ahead():
            self.i += 1
            self._unary()
            return ("computed", "")
        if self._at("+"):
            self.i += 1
            return self._unary()
        value = self._primary()
        return self._members(value)

    def _primary(self) -> tuple[str, str]:
        self._space_comments()
        if self._at("'"):
            return ("static", self._single())
        if self._at('"'):
            return self._double()
        if self._at("$"):
            return self._variable_value()
        if self._starts("@'"):
            raise _Reject("here-string")
        if self._starts('@"'):
            raise _Reject("here-string")
        if self._starts("@("):
            self.i += 2
            self._nested_pipeline()
            self._expect(")")
            return ("computed", "")
        if self._at("@"):
            raise _Reject("splat")
        if self._at("("):
            self.i += 1
            self._noise(newlines=True)
            if self._command_ahead():
                self._pipeline()
                value: tuple[str, str] = ("computed", "")
            else:
                value = self._expression()
            self._noise(newlines=True)
            self._expect(")")
            return value
        if self._number_ahead():
            return ("static", self._number())
        raise _Reject("primary")

    def _members(self, value: tuple[str, str]) -> tuple[str, str]:
        while True:
            self._space_comments()
            if self._at("["):
                self.i += 1
                self._expression()
                self._noise(newlines=True)
                self._expect("]")
                value = ("computed", "")
                continue
            if not self._at("."):
                break
            self.i += 1
            self._space_comments()
            if not self._ident_ahead():
                raise _Reject("member")
            name = self._read_ident()
            self._space_comments()
            if self._at("("):
                if name.lower() not in _METHODS:
                    raise _Reject("method")
                self.i += 1
                self._noise(newlines=True)
                if not self._at(")"):
                    self._expression()
                    while True:
                        self._noise(newlines=True)
                        if not self._at(","):
                            break
                        self.i += 1
                        self._expression()
                self._noise(newlines=True)
                self._expect(")")
            elif not name.replace("_", "").isalnum():
                raise _Reject("property")
            value = ("computed", "")
        return value

    def _nested_pipeline(self) -> None:
        self._noise(newlines=True)
        if self._command_ahead():
            self._pipeline()
            return
        self._expression()

    def _script_block(self) -> None:
        self._space_comments()
        self._expect("{")
        self._noise(newlines=True)
        if not self._at("}"):
            self._block_or()
            self._noise(newlines=True)
        self._expect("}")

    def _block_or(self) -> None:
        self._block_and()
        while self._cmp_ahead() == "or":
            self._eat_param()
            self._block_and()

    def _block_and(self) -> None:
        self._block_not()
        while self._cmp_ahead() == "and":
            self._eat_param()
            self._block_not()

    def _block_not(self) -> None:
        if self._cmp_ahead() == "not":
            self._eat_param()
            self._block_not()
            return
        self._block_cmp()

    def _block_cmp(self) -> None:
        self._block_value()
        while self._cmp_ahead() not in {"", "and", "or", "not"}:
            self._eat_param()
            self._block_value()

    def _block_value(self) -> None:
        self._noise(newlines=True)
        if self._at(")" ) or self._at("}"):
            raise _Reject("block")
        self._members(self._primary())

    def _value(self) -> tuple[str, str]:
        """One argument. A bare word is text. An expression is parsed."""
        self._space_comments()
        if self._at("'") or self._at('"') or self._at("$") or self._at("(") or self._at("@") or self._number_ahead():
            return self._expression()
        if self._at("{") or self._param_ahead() or self._arg_stop():
            raise _Reject("value")
        word = self._read_bare()
        return ("static", word)

    def _take_value_path(self) -> None:
        kind, text = self._value()
        if kind != "static":
            raise _Reject("dynamic-path")
        self._take_path(text, recurse=self._saw_recurse)

    def _take_properties(self) -> None:
        kind, text = self._value()
        if kind != "static":
            raise _Reject("property")
        if _pathish(text):
            raise _Reject("property-path")

    def _take_filter(self) -> None:
        kind, text = self._value()
        if kind != "static" or _pathish(text) or "$" in text or "~" in text:
            raise _Reject("filter")

    def _take_stream(self) -> None:
        kind, text = self._value()
        if kind != "static" or not _stream_name(text):
            raise _Reject("stream")

    def _take_path(self, text: str, *, recurse: bool) -> None:
        for path in _resolve_token(text, self.cwd, self.env):
            self.found.append(path)
            if recurse:
                self._walk_one(path)

    def _walk_links(self) -> None:
        roots = [path for path in self.found if _under(path, self.cwd)]
        for path in roots:
            self._walk_one(path)

    def _walk_one(self, root: Path) -> None:
        if not _under(root, self.cwd):
            return
        if not root.exists() or not root.is_dir():
            return
        seen = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in list(dirnames) + list(filenames):
                seen += 1
                if seen > 4000:
                    raise _Reject("walk")
                path = Path(dirpath) / name
                if not path.is_symlink():
                    continue
                try:
                    self.found.append(path.resolve())
                except OSError:
                    raise _Reject("link") from None

    def _bind(self, name: str, value: tuple[str, str]) -> None:
        key = name.lower()
        if value[0] == "static":
            self.computed.discard(key)
            self.bound[key] = value[1]
            return
        self.bound.pop(key, None)
        self.computed.add(key)

    def _variable_value(self) -> tuple[str, str]:
        kind, name = self._variable_name()
        suffix = ""
        if self.i < self.n and self.text[self.i] in "\\/":
            suffix = self._read_bare()
        if kind == "env":
            if name not in self.env or self.env.get(name, "") == "":
                raise _Reject("env")
            return ("static", self.env[name] + suffix)
        key = name.lower()
        if key in self.bound and key not in self.computed:
            return ("static", self.bound[key] + suffix)
        return ("computed", "")

    def _variable_name(self) -> tuple[str, str]:
        self._expect("$")
        if self._at("{"):
            self.i += 1
            body = self._until("}")
            self._expect("}")
            low = body.lower()
            if low.startswith("env:"):
                return ("env", body.split(":", 1)[1])
            if ":" in body:
                raise _Reject("scope")
            return ("var", body)
        if self._starts_fold("env:"):
            self.i += 4
            return ("env", self._read_ident())
        if not self._ident_ahead() and not self._at("?"):
            raise _Reject("variable")
        if self._at("?"):
            self.i += 1
            return ("var", "?")
        name = self._read_ident()
        if self._at(":"):
            raise _Reject("scope")
        return ("var", name)

    def _single(self) -> str:
        self._expect("'")
        chars: list[str] = []
        while self.i < self.n:
            if self._starts("''"):
                chars.append("'")
                self.i += 2
                continue
            if self._at("'"):
                self.i += 1
                return "".join(chars)
            chars.append(self.text[self.i])
            self.i += 1
        raise _Reject("string")

    def _double(self) -> tuple[str, str]:
        self._expect('"')
        chars: list[str] = []
        static = True
        while self.i < self.n:
            ch = self.text[self.i]
            if ch == "`":
                if self.i + 1 >= self.n:
                    raise _Reject("backtick")
                mapped = _BACKTICK.get(self.text[self.i + 1], self.text[self.i + 1])
                chars.append(mapped)
                self.i += 2
                continue
            if ch == '"':
                self.i += 1
                if static:
                    return ("static", "".join(chars))
                return ("computed", "")
            if ch == "$":
                if self._starts("$("):
                    self.i += 2
                    self._nested_pipeline()
                    self._expect(")")
                    static = False
                    continue
                kind, text = self._variable_value()
                if kind != "static":
                    static = False
                else:
                    chars.append(text)
                continue
            chars.append(ch)
            self.i += 1
        raise _Reject("string")

    def _separator(self) -> bool:
        if self._starts("&&") or self._starts("||"):
            self.i += 2
            return True
        if self._at(";") or self._at("\n"):
            self.i += 1
            return True
        if self._at("&"):
            nxt = self.i + 1
            while nxt < self.n and self.text[nxt] in " \t":
                nxt += 1
            if nxt < self.n and self.text[nxt] in "{('\"" + "$" + "@":
                raise _Reject("call")
            self.i += 1
            return True
        return False

    def _arg_stop(self) -> bool:
        if self._eof():
            return True
        if self._at("\n") and self.depth == 0:
            return True
        if self._at("|") or self._at(";") or self._at("&") or self._at(">") or self._at("<"):
            return True
        if self._at(")") or self._at("}"):
            return True
        return False

    def _boundary(self) -> bool:
        return self._at(";") or self._at("\n") or self._at(")") or self._at("}") or self._starts("&&") or self._starts("||")

    def _command_ahead(self) -> bool:
        if self._eof() or not (self.text[self.i].isalpha() or self.text[self.i] in "_?"):
            return False
        return True

    def _param_ahead(self) -> bool:
        return (
            self._at("-")
            and self.i + 1 < self.n
            and self.text[self.i + 1].isalpha()
        )

    def _cmp_ahead(self) -> str:
        if not self._param_ahead():
            return ""
        mark = self.i
        name = self._read_param().lstrip("-").lower()
        self.i = mark
        if name in _CMP:
            return name
        return ""

    def _number_ahead(self) -> bool:
        if self.i >= self.n or not self.text[self.i].isdigit():
            return False
        return True

    def _ident_ahead(self) -> bool:
        return self.i < self.n and (self.text[self.i].isalnum() or self.text[self.i] == "_")

    def _read_ident(self) -> str:
        if not self._ident_ahead() and not self._at("?"):
            raise _Reject("ident")
        start = self.i
        if self._at("?"):
            self.i += 1
            return "?"
        while self.i < self.n and (self.text[self.i].isalnum() or self.text[self.i] == "_"):
            if self.text[self.i] == "`":
                raise _Reject("backtick")
            self.i += 1
        return self.text[start:self.i]

    def _read_command_name(self) -> str:
        """Cmdlet names may contain a hyphen. Member names may not."""
        if not self._ident_ahead() and not self._at("?"):
            raise _Reject("ident")
        start = self.i
        if self._at("?"):
            self.i += 1
            return "?"
        while self.i < self.n and (self.text[self.i].isalnum() or self.text[self.i] in "_-"):
            if self.text[self.i] == "`":
                raise _Reject("backtick")
            self.i += 1
        return self.text[start:self.i]

    def _read_param(self) -> str:
        self._expect("-")
        ident = self._read_ident()
        if self._at(":"):
            self.i += 1
        return "-" + ident

    def _eat_param(self) -> None:
        self._space_comments()
        self._read_param()

    def _read_bare(self) -> str:
        if self._eof() or self.text[self.i] in " \t\n;|&><)}],='\"$@({":
            raise _Reject("bare")
        start = self.i
        while self.i < self.n and self.text[self.i] not in " \t\n;|&><)}],='\"$@({":
            if self.text[self.i] == "`":
                raise _Reject("backtick")
            self.i += 1
        if self.i == start:
            raise _Reject("bare")
        return self.text[start:self.i]

    def _number(self) -> str:
        start = self.i
        saw_dot = False
        while self.i < self.n and (self.text[self.i].isdigit() or (self.text[self.i] == "." and not saw_dot)):
            if self.text[self.i] == ".":
                saw_dot = True
            self.i += 1
        if self.i == start:
            raise _Reject("number")
        return self.text[start:self.i]

    def _until(self, stop: str) -> str:
        start = self.i
        while self.i < self.n and self.text[self.i] != stop:
            if self.text[self.i] in "\n`":
                raise _Reject("brace")
            self.i += 1
        if self.i >= self.n:
            raise _Reject("brace")
        return self.text[start:self.i]

    def _noise(self, *, newlines: bool) -> None:
        while True:
            progressed = self.i
            self._space_comments()
            if newlines:
                while self._at("\n"):
                    self.i += 1
                self._space_comments()
            if self.i == progressed:
                break

    def _space_comments(self) -> None:
        while self.i < self.n:
            if self.text[self.i] in " \t":
                self.i += 1
                continue
            if self._at("#"):
                while self.i < self.n and self.text[self.i] != "\n":
                    self.i += 1
                continue
            if self._starts("<#"):
                end = self.text.find("#>", self.i + 2)
                if end < 0:
                    raise _Reject("comment")
                self.i = end + 2
                continue
            break

    def _at_eq_assign(self) -> bool:
        return self._at("=") and not self._starts("==")

    def _starts_fold(self, token: str) -> bool:
        return self.text[self.i:self.i + len(token)].lower() == token.lower()

    def _starts(self, token: str) -> bool:
        return self.text.startswith(token, self.i)

    def _at(self, ch: str) -> bool:
        return self.i < self.n and self.text[self.i] == ch

    def _eof(self) -> bool:
        return self.i >= self.n

    def _expect(self, token: str) -> None:
        if not self._starts(token):
            raise _Reject("expect")
        self.i += len(token)


_BACKTICK = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "0": "\0", "'": "'", '"': '"', "`": "`", "$": "$"}


def _combine(left: tuple[str, str], right: tuple[str, str], op: str) -> tuple[str, str]:
    if op == "+" and left[0] == "static" and right[0] == "static":
        return ("static", left[1] + right[1])
    return ("computed", "")


def _unexpanded(text: str) -> bool:
    """A `$` that still starts a variable. A share like `c$` does not."""
    raw = text or ""
    index = 0
    while True:
        found = raw.find("$", index)
        if found < 0:
            return False
        nxt = raw[found + 1:found + 2]
        if nxt.isalpha() or nxt in "{_":
            return True
        index = found + 1


def _pathish(text: str) -> bool:
    raw = text or ""
    if any(ch in raw for ch in "\\/$~"):
        return True
    if ".." in raw:
        return True
    if len(raw) >= 2 and raw[0].isalpha() and raw[1] == ":":
        return True
    return False


def _stream_name(text: str) -> bool:
    if not text or ".." in text:
        return False
    return all(ch.isalnum() or ch in "_.$" for ch in text)


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _resolve_token(text: str, cwd: Path, env: dict) -> list[Path]:
    expanded = _expand_env(text, env)
    if _unexpanded(expanded) or "`" in expanded:
        raise _Reject("dynamic-path")
    stripped = _strip_extended(expanded.strip().strip('"').strip("'"))
    norm = stripped.replace("\\", "/")
    file, stream = _split_ads(norm)
    targets = [file]
    if stream is not None and os.name != "nt" and ":" in norm:
        targets.append(norm)
    found: list[Path] = []
    for target in targets:
        found.extend(_expand_one(target, cwd))
    if not found:
        raise _Reject("path")
    return found


def _expand_env(text: str, env: dict) -> str:
    out: list[str] = []
    i = 0
    raw = text or ""
    while i < len(raw):
        if raw.startswith("${env:", i) or raw.lower().startswith("${env:", i):
            end = raw.find("}", i + 6)
            if end < 0:
                raise _Reject("env")
            name = raw[i + 6:end]
            if name not in env or env.get(name, "") == "":
                raise _Reject("env")
            out.append(env[name])
            i = end + 1
            continue
        if raw.lower().startswith("$env:", i):
            j = i + 5
            while j < len(raw) and (raw[j].isalnum() or raw[j] == "_"):
                j += 1
            name = raw[i + 5:j]
            if not name or name not in env or env.get(name, "") == "":
                raise _Reject("env")
            out.append(env[name])
            i = j
            continue
        if raw.startswith("%", i):
            end = raw.find("%", i + 1)
            if end > i + 1 and all(ch.isalnum() or ch == "_" for ch in raw[i + 1:end]):
                name = raw[i + 1:end]
                if name not in env or env.get(name, "") == "":
                    raise _Reject("env")
                out.append(env[name])
                i = end + 1
                continue
        out.append(raw[i])
        i += 1
    return "".join(out)


def _strip_extended(text: str) -> str:
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    if text.startswith("//?/UNC/"):
        return "//" + text[8:]
    if text.startswith("//?/"):
        return text[4:]
    return text


def _drive_abs(text: str) -> bool:
    return len(text) >= 3 and text[0].isalpha() and text[1] == ":" and text[2] in "\\/"


def _split_ads(norm: str) -> tuple[str, str | None]:
    if _drive_abs(norm):
        rest = norm[2:]
        if ":" not in rest:
            return norm, None
        file, stream = norm[:2] + rest.split(":", 1)[0], rest.split(":", 1)[1]
    elif ":" in norm:
        file, stream = norm.split(":", 1)
    else:
        return norm, None
    if not _stream_name(stream):
        raise _Reject("ads")
    return file, stream


def _expand_one(target: str, cwd: Path) -> list[Path]:
    if _drive_abs(target) and os.name != "nt":
        return [Path("/__ea_outside__") / "drive"]
    if target.startswith("//") or target.startswith("\\\\"):
        return [Path(target)]
    pattern = target
    if not pattern.startswith("/") and not _drive_abs(pattern):
        pattern = str(Path(cwd) / pattern)
    if any(ch in pattern for ch in "*?["):
        if "**" in pattern:
            raise _Reject("glob")
        matches = glob.glob(pattern)
        if not matches:
            raise _Reject("glob")
        return [Path(match).resolve() for match in matches]
    path = Path(pattern)
    try:
        return [path.resolve()]
    except OSError:
        raise _Reject("path") from None
