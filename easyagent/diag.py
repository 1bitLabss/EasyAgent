"""Client disconnect notes and a diagnostics zip. Nothing here is sent anywhere."""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

_MAX_LOG = 1_000_000
_LAST: dict[str, float] = {}


def _logs(root: Path) -> Path:
    folder = Path(root) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def append_client_event(root: Path, body: dict) -> None:
    kind = " ".join(str(body.get("kind") or "note").split())[:80] or "note"
    now = time.time()
    if now - _LAST.get(kind, 0.0) < 5:
        return
    _LAST[kind] = now
    detail = " ".join(str(body.get("detail") or "").split())[:500]
    bot = " ".join(str(body.get("bot") or "").split())[:80]
    chat = " ".join(str(body.get("chat") or "").split())[:80]
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))} {kind} bot={bot} chat={chat} {detail}\n"
    path = _logs(root) / "client.log"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        if path.stat().st_size > _MAX_LOG:
            kept = path.read_text(encoding="utf-8", errors="replace")[-_MAX_LOG // 2 :]
            path.write_text(kept, encoding="utf-8")
    except OSError:
        return


def _read(path: Path, limit: int = 400_000) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > limit:
        return text[-limit:]
    return text


def build_bundle(root: Path) -> bytes:
    folder = _logs(root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("server.log", "server.log.1", "client.log"):
            for candidate in (folder / name, Path(root).parent / "logs" / name, Path(root) / name):
                text = _read(candidate)
                if text:
                    archive.writestr(name, text)
                    break
            else:
                archive.writestr(name, "")
        audits: list[str] = []
        bots = Path(root) / "bots"
        if bots.is_dir():
            for path in sorted(bots.glob("*/safety-audit.json")):
                try:
                    rows = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(rows, list):
                    audits.extend(json.dumps(row, ensure_ascii=False) for row in rows[-50:])
        archive.writestr("audit-tail.log", "\n".join(audits[-50:]))
    return buffer.getvalue()
