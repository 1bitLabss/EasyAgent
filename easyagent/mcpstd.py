"""Small local connectors. Each one is a stdio MCP server with a fixed tool list.

Filesystem, git, and SQLite stay inside the folder or file they were given.
Fetch only uses http and https. Paths resolve against that folder, not the
process working directory.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

from easyagent.mcpframe import encode, read_message

_READ_CAP = 12000


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("usage: python -m easyagent.mcpstd filesystem|fetch|git|sqlite ...", file=sys.stderr)
        return
    kind = args[0]
    root = ""
    index = 1
    while index < len(args):
        if args[index] == "--root" and index + 1 < len(args):
            root = args[index + 1]
            index += 2
            continue
        if args[index] == "--file" and index + 1 < len(args):
            root = args[index + 1]
            index += 2
            continue
        index += 1
    serve(kind, root)


def serve(kind: str, root: str) -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            message = read_message(stdin)
        except Exception as exc:
            _write(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
            return
        if message is None:
            return
        reply = handle(kind, root, message)
        if reply is not None:
            _write(stdout, reply)


def handle(kind: str, root: str, message: dict) -> dict | None:
    method = str(message.get("method") or "")
    msg_id = message.get("id")
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _ok(msg_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"easyagent-{kind}", "version": "0.3.12"},
        })
    if method == "tools/list":
        return _ok(msg_id, {"tools": _tools(kind)})
    if method == "tools/call":
        params = message.get("params") or {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            text = _call(kind, root, name, arguments)
        except Exception as exc:
            return _ok(msg_id, {"content": [{"type": "text", "text": str(exc)[:500]}], "isError": True})
        return _ok(msg_id, {"content": [{"type": "text", "text": text[:_READ_CAP]}]})
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Unknown method"}}


def _tools(kind: str) -> list[dict]:
    if kind == "filesystem":
        return [
            _tool("list_directory", "List one folder inside the allowed root.", {"path": {"type": "string"}}),
            _tool("read_file", "Read a text file inside the allowed root.", {"path": {"type": "string"}}),
            _tool("write_file", "Write a text file inside the allowed root.", {"path": {"type": "string"}, "text": {"type": "string"}}),
            _tool("delete_file", "Delete one file inside the allowed root. It does not delete a folder.", {"path": {"type": "string"}}),
        ]
    if kind == "fetch":
        return [_tool("fetch_url", "GET a public http or https page and return the text.", {"url": {"type": "string"}})]
    if kind == "git":
        return [
            _tool("git_status", "Show git status for the allowed repository.", {}),
            _tool("git_diff", "Show the git diff for the allowed repository.", {}),
            _tool("git_log", "Show recent commits in the allowed repository.", {}),
            _tool("git_commit", "Commit the current changes in the allowed repository.", {"message": {"type": "string"}}),
        ]
    if kind == "sqlite":
        return [
            _tool("read_query", "Run one SELECT, WITH, PRAGMA, or EXPLAIN query.", {"sql": {"type": "string"}}),
            _tool("write_query", "Run one INSERT, UPDATE, or DELETE.", {"sql": {"type": "string"}}),
        ]
    return []


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties},
    }


def _call(kind: str, root: str, name: str, arguments: dict) -> str:
    if kind == "filesystem":
        return _files(root, name, arguments)
    if kind == "fetch":
        return _fetch(str(arguments.get("url") or ""))
    if kind == "git":
        return _git(root, name, arguments)
    if kind == "sqlite":
        return _sql(root, name, arguments)
    raise RuntimeError("That connector is not one of the starters.")


def _inside(root: str, raw: str) -> Path:
    """The path after env vars, ``..``, 8.3 names, and symlinks are resolved.

    The root is the folder this server was given. The process working directory
    is not used.
    """
    from easyagent.psast import resolve_path_token

    base = Path(root).expanduser()
    try:
        base = base.resolve()
    except OSError:
        pass
    if not base.is_dir():
        raise RuntimeError("The folder for this connector is missing.")
    target = resolve_path_token(raw or ".", base)
    if base != target and base not in target.parents:
        raise RuntimeError("That path is outside the folder this connector is allowed to use.")
    return target


def _files(root: str, name: str, arguments: dict) -> str:
    target = _inside(root, str(arguments.get("path") or "."))
    if name == "list_directory":
        if not target.is_dir():
            raise RuntimeError("That is not a folder.")
        names = []
        for child in sorted(target.iterdir(), key=lambda item: item.name.lower())[:200]:
            names.append(child.name + ("/" if child.is_dir() else ""))
        return "\n".join(names) or "(empty)"
    if name == "read_file":
        if not target.is_file():
            raise RuntimeError("That file is not there.")
        return target.read_text(encoding="utf-8", errors="replace")[:_READ_CAP]
    if name == "write_file":
        if target.exists() and target.is_dir():
            raise RuntimeError("That path is a folder.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(arguments.get("text") or ""), encoding="utf-8")
        return f"Wrote {target.name}."
    if name == "delete_file":
        if not target.is_file():
            raise RuntimeError("That is not a file, so it was not deleted.")
        target.unlink()
        return f"Deleted {target.name}."
    raise RuntimeError("That filesystem tool does not exist.")


def _fetch(url: str) -> str:
    parsed = urllib.request.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("Fetch only opens http and https pages.")
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "EasyAgent"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read(100_000)
    return data.decode("utf-8", "replace")


def _git(root: str, name: str, arguments: dict) -> str:
    folder = Path(root).expanduser()
    try:
        folder = folder.resolve()
    except OSError:
        pass
    if not (folder / ".git").exists():
        raise RuntimeError("That folder is not a git repository.")
    if name == "git_status":
        argv = ["git", "status", "--short"]
    elif name == "git_diff":
        argv = ["git", "diff", "--stat"]
    elif name == "git_log":
        argv = ["git", "log", "-n", "10", "--oneline"]
    elif name == "git_commit":
        message = " ".join(str(arguments.get("message") or "").split())
        if not message:
            raise RuntimeError("A commit needs a message.")
        argv = ["git", "commit", "-m", message]
    else:
        raise RuntimeError("That git tool does not exist.")
    done = subprocess.run(argv, cwd=str(folder), capture_output=True, text=True, timeout=20, check=False)
    text = (done.stdout or done.stderr or "").strip()
    return text[:_READ_CAP] or "(no output)"


def _sql(file_path: str, name: str, arguments: dict) -> str:
    path = Path(file_path).expanduser()
    try:
        path = path.resolve()
    except OSError:
        pass
    sql = str(arguments.get("sql") or "").strip()
    if not sql or ";" in sql.rstrip(";"):
        raise RuntimeError("Send one statement.")
    first = sql.split(None, 1)[0].lower()
    if name == "read_query":
        if first not in {"select", "with", "pragma", "explain"}:
            raise RuntimeError("read_query only runs a SELECT, WITH, PRAGMA, or EXPLAIN.")
    elif name == "write_query":
        if first not in {"insert", "update", "delete"}:
            raise RuntimeError("write_query only runs INSERT, UPDATE, or DELETE.")
    else:
        raise RuntimeError("That SQLite tool does not exist.")
    if name == "read_query" and not path.is_file():
        raise RuntimeError("That database file is not there.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        cursor = db.execute(sql)
        if name == "read_query":
            rows = cursor.fetchmany(50)
            return json.dumps(rows, default=str)[:_READ_CAP]
        db.commit()
        return f"Changed {cursor.rowcount} row."


def _ok(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _write(stream, payload: dict) -> None:
    stream.write(encode(payload))
    stream.flush()


if __name__ == "__main__":
    main()
