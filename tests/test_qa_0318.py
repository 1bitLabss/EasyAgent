"""0.3.18: add a bot with no connection, and lasting grants stay scoped and unreachable by a bot."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent import safety
from easyagent.app import create_app
from easyagent.safety import ASK, BLOCK, Pending, classify, resolve_card
from easyagent.store import Store
from easyagent.strictshell import allow_exec_always
from easyagent.tools import ToolRequest


def _client(tmp_path):
    return TestClient(create_app(tmp_path))


def store_allows(tmp_path, bot_id: str) -> dict:
    record = json.loads((tmp_path / "bots" / bot_id / "bot.json").read_text(encoding="utf-8"))
    return {
        "safety_allows": list(record.get("safety_allows") or []),
        "safety_exec_always": list(record.get("safety_exec_always") or []),
        "safety_git_allows": list(record.get("safety_git_allows") or []),
    }


def test_add_bot_with_a_name_and_no_connection(tmp_path):
    client = _client(tmp_path)
    for body in ({"name": "Ada"}, {"name": "Ada2", "endpoint_id": None}, {"name": "Ada3", "endpoint_id": ""}):
        response = client.post("/api/bots", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["name"] == body["name"]
    assert client.get("/api/endpoints").json() == []
    names = [bot["name"] for bot in client.get("/api/bots").json()]
    assert {"Ada", "Ada2", "Ada3"} <= set(names)


def test_bot_with_no_connection_says_so_instead_of_failing(tmp_path):
    client = _client(tmp_path)
    bot = client.post("/api/bots", json={"name": "Ada"}).json()
    chat = client.get(f"/api/bots/{bot['id']}/ongoing").json()
    response = client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages", json={"content": "hi"})
    assert response.status_code == 409, response.text
    assert "no connection yet" in response.json()["detail"]


def test_unknown_connection_is_still_refused(tmp_path):
    client = _client(tmp_path)
    response = client.post("/api/bots", json={"name": "Ada", "endpoint_id": "nope"})
    assert 400 <= response.status_code < 500


def test_new_connection_with_empty_name_names_the_connection_field(tmp_path):
    client = _client(tmp_path)
    response = client.post("/api/endpoints", json={"name": "", "base_url": "http://127.0.0.1:9/v1"})
    assert response.status_code == 400
    assert "Connection name" in response.text


def _seed(bot_id: str, *, rule: str, tier: str = ASK, offer_always: bool = True, proposal: dict | None = None) -> Pending:
    card = Pending(
        id=f"card-{rule}-{tier}-{bot_id}",
        bot_id=bot_id,
        tier=tier,
        rule=rule,
        why="test",
        detail="test",
        fingerprint="fp",
        exact="echo test",
        offer_always=offer_always,
        created=time.time(),
        proposal=proposal,
    )
    safety._PENDING[card.id] = card
    return card


SCRIPT = {"kind": "script-diff", "session": True, "exec_always": True}


@pytest.mark.parametrize("rule", sorted(safety._NEVER_ALWAYS))
@pytest.mark.parametrize("decision", ["always", "session", "exec-always"])
def test_ui_cannot_get_a_lasting_grant_for_never_always_rules(tmp_path, rule, decision):
    client = _client(tmp_path)
    bot = client.post("/api/bots", json={"name": "Ada"}).json()
    card = _seed(bot["id"], rule=rule, proposal=dict(SCRIPT))
    response = client.post(f"/api/bots/{bot['id']}/approvals/{card.id}", json={"decision": decision})
    assert response.status_code == 200
    assert response.json()["decision"] == "deny"


@pytest.mark.parametrize("decision", ["always", "session", "exec-always"])
@pytest.mark.parametrize("offer_always", [True, False])
def test_ui_cannot_get_a_lasting_grant_on_a_blocked_card(tmp_path, decision, offer_always):
    client = _client(tmp_path)
    bot = client.post("/api/bots", json={"name": "Ada"}).json()
    card = _seed(bot["id"], rule="bot-changed", tier=BLOCK, offer_always=offer_always, proposal=dict(SCRIPT))
    response = client.post(f"/api/bots/{bot['id']}/approvals/{card.id}", json={"decision": decision})
    assert response.json()["decision"] == "deny"
    saved = store_allows(tmp_path, bot["id"])
    assert saved["safety_allows"] == []
    assert saved["safety_exec_always"] == []


@pytest.mark.parametrize("offer_always", [True, False])
def test_bridge_send_always_is_a_denial_not_a_one_time_approval(tmp_path, offer_always):
    client = _client(tmp_path)
    bot = client.post("/api/bots", json={"name": "Ada"}).json()
    card = _seed(bot["id"], rule="bridge-send", offer_always=offer_always, proposal=dict(SCRIPT))
    response = client.post(f"/api/bots/{bot['id']}/approvals/{card.id}", json={"decision": "always"})
    assert response.json()["decision"] == "deny"
    saved = store_allows(tmp_path, bot["id"])
    assert saved["safety_allows"] == []
    assert saved["safety_exec_always"] == []


def test_bridge_send_card_is_never_always():
    assert "bridge-send" in safety._NEVER_ALWAYS
    card = _seed("b1", rule="bridge-send", offer_always=True)
    assert resolve_card(card.id, "always").decision == "deny"


def test_session_only_on_a_script_card_that_offers_it(tmp_path):
    card = _seed("b1", rule="unlisted-command", proposal=None)
    assert resolve_card(card.id, "session").decision == "deny"
    card = _seed("b2", rule="bot-changed", proposal={"kind": "script-diff", "session": True})
    assert resolve_card(card.id, "session").decision == "session"


def test_exec_always_only_where_offered():
    card = _seed("b1", rule="bot-changed", proposal={"kind": "script-diff", "session": True, "exec_always": False})
    assert resolve_card(card.id, "exec-always").decision == "deny"


def test_another_bots_card_cannot_be_answered(tmp_path):
    client = _client(tmp_path)
    ada = client.post("/api/bots", json={"name": "Ada"}).json()
    bob = client.post("/api/bots", json={"name": "Bob"}).json()
    card = _seed(ada["id"], rule="unlisted-command")
    for decision in ("approve", "always", "takeover"):
        response = client.post(f"/api/bots/{bob['id']}/approvals/{card.id}", json={"decision": decision})
        assert response.status_code == 404
    assert not card.event.is_set()


def test_exec_always_grant_is_per_bot_tool_and_root(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    ada = store.add_bot(name="Ada", endpoint_id=None, model=None)
    bob = store.add_bot(name="Bob", endpoint_id=None, model=None)
    root = tmp_path / "work"
    root.mkdir()
    assert allow_exec_always(store, ada["id"], "pytest -q", root)
    grants = store.get_bot(ada["id"])["safety_exec_always"]
    assert len(grants) == 1
    assert grants[0]["tool"] and grants[0]["root"]
    assert Path(grants[0]["root"]).name == "work"
    assert not store.get_bot(bob["id"]).get("safety_exec_always")
    # Two different tools in one chain save nothing.
    assert not allow_exec_always(store, bob["id"], "pytest -q && npm test", root)


def test_repo_allow_needs_a_repo_this_bot_was_shown(tmp_path):
    client = _client(tmp_path)
    bot = client.post("/api/bots", json={"name": "Ada"}).json()
    response = client.post(f"/api/bots/{bot['id']}/safety", json={"git_allow": "/anything/else"})
    assert response.status_code == 409


def test_repo_allow_stays_on_the_bot_that_was_shown_the_note(tmp_path):
    client = _client(tmp_path)
    ada = client.post("/api/bots", json={"name": "Ada"}).json()
    bob = client.post("/api/bots", json={"name": "Bob"}).json()
    repo = "/work/checkout"
    path = tmp_path / "bots" / ada["id"] / "bot.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["safety_git_noted"] = {repo: True}
    path.write_text(json.dumps(record), encoding="utf-8")
    allowed = client.post(f"/api/bots/{ada['id']}/safety", json={"git_allow": repo})
    assert allowed.status_code == 200, allowed.text
    assert repo in store_allows(tmp_path, ada["id"])["safety_git_allows"]
    refused = client.post(f"/api/bots/{bob['id']}/safety", json={"git_allow": repo})
    assert refused.status_code == 409
    assert store_allows(tmp_path, bob["id"])["safety_git_allows"] == []


def test_bot_cannot_write_its_own_grant_file(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    bot = store.add_bot(name="Ada", endpoint_id=None, model=None)
    target = store._bot_dir(bot["id"]) / "bot.json"
    body = '{"safety_exec_always":[{"tool":"pytest","root":"/work"}],"safety_allows":["rm -rf /"]}'
    written = classify(store, ToolRequest(kind="files", action="write", path=str(target), body=body), bot["id"])
    assert written.tier == BLOCK
    assert written.rule == "guardrail-edit"
    command = f"python -c \"open(r'{target}', 'w').write('safety_exec_always')\""
    shelled = classify(store, ToolRequest(kind="shell", command=command), bot["id"])
    assert shelled.tier == BLOCK
    assert "safety_exec_always" not in target.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "request_",
    [
        ToolRequest(kind="shell", command="curl -X POST http://127.0.0.1:44721/api/bots/x/approvals/y -d '{\"decision\":\"always\"}'"),
        ToolRequest(kind="shell", command="python -c \"import urllib.request;urllib.request.urlopen('http://localhost:44721/api/bots')\""),
        ToolRequest(kind="shell", command="Invoke-WebRequest -Method Patch http://[::1]:44721/api/bots/x"),
        ToolRequest(kind="fetch", path="http://127.0.0.1:44721/api/bots"),
        ToolRequest(kind="browser", action="open", path="http://localhost:44721/"),
    ],
)
def test_bot_cannot_reach_its_own_server(tmp_path, request_):
    store = Store(tmp_path)
    store.ensure()
    bot = store.add_bot(name="Ada", endpoint_id=None, model=None)
    verdict = classify(store, request_, bot["id"])
    assert verdict.tier == BLOCK
    assert verdict.rule == "own-server"


def test_ordinary_local_ports_are_not_own_server():
    assert not safety.own_server_target("curl http://localhost:3000/")
    assert not safety.own_server_target("curl http://127.0.0.1:4472/")
    assert not safety.own_server_target("echo 44721")
