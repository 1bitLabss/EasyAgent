"""One scrubber covers memory, search, spill, undo, and provider logs."""

from easyagent.codenote import remember_undo, spill_output
from easyagent.coding import search_tree
from easyagent.journal import redact_leaks
from easyagent.llm import note_server_failure
from easyagent.selfinfo import search_history
from easyagent.store import Store
from easyagent.tools import ToolRequest, _run_files

KEY = "sk_live_secretvalue"
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAsecretbody\n-----END RSA PRIVATE KEY-----"


def test_the_shared_scrubber_covers_jwt_pem_and_password_lines():
    cleaned = redact_leaks(f"jwt {JWT}\n{PEM}\npassword: x\nAPI_TOKEN=abcdefghi\n{KEY}")
    assert JWT not in cleaned
    assert "MIIEowIBAAKCAQEAsecretbody" not in cleaned
    assert "BEGIN RSA PRIVATE KEY" not in cleaned
    assert "password: x" not in cleaned
    assert "API_TOKEN=abcdefghi" not in cleaned
    assert KEY not in cleaned
    assert "[redacted]" in cleaned


def test_spill_and_undo_keep_the_text_and_drop_the_secret(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("plain\n", encoding="utf-8")
    saved = remember_undo(path, "keep me\nAPI_TOKEN=abcdefghi\npassword=hunter2\n")
    undo = saved.read_text(encoding="utf-8")
    assert "keep me" in undo
    assert "API_TOKEN=" not in undo
    assert "password=" not in undo
    dest = spill_output("tail\nAPI_TOKEN=abcdefghi\npassword: x\n", tmp_path / ".ea-spill")
    spilled = dest.read_text(encoding="utf-8")
    assert "tail" in spilled
    assert "API_TOKEN=" not in spilled
    assert "password: x" not in spilled
    assert spill_output(KEY, tmp_path / ".ea-spill", label="secret") is None


def test_read_file_of_a_spill_does_not_return_the_secret(tmp_path):
    store = Store(tmp_path / "data")
    store.ensure()
    spill = tmp_path / "proj" / ".ea-spill"
    spill.mkdir(parents=True)
    (spill / "output.txt").write_text(f"note\nAPI_TOKEN=abcdefghi\npassword: x\n{KEY}\n", encoding="utf-8")
    text = _run_files(store, ToolRequest(kind="files", action="read", path=str(spill / "output.txt")))
    assert "note" in text
    assert "API_TOKEN=" not in text
    assert "password: x" not in text
    assert KEY not in text


def test_search_skips_undo_and_spill_and_does_not_echo_the_query(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "notes.txt").write_text("ordinary ALPHA-NOTE\n", encoding="utf-8")
    (root / ".ea-spill").mkdir()
    (root / ".ea-spill" / "output.txt").write_text("SPILL-SECRET-WORD\n", encoding="utf-8")
    (root / ".ea-undo").mkdir()
    (root / ".ea-undo" / "notes.txt").write_text("UNDO-SECRET-WORD\n", encoding="utf-8")
    hidden = search_tree(root, "SECRET-WORD")
    assert "SPILL-SECRET-WORD" not in hidden
    assert "UNDO-SECRET-WORD" not in hidden
    assert "ALPHA-NOTE" in search_tree(root, "ALPHA-NOTE")

    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    chat["messages"] = [{"id": "m1", "role": "user", "content": f"BLUE-HERON beside {KEY}"}]
    store.save_chat(chat)
    found = search_history(store, bot["id"], KEY)
    assert KEY not in found
    assert "Matches for \"[redacted]\"" in found


def test_a_provider_503_log_drops_the_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EASYAGENT_DATA", str(tmp_path))
    note_server_failure(503, '{"error":"down","api_key":"sk-AAAA12345678"}', 1)
    printed = capsys.readouterr().out
    assert "model call: http 503" in printed
    assert "sk-AAAA12345678" not in printed
    saved = (tmp_path / "debug-raw.txt").read_text(encoding="utf-8")
    assert "sk-AAAA12345678" not in saved
