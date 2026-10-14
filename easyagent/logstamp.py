"""Put a UTC timestamp on each line the server writes.

The desktop app saves stdout and stderr as logs/server.log. Print lines such
as a reconnect note had no time on them, so a long stall could not be read
back. Uvicorn is started after this wrapper is installed, so its lines go
through it too.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StampedStream:
    """Line-buffer a text stream and prefix each line with a UTC timestamp."""

    def __init__(self, raw) -> None:
        self._raw = raw
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text) -> int:
        data = "" if text is None else str(text)
        if not data:
            return 0
        with self._lock:
            self._buf += data
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._emit(line)
        return len(data)

    def _emit(self, line: str) -> None:
        if line == "":
            self._raw.write("\n")
            return
        self._raw.write(f"{_stamp()} {line}\n")

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                self._emit(self._buf)
                self._buf = ""
            flush = getattr(self._raw, "flush", None)
            if flush:
                flush()

    def fileno(self):
        return self._raw.fileno()

    def isatty(self) -> bool:
        fn = getattr(self._raw, "isatty", None)
        return bool(fn and fn())

    def __getattr__(self, name):
        return getattr(self._raw, name)


def stamp_stdio() -> None:
    """Wrap this process's stdout and stderr. A second call leaves them wrapped."""
    if not isinstance(sys.stdout, StampedStream):
        sys.stdout = StampedStream(sys.stdout)
    if not isinstance(sys.stderr, StampedStream):
        sys.stderr = StampedStream(sys.stderr)
