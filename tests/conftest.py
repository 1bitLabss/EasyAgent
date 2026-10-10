"""Shared fixtures. A page test must not call the public web for every image."""

import tempfile
from pathlib import Path

import pytest

# The temp directory pytest already chose. Product scratch moves off it, but
# file tests still write beside tmp_path and that tree must stay an allowed root.
_PYTEST_TEMP = Path(tempfile.gettempdir()).resolve()


@pytest.fixture(autouse=True)
def remote_assets_answer(monkeypatch):
    async def ok(_url: str) -> int:
        return 200

    monkeypatch.setattr("easyagent.tools._asset_status", ok)


@pytest.fixture(autouse=True)
def safety_starts_clean():
    from easyagent.safety import reset_for_tests

    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture(autouse=True)
def isolate_user_dirs(monkeypatch, tmp_path):
    """The suite must not touch a real profile, data dir, or temp folder.

    These folders sit beside the test's tmp directory. A store opened on tmp_path
    must not contain the fake home, or a normal command looks like a data-dir read.
    """
    real_home = Path.home()
    browsers = real_home / ".cache" / "ms-playwright"
    if browsers.is_dir():
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    base = tmp_path.parent / f"iso-{tmp_path.name}"
    home = base / "home"
    local = home / "AppData" / "Local"
    roaming = home / "AppData" / "Roaming"
    temp = base / "temp"
    data = base / "easyagent-data"
    for folder in (home, local, roaming, temp, data):
        folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.setenv("TMP", str(temp))
    monkeypatch.setenv("EASYAGENT_DATA", str(data))
    tempfile.tempdir = None

    import easyagent.safety as safety

    original = safety._workspace_roots

    def roots(store, bot_id, user_text):
        found = original(store, bot_id, user_text)
        if _PYTEST_TEMP not in found:
            found.append(_PYTEST_TEMP)
        return found

    monkeypatch.setattr(safety, "_workspace_roots", roots)
    yield
    tempfile.tempdir = None
    import shutil

    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True)
def checks_use_an_opt_in(monkeypatch, tmp_path):
    """Existing tests count model calls. A check is on only when a test asks for it."""
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    monkeypatch.setenv("EASYAGENT_CONTAIN_LEDGER", str(tmp_path / "contain.json"))
    monkeypatch.setenv("EASYAGENT_CHECK", "0")
    monkeypatch.setenv("EASYAGENT_LEARN", "0")
    monkeypatch.setenv("EASYAGENT_ROLLING", "0")
    monkeypatch.setenv("EASYAGENT_NIGHTLY", "0")
    monkeypatch.setenv("EASYAGENT_SAFETY_REVIEW", "0")
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "0.05")


@pytest.fixture(autouse=True)
def fast_model_retry(monkeypatch):
    """The retry window is real minutes. Tests move the clock instead of sleeping."""
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    async def pause(seconds):
        from easyagent import turn as turn_mod

        clock["t"] += max(0.0, float(seconds or 0))
        turn_mod.raise_if_cancelled()

    monkeypatch.setattr("easyagent.retry.now", now)
    monkeypatch.setattr("easyagent.retry.pause", pause)
