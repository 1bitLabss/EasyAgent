"""Email over IMAP and SMTP, standard library only.

The network calls stay thin so the bridge loop can fake them in tests.
Parsing is pure functions over already-fetched bytes.
"""

from __future__ import annotations

import email
import imaplib
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import parseaddr

DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 587


def _decode_header(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


def sender_of(message: Message) -> str:
    _name, addr = parseaddr(str(message.get("From") or ""))
    return (addr or "").strip().lower()


def subject_of(message: Message) -> str:
    return _decode_header(str(message.get("Subject") or ""))


def _strip_quoted(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            continue
        if line.strip() == "--":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def body_of(message: Message) -> str:
    part = message
    if message.is_multipart():
        part = None
        for candidate in message.walk():
            if str(candidate.get_content_type() or "") != "text/plain":
                continue
            if "attachment" in str(candidate.get("Content-Disposition") or ""):
                continue
            part = candidate
            break
        if part is None:
            return ""
    try:
        get_content = getattr(part, "get_content", None)
        payload = get_content() if callable(get_content) else (part.get_payload(decode=True) or b"")
    except Exception:
        payload = part.get_payload(decode=True) or b""
    if isinstance(payload, bytes):
        payload = payload.decode(str(part.get_content_charset() or "utf-8"), "replace")
    return _strip_quoted(str(payload))


def parse_raw(raw: bytes) -> dict:
    message = email.message_from_bytes(raw)
    return {
        "from_addr": sender_of(message),
        "subject": subject_of(message),
        "body_text": body_of(message),
    }


def _imap_connect(row: dict, token: str) -> imaplib.IMAP4_SSL:
    host = str(row.get("mail_imap_host") or "")
    port = int(row.get("mail_imap_port") or DEFAULT_IMAP_PORT)
    imap = imaplib.IMAP4_SSL(host, port)
    imap.login(str(row.get("mail_user") or ""), token)
    imap.select("INBOX")
    return imap


def fetch_unseen(row: dict, token: str) -> list[dict]:
    """Every unread message in the inbox, oldest first, with its uid."""
    found: list[dict] = []
    imap = _imap_connect(row, token)
    try:
        status, data = imap.uid("search", "1:*", "UNSEEN")
        uids = data[0].split() if status == "OK" and data and data[0] else []
        for uid in uids:
            status, fetched = imap.uid("fetch", uid, "(RFC822)")
            raw = b""
            if status == "OK":
                for item in fetched or []:
                    if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                        raw = item[1]
                        break
            if not raw:
                continue
            parsed = parse_raw(raw)
            parsed["uid"] = uid.decode() if isinstance(uid, bytes) else str(uid)
            found.append(parsed)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return found


def mark_seen(row: dict, token: str, uid) -> None:
    imap = _imap_connect(row, token)
    try:
        imap.uid("store", str(uid), "+FLAGS", "\\Seen")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def smtp_send(row: dict, token: str, to_addr: str, subject: str, body: str) -> None:
    host = str(row.get("mail_smtp_host") or "")
    port = int(row.get("mail_smtp_port") or DEFAULT_SMTP_PORT)
    user = str(row.get("mail_user") or "")
    message = EmailMessage()
    message["From"] = user
    message["To"] = to_addr
    message["Subject"] = subject
    message.set_content(body or "")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(user, token)
        smtp.send_message(message)
