"""Local HTTP API and the single-page UI."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from easyagent import __version__
from easyagent import gate
from easyagent import llm
from easyagent import turn as turn_mod
from easyagent.access import contained_file, is_lan, is_local, is_shell, presented_token, refusal_html
from easyagent.phone import (
    LanPort,
    PhoneBook,
    access_for,
    add_firewall_rule,
    qr_svg,
    status_payload,
)
from easyagent.context import (
    ERROR_PLACEHOLDER,
    bot_context_chars,
    bot_context_tokens,
    clip_message,
    context_window,
    prepare_context,
)
from easyagent.prompt import build_system
from easyagent.turnctx import model_turn, visible_prepare
from easyagent.schedule import (
    ScheduleError,
    active_routines,
    compile_routine,
    describe,
    parse_cron,
    pause_all,
    preview,
    run_routine_now,
    schedule_loop,
    template_records,
)
from easyagent.search import SearchError
from easyagent.handoff import take_file
from easyagent.night import run_night
from easyagent.notify import poke
from easyagent.tools import (
    ToolError,
    complete_with_tools,
    contains_secret,
    keep_lessons,
    redact,
    stream_with_tools,
    tapback_from_reply,
)
from easyagent.vault import seal
from easyagent.subagent import (
    child_messages,
    error_line,
    parse_subagent,
    short_result,
    strip_subagent_fences,
)
from easyagent.tunnel import relay_loop
from easyagent.unread import mark_chat_read, mark_room_read, unread_snapshot
from easyagent.learn import (
    approve_candidate,
    chats_active,
    reject_candidate,
    learn_loop,
    lesson_block,
    mark_origin,
    panel as learning_panel,
    rank_skills,
    request_stop as request_learn_stop,
    rollback_for_bot,
    rollback_latest,
    set_manual,
    set_paused,
    sleep_once,
)
from easyagent.skills import RESERVED_SLUGS, extract_skills, pack_skills, slugify
from easyagent.mascot import clean_face_color, colors_for_bots, face_color_for
from easyagent.store import Store, StoreError, message_index, new_id, now_iso, reaction_signal
from easyagent.limits import MAX_CONTEXT_TOKENS, MAX_STORED_MESSAGE_CHARS, MIN_CONTEXT_TOKENS
from easyagent.selfinfo import ensure_own_files, own_files_prompt

STATIC_DIR = Path(__file__).resolve().parent / "static"
UI_DIR = Path(__file__).resolve().parent / "ui"
_STATIC_MEDIA = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}
_LOCKS: dict[str, asyncio.Lock] = {}
_LOCKS_GUARD = threading.Lock()
LIVE_SAVE_SECONDS = 1.5
_live_saved_at: dict[str, float] = {}
_log = logging.getLogger("easyagent")


def default_data_dir() -> Path:
    env = os.environ.get("EASYAGENT_DATA")
    if env:
        return Path(env)
    return Path.cwd() / "data"


def chat_lock(store: Store, chat_id: str) -> asyncio.Lock:
    key = f"{store.root}:{chat_id}"
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[key] = lock
        return lock


def public_endpoint(endpoint: dict) -> dict:
    return {
        "id": endpoint["id"],
        "name": endpoint["name"],
        "base_url": endpoint["base_url"],
        "has_api_key": bool(endpoint.get("has_api_key")) or bool(endpoint.get("api_key")),
        "model": endpoint.get("model") or None,
        "max_parallel": gate.clamp_parallel(endpoint.get("max_parallel")),
        "created_at": endpoint.get("created_at"),
    }


def public_bot(store: Store, bot: dict) -> dict:
    endpoint = None
    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except StoreError:
        endpoint = None
    stored = _stored_face_color(bot)
    colors = colors_for_bots(store.list_bots())
    return {
        "id": bot["id"],
        "name": bot["name"],
        "endpoint_id": bot["endpoint_id"],
        "endpoint_name": endpoint["name"] if endpoint else None,
        "endpoint_base_url": endpoint["base_url"] if endpoint else None,
        "model": bot.get("model"),
        "context_tokens": bot_context_tokens(bot),
        "context_chars": bot_context_chars(bot),
        "created_at": bot.get("created_at"),
        "face_color": stored or colors.get(bot["id"]) or face_color_for(bot["id"]),
        "face_color_set": bool(stored),
        "check_enabled": bot.get("check_enabled") is not False,
        "learn_paused": bot.get("learn_paused") is True,
        "learn_manual": bot.get("learn_manual") is True,
        "safety_mode": bot.get("safety_mode") or "careful",
        "safety_unlocks": list(bot.get("safety_unlocks") or []),
        "browser_headless": bool(bot.get("browser_headless")),
        "browser_allow": list(bot.get("browser_allow") or []),
        "browser_deny": list(bot.get("browser_deny") or []),
    }


def _stored_face_color(bot: dict) -> str:
    """A saved palette color. A missing or unknown value stays off the file."""
    raw = bot.get("face_color")
    if not isinstance(raw, str):
        return ""
    try:
        return clean_face_color(raw)
    except StoreError:
        return ""


def _workspace_text(store: Store, bot_id: str) -> str:
    from easyagent.paths import display_path
    from easyagent.workspace import bot_workspace

    return display_path(bot_workspace(store, bot_id))


def make_title(content: str, store: Store | None = None) -> str:
    text = " ".join(content.split())
    if store is not None:
        from easyagent.journal import scrub_text

        text = " ".join(scrub_text(store, text).split())
    text = _redact_title(text)
    if len(text) <= 72:
        return text or "New chat"
    return text[:71] + "…"


def _redact_title(text: str) -> str:
    """A chat title does not keep an email address or a password."""
    cleaned = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[redacted]", text or "")
    cleaned = re.sub(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+", r"\1 [redacted]", cleaned)
    return cleaned


def clean_name(value: str, label: str) -> str:
    name = " ".join((value or "").split())
    if not name or len(name) > 80:
        raise StoreError(f"{label} must be 1–80 characters.", 400)
    return name


def clean_base_url(value: str) -> str:
    url = (value or "").strip().rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise StoreError("The address must start with http:// or https://.", 400)
    if any(ch.isspace() for ch in url):
        raise StoreError("The address cannot contain spaces.", 400)
    return url


def chosen_model(bot: dict, endpoint: dict) -> str | None:
    """The one model the user set for this turn, or nothing.

    A model typed on the bot wins. Otherwise the model typed on the endpoint
    is used. If neither was set, the request omits the field. No default.
    """
    bot_model = (bot.get("model") or "").strip()
    if bot_model:
        return bot_model
    endpoint_model = (endpoint.get("model") or "").strip()
    return endpoint_model or None


def clean_context_tokens(value: int | None) -> int | None:
    """Token budget for the summary + recent chat. None means the default."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError("How much chat it sees must be a whole number of tokens.", 400)
    if value < MIN_CONTEXT_TOKENS or value > MAX_CONTEXT_TOKENS:
        raise StoreError(
            f"How much chat it sees must be a whole number of tokens from {MIN_CONTEXT_TOKENS} to {MAX_CONTEXT_TOKENS}. "
            "The saved chat was not cut.",
            400,
        )
    return value


def clean_max_parallel(value) -> int:
    """How many replies may use this connection at once."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError("At once must be a whole number from 1 to 32.", 400)
    if value < 1 or value > 32:
        raise StoreError("At once must be a whole number from 1 to 32.", 400)
    return value


def clean_model(value: str | None) -> str | None:
    if value is None:
        return None
    model = value.strip()
    if not model:
        return None
    if len(model) > 120 or any(ch in model for ch in "\r\n"):
        raise StoreError("Model must be a single line, 120 characters or fewer.", 400)
    return model


class EndpointIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    base_url: str
    api_key: str | None = None
    model: str | None = None
    max_parallel: int | None = None


class EndpointPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    clear_api_key: bool = False
    max_parallel: int | None = None


class ConfirmIn(BaseModel):
    confirm_name: str = Field(min_length=1)


class BotIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    endpoint_id: str
    model: str | None = None
    context_tokens: int | None = None


class BotPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = None
    endpoint_id: str | None = None
    model: str | None = None
    context_tokens: int | None = None
    face_color: str | None = None
    check_enabled: bool | None = None


class LearnFlag(BaseModel):
    model_config = ConfigDict(extra="ignore")
    on: bool = False


class SafetyIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: str = "careful"
    confirm_name: str = ""
    unlocks: list[str] | None = None


class HonestyIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    receipts: bool | None = None
    pushback: bool | None = None
    excuse: bool | None = None
    loop: bool | None = None
    tripwires: bool | None = None
    stall: bool | None = None
    stall_minutes: int | None = None


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision: str = "deny"


class BrowserIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    headless: bool = False
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class ConnectorIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    transport: str = "stdio"
    command: list[str] = Field(default_factory=list)
    url: str = ""
    secrets: dict[str, str] = Field(default_factory=dict)
    package: str = ""
    version: str = ""


class ConnectorPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool | None = None
    tools: dict[str, str] = Field(default_factory=dict)


class StarterIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    starter: str = ""
    name: str = ""
    path: str = ""


class SearchSetupIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider: str = "duckduckgo"
    searxng_url: str = ""
    brave_key: str | None = None
    tavily_key: str | None = None
    clear_brave: bool = False
    clear_tavily: bool = False


class SandboxIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str = ""


class RetentionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    retain_days: int | None = None
    keep_forever: bool | None = None
    pruning: bool | None = None
    window_start: str | None = None
    window_end: str | None = None
    idle_minutes: int | None = None


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str = ""
    attachment_id: str | None = None


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str
    topic: str | None = None


class WatchIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str


class ReactionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emoji: str


class AskIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bot_id: str
    task: str


class RoomIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str


class RoomBotIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bot_id: str


class ComputerIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    kind: str
    host: str
    user: str
    password: str | None = None
    key: str | None = None
    port: int | None = None


class SkillIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    description: str | None = None
    body: str = ""


class DirectionIn(BaseModel):
    text: str


class PhoneIn(BaseModel):
    enabled: bool


class FirewallIn(BaseModel):
    consent: bool = False


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt: str = ""
    kind: str | None = None
    every_minutes: int | None = None
    every_hours: int | None = None
    cron: str | None = None
    name: str | None = None
    timezone: str | None = None
    quiet: bool | None = None
    paused: bool | None = None
    weekdays: str | None = None
    daily: str | None = None
    weekly: str | None = None
    weekly_day: str | None = None
    weekly_time: str | None = None
    once: str | None = None
    preset: str | None = None


class SchedulePauseIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    paused: bool


class ReadIn(BaseModel):
    """How many stored messages the open view has on screen. Omitted means the whole transcript."""

    model_config = ConfigDict(extra="ignore")
    through: int | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.loop = asyncio.get_running_loop()
    from easyagent.sandbox import warm_probe

    warm_probe()
    stop = asyncio.Event()
    task = asyncio.create_task(schedule_loop(app.state.store, stop))
    learn_task = asyncio.create_task(learn_loop(app.state.store, stop))
    relay_task = asyncio.create_task(relay_loop(app, stop))
    try:
        yield
    finally:
        stop.set()
        running = turn_mod.stop_all("the server restarted")
        for worker in running:
            worker.cancel()
        relay_task.cancel()
        learn_task.cancel()
        task.cancel()
        for worker in running:
            with suppress(asyncio.CancelledError, Exception):
                await worker
        with suppress(asyncio.CancelledError):
            await relay_task
        with suppress(asyncio.CancelledError):
            await learn_task
        with suppress(asyncio.CancelledError):
            await task


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    store = Store(Path(data_dir) if data_dir is not None else default_data_dir())
    store.ensure()
    _settle_abandoned_runs(store)
    app = FastAPI(title="EasyAgent", lifespan=_lifespan)
    app.state.store = store
    app.state.phone = PhoneBook(store.root)
    app.state.lan = LanPort()

    @app.middleware("http")
    async def remote_access(request: Request, call_next):
        host = request.client.host if request.client else None
        presented = presented_token(request.headers)
        phone = request.app.state.phone
        decision = access_for(host, presented, phone)
        if decision == "allow" and not is_local(host):
            phone.claim_if_invite(presented, request.headers.get("user-agent") or "")
        if decision == "refuse":
            if is_lan(host) and not phone.enabled:
                kind = "off"
                detail = "Phone access is off. Turn it on in Settings on the computer running EasyAgent."
            else:
                kind = "public"
                detail = "EasyAgent does not answer this network."
            headers = {"Cache-Control": "no-store"}
            if request.url.path == "/" or "text/html" in request.headers.get("accept", ""):
                return HTMLResponse(refusal_html(kind), status_code=403, headers=headers)
            return JSONResponse({"detail": detail}, status_code=403, headers=headers)
        if decision == "need_token" and not is_shell(request.url.path):
            return JSONResponse(
                {"detail": "This phone needs a pairing token."},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        response = await call_next(request)
        if request.url.path in {"/", "/classic"} or request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StoreError)
    async def on_store_error(_request: Request, exc: StoreError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def on_validation(_request: Request, exc: RequestValidationError):
        parts = []
        for err in exc.errors():
            loc = ".".join(str(item) for item in err.get("loc", []) if item != "body")
            message = err.get("msg", "invalid")
            parts.append(f"{loc}: {message}" if loc else message)
        return JSONResponse({"detail": "; ".join(parts) or "Invalid request."}, status_code=422)

    def _classic_page():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    def _react_page() -> Path | None:
        page = UI_DIR / "index.html"
        if os.environ.get("EASYAGENT_UI", "").strip().lower() == "classic":
            return None
        return page if page.is_file() else None

    @app.get("/")
    def index():
        page = _react_page()
        if page is None:
            return _classic_page()
        return FileResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/classic")
    def classic_index():
        return _classic_page()

    @app.get("/ui/{asset_path:path}")
    def ui_asset(asset_path: str):
        target = contained_file(UI_DIR, asset_path)
        if target is None or not target.is_file():
            raise HTTPException(404, "That file is not there.")
        return FileResponse(target)

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/api/health")
    def health():
        return {"ok": True, "data_dir": str(store.root), "version": __version__}

    @app.get("/api/sandbox")
    def sandbox_status():
        from easyagent.sandbox import public_status

        return public_status(store)

    def _phone_port() -> int:
        server = getattr(app.state, "uvicorn_server", None)
        if server is not None:
            return int(server.config.port)
        return int(os.environ.get("EASYAGENT_PORT", "44721"))

    def _local_only(request: Request) -> None:
        host = request.client.host if request.client else None
        if not is_local(host):
            raise HTTPException(403, "Phone settings stay on this computer.")

    def _apply_phone_listener() -> None:
        server = getattr(app.state, "uvicorn_server", None)
        loop = getattr(app.state, "loop", None)
        if server is None or loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(
            app.state.lan.apply(server, _phone_port(), app.state.phone.enabled),
            loop,
        )
        future.result(timeout=5)

    @app.get("/manifest.webmanifest")
    def manifest(pair: str = ""):
        data = json.loads((STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
        token = (pair or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
            data["start_url"] = f"/?pair={token}"
        return JSONResponse(
            data,
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/phone")
    def phone_status(request: Request):
        _local_only(request)
        return status_payload(app.state.phone, _phone_port(), listening=app.state.lan.listening)

    @app.post("/api/phone")
    def phone_set(body: PhoneIn, request: Request):
        _local_only(request)
        app.state.phone.set_enabled(body.enabled)
        _apply_phone_listener()
        return status_payload(app.state.phone, _phone_port(), listening=app.state.lan.listening)

    @app.get("/api/phone/qr.svg")
    def phone_qr(request: Request):
        _local_only(request)
        url = app.state.phone.pair_url(_phone_port())
        if not url:
            raise HTTPException(404, "Phone access is off, or this computer has no LAN address.")
        return Response(content=qr_svg(url), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.delete("/api/phone/devices/{device_id}")
    def phone_revoke(device_id: str, request: Request):
        _local_only(request)
        try:
            app.state.phone.revoke(device_id)
        except KeyError:
            raise HTTPException(404, "That phone is not paired.") from None
        return status_payload(app.state.phone, _phone_port(), listening=app.state.lan.listening)

    @app.post("/api/phone/firewall")
    def phone_firewall(body: FirewallIn, request: Request):
        _local_only(request)
        return add_firewall_rule(_phone_port(), body.consent)

    @app.get("/api/unread")
    def get_unread():
        return unread_snapshot(store)

    @app.get("/api/endpoints")
    def list_endpoints():
        return [public_endpoint(item) for item in store.list_endpoints()]

    @app.post("/api/endpoints")
    def create_endpoint(body: EndpointIn):
        record = store.add_endpoint(
            name=clean_name(body.name, "Name"),
            base_url=clean_base_url(body.base_url),
            api_key=(body.api_key or "").strip() or None,
            model=clean_model(body.model),
            max_parallel=1 if body.max_parallel is None else clean_max_parallel(body.max_parallel),
        )
        return public_endpoint(record)

    @app.patch("/api/endpoints/{endpoint_id}")
    def patch_endpoint(endpoint_id: str, body: EndpointPatch):
        bots_before = _snapshot_bots(store)
        fields = body.model_fields_set
        record = store.update_endpoint(
            endpoint_id,
            name=clean_name(body.name, "Name") if "name" in fields and body.name is not None else None,
            base_url=clean_base_url(body.base_url) if "base_url" in fields and body.base_url is not None else None,
            api_key=(body.api_key or "").strip() or None,
            api_key_set="api_key" in fields,
            clear_api_key=bool(body.clear_api_key),
            model=clean_model(body.model) if "model" in fields else None,
            model_set="model" in fields,
            max_parallel=clean_max_parallel(body.max_parallel) if "max_parallel" in fields and body.max_parallel is not None else None,
            max_parallel_set="max_parallel" in fields and body.max_parallel is not None,
        )
        if _snapshot_bots(store) != bots_before:
            raise HTTPException(500, "Saving a connection changed a bot or a chat.")
        return public_endpoint(record)

    @app.delete("/api/endpoints/{endpoint_id}")
    def remove_endpoint(endpoint_id: str, body: ConfirmIn):
        before_bots = _snapshot_bots(store)
        store.delete_endpoint(endpoint_id, body.confirm_name)
        after_bots = _snapshot_bots(store)
        if before_bots != after_bots:
            raise HTTPException(500, "Endpoint removal touched bot data. This should not happen.")
        return {"deleted": endpoint_id}

    @app.get("/api/bots")
    def list_bots():
        return [public_bot(store, bot) for bot in store.list_bots()]

    @app.post("/api/bots")
    def create_bot(body: BotIn):
        bot = store.add_bot(
            name=clean_name(body.name, "Name"),
            endpoint_id=body.endpoint_id,
            model=clean_model(body.model),
            context_tokens=clean_context_tokens(body.context_tokens) if "context_tokens" in body.model_fields_set else None,
        )
        return public_bot(store, bot)

    @app.get("/api/bots/{bot_id}")
    def get_bot(bot_id: str):
        return public_bot(store, store.get_bot(bot_id))

    @app.patch("/api/bots/{bot_id}")
    def patch_bot(bot_id: str, body: BotPatch):
        chats_before = _snapshot_chats(store, bot_id)
        fields = body.model_fields_set
        bot = store.update_bot(
            bot_id,
            name=clean_name(body.name, "Name") if "name" in fields and body.name is not None else None,
            endpoint_id=body.endpoint_id if "endpoint_id" in fields else None,
            model=clean_model(body.model) if "model" in fields else None,
            model_set="model" in fields,
            context_tokens=clean_context_tokens(body.context_tokens) if "context_tokens" in fields else None,
            context_tokens_set="context_tokens" in fields,
            face_color=clean_face_color(body.face_color) if "face_color" in fields else None,
            face_color_set="face_color" in fields,
            check_enabled=body.check_enabled if "check_enabled" in fields else None,
            check_enabled_set="check_enabled" in fields,
        )
        chats_after = _snapshot_chats(store, bot_id)
        if chats_before != chats_after:
            raise HTTPException(500, "Saving bot settings changed chat files.")
        return public_bot(store, bot)

    @app.get("/api/bots/{bot_id}/sandbox")
    def get_sandbox(bot_id: str):
        store.get_bot(bot_id)
        from easyagent.sandbox import public_status

        return public_status(store, bot_id)

    @app.post("/api/bots/{bot_id}/sandbox")
    async def post_sandbox(bot_id: str, body: SandboxIn):
        store.get_bot(bot_id)
        from easyagent import contain
        from easyagent.sandbox import public_status

        action = (body.action or "").strip().lower()
        if action == "undo":
            contain.undo(store, bot_id)
            return public_status(store, bot_id)
        if action == "setup":
            if not contain.consented():
                asyncio.create_task(contain.ensure_consent(store, bot_id))
                await asyncio.sleep(0)
            return public_status(store, bot_id)
        raise HTTPException(400, "Say setup or undo.")

    @app.get("/api/bots/{bot_id}/approvals")
    def get_approvals(bot_id: str):
        store.get_bot(bot_id)
        from easyagent.safety import list_pending

        return list_pending(bot_id)

    @app.post("/api/bots/{bot_id}/approvals/{card_id}")
    def post_approval(bot_id: str, card_id: str, body: ApprovalIn):
        store.get_bot(bot_id)
        from easyagent.safety import resolve_card

        if body.decision == "takeover":
            from easyagent.browser import focus
            from easyagent.safety import note_takeover

            card = note_takeover(card_id)
            if card is None or card.bot_id != bot_id:
                raise HTTPException(404, "That approval card is not waiting.")
            focus(store, bot_id)
            return {"ok": True, "decision": "takeover"}
        card = resolve_card(card_id, body.decision)
        if card is None or card.bot_id != bot_id:
            raise HTTPException(404, "That approval card is not waiting.")
        return {"ok": True, "decision": card.decision}

    @app.get("/api/bots/{bot_id}/connectors")
    def get_connectors(bot_id: str):
        store.get_bot(bot_id)
        from easyagent.connectors import list_records, public_record

        return [public_record(row) for row in list_records(store, bot_id)]

    @app.get("/api/bots/{bot_id}/connector-starters")
    def get_connector_starters(bot_id: str):
        store.get_bot(bot_id)
        from easyagent.connectors import starters

        return starters()

    @app.post("/api/bots/{bot_id}/connectors")
    def post_connector(bot_id: str, body: ConnectorIn):
        store.get_bot(bot_id)
        from easyagent.connectors import queue_install
        from easyagent.store import StoreError

        try:
            card = queue_install(
                store,
                bot_id,
                name=body.name,
                transport=body.transport,
                command=body.command,
                url=body.url,
                secrets=body.secrets,
                package=body.package,
                version=body.version,
            )
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        return {"status": "review", "card": card}

    @app.post("/api/bots/{bot_id}/connectors/starter")
    def post_connector_starter(bot_id: str, body: StarterIn):
        store.get_bot(bot_id)
        from easyagent.connectors import queue_starter
        from easyagent.store import StoreError

        try:
            card = queue_starter(store, bot_id, body.starter, body.name, body.path)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        return {"status": "review", "card": card}

    @app.post("/api/bots/{bot_id}/connectors/{connector_id}")
    def post_connector_update(bot_id: str, connector_id: str, body: ConnectorPatch):
        store.get_bot(bot_id)
        from easyagent.connectors import public_record, update_connector
        from easyagent.store import StoreError

        try:
            row = update_connector(store, bot_id, connector_id, enabled=body.enabled, tools=body.tools or None)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        return public_record(row)

    @app.post("/api/bots/{bot_id}/connectors/{connector_id}/refresh")
    def post_connector_refresh(bot_id: str, connector_id: str):
        store.get_bot(bot_id)
        from easyagent.connectors import public_record, refresh
        from easyagent.store import StoreError

        try:
            row = refresh(store, bot_id, connector_id)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        return public_record(row)

    @app.delete("/api/bots/{bot_id}/connectors/{connector_id}")
    def delete_connector_route(bot_id: str, connector_id: str):
        store.get_bot(bot_id)
        from easyagent.connectors import delete_connector
        from easyagent.store import StoreError

        try:
            delete_connector(store, bot_id, connector_id)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        return {"ok": True}

    @app.get("/api/browser/install")
    def get_browser_install():
        from easyagent.browser import install_status

        return install_status()

    @app.post("/api/browser/install")
    def post_browser_install():
        from easyagent.browser import install_browser

        try:
            return install_browser()
        except Exception as exc:
            raise HTTPException(400, str(exc) or "The browser could not be installed.") from exc

    @app.get("/api/search-setup")
    def get_search_setup():
        from easyagent.search import public_settings

        return public_settings(store)

    @app.put("/api/search-setup")
    def put_search_setup(body: SearchSetupIn):
        from easyagent.search import SearchError, save_settings

        try:
            return save_settings(
                store,
                provider=body.provider,
                searxng_url=body.searxng_url,
                brave_key=body.brave_key,
                tavily_key=body.tavily_key,
                clear_brave=body.clear_brave,
                clear_tavily=body.clear_tavily,
            )
        except SearchError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/bots/{bot_id}/browser")
    def get_browser(bot_id: str):
        store.get_bot(bot_id)
        from easyagent.browser import snapshot

        return snapshot(store, bot_id)

    @app.get("/api/bots/{bot_id}/browser/shot")
    def get_browser_shot(bot_id: str):
        store.get_bot(bot_id)
        from easyagent.browser import shot_path

        path = shot_path(store, bot_id)
        if not path.is_file():
            raise HTTPException(404, "No browser page is open.")
        return FileResponse(path, media_type="image/png")

    @app.post("/api/bots/{bot_id}/browser/stop")
    def post_browser_stop(bot_id: str):
        store.get_bot(bot_id)
        from easyagent.browser import cancel as cancel_browser
        from easyagent.browser import snapshot

        cancel_browser(store, bot_id)
        return snapshot(store, bot_id)

    @app.post("/api/bots/{bot_id}/browser")
    def post_browser(bot_id: str, body: BrowserIn):
        from easyagent.browser import cancel as cancel_browser
        from easyagent.browser import clean_hosts

        bot = store.save_browser_settings(
            bot_id,
            headless=body.headless,
            allow=clean_hosts(body.allow),
            deny=clean_hosts(body.deny),
        )
        cancel_browser(store, bot_id)
        return public_bot(store, bot)

    @app.get("/api/bots/{bot_id}/audit")
    def get_audit(bot_id: str):
        store.get_bot(bot_id)
        from easyagent.safety import read_audit

        return read_audit(store, bot_id)

    @app.post("/api/bots/{bot_id}/safety")
    def post_safety(bot_id: str, body: SafetyIn):
        from easyagent.safety import set_mode
        from easyagent.store import StoreError

        try:
            bot = set_mode(store, bot_id, body.mode, body.confirm_name, body.unlocks)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        return public_bot(store, bot)

    @app.get("/api/bots/{bot_id}/honesty")
    def get_honesty(bot_id: str):
        store.get_bot(bot_id)
        from easyagent.honesty import load_settings

        return load_settings(store, bot_id)

    @app.post("/api/bots/{bot_id}/honesty")
    def post_honesty(bot_id: str, body: HonestyIn):
        from easyagent.honesty import save_settings

        try:
            return save_settings(store, bot_id, body.model_dump(exclude_unset=True))
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @app.get("/api/bots/{bot_id}/trash")
    def get_trash(bot_id: str):
        store.get_bot(bot_id)
        from easyagent.safety import list_trash

        return list_trash(store)

    @app.post("/api/bots/{bot_id}/trash/{item_id}/restore")
    def post_restore(bot_id: str, item_id: str):
        store.get_bot(bot_id)
        from easyagent.safety import restore_trash

        try:
            text = restore_trash(store, item_id)
        except FileNotFoundError:
            raise HTTPException(404, "That trash item is not there.")
        return {"ok": True, "text": text}

    @app.delete("/api/bots/{bot_id}")
    def remove_bot(bot_id: str, body: ConfirmIn):
        others = {bot["id"]: _snapshot_chats(store, bot["id"]) for bot in store.list_bots() if bot["id"] != _safe_id(bot_id)}
        endpoints_before = store.endpoints_path.read_bytes()
        skills_before = _snapshot_skills(store)
        direction_before = store.direction_path.read_bytes() if store.direction_path.exists() else b""
        deleted = store.delete_bot(bot_id, body.confirm_name)
        for other_id, snapshot in others.items():
            if _snapshot_chats(store, other_id) != snapshot:
                raise HTTPException(500, "Removing a bot changed another bot's chats.")
        if store.endpoints_path.read_bytes() != endpoints_before:
            raise HTTPException(500, "Removing a bot changed endpoints.")
        if _snapshot_skills(store) != skills_before or (
            store.direction_path.read_bytes() if store.direction_path.exists() else b""
        ) != direction_before:
            raise HTTPException(500, "Removing a bot changed skills or direction.")
        return {"deleted": deleted["id"], "name": deleted["name"]}

    @app.get("/api/bots/{bot_id}/schedules")
    def list_schedules(bot_id: str):
        return [_public_schedule(item) for item in store.list_schedules(bot_id)]

    @app.post("/api/bots/{bot_id}/schedules")
    def create_schedule(bot_id: str, body: ScheduleIn):
        chats_before = _snapshot_chats(store, bot_id)
        rooms_before = _snapshot_rooms(store)
        schedule = store.add_schedule(bot_id, _schedule_record(body))
        if not _legacy_schedule(body):
            store.note_first_routine()
        if _snapshot_chats(store, bot_id) != chats_before or _snapshot_rooms(store) != rooms_before:
            raise HTTPException(500, "Saving a schedule changed chats or rooms.")
        return _public_schedule(schedule)

    @app.get("/api/bots/{bot_id}/schedules/trash")
    def list_schedule_trash(bot_id: str):
        return [_public_schedule(item) for item in store.list_routine_trash(bot_id)]

    @app.post("/api/bots/{bot_id}/schedules/{schedule_id}/restore")
    def restore_schedule(bot_id: str, schedule_id: str):
        restored = store.restore_schedule(bot_id, schedule_id)
        return _public_schedule(restored)

    @app.post("/api/bots/{bot_id}/schedules/{schedule_id}/run")
    async def run_schedule(bot_id: str, schedule_id: str):
        bot = store.get_bot(bot_id)
        match = next((item for item in store.list_schedules(bot_id) if item.get("id") == _safe_id(schedule_id)), None)
        if match is None:
            raise HTTPException(404, "Schedule not found.")
        return await run_routine_now(store, bot, match)

    @app.get("/api/bots/{bot_id}/routines/active")
    def routines_active(bot_id: str):
        store.get_bot(bot_id)
        return {"running": active_routines(bot_id)}

    @app.get("/api/routine-templates")
    def routine_templates():
        return template_records()

    @app.post("/api/routines/pause-all")
    def pause_all_routines():
        return {"paused": pause_all(store)}

    @app.post("/api/bots/{bot_id}/schedules/{schedule_id}/pause")
    def pause_schedule(bot_id: str, schedule_id: str, body: SchedulePauseIn):
        chats_before = _snapshot_chats(store, bot_id)
        rooms_before = _snapshot_rooms(store)
        schedules = store.list_schedules(bot_id)
        match = next((item for item in schedules if item.get("id") == _safe_id(schedule_id)), None)
        if match is None:
            raise HTTPException(404, "Schedule not found.")
        match["paused"] = bool(body.paused)
        store.save_schedules(bot_id, schedules)
        if _snapshot_chats(store, bot_id) != chats_before or _snapshot_rooms(store) != rooms_before:
            raise HTTPException(500, "Pausing a schedule changed chats or rooms.")
        return _public_schedule(match)

    @app.delete("/api/bots/{bot_id}/schedules/{schedule_id}")
    def delete_schedule(bot_id: str, schedule_id: str):
        chats_before = _snapshot_chats(store, bot_id)
        bot_before = (store.bots_dir / _safe_id(bot_id) / "bot.json").read_bytes()
        rooms_before = _snapshot_rooms(store)
        deleted = store.delete_schedule(bot_id, schedule_id)
        if _snapshot_chats(store, bot_id) != chats_before:
            raise HTTPException(500, "Deleting a schedule changed this bot's chats.")
        if (store.bots_dir / _safe_id(bot_id) / "bot.json").read_bytes() != bot_before:
            raise HTTPException(500, "Deleting a schedule changed the bot.")
        if _snapshot_rooms(store) != rooms_before:
            raise HTTPException(500, "Deleting a schedule changed a room.")
        return {"deleted": deleted["id"]}

    @app.get("/api/bots/{bot_id}/jobs")
    def list_jobs(bot_id: str):
        return store.list_jobs(bot_id)

    @app.get("/api/bots/{bot_id}/chats")
    def list_chats(bot_id: str):
        return store.list_chats(bot_id)

    @app.get("/api/bots/{bot_id}/ongoing")
    def get_ongoing(bot_id: str, window: int | None = 80):
        """The bot's one conversation. Older transcripts stay on disk."""
        return _public_chat(store, store.ongoing_chat(bot_id), window=window)

    @app.post("/api/bots/{bot_id}/chats")
    def create_chat(bot_id: str):
        return _public_chat(store, store.get_chat(bot_id, store.create_chat(bot_id)["id"]))

    @app.delete("/api/bots/{bot_id}/chats/{chat_id}")
    def remove_chat(bot_id: str, chat_id: str):
        """The person deletes one transcript. The model has no such action."""
        safe_bot = _safe_id(bot_id)
        safe_chat = _safe_id(chat_id)
        bot_file = store.bots_dir / safe_bot / "bot.json"
        if not bot_file.is_file():
            raise HTTPException(404, "Bot not found.")
        bot_before = bot_file.read_bytes()
        chats_before = _snapshot_chats(store, bot_id)
        target = f"{safe_chat}.json"
        if target not in chats_before:
            store.get_chat(bot_id, chat_id)
        kept_before = {name: blob for name, blob in chats_before.items() if name != target}
        other_bots = {
            bot["id"]: _snapshot_chats(store, bot["id"])
            for bot in store.list_bots()
            if bot["id"] != safe_bot
        }
        rooms_before = _snapshot_rooms(store)
        endpoints_before = store.endpoints_path.read_bytes()
        skills_before = _snapshot_skills(store)
        direction_before = store.direction_path.read_bytes() if store.direction_path.exists() else b""
        deleted = store.delete_chat(bot_id, chat_id)
        chats_after = _snapshot_chats(store, bot_id)
        if target in chats_after:
            raise HTTPException(500, "Deleting a chat left the transcript on disk.")
        if chats_after != kept_before:
            raise HTTPException(500, "Deleting a chat changed another chat.")
        if bot_file.read_bytes() != bot_before:
            raise HTTPException(500, "Deleting a chat changed the bot.")
        for other_id, snapshot in other_bots.items():
            if _snapshot_chats(store, other_id) != snapshot:
                raise HTTPException(500, "Deleting a chat changed another bot's chats.")
        if _snapshot_rooms(store) != rooms_before:
            raise HTTPException(500, "Deleting a chat changed a room.")
        if store.endpoints_path.read_bytes() != endpoints_before:
            raise HTTPException(500, "Deleting a chat changed a connection.")
        if _snapshot_skills(store) != skills_before or (
            store.direction_path.read_bytes() if store.direction_path.exists() else b""
        ) != direction_before:
            raise HTTPException(500, "Deleting a chat changed skills or direction.")
        return {"deleted": deleted["id"]}

    @app.get("/api/bots/{bot_id}/chats/{chat_id}")
    def get_chat(bot_id: str, chat_id: str, window: int | None = None):
        return _public_chat(store, store.get_chat(bot_id, chat_id), window=window)

    @app.get("/api/bots/{bot_id}/chats/{chat_id}/messages")
    def chat_messages(bot_id: str, chat_id: str, before: int = 0, limit: int = 80):
        """Older messages, read only. `before` is the first index the page already has."""
        chat = store.get_chat(bot_id, chat_id)
        messages = list(chat.get("messages") or [])
        end = before
        if end < 0:
            end = 0
        if end > len(messages):
            end = len(messages)
        size = limit
        if size < 1:
            size = 1
        if size > 200:
            size = 200
        start = max(0, end - size)
        return {"messages": messages[start:end], "start": start, "end": end, "total": len(messages)}

    @app.post("/api/bots/{bot_id}/chats/{chat_id}/fresh")
    def fresh_chat(bot_id: str, chat_id: str):
        """Clear what the model sees. The transcript is not deleted."""
        return _public_chat(store, store.start_fresh(bot_id, chat_id))

    @app.post("/api/bots/{bot_id}/chats/{chat_id}/read")
    def read_chat(bot_id: str, chat_id: str, body: ReadIn | None = None):
        through = None if body is None else body.through
        return mark_chat_read(store, bot_id, chat_id, through)

    @app.post("/api/bots/{bot_id}/chats/{chat_id}/files")
    async def upload_chat_file(bot_id: str, chat_id: str, file: UploadFile = File(...)):
        store.get_chat(bot_id, chat_id)
        data = await file.read()
        if contains_secret(store, data):
            raise HTTPException(400, "That file was not kept.")
        name = _safe_filename(file.filename or "file")
        media = _media_type(file.content_type or "", data)
        try:
            meta = store.save_chat_file(bot_id, chat_id, name=name, media_type=media, data=data)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        return {"id": meta["id"], "name": meta["name"], "media_type": meta["media_type"]}

    @app.get("/api/bots/{bot_id}/chats/{chat_id}/files/{file_id}")
    def download_chat_file(bot_id: str, chat_id: str, file_id: str):
        try:
            meta, data = store.read_chat_file(bot_id, chat_id, file_id)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        return Response(content=data, media_type=meta.get("media_type") or "application/octet-stream")

    @app.post("/api/bots/{bot_id}/chats/{chat_id}/messages")
    async def post_message(bot_id: str, chat_id: str, body: MessageIn, request: Request):
        content = (body.content or "").strip()
        attachment = _attachment_for_message(store, bot_id, chat_id, body.attachment_id)
        if not content and attachment is None:
            raise HTTPException(400, "Write a message first.")
        if len(content) > MAX_STORED_MESSAGE_CHARS:
            raise HTTPException(400, f"Message is too long ({MAX_STORED_MESSAGE_CHARS} characters max).")
        _require_endpoint(store, bot_id)
        store.get_chat(bot_id, chat_id)
        turn_mod.interrupt(store, chat_id, "a new message was sent in this chat")
        if _wants_stream(request):
            return StreamingResponse(
                _stream_new_message(store, bot_id, chat_id, content, attachment),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        async with chat_lock(store, chat_id):
            turn_mod.bind(store, chat_id, bot_id=bot_id)
            _save_user_turn(store, bot_id, chat_id, content, attachment)
            return await _reply_json(store, bot_id, chat_id)

    @app.post("/api/bots/{bot_id}/chats/{chat_id}/retry")
    async def retry_message(bot_id: str, chat_id: str, request: Request):
        _require_endpoint(store, bot_id)
        chat = store.get_chat(bot_id, chat_id)
        if not _can_retry(chat.get("messages") or []):
            raise HTTPException(400, "Nothing to retry. The last stored turn is not waiting on a reply.")
        turn_mod.interrupt(store, chat_id, "the reply is being tried again")
        if _wants_stream(request):
            return StreamingResponse(
                _stream_retry(store, bot_id, chat_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        async with chat_lock(store, chat_id):
            turn_mod.bind(store, chat_id, bot_id=bot_id)
            before = len(store.get_chat(bot_id, chat_id).get("messages") or [])
            result = await _reply_json(store, bot_id, chat_id)
            if len(store.get_chat(bot_id, chat_id).get("messages") or []) < before:
                raise HTTPException(500, "Retry shortened the transcript.")
            return result

    @app.post("/api/bots/{bot_id}/chats/{chat_id}/messages/{message_id}/reaction")
    async def react_to_message(bot_id: str, chat_id: str, message_id: str, body: ReactionIn):
        async with chat_lock(store, chat_id):
            current = store.get_chat(bot_id, chat_id)
            texts = [item.get("content") for item in current.get("messages") or []]
            try:
                chat = store.toggle_reaction(bot_id, chat_id, message_id, body.emoji)
            except StoreError as exc:
                raise HTTPException(exc.status, exc.message) from exc
            if [item.get("content") for item in chat.get("messages") or []] != texts:
                raise HTTPException(500, "A reaction changed the message text.")
            if len(chat.get("messages") or []) != len(texts):
                raise HTTPException(500, "A reaction changed the transcript.")
            _note_thumb(store, bot_id, chat.get("messages") or [], message_id)
            await _propose_thumb(store, bot_id, chat.get("messages") or [], message_id)
            landed = _reaction_on_latest(current.get("messages") or [], chat.get("messages") or [], message_id)
            if landed is not None:
                await _ack_chat_reaction(store, bot_id, chat_id, landed)
                chat = store.get_chat(bot_id, chat_id)
            return _public_chat(store, chat)

    @app.post("/api/bots/{bot_id}/chats/{chat_id}/stop")
    async def stop_chat(bot_id: str, chat_id: str):
        """Stop this chat's run. Another chat is not cancelled."""
        store.get_chat(bot_id, chat_id)
        worker = turn_mod.current_worker(store, chat_id)
        turn_mod.interrupt(store, chat_id, "you pressed Stop")
        if worker is not None and not worker.done():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(worker), timeout=8)
        return _public_chat(store, store.get_chat(bot_id, chat_id))

    @app.post("/api/bots/{bot_id}/chats/{chat_id}/ask")
    async def ask_bot(bot_id: str, chat_id: str, body: AskIn):
        """One existing bot does one task. No new bot is created."""
        task = (body.task or "").strip()
        if not task:
            raise HTTPException(400, "Write the task first.")
        if len(task) > 8000:
            raise HTTPException(400, "A task is one request, at most 8000 characters.")
        async with chat_lock(store, chat_id):
            return await _ask_bot_id(store, bot_id, chat_id, body.bot_id, task)

    @app.get("/api/rooms")
    def list_rooms():
        return store.list_rooms()

    @app.post("/api/rooms")
    def create_room(body: RoomIn):
        room = store.create_room(name=clean_name(body.name, "Name"))
        return _public_room(store, room)

    @app.get("/api/rooms/{room_id}")
    def get_room(room_id: str):
        return _public_room(store, store.get_room(room_id))

    @app.post("/api/rooms/{room_id}/read")
    def read_room(room_id: str, body: ReadIn | None = None):
        through = None if body is None else body.through
        return mark_room_read(store, room_id, through)

    @app.post("/api/rooms/{room_id}/bots")
    def add_room_bot(room_id: str, body: RoomBotIn):
        bots_before = _snapshot_bots(store)
        room = store.add_room_bot(room_id, body.bot_id)
        if _snapshot_bots(store) != bots_before:
            raise HTTPException(500, "Adding a bot to a room changed that bot's private data.")
        return _public_room(store, room)

    @app.delete("/api/rooms/{room_id}/bots/{bot_id}")
    def remove_room_bot(room_id: str, bot_id: str):
        before = store.get_room(room_id)
        messages = list(before.get("messages") or [])
        bots_before = _snapshot_bots(store)
        room = store.remove_room_bot(room_id, bot_id)
        if room.get("messages") != messages:
            raise HTTPException(500, "Removing a bot from a room changed the transcript.")
        if _snapshot_bots(store) != bots_before:
            raise HTTPException(500, "Removing a bot from a room changed that bot.")
        return _public_room(store, room)

    @app.post("/api/rooms/{room_id}/messages")
    async def post_room_message(room_id: str, body: MessageIn):
        content = (body.content or "").strip()
        if not content:
            raise HTTPException(400, "Write a message first.")
        if len(content) > MAX_STORED_MESSAGE_CHARS:
            raise HTTPException(400, f"Message is too long ({MAX_STORED_MESSAGE_CHARS} characters max).")
        async with chat_lock(store, room_id):
            room = store.get_room(room_id)
            member_ids = list(room.get("bot_ids") or [])
            if not member_ids:
                raise HTTPException(400, "Add a bot to this room first. Nothing was written.")
            bots_before = _snapshot_bots(store)
            room["messages"].append(
                {
                    "id": new_id(),
                    "role": "user",
                    "speaker": "user",
                    "speaker_name": "You",
                    "content": content,
                    "created_at": now_iso(),
                }
            )
            _persist_room(room)
            room["updated_at"] = now_iso()
            store.save_room(room)
            # Every bot answers this same transcript. A same-turn reply is not
            # fed to the next bot, so one failure cannot change what the others see.
            snapshot = store.get_room(room_id)
            for bot_id in member_ids:
                visible, saved, error, choices, reacted = await _complete_for_room_bot(store, snapshot, bot_id)
                if not error and not (visible or "").strip() and not choices and reacted:
                    continue
                room = store.get_room(room_id)
                try:
                    speaker_name = store.get_bot(bot_id)["name"]
                except StoreError:
                    speaker_name = "Missing bot"
                message = {
                    "id": new_id(),
                    "role": "assistant",
                    "speaker": bot_id,
                    "speaker_name": speaker_name,
                    "content": error or visible,
                    "created_at": now_iso(),
                }
                if error:
                    message["error"] = True
                if choices and not error:
                    message["choices"] = choices
                if saved:
                    message["skills_saved"] = saved
                room["messages"].append(message)
                _persist_room(room)
                room["updated_at"] = now_iso()
                store.save_room(room)
            if _snapshot_bots(store) != bots_before:
                raise HTTPException(500, "A room turn changed a bot's private chats.")
            return _public_room(store, store.get_room(room_id))

    @app.post("/api/rooms/{room_id}/messages/{message_id}/reaction")
    async def react_to_room_message(room_id: str, message_id: str, body: ReactionIn):
        async with chat_lock(store, room_id):
            current = store.get_room(room_id)
            texts = [item.get("content") for item in current.get("messages") or []]
            bots_before = _snapshot_bots(store)
            try:
                room = store.toggle_room_reaction(room_id, message_id, body.emoji)
            except StoreError as exc:
                raise HTTPException(exc.status, exc.message) from exc
            if [item.get("content") for item in room.get("messages") or []] != texts:
                raise HTTPException(500, "A reaction changed the message text.")
            if len(room.get("messages") or []) != len(texts):
                raise HTTPException(500, "A reaction changed the transcript.")
            if _snapshot_bots(store) != bots_before:
                raise HTTPException(500, "A reaction changed a bot's private chats.")
            speaker = ""
            for item in room.get("messages") or []:
                if item.get("id") == message_id:
                    speaker = str(item.get("speaker") or "")
                    break
            if speaker and speaker != "user":
                _note_thumb(store, speaker, room.get("messages") or [], message_id, quarantine=True)
                await _propose_thumb(
                    store, speaker, room.get("messages") or [], message_id, quarantine=True
                )
            landed = _reaction_on_latest(current.get("messages") or [], room.get("messages") or [], message_id)
            if landed is not None:
                await _ack_room_reaction(store, room_id, landed)
                room = store.get_room(room_id)
                if _snapshot_bots(store) != bots_before:
                    raise HTTPException(500, "A reaction changed a bot's private chats.")
            return _public_room(store, room)

    @app.get("/api/computers")
    def list_computers():
        return [public_computer(item) for item in store.list_computers()]

    @app.post("/api/computers")
    def create_computer(body: ComputerIn):
        bots_before = _snapshot_bots(store)
        record = store.add_computer(**_computer_record(body))
        if _snapshot_bots(store) != bots_before:
            raise HTTPException(500, "Saving a computer changed a bot or a chat.")
        return public_computer(record)

    @app.patch("/api/computers/{computer_id}")
    def patch_computer(computer_id: str, body: ComputerIn):
        bots_before = _snapshot_bots(store)
        current = next((item for item in store.list_computers() if item.get("id") == computer_id), None)
        if current is None:
            raise StoreError("Computer not found.", 404)
        cleaned = _computer_record(body, allow_blank=True, existing=current)
        record = store.update_computer(computer_id, **cleaned)
        if _snapshot_bots(store) != bots_before:
            raise HTTPException(500, "Saving a computer changed a bot or a chat.")
        return public_computer(record)

    @app.delete("/api/computers/{computer_id}")
    def remove_computer(computer_id: str, body: ConfirmIn):
        bots_before = _snapshot_bots(store)
        store.delete_computer(computer_id, body.confirm_name)
        if _snapshot_bots(store) != bots_before:
            raise HTTPException(500, "Removing a computer changed a bot or a chat.")
        return {"deleted": computer_id}

    @app.get("/api/bots/{bot_id}/memory")
    def list_memory(bot_id: str):
        return store.list_memory(bot_id)

    @app.get("/api/bots/{bot_id}/memory/index")
    def memory_index(bot_id: str):
        return {"topics": store.memory_index(bot_id)}

    @app.get("/api/bots/{bot_id}/memory/topics/{topic}")
    def memory_topic(bot_id: str, topic: str):
        return store.read_topic(bot_id, topic)

    @app.post("/api/bots/{bot_id}/memory")
    def create_memory(bot_id: str, body: MemoryIn):
        chats_before = _snapshot_chats(store, bot_id)
        skills_before = _snapshot_skills(store)
        text = _fact(store, body.text)
        record = store.add_memory(bot_id, text, topic=body.topic, create=bool(body.topic))
        if _snapshot_chats(store, bot_id) != chats_before:
            raise HTTPException(500, "Saving a memory line changed a chat.")
        if _snapshot_skills(store) != skills_before:
            raise HTTPException(500, "Saving a memory line changed a skill.")
        return record

    @app.patch("/api/bots/{bot_id}/memory/{memory_id}")
    def patch_memory(bot_id: str, memory_id: str, body: MemoryIn):
        chats_before = _snapshot_chats(store, bot_id)
        skills_before = _snapshot_skills(store)
        before = [(item.get("id"), item.get("text")) for item in store.list_memory(bot_id) if item.get("id") != memory_id]
        text = _fact(store, body.text)
        record = store.update_memory(bot_id, memory_id, text)
        after = [(item.get("id"), item.get("text")) for item in store.list_memory(bot_id) if item.get("id") != memory_id]
        if after != before:
            raise HTTPException(500, "Editing one memory line changed another.")
        if _snapshot_chats(store, bot_id) != chats_before:
            raise HTTPException(500, "Editing a memory line changed a chat.")
        if _snapshot_skills(store) != skills_before:
            raise HTTPException(500, "Editing a memory line changed a skill.")
        return record

    @app.delete("/api/bots/{bot_id}/memory/{memory_id}")
    def remove_memory(bot_id: str, memory_id: str):
        chats_before = _snapshot_chats(store, bot_id)
        skills_before = _snapshot_skills(store)
        store.delete_memory(bot_id, memory_id)
        if _snapshot_chats(store, bot_id) != chats_before:
            raise HTTPException(500, "Dropping a memory line changed a chat.")
        if _snapshot_skills(store) != skills_before:
            raise HTTPException(500, "Dropping a memory line changed a skill.")
        return {"deleted": memory_id}

    def _own_project(bot_id: str, project_id: str) -> dict:
        try:
            project = store.get_project(project_id)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        if project.get("kind") != "bot" or project.get("bot_id") != _safe_id(bot_id):
            raise HTTPException(404, "That project is not there.")
        return project

    def _group_project(project_id: str) -> dict:
        try:
            project = store.get_project(project_id)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        if project.get("kind") != "group":
            raise HTTPException(404, "That project is not there.")
        return project

    @app.get("/api/bots/{bot_id}/projects")
    def list_bot_projects(bot_id: str):
        store.get_bot(bot_id)
        return [
            _public_project(store, item)
            for item in store.projects_for_bot(bot_id)
            if item.get("kind") == "bot"
        ]

    @app.post("/api/bots/{bot_id}/projects")
    def create_bot_project(bot_id: str, body: ProjectIn):
        chats_before = _snapshot_bots(store)
        rooms_before = _snapshot_room_files(store)
        try:
            project = store.create_bot_project(bot_id, body.name)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        _guard_transcripts(store, chats_before, rooms_before)
        return _public_project(store, project)

    @app.get("/api/bots/{bot_id}/projects/{project_id}")
    def get_bot_project(bot_id: str, project_id: str):
        return _public_project(store, _own_project(bot_id, project_id))

    @app.post("/api/bots/{bot_id}/projects/{project_id}/files")
    async def upload_bot_project_files(bot_id: str, project_id: str, files: list[UploadFile] = File(...)):
        _own_project(bot_id, project_id)
        prepared = await _prepared_uploads(store, files)
        return _save_uploads(store, project_id, prepared)

    @app.delete("/api/bots/{bot_id}/projects/{project_id}/files/{file_id}")
    def remove_bot_project_file(bot_id: str, project_id: str, file_id: str):
        project = _own_project(bot_id, project_id)
        chats_before = _snapshot_bots(store)
        rooms_before = _snapshot_room_files(store)
        try:
            store.delete_project_file(project["id"], file_id)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        _guard_transcripts(store, chats_before, rooms_before)
        return _public_project(store, store.get_project(project["id"]))

    @app.delete("/api/bots/{bot_id}/projects/{project_id}")
    def remove_bot_project(bot_id: str, project_id: str, body: ConfirmIn):
        project = _own_project(bot_id, project_id)
        chats_before = _snapshot_bots(store)
        rooms_before = _snapshot_room_files(store)
        try:
            store.delete_project(project["id"], body.confirm_name)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        _guard_transcripts(store, chats_before, rooms_before)
        return {"deleted": project["id"], "name": project["name"]}

    @app.get("/api/projects")
    def list_group_projects():
        return [_public_project(store, item) for item in store.list_project_records() if item.get("kind") == "group"]

    @app.post("/api/projects")
    def create_group_project(body: ProjectIn):
        chats_before = _snapshot_bots(store)
        rooms_before = _snapshot_room_files(store)
        try:
            project = store.create_group_project(body.name)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        _guard_transcripts(store, chats_before, rooms_before)
        return _public_project(store, project)

    @app.get("/api/projects/{project_id}")
    def get_group_project(project_id: str):
        return _public_project(store, _group_project(project_id))

    @app.post("/api/projects/{project_id}/bots")
    def add_group_project_bot(project_id: str, body: RoomBotIn):
        _group_project(project_id)
        chats_before = _snapshot_bots(store)
        rooms_before = _snapshot_room_files(store)
        try:
            project = store.add_project_bot(project_id, body.bot_id)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        _guard_transcripts(store, chats_before, rooms_before)
        return _public_project(store, project)

    @app.delete("/api/projects/{project_id}/bots/{member_id}")
    def remove_group_project_bot(project_id: str, member_id: str):
        _group_project(project_id)
        chats_before = _snapshot_bots(store)
        rooms_before = _snapshot_room_files(store)
        try:
            project = store.remove_project_bot(project_id, member_id)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        _guard_transcripts(store, chats_before, rooms_before)
        return _public_project(store, project)

    @app.post("/api/projects/{project_id}/files")
    async def upload_group_project_files(project_id: str, files: list[UploadFile] = File(...)):
        _group_project(project_id)
        prepared = await _prepared_uploads(store, files)
        return _save_uploads(store, project_id, prepared)

    @app.delete("/api/projects/{project_id}/files/{file_id}")
    def remove_group_project_file(project_id: str, file_id: str):
        project = _group_project(project_id)
        chats_before = _snapshot_bots(store)
        rooms_before = _snapshot_room_files(store)
        try:
            store.delete_project_file(project["id"], file_id)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        _guard_transcripts(store, chats_before, rooms_before)
        return _public_project(store, store.get_project(project["id"]))

    @app.delete("/api/projects/{project_id}")
    def remove_group_project(project_id: str, body: ConfirmIn):
        project = _group_project(project_id)
        chats_before = _snapshot_bots(store)
        rooms_before = _snapshot_room_files(store)
        try:
            store.delete_project(project["id"], body.confirm_name)
        except StoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        _guard_transcripts(store, chats_before, rooms_before)
        return {"deleted": project["id"], "name": project["name"]}

    @app.get("/api/watches")
    def list_watches():
        return store.list_watches()

    @app.post("/api/watches")
    def create_watch(body: WatchIn):
        chats_before = _snapshot_bots(store)
        record = store.arm_watch(body.kind)
        if _snapshot_bots(store) != chats_before:
            raise HTTPException(500, "Arming a notice changed a chat.")
        return record

    @app.get("/api/proposals")
    def list_proposals():
        return [
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "name": item.get("name") or "",
                "text": item.get("text") or "",
                "quote": item.get("quote") or "",
                "counterexample": item.get("counterexample") or "",
                "installed": False,
            }
            for item in store.list_proposals()
        ]

    @app.post("/api/night")
    async def night_pass():
        chats_before = _snapshot_bots(store)
        skills_before = _snapshot_skills(store)
        memory_before = _snapshot_memory(store)
        result = await run_night(store)
        if _snapshot_bots(store) != chats_before:
            raise HTTPException(500, "The night pass changed a chat.")
        if _snapshot_skills(store) != skills_before:
            raise HTTPException(500, "The night pass changed a skill.")
        if _snapshot_memory(store) != memory_before:
            raise HTTPException(500, "The night pass changed a memory line.")
        return result

    @app.get("/api/skills")
    def list_skills():
        return [
            {"name": skill["name"], "description": skill.get("description") or "", "body": skill.get("body") or ""}
            for skill in store.list_skills()
        ]

    @app.post("/api/skills")
    def create_skill(body: SkillIn):
        bots_before = _snapshot_bots(store)
        parsed = {
            "name": body.name,
            "description": body.description or "",
            "body": body.body or "",
        }
        slug = slugify(body.name)
        if not slug or slug in RESERVED_SLUGS:
            raise StoreError("Type a short name for the skill, like desk-notes. Chats were not changed.", 400)
        record = _save_user_skill(store, parsed)
        if _snapshot_bots(store) != bots_before:
            raise HTTPException(500, "Saving a skill changed a bot or a chat.")
        return {"name": record["name"], "description": record.get("description") or "", "body": record.get("body") or ""}

    @app.get("/api/bots/{bot_id}/learning")
    def get_learning(bot_id: str):
        return learning_panel(store, store.get_bot(bot_id))

    @app.post("/api/bots/{bot_id}/learning/pause")
    def learning_pause(bot_id: str, body: LearnFlag):
        set_paused(store, bot_id, body.on)
        return learning_panel(store, store.get_bot(bot_id))

    @app.post("/api/bots/{bot_id}/learning/manual")
    def learning_manual(bot_id: str, body: LearnFlag):
        set_manual(store, bot_id, body.on)
        return learning_panel(store, store.get_bot(bot_id))

    @app.post("/api/bots/{bot_id}/learning/rollback")
    def learning_rollback(bot_id: str):
        store.get_bot(bot_id)
        rollback_latest(store, bot_id)
        return learning_panel(store, store.get_bot(bot_id))

    @app.post("/api/bots/{bot_id}/learning/rollback/{ledger_id}")
    def learning_rollback_one(bot_id: str, ledger_id: str):
        store.get_bot(bot_id)
        rollback_for_bot(store, bot_id, ledger_id)
        return learning_panel(store, store.get_bot(bot_id))

    @app.put("/api/bots/{bot_id}/notes/retention")
    def put_note_retention(bot_id: str, body: RetentionIn):
        from easyagent.journal import save_retention

        save_retention(store, bot_id, body.model_dump())
        return learning_panel(store, store.get_bot(bot_id))

    @app.post("/api/bots/{bot_id}/notes/habits/{entry_id}/approve")
    def approve_note_habit(bot_id: str, entry_id: str):
        from easyagent.journal import approve_habit

        chats_before = _snapshot_chats(store, bot_id)
        approve_habit(store, bot_id, entry_id)
        if _snapshot_chats(store, bot_id) != chats_before:
            raise HTTPException(500, "Approving a habit changed a chat.")
        return learning_panel(store, store.get_bot(bot_id))

    @app.post("/api/bots/{bot_id}/learning/approve/{candidate_id}")
    def learning_approve(bot_id: str, candidate_id: str):
        bot = store.get_bot(bot_id)
        approve_candidate(store, bot, candidate_id)
        return learning_panel(store, store.get_bot(bot_id))

    @app.post("/api/bots/{bot_id}/learning/reject/{candidate_id}")
    def learning_reject(bot_id: str, candidate_id: str):
        bot = store.get_bot(bot_id)
        reject_candidate(store, bot, candidate_id)
        return learning_panel(store, store.get_bot(bot_id))

    @app.post("/api/bots/{bot_id}/learning/stop")
    def learning_stop(bot_id: str):
        store.get_bot(bot_id)
        request_learn_stop()
        return {"stopped": True}

    @app.post("/api/bots/{bot_id}/learning/sleep")
    async def learning_sleep(bot_id: str):
        bot = store.get_bot(bot_id)
        if chats_active(store):
            raise HTTPException(409, "A chat is running.")
        return await sleep_once(store, mock=False, runs=3, bot_id=bot["id"])

    @app.get("/api/direction")
    def get_direction():
        return {"text": store.read_direction()}

    @app.put("/api/direction")
    def put_direction(body: DirectionIn):
        bots_before = _snapshot_bots(store)
        text = store.write_direction(body.text)
        if _snapshot_bots(store) != bots_before:
            raise HTTPException(500, "Saving direction changed bot data.")
        return {"text": text}

    @app.get("/static/{asset_path:path}")
    def static_asset(asset_path: str):
        # Starlette StaticFiles compares paths with commonpath. On Windows that
        # check 404s a file that is on disk when the real path and the directory
        # path differ by case. Serve the file when it is inside this folder.
        file = contained_file(STATIC_DIR, asset_path)
        if file is None:
            raise HTTPException(404, "Not found.")
        media = _STATIC_MEDIA.get(file.suffix.lower(), "application/octet-stream")
        return FileResponse(file, media_type=media)

    return app


def _safe_id(value: str) -> str:
    return (value or "").strip().lower()


def _snapshot_chats(store: Store, bot_id: str) -> dict[str, bytes]:
    directory = store.bots_dir / _safe_id(bot_id) / "chats"
    if not directory.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*.json")) if path.is_file()}


def _snapshot_bots(store: Store) -> dict[str, bytes]:
    if not store.bots_dir.exists():
        return {}
    found = {}
    for path in sorted(store.bots_dir.glob("*/bot.json")):
        if path.is_file():
            found[str(path.relative_to(store.root))] = path.read_bytes()
    for path in sorted(store.bots_dir.glob("*/chats/*.json")):
        if path.is_file():
            found[str(path.relative_to(store.root))] = path.read_bytes()
    return found


def _snapshot_skills(store: Store) -> dict[str, bytes]:
    if not store.skills_dir.exists():
        return {}
    found = {}
    for path in sorted(store.skills_dir.glob("*.md")):
        if path.is_file() and not path.is_symlink():
            found[path.name] = path.read_bytes()
    return found


def _snapshot_memory(store: Store) -> dict[str, bytes]:
    if not store.bots_dir.exists():
        return {}
    found = {}
    for path in sorted(store.bots_dir.glob("*/memory.json")):
        if path.is_file() and not path.is_symlink():
            found[str(path.relative_to(store.root))] = path.read_bytes()
    for folder in sorted(store.bots_dir.glob("*/memory")):
        if not folder.is_dir() or folder.is_symlink():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.is_symlink():
                found[str(path.relative_to(store.root))] = path.read_bytes()
    return found


def _safe_filename(name: str) -> str:
    base = Path(name or "file").name
    cleaned = "".join(ch for ch in base if ch.isalnum() or ch in "._- ").strip()
    return (cleaned or "file")[:80]


def _media_type(content_type: str, data: bytes) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        return ctype
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain"


def _fact(store: Store, text: str) -> str:
    cleaned = " ".join((text or "").split())
    redacted = " ".join(redact(store, cleaned).split())
    if cleaned and not redacted:
        raise StoreError("That was not saved.", 400)
    return redacted


def _attachment_for_message(store: Store, bot_id: str, chat_id: str, file_id: str | None) -> dict | None:
    if not file_id:
        return None
    meta, _data = store.read_chat_file(bot_id, chat_id, file_id)
    return {"id": meta["id"], "name": meta.get("name") or "file", "media_type": meta.get("media_type") or "application/octet-stream"}


def _public_attachment(attachment: dict | None) -> dict | None:
    if not attachment:
        return None
    public = {
        "id": attachment.get("id"),
        "name": attachment.get("name") or "file",
        "media_type": attachment.get("media_type") or "application/octet-stream",
    }
    excerpt = attachment.get("excerpt") or ""
    if isinstance(excerpt, str) and excerpt.strip():
        public["excerpt"] = excerpt
    source = attachment.get("path") or ""
    if isinstance(source, str) and source.strip():
        public["path"] = source
    return public


def _with_reaction(stored: dict, model: dict) -> dict:
    """The emoji and which message, on the message the model already sees."""
    note = reaction_signal(stored if isinstance(stored, dict) else {})
    if not note:
        return model
    content = model.get("content")
    if isinstance(content, list):
        return {**model, "content": [{"type": "text", "text": note}, *content]}
    text = content or ""
    return {**model, "content": f"{note}\n\n{text}".strip()}


def _model_message(store: Store, bot_id: str, chat_id: str, stored: dict, clipped: dict) -> dict:
    """Show an attached file to the model without putting a typed path in its place."""
    content = clipped.get("content") or ""
    role = clipped.get("role") or "user"
    attachment = stored.get("attachment") if isinstance(stored, dict) else None
    if not isinstance(attachment, dict) or not attachment.get("id"):
        return _with_reaction(stored, {"role": role, "content": content})
    try:
        meta, data = store.read_chat_file(bot_id, chat_id, attachment["id"])
    except StoreError:
        return _with_reaction(stored, {"role": role, "content": content})
    name = meta.get("name") or "file"
    media = meta.get("media_type") or ""
    if media.startswith("image/") and len(data) <= 400_000:
        encoded = base64.b64encode(data).decode("ascii")
        return _with_reaction(stored, {
            "role": role,
            "content": [
                {"type": "text", "text": content or "Look at the attached picture."},
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{encoded}"}},
            ],
        })
    if media.startswith("text/") or media == "application/octet-stream":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        text = redact(store, text)
        if len(text) > 8000:
            text = text[:7980].rstrip() + "\n[file truncated]"
        if text.strip():
            return _with_reaction(
                stored,
                {"role": role, "content": f"{content}\n\nAttached file {name}:\n{text}".strip()},
            )
    return _with_reaction(stored, {"role": role, "content": f"{content}\n\nAttached file {name}.".strip()})


def _snapshot_rooms(store: Store) -> dict[str, bytes]:
    if not store.rooms_dir.exists():
        return {}
    return {
        path.name: path.read_bytes()
        for path in sorted(store.rooms_dir.glob("*.json"))
        if path.is_file()
    }


def _legacy_schedule(body: ScheduleIn) -> bool:
    """The older timer form. It still allows a one-minute interval."""
    rich = any([
        body.name,
        body.timezone,
        body.weekdays,
        body.daily,
        body.weekly,
        body.weekly_day,
        body.once,
        body.preset,
        body.every_hours,
        body.quiet is True,
    ])
    return (body.kind or "") in {"interval", "cron"} and not rich


def _public_schedule(schedule: dict) -> dict:
    item = dict(schedule)
    item["label"] = describe(item)
    try:
        item["preview"] = preview(item)
    except Exception:
        item["preview"] = item["label"]
    return item


def _schedule_record(body: ScheduleIn) -> dict:
    if not _legacy_schedule(body):
        try:
            return compile_routine(body.model_dump())
        except ScheduleError as exc:
            raise StoreError(str(exc), 400) from exc
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise StoreError("Write a prompt for the schedule.", 400)
    if len(prompt) > 4000:
        raise StoreError("Prompt is too long (4000 characters max).", 400)
    kind = (body.kind or "").strip()
    record = {
        "id": new_id(),
        "prompt": prompt,
        "kind": kind,
        "paused": False,
        "last_slot": None,
        "created_at": now_iso(),
    }
    if kind == "interval":
        minutes = body.every_minutes
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1 or minutes > 1440:
            raise StoreError("Minutes must be a whole number from 1 to 1440.", 400)
        record["every_minutes"] = minutes
    elif kind == "cron":
        expression = " ".join((body.cron or "").split())
        try:
            parse_cron(expression)
        except ScheduleError as exc:
            raise StoreError(str(exc), 400) from exc
        record["cron"] = expression
    else:
        raise StoreError("Choose every-N-minutes or a 5-field cron.", 400)
    return record


def _snapshot_skills(store: Store) -> dict[str, bytes]:
    if not store.skills_dir.exists():
        return {}
    return {
        path.name: path.read_bytes()
        for path in sorted(store.skills_dir.glob("*.md"))
        if path.is_file()
    }


def _require_endpoint(store: Store, bot_id: str) -> dict:
    bot = store.get_bot(bot_id)
    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except StoreError:
        endpoint = None
    if endpoint is None:
        raise HTTPException(
            status_code=409,
            detail="This bot's endpoint is missing. Choose another endpoint. The chat was not changed.",
        )
    return endpoint


def _chat_connection_error(store: Store, bot_id: str, detail: str) -> str:
    """The error, plus the connection this bot has saved right now."""
    try:
        bot = store.get_bot(bot_id)
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except StoreError:
        return detail
    if endpoint is None:
        return detail
    return llm.connection_error_text(detail, endpoint, chosen_model(bot, endpoint), bot.get("name") or "")


def context_note(stats: dict) -> str:
    """Tell the model how much of this chat it is really seeing."""
    total = int(stats.get("transcript_messages") or 0)
    shown = int(stats.get("model_messages") or 0)
    folded = int(stats.get("compacted_messages") or 0)
    used = int(stats.get("context_tokens") or 0)
    limit = int(stats.get("max_context_tokens") or 0)
    budget = f"about {used:,} of your {limit:,}-token chat budget"
    if total == 0:
        return f"This chat has no saved messages yet. Your chat budget is {limit:,} tokens."
    if folded <= 0:
        return (
            f"This chat has {total} saved message{'s' if total != 1 else ''}. "
            f"All of them are in the message list below, in full ({budget}). Nothing was left out or compacted."
        )
    return (
        f"This chat has {total} saved messages. The newest {shown} are in the message list below, in full ({budget}). "
        f"The {folded} older ones did not fit, so they are summarized here. "
        "Read any of them word for word with the history tool."
    )


def _chat_budget(store: Store, chat: dict) -> int:
    try:
        bot = store.get_bot(chat["bot_id"])
    except StoreError:
        return bot_context_chars(None)
    return bot_context_chars(bot)


def _persist_context(store: Store, chat: dict) -> dict:
    prepared = visible_prepare(chat, _chat_budget(store, chat))
    chat["summary"] = prepared.summary
    chat["summarized_through"] = prepared.summarized_through
    return prepared.stats


def _public_chat(store: Store, chat: dict, window: int | None = None) -> dict:
    """Transcript plus the bounded view. Reading a chat does not write it.

    `window` returns only the recent tail. Omit it and every message is included.
    """
    prepared = visible_prepare(chat, _chat_budget(store, chat))
    messages = list(chat.get("messages") or [])
    shown = messages
    start = 0
    if window is not None:
        size = int(window)
        if size < 0:
            size = 0
        if len(messages) > size:
            start = len(messages) - size
            shown = messages[start:]
    return {
        "id": chat["id"],
        "bot_id": chat["bot_id"],
        "title": chat.get("title") or "New chat",
        "created_at": chat.get("created_at"),
        "updated_at": chat.get("updated_at"),
        "summary": prepared.summary,
        "summarized_through": prepared.summarized_through,
        "fresh_from": int(chat.get("fresh_from") or 0),
        "messages": shown,
        "message_count": len(messages),
        "window_start": start,
        "context": prepared.stats,
        "run": public_run(chat),
    }


def _wants_stream(request: Request) -> bool:
    return "text/event-stream" in request.headers.get("accept", "")


def _sse(payload: dict) -> str:
    """Stamp the open run so the page can drop an event that belongs to another chat."""
    slot = turn_mod.current_slot()
    if slot is not None and slot.run_id:
        payload = dict(payload)
        payload.setdefault("run_id", slot.run_id)
        if slot.bot_id:
            payload.setdefault("bot_id", slot.bot_id)
        if slot.chat_id:
            payload.setdefault("chat_id", slot.chat_id)
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _can_retry(messages: list) -> bool:
    if not messages:
        return False
    last = messages[-1]
    if last.get("role") == "user":
        return True
    if last.get("role") != "assistant":
        return False
    if last.get("error"):
        return True
    text = (last.get("content") or "").strip()
    return text == "Stopped." or text.startswith("Stopped:")


def _save_user_turn(store: Store, bot_id: str, chat_id: str, content: str, attachment: dict | None = None) -> dict:
    chat = store.get_chat(bot_id, chat_id)
    message = {"id": new_id(), "role": "user", "content": content, "created_at": now_iso()}
    if attachment:
        message["attachment"] = _public_attachment(attachment)
    chat["messages"].append(message)
    if chat.get("title") in {None, "", "New chat"}:
        chat["title"] = make_title(content, store)
    _persist_context(store, chat)
    chat["updated_at"] = now_iso()
    store.save_chat(chat)
    return message


def _computer_prompt(store: Store) -> str:
    rows = []
    for item in store.list_computers():
        kind = "Linux" if item.get("kind") == "linux" else "Windows"
        rows.append(f"- {item.get('name')} ({kind})")
    return "\n".join(rows)


def _computer_fields(body: ComputerIn) -> dict:
    kind = (body.kind or "").strip().lower()
    if kind not in {"linux", "windows"}:
        raise StoreError("Choose a Linux computer or a Windows computer. Chats were not changed.", 400)
    host = (body.host or "").strip()
    if not host or any(ch.isspace() for ch in host) or len(host) > 253:
        raise StoreError("Type the host, with no spaces. Chats were not changed.", 400)
    port = body.port
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or port < 1 or port > 65535):
        raise StoreError("Port must be a whole number from 1 to 65535. Chats were not changed.", 400)
    return {"name": clean_name(body.name, "Name"), "kind": kind, "host": host, "port": port}


def _computer_record(body: ComputerIn, *, allow_blank: bool = False, existing: dict | None = None) -> dict:
    fields = _computer_fields(body)
    user = " ".join((body.user or "").split())
    password = (body.password or "").strip()
    key = (body.key or "").strip()
    if len(password) > 100_000 or len(key) > 100_000:
        raise StoreError("That sign-in is too long. Chats were not changed.", 400)
    if not password and not key:
        if allow_blank and existing and existing.get("vault"):
            return {**fields, "vault": None}
        raise StoreError("Type a password or a key in the sign-in prompt. Chats were not changed.", 400)
    if not user or len(user) > 80:
        raise StoreError("Type the username in the sign-in prompt. Chats were not changed.", 400)
    if fields["kind"] == "windows" and not password:
        raise StoreError("Type the password in the sign-in prompt. Chats were not changed.", 400)
    if key and fields["kind"] == "linux":
        auth, secret = "key", key
    else:
        auth, secret = "password", password
    return {**fields, "vault": seal({"user": user, "auth": auth, "secret": secret})}


def public_computer(computer: dict) -> dict:
    return {
        "id": computer["id"],
        "name": computer["name"],
        "kind": computer.get("kind"),
        "host": computer.get("host"),
        "port": computer.get("port"),
        "has_sign_in": bool(computer.get("vault")),
        "created_at": computer.get("created_at"),
    }


def _last_user_text(messages: list) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _skills_for_prompt(store: Store, bot_id: str, request: str) -> str:
    """User skills first, then learned ones by measured success. Lessons only when one matches."""
    text = pack_skills(rank_skills(store, store.list_skills()))
    block = lesson_block(store, bot_id, request) if bot_id and request else ""
    if not block:
        return text
    return f"{text}\n\n{block}" if text else block


def _save_user_skill(store: Store, skill: dict) -> dict:
    """A skill typed by the person, or saved from a reply fence. Learning does not use this."""
    stored = store.save_skill(skill)
    slug = stored.get("name") or ""
    if slug:
        mark_origin(store, slug, "user")
    return stored


_CORRECTION = re.compile(
    r"(?i)(?:\bthat(?:'s| is) not\b|\bi meant\b|\bi said\b|\bnot what i\b|\bwrong\b|\bincorrect\b|\bno,)"
)


async def _propose_if_corrected(store: Store, bot_id: str, chat: dict) -> None:
    """A correction of the last reply can become a candidate. Replay still decides."""
    if os.environ.get("EASYAGENT_LEARN") == "0":
        return
    messages = chat.get("messages") or []
    if len(messages) < 2:
        return
    last = messages[-1]
    prev = messages[-2]
    if not isinstance(last, dict) or not isinstance(prev, dict):
        return
    if last.get("role") != "user" or prev.get("role") != "assistant":
        return
    text = str(last.get("content") or "")
    if not _CORRECTION.search(text):
        return
    try:
        from easyagent.learn import observe, propose_from_signal

        observe(
            store,
            bot_id,
            f"The person corrected a reply. {text[:240]}",
            reason="correction",
            task=str(prev.get("content") or "")[:400],
        )
        await propose_from_signal(
            store,
            store.get_bot(bot_id),
            text,
            reason="correction",
            task=str(prev.get("content") or ""),
        )
    except Exception:
        return


async def _propose_thumb(store: Store, bot_id: str, messages: list, message_id: str, *, quarantine: bool = False) -> None:
    """A thumbs-down can become a candidate. A thumbs-up does not."""
    if os.environ.get("EASYAGENT_LEARN") == "0" or not bot_id:
        return
    text = ""
    emoji = ""
    for item in messages or []:
        if item.get("id") == message_id:
            emoji = item.get("reaction") or ""
            text = str(item.get("content") or "")
            break
    if emoji != "👎":
        return
    try:
        from easyagent.learn import propose_from_signal

        await propose_from_signal(
            store,
            store.get_bot(bot_id),
            f"The person marked a reply down. {text[:300]}",
            reason="thumb",
            task=text,
            quarantine=quarantine,
        )
    except Exception:
        return


def _note_thumb(store: Store, bot_id: str, messages: list, message_id: str, *, quarantine: bool = False) -> None:
    """A thumbs-up or thumbs-down is an inbox note. It does not add a chat line."""
    if not bot_id:
        return
    text = ""
    emoji = ""
    for item in messages or []:
        if item.get("id") == message_id:
            emoji = item.get("reaction") or ""
            text = str(item.get("content") or "")
            break
    if emoji not in {"👍", "👎"}:
        return
    try:
        from easyagent.learn import credit_named, observe

        observe(
            store,
            bot_id,
            f"The person marked a reply {emoji}.",
            reason="thumb",
            task=text,
            quarantine=quarantine,
        )
        credit_named(store, text, emoji == "👍", "thumb")
    except Exception:
        return


def _turn_messages(store: Store, bot_id: str, chat: dict) -> tuple[dict, list[dict]]:
    bot = store.get_bot(bot_id)
    endpoint = _require_endpoint(store, bot_id)
    view = model_turn(store, bot, chat, endpoint)
    others = ", ".join(other["name"] for other in store.list_bots() if other["id"] != bot["id"])
    ensure_own_files(store, bot_id)
    system = build_system(
        bot_name=bot["name"],
        direction=store.read_direction(),
        summary=view.summary,
        skills_text=_skills_for_prompt(store, bot_id, _last_user_text(chat.get("messages") or [])),
        other_bots=others,
        computers=_computer_prompt(store),
        memory=_memory_prompt(store, bot_id),
        recalled=view.recalled,
        projects=_projects_prompt(store, bot_id),
        message_ids=message_index(chat.get("messages") or [], self_id=bot_id),
        context_note=context_note(view.stats),
        own_files=own_files_prompt(store, bot_id, chat.get("id")),
        earlier=view.earlier,
        workspace=_workspace_text(store, bot_id),
        connectors=_connectors_prompt(store, bot_id),
    )
    source = list(chat.get("messages") or [])[view.summarized_through :]
    if len(source) >= len(view.tail):
        source = source[len(source) - len(view.tail) :]
    if len(source) == len(view.tail):
        tail = [
            _model_message(store, bot_id, chat["id"], stored, item)
            for stored, item in zip(source, view.tail)
        ]
    else:
        tail = view.tail
    return endpoint, [{"role": "system", "content": system}, *tail]


def _connectors_prompt(store: Store, bot_id: str) -> str:
    from easyagent.connectors import prompt_block

    return prompt_block(store, bot_id)


def _projects_prompt(store: Store, bot_id: str) -> str:
    try:
        projects = store.projects_for_bot(bot_id)
    except StoreError:
        return ""
    lines = []
    for project in projects[:12]:
        kind = "yours" if project.get("kind") == "bot" else "shared"
        try:
            files = store.list_project_files(project["id"])
        except StoreError:
            files = []
        names = [item["name"] for item in files[:24]]
        extra = len(files) - len(names)
        shown = ", ".join(names) if names else "no files yet"
        if extra:
            shown += f", {extra} more"
        lines.append(f"- {project.get('name')} ({kind}): {shown}")
    return "\n".join(lines)


def _public_project(store: Store, project: dict) -> dict:
    return {
        "id": project["id"],
        "name": project.get("name") or "Project",
        "kind": project.get("kind"),
        "bot_id": project.get("bot_id"),
        "bot_ids": list(project.get("bot_ids") or []),
        "files": store.list_project_files(project["id"]),
        "created_at": project.get("created_at"),
        "updated_at": project.get("updated_at"),
    }


def _snapshot_room_files(store: Store) -> dict[str, bytes]:
    if not store.rooms_dir.is_dir():
        return {}
    return {
        path.name: path.read_bytes()
        for path in sorted(store.rooms_dir.glob("*.json"))
        if path.is_file() and not path.is_symlink()
    }


def _guard_transcripts(store: Store, chats_before: dict, rooms_before: dict) -> None:
    if _snapshot_bots(store) != chats_before:
        raise HTTPException(500, "A project change rewrote a chat.")
    if _snapshot_room_files(store) != rooms_before:
        raise HTTPException(500, "A project change rewrote a room.")


async def _prepared_uploads(store: Store, files: list[UploadFile]) -> list[tuple[str, bytes]]:
    if not files:
        raise HTTPException(400, "Choose a file. Nothing was kept.")
    prepared = []
    for upload in files:
        data = await upload.read()
        if contains_secret(store, data):
            raise HTTPException(400, "That file was not kept.")
        prepared.append((_safe_filename(upload.filename or "file"), data))
    return prepared


def _save_uploads(store: Store, project_id: str, prepared: list[tuple[str, bytes]]) -> dict:
    chats_before = _snapshot_bots(store)
    rooms_before = _snapshot_room_files(store)
    try:
        for name, data in prepared:
            store.save_project_file(project_id, name=name, data=data)
        project = store.get_project(project_id)
    except StoreError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    _guard_transcripts(store, chats_before, rooms_before)
    return _public_project(store, project)


def _memory_prompt(store: Store, bot_id: str) -> str:
    """The index only. Topic files are read later, one at a time."""
    try:
        slugs = store.memory_slugs(bot_id)
    except StoreError:
        return ""
    if not slugs:
        return ""
    return "Index:\n" + "\n".join(f"- {slug}" for slug in slugs[:24])


async def _complete_for_chat(store: Store, bot_id: str, chat: dict) -> tuple[str, list[str], list[str], str, str, bool, str, str]:
    bot = store.get_bot(bot_id)
    endpoint, messages = _turn_messages(store, bot_id, chat)
    conn = gate.bind_connection(endpoint, bot.get("name") or "")
    try:
        settled = await complete_with_tools(
            base_url=endpoint["base_url"],
            api_key=endpoint.get("api_key") or None,
            model=chosen_model(bot, endpoint),
            messages=messages,
            store=store,
            bot_id=bot_id,
            chat_id=chat.get("id"),
        )
    finally:
        gate.reset_connection(conn)
    visible, skills = extract_skills(settled.text)
    saved: list[str] = []
    for skill in skills:
        stored = _save_user_skill(store, skill)
        saved.append(stored["name"])
    if not visible.strip() and not settled.choices and not settled.reacted:
        visible = "Saved skill: " + ", ".join(saved) if saved else "(empty reply)"
    return (
        visible,
        saved,
        list(settled.choices),
        settled.made or "",
        settled.thinking or "",
        settled.reacted,
        settled.check or "",
        settled.lesson or "",
    )


def _labeled_room_messages(messages: list[dict]) -> list[dict]:
    labeled = []
    for message in messages:
        name = message.get("speaker_name") or "Someone"
        text = message.get("content") or ""
        if message.get("error"):
            text = ERROR_PLACEHOLDER
        item = {"role": message.get("role") or "user", "content": f"{name}: {text}"}
        if message.get("reaction") in {"👍", "👎", "❤️", "👀"}:
            item["reaction"] = message.get("reaction")
        labeled.append(item)
    return labeled


def _prepare_room(room: dict):
    return prepare_context(
        _labeled_room_messages(room.get("messages") or []),
        room.get("summary") or "",
        int(room.get("summarized_through") or 0),
    )


def _persist_room(room: dict) -> dict:
    prepared = _prepare_room(room)
    room["summary"] = prepared.summary
    room["summarized_through"] = prepared.summarized_through
    return prepared.stats


def _public_room(store: Store, room: dict) -> dict:
    """Full room transcript. Reading a room does not write it or any bot chat."""
    prepared = _prepare_room(room)
    members = []
    for bot_id in room.get("bot_ids") or []:
        try:
            bot = store.get_bot(bot_id)
        except StoreError:
            members.append({"id": bot_id, "name": "Missing bot", "missing": True})
            continue
        members.append({"id": bot["id"], "name": bot["name"], "missing": False})
    return {
        "id": room["id"],
        "name": room.get("name") or "Room",
        "created_at": room.get("created_at"),
        "updated_at": room.get("updated_at"),
        "bot_ids": list(room.get("bot_ids") or []),
        "members": members,
        "summary": prepared.summary,
        "summarized_through": prepared.summarized_through,
        "messages": room.get("messages") or [],
        "context": prepared.stats,
    }


def _room_tail_item(bot: dict, message: dict, clip_limit: int | None = None) -> dict:
    name = message.get("speaker_name") or "Someone"
    content = message.get("content") or ""
    note = reaction_signal(message)
    if note:
        content = f"{note}\n\n{content}"
    if message.get("speaker") == bot["id"] and not message.get("error"):
        return {"role": "assistant", "content": clip_message(content, clip_limit)}
    if message.get("speaker") == "user" and not message.get("error"):
        return {"role": "user", "content": clip_message(content, clip_limit)}
    if message.get("error"):
        text = f"{name} could not reply. {ERROR_PLACEHOLDER}"
    else:
        text = f"{name}: {content}"
    return {"role": "user", "content": clip_message(text, clip_limit)}


async def _complete_for_room_bot(store: Store, room: dict, bot_id: str) -> tuple[str, list[str], str | None]:
    """One bot's reply in a room. Returns (visible, skills, error). Never writes a private chat."""
    try:
        bot = store.get_bot(bot_id)
    except StoreError:
        return "", [], "This bot is no longer on disk. The room transcript was kept.", [], False
    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except StoreError:
        endpoint = None
    if endpoint is None:
        return "", [], f"{bot['name']}'s endpoint is missing. The room transcript was kept.", [], False
    budget = bot_context_chars(bot)
    prepared = prepare_context(
        _labeled_room_messages(room.get("messages") or []),
        room.get("summary") or "",
        int(room.get("summarized_through") or 0),
        budget,
    )
    others = []
    for other_id in room.get("bot_ids") or []:
        if other_id == bot["id"]:
            continue
        try:
            others.append(store.get_bot(other_id)["name"])
        except StoreError:
            others.append("a missing bot")
    company = f" and {', '.join(others)}" if others else ""
    note = (
        f'You are in the room "{room.get("name") or "Room"}" with the user{company}. '
        "Speak only as yourself. Other people's lines are prefixed with their names. "
        "Do not answer for them. Do not delete this room or anyone's private chats."
    )
    system = build_system(
        bot_name=bot["name"],
        direction=store.read_direction(),
        summary=prepared.summary,
        skills_text=_skills_for_prompt(store, bot["id"], _last_user_text(room.get("messages") or [])),
        room_note=note,
        computers=_computer_prompt(store),
        memory=_memory_prompt(store, bot["id"]),
        projects=_projects_prompt(store, bot["id"]),
        message_ids=message_index(room.get("messages") or [], self_id=bot["id"]),
        context_note=context_note(prepared.stats),
        own_files=own_files_prompt(store, bot["id"]),
        workspace=_workspace_text(store, bot["id"]),
        connectors=_connectors_prompt(store, bot["id"]),
    )
    _, _, clip_limit = context_window(budget)
    tail = [
        _room_tail_item(bot, message, clip_limit)
        for message in (room.get("messages") or [])[prepared.summarized_through :]
    ]
    messages = [{"role": "system", "content": system}, *tail]
    conn = gate.bind_connection(endpoint, bot.get("name") or "")
    try:
        try:
            settled = await complete_with_tools(
                base_url=endpoint["base_url"],
                api_key=endpoint.get("api_key") or None,
                model=chosen_model(bot, endpoint),
                messages=messages,
                store=store,
                bot_id=bot["id"],
                chat_id=room.get("id"),
            )
        except llm.ProviderError as exc:
            return "", [], llm.connection_error_text(str(exc), endpoint, chosen_model(bot, endpoint), bot["name"]), [], False
        except SearchError as exc:
            return "", [], str(exc), [], False
        except ToolError as exc:
            return "", [], str(exc), [], False
        visible, skills = extract_skills(settled.text)
        saved: list[str] = []
        for skill in skills:
            stored = _save_user_skill(store, skill)
            saved.append(stored["name"])
        if not visible.strip() and not settled.choices and not settled.reacted:
            visible = "Saved skill: " + ", ".join(saved) if saved else "(empty reply)"
        return visible, saved, None, list(settled.choices), settled.reacted
    finally:
        gate.reset_connection(conn)


def _reaction_on_latest(before: list, after: list, message_id: str) -> dict | None:
    """A new emoji on the latest assistant message. Clearing one does not wake a turn."""

    def find(messages: list) -> dict | None:
        for item in messages:
            if item.get("id") == message_id:
                return item
        return None

    previous = find(before)
    current = find(after)
    if current is None or not current.get("reaction"):
        return None
    if previous is not None and previous.get("reaction") == current.get("reaction"):
        return None
    if not after or after[-1].get("id") != message_id or current.get("role") != "assistant":
        return None
    return current


async def _ack_chat_reaction(store: Store, bot_id: str, chat_id: str, message: dict) -> None:
    """One completion. The reaction is already on the message the model sees."""
    print("heuristic reaction: a reaction on the latest reply wakes one short answer", flush=True)
    turn_mod.bind(store, chat_id)
    chat = store.get_chat(bot_id, chat_id)
    try:
        endpoint, messages = _turn_messages(store, bot_id, chat)
    except HTTPException:
        return
    note = reaction_signal(message)
    messages = [
        *messages,
        {
            "role": "user",
            "content": (
                f"{note}\n"
                "The person just reacted. This is not a new task. "
                "Write one short sentence if a reply helps. "
                "You may react to one of their messages. Do not use any other tool."
            ),
        },
    ]
    bot = store.get_bot(bot_id)
    conn = gate.bind_connection(endpoint, bot.get("name") or "")
    try:
        try:
            raw = await llm.complete(
                base_url=endpoint["base_url"],
                api_key=endpoint.get("api_key") or None,
                model=chosen_model(bot, endpoint),
                messages=messages,
            )
        except (llm.ProviderError, turn_mod.TurnCancelled):
            return
    finally:
        gate.reset_connection(conn)
    visible, _did = tapback_from_reply(store, bot_id, chat_id, raw)
    if not visible.strip():
        return
    _finish_reply(store, bot_id, chat_id, visible.strip(), [])


async def _ack_room_reaction(store: Store, room_id: str, message: dict) -> None:
    """The bot who wrote the latest line gets one short look. Other bots stay quiet."""
    speaker = message.get("speaker")
    if not speaker or speaker == "user":
        return
    print("heuristic reaction: a reaction on the latest reply wakes one short answer", flush=True)
    turn_mod.bind(store, room_id)
    try:
        bot = store.get_bot(speaker)
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except StoreError:
        return
    room = store.get_room(room_id)
    budget = bot_context_chars(bot)
    prepared = prepare_context(
        _labeled_room_messages(room.get("messages") or []),
        room.get("summary") or "",
        int(room.get("summarized_through") or 0),
        budget,
    )
    system = build_system(
        bot_name=bot["name"],
        direction=store.read_direction(),
        summary=prepared.summary,
        skills_text=_skills_for_prompt(store, bot["id"], _last_user_text(room.get("messages") or [])),
        room_note=(
            f'You are in the room "{room.get("name") or "Room"}". '
            "The person just reacted to your latest line. This is not a new task."
        ),
        memory=_memory_prompt(store, bot["id"]),
        message_ids=message_index(room.get("messages") or [], self_id=bot["id"]),
        connectors=_connectors_prompt(store, bot["id"]),
    )
    _, _, clip_limit = context_window(budget)
    tail = [
        _room_tail_item(bot, item, clip_limit)
        for item in (room.get("messages") or [])[prepared.summarized_through :]
    ]
    note = reaction_signal(message)
    messages = [
        {"role": "system", "content": system},
        *tail,
        {
            "role": "user",
            "content": (
                f"{note}\n"
                "The person just reacted. Write one short sentence if a reply helps. "
                "You may react to one of their messages. Do not use any other tool."
            ),
        },
    ]
    conn = gate.bind_connection(endpoint, bot.get("name") or "")
    try:
        try:
            raw = await llm.complete(
                base_url=endpoint["base_url"],
                api_key=endpoint.get("api_key") or None,
                model=chosen_model(bot, endpoint),
                messages=messages,
            )
        except (llm.ProviderError, turn_mod.TurnCancelled):
            return
    finally:
        gate.reset_connection(conn)
    visible, _did = tapback_from_reply(store, bot["id"], room_id, raw)
    if not visible.strip():
        return
    room = store.get_room(room_id)
    room["messages"].append(
        {
            "id": new_id(),
            "role": "assistant",
            "speaker": bot["id"],
            "speaker_name": bot["name"],
            "content": visible.strip(),
            "created_at": now_iso(),
        }
    )
    _persist_room(room)
    room["updated_at"] = now_iso()
    store.save_room(room)


def public_run(chat: dict) -> dict:
    """idle, running, stopped, or error. A chat with no run yet is idle."""
    raw = chat.get("run") if isinstance(chat.get("run"), dict) else {}
    status = raw.get("status") or "idle"
    if status not in {"idle", "running", "stopped", "error"}:
        status = "idle"
    return {
        "id": raw.get("id") or "",
        "status": status,
        "started_at": raw.get("started_at"),
        "last_activity_at": raw.get("last_activity_at"),
        "current_step": raw.get("current_step") or "",
        "reason": raw.get("reason") or "",
    }


def _apply_run(
    chat: dict,
    *,
    status: str,
    step: str | None = None,
    reason: str | None = None,
    start: bool = False,
) -> dict:
    run = dict(chat.get("run") or {})
    now = now_iso()
    if status == "idle":
        chat["run"] = {
            "id": "",
            "status": "idle",
            "started_at": None,
            "last_activity_at": None,
            "current_step": "",
            "reason": "",
        }
        return chat["run"]
    if start or not run.get("started_at"):
        run["started_at"] = now
        run["id"] = turn_mod.current_run_id() or run.get("id") or ""
    if start and reason is None:
        run["reason"] = ""
    run["status"] = status
    if step is not None:
        run["current_step"] = step
    if reason is not None:
        run["reason"] = reason
    run["last_activity_at"] = now
    chat["run"] = {
        "id": run.get("id") or "",
        "status": run.get("status") or "idle",
        "started_at": run.get("started_at"),
        "last_activity_at": run.get("last_activity_at"),
        "current_step": run.get("current_step") or "",
        "reason": run.get("reason") or "",
    }
    return chat["run"]


def _due_live(store: Store, chat_id: str, *, force: bool) -> bool:
    key = f"{store.root}:{chat_id}"
    now = time.monotonic()
    if force or now - _live_saved_at.get(key, 0.0) >= LIVE_SAVE_SECONDS:
        _live_saved_at[key] = now
        return True
    return False


def _write_live(
    store: Store,
    bot_id: str,
    chat_id: str,
    *,
    status: str = "running",
    step: str | None = None,
    reason: str | None = None,
    thinking: str | None = None,
    reply: str | None = None,
    force: bool = False,
    start: bool = False,
) -> dict | None:
    """Persist the running step. A locked chat file does not stop the reply."""
    if not _due_live(store, chat_id, force=force or start):
        return None
    try:
        chat = store.get_chat(bot_id, chat_id)
        run = _apply_run(chat, status=status, step=step, reason=reason, start=start)
        if thinking and thinking.strip():
            message = _live_slot(chat)
            message["thinking"] = thinking
        if reply and reply.strip():
            message = _live_slot(chat)
            message["content"] = reply
        if thinking or reply:
            chat["updated_at"] = now_iso()
        store.save_chat(chat)
        return run
    except OSError as exc:
        _log.warning("Live chat save skipped: %s", exc)
        print(
            "heuristic live-save: the chat file was locked, so this update was skipped and the reply kept going",
            flush=True,
        )
        return None


def _save_chat_final(store: Store, chat: dict) -> None:
    """The finished message. Retries live inside the replace. A lasting lock is an error."""
    try:
        store.save_chat(chat)
    except OSError as exc:
        _log.warning("Could not save the chat: %s", exc)
        raise HTTPException(
            500,
            "Stopped: Could not save the reply. The chat file was locked, and retrying did not help.",
        ) from exc


def _stopped_text(reason: str = "") -> str:
    """A stop always names why. A bare 'Stopped.' is not a finished line."""
    cleaned = " ".join((reason or "").split())
    if not cleaned or cleaned == "Stopped.":
        cleaned = "the reply stopped before it finished."
    if cleaned.startswith("Stopped:"):
        return cleaned
    return f"Stopped: {cleaned}"


def _live_slot(chat: dict) -> dict:
    messages = chat.setdefault("messages", [])
    if messages and messages[-1].get("live") and messages[-1].get("role") == "assistant":
        return messages[-1]
    message = {
        "id": new_id(),
        "role": "assistant",
        "content": "",
        "live": True,
        "created_at": now_iso(),
    }
    messages.append(message)
    return message


def _save_live_reply(store: Store, bot_id: str, chat_id: str, text: str) -> None:
    """Put the running account in the chat as soon as it exists."""
    if not (text or "").strip():
        return
    _write_live(store, bot_id, chat_id, reply=text, force=True)


def _save_live_thinking(store: Store, bot_id: str, chat_id: str, text: str) -> None:
    """Keep the model's reasoning on the live reply. A locked file does not end the turn."""
    if not (text or "").strip():
        return
    _write_live(store, bot_id, chat_id, thinking=text, step="Thinking")


def _replace_live_thinking(store: Store, bot_id: str, chat_id: str, text: str) -> None:
    """A replayed model turn replaces thinking from the attempt that dropped."""
    try:
        chat = store.get_chat(bot_id, chat_id)
        message = _live_slot(chat)
        cleaned = text or ""
        if cleaned.strip():
            message["thinking"] = cleaned
        else:
            message.pop("thinking", None)
        chat["updated_at"] = now_iso()
        store.save_chat(chat)
    except OSError as exc:
        _log.warning("Live thinking replace skipped: %s", exc)


def _stopped_transport(detail: str) -> bool:
    """A client abort shows up as an incomplete chunked read. It is a stop."""
    return "incomplete chunked read" in (detail or "").lower()


def _settle_abandoned_runs(store: Store) -> None:
    """A chat left 'running' by a dead process is stopped, with the reason named."""
    try:
        bots = store.list_bots()
    except StoreError:
        return
    for bot in bots:
        try:
            chats = store.list_chats(bot["id"])
        except StoreError:
            continue
        for summary in chats:
            try:
                chat = store.get_chat(bot["id"], summary["id"])
            except StoreError:
                continue
            run = chat.get("run") if isinstance(chat.get("run"), dict) else {}
            live = any(
                isinstance(item, dict) and item.get("role") == "assistant" and item.get("live")
                for item in chat.get("messages") or []
            )
            if run.get("status") == "running" or live:
                _settle_interrupted(store, bot["id"], summary["id"], "the server restarted")


def _settle_interrupted(store: Store, bot_id: str, chat_id: str, reason: str = "") -> None:
    """Keep partial Thinking and reply. An empty stop names the reason. It is not a blank line.

    Every live assistant is closed, not only the last line. Thinking with no answer
    is Stopped with a reason, so a dropped stream is not a blank success.
    """
    try:
        chat = store.get_chat(bot_id, chat_id)
    except StoreError:
        return
    messages = chat.setdefault("messages", [])
    if not messages:
        _apply_run(chat, status="stopped", reason=reason, step="")
        try:
            _save_chat_final(store, chat)
        except HTTPException:
            _log.warning("Could not record that the run stopped.")
        return
    line = _stopped_text(reason)
    changed = False
    for message in messages:
        if message.get("role") != "assistant" or not message.get("live"):
            continue
        if not (message.get("content") or "").strip():
            message["content"] = line
        message.pop("live", None)
        message.pop("error", None)
        changed = True
    if messages[-1].get("role") == "user":
        messages.append(
            {
                "id": new_id(),
                "role": "assistant",
                "content": line,
                "created_at": now_iso(),
            }
        )
        changed = True
    if not changed:
        return
    _apply_run(chat, status="stopped", reason=reason, step="")
    chat["updated_at"] = now_iso()
    try:
        _save_chat_final(store, chat)
    except HTTPException:
        _log.warning("Could not record that the run stopped.")


def _append_error(store: Store, bot_id: str, chat_id: str, detail: str) -> dict:
    """Keep every stored turn and append the error. The transcript is not shortened."""
    chat = store.get_chat(bot_id, chat_id)
    before = len(chat.get("messages") or [])
    shown = _stopped_text(redact(store, detail))
    if chat.get("messages") and chat["messages"][-1].get("live"):
        last = chat["messages"][-1]
        last.pop("live", None)
        if not (last.get("content") or "").strip():
            last["content"] = shown
            last["error"] = True
            stats = _persist_context(store, chat)
            chat["updated_at"] = now_iso()
            _apply_run(chat, status="error", reason=shown, step="")
            _save_chat_final(store, chat)
            poke(store, "message")
            public = _public_chat(store, chat)
            public["context"] = stats
            return {"reply": shown, "skills_saved": [], "chat": public, "context": stats}
    chat["messages"].append(
        {
            "id": new_id(),
            "role": "assistant",
            "content": shown,
            "error": True,
            "created_at": now_iso(),
        }
    )
    stats = _persist_context(store, chat)
    chat["updated_at"] = now_iso()
    _apply_run(chat, status="error", reason=shown, step="")
    _save_chat_final(store, chat)
    poke(store, "message")
    if len(chat.get("messages") or []) < before:
        raise HTTPException(500, "Recording a search error shortened the chat.")
    public = _public_chat(store, chat)
    public["context"] = stats
    return {"reply": shown, "skills_saved": [], "chat": public, "context": stats}


async def _finish_and_maybe_ask(
    store: Store,
    bot_id: str,
    chat_id: str,
    visible: str,
    saved: list[str],
    choices: list[str] | None = None,
    made: str = "",
    thinking: str = "",
    quiet: bool = False,
    check: str = "",
    lesson: str = "",
    thought_seconds: int = 0,
) -> dict:
    """Save the parent reply, then run at most one child ask found in it."""
    ask = parse_subagent(visible)
    visible = strip_subagent_fences(visible)
    if ask and not visible.strip():
        visible = f"Asked {ask[0]} to do one task."
    finished = _finish_reply(
        store,
        bot_id,
        chat_id,
        visible,
        saved,
        choices or [],
        made,
        thinking,
        quiet=quiet,
        check=check,
        lesson=lesson,
        thought_seconds=thought_seconds,
    )
    if not ask:
        return finished
    if len(ask[1]) > 8000:
        return _append_parent_result(
            store,
            bot_id,
            chat_id,
            "That task is longer than 8000 characters. Nothing was sent.",
            speaker=ask[0],
            failed=True,
            child_chat_id=None,
        )
    return await _ask_named(store, bot_id, chat_id, ask[0], ask[1])


async def _ask_bot_id(store: Store, parent_id: str, parent_chat_id: str, child_id: str, task: str) -> dict:
    parent = store.get_bot(parent_id)
    store.get_chat(parent_id, parent_chat_id)
    if child_id == parent["id"]:
        raise HTTPException(400, "A bot cannot ask itself. Pick another bot that already exists.")
    try:
        child = store.get_bot(child_id)
    except StoreError:
        raise HTTPException(404, "That bot does not exist. No bot was created.") from None
    return await _run_child(store, parent_id, parent_chat_id, child, task)


async def _ask_named(store: Store, parent_id: str, parent_chat_id: str, child_name: str, task: str) -> dict:
    matches = [
        bot for bot in store.list_bots() if bot.get("name") == child_name and bot["id"] != parent_id
    ]
    if len(matches) != 1:
        return _append_parent_result(
            store,
            parent_id,
            parent_chat_id,
            f"No single existing bot is named {child_name}. No bot was created.",
            speaker=child_name,
            failed=True,
            child_chat_id=None,
        )
    return await _run_child(store, parent_id, parent_chat_id, matches[0], task)


async def _run_child(store: Store, parent_id: str, parent_chat_id: str, child: dict, task: str) -> dict:
    """Write the task on the child's own chat, then append a short result to the parent."""
    parent_count = len(store.get_chat(parent_id, parent_chat_id).get("messages") or [])
    child_chat = store.create_chat(child["id"])
    child_chat["title"] = make_title(task, store)
    child_chat["messages"].append(
        {"id": new_id(), "role": "user", "content": task, "created_at": now_iso()}
    )
    child_chat["updated_at"] = now_iso()
    _persist_context(store, child_chat)
    store.save_chat(child_chat)

    error_text = None
    reply = ""
    try:
        endpoint = store.get_endpoint(child["endpoint_id"])
    except StoreError:
        endpoint = None
    if endpoint is None:
        error_text = f"{child['name']}'s endpoint is missing."
    else:
        conn = gate.bind_connection(endpoint, child.get("name") or "")
        try:
            try:
                reply = await llm.complete(
                    base_url=endpoint["base_url"],
                    api_key=endpoint.get("api_key") or None,
                    model=chosen_model(child, endpoint),
                    messages=child_messages(child["name"], store.read_direction(), task),
                )
            except llm.ProviderError as exc:
                error_text = llm.connection_error_text(str(exc), endpoint, chosen_model(child, endpoint), child["name"])
        finally:
            gate.reset_connection(conn)

    child_chat = store.get_chat(child["id"], child_chat["id"])
    if len(child_chat.get("messages") or []) < 1:
        raise HTTPException(500, "The child task was dropped before the endpoint returned.")
    if error_text:
        child_chat["messages"].append(
            {
                "id": new_id(),
                "role": "assistant",
                "content": error_text,
                "error": True,
                "created_at": now_iso(),
            }
        )
        parent_content = error_line(child["name"], error_text)
        failed = True
    else:
        stored_reply = reply if len(reply) <= MAX_STORED_MESSAGE_CHARS else reply[:MAX_STORED_MESSAGE_CHARS]
        child_chat["messages"].append(
            {"id": new_id(), "role": "assistant", "content": stored_reply, "created_at": now_iso()}
        )
        parent_content = short_result(task, stored_reply)
        failed = False
    child_chat["updated_at"] = now_iso()
    _persist_context(store, child_chat)
    store.save_chat(child_chat)
    return _append_parent_result(
        store,
        parent_id,
        parent_chat_id,
        parent_content,
        speaker=child["name"],
        failed=failed,
        child_chat_id=child_chat["id"],
        before=parent_count,
    )


def _append_parent_result(
    store: Store,
    parent_id: str,
    parent_chat_id: str,
    content: str,
    *,
    speaker: str,
    failed: bool,
    child_chat_id: str | None,
    before: int | None = None,
) -> dict:
    chat = store.get_chat(parent_id, parent_chat_id)
    prior = len(chat.get("messages") or [])
    if before is not None and prior < before:
        raise HTTPException(500, "Asking a bot shortened the parent chat.")
    message = {
        "id": new_id(),
        "role": "assistant",
        "content": content,
        "speaker_name": speaker,
        "subagent": True,
        "created_at": now_iso(),
    }
    if child_chat_id:
        message["child_chat_id"] = child_chat_id
    if failed:
        message["error"] = True
    chat["messages"].append(message)
    stats = _persist_context(store, chat)
    chat["updated_at"] = now_iso()
    store.save_chat(chat)
    if len(chat.get("messages") or []) < prior:
        raise HTTPException(500, "Asking a bot shortened the parent chat.")
    public = _public_chat(store, chat)
    public["context"] = stats
    return {"reply": content, "chat": public, "context": stats, "child_chat_id": child_chat_id}


def _preview_attachment(store: Store, bot_id: str, chat_id: str, made: str, visible: str) -> dict | None:
    """A preview only for a file this turn wrote and verified. A failed write attaches nothing."""
    if not made:
        return None
    lowered = (visible or "").strip().lower()
    if lowered.startswith("the file was not written") or lowered.startswith("write failed"):
        return None
    try:
        payload = json.loads(made)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_path = str(payload.get("path") or "")
    path = Path(raw_path)
    try:
        ready = path.is_file() and path.stat().st_size > 0
        resolved = path.resolve()
    except OSError:
        return None
    if not ready:
        return None
    root = store.root.resolve()
    if resolved == root or root in resolved.parents:
        return None
    try:
        data = resolved.read_bytes()
    except OSError:
        return None
    if not data or len(data) > 1_000_000 or contains_secret(store, data):
        return None
    media = str(payload.get("media_type") or "application/octet-stream")
    excerpt = str(payload.get("excerpt") or "")
    if len(excerpt) > 280:
        excerpt = excerpt[:277].rstrip() + "…"
    try:
        meta = store.save_chat_file(
            bot_id,
            chat_id,
            name=resolved.name,
            media_type=media,
            data=data,
            source=str(resolved),
        )
    except StoreError:
        return None
    return _public_attachment({**meta, "excerpt": excerpt, "path": str(resolved)})


def _finish_reply(
    store: Store,
    bot_id: str,
    chat_id: str,
    visible: str,
    saved: list[str],
    choices: list[str] | None = None,
    made: str = "",
    thinking: str = "",
    quiet: bool = False,
    check: str = "",
    lesson: str = "",
    thought_seconds: int = 0,
) -> dict:
    chat = store.get_chat(bot_id, chat_id)
    visible = redact(store, visible)
    visible = keep_lessons(store, bot_id, visible)
    visible, handed = take_file(visible)
    attachment = _preview_attachment(store, bot_id, chat_id, made, visible)
    if attachment is None and handed:
        body = redact(store, handed["body"])
        data = body.encode("utf-8")
        if body.strip() and not contains_secret(store, data):
            try:
                meta = store.save_chat_file(
                    bot_id,
                    chat_id,
                    name=handed["name"],
                    media_type="text/plain",
                    data=data,
                )
            except StoreError:
                meta = None
            if meta:
                attachment = _public_attachment(meta)
                if not visible.strip():
                    visible = f"Attached {handed['name']}."
    kept = []
    for choice in choices or []:
        cleaned = " ".join(redact(store, choice).split())
        if cleaned and cleaned not in kept:
            kept.append(cleaned)
    if not visible.strip() and not kept and not saved and quiet:
        chat = store.get_chat(bot_id, chat_id)
        messages = chat.setdefault("messages", [])
        if messages and messages[-1].get("role") == "assistant" and messages[-1].get("live"):
            blank = messages[-1]
            thought = redact(store, (thinking or blank.get("thinking") or "")).strip()
            if thought:
                blank["content"] = ""
                blank["thinking"] = thought
                if int(thought_seconds or 0) > 0:
                    blank["thought_seconds"] = int(thought_seconds)
                blank.pop("live", None)
                blank.pop("error", None)
            else:
                messages.pop()
        _apply_run(chat, status="idle")
        store.save_chat(chat)
        public = _public_chat(store, chat)
        return {"reply": "", "skills_saved": saved, "chat": public, "context": public["context"]}
    blank_reply = False
    if not visible.strip() and not kept:
        visible = "(empty reply)"
        blank_reply = True
    messages = chat.setdefault("messages", [])
    replacing = bool(messages and messages[-1].get("live") and messages[-1].get("role") == "assistant")
    if replacing:
        message = messages[-1]
        message["content"] = visible
        message.pop("live", None)
        message["created_at"] = now_iso()
    else:
        message = {
            "id": new_id(),
            "role": "assistant",
            "content": visible,
            "created_at": now_iso(),
        }
        messages.append(message)
    if not (message.get("content") or "").strip() and not kept:
        message["content"] = _stopped_text("the model returned nothing")
        visible = message["content"]
        blank_reply = True
    if blank_reply or (message.get("content") or "").strip() == "(empty reply)":
        message["error"] = True
        blank_reply = True
    kept_thought = redact(store, (thinking or message.get("thinking") or "")).strip()
    if kept_thought:
        message["thinking"] = kept_thought
        if int(thought_seconds or 0) > 0:
            message["thought_seconds"] = int(thought_seconds)
    elif "thinking" in message:
        message.pop("thinking", None)
        message.pop("thought_seconds", None)
    if saved:
        message["skills_saved"] = saved
    if check in {"checked", "revised"}:
        message["check"] = check
    elif "check" in message:
        message.pop("check", None)
    if str(lesson or "").startswith("Learned:"):
        message["lesson"] = lesson
    elif "lesson" in message:
        message.pop("lesson", None)
    from easyagent.honesty import take_outcome

    outcome = take_outcome()
    if outcome is not None and outcome.receipts:
        message["receipts"] = [
            {
                "claim": redact(store, str(item.get("claim") or ""))[:80],
                "tool": redact(store, str(item.get("tool") or ""))[:180],
                "output": redact(store, str(item.get("output") or ""))[:240],
            }
            for item in outcome.receipts
            if isinstance(item, dict)
        ]
    elif "receipts" in message:
        message.pop("receipts", None)
    if outcome is not None and outcome.unverified:
        message["unverified"] = True
    elif "unverified" in message:
        message.pop("unverified", None)
    if len(kept) >= 2:
        message["choices"] = kept
    elif "choices" in message:
        message.pop("choices", None)
    if attachment:
        message["attachment"] = attachment
    stats = _persist_context(store, chat)
    chat["updated_at"] = now_iso()
    if blank_reply:
        _apply_run(chat, status="error", reason=message.get("content") or "", step="")
    else:
        _apply_run(chat, status="idle")
    _save_chat_final(store, chat)
    poke(store, "message")
    public = _public_chat(store, chat)
    public["context"] = stats
    return {"reply": visible, "skills_saved": saved, "chat": public, "context": stats}


def _sse_stopped(store: Store, bot_id: str, chat_id: str, detail: str) -> str:
    payload: dict = {"type": "stopped", "detail": detail}
    try:
        payload["chat"] = _public_chat(store, store.get_chat(bot_id, chat_id))
    except StoreError:
        pass
    return _sse(payload)


async def _reply_json(store: Store, bot_id: str, chat_id: str) -> dict:
    chat = store.get_chat(bot_id, chat_id)
    await _propose_if_corrected(store, bot_id, chat)
    _write_live(store, bot_id, chat_id, step="Waiting on model", start=True, force=True)
    try:
        visible, saved, choices, made, thinking, reacted, checked, lesson = await _complete_for_chat(store, bot_id, chat)
    except llm.ProviderError as exc:
        return _append_error(store, bot_id, chat_id, _chat_connection_error(store, bot_id, str(exc)))
    except SearchError as exc:
        return _append_error(store, bot_id, chat_id, str(exc))
    except ToolError as exc:
        return _append_error(store, bot_id, chat_id, str(exc))
    return await _finish_and_maybe_ask(
        store,
        bot_id,
        chat_id,
        visible,
        saved,
        choices,
        made,
        thinking,
        quiet=bool(reacted) and not (visible or "").strip(),
        check=checked,
        lesson=lesson,
    )


async def _stream_reply(store: Store, bot_id: str, chat_id: str):
    """Yield SSE for one reply. The caller holds the chat lock. The user turn is already stored."""
    bot = store.get_bot(bot_id)
    chat = store.get_chat(bot_id, chat_id)
    await _propose_if_corrected(store, bot_id, chat)
    chat = store.get_chat(bot_id, chat_id)
    endpoint, messages = _turn_messages(store, bot_id, chat)
    opened = _write_live(store, bot_id, chat_id, step="Waiting on model", start=True, force=True)
    if opened:
        yield _sse({"type": "run", "run": opened})
    yield _sse({"type": "status", "text": "Waiting on model"})
    conn = gate.bind_connection(endpoint, bot.get("name") or "")
    try:
        final = None
        choices: list[str] = []
        made = ""
        sent = ""
        thought = ""
        reacted = False
        checked = ""
        lesson = ""
        thought_saved = False
        thought_started: float | None = None
        thought_seconds = 0

        def note_thought() -> None:
            nonlocal thought_started
            if thought_started is None:
                thought_started = time.monotonic()

        def freeze_thought() -> None:
            nonlocal thought_seconds
            if thought_started is not None and thought_seconds <= 0:
                thought_seconds = max(1, int(round(time.monotonic() - thought_started)))

        async for kind, text in stream_with_tools(
            base_url=endpoint["base_url"],
            api_key=endpoint.get("api_key") or None,
            model=chosen_model(bot, endpoint),
            messages=messages,
            store=store,
            bot_id=bot_id,
            chat_id=chat_id,
        ):
            if kind == "stage":
                freeze_thought()
                _save_live_reply(store, bot_id, chat_id, text)
                extra = text[len(sent):] if sent and text.startswith(sent) else text
                sent = text
                if extra:
                    yield _sse({"type": "delta", "text": extra})
                continue
            if kind == "thinking":
                if text:
                    note_thought()
                    thought += text
                    saved_run = _write_live(
                        store,
                        bot_id,
                        chat_id,
                        thinking=thought,
                        step="Thinking",
                        force=not thought_saved,
                    )
                    thought_saved = True
                    yield _sse({"type": "thinking", "text": text})
                    if saved_run:
                        yield _sse({"type": "run", "run": saved_run})
                continue
            if kind == "status":
                saved_run = _write_live(store, bot_id, chat_id, step=text, force=True)
                yield _sse({"type": "status", "text": text})
                if saved_run:
                    yield _sse({"type": "run", "run": saved_run})
                continue
            if kind == "replay":
                thought = text or ""
                thought_saved = False
                _replace_live_thinking(store, bot_id, chat_id, thought)
                yield _sse({"type": "replay", "text": thought})
                continue
            if kind == "final":
                final = text
            elif kind == "check":
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = {}
                if isinstance(parsed, dict) and parsed.get("badge") in {"checked", "revised"}:
                    checked = parsed["badge"]
            elif kind == "lesson":
                if str(text or "").startswith("Learned:"):
                    lesson = str(text)
            elif kind == "choices":
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = []
                if isinstance(parsed, list):
                    choices = [item for item in parsed if isinstance(item, str)]
            elif kind == "made":
                made = text
            elif kind == "reacted":
                reacted = True
            else:
                yield _sse({"type": kind, "text": text})
        from easyagent.tools import finish_honesty

        final = await finish_honesty(
            final or "",
            base_url=endpoint["base_url"],
            api_key=endpoint.get("api_key") or None,
            model=chosen_model(bot, endpoint),
            messages=messages,
            store=store,
            bot_id=bot_id,
        )
        visible, skills = extract_skills(final or "")
        saved: list[str] = []
        for skill in skills:
            stored = _save_user_skill(store, skill)
            saved.append(stored["name"])
        if not visible.strip() and not choices and not reacted:
            visible = "Saved skill: " + ", ".join(saved) if saved else "(empty reply)"
        result = await _finish_and_maybe_ask(
            store,
            bot_id,
            chat_id,
            visible,
            saved,
            choices,
            made,
            thought,
            quiet=reacted and not visible.strip(),
            check=checked,
            lesson=lesson,
            thought_seconds=thought_seconds or (max(1, int(round(time.monotonic() - thought_started))) if thought_started is not None else 0),
        )
        yield _sse({"type": "done", "chat": result["chat"]})
    except llm.ProviderError as exc:
        if turn_mod.cancelled():
            reason = turn_mod.cancel_reason() or "you pressed Stop"
            _settle_interrupted(store, bot_id, chat_id, reason)
            yield _sse_stopped(store, bot_id, chat_id, _stopped_text(reason))
            return
        if _stopped_transport(str(exc)):
            reason = "the reply stopped before it finished."
            _settle_interrupted(store, bot_id, chat_id, reason)
            yield _sse_stopped(store, bot_id, chat_id, _stopped_text(reason))
            return
        detail = llm.connection_error_text(str(exc), endpoint, chosen_model(bot, endpoint), bot["name"])
        failed = _append_error(store, bot_id, chat_id, detail)
        yield _sse({"type": "error", "detail": failed["reply"], "chat": failed["chat"]})
    except SearchError as exc:
        failed = _append_error(store, bot_id, chat_id, str(exc))
        yield _sse({"type": "error", "detail": failed["reply"], "chat": failed["chat"]})
    except turn_mod.TurnCancelled:
        reason = turn_mod.cancel_reason() or "the reply stopped before it finished."
        _settle_interrupted(store, bot_id, chat_id, reason)
        yield _sse_stopped(store, bot_id, chat_id, _stopped_text(reason))
        return
    except asyncio.CancelledError:
        reason = turn_mod.cancel_reason() or "the server restarted"
        _settle_interrupted(store, bot_id, chat_id, reason)
        raise
    except ToolError as exc:
        failed = _append_error(store, bot_id, chat_id, str(exc))
        yield _sse({"type": "error", "detail": failed["reply"], "chat": failed["chat"]})
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Could not save the reply."
        yield _sse({"type": "error", "detail": _stopped_text(detail)})
    except Exception as exc:
        _log.warning("Run failed: %s", exc)
        reason = str(exc).strip() or "the run failed before it finished"
        if len(reason) > 300:
            reason = reason[:297].rstrip() + "…"
        _settle_interrupted(store, bot_id, chat_id, reason)
        yield _sse_stopped(store, bot_id, chat_id, _stopped_text(reason))
    finally:
        gate.reset_connection(conn)
        if turn_mod.cancelled():
            _settle_interrupted(store, bot_id, chat_id, turn_mod.cancel_reason() or "you pressed Stop")


async def _emit_run(store: Store, bot_id: str, chat_id: str, producer):
    """Run the turn on its own task. Closing this response does not cancel that task.

    The page can leave the chat. The run keeps writing the chat file. Stop, or a
    new message in this same chat, is what cancels it.
    """
    subscribers: list[asyncio.Queue] = []
    guard = asyncio.Lock()

    async def publish(item) -> None:
        async with guard:
            targets = list(subscribers)
        for queue in targets:
            queue.put_nowait(item)

    async def worker() -> None:
        try:
            async with chat_lock(store, chat_id):
                turn_mod.bind(store, chat_id, bot_id=bot_id)
                print(
                    "heuristic isolation: this run belongs to one chat and keeps going if the viewer leaves",
                    flush=True,
                )
                try:
                    async for chunk in producer():
                        await publish(chunk)
                finally:
                    if turn_mod.cancelled():
                        reason = turn_mod.cancel_reason() or "you pressed Stop"
                        _settle_interrupted(store, bot_id, chat_id, reason)
        except asyncio.CancelledError:
            reason = turn_mod.cancel_reason() or "the server restarted"
            with suppress(Exception):
                _settle_interrupted(store, bot_id, chat_id, reason)
            raise
        except Exception as exc:
            _log.warning("Run failed: %s", exc)
            reason = str(exc).strip() or "the run failed before it finished"
            if len(reason) > 300:
                reason = reason[:297].rstrip() + "…"
            with suppress(Exception):
                _settle_interrupted(store, bot_id, chat_id, reason)
            with suppress(Exception):
                await publish(_sse_stopped(store, bot_id, chat_id, _stopped_text(reason)))
        finally:
            with suppress(Exception):
                await publish(None)

    queue: asyncio.Queue = asyncio.Queue()
    async with guard:
        subscribers.append(queue)
    task = asyncio.create_task(worker())
    turn_mod.track_worker(task)
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    except asyncio.CancelledError:
        async with guard:
            if queue in subscribers:
                subscribers.remove(queue)
        print(
            "heuristic stream-detach: the viewer left and this chat's run kept going",
            flush=True,
        )
        return


async def _stream_new_message(store: Store, bot_id: str, chat_id: str, content: str, attachment: dict | None = None):
    async def produce():
        message = _save_user_turn(store, bot_id, chat_id, content, attachment)
        # The user line goes out before the word scan. The scan runs inside the reply.
        yield _sse({"type": "user", "message": message})
        async for chunk in _stream_reply(store, bot_id, chat_id):
            yield chunk

    async for chunk in _emit_run(store, bot_id, chat_id, produce):
        yield chunk


async def _stream_retry(store: Store, bot_id: str, chat_id: str):
    async def produce():
        chat = store.get_chat(bot_id, chat_id)
        if not _can_retry(chat.get("messages") or []):
            yield _sse({"type": "error", "detail": "Nothing to retry. The last stored turn is not waiting on a reply."})
            return
        async for chunk in _stream_reply(store, bot_id, chat_id):
            yield chunk

    async for chunk in _emit_run(store, bot_id, chat_id, produce):
        yield chunk
