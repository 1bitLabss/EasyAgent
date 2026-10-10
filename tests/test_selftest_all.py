"""The release gate refuses the live data dir, checks a copy, and cleans up."""

import getpass
import json
import socket
from pathlib import Path

from easyagent.store import Store, new_id, now_iso


def _copy(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "copy"
    root.mkdir()
    store = Store(root)
    store.ensure()
    (root / "skills" / "keep.md").write_text("keep the skill\n", encoding="utf-8")
    (root / "host.json").write_text("{}\n", encoding="utf-8")
    (root / "marker.txt").write_text("keep", encoding="utf-8")
    endpoint = store.add_endpoint(
        name="local",
        base_url="http://127.0.0.1:9/v1",
        api_key=None,
        model="your-model",
    )
    bot = store.add_bot(name="Kept", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    chat["messages"] = [{
        "id": new_id(),
        "role": "user",
        "content": "leave this chat alone",
        "created_at": now_iso(),
    }]
    store.save_chat(chat)
    return root, bot


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


def _fake_complete():
    account = getpass.getuser()

    async def complete(*, base_url, api_key, model, messages, **kwargs):
        del base_url, api_key, model, kwargs
        if any(item.get("role") == "tool" for item in messages):
            return f"The account is {account}."
        last = ""
        for item in messages:
            if item.get("role") == "user":
                last = item.get("content") or ""
        folded = last.lower()
        if "whoami" in folded:
            return "```shell\nwhoami\n```"
        if "routine-ok" in folded:
            return "routine-ok"
        return "pong"

    return complete


def _fake_search():
    async def web_search(query, store=None):
        del query, store
        return "1. Harbor lighthouse notes\nhttps://example.com/harbor\nThe harbor lighthouse keeps a steady beam."

    return web_search


def _names(report) -> set[str]:
    return {item["name"] for item in report["checks"]}


def test_usage_and_help():
    from easyagent.selftest_all import main

    assert main([]) == 2
    assert main(["--port", "44747"]) == 2
    assert main(["--help"]) == 0


def test_the_live_data_dir_is_refused_and_left_alone(tmp_path, monkeypatch):
    from easyagent.selftest_all import main

    live = tmp_path / "live"
    live.mkdir()
    marker = live / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("EASYAGENT_DATA", str(live))
    assert main(["--data", str(live), "--port", str(_port())]) == 2
    assert marker.read_text(encoding="utf-8") == "keep"
    nested = live / "nested"
    nested.mkdir()
    assert main(["--data", str(nested), "--port", str(_port())]) == 2
    assert marker.read_text(encoding="utf-8") == "keep"


def test_a_busy_port_changes_nothing(tmp_path):
    from easyagent.selftest_all import main

    root, _bot = _copy(tmp_path)
    before = _snap(root)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        assert main(["--data", str(root), "--port", str(port)]) == 2
    finally:
        sock.close()
    assert _snap(root) == before


def test_the_gate_passes_on_a_copy_and_removes_its_bots(tmp_path, monkeypatch):
    from easyagent.selftest_all import run_gate

    monkeypatch.setattr("easyagent.llm.complete", _fake_complete())
    monkeypatch.setattr("easyagent.search.web_search", _fake_search())
    root, kept = _copy(tmp_path)
    before = _snap(root)
    report_path = tmp_path / "selftest-all.json"
    code, report, path = run_gate(
        ["--data", str(root), "--port", str(_port()), "--report", str(report_path), "--consent"],
        ui=False,
    )
    assert code == 0, report
    assert path == report_path
    assert report["ok"] is True
    assert report["persistent_changes"] is False
    assert report["version"]
    names = _names(report)
    assert names == {
        "health", "chat", "whoami", "files", "search", "ui", "polls",
        "routines", "secrets", "containment", "honesty", "copy",
    }
    assert all(item["status"] in {"PASS", "SKIP"} for item in report["checks"])
    assert all(isinstance(item["seconds"], float) for item in report["checks"])
    by_name = {item["name"]: item for item in report["checks"]}
    assert by_name["ui"]["status"] == "SKIP"
    assert by_name["secrets"]["detail"] == "0 plaintext hits"
    assert "next:" in by_name["routines"]["detail"]
    assert _snap(root) == before
    kept_dir = root / "bots" / kept["id"]
    assert kept_dir.is_dir()
    bots = [path.name for path in (root / "bots").iterdir() if path.is_dir()]
    assert bots == [kept["id"]]
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", report["port"]))
    finally:
        sock.close()


def test_a_failed_chat_exits_nonzero_and_still_cleans_up(tmp_path, monkeypatch):
    from easyagent.llm import ProviderError
    from easyagent.selftest_all import run_gate

    async def broken(*, base_url, api_key, model, messages, **kwargs):
        del base_url, api_key, model, messages, kwargs
        raise ProviderError("down")

    monkeypatch.setattr("easyagent.llm.complete", broken)
    monkeypatch.setattr("easyagent.search.web_search", _fake_search())
    root, kept = _copy(tmp_path)
    before = _snap(root)
    code, report, _path = run_gate(
        ["--data", str(root), "--port", str(_port()), "--report", str(tmp_path / "report.json")],
        ui=False,
    )
    assert code == 1
    assert report["ok"] is False
    assert any(item["name"] == "chat" and item["status"] == "FAIL" for item in report["checks"])
    assert _snap(root) == before
    assert (root / "bots" / kept["id"] / "bot.json").is_file()
    assert [path.name for path in (root / "bots").iterdir() if path.is_dir()] == [kept["id"]]


def test_main_asks_for_a_30_second_ui_probe(tmp_path, monkeypatch):
    from easyagent.selftest_all import main

    seen = {}

    def probe(url, bot_id, idle_s):
        seen["idle"] = idle_s
        seen["bot"] = bot_id
        return {
            "nodes_before": 60,
            "nodes_after": 60,
            "mutations": 0,
            "transcript_polls": 0,
            "approval_polls": 0,
            "unread_polls": 3,
            "unread_gaps_ms": [8000, 8000],
            "idle_s": idle_s,
        }, ""

    monkeypatch.setattr("easyagent.llm.complete", _fake_complete())
    monkeypatch.setattr("easyagent.search.web_search", _fake_search())
    monkeypatch.setattr("easyagent.selftest_all.probe_ui", probe)
    root, _kept = _copy(tmp_path)
    report_path = tmp_path / "out.json"
    assert main(["--data", str(root), "--port", str(_port()), "--report", str(report_path)]) == 0
    body = json.loads(report_path.read_text(encoding="utf-8"))
    assert seen["idle"] == 30
    assert str(seen["bot"])
    assert body["checks"]
    ui = next(item for item in body["checks"] if item["name"] == "ui")
    polls = next(item for item in body["checks"] if item["name"] == "polls")
    assert ui["status"] == "PASS"
    assert polls["status"] == "PASS"


def test_judge_ui_and_polls():
    from easyagent.selftest_all import judge_polls, judge_ui

    quiet = {
        "nodes_before": 60,
        "nodes_after": 60,
        "mutations": 0,
        "transcript_polls": 0,
        "approval_polls": 0,
        "unread_polls": 3,
        "unread_gaps_ms": [8000, 8100],
        "idle_s": 30,
    }
    assert judge_ui(quiet)[0] == "PASS"
    assert judge_polls(quiet)[0] == "PASS"
    lost = dict(quiet, nodes_after=12, mutations=9)
    assert judge_ui(lost)[0] == "FAIL"
    noisy = dict(quiet, transcript_polls=4, approval_polls=2, unread_gaps_ms=[1000])
    assert judge_polls(noisy)[0] == "FAIL"
    short = dict(quiet, idle_s=1, unread_polls=0, unread_gaps_ms=[])
    assert judge_polls(short)[0] == "PASS"


def test_a_missing_browser_skips_the_ui_probe(monkeypatch):
    from easyagent.selftest_all import probe_ui

    monkeypatch.setattr("easyagent.selftest_all.find_browser", lambda: None)
    sample, reason = probe_ui("http://127.0.0.1:9/", "bot", 30)
    assert sample is None
    assert reason.startswith("no ")


def test_routine_preview_is_local_time():
    from easyagent.schedule import compile_routine, preview
    from easyagent.selftest_all import judge_routine_preview

    schedule = compile_routine({
        "name": "selftest-routine",
        "prompt": "Reply with the single word routine-ok.",
        "daily": "7:30 AM",
    })
    schedule["preview"] = preview(schedule)
    status, detail = judge_routine_preview(schedule)
    assert status == "PASS", detail
    assert "7:30 AM" in detail
    assert "next:" in detail


def test_secrets_migration_leaves_zero_plaintext_hits():
    from easyagent.selftest_all import _check_secrets

    status, detail = _check_secrets()
    assert status == "PASS", detail
    assert detail == "0 plaintext hits"


def test_containment_stays_a_report_even_with_consent(monkeypatch):
    from easyagent.selftest_all import check_containment

    called = []

    def rows():
        called.append("rows")
        return [("mechanism", "PASS", "bubblewrap"), ("RESULT", "PASS", "")]

    def persist(*_args, **_kwargs):
        called.append("persist")
        raise AssertionError("persistent contain")

    monkeypatch.setattr("easyagent.selftest._platform_rows", rows)
    monkeypatch.setattr("easyagent.contain.main", persist)
    status, detail = check_containment(True)
    assert status == "PASS", detail
    assert called == ["rows"]


def test_a_firewall_warning_stays_a_pass(monkeypatch):
    from easyagent.selftest_all import check_containment

    def rows():
        return [
            ("mechanism", "PASS", "appcontainer"),
            ("web", "WARN", "a third-party firewall is blocking sandboxed bots from the network; EasyAgent's own web search still works"),
            ("RESULT", "PASS", ""),
        ]

    monkeypatch.setattr("easyagent.selftest._platform_rows", rows)
    status, detail = check_containment(True)
    assert status == "PASS"
    assert "a third-party firewall is blocking sandboxed bots from the network; EasyAgent's own web search still works" in detail
