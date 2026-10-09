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
import time
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
    r"(?i)\b(mkfs|diskpart)\b|\bdd\b[^\n]*\bof=/dev/|\bformat\s+[a-z]:\s"
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
    r"(?i)^(?:rm|unlink|del|erase|rmdir|remove-item|clear-content|shred|trash)\b"
)
_MOVE = re.compile(r"(?i)^(?:mv|move|ren|rename-item|move-item)\b")
_ENC = re.compile(r"(?i)(?:-EncodedCommand|-enc)\s+([A-Za-z0-9+/=]{8,})")
_WRAP = re.compile(
    r"(?is)^(?:cmd(?:\.exe)?\s+/c\s+|powershell(?:\.exe)?\s+(?:-noprofile\s+)?(?:-command|-c)\s+|"
    r"(?:ba)?sh\s+-c\s+)(.+)$"
)
_WEAKEN = re.compile(
    r"(?i)(disable|skip|turn off|bypass|ignore|weaken|remove).{0,48}"
    r"(safety|guardrail|approval|ask before)|always allow\s+rm|auto-approve|no approval"
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


_PENDING: dict[str, Pending] = {}
_UNTRUSTED: list[str] = []
_UNTRUSTED_GUARD = asyncio.Lock()


def reset_for_tests() -> None:
    _PENDING.clear()
    _UNTRUSTED.clear()


def lesson_weakens(text: str) -> bool:
    """A lesson, note, or playbook must not turn the guardrails down."""
    return bool(_WEAKEN.search(text or ""))


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
    if kind in {"files", "shell", "ssh", "windows", "search"} or action == "read":
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
    out = os.path.expandvars(out)
    return out


def _unwrap(command: str) -> str:
    text = _expand((command or "").strip())
    match = _ENC.search(text)
    if match:
        try:
            raw = base64.b64decode(match.group(1))
            decoded = raw.decode("utf-16-le", errors="ignore")
            if not decoded.strip():
                decoded = raw.decode("utf-8", errors="ignore")
            if decoded.strip():
                text = decoded.strip()
        except Exception:
            pass
    wrapped = _WRAP.match(text.strip())
    if wrapped:
        inner = wrapped.group(1).strip()
        if (inner.startswith('"') and inner.endswith('"')) or (inner.startswith("'") and inner.endswith("'")):
            inner = inner[1:-1]
        text = inner.strip()
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
        roots.append(store.root / "bots" / bot_id / "workspace")
    roots.append(Path(tempfile.gettempdir()))
    for match in re.finditer(r"(?:[A-Za-z]:[\\/]|/)[^\s\"']+", user_text or ""):
        candidate = Path(match.group(0).rstrip(".,);:"))
        if candidate.suffix:
            candidate = candidate.parent
        if str(candidate) not in {".", ""}:
            roots.append(candidate)
    return roots


def _inside(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in roots:
        try:
            root_resolved = root.resolve()
        except OSError:
            root_resolved = root
        if resolved == root_resolved or root_resolved in resolved.parents:
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


def _is_guardrail(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if _PACKAGE == resolved or _PACKAGE in resolved.parents:
        if resolved.name in {"safety.py", "approve.py"} or "guardrail" in resolved.name:
            return True
    name = resolved.name.lower()
    if name in {"safety.json", "guardrails.json"} and "easyagent" in str(resolved).lower():
        return True
    return False


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
    body = re.sub(r"(?i)\s-[A-Za-z]+\b", " ", command)
    body = re.sub(r"(?i)^(rm|unlink|del|erase|rmdir|remove-item|mv|move|ren|rename-item|move-item)\b", "", body).strip()
    paths: list[Path] = []
    for token in re.findall(r"[^\s]+", body):
        if token in {"|", ">", ">>", "<"}:
            break
        paths.append(_resolve(token, cwd))
    return paths


def _blocked_command(line: str) -> Verdict | None:
    detail = line.strip()
    fingerprint = _fp("cmd", detail)
    if _FORK.search(detail):
        return _verdict(BLOCK, "fork-bomb", "That command would spawn copies of itself until the computer stalls.", detail, fingerprint)
    if _PIPE_SH.search(detail):
        return _verdict(BLOCK, "remote-script", "That downloads a script and runs it. It is never run.", detail, fingerprint)
    if _DISK.search(detail):
        return _verdict(BLOCK, "disk-format", "That would erase a disk. It is never run.", detail, fingerprint)
    if _FIREWALL.search(detail):
        return _verdict(BLOCK, "firewall-off", "That turns off the firewall or Defender. It is never run.", detail, fingerprint)
    if _CREDS.search(detail):
        return _verdict(BLOCK, "credential-dump", "That reads or dumps credentials. It is never run.", detail, fingerprint)
    if re.search(r"(?i)\brm\b[^\n]*(\s/|\s/\*|\s~|\s\$HOME|\s%USERPROFILE%|\s[A-Za-z]:\\?)\s*$", detail):
        return _verdict(BLOCK, "root-delete", "That would delete the disk root or the whole user profile. It is never run.", detail, fingerprint)
    return None


def judge_command(
    command: str,
    *,
    remote: bool,
    cwd: Path,
    roots: list[Path],
    created: set[str],
    mode: str,
) -> Verdict:
    text = _unwrap(command)
    blocked = _blocked_command(text)
    if blocked is not None:
        return blocked
    if re.search(r"(?i)(cookies|login data|logins\.json|key4\.db|web data)", text):
        return _verdict(BLOCK, "browser-store", "Browser cookies and saved passwords are never read.", text.strip(), _fp("cmd", text.strip()))
    pieces = _split_chain(text)
    if len(pieces) > 1:
        verdicts = [
            judge_command(piece, remote=remote, cwd=cwd, roots=roots, created=created, mode=mode)
            for piece in pieces
        ]
        winner = verdicts[0]
        for item in verdicts[1:]:
            winner = _worst(winner, item)
        detail = text.strip()
        return _verdict(winner.tier, winner.rule, winner.why, detail, _fp("cmd", text.strip()))
    line = text.strip()
    detail = line
    fingerprint = _fp("cmd", line)
    if not line:
        return _verdict(ALLOW, "empty", "There was no command.", detail, fingerprint)
    for path in _paths_in(line, cwd):
        if _is_guardrail(path) and re.search(r"(?i)\b(rm|del|mv|move|>|set-content|out-file)\b", line):
            return _verdict(BLOCK, "guardrail-edit", "EasyAgent's own guardrails cannot be edited from a tool.", detail, fingerprint)
        if _secret_kind(path) == "browser" and re.search(r"(?i)\b(cat|type|get-content|copy|cp|curl|scp)\b", line):
            return _verdict(BLOCK, "browser-store", "Browser cookies and saved passwords are never read.", str(path), fingerprint)
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
    if re.search(r"(?i)(^|\s)>{1,2}\s*\S+", line) or re.search(r"(?i)\b(set-content|out-file|clear-content)\b", line):
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
    if re.search(r"(?i)\b(curl|wget|iwr|invoke-webrequest)\b.*\.(exe|msi|dmg|sh|ps1)\b", line):
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
    if kind in {"search", "question", "finish", "plan", "react", "memory", "history", "project"}:
        verdict = _verdict(ALLOW, kind or "meta", "That does not change the computer.", kind, _fp(kind, getattr(request, "body", "")[:80]))
    elif kind == "files":
        path = _resolve(getattr(request, "path", "") or ".", cwd)
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
        verdict = judge_command(
            getattr(request, "command", "") or "",
            remote=remote,
            cwd=cwd,
            roots=roots,
            created=created,
            mode=mode,
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
    needle = (getattr(request, "command", "") or getattr(request, "path", "") or verdict.detail or "").strip()
    if verdict.tier == ALLOW and _injected(needle, needle):
        verdict = _verdict(ASK, "injection", "That call matches text from a file, a page, or a tool. Those are data, so it waits for you.", verdict.detail, verdict.fingerprint)
    return verdict


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
        try:
            cwd = Path(turn_mod.tool_cwd())
        except OSError:
            return
        path = _resolve(request.path, cwd)
        _created().add(str(path))


def _audit(store: Store, bot_id: str | None, row: dict) -> None:
    if not bot_id:
        return
    try:
        directory = store.root / "bots" / bot_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "safety-audit.json"
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
    }


def resolve_card(card_id: str, decision: str) -> Pending | None:
    card = _PENDING.get(card_id)
    if card is None or card.event.is_set():
        return card
    if decision not in {"approve", "deny", "always"}:
        decision = "deny"
    if decision == "always" and not card.offer_always:
        decision = "approve"
    card.decision = decision
    card.event.set()
    return card


async def _wait(card: Pending) -> str:
    try:
        await asyncio.wait_for(card.event.wait(), approval_timeout())
    except asyncio.TimeoutError:
        card.decision = "deny"
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
    if verdict.tier == BLOCK:
        _audit(store, bot_id, {"at": now_iso(), "decision": "block", "rule": verdict.rule, "why": verdict.why, "detail": verdict.detail})
        if verdict.rule == "already-denied":
            _remember_denial(store, bot_id, verdict.fingerprint)
        raise ToolError(verdict.why + " It was not run.")
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
            offer_always=verdict.tier == ASK and verdict.rule != "already-denied",
            created=time.time(),
        )
        # A blocked rule that was unlocked is still not offered "always".
        if verdict.rule in {"root-delete", "disk-format", "fork-bomb", "remote-script", "firewall-off", "credential-dump", "browser-store", "guardrail-edit"}:
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
