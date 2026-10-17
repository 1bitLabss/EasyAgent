"""Session search scrubs a secret in the snippet and in the query echo."""

from easyagent.selfinfo import read_chat, search_history
from easyagent.store import Store

KEY = "sk-AAAA12345678"


def _bot(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    chat = store.create_chat(bot["id"])
    chat["messages"] = [
        {
            "id": "m1",
            "role": "user",
            "content": f"BLUE-HERON-42 sits beside {KEY}",
        }
    ]
    store.save_chat(chat)
    return store, bot, chat


def test_search_and_read_drop_the_key_and_keep_the_note(tmp_path):
    store, bot, chat = _bot(tmp_path)
    found = search_history(store, bot["id"], KEY)
    assert KEY not in found
    assert "BLUE-HERON-42" in found
    opened = read_chat(store, bot["id"], chat["id"])
    assert KEY not in opened
    assert "BLUE-HERON-42" in opened
