"""Shared fixtures. A page test must not call the public web for every image."""

import pytest


@pytest.fixture(autouse=True)
def remote_assets_answer(monkeypatch):
    async def ok(_url: str) -> int:
        return 200

    monkeypatch.setattr("easyagent.tools._asset_status", ok)
