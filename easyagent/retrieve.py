"""BM25 over a bot's saved chats, with optional embeddings.

Transcripts are only read. A miss, a 404, or a slow `/embeddings` call
leaves BM25 in place. The reply waits at most EMBED_TIMEOUT seconds for vectors.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import httpx

from easyagent.store import Store, StoreError
from easyagent.tools import redact

EMBED_TIMEOUT = 0.35
_K1 = 1.2
_B = 0.75
_TOP = 4
_CANDIDATES = 12
_PASSAGE_CHARS = 420
_INDEX_CHARS = 8000
_BLOCK_CHARS = 1600

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    """
    that this with from have were your what when where which about just they
    them then than into over also some only been will would could should there
    their here because before after under again other another these those very
    onto does didn wasn isn aren don can not but and the for you are was his her
    its who how all any our out too
    """.split()
)

_CACHE: dict[str, "_Index"] = {}
_EMBED: dict[str, bool] = {}


def tokens(text: str) -> list[str]:
    found = []
    for word in _WORD.findall((text or "").lower()):
        if len(word) < 3 or len(word) > 32 or word in _STOP:
            continue
        if len(set(word)) == 1:
            continue
        found.append(word)
    return found


@dataclass
class Passage:
    chat_id: str
    title: str
    index: int
    message_id: str
    role: str
    text: str
    created_at: str
    score: float = 0.0


@dataclass
class _Doc:
    passage: Passage
    tokens: list[str]
    tf: dict[str, int] = field(default_factory=dict)


@dataclass
class _Index:
    key: tuple
    docs: list[_Doc]
    avgdl: float
    idf: dict[str, float]


def _clip(text: str, limit: int) -> str:
    body = " ".join((text or "").split())
    if len(body) <= limit:
        return body
    return body[: limit - 1].rstrip() + "…"


def _key(store: Store, bot_id: str) -> tuple:
    try:
        listed = store.list_chats(bot_id)
    except StoreError:
        return ()
    return tuple(
        (item.get("id") or "", int(item.get("message_count") or 0), item.get("updated_at") or "")
        for item in listed
    )


def _build(store: Store, bot_id: str, key: tuple) -> _Index:
    docs: list[_Doc] = []
    try:
        listed = store.list_chats(bot_id)
    except StoreError:
        listed = []
    for item in listed:
        try:
            chat = store.get_chat(bot_id, item["id"])
        except StoreError:
            continue
        title = chat.get("title") or "New chat"
        for index, message in enumerate(chat.get("messages") or []):
            if not isinstance(message, dict):
                continue
            raw = message.get("content") or ""
            if not str(raw).strip():
                continue
            words = tokens(str(raw))
            if not words:
                continue
            counts: dict[str, int] = {}
            for word in words:
                counts[word] = counts.get(word, 0) + 1
            docs.append(
                _Doc(
                    passage=Passage(
                        chat_id=str(chat.get("id") or ""),
                        title=str(title),
                        index=index,
                        message_id=str(message.get("id") or ""),
                        role=str(message.get("role") or "user"),
                        text=_clip(str(raw), _INDEX_CHARS),
                        created_at=str(message.get("created_at") or ""),
                    ),
                    tokens=words,
                    tf=counts,
                )
            )
    df: dict[str, int] = {}
    total = 0
    for doc in docs:
        total += len(doc.tokens)
        for word in doc.tf:
            df[word] = df.get(word, 0) + 1
    count = len(docs) or 1
    idf = {word: math.log(1.0 + (count - seen + 0.5) / (seen + 0.5)) for word, seen in df.items()}
    avgdl = (total / len(docs)) if docs else 0.0
    return _Index(key=key, docs=docs, avgdl=avgdl or 1.0, idf=idf)


def indexed_message_ids(store: Store, bot_id: str) -> set[str]:
    """Message ids present in the on-disk search index. Empty text is not indexed."""
    index = index_for(store, bot_id)
    return {doc.passage.message_id for doc in index.docs if doc.passage.message_id}


def index_for(store: Store, bot_id: str) -> _Index:
    key = _key(store, bot_id)
    cached = _CACHE.get(bot_id)
    if cached is not None and cached.key == key:
        return cached
    built = _build(store, bot_id, key)
    _CACHE[bot_id] = built
    return built


def _score(doc: _Doc, query: list[str], idf: dict[str, float], avgdl: float) -> float:
    total = 0.0
    length = len(doc.tokens) or 1
    for word in query:
        tf = doc.tf.get(word) or 0
        if not tf:
            continue
        weight = idf.get(word) or 0.0
        denom = tf + _K1 * (1 - _B + _B * length / avgdl)
        total += weight * (tf * (_K1 + 1)) / denom
    return total


def search_passages(
    store: Store,
    bot_id: str,
    query: str,
    *,
    skip: set[tuple[str, int]] | None = None,
    limit: int = _CANDIDATES,
) -> list[Passage]:
    """Highest BM25 passages. `skip` is (chat id, message index) pairs already in the tail."""
    words = tokens(query)
    if not words:
        return []
    index = index_for(store, bot_id)
    if not index.docs:
        return []
    skipped = skip or set()
    ranked: list[Passage] = []
    for doc in index.docs:
        passage = doc.passage
        if (passage.chat_id, passage.index) in skipped:
            continue
        score = _score(doc, words, index.idf, index.avgdl)
        if score <= 0:
            continue
        ranked.append(
            Passage(
                chat_id=passage.chat_id,
                title=passage.title,
                index=passage.index,
                message_id=passage.message_id,
                role=passage.role,
                text=passage.text,
                created_at=passage.created_at,
                score=score,
            )
        )
    ranked.sort(key=lambda item: (item.score, item.index), reverse=True)
    return ranked[: max(1, limit)]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


def _post_embeddings(base_url: str, api_key: str | None, model: str | None, texts: list[str]) -> list[list[float]] | None:
    base = (base_url or "").strip().rstrip("/")
    if not base or not texts:
        return None
    if _EMBED.get(base) is False:
        return None
    payload: dict = {"input": texts}
    if model:
        payload["model"] = model
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with httpx.Client(timeout=httpx.Timeout(EMBED_TIMEOUT)) as client:
            response = client.post(f"{base}/embeddings", json=payload, headers=headers)
    except Exception:
        _EMBED[base] = False
        return None
    if response.status_code in {404, 405, 501}:
        _EMBED[base] = False
        return None
    if response.status_code >= 400:
        _EMBED[base] = False
        return None
    try:
        body = response.json()
    except Exception:
        _EMBED[base] = False
        return None
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        _EMBED[base] = False
        return None
    vectors: list[list[float] | None] = [None] * len(texts)
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index") or 0)
        except (TypeError, ValueError):
            continue
        raw = row.get("embedding")
        if not isinstance(raw, list) or index < 0 or index >= len(vectors):
            continue
        try:
            vectors[index] = [float(item) for item in raw]
        except (TypeError, ValueError):
            continue
    if any(item is None for item in vectors):
        _EMBED[base] = False
        return None
    _EMBED[base] = True
    return [item for item in vectors if item is not None]


def _rerank(endpoint: dict | None, query: str, hits: list[Passage]) -> list[Passage]:
    if not endpoint or not hits:
        return hits
    base = str(endpoint.get("base_url") or "")
    model = str(endpoint.get("model") or "").strip() or None
    vectors = _post_embeddings(base, endpoint.get("api_key") or None, model, [query, *[hit.text[:_PASSAGE_CHARS] for hit in hits]])
    if not vectors:
        return hits
    query_vec = vectors[0]
    peak = max(hit.score for hit in hits) or 1.0
    blended: list[Passage] = []
    for hit, vec in zip(hits, vectors[1:]):
        mixed = 0.65 * (hit.score / peak) + 0.35 * _cosine(query_vec, vec)
        blended.append(
            Passage(
                chat_id=hit.chat_id,
                title=hit.title,
                index=hit.index,
                message_id=hit.message_id,
                role=hit.role,
                text=hit.text,
                created_at=hit.created_at,
                score=mixed,
            )
        )
    blended.sort(key=lambda item: item.score, reverse=True)
    return blended


def _query_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _format(hits: list[Passage]) -> str:
    if not hits:
        return ""
    lines = ["From earlier:"]
    for hit in hits[:_TOP]:
        stamp = hit.created_at or "undated"
        role = hit.role or "user"
        lines.append(f"[{stamp}] {role}: {_clip(hit.text, _PASSAGE_CHARS)}")
    text = "\n".join(lines).strip()
    if len(text) <= _BLOCK_CHARS:
        return text
    return text[: _BLOCK_CHARS - 1].rstrip() + "…"


def earlier_block(
    store: Store,
    bot_id: str,
    chat: dict,
    skip_through: int,
    endpoint: dict | None = None,
) -> str:
    """Passages from the full on-disk history, labeled for the prompt.

    Messages already in the verbatim tail are skipped. Nothing is written.
    """
    messages = list(chat.get("messages") or [])
    query = _query_text(messages)
    if not tokens(query):
        return ""
    chat_id = str(chat.get("id") or "")
    through = skip_through if skip_through > 0 else 0
    if through > len(messages):
        through = len(messages)
    skip = {(chat_id, index) for index in range(through, len(messages))}
    hits = search_passages(store, bot_id, query, skip=skip, limit=_CANDIDATES)
    hits = _rerank(endpoint, query, hits)
    block = _format(hits)
    if not block:
        return ""
    return redact(store, block).strip()
