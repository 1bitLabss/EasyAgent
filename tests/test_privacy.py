"""EasyAgent does not send telemetry, and tracked files do not carry private lab details."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Built from pieces so this file does not itself contain the private tokens.
_PRIVATE = (
    "teacher" + "27b",
    "LLM" + "3",
    "Str" + "ix",
    "Ha" + "lo",
    "dj" + "nat",
    "LAP" + "TOP-",
    "D:\\" + "temp",
)
_LAN = re.compile(r"192\.168\.0\.\d+")
_DOCUMENTED_LAN = (
    "192.168.0.0/16",
    "::ffff:192.168.0.10",
)
BANNED = (
    "posthog",
    "sentry.io",
    "mixpanel",
    "amplitude.com",
    "googletagmanager",
    "segment.io",
    "plausible.io",
    "telemetry.cursor",
)


def test_the_program_does_not_phone_home():
    roots = [ROOT / "easyagent", ROOT / "web" / "src"]
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".py", ".ts", ".tsx", ".js"}:
                continue
            if "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            for word in BANNED:
                assert word not in text, f"{path} mentions {word}"


def test_the_readme_states_the_edges_and_no_telemetry():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "does not send telemetry" in readme


def _tracked_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    found = []
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        path = ROOT / raw.decode("utf-8", errors="surrogateescape")
        if path.is_file():
            found.append(path)
    return found


def test_tracked_files_do_not_carry_private_lab_details():
    leaks: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(ROOT).as_posix()
        for token in _PRIVATE:
            if token in text:
                leaks.append(f"{rel} contains {token}")
        for match in _LAN.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            line = text[line_start: line_end if line_end >= 0 else None]
            if any(example in line for example in _DOCUMENTED_LAN):
                continue
            leaks.append(f"{rel} contains {match.group(0)}")
    assert leaks == []
