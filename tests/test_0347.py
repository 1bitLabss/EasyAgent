"""0.3.47: connection honesty, a fair job queue, and a room that streams."""

import asyncio
import json

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.generation import GenerationLimit, is_local_base, limits_from_endpoint, still_working_text
from easyagent.subagent import consult_target


def _events(body: str) -> list[dict]:
    rows = []
    for block in body.split("\n\n"):
        data = "\n".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
        if data:
            rows.append(json.loads(data))
    return rows


def test_local_waits_longer_for_the_first_word_and_the_whole_reply():
    local = limits_from_endpoint({"base_url": "http://127.0.0.1:9/v1"})
    cloud = limits_from_endpoint({"base_url": "https://example.com/v1"})
    assert is_local_base("http://192.168.1.8:1234/v1")
    assert local.first_token_seconds == 600
    assert local.hard_seconds == 900
    assert cloud.first_token_seconds == 120
    assert cloud.hard_seconds == 480
    assert still_working_text(600).startswith("Model is busy/slow")
    assert "Can't reach" not in still_working_text(300)


def test_a_slow_first_word_is_not_a_lost_connection(tmp_path, monkeypatch):
    from easyagent.generation import GenerationLimit as Limit

    async def stream_complete(**_kwargs):
        raise Limit("first_token")
        yield ""

    monkeypatch.setattr("easyagent.llm.stream_complete", stream_complete)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    response = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "hello"},
        headers={"Accept": "text/event-stream"},
    )
    assert response.status_code == 200, response.text
    text = response.text
    assert "Can't reach the model" not in text
    assert "Model is busy/slow" in text
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    kinds = [item.get("kind") for item in saved["messages"] if item.get("role") == "assistant"]
    assert "still-working" in kinds
    assert "connection" not in kinds
    client.close()


def test_terminal_schema_names_the_timeout():
    from easyagent.llm import NATIVE_TOOLS

    terminal = next(tool for tool in NATIVE_TOOLS if tool["function"]["name"] == "terminal")
    timeout = terminal["function"]["parameters"]["properties"]["timeout"]
    assert "120" in timeout["description"]
    assert "900" in timeout["description"]


def test_a_chat_can_pass_a_waiting_job_once(monkeypatch):
    import easyagent.gate as gate

    gate.reset_lanes()
    monkeypatch.setenv("EASYAGENT_ROUTINE_MAX_YIELD_SECONDS", "60")

    async def hold():
        token = gate.bind_connection({"id": "local", "name": "local", "max_parallel": 1}, "Wire")
        seat = await gate.reserve()
        await seat.acquire()
        await asyncio.sleep(0.2)
        await seat.release()
        gate.reset_connection(token)

    async def job():
        await asyncio.sleep(0.02)
        token = gate.bind_connection({"id": "local", "name": "local", "max_parallel": 1}, "Cronny")
        seat = await gate.reserve(background=True)
        assert seat.waiting
        assert seat.label.startswith("Queued:")
        await asyncio.sleep(0.1)
        assert seat.overtaken
        await seat.acquire()
        await seat.release()
        gate.reset_connection(token)

    async def chat(name: str, delay: float):
        await asyncio.sleep(delay)
        token = gate.bind_connection({"id": "local", "name": "local", "max_parallel": 1}, name)
        seat = await gate.reserve()
        await seat.acquire()
        await seat.release()
        gate.reset_connection(token)
        return name

    async def scenario():
        await asyncio.gather(hold(), job(), chat("Ada", 0.05), chat("Bea", 0.08))

    asyncio.run(scenario())


def test_a_finished_one_shot_is_done(tmp_path):
    from easyagent.schedule import compile_routine, routine_views
    from easyagent.store import Store

    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    record = compile_routine({
        "name": "Once soon",
        "prompt": "Say the time.",
        "once": "2026-10-23 09:00",
        "timezone": "UTC",
        "created_at": "2026-10-01T00:00:00+00:00",
    })
    saved = store.add_schedule(bot["id"], record)
    rows = store.list_schedules(bot["id"])
    for row in rows:
        if row["id"] == saved["id"]:
            row["paused"] = True
            row["finished"] = True
            row["last_run_at"] = "2026-10-23T14:00:00+00:00"
            row["last_status"] = "ok"
    store.save_schedules(bot["id"], rows)
    views = routine_views(store)
    match = next(item for item in views if item["id"] == saved["id"])
    assert match["state"] == "done"


def test_a_job_that_has_not_run_is_not_ok_and_pause_clears_next(tmp_path):
    from easyagent.schedule import compile_routine, routine_views
    from easyagent.store import Store

    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    record = compile_routine({
        "name": "Inbox sweep",
        "prompt": "Check the inbox.",
        "cron": "*/5 * * * *",
        "timezone": "UTC",
        "created_at": "2026-10-01T00:00:00+00:00",
    })
    saved = store.add_schedule(bot["id"], record)
    match = next(item for item in routine_views(store) if item["id"] == saved["id"])
    assert match["state"] == "not run yet"
    rows = store.list_schedules(bot["id"])
    for row in rows:
        if row["id"] == saved["id"]:
            row["paused"] = True
            row["next_run"] = "2026-10-29T13:45:00+00:00"
    store.save_schedules(bot["id"], rows)
    paused = next(item for item in routine_views(store) if item["id"] == saved["id"])
    assert paused["state"] == "paused"
    assert paused["next_run"] is None


def test_room_streams_and_a_private_send_is_not_a_500(tmp_path, monkeypatch):
    calls = {"n": 0}

    async def complete(**_kwargs):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return f"Reply {calls['n']}."

    monkeypatch.setattr("easyagent.llm.complete", complete)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1", "max_parallel": 2}).json()
    alpha = client.post("/api/bots", json={"name": "Alpha", "endpoint_id": endpoint["id"]}).json()
    bravo = client.post("/api/bots", json={"name": "Bravo", "endpoint_id": endpoint["id"]}).json()
    for bot in (alpha, bravo):
        client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    room = client.post("/api/rooms", json={"name": "Picnic"}).json()
    client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": alpha["id"]})
    client.post(f"/api/rooms/{room['id']}/bots", json={"bot_id": bravo["id"]})
    private = client.post(f"/api/bots/{bravo['id']}/chats").json()
    with client.stream(
        "POST",
        f"/api/rooms/{room['id']}/messages",
        json={"content": "picnic plan"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    events = _events(body)
    kinds = [item.get("type") for item in events]
    assert "user" in kinds
    assert "bot_status" in kinds
    assert kinds.index("user") < kinds.index("message")
    speakers = [item["message"].get("speaker_name") for item in events if item.get("type") == "message"]
    assert speakers == ["Alpha", "Bravo"] or set(speakers) == {"Alpha", "Bravo"}
    sent = client.post(
        f"/api/bots/{bravo['id']}/chats/{private['id']}/messages",
        json={"content": "a private note"},
    )
    assert sent.status_code == 200, sent.text
    assert "A room turn changed" not in sent.text
    saved = client.get(f"/api/rooms/{room['id']}").json()
    assert any(item.get("speaker_name") == "Alpha" for item in saved["messages"])
    mentioned = client.post(
        f"/api/rooms/{room['id']}/messages",
        json={"content": "@Alpha bring the basket"},
        headers={"Accept": "text/event-stream"},
    )
    assert mentioned.status_code == 200, mentioned.text
    named = [item["message"].get("speaker_name") for item in _events(mentioned.text) if item.get("type") == "message"]
    assert named == ["Alpha"]
    client.close()


def test_consult_from_a_side_message_names_the_bot():
    assert consult_target("ask Kiln to count the boats", ["Kiln", "Ada"]) == ("Kiln", "count the boats")
    assert consult_target("what is the tide", ["Kiln"]) is None


def test_local_retry_budget_is_ten_minutes():
    from easyagent.retry import Window

    local = Window(base_url="http://127.0.0.1:9/v1")
    cloud = Window(base_url="https://example.com/v1")
    assert local.budget == 600
    assert cloud.budget == 120


def test_the_workspace_is_the_only_your_folder(tmp_path):
    from easyagent.selfinfo import own_files_prompt
    from easyagent.store import Store

    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    text = own_files_prompt(store, bot["id"])
    assert text.splitlines()[0].startswith("- Your folder:")
    assert "internal data for this bot (off-limits)" in text
    assert text.count("Your folder:") == 1


def test_a_data_dir_refusal_names_the_workspace_once(tmp_path):
    from easyagent.safety import _not_run
    from easyagent.store import Store
    from easyagent import turn as turn_mod

    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    slot = turn_mod.slot_for(store, "chat")
    slot.bot_id = bot["id"]
    slot.store = store
    token = turn_mod._slot.set(slot)
    try:
        text = _not_run("That reads EasyAgent's saved data. It was not run.")
    finally:
        turn_mod._slot.reset(token)
    assert text.count("It was not run.") == 1
    assert "Your folder is" in text


def test_a_wide_tool_result_keeps_the_head_and_the_tail():
    from easyagent.tools import ToolRequest, model_slice

    body = "HEAD-MARK " + ("x" * 9000) + " TAIL-MARK"
    sliced = model_slice(None, ToolRequest(kind="fetch", body="https://example.com"), body, [])
    assert "HEAD-MARK" in sliced
    assert "TAIL-MARK" in sliced
    assert len(sliced) <= 8000


def test_system_prompt_stays_under_eight_thousand_characters(monkeypatch):
    import sys

    from easyagent.prompt import _shell_note, build_system

    text = build_system(bot_name="Ada", direction="", summary="", skills_text="", workspace="/tmp/work")
    # The shared prompt stays under 8200 (raised from 8000 for the browser
    # credential and upload lines); the per-platform shell note is extra.
    assert len(text) - len(_shell_note()) <= 8200
    monkeypatch.setattr(sys, "platform", "win32")
    assert len(_shell_note()) <= 400
    assert "Never say you can't" not in text
    assert "python tool" in text or "python" in text


def test_python_and_ask_bot_are_native_tools():
    from easyagent.llm import NATIVE_TOOLS

    names = {tool["function"]["name"] for tool in NATIVE_TOOLS}
    assert "python" in names
    assert "ask_bot" in names
    assert "helper" in names


def test_a_blocked_search_falls_back_without_raising(monkeypatch):
    import asyncio
    from easyagent.search import SearchError, research

    async def blocked(_query, store=None):
        raise SearchError("DuckDuckGo: the search provider blocked this request; try again or configure a provider")

    async def html(query):
        return [{"title": "Pier", "url": "https://example.com/pier", "snippet": "quiet"}]

    monkeypatch.setattr("easyagent.search.web_search", blocked)
    monkeypatch.setattr("easyagent.search._duckduckgo_html_items", html)
    text = asyncio.run(research("pier tide"))
    assert "blocked" in text.lower()
    assert "Pier" in text or "example.com" in text


def test_heartbeat_comment_is_not_an_event():
    from easyagent.app import _heartbeat

    async def source():
        yield "data: {\"type\": \"status\"}\n\n"
        await asyncio.sleep(0.05)

    async def run():
        # The helper yields source items. A short source does not need a ping.
        found = []
        async for item in _heartbeat(source()):
            found.append(item)
        return found

    rows = asyncio.run(run())
    assert any("status" in item for item in rows)
    assert not any(item.startswith("data:") and "ping" in item for item in rows)


def test_a_follow_up_after_a_finished_answer_is_live_and_has_no_pause_row(tmp_path, monkeypatch):
    """The next message after a saved answer is its own turn. It is not queued or paused."""
    import easyagent.llm as llm

    calls = {"n": 0}

    async def stream_complete(**_kwargs):
        calls["n"] += 1
        yield f"Answer {calls['n']}."

    async def complete(**_kwargs):
        calls["n"] += 1
        return f"Answer {calls['n']}."

    monkeypatch.setattr(llm, "stream_complete", stream_complete)
    monkeypatch.setattr(llm, "complete", complete)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    client.patch(f"/api/bots/{bot['id']}", json={"check_enabled": False})
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    headers = {"Accept": "text/event-stream"}
    with client.stream("POST", url, json={"content": "one"}, headers=headers) as response:
        assert response.status_code == 200, response.read()
        response.read()
    with client.stream("POST", url, json={"content": "two"}, headers=headers) as response:
        assert response.status_code == 200, response.read()
        second = response.read().decode()
    saved = client.get(f"/api/bots/{bot['id']}/chats/{chat['id']}").json()
    texts = [item.get("content") for item in saved["messages"]]
    assert "Answer 1." in texts
    assert "Answer 2." in texts
    for stale in ("Paused, say continue to resume", "Paused, you sent a message", "Finished before pausing"):
        assert stale not in texts
    assert saved["run"]["status"] != "paused"
    done = [item for item in _events(second) if item.get("type") == "done"]
    assert done
    live = [item.get("content") for item in done[-1]["chat"]["messages"]]
    assert "Answer 2." in live
    assert "Paused, say continue to resume" not in live
    client.close()


def test_a_routine_line_reaches_an_open_empty_chat(tmp_path):
    """A chat with no live turn still hears the routine on /feed."""
    import socket
    import threading
    import urllib.request

    import uvicorn

    app = create_app(tmp_path)
    store = app.state.store
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    def serve() -> None:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=0.2)
            break
        except Exception:
            thread.join(0.05)
    endpoint = json.loads(urllib.request.urlopen(urllib.request.Request(
        base + "/api/endpoints",
        data=json.dumps({"name": "local", "base_url": "http://127.0.0.1:9/v1"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    ), timeout=2).read())
    bot = json.loads(urllib.request.urlopen(urllib.request.Request(
        base + "/api/bots",
        data=json.dumps({"name": "Tick", "endpoint_id": endpoint["id"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    ), timeout=2).read())
    chat = json.loads(urllib.request.urlopen(urllib.request.Request(
        base + f"/api/bots/{bot['id']}/chats",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    ), timeout=2).read())
    assert chat["messages"] == []
    found: list[str] = []
    opened = threading.Event()

    def listen() -> None:
        req = urllib.request.Request(
            base + f"/api/bots/{bot['id']}/chats/{chat['id']}/feed",
            headers={"Accept": "text/event-stream"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            opened.set()
            while True:
                line = response.readline().decode()
                if not line:
                    return
                found.append(line)
                if "Tick once says hello." in line:
                    return

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    assert opened.wait(3)
    store.append_routine_message(bot["id"], "Tick once says hello.", routine_name="Tick once")
    listener.join(3)
    text = "".join(found)
    assert "Tick once says hello." in text, text
    assert "Tick once" in text


def test_a_room_approval_is_shown_and_the_tool_runs(tmp_path, monkeypatch):
    """A card raised in a room carries the room id, and approving it runs the tool."""
    from easyagent import safety, turn as turn_mod
    from easyagent.app import _room_pending
    from easyagent.tools import ToolRequest

    # Earlier tests leave YOLO, unattended mode, and a short card timer behind.
    safety.reset_for_tests()
    safety._UNATTENDED.set(False)
    monkeypatch.delenv("EASYAGENT_YOLO", raising=False)
    monkeypatch.delenv("EASYAGENT_YOLO_FLOOR", raising=False)
    monkeypatch.setattr("easyagent.yolo.passes", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("easyagent.safety.is_unattended", lambda: False)
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "30")
    app = create_app(tmp_path)
    store = app.state.store
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None)
    bot = store.add_bot(name="Strict", endpoint_id=endpoint["id"], model=None)
    room = store.create_room(name="Lab")
    store.add_room_bot(room["id"], bot["id"])

    async def run():
        async def raised():
            token = turn_mod.push_room_chat(room["id"])
            try:
                return await safety.guard(
                    store, ToolRequest(kind="shell", command="python3 -c 'print(42)'"), bot["id"]
                )
            finally:
                turn_mod.reset_room_chat(token)

        task = asyncio.create_task(raised())
        await asyncio.wait({task}, timeout=1)
        cards = safety.list_pending(bot["id"])
        if not cards:
            if task.done():
                exc = task.exception()
                raise AssertionError(f"no card; guard finished with {exc!r}; unattended={safety.is_unattended()}")
            raise AssertionError(f"no card while the guard is still waiting; unattended={safety.is_unattended()}")
        assert cards[0].get("chat_id") == room["id"]
        assert _room_pending(store.get_room(room["id"]))
        assert safety.resolve_card(cards[0]["id"], "approve", bot["id"]) is not None
        result = await asyncio.wait_for(task, timeout=10)
        assert "42" in str(result)

    asyncio.run(run())
