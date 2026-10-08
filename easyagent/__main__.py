"""Start the local EasyAgent server."""

from __future__ import annotations

import os
import threading

import uvicorn

from easyagent.access import configured_token, lan_ip
from easyagent.app import create_app, default_data_dir
from easyagent.tray import start_tray


def main() -> None:
    port = int(os.environ.get("EASYAGENT_PORT", "44721"))
    host = os.environ.get("EASYAGENT_HOST", "0.0.0.0")
    data = default_data_dir()
    print(f"EasyAgent on this computer: http://127.0.0.1:{port}", flush=True)
    print(f"Listening on {host}:{port}", flush=True)
    phone = lan_ip()
    if phone and host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"Phone on the same network: http://{phone}:{port}", flush=True)
    elif host in {"127.0.0.1", "localhost", "::1"}:
        print("Phone access is off because EASYAGENT_HOST is loopback only.", flush=True)
    else:
        print(f"Phone on the same network: http://<this-computer-lan-address>:{port}", flush=True)
    if configured_token():
        print("Remote access is on. Type EASYAGENT_TOKEN on the phone. It is not printed here.", flush=True)
    else:
        print("Remote access is off until EASYAGENT_TOKEN is set.", flush=True)
    relay = os.environ.get("EASYAGENT_RELAY_URL", "").strip()
    if relay:
        print(f"Away from home: this computer dials {relay}", flush=True)
    print(f"Data directory: {data.resolve()}", flush=True)
    app = create_app(data)
    tray_stop = threading.Event()
    tray = start_tray(app.state.store, port, tray_stop)
    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        tray_stop.set()
        if tray is not None:
            tray.join(timeout=5)


if __name__ == "__main__":
    main()
