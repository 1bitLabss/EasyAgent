"""Per-bot sub-counters inside today's token rollup.

The rollup file (usage_today.json) keeps its top-level totals exactly as
before. Each bot additionally gets a sub-counter under "bots", and the
/api/usage/today endpoint can serve either the whole map or one bot."""

import json
from datetime import datetime

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.store import Store


def today() -> str:
    return datetime.now().date().isoformat()


def stamp_file(store: Store) -> dict:
    return json.loads(store.usage_today_path().read_text(encoding="utf-8"))


def rewrite_file(store: Store, data: dict) -> None:
    store.usage_today_path().write_text(json.dumps(data), encoding="utf-8")


def test_per_bot_accumulation(tmp_path):
    store = Store(tmp_path)
    store.note_usage_today(100, 20, "alpha")
    store.note_usage_today(50, 10, "alpha")
    store.note_usage_today(7, 3, "bravo")
    usage = store.usage_today()
    assert usage["date"] == today()
    assert usage["bots"]["alpha"] == {"prompt_tokens": 150, "completion_tokens": 30}
    assert usage["bots"]["bravo"] == {"prompt_tokens": 7, "completion_tokens": 3}


def test_total_equals_sum_of_bots(tmp_path):
    store = Store(tmp_path)
    store.note_usage_today(100, 20, "alpha")
    store.note_usage_today(7, 3, "bravo")
    store.note_usage_today(5, 5)  # no owner, still in the total
    usage = store.usage_today()
    bot_prompt = sum(b["prompt_tokens"] for b in usage["bots"].values())
    bot_completion = sum(b["completion_tokens"] for b in usage["bots"].values())
    assert usage["prompt_tokens"] == bot_prompt + 5
    assert usage["completion_tokens"] == bot_completion + 5
    assert usage["prompt_tokens"] == 112
    assert usage["completion_tokens"] == 28


def test_day_rollover_clears_bots(tmp_path):
    store = Store(tmp_path)
    store.note_usage_today(10, 1, "alpha")
    data = stamp_file(store)
    data["date"] = "2000-01-01"
    rewrite_file(store, data)
    usage = store.usage_today()
    assert usage == {"date": today(), "prompt_tokens": 0, "completion_tokens": 0, "bots": {}}
    # The next stamp starts the day clean, old bots included.
    store.note_usage_today(5, 2, "beta")
    usage = store.usage_today()
    assert usage["bots"] == {"beta": {"prompt_tokens": 5, "completion_tokens": 2}}
    assert usage["prompt_tokens"] == 5


def test_bot_id_none_counts_total_only(tmp_path):
    store = Store(tmp_path)
    store.note_usage_today(10, 5, None)
    usage = store.usage_today()
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 5
    assert usage["bots"] == {}


def test_malformed_bot_id_still_counts_total(tmp_path):
    store = Store(tmp_path)
    store.note_usage_today(4, 1, "")
    store.note_usage_today(4, 1, 123)  # type: ignore[arg-type]
    usage = store.usage_today()
    assert usage["prompt_tokens"] == 8
    assert usage["completion_tokens"] == 2
    assert usage["bots"] == {}


def test_corrupt_bots_section_degrades(tmp_path):
    store = Store(tmp_path)
    store.note_usage_today(10, 2, "alpha")
    data = stamp_file(store)
    data["bots"] = "nonsense"
    rewrite_file(store, data)
    usage = store.usage_today()
    assert usage["bots"] == {}
    assert usage["prompt_tokens"] == 10  # the total survives the broken section
    # Writing through a corrupt section repairs it instead of raising.
    store.note_usage_today(1, 1, "beta")
    assert store.usage_today()["bots"] == {"beta": {"prompt_tokens": 1, "completion_tokens": 1}}


def test_corrupt_bot_entry_degrades(tmp_path):
    store = Store(tmp_path)
    store.note_usage_today(10, 2, "alpha")
    data = stamp_file(store)
    data["bots"]["alpha"] = "junk"
    rewrite_file(store, data)
    assert store.usage_today()["bots"] == {}
    store.note_usage_today(3, 1, "alpha")
    assert store.usage_today()["bots"] == {"alpha": {"prompt_tokens": 3, "completion_tokens": 1}}


# ------------------------------------------------------------- endpoint


def client_for(tmp_path):
    return TestClient(create_app(tmp_path))


def test_endpoint_without_bot_id_includes_bots(tmp_path):
    store = Store(tmp_path)
    store.note_usage_today(40, 8, "alpha")
    client = client_for(tmp_path)
    payload = client.get("/api/usage/today").json()
    assert payload["date"] == today()
    assert payload["prompt_tokens"] == 40
    assert payload["completion_tokens"] == 8
    assert payload["bots"] == {"alpha": {"prompt_tokens": 40, "completion_tokens": 8}}


def test_endpoint_with_bot_id_returns_that_pair(tmp_path):
    store = Store(tmp_path)
    store.note_usage_today(40, 8, "alpha")
    store.note_usage_today(6, 2, "bravo")
    client = client_for(tmp_path)
    payload = client.get("/api/usage/today", params={"bot_id": "alpha"}).json()
    assert payload == {"date": today(), "prompt_tokens": 40, "completion_tokens": 8}


def test_endpoint_with_unseen_bot_id_reads_zeros(tmp_path):
    store = Store(tmp_path)
    store.note_usage_today(40, 8, "alpha")
    client = client_for(tmp_path)
    payload = client.get("/api/usage/today", params={"bot_id": "ghost"}).json()
    assert payload == {"date": today(), "prompt_tokens": 0, "completion_tokens": 0}
    # The total is untouched by the per-bot read.
    assert client.get("/api/usage/today").json()["prompt_tokens"] == 40
