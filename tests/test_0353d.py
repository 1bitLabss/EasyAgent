"""Streamed tool-call deltas with no index field split into separate calls.

Older Ollama omits the index on every tool_call delta, so every delta used to
land in slot 0: two calls came back glued ('read_file' + 'list_dir' ->
'read_filelist_dir'). A fresh id on a slot that already has a name opens a new
slot; the index-present path keeps its exact old behaviour.
"""

from __future__ import annotations

import asyncio
import json

import easyagent.llm as llm
from easyagent.llm import _collect_calls


def _sse(lines: list[str], monkeypatch):
    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            for line in lines:
                yield line

        async def aread(self):
            return b""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            return None

        def stream(self, method, url, json=None, headers=None):
            class Context:
                async def __aenter__(self_inner):
                    return FakeResponse()

                async def __aexit__(self_inner, *args):
                    return False

            return Context()

    monkeypatch.setattr(llm.httpx, "AsyncClient", FakeClient)

    async def read():
        async for _piece in llm._stream_attempt(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="stub",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        ):
            pass
        return llm.take_native_calls()

    return asyncio.run(read())


def test_two_calls_without_index_split_into_two_slots(monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_read","type":"function","function":{"name":"read_file","arguments":""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\\"path\\": \\"a.txt\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_list","type":"function","function":{"name":"list_dir","arguments":""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\\"path\\": \\"b\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    calls = _sse(lines, monkeypatch)
    assert len(calls) == 2
    assert calls[0]["id"] == "call_read"
    assert calls[0]["name"] == "read_file"
    assert json.loads(calls[0]["arguments"]) == {"path": "a.txt"}
    assert calls[1]["id"] == "call_list"
    assert calls[1]["name"] == "list_dir"
    assert json.loads(calls[1]["arguments"]) == {"path": "b"}


def test_index_present_path_is_unchanged(monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_read","type":"function","function":{"name":"read_file","arguments":""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\": \\"a.txt\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_list","type":"function","function":{"name":"list_dir","arguments":""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"{\\"path\\": \\"b\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    calls = _sse(lines, monkeypatch)
    assert len(calls) == 2
    assert calls[0]["id"] == "call_read"
    assert calls[0]["name"] == "read_file"
    assert json.loads(calls[0]["arguments"]) == {"path": "a.txt"}
    assert calls[1]["id"] == "call_list"
    assert calls[1]["name"] == "list_dir"
    assert json.loads(calls[1]["arguments"]) == {"path": "b"}


def test_repeated_index_zero_still_merges_into_one_call():
    bucket: dict[int, dict] = {}
    _collect_calls(bucket, {"delta": {"tool_calls": [
        {"index": 0, "id": "call_pwd", "function": {"name": "terminal", "arguments": ""}},
    ]}})
    _collect_calls(bucket, {"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": '{"command": "pwd"}'}},
    ]}})
    assert len(bucket) == 1
    assert bucket[0]["name"] == "terminal"
    assert json.loads(bucket[0]["arguments"]) == {"command": "pwd"}


def test_same_id_repeated_on_every_delta_stays_one_call():
    bucket: dict[int, dict] = {}
    _collect_calls(bucket, {"delta": {"tool_calls": [
        {"id": "call_x", "function": {"name": "terminal", "arguments": '{"com'}},
    ]}})
    _collect_calls(bucket, {"delta": {"tool_calls": [
        {"id": "call_x", "function": {"arguments": 'mand": "ls"}'}},
    ]}})
    assert len(bucket) == 1
    assert bucket[0]["name"] == "terminal"
    assert json.loads(bucket[0]["arguments"]) == {"command": "ls"}
