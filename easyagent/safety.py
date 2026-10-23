"""Safety review before a tool runs.

A deterministic rules engine decides allow, ask, or block. Anything it does
not recognize is sent to this bot's own model, and if that call cannot be
made the action is asked, not run. A denial stops this turn only. An expired card is not a denial, and neither one is remembered.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import unicodedata
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
    r"(?i)\b(?:mkfs(?:\.[a-z0-9]+)?|diskpart|wipefs|sgdisk|parted|fdisk|blkdiscard)\b|"
    r"\bdiskutil\b[^\n]*\b(?:erase\w*|partitiondisk|secureerase|zerodisk|reformat|resetfusion)\b|"
    r"\bsrm\b|"
    r"\btmutil\b[^\n]*\b(?:delete|deleteinprogress|deletelocalsnapshots|disable|removedestination|thinlocalsnapshots)\b|"
    r"\bdd\b[^\n]*\bof=/dev/|"
    r"\bformat(?:\.com)?(?:\s+/[A-Za-z0-9:]+)*\s+[a-z]:|"
    r"\b[a-z]+-(?:disk|partition|volume)\b|"
    r"\bcipher(?:\.exe)?\s+/w\b|"
    r"\bsdelete(?:64)?(?:\.exe)?\b|"
    r"\bvssadmin(?:\.exe)?\s+delete\s+shadows\b|"
    r"\bwbadmin(?:\.exe)?\s+delete\b|"
    r"\bbcdedit(?:\.exe)?\s+/delete\b|"
    r"\breg(?:\.exe)?\s+delete\s+hklm\b"
)
_BENIGN_EVAL = re.compile(
    r"(?i)^\s*eval\s+(?:"
    r"\"\$\(\s*ssh-agent(?:\s+-s)?\s*\)\""
    r"|\"\$\(\s*pyenv\s+init\b[^\"]*\)\""
    r"|\"\$\(\s*conda\s+shell\.[a-z0-9]+\s+hook\s*\)\""
    r"|\"\$\(\s*(?:\S+/)?brew\s+shellenv\b[^\"]*\)\""
    r"|`ssh-agent(?:\s+-s)?`"
    r")\s*$"
)
_PIPE_INTERPRETER = re.compile(
    r"(?i)\|\s*(?:sudo\s+)?(?:(?:ba|z|da|k)?sh|bash|zsh|dash|ksh|pwsh|powershell(?:\.exe)?|iex|invoke-expression|"
    r"python3?|perl|ruby|php|lua|node|deno|cmd(?:\.exe)?)\b"
)
_DEVICE = re.compile(
    r"(?i)/dev/(?:sd[a-z]\d*|nvme\d+n\d+(?:p\d+)?|vd[a-z]\d*|xvd[a-z]\d*|mmcblk\d+(?:p\d+)?|disk\d*|null|zero)\b"
)
_DECODE_TO_SHELL = re.compile(
    r"(?i)\b(?:base64\b[^\n|]*\s(?:-d|--decode)\b|base64\s+(?:-d|--decode)\b|"
    r"certutil(?:\.exe)?\b[^\n|]*-decode)\b"
)
_INLINE_HOST = re.compile(
    r"(?i)(?:^|[;&|\s])(?:perl|ruby|php|lua|node|deno|python\d*|py|pwsh|powershell)(?:\.exe)?\b"
)
_INLINE_DELETE_API = re.compile(
    r"(?i)(?:"
    r"\bunlink\b|\brmdir\b|\brm_rf\b|\brm_r\b|\bos\.remove\b|\bos\.execute\b|"
    r"shutil\.rmtree|\bos\.unlink\b|File\.delete|File\.unlink|FileUtils|"
    r"fs\.(?:promises\.)?(?:unlink|rm|rmSync|unlinkSync)|Deno\.remove|"
    r"Remove-Item|Clear-Disk|Format-Volume|Initialize-Disk"
    r")"
)
_DELETE_VERB = re.compile(
    r"(?i)^(?:sudo\s+|command\s+|exec\s+)?(?:rm|unlink|del|erase|rd|ri|rmdir|remove-item|shred|trash)\b"
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
    r"safety_mode\s*(?:to|=|:)\s*(?:advanced|balanced)|always (?:pick|choose|select|use) the always allow"
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
    action_class: str = ""
    action_tool: str = ""
    class_label: str = ""


_PENDING: dict[str, Pending] = {}


def clear_pending(bot_id: str | None = None) -> None:
    """Drop open cards. Nothing is approved and no denial is written."""
    if not bot_id:
        _PENDING.clear()
        return
    for card_id, card in list(_PENDING.items()):
        if card.bot_id == bot_id:
            _PENDING.pop(card_id, None)
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


_SKILL_CHECK: ContextVar[bool] = ContextVar("easyagent_skill_check", default=False)


def begin_skill_check() -> Token:
    """A learned skill's check. It does not get the interactive allow-list."""
    return _SKILL_CHECK.set(True)


def end_skill_check(token: Token) -> None:
    _SKILL_CHECK.reset(token)


def ordinary_relax() -> bool:
    """sed, mkdir, cp, and touch may run while a person is here. A routine or a skill check may not."""
    return not is_unattended() and not _SKILL_CHECK.get()


def reset_for_tests() -> None:
    _PENDING.clear()
    _UNTRUSTED.clear()
    _SNIFF_CACHE.clear()
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


def note_untrusted(text: str) -> None:
    """Remember text so a command copied from it waits.

    This does not wrap the sentence. Inbound chat stores the plain words.
    The fenced copy is what ``mark_untrusted`` returns for the model.
    """
    body = text or ""
    if _MARK_START in body:
        return
    remembered = body[:4000]
    if not remembered.strip():
        return
    _UNTRUSTED.append(remembered)
    del _UNTRUSTED[:-24]


def mark_untrusted(text: str) -> str:
    body = text or ""
    note_untrusted(body)
    if _MARK_START in body:
        return body
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
    if kind in {"files", "shell", "ssh", "windows", "search", "search_files", "mcp"} or action == "read":
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
    """Strict is the allow-list. A saved bot with no mode is Strict. Balanced and Careful are the 0.3.13 rules."""
    mode = str((bot or {}).get("safety_mode") or "strict")
    if mode == "balanced":
        mode = "careful"
    if mode not in {"careful", "normal", "advanced", "strict"}:
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


def shell_text_as_run(command: str) -> str:
    """The local shell text. On Windows the call operator is added before any rule runs."""
    if sys.platform != "win32":
        return command or ""
    from easyagent.tools import _powershell_command

    return _powershell_command(command or "")


def _escaped_double_quote(text: str, index: int) -> bool:
    """A ``"`` preceded by a live backslash or backtick is still inside the string.

    Cmd and bash use ``\\"``. PowerShell uses a backtick. An even run of
    backslashes (``\\\\"``) ends the string.
    """
    if index <= 0 or text[index] != '"':
        return False
    if text[index - 1] == "`":
        return True
    if text[index - 1] != "\\":
        return False
    slashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def _shell_segments(command: str):
    """Statements and the separators between them. Quotes stay in the statement."""
    buf: list[str] = []
    i = 0
    text = command or ""
    n = len(text)
    quote = ""
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote and not (quote == '"' and _escaped_double_quote(text, i)):
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "'":
            buf.append("$'")
            i += 2
            while i < n and text[i] != "'":
                buf.append(text[i])
                i += 1
            if i < n:
                buf.append(text[i])
                i += 1
            continue
        matched = ""
        if text.startswith("&&", i) or text.startswith("||", i):
            matched = text[i : i + 2]
        elif ch in {";", "|", "&", "\n"}:
            matched = ch
        if matched:
            yield ("stmt", "".join(buf))
            yield ("sep", matched)
            buf = []
            i += len(matched)
            continue
        buf.append(ch)
        i += 1
    yield ("stmt", "".join(buf))


def _strip_call_prefix(body: str) -> str:
    """Drop a leading call operator, dot-source, ``command``, or ``env``. A path like ``.\\cmd`` stays."""
    while body:
        if body.startswith("&"):
            nxt = body[1:].lstrip()[:1]
            if nxt == "{":
                break
            body = body[1:].lstrip()
            continue
        if body.startswith(".") and not body.startswith(("..", ".\\", "./")):
            nxt = body[1:2]
            if nxt in {"'", '"', "$", "`"} or nxt.isspace():
                body = body[1:].lstrip()
                continue
        lowered = body.lower()
        rest = ""
        for prefix in ("command", "env"):
            if lowered.startswith(prefix) and (len(body) == len(prefix) or body[len(prefix)].isspace()):
                candidate = body[len(prefix) :].lstrip()
                if candidate.startswith("-"):
                    rest = ""
                    break
                rest = candidate
                break
        else:
            break
        if not rest:
            break
        body = rest
    return body


def _raw_word_end(text: str, start: int) -> int:
    i = start
    n = len(text)
    quote = ""
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote and not (quote == '"' and _escaped_double_quote(text, i)):
                quote = ""
            i += 1
            continue
        if ch.isspace():
            break
        if ch == "$" and i + 1 < n and text[i + 1] == "'":
            i += 2
            while i < n and text[i] != "'":
                i += 1
            if i < n:
                i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            i += 1
            continue
        i += 1
    return i


def _backslash_escapes(text: str) -> bool:
    """Bash ``\\rm`` and ``\\r\\m``. A Windows path or ``.\\script`` keeps its backslashes."""
    raw = text.lstrip("'\"")
    if re.search(r"[A-Za-z]:\\", raw) or raw.startswith("\\\\") or raw.startswith(".\\") or raw.startswith("./"):
        return False
    return "\\" in raw


def _collapse_slice(text: str, *, escapes: bool) -> str:
    """One shell word, with adjacent quotes removed. Escapes apply to the program word."""
    use_backslash = escapes and _backslash_escapes(text)
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            i += 1
            while i < n and text[i] != "'":
                out.append(text[i])
                i += 1
            if i < n:
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n and not (text[i] == '"' and not _escaped_double_quote(text, i)):
                if text[i] == "\\" and i + 1 < n and text[i + 1] == '"':
                    out.append('"')
                    i += 2
                    continue
                if text[i] == "`" and i + 1 < n and text[i + 1] == '"':
                    out.append('"')
                    i += 2
                    continue
                if escapes and text[i] == "`" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                out.append(text[i])
                i += 1
            if i < n:
                i += 1
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "'":
            i += 2
            while i < n and text[i] != "'":
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                out.append(text[i])
                i += 1
            if i < n:
                i += 1
            continue
        if escapes and ch == "`" and i + 1 < n:
            out.append(text[i + 1])
            i += 2
            continue
        if escapes and ch == "^" and i + 1 < n:
            out.append(text[i + 1])
            i += 2
            continue
        if use_backslash and ch == "\\" and i + 1 < n and not text[i + 1].isspace():
            out.append(text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _emit_word(word: str, raw: str) -> str:
    """Keep a quoted argument quoted when its interior still has quotes, code, or a star.

    A second pass would otherwise read Python quotes or ``'*'`` as shell syntax.
    A bare verb such as ``format`` stays bare. A single-quoted sed address such as
    ``$p`` or a ``;`` between two substitutions stays one word and is not expanded.
    """
    if raw == word and not any(ch.isspace() for ch in word):
        return word
    if any(ch.isspace() for ch in word) or any(ch in word for ch in "'\"$*();|&`"):
        single = len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'" and "'" not in raw[1:-1]
        if single:
            return "'" + word.replace("'", "") + "'"
        return '"' + word.replace('"', "") + '"'
    return word


def _normalize_statement(statement: str) -> str:
    if not statement.strip():
        return statement
    lead = statement[: len(statement) - len(statement.lstrip())]
    tail = statement[len(statement.rstrip()) :]
    body = _strip_call_prefix(statement.strip())
    words: list[str] = []
    i = 0
    while i < len(body):
        while i < len(body) and body[i].isspace():
            i += 1
        if i >= len(body):
            break
        end = _raw_word_end(body, i)
        raw = body[i:end]
        collapsed = _collapse_slice(raw, escapes=not words)
        i = end
        if not collapsed:
            continue
        words.append(_emit_word(collapsed, raw))
    return lead + " ".join(words) + tail


def normalize_shell_command(command: str) -> str:
    """The program token the shell will run, for every verb and blocklist check.

    Fullwidth letters fold first, so ``ＦＯＲＭＡＴ`` is ``format``.

    Quotes, empty quotes, backticks, carets, and backslash escapes collapse
    inside a word the way bash, cmd, and PowerShell do. A leading ``&`` or
    dot-source ``.`` is not part of the program name. The same text is what
    the rules match on every shell.
    """
    command = unicodedata.normalize("NFKC", command or "")
    if not command:
        return command or ""
    segments = list(_shell_segments(command))
    if (
        len(segments) >= 3
        and segments[0][0] == "stmt"
        and not segments[0][1].strip()
        and segments[1] == ("sep", "&")
        and segments[2][0] == "stmt"
        and not segments[2][1].lstrip().startswith("{")
    ):
        segments = [("stmt", segments[2][1].lstrip()), *segments[3:]]
    parts: list[str] = []
    for kind, text in segments:
        if kind == "sep":
            parts.append(text)
        else:
            parts.append(_normalize_statement(text))
    return "".join(parts)


def decode_shell(command: str) -> tuple[str, str]:
    """Peel an encoded command and a cmd / powershell wrapper.

    Variables stay in the text. The data guard expands them while it resolves
    paths. Expanding first can turn a Join-Path into a slash style this
    computer does not treat as the data folder. The program token is normalized
    before and after the peel, so a quoted wrapper is the same command.
    """
    text = normalize_shell_command((command or "").strip())
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
    return normalize_shell_command(text), "ok"


def prepare_shell(command: str) -> tuple[str, str]:
    """Decoded command with variables expanded, for the rules engine."""
    text, status = decode_shell(command)
    if status == "ask":
        return text, "ask"
    return normalize_shell_command(_expand(text)), "ok"


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
            if ch == quote and not (quote == '"' and _escaped_double_quote(text, i)):
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
            prev = buf[-1] if buf else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if prev in {">", "<"} or nxt in {">", "<", "|"}:
                buf.append(ch)
                i += 1
                continue
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
    """A relative file path is this bot's workspace, not the process working directory."""
    text = (raw or ".").strip().strip('"') or "."
    expanded = Path(_expand(text)).expanduser()
    if bot_id and not expanded.is_absolute():
        from easyagent.workspace import bot_workspace

        return bot_workspace(store, bot_id) / expanded
    return _resolve(text, cwd)


def _resolve(raw: str, cwd: Path) -> Path:
    """Resolve one path. A Windows ``\\\\?\\`` prefix is removed first.

    Windows realpath keeps that prefix when the input had it, and drops it
    when the input did not. Stripping it here makes both spellings one path.
    ``os.path`` is what decides absolute and realpath, so a test can install
    ``ntpath`` and see the same decision Windows makes.
    """
    from easyagent.strictshell import _strip_extended

    text = _strip_extended(_expand(raw).strip().strip('"').strip("'"))
    if text.startswith("~"):
        text = os.path.expanduser(text)
    if text and not os.path.isabs(text):
        text = os.path.join(os.fspath(cwd), text)
    try:
        resolved = os.path.realpath(text)
    except (OSError, ValueError, RuntimeError):
        resolved = text
    # realpath can put the prefix back. The printed cwd and the folder check
    # have to see one spelling.
    resolved = _strip_extended(resolved)
    return Path(resolved)


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
    from easyagent.strictshell import _strip_extended

    text = _strip_extended(str(path))
    text = os.path.normcase(os.path.abspath(text))
    text = text.replace("/", "\\") if os.name == "nt" else text
    if text.upper().startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return text.rstrip("\\/")


def _inside(path: Path, roots: list[Path]) -> bool:
    """True when path is the root or a file inside it.

    Both sides go through realpath and the same fold as a script record.
    ``/var`` and ``/private/var`` name one directory. macOS realpath keeps the
    case you passed in, and a casefolded record key is still inside the folder.
    """
    from easyagent.strictshell import _canonical_key, _fold_text, _key_under

    path_key = _canonical_key(path) or _fold_text(os.fspath(path))
    for root in roots:
        root_key = _canonical_key(root) or _fold_text(os.fspath(root))
        if _key_under(path_key, root_key):
            return True
    return False


_ENV_TEMPLATE_MARKS = frozenset({"example", "sample", "template", "dist", "default"})
# A template mark does not hide a real env, a key, or a backup.
_ENV_REAL_PARTS = frozenset({
    "local", "production", "prod", "development", "dev", "test", "testing",
    "staging", "stage", "pem", "key", "bak", "swp", "json", "old", "backup",
    "orig", "secret", "priv", "private", "credentials", "save",
})


def _env_name_parts(name: str) -> list[str]:
    low = (name or "").strip().lower()
    return [part for part in low.split(".") if part and part != "env"]


def _env_template_name(name: str) -> bool:
    """A committed env template. ``.envrc`` and a real ``.env`` are not templates.

    The name is only a candidate. ``secret_class`` still sniffs the bytes, and a
    name that also says local, production, pem, key, bak, or json is never a template.
    Leading or trailing whitespace is not a template: ``' .env.example'`` stays a secret name.
    """
    raw = name or ""
    if raw != raw.strip():
        return False
    low = raw.strip().lower()
    if low.endswith(_SECRET_SUFFIXES) or any(part in _ENV_REAL_PARTS for part in _env_name_parts(low)):
        return False
    bare = low.rstrip(".")
    if bare in {f"{mark}.env" for mark in _ENV_TEMPLATE_MARKS}:
        return True
    if not low.startswith(".env."):
        return False
    parts = _env_name_parts(bare)
    marks = [part for part in parts if part in _ENV_TEMPLATE_MARKS]
    others = [part for part in parts if part not in _ENV_TEMPLATE_MARKS]
    return bool(marks) and not others


def _secret_kind(path: Path) -> str:
    name = path.name.lower()
    text = str(path).lower().replace("\\", "/")
    if name in {"cookies", "cookies.sqlite", "login data", "logins.json", "key4.db", "web data"}:
        return "browser"
    if "cookies" in text and ("chrome" in text or "firefox" in text or "safari" in text or "edge" in text):
        return "browser"
    if _env_template_name(name):
        return ""
    if name in {".env", ".env.local", ".env.production"} or name.startswith(".env."):
        return "secret"
    if name in {"id_rsa", "id_ed25519", "id_ecdsa"} or name.endswith(".pem"):
        return "secret"
    if "wallet" in name or name in {"keystore", "credentials", "secrets.json"}:
        return "secret"
    if text.endswith("/etc/shadow") or text.endswith("/etc/gshadow"):
        return "browser"
    return ""


# Names and folders read_file cards, plus credential, key, and token files
# the same card must cover. Search drops these files instead of printing a line.
_SECRET_NAMES = frozenset({
    "id_rsa",
    "id_dsa",
    "id_ed25519",
    "id_ecdsa",
    "id_rsa.pub",
    "id_dsa.pub",
    "id_ed25519.pub",
    "id_ecdsa.pub",
    "credentials",
    "credentials.json",
    "credentials.yml",
    "credentials.yaml",
    "credentials.xml",
    "credential.json",
    "creds",
    "creds.json",
    "creds.yml",
    "creds.yaml",
    "creds.txt",
    "token.json",
    "tokens.json",
    "token.yaml",
    "tokens.yaml",
    "token.yml",
    "tokens.yml",
    "access_token",
    "access_token.json",
    "api_token.json",
    "secrets.json",
    "secret.json",
    "kubeconfig",
    "secrets.db",
    "secrets.passphrase",
    ".netrc",
    ".pgpass",
    ".token",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".jks", ".token")
_SECRET_DIRS = frozenset({".ssh", ".gnupg", ".aws"})
_TEXT_SUFFIXES = frozenset({
    "", ".txt", ".text", ".json", ".yml", ".yaml", ".xml", ".env",
    ".conf", ".cfg", ".ini", ".toml", ".csv", ".log",
})
_SNIFF_CACHE: dict[tuple, str | None] = {}
_SNIFF_MAX = 1_000_000
_TEMPLATE_CAP = 16_000_000
_ASSIGN_LINE = re.compile(
    br"(?im)^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]{0,60})\s*=\s*['\"]?([^\s'\"#]*)"
)
_SECRET_KEY_NAME = re.compile(
    br"(?i)(?:pass(?:word|wd)?|secret|token|api[_-]?key|auth|credential|private)"
)
_PRIVATE_KEY = re.compile(br"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_PUTTY_KEY = b"PuTTY-User-Key-File"
_AUTH_ASSIGN = re.compile(br"(?i)_auth(?:Token)?\s*=")
_AWS_SECRET = re.compile(br"(?i)\b(?:AWS_SECRET_ACCESS_KEY|aws_secret)\b")
_TOKEN_SHAPE = re.compile(
    br"(?:ghp_|gho_|github_pat_|sk_live_|sk_test_|sk-|xox[bp]-)[A-Za-z0-9_\-]{8,}"
)
_NAMED_SECRET = re.compile(
    br"(?im)^[^\r\n]{0,80}\b(?:token|secret|password|api[_-]?key)\b[^\r\n]{0,40}"
    br"[=:][ \t]*['\"]?([A-Za-z0-9+/_=.-]{32,})"
)
_ASSIGN_VALUE = re.compile(br"(?im)^[A-Za-z_][A-Za-z0-9_]{0,60}\s*=\s*['\"]?([^\s'\"#]{16,})")
_OBVIOUS_PLACEHOLDER = re.compile(
    br"(?i)(?:your[_\-]|changeme|change_me|placeholder|example|xxx+|todo|replace|dummy|sample|<.*>|insert|password123)"
)


def _basename_is_secret(name: str) -> bool:
    """True when this one component is a secret name. It does not open a file.

    The caller passes either the raw component or a name that was already
    normalized (case, trailing dot, NTFS stream). A leading or trailing space
    on a template is still a secret name.
    """
    raw = name or ""
    if raw != raw.strip() and (
        _env_template_name(raw.strip()) or raw.strip().lower().startswith(".env")
    ):
        return True
    low = raw.lower()
    if "env" in low and any(part in _ENV_REAL_PARTS for part in _env_name_parts(low)):
        return True
    if _env_template_name(raw):
        return False
    if low in {".env", ".env.local", ".env.production"} or low.startswith(".env."):
        return True
    if low in {"id_rsa", "id_ed25519", "id_ecdsa"} or low.endswith(".pem"):
        return True
    if low in {"cookies", "cookies.sqlite", "login data", "logins.json", "key4.db", "web data"}:
        return True
    if "wallet" in low or low in {"keystore", "credentials", "secrets.json"}:
        return True
    if low in _SECRET_NAMES or low.endswith(_SECRET_SUFFIXES):
        return True
    suffix = Path(low).suffix.lower()
    stem = Path(low).stem.lower()
    if "token" in stem and suffix in _TEXT_SUFFIXES:
        return True
    return False


def _classify_names(path: Path) -> str | None:
    """Name, suffix, and directory classification. Does not open the file.

    The Windows spelling is folded first: backslashes, a drive letter, ``\\\\?\\``,
    case, a trailing dot or space, and an NTFS stream. An 8.3 short name is
    folded only on Windows, and only when that path is already on disk.
    """
    from easyagent.strictshell import secret_basename, secret_components, windows_long_basename

    raw = path.name
    if _basename_is_secret(raw):
        return "name"
    normalized = secret_basename(os.fspath(path))
    if normalized and _basename_is_secret(normalized):
        return "name"
    long_name = windows_long_basename(path)
    if long_name and _basename_is_secret(long_name):
        return "name"
    if _secret_kind(path):
        return "name"
    parts: set[str] = set()
    try:
        parts.update(part.lower() for part in path.parts)
    except (OSError, ValueError):
        parts = set()
    parts.update(part.casefold() for part in secret_components(os.fspath(path)))
    if parts & _SECRET_DIRS:
        return "name"
    return None


def _name_secret(path: Path) -> str | None:
    reason = _classify_names(path)
    if reason:
        return reason
    try:
        link = path.is_symlink()
    except OSError:
        return None
    if not link:
        return None
    try:
        target = path.resolve()
    except OSError:
        return "symlink"
    return _classify_names(target)


def _shares_secret_inode(path: Path) -> bool:
    """A hardlink keeps the secret bytes under a bland name."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or not st.st_ino:
            return False
        siblings = list(path.parent.iterdir())
    except OSError:
        return False
    for entry in siblings:
        if entry.name == path.name:
            continue
        try:
            if entry.is_symlink():
                continue
            other = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(other.st_mode):
            continue
        if other.st_ino != st.st_ino or other.st_dev != st.st_dev:
            continue
        if _name_secret(entry) is None:
            continue
        return True
    return False


def _sniff_blob(blob: bytes) -> str | None:
    if not blob:
        return None
    if _PRIVATE_KEY.search(blob) or _PUTTY_KEY in blob:
        return "content"
    if _AUTH_ASSIGN.search(blob) or _AWS_SECRET.search(blob) or _TOKEN_SHAPE.search(blob):
        return "content"
    if _NAMED_SECRET.search(blob):
        return "content"
    return None


def sniff_secret(path: Path) -> str | None:
    """First 8 KB of a regular file at most 1 MB. A symlink that leaves the folder is not followed.

    The result is cached by path, mtime, and size. Name-classified files should
    be rejected before this runs, so a key named id_rsa is never opened.
    """
    try:
        if path.is_symlink():
            parent = path.parent.resolve()
            target = path.resolve()
            if target != parent and parent not in target.parents:
                return None
            path = target
        if not path.is_file():
            return None
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > _SNIFF_MAX or st.st_size <= 0:
            return None
        key = (str(path), st.st_mtime_ns, st.st_size)
        if key in _SNIFF_CACHE:
            return _SNIFF_CACHE[key]
        with path.open("rb") as handle:
            blob = handle.read(8192)
    except OSError:
        return None
    reason = _sniff_blob(blob)
    if len(_SNIFF_CACHE) > 4096:
        _SNIFF_CACHE.pop(next(iter(_SNIFF_CACHE)), None)
    _SNIFF_CACHE[key] = reason
    return reason


def _template_assignment_secret(blob: bytes) -> bool:
    """A template KEY=value whose value is not an obvious placeholder.

    ``COLOR=blue``, ``PORT=3000``, ``DEBUG=true``, and ``API_KEY=your_key_here``
    stay. ``DB_PASSWORD=hunter2`` and a long value on any key do not: a password
    does not have to look like a token.
    """
    for match in _ASSIGN_LINE.finditer(blob or b""):
        key, value = match.group(1), match.group(2)
        if not value or _OBVIOUS_PLACEHOLDER.search(value):
            continue
        if value.lower() in {b"true", b"false", b"null", b"none", b"yes", b"no"}:
            continue
        if re.fullmatch(br"\d{1,6}", value):
            continue
        if _SECRET_KEY_NAME.search(key) or len(value) >= 8:
            return True
    return False


def _stream_template(path: Path) -> str | None:
    """Read a template in chunks. A match stops the read. No whole-file buffer."""
    pending = b""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                pending += chunk
                if b"\n" not in pending and len(pending) > 65536:
                    if _sniff_blob(pending):
                        return "content"
                    pending = pending[-4096:]
                    continue
                lines = pending.split(b"\n")
                pending = lines[-1]
                block = b"\n".join(lines[:-1])
                if block and (_sniff_blob(block) or _template_assignment_secret(block)):
                    return "content"
    except OSError:
        return "content"
    if pending and (_sniff_blob(pending) or _template_assignment_secret(pending)):
        return "content"
    return None


def _sniff_template(path: Path) -> str | None:
    """Whole template, streamed, up to 16 MB. Larger than that is secret."""
    try:
        if path.is_symlink():
            path = path.resolve()
        if not path.is_file():
            return None
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            return None
        key = ("tmpl", str(path), st.st_mtime_ns, st.st_size)
        if key in _SNIFF_CACHE:
            return _SNIFF_CACHE[key]
        if st.st_size > _TEMPLATE_CAP:
            reason = "content"
        else:
            reason = _stream_template(path)
    except OSError:
        return "content"
    if len(_SNIFF_CACHE) > 4096:
        _SNIFF_CACHE.pop(next(iter(_SNIFF_CACHE)), None)
    _SNIFF_CACHE[key] = reason
    return reason


def _classify_resolved(path: Path) -> str | None:
    """Name, hardlink, or content. A template name is exempt only when the bytes are placeholders."""
    raw_name = path.name
    if raw_name != raw_name.strip() and (
        _env_template_name(raw_name.strip()) or raw_name.strip().lower().startswith(".env")
    ):
        return "name"
    if {part.lower() for part in path.parts} & _SECRET_DIRS:
        return "name"
    if _shares_secret_inode(path):
        return "hardlink"
    if _env_template_name(raw_name):
        # A missing template has no bytes. A file we cannot read is not proved placeholder-only.
        # nlink > 1 is a hardlink even when the secret-named sibling is not in this directory.
        try:
            if not path.is_file():
                return None
            st = path.stat()
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                return "hardlink"
        except OSError:
            return "content"
        sniffed = _sniff_template(path)
        if sniffed:
            return sniffed
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except OSError:
            return "content"
        return None
    reason = _name_secret(path)
    if reason:
        return reason
    return sniff_secret(path)


def secret_class(path: Path) -> str | None:
    """Why this path is a secret, or None. One classifier for read, write, search, and the shell.

    A template name is not an exemption by itself. The bytes have to be placeholder-only,
    the name cannot also be a real env or a key, and a symlink is classified as its target.
    The name is classified before any stat, so a trailing dot or an NTFS stream does not
    have to be a file the operating system can open.
    """
    named = _classify_names(path)
    if named:
        return named
    try:
        linked = path.is_symlink()
    except OSError:
        linked = False
    if linked:
        try:
            target = path.resolve()
        except OSError:
            return "symlink"
        reason = _classify_resolved(target)
        if reason:
            return "symlink"
        if _env_template_name(path.name):
            return None
        return _classify_resolved(path)
    return _classify_resolved(path)


def secret_file(path: Path) -> bool:
    """True for a key, a credential, a token file, or anything _secret_kind already cards.

    A search must not open these files. read_file and patch card them first.
    """
    return secret_class(path) is not None


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
    if "/chats/" in text and (name.endswith(".queue.json") or name.endswith(".plan.json")):
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


_WRAP_DEPTH = 8
_SHELL_C = {"bash", "sh", "zsh", "dash"}
_PS_NAMES = {"powershell", "pwsh"}
_EVAL_NAMES = {"eval", "invoke-expression", "iex"}
_SUDO_NAMES = {"sudo", "doas"}
_ICM_NAMES = {"invoke-command", "icm"}
_XARGS_ARG = {
    "-n",
    "-i",
    "-I",
    "-a",
    "-p",
    "-P",
    "-l",
    "-L",
    "-s",
    "-e",
    "-E",
    "-j",
    "-J",
    "-d",
    "--max-args",
    "--replace",
    "--arg-file",
    "--max-procs",
    "--max-chars",
    "--max-lines",
    "--delimiter",
}
_SUDO_ARG = {
    "-u",
    "-g",
    "-p",
    "-h",
    "-c",
    "-C",
    "-d",
    "-D",
    "-r",
    "-t",
    "-T",
    "--user",
    "--group",
    "--prompt",
    "--chdir",
    "--close-from",
    "--role",
    "--command",
}


@dataclass(frozen=True)
class _Word:
    text: str
    literal: bool
    readable: bool = True


@dataclass(frozen=True)
class _CommandView:
    uncertain: bool = False
    dynamic: bool = False
    inner: str | None = None


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _chain_at(text: str, index: int) -> int:
    """Length of a separator outside quotes. A call-operator & is still a separator here."""
    if text.startswith("&&", index) or text.startswith("||", index):
        return 2
    if index < len(text) and text[index] in {";", "|", "\n", "&"}:
        return 1
    return 0


def _cut_chain(text: str) -> str:
    """Arguments of one program, stopping before the next statement."""
    index = 0
    quote = ""
    while index < len(text):
        ch = text[index]
        if quote:
            if ch == quote and not (quote == '"' and _escaped_double_quote(text, index)):
                quote = ""
            index += 1
            continue
        if ch == "$" and index + 1 < len(text) and text[index + 1] == "'":
            index += 2
            while index < len(text) and text[index] != "'":
                index += 1
            if index >= len(text):
                break
            index += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            index += 1
            continue
        if _chain_at(text, index):
            return text[:index].strip()
        index += 1
    return text.strip()


def _scan_uncertain(text: str) -> bool:
    """Unbalanced quotes, an unclosed ``$'``, or an escape with nothing after it."""
    index = 0
    quote = ""
    while index < len(text):
        ch = text[index]
        if quote == "'":
            if ch == "'":
                quote = ""
            index += 1
            continue
        if quote == '"':
            if ch == '"' and not _escaped_double_quote(text, index):
                quote = ""
            index += 1
            continue
        if ch == "$" and index + 1 < len(text) and text[index + 1] == "'":
            index += 2
            while index < len(text) and text[index] != "'":
                if text[index] == "\\" and index + 1 < len(text):
                    index += 2
                    continue
                index += 1
            if index >= len(text):
                return True
            index += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            index += 1
            continue
        if ch in {"\\", "^", "`"} and index + 1 >= len(text):
            return True
        index += 1
    return bool(quote)


def _read_quoted(text: str, index: int) -> tuple[str, int, bool, bool]:
    """Content, new index, closed, literal. A ``$`` or command substitution is not literal."""
    quote = text[index]
    index += 1
    out: list[str] = []
    literal = True
    if quote == "'":
        while index < len(text) and text[index] != "'":
            out.append(text[index])
            index += 1
        if index >= len(text):
            return "".join(out), index, False, False
        return "".join(out), index + 1, True, True
    while index < len(text) and not (text[index] == '"' and not _escaped_double_quote(text, index)):
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] == '"':
            out.append('"')
            index += 2
            continue
        if text[index] == "`" and index + 1 < len(text):
            if text[index + 1] == "`":
                literal = False
            out.append(text[index + 1])
            index += 2
            continue
        if text[index] == "$":
            literal = False
        out.append(text[index])
        index += 1
    if index >= len(text):
        return "".join(out), index, False, False
    return "".join(out), index + 1, True, literal


def _read_bare(text: str, index: int) -> tuple[str, int, bool, bool]:
    """One word. Caret, backtick, and quote pieces collapse. ``$`` is not literal."""
    out: list[str] = []
    literal = True
    closed = True
    start = index
    while index < len(text):
        if _chain_at(text, index) or text[index].isspace() or text[index] == "(":
            break
        ch = text[index]
        if ch in {"'", '"'}:
            content, index, ok, lit = _read_quoted(text, index)
            out.append(content)
            literal = literal and lit and ok
            closed = closed and ok
            continue
        if ch == "$":
            literal = False
            out.append(ch)
            index += 1
            continue
        if ch == "`":
            if index + 1 >= len(text):
                return "".join(out), index, False, False
            closer = text.find("`", index + 1)
            if closer > index + 1 and not any(text[pos].isspace() for pos in range(index + 1, closer)):
                literal = False
                out.append(text[index + 1 : closer])
                index = closer + 1
                continue
            out.append(text[index + 1])
            index += 2
            continue
        if ch == "^":
            if index + 1 >= len(text):
                return "".join(out), index, False, False
            out.append(text[index + 1])
            index += 2
            continue
        out.append(ch)
        index += 1
    if index == start:
        return "", index, closed, False
    return "".join(out), index, closed, literal and closed


def _one_literal(body: str) -> str | None:
    """A parenthesized ``("format")`` or ``(format)``. Concatenation is not a literal."""
    body = body.strip()
    if not body:
        return None
    if body[0] in {"'", '"'}:
        content, end, ok, lit = _read_quoted(body, 0)
        if ok and lit and not body[end:].strip():
            return content
        return None
    if re.fullmatch(r"[A-Za-z_][\w.-]*", body):
        return body
    return None


def _read_group(text: str, index: int) -> tuple[str | None, int, bool, bool]:
    """``(literal)`` unwraps. Anything else is dynamic. The bools are closed, dynamic."""
    index += 1
    depth = 1
    quote = ""
    start = index
    while index < len(text) and depth:
        ch = text[index]
        if quote:
            if ch == quote and not (quote == '"' and _escaped_double_quote(text, index)):
                quote = ""
            index += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            index += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                lit = _one_literal(text[start:index])
                index += 1
                if lit is None:
                    return None, index, True, True
                return lit, index, True, False
        index += 1
    return None, index, False, True


def _lex_words(text: str) -> tuple[list[_Word], bool]:
    words: list[_Word] = []
    index = 0
    uncertain = False
    while True:
        index = _skip_ws(text, index)
        if index >= len(text) or _chain_at(text, index):
            break
        if text[index] == "(":
            lit, new_index, closed, dynamic = _read_group(text, index)
            if new_index <= index:
                uncertain = True
                break
            index = new_index
            if not closed:
                uncertain = True
            if dynamic or lit is None:
                words.append(_Word("", False, readable=False))
            else:
                words.append(_Word(lit, True, True))
            continue
        if text[index] in {"'", '"'}:
            content, index, closed, lit = _read_quoted(text, index)
            while index < len(text) and text[index] in {"'", '"'}:
                more, index, ok, more_lit = _read_quoted(text, index)
                content += more
                closed = closed and ok
                lit = lit and more_lit
            if not closed:
                uncertain = True
                lit = False
            words.append(_Word(content, bool(lit and closed), True))
            continue
        content, new_index, closed, lit = _read_bare(text, index)
        if new_index <= index:
            uncertain = True
            break
        index = new_index
        if not closed:
            uncertain = True
        words.append(_Word(content, bool(lit and closed), True))
    return words, uncertain


def _join_words(words: list[_Word]) -> tuple[str | None, bool]:
    if not words:
        return None, False
    if any(not word.readable for word in words):
        return None, True
    return " ".join(word.text for word in words), any(not word.literal for word in words)


def _after_flags(words: list[_Word], arg_flags: set[str]) -> list[_Word]:
    index = 0
    while index < len(words):
        token = words[index].text
        low = token.lower()
        if low == "--":
            return words[index + 1 :]
        if words[index].literal and low.startswith("-"):
            head = low.split("=", 1)[0]
            if head in arg_flags and "=" not in token:
                index += 2
                continue
            index += 1
            continue
        if words[index].literal and _is_assignment(token):
            index += 1
            continue
        return words[index:]
    return []


def _is_assignment(token: str) -> bool:
    if "=" not in token or token.startswith("-"):
        return False
    name = token.split("=", 1)[0]
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def _script_of(args: str) -> tuple[str | None, bool]:
    words, uncertain = _lex_words(args)
    inner, dynamic = _join_words(words)
    return inner, dynamic or uncertain


def _cmd_of(args: str) -> tuple[str | None, bool]:
    words, uncertain = _lex_words(args)
    for index, word in enumerate(words):
        if word.text.lower() in {"/c", "/k"}:
            inner, dynamic = _join_words(words[index + 1 :])
            return inner, dynamic or uncertain
    return None, uncertain


def _shell_c_of(args: str) -> tuple[str | None, bool]:
    words, uncertain = _lex_words(args)
    for index, word in enumerate(words):
        token = word.text
        if token == "--":
            return _join_words(words[index + 1 :])
        if word.literal and token.startswith("-") and not token.startswith("--") and "c" in token[1:]:
            inner, dynamic = _join_words(words[index + 1 :])
            return inner, dynamic or uncertain
        if word.literal and token.startswith("-"):
            continue
    return None, uncertain


def _env_of(args: str) -> tuple[str | None, bool]:
    words, uncertain = _lex_words(args)
    index = 0
    while index < len(words):
        token = words[index].text
        low = token.lower()
        if low == "--":
            index += 1
            break
        if words[index].literal and low in {"-u", "--unset", "-c", "--chdir"}:
            index += 2
            continue
        if words[index].literal and (low.startswith("-") or _is_assignment(token)):
            index += 1
            continue
        break
    inner, dynamic = _join_words(words[index:])
    return inner, dynamic or uncertain


def _sudo_of(args: str) -> tuple[str | None, bool]:
    words, uncertain = _lex_words(args)
    inner, dynamic = _join_words(_after_flags(words, _SUDO_ARG))
    return inner, dynamic or uncertain


def _simple_of(args: str, arg_flags: set[str]) -> tuple[str | None, bool]:
    words, uncertain = _lex_words(args)
    inner, dynamic = _join_words(_after_flags(words, arg_flags))
    return inner, dynamic or uncertain


def _cmd_start_of(args: str) -> tuple[str | None, bool]:
    words, uncertain = _lex_words(args)
    index = 0
    while index < len(words):
        token = words[index].text.lower()
        if token in {"/d", "/node", "/affinity"}:
            index += 2
            continue
        if token.startswith("/"):
            index += 1
            continue
        break
    if (
        index < len(words)
        and index + 1 < len(words)
        and " " in words[index].text
        and not any(sep in words[index].text for sep in "\\/:")
    ):
        index += 1
    inner, dynamic = _join_words(words[index:])
    return inner, dynamic or uncertain


_SP_PARAMS = {
    "-filepath",
    "-filename",
    "-path",
    "-argumentlist",
    "-args",
    "-workingdirectory",
    "-verb",
    "-windowstyle",
    "-credential",
    "-loaduserprofile",
    "-nonewwindow",
    "-passthru",
    "-redirectstandarderror",
    "-redirectstandardinput",
    "-redirectstandardoutput",
    "-usenewenvironment",
    "-wait",
    "-whatif",
    "-confirm",
}


def _sp_param(token: str) -> bool:
    return token.lower() in _SP_PARAMS


def _start_process_of(args: str) -> tuple[str | None, bool]:
    words, uncertain = _lex_words(args)
    filepath = ""
    filepath_ok = True
    arguments: list[str] = []
    arguments_ok = True
    positionals: list[_Word] = []
    index = 0
    while index < len(words):
        token = words[index].text.lower()
        if token in {"-filepath", "-filename", "-path"} and index + 1 < len(words):
            filepath = words[index + 1].text
            filepath_ok = words[index + 1].literal and words[index + 1].readable
            index += 2
            continue
        if token in {"-argumentlist", "-args"}:
            index += 1
            while index < len(words) and not _sp_param(words[index].text):
                arguments.append(words[index].text)
                arguments_ok = arguments_ok and words[index].literal and words[index].readable
                index += 1
            continue
        if _sp_param(token) or (token.startswith("-") and len(token) > 2 and token[1].isalpha()):
            if index + 1 < len(words) and not _sp_param(words[index + 1].text) and not words[index + 1].text.startswith("-"):
                index += 2
            else:
                index += 1
            continue
        positionals.append(words[index])
        index += 1
    if not filepath and positionals:
        filepath = positionals[0].text
        filepath_ok = positionals[0].literal and positionals[0].readable
        positionals = positionals[1:]
    if not arguments and positionals:
        arguments = [word.text for word in positionals]
        arguments_ok = all(word.literal and word.readable for word in positionals)
    if not filepath or not filepath_ok:
        return None, True
    inner = filepath if not arguments else filepath + " " + " ".join(arguments)
    return inner, (not arguments_ok) or uncertain


def _scriptblock(text: str) -> tuple[str | None, bool]:
    """Body of the first ``{...}`` and whether a brace was left open."""
    index = 0
    quote = ""
    while index < len(text):
        ch = text[index]
        if quote:
            if ch == quote and not (quote == '"' and _escaped_double_quote(text, index)):
                quote = ""
            index += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            index += 1
            continue
        if ch == "{":
            depth = 1
            index += 1
            start = index
            while index < len(text) and depth:
                if text[index] in {"'", '"'}:
                    inner_quote = text[index]
                    index += 1
                    while index < len(text) and not (
                        text[index] == inner_quote and not (inner_quote == '"' and _escaped_double_quote(text, index))
                    ):
                        index += 1
                    if index < len(text):
                        index += 1
                    continue
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start:index].strip(), False
                index += 1
            return None, True
        index += 1
    return None, False


def _base_name(program: str) -> str:
    name = program.strip().strip('"').strip("'").lower().replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _parse_invocation(text: str) -> tuple[str | None, str, bool, bool, bool]:
    """Program, args, dynamic, uncertain, and whether a parenthesized literal was unwrapped."""
    index = _skip_ws(text, 0)
    uncertain = False
    while index < len(text):
        if text.startswith("&&", index):
            break
        if text[index] == "&":
            if text[index + 1 : index + 2] == "{":
                return None, text[index:].strip(), True, uncertain, False
            index = _skip_ws(text, index + 1)
            continue
        if text[index] == "." and not text.startswith(("..", "./", ".\\"), index):
            nxt = text[index + 1 : index + 2]
            if nxt in {"'", '"', "$", "`", "(", " ", "\t"}:
                index = _skip_ws(text, index + 1)
                continue
        break
    if index >= len(text):
        return None, "", True, uncertain, False
    unwrapped = False
    if text[index] == "(":
        lit, index, closed, dynamic = _read_group(text, index)
        if not closed:
            uncertain = True
        args = _cut_chain(text[_skip_ws(text, index) :])
        if dynamic or lit is None:
            return None, args, True, uncertain, True
        return lit, args, False, uncertain, True
    if text[index] in {"'", '"'}:
        content, index, closed, lit = _read_quoted(text, index)
        while index < len(text) and text[index] in {"'", '"'}:
            more, index, ok, more_lit = _read_quoted(text, index)
            content += more
            closed = closed and ok
            lit = lit and more_lit
        if not closed:
            uncertain = True
            lit = False
        args = _cut_chain(text[_skip_ws(text, index) :])
        if not lit:
            return None, args, True, uncertain, False
        program = content
    else:
        content, index, closed, lit = _read_bare(text, index)
        if not closed:
            uncertain = True
        args = _cut_chain(text[_skip_ws(text, index) :])
        if not lit or not content:
            return None, args, True, uncertain, False
        program = content
        unwrapped = False
    if any(ch in program for ch in "?*"):
        uncertain = True
    return program, args, False, uncertain, unwrapped


def _command_view(command: str) -> _CommandView:
    """Wrapper body, a dynamic program, or a command the tokenizer could not read."""
    text = (command or "").strip()
    if not text:
        return _CommandView()
    if _BENIGN_EVAL.match(text):
        return _CommandView()
    uncertain = _scan_uncertain(text)
    program, args, dynamic, parsed_uncertain, unwrapped = _parse_invocation(text)
    uncertain = uncertain or parsed_uncertain
    if program is None:
        return _CommandView(uncertain=uncertain, dynamic=True)
    base = _base_name(program)
    inner: str | None = None
    wrapped = False
    if base == "cmd":
        inner, dynamic = _cmd_of(args)
        wrapped = bool(re.search(r"(?i)(?:^|\s)/[ck](?:\s|$)", args))
    elif base in _SHELL_C:
        inner, dynamic = _shell_c_of(args)
        wrapped = inner is not None or bool(re.search(r"(?i)(?:^|\s)-[a-z]*c(?:\s|$)", args))
    elif base in _EVAL_NAMES:
        inner, dynamic = _script_of(args)
        wrapped = True
        if inner is None:
            dynamic = True
    elif base == "exec":
        inner, dynamic = _simple_of(args, {"-a", "-c"})
        wrapped = True
    elif base == "xargs":
        inner, dynamic = _simple_of(args, _XARGS_ARG)
        wrapped = bool(args.strip())
    elif base == "env":
        inner, dynamic = _env_of(args)
        wrapped = True
    elif base == "nohup":
        inner, dynamic = _simple_of(args, set())
        wrapped = bool(args.strip())
    elif base in _SUDO_NAMES:
        inner, dynamic = _sudo_of(args)
        wrapped = bool(args.strip())
    elif base == "time":
        inner, dynamic = _simple_of(args, {"-f", "--format", "-o", "--output"})
        wrapped = bool(args.strip())
    elif base in _PS_NAMES:
        inner, dynamic = _ps_command_of(args)
        wrapped = inner is not None
    elif base == "start-process":
        inner, dynamic = _start_process_of(args)
        wrapped = True
        if inner is None:
            dynamic = True
    elif base == "start":
        inner, dynamic = _cmd_start_of(args)
        wrapped = bool(args.strip())
    elif base in _ICM_NAMES:
        block, unclosed = _scriptblock(args)
        if unclosed:
            uncertain = True
            dynamic = True
        inner = block
        wrapped = block is not None
    elif unwrapped:
        rebuilt = (program + (" " + args if args else "")).strip()
        if rebuilt and rebuilt != text:
            inner = rebuilt
    if wrapped and inner is None and base in _EVAL_NAMES | {"start-process", "cmd"}:
        dynamic = True
    if inner is not None and inner.strip() == text:
        inner = None
    return _CommandView(uncertain=uncertain, dynamic=dynamic, inner=inner)


def _ps_command_of(args: str) -> tuple[str | None, bool]:
    words, uncertain = _lex_words(args)
    index = 0
    while index < len(words):
        token = words[index].text.lower()
        if token in {"-c", "-command"}:
            inner, dynamic = _join_words(words[index + 1 :])
            return inner, dynamic or uncertain
        if token.startswith("-"):
            index += 1
            continue
        break
    return None, uncertain


def _ask_command(rule: str, why: str, command: str) -> Verdict:
    detail = (command or "").strip()
    return _verdict(ASK, rule, why, detail, _fp("cmd", detail))


def judge_command(
    command: str,
    *,
    remote: bool,
    cwd: Path,
    roots: list[Path],
    created: set[str],
    mode: str,
    denied: set[str] | None = None,
    depth: int = 0,
) -> Verdict:
    """Tier for one command. Wrappers are the inner command. A built program waits."""
    verdict = _judge_command_body(
        command,
        remote=remote,
        cwd=cwd,
        roots=roots,
        created=created,
        mode=mode,
        denied=denied,
        depth=depth,
    )
    closed = _fail_closed(
        command,
        verdict,
        depth,
        remote=remote,
        cwd=cwd,
        roots=roots,
        created=created,
        mode=mode,
        denied=denied,
    )
    if mode != "strict":
        return closed
    from easyagent.strictshell import apply

    return apply(
        command,
        closed,
        cwd=cwd,
        roots=roots,
        created=created,
        mode=mode,
        denied=denied,
        remote=remote,
        depth=depth,
    )


def _fail_closed(
    command: str,
    verdict: Verdict,
    depth: int,
    *,
    remote: bool,
    cwd: Path,
    roots: list[Path],
    created: set[str],
    mode: str,
    denied: set[str] | None,
) -> Verdict:
    """Allow is only for a command that was read and whose program is a literal."""
    if verdict.tier == BLOCK:
        return verdict
    view = _command_view(command)
    out = verdict
    if view.uncertain:
        out = _worst(
            out,
            _ask_command(
                "unparsed-command",
                "That command could not be read cleanly. It waits for you.",
                command,
            ),
        )
    if view.dynamic:
        out = _worst(
            out,
            _ask_command(
                "dynamic-command",
                "That command builds the program when it runs. It waits for you.",
                command,
            ),
        )
    inner = (view.inner or "").strip()
    if inner and inner != (command or "").strip():
        if depth >= _WRAP_DEPTH:
            out = _worst(
                out,
                _ask_command(
                    "wrapped-command",
                    "That command is nested too deep to read. It waits for you.",
                    command,
                ),
            )
        else:
            out = _worst(
                out,
                judge_command(
                    inner,
                    remote=remote,
                    cwd=cwd,
                    roots=roots,
                    created=created,
                    mode=mode,
                    denied=denied,
                    depth=depth + 1,
                ),
            )
    return out


def _quoted_words(line: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"'([^']*)'|\"([^\"]*)\"|(\S+)", line or ""):
        token = next(group for group in match.groups() if group is not None)
        if token:
            found.append(token)
    return found


def _wide_place(token: str) -> bool:
    """A root, a drive, a home directory, a system directory, or a disk device.

    ``C:/Users`` and ``C:/Users/<name>`` are the users directory and one profile.
    A folder inside the profile is a specific directory, the same as a folder
    under ``/tmp``. ``C:/Windows`` and everything under it stays a system directory.
    """
    text = (token or "").strip().strip("'\"")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    text = text.replace("\\", "/")
    if re.fullmatch(r"(?i)(?:/|/\*|~|~/|\$HOME|\$\{HOME\}|%USERPROFILE%|\$env:USERPROFILE)", text):
        return True
    if re.fullmatch(r"(?i)[A-Za-z]:/?\*?", text):
        return True
    if re.fullmatch(
        r"(?i)/dev/(?:null|zero|sd[a-z]\d*|nvme\d+n\d+(?:p\d+)?|vd[a-z]\d*|xvd[a-z]\d*|mmcblk\d+(?:p\d+)?|disk\d+)",
        text,
    ):
        return True
    if re.match(r"(?i)^(?:/etc|/usr|/bin|/boot|/sys|/proc|/System)(?:/|$)", text):
        return True
    if re.match(r"(?i)^[A-Za-z]:/(?:Windows|WinNT)(?:/|$)", text):
        return True
    if re.fullmatch(r"(?i)[A-Za-z]:/Users/?", text):
        return True
    if re.fullmatch(r"(?i)[A-Za-z]:/Users/[^/]+/?", text):
        return True
    return False


def _positionals(line: str) -> list[str]:
    words = _quoted_words(line)
    found: list[str] = []
    for token in words[1:]:
        if token.startswith("-"):
            continue
        if re.fullmatch(r"(?i)/[a-z][\w.-]*(?::.*)?", token):
            continue
        found.append(token)
    return found


def _block_root(line: str, why: str) -> Verdict:
    detail = line.strip()
    return _verdict(BLOCK, "root-delete", why, detail, _fp("cmd", detail))


def _block_disk(line: str) -> Verdict:
    detail = line.strip()
    return _verdict(BLOCK, "disk-format", "That would erase a disk. It is never run.", detail, _fp("cmd", detail))


def _ask_delete(line: str) -> Verdict:
    detail = line.strip()
    return _verdict(
        ASK,
        "delete",
        "That deletes a file. It waits for you, and an approved delete goes to Trash.",
        detail,
        _fp("cmd", detail),
    )


def _unquote_value(token: str) -> tuple[str, bool]:
    text = (token or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        inner = text[1:-1]
        return inner, text[0] == '"' and ("$" in inner or "`" in inner)
    return text, ("$" in text or "`" in text or text.startswith("(") or "+" in text)


def _alias_definition(statement: str) -> tuple[str, str, bool] | None:
    """``(name, value, unresolved)`` when this statement defines an alias."""
    stripped = statement.strip()
    bash = re.match(r"(?i)^alias\s+([A-Za-z_][\w]*)=(.*)$", stripped)
    if bash:
        value, dynamic = _unquote_value(bash.group(2).strip())
        return bash.group(1), value, dynamic or not value
    if not re.match(r"(?i)^(?:set-alias|new-alias|sal|nal)\b", stripped):
        return None
    words = _quoted_words(stripped)
    name = ""
    value = ""
    index = 1
    while index < len(words):
        token = words[index]
        low = token.lower()
        if low in {"-name", "-value", "-scope", "-option", "-description", "-passthru"}:
            if low == "-name" and index + 1 < len(words):
                name = words[index + 1]
                index += 2
                continue
            if low == "-value" and index + 1 < len(words):
                value = words[index + 1]
                index += 2
                continue
            if index + 1 < len(words) and not words[index + 1].startswith("-"):
                index += 2
                continue
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        if not name:
            name = token
        elif not value:
            value = token
        index += 1
    if not name:
        return "", "", True
    body, dynamic = _unquote_value(value)
    return name, body, dynamic or not body


def _expand_aliases(text: str) -> tuple[str, bool]:
    """Replace alias calls on this line. Unresolved targets are reported, not guessed."""
    parts = _split_chain(text)
    aliases: dict[str, str] = {}
    unresolved = False
    changed = False
    out: list[str] = []
    for part in parts:
        defined = _alias_definition(part)
        if defined is not None:
            name, value, bad = defined
            changed = True
            if bad or not name:
                unresolved = True
                continue
            aliases[name.lower()] = value
            continue
        match = re.match(r"\s*(\S+)([\s\S]*)$", part.strip())
        if match and match.group(1).lower() in aliases:
            changed = True
            out.append((aliases[match.group(1).lower()] + match.group(2)).strip())
            continue
        out.append(part.strip())
    if not changed:
        return text, False
    return " ; ".join(item for item in out if item), unresolved


def _literal_pipe_script(left: str) -> str | None:
    """The script an echo or printf feeds to a shell, when that text is literal."""
    if re.search(r"[`$]|\$\(|<\(", left):
        return None
    if re.search(r"(?i)\b(?:cat|type|get-content|gc|curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b", left):
        return None
    match = re.search(r"(?is)\b(?:echo|printf)\b\s+(?:-[\w]+\s+)*(?P<q>['\"])(?P<body>.*?)(?P=q)\s*$", left.strip())
    if not match:
        return None
    return match.group("body")


def _inline_code_effect(line: str, *, force: bool = False) -> Verdict | None:
    """Delete and format calls inside perl, ruby, php, lua, node, deno, python, or pwsh."""
    if not force and not (_INLINE_HOST.search(line) and re.search(r"(?i)(?:\s-e\b|\s-c\b|\s-r\b|\beval\b)", line)):
        return None
    if _DISK.search(line) and re.search(r"(?i)format-volume|clear-disk|initialize-disk|\bformat(?:\.com)?\s+[a-z]:", line):
        return _block_disk(line)
    if not _INLINE_DELETE_API.search(line):
        return None
    wide = bool(
        re.search(
            r"""(?i)(?:['\"]\s*/\s*['\"]|\(\s*/\s*[,)]|['\"]/\*['\"]|glob\s*[( ]?\s*['\"]/\*|['\"][A-Za-z]:\\?['\"]|/\*|\$HOME|%USERPROFILE%|\(\s*['\"][A-Za-z]:\\?)""",
            line,
        )
    )
    if wide or _DISK.search(line):
        return _block_root(line, "That would delete the disk root or the whole user profile. It is never run.")
    return _ask_delete(line)


def _command_effect(
    line: str,
    *,
    remote: bool,
    cwd: Path,
    roots: list[Path],
    created: set[str],
    mode: str,
    denied: set[str] | None,
    depth: int,
) -> Verdict | None:
    """Delete, format, and decode effects that are not a plain ``rm`` or ``format``."""
    detail = line.strip()
    if not detail:
        return None
    if _BENIGN_EVAL.match(detail):
        return _verdict(ALLOW, "harmless", "That command only reads or prints.", detail, _command_fingerprint(detail))

    def recurse(inner: str) -> Verdict:
        return judge_command(
            inner,
            remote=remote,
            cwd=cwd,
            roots=roots,
            created=created,
            mode=mode,
            denied=denied,
            depth=depth + 1,
        )

    piped = _PIPE_INTERPRETER.search(detail)
    if piped:
        left = detail[: piped.start()]
        if _DECODE_TO_SHELL.search(left) or _DECODE_TO_SHELL.search(detail):
            return _verdict(
                BLOCK,
                "remote-script",
                "That decodes a script and runs it. It is never run.",
                detail,
                _fp("cmd", detail),
            )
        literal = _literal_pipe_script(left)
        if literal is None:
            coded = _inline_code_effect(left, force=True)
            if coded is not None and coded.tier == BLOCK:
                return coded
            return _ask_command(
                "piped-shell",
                "That pipes a command into a shell. It waits for you.",
                detail,
            )
        inner = recurse(literal)
        if inner.tier == ALLOW:
            coded = _inline_code_effect(literal, force=True)
            if coded is not None:
                return coded
        return inner

    inline = _inline_code_effect(detail)
    if inline is not None:
        return inline

    redirected = re.search(r"(?i)(?:^|\s)>{1,2}\s*(\S+)", detail)
    if redirected and _DEVICE.search(redirected.group(1)):
        return _block_disk(detail)
    if re.search(r"(?i)\bblkdiscard\b", detail):
        return _block_disk(detail)
    if re.search(r"(?i)\bshred\b", detail) and _DEVICE.search(detail):
        return _block_disk(detail)
    if re.search(r"(?i)(?:^|[;&|]\s*)truncate\b", detail):
        targets = [token for token in _positionals(detail) if not re.fullmatch(r"(?i)-s\d*", token)]
        if any(_wide_place(token) for token in targets):
            return _block_disk(detail)
        return _ask_delete(detail)

    if re.match(r"(?i)find\b", detail):
        deleting = bool(re.search(r"(?i)(?:^|\s)-delete\b", detail))
        executed = re.search(r"(?i)\s-(?:exec|execdir|ok|okdir)\s+(.+?)\s+(?:\\;|;|\+)\s*", detail)
        if deleting or executed:
            places = _positionals(detail)
            root = bool(places) and _wide_place(places[0])
            if deleting:
                return _block_root(detail, "That would delete the disk root or the whole user profile. It is never run.") if root else _ask_delete(detail)
            command = executed.group(1).strip() if executed else ""
            if root and _DELETE_VERB.match(command):
                return _block_root(detail, "That would delete the disk root or the whole user profile. It is never run.")
            if command:
                return recurse(command)
            return _ask_delete(detail)

    if re.search(r"(?i)\brsync\b", detail) and re.search(r"(?i)--del(?:ete\b|-)", detail):
        places = _positionals(detail)
        if places and _wide_place(places[-1]):
            return _block_root(detail, "That would delete the disk root or the whole user profile. It is never run.")
        return _ask_delete(detail)

    if re.search(r"(?i)\brobocopy(?:\.exe)?\b", detail) and re.search(r"(?i)(?:^|\s)/(?:mir|purge)\b", detail):
        places = _positionals(detail)
        dest = places[1] if len(places) > 1 else ""
        if dest and _wide_place(dest):
            return _block_root(detail, "That would delete the disk root or the whole user profile. It is never run.")
        return _ask_delete(detail)

    forfiles = re.search(
        r"(?i)(?:^|\s)/c\s+(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\S+)",
        detail,
    )
    if re.search(r"(?i)\bforfiles(?:\.exe)?\b", detail) and forfiles:
        raw = forfiles.group(1)
        inner, _dynamic = _unquote_value(raw)
        verdict = recurse(inner or raw)
        path = re.search(r"(?i)(?:^|\s)/p\s+(\S+)", detail)
        if path and _wide_place(path.group(1)) and verdict.tier != ALLOW and (
            verdict.tier == BLOCK or _DELETE_VERB.match(inner) or re.search(r"(?i)\b(?:del|erase|rd|rm|remove-item)\b", inner)
        ):
            return _block_root(detail, "That would delete the disk root or the whole user profile. It is never run.")
        return verdict

    if re.match(r"(?i)(?:xcopy|copy(?!-)|cp|mv|move(?!-)|move-item)(?:\.exe)?\b", detail):
        places = _positionals(detail)
        if places and _wide_place(places[-1]):
            return _block_root(detail, "That would delete the disk root or the whole user profile. It is never run.")
    for match in re.finditer(
        r"(?is)(?:function\s+[A-Za-z_]\w*|[A-Za-z_]\w*\s*\(\s*\))\s*\{([^{}]*)\}",
        detail,
    ):
        body = match.group(1).strip()
        if re.match(r"(?i)(?:\$|&\s*\$|&\s*\()", body):
            return _ask_command(
                "dynamic-command",
                "That function's command could not be read. It waits for you.",
                detail,
            )
    if re.search(r"(?i)\bset-item\b.*\bfunction:", detail) and re.search(r"(?i)(?:-value\s+|function:\S+\s+)\$", detail):
        return _ask_command(
            "dynamic-command",
            "That function's command could not be read. It waits for you.",
            detail,
        )
    return None


def _judge_command_body(
    command: str,
    *,
    remote: bool,
    cwd: Path,
    roots: list[Path],
    created: set[str],
    mode: str,
    denied: set[str] | None = None,
    depth: int = 0,
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
    rewritten, alias_unresolved = _expand_aliases(text)
    if alias_unresolved or rewritten.strip() != text.strip():
        if rewritten.strip() and rewritten.strip() != text.strip():
            verdict = judge_command(
                rewritten,
                remote=remote,
                cwd=cwd,
                roots=roots,
                created=created,
                mode=mode,
                denied=denied,
                depth=depth + 1,
            )
        elif alias_unresolved:
            verdict = _ask_command(
                "dynamic-command",
                "That alias target could not be read. It waits for you.",
                text,
            )
        else:
            detail = text.strip()
            verdict = _verdict(ALLOW, "plain-command", "That is an ordinary command with no write, delete, or network post.", detail, _command_fingerprint(detail))
        if alias_unresolved and rewritten.strip():
            verdict = _worst(
                verdict,
                _ask_command(
                    "dynamic-command",
                    "That alias target could not be read. It waits for you.",
                    text,
                ),
            )
        return verdict
    effect = _command_effect(
        text,
        remote=remote,
        cwd=cwd,
        roots=roots,
        created=created,
        mode=mode,
        denied=denied,
        depth=depth,
    )
    if effect is not None:
        return effect
    if re.search(r"(?i)(cookies|login data|logins\.json|key4\.db|web data)", text):
        return _verdict(BLOCK, "browser-store", "Browser cookies and saved passwords are never read.", text.strip(), _fp("cmd", text.strip()))
    from easyagent.scriptgate import plain_loop, sed_plain_read

    if sed_plain_read(text, cwd) and ordinary_relax():
        script = text.strip()
        return _verdict(ALLOW, "read-command", "That command reads. It does not change the computer.", script, _command_fingerprint(script))
    if plain_loop(text, cwd) and ordinary_relax():
        loop = text.strip()
        return _verdict(ALLOW, "read-command", "That command reads. It does not change the computer.", loop, _command_fingerprint(loop))
    pieces = _split_chain(text)
    if len(pieces) > 1:
        verdicts = [
            judge_command(
                piece,
                remote=remote,
                cwd=cwd,
                roots=roots,
                created=created,
                mode=mode,
                denied=denied,
                depth=depth,
            )
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
        if (kind == "secret" or secret_file(path)) and _READ_CMD.search(line):
            return _verdict(ASK, "read-secret", "That reads a secret file. It waits for you, and the secret is not copied onward.", str(path), fingerprint)
    if remote and not (_HARMLESS.search(line) or _READ_CMD.search(line) or _GIT_READ.search(line)):
        return _verdict(ASK, "remote-change", "A remote computer is stricter. Anything that changes it waits for you.", detail, fingerprint)
    if _HARMLESS.search(line) or _GIT_READ.search(line):
        return _verdict(ALLOW, "harmless", "That command only reads or prints.", detail, fingerprint)
    if _READ_CMD.search(line):
        return _verdict(ALLOW, "read-command", "That command reads. It does not change the computer.", detail, fingerprint)
    from easyagent.scriptgate import awk_plain_read

    if awk_plain_read(line, cwd):
        return _verdict(ALLOW, "read-command", "That command reads. It does not change the computer.", detail, fingerprint)
    quiet = re.sub(r"\s+&\s*$", "", line)
    quiet = re.sub(r"(?i)(?:^|\s)<\s*\S+", " ", quiet)
    quiet = re.sub(r"(?:^|\s)\d*>&\d+", " ", quiet)
    if re.search(r"[|&;<>`$]", quiet):
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


def _same_canonical(left: Path, right: Path) -> bool:
    """True when both paths are the same file after symlink and case folding."""
    from easyagent.strictshell import _canonical_key

    left_key = _canonical_key(left)
    right_key = _canonical_key(right)
    if left_key and right_key:
        return left_key == right_key
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _named_paths(text: str) -> list[Path]:
    found: list[Path] = []
    for match in re.finditer(r"(?:[A-Za-z]:[\\/]|/)[^\s\"'`<>]+", text or ""):
        raw = match.group(0).rstrip(".,);:")
        if len(raw) >= 2:
            found.append(Path(raw))
    return found


def _requested_write(path: Path, user_text: str) -> bool:
    """The person named this file, or its folder, and asked for it to be written.

    The name in the sentence and the path we are about to write can differ by a
    symlink (``/var`` and ``/private/var``) or by APFS case. Those still name one file.
    """
    text = (user_text or "").replace("\\", "/")
    if not _ASKED_WRITE.search(text):
        return False
    target = str(path).replace("\\", "/")
    if len(target) >= 4 and target in text:
        return True
    for named in _named_paths(text):
        if _same_canonical(named, path):
            return True
    parent = path.parent
    parent_text = str(parent).replace("\\", "/")
    if _generic_root(parent) or len(parent_text) < 8:
        return False
    if parent_text in text:
        return True
    return any(_same_canonical(named, parent) for named in _named_paths(text))


def _is_passphrase_file(path: Path) -> bool:
    """The passphrase file, including when its name is set by the environment."""
    try:
        from easyagent.secrets import passphrase_path

        return path.resolve() == passphrase_path().resolve()
    except OSError:
        return False


def _under_data(root: Path, target: Path) -> bool:
    try:
        root = root.resolve()
        target = target.resolve()
    except OSError:
        pass
    return target == root or root in target.parents


def _work_exempt(store: Store, resolved: Path, bot_id: str | None) -> bool:
    """This bot's workspace, workbench, tmp, and notes sit outside the data-dir block.

    ``secrets.db`` stays blocked wherever it sits. Another bot's notes do not match.
    """
    if not bot_id:
        return False
    try:
        parts = resolved.resolve().relative_to(store.root.resolve()).parts
    except (OSError, ValueError):
        return False
    if resolved.name.lower() in {"secrets.db", "secrets.passphrase"}:
        return False
    return (
        len(parts) >= 3
        and parts[0] == "bots"
        and parts[1] == bot_id
        and parts[2] in {"workspace", "workbench", "tmp", "notes"}
    )


def saved_data_blocked(store: Store, resolved: Path, bot_id: str | None) -> bool:
    """True when a path is in the data folder outside this bot's own work folders.

    The folder this store opened is protected. So is EASYAGENT_DATA when it names
    a different directory. The files tool and the checker use this same test.
    """
    target = Path(resolved)
    roots = [Path(store.root)]
    env = (os.environ.get("EASYAGENT_DATA") or "").strip()
    if env:
        roots.append(Path(env))
    if not any(_under_data(root, target) for root in roots):
        return False
    return not _work_exempt(store, target, bot_id)


def _foreign_bot_data(store: Store, path: Path, bot_id: str | None) -> bool:
    """True when a file tool reaches another bot's folder inside the data directory."""
    try:
        resolved = path.resolve()
        root = Path(store.root).resolve()
    except OSError:
        return False
    bots = root / "bots"
    try:
        bots_resolved = bots.resolve()
    except OSError:
        bots_resolved = bots
    if resolved != bots_resolved and not _inside(resolved, [bots_resolved]):
        return False
    if not bot_id:
        return True
    try:
        own = store._bot_dir(bot_id).resolve()
    except OSError:
        return True
    if resolved == own or _inside(resolved, [own]):
        return False
    return True


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
    lowered = path.name.lower()
    folder = str(path).replace("\\", "/").lower()
    if "/chats/" in folder and (lowered.endswith(".queue.json") or lowered.endswith(".plan.json")):
        return _verdict(BLOCK, "data-dir", "That reads EasyAgent's saved data.", detail, fingerprint)
    secret = _secret_kind(path)
    if secret == "browser":
        return _verdict(BLOCK, "browser-store", "Browser cookies and saved passwords are never read.", detail, fingerprint)
    if action in {"list", "read"}:
        if secret == "secret" or secret_file(path):
            return _verdict(ASK, "read-secret", "That reads a secret file. It waits for you.", detail, fingerprint)
        return _verdict(ALLOW, "read", "Reading and listing are allowed.", detail, fingerprint)
    if action not in {"write", "patch"}:
        return _verdict(REVIEW, "file-other", "That file action is not one the rules recognize.", detail, fingerprint)
    if secret == "secret" or secret_file(path):
        return _verdict(ASK, "write-secret", "That writes a secret file. It waits for you.", detail, fingerprint)
    if mode == "strict" and action == "write":
        from easyagent.strictshell import place_class

        place = place_class(path, roots)
        if place in {"root", "device"}:
            return _verdict(BLOCK, "root-delete", "That would change the disk root or the whole user profile. It is never run.", detail, fingerprint)
        if place in {"system", "dotfile"}:
            return _verdict(BLOCK, "off-limits", "That changes a home login file or a system folder. It is never run.", detail, fingerprint)
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



_OWN_HOSTS = r"(?:localhost|127(?:\.\d{1,3}){0,3}|0(?:\.0){0,3}|\[?(?:0*:)*:?0*1\]?|\[?::ffff:127(?:\.\d{1,3}){3}\]?|2130706433|0x7f(?:[0-9a-f]{6})?)"


def own_server_ports() -> set[str]:
    ports = {"44721"}
    raw = (os.environ.get("EASYAGENT_PORT") or "").strip()
    if raw.isdigit():
        ports.add(str(int(raw)))
    return ports


def own_server_target(text: str) -> bool:
    """True when a tool would talk to this EasyAgent server itself.

    Its API answers approval cards, saves Always allow, and changes bot settings,
    and its page has the same buttons. A bot never reaches it: not with a shell
    command, the fetch tool, or its browser. Only this server's port is matched,
    so a bot's own dev server on another port still works.
    """
    if not text:
        return False
    ports = "|".join(sorted(re.escape(port) for port in own_server_ports()))
    if re.search(rf"(?i)://[^/\s'\"<>]*:(?:{ports})(?![0-9])", text):
        return True
    return bool(re.search(rf"(?i)(?<![\w.-]){_OWN_HOSTS}\s*:\s*(?:{ports})(?![0-9])", text))


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
    if kind in {"shell", "fetch", "research", "browser"}:
        reach = " ".join(
            str(getattr(request, field, "") or "") for field in ("command", "path", "body", "url")
        )
        if own_server_target(reach):
            return _verdict(
                BLOCK,
                "own-server",
                "That reaches EasyAgent itself, where approvals and settings live. A bot cannot answer its own card or change its own rules. It was not run.",
                reach[:500],
                _fp("own-server", reach[:180]),
            )
    if kind in {"search", "fetch", "research", "question", "finish", "plan", "react", "memory", "history", "project"}:
        verdict = _verdict(ALLOW, kind or "meta", "That does not change the computer.", kind, _fp(kind, getattr(request, "body", "")[:80]))
    elif kind == "search_files":
        path = _file_request_path(store, getattr(request, "path", "") or ".", cwd, bot_id)
        if _foreign_bot_data(store, path, bot_id):
            verdict = _verdict(BLOCK, "bot-isolation", "That is another bot's data. It stays in that bot's folder.", str(path), _fp("isolation", str(path)))
        elif saved_data_blocked(store, path, bot_id):
            verdict = _verdict(BLOCK, "data-dir", "That reads EasyAgent's saved data.", str(path), _fp("file", "read", str(path)))
        else:
            verdict = judge_files("read", path, "", roots=roots, created=created, mode=mode, user_text=user_text)
    elif kind == "files":
        path = _file_request_path(store, getattr(request, "path", "") or ".", cwd, bot_id)
        action = getattr(request, "action", "") or ""
        if _foreign_bot_data(store, path, bot_id):
            verdict = _verdict(
                BLOCK,
                "bot-isolation",
                "That is another bot's data. It stays in that bot's folder.",
                str(path),
                _fp("isolation", str(path)),
            )
        elif action in {"read", "list"} and saved_data_blocked(store, path, bot_id):
            verdict = _verdict(
                BLOCK,
                "data-dir",
                "That reads EasyAgent's saved data.",
                str(path),
                _fp("file", action, str(path)),
            )
        else:
            body = getattr(request, "body", "") or ""
            if action == "patch":
                body = getattr(request, "new_string", "") or body
            verdict = judge_files(
                getattr(request, "action", ""),
                path,
                body,
                roots=roots,
                created=created,
                mode=mode,
                user_text=user_text,
            )
    elif kind in {"shell", "ssh", "windows"}:
        command = getattr(request, "command", "") or ""
        if kind == "shell":
            command = shell_text_as_run(command)
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
            from easyagent.psast import resolved_data_decision

            decision = resolved_data_decision(store, command, cwd, bot_id)
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
        if kind == "shell" and verdict.tier != BLOCK:
            from easyagent.scriptgate import command_reads_secrets

            if command_reads_secrets(command, cwd):
                verdict = _worst(
                    verdict,
                    _verdict(
                        ASK,
                        "read-secret",
                        "That reads a secret file. It waits for you, and the secret is not copied onward.",
                        command[:500],
                        _fp("bulk-secret", command[:180]),
                    ),
                )
        if bot_id and verdict.tier != BLOCK and verdict.rule not in {
            "injection",
            "mcp-install",
            "data-dir",
            "guardrail-edit",
            "root-delete",
            "remote-script",
            "fork-bomb",
            "disk-format",
            "firewall-off",
            "credential-dump",
            "browser-store",
        }:
            from easyagent.browser import command_runs_download

            # A downloaded file still waits under the download rule. Scanning its
            # bytes can also look like a secret read; the download card is the
            # one that names why this file must not run. YOLO allows it in guard.
            if command_runs_download(store, bot_id, command) and (
                verdict.tier == ALLOW or verdict.rule == "read-secret"
            ):
                verdict = _verdict(
                    ASK,
                    "download-exec",
                    "That runs a file this bot downloaded. It waits for you, and it was not opened on its own.",
                    verdict.detail,
                    verdict.fingerprint,
                )
    elif kind == "skill":
        verdict = _verdict(
            ALLOW,
            "skill",
            "A skill file stays in this bot's skills folder. It does not change approvals.",
            getattr(request, "path", "") or "",
            _fp("skill", getattr(request, "action", ""), getattr(request, "path", "")),
        )
    elif kind == "delegate":
        verdict = _verdict(
            ALLOW,
            "delegate",
            "Starting a child does not approve its tools. Each tool the child uses is judged on its own.",
            (getattr(request, "body", "") or "")[:180],
            _fp("delegate", (getattr(request, "body", "") or "")[:80]),
        )
    elif kind == "mcp":
        from easyagent.connectors import judge_mcp

        verdict = judge_mcp(request, store, bot_id)
    elif kind == "bridge":
        from easyagent.bridges import judge_bridge

        verdict = judge_bridge(request, store, bot_id)
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
    if verdict.tier == ASK and verdict.rule not in {"bot-changed", "git-hooks", "bridge-send"} and exact in _allows(bot):
        verdict = _verdict(ALLOW, "always", "You always allow this exact command for this bot.", verdict.detail, verdict.fingerprint)
    if kind == "browser":
        needle = (getattr(request, "path", "") or "").strip() if getattr(request, "action", "") == "open" else (getattr(request, "body", "") or "").strip()
        if verdict.tier == ALLOW and needle and _injected(needle, needle) and not _only_browser_label(needle):
            verdict = _verdict(ASK, "injection", "That call matches text from a file, a page, or a tool. Those are data, so it waits for you.", verdict.detail, verdict.fingerprint)
        return verdict
    needle = (getattr(request, "command", "") or getattr(request, "path", "") or verdict.detail or "").strip()
    copied = bool(needle) and _injected(needle, needle)
    if copied and (verdict.tier == ALLOW or verdict.rule == "unlisted-command"):
        if not _folder_edit_not_tainted(store, request, bot_id):
            verdict = _verdict(ASK, "injection", "That call matches text from a file, a page, or a tool. Those are data, so it waits for you.", verdict.detail, verdict.fingerprint)
    return verdict


def _folder_edit_not_tainted(store: Store, request, bot_id: str | None) -> bool:
    """A file inside this bot's folder is not an injection just because a listing printed its path.

    A read or a list of that file does not wait. A write waits unless this chat
    already has a fresh read of it. A secret, or a path outside the folder,
    still waits. A path that only appeared on a web page, and was not read,
    still waits.
    """
    kind = getattr(request, "kind", "")
    action = getattr(request, "action", "") or ""
    if kind == "search_files":
        kind, action = "files", "read"
    if kind != "files" or action not in {"read", "list", "write", "patch"}:
        return False
    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        return False
    path = _file_request_path(store, getattr(request, "path", "") or ".", cwd, bot_id)
    if path.name.lower() in {"secrets.db", "secrets.passphrase"} or _is_passphrase_file(path):
        return False
    if secret_file(path):
        return False
    roots = _workspace_roots(store, bot_id, _user_text(store, bot_id))
    if not _inside(path, roots):
        return False
    if action in {"read", "list"}:
        return True
    return _chat_has_fresh_read(path)


def _chat_has_fresh_read(path: Path) -> bool:
    from easyagent import filestate
    from easyagent.tools import _edit_reads, _file_stamp, _path_key

    stamps = _edit_reads.get()
    key = _path_key(str(path))
    if isinstance(stamps, dict) and key in stamps and stamps.get(key) == _file_stamp(path):
        return True
    slot = turn_mod.current_slot()
    chat = slot.chat_id if slot is not None and slot.chat_id else "default"
    return filestate.has_full_read(chat, str(path))


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


_BUDGET_LOCK = threading.Lock()


def _now() -> float:
    """Clock for tool time. Tests move this without waiting."""
    return time.monotonic()


def _budget_state(slot) -> dict:
    """Call count and tool seconds for this run. A new run id starts over."""
    run_id = str(getattr(slot, "run_id", "") or "")
    state = getattr(slot, "safety_budget", None)
    if not isinstance(state, dict) or state.get("run_id") != run_id:
        state = {"run_id": run_id, "calls": 0, "seconds": 0.0}
        slot.safety_budget = state
    return state


def note_tool_seconds(seconds: float) -> None:
    """Add time a tool actually spent running. Model waits and idle time are not added."""
    slot = turn_mod.current_slot()
    if slot is None:
        return
    spent = max(0.0, float(seconds))
    if spent <= 0:
        return
    with _BUDGET_LOCK:
        state = _budget_state(slot)
        state["seconds"] = float(state["seconds"]) + spent


def _budget() -> Verdict | None:
    slot = turn_mod.current_slot()
    if slot is None:
        return None
    with _BUDGET_LOCK:
        state = _budget_state(slot)
        if float(state["seconds"]) > MAX_SECONDS:
            return _verdict(BLOCK, "time-budget", "This turn ran for twenty minutes of tool time. It stops.", "", _fp("budget", "time"))
        state["calls"] = int(state["calls"]) + 1
        calls = int(state["calls"])
    if calls > MAX_CALLS:
        return _verdict(BLOCK, "call-budget", f"This turn already ran {MAX_CALLS} tools. It stops so it cannot loop.", "", _fp("budget", "calls"))
    return None


def _opened_session(store: Store, bot_id: str | None, decision: str, command: str) -> tuple[str, bool]:
    """Session stays in memory. Always allow is saved for this bot, tool, and directory.

    Either one skips stamping the files. Neither one skips a block or an off-limits path,
    because those are decided before this grant is consulted.
    """
    if decision not in {"session", "exec-always"} or not bot_id:
        return decision, False
    try:
        cwd = Path(turn_mod.tool_cwd())
    except OSError:
        cwd = Path(".")
    if decision == "session":
        from easyagent.strictshell import allow_exec_session

        granted = allow_exec_session(bot_id, command, cwd)
        return "approve", bool(granted)
    from easyagent.strictshell import allow_exec_always

    granted = allow_exec_always(store, bot_id, command, cwd)
    return "approve", bool(granted)


def _stamp_script(store: Store, bot_id: str, command: str) -> None:
    """The approval covers the scripts and build files as they are now."""
    from easyagent.strictshell import stamp_scripts

    try:
        cwd = Path(turn_mod.tool_cwd()).resolve()
    except OSError:
        cwd = Path(turn_mod.tool_cwd())
    stamp_scripts(store, bot_id, command, cwd, _workspace_roots(store, bot_id, _user_text(store, bot_id)))


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
        if store is not None and bot_id:
            from easyagent.strictshell import note_bot_write

            note_bot_write(store, bot_id, path, getattr(request, "body", "") or "")


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


def clear_remembered(store: Store, bot_id: str) -> dict:
    """Drop saved approvals and old denials for this bot. The next command can ask again."""
    bot = store.get_bot(bot_id)
    for key in ("safety_denied", "safety_allows", "safety_type_allows"):
        bot.pop(key, None)
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)
    chats = 0
    for item in store.list_chats(bot_id):
        chat_id = str(item.get("id") or "")
        if not chat_id:
            continue
        chat = store.get_chat(bot_id, chat_id)
        changed = False
        for key in ("type_grants", "class_grants"):
            if key in chat:
                chat.pop(key, None)
                changed = True
        if changed:
            chat["updated_at"] = now_iso()
            store.save_chat(chat)
            chats += 1
    return {"cleared": True, "chats": chats}


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
        "action_class": card.action_class,
        "action_tool": card.action_tool,
        "class_label": card.class_label,
    }


# A card for one of these is never offered, and never takes, a lasting grant:
# blocked rules that were unlocked, browser actions, connectors, and bridge sends.
_NEVER_ALWAYS = frozenset({"root-delete", "disk-format", "fork-bomb", "remote-script", "firewall-off", "credential-dump", "browser-store", "guardrail-edit", "browser-pay", "browser-login", "browser-post", "browser-settings", "browser-submit", "browser-secret", "mcp-ask", "mcp-install", "bridge-send"}) | {"own-server", "bot-isolation", "data-dir"}


def resolve_card(card_id: str, decision: str, bot_id: str | None = None) -> Pending | None:
    card = _PENDING.get(card_id)
    if card is None or card.event.is_set():
        return card
    if bot_id is not None and card.bot_id != bot_id:
        # Another bot's card is not answered through this bot.
        return None
    if decision not in {"approve", "deny", "always", "done", "session", "exec-always", "class"}:
        decision = "deny"
    if decision == "done" and (card.proposal or {}).get("kind") != "takeover":
        decision = "deny"
    if decision == "exec-always":
        proposal = card.proposal or {}
        if proposal.get("kind") != "script-diff" or not proposal.get("exec_always"):
            decision = "deny"
    if decision == "session":
        # Allow for this session is offered only on a script-diff card that says so.
        proposal = card.proposal or {}
        if proposal.get("kind") != "script-diff" or not proposal.get("session"):
            decision = "deny"
    # Before "always" is folded into a one-time approval. A card that does not
    # offer a lasting grant must not accept one, including a blocked card and a
    # bridge send whose button was hidden.
    if decision in {"always", "exec-always", "session", "class"} and (card.rule in _NEVER_ALWAYS or card.tier == BLOCK):
        decision = "deny"
    elif decision == "class" and not card.action_class:
        decision = "deny"
    elif decision == "always" and not card.offer_always:
        decision = "approve"
    card.decision = decision
    card.event.set()
    if (card.proposal or {}).get("kind") == "mcp-install":
        from easyagent.connectors import finish_install
        from easyagent.store import StoreError

        try:
            result = finish_install(card.id, decision)
        except StoreError as exc:
            card.install_error = exc.message
            card.install_failed = True
        except Exception as exc:
            card.install_error = str(exc).strip() or "The connector was not installed."
            card.install_failed = True
        else:
            if isinstance(result, dict) and result.get("refresh_error"):
                card.install_error = str(result["refresh_error"])
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


def _notify_bridge(store: Store, bot_id: str | None, card: Pending) -> None:
    """Tell the person on their own bridge. A stranger is never the recipient."""
    try:
        from easyagent.bridges import schedule_card

        schedule_card(store, bot_id or "", card)
    except Exception:
        return


def _yolo_passes(store: Store, bot_id: str | None) -> bool:
    """The single YOLO approval bypass. False when this turn is not that chat."""
    if not bot_id or is_unattended():
        return False
    from easyagent.yolo import passes

    try:
        return bool(passes(store, bot_id))
    except Exception:
        return False


def _class_take(store: Store, request, bot_id: str | None, verdict: Verdict) -> bool:
    """Skip the card when this chat already allowed this action class and the paths still fit."""
    if verdict.tier != ASK or not bot_id or is_unattended():
        return False
    from easyagent.actionclass import covers

    try:
        return bool(covers(store, bot_id, request, verdict))
    except Exception:
        return False


def _decorate_class(store: Store, request, verdict: Verdict, card: Pending) -> None:
    """Name the class on the card when this command is one instance of it."""
    if verdict.tier != ASK or verdict.rule in _NEVER_ALWAYS or is_unattended():
        return
    from easyagent.actionclass import match
    from easyagent.helpers import in_helper

    if in_helper():
        return
    try:
        found = match(store, card.bot_id, request, verdict)
    except Exception:
        return
    if found is None:
        return
    card.action_class = found.name
    card.action_tool = found.tool
    card.class_label = found.label


def _release_ask(store: Store, request, bot_id: str | None, verdict: Verdict):
    """Trash and snapshots that an approval of this ask would have taken."""
    if getattr(request, "kind", "") == "shell" and verdict.rule == "delete":
        paths = _pure_delete_paths(getattr(request, "command", "") or "")
        if paths is not None:
            text = _trash_paths(store, paths)
            _remember_created(request)
            return request, text
    if verdict.rule in {"overwrite", "redirect-overwrite", "script-body", "write-outside"}:
        try:
            cwd = Path(turn_mod.tool_cwd())
        except OSError:
            cwd = Path(".")
        for path in _paths_in(verdict.detail, cwd):
            _snapshot(store, path)
        if getattr(request, "kind", "") == "files":
            _snapshot(store, _file_request_path(store, getattr(request, "path", "") or ".", cwd, bot_id))
    _remember_created(request)
    return request, None


async def guard(store: Store, request, bot_id: str | None):
    """Return (request, early_result). early_result is set when the tool must not run."""
    from easyagent.tools import ToolError

    slot = turn_mod.current_slot()
    if slot is not None:
        slot.script_human_approved = False
    if getattr(request, "kind", "") == "failed":
        raise ToolError(getattr(request, "body", "") or "That call could not be run.")

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
    if getattr(request, "kind", "") == "bridge" and verdict.rule == "bridge-send":
        from easyagent.bridges import card_proposal

        proposal = card_proposal(request)
    if proposal is None and verdict.rule in {"bot-changed", "git-hooks"}:
        slot = turn_mod.current_slot()
        proposal = getattr(slot, "safety_script_proposal", None) if slot is not None else None
    if _yolo_passes(store, bot_id):
        # One check. Hermes returns approved before any card, after the hardline
        # floor. That floor is the only block left. A routine or a helper never
        # reaches this, because passes() is false there.
        from easyagent.yolo import FLOOR_MESSAGE, catastrophic, floor_on

        kind = getattr(request, "kind", "")
        floor = catastrophic(getattr(request, "command", "") or "") if kind in {"shell", "ssh", "windows"} else None
        if floor and floor_on():
            _audit(
                store,
                bot_id,
                {
                    "at": now_iso(),
                    "decision": "block",
                    "tag": "yolo",
                    "rule": "catastrophic",
                    "why": FLOOR_MESSAGE,
                    "detail": floor,
                },
            )
            raise ToolError(FLOOR_MESSAGE + " It was not run.")
        if verdict.tier in {BLOCK, ASK}:
            _audit(
                store,
                bot_id,
                {
                    "at": now_iso(),
                    "decision": "approve",
                    "tag": "yolo",
                    "rule": verdict.rule,
                    "why": verdict.why,
                    "detail": verdict.detail,
                },
            )
            if verdict.tier == ASK:
                return _release_ask(store, request, bot_id, verdict)
            _remember_created(request)
            return request, None
    if verdict.rule == "browser-secret":
        return await _hand_browser(store, request, bot_id, verdict, proposal)
    if verdict.tier == BLOCK:
        _audit(store, bot_id, {"at": now_iso(), "decision": "block", "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
        raise ToolError(verdict.why + " It was not run.")
    if verdict.tier == ASK and is_unattended():
        exact = (getattr(request, "command", "") or getattr(request, "path", "") or "").strip()
        card = _make_card(bot_id, verdict, exact, proposal)
        _PENDING[card.id] = card
        _notify(verdict.why)
        _audit(store, bot_id, {"at": now_iso(), "decision": "ask", "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
        asyncio.create_task(_settle_unattended(store, request, bot_id, card, verdict))
        _notify_bridge(store, bot_id, card)
        raise ToolError("That needs you. A card is in the chat. This routine went on without it. If nobody answers, the command is not run, and it can be asked again.")
    if verdict.tier == ASK and _class_take(store, request, bot_id, verdict):
        from easyagent.actionclass import match as class_match

        found = None
        try:
            found = class_match(store, bot_id or "", request, verdict)
        except Exception:
            found = None
        _audit(
            store,
            bot_id,
            {
                "at": now_iso(),
                "decision": "approve",
                "tag": "class",
                "class": found.name if found is not None else "",
                "tool": found.tool if found is not None else "",
                "rule": verdict.rule,
                "why": verdict.why,
                "detail": verdict.detail,
            },
        )
        return _release_ask(store, request, bot_id, verdict)
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
        if verdict.rule in _NEVER_ALWAYS:
            card.offer_always = False
        _decorate_class(store, request, verdict, card)
        _PENDING[card.id] = card
        _notify(verdict.why)
        _notify_bridge(store, bot_id, card)
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
        class_grant = False
        if decision == "class" and bot_id and card.action_class:
            from easyagent.actionclass import remember

            try:
                class_grant = remember(store, bot_id, request, verdict) is not None
            except Exception:
                class_grant = False
            decision = "approve" if class_grant else "deny"
        decision, session_grant = _opened_session(store, bot_id, decision, card.exact or getattr(request, "command", "") or "")
        audited = {"at": now_iso(), "decision": decision, "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail}
        if class_grant:
            audited["tag"] = "class"
            audited["class"] = card.action_class
            audited["tool"] = card.action_tool
        _audit(store, bot_id, audited)
        if decision == "expired":
            raise ToolError("That card expired. It was not run.")
        if decision != "approve":
            raise ToolError("You denied that. It was not run.")
        approved_slot = turn_mod.current_slot()
        if approved_slot is not None:
            approved_slot.script_human_approved = True
        if verdict.rule in {"bot-changed", "git-hooks"} and bot_id and not session_grant and not class_grant:
            _stamp_script(store, bot_id, card.exact or getattr(request, "command", "") or "")
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
    if verdict.rule in _NEVER_ALWAYS:
        card.offer_always = False
    return card


async def _settle_unattended(store: Store, request, bot_id: str | None, card: Pending, verdict: Verdict) -> None:
    """The card stays until someone answers. Expiry does not remember a denial."""
    decision = await _wait(card)
    if decision == "always" and bot_id and card.exact:
        _remember_allow(store, bot_id, card.exact)
        decision = "approve"
    decision, session_grant = _opened_session(store, bot_id, decision, card.exact or getattr(request, "command", "") or "")
    _audit(store, bot_id, {"at": now_iso(), "decision": decision, "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
    _PENDING.pop(card.id, None)
    if decision != "approve":
        return
    if verdict.rule in {"bot-changed", "git-hooks"} and bot_id and not session_grant:
        _stamp_script(store, bot_id, card.exact or getattr(request, "command", "") or "")
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
        if getattr(request, "kind", "") == "bridge":
            from easyagent.bridges import deliver

            await deliver(store, request, bot_id)
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


def note_takeover(card_id: str, bot_id: str | None = None) -> Pending | None:
    """The person is using the browser window. The card stays up until Done."""
    card = _PENDING.get(card_id)
    if card is None or card.event.is_set():
        return None
    if bot_id is not None and card.bot_id != bot_id:
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
        if decision == "expired":
            raise ToolError("That card expired. EasyAgent did not type a password, a card number, or a 2FA code.")
        raise ToolError("You denied that. EasyAgent did not type a password, a card number, or a 2FA code.")
    return request, "EasyAgent did not type a password, a card number, or a 2FA code. The browser window was yours."


async def _settle_secret(store: Store, bot_id: str | None, card: Pending, verdict: Verdict) -> None:
    decision = await _wait(card)
    _audit(store, bot_id, {"at": now_iso(), "decision": decision or "deny", "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
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
    if "safety_mode" in blob or "safety_unlocks" in blob or "safety_allows" in blob or "safety_exec_always" in blob:
        return True
    return False


def read_audit(store: Store, bot_id: str) -> list[dict]:
    path = store.root / "bots" / bot_id / "safety-audit.json"
    if not path.is_file():
        return []
    rows = read_json(path)
    return rows if isinstance(rows, list) else []


def set_mode(store: Store, bot_id: str, mode: str, confirm_name: str = "", unlocks: list[str] | None = None) -> dict:
    from easyagent.store import StoreError, names_match, now_iso

    if mode not in {"strict", "balanced", "careful", "normal", "advanced"}:
        raise StoreError("Safety is Strict, Balanced, Normal, or Advanced.", 400)
    bot = store.get_bot(bot_id)
    if mode == "advanced" and not names_match(confirm_name, bot.get("name") or ""):
        raise StoreError("Type the bot's name to turn on Advanced.", 400)
    bot["safety_mode"] = mode
    bot["safety_mode_origin"] = "user"
    bot["safety_mode_changed_at"] = now_iso()
    if unlocks is not None:
        if mode != "advanced":
            bot["safety_unlocks"] = []
        else:
            bot["safety_unlocks"] = [str(item) for item in unlocks if str(item).strip()][:40]
    atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)
    return bot
