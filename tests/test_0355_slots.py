"""0.3.55 regressions: a finished chat frees the model line; env prefixes stay visible.

A scheduled or background job must not wait behind a seat that a finished chat
left behind. A detached viewer must not leave a producer stream open. An
approval card shows the full command, env prefix included, without changing
what was judged.
"""

import asyncio
import time


def test_a_finished_chat_frees_the_line_for_a_scheduled_job_repeatedly(tmp_path):
    """Five rounds: a seat from a finished chat never delays a background job."""
    from easyagent import gate
    from easyagent import turn as turn_mod
    from easyagent.store import Store

    async def one_round(round_index):
        gate.reset_lanes()
        store = Store(tmp_path / f"r{round_index}")
        store.ensure()
        endpoint = store.add_endpoint(
            name=f"local{round_index}", base_url=f"http://127.0.0.1:9{round_index}/v1", api_key=None
        )
        bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
        chat = store.create_chat(bot["id"])
        slot = turn_mod.slot_for(store, chat["id"])
        slot.bot_id = bot["id"]
        slot.chat_id = chat["id"]
        slot.store = store
        token = turn_mod._slot.set(slot)
        conn = gate.bind_connection(
            {"id": endpoint["id"], "name": "local", "max_parallel": 1}, "Ada"
        )
        try:
            seat = await gate.reserve()
            await seat.acquire()
            assert gate.lane_busy(endpoint["id"])
            # The chat finished: its run is no longer running. Age the seat past
            # the one-second grace so the sweep sees a finished chat, not a race.
            seat.held_at = time.monotonic() - 2.0
            started = time.monotonic()
            job = await gate.reserve(step_aside=True, background=True)
            waited = time.monotonic() - started
            assert job.holding, "the scheduled job waited behind a finished chat's seat"
            assert waited < 0.5, f"the scheduled job waited {waited:.2f}s"
            await job.release()
        finally:
            gate.reset_connection(conn)
            turn_mod._slot.reset(token)
            gate.reset_lanes()

    for index in range(5):
        asyncio.run(one_round(index))


def test_detaching_from_the_heartbeat_closes_the_source_stream():
    """When the viewer detaches, _heartbeat's finally must aclose its generator.

    Two detach moments: mid-read on a quiet socket (the pending task is
    cancelled) and between items (no task pending — only the aclose reaches
    the source). The second is the one the old finally left open.
    """
    from easyagent.app import _heartbeat

    async def scenario():
        # Detach mid-read: the socket goes quiet while a read is pending.
        closed_mid = {"done": False}

        async def quiet_source():
            try:
                yield "data: one\n\n"
                await asyncio.sleep(30)
                yield "data: two\n\n"
            finally:
                closed_mid["done"] = True

        stream = _heartbeat(quiet_source())
        assert await stream.__anext__() == "data: one\n\n"
        await asyncio.sleep(0)  # let the heartbeat arm its pending read
        await stream.aclose()
        assert closed_mid["done"], "the source stream was never closed on mid-read detach"

        # Detach between items: nothing is pending; only aclose reaches the source.
        closed_between = {"done": False}

        async def quick_source():
            try:
                yield "data: one\n\n"
                yield "data: two\n\n"
                await asyncio.sleep(30)
                yield "data: three\n\n"
            finally:
                closed_between["done"] = True

        stream = _heartbeat(quick_source())
        assert await stream.__anext__() == "data: one\n\n"
        assert await stream.__anext__() == "data: two\n\n"
        await stream.aclose()
        assert closed_between["done"], "the source stream was never closed on between-items detach"

    asyncio.run(scenario())


def test_finish_turn_releases_the_bots_seat(tmp_path):
    """_finish_turn must drop the lane seat the bot still holds."""
    from easyagent import gate
    from easyagent.app import _finish_turn
    from easyagent.store import Store

    async def scenario():
        gate.reset_lanes()
        store = Store(tmp_path)
        store.ensure()
        endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
        bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
        chat = store.create_chat(bot["id"])
        conn = gate.bind_connection(
            {"id": endpoint["id"], "name": "local", "max_parallel": 1}, "Ada"
        )
        try:
            seat = await gate.reserve()
            await seat.acquire()
            assert gate.lane_busy(endpoint["id"])
            await _finish_turn(store, bot["id"], chat["id"])
            assert gate.lane_busy(endpoint["id"]) is False
        finally:
            gate.reset_connection(conn)
            gate.reset_lanes()

    asyncio.run(scenario())


def test_an_env_prefix_shows_on_the_card_without_changing_the_verdict(tmp_path):
    """FOO=1 python a.py shows whole on the card; tier/rule/fingerprint unchanged."""
    from easyagent.safety import classify
    from easyagent.store import Store
    from easyagent.tools import ToolRequest

    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    prefixed = classify(store, ToolRequest(kind="shell", command="FOO=1 python a.py"), bot["id"])
    plain = classify(store, ToolRequest(kind="shell", command="python a.py"), bot["id"])
    assert "FOO=1 python a.py" in (prefixed.detail or "")
    # The safety decision is untouched: same tier, rule and fingerprint, so an
    # approval granted for the card still matches what was actually checked.
    assert prefixed.tier == plain.tier
    assert prefixed.rule == plain.rule
    assert prefixed.fingerprint == plain.fingerprint


def test_a_command_without_an_env_prefix_keeps_its_detail_untouched(tmp_path):
    from easyagent.safety import classify
    from easyagent.store import Store
    from easyagent.tools import ToolRequest

    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    for command in ("rm -rf /tmp/x", "python a.py", "curl -s http://example/price"):
        verdict = classify(store, ToolRequest(kind="shell", command=command), bot["id"])
        assert (verdict.detail or "").startswith(command.split()[0])
        assert "FOO=" not in (verdict.detail or "")
