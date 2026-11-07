"""One live Python session per chat for the python tool.

The python tool runs code in a process that stays up between calls, so
variables and imports carry from one call to the next inside one chat.
Pressing Stop kills the session and clears that state; the next call starts
a fresh process and says so. A run that goes past its timeout is killed the
same way. Kernels idle for more than fifteen minutes are reaped.

When the sandbox contains the shell, the kernel does not run: the caller
falls back to the one-shot write-and-run path so containment still applies.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from easyagent import turn as turn_mod

_RUNNER = Path(__file__).with_name("kernel_runner.py")
_OUTPUT_CAP = 8000
_DEFAULT_TIMEOUT = 120
_MAX_TIMEOUT = 900
_IDLE_KILL_SECONDS = 15 * 60
_REAP_SECONDS = 60
_BEGIN = "@@EA-KERNEL-BEGIN@@"
_END = "@@EA-KERNEL-END@@"

_GUARD = threading.Lock()
_KERNELS: dict[str, "_Kernel"] = {}
_reaper: threading.Thread | None = None


class KernelUnavailable(Exception):
    """The kernel cannot run here (contained sandbox). Use the one-shot path."""


class _Kernel:
    def __init__(self, store, cwd: str) -> None:
        self.lock = threading.Lock()
        self.store = store
        self.cwd = cwd or ""
        self.proc: subprocess.Popen | None = None
        self.generation = 0
        self.last_used = time.monotonic()
        # Lines from the runner's stdout, routed by the process reader thread.
        self.box: queue.Queue | None = None
        self.errbuf: list[bytes] = []
        # Why the state was cleared. The next run reports it once, then it clears.
        self.lost = ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            turn_mod.stop_process(proc)


def _key(store, chat_id: str) -> str:
    return turn_mod._key(store, chat_id)


def _chat_id() -> str:
    slot = turn_mod.current_slot()
    return slot.chat_id if slot is not None else ""


def _contained(store) -> bool:
    """True when the shell runs inside the OS container (or refuses to start).

    The kernel process would escape containment, so the caller keeps the
    one-shot path, which already honors the sandbox rules.
    """
    from easyagent.sandbox import shell_mode

    mode = shell_mode(store)
    return bool(mode.get("contained") or mode.get("refuse"))


def _start(kernel: _Kernel) -> None:
    from easyagent.secrets import scrub_env

    env = scrub_env(kernel.store, turn_mod.tool_env())
    proc = subprocess.Popen(
        [sys.executable, "-u", str(_RUNNER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=kernel.cwd or None,
        env=env,
        start_new_session=True,
    )
    kernel.proc = proc
    kernel.errbuf = []
    # One reader per pipe for the life of the process. A per-job reader would
    # stay parked on the pipe after its job ends and steal the next job's line.
    threading.Thread(target=_pump_stdout, args=(kernel, proc), daemon=True).start()
    threading.Thread(target=_pump_stderr, args=(kernel, proc), daemon=True).start()


def _pump_stdout(kernel: _Kernel, proc: subprocess.Popen) -> None:
    try:
        for line in proc.stdout or []:
            if kernel.proc is proc and kernel.box is not None:
                kernel.box.put(line)
    except (OSError, ValueError):
        pass
    if kernel.proc is proc and kernel.box is not None:
        kernel.box.put(None)


def _pump_stderr(kernel: _Kernel, proc: subprocess.Popen) -> None:
    try:
        rest = proc.stderr.read() if proc.stderr else b""
    except (OSError, ValueError):
        rest = b""
    if kernel.proc is proc and rest:
        kernel.errbuf.append(rest)


def _send(kernel: _Kernel, job: dict) -> None:
    assert kernel.proc is not None and kernel.proc.stdin is not None
    kernel.proc.stdin.write((json.dumps(job) + "\n").encode("utf-8"))
    kernel.proc.stdin.flush()


def _run_job(kernel: _Kernel, code: str, wait: float) -> str:
    from easyagent.tools import ToolError
    from easyagent.truncate import truncate_head_tail

    assert kernel.proc is not None
    kernel.generation += 1
    job_id = uuid.uuid4().hex[:8]
    box: queue.Queue = queue.Queue()
    kernel.box = box
    try:
        _send(kernel, {"job_id": job_id, "code": code, "cwd": kernel.cwd})
    except OSError as exc:
        kernel.kill()
        kernel.box = None
        kernel.lost = "the Python session could not be reached"
        raise ToolError(_lost_note(kernel) + "The Python session could not be started. Nothing was run.") from exc
    deadline = time.monotonic() + wait
    result: dict | None = None
    while result is None:
        if turn_mod.cancelled():
            kernel.kill()
            kernel.box = None
            kernel.lost = "you pressed Stop"
            raise turn_mod.TurnCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            kernel.kill()
            kernel.box = None
            kernel.lost = f"a run went past {int(wait)} seconds"
            raise ToolError(
                f"The code timed out after {int(wait)} s. The Python session was restarted. "
                "Variables from earlier calls are gone."
            )
        try:
            line = box.get(timeout=min(0.25, remaining))
        except queue.Empty:
            continue
        if line is None:
            break
        text = line.decode("utf-8", "replace").strip()
        if not text.startswith(_BEGIN):
            continue
        try:
            payload = json.loads(text[len(_BEGIN) : -len(_END)])
        except ValueError:
            continue
        if payload.get("job_id") == job_id:
            result = payload
    kernel.box = None
    if result is None:
        # The runner died mid-job. Its state is unknown, so it is gone.
        stderr = b"".join(kernel.errbuf).decode("utf-8", "replace").strip()
        kernel.kill()
        kernel.lost = "the Python session stopped on its own"
        detail = f"\n{stderr}" if stderr else ""
        raise ToolError(
            _lost_note(kernel) + "The Python session stopped while running the code. "
            "It starts fresh on the next call." + detail
        )
    note = _lost_note(kernel)
    kernel.last_used = time.monotonic()
    if not result.get("ok"):
        trace = str(result.get("error") or result.get("stderr") or "")
        raise ToolError(note + "exit code 1\n" + truncate_head_tail(trace.strip(), _OUTPUT_CAP))
    text = (str(result.get("stdout") or "") + str(result.get("stderr") or "")).strip()
    if not text:
        text = "(no output)"
    else:
        text = truncate_head_tail(text, _OUTPUT_CAP)
    return note + text if note else text


def _lost_note(kernel: _Kernel) -> str:
    if not kernel.lost:
        return ""
    note = f"The Python session was restarted ({kernel.lost}). Variables from earlier calls are gone.\n"
    kernel.lost = ""
    return note


def _reap() -> None:
    """Drop kernels that have been idle for a long time."""
    now = time.monotonic()
    with _GUARD:
        stale = [k for k in _KERNELS.values() if now - k.last_used > _IDLE_KILL_SECONDS]
    for kernel in stale:
        if kernel.lock.acquire(blocking=False):
            try:
                if kernel.alive() and time.monotonic() - kernel.last_used > _IDLE_KILL_SECONDS:
                    kernel.kill()
                    kernel.lost = "it sat idle for over fifteen minutes"
            finally:
                kernel.lock.release()


def _ensure_reaper() -> None:
    global _reaper
    with _GUARD:
        if _reaper is not None and _reaper.is_alive():
            return

        def loop() -> None:
            while True:
                time.sleep(_REAP_SECONDS)
                try:
                    _reap()
                except Exception:
                    pass

        _reaper = threading.Thread(target=loop, name="easyagent-kernel-reaper", daemon=True)
        _reaper.start()


def execute(store, code: str, timeout: float | None = None, cwd: str | None = None) -> str | None:
    """Run code in this chat's live Python session. None means use the one-shot path.

    Called from a worker thread (asyncio.to_thread), so the blocking pipe
    reads and the inherited turn context both work here.
    """
    # Code started without a bound slot must clear an inherited stop flag
    # before it begins work, or an earlier turn's Stop cancels this call.
    if turn_mod.cancel_is_orphaned():
        turn_mod.renew_cancel()
    if _contained(store):
        return None
    wait = float(timeout) if timeout and timeout > 0 else float(_DEFAULT_TIMEOUT)
    if wait > _MAX_TIMEOUT:
        wait = float(_MAX_TIMEOUT)
    turn_mod.raise_if_cancelled()
    chat_id = _chat_id()
    key = _key(store, chat_id)
    _reap()
    _ensure_reaper()
    with _GUARD:
        kernel = _KERNELS.get(key)
        if kernel is None:
            kernel = _Kernel(store, cwd or "")
            _KERNELS[key] = kernel
    with kernel.lock:
        if cwd:
            kernel.cwd = cwd
        if not kernel.alive():
            kernel.kill()
            try:
                _start(kernel)
            except OSError as exc:
                with _GUARD:
                    _KERNELS.pop(key, None)
                from easyagent.tools import ToolError

                raise ToolError(f"Could not start the Python session. {exc}") from exc
        return _run_job(kernel, code, wait)


def interrupt(store, chat_id: str) -> None:
    """Stop pressed for this chat: kill the session and clear its state.

    The next python call starts a fresh process and reports that the
    variables from earlier calls are gone.
    """
    with _GUARD:
        kernel = _KERNELS.get(_key(store, chat_id))
    if kernel is None:
        return
    with kernel.lock:
        kernel.kill()
        kernel.generation += 1
        kernel.lost = "you pressed Stop"


def reset(store, chat_id: str) -> None:
    """Drop this chat's session. The next call starts with nothing kept."""
    with _GUARD:
        kernel = _KERNELS.pop(_key(store, chat_id), None)
    if kernel is None:
        return
    with kernel.lock:
        kernel.kill()
        kernel.generation += 1


drop = reset


def drop_bot(store) -> None:
    """A bot is being removed and its workspace folder is about to go away.

    A live kernel may be sitting inside that folder, which would block the
    delete on Windows, so every session belonging to this store is dropped.
    """
    with _GUARD:
        doomed = [(key, k) for key, k in _KERNELS.items() if k.store is store]
        for key, _ in doomed:
            _KERNELS.pop(key, None)
    for _, kernel in doomed:
        with kernel.lock:
            kernel.kill()
            kernel.generation += 1


def shutdown_all() -> None:
    """The server is going away. Take every session with it."""
    with _GUARD:
        kernels = list(_KERNELS.values())
        _KERNELS.clear()
    for kernel in kernels:
        with kernel.lock:
            kernel.kill()
