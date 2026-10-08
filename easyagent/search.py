"""Web search that runs on the computer next to EasyAgent.

The model asks with one ```search fence. This module fetches the result.
The phone is not involved, and nothing is written to disk.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import unquote

import httpx

from easyagent import llm

FENCE_RE = re.compile(r"```search[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_IGNORED_QUERIES = {"the query", "query"}
_DEFAULT_URL = "https://api.duckduckgo.com/"
_DDG_HTML = "https://html.duckduckgo.com/html/"
_CNN_LITE = "https://lite.cnn.com/"
_NOTHING = "The search found nothing."


class SearchError(Exception):
    """The search provider did not return usable results. The chat stays."""


def search_query(reply: str) -> str | None:
    match = FENCE_RE.search(reply or "")
    if not match:
        return None
    query = " ".join(match.group(1).split())
    if not query or query.lower() in _IGNORED_QUERIES:
        return None
    return query[:200]


def strip_search_fences(reply: str) -> str:
    text = FENCE_RE.sub("", reply or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def complete_with_search(*, base_url: str, api_key: str | None, model: str | None, messages: list[dict]) -> str:
    """One model call, plus a second only when the model asked for a search."""
    raw = await llm.complete(base_url=base_url, api_key=api_key, model=model, messages=messages)
    query = search_query(raw)
    if not query:
        return strip_search_fences(raw)
    findings = await web_search(query)
    followup = [
        *messages,
        {"role": "assistant", "content": f"I need a web search for: {query}"},
        {
            "role": "user",
            "content": (
                "Web search ran on this computer, not on the phone.\n"
                f"Query: {query}\n\n{findings}\n\n"
                "Answer the user from these results. Do not include a search fence."
            ),
        },
    ]
    answered = await llm.complete(base_url=base_url, api_key=api_key, model=model, messages=followup)
    return strip_search_fences(answered)


async def stream_with_search(*, base_url: str, api_key: str | None, model: str | None, messages: list[dict]):
    """Yield ('delta'|'status'|'replace', text) and then ('final', saved text)."""
    first: list[str] = []
    async for piece in llm.stream_complete(base_url=base_url, api_key=api_key, model=model, messages=messages):
        first.append(piece)
        yield ("delta", piece)
    raw = "".join(first)
    query = search_query(raw)
    if not query:
        yield ("final", strip_search_fences(raw))
        return
    yield ("replace", "")
    yield ("status", "Searching")
    findings = await web_search(query)
    followup = [
        *messages,
        {"role": "assistant", "content": f"I need a web search for: {query}"},
        {
            "role": "user",
            "content": (
                "Web search ran on this computer, not on the phone.\n"
                f"Query: {query}\n\n{findings}\n\n"
                "Answer the user from these results. Do not include a search fence."
            ),
        },
    ]
    second: list[str] = []
    async for piece in llm.stream_complete(base_url=base_url, api_key=api_key, model=model, messages=followup):
        second.append(piece)
        yield ("delta", piece)
    yield ("final", strip_search_fences("".join(second)))


def _news_query(text: str) -> bool:
    return bool(re.search(r"(?i)\b(news|headline|headlines|top story|top stories|cnn|bbc)\b", text or ""))


def stories_from_cnn_lite(html: str) -> list[dict]:
    """The first article link on the CNN lite page. Navigation labels are skipped."""
    items = []
    for href, inner in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html or "", re.I | re.S):
        title = " ".join(re.sub(r"<[^>]+>", " ", inner).split())
        if len(title) < 30 or not re.search(r"/\d{4}/\d{2}/\d{2}/", href or ""):
            continue
        url = href if href.startswith("http") else "https://www.cnn.com" + href
        items.append({"title": title[:180], "url": url, "snippet": title[:300]})
        break
    return items


def stories_from_duckduckgo_html(html: str) -> list[dict]:
    items = []
    pattern = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
    for href, inner in pattern.findall(html or ""):
        title = " ".join(re.sub(r"<[^>]+>", " ", inner).split()).replace("&amp;", "&")
        if len(title) < 8:
            continue
        match = re.search(r"uddg=([^&]+)", href or "")
        url = unquote(match.group(1)) if match else href
        if not str(url).startswith("http"):
            continue
        items.append({"title": title[:180], "url": url, "snippet": title[:300]})
        if len(items) >= 5:
            break
    return items


async def _get_text(url: str, params: dict | None = None) -> str:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"User-Agent": "EasyAgent"})
    except httpx.TimeoutException as exc:
        raise SearchError("Search failed: the search provider timed out.") from exc
    except httpx.HTTPError as exc:
        raise SearchError(f"Search failed: could not reach the search provider. {exc}") from exc
    if response.status_code >= 400:
        raise SearchError(f"Search failed: {response.status_code} from the search provider.")
    return response.text


async def _duckduckgo_items(query: str) -> list[dict]:
    url = (os.environ.get("EASYAGENT_SEARCH_URL") or _DEFAULT_URL).strip()
    raw = await _get_text(url, {"q": query[:200], "format": "json", "no_html": "1", "skip_disambig": "1"})
    try:
        payload = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    return results_from_json(payload)


async def _cnn_lite_items() -> list[dict]:
    return stories_from_cnn_lite(await _get_text(_CNN_LITE))


async def _duckduckgo_html_items(query: str) -> list[dict]:
    return stories_from_duckduckgo_html(await _get_text(_DDG_HTML, {"q": query[:200]}))


async def web_search(query: str) -> str:
    text = " ".join((query or "").split())
    if not text:
        raise SearchError("Search failed: the query was empty.")
    items: list[dict] = []
    errors: list[SearchError] = []
    reached = False
    try:
        items = await _duckduckgo_items(text)
        reached = True
    except SearchError as exc:
        errors.append(exc)
    if not items and _news_query(text):
        try:
            items = await _cnn_lite_items()
            reached = True
        except SearchError as exc:
            errors.append(exc)
    elif not items:
        try:
            items = await _duckduckgo_html_items(text)
            reached = True
        except SearchError as exc:
            errors.append(exc)
    if items:
        return format_results(items)
    if reached:
        return _NOTHING
    if errors:
        raise errors[-1]
    return _NOTHING


def results_from_json(data: dict) -> list[dict]:
    if not isinstance(data, dict):
        return []
    items = []
    abstract = " ".join((data.get("AbstractText") or "").split())
    url = data.get("AbstractURL") or ""
    if abstract and str(url).startswith("http"):
        items.append({"title": data.get("Heading") or "Result", "url": url, "snippet": abstract})

    def walk(topics):
        for topic in topics or []:
            if not isinstance(topic, dict):
                continue
            if topic.get("Topics"):
                walk(topic.get("Topics"))
                continue
            text = " ".join((topic.get("Text") or "").split())
            link = topic.get("FirstURL") or ""
            if text and str(link).startswith("http"):
                items.append({"title": text.split(" - ", 1)[0][:120], "url": link, "snippet": text})

    walk(data.get("RelatedTopics"))
    return items[:5]


def format_results(items: list[dict]) -> str:
    lines = []
    for index, item in enumerate(items, start=1):
        block = f"{index}. {item['title']}\n{item['url']}"
        if item.get("snippet"):
            block += f"\n{item['snippet']}"
        lines.append(block)
    text = "\n\n".join(lines)
    if len(text) > 2000:
        text = text[:1960].rstrip() + "\n[search truncated]"
    return text

