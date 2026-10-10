"""Safety review before a tool runs.

A deterministic rules engine decides allow, ask, or block. Anything it does
not recognize is sent to this bot's own model, and if that call cannot be
made the action is asked, not run. A denial or an expired card is final.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path

from easyagent import turn as turn_mod
from easyagent.paths import default_deliverable_dir
from easyagent.store import Store, atomic_write_json, new_id, now_iso, read_json

ALLOW = "allow"
ASK = "ask"
BLOCK = "block"
REVIEW = "review"

MAX_CALLS = 48
MAX_SECONDS = 20 * 60
APPROVAL_SECONDS = 10 * 60

_MARK_START = "<<<UNTRUSTED"
_MARK_END = "<<<END UNTRUSTED>>>"

_PACKAGE = Path(__file__).resolve().parent

_HARMLESS = re.compile(
    r"(?i)^(?:printf|echo|pwd|ls|dir|whoami|date|uname|true|false|hostname|"
    r"id|which|where|head|tail|wc|stat|cd|pushd|popd|"
    r"get-date|get-location|get-childitem|write-output|write-host|"
    r"python3?\s+-c\s+['\"]print\b|node\s+-e\s+['\"]console\.log\b)\b"
)
_READ_CMD = re.compile(
    r"(?i)^(?:cat|type|get-content|less|more|head|tail|rg|grep|find|fd|top|ps|df|free|uptime)\b"
)
_GIT_READ = re.compile(r"(?i)^git\s+(status|diff|log|show|rev-parse|branch|remote)\b")
_GIT_HARM = re.compile(r"(?i)\bgit\s+(push|reset|clean)\b|\bgit\s+branch\s+(-D|-d|--delete)\b")
_INSTALL = re.compile(
    r"(?i)\b(apt(-get)?|dnf|yum|pacman|brew|choco|winget|pip3?|npm|pnpm|yarn|cargo)\s+"
    r"(install|uninstall|remove|add|purge)\b"
)
_ADMIN = re.compile(
    r"(?i)\b(sudo|runas|shutdown|reboot|poweroff|useradd|userdel|net\s+user|"
    r"chmod\s+-R|chown\s+-R|icacls)\b"
)
_PERSIST = re.compile(
    r"(?i)\b(schtasks|crontab|systemctl\s+(enable|disable|start|stop)|sc\s+create|"
    r"new-service|register-scheduledtask)\b|\breg\s+add\b.*\\run\b"
)
_SEND = re.compile(
    r"(?i)\b(sendmail|msmtp|mailx|postfix|send-mailmessage)\b|"
    r"api\.slack\.com|discord\.com/api/webhooks|graph\.microsoft\.com/.*/sendmail|"
    r"hooks\.slack\.com"
)
_POST = re.compile(
    r"(?i)\bcurl\b.*(\s-d\b|\s--data\b|\s-F\b|\s--form\b|\s-X\s*POST\b)|"
    r"\b(invoke-webrequest|iwr|wget)\b.*(-method\s+post|--post-data|--post-file)"
)
_MONEY = re.compile(r"(?i)\b(stripe\.com|checkout\.stripe|paypal\.com|billing)\b")
_PIPE_SH = re.compile(
    r"(?i)(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba)?sh\b|"
    r"\biwr\b[^|\n]*\|\s*iex\b|"
    r"invoke-webrequest\b[^|\n]*\|\s*invoke-expression\b|"
    r"invoke-expression\b.*invoke-webrequest"
)
_FORK = re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:|while\s+true\s*;\s*do\s*:\s*;\s*done")
_DISK = re.compile(
    r"(?i)\b(mkfs|diskpart)\b|\bdd\b[^\n]*\bof=/dev/|"
    r"\bformat(?:\.com)?\s+[a-z]:|"
    r"\bcipher(?:\.exe)?\s+/w\b|"
    r"\bsdelete(?:64)?(?:\.exe)?\b|"
    r"\bvssadmin(?:\.exe)?\s+delete\s+shadows\b|"
    r"\bwbadmin(?:\.exe)?\s+delete\b|"
    r"\bbcdedit(?:\.exe)?\s+/delete\b|"
    r"\breg(?:\.exe)?\s+delete\s+hklm\b"
)
_FETCH = re.compile(
    r"(?i)\bcertutil(?:\.exe)?\b[^\n]*-(?:urlcache|decode)\b|"
    r"\bbitsadmin(?:\.exe)?\b[^\n]*/transfer\b|"
    r"\bstart-bitstransfer\b|"
    r"\b(?:curl|wget)(?:\.exe)?\b[^\n]*\s(?:-o|--output)\b[^\n]*[&;|]|"
    r"\bmshta(?:\.exe)?\b[^\n]*https?://|"
    r"\b(?:rundll32|regsvr32)(?:\.exe)?\b[^\n]*https?://|"
    r"\bmsiexec(?:\.exe)?\b[^\n]*/i\b[^\n]*https?://"
)
_FIREWALL = re.compile(
    r"(?i)advfirewall[^\n]*state\s+off|disableRealtimeMonitoring\s+\$true|"
    r"set-netfirewallprofile[^\n]*-enabled\s+false|\bufw\b[^\n]*disable|"
    r"set-mppreference[^\n]*disablerealtimemonitoring"
)
_CREDS = re.compile(
    r"(?i)\b(mimikatz|procdump|secretsdump)\b|\blsass\b|\breg\s+save\s+hklm\\sam\b|"
    r"\bsekurlsa\b"
)
_DELETE = re.compile(
    r"(?i)^(?:rm|unlink|del|erase|rd|ri|rmdir|remove-item|clear-content|shred|trash)\b"
)
_REMOVE_ANY = re.compile(r"(?i)\bremove-item\b")
_DOTNET_WRITE = re.compile(
    r"(?i)\[(?:system\.)?io\.(?:file|directory|fileinfo)\]::\s*(delete|move|moveTo|writeall\w*|replace|copy)"
)
_PY_DELETE = re.compile(r"(?i)\b(?:os\.(?:remove|unlink)|shutil\.rmtree|pathlib\.[^\n]{0,80}\.unlink)\s*\(")
_JS_DELETE = re.compile(
    r"(?i)(?:\bfs\.(?:promises\.)?(?:unlink|rm|rmSync|unlinkSync)\s*\(|\.(?:unlinkSync|rmSync)\s*\()"
)
_DOTNET_CALL = re.compile(r"\[[A-Za-z_][\w.]*(?:\.[A-Za-z_][\w.]*)*\]::")
_MOVE = re.compile(r"(?i)^(?:mv|move|ren|rename-item|move-item)\b")
_ENC_LONG = re.compile(
    r"(?i)(?:^|[\s;&|])-(?:encodedcommand|enc)\s+([A-Za-z0-9+/]{8,}={0,2})"
)
_ENC_SHORT = re.compile(
    r"(?i)\b(?:powershell|pwsh)(?:\.exe)?\b[^\n]*?\s-(?:ec|e)\s+([A-Za-z0-9+/]{8,}={0,2})"
)
_WRAP = re.compile(
    r"(?is)^(?:cmd(?:\.exe)?\s+/c\s+|powershell(?:\.exe)?\s+(?:-noprofile\s+)?(?:-command|-c)\s+|"
    r"(?:ba)?sh\s+-c\s+)(.+)$"
)
_WEAKEN = re.compile(
    r"(?i)(disable|skip|turn off|bypass|ignore|weaken|remove).{0,48}"
    r"(safety|guardrail|approval|ask before|confirmation|confirm)|always allow\s+rm|auto-approve|no approval|"
    r"never ask before|approve deletes automatically|do not wait for a yes|don't wait for a yes|"
    r"no card appears|so no card|without (an? )?(approval|asking|a card|confirming)|instead of remove-item|"
    r"delete without confirm|just run deletes|run deletes directly|"
    r"safety_mode\s*(?:to|=|:)\s*advanced|always (?:pick|choose|select|use) the always allow"
)
_FLAG_SWITCH = re.compile(r"(?i)(?:^|\s)-+(?:Force|Verbose|Debug)\b(?::\S+)?")
_FLAG_VALUE = re.compile(
    r"(?i)(?:^|\s)-+(?:ErrorAction|WarningAction|InformationAction|ErrorVariable|WarningVariable|"
    r"OutVariable|PipelineVariable|ea|wa|ev|wv|ov|iv)\b(?::\S+|\s+\S+)?"
)
_WIDE = re.compile(
    r"(?i)^(?:/|\\|/\\*|\\*|/\*|~|\$HOME|%USERPROFILE%|\*|[A-Za-z]:[/\\]?\*?|[A-Za-z]:[/\\]\*)$"
)
_ASKED_WRITE = re.compile(
    r"(?i)\b(write|replace|edit|save|build|create|make|put|overwrite|update|change|summary)\b"
)
_PERSIST_PATH = re.compile(
    r"(?i)(autostart|systemd/user|cron\.d|/etc/cron|launchagents|launchdaemons|"
    r"programs/startup|currentversion/run)"
)


@dataclass(frozen=True)
class Verdict:
    tier: str
    rule: str
    why: str
    detail: str
    fingerprint: str


@dataclass
class Pending:
    id: str
    bot_id: str
    tier: str
    rule: str
    why: str
    detail: str
    fingerprint: str
    exact: str
    offer_always: bool
    created: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: str = ""
    proposal: dict | None = None


_PENDING: dict[str, Pending] = {}
_AUDIT_LOCK = threading.Lock()
_UNTRUSTED: list[str] = []
_UNTRUSTED_GUARD = asyncio.Lock()
_UNATTENDED: ContextVar[bool] = ContextVar("easyagent_unattended", default=False)


def unattended() -> Token:
    """A routine run. Ask-tier tools do not block the run."""
    return _UNATTENDED.set(True)


def attend(token: Token) -> None:
    _UNATTENDED.reset(token)


def is_unattended() -> bool:
    return bool(_UNATTENDED.get())


def reset_for_tests() -> None:
    _PENDING.clear()
    _UNTRUSTED.clear()
    try:
        from easyagent.connectors import reset_for_tests as reset_connectors

        reset_connectors()
    except Exception:
        return


_SEMANTIC_CHECK = None


def lesson_weakens(text: str) -> bool:
    """A lesson, note, or playbook must not turn the guardrails down."""
    if _WEAKEN.search(text or ""):
        return True
    checker = _SEMANTIC_CHECK
    if checker is None:
        return False
    try:
        return bool(checker(text or ""))
    except Exception:
        return False


def mark_untrusted(text: str) -> str:
    body = text or ""
    if _MARK_START in body:
        return body
    remembered = body[:4000]
    if remembered.strip():
        _UNTRUSTED.append(remembered)
        del _UNTRUSTED[:-24]
    return f"{_MARK_START} source=\"tool\">>>\n{body}\n{_MARK_END}"


def strip_untrusted(text: str) -> str:
    """The bytes inside the markers. Bookkeeping reads those, not the label."""
    body = text or ""
    if _MARK_START not in body:
        return body
    body = re.sub(r"<<<UNTRUSTED[^>\n]*>>>\n?", "", body)
    return body.replace(_MARK_END, "")


def wrap_output(request, text: str, store: Store | None = None) -> str:
    """File contents, pages, and command output are data, not instructions."""
    kind = getattr(request, "kind", "")
    action = getattr(request, "action", "")
    if kind == "files" and action in {"list", "write"}:
        return text
    if kind in {"memory", "history", "project", "question", "finish", "plan", "react"}:
        return text
    if not (text or "").strip():
        return text
    cleaned = text
    if store is not None:
        try:
            from easyagent.journal import scrub_text

            cleaned = scrub_text(store, text)
        except Exception:
            cleaned = _scrub_patterns(text)
    else:
        cleaned = _scrub_patterns(text)
    if kind in {"files", "shell", "ssh", "windows", "search", "mcp"} or action == "read":
        return mark_untrusted(cleaned)
    if kind == "browser" and action in {"read", "open", "tabs"}:
        return mark_untrusted(cleaned)
    return cleaned


def _scrub_patterns(text: str) -> str:
    cleaned = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", text or "")
    cleaned = re.sub(r"(?i)\b(ghp_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9_-]{8,}\b", "[redacted]", cleaned)
    cleaned = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}\b", "Bearer [redacted]", cleaned)
    if "-----BEGIN " in cleaned and "PRIVATE KEY-----" in cleaned:
        cleaned = "[redacted private key]"
    return cleaned


def _mode(bot: dict | None) -> str:
    mode = str((bot or {}).get("safety_mode") or "careful")
    if mode not in {"careful", "normal", "advanced"}:
        return "careful"
    return mode


def _expand(text: str) -> str:
    home = str(Path.home())
    out = text or ""
    out = out.replace("%USERPROFILE%", home).replace("%HOME%", home)
    out = out.replace("${HOME}", home).replace("$HOME", home)
    from easyagent.shellexpand import expand_command, expand_join_path, local_assignments

    env = dict(os.environ)
    env.setdefault("USERPROFILE", home)
    env.setdefault("HOME", home)
    env.update(local_assignments(out))
    out = expand_command(expand_join_path(out, env), env)
    return os.path.expandvars(out)


def _strip_benign_flags(line: str) -> str:
    """-Force does not eat the next path. -ErrorAction does, including the colon form."""
    cleaned = _FLAG_VALUE.sub(" ", line or "")
    return _FLAG_SWITCH.sub(" ", cleaned)


def _command_fingerprint(line: str) -> str:
    """Benign flags do not make a new fingerprint. -Force and -ErrorAction are ignored."""
    return _fp("cmd", " ".join(_strip_benign_flags(line).split()))


def _decode_blob(blob: str) -> str | None:
    """UTF-16LE PowerShell -EncodedCommand payload. None when it cannot be read."""
    try:
        raw = base64.b64decode(blob, validate=True)
    except Exception:
        return None
    if not raw or len(raw) % 2:
        return None
    try:
        decoded = raw.decode("utf-16-le")
    except UnicodeError:
        return None
    decoded = decoded.replace("\x00", "").strip()
    if not decoded:
        return None
    return decoded


def _replace_encoded(current: str, match: re.Match, decoded: str) -> str:
    """Swap the launcher and the blob for the decoded command. The rest of a chain stays."""
    start = match.start()
    segment = current[:start]
    cut = max(segment.rfind("\n"), segment.rfind(";"), segment.rfind("&"), segment.rfind("|"))
    head = segment[cut + 1 :]
    launcher = re.search(r"(?i)(?:^|\s)((?:powershell|pwsh)(?:\.exe)?)\b", head)
    if launcher:
        start = cut + 1 + launcher.start(1)
    return (current[:start] + " " + decoded + " " + current[match.end() :]).strip()


def _peel_encoded(text: str) -> tuple[str, str]:
    """Peel encoded layers. ``ask`` when a layer is undecodable or still nested after three."""
    current = text or ""
    for _ in range(3):
        match = _ENC_LONG.search(current) or _ENC_SHORT.search(current)
        if not match:
            return current, "ok"
        decoded = _decode_blob(match.group(1))
        if decoded is None:
            return current, "ask"
        current = _replace_encoded(current, match, decoded)
    if _ENC_LONG.search(current) or _ENC_SHORT.search(current):
        return current, "ask"
    return current, "ok"


def decode_shell(command: str) -> tuple[str, str]:
    """Peel an encoded command and a cmd / powershell wrapper.

    Variables stay in the text. The data guard expands them while it resolves
    paths. Expanding first can turn a Join-Path into a slash style this
    computer does not treat as the data folder.
    """
    text = (command or "").strip()
    if not text:
        return "", "ok"
    text, status = _peel_encoded(text)
    if status == "ask":
        return text, "ask"
    wrapped = _WRAP.match(text.strip())
    if wrapped:
        inner = wrapped.group(1).strip()
        if (inner.startswith('"') and inner.endswith('"')) or (inner.startswith("'") and inner.endswith("'")):
            inner = inner[1:-1]
        text, status = _peel_encoded(inner.strip())
        if status == "ask":
            return text, "ask"
    return text, "ok"


def prepare_shell(command: str) -> tuple[str, str]:
    """Decoded command with variables expanded, for the rules engine."""
    text, status = decode_shell(command)
    if status == "ask":
        return text, "ask"
    return _expand(text), "ok"


def _unwrap(command: str) -> str:
    text, _status = prepare_shell(command)
    return text


def _split_chain(command: str) -> list[str]:
    """Pieces of a chain. Quotes stay intact. A trailing bare & is not a second command."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    text = command or ""
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if text.startswith("&&", i) or text.startswith("||", i):
            parts.append("".join(buf).strip())
            buf = []
            i += 2
            continue
        if ch in {";", "\n"} or (ch == "|" and not text.startswith("||", i)):
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        if ch == "&":
            rest = text[i + 1:].strip()
            if not rest:
                i += 1
                continue
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part] or [text.strip()]


def _worst(left: Verdict, right: Verdict) -> Verdict:
    rank = {ALLOW: 0, REVIEW: 1, ASK: 2, BLOCK: 3}
    return left if rank[left.tier] >= rank[right.tier] else right


def _verdict(tier: str, rule: str, why: str, detail: str, fingerprint: str) -> Verdict:
    return Verdict(tier, rule, why, detail, fingerprint)


def _fp(*parts: str) -> str:
    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _file_request_path(store: Store, raw: str, cwd: Path, bot_id: str | None) -> Path:
    """A file named with no folder is this bot's workspace, not the install folder."""
    text = (raw or ".").strip().strip('"')
    if (
        bot_id
        and text not in {"", "."}
        and "/" not in text
        and "\\" not in text
        and not text.startswith("~")
    ):
        from easyagent.workspace import bot_workspace

        return bot_workspace(store, bot_id) / Path(text).name
    return _resolve(text, cwd)


def _resolve(raw: str, cwd: Path) -> Path:
    text = _expand(raw).strip().strip('"').strip("'")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve()
    except OSError:
        return path


def _workspace_roots(store: Store, bot_id: str | None, user_text: str) -> list[Path]:
    roots = [default_deliverable_dir()]
    try:
        roots.append(Path(turn_mod.tool_cwd()).resolve())
    except OSError:
        roots.append(Path(turn_mod.tool_cwd()))
    if bot_id:
        from easyagent.workspace import bot_workspace

        roots.append(bot_workspace(store, bot_id))
    roots.append(Path(tempfile.gettempdir()))
    for match in re.finditer(r"(?:[A-Za-z]:[\\/]|/)[^\s\"']+", user_text or ""):
        candidate = Path(match.group(0).rstrip(".,);:"))
        if candidate.suffix:
            candidate = candidate.parent
        if str(candidate) not in {".", ""}:
            roots.append(candidate)
    return roots


def _path_key(path: Path) -> str:
    """One spelling for a path. Windows resolve can differ by case or a \\\\?\\ prefix."""
    text = os.path.normcase(os.path.abspath(str(path)))
    text = text.replace("/", "\\") if os.name == "nt" else text
    if os.name == "nt" and text.startswith("\\\\?\\"):
        if text.upper().startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[8:]
        else:
            text = text[4:]
    return text.rstrip("\\/")


def _inside(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    folded = _path_key(resolved)
    for root in roots:
        try:
            root_resolved = root.resolve()
        except OSError:
            root_resolved = root
        if resolved == root_resolved or root_resolved in resolved.parents:
            return True
        other = _path_key(root_resolved)
        if folded == other or folded.startswith(other + os.sep):
            return True
    return False


def _secret_kind(path: Path) -> str:
    name = path.name.lower()
    text = str(path).lower().replace("\\", "/")
    if name in {"cookies", "cookies.sqlite", "login data", "logins.json", "key4.db", "web data"}:
        return "browser"
    if "cookies" in text and ("chrome" in text or "firefox" in text or "safari" in text or "edge" in text):
        return "browser"
    if name in {".env", ".env.local", ".env.production"} or name.startswith(".env."):
        return "secret"
    if name in {"id_rsa", "id_ed25519", "id_ecdsa"} or name.endswith(".pem"):
        return "secret"
    if "wallet" in name or name in {"keystore", "credentials", "secrets.json"}:
        return "secret"
    if text.endswith("/etc/shadow") or text.endswith("/etc/gshadow"):
        return "browser"
    return ""


def _is_profile_root(path: Path) -> bool:
    text = str(path).rstrip("\\/")
    home = str(Path.home())
    if text in {"/", "~", home, os.environ.get("USERPROFILE", home)}:
        return True
    if re.fullmatch(r"[A-Za-z]:\\?", text):
        return True
    return False


def _install_venv() -> Path | None:
    """The .venv this copy of EasyAgent lives in. A project's .venv is not this one."""
    for parent in _PACKAGE.parents:
        if parent.name.lower() != ".venv":
            continue
        try:
            return parent.resolve()
        except OSError:
            return parent
    sibling = _PACKAGE.parent / ".venv"
    try:
        if sibling.is_dir():
            return sibling.resolve()
    except OSError:
        return None
    return None


def _in_install_venv(path: Path) -> bool:
    venv = _install_venv()
    if venv is None:
        return False
    key = _path_key(path)
    root = _path_key(venv)
    return key == root or key.startswith(root + os.sep)


def _is_guardrail(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    text = str(resolved).replace("\\", "/").lower()
    name = resolved.name.lower()
    if _PACKAGE == resolved or _PACKAGE in resolved.parents:
        return True
    if _in_install_venv(resolved):
        return True
    if name in {
        "safety.json",
        "guardrails.json",
        "safety-audit.json",
        "sandbox.json",
        "schedules.json",
        "endpoints.json",
        "secrets.db",
        "secrets.passphrase",
    }:
        return True
    if name == "bot.json" and "/bots/" in text:
        return True
    if name == "index.json" and "/trash/" in text:
        return True
    return False


def _names_saved_data(line: str) -> bool:
    """A command that names the data files a bot must not read or write."""
    return bool(
        re.search(
            r"(?i)(?:^|[\\/\s'\"`])(?:endpoints\.json|secrets\.db|secrets\.passphrase|"
            r"safety-audit\.json|guardrails\.json|safety\.json|sandbox\.json|schedules\.json)"
            r"(?:$|[\\/\s'\"`])",
            line or "",
        )
    )


_INTERPRETER = re.compile(
    r"(?i)^(?:python\d*(?:\.\d+)?w?|py|node|nodejs|pwsh|powershell|cmd|bash|sh|zsh|dash|ruby|perl|deno)$"
)


def _interpreter_name(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _interpreter_on_path(path: Path) -> bool:
    for name in {path.name, path.stem}:
        found = shutil.which(name)
        if not found:
            continue
        if _path_key(Path(found)) == _path_key(path):
            return True
    return False


def _exempt_interpreter(path: Path) -> bool:
    """An interpreter in EasyAgent's install venv, or one found on PATH, is the program, not a file read."""
    if not _INTERPRETER.fullmatch(_interpreter_name(path)):
        return False
    return _in_install_venv(path) or _interpreter_on_path(path)


def _glued_verb_path(token: str) -> tuple[str, str] | None:
    """``Get-ContentC:\\chats\\a.json`` or ``Get-Content/tmp/a`` with no space."""
    match = re.search(r"(?:[A-Za-z]:[\\/]|/)", token)
    if not match or match.start() == 0:
        return None
    verb, path = token[: match.start()], token[match.start() :]
    if not re.fullmatch(r"[A-Za-z][\w.-]*", verb):
        return None
    if re.match(r"[A-Za-z]:[\\/]", path):
        return verb, path
    if path.startswith("/") and ("/" in path[1:] or "\\" in path):
        return verb, path
    return None


def _unglue(line: str) -> str:
    stripped = (line or "").lstrip()
    if not stripped or stripped[0] in {"'", '"'}:
        return line or ""
    token = stripped.split(None, 1)[0]
    split = _glued_verb_path(token)
    if split is None:
        return line or ""
    verb, path = split
    lead = len(line) - len(stripped)
    return (line or "")[:lead] + verb + " " + path + stripped[len(token) :]


def _command_program(line: str, cwd: Path) -> Path | None:
    """The executable a command launches. Launching it is not a read of that file."""
    stripped = (line or "").strip()
    if not stripped:
        return None
    if stripped[0] in {"'", '"'}:
        end = stripped.find(stripped[0], 1)
        token = stripped[1:end] if end > 1 else ""
    else:
        token = stripped.split()[0]
    if not token:
        return None
    return _resolve(token, cwd)


def _without_program(line: str) -> str:
    """The command after the executable token. The token is what ``_command_program`` takes."""
    stripped = (line or "").lstrip()
    if not stripped:
        return ""
    if stripped[0] in {"'", '"'}:
        end = stripped.find(stripped[0], 1)
        if end > 1:
            return stripped[end + 1 :]
        return stripped
    token = stripped.split(None, 1)[0]
    if stripped.startswith(token):
        return stripped[len(token) :]
    return stripped


def _paths_in(command: str, cwd: Path) -> list[Path]:
    found: list[Path] = []
    for match in re.finditer(r"(?P<path>(?:[A-Za-z]:[\\/]|/)[\w .~\\/-]+|~[/\\][\w .\\/-]+)", command or ""):
        found.append(_resolve(match.group("path"), cwd))
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", command or "")
    for item in quoted:
        if "/" in item or "\\" in item or item.startswith("."):
            found.append(_resolve(item, cwd))
    return found


def _targets_after_verb(command: str, cwd: Path) -> list[Path]:
    body = _strip_benign_flags(command)
    body = re.sub(r"(?i)\s-[A-Za-z]+\b", " ", body)
    body = re.sub(
        r"(?i)^(rm|unlink|del|erase|rd|ri|rmdir|remove-item|mv|move|ren|rename-item|move-item)\b",
        "",
        body,
    ).strip()
    paths: list[Path] = []
    for token in re.findall(r"[^\s]+", body):
        if token in {"|", ">", ">>", "<"}:
            break
        paths.append(_resolve(token, cwd))
    return paths


def _content_target(line: str, cwd: Path) -> Path | None:
    """The file Set-Content or Out-File names. None when the command does not name one."""
    quoted = re.search(r"(?i)(?:-literalpath|-filepath|-path)\s+(['\"])(.+?)\1", line or "")
    if quoted:
        return _resolve(quoted.group(2), cwd)
    named = re.search(r"(?i)(?:-literalpath|-filepath|-path)\s+(\S+)", line or "")
    if named:
        return _resolve(named.group(1), cwd)
    bare = re.search(r"(?i)\b(?:set-content|out-file)\s+(['\"])(.+?)\1", line or "")
    if bare:
        return _resolve(bare.group(2), cwd)
    bare = re.search(r"(?i)\b(?:set-content|out-file)\s+(\S+)", line or "")
    if bare and not bare.group(1).startswith("-"):
        return _resolve(bare.group(1), cwd)
    return None


def _wide_token(token: str) -> bool:
    text = (token or "").strip().strip('"').strip("'")
    if text in {"", "/", "\\", "/*", "\\*", "*", "~", "$HOME", "%USERPROFILE%"}:
        return True
    return bool(_WIDE.fullmatch(text))


def _drive_or_unresolved(text: str) -> Verdict | None:
    """A delete of a drive root, or of a variable that is empty, never runs."""
    from easyagent.shellexpand import inline_code, prepared_command, still_unresolved

    detail = (text or "").strip()
    if not detail:
        return None
    expanded = prepared_command(detail)
    extra = inline_code(expanded)
    blob = expanded if not extra else expanded + "\n" + extra
    deleting = bool(
        re.search(r"(?i)\b(rm|rd|ri|rmdir|del|erase|remove-item|unlink|shutil\.rmtree|os\.remove|os\.unlink|rmSync|unlinkSync|\.Delete\s*\()", blob)
    )
    if not deleting:
        return None
    fingerprint = _fp("cmd", detail)
    why = "That would delete the disk root or the whole user profile. It is never run."
    if still_unresolved(expanded):
        return _verdict(BLOCK, "root-delete", "That path is not set. It is never run.", detail, fingerprint)
    pipeline = re.search(
        r"(?i)(?:Get-ChildItem|gci|dir|ls)\s+([^\s|]+)\s*\|\s*(?:Remove-Item|ri|del|erase|rm|rmdir|rd)\b",
        blob,
    )
    if pipeline and _wide_token(pipeline.group(1).strip("'\"")):
        return _verdict(BLOCK, "root-delete", why, detail, fingerprint)
    for match in re.finditer(r"['\"]([^'\"]+)['\"]", blob):
        if _wide_token(match.group(1)) and re.search(r"(?i)(remove|unlink|rmtree|rmSync|unlinkSync|delete|rmdir|\brd\b|\bri\b|\bdel\b|\berase\b)", blob):
            return _verdict(BLOCK, "root-delete", why, detail, fingerprint)
    for piece in _split_chain(blob):
        tokens = re.findall(r"[^\s]+", _strip_benign_flags(piece))
        if not tokens:
            continue
        head = tokens[0].lower()
        if head not in {"rm", "rd", "ri", "rmdir", "del", "erase", "remove-item", "unlink"}:
            continue
        positional = []
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token.lower() in {"-erroraction", "-warningaction", "-ea", "-wa"} and index + 1 < len(tokens):
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            positional.append(token)
            index += 1
        if any(_wide_token(token) for token in positional):
            return _verdict(BLOCK, "root-delete", why, detail, fingerprint)
        if not positional and re.search(r"(?i)(\\+\*?|/\*?)\s*$", piece):
            return _verdict(BLOCK, "root-delete", why, detail, fingerprint)
    if re.search(r"(?i)(remove-item|rm|del|erase|rmdir|rd|ri)\b", blob) and re.search(
        r"""['\"]\\?\*['\"]|['\"]\\['\"]|['\"]/['\"]|\s\\+\*?\s*$""",
        blob,
    ):
        return _verdict(BLOCK, "root-delete", why, detail, fingerprint)
    return None


def _acl_on_protected_root(detail: str) -> bool:
    """takeown or icacls aimed at a drive, Windows, or a user profile. A work folder is not."""
    if not re.search(r"(?i)\b(?:takeown|icacls)(?:\.exe)?\b", detail or ""):
        return False
    if re.search(
        r"(?i)(?:"
        r"(?:^|[\s\"'])[a-z]:\\?(?=[\s\"']|$)|"
        r"[a-z]:\\(?:windows|winnt|users|program files(?: \(x86\))?|programdata|perflogs)\b|"
        r"%USERPROFILE%|%SYSTEMROOT%|%WINDIR%|%HOMEPATH%|"
        r"\$env:USERPROFILE|\$HOME|\$env:HOME|"
        r"(?:^|[\s\"'])/(?=[\s\"']|$)|"
        r"(?:^|[\s\"'])/(?:etc|usr|bin|Users|System)\b"
        r")",
        detail or "",
    ):
        return True
    # Expansion already replaced %USERPROFILE% and $HOME with this account's folder.
    for root in {str(Path.home()).rstrip("/\\"), (os.environ.get("USERPROFILE") or "").rstrip("/\\")}:
        if root and re.search(r"(?:^|[\s\"'])" + re.escape(root) + r"(?:[\s\"']|$)", detail or ""):
            return True
    return False


def _blocked_command(line: str) -> Verdict | None:
    detail = line.strip()
    fingerprint = _fp("cmd", detail)
    if _FORK.search(detail):
        return _verdict(BLOCK, "fork-bomb", "That command would spawn copies of itself until the computer stalls.", detail, fingerprint)
    if _PIPE_SH.search(detail):
        return _verdict(BLOCK, "remote-script", "That downloads a script and runs it. It is never run.", detail, fingerprint)
    if _DISK.search(detail):
        return _verdict(BLOCK, "disk-format", "That would erase a disk. It is never run.", detail, fingerprint)
    if _acl_on_protected_root(detail):
        return _verdict(
            BLOCK,
            "disk-format",
            "That takes ownership of a system folder or a user profile. It is never run.",
            detail,
            fingerprint,
        )
    if _FETCH.search(detail):
        return _verdict(BLOCK, "remote-script", "That downloads a program and runs it. It is never run.", detail, fingerprint)
    if _FIREWALL.search(detail):
        return _verdict(BLOCK, "firewall-off", "That turns off the firewall or Defender. It is never run.", detail, fingerprint)
    if _CREDS.search(detail):
        return _verdict(BLOCK, "credential-dump", "That reads or dumps credentials. It is never run.", detail, fingerprint)
    if re.search(
        r"(?i)\b(rm|rd|ri|rmdir|del|erase|remove-item)\b[^\n]*(\s/|\s/\*|\s~|\s\$HOME|\s%USERPROFILE%|\s[A-Za-z]:[/\\]?)\s*$",
        detail,
    ):
        return _verdict(BLOCK, "root-delete", "That would delete the disk root or the whole user profile. It is never run.", detail, fingerprint)
    if re.search(r"(?i)\b(irm|iwr|invoke-webrequest|invoke-restmethod|curl|wget)\b", detail) and re.search(
        r"(?i)(-outfile|-out-file|\s-o\b).{0,80}\.(ps1|bat|cmd|exe|js|vbs|sh)\b",
        detail,
    ) and re.search(r"(?i)(?:^|[;&|]\s*)(?:\.\\|./|bash\s+|sh\s+|powershell\s+).{0,120}\.(ps1|bat|cmd|sh)\b", detail):
        return _verdict(BLOCK, "remote-script", "That downloads a script and runs it. It is never run.", detail, fingerprint)
    if re.search(r"(?i)\b(irm|iwr|invoke-webrequest|invoke-restmethod|downloadstring)\b", detail) and re.search(
        r"(?i)\b(iex|invoke-expression)\b|\&\s*\$[A-Za-z_]",
        detail,
    ):
        return _verdict(BLOCK, "remote-script", "That downloads a script and runs it. It is never run.", detail, fingerprint)
    if re.search(r"(?i)&\s*\{[^}]{0,500}\b(iex|invoke-expression)\b", detail) and re.search(
        r"(?i)\b(irm|iwr|invoke-webrequest|invoke-restmethod|downloadstring)\b",
        detail,
    ):
        return _verdict(BLOCK, "remote-script", "That downloads a script and runs it. It is never run.", detail, fingerprint)
    if re.search(r"(?i)scriptblock\s*\]\s*::\s*create", detail) and re.search(
        r"(?i)downloadstring|downloadfile|downloaddata|\birm\b|\biwr\b|invoke-webrequest|invoke-restmethod|webclient",
        detail,
    ):
        return _verdict(BLOCK, "remote-script", "That downloads a script and runs it. It is never run.", detail, fingerprint)
    return None


def judge_command(
    command: str,
    *,
    remote: bool,
    cwd: Path,
    roots: list[Path],
    created: set[str],
    mode: str,
    denied: set[str] | None = None,
) -> Verdict:
    text, encoded = prepare_shell(command)
    if encoded == "ask":
        detail = (command or "").strip()
        return _verdict(
            ASK,
            "encoded-command",
            "That encoded command could not be read. It waits for you.",
            detail,
            _fp("cmd", detail),
        )
    text = _unglue(text)
    hard = _drive_or_unresolved(text)
    if hard is not None:
        return hard
    blocked = _blocked_command(text)
    if blocked is not None:
        return blocked
    if re.search(r"(?i)(cookies|login data|logins\.json|key4\.db|web data)", text):
        return _verdict(BLOCK, "browser-store", "Browser cookies and saved passwords are never read.", text.strip(), _fp("cmd", text.strip()))
    pieces = _split_chain(text)
    if len(pieces) > 1:
        verdicts = [
            judge_command(piece, remote=remote, cwd=cwd, roots=roots, created=created, mode=mode, denied=denied)
            for piece in pieces
        ]
        if denied:
            for item in verdicts:
                if item.fingerprint in denied or item.rule == "already-denied":
                    return _verdict(
                        BLOCK,
                        "already-denied",
                        "You already denied that, or the card expired. It will not be asked again.",
                        item.detail,
                        item.fingerprint,
                    )
        winner = verdicts[0]
        for item in verdicts[1:]:
            winner = _worst(winner, item)
        detail = text.strip()
        return _verdict(winner.tier, winner.rule, winner.why, detail, _command_fingerprint(text))
    line = text.strip()
    detail = line
    fingerprint = _command_fingerprint(line)
    if not line:
        return _verdict(ALLOW, "empty", "There was no command.", detail, fingerprint)
    writing = bool(
        re.search(
            r"(?i)(^|\s)>{1,2}\s*\S+|\b(set-content|out-file|add-content|copy-item|cpi|new-item|tee-object|move-item)\b",
            line,
        )
    )
    program = _command_program(line, cwd)
    paths = list(_paths_in(_without_program(line), cwd))
    if program is not None and not _exempt_interpreter(program):
        paths.insert(0, program)
    for path in paths:
        if program is not None and _exempt_interpreter(program) and _path_key(path) == _path_key(program):
            continue
        if _is_guardrail(path):
            if writing:
                return _verdict(BLOCK, "guardrail-edit", "EasyAgent's own guardrails cannot be edited from a tool.", detail, fingerprint)
            return _verdict(BLOCK, "data-dir", "That reads EasyAgent's saved data.", detail, fingerprint)
        if _secret_kind(path) == "browser" and re.search(r"(?i)\b(cat|type|get-content|copy|cp|curl|scp)\b", line):
            return _verdict(BLOCK, "browser-store", "Browser cookies and saved passwords are never read.", str(path), fingerprint)
    if _names_saved_data(line) and (
        writing or _READ_CMD.search(line) or re.search(r"(?i)\b(cat|type|get-content|gc|select-string)\b", line)
    ):
        if writing:
            return _verdict(BLOCK, "guardrail-edit", "EasyAgent's own guardrails cannot be edited from a tool.", detail, fingerprint)
        return _verdict(BLOCK, "data-dir", "That reads EasyAgent's saved data.", detail, fingerprint)
    if _DELETE.search(line):
        targets = _targets_after_verb(line, cwd)
        if any(_is_profile_root(path) or str(path).rstrip("\\/") in {"/", ""} for path in targets) or re.search(
            r"(?i)\brm\s+.*\s+(/|/\*|~)\s*$", line
        ):
            if any(_is_profile_root(path) for path in targets) or re.search(r"(?i)(/|/\*|~|\$HOME|%USERPROFILE%|[A-Za-z]:\\)\s*$", line):
                return _verdict(BLOCK, "root-delete", "That would delete the disk root or the whole user profile. It is never run.", detail, fingerprint)
        if targets:
            fingerprint = _fp("delete", ",".join(sorted(str(path) for path in targets)))
        return _verdict(ASK, "delete", "That deletes a file. It waits for you, and an approved delete goes to Trash.", detail, fingerprint)
    if _DOTNET_WRITE.search(line) or _REMOVE_ANY.search(line) or _PY_DELETE.search(line) or _JS_DELETE.search(line) or re.search(r"(?i)\.Delete\s*\(", line):
        return _verdict(ASK, "delete", "That deletes or overwrites a file. It waits for you.", detail, fingerprint)
    if re.search(r"(?i)\b(copy-item|cpi|cp|copy)\b", line) and re.search(r"(?i)-force\b", line):
        return _verdict(ASK, "overwrite", "That copies over a file that is already there. It waits for you.", detail, fingerprint)
    if re.search(r"(?i)(^|\s)>{1,2}\s*\S+", line) or re.search(r"(?i)\bclear-content\b", line):
        return _verdict(ASK, "redirect-overwrite", "That command writes over a file. It waits for you, and the old copy is saved first.", detail, fingerprint)
    if re.search(r"(?i)\b(set-content|out-file)\b", line):
        target = _content_target(line, cwd)
        if target is not None and not target.exists():
            if _inside(target, roots):
                return _verdict(ALLOW, "write-new", "Writing a new file inside the workspace is allowed.", detail, fingerprint)
            return _verdict(ASK, "write-outside", "That writes a new file outside the workspace. It waits for you.", detail, fingerprint)
        return _verdict(ASK, "redirect-overwrite", "That command writes over a file. It waits for you, and the old copy is saved first.", detail, fingerprint)
    if _MOVE.search(line):
        return _verdict(ASK, "move", "That moves or renames an existing file. It waits for you.", detail, fingerprint)
    if _GIT_HARM.search(line):
        return _verdict(ASK, "git-destructive", "That git command can throw away history or publish it. It waits for you.", detail, fingerprint)
    if _INSTALL.search(line):
        return _verdict(ASK, "install", "Installing or removing software waits for you.", detail, fingerprint)
    if _ADMIN.search(line):
        return _verdict(ASK, "admin", "That changes the system or uses an administrator account. It waits for you.", detail, fingerprint)
    if _PERSIST.search(line):
        return _verdict(ASK, "persistence", "That changes a service, a scheduled task, or a startup entry. It waits for you.", detail, fingerprint)
    if _SEND.search(line) or (_POST.search(line) and _MONEY.search(line)):
        return _verdict(ASK, "send-or-pay", "That would send a message or spend money. It is only a draft until you approve it.", detail, fingerprint)
    if _POST.search(line) or _SEND.search(line):
        return _verdict(ASK, "outbound", "That uploads or posts data. It waits for you.", detail, fingerprint)
    if re.search(r"(?i)\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|start-bitstransfer)\b.*\.(exe|msi|dmg|sh|ps1)\b", line):
        return _verdict(ASK, "download-exec", "That downloads a program. It waits for you before anything runs.", detail, fingerprint)
    for path in _paths_in(line, cwd):
        kind = _secret_kind(path)
        if kind == "browser":
            return _verdict(BLOCK, "browser-store", "Browser cookies and saved passwords are never read.", str(path), fingerprint)
        if kind == "secret" and _READ_CMD.search(line):
            return _verdict(ASK, "read-secret", "That reads a secret file. It waits for you, and the secret is not copied onward.", str(path), fingerprint)
    if remote and not (_HARMLESS.search(line) or _READ_CMD.search(line) or _GIT_READ.search(line)):
        return _verdict(ASK, "remote-change", "A remote computer is stricter. Anything that changes it waits for you.", detail, fingerprint)
    if _HARMLESS.search(line) or _GIT_READ.search(line):
        return _verdict(ALLOW, "harmless", "That command only reads or prints.", detail, fingerprint)
    if _READ_CMD.search(line):
        return _verdict(ALLOW, "read-command", "That command reads. It does not change the computer.", detail, fingerprint)
    if re.search(r"[|&;<>`$]", line):
        return _verdict(REVIEW, "unmatched-shell", "That command is not one the rules recognize.", detail, fingerprint)
    if _DOTNET_CALL.search(line):
        return _verdict(REVIEW, "dotnet-call", "That calls into .NET. The rules do not treat an unknown call as harmless.", detail, fingerprint)
    return _verdict(ALLOW, "plain-command", "That is an ordinary command with no write, delete, or network post.", detail, fingerprint)


def _generic_root(path: Path) -> bool:
    text = str(path).replace("\\", "/").rstrip("/") or "/"
    generic = {
        "/",
        "/tmp",
        "/var/tmp",
        "/private/tmp",
        str(Path.home()).replace("\\", "/").rstrip("/"),
        str(default_deliverable_dir()).replace("\\", "/").rstrip("/"),
        str(Path(tempfile.gettempdir())).replace("\\", "/").rstrip("/"),
    }
    return text in generic


def _persistence_path(path: Path) -> bool:
    name = path.name.lower()
    if name in {".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile", ".bash_login"}:
        return True
    return bool(_PERSIST_PATH.search(str(path).replace("\\", "/")))


def _requested_write(path: Path, user_text: str) -> bool:
    """The person named this file, or its folder, and asked for it to be written."""
    text = (user_text or "").replace("\\", "/")
    if not _ASKED_WRITE.search(text):
        return False
    target = str(path).replace("\\", "/")
    if len(target) >= 4 and target in text:
        return True
    parent = path.parent
    parent_text = str(parent).replace("\\", "/")
    if _generic_root(parent) or len(parent_text) < 8:
        return False
    return parent_text in text


def _is_passphrase_file(path: Path) -> bool:
    """The passphrase file, including when its name is set by the environment."""
    try:
        from easyagent.secrets import passphrase_path

        return path.resolve() == passphrase_path().resolve()
    except OSError:
        return False


def judge_files(
    action: str,
    path: Path,
    body: str,
    *,
    roots: list[Path],
    created: set[str],
    mode: str,
    user_text: str = "",
) -> Verdict:
    detail = str(path)
    fingerprint = _fp("file", action, str(path))
    if path.name.lower() in {"secrets.db", "secrets.passphrase"} or _is_passphrase_file(path):
        return _verdict(BLOCK, "data-dir", "That file holds a saved secret. It was not read.", detail, fingerprint)
    if _is_guardrail(path) and action == "write":
        return _verdict(BLOCK, "guardrail-edit", "EasyAgent's own guardrails cannot be edited from a tool.", detail, fingerprint)
    secret = _secret_kind(path)
    if secret == "browser":
        return _verdict(BLOCK, "browser-store", "Browser cookies and saved passwords are never read.", detail, fingerprint)
    if action in {"list", "read"}:
        if secret == "secret":
            return _verdict(ASK, "read-secret", "That reads a secret file. It waits for you.", detail, fingerprint)
        return _verdict(ALLOW, "read", "Reading and listing are allowed.", detail, fingerprint)
    if action != "write":
        return _verdict(REVIEW, "file-other", "That file action is not one the rules recognize.", detail, fingerprint)
    if secret == "secret":
        return _verdict(ASK, "write-secret", "That writes a secret file. It waits for you.", detail, fingerprint)
    if _persistence_path(path):
        return _verdict(ASK, "persistence", "That writes a startup or login script. It waits for you.", detail, fingerprint)
    script = path.suffix.lower() in {".sh", ".ps1", ".bat", ".cmd", ".py"}
    if script and body:
        inner = judge_command(body, remote=False, cwd=path.parent, roots=roots, created=created, mode=mode)
        if inner.tier == BLOCK:
            return _verdict(BLOCK, inner.rule, "That file is a script of a blocked command. It is not written.", detail, fingerprint)
        if inner.tier == ASK:
            return _verdict(ASK, "script-body", "That file is a script of a command that needs approval. It waits for you.", detail, fingerprint)
    exists = path.exists()
    own = str(path) in created
    inside = _inside(path, roots)
    if exists and not own:
        fingerprint = _fp("overwrite", str(path))
        if _requested_write(path, user_text):
            return _verdict(
                ALLOW,
                "overwrite-requested",
                "You asked for this file to be written. A copy of the old one is kept first.",
                detail,
                fingerprint,
            )
        if mode == "normal" and inside:
            return _verdict(ALLOW, "overwrite-workspace", "Normal mode allows replacing a file inside the workspace. A copy is kept first.", detail, fingerprint)
        return _verdict(ASK, "overwrite", "That replaces a file that is already there. It waits for you, and a copy is kept first.", detail, fingerprint)
    if not inside:
        return _verdict(ASK, "write-outside", "That writes a new file outside the workspace. It waits for you.", detail, fingerprint)
    return _verdict(ALLOW, "write-new", "Writing a new file inside the workspace is allowed.", detail, fingerprint)


def _user_text(store: Store, bot_id: str | None) -> str:
    slot = turn_mod.current_slot()
    if slot is None or not bot_id or not slot.chat_id:
        return ""
    try:
        chat = store.get_chat(bot_id, slot.chat_id)
    except Exception:
        return ""
    parts = [
        str(message.get("content") or "")
        for message in (chat.get("messages") or [])
        if message.get("role") == "user"
    ]
    return "\n".join(parts)


def _created() -> set[str]:
    slot = turn_mod.current_slot()
    if slot is None:
        return set()
    found = getattr(slot, "safety_created", None)
    if found is None:
        found = set()
        slot.safety_created = found
    return found


def _denied(bot: dict | None) -> set[str]:
    raw = (bot or {}).get("safety_denied") or []
    return {str(item) for item in raw if item}


def _allows(bot: dict | None) -> set[str]:
    raw = (bot or {}).get("safety_allows") or []
    return {str(item) for item in raw if item}


def classify(store: Store, request, bot_id: str | None) -> Verdict:
    bot = None
    if bot_id:
        try:
            bot = store.get_bot(bot_id)
        except Exception:
            bot = None
    mode = _mode(bot)
    user_text = _user_text(store, bot_id)
    try:
        cwd = Path(turn_mod.tool_cwd()).resolve()
    except OSError:
        cwd = Path(turn_mod.tool_cwd())
    roots = _workspace_roots(store, bot_id, user_text)
    created = _created()
    remote = getattr(request, "kind", "") in {"ssh", "windows"}
    kind = getattr(request, "kind", "")
    if kind in {"search", "fetch", "research", "question", "finish", "plan", "react", "memory", "history", "project"}:
        verdict = _verdict(ALLOW, kind or "meta", "That does not change the computer.", kind, _fp(kind, getattr(request, "body", "")[:80]))
    elif kind == "files":
        path = _file_request_path(store, getattr(request, "path", "") or ".", cwd, bot_id)
        verdict = judge_files(
            getattr(request, "action", ""),
            path,
            getattr(request, "body", "") or "",
            roots=roots,
            created=created,
            mode=mode,
            user_text=user_text,
        )
    elif kind in {"shell", "ssh", "windows"}:
        command = getattr(request, "command", "") or ""
        verdict = judge_command(
            command,
            remote=remote,
            cwd=cwd,
            roots=roots,
            created=created,
            mode=mode,
            denied=_denied(bot),
        )
        if kind == "shell" and not remote:
            from easyagent.connectors import looks_like_mcp_install

            if looks_like_mcp_install(command) and verdict.rule not in {
                "guardrail-edit",
                "root-delete",
                "remote-script",
                "fork-bomb",
                "disk-format",
                "firewall-off",
                "credential-dump",
                "browser-store",
            }:
                verdict = _verdict(
                    BLOCK,
                    "mcp-install",
                    "A connector is installed from Connectors, after you review the command, the package, the version, and the environment. A page or a tool result cannot install one.",
                    command[:500],
                    _fp("mcp-install", command[:180]),
                )
        if kind in {"shell", "ssh", "windows"} and not remote:
            from easyagent.psast import data_decision

            decision = data_decision(store, command, cwd, bot_id)
            if decision == "block" and verdict.rule not in {
                "guardrail-edit",
                "root-delete",
                "remote-script",
                "fork-bomb",
                "disk-format",
                "firewall-off",
                "credential-dump",
                "browser-store",
            }:
                verdict = _verdict(
                    BLOCK,
                    "data-dir",
                    "That command reaches EasyAgent's saved chats. It was not run.",
                    verdict.detail,
                    verdict.fingerprint,
                )
            elif decision == "ask" and verdict.tier in {ALLOW, REVIEW}:
                verdict = _verdict(
                    ASK,
                    "data-unresolved",
                    "That path could not be resolved, so it was not run.",
                    verdict.detail,
                    verdict.fingerprint,
                )
        if verdict.tier == ALLOW and bot_id:
            from easyagent.browser import command_runs_download

            if command_runs_download(store, bot_id, command):
                verdict = _verdict(
                    ASK,
                    "download-exec",
                    "That runs a file this bot downloaded. It waits for you, and it was not opened on its own.",
                    verdict.detail,
                    verdict.fingerprint,
                )
    elif kind == "mcp":
        from easyagent.connectors import judge_mcp

        verdict = judge_mcp(request, store, bot_id)
    elif kind == "browser":
        from easyagent.browser import judge_browser

        verdict = judge_browser(
            request,
            allow=list((bot or {}).get("browser_allow") or []),
            deny=list((bot or {}).get("browser_deny") or []),
            store=store,
        )
    else:
        verdict = _verdict(REVIEW, "unknown-tool", "That tool is not one the rules recognize.", kind, _fp(kind))
    if verdict.fingerprint in _denied(bot):
        return _verdict(
            BLOCK,
            "already-denied",
            "You already denied that, or the card expired. It will not be asked again.",
            verdict.detail,
            verdict.fingerprint,
        )
    unlocks = {str(item) for item in ((bot or {}).get("safety_unlocks") or [])}
    if verdict.tier == BLOCK and mode == "advanced" and verdict.rule in unlocks:
        verdict = _verdict(ASK, verdict.rule, verdict.why + " You unlocked this blocked rule, so it still waits for a yes.", verdict.detail, verdict.fingerprint)
    exact = (getattr(request, "command", "") or getattr(request, "path", "") or "").strip()
    if verdict.tier == ASK and exact in _allows(bot):
        verdict = _verdict(ALLOW, "always", "You always allow this exact command for this bot.", verdict.detail, verdict.fingerprint)
    if kind == "browser":
        needle = (getattr(request, "path", "") or "").strip() if getattr(request, "action", "") == "open" else (getattr(request, "body", "") or "").strip()
        if verdict.tier == ALLOW and needle and _injected(needle, needle) and not _only_browser_label(needle):
            verdict = _verdict(ASK, "injection", "That call matches text from a file, a page, or a tool. Those are data, so it waits for you.", verdict.detail, verdict.fingerprint)
        return verdict
    needle = (getattr(request, "command", "") or getattr(request, "path", "") or verdict.detail or "").strip()
    if verdict.tier == ALLOW and _injected(needle, needle):
        verdict = _verdict(ASK, "injection", "That call matches text from a file, a page, or a tool. Those are data, so it waits for you.", verdict.detail, verdict.fingerprint)
    return verdict


def _only_browser_label(needle: str) -> bool:
    """A URL we wrote on our own snapshot is not an instruction copied off the page."""
    seen = False
    for blob in _UNTRUSTED:
        if needle not in blob:
            continue
        seen = True
        stripped = blob.replace(f"URL: {needle}", "").replace(f"— {needle}", "")
        if needle in stripped:
            return False
    return seen


def _injected(detail: str, command: str) -> bool:
    needle = (command or detail or "").strip()
    if len(needle) < 12:
        return False
    sample = needle[:180]
    for blob in _UNTRUSTED:
        if sample in blob:
            return True
    return False


def _budget() -> Verdict | None:
    slot = turn_mod.current_slot()
    if slot is None:
        return None
    started = getattr(slot, "safety_started", None)
    if started is None:
        slot.safety_started = time.monotonic()
        slot.safety_calls = 0
        started = slot.safety_started
    calls = int(getattr(slot, "safety_calls", 0) or 0) + 1
    slot.safety_calls = calls
    if calls > MAX_CALLS:
        return _verdict(BLOCK, "call-budget", f"This turn already ran {MAX_CALLS} tools. It stops so it cannot loop.", "", _fp("budget", "calls"))
    if time.monotonic() - float(started) > MAX_SECONDS:
        return _verdict(BLOCK, "time-budget", "This turn ran for twenty minutes of tool time. It stops.", "", _fp("budget", "time"))
    return None


def _remember_created(request) -> None:
    if getattr(request, "kind", "") == "files" and getattr(request, "action", "") == "write":
        slot = turn_mod.current_slot()
        try:
            cwd = Path(turn_mod.tool_cwd())
        except OSError:
            return
        store = getattr(slot, "store", None) if slot is not None else None
        bot_id = getattr(slot, "bot_id", None) if slot is not None else None
        if store is not None:
            path = _file_request_path(store, getattr(request, "path", "") or ".", cwd, bot_id)
        else:
            path = _resolve(getattr(request, "path", "") or ".", cwd)
        created = _created()
        created.add(str(path))
        try:
            created.add(str(path.resolve()))
        except OSError:
            pass


def _audit(store: Store, bot_id: str | None, row: dict) -> None:
    if not bot_id:
        return
    try:
        directory = store.root / "bots" / bot_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "safety-audit.json"
        with _AUDIT_LOCK:
            rows = read_json(path) if path.is_file() else []
            if not isinstance(rows, list):
                rows = []
            rows.append(row)
            atomic_write_json(path, rows[-200:])
    except Exception:
        return


def _remember_denial(store: Store, bot_id: str | None, fingerprint: str) -> None:
    if not bot_id or not fingerprint:
        return
    try:
        bot = store.get_bot(bot_id)
    except Exception:
        return
    denied = [str(item) for item in (bot.get("safety_denied") or []) if item]
    if fingerprint not in denied:
        denied.append(fingerprint)
    bot["safety_denied"] = denied[-200:]
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)


def _remember_allow(store: Store, bot_id: str, exact: str) -> None:
    bot = store.get_bot(bot_id)
    allows = [str(item) for item in (bot.get("safety_allows") or []) if item]
    if exact and exact not in allows:
        allows.append(exact)
    bot["safety_allows"] = allows[-100:]
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)


def approval_timeout() -> float:
    raw = (os.environ.get("EASYAGENT_APPROVAL_SECONDS") or "").strip()
    if not raw:
        return float(APPROVAL_SECONDS)
    try:
        return max(0.01, float(raw))
    except ValueError:
        return float(APPROVAL_SECONDS)


def list_pending(bot_id: str) -> list[dict]:
    rows = []
    for card in _PENDING.values():
        if card.bot_id == bot_id and not card.event.is_set():
            rows.append(_public_card(card))
    return rows


def _public_card(card: Pending) -> dict:
    return {
        "id": card.id,
        "bot_id": card.bot_id,
        "tier": card.tier,
        "rule": card.rule,
        "why": card.why,
        "detail": card.detail,
        "offer_always": card.offer_always,
        "proposal": card.proposal,
    }


def resolve_card(card_id: str, decision: str) -> Pending | None:
    card = _PENDING.get(card_id)
    if card is None or card.event.is_set():
        return card
    if decision not in {"approve", "deny", "always", "done"}:
        decision = "deny"
    if decision == "done" and (card.proposal or {}).get("kind") != "takeover":
        decision = "deny"
    if decision == "always" and not card.offer_always:
        decision = "approve"
    card.decision = decision
    card.event.set()
    if (card.proposal or {}).get("kind") == "mcp-install":
        from easyagent.connectors import finish_install

        try:
            finish_install(card.id, decision)
        except Exception:
            return card
    return card


async def _wait(card: Pending) -> str:
    try:
        await asyncio.wait_for(card.event.wait(), approval_timeout())
    except asyncio.TimeoutError:
        card.decision = "expired"
        card.event.set()
    return card.decision or "deny"


def _notify(why: str) -> None:
    try:
        from easyagent.notify import deliver

        deliver("EasyAgent", why[:180] or "A bot is waiting for approval.")
    except Exception:
        return


def _snapshot(store: Store, path: Path) -> None:
    if not path.is_file():
        return
    folder = store.root / "trash" / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{new_id()}-{path.name}"
    shutil.copy2(path, dest)
    index = store.root / "trash" / "index.json"
    rows = read_json(index) if index.is_file() else []
    if not isinstance(rows, list):
        rows = []
    rows.append({"id": dest.stem, "name": path.name, "from": str(path), "at": now_iso(), "kind": "snapshot"})
    atomic_write_json(index, rows[-200:])


def _trash_paths(store: Store, paths: list[Path]) -> str:
    folder_id = new_id()
    dest_root = store.root / "trash" / folder_id
    dest_root.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in paths:
        if not path.exists():
            continue
        target = dest_root / path.name
        if target.exists():
            target = dest_root / f"{path.stem}-{new_id()[:6]}{path.suffix}"
        shutil.move(str(path), str(target))
        moved.append(path.name)
    index = store.root / "trash" / "index.json"
    rows = read_json(index) if index.is_file() else []
    if not isinstance(rows, list):
        rows = []
    rows.append({"id": folder_id, "names": moved, "from": [str(path) for path in paths], "at": now_iso(), "kind": "delete"})
    atomic_write_json(index, rows[-200:])
    if not moved:
        return "Nothing was there to delete."
    return "Moved to EasyAgent Trash: " + ", ".join(moved) + ". Restore it from Settings."


def list_trash(store: Store) -> list[dict]:
    path = store.root / "trash" / "index.json"
    if not path.is_file():
        return []
    rows = read_json(path)
    return rows if isinstance(rows, list) else []


def restore_trash(store: Store, item_id: str) -> str:
    rows = list_trash(store)
    match = next((row for row in rows if row.get("id") == item_id), None)
    if match is None:
        raise FileNotFoundError(item_id)
    if match.get("kind") == "snapshot":
        src = store.root / "trash" / "snapshots"
        found = list(src.glob(item_id + "*")) if src.is_dir() else []
        if not found:
            raise FileNotFoundError(item_id)
        target = Path(match.get("from") or "")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(found[0], target)
        return f"Restored {target}"
    folder = store.root / "trash" / item_id
    origins = match.get("from") or []
    names = match.get("names") or []
    restored = []
    for name, origin in zip(names, origins):
        src = folder / name
        if not src.exists():
            continue
        target = Path(origin)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            continue
        shutil.move(str(src), str(target))
        restored.append(str(target))
    return "Restored " + ", ".join(restored) if restored else "Nothing was restored."


def _pure_delete_paths(command: str) -> list[Path] | None:
    line = _unwrap(command)
    if not _DELETE.search(line) or _split_chain(line) != [line.strip()]:
        return None
    if re.search(r"(?i)(^|\s)>{1,2}", line):
        return None
    try:
        cwd = Path(turn_mod.tool_cwd()).resolve()
    except OSError:
        cwd = Path(turn_mod.tool_cwd())
    paths = _targets_after_verb(line, cwd)
    return paths or None


async def _review_with_model(store: Store, bot_id: str | None, verdict: Verdict) -> Verdict:
    if (os.environ.get("EASYAGENT_SAFETY_REVIEW") or "").strip() == "0":
        return _verdict(ASK, "unreviewed", "The rules did not recognize that, and no reviewer ran, so it waits for you.", verdict.detail, verdict.fingerprint)
    if not bot_id:
        return _verdict(ASK, "unreviewed", "The rules did not recognize that, so it waits for you.", verdict.detail, verdict.fingerprint)
    try:
        bot = store.get_bot(bot_id)
        endpoint = store.get_endpoint(bot["endpoint_id"])
    except Exception:
        return _verdict(ASK, "unreviewed", "The rules did not recognize that, so it waits for you.", verdict.detail, verdict.fingerprint)
    if endpoint is None:
        return _verdict(ASK, "unreviewed", "The rules did not recognize that, so it waits for you.", verdict.detail, verdict.fingerprint)
    from easyagent import llm

    prompt = (
        "You are the safety reviewer for this same bot. No other model is used. "
        "Reply with JSON only: {\"tier\":\"allow\"|\"ask\"|\"block\",\"why\":\"one sentence\"}. "
        "allow is only for a read, a listing, or a harmless print. "
        "ask is for anything that changes a file, the system, or the network. "
        "block is for wiping a disk, dumping passwords, or running a downloaded script. "
        f"Command or path:\n{verdict.detail}"
    )
    try:
        text = await llm.complete(
            base_url=endpoint["base_url"],
            api_key=endpoint.get("api_key") or None,
            model=(bot.get("model") or endpoint.get("model") or None),
            messages=[{"role": "user", "content": prompt}],
            tools=False,
        )
        data = llm.extract_json_text(text)
        import json

        parsed = json.loads(data)
        tier = str(parsed.get("tier") or "").lower()
        why = " ".join(str(parsed.get("why") or "").split()) or verdict.why
        if tier not in {ALLOW, ASK, BLOCK}:
            raise ValueError(tier)
        return _verdict(tier, "model-review", why, verdict.detail, verdict.fingerprint)
    except Exception:
        return _verdict(ASK, "unreviewed", "The rules did not recognize that, so it waits for you.", verdict.detail, verdict.fingerprint)


async def guard(store: Store, request, bot_id: str | None):
    """Return (request, early_result). early_result is set when the tool must not run."""
    from easyagent.tools import ToolError

    budget = _budget()
    verdict = budget or classify(store, request, bot_id)
    if verdict.tier == REVIEW:
        verdict = await _review_with_model(store, bot_id, verdict)
    proposal = None
    if getattr(request, "kind", "") == "browser" and str(verdict.rule).startswith("browser-"):
        from easyagent.browser import card_proposal

        proposal = card_proposal(request, verdict.rule)
    if getattr(request, "kind", "") == "mcp" and verdict.rule == "mcp-ask":
        proposal = {
            "kind": "mcp",
            "server": getattr(request, "path", "") or "",
            "tool": getattr(request, "command", "") or "",
            "arguments": verdict.detail,
        }
    if verdict.rule == "browser-secret":
        return await _hand_browser(store, request, bot_id, verdict, proposal)
    if verdict.tier == BLOCK:
        _audit(store, bot_id, {"at": now_iso(), "decision": "block", "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
        if verdict.rule == "already-denied":
            _remember_denial(store, bot_id, verdict.fingerprint)
        raise ToolError(verdict.why + " It was not run.")
    if verdict.tier == ASK and is_unattended():
        exact = (getattr(request, "command", "") or getattr(request, "path", "") or "").strip()
        card = _make_card(bot_id, verdict, exact, proposal)
        _PENDING[card.id] = card
        _notify(verdict.why)
        _audit(store, bot_id, {"at": now_iso(), "decision": "ask", "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
        asyncio.create_task(_settle_unattended(store, request, bot_id, card, verdict))
        raise ToolError("That needs you. A card is in the chat. This routine went on without it. If nobody answers, that counts as a denial.")
    if verdict.tier == ASK:
        exact = (getattr(request, "command", "") or getattr(request, "path", "") or "").strip()
        card = Pending(
            id=new_id(),
            bot_id=bot_id or "",
            tier=ASK,
            rule=verdict.rule,
            why=verdict.why,
            detail=verdict.detail,
            fingerprint=verdict.fingerprint,
            exact=exact,
            offer_always=verdict.tier == ASK and verdict.rule != "already-denied" and proposal is None,
            created=time.time(),
            proposal=proposal,
        )
        # A blocked rule that was unlocked is still not offered "always".
        if verdict.rule in {"root-delete", "disk-format", "fork-bomb", "remote-script", "firewall-off", "credential-dump", "browser-store", "guardrail-edit", "browser-pay", "browser-login", "browser-post", "browser-settings", "browser-submit", "browser-secret", "mcp-ask", "mcp-install"}:
            card.offer_always = False
        _PENDING[card.id] = card
        _notify(verdict.why)
        slot = turn_mod.current_slot()
        if slot is not None:
            queue = getattr(slot, "safety_events", None)
            if queue is not None:
                queue.put_nowait(("approval", card.id))
        decision = await _wait(card)
        _PENDING.pop(card.id, None)
        if decision == "always" and bot_id and exact:
            _remember_allow(store, bot_id, exact)
            decision = "approve"
        _audit(
            store,
            bot_id,
            {"at": now_iso(), "decision": decision, "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail},
        )
        if decision != "approve":
            _remember_denial(store, bot_id, verdict.fingerprint)
            raise ToolError("You denied that, or the card expired. It will not be asked again, and it was not run.")
        if getattr(request, "kind", "") == "shell":
            paths = _pure_delete_paths(getattr(request, "command", "") or "")
            if paths is not None:
                text = _trash_paths(store, paths)
                return request, text
        if verdict.rule in {"overwrite", "overwrite-workspace", "overwrite-requested", "redirect-overwrite"}:
            try:
                cwd = Path(turn_mod.tool_cwd())
            except OSError:
                cwd = Path(".")
            for path in _paths_in(verdict.detail, cwd):
                _snapshot(store, path)
            if getattr(request, "kind", "") == "files":
                _snapshot(store, _resolve(getattr(request, "path", ""), cwd))
    if verdict.tier == ALLOW and verdict.rule in {"overwrite-workspace", "overwrite-requested"}:
        try:
            cwd = Path(turn_mod.tool_cwd())
        except OSError:
            cwd = Path(".")
        if getattr(request, "kind", "") == "files":
            _snapshot(store, _resolve(getattr(request, "path", ""), cwd))
    _remember_created(request)
    return request, None


def _make_card(bot_id: str | None, verdict: Verdict, exact: str, proposal: dict | None = None) -> Pending:
    card = Pending(
        id=new_id(),
        bot_id=bot_id or "",
        tier=ASK,
        rule=verdict.rule,
        why=verdict.why,
        detail=verdict.detail,
        fingerprint=verdict.fingerprint,
        exact=exact,
        offer_always=verdict.rule != "already-denied" and proposal is None,
        created=time.time(),
        proposal=proposal,
    )
    if verdict.rule in {"root-delete", "disk-format", "fork-bomb", "remote-script", "firewall-off", "credential-dump", "browser-store", "guardrail-edit", "browser-pay", "browser-login", "browser-post", "browser-settings", "browser-submit", "browser-secret", "mcp-ask", "mcp-install"}:
        card.offer_always = False
    return card


async def _settle_unattended(store: Store, request, bot_id: str | None, card: Pending, verdict: Verdict) -> None:
    """The card stays until someone answers. Expiry is a denial and is remembered."""
    decision = await _wait(card)
    if decision == "always" and bot_id and card.exact:
        _remember_allow(store, bot_id, card.exact)
        decision = "approve"
    _audit(store, bot_id, {"at": now_iso(), "decision": decision, "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
    if decision != "approve":
        _remember_denial(store, bot_id, verdict.fingerprint)
    _PENDING.pop(card.id, None)
    if decision != "approve":
        return
    try:
        from easyagent.tools import _execute

        if getattr(request, "kind", "") == "shell":
            paths = _pure_delete_paths(getattr(request, "command", "") or "")
            if paths is not None:
                _trash_paths(store, paths)
                return
        if getattr(request, "kind", "") == "browser":
            from easyagent.browser import perform

            await asyncio.to_thread(perform, store, request, bot_id)
            return
        if getattr(request, "kind", "") == "mcp":
            from easyagent.connectors import invoke

            await asyncio.to_thread(invoke, store, request, bot_id)
            return
        if verdict.rule in {"overwrite", "overwrite-workspace", "overwrite-requested", "redirect-overwrite"}:
            try:
                cwd = Path(turn_mod.tool_cwd())
            except OSError:
                cwd = Path(".")
            for path in _paths_in(verdict.detail, cwd):
                _snapshot(store, path)
            if getattr(request, "kind", "") == "files":
                _snapshot(store, _resolve(getattr(request, "path", ""), cwd))
        await asyncio.to_thread(_execute, store, request, bot_id)
    except Exception:
        return


def note_takeover(card_id: str) -> Pending | None:
    """The person is using the browser window. The card stays up until Done."""
    card = _PENDING.get(card_id)
    if card is None or card.event.is_set():
        return None
    if (card.proposal or {}).get("kind") != "takeover":
        return None
    card.proposal = {**card.proposal, "handed": True}
    return card


async def _hand_browser(store: Store, request, bot_id: str | None, verdict: Verdict, proposal: dict | None):
    """A secret is never typed. The person uses the browser window, then clicks Done."""
    from easyagent.tools import ToolError

    card = _make_card(bot_id, verdict, "", proposal)
    card.offer_always = False
    _PENDING[card.id] = card
    _notify(verdict.why)
    _audit(store, bot_id, {"at": now_iso(), "decision": "ask", "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
    if is_unattended():
        asyncio.create_task(_settle_secret(store, bot_id, card, verdict))
        raise ToolError("That field is a password, a card number, or a 2FA code. A card is in the chat. It was not typed.")
    decision = await _wait(card)
    _PENDING.pop(card.id, None)
    _audit(store, bot_id, {"at": now_iso(), "decision": decision or "deny", "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
    if decision != "done":
        _remember_denial(store, bot_id, verdict.fingerprint)
        raise ToolError("You denied that. EasyAgent did not type a password, a card number, or a 2FA code.")
    return request, "EasyAgent did not type a password, a card number, or a 2FA code. The browser window was yours."


async def _settle_secret(store: Store, bot_id: str | None, card: Pending, verdict: Verdict) -> None:
    decision = await _wait(card)
    _audit(store, bot_id, {"at": now_iso(), "decision": decision or "deny", "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
    if decision == "deny":
        _remember_denial(store, bot_id, verdict.fingerprint)
    _PENDING.pop(card.id, None)


_ROUTINE_TARGETS = ("schedules.json", "routines-trash.json", "host.json", "bot.json")


def unattended_blocked_target(request) -> bool:
    """A routine must not rewrite its own schedule, the host toggles, or safety settings."""
    if not is_unattended():
        return False
    blob = "\n".join([
        str(getattr(request, "path", "") or ""),
        str(getattr(request, "command", "") or ""),
    ]).lower().replace("\\", "/")
    if any(name in blob for name in _ROUTINE_TARGETS):
        return True
    if "safety_mode" in blob or "safety_unlocks" in blob or "safety_allows" in blob:
        return True
    return False


def read_audit(store: Store, bot_id: str) -> list[dict]:
    path = store.root / "bots" / bot_id / "safety-audit.json"
    if not path.is_file():
        return []
    rows = read_json(path)
    return rows if isinstance(rows, list) else []


def set_mode(store: Store, bot_id: str, mode: str, confirm_name: str = "", unlocks: list[str] | None = None) -> dict:
    from easyagent.store import StoreError, names_match

    if mode not in {"careful", "normal", "advanced"}:
        raise StoreError("Safety is Careful, Normal, or Advanced.", 400)
    bot = store.get_bot(bot_id)
    if mode == "advanced" and not names_match(confirm_name, bot.get("name") or ""):
        raise StoreError("Type the bot's name to turn on Advanced.", 400)
    bot["safety_mode"] = mode
    if unlocks is not None:
        if mode != "advanced":
            bot["safety_unlocks"] = []
        else:
            bot["safety_unlocks"] = [str(item) for item in unlocks if str(item).strip()][:40]
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)
    return bot
