"""Routine creation guards from the 2026-11-04 dogfood.

The bot made routines from casual chat without asking, stored the person's raw
sentence as the prompt, and let three near-identical 8 AM routines pile up.
The tool description now demands a confirm-first and a clean written prompt,
and the store refuses an exact duplicate: same schedule time and same prompt.
"""

import pytest

from easyagent.schedule import apply_confirmed, compile_routine
from easyagent.store import StoreError
from tests.test_schedule import seed


def routine(prompt="Give me the news.", **fields):
    return compile_routine({"name": "News", "prompt": prompt, "daily": "8:00 AM", **fields})


def test_the_same_time_and_prompt_twice_fails_the_second_time(tmp_path):
    store, bot, _ = seed(tmp_path)
    store.add_schedule(bot["id"], routine())
    with pytest.raises(StoreError) as exc:
        store.add_schedule(bot["id"], routine())
    assert "That routine already exists." in str(exc.value)
    assert len(store.list_schedules(bot["id"])) == 1


def test_the_duplicate_guard_ignores_spacing_and_case(tmp_path):
    store, bot, _ = seed(tmp_path)
    store.add_schedule(bot["id"], routine(prompt="Give me the news."))
    with pytest.raises(StoreError):
        store.add_schedule(bot["id"], routine(prompt="  give me   the NEWS. "))


def test_different_prompts_at_the_same_time_both_create(tmp_path):
    store, bot, _ = seed(tmp_path)
    store.add_schedule(bot["id"], routine(prompt="Give me the news."))
    store.add_schedule(bot["id"], routine(prompt="Check the server."))
    assert len(store.list_schedules(bot["id"])) == 2


def test_the_same_prompt_at_a_different_time_creates(tmp_path):
    store, bot, _ = seed(tmp_path)
    store.add_schedule(bot["id"], routine())
    store.add_schedule(bot["id"], routine(daily="9:00 AM"))
    assert len(store.list_schedules(bot["id"])) == 2


def test_duplicate_guard_covers_the_confirmed_card_path(tmp_path):
    store, bot, _ = seed(tmp_path)
    spec = {"action": "create", "name": "News", "prompt": "Give me the news.", "daily": "8:00 AM"}
    apply_confirmed(store, bot["id"], spec)
    with pytest.raises(StoreError) as exc:
        apply_confirmed(store, bot["id"], spec)
    assert "That routine already exists." in str(exc.value)
    assert len(store.list_schedules(bot["id"])) == 1


def test_the_routine_tool_description_demands_a_confirm_and_a_clean_prompt():
    from easyagent.llm import NATIVE_TOOLS

    routine_tool = next(tool for tool in NATIVE_TOOLS if tool["function"]["name"] == "routine")
    text = routine_tool["function"]["description"].lower()
    assert "get the person's yes" in text
    assert "never copy the person's message" in text
