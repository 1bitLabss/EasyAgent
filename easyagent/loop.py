"""One send's contract, plan, and evidence.

These stay beside the short message tail and are put back in front of the
model on every step. Compaction of the tail cannot drop them. A quick fact
does not open a contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from easyagent.paths import deliverable_file

# A turn is not cut off because time passed or because many steps ran.
# It stops when the same tool repeats with nearly the same arguments and
# nothing new happened, or the model calls finish, or it asks a question
# only the person can answer, or it names a real blocker.

_MARKER = "[[easyagent-working]]"
_VERIFIABLE = re.compile(
    r"(?i)\b(write|create|save|run|ssh|command|landing|file|folder|"
    r"read|list|fix|code|page|html|script|bug|broken)\b"
)
_PATH = re.compile(r"[A-Za-z]:\\|/(?:[\w.-]+/)+")
_BARE_NOT_WRITTEN = re.compile(
    r"(?i)^the file was not written\.?(?:\s+that is not a finish\.?)?$"
)
_SHRUG = re.compile(
    r"(?i)^(none|no|n/?a|nothing|no problem|looks good|looks fine|cannot|no counterexample|"
    r"fine|ok|okay|no issue|nothing wrong|consider adding more detail|consider adding detail)\b"
)
_VAGUE = re.compile(r"(?i)^(?:consider adding(?: more)? detail|add more detail|looks fine|looks good)$")
_DEFECT = re.compile(r"(?i)\b(C\d+)\b\s*[:\-]\s*(.+)")

PLAYBOOK_DIR = Path(__file__).resolve().parent / "playbooks"


_JUDGMENT = re.compile(r"(?i)\b(research|compare|sources|look up|decide|tradeoff|versus|\bvs\b)\b")
PLAYBOOKS = ("build", "research", "fix", "decide", "quick")


def is_quick(ask: str) -> bool:
    """A fact with no file, no command, and no judgment to check."""
    text = ask or ""
    if _VERIFIABLE.search(text) or _PATH.search(text) or _JUDGMENT.search(text):
        return False
    return True


def is_bare_not_written(text: str) -> bool:
    folded = " ".join((text or "").split())
    return bool(_BARE_NOT_WRITTEN.match(folded))


def pick_playbook(ask: str) -> str:
    if is_quick(ask):
        return "quick"
    if re.search(r"(?i)\b(fix|bug|broken|missing|does not work|doesn't work)\b", ask):
        return "fix"
    if re.search(r"(?i)\b(decide|which should|tradeoff|versus|\bvs\b)\b", ask):
        return "decide"
    if re.search(r"(?i)\b(research|compare|sources|look up)\b", ask):
        return "research"
    return "build"


def playbook_text(name: str) -> str:
    path = PLAYBOOK_DIR / f"{name}.md"
    if not path.is_file():
        return name
    return path.read_text(encoding="utf-8").strip()


def norm_arg(text: str) -> str:
    return " ".join((text or "").replace("\\", "/").casefold().split()).strip("/")


def near_arg(left: str, right: str) -> bool:
    """Same argument with a small edit: case, slash, or repeated space."""
    if left == right:
        return True
    if not left or not right:
        return False
    if left.rstrip("/") == right.rstrip("/") or left.replace(" ", "") == right.replace(" ", ""):
        return True
    return False


_FILEISH = re.compile(r"(?i)\.[A-Za-z][A-Za-z0-9]{1,7}$")
_WIN_FILE = re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]+")
_POSIX_FILE = re.compile(r"(?<![\w])(/(?:[\w.-]+/)*[\w.-]+)")


def _norm_path(path: str) -> str:
    return (path or "").replace("\\", "/").casefold().rstrip("/")


def paths_match(left: str, right: str) -> bool:
    return bool(left) and bool(right) and _norm_path(left) == _norm_path(right)


def _paths_in(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _WIN_FILE.finditer(text or ""):
        path = match.group(0).rstrip("\\").rstrip(".,);]")
        key = _norm_path(path)
        if path and key not in seen:
            seen.add(key)
            found.append(path)
    for match in _POSIX_FILE.finditer(text or ""):
        path = match.group(1).rstrip(".,);]")
        key = _norm_path(path)
        if path and key not in seen:
            seen.add(key)
            found.append(path)
    return found


def _is_file_path(path: str) -> bool:
    name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return bool(_FILEISH.search(name))


def summary_job(ask: str) -> tuple[str, list[str]] | None:
    """Several named files to read, and one summary file to write.

    None for every other request. A single write, a page, or "read each part"
    does not match.
    """
    text = ask or ""
    if not re.search(r"(?i)\bread\b", text) or not re.search(r"(?i)\bsummary\b", text):
        return None
    files = [path for path in _paths_in(text) if _is_file_path(path)]
    if len(files) < 3:
        return None
    summary = ""
    for path in files:
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        if "summary" in name.casefold():
            summary = path
            break
    if not summary:
        return None
    notes = [path for path in files if not paths_match(path, summary)]
    if len(notes) < 2:
        return None
    return summary, notes


def page_subject(ask: str) -> str:
    """The thing a landing page is for. A sample page has no subject."""
    text = ask or ""
    match = re.search(r"(?i)\b(?:for|about)\s+(?:a|an|the)\s+([A-Za-z][A-Za-z-]{1,40})", text)
    if not match:
        return ""
    word = match.group(1)
    if word.lower() in {"page", "website", "site", "landing", "sample"}:
        return ""
    return word


def needs_stages(ask: str) -> bool:
    """A task that builds or changes something. A quick chat does not."""
    if is_quick(ask):
        return False
    text = ask or ""
    if re.search(r"(?i)\b(landing page|web page|html page|homepage)\b", text):
        return True
    if re.search(r"(?i)\b(fix|bug|broken|missing|does not work|doesn't work)\b", text):
        return True
    if re.search(r"(?i)\b(write|create|save)\b", text):
        return True
    return False


def task_goal(ask: str, memory: str = "") -> str:
    """The request, restated. A memory line is included only when it overlaps."""
    text = " ".join((ask or "").split()).strip()
    if not text:
        return ""
    if text[-1] not in ".?!":
        text += "."
    text = text[:1].upper() + text[1:]
    remembered = " ".join((memory or "").split()).strip()
    if remembered:
        if remembered[-1] not in ".?!":
            remembered += "."
        text += " Using memory: " + remembered
    return text


def plan_steps(ask: str) -> list[str]:
    """A short ordered plan. A hidden checklist is not this."""
    text = ask or ""
    if re.search(r"(?i)\b(landing page|web page|html page|homepage)\b", text):
        return [
            "Pick a concrete name if the request did not give one. That choice is made here and not asked.",
            f"Write the page to {deliverable_file('landing.html')} unless a folder was named. "
            "Static HTML and CSS. Modern styling, and a real picture in the file.",
            "Look at the file again and compare it to the goal. "
            "A header, a paragraph, and a stack of cards is not done.",
        ]
    if re.search(r"(?i)\b(fix|bug|broken|missing|does not work|doesn't work)\b", text):
        return ["Look at what is broken.", "Change it.", "Check the result."]
    if re.search(r"(?i)\b(write|create|save)\b", text):
        return ["Write the file that was asked for.", "Look at the file again before calling it ready."]
    return ["Do the work that was asked for.", "Check the result before calling it done."]


def question_line(ask: str) -> str:
    """No question is still said out loud. A name for a new page is not a question."""
    if re.search(r"(?i)\b(landing page|web page|html page|homepage)\b", ask or ""):
        return "No question. A name is picked in the plan."
    return "No question. Continuing."


def research_query(ask: str) -> str:
    """What to look up before a page is written. Empty when a lookup would not change the work."""
    if not re.search(r"(?i)\b(landing page|web page|html page|homepage)\b", ask or ""):
        return ""
    subject = page_subject(ask)
    if subject:
        return f"what a {subject} landing page contains"
    return "what a landing page contains"


def research_blurb(findings: str) -> str:
    """A title or two from a lookup. A URL dump is not the chat."""
    titles: list[str] = []
    for line in (findings or "").splitlines():
        match = re.match(r"^\d+\.\s+(\S.*)$", " ".join(line.split()))
        if not match:
            continue
        title = match.group(1).strip()
        if title.lower().startswith("http"):
            continue
        titles.append(title[:160])
        if len(titles) == 2:
            break
    return " ".join(titles)


def _first_person(ask: str) -> str:
    """The request as a sentence the bot would say. A label is not this."""
    text = " ".join((ask or "").split()).strip()
    text = re.sub(
        r"(?i)^(please\s+)?(can you|could you|would you|will you)\s+",
        "",
        text,
    )
    text = re.sub(r"(?i)^please\s+", "", text)
    text = text.rstrip(".?!")
    text = re.sub(r"(?i)^(build|write|make|create)\s+me\b", r"\1", text)
    if not text:
        return "I'll do that."
    return "I'll " + text[:1].lower() + text[1:] + "."


def thinking_sentence(ask: str, findings: str = "") -> str:
    """What the work looks like, before a write. A page says what was looked up."""
    if not research_query(ask):
        return "The request already says what to change. Nothing public would change the result."
    blurb = research_blurb(findings)
    heard = f" {blurb}" if blurb else ""
    return (
        f"I looked up {research_query(ask)}.{heard} "
        "A header, a paragraph, and a stack of cards is not that page. "
        "It needs modern styling and a real picture in the file, an image address or a drawn picture. "
        "A CSS gradient is not a picture."
    )


def turn_opening(ask: str, memory: str = "", findings: str = "") -> str:
    """What was understood, what was found, and the next step. Empty for a quick chat."""
    if not needs_stages(ask):
        return ""
    understood = _first_person(ask)
    remembered = " ".join((memory or "").split()).strip()
    if remembered:
        if remembered[-1] not in ".?!":
            remembered += "."
        understood += " I'm using this note: " + remembered
    if research_query(ask):
        thought = thinking_sentence(ask, findings)
        nxt = (
            "I'll pick a name if one was not given. I'm not asking which one. "
            f"I'll write the page to {deliverable_file('landing.html')} unless a folder was named, "
            "with modern styling and a real picture in the file, then look at the file."
        )
    elif re.search(r"(?i)\b(fix|bug|broken|missing|does not work|doesn't work)\b", ask or ""):
        thought = "The request already says what to change. I'll look at what is broken, change it, and check the result."
        nxt = "I don't have a question only you would know, so I'm continuing."
    else:
        thought = "I'll write the file that was asked for, then look at it again before calling it ready."
        nxt = "I don't have a question only you would know, so I'm continuing."
    return f"{understood}\n\n{thought}\n\n{nxt}"


_MEMORY_SKIP = frozenset({
    "this", "that", "with", "from", "your", "have", "page", "file", "landing",
    "simple", "build", "write", "please", "could", "would", "about", "there",
    "make", "want", "them", "they", "into", "then", "than", "does", "what",
})


def memory_overlap(ask: str, lines: list[str]) -> str:
    """One standing line that shares a word with the request. Unrelated lines stay out."""
    words = {word.casefold() for word in re.findall(r"[A-Za-z]{4,}", ask or "")} - _MEMORY_SKIP
    if not words:
        return ""
    for line in lines:
        bits = {word.casefold() for word in re.findall(r"[A-Za-z]{4,}", line or "")}
        if words & bits:
            return " ".join(line.split())
    return ""


def only_the_user_knows(body: str, choices: tuple[str, ...] = ()) -> bool:
    """True when the answer would change the result and the model cannot pick it.

    A name for something being created is the model's choice. A folder, a file,
    a password, or which computer is not.
    """
    blob = " ".join([body or "", *choices])
    if re.search(
        r"(?i)\b(made-?up|i'?ll pick|i will pick|pick a name|you decide|invent|"
        r"i'?ll choose|i'?ll decide|a sample)\b",
        blob,
    ):
        return False
    text = body or ""
    if re.search(
        r"(?i)\b(password|secret|account|which folder|which file|which path|"
        r"which computer|which repo|which project)\b",
        text,
    ):
        return True
    if re.search(r"(?i)\bwhich\b", text):
        return False
    return True


def _visible_page(text: str) -> str:
    visible = re.sub(r"(?is)<script\b.*?</script>", " ", text)
    visible = re.sub(r"(?is)<style\b.*?</style>", " ", visible)
    visible = re.sub(r"(?s)<[^>]+>", " ", visible)
    return " ".join(visible.split())


def page_matches(data: bytes, subject: str) -> bool:
    """A sample title is not the page that was asked for. The layout is not graded here."""
    if not subject:
        return bool(data)
    text = data.decode("utf-8", errors="replace")
    if re.search(r"(?i)<title>\s*sample landing page\s*</title>", text):
        return False
    if re.search(r"(?i)<h1>\s*sample landing page\s*</h1>", text):
        return False
    if subject.lower() not in text.lower():
        return False
    if re.search(rf"(?i)<h1>\s*{re.escape(subject)}\s+landing page\s*</h1>", text):
        return False
    if subject.lower() != "hotel":
        return True
    visible = _visible_page(text)
    if not re.search(r"(?i)\brooms?\b|\bstay\b", visible):
        return False
    if not re.search(r"(?i)\breserve\b|\bbook(?:ing)?\b", visible):
        return False
    return True


def _note_bit(result: str) -> str:
    """One line of a note. A path by itself is not the note."""
    for line in (result or "").splitlines():
        bit = " ".join(line.split())
        if len(bit) < 4:
            continue
        if re.match(r"(?i)^[A-Za-z]:\\", bit) or bit.startswith("/"):
            continue
        return bit[:120]
    return ""


def arg_key(kind: str, action: str, path: str, command: str, computer: str, body: str) -> tuple[str, ...]:
    return (
        kind,
        action,
        norm_arg(computer),
        norm_arg(path),
        norm_arg(command),
        norm_arg(body)[:180],
    )


def near_keys(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if left[:3] != right[:3]:
        return False
    return all(near_arg(a, b) for a, b in zip(left[3:], right[3:]))


@dataclass
class Defect:
    line: str
    detail: str
    evidence: str


def _only_vague(text: str) -> bool:
    folded = " ".join((text or "").split()).strip(" .")
    return bool(_SHRUG.match(folded) or _VAGUE.match(folded))


def judge_breaker(text: str) -> Defect | None:
    """A specific defect tied to a contract line. A shrug is not approval."""
    raw = " ".join((text or "").split())
    if not raw or _only_vague(raw):
        return None
    match = _DEFECT.search(text or "")
    if not match:
        return None
    detail = match.group(2).strip()
    if not detail or _only_vague(detail):
        return None
    evidence = ""
    evidence_match = re.search(r"(?i)\bevidence:\s*(.+)$", detail)
    if evidence_match:
        evidence = evidence_match.group(1).strip()
        detail = detail[: evidence_match.start()].strip(" .:-")
    if not detail or _only_vague(detail) or not evidence or _only_vague(evidence):
        return None
    return Defect(match.group(1).upper(), detail, evidence)


class Ledger:
    """The contract, the plan, and the evidence for one send."""

    def __init__(self, playbook: str, lines: list[tuple[str, str]], assumptions: str, scope: str):
        self.playbook = playbook
        self.lines = lines
        self.assumptions = assumptions
        self.scope = scope
        self.evidence: list[tuple[str, str]] = []
        self.proven: set[str] = set()
        self.plan = [f"P1 -> {lines[0][0]}: {playbook_text(playbook)}" if lines else playbook_text(playbook)]
        self.blocker = ""
        self.defects: list[Defect] = []
        self.defect_rounds = 0
        self.used_breaker = False
        self.summary_path = ""
        self.note_paths: list[str] = []
        self.note_bits: dict[str, str] = {}
        self.notes_read: set[str] = set()
        self.page_subject = ""

    @classmethod
    def from_ask(cls, ask: str) -> "Ledger":
        playbook = pick_playbook(ask)
        job = summary_job(ask)
        if job is not None:
            summary, notes = job
            ledger = cls(
                playbook,
                [
                    ("C1", "Each named note was read."),
                    ("C2", "The summary file is at the path that was asked for and mentions more than one note."),
                ],
                "Use only the paths in the request.",
                "Chats, secrets, and unrelated files are out of scope.",
            )
            ledger.summary_path = summary
            ledger.note_paths = list(notes)
            return ledger
        subject = ""
        if re.search(r"(?i)\b(landing|web page|html page|sample page|homepage)\b", ask):
            subject = page_subject(ask)
            if subject:
                lines = [
                    ("C1", f"The page file is on disk and is a {subject} landing page."),
                    ("C2", "The page file is HTML."),
                ]
            else:
                lines = [
                    ("C1", "The page file is on disk and is not empty."),
                    ("C2", "The page file is HTML."),
                ]
        elif re.search(r"(?i)\b(picture|image|png|jpe?g|gif|webp)\b", ask):
            lines = [
                ("C1", "The picture file is on disk and is not empty."),
                ("C2", "The picture file is a PNG."),
            ]
        elif re.search(r"(?i)\b(write|create|save)\b", ask):
            lines = [("C1", "The file is on disk and is not empty.")]
        elif re.search(r"(?i)\bread\b", ask):
            lines = [("C1", "The file was read.")]
        elif re.search(r"(?i)\b(run|ssh|command)\b", ask):
            lines = [("C1", "The command ran.")]
        else:
            lines = [("C1", "The tool output was checked.")]
        ledger = cls(
            playbook,
            lines,
            "Use only the path and the command in the request.",
            "Chats, secrets, and unrelated files are out of scope.",
        )
        ledger.page_subject = subject
        return ledger

    def all_proven(self) -> bool:
        return bool(self.lines) and all(line_id in self.proven for line_id, _text in self.lines)

    def _mark(self, line_id: str, note: str) -> None:
        if any(line_id == item for item, _text in self.lines):
            self.proven.add(line_id)
            self.evidence.append((line_id, note))

    def _file_job(self) -> bool:
        return any("on disk" in text for _line_id, text in self.lines)

    def prove_summary(self, path: str) -> None:
        """The summary file is the path he named. A different path is not evidence."""
        if not self.summary_path or not paths_match(path, self.summary_path):
            return
        file = Path(path)
        try:
            if not file.is_file():
                return
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        if not text.strip():
            return
        hits = [bit for bit in self.note_bits.values() if bit and bit in text]
        if len(hits) < 2:
            return
        self._mark("C2", f"Bytes came back from {file}.")

    def prove_disk(self, path: str) -> None:
        """Evidence is the file that was read back, not the model's sentence."""
        if self.summary_path:
            self.prove_summary(path)
            return
        from easyagent.tools import _placed_file

        file = _placed_file(path)
        try:
            data = file.read_bytes() if file.is_file() else b""
        except OSError:
            return
        if not data:
            return
        if self.page_subject and not page_matches(data, self.page_subject):
            return
        if any("on disk" in text for _line_id, text in self.lines):
            self._mark("C1", f"Read back {len(data)} bytes from {file}.")
        lower = data[:800].lower()
        if lower.startswith(b"\x89png") and any("PNG" in text for _line_id, text in self.lines):
            self._mark("C2", f"Read back a PNG from {file}.")
        elif any("HTML" in text for _line_id, text in self.lines) and (
            b"<html" in lower or b"<h1" in lower or b"<!doctype html" in lower
        ):
            self._mark("C2", f"Read back HTML from {file}.")

    def observe(self, kind: str, action: str, path: str, result: str) -> None:
        from easyagent.safety import strip_untrusted

        result = strip_untrusted(result)
        if self.summary_path:
            if kind == "files" and action == "read" and (result or "").strip():
                for note in self.note_paths:
                    if not paths_match(path, note):
                        continue
                    key = _norm_path(note)
                    self.notes_read.add(key)
                    bit = _note_bit(result)
                    if bit:
                        self.note_bits[key] = bit
                if self.note_paths and len(self.notes_read) >= len(self.note_paths):
                    self._mark("C1", "Bytes came back from each named note.")
                return
            if kind == "files" and action == "write":
                self.prove_summary(path)
                return
            return
        if kind == "files" and action == "write":
            self.prove_disk(path)
            return
        if self._file_job():
            return
        if kind == "files" and action == "read" and (result or "").strip():
            self._mark("C1", f"Bytes came back from {path}.")
            return
        if kind == "files" and action == "list" and (result or "").strip():
            self._mark("C1", "The folder listing came back.")
            return
        if kind in {"project", "memory", "history"} and (result or "").strip():
            self._mark("C1", "The tool returned output.")
            return
        if kind in {"shell", "ssh", "windows"}:
            note = "The command returned output." if (result or "").strip() else "The command ran."
            self._mark("C1", note)
            return
        if kind == "search" and (result or "").strip():
            self._mark("C1", "The search returned a result.")

    def note_finish(self, action: str, body: str) -> None:
        if action == "blocked" and body and not is_bare_not_written(body):
            self.blocker = " ".join(body.split())

    def add_defect(self, defect: Defect) -> None:
        self.defects.append(defect)
        self.defect_rounds += 1
        self.proven.discard(defect.line)
        self.plan.append(f"P{len(self.plan) + 1} -> {defect.line}: {defect.detail}")

    def _note_for(self, line_id: str) -> str:
        notes = [note for item, note in self.evidence if item == line_id]
        return notes[-1] if notes else ""

    def status_text(self) -> str:
        rows: list[str] = []
        for line_id, text in self.lines:
            if line_id in self.proven:
                note = self._note_for(line_id)
                extra = f" Evidence: {note}" if note else ""
                rows.append(f"Proven: {line_id} {text}{extra}")
            else:
                rows.append(f"Unproven: {line_id} {text}")
        if self.blocker:
            rows.append(f"Blocked: {self.blocker}")
        if self.used_breaker:
            rows.append("The breaker is the same model, not a stronger judge.")
        return "\n".join(rows)

    def render(self) -> str:
        contract = "\n".join(f"{line_id}: {text}" for line_id, text in self.lines)
        evidence = "\n".join(f"{line_id}: {note}" for line_id, note in self.evidence) or "(none yet)"
        plan = "\n".join(self.plan)
        defects = "\n".join(f"{item.line}: {item.detail} Evidence: {item.evidence}" for item in self.defects)
        defect_block = f"\n# Defects\n{defects}\n" if defects else ""
        return (
            f"{_MARKER}\n"
            f"Playbook: {self.playbook}\n"
            f"{playbook_text(self.playbook)}\n"
            f"# CONTRACT\n{contract}\n"
            f"Assumptions: {self.assumptions}\n"
            f"Out of scope: {self.scope}\n"
            f"# PLAN\n{plan}\n"
            f"# EVIDENCE\n{evidence}\n"
            f"{defect_block}"
        )


def inject_working(messages: list[dict], ledger: Ledger) -> list[dict]:
    """Put the working files back on every step. The latest tool note stays last."""
    kept = [item for item in messages if _MARKER not in str(item.get("content") or "")]
    block = {"role": "user", "content": ledger.render()}
    if not kept:
        return [block]
    kept.insert(len(kept) - 1, block)
    return kept


BREAKER_PROMPT = (
    "You did not write this answer. Name one defect tied to one contract line. "
    "Use the shape C1: what is wrong. Evidence: the tool output or the file read-back that shows it. "
    "If you cannot name that, reply NONE. Do not say it looks fine, and do not say to consider adding more detail. "
    "A failure to find a problem is not approval. "
    "You are the same model as the author, not a stronger judge."
)
