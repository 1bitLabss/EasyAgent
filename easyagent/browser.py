"""This bot's browser. It is never the browser the person uses themselves.

Playwright Chromium is installed on first use. Each bot has its own profile
under its data folder. Pages are data. Passwords, card numbers, and 2FA codes
are not typed. Downloads land in this bot's workspace and are not opened.
"""

from __future__ import annotations

import base64
import contextvars
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from easyagent.store import Store, atomic_write_json, new_id, now_iso, read_json

_VISION: contextvars.ContextVar[str] = contextvars.ContextVar("ea_browser_shot", default="")
_READY = False
_READY_LOCK = threading.Lock()
_OPEN: set[str] = set()
_OPEN_LOCK = threading.Lock()

_REAL = re.compile(
    r"(?i)(chrome[\\/]+user data|chromium[\\/]+user data|google[\\/]+chrome|"
    r"microsoft[\\/]+edge|firefox[\\/]+profiles|cookies\.binarycookies|"
    r"login data|web data|cookies\.sqlite|saved passwords|password-manager)"
)
_SECRET_NAME = re.compile(
    r"(?i)password|passwd|secret|cvv|cvc|csc|card.?number|credit.?card|"
    r"cc-number|one-time|otp|2fa|pin code"
)
_SECRET_AUTO = re.compile(r"(?i)current-password|new-password|cc-number|cc-csc|one-time-code")
_PAY = re.compile(r"(?i)\b(place order|pay now|pay|buy|purchase|checkout|order now|confirm purchase)\b")
_LOGIN = re.compile(r"(?i)\b(log ?in|sign ?in|sign ?on)\b")
_POST = re.compile(r"(?i)\b(post|publish|send|reply|comment|tweet|submit post)\b")
_SETTINGS = re.compile(r"(?i)\b(save settings|update account|delete account|change password|account settings)\b")
_SUBMIT = re.compile(r"(?i)\bsubmit\b|type=submit")
_VISION_MODEL = re.compile(r"(?i)vision|llava|pixtral|qwen.{0,8}vl|gpt-4o|gemini|minicpm-v|moondream")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")

_STAMP = """
() => {
  const sel = 'a, button, input, textarea, select, summary, [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="combobox"], [role="textbox"]';
  const nodes = [];
  document.querySelectorAll('[data-ea-n]').forEach((el) => el.removeAttribute('data-ea-n'));
  for (const el of document.querySelectorAll(sel)) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    let name = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
    if (!name && el.labels && el.labels[0]) name = el.labels[0].innerText || '';
    if (!name) name = (el.innerText || el.getAttribute('name') || '').trim();
    name = name.replace(/\\s+/g, ' ').slice(0, 80);
    const n = nodes.length + 1;
    el.setAttribute('data-ea-n', String(n));
    nodes.push({
      n,
      tag,
      type,
      name,
      auto: (el.getAttribute('autocomplete') || '').toLowerCase(),
      role: el.getAttribute('role') || tag,
      value: ('value' in el ? String(el.value || '') : '').slice(0, 120),
      href: el.getAttribute('href') || '',
    });
    if (nodes.length >= 80) break;
  }
  return nodes;
}
"""


def note_vision_shot(url: str) -> None:
    _VISION.set(url or "")


def take_vision_shot() -> str:
    value = _VISION.get()
    _VISION.set("")
    return value


def model_has_vision(store: Store, bot_id: str | None) -> bool:
    if not bot_id:
        return False
    try:
        bot = store.get_bot(bot_id)
    except Exception:
        return False
    if bot.get("browser_vision") is True:
        return True
    names = [str(bot.get("model") or "")]
    try:
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except Exception:
        endpoint = None
    if endpoint:
        names.append(str(endpoint.get("model") or ""))
    return any(_VISION_MODEL.search(name or "") for name in names)


def clean_host(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/")[0].split("@")[-1]
    if text.startswith("["):
        return ""
    text = text.split(":")[0].strip().strip(".")
    if not text or not _HOST.match(text) or ".." in text:
        return ""
    return text


def clean_hosts(values: list[str] | None) -> list[str]:
    found: list[str] = []
    for value in values or []:
        host = clean_host(str(value))
        if host and host not in found:
            found.append(host)
        if len(found) >= 40:
            break
    return found


def _is_real_browser_path(text: str) -> bool:
    return bool(_REAL.search(text or ""))


def profile_dir(store: Store, bot_id: str) -> Path:
    bot = store.get_bot(bot_id)
    path = (store._bot_dir(bot["id"]) / "browser-profile").resolve()
    root = store._bot_dir(bot["id"]).resolve()
    if root not in path.parents and path != root:
        from easyagent.tools import ToolError

        raise ToolError("The browser profile is outside this bot.")
    if _is_real_browser_path(str(path)):
        from easyagent.tools import ToolError

        raise ToolError("EasyAgent will not use the browser you use yourself. It was not opened.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def download_dir(store: Store, bot_id: str) -> Path:
    """Downloads land in this bot's workspace. They are not opened."""
    from easyagent.workspace import bot_workspace

    path = bot_workspace(store, bot_id, create=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _download_index(store: Store, bot_id: str) -> Path:
    return store._bot_dir(store.get_bot(bot_id)["id"]) / "browser-downloads.json"


def note_download(store: Store, bot_id: str, dest: Path) -> None:
    path = _download_index(store, bot_id)
    rows = read_json(path) if path.is_file() else []
    if not isinstance(rows, list):
        rows = []
    rows.append({"path": str(dest), "name": dest.name, "at": now_iso(), "opened": False})
    atomic_write_json(path, rows[-100:])


def download_paths(store: Store, bot_id: str | None) -> list[str]:
    if not bot_id:
        return []
    try:
        path = _download_index(store, bot_id)
    except Exception:
        return []
    rows = read_json(path) if path.is_file() else []
    if not isinstance(rows, list):
        return []
    found = []
    for row in rows:
        if isinstance(row, dict) and row.get("path"):
            found.append(str(row["path"]))
    return found


def command_runs_download(store: Store, bot_id: str | None, command: str) -> bool:
    """Running a downloaded file waits. Reading it does not."""
    text = command or ""
    lowered = text.replace("\\", "/").lower()
    if not lowered.strip():
        return False
    reading = bool(re.search(r"(?i)\b(cat|type|get-content|gc|less|more|head|tail|select-string)\b", text))
    launching = bool(re.search(r"(?i)(^|[|&;]|&&|\|\|)\s*(\./|bash|sh|python|pwsh|powershell|cmd|start|chmod\s+\+x)\b", text))
    if reading and not launching:
        return False
    for item in download_paths(store, bot_id):
        full = item.replace("\\", "/").lower()
        name = Path(item).name.lower()
        if len(name) < 8:
            continue
        if full in lowered or name in lowered:
            return True
    return False


def names_data_dir(store: Store | None, text: str) -> bool:
    """A browser target that names the data folder is refused."""
    if store is None:
        return False
    raw = unquote(text or "").replace("\\", "/")
    if not raw.strip():
        return False
    try:
        root = str(Path(store.root).resolve()).replace("\\", "/")
    except OSError:
        root = str(Path(store.root)).replace("\\", "/")
    if root and root in raw:
        return True
    return False


def shot_path(store: Store, bot_id: str) -> Path:
    bot = store.get_bot(bot_id)
    return store._bot_dir(bot["id"]) / "browser-live.png"


def _live_path(store: Store, bot_id: str) -> Path:
    bot = store.get_bot(bot_id)
    return store._bot_dir(bot["id"]) / "browser-live.json"


def wants_headless(bot: dict | None) -> bool:
    if (os.environ.get("EASYAGENT_BROWSER_HEADLESS") or "").strip() == "1":
        return True
    return bool((bot or {}).get("browser_headless"))


def _host_matches(rule: str, host: str) -> bool:
    item = clean_host(rule)
    if not item or not host:
        return False
    return host == item or host.endswith("." + item)


def domain_block(host: str, allow: list[str], deny: list[str]) -> str | None:
    for item in deny:
        if _host_matches(item, host):
            return "browser-deny"
    if allow and not any(_host_matches(item, host) for item in allow):
        return "browser-allow"
    return None


def _meta(request) -> dict:
    raw = getattr(request, "call_arguments", "") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _detail(url: str, element: str, values: str) -> str:
    lines = [url or "(no page)"]
    if element:
        lines.append("element: " + element)
    if values:
        lines.append("values: " + values)
    return "\n".join(lines)


def _secret_field(element: dict) -> bool:
    if (element.get("type") or "") == "password":
        return True
    if _SECRET_AUTO.search(element.get("auto") or ""):
        return True
    if _SECRET_NAME.search(element.get("name") or ""):
        return True
    return False


def looks_like_secret_value(value: str, element: dict | None = None) -> bool:
    if element and _secret_field(element):
        return True
    raw = (value or "").strip()
    digits = re.sub(r"[\s-]", "", raw)
    if re.fullmatch(r"\d{13,19}", digits) and raw[:1].isdigit():
        return True
    name = (element or {}).get("name") or ""
    if re.search(r"(?i)otp|2fa|code|pin", name) and re.fullmatch(r"\d{6,8}", raw):
        return True
    return False


def _click_rule(element: str, url: str) -> str | None:
    blob = f"{element} {url}"
    if _PAY.search(blob):
        return "browser-pay"
    if _LOGIN.search(element or ""):
        return "browser-login"
    if _SETTINGS.search(blob):
        return "browser-settings"
    if _POST.search(element or ""):
        return "browser-post"
    if _SUBMIT.search(element or ""):
        return "browser-submit"
    if re.search(r"(?i)/account|/settings", url or "") and re.search(r"(?i)\b(save|update|change|delete)\b", element or ""):
        return "browser-settings"
    return None


def judge_browser(request, *, allow: list[str] | None = None, deny: list[str] | None = None, store: Store | None = None):
    """Allow, ask, or block one browser action. The caller is the safety engine."""
    from easyagent.safety import ASK, BLOCK, ALLOW, _verdict, _fp

    action = getattr(request, "action", "") or "open"
    meta = _meta(request)
    url = (getattr(request, "path", "") or meta.get("url") or "").strip()
    element = str(meta.get("element") or "")
    values = str(meta.get("values") or "")
    secret = bool(meta.get("secret"))
    detail = _detail(url, element, values)
    fingerprint = _fp("browser", action, url, element[:80])
    blob = " ".join((url, element, getattr(request, "command", "") or "", getattr(request, "body", "") or ""))
    if names_data_dir(store, blob):
        return _verdict(BLOCK, "data-dir", "That reaches EasyAgent's saved data. It was not opened.", detail, fingerprint)
    if _is_real_browser_path(blob):
        return _verdict(BLOCK, "real-browser", "That would open the browser you use yourself. This bot has its own browser, and that was not opened.", detail, fingerprint)
    parsed = urlparse(url) if url else None
    scheme = (parsed.scheme if parsed else "").lower()
    if action == "open" or (url and scheme and scheme not in {"http", "https"}):
        if scheme in {"javascript", "file", "chrome", "edge", "brave", "opera", "about", "data", "view-source"}:
            return _verdict(BLOCK, "browser-scheme", "The browser only opens ordinary web pages. It was not opened.", detail, fingerprint)
        if action == "open" and scheme not in {"http", "https"}:
            return _verdict(BLOCK, "browser-scheme", "The browser only opens ordinary web pages. It was not opened.", detail, fingerprint)
    host = (parsed.hostname or "").lower() if parsed else ""
    if host:
        blocked = domain_block(host, list(allow or []), list(deny or []))
        if blocked == "browser-deny":
            return _verdict(BLOCK, "browser-deny", "That site is on this bot's deny list. It was not opened.", detail, fingerprint)
        if blocked == "browser-allow":
            return _verdict(BLOCK, "browser-allow", "That site is not on this bot's allow list. It was not opened.", detail, fingerprint)
    meta_rule = str(meta.get("rule") or "")
    if meta_rule == "browser-deny":
        return _verdict(BLOCK, "browser-deny", "That site is on this bot's deny list. It was not opened.", detail, fingerprint)
    if meta_rule == "browser-allow":
        return _verdict(BLOCK, "browser-allow", "That site is not on this bot's allow list. It was not opened.", detail, fingerprint)
    if meta_rule == "browser-scheme":
        return _verdict(BLOCK, "browser-scheme", "The browser only opens ordinary web pages. It was not opened.", detail, fingerprint)
    if secret or meta_rule == "browser-secret" or (action == "type" and meta.get("secret")):
        return _verdict(
            ASK,
            "browser-secret",
            "EasyAgent will not type a password, a card number, or a 2FA code.",
            _detail(url, element, ""),
            fingerprint,
        )
    rule = meta_rule if meta_rule.startswith("browser-") else None
    if action in {"click", "select"} and not rule:
        rule = _click_rule(element, url)
    if action == "type" and not secret:
        rule = rule if rule in {"browser-settings"} else None
    why = {
        "browser-pay": "This pays or places an order. It waits for you.",
        "browser-login": "This logs in. It waits for you.",
        "browser-post": "This posts or sends. It waits for you.",
        "browser-settings": "This changes account settings. It waits for you.",
        "browser-submit": "This submits a form. It waits for you.",
    }
    if rule in why:
        return _verdict(ASK, rule, why[rule], detail, fingerprint)
    return _verdict(ALLOW, "browser", "That only reads or moves this bot's browser.", detail, _fp("browser", action, url[:120]))


def card_proposal(request, rule: str) -> dict | None:
    if not str(rule).startswith("browser-"):
        return None
    meta = _meta(request)
    url = getattr(request, "path", "") or meta.get("url") or ""
    element = str(meta.get("element") or "")
    if rule == "browser-secret":
        return {
            "kind": "takeover",
            "url": url,
            "element": element,
            "hint": "EasyAgent will not type a password, a card number, or a 2FA code. Type it in this bot's browser window, then click Done.",
        }
    return {
        "kind": "browser",
        "url": url,
        "element": element,
        "values": str(meta.get("values") or ""),
    }


def _describe(element: dict) -> str:
    """One numbered ref. Empty values are left out. A secret is marked, not copied."""
    role = str(element.get("role") or element.get("tag") or "element").strip() or "element"
    name = " ".join(str(element.get("name") or "").split())
    if len(name) > 60:
        name = name[:57].rstrip() + "..."
    parts = [f"[{element['n']}]", role]
    if name:
        parts.append(f'"{name}"')
    kind = str(element.get("type") or "")
    if kind == "password":
        parts.append("password")
    if _secret_field(element):
        parts.append("secret")
    elif kind == "submit":
        parts.append("submit")
    href = str(element.get("href") or "").strip()
    if href and not href.lower().startswith("javascript"):
        parts.append(href[:80])
    value = " ".join(str(element.get("value") or "").split())
    if value and not _secret_field(element):
        parts.append("value=" + value[:40])
    return " ".join(parts)


def _values_line(elements: list[dict]) -> str:
    parts = []
    for element in elements:
        if _secret_field(element):
            continue
        value = (element.get("value") or "").strip()
        if not value:
            continue
        label = element.get("name") or f"[{element['n']}]"
        parts.append(f"{label}={value[:80]}")
    return ", ".join(parts[:12])


def _pack(request, *, url: str, element: str = "", values: str = "", secret: bool = False, rule: str | None = None):
    from easyagent.tools import ToolRequest

    meta = {
        "url": url,
        "element": element,
        "values": "" if secret else values,
        "secret": secret,
        "rule": rule or "",
    }
    return ToolRequest(
        kind="browser",
        action=request.action,
        path=url,
        command="" if secret else (request.command or ""),
        body="" if secret else (request.body or ""),
        call_arguments=json.dumps(meta),
    )


class _Hub:
    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="easyagent-browser", daemon=True)
        self._started = False
        self._start_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            ensure_chromium()
            self._started = True
            self._thread.start()

    def call(self, fn, timeout: float = 40):
        from easyagent.tools import ToolError

        self.start()
        done: queue.Queue = queue.Queue(1)
        self._queue.put((fn, done))
        try:
            ok, payload = done.get(timeout=timeout)
        except queue.Empty as exc:
            raise ToolError("The browser took too long and was stopped.") from exc
        if not ok:
            if isinstance(payload, ToolError):
                raise payload
            raise ToolError(str(payload)) from payload
        return payload

    def _loop(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            sessions: dict[str, dict] = {}
            while True:
                fn, done = self._queue.get()
                try:
                    done.put((True, fn(playwright, sessions)))
                except Exception as exc:
                    done.put((False, exc))


_HUB = _Hub()
_WRAP_NOTE: dict[str, str] = {}


INSTALL_LABEL = "Install browser (~700 MB)"


def chromium_present() -> bool:
    """True when Chromium is already on disk. This does not download it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            path = Path(playwright.chromium.executable_path)
    except Exception:
        return False
    return path.is_file()


def install_status() -> dict:
    return {"installed": chromium_present(), "label": INSTALL_LABEL}


def install_browser() -> dict:
    """The Settings button. Startup does not call this."""
    ensure_chromium()
    return install_status()


def ensure_chromium() -> None:
    """Install the Playwright library and Chromium the first time a browser opens."""
    global _READY
    with _READY_LOCK:
        if _READY:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright>=1.40"])
            from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
                browser.close()
            except Exception:
                subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        _READY = True


def _mark_open(bot_id: str, open_: bool) -> None:
    with _OPEN_LOCK:
        if open_:
            _OPEN.add(bot_id)
        else:
            _OPEN.discard(bot_id)


def is_open(bot_id: str) -> bool:
    with _OPEN_LOCK:
        return bot_id in _OPEN


def _write_live(store: Store, bot_id: str, *, url: str, title: str, headless: bool, active: bool) -> None:
    payload = {
        "active": active,
        "url": url,
        "title": title,
        "headless": headless,
        "updated": now_iso(),
    }
    atomic_write_json(_live_path(store, bot_id), payload)


def snapshot(store: Store, bot_id: str) -> dict:
    path = _live_path(store, bot_id)
    data = read_json(path) if path.is_file() else {}
    if not isinstance(data, dict):
        data = {}
    active = is_open(bot_id)
    return {
        "active": active,
        "url": data.get("url") or "",
        "title": data.get("title") or "",
        "headless": bool(data.get("headless")),
        "updated": data.get("updated") or "",
    }


def _containment_notice(store: Store, bot_id: str) -> str:
    from easyagent.sandbox import shell_mode

    wrapped = _WRAP_NOTE.get(bot_id) or ""
    if wrapped:
        return wrapped
    mode = shell_mode(store)
    if mode.get("contained"):
        return ""
    if mode.get("status") == "unavailable" and mode.get("notice"):
        return str(mode["notice"])
    return ""


def _launch_kwargs(playwright, store: Store, bot_id: str, folder: Path, headless: bool) -> dict:
    """Chromium uses the same container as the shell when containment is on."""
    args = ["--disable-dev-shm-usage"]
    kwargs = {
        "headless": headless,
        "viewport": {"width": 1100, "height": 800},
        "accept_downloads": True,
        "args": args,
    }
    from easyagent.sandbox import shell_mode

    mode = shell_mode(store)
    if not mode.get("contained"):
        return kwargs
    try:
        real = str(playwright.chromium.executable_path)
    except Exception:
        return kwargs
    wrapper = _write_containment_wrapper(store, bot_id, real, folder)
    if not wrapper:
        _WRAP_NOTE[bot_id] = (
            "OS containment is unavailable: this browser could not be wrapped in the container. "
            "This browser used the file guards."
        )
        return kwargs
    _WRAP_NOTE.pop(bot_id, None)
    args.extend(["--no-sandbox", "--disable-setuid-sandbox"])
    kwargs["executable_path"] = wrapper
    return kwargs


def _write_containment_wrapper(store: Store, bot_id: str, real: str, profile: Path) -> str:
    from easyagent.sandbox import shell_mode
    from easyagent.workspace import bot_workspace

    mode = shell_mode(store)
    if not mode.get("contained"):
        return ""
    workspace = bot_workspace(store, bot_id, create=True)
    mechanism = mode.get("mechanism") or ""
    script = store._bot_dir(bot_id) / "browser-launch.sh"
    if mechanism == "bubblewrap" and shutil.which("bwrap"):
        prefix = browser_bwrap_prefix(store, bot_id, profile, workspace)
        quoted = " ".join(_sh_quote(part) for part in prefix)
        body = f"#!/bin/sh\nexec {quoted} {_sh_quote(real)} \"$@\"\n"
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
        return str(script)
    if mechanism == "landlock":
        return _write_landlock_wrapper(store, bot_id, real, profile, workspace)
    if mechanism == "sandbox-exec" and shutil.which("sandbox-exec"):
        from easyagent.sandbox import macos_profile

        profile_text = macos_profile([profile, workspace], Path(store.root))
        sb = store._bot_dir(bot_id) / "browser.sb"
        sb.write_text(profile_text, encoding="utf-8")
        body = f"#!/bin/sh\nexec sandbox-exec -f {_sh_quote(str(sb))} {_sh_quote(real)} \"$@\"\n"
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
        return str(script)
    return ""


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def browser_bwrap_prefix(store: Store, bot_id: str, profile: Path, workspace: Path) -> list[str]:
    """Bubblewrap prefix for Chromium. The data directory is hidden; the profile and workspace stay."""
    from easyagent.sandbox import web_enabled
    from easyagent.secrets import keychain_paths

    empty = Path(tempfile.gettempdir()) / "easyagent-seal-empty"
    empty.mkdir(parents=True, exist_ok=True)
    argv = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/"]
    if not web_enabled():
        argv.append("--unshare-net")
    tmp = Path(tempfile.gettempdir())
    argv += ["--bind", str(tmp), str(tmp)]
    try:
        root = Path(store.root).resolve()
    except OSError:
        root = Path(store.root)
    # After /tmp, so a data folder that lives under /tmp stays hidden.
    argv += ["--bind", str(empty), str(root)]
    for folder in (profile, workspace):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        argv += ["--bind", str(folder), str(folder)]
    shm = Path("/dev/shm")
    if shm.exists():
        argv += ["--bind", "/dev/shm", "/dev/shm"]
    for path in keychain_paths():
        if not path.exists():
            continue
        if path.is_dir():
            argv += ["--ro-bind", str(empty), str(path)]
        else:
            argv += ["--ro-bind", "/dev/null", str(path)]
    argv += ["--dev", "/dev", "--proc", "/proc", "--"]
    return argv


def _write_landlock_wrapper(store: Store, bot_id: str, real: str, profile: Path, workspace: Path) -> str:
    script = store._bot_dir(bot_id) / "browser-launch.py"
    root = str(Path(store.root))
    chrome_dir = str(Path(real).resolve().parent)
    body = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from easyagent.sandbox import apply_landlock\n"
        "from easyagent.store import Store\n"
        f"store = Store({root!r})\n"
        f"apply_landlock(store, {bot_id!r}, extra_write=[Path({str(profile)!r}), Path({str(workspace)!r})], "
        f"extra_read=[Path({chrome_dir!r})])\n"
        f"os.execv({real!r}, [{real!r}, *sys.argv[1:]])\n"
    )
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _session(playwright, sessions: dict, store: Store, bot_id: str) -> dict:
    from easyagent.tools import ToolError

    bot = store.get_bot(bot_id)
    headless = wants_headless(bot)
    current = sessions.get(bot_id)
    if current and current.get("headless") == headless:
        return current
    if current:
        _close_session(sessions, bot_id)
    folder = profile_dir(store, bot_id)
    launch = _launch_kwargs(playwright, store, bot_id, folder, headless)
    try:
        context = playwright.chromium.launch_persistent_context(str(folder), **launch)
    except Exception as exc:
        message = str(exc)
        if "display" in message.lower() or "x server" in message.lower() or "missing" in message.lower():
            raise ToolError(
                "This computer has no display, so the browser window could not open. "
                "Turn on headless in this bot's settings, or start EasyAgent with EASYAGENT_BROWSER_HEADLESS=1."
            ) from exc
        raise ToolError("The browser could not start. " + message[:240]) from exc
    page = context.pages[0] if context.pages else context.new_page()
    sessions[bot_id] = {"context": context, "page": page, "headless": headless, "store": store}
    _mark_open(bot_id, True)
    return sessions[bot_id]


def _close_session(sessions: dict, bot_id: str) -> None:
    session = sessions.pop(bot_id, None)
    _mark_open(bot_id, False)
    if not session:
        return
    try:
        session["context"].close()
    except Exception:
        return


def _page(session: dict):
    page = session.get("page")
    context = session["context"]
    if page is None or page.is_closed():
        page = context.pages[0] if context.pages else context.new_page()
        session["page"] = page
    return page


def _elements(page) -> list[dict]:
    raw = page.evaluate(_STAMP)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("n")]


def _pick(elements: list[dict], command: str) -> dict:
    from easyagent.tools import ToolError

    match = re.search(r"\d+", command or "")
    if not match:
        raise ToolError("Name the element by its number. Read the page first.")
    number = int(match.group(0))
    for element in elements:
        if int(element["n"]) == number:
            return element
    raise ToolError(f"There is no element {number} on this page. Read the page again.")


def _snapshot_text(page, elements: list[dict]) -> str:
    title = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    lines = [f"Page: {title}" if title else "Page", f"URL: {page.url}"]
    for element in elements:
        lines.append(_describe(element))
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    text = " ".join((text or "").split())
    if text:
        lines.append("Text: " + text[:400])
    return "\n".join(lines)


def _shoot(store: Store, bot_id: str, page, *, vision: bool) -> str:
    try:
        png = page.screenshot(type="png")
        shot_path(store, bot_id).write_bytes(png)
    except Exception:
        png = b""
    if not vision:
        return ""
    try:
        jpeg = page.screenshot(type="jpeg", quality=40)
    except Exception:
        jpeg = b""
    if not jpeg or len(jpeg) > 500_000:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def _save_download(store: Store, bot_id: str, download) -> str:
    folder = download_dir(store, bot_id)
    raw_name = Path(download.suggested_filename or "download").name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name)[:80] or "download"
    dest = folder / f"{new_id()[:8]}-{name}"
    download.save_as(str(dest))
    dest.chmod(0o644)
    note_download(store, bot_id, dest)
    return f"Saved {dest.name} in this bot's workspace. It was not opened."


def _target_url(page, element: dict, action: str, request) -> str:
    if action == "open":
        base = page.url if page is not None and page.url and page.url != "about:blank" else ""
        raw = (request.path or request.command or "").strip()
        if base and raw and "://" not in raw:
            return urljoin(base, raw)
        return raw
    href = (element or {}).get("href") or ""
    if href and page is not None:
        return urljoin(page.url or "", href)
    if page is not None:
        return page.url or ""
    return ""


def prepare(store: Store, request, bot_id: str | None):
    """Read the page and describe the action. Do not click, type, or navigate."""
    from easyagent.tools import ToolError

    if not bot_id:
        raise ToolError("This turn has no bot, so no browser was opened.")

    def work(playwright, sessions):
        action = request.action or "open"
        if action == "open":
            url = (request.path or request.command or "").strip()
            session = sessions.get(bot_id)
            page = _page(session) if session else None
            absolute = _target_url(page, {}, "open", request)
            return _pack(request, url=absolute or url)
        session = sessions.get(bot_id)
        if session is None:
            raise ToolError("Open a page first.")
        page = _page(session)
        elements = _elements(page)
        if action in {"read", "scroll", "back", "wait", "tabs"}:
            return _pack(request, url=page.url or "", values="")
        if action == "tabs" or (action == "tabs"):
            return _pack(request, url=page.url or "")
        element = {}
        if action in {"click", "type", "select", "download"}:
            element = _pick(elements, request.command)
        described = _describe(element) if element else ""
        secret = action == "type" and looks_like_secret_value(request.body or "", element)
        rule = _click_rule(described, page.url or "") if action in {"click", "select"} else None
        if action in {"type", "select"} and not secret and re.search(r"(?i)/account|/settings", page.url or ""):
            rule = "browser-settings"
        if action == "click" and element.get("href"):
            href = urljoin(page.url or "", element.get("href") or "")
            parsed = urlparse(href)
            if parsed.scheme and parsed.scheme not in {"http", "https"}:
                rule = "browser-scheme"
            else:
                host = (parsed.hostname or "").lower()
                bot = store.get_bot(bot_id)
                blocked = domain_block(host, list(bot.get("browser_allow") or []), list(bot.get("browser_deny") or []))
                if blocked and parsed.scheme in {"http", "https"}:
                    rule = blocked
        values = _values_line(elements) if rule or secret else ""
        packed = _pack(request, url=page.url or "", element=described, values=values, secret=secret, rule=rule)
        if secret:
            packed = _pack(
                request,
                url=page.url or "",
                element=described,
                values="",
                secret=True,
                rule="browser-secret",
            )
        return packed

    if (request.action or "open") == "open":
        url = (request.path or request.command or "").strip()
        return _pack(request, url=url)
    return _HUB.call(work)


def perform(store: Store, request, bot_id: str | None) -> tuple[str, str]:
    """Run an action the safety engine already allowed."""
    from easyagent.tools import ToolError

    if not bot_id:
        raise ToolError("This turn has no bot, so no browser was opened.")
    vision = model_has_vision(store, bot_id)

    def work(playwright, sessions):
        action = request.action or "open"
        session = _session(playwright, sessions, store, bot_id)
        page = _page(session)
        bot = store.get_bot(bot_id)
        headless = bool(session.get("headless"))
        if action == "open":
            url = (request.path or "").strip()
            if names_data_dir(store, url):
                raise ToolError("That reaches EasyAgent's saved data. It was not opened.")
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
        elif action == "back":
            page.go_back(wait_until="domcontentloaded", timeout=15000)
        elif action == "scroll":
            direction = (request.command or "down").strip().lower()
            amount = -700 if direction in {"up", "top"} else 700
            page.mouse.wheel(0, amount)
        elif action == "wait":
            raw = (request.command or "1").strip()
            try:
                seconds = float(raw)
                page.wait_for_timeout(int(min(30, max(0, seconds)) * 1000))
            except ValueError:
                page.get_by_text(raw, exact=False).first.wait_for(timeout=15000)
        elif action == "tabs":
            command = (request.command or "").strip()
            if command.lower().startswith("new"):
                fresh = session["context"].new_page()
                session["page"] = fresh
                rest = command.split(None, 1)
                if len(rest) == 2 and rest[1].strip():
                    fresh.goto(rest[1].strip(), wait_until="domcontentloaded", timeout=20000)
                page = fresh
            elif command:
                match = re.search(r"\d+", command)
                pages = session["context"].pages
                if not match:
                    raise ToolError("Name the tab by its number.")
                index = int(match.group(0)) - 1
                if index < 0 or index >= len(pages):
                    raise ToolError("That tab is not open.")
                page = pages[index]
                session["page"] = page
                page.bring_to_front()
            else:
                rows = [f"[{index}] {item.url}" for index, item in enumerate(session["context"].pages, start=1)]
                _write_live(store, bot_id, url=page.url or "", title=page.title() or "", headless=headless, active=True)
                return "\n".join(rows) or "(no tabs)", ""
        elif action == "read":
            pass
        elif action in {"click", "type", "select", "download"}:
            elements = _elements(page)
            element = _pick(elements, request.command)
            locator = page.locator(f"[data-ea-n='{int(element['n'])}']")
            if action == "download":
                with page.expect_download(timeout=15000) as pending:
                    locator.click(timeout=10000)
                text = _save_download(store, bot_id, pending.value)
                _shoot(store, bot_id, page, vision=False)
                _write_live(store, bot_id, url=page.url or "", title=page.title() or "", headless=headless, active=True)
                return text, ""
            if action == "type":
                if looks_like_secret_value(request.body or "", element):
                    raise ToolError("EasyAgent will not type a password, a card number, or a 2FA code.")
                locator.fill(request.body or "", timeout=10000)
            elif action == "select":
                option = (request.body or "").strip()
                locator.select_option(label=option)
            else:
                locator.click(timeout=10000)
                page.wait_for_timeout(200)
        else:
            raise ToolError("A browser action is open, read, click, type, select, scroll, back, wait, tabs, or download.")
        page = _page(session)
        host = (urlparse(page.url or "").hostname or "").lower()
        if host:
            blocked = domain_block(host, list(bot.get("browser_allow") or []), list(bot.get("browser_deny") or []))
            if blocked:
                try:
                    page.go_back(timeout=5000)
                except Exception:
                    pass
                raise ToolError("That site is not allowed for this bot. The browser went back.")
        shot = _shoot(store, bot_id, page, vision=vision and action in {"open", "read"})
        title = ""
        try:
            title = page.title() or ""
        except Exception:
            title = ""
        _write_live(store, bot_id, url=page.url or "", title=title, headless=headless, active=True)
        notice = _containment_notice(store, bot_id)
        prefix = (notice + "\n") if notice else ""
        if action == "read":
            return prefix + _snapshot_text(page, _elements(page)), shot
        if action == "open":
            return prefix + f"Opened {title or 'a page'} — {page.url}", shot
        if action == "tabs":
            return prefix + _snapshot_text(page, _elements(page)), shot
        line = {
            "back": "Went back.",
            "scroll": "Scrolled the page.",
            "wait": "Waited.",
            "click": "Clicked the element.",
            "type": "Typed into the element.",
            "select": "Chose that option.",
        }.get(action, "Used the browser.")
        return prefix + line, shot

    return _HUB.call(work)


def focus(store: Store, bot_id: str) -> None:
    def work(playwright, sessions):
        session = sessions.get(bot_id)
        if not session:
            return False
        _page(session).bring_to_front()
        return True

    if not is_open(bot_id):
        return
    _HUB.call(work)


def cancel(store: Store, bot_id: str) -> None:
    def work(playwright, sessions):
        _close_session(sessions, bot_id)
        return True

    if not is_open(bot_id) and not _HUB._started:
        _write_live(store, bot_id, url="", title="", headless=wants_headless(None), active=False)
        return
    try:
        _HUB.call(work, timeout=10)
    except Exception:
        _mark_open(bot_id, False)
    try:
        _write_live(store, bot_id, url="", title="", headless=False, active=False)
    except Exception:
        return


def reset_for_tests() -> None:
    def work(playwright, sessions):
        for bot_id in list(sessions):
            _close_session(sessions, bot_id)
        return True

    if _HUB._started:
        try:
            _HUB.call(work, timeout=15)
        except Exception:
            with _OPEN_LOCK:
                _OPEN.clear()
    else:
        with _OPEN_LOCK:
            _OPEN.clear()


def remove_profile(store: Store, bot_id: str) -> None:
    """Used when a test needs the folder gone. The person's browser is not touched."""
    cancel(store, bot_id)
    folder = store._bot_dir(store.get_bot(bot_id)["id"]) / "browser-profile"
    if folder.exists() and not _is_real_browser_path(str(folder.resolve())):
        shutil.rmtree(folder, ignore_errors=True)
