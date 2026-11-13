"""0.3.66 wave 1: local-server detection, ad-hoc probe, and the setup flag.

The network is always stubbed: no test here needs a real Ollama, llama.cpp,
or LM Studio running. The fake client answers by URL, exactly like the
httpx stubs in test_0329/test_0353d.
"""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from easyagent import detect
from easyagent.app import create_app


@pytest.fixture
def world(tmp_path):
    app = create_app(tmp_path)
    return type("W", (), {"client": TestClient(app), "path": tmp_path})()


def stub_httpx(monkeypatch, table):
    """table: {url: payload | Exception | (seconds, payload)}. Matched by suffix."""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, **kwargs):
            for suffix, outcome in table.items():
                if str(url).endswith(suffix):
                    delay, outcome = outcome if isinstance(outcome, tuple) else (0.0, outcome)
                    if delay:
                        await asyncio.sleep(delay)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return _Response(outcome)
            raise httpx.ConnectError("nothing is listening")

    monkeypatch.setattr(detect.httpx, "AsyncClient", FakeClient)


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


OLLAMA = {"models": [{"name": "llama3:8b"}, {"name": "mistral"}]}
PROPS = {"model_path": "C:/models/test.gguf", "total_slots": 4}
MODELS = {"data": [{"id": "test-model"}, {"id": "second-model"}]}


def test_probe_returns_the_model_list(world, monkeypatch):
    stub_httpx(monkeypatch, {"/v1/models": MODELS})
    result = world.client.post(
        "/api/endpoints/probe", json={"base_url": "http://127.0.0.1:9/v1"}
    )
    assert result.status_code == 200
    body = result.json()
    assert body["ok"] is True
    assert body["models"] == ["test-model", "second-model"]


def test_probe_failure_is_plain_ok_false(world, monkeypatch):
    stub_httpx(monkeypatch, {})  # every address refuses
    result = world.client.post(
        "/api/endpoints/probe", json={"base_url": "http://127.0.0.1:9/v1"}
    )
    assert result.status_code == 200
    body = result.json()
    assert body["ok"] is False
    assert "traceback" not in str(body).lower()


def test_probe_rejects_a_bad_scheme_without_raising(world):
    result = world.client.post("/api/endpoints/probe", json={"base_url": "ftp://nowhere"})
    assert result.status_code == 200
    assert result.json()["ok"] is False


def test_probe_never_writes_files(world, monkeypatch):
    stub_httpx(monkeypatch, {"/v1/models": MODELS})
    before = {p.name for p in world.path.iterdir()}
    world.client.post("/api/endpoints/probe", json={"base_url": "http://127.0.0.1:9/v1"})
    assert {p.name for p in world.path.iterdir()} == before


def test_local_servers_returns_rows(world, monkeypatch):
    stub_httpx(
        monkeypatch,
        {
            "127.0.0.1:11434/api/tags": OLLAMA,
            "127.0.0.1:8080/props": PROPS,
            "127.0.0.1:8080/v1/models": MODELS,
        },
    )
    body = world.client.get("/api/local-servers").json()
    kinds = {row["kind"] for row in body["servers"]}
    assert {"ollama", "llama.cpp"} <= kinds
    ollama = next(row for row in body["servers"] if row["kind"] == "ollama")
    assert ollama["base_url"] == "http://127.0.0.1:11434/v1"
    assert ollama["models"] == ["llama3:8b", "mistral"]
    assert all(row.get("label") for row in body["servers"])


def test_local_servers_skips_a_slow_server_inside_the_budget(world, monkeypatch):
    stub_httpx(
        monkeypatch,
        {
            "127.0.0.1:11434/api/tags": (5.0, OLLAMA),  # answers, but far too late
            "127.0.0.1:1234/v1/models": MODELS,         # answers at once
        },
    )
    started = time.monotonic()
    body = world.client.get("/api/local-servers").json()
    elapsed = time.monotonic() - started
    assert elapsed < detect.BUDGET_SECONDS + 1.0, "the budget did not cap the probes"
    assert all(row["kind"] != "ollama" for row in body["servers"])
    assert any(row["kind"] == "lmstudio" for row in body["servers"])


def test_local_servers_is_empty_when_nothing_answers(world, monkeypatch):
    stub_httpx(monkeypatch, {})
    assert world.client.get("/api/local-servers").json() == {"servers": []}


def test_setup_flag_round_trip(world):
    assert world.client.get("/api/setup").json() == {"onboarded": False}
    saved = world.client.put("/api/setup", json={"onboarded": True})
    assert saved.status_code == 200
    assert saved.json()["onboarded"] is True
    assert world.client.get("/api/setup").json()["onboarded"] is True
    raw = json.loads((world.path / "setup.json").read_text(encoding="utf-8"))
    assert raw["onboarded"] is True


def test_setup_put_rejects_garbage_with_400(world):
    for junk in ("not json at all", json.dumps([1, 2]), json.dumps({"onboarded": "yes"})):
        response = world.client.put(
            "/api/setup",
            content=junk.encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400, junk
    assert world.client.get("/api/setup").json() == {"onboarded": False}


def test_existing_endpoint_routes_untouched(world, monkeypatch):
    created = world.client.post(
        "/api/endpoints", json={"name": "Down", "base_url": "http://127.0.0.1:9/v1"}
    )
    assert created.status_code == 200
    stub_httpx(monkeypatch, {})
    tested = world.client.post(f"/api/endpoints/{created.json()['id']}/test").json()
    assert tested["ok"] is False
