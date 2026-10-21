"""Start the slow stub and EasyAgent, then run the 19 busy-send cases.

One process, one temporary data directory. Checks stay on. The stub is the
same 30 token/s server the hands-on run used. Exit 1 unless every case passes.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "web" / "e2e" / "busy_harness"


def _free() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait(url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception as exc:
            last = str(exc)
            time.sleep(0.2)
    raise SystemExit(f"did not answer {url}: {last}")


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    app_port = _free()
    stub_port = _free()
    data = Path(os.environ.get("BUSY_HARNESS_DATA") or f"/tmp/ea-busy-{app_port}")
    out = Path(os.environ.get("BUSY_HARNESS_OUT") or f"/tmp/busy-harness-{app_port}/")
    data.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    stub = subprocess.Popen(
        [sys.executable, str(HARNESS / "slowstub.py"), str(stub_port)],
        cwd=str(ROOT),
    )
    env = os.environ.copy()
    env.update(
        {
            "EASYAGENT_DATA": str(data),
            "EASYAGENT_PORT": str(app_port),
            "EASYAGENT_HOST": "127.0.0.1",
            "EASYAGENT_TRAY": "0",
        }
    )
    env.pop("EASYAGENT_CHECK", None)
    app = subprocess.Popen(
        [sys.executable, "-m", "easyagent"],
        cwd=str(ROOT),
        env=env,
        stdout=open(out / "server.log", "w"),
        stderr=subprocess.STDOUT,
    )
    try:
        _wait(f"http://127.0.0.1:{stub_port}/v1/models")
        _wait(f"http://127.0.0.1:{app_port}/api/health")
        body = json.dumps(
            {
                "name": "stub",
                "base_url": f"http://127.0.0.1:{stub_port}/v1",
                "model": "stub",
            }
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{app_port}/api/endpoints",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        endpoint = json.load(urllib.request.urlopen(request))
        child_env = env.copy()
        child_env["BUSY_HARNESS_OUT"] = str(out)
        child_env["BUSY_HARNESS_URL"] = f"http://127.0.0.1:{app_port}"
        mode = os.environ.get("BUSY_HARNESS_MODE") or "cases"
        script = {"cases": "run.py", "seam": "seam.py", "long": "long10.py"}.get(mode, "run.py")
        args = [sys.executable, str(HARNESS / script), endpoint["id"]]
        if only and script == "run.py":
            args.append(only)
        ran = subprocess.run(args, cwd=str(HARNESS), env=child_env)
        return ran.returncode
    finally:
        app.terminate()
        stub.terminate()
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()
        try:
            stub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stub.kill()


if __name__ == "__main__":
    sys.exit(main())
