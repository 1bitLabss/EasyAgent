"""An inherited SSLKEYLOGFILE must not stop a plain chat (Bitdefender; fixed in 0.3.19)."""

import os
import ssl
import subprocess
import sys


def _run(env_extra: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
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
