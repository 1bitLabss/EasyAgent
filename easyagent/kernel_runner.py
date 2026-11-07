"""The child side of the persistent Python kernel.

One process per chat. It reads one JSON job per line from stdin, runs the
code in a namespace that never resets, and prints each result as a single
framed line between sentinel markers so the parent can pick it out of the
pipe. Variables and imports survive between jobs.

Job: {"job_id": str, "code": str, "cwd": str, "op": "exec" | "reset"}
Reply: @@EA-KERNEL-BEGIN@@{json}@@EA-KERNEL-END@@ on one line.
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback

BEGIN = "@@EA-KERNEL-BEGIN@@"
END = "@@EA-KERNEL-END@@"


def _reply(payload: dict) -> None:
    # json.dumps escapes newlines, so the framed reply is always one line.
    sys.stdout.write(f"{BEGIN}{json.dumps(payload)}{END}\n")
    sys.stdout.flush()


def main() -> None:
    # The parent reads this pipe as UTF-8. Windows would otherwise write the
    # console code page and mangle anything outside ASCII.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    namespace: dict = {"__name__": "__main__", "__builtins__": __builtins__}
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
        except ValueError:
            continue
        job_id = str(job.get("job_id") or "")
        if job.get("op") == "reset":
            namespace = {"__name__": "__main__", "__builtins__": __builtins__}
            _reply({"job_id": job_id, "ok": True, "stdout": "", "stderr": "", "error": ""})
            continue
        cwd = str(job.get("cwd") or "")
        if cwd:
            try:
                os.chdir(cwd)
            except OSError:
                pass
        code = str(job.get("code") or "")
        out = io.StringIO()
        err = io.StringIO()
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        ok = True
        error = ""
        try:
            exec(compile(code, "<ea-python>", "exec"), namespace)
        except SystemExit:
            pass
        except BaseException:
            ok = False
            error = traceback.format_exc()
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err
        _reply(
            {
                "job_id": job_id,
                "ok": ok,
                "stdout": out.getvalue(),
                "stderr": err.getvalue(),
                "error": error,
            }
        )


if __name__ == "__main__":
    main()
