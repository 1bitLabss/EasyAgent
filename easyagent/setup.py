"""The first-run flag. One small file, the same shape as ``search.json``.

The wizard asks the server whether onboarding is done; the answer lives in
``setup.json`` under the data dir. Nothing else reads it at boot.
"""

from __future__ import annotations

from easyagent.store import Store, atomic_write_json, read_json

FILENAME = "setup.json"


def load_setup(store: Store) -> dict:
    """The stored flags. A missing or broken file reads as a fresh install."""
    found: dict = {"onboarded": False}
    path = store.root / FILENAME
    if path.is_file():
        try:
            data = read_json(path)
        except Exception:
            data = None
        if isinstance(data, dict):
            found.update(data)
    found["onboarded"] = bool(found.get("onboarded"))
    return found


def save_setup(store: Store, *, onboarded: bool, extra: dict | None = None) -> dict:
    """Write the flags. Unknown keys already in the file are kept."""
    path = store.root / FILENAME
    current = load_setup(store)
    payload = dict(current)
    if extra:
        payload.update(extra)
    payload["onboarded"] = bool(onboarded)
    atomic_write_json(path, payload)
    return load_setup(store)
