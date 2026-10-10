"""Secrets for endpoints, connectors, bridges, and search.

One SQLite file, ``secrets.db``, sits in the data directory. Each value is
sealed with AES-GCM. The master key is not stored next to that file: it lives
in the OS keychain, or it is derived from a passphrase when no keychain
exists. A data file read back as text therefore holds no key. The server
process is the only reader. A bot shell is not given the key, the passphrase,
or a keychain session.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

class SecretError(RuntimeError):
    """The encrypted store could not be opened. Nothing new was written over the key."""


_MEMORY_KEYS: dict[str, bytes] = {}
_DERIVED: dict[str, bytes] = {}
_SERVICE = "easyagent"
_MASTER_ACCOUNT = "secrets-master"
_LOCK = threading.Lock()
_KDF_ROUNDS = 200_000
_KDF_ROUNDS_USER = 600_000
_KEYRING_WAIT = 10.0
_KEYRING_TRIES = 3
_VERIFIER_PLAIN = b"easyagent-secrets-v1"
_VERIFIER_AD = b"easyagent-verifier"


def _memory() -> bool:
    return (os.environ.get("EASYAGENT_KEYRING") or "").strip().lower() == "memory"


def _file_only() -> bool:
    mode = (os.environ.get("EASYAGENT_KEYRING") or "").strip().lower()
    return mode in {"file", "fallback", "passphrase"}


def secrets_path(store) -> Path:
    root = _root(store)
    return root / "secrets.db"


def passphrase_path() -> Path:
    """The random passphrase file. It is never inside the data directory."""
    override = (os.environ.get("EASYAGENT_SECRETS_PASSFILE") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".easyagent" / "secrets.passphrase"


def is_protected_secret(store, path: Path) -> bool:
    """True for secrets.db or the passphrase file. A bot must not read either."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    for target in _protected_targets(store):
        try:
            other = target.resolve()
        except OSError:
            other = target
        if resolved == other:
            return True
    return False


def command_targets_secret(store, command: str) -> bool:
    """A shell line that names secrets.db or the passphrase file."""
    raw = command or ""
    folded = raw.replace("\\", "/").casefold()
    for target in _protected_targets(store):
        shown = str(target).replace("\\", "/").casefold()
        if shown and shown in folded:
            return True
    if re.search(r"(?i)(?:^|[\s'\"=])secrets\.passphrase(?:$|[\s'\"])", raw):
        return True
    if re.search(r"(?i)(?:^|[\\/\s'\"=])secrets\.db(?:$|[\s'\"])", raw):
        return True
    return False


def _protected_targets(store) -> list[Path]:
    found = [passphrase_path()]
    if store is not None:
        found.append(secrets_path(store))
    return found


def _require_outside(root: Path, path: Path, what: str) -> None:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return
    except OSError:
        return
    raise SecretError(f"The {what} must live outside the data directory. No secret was written.")


def keychain_paths() -> list[Path]:
    """Where the master key lives. A bot shell is not allowed to read these.

    The database is sealed with the data directory. These are the other
    places: the OS keychain, and the passphrase file when no keychain exists.
    """
    return _unique_paths([Path(name) for name in keychain_names()])


def keychain_names() -> list[str]:
    """The same locations as strings, so a Windows list can be checked anywhere."""
    if os.name == "nt":
        home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        roaming = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return [
            os.path.join(home, ".easyagent"),
            os.path.join(local, "Microsoft", "Credentials"),
            os.path.join(local, "Microsoft", "Vault"),
            os.path.join(local, "Microsoft", "Protect"),
            os.path.join(roaming, "Microsoft", "Credentials"),
            os.path.join(roaming, "Microsoft", "Protect"),
        ]
    home = Path.home()
    if sys.platform == "darwin":
        return _unique_strings([
            str(home / ".easyagent"),
            str(home / "Library" / "Keychains"),
        ])
    runtime = (os.environ.get("XDG_RUNTIME_DIR") or "").strip() or f"/run/user/{os.getuid()}"
    base = Path(runtime)
    return _unique_strings([
        str(home / ".easyagent"),
        str(home / ".local" / "share" / "keyrings"),
        str(home / ".local" / "share" / "kwalletd"),
        str(home / ".local" / "share" / "gnome-keyring"),
        str(base / "bus"),
        str(base / "keyring"),
    ])


def _unique_strings(names: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        found.append(name)
    return found


def _unique_paths(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return found


def endpoint_account(endpoint_id: str) -> str:
    return f"endpoint:{endpoint_id}:api_key"


def put_secret(account: str, value: str, store=None) -> None:
    name = (account or "").strip()
    text = value if isinstance(value, str) else ""
    if not name or not text:
        raise RuntimeError("The secret needs a name and a value.")
    root = _root(store)
    key = _master_key(root)
    nonce, box = _seal(key, name, text)
    with _LOCK:
        conn = _connect(root)
        try:
            conn.execute(
                "INSERT INTO secrets(account, nonce, ciphertext) VALUES(?, ?, ?) "
                "ON CONFLICT(account) DO UPDATE SET nonce=excluded.nonce, ciphertext=excluded.ciphertext",
                (name, nonce, box),
            )
            conn.commit()
        finally:
            conn.close()


def get_secret(account: str, store=None) -> str:
    name = (account or "").strip()
    if not name:
        return ""
    root = _root(store)
    path = root / "secrets.db"
    if not path.is_file():
        return ""
    key = _master_key(root)
    with _LOCK:
        conn = _connect(root)
        try:
            row = conn.execute("SELECT nonce, ciphertext FROM secrets WHERE account = ?", (name,)).fetchone()
        finally:
            conn.close()
    if not row:
        return ""
    opened = _open(key, name, row[0], row[1])
    if opened is None:
        return ""
    plain, legacy = opened
    if legacy:
        _reseal(root, key, name, plain)
    return plain


def delete_secret(account: str, store=None) -> None:
    name = (account or "").strip()
    if not name:
        return
    root = _root(store)
    path = root / "secrets.db"
    if not path.is_file():
        return
    with _LOCK:
        conn = _connect(root)
        try:
            conn.execute("DELETE FROM secrets WHERE account = ?", (name,))
            conn.commit()
        finally:
            conn.close()


def secret_values(store) -> list[str]:
    """Every saved value. Callers scrub with these. They are not logged."""
    root = _root(store)
    path = root / "secrets.db"
    if not path.is_file():
        return []
    key = _master_key(root)
    with _LOCK:
        conn = _connect(root)
        try:
            conn_rows = conn.execute("SELECT account, nonce, ciphertext FROM secrets").fetchall()
        finally:
            conn.close()
    found: list[str] = []
    for account, nonce, box in conn_rows:
        opened = _open(key, str(account), nonce, box)
        if opened is None:
            continue
        plain, _legacy = opened
        if plain and plain not in found:
            found.append(plain)
    return found


def migrate_store(store) -> None:
    """Move plaintext keys out of data files. A second pass changes nothing.

    The master key is opened first. A timeout, a backend change, or a verifier
    that does not decrypt stops the migration before any file is wiped. The
    original JSON is copied outside the data directory until the values
    round-trip.
    """
    if not _migration_needed(store):
        return
    root = _root(store)
    _master_key(root)
    jobs = _plaintext_jobs(store)
    if not jobs:
        return
    backup = _backup_files(store)
    try:
        _migrate_endpoints(store)
        _migrate_rows(store, store.root / "bridges.json", "bridge")
        _migrate_rows(store, store.root / "connectors.json", "connector")
        mcp = store.root / "mcp.json"
        if mcp.is_file():
            _migrate_rows(store, mcp, "mcp")
        _round_trip(store, jobs)
        _remove_backup(backup)
    except Exception:
        _restore_backup(backup)
        raise


def _migrate_endpoints(store) -> None:
    path = store.endpoints_path
    if not path.is_file():
        return
    try:
        rows = store.list_endpoints()
    except Exception:
        return
    changed = False
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = item.get("api_key") or ""
        if isinstance(key, str) and key.strip():
            put_secret(endpoint_account(str(item.get("id") or "")), key.strip(), store)
            item["api_key"] = ""
            item["has_api_key"] = True
            changed = True
        elif "api_key" in item and item.get("api_key"):
            item["api_key"] = ""
            changed = True
    if changed:
        from easyagent.store import atomic_write_json

        atomic_write_json(path, rows)


_SECRET_FIELDS = ("token", "api_key", "access_token", "secret", "password")


def _migrate_rows(store, path: Path, kind: str) -> None:
    if not path.is_file():
        return
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(rows, list):
        return
    changed = False
    for item in rows:
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("id") or "")
        for field in _SECRET_FIELDS:
            value = item.get(field)
            if isinstance(value, str) and len(value.strip()) >= 6:
                put_secret(f"{kind}:{account_id}:{field}", value.strip(), store)
                item.pop(field, None)
                if field == "token":
                    item["has_token"] = True
                changed = True
        secrets = item.get("secrets")
        if isinstance(secrets, dict):
            for key, value in list(secrets.items()):
                if isinstance(value, str) and value.strip():
                    put_secret(f"{kind}:{account_id}:{key}", value.strip(), store)
                    names = list(item.get("secret_names") or [])
                    if key not in names:
                        names.append(key)
                    item["secret_names"] = names
            item.pop("secrets", None)
            changed = True
    if changed:
        from easyagent.store import atomic_write_json

        atomic_write_json(path, rows)


def _root(store) -> Path:
    if store is not None:
        root = getattr(store, "root", store)
        return Path(root)
    try:
        from easyagent import turn as turn_mod

        slot = turn_mod.current_slot()
        bound = getattr(slot, "store", None) if slot is not None else None
        if bound is not None:
            return Path(bound.root)
    except Exception:
        pass
    env = (os.environ.get("EASYAGENT_DATA") or "").strip()
    if env:
        return Path(env)
    return Path.cwd() / "data"


def _connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "secrets.db"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS secrets ("
        "account TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    conn.commit()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def _master_key(root: Path) -> bytes:
    """The key that opens this store. A new key is created only on a definite miss."""
    recorded = _recorded_backend(root)
    wanted = _wanted_backend()
    if recorded and recorded != wanted:
        raise SecretError(
            f"Secrets were sealed with {recorded}. This process is set to {wanted}. "
            "The master key was not replaced."
        )
    if wanted == "memory" or recorded == "memory":
        key = _memory_key(root)
        _ensure_verifier(root, key, "memory")
        return key
    if wanted == "passphrase" or recorded == "passphrase":
        create_file = not recorded and not _has_verifier(root) and not _has_rows(root)
        key = _passphrase_key(root, create_file=create_file)
        _ensure_verifier(root, key, "passphrase")
        return key
    if _has_verifier(root) or _has_rows(root):
        key = _keyring_master(root, create=False)
        if key is None:
            phrase = _existing_phrase()
            if phrase is None:
                raise SecretError(
                    "The keychain has no master key for this store. No new key was written."
                )
            key = _derive_phrase(root, phrase, user_set=bool(os.environ.get("EASYAGENT_SECRETS_PASSPHRASE")))
            if not _opens_existing(root, key):
                raise SecretError(
                    "secrets.db did not open with the keychain or the saved passphrase. "
                    "No new key was written."
                )
            _ensure_verifier(root, key, "passphrase")
            return key
        if not _opens_existing(root, key):
            raise SecretError("The keychain key did not open secrets.db. It was not replaced.")
        _ensure_verifier(root, key, "keyring")
        return key
    choice = _select_new_backend()
    if choice == "passphrase":
        key = _passphrase_key(root, create_file=True)
        _ensure_verifier(root, key, "passphrase")
        return key
    key = _keyring_master(root, create=True)
    if key is None:
        raise SecretError("The keychain did not accept a master key. No secret was written.")
    _ensure_verifier(root, key, "keyring")
    return key


def _memory_key(root: Path) -> bytes:
    with _LOCK:
        found = _MEMORY_KEYS.get(str(root))
        if found is None:
            found = AESGCM.generate_key(bit_length=256)
            _MEMORY_KEYS[str(root)] = found
        return found


def _keyring_master(root: Path, *, create: bool) -> bytes | None:
    """Read the keychain key. Create one only after every try says it is absent."""
    del root
    last = "the keychain did not answer"
    misses = 0
    for _attempt in range(_KEYRING_TRIES):
        status, detail = _keyring("get", _MASTER_ACCOUNT, "")
        if status == "ok":
            try:
                raw = base64.b64decode(detail)
            except Exception:
                raw = b""
            if len(raw) != 32:
                raise SecretError("The keychain entry is not a master key. It was not overwritten.")
            return raw
        if status == "missing":
            misses += 1
            continue
        if status == "absent":
            return None
        last = detail or last
    if misses == _KEYRING_TRIES:
        if not create:
            return None
        key = AESGCM.generate_key(bit_length=256)
        stored = base64.b64encode(key).decode("ascii")
        status, detail = _keyring("set", _MASTER_ACCOUNT, stored)
        if status != "ok":
            raise SecretError(
                f"The new master key could not be saved ({detail or 'keychain'}). Nothing was overwritten."
            )
        return key
    raise SecretError(f"The keychain did not answer ({last}). No new master key was written.")


def _select_new_backend() -> str:
    """Keyring when it answers. Passphrase only when no keychain exists."""
    if _file_only():
        return "passphrase"
    last = "the keychain did not answer"
    for _attempt in range(_KEYRING_TRIES):
        status, detail = _keyring("get", "easyagent-probe", "")
        if status in {"ok", "missing"}:
            return "keyring"
        if status == "absent":
            return "passphrase"
        last = detail or last
    raise SecretError(f"The keychain did not answer ({last}). No passphrase key was created.")


def _forget_derived(root: Path) -> None:
    prefix = f"{root}:"
    with _LOCK:
        for key in [item for item in _DERIVED if item.startswith(prefix)]:
            _DERIVED.pop(key, None)


def _phrase_opens(root: Path, key: bytes) -> bool:
    if not _has_verifier(root) and not _has_rows(root):
        return True
    return _opens_existing(root, key)


def _repair_newline_phrase(raw: bytes) -> bytes | None:
    """A text-mode write expands every 0x0A, so the file no longer matches the phrase that sealed the store."""
    if len(raw) < 33 or b"\r\n" not in raw:
        return None
    repaired = raw.replace(b"\r\n", b"\n")
    if len(repaired) < 16 or repaired == raw:
        return None
    return repaired


def _passphrase_key(root: Path, *, create_file: bool) -> bytes:
    """Derive the master key. The passphrase is never written into the data directory."""
    path = passphrase_path()
    _require_outside(root, path, "passphrase file")
    phrase = _existing_phrase()
    user_set = bool((os.environ.get("EASYAGENT_SECRETS_PASSPHRASE") or "").strip())
    if phrase is None:
        if not create_file:
            raise SecretError("There is no passphrase for this store. No new passphrase was created.")
        phrase = _passphrase_bytes()
        user_set = False
        return _derive_phrase(root, phrase, user_set=user_set)
    key = _derive_phrase(root, phrase, user_set=user_set)
    if _phrase_opens(root, key) or user_set:
        return key
    repaired = _repair_newline_phrase(phrase)
    if repaired is None:
        return key
    _forget_derived(root)
    fixed = _derive_phrase(root, repaired, user_set=False)
    if not _phrase_opens(root, fixed):
        _forget_derived(root)
        return _derive_phrase(root, phrase, user_set=user_set)
    _write_passphrase_file(path, repaired)
    return fixed


def _derive_phrase(root: Path, phrase: bytes, *, user_set: bool) -> bytes:
    salt = _meta_get(root, "kdf_salt")
    if not isinstance(salt, (bytes, bytearray)) or len(salt) != 16:
        salt = os.urandom(16)
        _meta_put(root, "kdf_salt", bytes(salt))
    recorded = _meta_get(root, "kdf_rounds")
    if recorded:
        try:
            rounds = int(recorded.decode("ascii"))
        except ValueError:
            rounds = _KDF_ROUNDS_USER if user_set else _KDF_ROUNDS
    else:
        rounds = _KDF_ROUNDS_USER if user_set else _KDF_ROUNDS
        _meta_put(root, "kdf_rounds", str(rounds).encode("ascii"))
    cache_key = f"{root}:{rounds}"
    with _LOCK:
        cached = _DERIVED.get(cache_key)
        if cached is not None:
            return cached
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=bytes(salt), iterations=rounds)
    derived = kdf.derive(phrase)
    with _LOCK:
        _DERIVED[cache_key] = derived
    return derived


def _passphrase_bytes() -> bytes:
    path = passphrase_path()
    if path.is_file() and not path.is_symlink():
        raw = path.read_bytes()
        if len(raw) >= 16:
            return raw
    path.parent.mkdir(parents=True, exist_ok=True)
    phrase = os.urandom(32)
    _write_passphrase_file(path, phrase)
    return phrase


def _write_passphrase_file(path: Path, phrase: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, phrase)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _meta_get(root: Path, name: str) -> bytes | None:
    path = root / "secrets.db"
    if not path.is_file() and name != "kdf_salt":
        return None
    with _LOCK:
        conn = _connect(root)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (name,)).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return row[0]


def _meta_put(root: Path, name: str, value: bytes) -> None:
    with _LOCK:
        conn = _connect(root)
        try:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (name, value),
            )
            conn.commit()
        finally:
            conn.close()


def _keyring(op: str, account: str, value: str) -> tuple[str, str]:
    """One keychain call. A timeout or an error is not a missing password.

    Status is ``ok``, ``missing`` (the backend answered and has no entry),
    ``absent`` (there is no keychain), or ``error``.
    """
    box: dict = {}

    def run() -> None:
        try:
            import keyring

            if op == "set":
                keyring.set_password(_SERVICE, account, value)
                box["status"] = "ok"
            elif op == "get":
                found = keyring.get_password(_SERVICE, account)
                if found:
                    box["status"] = "ok"
                    box["value"] = found
                else:
                    box["status"] = "missing"
            else:
                try:
                    keyring.delete_password(_SERVICE, account)
                except Exception:
                    pass
                box["status"] = "ok"
        except Exception as exc:
            box["error"] = exc
            box["status"] = "absent" if _keychain_absent(exc) else "error"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(_KEYRING_WAIT)
    if thread.is_alive():
        return "error", "timed out"
    status = str(box.get("status") or "error")
    if status == "ok":
        return "ok", str(box.get("value") or "")
    if status == "missing":
        return "missing", ""
    detail = str(box.get("error") or "keychain")
    return status, detail


def _keychain_absent(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in {"NoKeyringError", "NoKeyringBackend"}:
        return True
    text = str(exc).lower()
    return "no recommended backend" in text or "no keyring backend" in text


def _seal(key: bytes, account: str, text: str) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    box = AESGCM(key).encrypt(nonce, text.encode("utf-8"), account.encode("utf-8"))
    return nonce, box


def _open(key: bytes, account: str, nonce: bytes, box: bytes) -> tuple[str, bool] | None:
    aes = AESGCM(key)
    try:
        plain = aes.decrypt(nonce, box, account.encode("utf-8"))
        return plain.decode("utf-8"), False
    except Exception:
        pass
    try:
        plain = aes.decrypt(nonce, box, None)
    except Exception:
        return None
    return plain.decode("utf-8"), True


def _reseal(root: Path, key: bytes, account: str, text: str) -> None:
    nonce, box = _seal(key, account, text)
    with _LOCK:
        conn = _connect(root)
        try:
            conn.execute(
                "UPDATE secrets SET nonce = ?, ciphertext = ? WHERE account = ?",
                (nonce, box, account),
            )
            conn.commit()
        finally:
            conn.close()


def _wanted_backend() -> str:
    if _memory():
        return "memory"
    if _file_only():
        return "passphrase"
    return "keyring"


def _recorded_backend(root: Path) -> str:
    raw = _meta_get(root, "backend")
    if not raw:
        return ""
    try:
        return raw.decode("utf-8").strip()
    except Exception:
        return ""


def _has_verifier(root: Path) -> bool:
    raw = _meta_get(root, "verifier")
    return isinstance(raw, (bytes, bytearray)) and len(raw) > 12


def _has_rows(root: Path) -> bool:
    path = root / "secrets.db"
    if not path.is_file():
        return False
    with _LOCK:
        conn = _connect(root)
        try:
            row = conn.execute("SELECT 1 FROM secrets LIMIT 1").fetchone()
        finally:
            conn.close()
    return row is not None


def _opens_existing(root: Path, key: bytes) -> bool:
    if _has_verifier(root):
        return _verifier_ok(root, key)
    if not _has_rows(root):
        return True
    with _LOCK:
        conn = _connect(root)
        try:
            rows = conn.execute("SELECT account, nonce, ciphertext FROM secrets").fetchall()
        finally:
            conn.close()
    opened = 0
    for account, nonce, box in rows:
        if _open(key, str(account), nonce, box) is not None:
            opened += 1
    return opened > 0


def _verifier_ok(root: Path, key: bytes) -> bool:
    raw = _meta_get(root, "verifier")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) <= 12:
        return False
    nonce, box = bytes(raw[:12]), bytes(raw[12:])
    try:
        plain = AESGCM(key).decrypt(nonce, box, _VERIFIER_AD)
    except Exception:
        return False
    return plain == _VERIFIER_PLAIN


def _ensure_verifier(root: Path, key: bytes, backend: str) -> None:
    recorded = _recorded_backend(root)
    if recorded and recorded != backend:
        raise SecretError(
            f"Secrets were sealed with {recorded}. This process is set to {backend}. "
            "The master key was not replaced."
        )
    if _has_verifier(root):
        if _verifier_ok(root, key):
            return
        raise SecretError(
            "The secrets store could not be opened. The master key does not match. "
            "Migration was not started and no secret was written."
        )
    if _has_rows(root) and not _opens_existing(root, key):
        raise SecretError(
            "secrets.db did not open with this master key. No new key was written."
        )
    nonce = os.urandom(12)
    box = AESGCM(key).encrypt(nonce, _VERIFIER_PLAIN, _VERIFIER_AD)
    _meta_put(root, "verifier", nonce + box)
    _meta_put(root, "backend", backend.encode("utf-8"))


def _existing_phrase() -> bytes | None:
    env = (os.environ.get("EASYAGENT_SECRETS_PASSPHRASE") or "").encode("utf-8")
    if env:
        return env
    path = passphrase_path()
    if path.is_file() and not path.is_symlink():
        raw = path.read_bytes()
        if len(raw) >= 16:
            return raw
    return None


def _migration_needed(store) -> bool:
    root = _root(store)
    if (root / "secrets.db").is_file():
        return True
    return bool(_plaintext_jobs(store))


def _plaintext_jobs(store) -> list[tuple[str, str]]:
    jobs: list[tuple[str, str]] = []
    root = _root(store)
    endpoints = getattr(store, "endpoints_path", root / "endpoints.json")
    jobs.extend(_jobs_in(endpoints, "endpoint"))
    jobs.extend(_jobs_in(root / "bridges.json", "bridge"))
    jobs.extend(_jobs_in(root / "connectors.json", "connector"))
    jobs.extend(_jobs_in(root / "mcp.json", "mcp"))
    return jobs


def _jobs_in(path: Path, kind: str) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    found: list[tuple[str, str]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("id") or "")
        if kind == "endpoint":
            key = item.get("api_key") or ""
            if isinstance(key, str) and key.strip():
                found.append((endpoint_account(account_id), key.strip()))
            continue
        for field in _SECRET_FIELDS:
            value = item.get(field)
            if isinstance(value, str) and len(value.strip()) >= 6:
                found.append((f"{kind}:{account_id}:{field}", value.strip()))
        secrets = item.get("secrets")
        if isinstance(secrets, dict):
            for key, value in secrets.items():
                if isinstance(value, str) and value.strip():
                    found.append((f"{kind}:{account_id}:{key}", value.strip()))
    return found


def _backup_dir() -> Path:
    override = (os.environ.get("EASYAGENT_MIGRATION_BACKUP") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".easyagent" / "migration-backup"


def _backup_files(store) -> Path | None:
    root = _root(store)
    candidates = [
        (getattr(store, "endpoints_path", root / "endpoints.json"), "endpoint"),
        (root / "bridges.json", "bridge"),
        (root / "connectors.json", "connector"),
        (root / "mcp.json", "mcp"),
    ]
    chosen = [path for path, kind in candidates if path.is_file() and _jobs_in(path, kind)]
    if not chosen:
        return None
    folder = _backup_dir() / str(time.time_ns())
    _require_outside(root, folder, "migration backup")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    manifest = []
    for path in chosen:
        dest = folder / path.name
        dest.write_bytes(path.read_bytes())
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        manifest.append({"name": path.name, "src": str(path)})
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def _round_trip(store, jobs: list[tuple[str, str]]) -> None:
    for account, value in jobs:
        if get_secret(account, store) != value:
            raise SecretError(
                "A saved secret did not round-trip. The original files were restored. "
                "No empty value was left in their place."
            )


def _remove_backup(folder: Path | None) -> None:
    if folder is None or not folder.is_dir():
        return
    for child in folder.iterdir():
        if child.is_file():
            child.unlink()
    folder.rmdir()


def _restore_backup(folder: Path | None) -> None:
    if folder is None or not folder.is_dir():
        return
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        return
    try:
        rows = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        src = folder / str(item.get("name") or "")
        dest = Path(str(item.get("src") or ""))
        if src.is_file() and dest:
            dest.write_bytes(src.read_bytes())


def scrub_env(store, env: dict | None) -> dict:
    """Drop keychain sessions and any value that is itself a saved secret."""
    blocked = {
        "DBUS_SESSION_BUS_ADDRESS",
        "DBUS_SESSION_BUS_PID",
        "EASYAGENT_SECRETS_PASSPHRASE",
        "EASYAGENT_SECRETS_PASSFILE",
        "EASYAGENT_VAULT_KEY_FILE",
        "EASYAGENT_KEYRING",
    }
    secrets: list[str] = []
    try:
        secrets = [item for item in secret_values(store) if len(item) >= 6]
    except Exception:
        secrets = []
    cleaned: dict = {}
    for key, value in (env or {}).items():
        if key in blocked:
            continue
        upper = key.upper()
        if upper.endswith("_API_KEY") or upper.endswith("_TOKEN") or upper.endswith("_PASSWORD") or "SECRET" in upper:
            continue
        text = str(value)
        if any(secret in text for secret in secrets):
            continue
        cleaned[key] = value
    return cleaned
