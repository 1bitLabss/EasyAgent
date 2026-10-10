"""Web search that runs on the computer next to EasyAgent.

The model asks with one ```search fence. This module fetches the result.
The phone is not involved. A provider key stays in the encrypted secrets database.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import re
from urllib.parse import unquote, urlparse

import httpx

from easyagent import llm
from easyagent.secrets import delete_secret, get_secret, put_secret
from easyagent.store import Store, atomic_write_json, read_json

FENCE_RE = re.compile(r"```search[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
FETCH_RE = re.compile(r"```fetch[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
RESEARCH_RE = re.compile(r"```research[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_SEARCH_STORE: contextvars.ContextVar = contextvars.ContextVar("ea_search_store", default=None)
_IGNORED_QUERIES = {"the query", "query"}
_DEFAULT_URL = "https://api.duckduckgo.com/"
_DDG_HTML = "https://html.duckduckgo.com/html/"
_CNN_LITE = "https://lite.cnn.com/"
_NOTHING = "The search found nothing."


class SearchError(Exception):
    """The search provider did not return usable results. The chat stays."""


def bind_store(store: Store | None):
    """Chat and routine turns use the saved provider without changing every call."""
    return _SEARCH_STORE.set(store)


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
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|span|div)>', html or "", re.I | re.S)
    dates = re.findall(r'class="result__timestamp"[^>]*>(.*?)</(?:span|td)>', html or "", re.I | re.S)
    for index, (href, inner) in enumerate(pattern.findall(html or "")):
        title = " ".join(re.sub(r"<[^>]+>", " ", inner).split()).replace("&amp;", "&")
        if len(title) < 8:
            continue
        match = re.search(r"uddg=([^&]+)", href or "")
        url = unquote(match.group(1)) if match else href
        if not str(url).startswith("http"):
            continue
        snippet = title[:300]
        if index < len(snippets):
            found = " ".join(re.sub(r"<[^>]+>", " ", snippets[index]).split())
            if found:
                snippet = found[:300]
        date = ""
        if index < len(dates):
            date = " ".join(re.sub(r"<[^>]+>", " ", dates[index]).split())[:40]
        items.append({"title": title[:180], "url": url, "snippet": snippet, "date": date})
        if len(items) >= 5:
            break
    return items


async def _get_text(url: str, params: dict | None = None, headers: dict | None = None) -> str:
    try:
        sent = {"User-Agent": "EasyAgent"}
        if headers:
            sent.update(headers)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=sent)
    except httpx.TimeoutException as exc:
        raise SearchError("Search failed: the search provider timed out.") from exc
    except httpx.HTTPError as exc:
        # A keyed provider puts the secret in a header. The error text must not echo it.
        if headers:
            raise SearchError("Search failed: could not reach the search provider.") from exc
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


async def web_search(query: str, store: Store | None = None) -> str:
    """Search from this process. The request is not sent from inside the tool container."""
    text = " ".join((query or "").split())
    if not text:
        raise SearchError("Search failed: the query was empty.")
    if store is None:
        store = _SEARCH_STORE.get()
    settings = load_settings(store)
    provider = settings["provider"]
    if provider == "searxng":
        items = await _searxng_items(settings["searxng_url"], text)
        return format_results(items) if items else _NOTHING
    if provider == "brave":
        items = await _brave_items(text, store)
        return format_results(items) if items else _NOTHING
    if provider == "tavily":
        items = await _tavily_items(text, store)
        return format_results(items) if items else _NOTHING
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
        if item.get("date"):
            block += f"\n{item['date']}"
        if item.get("snippet"):
            block += f"\n{item['snippet']}"
        lines.append(block)
    text = "\n\n".join(lines)
    if len(text) > 2000:
        text = text[:1960].rstrip() + "\n[search truncated]"
    return text


_PROVIDERS = {"duckduckgo", "searxng", "brave", "tavily"}
_BRAVE_ACCOUNT = "search:brave"
_TAVILY_ACCOUNT = "search:tavily"
_PAGE_CAP = 8000
_CACHE_CAP = 20
_CITE = re.compile(r"\[(\d+)\]")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'-]{4,}")
_STOP = {
    "about", "after", "again", "being", "could", "every", "first", "their", "there",
    "these", "thing", "those", "under", "where", "which", "while", "would", "source",
}


def load_settings(store: Store | None) -> dict:
    """Which provider to use. Keys are not in this file. An env value wins over the file."""
    found = {"provider": "duckduckgo", "searxng_url": ""}
    if store is not None:
        path = store.root / "search.json"
        if path.is_file():
            try:
                data = read_json(path)
            except Exception:
                data = None
            if isinstance(data, dict):
                provider = str(data.get("provider") or "").strip().lower()
                if provider in _PROVIDERS:
                    found["provider"] = provider
                url = str(data.get("searxng_url") or "").strip()
                if url:
                    found["searxng_url"] = url
    env_provider = (os.environ.get("EASYAGENT_SEARCH_PROVIDER") or "").strip().lower()
    if env_provider in _PROVIDERS:
        found["provider"] = env_provider
    env_url = (os.environ.get("EASYAGENT_SEARXNG_URL") or "").strip()
    if env_url:
        found["searxng_url"] = env_url
    return found


def public_settings(store: Store) -> dict:
    settings = load_settings(store)
    return {
        "provider": settings["provider"],
        "searxng_url": settings["searxng_url"],
        "has_brave_key": bool(get_secret(_BRAVE_ACCOUNT, store)),
        "has_tavily_key": bool(get_secret(_TAVILY_ACCOUNT, store)),
    }


def save_settings(
    store: Store,
    *,
    provider: str,
    searxng_url: str = "",
    brave_key: str | None = None,
    tavily_key: str | None = None,
    clear_brave: bool = False,
    clear_tavily: bool = False,
) -> dict:
    name = (provider or "duckduckgo").strip().lower()
    if name not in _PROVIDERS:
        raise SearchError("Choose DuckDuckGo, SearXNG, Brave, or Tavily. Nothing was saved.")
    url = (searxng_url or "").strip()
    if name == "searxng":
        if not url.startswith("http://") and not url.startswith("https://"):
            raise SearchError("SearXNG needs an http or https address. Nothing was saved.")
    elif url and not url.startswith("http://") and not url.startswith("https://"):
        raise SearchError("That SearXNG address is not http or https. Nothing was saved.")
    if clear_brave:
        delete_secret(_BRAVE_ACCOUNT, store)
    if clear_tavily:
        delete_secret(_TAVILY_ACCOUNT, store)
    if brave_key:
        put_secret(_BRAVE_ACCOUNT, brave_key.strip(), store)
    if tavily_key:
        put_secret(_TAVILY_ACCOUNT, tavily_key.strip(), store)
    atomic_write_json(store.root / "search.json", {"provider": name, "searxng_url": url})
    return public_settings(store)


def stories_from_searxng(data: dict) -> list[dict]:
    items = []
    for row in (data or {}).get("results") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        title = " ".join(str(row.get("title") or "").split())
        if not title or not url.startswith("http"):
            continue
        items.append({
            "title": title[:180],
            "url": url,
            "snippet": " ".join(str(row.get("content") or "").split())[:300],
            "date": " ".join(str(row.get("publishedDate") or "").split())[:40],
        })
        if len(items) >= 5:
            break
    return items


def stories_from_brave(data: dict) -> list[dict]:
    items = []
    web = (data or {}).get("web") if isinstance(data, dict) else None
    rows = web.get("results") if isinstance(web, dict) else []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        title = " ".join(str(row.get("title") or "").split())
        if not title or not url.startswith("http"):
            continue
        items.append({
            "title": title[:180],
            "url": url,
            "snippet": " ".join(str(row.get("description") or "").split())[:300],
            "date": " ".join(str(row.get("age") or row.get("page_age") or "").split())[:40],
        })
        if len(items) >= 5:
            break
    return items


def stories_from_tavily(data: dict) -> list[dict]:
    items = []
    for row in (data or {}).get("results") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        title = " ".join(str(row.get("title") or "").split())
        if not title or not url.startswith("http"):
            continue
        items.append({
            "title": title[:180],
            "url": url,
            "snippet": " ".join(str(row.get("content") or "").split())[:300],
            "date": " ".join(str(row.get("published_date") or "").split())[:40],
        })
        if len(items) >= 5:
            break
    return items


def clean_http_url(value: str) -> str:
    text = (value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SearchError("Fetch failed: only an http or https page can be read.")
    return text


def readable_text(html: str) -> str:
    """Article text from a page. Scripts and styles are not part of it."""
    raw = html or ""
    raw = re.sub(r"(?is)<(script|style|noscript|svg)\b[^>]*>.*?</\1>", " ", raw)
    chunk = raw
    for tag in ("article", "main"):
        match = re.search(rf"(?is)<{tag}\b[^>]*>(.*?)</{tag}>", raw)
        if match and len(match.group(1)) > 80:
            chunk = match.group(1)
            break
    chunk = re.sub(r"(?is)<(nav|footer|header|aside)\b[^>]*>.*?</\1>", " ", chunk)
    chunk = re.sub(r"(?i)<br\s*/?>", "\n", chunk)
    chunk = re.sub(r"(?i)</p>", "\n", chunk)
    text = re.sub(r"<[^>]+>", " ", chunk)
    text = " ".join(text.replace("&amp;", "&").replace("&nbsp;", " ").split())
    return text[:_PAGE_CAP]


def looks_js_heavy(html: str, text: str) -> bool:
    if len((text or "").strip()) >= 200:
        return False
    return (html or "").lower().count("<script") >= 3


def _cache_file(store: Store, bot_id: str, chat_id: str):
    slot = None
    try:
        from easyagent import turn as turn_mod

        slot = turn_mod._slot.get()
    except Exception:
        slot = None
    bot = bot_id or (getattr(slot, "bot_id", "") if slot else "")
    chat = chat_id or (getattr(slot, "chat_id", "") if slot else "")
    if not bot or not chat:
        return None
    try:
        return store._bot_dir(bot) / "pages" / f"{chat}.json"
    except Exception:
        return None


def cached_page(store: Store | None, bot_id: str | None, url: str, chat_id: str = "") -> str:
    if store is None:
        return ""
    path = _cache_file(store, bot_id or "", chat_id)
    if path is None or not path.is_file():
        return ""
    try:
        data = read_json(path)
    except Exception:
        return ""
    row = data.get(_page_key(url)) if isinstance(data, dict) else None
    if isinstance(row, dict):
        return str(row.get("text") or "")
    return ""


def remember_page(store: Store | None, bot_id: str | None, url: str, text: str, chat_id: str = "") -> None:
    if store is None or not text.strip():
        return
    path = _cache_file(store, bot_id or "", chat_id)
    if path is None:
        return
    try:
        data = read_json(path) if path.is_file() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[_page_key(url)] = {"url": url, "text": text[:_PAGE_CAP]}
    while len(data) > _CACHE_CAP:
        data.pop(next(iter(data)))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)


def _page_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


async def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.post(url, json=payload, headers=headers or {})
    except httpx.TimeoutException as exc:
        raise SearchError("Search failed: the search provider timed out.") from exc
    except httpx.HTTPError as exc:
        raise SearchError("Search failed: could not reach the search provider.") from exc
    if response.status_code >= 400:
        raise SearchError(f"Search failed: {response.status_code} from the search provider.")
    try:
        data = response.json()
    except ValueError as exc:
        raise SearchError("Search failed: the search provider did not return JSON.") from exc
    return data if isinstance(data, dict) else {}


async def _searxng_items(base: str, query: str) -> list[dict]:
    url = (base or "").strip().rstrip("/")
    if not url:
        raise SearchError("Add a SearXNG address in Connections. Nothing was searched.")
    raw = await _get_text(url + "/search", {"q": query[:200], "format": "json"})
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SearchError("Search failed: SearXNG did not return JSON.") from exc
    return stories_from_searxng(data if isinstance(data, dict) else {})


async def _brave_items(query: str, store=None) -> list[dict]:
    key = get_secret(_BRAVE_ACCOUNT, store)
    if not key:
        raise SearchError("Add a Brave key in Connections. Nothing was searched.")
    raw = await _get_text(
        "https://api.search.brave.com/res/v1/web/search",
        {"q": query[:200]},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SearchError("Search failed: Brave did not return JSON.") from exc
    return stories_from_brave(data if isinstance(data, dict) else {})


async def _tavily_items(query: str, store=None) -> list[dict]:
    key = get_secret(_TAVILY_ACCOUNT, store)
    if not key:
        raise SearchError("Add a Tavily key in Connections. Nothing was searched.")
    data = await _post_json(
        "https://api.tavily.com/search",
        {"api_key": key, "query": query[:200], "max_results": 5},
    )
    return stories_from_tavily(data)


def sources_from_text(text: str) -> list[dict]:
    """Numbered results in a search or research packet. Page text stays with the source."""
    from easyagent.safety import strip_untrusted

    items = []
    for block in strip_untrusted(text or "").split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        url = next((line for line in lines if line.startswith("http")), "")
        if not url:
            continue
        title = lines[0].split(". ", 1)[-1][:180]
        extras = [line for line in lines if line != lines[0] and not line.startswith("http")]
        snippet = max(extras, key=len) if extras else ""
        items.append({
            "title": title,
            "url": url,
            "snippet": snippet[:300],
            "text": " ".join(extras)[:_PAGE_CAP],
        })
    return items


async def _browser_read(store: Store, bot_id: str, url: str) -> str:
    from easyagent.browser import perform
    from easyagent.tools import ToolRequest

    request = ToolRequest(kind="browser", action="open", path=url, body=url)
    text, _shot = await asyncio.to_thread(perform, store, request, bot_id)
    return readable_text(text or "")


async def fetch_page(url: str, store: Store | None = None, bot_id: str | None = None, opener=None) -> str:
    """Readable text from one page, fenced as data. The fetch runs in this process, not in the tool container. A thin script page uses the browser."""
    from easyagent.safety import mark_untrusted

    target = clean_http_url(url)
    cached = cached_page(store, bot_id, target)
    if cached:
        return mark_untrusted(cached)
    html = await _get_text(target)
    text = readable_text(html)
    if looks_js_heavy(html, text):
        opened = ""
        try:
            if opener is not None:
                opened = await opener(target)
            elif store is not None and bot_id:
                opened = await _browser_read(store, bot_id, target)
        except Exception:
            opened = ""
        if opened and len(opened.strip()) > len(text.strip()):
            text = readable_text(opened) or opened
    text = (text or "").strip()
    if not text:
        raise SearchError("Fetch failed: that page had no readable text.")
    remember_page(store, bot_id, target, text)
    return mark_untrusted(text[:_PAGE_CAP])


async def research(question: str, store: Store | None = None, bot_id: str | None = None) -> str:
    """Search, then read the top results. The answer has to cite those numbers."""
    from easyagent.safety import mark_untrusted

    findings = await web_search(question, store=store)
    sources = sources_from_text(findings)[:3]
    if not sources:
        return findings
    blocks = [
        "Cite each claim with the source number in brackets, like [1]. A claim with no source number is not done."
    ]
    for index, source in enumerate(sources, start=1):
        try:
            page = await fetch_page(source["url"], store=store, bot_id=bot_id)
        except SearchError as exc:
            page = str(exc)
        source["text"] = page
        date = f"\n{source['date']}" if source.get("date") else ""
        blocks.append(f"[{index}] {source['title']}\n{source['url']}{date}\n{page}")
    return mark_untrusted("\n\n".join(blocks))


def _sentence_with(answer: str, number: int) -> str:
    marker = f"[{number}]"
    for piece in re.split(r"(?<=[.!?])\s+", answer or ""):
        if marker in piece:
            return piece
    return answer or ""


def _shares(sentence: str, source: dict) -> bool:
    hay = " ".join(
        str(source.get(key) or "") for key in ("title", "snippet", "text", "url")
    ).lower()
    for word in _WORD.findall(sentence or ""):
        token = word.lower().strip("-'")
        if token in _STOP or len(token) < 5:
            continue
        if token in hay:
            return True
    return False


def citation_problems(answer: str, sources: list[dict]) -> list[str]:
    """Each bracketed number has to point at a fetched source that supports the sentence."""
    if not sources:
        return []
    numbers = [int(item) for item in _CITE.findall(answer or "")]
    if not numbers:
        return ["The answer does not cite a source."]
    problems = []
    for number in numbers:
        if number < 1 or number > len(sources):
            problems.append(f"[{number}] is not a source that was fetched.")
            continue
        if not _shares(_sentence_with(answer, number), sources[number - 1]):
            problems.append(f"[{number}] does not support that sentence.")
    return problems

