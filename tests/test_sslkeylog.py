"""An inherited SSLKEYLOGFILE must not stop a plain chat (Bitdefender; fixed in 0.3.19)."""

import os
import ssl
import subprocess
import sys
from pathlib import Path


def _run(env_extra: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    # The suite points HOME at an empty folder, so this child does not see the
    # user site where httpx is installed. Put that directory on PYTHONPATH.
    import httpx

    site = str(Path(httpx.__file__).resolve().parents[1])
    parts = [env.get("PYTHONPATH", ""), site]
    env["PYTHONPATH"] = os.pathsep.join(part for part in parts if part)
    code = "import easyagent, os, httpx; print(os.environ.get('SSLKEYLOGFILE')); httpx.AsyncClient()"
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)


def test_unwritable_keylog_is_dropped_on_import(tmp_path):
    blocked = tmp_path / "virtual_file.log"
    blocked.mkdir()  # opening a directory for append fails, like the locked volume path
    done = _run({"SSLKEYLOGFILE": str(blocked)})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "None"


def test_keylog_kept_only_when_explicitly_allowed(tmp_path):
    target = tmp_path / "keys.log"
    done = _run({"SSLKEYLOGFILE": str(target), "EASYAGENT_ALLOW_SSLKEYLOGFILE": "1"})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(target)


def test_plain_ssl_context_would_fail_without_the_guard(tmp_path):
    blocked = tmp_path / "virtual_file.log"
    blocked.mkdir()
    code = "import ssl; ssl.create_default_context()"
    done = subprocess.run([sys.executable, "-c", code], env={**os.environ, "SSLKEYLOGFILE": str(blocked)},
                          capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and ("Errno" in done.stderr)
