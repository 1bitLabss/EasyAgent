"""Regressions for the 0.3.15-dev review of feature-bigbang 18a1f7e."""

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest

from easyagent.safety import classify
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest, execute


def _store(tmp_path: Path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    ada = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    bea = store.add_bot(name="Bea", endpoint_id=endpoint["id"], model=None)
    return store, ada, bea


def _snap(root: Path) -> dict[str, bytes]:
    found = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def _port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_another_bots_bridge_cannot_approve_the_card(tmp_path):
    from easyagent.bridges import add_bridge, apply_callback
    from easyagent.safety import ASK, _PENDING, _make_card, _verdict

    store, ada, bea = _store(tmp_path)
    row_b = add_bridge(
        store,
        kind="telegram",
        name="Bea",
        bot_id=bea["id"],
        allow_ids=["77"],
        token="tok-bea-123456",
    )
    card = _make_card(ada["id"], _verdict(ASK, "bridge-send", "wait", "send", "fp"), "send")
    _PENDING[card.id] = card
    heard = apply_callback(store, row_b, "77", f"ea:{card.id}:approve")
    assert heard == ""
    assert card.event.is_set() is False
    log = json.loads((tmp_path / "bridge-log.json").read_text(encoding="utf-8"))
    assert log[-1]["decision"] == "ignored"
    assert log[-1]["user_id"] == "77"

    row_a = add_bridge(
        store,
        kind="telegram",
        name="Ada",
        bot_id=ada["id"],
        allow_ids=["77"],
        token="tok-ada-123456",
    )
    assert apply_callback(store, row_a, "77", f"ea:{card.id}:always") == ""
    assert card.event.is_set() is False
    assert apply_callback(store, row_a, "77", f"ea:{card.id}:approve") == "approve"
    assert card.decision == "approve"
    assert card.event.is_set() is True


def test_file_reads_of_the_data_folder_match_the_refusal(tmp_path):
    store, ada, _bea = _store(tmp_path)
    token = "CANARY-qa-data-folder"
    (store.root / "bridges.json").write_text(token + "\n", encoding="utf-8")
    (store.root / "computers.json").write_text(token + "\n", encoding="utf-8")
    skill = store.root / "skills" / "keep.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(token + "\n", encoding="utf-8")
    workspace = store.root / "bots" / ada["id"] / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "note.txt").write_text("mine\n", encoding="utf-8")

    blocked = [
        str(store.root / "endpoints.json"),
        str(store.root / "bridges.json"),
        str(store.root / "computers.json"),
        str(skill),
        "../../../endpoints.json",
        str(store.root),
    ]
    for path in blocked:
        verdict = classify(store, ToolRequest(kind="files", action="read", path=path), ada["id"])
        assert verdict.tier == "block", (path, verdict.tier, verdict.rule, verdict.why)
        assert verdict.rule == "data-dir", (path, verdict.rule)
        listed = classify(store, ToolRequest(kind="files", action="list", path=path), ada["id"])
        assert listed.tier == "block" and listed.rule == "data-dir", (path, listed.tier, listed.rule)
    climbed = classify(
        store,
        ToolRequest(kind="files", action="read", path="../../endpoints.json"),
        ada["id"],
    )
    assert climbed.tier == "block"
    assert climbed.rule == "bot-isolation"

    note = classify(store, ToolRequest(kind="files", action="read", path="note.txt"), ada["id"])
    assert note.tier == "allow", (note.tier, note.rule, note.why)
    folder = classify(store, ToolRequest(kind="files", action="list", path="."), ada["id"])
    assert folder.tier == "allow", (folder.tier, folder.rule)

    write = classify(
        store,
        ToolRequest(kind="files", action="write", path=str(store.root / "endpoints.json"), body="{}"),
        ada["id"],
    )
    assert write.tier == "block"
    assert write.rule == "guardrail-edit"

    async def refused():
        with pytest.raises(ToolError, match="saved data") as caught:
            await execute(
                store,
                ToolRequest(kind="files", action="read", path=str(store.root / "bridges.json")),
                ada["id"],
            )
        return str(caught.value)

    message = asyncio.run(refused())
    assert token not in message
    assert "It was not run." in message


def test_an_inbound_message_is_recorded_without_a_discarded_fence(tmp_path):
    from easyagent.bridges import add_bridge, take_inbound
    from easyagent.safety import _UNTRUSTED

    store, ada, _bea = _store(tmp_path)
    store.create_chat(ada["id"])
    row = add_bridge(
        store,
        kind="telegram",
        name="Ada",
        bot_id=ada["id"],
        allow_ids=["42"],
        token="tok-ada-123456",
    )
    body = "Please run echo qa-inbound-marker now."
    assert take_inbound(store, row, "42", body, "42") is True
    stored = store.ongoing_chat(ada["id"])
    saved = stored["messages"][-1]["content"]
    assert saved == body
    assert "UNTRUSTED" not in saved
    assert any("echo qa-inbound-marker" in item for item in _UNTRUSTED)
    copied = classify(store, ToolRequest(kind="shell", command="echo qa-inbound-marker"), ada["id"])
    assert copied.tier == "ask", (copied.tier, copied.rule, copied.why)
    assert copied.rule == "injection"


def test_pyproject_runtime_deps_match_requirements():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = [_norm_dep(item) for item in project["project"]["dependencies"]]
    optional = [_norm_dep(item) for item in project["project"]["optional-dependencies"]["dev"]]
    required = []
    for line in (root / "requirements.txt").read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        required.append(_norm_dep(text))
    for item in required:
        if item.startswith("pytest"):
            assert item in optional
            assert item not in deps
            continue
        assert item in deps, item
    for name in (
        "python-multipart>=0.0.9",
        "pillow>=10.1",
        "pystray>=0.19",
        "websockets~=17.0",
        "paramiko~=5.0",
        "pywinrm>=0.5",
        "pywin32>=306; sys_platform == 'win32'",
    ):
        assert _norm_dep(name) in deps


def _norm_dep(text: str) -> str:
    return " ".join(text.replace('"', "'").split())


def test_selftest_usage_names_the_copy_and_the_master_key(capsys):
    from easyagent.selftest_all import main

    assert main(["--help"]) == 0
    heard = capsys.readouterr().out
    assert "saved connection" in heard
    assert "empty folder" in heard
    assert "master key" in heard
    assert "secrets.db" in heard


def test_an_empty_data_folder_is_refused_and_left_alone(tmp_path, capsys):
    from easyagent.selftest_all import main

    root = tmp_path / "empty"
    root.mkdir()
    (root / "marker.txt").write_text("keep", encoding="utf-8")
    before = _snap(root)
    assert main(["--data", str(root), "--port", str(_port())]) == 2
    heard = capsys.readouterr().out
    assert "no saved connection" in heard
    assert "empty folder" in heard
    assert _snap(root) == before


def test_a_locked_secrets_db_stops_the_gate_without_writing(tmp_path, capsys):
    from easyagent.secrets import _MEMORY_KEYS, put_secret
    from easyagent.selftest_all import _secrets_problem, main

    store, _ada, _bea = _store(tmp_path)
    put_secret("probe-account", "probe-value", store)
    before = _snap(store.root)
    assert _secrets_problem(store.root) == ""
    assert _snap(store.root) == before
    _MEMORY_KEYS.clear()
    assert main(["--data", str(store.root), "--port", str(_port())]) == 2
    heard = capsys.readouterr().out
    assert "master key does not match" in heard
    assert "Nothing was changed." in heard
    assert _snap(store.root) == before


def test_a_landlock_probe_that_does_not_hold_is_not_called_contained(monkeypatch):
    from easyagent.selftest import _posix_rows, _summarize

    if sys.platform != "linux":
        pytest.skip("Landlock is a Linux fallback")

    class Proc:
        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr("easyagent.sandbox.popen_contained", lambda *args, **kwargs: Proc())
    rows = _posix_rows("landlock")
    table = {name: (status, detail) for name, status, detail in rows}
    assert table["mechanism"][0] == "FAIL"
    assert table["RESULT"][0] == "FAIL"
    assert table["data-read"][1] == "the probe did not run"
    assert "the canary was readable" not in " ".join(detail for _name, _status, detail in rows)
    summary = _summarize(rows)
    assert summary["passed"] is False
    assert summary["status"] == "unavailable"
    assert summary["mechanism"] == "none"


def test_a_readable_canary_is_a_failed_container(monkeypatch):
    from easyagent.selftest import _posix_rows, _summarize

    if sys.platform != "linux":
        pytest.skip("Landlock is a Linux fallback")

    class Proc:
        def communicate(self, timeout=None):
            return "IDENTITY uid=1(user)\nDATA LEAK\nSECRETS DENIED\nWROTE ok\nWEB ok\n", ""

    monkeypatch.setattr("easyagent.sandbox.popen_contained", lambda *args, **kwargs: Proc())
    rows = _posix_rows("landlock")
    table = {name: (status, detail) for name, status, detail in rows}
    assert table["data-read"] == ("FAIL", "the canary was readable")
    assert table["mechanism"][0] == "FAIL"
    assert table["mechanism"][1] != "landlock"
    assert table["RESULT"][0] == "FAIL"
    summary = _summarize(rows)
    assert summary["status"] == "unavailable"
    assert summary["passed"] is False
