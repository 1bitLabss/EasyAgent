"""A bot can hide shared skills. Its own skills are never hidden.

Skills saved to the shared folder show for every bot. A bot may opt out of
specific shared skills by name; the opt-out lives in the bot's own folder and
touches only shared rows. A broken or missing hidden list hides nothing.
"""

import pytest
from fastapi.testclient import TestClient

from easyagent.app import _skills_for_prompt, create_app
from easyagent.botskills import SkillError, catalog, read_skill, visible_skills, write_skill
from easyagent.store import Store


def _world(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="«redacted:sk_live_…»")
    ada = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    bob = store.add_bot(name="Bob", endpoint_id=endpoint["id"], model=None)
    store.save_skill({"name": "shared-note", "description": "A shared note", "body": "Shared body."})
    return store, ada, bob


def _names(store, bot_id):
    return [row["name"] for row in visible_skills(store, bot_id)]


def test_hidden_shared_skill_shows_for_one_bot_not_the_other(tmp_path):
    store, ada, bob = _world(tmp_path)
    assert "shared-note" in _names(store, ada["id"])  # baseline
    store.write_skills_hidden(ada["id"], ["shared-note"])
    assert "shared-note" not in _names(store, ada["id"])
    assert "shared-note" not in catalog(store, ada["id"])
    assert "shared-note" not in _skills_for_prompt(store, ada["id"], "what next")
    assert "shared-note" in _names(store, bob["id"])  # the other bot still sees it
    assert "shared-note" in catalog(store, bob["id"])


def test_hidden_name_matches_regardless_of_slug_form(tmp_path):
    store, ada, _ = _world(tmp_path)
    store.write_skills_hidden(ada["id"], ["Shared Note"])  # not the slug form
    assert "shared-note" not in _names(store, ada["id"])


def test_read_skill_refuses_a_hidden_shared_skill(tmp_path):
    store, ada, bob = _world(tmp_path)
    store.write_skills_hidden(ada["id"], ["shared-note"])
    with pytest.raises(SkillError, match="not saved"):
        read_skill(store, ada["id"], "shared-note")
    heard = read_skill(store, bob["id"], "shared-note")  # same skill, not hidden there
    assert "Shared body." in heard


def test_opt_out_never_touches_a_bot_local_skill(tmp_path):
    store, ada, _ = _world(tmp_path)
    write_skill(store, ada["id"], "shared-note", "Ada keeps her own copy.")
    store.write_skills_hidden(ada["id"], ["shared-note"])
    assert "shared-note" in _names(store, ada["id"])  # local row stays
    heard = read_skill(store, ada["id"], "shared-note")
    assert "Ada keeps her own copy." in heard


def test_no_hidden_file_is_the_old_behavior(tmp_path):
    store, ada, _ = _world(tmp_path)
    assert store.read_skills_hidden(ada["id"]) == set()
    assert "shared-note" in _names(store, ada["id"])
    assert "Shared body." in read_skill(store, ada["id"], "shared-note")


def test_a_broken_hidden_file_hides_nothing(tmp_path):
    store, ada, _ = _world(tmp_path)
    path = store.bot_skills_hidden_path(ada["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{{", encoding="utf-8")
    assert store.read_skills_hidden(ada["id"]) == set()
    assert "shared-note" in _names(store, ada["id"])
    path.write_text('{"names": true}', encoding="utf-8")  # a list was expected
    assert store.read_skills_hidden(ada["id"]) == set()


def test_write_skills_hidden_stores_a_clean_sorted_list(tmp_path):
    store, ada, _ = _world(tmp_path)
    stored = store.write_skills_hidden(ada["id"], [" beta ", "alpha", "", "beta"])
    assert stored == ["alpha", "beta"]
    assert store.read_skills_hidden(ada["id"]) == {"alpha", "beta"}


# endpoints: GET / PUT round-trip, plain-language refusal, unknown bot


def _client_world(tmp_path):
    client = TestClient(create_app(tmp_path))
    endpoint = client.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    bot = client.post(
        "/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"], "model": None}
    ).json()
    return client, bot


def _url(bot):
    return f"/api/bots/{bot['id']}/skills/hidden"


def test_hidden_endpoint_round_trip(tmp_path):
    client, bot = _client_world(tmp_path)
    assert client.get(_url(bot)).json() == {"names": []}
    put = client.put(_url(bot), json={"names": ["shared-note", "other"]})
    assert put.status_code == 200, put.text
    assert put.json() == {"names": ["other", "shared-note"]}
    assert client.get(_url(bot)).json() == {"names": ["other", "shared-note"]}
    cleared = client.put(_url(bot), json={"names": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"names": []}
    assert client.get(_url(bot)).json() == {"names": []}


def test_hidden_endpoint_refuses_a_bad_list(tmp_path):
    client, bot = _client_world(tmp_path)
    not_a_list = client.put(_url(bot), json={"names": "shared-note"})
    assert not_a_list.status_code == 400
    assert "list" in not_a_list.json()["detail"]
    bad_items = client.put(_url(bot), json={"names": ["ok", 7]})
    assert bad_items.status_code == 400
    assert client.get(_url(bot)).json() == {"names": []}  # the refusal wrote nothing


def test_hidden_endpoint_needs_a_real_bot(tmp_path):
    client, _ = _client_world(tmp_path)
    missing = client.get("/api/bots/nobody/skills/hidden")
    assert missing.status_code == 404
    assert client.put("/api/bots/nobody/skills/hidden", json={"names": []}).status_code == 404
