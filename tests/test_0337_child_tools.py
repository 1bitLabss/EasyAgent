"""A child agent's request lists only the tools it was allowed."""

import asyncio

from easyagent import llm
from easyagent.helpers import HelperSpec, _run_one
from easyagent.store import Store


def _world(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model="stub")
    return store, bot, endpoint


def _names() -> list[str]:
    return [tool["function"]["name"] for tool in llm.native_tools()]


def test_the_parent_tool_list_stays_whole_outside_a_child():
    full = _names()
    assert len(full) == 22
    token = llm.only_tools(["read_file"])
    try:
        assert _names() == ["read_file"]
        payload = llm._completion_payload(
            [{"role": "user", "content": "count the notes"}],
            "stub",
            stream=False,
            tools=True,
        )
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["read_file"]
    finally:
        llm.reset_only_tools(token)
    assert _names() == full


def test_a_child_request_lists_only_the_tool_it_was_allowed(tmp_path, monkeypatch):
    store, bot, endpoint = _world(tmp_path)
    seen: dict = {}

    async def fake_complete(**kwargs):
        payload = llm._completion_payload(
            kwargs["messages"],
            kwargs.get("model"),
            stream=False,
            tools=kwargs.get("tools", True),
        )
        seen["sent"] = [tool["function"]["name"] for tool in payload["tools"]]
        seen["tools_flag"] = kwargs.get("tools")
        return "two notes"

    monkeypatch.setattr(llm, "complete", fake_complete)
    spec = HelperSpec(
        name="Child",
        task="count the notes",
        tools=frozenset({"files"}),
        steps=2,
        seconds=30,
        listed=("read_file",),
    )
    row = asyncio.run(_run_one(store, bot, endpoint, spec, yield_to_chats=False))
    assert seen["tools_flag"] is True
    assert seen["sent"] == ["read_file"]
    assert len(_names()) == 22
    assert row["status"] == "done"
    assert row["result"] == "two notes"


def test_a_child_named_by_kind_sends_those_tools(tmp_path, monkeypatch):
    store, bot, endpoint = _world(tmp_path)
    seen: dict = {}

    async def fake_complete(**kwargs):
        payload = llm._completion_payload(
            kwargs["messages"],
            kwargs.get("model"),
            stream=False,
            tools=True,
        )
        seen["sent"] = [tool["function"]["name"] for tool in payload["tools"]]
        return "done"

    monkeypatch.setattr(llm, "complete", fake_complete)
    spec = HelperSpec(
        name="Helper",
        task="look at the notes",
        tools=frozenset({"files", "search"}),
        steps=2,
        seconds=30,
    )
    asyncio.run(_run_one(store, bot, endpoint, spec, yield_to_chats=False))
    assert seen["sent"] == ["list_dir", "read_file", "write_file", "patch", "web_search"]
    assert len(_names()) == 22
