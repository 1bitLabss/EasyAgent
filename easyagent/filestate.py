"""Per-chat file reads and writes.

A write of a file this chat already read is refused when the bytes changed,
including a read from an earlier turn. The registry is the Hermes file-state
registry: per-path locks, a last writer, and a read stamp. EasyAgent keys it
by chat id and stores a content hash next to the mtime.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

# (mtime, read_ts, partial, digest). partial=True when the read was a window.
ReadStamp = tuple[float, float, bool, str]

_MAX_PATHS_PER_CHAT = 4096
_MAX_GLOBAL_WRITERS = 4096


def _mtime_or_none(resolved: str) -> Optional[float]:
    try:
        return os.path.getmtime(resolved)
    except OSError:
        return None


def content_hash(resolved: str) -> str:
    """SHA-256 of the file bytes. Empty when the file cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(resolved, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _evict_oldest(container, cap: int) -> None:
    for _ in range(len(container) - cap):
        try:
            if isinstance(container, set):
                container.pop()
            else:
                container.pop(next(iter(container)))
        except (StopIteration, KeyError):
            break


class FileStateRegistry:
    """Process-wide coordinator for file edits, split by chat id."""

    def __init__(self) -> None:
        self._reads: dict[str, dict[str, ReadStamp]] = defaultdict(dict)
        self._last_writer: dict[str, tuple[str, float]] = {}
        self._path_locks: dict[str, threading.Lock] = {}
        self._path_lock_users: dict[str, int] = {}
        self._meta_lock = threading.Lock()
        self._state_lock = threading.Lock()

    @contextmanager
    def lock_path(self, resolved: str):
        """Threads on the same path wait. Different paths do not."""
        with self._meta_lock:
            lock = self._path_locks.setdefault(resolved, threading.Lock())
            self._path_lock_users[resolved] = self._path_lock_users.get(resolved, 0) + 1
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._meta_lock:
                users = self._path_lock_users[resolved] - 1
                if users:
                    self._path_lock_users[resolved] = users
                else:
                    self._path_lock_users.pop(resolved, None)
                    self._path_locks.pop(resolved, None)

    def _stamp(self, task_id: str, resolved: str, mtime: float, now: float, partial: bool, digest: str) -> None:
        agent_reads = self._reads[task_id]
        agent_reads[resolved] = (float(mtime), now, bool(partial), digest or "")
        _evict_oldest(agent_reads, _MAX_PATHS_PER_CHAT)

    def record_read(self, task_id: str, resolved: str, *, partial: bool = False, mtime: Optional[float] = None) -> None:
        mtime = _mtime_or_none(resolved) if mtime is None else mtime
        if mtime is None:
            return
        digest = content_hash(resolved)
        with self._state_lock:
            self._stamp(task_id, resolved, mtime, time.time(), partial, digest)

    def note_write(self, task_id: str, resolved: str, *, mtime: Optional[float] = None) -> None:
        """A successful write is an implicit full read of the bytes just written."""
        mtime = _mtime_or_none(resolved) if mtime is None else mtime
        if mtime is None:
            return
        digest = content_hash(resolved)
        now = time.time()
        with self._state_lock:
            self._last_writer[resolved] = (task_id, now)
            _evict_oldest(self._last_writer, _MAX_GLOBAL_WRITERS)
            self._stamp(task_id, resolved, mtime, now, False, digest)

    def _current_digest(self, resolved: str, stamp: ReadStamp | None) -> str:
        if stamp and stamp[3]:
            return content_hash(resolved)
        return content_hash(resolved)

    def check_stale(self, task_id: str, resolved: str) -> Optional[str]:
        """A reason this write would clobber unseen bytes, or None."""
        with self._state_lock:
            stamp = self._reads.get(task_id, {}).get(resolved)
            last_writer = self._last_writer.get(resolved)

        if stamp is None and last_writer is None:
            return None
        current_mtime = _mtime_or_none(resolved)
        if current_mtime is None:
            return None

        if last_writer is not None:
            writer_tid, writer_ts = last_writer
            if writer_tid != task_id:
                if stamp is None:
                    return (
                        f"{resolved} was modified by another chat "
                        f"{writer_tid!r} but this chat never read it. "
                        "Read the file before writing to avoid overwriting those changes."
                    )
                read_ts = stamp[1]
                if writer_ts > read_ts:
                    return (
                        f"{resolved} was modified by another chat "
                        f"{writer_tid!r} at {_fmt_ts(writer_ts)} — after "
                        f"this chat's last read at {_fmt_ts(read_ts)}. "
                        "Re-read the file before writing."
                    )

        if stamp is not None:
            read_mtime, _read_ts, partial, digest = stamp
            current_digest = content_hash(resolved) if digest else ""
            if current_mtime != read_mtime or (digest and current_digest and current_digest != digest):
                return (
                    f"{resolved} was modified since you last read it "
                    "on disk (external edit or unrecorded writer). "
                    "Re-read the file before writing. The file changed since it was last read."
                )
            if partial:
                return (
                    f"{resolved} was last read with offset/limit pagination "
                    "(partial view). Read the remaining pages, or use patch, "
                    "before overwriting it."
                )
            return None

        return (
            f"{resolved} was not read by this chat. "
            "Read the file first so you can write an informed edit."
        )

    def has_full_read(self, task_id: str, resolved: str) -> bool:
        """True when this chat has a full read whose mtime and hash still match."""
        with self._state_lock:
            stamp = self._reads.get(task_id, {}).get(resolved)
        if stamp is None or stamp[2]:
            return False
        current_mtime = _mtime_or_none(resolved)
        if current_mtime is None or current_mtime != stamp[0]:
            return False
        digest = stamp[3]
        if digest and content_hash(resolved) != digest:
            return False
        return True

    def writes_since(self, exclude_task_id: str, since_ts: float, paths: Iterable[str]) -> dict[str, list[str]]:
        paths_set = set(paths)
        out: dict[str, list[str]] = defaultdict(list)
        with self._state_lock:
            for path, (writer_tid, ts) in self._last_writer.items():
                if writer_tid != exclude_task_id and ts >= since_ts and path in paths_set:
                    out[writer_tid].append(path)
        return dict(out)

    def known_reads(self, task_id: str) -> list[str]:
        with self._state_lock:
            return list(self._reads.get(task_id, {}).keys())

    def forget_task(self, task_id: str) -> None:
        with self._state_lock:
            self._reads.pop(task_id, None)
            for path in [path for path, (writer_tid, _ts) in self._last_writer.items() if writer_tid == task_id]:
                del self._last_writer[path]

    def clear(self) -> None:
        with self._state_lock:
            self._reads.clear()
            self._last_writer.clear()
        with self._meta_lock:
            self._path_locks.clear()
            self._path_lock_users.clear()


_registry = FileStateRegistry()


def get_registry() -> FileStateRegistry:
    return _registry


def record_read(task_id: str, resolved_or_path: str | Path, *, partial: bool = False) -> None:
    _registry.record_read(task_id, str(resolved_or_path), partial=partial)


def note_write(task_id: str, resolved_or_path: str | Path) -> None:
    _registry.note_write(task_id, str(resolved_or_path))


def check_stale(task_id: str, resolved_or_path: str | Path) -> Optional[str]:
    return _registry.check_stale(task_id, str(resolved_or_path))


def has_full_read(task_id: str, resolved_or_path: str | Path) -> bool:
    return _registry.has_full_read(task_id, str(resolved_or_path))


def lock_path(resolved_or_path: str | Path):
    return _registry.lock_path(str(resolved_or_path))


def known_reads(task_id: str) -> list[str]:
    return _registry.known_reads(task_id)


__all__ = [
    "FileStateRegistry",
    "check_stale",
    "content_hash",
    "get_registry",
    "has_full_read",
    "known_reads",
    "lock_path",
    "note_write",
    "record_read",
]
