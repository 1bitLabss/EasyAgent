"""Strict mode: a command auto-runs only when every program is known-safe.

Balanced mode is the 0.3.13 rules. Strict keeps those blocks, then refuses to
auto-run anything that is not on the safe list or that writes outside the bot
workspace and temp. Home login files, system folders, and drive roots are
blocked. The operating-system sandbox is still what contains a command that runs.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

_READ_ONLY = {
    "ls", "dir", "cat", "type", "gc", "get-content", "head", "tail", "wc",
    "grep", "egrep", "fgrep", "rg", "pwd", "echo", "printf", "which", "where",
    "gci", "get-childitem", "test-path", "whoami", "date", "uname", "true",
    "false", "hostname", "id", "cd", "pushd", "popd", "get-date", "get-location",
    "write-output", "write-host", "less", "more", "stat", "df", "du", "free",
    "uptime", "ps", "top", "file", "basename", "dirname", "realpath", "nl",
    "sort", "uniq", "cut", "tr", "column", "diff", "cmp", "comm", "tac",
    "printenv", "format-table", "format-list", "format-wide", "select-string",
    "measure-object", "get-item", "get-filehash", "resolve-path", "fd",
    "findstr", "pwd", "dirs", "test", "cksum", "md5sum", "sha1sum", "sha256sum",
    "od", "hexdump", "strings", "fold", "paste", "rev", "basename",
}

_GIT_SAFE = {
    "status", "log", "diff", "show", "rev-parse", "branch", "blame", "describe",
    "shortlog", "ls-files", "grep", "commit", "pull", "add", "fetch", "remote",
    "stash", "tag",
}

_NPM_SAFE = {
    "test", "start", "run", "ci", "version", "outdated", "ls", "list", "view",
    "explain", "prefix", "bin", "root", "fund", "audit",
}

_DOCKER_READ = {"ps", "images", "version", "info", "inspect", "logs", "top", "port", "stats", "history"}

_CARGO_SAFE = {"build", "test", "check", "clippy", "fmt", "metadata", "tree", "version", "run"}

_GO_SAFE = {"build", "test", "vet", "fmt", "list", "env", "version", "doc", "mod"}

_WRAPPERS = {
    "cmd", "bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell",
    "eval", "iex", "invoke-expression", "exec", "xargs", "env", "nohup",
    "sudo", "doas", "time", "start", "start-process", "invoke-command", "icm",
    "nice", "stdbuf", "builtin",
}

_INTERPRETERS = {"python", "python3", "py", "node", "ruby", "perl", "php", "lua", "deno"}

_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"}

_MUTATORS = {
    "chmod", "chown", "chgrp", "ln", "link", "mv", "move", "cp", "copy", "xcopy",
    "tee", "srm", "shred", "truncate", "set-content", "out-file", "add-content",
    "new-item", "clear-content",
}

_EDITORS = {"vim", "vi", "nvim", "ex", "emacs", "ed", "nano"}

_AWK = {"awk", "gawk", "mawk", "nawk"}

_DOT_DIRS = {".ssh", ".config", ".gnupg", ".aws", ".kube", ".docker"}

_DOT_FILES = {
    ".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zprofile",
    ".zlogin", ".zshenv", ".cshrc", ".kshrc", ".gitconfig", ".npmrc", ".netrc",
    ".wgetrc", ".curlrc", ".pgpass", ".inputrc", ".vimrc", ".nanorc",
}

_FIND_UNSAFE = re.compile(r"(?i)(?:^|\s)-(?:exec|execdir|ok|okdir|delete|fls|fprint|fprintf)\b")

_ROOT_WIPE = re.compile(r"(?i)\brm\s+-rf\s+/|\bformat\s+[a-z]:")

_SYSTEM_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc", "/system",
    "/library", "/private/etc",
)


def _safety():
    from easyagent import safety

    return safety


def place_class(path: Path, roots: list[Path]) -> str:
    """root, device, system, dotfile, inside, or outside."""
    safety = _safety()
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    text = str(resolved).replace("\\", "/")
    if text.lower().startswith("//?/"):
        text = text[4:]
    folded = text.rstrip("/") or "/"
    low = folded.lower()
    if low in {"/", ""} or re.fullmatch(r"[a-z]:", low):
        return "root"
    if re.match(r"/dev/(?:sd|nvme|vd|xvd|mmcblk|disk)", low):
        return "device"
    for prefix in _SYSTEM_PREFIXES:
        if low == prefix or low.startswith(prefix + "/"):
            return "system"
    if re.match(r"[a-z]:/windows(?:/|$)", low):
        return "system"
    home = Path.home()
    try:
        home_resolved = home.resolve()
    except OSError:
        home_resolved = home
    home_low = str(home_resolved).replace("\\", "/").rstrip("/").lower()
    if low == home_low:
        return "root"
    try:
        rel = resolved.resolve().relative_to(home_resolved)
        parts = rel.parts
    except (OSError, ValueError):
        parts = ()
    if parts:
        head = parts[0].lower()
        if head in _DOT_DIRS or head in _DOT_FILES or (parts[0].startswith(".") and len(parts) == 1):
            return "dotfile"
    if safety._inside(resolved, roots):
        return "inside"
    try:
        tmp = str(Path(tempfile.gettempdir()).resolve())
    except OSError:
        tmp = tempfile.gettempdir()
    if str(resolved).replace("\\", "/").lower().startswith(tmp.replace("\\", "/").lower().rstrip("/") + "/"):
        return "inside"
    return "outside"


def _token_place(token: str, cwd: Path, roots: list[Path]) -> str:
    raw = (token or "").strip().strip("'\"")
    if not raw or (raw.startswith("-") and raw != "-"):
        return "skip"
    if "://" in raw:
        return "outside"
    if raw in {"/", "\\", "/*", "\\*", "~", "$HOME", "${HOME}", "%USERPROFILE%"}:
        return "root"
    if re.fullmatch(r"(?i)[A-Za-z]:[/\\]?", raw):
        return "root"
    safety = _safety()
    return place_class(safety._resolve(raw, cwd), roots)


def _positionals(args: str) -> list[str]:
    safety = _safety()
    words = safety._quoted_words(args or "")
    found: list[str] = []
    skip_value = {"-m", "--mode", "-o", "--owner", "-g", "--group", "-r", "--reference"}
    index = 0
    while index < len(words):
        word = words[index]
        if word in skip_value:
            index += 2
            continue
        if word.startswith("-") and word != "-":
            index += 1
            continue
        found.append(word)
        index += 1
    return found


def _worst_place(tokens: list[str], cwd: Path, roots: list[Path]) -> str | None:
    rank = {"device": 5, "root": 4, "system": 3, "dotfile": 3, "outside": 1, "inside": 0, "skip": 0}
    best = None
    score = 0
    for token in tokens:
        kind = _token_place(token, cwd, roots)
        if rank.get(kind, 0) > score:
            best = kind
            score = rank[kind]
    return best if score else None


def _block_place(line: str, kind: str):
    safety = _safety()
    detail = line.strip()
    if kind == "device":
        return safety._verdict(
            safety.BLOCK,
            "disk-format",
            "That would erase a disk. It is never run.",
            detail,
            safety._fp("cmd", detail),
        )
    if kind == "root":
        return safety._verdict(
            safety.BLOCK,
            "root-delete",
            "That would change the disk root or the whole user profile. It is never run.",
            detail,
            safety._fp("cmd", detail),
        )
    return safety._verdict(
        safety.BLOCK,
        "off-limits",
        "That changes a home login file or a system folder. It is never run.",
        detail,
        safety._fp("cmd", detail),
    )


def _ask(line: str, rule: str = "unlisted-command"):
    safety = _safety()
    return safety._ask_command(rule, "That program is not on the safe list. It waits for you.", line)


def _protected(line: str, cwd: Path, roots: list[Path]):
    safety = _safety()
    program, args, dynamic, _uncertain, _unwrapped = safety._parse_invocation(line)
    if dynamic or not program:
        return None
    base = safety._base_name(program)
    tokens: list[str] = []
    if base in _MUTATORS:
        positionals = _positionals(args)
        if base in {"chmod", "chown", "chgrp"}:
            tokens = positionals[1:]
        elif base in {"ln", "link", "mv", "move", "cp", "copy", "xcopy"}:
            tokens = positionals[-1:]
        else:
            tokens = positionals
    if base == "tar" and re.search(r"(?i)--remove-files|--delete\b", args or ""):
        tokens.extend(_positionals(args))
    if base == "srm":
        tokens.extend(_positionals(args))
    if base == "diskutil" and re.search(r"(?i)\berase", args or ""):
        return _block_place(line, "device")
    for match in re.finditer(r"(?i)(?:^|[\s;&|])>{1,2}\s*(\S+)", line):
        tokens.append(match.group(1))
    kind = _worst_place(tokens, cwd, roots)
    if kind in {"device", "root", "system", "dotfile"}:
        return _block_place(line, kind)
    if kind == "outside" and (base in _MUTATORS or ">" in line):
        detail = line.strip()
        return safety._verdict(
            safety.ASK,
            "write-outside",
            "That writes outside the workspace. It waits for you.",
            detail,
            safety._fp("cmd", detail),
        )
    return None


def _script_inside(token: str, cwd: Path, roots: list[Path]) -> bool:
    if not token or "://" in token:
        return False
    kind = _token_place(token, cwd, roots)
    if kind in {"root", "device", "system", "dotfile", "outside"}:
        return False
    raw = token.strip().strip("'\"")
    if raw.startswith(("/", "\\")) or re.match(r"(?i)^[A-Za-z]:[/\\]", raw):
        return kind == "inside"
    return kind in {"inside", "skip"} or not raw.startswith("-")


def _first_command(args: str) -> str:
    for word in _positionals(args):
        return word
    return ""


_INERT_ENV = {
    "LANG", "TZ", "NO_COLOR", "CI", "DEBUG", "NODE_ENV", "PYTHONUNBUFFERED", "FORCE_COLOR",
}
_RUN_ENV = {
    "PAGER", "EDITOR", "VISUAL", "PROMPT_COMMAND", "BASH_ENV", "ENV",
    "NODE_OPTIONS", "PYTHONSTARTUP", "PERL5OPT",
    "GIT_SSH", "GIT_EDITOR", "GIT_PAGER", "GIT_EXTERNAL_DIFF", "GIT_SEQUENCE_EDITOR", "GIT_ASKPASS",
}

_OPEN_FLAGS = {
    "echo", "printf", "pwd", "whoami", "date", "uname", "true", "false", "hostname", "id",
    "cd", "pushd", "popd", "which", "where", "wc", "nl", "uniq", "cut", "tr", "column",
    "cmp", "comm", "tac", "fold", "paste", "rev", "cksum", "md5sum", "sha1sum", "sha256sum",
    "od", "hexdump", "strings", "stat", "df", "du", "free", "uptime", "file", "basename",
    "dirname", "realpath", "dirs", "test", "printenv", "get-date", "get-location",
    "write-output", "write-host", "format-table", "format-list", "format-wide",
    "measure-object", "get-item", "get-filehash", "resolve-path", "select-string",
    "findstr", "ps", "top", "fd", "cat", "type", "gc", "get-content", "get-childitem",
    "gci", "dir", "head", "tail",
}

_LS_SHORT = set("aAbBcCdDfFgGhHiIklLmMnNopPqQrRsStTuUvWxX1")
_GREP_SHORT = set("EFGPRabcdefgHhIiLlmnopqrsvwxyzABCUZ")
_GREP_VALUE = set("ABcefHm")
_RG_LONG = {
    "--line-number", "--ignore-case", "--word-regexp", "--files-with-matches", "--count",
    "--only-matching", "--after-context", "--before-context", "--context", "--glob",
    "--iglob", "--type", "--type-add", "--type-not", "--hidden", "--fixed-strings",
    "--case-sensitive", "--smart-case", "--heading", "--no-heading", "--column",
    "--regexp", "--file", "--max-count", "--max-depth", "--color", "--colors",
    "--json", "--stats", "--quiet", "--invert-match", "--multiline", "--pcre2",
    "--replace", "--sortr", "--sort", "--trim", "--crlf", "--text", "--binary",
    "--null", "--passthru", "--vimgrep", "--no-filename", "--with-filename",
    "--line-buffered", "--heading", "--debug", "--stats", "--files",
}
_RG_VALUE = {
    "--after-context", "--before-context", "--context", "--glob", "--iglob", "--type",
    "--type-add", "--type-not", "--regexp", "--file", "--max-count", "--max-depth",
    "--color", "--colors", "--replace", "--sortr", "--sort",
}
_SORT_SHORT = set("nruktsbdfiVghMcmCSz")
_SORT_VALUE = set("ktoS")
_DIFF_SHORT = set("urqwbBiEZNasyWpc")
_DIFF_VALUE = set("UW")
_LESS_SHORT = set("nNfFrRsSXiIQqeEmM")
_GIT_LOG_LONG = {
    "--oneline", "--stat", "--name-only", "--name-status", "--pretty", "--format",
    "--abbrev-commit", "--graph", "--decorate", "--all", "--author", "--grep",
    "--since", "--until", "--patch", "--max-count", "--skip", "--reverse",
    "--first-parent", "--merges", "--no-merges", "--date", "--color", "--no-color",
    "--relative", "--numstat", "--shortstat", "--raw", "--summary", "--follow",
    "--diff-filter", "--find-renames", "--find-copies", "--ignore-space-change",
    "--ignore-all-space", "--ignore-blank-lines", "--word-diff", "--abbrev",
    "--full-history", "--compact-summary", "--dirstat", "--check", "--encoding",
    "--notes", "--no-notes", "--show-signature", "--source", "--branches", "--tags",
    "--remotes", "--glob", "--exclude", "--parents", "--children", "--date-order",
    "--topo-order", "--walk-reflogs", "--boundary", "--left-right",
}
_GIT_LOG_VALUE = {
    "--pretty", "--format", "--author", "--grep", "--since", "--until", "--max-count",
    "--skip", "--date", "--diff-filter", "--encoding", "--glob", "--exclude",
    "--find-renames", "--find-copies",
}
_GIT_COMMIT_LONG = {
    "--message", "--all", "--amend", "--no-edit", "--allow-empty", "--allow-empty-message",
    "--author", "--date", "--signoff", "--file", "--reset-author", "--no-signoff",
}
_GIT_COMMIT_VALUE = {"--message", "--author", "--date", "--file"}
_GIT_ADD_LONG = {"--all", "--update", "--patch", "--dry-run", "--verbose", "--force", "--intent-to-add", "--ignore-errors", "--renormalize", "--chmod"}
_GIT_ADD_VALUE = {"--chmod"}
_GIT_PULL_LONG = {"--rebase", "--no-rebase", "--ff", "--ff-only", "--no-ff", "--autostash", "--quiet", "--verbose", "--tags", "--prune", "--no-tags"}
_GIT_FETCH_LONG = {"--all", "--tags", "--prune", "--dry-run", "--quiet", "--verbose", "--no-tags"}
_GIT_BRANCH_LONG = {"--list", "--show-current", "--no-merged", "--merged", "--all", "--remotes", "--verbose", "--delete"}
_GIT_STASH_LONG = {"--include-untracked", "--all", "--keep-index", "--message", "--index"}
_GIT_STASH_VALUE = {"--message"}
_FIND_SWITCH = {
    "-print", "-print0", "-ls", "-depth", "-xdev", "-mount", "-follow", "-noleaf",
    "-prune", "-not", "-true", "-false", "-empty", "-readable", "-writable", "-executable",
    "-quit", "-ignore_readdir_race", "-noignore_readdir_race", "-and", "-or",
}
_FIND_VALUE = {
    "-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-wholename", "-iwholename",
    "-type", "-maxdepth", "-mindepth", "-size", "-mtime", "-mmin", "-atime", "-amin",
    "-ctime", "-cmin", "-newer", "-newermt", "-newerct", "-user", "-group", "-uid", "-gid",
    "-perm", "-inum", "-links", "-fstype", "-printf", "-regextype", "-samefile", "-lname",
    "-ilname", "-context",
}
_PY_SHORT = set("BESsqubOvXWmd")
_PY_VALUE = set("XW")
_NODE_ASK = {"--require", "--import", "--loader", "--experimental-loader", "--eval", "-e", "-p", "--print", "-r"}
_NODE_OK = {"--check", "--version", "-v", "--help", "-h", "--no-warnings", "--trace-warnings", "--enable-source-maps"}
_PYTEST_OK = {
    "-q", "-v", "-vv", "-vvv", "-x", "-s", "-k", "-m", "--tb", "--maxfail", "--lf", "--ff",
    "-r", "--durations", "--collect-only", "--co", "--fixtures", "--setup-show",
    "--disable-warnings", "--strict-markers", "--strict-config", "--import-mode",
    "--rootdir", "--confcutdir", "--basetemp", "--cache-clear", "--noconftest", "-W",
    "--override-ini",
}
_PYTEST_ASK = {"-p", "--override-ini"}
_PYTEST_VALUE = {"-k", "-m", "--tb", "--maxfail", "--durations", "-r", "--import-mode", "--rootdir", "--confcutdir", "--basetemp", "-W", "--override-ini", "-p"}
_MAKE_SHORT = set("jknBsCfewqrR")
_MAKE_VALUE = set("jCf")
_MAKE_LONG = {
    "--jobs", "--keep-going", "--dry-run", "--always-make", "--silent", "--directory",
    "--file", "--makefile", "--question", "--print-directory", "--no-print-directory",
    "--environment-overrides", "--no-builtin-rules", "--no-builtin-variables",
}
_MAKE_LONG_VALUE = {"--jobs", "--directory", "--file", "--makefile"}
_NPM_LONG = {
    "--silent", "--quiet", "--prefix", "--userconfig", "--cache", "--offline",
    "--prefer-offline", "--no-audit", "--audit", "--ignore-scripts", "--no-fund",
    "--fund", "--loglevel", "--color", "--no-color", "--parseable", "--json",
    "--long", "--depth", "--global", "--workspace", "--workspaces", "--include",
    "--omit", "--foreground-scripts", "--no-progress", "--progress",
}
_NPM_VALUE = {"--prefix", "--userconfig", "--cache", "--loglevel", "--depth", "--workspace", "--include", "--omit"}
_CARGO_LONG = {
    "--release", "--quiet", "--verbose", "--jobs", "--target", "--features",
    "--all-features", "--no-default-features", "--offline", "--locked", "--frozen",
    "--color", "--message-format", "--manifest-path", "--package", "--workspace",
    "--all", "--lib", "--bins", "--bin", "--test", "--tests", "--example",
    "--examples", "--doc", "--all-targets", "--no-run", "--no-fail-fast",
}
_CARGO_VALUE = {"--jobs", "--target", "--features", "--color", "--message-format", "--manifest-path", "--package", "--bin", "--test", "--example"}
_GO_ASK = {"-toolexec", "-exec"}
_GO_OK = {"-count", "-timeout", "-run", "-v", "-race", "-cover", "-short", "-json", "-c", "-o", "-mod", "-trimpath", "-x", "-n", "-a", "-p", "-ldflags", "-gcflags", "-asmflags", "-tags"}
_GO_VALUE = {"-count", "-timeout", "-run", "-o", "-mod", "-p", "-ldflags", "-gcflags", "-asmflags", "-tags", "-toolexec", "-exec", "-coverpkg"}
_SCRIPT_SUFFIX = {".sh", ".bash", ".py", ".js", ".ts", ".mjs", ".ps1", ".bat", ".cmd", ".rb", ".pl"}
_BUILD_NAMES = ("package.json", "makefile", "gnumakefile", "conftest.py", "setup.py", "pyproject.toml")


def _ask_rule(line: str, rule: str, why: str):
    safety = _safety()
    return safety._ask_command(rule, why, line)


def _judge_text(text: str, cwd, roots, remote, created, mode, denied, depth):
    safety = _safety()
    if depth >= safety._WRAP_DEPTH:
        return _ask_rule(text, "wrapped-command", "That command is nested too deep to read. It waits for you.")
    return safety.judge_command(
        text,
        remote=remote,
        cwd=cwd,
        roots=roots,
        created=created,
        mode=mode,
        denied=denied,
        depth=depth + 1,
    )


def _read_paren(text: str, start: int) -> tuple[str, int, bool]:
    depth = 1
    index = start
    quote = ""
    while index < len(text):
        ch = text[index]
        if quote:
            if ch == "\\" and quote == '"' and index + 1 < len(text):
                index += 2
                continue
            if ch == quote:
                quote = ""
            index += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            index += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1, True
        index += 1
    return text[start:], len(text), False


def _body_dynamic(body: str) -> bool:
    if not (body or "").strip():
        return True
    quote = ""
    index = 0
    while index < len(body):
        ch = body[index]
        if quote:
            if ch == quote:
                quote = ""
            index += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            index += 1
            continue
        if ch in {"$", "`"}:
            return True
        if body.startswith("<(", index) or body.startswith(">(", index):
            return True
        index += 1
    return False


def _substitutions(text: str) -> list[tuple[str, bool, str]]:
    """Unquoted process substitutions, and ``$()`` inside or outside double quotes."""
    found: list[tuple[str, bool, str]] = []
    index = 0
    quote = ""
    source = text or ""
    while index < len(source):
        ch = source[index]
        if quote == "'":
            if ch == "'":
                quote = ""
            index += 1
            continue
        if quote == '"':
            if ch == '"':
                quote = ""
                index += 1
                continue
            if source.startswith("$(", index):
                body, index, closed = _read_paren(source, index + 2)
                found.append((body, (not closed) or _body_dynamic(body), "command-substitution"))
                continue
            index += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            index += 1
            continue
        if source.startswith("$(", index):
            body, index, closed = _read_paren(source, index + 2)
            found.append((body, (not closed) or _body_dynamic(body), "command-substitution"))
            continue
        if source.startswith("<(", index) or source.startswith(">(", index):
            body, index, closed = _read_paren(source, index + 2)
            found.append((body, (not closed) or _body_dynamic(body), "process-substitution"))
            continue
        index += 1
    return found


def _substitution_decision(line: str, cwd, roots, remote, created, mode, denied, depth):
    worst = None
    safety = _safety()
    for body, dynamic, rule in _substitutions(line):
        if dynamic:
            hit = _ask_rule(line, rule, "That substitution builds a command when it runs. It waits for you.")
        else:
            hit = _judge_text(body, cwd, roots, remote, created, mode, denied, depth)
            if hit.tier == safety.ALLOW:
                continue
            if hit.tier != safety.BLOCK:
                hit = _ask_rule(line, rule, "That substitution runs another command. It waits for you.")
        if hit.tier == safety.BLOCK:
            return hit
        worst = hit
    return worst


def _env_kind(name: str) -> str:
    upper = (name or "").upper()
    if upper in _INERT_ENV or upper.startswith("LC_"):
        return "inert"
    if upper.endswith("_COMMAND") or upper in _RUN_ENV or upper.startswith(("LD_", "DYLD_")):
        return "run" if (upper.endswith("_COMMAND") or upper in _RUN_ENV) else "ask"
    if upper.startswith("GIT_CONFIG_") or upper in {"GIT_EXEC_PATH", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}:
        return "run"
    if upper in {"GIT_DIR", "GIT_WORK_TREE"}:
        return "locate"
    return "ask"


def _unquote_assign(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _assignment_hit(name: str, value: str, line: str, cwd, roots, remote, created, mode, denied, depth):
    kind = _env_kind(name)
    if kind == "inert":
        return None
    safety = _safety()
    if kind == "run":
        inner = _judge_text(_unquote_assign(value), cwd, roots, remote, created, mode, denied, depth)
        if inner.tier == safety.BLOCK:
            return inner
    return _ask_rule(
        line,
        "env-prefix",
        "That environment assignment can change what the command runs. It waits for you.",
    )


_BASH_ASSIGN = re.compile(
    r"""^([A-Za-z_][A-Za-z0-9_]*)=(?:'([^']*)'|"((?:\\.|[^"\\])*)"|(\S*))\s*"""
)
_PS_ASSIGN = re.compile(
    r"""(?i)^\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:'([^']*)'|"([^"]*)"|(\S+))\s*$"""
)
_CMD_ASSIGN = re.compile(
    r"""(?i)^set\s+(?:"([A-Za-z_][A-Za-z0-9_]*)=([^"]*)"|([A-Za-z_][A-Za-z0-9_]*)=(.*))\s*$"""
)


def _bash_assignments(piece: str) -> tuple[list[tuple[str, str]], str] | None:
    rest = piece.strip()
    lowered = rest.lower()
    if lowered.startswith("env ") and not rest[4:].lstrip().startswith("-"):
        rest = rest[4:].lstrip()
    found: list[tuple[str, str]] = []
    while True:
        match = _BASH_ASSIGN.match(rest)
        if not match:
            break
        value = match.group(2)
        if value is None:
            value = match.group(3) if match.group(3) is not None else match.group(4) or ""
        found.append((match.group(1), value))
        rest = rest[match.end():]
    if not found:
        return None
    return found, rest.strip()


def _one_assignment(piece: str) -> tuple[list[tuple[str, str]], str] | None:
    text = piece.strip()
    ps = _PS_ASSIGN.match(text)
    if ps:
        value = next(group for group in ps.groups()[1:] if group is not None)
        return [(ps.group(1), value)], ""
    cmd = _CMD_ASSIGN.match(text)
    if cmd:
        if cmd.group(1) is not None:
            return [(cmd.group(1), cmd.group(2) or "")], ""
        return [(cmd.group(3), cmd.group(4) or "")], ""
    return _bash_assignments(text)


def _env_policy(command: str, cwd, roots, remote, created, mode, denied, depth):
    """Ask or block on a non-inert assignment. Return a rewritten command when only inert ones were removed."""
    safety = _safety()
    pieces = safety._split_chain(command or "")
    if not pieces:
        return None, None
    kept: list[str] = []
    saw = False
    for piece in pieces:
        parsed = _one_assignment(piece)
        if parsed is None:
            kept.append(piece.strip())
            continue
        assigns, rest = parsed
        if assigns and all(_env_kind(name) == "locate" for name, _value in assigns):
            kept.append(piece.strip())
            saw = True
            continue
        saw = True
        for name, value in assigns:
            hit = _assignment_hit(name, value, command, cwd, roots, remote, created, mode, denied, depth)
            if hit is not None:
                return hit, None
        if rest:
            kept.append(rest)
    if not saw:
        return None, None
    rewritten = " ; ".join(item for item in kept if item)
    if rewritten.strip() == (command or "").strip():
        return None, None
    return None, rewritten


def _words(args: str) -> list[str]:
    return _safety()._quoted_words(args or "")


def _flag_walk(args: str, *, shorts: set[str], values: set[str], longs: set[str], long_values: set[str], exec_long: set[str], exec_short: set[str], ask_long: set[str], numeric: bool = False, plus: bool = False, windows: bool = False):
    """Return ('ask', None), ('exec', value), or None when every flag is inert."""
    words = _words(args)
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            return None
        if plus and word.startswith("+"):
            return ("exec", word[1:].lstrip("!"))
        if windows and word.startswith("/") and not word.startswith("//"):
            head = word.split(":", 1)[0].lower()
            if head in longs or head.lstrip("/")[:1] in shorts:
                index += 1
                continue
            return ("ask", None)
        if word.startswith("--"):
            name, _, attached = word.partition("=")
            low = name.lower()
            if low in exec_long or low in ask_long:
                if low in ask_long and low not in exec_long:
                    return ("ask", None)
                value = attached if attached or "=" in word else (words[index + 1] if index + 1 < len(words) else "")
                return ("exec", value)
            if low in longs:
                if low in long_values and "=" not in word:
                    index += 2
                else:
                    index += 1
                continue
            return ("ask", None)
        if numeric and re.fullmatch(r"-\d+", word):
            index += 1
            continue
        if word.startswith("-") and word != "-" and not word.startswith("--"):
            body = word[1:]
            cursor = 0
            consume = False
            while cursor < len(body):
                ch = body[cursor]
                if ch in exec_short:
                    value = body[cursor + 1:] or (words[index + 1] if index + 1 < len(words) else "")
                    return ("exec", value)
                if ch in values:
                    if cursor + 1 < len(body):
                        break
                    consume = True
                    break
                if ch not in shorts:
                    return ("ask", None)
                cursor += 1
            index += 2 if consume else 1
            continue
        index += 1
    return None


def _from_walk(walk, line, cwd, roots, remote, created, mode, denied, depth):
    if walk is None:
        return None
    kind, value = walk
    if kind == "exec" and value:
        inner = _judge_text(value, cwd, roots, remote, created, mode, denied, depth)
        if inner.tier == _safety().BLOCK:
            return inner
    return _ask_rule(line, "exec-flag", "That flag can run a program. It waits for you.")


def _closed_flags(base: str, args: str, line: str, cwd, roots, remote, created, mode, denied, depth):
    specs = {
        "ls": dict(shorts=_LS_SHORT, values=set(), longs={"--all", "--almost-all", "--human-readable", "--color", "--classify", "--directory", "--inode", "--size", "--reverse", "--recursive", "--group-directories-first"}, long_values={"--color", "--sort", "--time", "--format", "--quoting-style", "--hide", "--ignore", "--time-style", "--block-size", "--width"}, exec_long=set(), exec_short=set(), ask_long=set()),
        "grep": dict(shorts=_GREP_SHORT, values=_GREP_VALUE, longs={"--extended-regexp", "--fixed-strings", "--basic-regexp", "--perl-regexp", "--recursive", "--line-number", "--ignore-case", "--invert-match", "--word-regexp", "--line-regexp", "--count", "--files-with-matches", "--only-matching", "--text", "--after-context", "--before-context", "--context", "--with-filename", "--no-filename", "--binary-files", "--max-count", "--quiet", "--no-messages", "--null", "--regexp", "--file", "--include", "--exclude", "--exclude-dir", "--color", "--colour"}, long_values={"--after-context", "--before-context", "--context", "--max-count", "--regexp", "--file", "--include", "--exclude", "--exclude-dir", "--binary-files", "--color", "--colour"}, exec_long={"--pre"}, exec_short=set(), ask_long={"--pre-glob"}),
        "egrep": dict(shorts=_GREP_SHORT, values=_GREP_VALUE, longs={"--line-number", "--ignore-case", "--invert-match", "--recursive", "--color"}, long_values={"--color"}, exec_long={"--pre"}, exec_short=set(), ask_long={"--pre-glob"}),
        "fgrep": dict(shorts=_GREP_SHORT, values=_GREP_VALUE, longs={"--line-number", "--ignore-case", "--invert-match", "--recursive", "--color"}, long_values={"--color"}, exec_long={"--pre"}, exec_short=set(), ask_long={"--pre-glob"}),
        "rg": dict(shorts=set("nilwcABgtsSFvqHNeUPm"), values=set("ABCgeftm"), longs=_RG_LONG, long_values=_RG_VALUE, exec_long={"--pre"}, exec_short=set(), ask_long={"--pre-glob"}),
        "sort": dict(shorts=_SORT_SHORT, values=_SORT_VALUE, longs={"--numeric-sort", "--reverse", "--unique", "--key", "--field-separator", "--stable", "--human-numeric-sort", "--version-sort", "--general-numeric-sort", "--month-sort", "--ignore-leading-blanks", "--ignore-case", "--dictionary-order", "--check", "--merge", "--output", "--buffer-size", "--parallel", "--zero-terminated"}, long_values={"--key", "--field-separator", "--output", "--buffer-size", "--parallel"}, exec_long={"--compress-program"}, exec_short=set(), ask_long=set()),
        "diff": dict(shorts=_DIFF_SHORT, values=_DIFF_VALUE, longs={"--unified", "--recursive", "--brief", "--ignore-space-change", "--ignore-all-space", "--ignore-blank-lines", "--ignore-case", "--report-identical-files", "--text", "--strip-trailing-cr", "--color", "--show-c-function", "--side-by-side", "--width", "--suppress-common-lines", "--normal", "--context", "--minimal"}, long_values={"--unified", "--width", "--color", "--tabsize", "--horizon-lines"}, exec_long=set(), exec_short=set(), ask_long={"--to-file", "--from-file"}),
        "less": dict(shorts=_LESS_SHORT, values=set("pP"), longs={"--quit-if-one-screen", "--raw-control-chars", "--chop-long-lines", "--ignore-case", "--no-init", "--quit-at-eof", "--silent", "--quiet", "--squeeze-blank-lines", "--pattern", "--prompt"}, long_values={"--pattern", "--prompt"}, exec_long=set(), exec_short=set(), ask_long=set(), plus=True),
        "more": dict(shorts=set("dlfpsu"), values=set("p"), longs={"--logical", "--silent", "--plain", "--squeeze"}, long_values=set(), exec_long=set(), exec_short=set(), ask_long=set(), plus=True),
    }
    if base in {"man"}:
        walk = _flag_walk(args, shorts=set("akfwl"), values=set("P"), longs={"--all", "--apropos", "--whatis", "--where", "--local-file", "--pager"}, long_values={"--pager"}, exec_long={"--pager"}, exec_short=set("P"), ask_long=set())
        return _from_walk(walk, line, cwd, roots, remote, created, mode, denied, depth)
    spec = specs.get(base)
    if spec is None:
        return None
    walk = _flag_walk(args, **spec)
    return _from_walk(walk, line, cwd, roots, remote, created, mode, denied, depth)


def _find_decision(args: str, line: str, cwd, roots, remote, created, mode, denied, depth):
    if _FIND_UNSAFE.search(args or ""):
        match = re.search(r"(?i)\s-(?:exec|execdir|ok|okdir)\s+(.+?)\s+(?:\\;|;|\+)", args or "")
        if match:
            inner = _judge_text(match.group(1), cwd, roots, remote, created, mode, denied, depth)
            if inner.tier == _safety().BLOCK:
                return inner
        return _ask_rule(line, "exec-flag", "That find action can run a program or delete a file. It waits for you.")
    words = _words(args)
    index = 0
    while index < len(words):
        word = words[index]
        low = word.lower()
        if low in {"-a", "-o"}:
            index += 1
            continue
        if low in _FIND_SWITCH or low in {"-and", "-or"}:
            index += 1
            continue
        if low in _FIND_VALUE:
            index += 2
            continue
        if word.startswith("-"):
            return _ask_rule(line, "exec-flag", "That find flag is not a known test. It waits for you.")
        index += 1
    return None


def _option_value(text: str, name: str) -> str:
    """The value of ``--name=value`` or ``--name value``, including when the whole option is quoted."""
    needle = name + "="
    folded = (text or "").lower()
    idx = folded.find(needle)
    if idx < 0:
        match = re.search(rf"(?i)(?:^|\s){re.escape(name)}\s+(\S+)", text or "")
        return match.group(1).strip("'\"") if match else ""
    start = idx + len(needle)
    if start < len(text) and text[start] in {"'", '"'}:
        quote = text[start]
        end = text.find(quote, start + 1)
        return text[start + 1:end] if end > start else text[start + 1:]
    if idx > 0 and text[idx - 1] in {"'", '"'}:
        quote = text[idx - 1]
        end = text.find(quote, start)
        return text[start:end] if end > start else text[start:]
    end = start
    while end < len(text) and not text[end].isspace():
        end += 1
    return text[start:end].strip("'\"")


def _tar_decision(args: str, line: str, cwd, roots, remote, created, mode, denied, depth):
    text = line or args or ""
    if re.search(r"(?i)--remove-files|--delete\b", text):
        return _ask(line)
    if re.search(r"(?i)--to-command|--use-compress-program|(?:^|\s)-I\b", text):
        value = _option_value(text, "--to-command") or _option_value(text, "--use-compress-program")
        if not value:
            words = _words(text)
            for index, word in enumerate(words):
                if word == "-I" and index + 1 < len(words):
                    value = words[index + 1]
                    break
                if word.startswith("-I") and len(word) > 2:
                    value = word[2:]
                    break
        if value:
            inner = _judge_text(value.strip("'\""), cwd, roots, remote, created, mode, denied, depth)
            if inner.tier == _safety().BLOCK:
                return inner
        return _ask_rule(line, "exec-flag", "That tar flag runs a program. It waits for you.")
    listing = re.search(r"(?i)(?:^|\s)-[^-]*t|--list\b", text) and not re.search(r"(?i)(?:^|\s)-[^-]*[xcru]", text)
    if not listing:
        return _ask(line)
    walk = _flag_walk(
        text,
        shorts=set("tfvzjJaOxC"),
        values=set("fC"),
        longs={"--list", "--file", "--verbose", "--gzip", "--bzip2", "--xz", "--auto-compress", "--to-stdout", "--directory", "--wildcards", "--exclude", "--anchored"},
        long_values={"--file", "--directory", "--exclude"},
        exec_long=set(),
        exec_short=set(),
        ask_long=set(),
    )
    if walk is not None:
        return _ask_rule(line, "exec-flag", "That tar flag is not a known list option. It waits for you.")
    return None


def _git_config_values(line: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in re.finditer(r"(?i)(?:^|\s)(?:-c|--config)\s+(?:(['\"])(.+?)\1|(\S+))", line or ""):
        raw = match.group(2) if match.group(2) is not None else match.group(3) or ""
        if "=" in raw:
            key, value = raw.split("=", 1)
            found.append((key, value))
    return found


def _command_key(key: str, value: str) -> bool:
    if re.search(r"(?i)command$", key or "") or re.fullmatch(r"(?i)core\.hookspath", key or ""):
        return True
    if re.match(r"(?i)alias\.", key or "") and (value or "").lstrip().startswith("!"):
        return True
    return False


def _git_decision(args: str, line: str, cwd, roots, remote, created, mode, denied, depth):
    safety = _safety()
    if re.search(r"(?i)--(?:upload|receive)-pack\b|--exec-path\b", args or "") or re.search(r"(?:^|\s)-c\b|--config\b", args or ""):
        for _key, value in _git_config_values(line):
            if not value:
                continue
            inner = _judge_text(value, cwd, roots, remote, created, mode, denied, depth)
            if inner.tier == safety.BLOCK:
                return inner
        for match in re.finditer(r"(?i)--(?:upload-pack|receive-pack|exec-path)(?:=|\s+)(\S+)", args or ""):
            inner = _judge_text(match.group(1).strip("'\""), cwd, roots, remote, created, mode, denied, depth)
            if inner.tier == safety.BLOCK:
                return inner
        if _ROOT_WIPE.search(line):
            return _block_place(line, "root")
        return _ask_rule(line, "exec-flag", "That git option can run a program. It waits for you.")
    words = _words(args)
    sub = ""
    rest: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        low = word.lower()
        if word == "-C" or low in {"--git-dir", "--work-tree", "--namespace"}:
            index += 2
            continue
        if word == "-c" or low == "--config":
            return _ask_rule(line, "exec-flag", "That git option can run a program. It waits for you.")
        if low.startswith("-"):
            return _ask(line)
        sub = low
        rest = words[index + 1:]
        break
    if sub == "config":
        key = ""
        value = ""
        for word in rest:
            if word.startswith("-"):
                continue
            if not key:
                key = word
                continue
            value = word
            break
        if _command_key(key, value):
            inner = _judge_text(value.lstrip("!"), cwd, roots, remote, created, mode, denied, depth) if value else None
            if inner is not None and inner.tier == safety.BLOCK:
                return inner
            if _ROOT_WIPE.search(line):
                return _block_place(line, "root")
        return _ask_rule(line, "exec-flag", "That git config write can name a command. It waits for you.")
    if sub not in _GIT_SAFE:
        return _ask(line)
    if sub == "branch" and re.search(r"(?i)(?:^|\s)(?:-D|-d|--delete)\b", args or ""):
        return _ask(line)
    if re.search(r"(?i)--ext-diff\b|--textconv\b", args or ""):
        return _ask_rule(line, "exec-flag", "That git flag runs an external diff. It waits for you.")
    joined = " ".join(rest)
    tables = {
        "log": (_GIT_LOG_LONG, _GIT_LOG_VALUE, set("psng"), True),
        "diff": (_GIT_LOG_LONG, _GIT_LOG_VALUE, set("pwuab"), False),
        "show": (_GIT_LOG_LONG, _GIT_LOG_VALUE, set("ps"), True),
        "commit": (_GIT_COMMIT_LONG, _GIT_COMMIT_VALUE, set("am"), False),
        "add": (_GIT_ADD_LONG, _GIT_ADD_VALUE, set("AupnvfN"), False),
        "pull": (_GIT_PULL_LONG, set(), set("qv"), False),
        "fetch": (_GIT_FETCH_LONG, set(), set("qv"), False),
        "branch": (_GIT_BRANCH_LONG, set(), set("arvv"), False),
        "stash": (_GIT_STASH_LONG, _GIT_STASH_VALUE, set("uam"), False),
        "status": ({"--short", "--branch", "--porcelain", "--verbose"}, set(), set("sbuz"), False),
        "blame": ({"--line-porcelain", "--porcelain"}, {"-L", "--since"}, set("p"), False),
        "tag": ({"--list", "--points-at"}, {"--points-at"}, set("l"), False),
        "remote": ({"--verbose"}, set(), set("v"), False),
        "ls-files": ({"--stage", "--deleted", "--modified", "--others", "--exclude-standard"}, set(), set("s"), False),
        "grep": ({"--line-number", "--ignore-case"}, set(), set("ni"), False),
        "rev-parse": ({"--abbrev-ref", "--short", "--verify", "--quiet"}, set(), set("q"), False),
        "describe": ({"--tags", "--always", "--dirty"}, set(), set(), False),
        "shortlog": ({"--summary", "--numbered", "--email"}, set(), set("sne"), False),
    }
    longs, long_values, shorts, numeric = tables.get(sub, (set(), set(), set(), False))
    walk = _flag_walk(joined, shorts=shorts, values=set("mL"), longs=longs, long_values=long_values, exec_long=set(), exec_short=set(), ask_long=set(), numeric=numeric)
    if walk is not None:
        return _from_walk(walk, line, cwd, roots, remote, created, mode, denied, depth)
    return None


def _npm_flags(args: str, line: str):
    walk = _flag_walk(args, shorts=set("sgq"), values=set(), longs=_NPM_LONG, long_values=_NPM_VALUE, exec_long=set(), exec_short=set(), ask_long=set())
    if walk is not None:
        return _ask_rule(line, "exec-flag", "That package-manager flag is not a known inert flag. It waits for you.")
    return None


def _interp_flags(base: str, args: str, line: str, cwd, roots, remote, created, mode, denied, depth):
    words = _words(args)
    index = 0
    while index < len(words):
        word = words[index]
        low = word.lower()
        if base in {"node", "deno"}:
            name = low.split("=", 1)[0]
            if low in {"-e", "-p"}:
                return None
            if name in _NODE_ASK:
                return _ask_rule(line, "exec-flag", "That node flag can run a program. It waits for you.")
            if low in _NODE_OK or low.startswith("--"):
                if name not in _NODE_OK and low.startswith("--"):
                    return _ask_rule(line, "exec-flag", "That node flag is not a known inert flag. It waits for you.")
                index += 1
                continue
            return None
        if low in {"-c", "-e", "-m", "--module"} or low.startswith("--eval"):
            return None
        if low.startswith("-"):
            name = low.split("=", 1)[0]
            letter = name[1:2]
            if letter and letter not in _PY_SHORT and name not in {"-x", "-w"}:
                return _ask_rule(line, "exec-flag", "That interpreter flag is not a known inert flag. It waits for you.")
            if letter in _PY_VALUE and "=" not in word:
                index += 2
                continue
            index += 1
            continue
        return None
    return None


def _pytest_flags(args: str, line: str):
    walk = _flag_walk(
        args,
        shorts=set("qvxskmrW"),
        values=set("kmrW"),
        longs={item for item in _PYTEST_OK if item.startswith("--") and item not in _PYTEST_ASK},
        long_values={item for item in _PYTEST_VALUE if item.startswith("--") and item not in _PYTEST_ASK},
        exec_long=set(),
        exec_short=set("p"),
        ask_long=_PYTEST_ASK,
    )
    if walk is not None:
        return _ask_rule(line, "exec-flag", "That pytest flag can load code. It waits for you.")
    return None


def _make_flags(args: str, line: str):
    walk = _flag_walk(args, shorts=_MAKE_SHORT, values=_MAKE_VALUE, longs=_MAKE_LONG, long_values=_MAKE_LONG_VALUE, exec_long=set(), exec_short=set(), ask_long=set())
    if walk is not None:
        return _ask_rule(line, "exec-flag", "That make flag is not a known inert flag. It waits for you.")
    return None


def _cargo_flags(args: str, line: str):
    walk = _flag_walk(args, shorts=set("qvjp"), values=set("jp"), longs=_CARGO_LONG, long_values=_CARGO_VALUE, exec_long=set(), exec_short=set(), ask_long=set())
    if walk is not None:
        return _ask_rule(line, "exec-flag", "That cargo flag is not a known inert flag. It waits for you.")
    return None


def _go_flags(args: str, line: str, cwd, roots, remote, created, mode, denied, depth):
    walk = _flag_walk(args, shorts=set(), values=set(), longs=_GO_OK, long_values=_GO_VALUE, exec_long=set(_GO_ASK), exec_short=set(), ask_long=set())
    return _from_walk(walk, line, cwd, roots, remote, created, mode, denied, depth)


def _npm_decision(base: str, args: str, line: str, cwd, roots):
    if base == "npx":
        if _worst_place(_positionals(args), cwd, roots) in {"root", "device", "system"}:
            return _block_place(line, "root")
        return _ask(line)
    sub = _first_command(args).lower()
    if sub in {"exec", "explore", "x"}:
        if _worst_place(_positionals(args), cwd, roots) in {"root", "device", "system"}:
            return _block_place(line, "root")
        return _ask(line)
    if sub in _NPM_SAFE:
        return None
    return _ask(line)


def _docker_decision(args: str, line: str):
    if re.search(r"(?i)(?:^|\s)(?:-v|--volume)\s+(/|[A-Za-z]:[/\\]?)(?::|\s|$)", args or ""):
        return _block_place(line, "root")
    words = _positionals(args)
    if not words:
        return _ask(line)
    head = words[0].lower()
    if head == "compose":
        sub = words[1].lower() if len(words) > 1 else ""
        if sub in {"ps", "version", "logs", "images", "config", "top"}:
            return None
        return _ask(line)
    if head in _DOCKER_READ:
        return None
    return _ask(line)


def _interp_decision(base: str, args: str, line: str, cwd, roots):
    if re.match(r"(?i)^python3?\s+-c\s+['\"]print\b", line) or re.match(
        r"(?i)^node\s+-e\s+['\"]console\.log\b", line
    ):
        return None
    words = _safety()._quoted_words(args or "")
    inline = {"-c", "-e", "-r", "-E", "--eval", "-p", "eval"}
    index = 0
    while index < len(words):
        word = words[index]
        low = word.lower()
        if low in inline or low.startswith("--eval"):
            if _ROOT_WIPE.search(line):
                return _block_place(line, "root")
            return _ask(line, "inline-code")
        if low in {"-m", "--module"}:
            module = words[index + 1].lower() if index + 1 < len(words) else ""
            if module in {"pytest", "unittest"}:
                return None
            return _ask(line, "inline-code")
        if low.startswith("-"):
            index += 1
            continue
        if _script_inside(word, cwd, roots):
            return None
        return _ask(line)
    return _ask(line, "inline-code")


def _make_decision(args: str, line: str, cwd, roots):
    words = _safety()._quoted_words(args or "")
    index = 0
    while index < len(words):
        word = words[index]
        spec = ""
        if word in {"-f", "--file", "--makefile"}:
            spec = words[index + 1] if index + 1 < len(words) else ""
            index += 2
        elif word.startswith("-f") and len(word) > 2:
            spec = word[2:]
            index += 1
        else:
            index += 1
            continue
        if "://" in spec or _token_place(spec, cwd, roots) in {"system", "root", "device", "dotfile", "outside"}:
            return _ask(line)
    return None


def _ssh_command(args: str) -> str | None:
    words = _safety()._quoted_words(args or "")
    valued = {"-o", "-i", "-p", "-l", "-F", "-J", "-b", "-c", "-D", "-L", "-R", "-S", "-E", "-W"}
    index = 0
    while index < len(words):
        word = words[index]
        if word in valued:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        index += 1
        rest = words[index:]
        return " ".join(rest) if rest else None
    return None


def _tool_decision(base: str, args: str, program: str, line: str, cwd, roots, remote, created, mode, denied, depth):
    safety = _safety()
    if base in _AWK or base in _EDITORS:
        if _ROOT_WIPE.search(line):
            return _block_place(line, "root")
        return _ask(line)
    if base in {"ssh", "scp", "sftp"}:
        if _ROOT_WIPE.search(line):
            return _block_place(line, "root")
        if base == "ssh":
            remote_cmd = _ssh_command(args)
            if remote_cmd and depth < safety._WRAP_DEPTH:
                inner = safety.judge_command(
                    remote_cmd,
                    remote=True,
                    cwd=cwd,
                    roots=roots,
                    created=created,
                    mode=mode,
                    denied=denied,
                    depth=depth + 1,
                )
                if inner.tier == safety.BLOCK:
                    return inner
        return _ask(line)
    if base in {"docker", "podman"}:
        return _docker_decision(args, line)
    if base in {"npx", "npm", "pnpm", "yarn"}:
        flagged = _npm_flags(args, line)
        if flagged is not None:
            return flagged
        return _npm_decision(base, args, line, cwd, roots)
    if base == "git":
        return _git_decision(args, line, cwd, roots, remote, created, mode, denied, depth)
    if base == "find":
        return _find_decision(args, line, cwd, roots, remote, created, mode, denied, depth)
    if base in {"make", "gmake"}:
        flagged = _make_flags(args, line)
        if flagged is not None:
            return flagged
        return _make_decision(args, line, cwd, roots)
    if base in _INTERPRETERS:
        flagged = _interp_flags(base, args, line, cwd, roots, remote, created, mode, denied, depth)
        if flagged is not None:
            return flagged
        return _interp_decision(base, args, line, cwd, roots)
    if base in _SHELLS:
        script = _first_command(args)
        if script and _script_inside(script, cwd, roots):
            return None
        return _ask(line)
    if base == "cargo":
        sub = _first_command(args).lower() or "build"
        if sub not in _CARGO_SAFE:
            return _ask(line)
        flagged = _cargo_flags(args, line)
        if flagged is not None:
            return flagged
        return None
    if base == "go":
        sub = _first_command(args).lower()
        if sub not in _GO_SAFE:
            return _ask(line)
        flagged = _go_flags(args, line, cwd, roots, remote, created, mode, denied, depth)
        if flagged is not None:
            return flagged
        return None
    if base in {"pytest", "py.test"}:
        return _pytest_flags(args, line)
    if base in {"pip", "pip3"}:
        sub = _first_command(args).lower()
        if sub in {"list", "show", "freeze", "check", "debug", "index"}:
            return None
        return _ask(line)
    if base == "man":
        flagged = _closed_flags(base, args, line, cwd, roots, remote, created, mode, denied, depth)
        if flagged is not None and flagged.tier == safety.BLOCK:
            return flagged
        return _ask(line)
    if base in _READ_ONLY:
        if base not in _OPEN_FLAGS:
            flagged = _closed_flags(base, args, line, cwd, roots, remote, created, mode, denied, depth)
            if flagged is not None:
                return flagged
        return None
    if base == "tar":
        return _tar_decision(args, line, cwd, roots, remote, created, mode, denied, depth)
    if base in {"rsync", "xargs"}:
        return _ask(line)
    raw = program.strip().strip("'\"")
    if raw.startswith(("./", ".\\")) and _script_inside(raw, cwd, roots):
        return None
    return _ask(line)


def _records(store, bot_id: str) -> tuple[dict, dict]:
    bot = store.get_bot(bot_id)
    raw = bot.get("safety_scripts") or {}
    return bot, raw if isinstance(raw, dict) else {}


def _strip_extended(text: str) -> str:
    """Drop a Windows \\\\?\\ prefix so the same file does not have two spellings."""
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    if text.startswith("//?/UNC/"):
        return "//" + text[8:]
    if text.startswith("//?/"):
        return text[4:]
    return text


def _fold_text(text: str) -> str:
    """Case, slashes, and the \\\\?\\ prefix, without resolving. Used only when resolve fails.

    Windows normcase folds case. APFS is case-insensitive too, and normcase does not fold it.
    """
    folded = os.path.normcase(_strip_extended(text)).replace("\\", "/")
    if sys.platform == "darwin":
        folded = folded.casefold()
    if folded != "/" and not (len(folded) == 3 and folded[1:3] == ":/"):
        folded = folded.rstrip("/")
    return folded


def _canonical_key(path: Path | str) -> str | None:
    """One key: resolved, case-normalized, no \\\\?\\ prefix, forward slashes.

    None means the path could not be resolved. An exec tool then asks.
    """
    try:
        stripped = _strip_extended(os.fspath(path))
        resolved = Path(stripped).resolve()
        folded = _fold_text(os.fspath(resolved))
    except (OSError, ValueError, RuntimeError):
        return None
    return folded or None


def _key_under(path_key: str, root_key: str) -> bool:
    if not path_key or not root_key:
        return False
    if path_key == root_key:
        return True
    return path_key.startswith(root_key.rstrip("/") + "/")


def _index_records(records: dict) -> tuple[dict[str, dict], bool]:
    """Map each record onto its canonical key. The flag is set when one key cannot be resolved."""
    index: dict[str, dict] = {}
    failed = False
    for key, rec in records.items():
        if not isinstance(rec, dict):
            continue
        canon = _canonical_key(key)
        if canon is None or rec.get("unresolved"):
            failed = True
            canon = canon or _fold_text(str(key))
        if not canon:
            continue
        existing = index.get(canon)
        if existing is None:
            index[canon] = rec
            continue
        existing_open = str(existing.get("approved") or "") != str(existing.get("bot_sha") or "")
        this_open = str(rec.get("approved") or "") != str(rec.get("bot_sha") or "")
        if this_open and not existing_open:
            index[canon] = rec
    return index, failed


def _rekey_record(records: dict, path: Path, rec: dict) -> None:
    """Store the record under the canonical key and drop other spellings of that file."""
    canon = _canonical_key(path)
    stored = dict(rec)
    if canon is None:
        stored["unresolved"] = True
        key = _fold_text(os.fspath(path))
    else:
        stored.pop("unresolved", None)
        key = canon
    for old in list(records):
        if old == key:
            continue
        other = _canonical_key(old) or _fold_text(str(old))
        if other == key:
            records.pop(old, None)
    records[key] = stored


def note_bot_write(store, bot_id: str, path: Path, body: str) -> None:
    """Remember a file this bot is about to write, and the bytes from before that write."""
    from easyagent.store import atomic_write_json

    bot, records = _records(store, bot_id)
    key = _canonical_key(path) or _fold_text(os.fspath(path))
    prev: dict = {}
    for old, rec in list(records.items()):
        if not isinstance(rec, dict):
            continue
        other = _canonical_key(old) or _fold_text(str(old))
        if other == key:
            prev = rec
            break
    before = prev.get("before") if prev else None
    if before is None:
        try:
            before = path.read_text(encoding="utf-8", errors="replace")[:20000] if path.is_file() else ""
        except OSError:
            before = ""
    payload = body if isinstance(body, str) else ""
    _rekey_record(records, path, {
        "bot_sha": _logical_sha(payload.encode("utf-8", errors="replace")),
        "approved": str(prev.get("approved") or ""),
        "before": before if isinstance(before, str) else "",
    })
    bot["safety_scripts"] = records
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)


def _logical_sha(data: bytes) -> str:
    """Hash text the way the bot wrote it. A Windows text write uses \\r\\n for the same \\n."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")).hexdigest()


def _file_sha(path: Path) -> str:
    try:
        return _logical_sha(path.read_bytes())
    except OSError:
        return ""


_CONFIG_NAMES = {
    "pytest.ini", "setup.cfg", "tox.ini", "conftest.py", "pyproject.toml", "noxfile.py",
    ".npmrc", ".pnpmfile.cjs", "pip.conf", ".pypirc", "makefile", "gnumakefile",
    "cmakelists.txt", "cargo.toml", "build.rs", "go.mod", "rakefile", "gemfile",
    ".gitattributes", ".gitmodules", ".editorconfig", ".envrc", ".env", "package.json",
    "tsconfig.json", "setup.py",
}
_CONFIG_PREFIXES = (".yarnrc", "jest.config.", "vite.config.", "webpack.config.", "babel.config.")
_HOOK_GIT = {"commit", "merge", "pull", "checkout", "rebase", "am", "push"}
_CLONE_VALUES = {
    "-b", "--branch", "-c", "--config", "--origin", "-o", "--depth", "--reference",
    "--template", "--separate-git-dir", "-j", "--jobs", "--filter", "--recurse-submodules",
    "--reference-if-able", "--server-option", "--initial-branch",
}
_COMMAND_KEY = re.compile(
    r"(?i)(?:^|\.)(?:command|fsmonitor|hookspath|editor|pager|askpass|sshcommand|program|helper|smudge|clean|textconv|cmd)$"
)


def _unique_paths(found: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen = set()
    for path in found:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _known_config(name: str) -> bool:
    low = (name or "").lower()
    if low in _CONFIG_NAMES or low.startswith(_CONFIG_PREFIXES):
        return True
    return False


def _dot_piece(name: str) -> bool:
    return bool(name) and name.startswith(".") and name not in {".", ".."}


def _home() -> Path:
    return Path.home()


def _plain_resolved(path: Path) -> Path | None:
    """The path in canonical form, or None when it cannot be resolved."""
    key = _canonical_key(path)
    if key is None:
        return None
    return Path(key)


def _scope_dirs(cwd: Path, roots: list[Path]) -> list[Path]:
    """Directories from the command's cwd up through the outermost workspace root that contains it."""
    safety = _safety()
    current = _plain_resolved(cwd)
    if current is None:
        try:
            current = cwd.resolve()
        except OSError:
            current = cwd
    containing: list[Path] = []
    for root in roots:
        resolved = _plain_resolved(root)
        if resolved is None:
            try:
                resolved = root.resolve()
            except OSError:
                resolved = root
        if current == resolved or resolved in current.parents or safety._inside(current, [resolved]):
            containing.append(resolved)
    stop = min(containing, key=lambda path: len(path.parts)) if containing else current
    chain = [current]
    while current != stop:
        parent = current.parent
        if parent == current:
            break
        current = parent
        chain.append(current)
    return chain


def _relative_key(path_key: str, root_key: str) -> str | None:
    """The path under root, or None when it is not under that directory."""
    if not path_key or not root_key or not _key_under(path_key, root_key):
        return None
    if path_key == root_key:
        return ""
    return path_key[len(root_key.rstrip("/")) + 1 :]


def _in_config_scope(path: Path, chain: list[Path], home: Path) -> bool:
    """A bot-written dotfile, a file inside a dot-directory, or a known tool config on the way up, or in $HOME."""
    raw = Path(_strip_extended(os.fspath(path)))
    sensitive = _dot_piece(raw.name) or _known_config(raw.name) or any(_dot_piece(part) for part in raw.parts)
    if not sensitive:
        return False
    canon = _canonical_key(path)
    if canon is None:
        return False
    home_key = _canonical_key(home)
    if home_key:
        relative = _relative_key(canon, home_key)
        if relative:
            first = relative.split("/", 1)[0]
            if _dot_piece(first) or ("/" not in relative and _known_config(first)):
                return True
    for directory in chain:
        dir_key = _canonical_key(directory)
        if not dir_key:
            continue
        relative = _relative_key(canon, dir_key)
        if not relative:
            continue
        first = relative.split("/", 1)[0]
        if "/" not in relative and (_dot_piece(first) or _known_config(first)):
            return True
        if _dot_piece(first):
            return True
    return False


def _tracked_configs(records: dict, cwd: Path, roots: list[Path]) -> list[Path]:
    chain = _scope_dirs(cwd, roots)
    home = _home()
    found: list[Path] = []
    for key in records:
        path = Path(str(key))
        if _in_config_scope(path, chain, home):
            found.append(path)
    return found


def _implicated(command: str, cwd: Path, roots: list[Path]) -> list[Path]:
    safety = _safety()
    prepared, status = safety.prepare_shell(command)
    if status == "ask":
        return []
    line = (prepared or "").strip()
    program, args, dynamic, _uncertain, _unwrapped = safety._parse_invocation(line)
    if dynamic or not program:
        return []
    base = safety._base_name(program)
    if re.fullmatch(r"python\d+(?:\.\d+)?", base):
        base = "python"
    found: list[Path] = []

    def add(token: str) -> None:
        raw = (token or "").strip().strip("'\"")
        if not raw or raw.startswith("-") or "://" in raw:
            return
        path = safety._resolve(raw, cwd)
        try:
            path = path.resolve()
        except OSError:
            pass
        suffix = path.suffix.lower()
        scriptish = suffix in _SCRIPT_SUFFIX or raw.startswith(("./", ".\\")) or "/" in raw or "\\" in raw
        if scriptish:
            found.append(path)

    add(program)
    if base in _INTERPRETERS or base in _SHELLS:
        for word in _positionals(args or ""):
            add(word)
            break
    names: list[str] = []
    if base in {"npm", "pnpm", "yarn", "npx"}:
        names.append("package.json")
    if base in {"make", "gmake"}:
        names.extend(["Makefile", "makefile", "GNUmakefile"])
        for match in re.finditer(r"(?i)(?:^|\s)(?:-f|--file|--makefile)(?:=|\s+)(\S+)", args or ""):
            add(match.group(1))
    if base in {"pytest", "py.test"} or re.search(r"(?i)\bpytest\b", line):
        names.extend(["conftest.py", "setup.py", "pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "noxfile.py"])
    if base in {"pip", "pip3"} or re.search(r"(?i)\bsetup\.py\b", line):
        names.extend(["setup.py", "pyproject.toml"])
    for name in names:
        found.append(cwd / name)
    if base in {"pytest", "py.test"}:
        current = cwd
        for _ in range(6):
            found.append(current / "conftest.py")
            parent = current.parent
            if parent == current or not safety._inside(parent, roots):
                break
            current = parent
    return _unique_paths(found)


def _module_of(args: str) -> str:
    words = _words(args)
    index = 0
    while index < len(words):
        word = words[index]
        low = word.lower()
        if low in {"-m", "--module"} and index + 1 < len(words):
            return words[index + 1]
        if low.startswith("--module="):
            return word.split("=", 1)[1]
        index += 1
    return ""


def _exec_id(command: str) -> str | None:
    """Tool id when this command can discover or run workspace code. None for a reader."""
    safety = _safety()
    prepared, status = safety.prepare_shell(command or "")
    line = (prepared or command or "").strip() if status != "ask" else (command or "").strip()
    program, args, dynamic, uncertain, _unwrapped = safety._parse_invocation(line)
    if not program:
        return "unknown" if (dynamic or uncertain) else None
    base = _program_id(program)
    if re.fullmatch(r"python\d+(?:\.\d+)?", base):
        base = "python"
    if base in _READ_ONLY or base in {"find", "fd"}:
        return None
    if base == "git":
        return None
    if base in {"docker", "podman", "docker-compose", "podman-compose"}:
        words = [item.lower() for item in _positionals(args or "")]
        head = words[0] if words else ""
        if head in _DOCKER_READ:
            return None
        if head == "compose":
            inner = words[1] if len(words) > 1 else ""
            if inner in {"ps", "version", "logs", "images", "config", "top"}:
                return None
        return base
    if base in {"pip", "pip3"}:
        if _first_command(args).lower() in {"list", "show", "freeze", "check", "debug", "index", ""}:
            return None
        return "pip"
    if base == "cargo":
        if (_first_command(args).lower() or "build") in {"metadata", "tree", "version"}:
            return None
        return "cargo"
    if base == "go":
        if _first_command(args).lower() in {"list", "env", "version", "doc", "mod", ""}:
            return None
        return "go"
    if base in {"npm", "yarn", "pnpm"}:
        if _first_command(args).lower() in {"ls", "list", "view", "outdated", "explain", "prefix", "bin", "root", "fund", "version", "audit"}:
            return None
        return base
    if base == "npx":
        return "npx"
    if base in {"pytest", "py.test"}:
        return "pytest"
    if base in _INTERPRETERS or base == "py":
        module = _module_of(args or "")
        if module in {"pytest", "py.test"}:
            return "pytest"
        if module == "unittest":
            return "unittest"
        return base
    if base in {"make", "gmake"}:
        return "make"
    if base in _SHELLS or base in {"cmake", "ninja", "meson", "tox", "nox", "poetry", "gradle", "mvn", "just", "task"}:
        return base
    if dynamic or uncertain:
        return "unknown"
    return base


_GRANT_SCRIPTED = {
    "npm", "yarn", "pnpm", "npx", "cargo", "go", "pip",
    "docker", "podman", "docker-compose", "podman-compose",
}
_GRANT_PLAIN = _INTERPRETERS | {
    "pytest", "unittest", "make",
    "cmake", "ninja", "meson", "tox", "nox", "poetry", "gradle", "mvn", "just", "task",
}


def _known_tool(stem: str) -> bool:
    """A program we name. A reader stem is not enough: ls.ps1 is a script, not ls."""
    if not stem or stem in _READ_ONLY:
        return False
    if re.fullmatch(r"python\d+(?:\.\d+)?", stem):
        return True
    return stem in _INTERPRETERS | _SHELLS | _WRAPPERS | _GRANT_SCRIPTED | _GRANT_PLAIN | {
        "git", "pytest", "py.test", "make", "gmake", "pip", "pip3",
    }


def _program_id(program: str) -> str:
    """Tool name. Case, a full path, and .exe are already folded. .cmd, .bat, and .ps1 fold when the stem is that tool."""
    base = _safety()._base_name(program)
    for ext in (".cmd", ".bat", ".ps1"):
        if not base.endswith(ext):
            continue
        stem = base[: -len(ext)]
        if _known_tool(stem):
            return stem
        return base
    return base


def _grant_tool(command: str) -> str | None:
    """Persistent-allow id. None for a reader, a shell, or a program we do not name.

    pytest and python -m pytest share pytest. npm test stays npm test, so it does
    not cover npm ci or npm run build.
    """
    tool = _exec_id(command)
    if not tool or tool == "unknown" or tool in _SHELLS:
        return None
    if tool in _GRANT_SCRIPTED:
        safety = _safety()
        prepared, status = safety.prepare_shell(command or "")
        line = (prepared or command or "").strip() if status != "ask" else (command or "").strip()
        _program, args, _dynamic, _uncertain, _unwrapped = safety._parse_invocation(line)
        words = _positionals(args or "")
        if not words:
            return tool
        head = words[0]
        if head.lower() == "run" and len(words) > 1 and tool in {"npm", "yarn", "pnpm", "npx"}:
            return f"{tool} run {words[1]}"
        if head.lower() == "compose" and len(words) > 1 and tool in {"docker", "podman"}:
            return f"{tool} compose {words[1]}"
        return f"{tool} {head}"
    if tool in _GRANT_PLAIN:
        return tool
    return None


def _review_paths(command: str, cwd: Path, roots: list[Path], records: dict) -> list[Path]:
    found: list[Path] = []
    for directory in _reached_dirs(command, cwd):
        found.extend(_implicated(command, directory, roots))
        found.extend(_tracked_configs(records, directory, roots))
        found.extend(_records_under(records, directory / ".git"))
    for site in _sites(command, cwd):
        directory = site.get("cwd") or cwd
        text = site.get("text") or command
        found.extend(_implicated(text, directory, roots))
        if _exec_id(text):
            found.extend(_records_under(records, directory))
            work = site.get("work_tree")
            if work is not None:
                found.extend(_records_under(records, work))
    return _unique_paths(found)


def _diff_text(name: str, before: str, after: str) -> str:
    diff = "".join(
        difflib.unified_diff(
            (before or "").splitlines(True),
            (after or "").splitlines(True),
            fromfile=name,
            tofile=name,
            n=2,
        )
    )
    if not diff:
        diff = f"--- {name}\n+++ {name}\n" + "\n".join("+ " + line for line in (after or "").splitlines()[:80])
    lines = diff.splitlines()
    if len(lines) > 120:
        diff = "\n".join(lines[:120]) + "\n…"
    return diff


def _spell_from_entries(text: str) -> str | None:
    """Rebuild an absolute POSIX path from directory entries.

    ``realpath`` on macOS keeps the case you passed in. APFS still opens
    ``makefile`` when the entry is ``Makefile``. The walk prefers an exact
    entry and otherwise the one ``casefold`` match.
    """
    if not text.startswith("/"):
        return None
    current = Path("/")
    built: list[str] = []
    for part in text.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if not built:
                return None
            built.pop()
            current = Path("/" + "/".join(built)) if built else Path("/")
            continue
        try:
            names = os.listdir(current)
        except OSError:
            return None
        chosen = None
        folded = part.casefold()
        for name in names:
            if name == part:
                chosen = name
                break
            if chosen is None and name.casefold() == folded:
                chosen = name
        if chosen is None:
            return None
        built.append(chosen)
        current = current / chosen
    if not built:
        return "/"
    return "/" + "/".join(built)


def _on_disk_text(path: Path) -> str:
    """Forward slashes and the directory entry's spelling.

    Windows ``resolve`` returns the final path in the entry's case. macOS does
    not, so the entry walk supplies it. This string is for the card. Matching
    stays on the canonical key.
    """
    raw = _strip_extended(os.fspath(path))
    try:
        text = _strip_extended(os.fspath(Path(raw).resolve())).replace("\\", "/")
    except (OSError, ValueError, RuntimeError):
        text = raw.replace("\\", "/")
    if os.name == "nt":
        return text
    if sys.platform == "darwin" or not Path(text).exists():
        spelled = _spell_from_entries(text)
        if spelled:
            return spelled
    return text


def _short_name(path: Path, cwd: Path) -> str:
    """The card path: on-disk spelling, relative to the cwd, forward slashes.

    The canonical key decides whether the file is inside the cwd, and how many
    trailing parts that relative path has. Those parts are taken from the
    directory entry, so a folded key cannot print ``makefile`` for ``Makefile``.
    A file outside the cwd stays the full path. The last name alone is not a path.
    """
    shown = _on_disk_text(path)
    file_key = _canonical_key(path) or _fold_text(os.fspath(path))
    root_key = _canonical_key(cwd) or _fold_text(os.fspath(cwd))
    if file_key and root_key and _key_under(file_key, root_key) and file_key != root_key:
        extra = file_key[len(root_key):].lstrip("/")
        count = len([part for part in extra.split("/") if part])
        parts = [part for part in shown.split("/") if part and part != "."]
        if count and len(parts) >= count:
            return "/".join(parts[-count:])
    return shown


def _change_summary(items: list[tuple[Path, str, str]], cwd: Path) -> str:
    """Count, names, and a diff for each small file."""
    names = [_short_name(path, cwd) for path, _before, _after in items[:40]]
    extra = len(items) - len(names)
    line = f"{len(items)} file" + ("s" if len(items) != 1 else "") + ": " + ", ".join(names)
    if extra > 0:
        line += f", and {extra} more"
    parts = [line]
    for path, before, after in items[:40]:
        if len(after) <= 4000 and after.count("\n") <= 80:
            parts.append(_diff_text(_short_name(path, cwd), before, after[:20000]))
        else:
            parts.append(f"{_short_name(path, cwd)} changed ({len(after)} bytes)")
    return "\n".join(parts)


# Process memory only. A restart drops it. It is never written to the bot record.
_EXEC_SESSION: dict[str, list[dict]] = {}


def _under(path: Path, root: Path) -> bool:
    """True only when both paths resolve and one is inside the other. A miss is not a cover."""
    path_key = _canonical_key(path)
    root_key = _canonical_key(root)
    if path_key is None or root_key is None:
        return False
    return _key_under(path_key, root_key)


def _always_covers(bot: dict, command: str, cwd: Path, path: Path) -> bool:
    """A saved grant matches this bot's tool and this directory, and nothing else."""
    grants = [item for item in ((bot or {}).get("safety_exec_always") or []) if isinstance(item, dict)]
    if not grants:
        return False
    for site in _sites(command, cwd):
        tool = _grant_tool(site.get("text") or "")
        if not tool:
            continue
        for grant in grants:
            if str(grant.get("tool") or "") != tool:
                continue
            root = Path(str(grant.get("root") or ""))
            if str(grant.get("root") or "") and _under(site.get("cwd") or cwd, root) and _under(path, root):
                return True
    return False


def _session_covers(bot_id: str, command: str, cwd: Path, path: Path) -> bool:
    grants = _EXEC_SESSION.get(bot_id or "") or []
    if not grants:
        return False
    for site in _sites(command, cwd):
        tool = _exec_id(site.get("text") or "")
        if not tool:
            continue
        for grant in grants:
            if grant.get("tool") != tool:
                continue
            root = Path(str(grant.get("root") or ""))
            if _under(site.get("cwd") or cwd, root) and _under(path, root):
                return True
    return False


def allow_exec_session(bot_id: str, command: str, cwd: Path) -> bool:
    """Let this exec tool keep running under this directory until the process restarts.

    The grant is not stored on the bot. It does not cover another tool, or a file
    outside this directory.
    """
    if not bot_id:
        return False
    grants = _EXEC_SESSION.setdefault(bot_id, [])
    stored = False
    for site in _sites(command, cwd):
        tool = _exec_id(site.get("text") or "")
        if not tool:
            continue
        root = _path_key_of(site.get("cwd") or cwd)
        if not any(item.get("tool") == tool and item.get("root") == root for item in grants):
            grants.append({"tool": tool, "root": root})
        stored = True
    return stored


def clear_exec_session(bot_id: str | None = None) -> None:
    """Drop session grants. A restart does this by leaving the dict behind in the old process."""
    if bot_id:
        _EXEC_SESSION.pop(bot_id, None)
    else:
        _EXEC_SESSION.clear()


def allow_exec_always(store, bot_id: str, command: str, cwd: Path) -> bool:
    """Save Always allow for this one tool under each directory the command runs in.

    Only the approval path calls this. A chain with two different tools saves nothing.
    A later command is still judged on its own, including a block or an off-limits path.
    """
    from easyagent.store import atomic_write_json, new_id

    if not bot_id or store is None:
        return False
    specs: list[tuple[str, str]] = []
    for site in _sites(command, cwd):
        tool = _grant_tool(site.get("text") or "")
        if not tool:
            continue
        root = _path_key_of(site.get("cwd") or cwd)
        if (tool, root) not in specs:
            specs.append((tool, root))
    if len({tool for tool, _root in specs}) != 1:
        return False
    bot, _records_map = _records(store, bot_id)
    grants = [item for item in (bot.get("safety_exec_always") or []) if isinstance(item, dict)]
    for tool, root in specs:
        if any(str(item.get("tool") or "") == tool and str(item.get("root") or "") == root for item in grants):
            continue
        name = Path(root).name or root
        label = f"{tool} in {name}"
        if any(str(item.get("label") or "") == label for item in grants):
            label = f"{tool} in {root}"
        grants.append({"id": new_id(), "tool": tool, "root": root, "label": label})
    bot["safety_exec_always"] = grants[-100:]
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)
    return True


def remove_exec_always(store, bot_id: str, grant_id: str) -> dict:
    """Drop one saved tool rule. Settings calls this. It does not change the mode."""
    from easyagent.store import atomic_write_json

    bot, _records_map = _records(store, bot_id)
    key = str(grant_id or "").strip()
    bot["safety_exec_always"] = [
        item
        for item in (bot.get("safety_exec_always") or [])
        if isinstance(item, dict) and str(item.get("id") or "") != key
    ]
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)
    return bot


def _exec_command(sites: list[dict]) -> bool:
    return any(_exec_id(site.get("text") or "") for site in sites)


def _always_hint(tool: str, folder: str) -> str:
    return (
        f"Always allow: {tool} will run in {folder} for this bot without asking again, "
        "even after the bot changes files there. Drive wipes and protected folders stay blocked. "
        "You can remove this in Settings."
    )


def _grant_folder(sites: list[dict], tool: str, cwd: Path) -> str:
    names: list[str] = []
    for site in sites:
        if _grant_tool(site.get("text") or "") != tool:
            continue
        directory = site.get("cwd") or cwd
        name = Path(directory).name or str(directory)
        if name not in names:
            names.append(name)
    if names:
        return " and ".join(names)
    return Path(cwd).name or str(cwd)


# How many times an exec command actually consulted bot-write records. Tests spy on this.
_RECORDS_CHECK_COUNT = 0


def records_check_count() -> int:
    return _RECORDS_CHECK_COUNT


def reset_records_checks() -> None:
    global _RECORDS_CHECK_COUNT
    _RECORDS_CHECK_COUNT = 0


def _bump_records_check() -> None:
    global _RECORDS_CHECK_COUNT
    _RECORDS_CHECK_COUNT += 1


def _unchecked(command: str):
    """An exec tool whose records check could not run. It waits."""
    return _ask_rule(command, "bot-changed", "That command's files could not be checked. It waits for you.")


def script_review(store, bot_id: str, command: str, cwd: Path, roots: list[Path]):
    """Ask when a script, dotfile, tool config, or file an exec tool can discover is still unapproved."""
    from easyagent import turn as turn_mod

    sites = _sites(command, cwd)
    exec_here = _exec_command(sites)
    if exec_here:
        _bump_records_check()
    for site in sites:
        if site.get("dir_unknown"):
            return _ask_rule(command, "bot-changed", "That command's directory could not be read. It waits for you.")
        if exec_here and _exec_id(site.get("text") or "") and _canonical_key(site.get("cwd") or cwd) is None:
            return _ask_rule(command, "bot-changed", "That command's directory could not be read. It waits for you.")
    _bot, records = _records(store, bot_id)
    if not records:
        return None
    index, failed = _index_records(records)
    if failed and exec_here:
        return _ask_rule(command, "bot-changed", "That command's directory could not be read. It waits for you.")
    pending: list[tuple[Path, str, str]] = []
    paths: list[str] = []
    for path in _review_paths(command, cwd, roots, records):
        key = _canonical_key(path)
        if key is None:
            if exec_here:
                return _ask_rule(command, "bot-changed", "That command's directory could not be read. It waits for you.")
            continue
        rec = index.get(key)
        if not isinstance(rec, dict):
            continue
        current = _file_sha(path)
        bot_sha = str(rec.get("bot_sha") or "")
        approved = str(rec.get("approved") or "")
        if not current:
            if exec_here and path.exists():
                return _ask_rule(command, "bot-changed", "That command's files could not be checked. It waits for you.")
            continue
        if current != bot_sha or current == approved:
            continue
        if _session_covers(bot_id, command, cwd, path) or _always_covers(_bot, command, cwd, path):
            continue
        try:
            after = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            after = ""
        pending.append((path, str(rec.get("before") or ""), after[:20000]))
        paths.append(key)
    if not paths:
        return None
    diff = _change_summary(pending, cwd)
    detail = (command or "").strip() + "\n\nThis bot changed a file this command can run since the last approval.\n\n" + diff
    grant_tools: list[str] = []
    for site in sites:
        tool = _grant_tool(site.get("text") or "")
        if tool and tool not in grant_tools:
            grant_tools.append(tool)
    session = exec_here
    proposal = {
        "kind": "script-diff",
        "diff": diff,
        "paths": paths,
        "session": session,
        "exec_always": len(grant_tools) == 1,
    }
    if len(grant_tools) == 1:
        proposal["hint"] = _always_hint(grant_tools[0], _grant_folder(sites, grant_tools[0], cwd))
    slot = turn_mod.current_slot()
    if slot is not None:
        slot.safety_script_proposal = proposal
    return _ask_rule(detail, "bot-changed", "This bot changed a file this command can run since you last approved it. It waits for you.")


def _git_words(command: str) -> tuple[str, list[str]] | None:
    """Return the git subcommand and the words after it, or None when this is not a git command."""
    safety = _safety()
    prepared, status = safety.prepare_shell(command)
    if status == "ask":
        return None
    line = (prepared or "").strip()
    program, args, dynamic, uncertain, _unwrapped = safety._parse_invocation(line)
    if dynamic or uncertain or not program or safety._base_name(program) != "git":
        return None
    words = _words(args or "")
    index = 0
    while index < len(words):
        word = words[index]
        low = word.lower()
        if word == "-C" or low in {"--git-dir", "--work-tree", "--namespace"}:
            index += 2
            continue
        if low.startswith("-"):
            return None
        return low, words[index + 1:]
    return None


def _git_start(command: str, cwd: Path) -> Path:
    safety = _safety()
    prepared, status = safety.prepare_shell(command)
    line = (prepared or command or "").strip() if status != "ask" else (command or "").strip()
    program, args, dynamic, uncertain, _unwrapped = safety._parse_invocation(line)
    if dynamic or uncertain or not program or safety._base_name(program) != "git":
        return cwd
    words = _words(args or "")
    base = cwd
    index = 0
    while index < len(words):
        word = words[index]
        if word == "-C" and index + 1 < len(words):
            raw = words[index + 1].strip("'\"")
            base = Path(raw) if Path(raw).is_absolute() else cwd / raw
            index += 2
            continue
        break
    return base


def _resolve_git_dir(path: Path) -> Path:
    if path.is_dir():
        return path
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path
    for line in text.splitlines():
        if line.lower().startswith("gitdir:"):
            raw = line.split(":", 1)[1].strip()
            other = Path(raw)
            if not other.is_absolute():
                other = (path.parent / other)
            try:
                return other.resolve()
            except OSError:
                return other
    return path


def _worktree_of(start: Path) -> tuple[Path, Path] | None:
    try:
        current = start.resolve()
    except OSError:
        current = start
    for _ in range(16):
        git_path = current / ".git"
        if git_path.exists():
            return current, _resolve_git_dir(git_path)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _section_name(header: str) -> str:
    body = header.strip()
    if body.startswith("[") and "]" in body:
        body = body[1:body.index("]")]
    body = body.strip()
    match = re.match(r'(?i)([A-Za-z0-9-]+)\s+"([^"]*)"', body)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return body.replace(" ", "")


def _command_config_entries(text: str) -> list[tuple[str, str]]:
    """Keys whose values git runs: fsmonitor, hooksPath, *command, alias !sh, and the same family."""
    section = ""
    found: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and "]" in line:
            section = _section_name(line)
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key or not value:
            continue
        full = f"{section}.{key}" if section else key
        if _config_runs(full, value):
            found.append((full, value))
    return found


def _config_runs(full: str, value: str) -> bool:
    """True when this git config key can run a program or redirect hooks."""
    if re.match(r"(?i)alias\.", full) and (value or "").lstrip().startswith("!"):
        return True
    if re.match(r"(?i)filter\.", full):
        return True
    if re.match(r"(?i)pager\.", full):
        return True
    if re.match(r"(?i)diff\.(?:.+\.)?command$", full):
        return True
    if re.match(r"(?i)merge\.(?:.+\.)?driver$", full):
        return True
    if re.fullmatch(r"(?i)interactive\.difffilter", full):
        return True
    if re.match(r"(?i)core\..*command$", full):
        return True
    return _COMMAND_KEY.search(full) is not None


def _dirty_hooks(hooks: Path) -> list[Path]:
    if not hooks.is_dir():
        return []
    found: list[Path] = []
    try:
        children = list(hooks.iterdir())
    except OSError:
        return [hooks]
    for child in children:
        if child.name.endswith(".sample"):
            continue
        found.append(child)
    return found


def _git_fingerprint(git_dir: Path) -> str:
    chunks: list[str] = []
    for child in _dirty_hooks(git_dir / "hooks"):
        if child.is_file():
            chunks.append(child.name + ":" + _file_sha(child))
        else:
            chunks.append(child.name + ":other")
    try:
        text = (git_dir / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for key, value in _command_config_entries(text):
        chunks.append(f"{key}={value}")
    return hashlib.sha256("\n".join(chunks).encode("utf-8", errors="replace")).hexdigest()


def _git_evidence(git_dir: Path) -> str:
    parts: list[str] = []
    for child in _dirty_hooks(git_dir / "hooks"):
        try:
            body = child.read_text(encoding="utf-8", errors="replace")[:4000] if child.is_file() else "(not a file)"
        except OSError:
            body = ""
        parts.append(_diff_text(str(Path(".git") / "hooks" / child.name), "", body))
    try:
        text = (git_dir / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    entries = _command_config_entries(text)
    if entries:
        shown = "\n".join(f"{key} = {value}" for key, value in entries)
        parts.append(_diff_text(".git/config", "", shown))
    return "\n".join(parts)


def note_session_git(store, bot_id: str, worktree: Path) -> None:
    """Remember a work tree whose .git this bot created with git init or git clone."""
    from easyagent.store import atomic_write_json

    if not bot_id:
        return
    bot, _records_map = _records(store, bot_id)
    try:
        key = str(worktree.resolve())
    except OSError:
        key = str(worktree)
    repos = [str(item) for item in (bot.get("safety_git_repos") or []) if item]
    if key not in repos:
        repos.append(key)
    bot["safety_git_repos"] = repos[-100:]
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)


def _init_clone_worktree(command: str, cwd: Path) -> Path | None:
    parsed = _git_words(command)
    if parsed is None:
        return None
    sub, rest = parsed
    if sub not in {"init", "clone"}:
        return None
    base = _git_start(command, cwd)
    positionals: list[str] = []
    index = 0
    while index < len(rest):
        word = rest[index]
        name = word.lower().split("=", 1)[0]
        if name in _CLONE_VALUES:
            index += 1 if "=" in word else 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        positionals.append(word.strip("'\""))
        index += 1
    if sub == "init":
        raw = positionals[0] if positionals else "."
        return Path(raw) if Path(raw).is_absolute() else base / raw
    if not positionals:
        return base
    if len(positionals) >= 2:
        raw = positionals[-1]
        return Path(raw) if Path(raw).is_absolute() else base / raw
    url = positionals[0].rstrip("/")
    name = url.split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if not name or name in {".", ".."}:
        return base
    return base / name


def remember_git_command(store, bot_id: str, command: str, cwd: Path) -> None:
    """After git init or git clone runs, the repo counts as created by this bot."""
    if not bot_id:
        return
    target = _init_clone_worktree(command, cwd)
    if target is None:
        safety = _safety()
        view = safety._command_view(command)
        if view.inner and not view.dynamic:
            target = _init_clone_worktree(view.inner, cwd)
    if target is None:
        return
    note_session_git(store, bot_id, target)


def _session_git(bot: dict, worktree: Path, git_dir: Path) -> bool:
    repos = {str(item) for item in (bot.get("safety_git_repos") or []) if item}
    if not repos:
        return False
    keys = []
    for path in (worktree, git_dir):
        keys.append(str(path))
        try:
            keys.append(str(path.resolve()))
        except OSError:
            pass
    return any(key in repos for key in keys)


_CD_NAMES = {"cd", "pushd", "set-location", "push-location", "sl", "chdir"}
_CD_VALUE_FLAGS = {"-path", "-literalpath", "-stackname"}
_CD_SWITCHES = {"/d"}


def _path_token(token: str) -> bool:
    """True when a flag value is a filesystem path rather than a version or a name."""
    text = (token or "").strip().strip("'\"")
    if not text:
        return False
    if text.startswith(("./", ".\\", "../", "..\\", "/", "\\", "~")):
        return True
    if re.match(r"(?i)^[A-Za-z]:[/\\]", text):
        return True
    return "/" in text or "\\" in text


# One class of directory change. dir is a directory, parent is a file's folder,
# python is an interpreter path, and the git kinds locate a repository.
_CWD_SPECS: dict[str, dict] = {
    "env": {"shorts": {"C": "dir"}, "longs": {"--chdir": "dir"}, "stop": True, "chain": False},
    "make": {"shorts": {"C": "dir"}, "longs": {"--directory": "dir"}, "stop": False, "chain": True},
    "gmake": {"shorts": {"C": "dir"}, "longs": {"--directory": "dir"}, "stop": False, "chain": True},
    "git": {
        "shorts": {"C": "dir"},
        "longs": {"--git-dir": "git-dir", "--work-tree": "work-tree"},
        "stop": True,
        "chain": True,
    },
    "npm": {"shorts": {"C": "dir"}, "longs": {"--prefix": "dir"}, "stop": False, "chain": False},
    "npx": {"shorts": {"C": "dir"}, "longs": {"--prefix": "dir"}, "stop": False, "chain": False},
    "yarn": {"shorts": {}, "longs": {"--cwd": "dir"}, "stop": False, "chain": False},
    "pnpm": {"shorts": {"C": "dir"}, "longs": {"--dir": "dir"}, "stop": False, "chain": False},
    "cargo": {"shorts": {}, "longs": {"--manifest-path": "parent"}, "stop": False, "chain": False},
    "go": {"shorts": {"C": "dir"}, "longs": {}, "stop": False, "chain": False},
    "cmake": {"shorts": {"S": "dir", "B": "dir"}, "longs": {}, "stop": False, "chain": False},
    "ninja": {"shorts": {"C": "dir"}, "longs": {}, "stop": False, "chain": True},
    "meson": {"shorts": {}, "longs": {"--builddir": "dir"}, "stop": False, "chain": False},
    "docker": {"shorts": {"f": "parent"}, "longs": {"--file": "parent"}, "stop": False, "chain": False},
    "podman": {"shorts": {"f": "parent"}, "longs": {"--file": "parent"}, "stop": False, "chain": False},
    "docker-compose": {"shorts": {"f": "parent"}, "longs": {"--file": "parent"}, "stop": False, "chain": False},
    "podman-compose": {"shorts": {"f": "parent"}, "longs": {"--file": "parent"}, "stop": False, "chain": False},
    "task": {"shorts": {"d": "dir"}, "longs": {"--dir": "dir"}, "stop": False, "chain": False},
    "just": {"shorts": {}, "longs": {"--justfile": "parent"}, "stop": False, "chain": False},
    "tox": {"shorts": {"c": "parent"}, "longs": {"--conf": "parent"}, "stop": False, "chain": False},
    "poetry": {"shorts": {"C": "dir"}, "longs": {"--directory": "dir"}, "stop": False, "chain": False},
    "pip": {"shorts": {}, "longs": {"--python": "python"}, "stop": False, "chain": False},
    "pip3": {"shorts": {}, "longs": {"--python": "python"}, "stop": False, "chain": False},
}


def _flag_pairs(words: list[str], shorts: dict[str, str], longs: dict[str, str], stop: bool) -> tuple[list[tuple[str, str]], bool]:
    """Known flag values. Short options may carry the value (`-Cdir`) or take the next word. `--flag=value` and `--flag value` both count."""
    found: list[tuple[str, str]] = []
    unknown = False
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            break
        if word.startswith("--"):
            name, eq, attached = word.partition("=")
            kind = longs.get(name.lower())
            if kind is None:
                index += 1
                continue
            if eq:
                found.append((kind, attached))
                index += 1
                continue
            if index + 1 >= len(words):
                unknown = True
                break
            found.append((kind, words[index + 1]))
            index += 2
            continue
        if word.startswith("-") and not word.startswith("--") and word != "-":
            body = word[1:]
            matched = False
            for pos, letter in enumerate(body):
                kind = shorts.get(letter)
                if kind is None:
                    continue
                rest = body[pos + 1:]
                if rest.startswith("="):
                    rest = rest[1:]
                if rest:
                    found.append((kind, rest))
                    index += 1
                elif index + 1 >= len(words):
                    unknown = True
                    index += 1
                else:
                    found.append((kind, words[index + 1]))
                    index += 2
                matched = True
                break
            if not matched:
                index += 1
            continue
        if stop:
            break
        index += 1
    return found, unknown


def _program_dirs(base: str, args: str, cwd: Path) -> tuple[list[Path], Path | None, Path | None, bool]:
    """Directories this program named, plus a git dir and work tree when the flags say so.

    An empty directory list means the command did not name a directory. The last
    bool is true when a named path could not be read.
    """
    spec = _CWD_SPECS.get(base or "")
    if spec is None:
        return [], None, None, False
    pairs, unknown = _flag_pairs(_words(args), spec["shorts"], spec["longs"], spec["stop"])
    cursor = cwd
    cwds: list[Path] = []
    git_dir = None
    work_tree = None
    chain = bool(spec["chain"])
    for kind, raw in pairs:
        if kind == "python" and not _path_token(raw):
            continue
        origin = cursor if chain and kind in {"dir", "git-dir", "work-tree"} else cwd
        paths, missed = _expand_dirs(raw, origin)
        if missed or not paths:
            unknown = True
            continue
        if kind == "git-dir":
            git_dir = paths[0]
            continue
        if kind == "work-tree":
            work_tree = paths[0]
            continue
        if kind in {"parent", "python"}:
            paths = [path.parent if path.name else path for path in paths]
        if kind == "dir" and chain:
            cursor = paths[0]
        cwds.extend(paths)
    return cwds, git_dir, work_tree, unknown


def _expand_dirs(token: str, cwd: Path) -> tuple[list[Path], bool]:
    """Directories a cd, -C, or GIT_DIR value names. A glob with no match is unknown."""
    import glob as globmod

    raw = (token or "").strip().strip("'\"")
    if not raw or raw in {".", ""}:
        return [cwd], False
    if raw == "-":
        return [], True
    globbing = any(ch in raw for ch in "*?[")
    if globbing:
        pattern = raw if Path(raw).is_absolute() else str(cwd / raw)
        matches = [Path(item) for item in globmod.glob(pattern)]
        if not matches:
            return [], True
        return matches, False
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / raw
    return [path], False


def _peel_locators(piece: str) -> tuple[dict[str, str], str]:
    rest = (piece or "").strip()
    if rest.lower().startswith("env ") and not rest[4:].lstrip().startswith("-"):
        rest = rest[4:].lstrip()
    found: dict[str, str] = {}
    while True:
        match = _BASH_ASSIGN.match(rest)
        if not match:
            break
        name = match.group(1)
        if name.upper() not in {"GIT_DIR", "GIT_WORK_TREE"}:
            break
        value = match.group(2)
        if value is None:
            value = match.group(3) if match.group(3) is not None else match.group(4) or ""
        found[name.upper()] = value
        rest = rest[match.end():].strip()
    return found, rest


def _piece_program(piece: str) -> tuple[str, str, bool]:
    safety = _safety()
    prepared, status = safety.prepare_shell(piece)
    line = (prepared or piece or "").strip() if status != "ask" else (piece or "").strip()
    program, args, dynamic, uncertain, _unwrapped = safety._parse_invocation(line)
    if not program:
        return "", args or "", True
    return _program_id(program), args or "", bool(dynamic or uncertain)


def _cd_targets(base: str, args: str, cwd: Path) -> tuple[list[Path], bool]:
    words = _words(args)
    positionals: list[str] = []
    saw_switch = False
    index = 0
    while index < len(words):
        word = words[index]
        low = word.lower()
        if low in _CD_SWITCHES:
            saw_switch = True
            index += 1
            continue
        if "=" in word or (low.startswith("-") and ":" in word[1:]):
            name, _sep, value = word.partition("=") if "=" in word else word.partition(":")
            if name.lower() in _CD_VALUE_FLAGS and value:
                positionals.append(value)
                index += 1
                continue
        if low in _CD_VALUE_FLAGS and index + 1 < len(words):
            positionals.append(words[index + 1])
            index += 2
            continue
        if low in {"-p", "-l", "--"} or low.startswith("-"):
            index += 1
            continue
        positionals.append(word)
        index += 1
    if not positionals:
        if saw_switch:
            return [], True
        if base in {"cd", "set-location", "sl", "chdir"}:
            return [_home()], False
        return [], True
    found, unknown = _expand_dirs(positionals[0], cwd)
    return found, unknown


def _sites(command: str, cwd: Path, depth: int = 0) -> list[dict]:
    """Each command after cd, pushd, Set-Location, Push-Location, and any cwd flag."""
    if depth > 6:
        return [{"text": command, "cwd": cwd, "git_dir": None, "work_tree": None, "unknown": True}]
    safety = _safety()
    pieces = safety._split_chain(command or "")
    sites: list[dict] = []
    current = cwd
    unknown = False
    stack: list[Path] = []
    index = 0
    while index < len(pieces):
        piece = pieces[index]
        locators, body = _peel_locators(piece)
        git_dir = None
        work_tree = None
        if "GIT_DIR" in locators:
            paths, missed = _expand_dirs(locators["GIT_DIR"], current)
            git_dir = paths[0] if paths else None
            unknown = unknown or missed or git_dir is None
        if "GIT_WORK_TREE" in locators:
            paths, missed = _expand_dirs(locators["GIT_WORK_TREE"], current)
            work_tree = paths[0] if paths else None
            unknown = unknown or missed or work_tree is None
        if not body:
            index += 1
            continue
        base, args, dynamic = _piece_program(body)
        if base in _WRAPPERS:
            view = safety._command_view(body)
            wrap_dirs, wrap_git, wrap_work, wrap_unknown = _program_dirs(base, args, current)
            if wrap_git is not None:
                git_dir = wrap_git
            if wrap_work is not None:
                work_tree = wrap_work
            if view.inner and not view.dynamic:
                targets = wrap_dirs or [current]
                for target in targets:
                    for site in _sites(view.inner, target, depth + 1):
                        if git_dir is not None and site.get("git_dir") is None:
                            site["git_dir"] = git_dir
                        if work_tree is not None and site.get("work_tree") is None:
                            site["work_tree"] = work_tree
                        if unknown or wrap_unknown:
                            site["unknown"] = True
                        if wrap_unknown:
                            site["dir_unknown"] = True
                        sites.append(site)
                index += 1
                continue
        if base in _CD_NAMES:
            targets, missed = _cd_targets(base, args, current)
            if missed or not targets or dynamic:
                unknown = True
                index += 1
                continue
            if len(targets) > 1:
                rest = " ; ".join(pieces[index + 1 :])
                for target in targets:
                    sites.extend(_sites(rest, target, depth + 1))
                return sites
            stack.append(current)
            current = targets[0]
            unknown = False
            index += 1
            continue
        if base == "popd":
            if stack:
                current = stack.pop()
                unknown = False
            else:
                unknown = True
            index += 1
            continue
        cwds, flag_git, flag_work, flag_unknown = _program_dirs(base, args, current)
        if flag_git is not None:
            git_dir = flag_git
        if flag_work is not None:
            work_tree = flag_work
        starts = cwds or [current]
        for start in starts:
            sites.append({
                "text": body,
                "cwd": start,
                "git_dir": git_dir,
                "work_tree": work_tree,
                "unknown": unknown or flag_unknown or dynamic,
                "dir_unknown": flag_unknown,
            })
        index += 1
    return sites


def _reached_dirs(command: str, cwd: Path) -> list[Path]:
    found = [cwd]
    for site in _sites(command, cwd):
        found.append(site["cwd"])
        if site.get("work_tree") is not None:
            found.append(site["work_tree"])
        git_dir = site.get("git_dir")
        if git_dir is not None:
            found.append(git_dir.parent if git_dir.name == ".git" else git_dir)
    return _unique_paths(found)


def _records_under(records: dict, root: Path) -> list[Path]:
    """Records whose canonical path is inside root. An unresolvable key is omitted here.

    script_review asks for an exec tool when any record key cannot be resolved, so a
    miss does not become an allow.
    """
    anchor = _canonical_key(root)
    if anchor is None:
        return []
    found: list[Path] = []
    for key in records:
        canon = _canonical_key(key)
        if canon is None or not _key_under(canon, anchor):
            continue
        found.append(Path(_strip_extended(str(key))))
    return found


def _repo_of(site: dict) -> tuple[Path, Path] | None:
    git_dir = site.get("git_dir")
    if git_dir is not None:
        if git_dir.name != ".git" and (git_dir / ".git").exists():
            git_dir = git_dir / ".git"
        resolved = _resolve_git_dir(git_dir)
        work = site.get("work_tree") or (resolved.parent if resolved.name == ".git" else site["cwd"])
        return work, resolved
    start = site.get("work_tree") or site["cwd"]
    return _worktree_of(start)


def _inside_roots(path: Path, roots: list[Path]) -> bool:
    return _safety()._inside(path, roots)


def _path_key_of(path: Path) -> str:
    key = _canonical_key(path)
    if key:
        return key
    return _fold_text(os.fspath(path))


def _repo_predates(git_dir: Path, bot: dict) -> bool:
    from datetime import datetime

    raw = str(bot.get("safety_first_turn") or bot.get("created_at") or "")
    if not raw:
        return False
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    times: list[float] = []
    for path in (git_dir, git_dir / "config", git_dir / "HEAD"):
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            continue
    if not times:
        return False
    return min(times) + 1 < stamp.timestamp()


def _unapproved_git(records: dict, git_dir: Path) -> list[tuple[Path, dict]]:
    found: list[tuple[Path, dict]] = []
    index, _failed = _index_records(records)
    for path in _records_under(records, git_dir):
        key = _canonical_key(path)
        rec = index.get(key or "")
        if not isinstance(rec, dict):
            continue
        current = _file_sha(path)
        if current and current == str(rec.get("bot_sha") or "") and current != str(rec.get("approved") or ""):
            found.append((path, rec))
    return found


def _note_existing_repo(store, bot_id: str, work_key: str) -> None:
    """One line the first time a pre-existing checkout has hooks this bot did not write."""
    from easyagent.store import atomic_write_json, new_id, now_iso

    bot, _records_map = _records(store, bot_id)
    allows = {str(item) for item in (bot.get("safety_git_allows") or []) if item}
    if work_key in allows:
        return
    noted = dict(bot.get("safety_git_noted") or {}) if isinstance(bot.get("safety_git_noted"), dict) else {}
    if work_key in noted:
        return
    noted[work_key] = True
    bot["safety_git_noted"] = noted
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)
    notice = "This git repo already had hooks or a command in its config before this bot's first turn, and this bot did not change them, so the command ran."
    try:
        chat = store.ongoing_chat(bot_id)
    except Exception:
        return
    with store._lock:
        chat = store.get_chat(bot_id, chat["id"])
        messages = list(chat.get("messages") or [])
        if any(isinstance(item, dict) and item.get("kind") == "safety-git-note" and item.get("git_repo") == work_key for item in messages):
            return
        messages.append({
            "id": new_id(),
            "role": "assistant",
            "kind": "safety-git-note",
            "git_repo": work_key,
            "content": notice,
            "created_at": now_iso(),
        })
        chat["messages"] = messages
        chat["updated_at"] = now_iso()
        atomic_write_json(store._chat_path(chat["bot_id"], chat["id"]), chat)


def allow_git_repo(store, bot_id: str, repo: str) -> dict:
    """Remember that this checkout's existing hooks may run."""
    from easyagent.store import atomic_write_json

    bot, _records_map = _records(store, bot_id)
    allows = [str(item) for item in (bot.get("safety_git_allows") or []) if item]
    key = str(repo or "").strip()
    noted = bot.get("safety_git_noted") if isinstance(bot.get("safety_git_noted"), dict) else {}
    if key and key not in allows and key not in noted:
        from easyagent.store import StoreError

        raise StoreError("Only a repo this bot was shown a note for can be allowed. Nothing was changed.", 409)
    if key and key not in allows:
        allows.append(key)
    bot["safety_git_allows"] = allows[-100:]
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)
    return bot


def git_repo_review(store, bot_id: str, command: str, cwd: Path, roots: list[Path] | None = None):
    """Ask when a reached git repo has a real hook or a command in config.

    Any .git under the workspace counts, not only one this bot created with git init or clone.
    Directory changes are resolved first, including cd /d, env -C, make -C, and the other cwd flags. A checkout that already
    existed before this bot's first turn, and that this bot did not change, runs with one note.
    """
    from easyagent import turn as turn_mod

    roots = list(roots or [])
    bot, records = _records(store, bot_id)
    allows = {str(item) for item in (bot.get("safety_git_allows") or []) if item}
    for site in _sites(command, cwd):
        parsed = _git_words(site["text"])
        if parsed is None:
            if site.get("unknown") and re.search(r"(?i)\bgit\b", site["text"] or ""):
                return _ask_rule(command, "git-hooks", "That git command's directory could not be read. It waits for you.")
            continue
        sub, _rest = parsed
        if sub not in _HOOK_GIT and sub not in _GIT_SAFE:
            continue
        if site.get("unknown"):
            return _ask_rule(command, "git-hooks", "That git command's directory could not be read. It waits for you.")
        found = _repo_of(site)
        if found is None:
            continue
        worktree, git_dir = found
        in_workspace = _inside_roots(worktree, roots) or _inside_roots(git_dir, roots)
        if not in_workspace and not _session_git(bot, worktree, git_dir):
            continue
        hooks = _dirty_hooks(git_dir / "hooks")
        try:
            config_text = (git_dir / "config").read_text(encoding="utf-8", errors="replace")
        except OSError:
            config_text = ""
        commands = _command_config_entries(config_text)
        if not hooks and not commands:
            continue
        work_key = _path_key_of(worktree)
        pending = _unapproved_git(records, git_dir)
        if pending:
            parts = []
            paths = []
            for path, rec in pending:
                try:
                    after = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    after = ""
                parts.append(_diff_text(str(path.name), str(rec.get("before") or ""), after[:20000]))
                paths.append(_path_key_of(path))
            diff = "\n".join(parts)
            detail = (command or "").strip() + "\n\nThis bot changed a git hook or git config.\n\n" + diff
            slot = turn_mod.current_slot()
            if slot is not None:
                slot.safety_script_proposal = {"kind": "script-diff", "diff": diff, "paths": paths}
            return _ask_rule(detail, "bot-changed", "This bot changed a git hook or git config. It waits for you.")
        if _repo_predates(git_dir, bot):
            if work_key not in allows:
                _note_existing_repo(store, bot_id, work_key)
            continue
        fingerprint = _git_fingerprint(git_dir)
        approved = bot.get("safety_git_ok") if isinstance(bot.get("safety_git_ok"), dict) else {}
        if str(approved.get(work_key) or "") == fingerprint:
            continue
        evidence = _git_evidence(git_dir)
        detail = (command or "").strip() + "\n\nThis git repo has a hook or a command in its config.\n\n" + evidence
        slot = turn_mod.current_slot()
        if slot is not None:
            slot.safety_script_proposal = {"kind": "script-diff", "diff": evidence, "paths": [work_key]}
        return _ask_rule(detail, "git-hooks", "This git repo has a hook or a command in its config. It waits for you.")
    return None


def _mark_git_ok(bot: dict, command: str, cwd: Path) -> bool:
    approved = dict(bot.get("safety_git_ok") or {}) if isinstance(bot.get("safety_git_ok"), dict) else {}
    changed = False
    for site in _sites(command, cwd):
        found = _repo_of(site)
        if found is None:
            continue
        worktree, git_dir = found
        approved[_path_key_of(worktree)] = _git_fingerprint(git_dir)
        changed = True
    if changed:
        bot["safety_git_ok"] = approved
    return changed


def stamp_scripts(store, bot_id: str, command: str, cwd: Path, roots: list[Path]) -> None:
    """The user approved this generation of the implicated files, and this repo's hooks and config."""
    from easyagent.store import atomic_write_json

    bot, records = _records(store, bot_id)
    changed = False
    if records:
        index, _failed = _index_records(records)
        for path in _review_paths(command, cwd, roots, records):
            key = _canonical_key(path)
            rec = index.get(key or "")
            if not isinstance(rec, dict) or not path.is_file():
                continue
            current = _file_sha(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError:
                text = ""
            rec["approved"] = current
            rec["bot_sha"] = current
            rec["before"] = text
            _rekey_record(records, path, rec)
            changed = True
        if changed:
            bot["safety_scripts"] = records
    if _mark_git_ok(bot, command, cwd):
        changed = True
    if changed:
        atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)


def _needs_records(command: str, cwd: Path) -> bool:
    """A named exec tool. An arbitrary program such as rm is not one."""
    try:
        sites = _sites(command, cwd)
    except OSError:
        return True
    for site in sites:
        tool = _exec_id(site.get("text") or "")
        if tool and tool != "unknown" and (tool in _GRANT_SCRIPTED or tool in _GRANT_PLAIN):
            return True
    return False


def _with_script(verdict, command: str, cwd, roots, *, required: bool = False):
    if verdict.tier != _safety().ALLOW:
        return verdict
    from easyagent import turn as turn_mod

    slot = turn_mod.current_slot()
    store = getattr(slot, "store", None) if slot is not None else None
    bot_id = getattr(slot, "bot_id", None) if slot is not None else None
    directory = Path(cwd)
    if store is None or not bot_id:
        if required and _needs_records(command, directory):
            _bump_records_check()
            return _unchecked(command)
        return verdict
    try:
        hit = script_review(store, bot_id, command, directory, roots)
    except OSError:
        if required:
            return _unchecked(command)
        return verdict
    if hit is not None:
        return hit
    try:
        hit = git_repo_review(store, bot_id, command, directory, roots)
    except OSError:
        if required and _needs_records(command, directory):
            return _unchecked(command)
        return verdict
    return hit if hit is not None else verdict


def apply(command: str, verdict, *, cwd: Path, roots: list[Path], created: set[str], mode: str, denied, remote: bool, depth: int):
    """Raise an allow to ask, or a protected write to block. A block stays a block."""
    safety = _safety()
    if mode != "strict" or verdict.tier == safety.BLOCK:
        return verdict
    prepared, status = safety.prepare_shell(command)
    if status == "ask":
        return verdict
    line = (prepared or "").strip()
    if not line:
        return verdict
    if safety._BENIGN_EVAL.match(line):
        return verdict
    sub_hit = _substitution_decision(command, cwd, roots, remote, created, mode, denied, depth)
    prepared_hit = _substitution_decision(line, cwd, roots, remote, created, mode, denied, depth) if line != (command or "").strip() else None
    for candidate in (sub_hit, prepared_hit):
        if candidate is not None and candidate.tier == safety.BLOCK:
            return candidate
    if sub_hit is None:
        sub_hit = prepared_hit
    env_hit, rewritten = _env_policy(command, cwd, roots, remote, created, mode, denied, depth)
    if env_hit is not None and env_hit.tier == safety.BLOCK:
        return env_hit
    if rewritten is not None:
        return _judge_text(rewritten, cwd, roots, remote, created, mode, denied, depth)
    if env_hit is not None:
        return env_hit
    if sub_hit is not None:
        return sub_hit
    hit = _protected(line, cwd, roots)
    if hit is not None and hit.tier == safety.BLOCK:
        return hit
    if verdict.tier != safety.ALLOW:
        if verdict.tier == safety.ASK:
            allow = safety._verdict(safety.ALLOW, "listed", "", line, safety._fp("cmd", line))
            reviewed = _with_script(allow, line, cwd, roots)
            if reviewed.tier == safety.ASK and reviewed.rule in {"bot-changed", "git-hooks"}:
                return reviewed
        return safety._worst(verdict, hit) if hit is not None else verdict
    if hit is not None:
        return hit
    program, args, dynamic, uncertain, _unwrapped = safety._parse_invocation(line)
    if dynamic or uncertain or not program:
        reviewed = _with_script(verdict, line, cwd, roots)
        if reviewed.tier == safety.ASK and reviewed.rule in {"bot-changed", "git-hooks"}:
            return reviewed
        return _ask(line)
    view = safety._command_view(line)
    base = _program_id(program)
    if re.fullmatch(r"python\d+(?:\.\d+)?", base):
        base = "python"
    if view.inner and not view.dynamic and base in _WRAPPERS:
        targets, _git_dir, _work_tree, dir_unknown = _program_dirs(base, args or "", cwd)
        if dir_unknown:
            return _ask_rule(line, "bot-changed", "That command's directory could not be read. It waits for you.")
        for target in targets or [cwd]:
            reviewed = _with_script(verdict, view.inner, target, roots, required=True)
            if reviewed.tier != safety.ALLOW:
                return reviewed
        return verdict
    flagged = _closed_flags(base, args or "", line, cwd, roots, remote, created, mode, denied, depth)
    if flagged is not None and flagged.tier == safety.BLOCK:
        return flagged
    decided = _tool_decision(
        base, args or "", program, line, cwd, roots, remote, created, mode, denied, depth
    )
    if decided is None:
        if flagged is not None:
            return flagged
        return _with_script(verdict, line, cwd, roots, required=True)
    if decided.tier != safety.BLOCK and flagged is not None and flagged.tier == safety.BLOCK:
        return flagged
    if decided.tier == safety.ASK:
        allow = safety._verdict(safety.ALLOW, "listed", "", line, safety._fp("cmd", line))
        reviewed = _with_script(allow, line, cwd, roots)
        if reviewed.tier == safety.ASK and reviewed.rule in {"bot-changed", "git-hooks"}:
            return reviewed
    return decided
