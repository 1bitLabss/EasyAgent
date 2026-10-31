"""Scheduler truth, packaged zones, and a python type grant."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from easyagent.subagent import automatic_ask_allowed


def test_a_local_child_may_think_longer_than_the_cloud_read():
    from easyagent.app import _quiet_model_wait
    from easyagent.generation import LOCAL_FIRST_TOKEN_SECONDS

    assert _quiet_model_wait("http://127.0.0.1:18963/v1") >= LOCAL_FIRST_TOKEN_SECONDS
    assert _quiet_model_wait("http://127.0.0.1:18963/v1") > 150
    assert _quiet_model_wait("https://api.example.com/v1") < LOCAL_FIRST_TOKEN_SECONDS


def test_an_unrequested_long_statement_is_not_forwarded():
    from easyagent.subagent import consult_target, side_consult

    aside = "subsonic ammo and a surpresser will be like super quiet"
    assert side_consult(aside, ("Helper2", "Helper9"), True) is None
    assert side_consult("thanks!", ("Helper9",), True) is None
    assert side_consult("ask Helper9 to check the noise", ("Helper9",), True) == ("Helper9", "check the noise")
    asked = side_consult("ask another bot whether it is quiet", ("Helper9",), True)
    assert asked is not None and asked[0] == "Helper9"
    assert side_consult("ask Helper9 to check the noise", ("Helper9",), False) is None
    assert consult_target("ask Helper9 to check the noise", ("Helper9",)) == ("Helper9", "check the noise")


def test_a_full_queue_keeps_the_closing_signal():
    import asyncio

    from easyagent.boundq import offer

    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    offer(queue, ("text", "one"))
    offer(queue, ("text", " two"))
    offer(queue, ("text", " three"))
    offer(queue, ("end", ""), final=True)
    kinds = []
    body = ""
    while not queue.empty():
        kind, payload = queue.get_nowait()
        kinds.append(kind)
        if kind == "text":
            body += payload
    assert kinds[-1] == "end"
    assert body == "one two three"
    viewer: asyncio.Queue = asyncio.Queue(maxsize=2)
    for piece in ("a", "b", "c"):
        offer(viewer, piece)
    offer(viewer, None, final=True)
    got = []
    while not viewer.empty():
        got.append(viewer.get_nowait())
    assert got[-1] is None
    assert len(got) <= 2
    assert automatic_ask_allowed("How many boats are at the east pier?") is False
    assert automatic_ask_allowed("what is the capital of France?") is False
    assert automatic_ask_allowed("The notes mention Helper3.", ("Helper3",)) is False


def test_a_finished_bot_does_not_keep_the_lane():
    import asyncio

    from easyagent import gate

    async def scenario():
        gate.reset_lanes()
        token = gate.bind_connection({"id": "local", "name": "qa-stub", "max_parallel": 1}, "Hog")
        seat = await gate.reserve()
        await seat.acquire()
        assert gate.lane_busy("local")
        assert gate.release_bot("Hog") == 1
        assert gate.lane_busy("local") is False
        gate.reset_connection(token)

    asyncio.run(scenario())


def test_an_expired_lane_lease_lets_the_next_bot_start(monkeypatch):
    import asyncio
    import time

    from easyagent import gate

    async def scenario():
        gate.reset_lanes()
        monkeypatch.setenv("EASYAGENT_LANE_LEASE_SECONDS", "5")
        held = gate.bind_connection({"id": "local", "name": "qa-stub", "max_parallel": 1}, "Hog")
        seat = await gate.reserve()
        await seat.acquire()
        seat.held_at = time.monotonic() - 30
        nxt = gate.bind_connection({"id": "local", "name": "qa-stub", "max_parallel": 1}, "Tick")
        follower = await gate.reserve()
        assert follower.holding
        await follower.release()
        gate.reset_connection(held)
        gate.reset_connection(nxt)
        assert gate.lane_busy("local") is False

    asyncio.run(scenario())


def test_an_env_prefix_stays_on_the_approval_card(tmp_path):
    from easyagent.safety import classify
    from easyagent.store import Store
    from easyagent.tools import ToolRequest

    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    verdict = classify(store, ToolRequest(kind="shell", command="FOO=1 python a.py"), bot["id"])
    assert "FOO=1 python a.py" in (verdict.detail or "")


def test_packaged_tzdata_converts_chicago_when_zoneinfo_cannot(monkeypatch):
    import easyagent.schedule as schedule

    def missing(name):
        raise ZoneInfoNotFoundError(str(name))

    missing.from_file = ZoneInfo.from_file
    monkeypatch.setattr(schedule, "ZoneInfo", missing)
    zone = schedule.load_zone("America/Chicago")
    assert zone is not None
    moment = datetime(2026, 10, 31, 9, 0, tzinfo=zone)
    assert moment.astimezone(timezone.utc) == datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc)
    try:
        schedule.compile_routine({
            "name": "Odd",
            "prompt": "Nothing.",
            "once": "2027-03-14T02:30",
            "timezone": "America/Chicago",
        })
    except schedule.ScheduleError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("the spring-forward gap was stored")


def test_a_python_type_grant_covers_later_python_not_curl(tmp_path):
    from easyagent import turn as turn_mod
    from easyagent.safety import _remember_type, _type_covers, command_family
    from easyagent.store import Store

    assert command_family("python3 a.py") == "python"
    assert command_family('python3 -c "print(7*6)"') == "python"
    assert command_family("/tmp/work/b.py") == "python"
    assert command_family("curl -s http://example/price") == "curl"
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    slot = turn_mod.slot_for(store, chat["id"])
    slot.bot_id = bot["id"]
    slot.chat_id = chat["id"]
    slot.store = store
    token = turn_mod._slot.set(slot)
    try:
        assert _remember_type(store, bot["id"], "python") is True
        assert _type_covers(store, bot["id"], "python3 b.py") is True
        assert _type_covers(store, bot["id"], 'python3 -c "print(1)"') is True
        assert _type_covers(store, bot["id"], "curl -s http://example/price") is False
    finally:
        turn_mod._slot.reset(token)
