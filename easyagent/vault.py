"""Encrypted sign-in for a saved computer.

The username, password, and key are sealed before they touch disk.
The vault key is not stored under the data directory.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultError(Exception):
    """The sealed sign-in could not be opened."""


def vault_key_path() -> Path:
    override = (os.environ.get("EASYAGENT_VAULT_KEY_FILE") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".easyagent" / "vault.key"


def _load_key() -> bytes:
    path = vault_key_path()
    if path.is_file() and not path.is_symlink():
        raw = path.read_bytes()
        if len(raw) == 32:
            return raw
        raise VaultError("The vault key could not be read.")
    if path.exists():
        raise VaultError("The vault key could not be read.")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raw = path.read_bytes()
        if len(raw) != 32:
            raise VaultError("The vault key could not be read.") from None
        return raw
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def seal(payload: dict) -> dict:
    """Return a nonce and ciphertext. The payload is not written here."""
    nonce = os.urandom(12)
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    box = AESGCM(_load_key()).encrypt(nonce, plain, None)
    return {
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "box": base64.b64encode(box).decode("ascii"),
    }


def open_vault(vault: dict) -> dict:
    if not isinstance(vault, dict):
        raise VaultError("The saved sign-in could not be opened.")
    try:
        nonce = base64.b64decode(vault["nonce"])
        box = base64.b64decode(vault["box"])
        plain = AESGCM(_load_key()).decrypt(nonce, box, None)
        data = json.loads(plain.decode("utf-8"))
    except VaultError:
        raise
    except Exception as exc:
        raise VaultError("The saved sign-in could not be opened.") from exc
    if not isinstance(data, dict):
        raise VaultError("The saved sign-in could not be opened.")
    return data
