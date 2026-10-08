"""One notice when something happens. It fires once, from this computer."""

from __future__ import annotations

import os
import shutil
import subprocess

from easyagent.store import Store


def deliver(title: str, body: str) -> None:
    """Best-effort desktop notice. The watch is already spent before this runs."""
    if os.environ.get("EASYAGENT_NOTIFY", "1").strip() == "0":
        return
    if not shutil.which("notify-send"):
        return
    subprocess.run(
        ["notify-send", title, body],
        timeout=3,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def poke(store: Store, kind: str) -> bool:
    """Fire one armed watch of this kind. A second event does not fire again."""
    watch = store.claim_watch(kind)
    if watch is None:
        return False
    if kind == "job_failed":
        deliver("EasyAgent", "A job failed.")
    else:
        deliver("EasyAgent", "A new message is waiting.")
    return True
