"""A routine's time limit cancels a model call that is still running."""

import asyncio

import pytest

from easyagent import llm
from easyagent.schedule import _Stopped, _routine_loop


def test_a_hanging_model_call_stops_at_the_routine_limit(monkeypatch):
    async def hang(**_kwargs):
        await asyncio.sleep(30)
        return "late"

    monkeypatch.setattr(llm, "complete", hang)
    monkeypatch.setenv("EASYAGENT_ROUTINE_SECONDS", "1")

    async def run() -> float:
        started = asyncio.get_running_loop().time()
        with pytest.raises(_Stopped, match="time limit"):
            await _routine_loop(
                None,
                {"model": "m"},
                {"base_url": "http://127.0.0.1:9/v1", "model": "m"},
                [{"role": "user", "content": "go"}],
                asyncio.Event(),
            )
        return asyncio.get_running_loop().time() - started

    elapsed = asyncio.run(run())
    assert elapsed < 5
