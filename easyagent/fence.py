"""Untrusted fences. The markers are data, and the words inside are not a request.

A window title, a page, or a tool result can contain the marker text. That text
is escaped, and the real markers carry a nonce chosen for this turn, so the
content cannot close the fence or open a new one. Bookkeeping that needs the
bytes inside a fence uses ``inner_untrusted``. Anything that decides what the
person asked for uses ``typed_text``, which drops the fence entirely.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from contextvars import ContextVar

_NONCE: ContextVar[str] = ContextVar("easyagent_fence_nonce", default="")

# One-for-one lookalikes. NFKC already folds fullwidth letters.
_CONFUSABLE = str.maketrans(
    {
        "\u0430": "a",
        "\u0435": "e",
        "\u043e": "o",
        "\u0440": "p",
        "\u0441": "c",
        "\u0443": "y",
        "\u0445": "x",
        "\u0456": "i",
        "\u0455": "s",
        "\u04bb": "h",
        "\u0410": "A",
        "\u0412": "B",
        "\u0415": "E",
        "\u041a": "K",
        "\u041c": "M",
        "\u041d": "H",
        "\u041e": "O",
        "\u0420": "P",
        "\u0421": "C",
        "\u0422": "T",
        "\u0425": "X",
        "\u0406": "I",
        "\u0408": "J",
        "\u0391": "A",
        "\u0392": "B",
        "\u0395": "E",
        "\u0396": "Z",
        "\u0397": "H",
        "\u0399": "I",
        "\u039a": "K",
        "\u039c": "M",
        "\u039d": "N",
        "\u039f": "O",
        "\u03a1": "P",
        "\u03a4": "T",
        "\u03a5": "Y",
        "\u03a7": "X",
        "\u03b1": "a",
        "\u03bf": "o",
        "\u03bd": "v",
    }
)

_MARKER = re.compile(
    r"<{2,}\s*/?\s*(?:end\s+)?untrusted\b[^>\n]{0,120}>{0,3}",
    re.IGNORECASE,
)
_BLOCK = re.compile(
    r"<<<UNTRUSTED\b[^>\n]*>>>\n?(.*?)<<<END UNTRUSTED\b[^>\n]*>>>",
    re.IGNORECASE | re.DOTALL,
)
_TYPED = re.compile(
    r"<<<UNTRUSTED\b[^>\n]*>>>.*?<<<END UNTRUSTED\b[^>\n]*>>>",
    re.IGNORECASE | re.DOTALL,
)
_OPEN = re.compile(r"<<<UNTRUSTED\b[^>\n]*>>>.*\Z", re.IGNORECASE | re.DOTALL)
_ALREADY = re.compile(r"<<<UNTRUSTED\b", re.IGNORECASE)


def fold_text(text: str) -> str:
    """NFKC, then lookalike letters. Matching uses this. The length may change."""
    return unicodedata.normalize("NFKC", text or "").translate(_CONFUSABLE)


def neutralize_markers(text: str) -> str:
    """Escape fence markers inside content so they cannot open or close a fence."""
    nfkc = unicodedata.normalize("NFKC", text or "")
    folded = nfkc.translate(_CONFUSABLE)
    pieces: list[str] = []
    last = 0
    for match in _MARKER.finditer(folded):
        pieces.append(nfkc[last : match.start()])
        pieces.append(nfkc[match.start() : match.end()].replace("<", "\u2039").replace(">", "\u203a"))
        last = match.end()
    pieces.append(nfkc[last:])
    return "".join(pieces)


def begin_untrusted_turn() -> str:
    """A new nonce for this turn. Later fences in the same task reuse it."""
    nonce = secrets.token_hex(8)
    _NONCE.set(nonce)
    return nonce


def current_nonce() -> str:
    nonce = _NONCE.get()
    if not nonce:
        nonce = secrets.token_hex(8)
        _NONCE.set(nonce)
    return nonce


def wrap_untrusted(source: str, body: str) -> str:
    """One fenced block. Markers inside ``body`` are escaped first."""
    nonce = current_nonce()
    safe = re.sub(r"[^a-z0-9_-]", "", (source or "tool").lower()) or "tool"
    inner = neutralize_markers(body or "")
    return (
        f'<<<UNTRUSTED n="{nonce}" source="{safe}">>>\n'
        f"{inner}\n"
        f'<<<END UNTRUSTED n="{nonce}">>>'
    )


def typed_text(text: str) -> str:
    """The person's own words. Fenced blocks are removed, not read."""
    body = text or ""
    if "untrusted" not in body.lower():
        return body
    cleaned = _TYPED.sub("", body)
    cleaned = _OPEN.sub("", cleaned)
    return cleaned.strip()


def inner_untrusted(text: str) -> str:
    """The bytes inside the markers. Bookkeeping reads those, not the label."""
    body = text or ""
    if "untrusted" not in body.lower():
        return body
    return _BLOCK.sub(lambda match: match.group(1), body)


def already_fenced(text: str) -> bool:
    return _ALREADY.match((text or "").lstrip()) is not None
