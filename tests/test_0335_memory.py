"""Memory scrubs a secret before it is stored and again when it is read."""

from easyagent.store import Store
from easyagent.tools import ToolRequest, _run_memory

KEY = "sk-AAAA12345678"


def _bot(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    return store, bot


def test_a_secret_is_not_stored_and_a_plain_line_is(tmp_path):
    store, bot = _bot(tmp_path)
    plain = store.add_memory(bot["id"], "The garage code is 1234.", "home", create=True)
    assert plain["text"] == "The garage code is 1234."
    secret = store.add_memory(bot["id"], f"the token is {KEY}", "notes", create=True)
    assert KEY not in secret["text"]
    assert KEY not in store.topic_text(bot["id"], secret["topic"])
    changed = store.update_memory(bot["id"], secret["id"], f"password={KEY}now")
    assert KEY not in changed["text"]
    heard = _run_memory(
        store,
        ToolRequest(kind="memory", action="read", path=secret["topic"]),
        bot["id"],
    )
    assert KEY not in heard
    assert "MARGIN-TOKEN-WIDE" not in heard
    kept = store.add_memory(bot["id"], "MARGIN-TOKEN-WIDE", "notes")
    assert kept["text"] == "MARGIN-TOKEN-WIDE"
