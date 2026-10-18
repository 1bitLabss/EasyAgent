"""Paginated reads, patch, and search_files.

The matching, the line-numbered window, the unchanged-then-blocked repeat,
and the ripgrep-or-walk search follow the Hermes file tools. Paths still go
through EasyAgent's own folder and secret checks before any of this runs.
"""

from __future__ import annotations

import difflib
import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

from easyagent.filestate import content_hash
from easyagent.fuzzy import find_closest_lines, format_no_match_hint, fuzzy_find_and_replace
from easyagent.linting import check_lint_delta
from easyagent.truncate import truncate_head_tail

DEFAULT_READ_LIMIT = 2000
CODING_READ_CHARS = 60_000
_LARGE_REWRITE = 20_000
_REWRITE_RATIO = 0.80
_BLOCKED_DEVICES = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr",
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".ea-undo", ".ea-spill",
})
SEARCH_SECONDS = 20.0
SEARCH_MAX_FILES = 4000
SEARCH_MAX_BYTES = 1_000_000
_SYSTEM_ROOTS = ("/proc", "/sys", "/dev")
_READ_DEDUP_MESSAGE = (
    "File unchanged since last read. The content from "
    "the earlier read_file result in this conversation is "
    "still current — refer to that instead of re-reading."
)
_PATCH_MISS_HINT = (
    "Stop retrying with variations of the same old_string. "
    "Either: (1) re-read the file fresh to verify current content, "
    "(2) use a longer / more unique old_string with surrounding context lines, "
    "or (3) re-read the file in full, then use write_file to replace it if the "
    "targeted region is hard to anchor."
)

_dedup_lock = threading.Lock()
_dedup_hits: dict[tuple, int] = {}
_dedup_seen: dict[tuple, tuple] = {}
_patch_misses: dict[tuple, int] = {}


def clear_trackers() -> None:
    with _dedup_lock:
        _dedup_hits.clear()
        _dedup_seen.clear()
        _patch_misses.clear()


def looks_like_numbered_read(content: str) -> bool:
    """True when the body is read_file's ``N|text`` display, not file bytes."""
    if not isinstance(content, str):
        return False
    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    numbered: list[int] = []
    for line in lines:
        prefix, sep, _rest = line.lstrip().partition("|")
        if sep and prefix.isdigit():
            numbered.append(int(prefix))
    if len(numbered) < 2 or len(numbered) / len(lines) < 0.6:
        return False
    pairs = sum(1 for prev, current in zip(numbered, numbered[1:]) if current == prev + 1)
    return pairs >= len(numbered) - 1


def is_internal_display(content: str) -> bool:
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if stripped and _READ_DEDUP_MESSAGE in stripped and len(stripped) <= 2 * len(_READ_DEDUP_MESSAGE):
        return True
    return looks_like_numbered_read(content)


def blocked_device(path: Path) -> bool:
    text = str(path)
    if text in _BLOCKED_DEVICES:
        return True
    try:
        if not path.exists() or path.is_symlink():
            return False
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)


def _numbered(lines: list[str], offset: int) -> str:
    return "\n".join(f"{offset + index}|{line}" for index, line in enumerate(lines))


def _fit_budget(numbered: str, max_chars: int) -> tuple[str, int, bool]:
    if len(numbered) <= max_chars:
        count = numbered.count("\n") + 1 if numbered else 0
        return numbered, count, False
    lines = numbered.split("\n")
    kept: list[str] = []
    running = 0
    for line in lines:
        addition = len(line) + (1 if kept else 0)
        if running + addition > max_chars:
            break
        kept.append(line)
        running += addition
    if not kept:
        kept.append(lines[0][:max_chars])
    return "\n".join(kept), len(kept), True


def similar_names(path: Path) -> str:
    parent = path.parent
    if not parent.is_dir():
        return ""
    try:
        names = [child.name for child in parent.iterdir() if not child.name.startswith(".")]
    except OSError:
        return ""
    close = difflib.get_close_matches(path.name, names, n=3, cutoff=0.6)
    if not close:
        return ""
    return "Similar filenames: " + ", ".join(close)


def _coerce_window_int(value, label: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    text = str(value).strip().strip('"').strip("'")
    try:
        number = int(text)
    except (TypeError, ValueError):
        try:
            number = float(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be an integer") from exc
        if number != int(number):
            raise ValueError(f"{label} must be an integer")
        number = int(number)
    return number


def read_window(path: Path, *, offset: int, limit: int, task_id: str) -> tuple[str, bool]:
    """Line-numbered window. The bool is True when the window is not the whole file.

    offset is 1-based. A repeat of the same window on an unchanged file returns
    a short stub, then a BLOCKED line on the third identical read.
    A numeric string such as ``"200"`` is an integer.
    """
    offset = _coerce_window_int(offset, "offset")
    limit = _coerce_window_int(limit, "limit")
    if offset < 1:
        raise ValueError("offset starts at 1")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if blocked_device(path) or str(path) in _BLOCKED_DEVICES:
        raise ValueError(f"Cannot read '{path}': this is a device file that would block or produce infinite output.")
    if not path.exists() or not path.is_file():
        hint = similar_names(path)
        message = f"Could not read {path}: that file is not there."
        if hint:
            message = f"{message} {hint}"
        raise FileNotFoundError(message)
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"Could not read {path}. {exc}") from exc
    version = (stat.st_mtime_ns, stat.st_size)
    key = (task_id, str(path), int(offset), int(limit))
    with _dedup_lock:
        previous = _dedup_seen.get(key)
        if previous == version:
            hits = _dedup_hits.get(key, 0) + 1
            _dedup_hits[key] = hits
            if hits >= 2:
                return (
                    "BLOCKED: You have called read_file on this "
                    f"exact region {hits + 1} times and the file "
                    "has NOT changed. STOP calling read_file for "
                    "this path — the content from your earlier "
                    "read_file result in this conversation is "
                    "still current. Proceed with your task using "
                    "the information you already have.",
                    True,
                )
            return _READ_DEDUP_MESSAGE, True
        _dedup_hits.pop(key, None)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"Could not read {path}. {exc}") from exc
    lines = text.splitlines()
    total = len(lines)
    start = offset - 1
    if start >= total and total:
        raise ValueError(f"{path} has {total} lines. offset {offset} is past the end.")
    window = lines[start:start + limit]
    numbered = _numbered(window, offset)
    kept, kept_count, truncated = _fit_budget(numbered, CODING_READ_CHARS)
    end_line = offset + kept_count - 1
    partial = offset > 1 or end_line < total or truncated
    footer = f"lines {offset}-{end_line} of {total}"
    if truncated or end_line < total:
        footer += f". next_offset={end_line + 1}"
    body = kept + ("\n" + footer if kept else footer)
    with _dedup_lock:
        _dedup_seen[key] = version
        _dedup_hits[key] = 0
    return body, partial


def rewrite_hint(old: str, new: str) -> str:
    if not (_LARGE_REWRITE <= len(new) <= 400_000):
        return ""
    if not (_LARGE_REWRITE <= len(old) <= 400_000):
        return ""
    from collections import Counter

    old_lines, new_lines = old.splitlines(), new.splitlines()
    if not old_lines:
        return ""
    unchanged = sum((Counter(old_lines) & Counter(new_lines)).values())
    ratio = unchanged / max(len(old_lines), len(new_lines))
    if ratio < _REWRITE_RATIO:
        return ""
    changed = max(len(old_lines), len(new_lines)) - unchanged
    return (
        f"{unchanged:,} of {len(new_lines):,} lines were already on disk ({ratio:.0%} unchanged); ~{changed:,} "
        f"line(s) actually changed. Re-sending a {len(new):,}-char file costs output tokens for every "
        "unchanged line; for edits like this use patch (old_string/new_string), which sends only the changed region."
    )


def unified_diff(path: Path, before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=str(path),
        tofile=str(path),
        n=3,
    )
    text = "".join(diff)
    if text and not text.endswith("\n"):
        text += "\n"
    return text or "(no textual diff)\n"


def apply_patch(path: Path, old_string: str, new_string: str, *, replace_all: bool, task_id: str) -> tuple[str, str]:
    """Replace old_string. Returns the diff, the strategy, and a lint note."""
    if is_internal_display(new_string) or is_internal_display(old_string):
        raise ValueError(
            "Refusing to write internal read_file display text as file content. "
            "Strip read_file line-number prefixes or reconstruct the intended file contents before writing."
        )
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"Failed to read file: {path}: {exc}") from exc
    new_content, match_count, strategy, error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all
    )
    key = (task_id, str(path))
    if error or match_count == 0:
        with _dedup_lock:
            misses = _patch_misses.get(key, 0) + 1
            _patch_misses[key] = misses
        message = error or "Could not find a match for old_string in the file"
        hint = format_no_match_hint(message if message.startswith("Could not find") else "Could not find", 0, old_string, content)
        if not hint and "Could not find" in message:
            close = find_closest_lines(old_string, content)
            if close:
                hint = "\n\nDid you mean one of these sections?\n" + close
        if misses >= 3:
            hint = (hint or "") + f"\n\nThis is failure #{misses} patching {path!r}. " + _PATCH_MISS_HINT
        raise ValueError((message + hint).strip())
    with _dedup_lock:
        _patch_misses.pop(key, None)
    diff = unified_diff(path, content, new_content)
    lint = check_lint_delta(str(path), content, new_content)
    lines = [
        f"Patched {path} ({strategy}, {match_count} match).",
        diff.rstrip(),
        "verified: true",
    ]
    if lint:
        lines.append(lint)
    report = "\n".join(line for line in lines if line)
    return report, new_content  # type: ignore[return-value]


def shell_read_target(command: str) -> tuple[str, bool] | None:
    """A successful read-only shell of one file: (path, partial).

    cat, head, tail, less, more, sed -n, type, and Get-Content. A pipe,
    a redirect, or a second command is not a read.
    """
    text = (command or "").strip()
    if not text or re.search(r"[|&;<>`$()]", text):
        return None
    try:
        parts = shlex.split(text, posix=os.name != "nt")
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    program = parts[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    lowered = program.lower()
    args = parts[1:]
    if lowered in {"cat", "type", "less", "more", "get-content"}:
        operands = [item for item in args if not item.startswith("-")]
        if len(operands) != 1 or len(args) != len(operands):
            return None
        return operands[0], False
    if lowered in {"head", "tail"}:
        operands = [item for item in args if not item.startswith("-")]
        flags = [item for item in args if item.startswith("-")]
        if flags and any(not re.fullmatch(r"-[a-zA-Z]*\d*", item) and not re.fullmatch(r"-n", item) for item in flags):
            # -n 20 is two tokens. Allow a numeric token after -n.
            pass
        cleaned: list[str] = []
        skip = False
        for item in args:
            if skip:
                skip = False
                continue
            if item in {"-n", "-c", "-q"}:
                skip = True
                continue
            if item.startswith("-"):
                continue
            cleaned.append(item)
        if len(cleaned) != 1:
            return None
        return cleaned[0], True
    if lowered == "sed":
        if len(args) < 2 or args[0] != "-n":
            return None
        operands = [item for item in args[1:] if not item.startswith("-")]
        if len(operands) != 2:
            return None
        return operands[-1], True
    return None


def _skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.endswith(".egg-info")


def search_root_error(root: Path) -> str | None:
    """Refuse a filesystem root or a virtual system tree. Other folders are capped."""
    try:
        resolved = root.resolve()
    except OSError:
        return "Could not search that folder."
    text = str(resolved).replace("\\", "/").rstrip("/") or "/"
    if text in {"/", ""} or re.fullmatch(r"[A-Za-z]:", text):
        return "That search starts at the filesystem root. It was not run."
    lowered = text.lower()
    for prefix in _SYSTEM_ROOTS:
        if lowered == prefix or lowered.startswith(prefix + "/"):
            return "That search is inside a system folder. It was not run."
    return None


def _within(root: Path, path: Path) -> bool:
    """True when path is root or a file inside it.

    Case and a ``\\\\?\\`` prefix are the same file on Windows. A relative
    comparison that keeps the caller's case drops that file.
    """
    from easyagent.strictshell import _canonical_key, _fold_text, _key_under

    try:
        path_key = _canonical_key(path) or _fold_text(os.fspath(path))
        root_key = _canonical_key(root) or _fold_text(os.fspath(root))
    except (OSError, ValueError, RuntimeError):
        return False
    return _key_under(path_key, root_key)


def _system_dir(path: Path) -> bool:
    try:
        text = str(path.resolve()).replace("\\", "/").rstrip("/").lower()
    except OSError:
        text = str(path).replace("\\", "/").rstrip("/").lower()
    return any(text == prefix or text.startswith(prefix + "/") for prefix in _SYSTEM_ROOTS)


def _whole_file_secret(path: Path) -> bool:
    """The shell grep gate reads the whole file. The first 8 KB is not enough."""
    from easyagent.scriptgate import _stream_secret

    return _stream_secret(path)


def _shown_match(path: Path, number: int, line: str) -> str:
    """Scrub the full line, then clip it. Clipping first can cut a token in half."""
    from easyagent.journal import redact_leaks

    text = redact_leaks(line or "").replace("\r", "").rstrip("\n")
    return f"{path}:{number}:{text[:400]}"


def _skip_secret(path: Path, skip) -> bool:
    from easyagent.safety import secret_file

    if secret_file(path) or _system_dir(path) or _whole_file_secret(path):
        return True
    if skip is not None:
        try:
            if skip(path):
                return True
        except Exception:
            return True
    return False


def _python_search(root: Path, pattern: str, file_glob: str, limit: int, skip) -> tuple[list[str], str]:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"That pattern is not a regular expression. {exc}") from exc
    matches: list[str] = []
    started = time.monotonic()
    examined = 0
    note = ""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if time.monotonic() - started >= SEARCH_SECONDS:
            note = "Search stopped after 20 seconds."
            break
        here = Path(dirpath)
        if _system_dir(here) or not _within(root, here):
            dirnames[:] = []
            continue
        kept_dirs = []
        for name in dirnames:
            if _skip_dir(name):
                continue
            child = here / name
            if child.is_symlink() or _system_dir(child) or not _within(root, child):
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in filenames:
            if time.monotonic() - started >= SEARCH_SECONDS:
                note = "Search stopped after 20 seconds."
                break
            examined += 1
            if examined > SEARCH_MAX_FILES:
                note = f"Search stopped after {SEARCH_MAX_FILES} files."
                break
            if file_glob and not Path(name).match(file_glob):
                continue
            file_path = here / name
            if file_path.is_symlink() or not _within(root, file_path) or _skip_secret(file_path, skip):
                continue
            try:
                if file_path.stat().st_size > SEARCH_MAX_BYTES:
                    continue
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(_shown_match(file_path, number, line))
                    if len(matches) >= limit:
                        return matches, note
        if note:
            break
    return matches, note


_RG_SECRET_GLOBS = (
    "!id_rsa", "!id_dsa", "!id_ed25519", "!id_ecdsa",
    "!id_rsa.pub", "!id_dsa.pub", "!id_ed25519.pub", "!id_ecdsa.pub",
    "!.env", "!.env.*", "!.npmrc", "!.pypirc", "!.netrc", "!.pgpass",
    "!.git-credentials", "!.token", "!.ssh", "!.gnupg", "!.aws",
    "!*.pem", "!*.key", "!*.p12", "!*.pfx", "!*.kdbx", "!*.jks", "!*.token",
    "!credentials*", "!credential.json", "!creds*", "!token*", "!*token*",
    "!secrets.json", "!secret.json", "!kubeconfig", "!secrets.db",
)


def _content_secret_globs(root: Path) -> list[str]:
    """Relative rg excludes for bland names whose bytes are a key or a token.

    Name-classified files are already excluded and are not opened here.
    """
    from easyagent.safety import _name_secret

    found: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not _skip_dir(name)]
        here = Path(dirpath)
        if _system_dir(here) or not _within(root, here):
            dirnames[:] = []
            continue
        for name in filenames:
            seen += 1
            if seen > SEARCH_MAX_FILES:
                return found
            path = here / name
            if path.is_symlink() or not _within(root, path):
                continue
            try:
                if _name_secret(path):
                    continue
            except Exception:
                continue
            if _whole_file_secret(path):
                try:
                    rel = path.resolve().relative_to(root.resolve()).as_posix()
                except (OSError, ValueError):
                    rel = name
                found.append("!" + rel)
    return found


def _rg_search(root: Path, pattern: str, file_glob: str, limit: int, skip) -> list[str] | None:
    rg = shutil.which("rg")
    if not rg:
        return None
    command = [
        rg, "--json", "--line-number", "--no-heading", "--color", "never",
        "--hidden", "--max-columns", "400", "--max-columns-preview",
        "--max-filesize", f"{SEARCH_MAX_BYTES}",
        "-g", "!.git", "-g", "!node_modules", "-g", "!venv",
        "-g", "!.venv", "-g", "!__pycache__",
        "-g", "!.ea-undo", "-g", "!.ea-spill",
    ]
    for glob in (*_RG_SECRET_GLOBS, *_content_secret_globs(root)):
        command.extend(["-g", glob])
    if file_glob:
        command.extend(["-g", file_glob])
    command.extend(["--", pattern, str(root)])
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in {0, 1}:
        return None
    import json

    matches: list[str] = []
    for line in (proc.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data") or {}
        path_text = ((data.get("path") or {}).get("text")) or ""
        line_number = data.get("line_number") or 0
        line_text = ((data.get("lines") or {}).get("text")) or ""
        if not path_text:
            continue
        file_path = Path(path_text)
        if not file_path.is_absolute():
            file_path = root / file_path
        if file_path.is_symlink() or not _within(root, file_path) or _skip_secret(file_path, skip):
            continue
        matches.append(_shown_match(file_path, int(line_number or 0), line_text))
        if len(matches) >= limit:
            break
    return matches


def search_tree(root: Path, pattern: str, *, file_glob: str = "", limit: int = 50, skip=None) -> str:
    """file:line:text. rg when it is installed, otherwise a Python walk.

    Secret files are never opened and their lines are never returned. The
    Python walk stops after SEARCH_SECONDS or SEARCH_MAX_FILES. A filesystem
    root is refused. Symlinks are not followed.
    """
    if not pattern:
        raise ValueError("Name a pattern to search for.")
    refused = search_root_error(root)
    if refused:
        raise ValueError(refused)
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    found = _rg_search(root, pattern, file_glob, limit, skip)
    engine = "rg"
    note = ""
    if found is None:
        found, note = _python_search(root, pattern, file_glob, limit, skip)
        engine = "python"
    if not found:
        body = f"No matches for {pattern} under {root} ({engine})."
    else:
        body = "\n".join(found)
        body += f"\n{len(found)} match(es) via {engine}."
        if len(found) >= limit:
            body += " Results truncated. Narrow the pattern or the folder."
    if note:
        body += "\n" + note
    from easyagent.journal import redact_leaks

    return truncate_head_tail(redact_leaks(body), 8000, label="SEARCH")


def verified_line(path: Path) -> str:
    return f"verified: true\ncontent_sha256: {content_hash(str(path))}"
