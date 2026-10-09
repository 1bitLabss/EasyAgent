"""Idle notes: caps, dedupe, provenance, secrets, prune, and the overnight gate."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from easyagent.journal import (
    NOTE_CAP,
    NOTE_NAMES,
    Entry,
    commit_entries,
    find_leaks,
    learning_extra,
    nightly_idle,
    note_block,
    prune_expired,
    prune_plan,
    read_entries,
    run_pass,
    save_retention,
    scrub_text,
)
from easyagent.judge import check_revision_limit
from easyagent.learn import _candidate_path, mark_origin, rollback_latest, save_candidate
from easyagent.store import Store, StoreError


WHEN = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)


def _world(tmp_path, *, key="endpoint-key-998877"):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(
        name="kiln",
        base_url="http://10.1.2.3:11434/v1",
        api_key=key,
        model="kiln-model",
    )
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="kiln-model")
    return store, endpoint, bot


def _chat(store, bot_id, messages):
    chat = store.create_chat(bot_id)
    chat["messages"] = messages
    return store.save_chat(chat)


def _msg(mid, role, content, stamp):
    return {"id": mid, "role": role, "content": content, "created_at": stamp}


def _silence(monkeypatch, scripted=""):
    calls = []

    async def complete(*, base_url, api_key, model, messages, timeout=120, **_extra):
        calls.append(messages[0]["content"])
        if "Summarize only" in messages[0]["content"]:
            return "no"
        return scripted

    monkeypatch.setattr("easyagent.llm.complete", complete)
    return calls


def _notes(store, bot_id):
    return store.root / "bots" / bot_id / "notes"


def test_caps_dedupe_and_provenance(tmp_path):
    store, _endpoint, bot = _world(tmp_path)
    messages = [
        _msg("cite-old", "user", "ceramic drawer", "2026-01-01T01:00:00+00:00"),
        _msg("cite-mid", "user", "ceramic drawer", "2026-01-02T01:00:00+00:00"),
        _msg("cite-new", "user", "ceramic drawer", "2026-01-03T01:00:00+00:00"),
        _msg("goodcite", "user", "ceramic drawer", "2026-01-04T01:00:00+00:00"),
        _msg("samecite", "user", "ceramic drawer", "2026-01-05T01:00:00+00:00"),
    ]
    _chat(store, bot["id"], messages)
    folder = _notes(store, bot["id"])
    folder.mkdir(parents=True)
    original = "# Mistakes\n\nLeave this sentence.\n"
    (folder / "MISTAKES.md").write_text(original, encoding="utf-8")
    fat = "ceramic " * 200
    commit_entries(
        store,
        bot["id"],
        [
            Entry(kind="mistake", title="Old", body=fat, cites=["cite-old"], dates=["2026-01-01"], source="derived"),
            Entry(kind="mistake", title="Mid", body=fat, cites=["cite-mid"], dates=["2026-01-02"], source="derived"),
            Entry(kind="mistake", title="New", body=fat, cites=["cite-new"], dates=["2026-01-03"], source="derived"),
        ],
        messages,
    )
    text = (folder / "MISTAKES.md").read_text(encoding="utf-8")
    assert text.startswith(original)
    assert "Leave this sentence." in text
    assert len(text) <= NOTE_CAP
    assert "[m:cite-new]" in text
    assert "[m:cite-old]" not in text

    commit_entries(
        store,
        bot["id"],
        [
            Entry(kind="mistake", title="Short", body="short", cites=["samecite"], dates=["2026-01-05"], source="derived"),
            Entry(
                kind="mistake",
                title="Longer",
                body="the longer ceramic fix",
                cites=["samecite"],
                dates=["2026-01-05"],
                source="derived",
            ),
        ],
        messages,
    )
    merged = read_entries((folder / "MISTAKES.md").read_text(encoding="utf-8"))
    same = [entry for entry in merged if "samecite" in entry.cites]
    assert len(same) == 1
    assert "longer ceramic" in same[0].body
    commit_entries(
        store,
        bot["id"],
        [Entry(kind="mistake", title="Longer", body="the longer ceramic fix", cites=["samecite"], dates=["2026-01-05"])],
        messages,
    )
    again = [entry for entry in read_entries((folder / "MISTAKES.md").read_text(encoding="utf-8")) if "samecite" in entry.cites]
    assert len(again) == 1

    commit_entries(
        store,
        bot["id"],
        [
            Entry(kind="mistake", title="Kept", body="ceramic drawer", cites=["goodcite"], dates=["2026-01-04"], source="derived"),
            Entry(kind="mistake", title="Missing", body="ceramic drawer", cites=["no-such-id"], dates=["2026-01-04"], source="model"),
            Entry(kind="mistake", title="Undated", body="ceramic drawer", cites=["goodcite"], dates=[], source="derived"),
        ],
        messages,
    )
    proven = (folder / "MISTAKES.md").read_text(encoding="utf-8")
    assert "[m:goodcite]" in proven
    assert "no-such-id" not in proven
    assert "Undated" not in proven


def test_secrets_are_not_written(tmp_path, monkeypatch):
    store, _endpoint, bot = _world(tmp_path)
    monkeypatch.setattr("easyagent.tools.secret_strings", lambda _store: ["vault-secret-value"])
    token = "sk-LIVEKEY123456789"  # fake-key-fixture
    messages = [_msg("m1", "user", f"ceramic drawer {token} vault-secret-value", "2026-10-08T01:00:00+00:00")]
    _chat(store, bot["id"], messages)
    leaked = f"ceramic drawer {token} vault-secret-value Bearer abcdefghijklmnop"
    assert find_leaks(leaked)
    assert token not in scrub_text(store, leaked)
    assert "vault-secret-value" not in scrub_text(store, leaked)
    assert not find_leaks(scrub_text(store, leaked))
    commit_entries(
        store,
        bot["id"],
        [
            Entry(kind="mistake", title="Leak", body=leaked, cites=["m1"], dates=["2026-10-08"], source="derived"),
            Entry(
                kind="world",
                title="kiln",
                body="Host: 10.1.2.3. Model: kiln-model. Key endpoint-key-998877.",
                cites=["observed"],
                dates=["2026-10-08"],
                source="observed",
            ),
        ],
        messages,
    )
    folder = _notes(store, bot["id"])
    blob = "\n".join((folder / name).read_text(encoding="utf-8") for name in NOTE_NAMES if (folder / name).is_file())
    assert token not in blob
    assert "vault-secret-value" not in blob
    assert "endpoint-key-998877" not in blob
    assert "abcdefghijklmnop" not in blob
    assert "10.1.2.3" in blob
    for name in NOTE_NAMES:
        path = folder / name
        if path.is_file():
            assert not find_leaks(path.read_text(encoding="utf-8"))


def test_prune_only_after_a_verified_digest(tmp_path, monkeypatch):
    _silence(monkeypatch)
    store, _endpoint, bot = _world(tmp_path)
    early = "2026-08-01T01:00:00+00:00"
    chat = _chat(
        store,
        bot["id"],
        [
            _msg("promiseid", "assistant", "I'll check the kiln later", early),
            _msg("helloidd", "user", "hello kiln", early),
        ],
    )
    asyncio.run(run_pass(store, bot, now=datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc), force=True))
    folder = _notes(store, bot["id"])
    promises = (folder / "PROMISES.md").read_text(encoding="utf-8")
    assert "[m:promiseid]" in promises
    assert "helloidd" not in promises
    marked = json.loads((folder / "digested.json").read_text(encoding="utf-8"))["messages"]
    assert any(row.get("message_id") == "promiseid" for row in marked.values())
    assert all(row.get("message_id") != "helloidd" for row in marked.values())

    chat = store.get_chat(bot["id"], chat["id"])
    chat["messages"].append(_msg("okidshort", "user", "ok", early))
    store.save_chat(chat)
    note = (folder / "MISTAKES.md").read_text(encoding="utf-8")
    note = note.replace("<!-- /ea:auto -->", "[m:okidshort]\n<!-- /ea:auto -->")
    (folder / "MISTAKES.md").write_text(note, encoding="utf-8")
    marked["x:okidshort"] = {"chat_id": chat["id"], "message_id": "okidshort", "created_at": early}
    (folder / "digested.json").write_text(json.dumps({"messages": marked}), encoding="utf-8")

    later = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
    pending = {item["message_id"] for item in prune_plan(store, bot["id"], later)}
    assert "promiseid" in pending
    assert "helloidd" not in pending
    assert "okidshort" not in pending

    save_retention(store, bot["id"], {"pruning": False})
    assert prune_expired(store, bot["id"], later) == []
    assert any(item.get("id") == "promiseid" for item in store.get_chat(bot["id"], chat["id"])["messages"])

    save_retention(store, bot["id"], {"pruning": True, "keep_forever": True})
    assert prune_expired(store, bot["id"], later) == []
    preview = learning_extra(store, bot)["prune"]
    assert preview["keep_forever"] is True
    assert any(item["message_id"] == "promiseid" for item in preview["pending"])

    kept = (folder / "PROMISES.md").read_text(encoding="utf-8")
    (folder / "PROMISES.md").write_text(kept.replace("[m:promiseid]", "[m:gone]"), encoding="utf-8")
    save_retention(store, bot["id"], {"keep_forever": False, "pruning": True})
    assert prune_expired(store, bot["id"], later) == []
    assert any(item.get("id") == "promiseid" for item in store.get_chat(bot["id"], chat["id"])["messages"])
    (folder / "PROMISES.md").write_text(kept, encoding="utf-8")

    dropped = prune_expired(store, bot["id"], later)
    assert dropped == ["promiseid"]
    left = [item.get("id") for item in store.get_chat(bot["id"], chat["id"])["messages"]]
    assert "promiseid" not in left
    assert "helloidd" in left
    assert "okidshort" in left
    try:
        chat = store.get_chat(bot["id"], chat["id"])
        chat["messages"] = chat["messages"][:-1]
        store.save_chat(chat)
    except StoreError as exc:
        assert "shorten" in str(exc).lower()
    else:
        raise AssertionError("A normal save shortened the transcript.")


def test_the_pass_runs_only_while_idle(tmp_path, monkeypatch):
    calls = _silence(monkeypatch)
    store, _endpoint, bot = _world(tmp_path)
    other = store.add_bot(name="Bea", endpoint_id=bot["endpoint_id"], model="kiln-model")
    marker = _notes(store, other["id"])
    marker.mkdir(parents=True)
    (marker / "ONLY.md").write_text("only-b\n", encoding="utf-8")
    memory = _notes(store, bot["id"])
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("# Memory\n\nkeep-me\n", encoding="utf-8")
    (memory / "USER.md").write_text("# User\n\nkeep-user\n", encoding="utf-8")
    hand = "# Mistakes\n\nI wrote this.\n"
    (memory / "MISTAKES.md").write_text(hand, encoding="utf-8")
    chat = _chat(
        store,
        bot["id"],
        [
            _msg("u1", "user", "the kiln needs a look", "2026-10-08T02:50:00+00:00"),
            _msg("a1", "assistant", "I'll check the kiln later", "2026-10-08T02:51:00+00:00"),
        ],
    )
    asyncio.run(nightly_idle(store, now=WHEN))
    assert calls == []
    assert not (memory / "PROMISES.md").exists()

    monkeypatch.setenv("EASYAGENT_NIGHTLY", "1")
    asyncio.run(nightly_idle(store, now=WHEN.replace(hour=15)))
    assert calls == []

    asyncio.run(nightly_idle(store, now=WHEN))
    assert calls == []
    log = (memory / "nightly-log.md").read_text(encoding="utf-8")
    assert "chatting" in log

    record = store.get_chat(bot["id"], chat["id"])
    record["run"] = {"status": "running"}
    record["messages"][0]["created_at"] = "2026-10-08T01:00:00+00:00"
    record["messages"][1]["created_at"] = "2026-10-08T01:01:00+00:00"
    store.save_chat(record)
    before = calls[:]
    asyncio.run(nightly_idle(store, now=WHEN))
    assert calls == before
    assert "running" in (memory / "nightly-log.md").read_text(encoding="utf-8")
    assert (marker / "ONLY.md").read_text(encoding="utf-8") == "only-b\n"
    assert not (marker / "MISTAKES.md").exists()

    record = store.get_chat(bot["id"], chat["id"])
    record["run"] = {"status": "idle"}
    store.save_chat(record)
    asyncio.run(nightly_idle(store, now=WHEN))
    assert len(calls) > len(before)
    promises = (memory / "PROMISES.md").read_text(encoding="utf-8")
    assert promises.startswith(hand) is False
    mistakes = (memory / "MISTAKES.md").read_text(encoding="utf-8")
    assert mistakes.startswith(hand)
    assert "[m:a1]" in promises
    assert (memory / "MEMORY.md").read_text(encoding="utf-8") == "# Memory\n\nkeep-me\n"
    assert (memory / "USER.md").read_text(encoding="utf-8") == "# User\n\nkeep-user\n"
    assert (marker / "ONLY.md").read_text(encoding="utf-8") == "only-b\n"
    assert "only-b" not in mistakes
    seen = len(calls)
    asyncio.run(nightly_idle(store, now=WHEN))
    assert len(calls) == seen
    restored = rollback_latest(store, bot["id"])
    assert restored["kind"] == "notes"
    assert (memory / "MISTAKES.md").read_text(encoding="utf-8") == hand


def test_predictions_habits_playbook_dreams_and_retrieval(tmp_path, monkeypatch):
    store, _endpoint, bot = _world(tmp_path)
    day = "2026-10-08T01:00:00+00:00"
    messages = []
    for index in range(3):
        messages.append(_msg(f"ask{index}", "user", "please alphabetize the ceramic drawer labels", f"2026-10-08T01:{index * 2:02d}:00+00:00"))
        messages.append(_msg(f"ans{index}", "assistant", "ack", f"2026-10-08T01:{index * 2 + 1:02d}:00+00:00"))
    messages.append(_msg("habit-a", "user", "hello kiln", "2026-10-07T01:00:00+00:00"))
    messages.append(_msg("habit-b", "user", "hello kiln", "2026-10-08T01:10:00+00:00"))
    messages.append(_msg("p1", "assistant", "I'll check the kiln later", "2026-10-08T01:11:00+00:00"))
    messages.append(_msg("pred", "assistant", "I expect the ceramic volcano to erupt tonight", "2026-10-08T01:12:00+00:00"))
    messages.append(_msg("pred-actual", "user", "ack", "2026-10-08T01:13:00+00:00"))
    messages.append(_msg("pred2", "assistant", "I expect a quiet kiln tonight", "2026-10-08T01:14:00+00:00"))
    messages.append(_msg("pred2-actual", "user", "ack", "2026-10-08T01:15:00+00:00"))
    messages.append(_msg("pred3", "assistant", "I expect the cobalt stain to fade", "2026-10-08T01:16:00+00:00"))
    messages.append(_msg("pred3-actual", "user", "ack", "2026-10-08T01:17:00+00:00"))
    messages.append(_msg("p2", "assistant", "Checked the kiln, done", "2026-10-08T01:18:00+00:00"))
    messages.append(_msg("play", "user", "morning Read the kiln log Invent a new glaze", "2026-10-08T01:19:00+00:00"))
    messages.append(_msg("dream", "user", "a quieter kiln alarm would help", "2026-10-08T01:19:30+00:00"))
    messages.append(_msg("q1", "user", "What is the ceramic glaze code?", "2026-10-08T01:30:00+00:00"))
    messages.append(_msg("q1a", "assistant", "ack", "2026-10-08T01:31:00+00:00"))
    messages.append(_msg("q2", "user", "Where did the cobalt stain go?", "2026-10-08T01:32:00+00:00"))
    messages.append(_msg("q2a", "assistant", "ack", "2026-10-08T01:33:00+00:00"))
    chat = _chat(store, bot["id"], messages)
    count = len(chat["messages"])

    store.save_skill({"name": "kiln-log", "description": "Read it", "body": "Read the kiln log."})
    mark_origin(store, "kiln-log", "learned", bot_id=bot["id"])
    store.save_skill({"name": "user-glaze", "description": "Mine", "body": "Invent a new glaze."})
    mark_origin(store, "user-glaze", "user", bot_id=bot["id"])
    saved = save_candidate(
        store,
        bot["id"],
        {
            "name": "kiln-log",
            "trigger": "when the kiln log is asked for",
            "steps": ["Read the kiln log"],
            "pitfalls": ["Do not invent a glaze."],
            "scope": "this bot",
            "check": {"kind": "command", "command": "python -c \"raise SystemExit(0)\"", "exit_code": 0},
            "replay_without": "plain",
            "replay_with": "with-skill",
        },
    )
    path = _candidate_path(store, bot["id"], saved["id"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "promoted"
    path.write_text(json.dumps(data), encoding="utf-8")
    user_saved = save_candidate(
        store,
        bot["id"],
        {
            "name": "user-glaze",
            "trigger": "when a glaze is invented",
            "steps": ["Invent a new glaze"],
            "pitfalls": [],
            "scope": "this bot",
            "check": {"kind": "command", "command": "python -c \"raise SystemExit(0)\"", "exit_code": 0},
            "replay_without": "plain",
            "replay_with": "with-skill",
        },
    )
    user_path = _candidate_path(store, bot["id"], user_saved["id"])
    user_data = json.loads(user_path.read_text(encoding="utf-8"))
    user_data["status"] = "promoted"
    user_path.write_text(json.dumps(user_data), encoding="utf-8")

    scripted = "\n".join(
        [
            "PLAYBOOK|[m:play]|2026-10-08|Kiln morning|Read the kiln log / Invent a new glaze",
            "DREAM|[m:dream]|2026-10-08|0.90|a quieter kiln alarm",
            "MISTAKE|[m:play]|2026-10-08|Wrong glaze|Invent a volcano|kiln|Read the kiln log",
        ]
    )
    _silence(monkeypatch, scripted)
    asyncio.run(run_pass(store, bot, now=WHEN, force=True))
    folder = _notes(store, bot["id"])
    play = (folder / "PLAYBOOK.md").read_text(encoding="utf-8")
    assert "Read the kiln log" in play
    assert "Invent a new glaze" not in play
    assert "volcano" not in (folder / "MISTAKES.md").read_text(encoding="utf-8")
    dreams = (folder / "DREAMS.md").read_text(encoding="utf-8")
    assert "quieter" in dreams
    assert store.list_schedules(bot["id"]) == []
    assert len(store.get_chat(bot["id"], chat["id"])["messages"]) == count
    assert check_revision_limit(store.get_bot(bot["id"])) == 3
    predictions = (folder / "PREDICTIONS.md").read_text(encoding="utf-8")
    assert "score=0.00" in predictions
    assert "Expected: I expect the ceramic volcano" in predictions
    assert "Expected: I expect a quiet kiln tonight" in predictions
    assert "Expected: I expect the cobalt stain to fade" in predictions
    assert "Expected: please alphabetize" not in predictions
    promises = (folder / "PROMISES.md").read_text(encoding="utf-8")
    assert "status=closed" in promises
    question = learning_extra(store, bot)["question"]
    assert question.startswith("I am still unsure about one thing:")
    assert "ceramic glaze" in question
    assert "cobalt stain" in question
    assert question.count("I am still unsure") == 1

    habit = next(entry for entry in read_entries((folder / "HABITS.md").read_text(encoding="utf-8")) if entry.hour == "1")
    assert habit.status == "suggested"
    from easyagent.journal import approve_habit

    schedule = approve_habit(store, bot["id"], habit.entry_id)
    assert schedule["cron"] == "0 1 * * *"
    assert len(store.list_schedules(bot["id"])) == 1
    assert len(store.get_chat(bot["id"], chat["id"])["messages"]) == count

    mistake = note_block(
        store,
        bot["id"],
        [{"role": "user", "content": "please repair the ceramic drawer hinge before the shift"}],
    )
    assert "ceramic" in mistake.lower()
    weather = note_block(
        store,
        bot["id"],
        [{"role": "user", "content": "please describe the harbor weather forecast for this coming week in detail"}],
    )
    assert "drawer" not in weather.lower()
    assert "Invent a new glaze" not in weather
    hello = note_block(store, bot["id"], [{"role": "user", "content": "hi"}])
    assert "quieter" in hello.lower()
    assert "only if you want it" in hello.lower()


def test_a_high_score_checks_less(tmp_path, monkeypatch):
    _silence(monkeypatch)
    store, _endpoint, bot = _world(tmp_path)
    line = "I expect the ceramic drawer labels alphabetize cleanly"
    follow = "the ceramic drawer labels alphabetize cleanly"
    messages = []
    for index in range(3):
        messages.append(_msg(f"ask{index}", "user", follow, f"2026-10-08T01:{index * 2:02d}:00+00:00"))
        messages.append(_msg(f"ans{index}", "assistant", line, f"2026-10-08T01:{index * 2 + 1:02d}:00+00:00"))
    messages.append(_msg("after", "user", follow, "2026-10-08T01:12:00+00:00"))
    _chat(store, bot["id"], messages)
    asyncio.run(run_pass(store, bot, now=WHEN, force=True))
    assert check_revision_limit(store.get_bot(bot["id"])) == 1


def test_the_learning_panel_lists_the_eight_notes(tmp_path):
    client = TestClient(__import__("easyagent.app", fromlist=["create_app"]).create_app(tmp_path))
    endpoint = client.post("/api/endpoints", json={"name": "kiln", "base_url": "http://10.1.2.3:11434/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    panel = client.get(f"/api/bots/{bot['id']}/learning")
    assert panel.status_code == 200
    names = [item["name"] for item in panel.json()["notes"]]
    assert names == [
        "MISTAKES.md",
        "PROMISES.md",
        "UNKNOWNS.md",
        "PREDICTIONS.md",
        "HABITS.md",
        "PLAYBOOK.md",
        "WORLD.md",
        "DREAMS.md",
    ]
    assert panel.json()["prune"]["retain_days"] == 30
    assert panel.json()["prune"]["pruning"] is True
    assert panel.json()["prune"]["pending"] == []
    saved = client.put(f"/api/bots/{bot['id']}/notes/retention", json={"keep_forever": True, "retain_days": 14})
    assert saved.status_code == 200
    body = saved.json()
    assert body["prune"]["keep_forever"] is True
    assert body["prune"]["retain_days"] == 14
    classic = client.get("/classic")
    assert "What changed last night" in classic.text
    assert "What can be removed" in classic.text
    readme = __import__("pathlib").Path("README.md").read_text(encoding="utf-8")
    assert "What your bot keeps track of" in readme


def test_hyphenated_keys_are_scrubbed_from_notes_summaries_and_lessons(tmp_path, monkeypatch):
    from easyagent.llm import IDLE_MODEL_TIMEOUT
    from easyagent.rolling import refresh_rolling_summary
    from easyagent.tools import keep_lessons

    store, _endpoint, bot = _world(tmp_path)
    samples = [  # fake-key-fixture
        "sk-test-FAKE123",
        "sk-proj-abc_def123456",
        "sk-ant-api03-ABCDEFGH12",
        "ghp_1234567890abcd",
        "github_pat_11AAAA12345678",
        "xoxb-1234-5678-abcdef",
        "xoxp-1234567890-abcdef",
        "xoxa-1234567890-abcdef",
        "AKIAIOSFODNN7EXAMPLE",
        "Bearer sk-test-FAKE123456",
    ]
    assert find_leaks("sk-test-FAKE123")  # fake-key-fixture
    assert not find_leaks("please check the skill later")
    assert not find_leaks("sk-test")
    for sample in samples:
        assert find_leaks(sample), sample
        cleaned = scrub_text(store, f"The note says {sample} at the end.")
        assert sample not in cleaned
        assert "[redacted]" in cleaned
    token = "sk-test-FAKE123"  # fake-key-fixture
    messages = [_msg("m1", "user", f"ceramic drawer {token}", "2026-10-08T01:00:00+00:00")]
    chat = _chat(store, bot["id"], messages)
    chat["summarized_through"] = len(messages)
    store.save_chat(chat)
    commit_entries(
        store,
        bot["id"],
        [
            Entry(kind="unknown", title="Leak", body=f"Is {token} still valid?", cites=["m1"], dates=["2026-10-08"], source="derived"),
            Entry(
                kind="prediction",
                title="Leak",
                body=f"Expected {token} to expire",
                cites=["m1"],
                dates=["2026-10-08"],
                source="derived",
                score="0.5",
            ),
        ],
        messages,
    )
    folder = _notes(store, bot["id"])
    blob = "\n".join((folder / name).read_text(encoding="utf-8") for name in NOTE_NAMES if (folder / name).is_file())
    assert token not in blob
    assert "PREDICTIONS.md" in {path.name for path in folder.glob("*.md")} or "prediction" in blob.lower() or (folder / "PREDICTIONS.md").is_file() or (folder / "UNKNOWNS.md").is_file()
    assert token not in (folder / "UNKNOWNS.md").read_text(encoding="utf-8")
    keep_lessons(store, bot["id"], f"```memory\nThe saved key was {token} once.\n```")
    memory = (folder / "MEMORY.md").read_text(encoding="utf-8")
    assert token not in memory
    assert "[redacted]" in memory
    seen = []

    async def complete(**kwargs):
        seen.append(kwargs.get("timeout"))
        return f"token {token} [m:m1]"

    monkeypatch.setattr("easyagent.llm.complete", complete)
    text = asyncio.run(refresh_rolling_summary(store, store.get_bot(bot["id"]), store.get_chat(bot["id"], chat["id"])))
    assert token not in text
    assert seen == [IDLE_MODEL_TIMEOUT]
    assert IDLE_MODEL_TIMEOUT >= 180


def test_nightly_model_calls_wait_several_minutes(tmp_path, monkeypatch):
    from easyagent.journal import _ask
    from easyagent.llm import IDLE_MODEL_TIMEOUT

    store, _endpoint, bot = _world(tmp_path)
    seen = []

    async def complete(**kwargs):
        seen.append(kwargs.get("timeout"))
        return ""

    monkeypatch.setattr("easyagent.llm.complete", complete)
    asyncio.run(_ask(store, bot, "Summarize the kiln."))
    assert seen == [IDLE_MODEL_TIMEOUT]
