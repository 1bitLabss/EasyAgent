"""Start the local EasyAgent server."""

from __future__ import annotations

import os

import uvicorn

from easyagent.access import configured_token, lan_ip
from easyagent.app import create_app, default_data_dir
from easyagent.phone import PhoneBook, launcher_firewall
from easyagent.tray import start_tray


def _print_banner(port: int, host: str, data) -> None:
    print(f"EasyAgent on this computer: http://127.0.0.1:{port}", flush=True)
    print(f"Listening on {host}:{port}", flush=True)
    book = PhoneBook(data)
    phone = lan_ip()
    if book.enabled and phone:
        print(f"Phone access is on. A phone on this network can open http://{phone}:{port}", flush=True)
        print("Pair it from Settings. The pairing token is not printed here.", flush=True)
        print(
            "Windows Defender Firewall, Advanced settings, Inbound Rules: "
            f"allow TCP port {port} from the local subnet. "
            "Settings can add that rule when you agree, or start with EASYAGENT_FIREWALL=1.",
            flush=True,
        )
    elif book.enabled:
        print("Phone access is on, and this computer has no LAN address.", flush=True)
    else:
        print("Phone access is off. Turn it on in Settings to open the home network.", flush=True)
    if configured_token():
        print("EASYAGENT_TOKEN is an extra home-network token. It is not printed here.", flush=True)
    relay = os.environ.get("EASYAGENT_RELAY_URL", "").strip()
    if relay:
        print(f"Away from home: this computer dials {relay}", flush=True)
    print(f"Data directory: {data.resolve()}", flush=True)
    rule = launcher_firewall(book.enabled, port)
    if rule is not None:
        print(rule["detail"], flush=True)


def attach_lan(server: uvicorn.Server, app) -> None:
    app.state.uvicorn_server = server

    async def startup(sockets=None):
        await uvicorn.Server.startup(server, sockets=sockets)
        if server.started:
            await app.state.lan.apply(server, int(server.config.port), app.state.phone.enabled)

    server.startup = startup  # type: ignore[method-assign]


def serve(app, host: str, port: int) -> None:
    config = uvicorn.Config(app, host=host, port=port)
    server = uvicorn.Server(config)
    attach_lan(server, app)
    server.run()


def main() -> None:
    import threading

    port = int(os.environ.get("EASYAGENT_PORT", "44721"))
    host = os.environ.get("EASYAGENT_HOST", "127.0.0.1")
    data = default_data_dir()
    _print_banner(port, host, data)
    app = create_app(data)
    tray_stop = threading.Event()
    tray = start_tray(app.state.store, port, tray_stop)
    try:
        serve(app, host, port)
    finally:
        tray_stop.set()
        if tray is not None:
            tray.join(timeout=5)


if __name__ == "__main__":
    main()
