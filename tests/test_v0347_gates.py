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


def test_a_side_line_during_a_reply_asks_the_one_other_bot():
    from easyagent.subagent import side_consult

    text = "subsonic ammo and a surpresser will be like super quiet"
    assert side_consult(text, ("LLM3",), True) == ("LLM3", text)
    assert side_consult(text, ("Helper2", "LLM3M"), True) == ("LLM3M", text)
    assert side_consult(text, ("LLM3",), False) is None
    assert side_consult(text, (), True) is None
    assert side_consult("How many boats are at the east pier?", ("LLM3",), False) is None


def test_a_question_does_not_ask_another_bot_on_its_own():
    assert automatic_ask_allowed("How many boats are at the east pier?") is False
    assert automatic_ask_allowed("what is the capital of France?") is False
    assert automatic_ask_allowed("The notes mention Helper3.", ("Helper3",)) is False


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
