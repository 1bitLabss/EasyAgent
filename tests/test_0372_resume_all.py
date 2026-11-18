"""Night-wave regression: resume_all replaces the dead paused_all host flag.

pause_all used to stamp host["paused_all"] = True — a write-only flag nothing
ever read and stale the moment one schedule was unpaused. The flag is gone and
resume_all derives its work from the schedule rows themselves, never a host
flag.
"""

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.schedule import pause_all, resume_all
from tests.test_schedule import put_schedule, seed


def _second_bot(store, endpoint):
    return store.add_bot(name="Bo", endpoint_id=endpoint["id"], model=None)


def test_resume_all_unpauses_every_paused_schedule_across_bots(tmp_path):
    store, bot, endpoint = seed(tmp_path)
    other = _second_bot(store, endpoint)
    put_schedule(store, bot, id="11111111-1111-1111-1111-111111111111", kind="interval", every_minutes=5, name="Watch", paused=True)
    put_schedule(store, bot, id="22222222-2222-2222-2222-222222222222", kind="interval", every_minutes=10, name="Quiet", paused=True)
    put_schedule(store, other, id="33333333-3333-3333-3333-333333333333", kind="interval", every_minutes=15, name="Bo Watch", paused=True)
    put_schedule(store, other, id="44444444-4444-4444-4444-444444444444", kind="interval", every_minutes=20, name="Bo Idle", paused=False)

    resumed = resume_all(store)
    assert resumed == 3
    for bot_row in (bot, other):
        for row in store.list_schedules(bot_row["id"]):
            assert not row.get("paused")


def test_pause_all_no_longer_writes_the_paused_all_host_flag(tmp_path):
    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=5, name="Watch")
    host_path = store.root / "host.json"
    before = host_path.read_bytes() if host_path.is_file() else None
    paused = pause_all(store)
    assert paused == 1
    assert "paused_all" not in store.read_host()
    # pause_all must not touch host.json at all any more.
    after = host_path.read_bytes() if host_path.is_file() else None
    assert after == before


def test_save_host_drops_the_paused_all_key(tmp_path):
    from easyagent.store import Store

    store = Store(tmp_path)
    store.ensure()
    saved = store.save_host({"background": True, "paused_all": True})
    assert "paused_all" not in saved
    assert "paused_all" not in store.read_host()


def test_resume_all_endpoint_reports_the_resumed_count(tmp_path):
    store, bot, endpoint = seed(tmp_path)
    other = _second_bot(store, endpoint)
    put_schedule(store, bot, id="11111111-1111-1111-1111-111111111111", kind="interval", every_minutes=5, name="Watch", paused=True)
    put_schedule(store, other, id="33333333-3333-3333-3333-333333333333", kind="interval", every_minutes=15, name="Bo Watch", paused=True)
    client = TestClient(create_app(tmp_path))
    body = client.post("/api/routines/resume-all").json()
    assert body == {"resumed": 2}
    for bot_row in (bot, other):
        for row in store.list_schedules(bot_row["id"]):
            assert not row.get("paused")


def test_resume_all_on_an_unpaused_set_changes_nothing(tmp_path):
    store, bot, _endpoint = seed(tmp_path)
    schedule = put_schedule(store, bot, kind="interval", every_minutes=5, name="Watch")
    before = store.list_schedules(bot["id"])
    assert resume_all(store) == 0
    after = store.list_schedules(bot["id"])
    assert after == before
    assert after[0]["id"] == schedule["id"]
    assert not after[0].get("paused")


def test_pause_and_resume_all_keep_next_run_honest(tmp_path):
    # The per-schedule pause endpoint nulls next_run on pause and stamps it on
    # resume; the all-variants must match or the jobs panel shows a stale past
    # "next run" for every resumed routine until its next slot comes due.
    from datetime import datetime

    store, bot, _endpoint = seed(tmp_path)
    put_schedule(store, bot, kind="interval", every_minutes=5, name="Watch")
    rows = store.list_schedules(bot["id"])
    rows[0]["next_run"] = datetime.now().astimezone().isoformat()
    store.save_schedules(bot["id"], rows)

    assert pause_all(store) == 1
    assert store.list_schedules(bot["id"])[0]["next_run"] is None

    assert resume_all(store) == 1
    resumed = store.list_schedules(bot["id"])[0]
    assert resumed["next_run"] is not None
    when = datetime.fromisoformat(resumed["next_run"])
    assert when > datetime.now().astimezone()
