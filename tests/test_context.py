import json

from easyagent.context import (
    ERROR_PLACEHOLDER,
    bot_context_chars,
    bot_context_tokens,
    clip_message,
    context_window,
    prepare_context,
)
from easyagent.limits import (
    CHARS_PER_TOKEN,
    DEFAULT_CONTEXT_TOKENS,
    MAX_CONTEXT_CHARS,
    MAX_SUMMARY_CHARS,
    MIN_CONTEXT_TOKENS,
)


def _messages(count, size):
    rows = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        rows.append({"role": role, "content": ("m" * size) + f"-{index}-"})
    return rows


def test_default_budget_is_tokens_and_large():
    assert DEFAULT_CONTEXT_TOKENS >= 24_000
    assert bot_context_tokens(None) == DEFAULT_CONTEXT_TOKENS
    assert bot_context_chars(None) == DEFAULT_CONTEXT_TOKENS * CHARS_PER_TOKEN
    assert MAX_CONTEXT_CHARS == DEFAULT_CONTEXT_TOKENS * CHARS_PER_TOKEN


def test_old_character_budget_on_a_bot_is_ignored():
    """A bot saved with the old 7,200-character cap gets the token default, not a tiny window."""
    assert bot_context_tokens({"context_chars": 7200}) == DEFAULT_CONTEXT_TOKENS
    assert bot_context_tokens({"context_chars": 900}) == DEFAULT_CONTEXT_TOKENS
    assert bot_context_tokens({"context_tokens": 4096}) == 4096
    assert bot_context_tokens({"context_tokens": 3}) == DEFAULT_CONTEXT_TOKENS


def test_env_changes_the_default(monkeypatch):
    monkeypatch.setenv("EASYAGENT_CONTEXT_TOKENS", "60000")
    assert bot_context_tokens(None) == 60000
    assert bot_context_tokens({"context_tokens": 8000}) == 8000
    monkeypatch.setenv("EASYAGENT_CONTEXT_TOKENS", "not a number")
    assert bot_context_tokens(None) == DEFAULT_CONTEXT_TOKENS


def test_prepare_does_not_mutate_or_drop_messages():
    messages = _messages(30, 80)
    snapshot = json.dumps(messages)
    prepare_context(messages, "", 0)
    assert json.dumps(messages) == snapshot


def test_short_chat_fits_without_a_summary():
    messages = _messages(3, 40)
    prepared = prepare_context(messages, "", 0)
    assert prepared.summarized_through == 0
    assert prepared.summary == ""
    assert len(prepared.tail) == 3
    assert prepared.stats["bounded"]
    assert prepared.stats["compacted"] is False


def test_screenshot_chat_is_sent_whole_with_real_numbers():
    """8 messages, 1,030 characters: every message goes in and the stats say so."""
    messages = _messages(8, 125)
    total = sum(len(m["content"]) for m in messages)
    prepared = prepare_context(messages, "", 0)
    stats = prepared.stats
    assert len(prepared.tail) == 8
    assert stats["transcript_messages"] == 8
    assert stats["transcript_chars"] == total
    assert stats["context_chars"] == total
    assert stats["compacted_messages"] == 0
    assert stats["max_context_tokens"] == DEFAULT_CONTEXT_TOKENS
    assert stats["max_context_chars"] == DEFAULT_CONTEXT_TOKENS * CHARS_PER_TOKEN
    assert stats["context_tokens"] == -(-total // CHARS_PER_TOKEN)


def test_many_short_turns_all_fit_no_message_count_cap():
    """The old harness kept only the last 8 turns. Now the budget decides."""
    messages = _messages(120, 60)
    prepared = prepare_context(messages, "", 0)
    assert len(prepared.tail) == 120
    assert prepared.summarized_through == 0
    assert prepared.summary == ""


def test_stale_summary_is_dropped_when_everything_fits():
    messages = _messages(10, 50)
    prepared = prepare_context(messages, "old summary line", 4)
    assert prepared.summarized_through == 0
    assert prepared.summary == ""
    assert len(prepared.tail) == 10


def test_long_chat_stays_bounded_while_growing():
    messages = _messages(400, 3000)
    summary = ""
    through = 0
    for end in range(1, len(messages) + 1, 7):
        prepared = prepare_context(messages[:end], summary, through)
        assert prepared.summarized_through <= end
        assert prepared.stats["context_chars"] <= MAX_CONTEXT_CHARS
        assert len(prepared.summary) <= MAX_SUMMARY_CHARS
        assert prepared.stats["bounded"]
        _, _, per_message = context_window(None)
        for offset, item in enumerate(prepared.tail):
            original = messages[prepared.summarized_through + offset]["content"]
            assert item["content"] == clip_message(original, per_message)
        summary = prepared.summary
        through = prepared.summarized_through
    assert through > 0
    assert prepared.stats["compacted"] is True
    assert len(messages) == 400
    # Far more than 8 turns are still sent in full.
    assert len(prepared.tail) > 20


def test_big_messages_are_not_cut_to_2000_characters():
    messages = _messages(6, 5000)
    prepared = prepare_context(messages, "", 0)
    assert prepared.stats["context_chars"] <= MAX_CONTEXT_CHARS
    assert all(len(item["content"]) == 5000 + len(f"-{i}-") for i, item in enumerate(prepared.tail))


def test_one_enormous_message_is_clipped_only_in_the_request():
    messages = _messages(2, 500_000)
    prepared = prepare_context(messages, "", 0)
    assert prepared.stats["context_chars"] <= MAX_CONTEXT_CHARS
    assert len(messages[0]["content"]) == 500_000 + len("-0-")


def test_summary_covers_turns_that_left_the_tail():
    budget_tokens = MIN_CONTEXT_TOKENS * 2
    messages = _messages(60, 300)
    prepared = prepare_context(messages, "", 0, context_chars=budget_tokens * CHARS_PER_TOKEN)
    assert prepared.summarized_through > 0
    assert prepared.stats["compacted_messages"] == prepared.summarized_through
    assert "m" * 20 in prepared.summary
    assert all(item["content"] != messages[0]["content"] for item in prepared.tail)


def test_smaller_budget_prunes_earlier_and_keeps_every_turn():
    messages = _messages(80, 400)
    snapshot = json.dumps(messages)
    wide = prepare_context(messages, "", 0)
    small_chars = MIN_CONTEXT_TOKENS * CHARS_PER_TOKEN
    small = prepare_context(messages, "", 0, context_chars=small_chars)
    assert json.dumps(messages) == snapshot
    assert wide.summarized_through == 0
    assert small.summarized_through > wide.summarized_through
    assert len(small.tail) < len(wide.tail)
    assert small.stats["context_chars"] <= small_chars
    assert small.stats["max_context_chars"] == small_chars
    assert small.stats["max_context_tokens"] == MIN_CONTEXT_TOKENS
    assert small.stats["bounded"] is True
    grown = prepare_context(messages, small.summary, small.summarized_through)
    assert grown.summarized_through == 0
    assert len(grown.tail) == 80
    assert len(messages) == 80


def test_an_old_error_line_is_not_replayed_to_the_model():
    messages = [
        {"role": "user", "content": "are you there?"},
        {"role": "assistant", "content": "Could not reach http://localhost:8080/v1: refused", "error": True},
        {"role": "user", "content": "hello again"},
    ]
    prepared = prepare_context(messages, "", 0)
    blob = json.dumps(prepared.tail)
    assert "localhost:8080" not in blob
    assert prepared.tail[1]["content"] == ERROR_PLACEHOLDER
    small = prepare_context(messages * 40, "", 0, context_chars=MIN_CONTEXT_TOKENS * CHARS_PER_TOKEN)
    assert "localhost:8080" not in small.summary
