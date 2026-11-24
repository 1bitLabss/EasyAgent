"""Per-task judge routing: the goal judge can point at its own connection.

A bot record may carry ``task_routes: {"goal_judge": {"endpoint_id": ...,
"model": ..., "max_tokens": ..., "timeout_seconds": ...}}``. Resolution
priority in goaljudge.call_judge: explicit task route > the bot's main
endpoint. An empty or missing route inherits the main connection exactly
(Hermes' provider:auto semantics). An unknown route endpoint id falls back to
the main connection with a one-line note in the judge log — it never crashes.
Temperature stays hardcoded 0; max_tokens and timeout are overridable per
route (defaults 4096 / 30 s). The route's own saved fallbacks ride the
existing fallbacks plumbing. GET/PUT /api/bots/{id}/judge-route manages it.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from easyagent import goaljudge
from easyagent.app import create_app
from easyagent.goaljudge import JUDGE_MAX_TOKENS, JUDGE_TIMEOUT
from easyagent.store import Store


def _store(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    main = store.add_endpoint(name="main", base_url="http://127.0.0.1:9/v1", api_key=None)
    cheap = store.add_endpoint(name="cheap", base_url="http://127.0.0.1:8/v1", api_key=None, model="small")
    bot = store.add_bot(name="Ada", endpoint_id=main["id"], model="big")
    return store, main, cheap, bot


def _ask(store, bot_id, seen):
    async def fake_complete(**kwargs):
        seen.append(kwargs)
        return '{"verdict": "done", "reason": "ok"}'

    import easyagent.llm as llm_mod

    original = llm_mod.complete
    llm_mod.complete = fake_complete
    try:
        out = asyncio.run(
            goaljudge.call_judge(
                store, bot_id, goal="send the summary", answer="I sent it.", plan={"steps": []}, children=0
            )
        )
    finally:
        llm_mod.complete = original
    assert out[0] == "done"
    return seen[-1]


def test_missing_route_inherits_the_main_connection(tmp_path):
    """No task_routes at all: today's behavior, unchanged."""
    store, main, cheap, bot = _store(tmp_path)
    call = _ask(store, bot["id"], [])
    assert call["base_url"] == "http://127.0.0.1:9/v1"
    assert call["model"] == "big"
    assert call["temperature"] == 0
    assert call["max_tokens"] == JUDGE_MAX_TOKENS == 4096
    assert call["timeout"] == JUDGE_TIMEOUT == 30.0
    assert call["fallbacks"] is None


def test_empty_route_inherits_the_main_connection(tmp_path):
    store, main, cheap, bot = _store(tmp_path)
    store.set_task_route(bot["id"], "goal_judge", {})
    call = _ask(store, bot["id"], [])
    assert call["base_url"] == "http://127.0.0.1:9/v1"
    assert call["model"] == "big"


def test_explicit_route_beats_the_main_endpoint(tmp_path):
    store, main, cheap, bot = _store(tmp_path)
    store.set_task_route(bot["id"], "goal_judge", {"endpoint_id": cheap["id"]})
    call = _ask(store, bot["id"], [])
    assert call["base_url"] == "http://127.0.0.1:8/v1"
    # No route model: the route connection's own model answers, not the bot's.
    assert call["model"] == "small"


def test_route_model_wins_over_both(tmp_path):
    store, main, cheap, bot = _store(tmp_path)
    store.set_task_route(bot["id"], "goal_judge", {"endpoint_id": cheap["id"], "model": "cheapest"})
    call = _ask(store, bot["id"], [])
    assert call["base_url"] == "http://127.0.0.1:8/v1"
    assert call["model"] == "cheapest"


def test_unknown_route_endpoint_falls_back_to_main(tmp_path):
    """A stale route id is a note in the log, not a crash."""
    store, main, cheap, bot = _store(tmp_path)
    store.set_task_route(bot["id"], "goal_judge", {"endpoint_id": "00000000-0000-4000-8000-000000000000"})
    call = _ask(store, bot["id"], [])
    assert call["base_url"] == "http://127.0.0.1:9/v1"
    assert call["model"] == "big"
    assert call["max_tokens"] == JUDGE_MAX_TOKENS  # route ignored with its overrides


def test_route_overrides_max_tokens_and_timeout(tmp_path):
    store, main, cheap, bot = _store(tmp_path)
    store.set_task_route(
        bot["id"], "goal_judge", {"endpoint_id": cheap["id"], "max_tokens": 512, "timeout_seconds": 5}
    )
    call = _ask(store, bot["id"], [])
    assert call["max_tokens"] == 512
    assert call["timeout"] == 5.0
    assert call["temperature"] == 0  # never overridable


def test_route_fallbacks_ride_the_existing_plumbing(tmp_path):
    store, main, cheap, bot = _store(tmp_path)
    third = store.add_endpoint(name="third", base_url="http://127.0.0.1:7/v1", api_key=None)
    store.update_endpoint(cheap["id"], fallback_ids=[third["id"]], fallback_ids_set=True)
    store.set_task_route(bot["id"], "goal_judge", {"endpoint_id": cheap["id"]})
    call = _ask(store, bot["id"], [])
    assert call["base_url"] == "http://127.0.0.1:8/v1"
    assert call["fallbacks"] and call["fallbacks"][0]["base_url"] == "http://127.0.0.1:7/v1"


def test_resolve_judge_endpoint_priority(tmp_path):
    store, main, cheap, bot = _store(tmp_path)
    endpoint, route = goaljudge.resolve_judge_endpoint(store, store.get_bot(bot["id"]))
    assert endpoint["id"] == main["id"] and route == {}
    store.set_task_route(bot["id"], "goal_judge", {"endpoint_id": cheap["id"], "max_tokens": 64})
    endpoint, route = goaljudge.resolve_judge_endpoint(store, store.get_bot(bot["id"]))
    assert endpoint["id"] == cheap["id"] and route["max_tokens"] == 64


# ── the settings API ──────────────────────────────────────────────────


def test_judge_route_api_round_trip(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    cheap = client.post(
        "/api/endpoints", json={"name": "cheap", "base_url": "http://127.0.0.1:8/v1"}
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()

    # No route yet: the judge inherits the main connection.
    assert client.get(f"/api/bots/{bot['id']}/judge-route").json() == {"route": {}}

    saved = client.put(
        f"/api/bots/{bot['id']}/judge-route",
        json={"route": {"endpoint_id": cheap["id"], "model": "small", "max_tokens": 1024, "timeout_seconds": 10}},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "route": {"endpoint_id": cheap["id"], "model": "small", "max_tokens": 1024, "timeout_seconds": 10.0}
    }
    assert client.get(f"/api/bots/{bot['id']}/judge-route").json() == saved.json()
    # The route survives on the bot record.
    assert app.state.store.get_bot(bot["id"])["task_routes"]["goal_judge"]["endpoint_id"] == cheap["id"]

    # An empty route clears it: back to inheriting the main connection.
    assert client.put(f"/api/bots/{bot['id']}/judge-route", json={"route": {}}).json() == {"route": {}}
    assert client.get(f"/api/bots/{bot['id']}/judge-route").json() == {"route": {}}


def test_judge_route_api_validation(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()

    assert client.get("/api/bots/nosuchbot0/judge-route").status_code == 404
    assert client.put("/api/bots/nosuchbot0/judge-route", json={"route": {}}).status_code == 404
    # The route needs a saved connection.
    bad = client.put(
        f"/api/bots/{bot['id']}/judge-route", json={"route": {"endpoint_id": "00000000-0000-4000-8000-000000000000"}}
    )
    assert bad.status_code == 400
    missing = client.put(f"/api/bots/{bot['id']}/judge-route", json={"route": {"model": "small"}})
    assert missing.status_code == 400
    numbers = client.put(
        f"/api/bots/{bot['id']}/judge-route",
        json={"route": {"endpoint_id": endpoint["id"], "max_tokens": 0}},
    )
    assert numbers.status_code == 400
    assert client.get(f"/api/bots/{bot['id']}/judge-route").json() == {"route": {}}
