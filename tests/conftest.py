"""Shared fixtures. A page test must not call the public web for every image."""

import pytest


@pytest.fixture(autouse=True)
def remote_assets_answer(monkeypatch):
    async def ok(_url: str) -> int:
        return 200

    monkeypatch.setattr("easyagent.tools._asset_status", ok)


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
