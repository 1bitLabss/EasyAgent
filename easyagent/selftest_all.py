"""`python -m easyagent selftest all` — one release gate against a copy of the data.

The live data directory is refused. The server listens on --port, talks only to
bots this command creates (names start with selftest-), and those bots, the
temp files, and the server are removed before exit. Containment is the existing
report. This command does not apply ACL or profile changes, with or without
--consent. A FAIL exits nonzero.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from easyagent import __version__
from easyagent.app import create_app, default_data_dir

BOT_PREFIX = "selftest-"
BOT_NAME = "selftest-gate"
_USAGE = "usage: python -m easyagent selftest all --data COPY --port PORT [--report FILE] [--consent]"
_QUIET = {
    "EASYAGENT_LEARN": "0",
    "EASYAGENT_ROLLING": "0",
    "EASYAGENT_NIGHTLY": "0",
    "EASYAGENT_TRAY": "0",
}
_CHAT_TIMEOUT = 300.0
_SIDECAR_SUFFIXES = (".db-wal", ".db-shm", ".db-journal")


def main(argv: list[str] | None = None) -> int:
    code, report, path = run_gate(list(sys.argv[1:] if argv is None else argv))
    if report is None:
        return code
    _write_report(path, report)
    print(_format_table(report["checks"]), flush=True)
    print(f"report: {path}", flush=True)
    return code


def run_gate(
    argv: list[str],
    *,
    ui: bool = True,
    idle_s: float = 30.0,
) -> tuple[int, dict | None, Path | None]:
    """Run the gate. Returns (exit code, report or None, report path or None)."""
    parsed = _parse(argv)
    if parsed.get("help"):
        print(_USAGE, flush=True)
        print("Containment stays a report. --consent does not apply ACL or profile changes.", flush=True)
        return 0, None, None
    if parsed.get("error"):
        print(_USAGE, flush=True)
        return 2, None, None
    data = Path(parsed["data"])
    try:
        resolved = data.resolve()
    except OSError:
        print(f"Refusing to start. {data} is not a folder. Nothing was changed.", flush=True)
        return 2, None, None
    if not resolved.is_dir():
        print(f"Refusing to start. {resolved} is not a folder. Nothing was changed.", flush=True)
        return 2, None, None
    live = _live_dirs()
    for other in live:
        if _overlaps(resolved, other):
            print(
                f"Refusing to start. --data is the live data directory ({other}). "
                "Point it at a copy. Nothing was changed.",
                flush=True,
            )
            return 2, None, None
    report_path = Path(parsed["report"]) if parsed.get("report") else Path.cwd() / "selftest-all.json"
    try:
        report_resolved = report_path.resolve()
    except OSError:
        report_resolved = report_path
    if _overlaps(report_resolved, resolved) or report_resolved == resolved:
        print("Refusing to start. --report is inside the data copy. Nothing was changed.", flush=True)
        return 2, None, None
    port = int(parsed["port"])
    if port < 1 or port > 65535:
        print(_USAGE, flush=True)
        return 2, None, None
    if _port_busy(port):
        print(f"Refusing to start. Port {port} is already in use. Nothing was changed.", flush=True)
        return 2, None, None

    consent = bool(parsed.get("consent"))
    report: dict = {
        "version": __version__,
        "data": str(resolved),
        "port": port,
        "ok": False,
        "persistent_changes": False,
        "checks": [],
    }
    holder: dict = {}
    saved_env = {key: os.environ.get(key) for key in _QUIET}
    saved_due = None
    try:
        for key, value in _QUIET.items():
            os.environ[key] = value
        import easyagent.schedule as schedule_mod

        saved_due = schedule_mod.run_due_schedules

        async def _no_due(_store):
            return []

        schedule_mod.run_due_schedules = _no_due
        holder["before"] = _fingerprint(resolved)
        holder["restore"] = _capture_bytes(resolved)
        server = _Server(resolved, port)
        holder["server"] = server
        started = server.start()
        if not started:
            report["checks"].append(_row("health", "FAIL", server.error or "the server did not start", 0))
            for name in ("chat", "whoami", "files", "search", "ui", "polls", "routines"):
                report["checks"].append(_row(name, "FAIL", "the server did not start", 0))
        else:
            client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=httpx.Timeout(_CHAT_TIMEOUT, connect=5.0))
            holder["client"] = client
            ready = _fingerprint(resolved)
            holder["ready"] = ready
            holder["restore"] = _capture_bytes(resolved)
            bot_id = ""

            def health():
                return _check_health(client, resolved)

            report["checks"].append(_timed("health", health))
            if report["checks"][-1]["status"] == "PASS":
                bot_id = _create_bot(client)
                holder["bot_id"] = bot_id

            def need_bot(check):
                def run():
                    if not bot_id:
                        return "FAIL", "no selftest bot was created"
                    return check()

                return run

            report["checks"].append(_timed("chat", need_bot(lambda: _check_chat(client, bot_id))))
            report["checks"].append(_timed("whoami", need_bot(lambda: _check_whoami(client, bot_id))))
            report["checks"].append(_timed("files", need_bot(lambda: _check_files(server.store, bot_id))))
            report["checks"].append(_timed("search", lambda: _check_search(server.store)))
            ui_sample: dict = {}

            def ui_check():
                if not ui:
                    return "SKIP", "the UI probe was not requested"
                if not bot_id:
                    return "FAIL", "no selftest bot was created"
                _seed_long_chat(server.store, bot_id)
                sample, reason = probe_ui(f"http://127.0.0.1:{port}/", bot_id, idle_s)
                if sample is None:
                    if reason.startswith("no ") or "not installed" in reason:
                        return "SKIP", reason
                    return "FAIL", reason
                ui_sample["sample"] = sample
                return judge_ui(sample)

            def poll_check():
                sample = ui_sample.get("sample")
                if sample is None:
                    previous = next(item for item in report["checks"] if item["name"] == "ui")
                    if previous["status"] == "SKIP":
                        return "SKIP", previous["detail"]
                    return "FAIL", "the UI probe did not return poll counts"
                return judge_polls(sample)

            report["checks"].append(_timed("ui", ui_check))
            report["checks"].append(_timed("polls", poll_check))
            report["checks"].append(_timed("routines", need_bot(lambda: _check_routines(client, bot_id))))
    except Exception as exc:
        report["checks"].append(_row("gate", "FAIL", str(exc), 0))
    finally:
        client = holder.get("client")
        if client is not None:
            client.close()
            holder["client"] = None
        server = holder.get("server")
        if server is not None:
            server.stop()
        if saved_due is not None:
            import easyagent.schedule as schedule_mod

            schedule_mod.run_due_schedules = saved_due
            saved_due = None
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        selftest_ids = _selftest_bot_ids(resolved)
        if server is not None and server.store is not None:
            _delete_selftest_bots(server.store)
        _delete_selftest_bots_on_disk(resolved)
        _scrub_ledger(resolved, selftest_ids)
        report["checks"].append(_timed("secrets", _check_secrets))
        report["checks"].append(_timed("containment", lambda: check_containment(consent)))
        report["checks"].append(_timed("honesty", _check_honesty))
        baseline = holder.get("ready")
        if baseline is None:
            baseline = holder.get("before") or {}
        _restore_bytes(resolved, holder.get("restore") or {})
        _drop_extras(resolved, baseline)
        report["checks"].append(_timed("copy", lambda: _check_copy(resolved, baseline)))
    report["ok"] = all(item["status"] != "FAIL" for item in report["checks"])
    report["persistent_changes"] = False
    return (0 if report["ok"] else 1), report, report_path


def judge_ui(sample: dict) -> tuple[str, str]:
    """DOM nodes survive idle and scroll, and the message list stays still."""
    before = int(sample.get("nodes_before") or 0)
    after = int(sample.get("nodes_after") or 0)
    mutations = int(sample.get("mutations") or 0)
    problems = []
    if before < 60 or after < 60:
        problems.append(f"messages mounted {before} -> {after}")
    if before != after:
        problems.append("message rows changed")
    if mutations > 2:
        problems.append(f"mutations {mutations}")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", f"nodes {after}, mutations {mutations}"


def judge_polls(sample: dict) -> tuple[str, str]:
    """After a reply settles: no transcript polls, unread about every 8s, no approval polls."""
    transcript = int(sample.get("transcript_polls") or 0)
    approvals = int(sample.get("approval_polls") or 0)
    unread = int(sample.get("unread_polls") or 0)
    gaps = [int(item) for item in sample.get("unread_gaps_ms") or []]
    idle = float(sample.get("idle_s") or 0)
    problems = []
    if transcript != 0:
        problems.append(f"transcript polls {transcript}")
    if approvals != 0:
        problems.append(f"approval polls {approvals}")
    if idle >= 20:
        if unread < 2 or unread > 6:
            problems.append(f"unread polls {unread}")
        for gap in gaps:
            if gap < 6000 or gap > 12000:
                problems.append(f"unread gap {gap}ms")
    if problems:
        return "FAIL", "; ".join(problems)
    if gaps:
        mid = sorted(gaps)[len(gaps) // 2]
        return "PASS", f"transcript 0, unread {unread} every {mid / 1000:.0f}s, approvals 0"
    return "PASS", f"transcript 0, unread {unread}, approvals 0"


def check_containment(consent: bool) -> tuple[str, str]:
    """The existing probe, report only. consent does not apply a profile or an ACL."""
    del consent
    from easyagent import selftest as selftest_mod

    rows = selftest_mod._platform_rows()
    if not rows:
        return "FAIL", "no probe ran"
    status = rows[-1][1]
    if status not in {"PASS", "FAIL", "SKIP"}:
        status = "FAIL"
    if status == "FAIL":
        problems = [
            f"{name}: {detail}"
            for name, row_status, detail in rows
            if row_status in {"FAIL", "INCONCLUSIVE"} and name != "RESULT"
        ]
        return "FAIL", "; ".join(problems) or (rows[-1][2] or "the container did not hold")
    if status == "PASS":
        warnings = [detail for _name, row_status, detail in rows if row_status == "WARN" and detail]
        if warnings:
            return "PASS", "; ".join(warnings)
    mechanism = next((detail for name, row_status, detail in rows if name == "mechanism" and row_status == "PASS"), "")
    return status, rows[-1][2] or mechanism or "report only"


def find_browser() -> str | None:
    """Headless Edge or Chromium, when one is installed."""
    names = ("msedge", "microsoft-edge", "chromium", "chromium-browser", "google-chrome", "chrome")
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    roots: list[Path] = []
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value))
    candidates = []
    for root in roots:
        candidates.append(root / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        candidates.append(root / "Google" / "Chrome" / "Application" / "chrome.exe")
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def probe_ui(url: str, bot_id: str, idle_s: float) -> tuple[dict | None, str]:
    """Open the long chat, sit idle, and count DOM mutations and polls."""
    browser_path = find_browser()
    if not browser_path:
        return None, "no headless Edge or Chromium"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright is not installed"
    events: list[tuple[float, str, str]] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=browser_path,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.add_init_script(
                    "localStorage.setItem('easyagent.bot', %s);"
                    "Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});"
                    "Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});"
                    % json.dumps(bot_id)
                )
                page.on("request", lambda request: events.append((time.perf_counter(), request.method, request.url)))
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_function(
                    "() => document.querySelectorAll('.message-row').length >= 60",
                    timeout=20000,
                )
                page.wait_for_timeout(1000)
                start = len(events)
                origin = time.perf_counter()
                nodes_before = page.evaluate(
                    """() => {
                        const rows = document.querySelectorAll('.message-row');
                        const parent = rows[0] && rows[0].parentElement;
                        window.__eaMutations = 0;
                        if (parent) {
                          const obs = new MutationObserver((list) => { window.__eaMutations += list.length; });
                          obs.observe(parent, {childList: true, subtree: true, characterData: true, attributes: true});
                        }
                        const scroller = document.querySelector('div.min-h-0.flex-1.overflow-y-auto');
                        if (scroller) {
                          scroller.scrollTop = 0;
                          scroller.scrollTop = scroller.scrollHeight;
                        }
                        return rows.length;
                    }"""
                )
                if idle_s > 0:
                    page.wait_for_timeout(int(idle_s * 1000))
                nodes_after, mutations = page.evaluate(
                    "() => [document.querySelectorAll('.message-row').length, window.__eaMutations || 0]"
                )
            finally:
                browser.close()
    except Exception as exc:
        return None, f"could not launch the browser: {exc}"
    later = [(stamp, method, target) for stamp, method, target in events[start:] if stamp >= origin]
    unread_at = [stamp for stamp, method, target in later if _request_kind(method, target) == "unread"]
    gaps = [int((right - left) * 1000) for left, right in zip(unread_at, unread_at[1:])]
    sample = {
        "nodes_before": int(nodes_before),
        "nodes_after": int(nodes_after),
        "mutations": int(mutations),
        "transcript_polls": sum(1 for _stamp, method, target in later if _request_kind(method, target) == "transcript"),
        "approval_polls": sum(1 for _stamp, method, target in later if _request_kind(method, target) == "approvals"),
        "unread_polls": len(unread_at),
        "unread_gaps_ms": gaps,
        "idle_s": idle_s,
    }
    return sample, ""


def _request_kind(method: str, url: str) -> str:
    path = url.split("?", 1)[0]
    if path.rstrip("/").endswith("/api/unread"):
        return "unread"
    if "/approvals" in path:
        return "approvals"
    if method.upper() != "GET" or "/chats/" not in path:
        return ""
    if any(piece in path for piece in ("/messages", "/files", "/read", "/fresh")):
        return ""
    tail = path.split("/chats/", 1)[-1].strip("/")
    if tail and "/" not in tail:
        return "transcript"
    return ""


def _check_health(client: httpx.Client, data: Path) -> tuple[str, str]:
    health = client.get("/api/health", timeout=10)
    if health.status_code != 200:
        return "FAIL", f"health {health.status_code}"
    body = health.json()
    if body.get("ok") is not True:
        return "FAIL", "health was not ok"
    if body.get("version") != __version__:
        return "FAIL", f"version {body.get('version')}"
    try:
        served = Path(body.get("data_dir") or "").resolve()
    except OSError:
        served = Path(body.get("data_dir") or "")
    if served != data.resolve():
        return "FAIL", "health data_dir is not the copy"
    endpoints = client.get("/api/endpoints", timeout=10)
    if endpoints.status_code != 200 or not isinstance(endpoints.json(), list):
        return "FAIL", "endpoints did not list"
    sandbox = client.get("/api/sandbox", timeout=10)
    if sandbox.status_code != 200 or "status" not in sandbox.json():
        return "FAIL", "sandbox did not answer"
    return "PASS", f"version {body.get('version')}, {len(endpoints.json())} endpoint(s)"


def _create_bot(client: httpx.Client) -> str:
    endpoints = client.get("/api/endpoints", timeout=10).json()
    if not endpoints:
        return ""
    created = client.post(
        "/api/bots",
        json={"name": BOT_NAME, "endpoint_id": endpoints[0]["id"]},
        timeout=10,
    )
    if created.status_code != 200:
        return ""
    bot = created.json()
    if not str(bot.get("name") or "").startswith(BOT_PREFIX):
        return ""
    return str(bot.get("id") or "")


def _check_chat(client: httpx.Client, bot_id: str) -> tuple[str, str]:
    text, failed = _say(client, bot_id, "Reply with the single word pong.")
    if failed:
        return "FAIL", _one_line(text or "the chat failed")
    if "pong" not in text.lower():
        return "FAIL", _one_line(text or "the reply was empty")
    return "PASS", "pong"


def _check_whoami(client: httpx.Client, bot_id: str) -> tuple[str, str]:
    account = getpass.getuser()
    text, failed = _say(
        client,
        bot_id,
        "Use a shell fence to run whoami. Then tell me the exact account name from the output.",
    )
    if failed:
        return "FAIL", _one_line(text or "the tool chat failed")
    if not account or account.casefold() not in text.casefold():
        return "FAIL", _one_line(text or "the account name was not in the reply")
    return "PASS", account


def _say(client: httpx.Client, bot_id: str, content: str) -> tuple[str, bool]:
    ongoing = client.get(f"/api/bots/{bot_id}/ongoing", timeout=10)
    if ongoing.status_code != 200:
        return ongoing.text, True
    chat_id = ongoing.json().get("id")
    sent = client.post(
        f"/api/bots/{bot_id}/chats/{chat_id}/messages",
        json={"content": content},
    )
    if sent.status_code != 200:
        return sent.text, True
    body = sent.json()
    messages = (body.get("chat") or {}).get("messages") or []
    last = messages[-1] if messages else {}
    text = str(last.get("content") or body.get("reply") or "")
    if last.get("role") != "assistant" or last.get("error"):
        return text, True
    return text, False


def _check_files(store, bot_id: str) -> tuple[str, str]:
    from easyagent import turn as turn_mod
    from easyagent.tools import ToolRequest, execute
    from easyagent.workspace import bot_workspace

    async def once():
        folder = bot_workspace(store, bot_id, create=True)
        slot = turn_mod.bind(store, "selftest-files", bot_id=bot_id)
        slot.cwd = str(folder)
        note = "selftest-note.txt"
        body = "selftest note"
        await execute(store, ToolRequest(kind="files", action="write", path=note, body=body), bot_id)
        listed = await execute(store, ToolRequest(kind="files", action="list", path=str(folder)), bot_id)
        read = await execute(store, ToolRequest(kind="files", action="read", path=note), bot_id)
        return listed, read, body, note

    listed, read, body, note = asyncio.run(once())
    if note not in listed or body not in read:
        return "FAIL", "list, read, or write missed the workspace note"
    return "PASS", note


def _check_search(store) -> tuple[str, str]:
    from easyagent.search import SearchError, citation_problems, sources_from_text, web_search

    async def once():
        return await asyncio.wait_for(web_search("lighthouse notes", store=store), timeout=30)

    try:
        text = asyncio.run(once())
    except (SearchError, asyncio.TimeoutError) as exc:
        return "FAIL", _one_line(str(exc) or "search failed")
    sources = sources_from_text(text)
    if not sources or not str(sources[0].get("url") or "").startswith("http"):
        return "FAIL", "search returned no cited source"
    word = _support_word(sources[0])
    if not word:
        return "FAIL", "the source had no word to cite"
    answer = f"The page mentions {word} [1]."
    problems = citation_problems(answer, sources)
    if problems:
        return "FAIL", _one_line(problems[0])
    if not citation_problems("No citation in this sentence.", sources):
        return "FAIL", "a missing citation was accepted"
    return "PASS", f"cited {word}"


def _support_word(source: dict) -> str:
    from easyagent.search import _STOP, _WORD

    hay = " ".join(str(source.get(key) or "") for key in ("title", "snippet", "text"))
    for word in _WORD.findall(hay):
        token = word.lower().strip("-'")
        if token not in _STOP and len(token) >= 5:
            return token
    return ""


def _seed_long_chat(store, bot_id: str, count: int = 60) -> None:
    from easyagent.store import new_id, now_iso

    chat = store.ongoing_chat(bot_id)
    messages = list(chat.get("messages") or [])
    line = " ".join(["selftest"] * 12)
    block = "\n".join([line] * 8)
    for index in range(count):
        messages.append({
            "id": new_id(),
            "role": "assistant",
            "content": f"Note {index + 1}\n{block}",
            "created_at": now_iso(),
        })
    chat["messages"] = messages
    chat["updated_at"] = now_iso()
    run = chat.get("run") if isinstance(chat.get("run"), dict) else {}
    run["status"] = "idle"
    chat["run"] = run
    store.save_chat(chat)


def _check_routines(client: httpx.Client, bot_id: str) -> tuple[str, str]:
    from easyagent.schedule import _time_of_day

    moment = datetime.now().astimezone() + timedelta(hours=5)
    daily = _time_of_day(moment.hour, moment.minute)
    created = client.post(
        f"/api/bots/{bot_id}/schedules",
        json={
            "name": "selftest-routine",
            "prompt": "Reply with the single word routine-ok.",
            "daily": daily,
        },
        timeout=10,
    )
    if created.status_code != 200:
        return "FAIL", _one_line(created.text)
    schedule = created.json()
    status, detail = judge_routine_preview(schedule)
    if status != "PASS":
        return status, detail
    ran = client.post(
        f"/api/bots/{bot_id}/schedules/{schedule['id']}/run",
        timeout=_CHAT_TIMEOUT,
    )
    if ran.status_code != 200:
        return "FAIL", _one_line(ran.text)
    entry = ran.json()
    output = str(entry.get("output") or entry.get("result") or "")
    if entry.get("status") == "error" or "routine-ok" not in output.lower():
        return "FAIL", _one_line(entry.get("error") or output or "the routine did not answer")
    return "PASS", detail


def judge_routine_preview(schedule: dict) -> tuple[str, str]:
    """The preview names the next run in local time."""
    from easyagent.schedule import _clock, local_zone_name, next_run, zone_for

    zone_name = str(schedule.get("timezone") or "")
    local = local_zone_name()
    if zone_name != local:
        return "FAIL", f"timezone {zone_name or 'missing'} is not local time"
    text = str(schedule.get("preview") or "")
    nxt = next_run(schedule, datetime.now().astimezone())
    if nxt is None or "next:" not in text:
        return "FAIL", "the preview has no next run"
    zone = zone_for(schedule)
    clock = _clock(nxt.astimezone(zone) if zone else nxt.astimezone())
    if clock not in text:
        return "FAIL", f"preview missing local clock {clock}"
    return "PASS", text


def _check_secrets() -> tuple[str, str]:
    """Migrate a throwaway store. The plaintext key must not remain in that data dir."""
    from easyagent.secrets import endpoint_account, get_secret, migrate_store
    from easyagent.store import Store

    key = "ea-" + "selftest-" + "fake-" + "key"
    root = Path(tempfile.mkdtemp(prefix="easyagent-selftest-secrets-"))
    backup = Path(tempfile.mkdtemp(prefix="easyagent-selftest-backup-"))
    endpoint_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    saved = {
        "EASYAGENT_KEYRING": os.environ.get("EASYAGENT_KEYRING"),
        "EASYAGENT_SECRETS_PASSPHRASE": os.environ.get("EASYAGENT_SECRETS_PASSPHRASE"),
        "EASYAGENT_MIGRATION_BACKUP": os.environ.get("EASYAGENT_MIGRATION_BACKUP"),
    }
    try:
        os.environ["EASYAGENT_KEYRING"] = "passphrase"
        os.environ["EASYAGENT_SECRETS_PASSPHRASE"] = "selftest-passphrase"
        os.environ["EASYAGENT_MIGRATION_BACKUP"] = str(backup)
        (root / "endpoints.json").write_text(
            json.dumps([{
                "id": endpoint_id,
                "name": "Local model",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": key,
                "model": "your-model",
            }]),
            encoding="utf-8",
        )
        store = Store(root)
        store.ensure()
        hits = _grep_count(root, key.encode("utf-8"))
        if hits != 0:
            return "FAIL", f"plaintext hits {hits}"
        if get_secret(endpoint_account(endpoint_id), store) != key:
            return "FAIL", "the migrated key did not round-trip"
        before = (root / "endpoints.json").read_bytes()
        migrate_store(store)
        if (root / "endpoints.json").read_bytes() != before:
            return "FAIL", "a second migration changed the file"
        if _grep_count(root, key.encode("utf-8")) != 0:
            return "FAIL", "plaintext hits after the second pass"
        return "PASS", "0 plaintext hits"
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)


def _check_honesty() -> tuple[str, str]:
    from easyagent.honesty import run_scripted

    def ok(_call):
        return "1 passed\nexit code 0"

    async def once():
        backed = await run_scripted(
            ['TOOL shell command="pytest -q"', "Done."],
            "run it",
            ok,
        )
        bare = await run_scripted(["Fixed."], "repair it", ok)
        return backed, bare

    backed, bare = asyncio.run(once())
    receipts = backed.get("receipts") or []
    if backed.get("unverified") or not receipts or "1 passed" not in str(receipts[0].get("output") or ""):
        return "FAIL", "a backed claim did not keep a receipt"
    if not bare.get("unverified"):
        return "FAIL", "an unbacked claim was treated as verified"
    return "PASS", "receipt kept, bare claim unverified"


def _grep_count(root: Path, needle: bytes) -> int:
    count = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        count += path.read_bytes().count(needle)
    return count


def _check_copy(root: Path, before: dict[str, str]) -> tuple[str, str]:
    after = _fingerprint(root)
    changed = sorted(path for path, digest in before.items() if after.get(path) != digest)
    extra = sorted(path for path in after if path not in before)
    missing = sorted(path for path in before if path not in after)
    if not changed and not extra and not missing:
        return "PASS", "unchanged aside from selftest bots"
    shown = (changed + extra + missing)[:4]
    return "FAIL", "copy changed: " + ", ".join(shown)


def _fingerprint(root: Path) -> dict[str, str]:
    skip = _selftest_bot_ids(root)
    found: dict[str, str] = {}
    if not root.is_dir():
        return found
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if _under_selftest(rel, skip) or rel.endswith(_SIDECAR_SUFFIXES):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        found[rel] = digest.hexdigest()
    return found


def _drop_extras(root: Path, before: dict[str, str]) -> None:
    skip = _selftest_bot_ids(root)
    for path in list(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in before or _under_selftest(rel, skip):
            continue
        if rel == "host.json" or rel.startswith("skills/") or rel.endswith(_SIDECAR_SUFFIXES):
            path.unlink(missing_ok=True)


def _selftest_bot_ids(root: Path) -> set[str]:
    found: set[str] = set()
    bots = root / "bots"
    if not bots.is_dir():
        return found
    for path in bots.iterdir():
        meta = path / "bot.json"
        if not path.is_dir() or path.is_symlink() or not meta.is_file():
            continue
        try:
            bot = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(bot.get("name") or "").startswith(BOT_PREFIX):
            found.add(path.name)
    return found


def _under_selftest(rel: str, ids: set[str]) -> bool:
    parts = rel.split("/")
    return len(parts) >= 2 and parts[0] == "bots" and parts[1] in ids


def _delete_selftest_bots(store) -> None:
    try:
        bots = store.list_bots()
    except Exception:
        return
    for bot in bots:
        name = str(bot.get("name") or "")
        if name.startswith(BOT_PREFIX):
            try:
                store.delete_bot(bot["id"], name)
            except Exception:
                continue


def _capture_bytes(root: Path) -> dict[str, bytes | None]:
    """Files the page rewrites outside a bot directory. Restored after the probe."""
    saved: dict[str, bytes | None] = {}
    for name in ("unread.json",):
        path = root / name
        if path.is_file() and not path.is_symlink():
            saved[name] = path.read_bytes()
        else:
            saved[name] = None
    return saved


def _restore_bytes(root: Path, saved: dict[str, bytes | None]) -> None:
    for name, blob in saved.items():
        path = root / name
        if blob is None:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        else:
            path.write_bytes(blob)


def _scrub_ledger(root: Path, ids: set[str]) -> None:
    """Drop ledger rows for bots this gate created. Other rows stay."""
    if not ids:
        return
    index = root / "skills" / "_ledger" / "index.json"
    if not index.is_file() or index.is_symlink():
        return
    try:
        rows = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(rows, list):
        return
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            kept.append(row)
            continue
        bot_id = str(row.get("bot_id") or "")
        key = str(row.get("key") or "")
        if bot_id in ids or any(key.startswith(f"{item}/") for item in ids):
            continue
        kept.append(row)
    if len(kept) == len(rows):
        return
    index.write_text(json.dumps(kept, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _delete_selftest_bots_on_disk(root: Path) -> None:
    bots = root / "bots"
    if not bots.is_dir():
        return
    for bot_id in _selftest_bot_ids(root):
        folder = bots / bot_id
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder, ignore_errors=True)


def _live_dirs() -> list[Path]:
    found: list[Path] = []
    try:
        found.append(default_data_dir().resolve())
    except OSError:
        pass
    try:
        response = httpx.get("http://127.0.0.1:44721/api/health", timeout=0.4)
        if response.status_code == 200:
            raw = response.json().get("data_dir")
            if raw:
                found.append(Path(str(raw)).resolve())
    except Exception:
        pass
    unique: list[Path] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return unique


def _overlaps(left: Path, right: Path) -> bool:
    try:
        left = left.resolve()
        right = right.resolve()
    except OSError:
        return False
    return left == right or left in right.parents or right in left.parents


def _port_busy(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        return True
    finally:
        sock.close()
    return False


def _parse(argv: list[str]) -> dict:
    if argv in (["-h"], ["--help"]):
        return {"help": True}
    parser = argparse.ArgumentParser(prog="python -m easyagent selftest all", add_help=False)
    parser.add_argument("--data")
    parser.add_argument("--port")
    parser.add_argument("--report")
    parser.add_argument("--consent", action="store_true")
    try:
        parsed, extra = parser.parse_known_args(argv)
    except SystemExit:
        return {"error": True}
    if extra or not parsed.data or parsed.port is None:
        return {"error": True}
    if not str(parsed.port).isdigit():
        return {"error": True}
    return {
        "data": parsed.data,
        "port": int(parsed.port),
        "report": parsed.report,
        "consent": bool(parsed.consent),
    }


def _timed(name: str, fn) -> dict:
    started = time.perf_counter()
    try:
        status, detail = fn()
    except Exception as exc:
        status, detail = "FAIL", f"{exc.__class__.__name__}: {exc}"
    return _row(name, status, detail, time.perf_counter() - started)


def _row(name: str, status: str, detail: str, seconds: float) -> dict:
    return {
        "name": name,
        "status": status,
        "detail": _one_line(detail),
        "seconds": round(seconds, 3),
    }


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())[:240]


def _format_table(checks: list[dict]) -> str:
    lines = []
    for item in checks:
        lines.append(f"{item['name']:<14} {item['status']:<4}  {item['seconds']:7.2f}s  {item['detail']}")
    return "\n".join(lines)


def _write_report(path: Path | None, report: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


class _Server:
    def __init__(self, data: Path, port: int) -> None:
        self.data = data
        self.port = port
        self.error = ""
        self.store = None
        self.server = None
        self.thread: threading.Thread | None = None

    def start(self) -> bool:
        import uvicorn

        try:
            app = create_app(self.data)
        except Exception as exc:
            self.error = str(exc)
            return False
        self.store = app.state.store
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="easyagent-selftest", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{self.port}/api/health", timeout=0.5)
                if response.status_code == 200:
                    return True
            except Exception:
                if self.thread and not self.thread.is_alive():
                    break
            time.sleep(0.1)
        self.error = "the server did not answer /api/health"
        return False

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=8)
            if self.thread.is_alive() and self.server is not None:
                self.server.force_exit = True
                self.thread.join(timeout=3)
