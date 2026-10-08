"""Caps for what the model is allowed to see.

The transcript on disk is not capped. These limits apply only to the
request built from a summary plus the recent chat.

The chat budget is counted in tokens. A token is estimated as
CHARS_PER_TOKEN characters, so the footer and the prompt can report a real
number without loading a tokenizer. The whole recent chat goes to the model
until it no longer fits in the budget. Only then are the oldest turns folded
into a short summary.
"""

import os

# Rough characters per token for English text and code.
CHARS_PER_TOKEN = 4

# Default chat budget (summary + recent chat) for a bot that did not set one.
# Most of a 32k-context model, after the system prompt and the reply.
# EASYAGENT_CONTEXT_TOKENS changes the default for every bot without a setting.
DEFAULT_CONTEXT_TOKENS = 24_000
MIN_CONTEXT_TOKENS = 512
MAX_CONTEXT_TOKENS = 1_000_000

# Character equivalents of the default budget.
MAX_CONTEXT_CHARS = DEFAULT_CONTEXT_TOKENS * CHARS_PER_TOKEN
MIN_CONTEXT_CHARS = MIN_CONTEXT_TOKENS * CHARS_PER_TOKEN

# A single message is clipped only when it alone would take more than this
# share of the recent-chat budget. Never below MIN_MESSAGE_PAYLOAD_CHARS.
MIN_MESSAGE_PAYLOAD_CHARS = 2000
MAX_MESSAGE_PAYLOAD_CHARS = MIN_MESSAGE_PAYLOAD_CHARS

# Rolling summary of everything older than the recent chat. It only exists
# once the chat is over budget.
MIN_SUMMARY_CHARS = 1200
MAX_SUMMARY_CHARS = 16_000
EXCERPT_CHARS = 160

# System prompt pieces. They do not grow with the transcript.
DIRECTION_CAP = 3000
SKILL_PACK_CAP = 2500

# Bots saved before the budget was counted in tokens carry a context_chars
# value of at most this many characters. It is ignored now.
LEGACY_MAX_CONTEXT_CHARS = 7200

# A single user message stored on disk. The model still only sees a clip.
MAX_STORED_MESSAGE_CHARS = 100_000


def default_context_tokens() -> int:
    """The chat budget for a bot that did not set one."""
    raw = (os.environ.get("EASYAGENT_CONTEXT_TOKENS") or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = DEFAULT_CONTEXT_TOKENS
    else:
        value = DEFAULT_CONTEXT_TOKENS
    return max(MIN_CONTEXT_TOKENS, min(MAX_CONTEXT_TOKENS, value))
