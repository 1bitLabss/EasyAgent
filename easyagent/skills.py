"""Markdown skills the agent can write and reuse.

A skill is a file in the skills directory. The model saves one by including
a fenced block in its reply. The harness stores the file and strips the block
from the visible transcript.
"""

from __future__ import annotations

import re

from easyagent.limits import SKILL_PACK_CAP

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FENCE_RE = re.compile(r"```skill[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
# Placeholder names from the prompt example. Echoing the instructions must not write a skill.
RESERVED_SLUGS = {"kebab-case-name", "short-slug", "name"}


def slugify(name: str) -> str | None:
    raw = (name or "").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not raw or len(raw) > 48 or not SLUG_RE.fullmatch(raw):
        return None
    return raw


def parse_skill_document(text: str) -> dict | None:
    """Parse a skill document. Returns None when the name is missing or unsafe."""
    raw = (text or "").strip()
    if not raw:
        return None
    name = ""
    description = ""
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) < 3:
            return None
        meta, body = parts[1], parts[2]
        for line in meta.splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key = key.strip().lower()
            val = val.strip()
            if key == "name":
                name = val
            elif key == "description":
                description = val
    slug = slugify(name)
    if not slug:
        return None
    return {"name": slug, "description": " ".join(description.split()), "body": body.strip()}


def render_skill(skill: dict) -> str:
    description = " ".join((skill.get("description") or "").split())
    body = (skill.get("body") or "").strip()
    return f"---\nname: {skill['name']}\ndescription: {description}\n---\n\n{body}\n"


def extract_skills(reply: str) -> tuple[str, list[dict]]:
    """Pull ```skill fences out of a model reply.

    Fences that do not parse are left in the text so nothing is discarded silently.
    """
    found: list[dict] = []

    def replace(match: re.Match[str]) -> str:
        parsed = parse_skill_document(match.group(1))
        if not parsed or parsed["name"] in RESERVED_SLUGS:
            return match.group(0)
        found.append(parsed)
        return ""

    visible = FENCE_RE.sub(replace, reply or "")
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    return visible, found


def pack_skills(skills: list[dict]) -> str:
    """Catalog plus as many bodies as fit in the skill budget."""
    if not skills:
        return "(none yet)"
    lines = ["Catalog:"]
    for skill in skills:
        desc = (skill.get("description") or "").strip()
        if len(desc) > 120:
            desc = desc[:119] + "…"
        lines.append(f"- {skill['name']}: {desc}" if desc else f"- {skill['name']}")
    catalog = "\n".join(lines)
    bodies: list[str] = []
    used = len(catalog) + 2
    for skill in skills:
        body = (skill.get("body") or "").strip()
        if not body:
            continue
        chunk = f"### {skill['name']}\n{body}\n"
        if used + len(chunk) > SKILL_PACK_CAP:
            continue
        bodies.append(chunk)
        used += len(chunk)
    if len(catalog) > SKILL_PACK_CAP and not bodies:
        return catalog[: SKILL_PACK_CAP - 1] + "…"
    if not bodies:
        return catalog
    packed = catalog + "\n\n" + "\n".join(bodies)
    if len(packed) > SKILL_PACK_CAP:
        return packed[: SKILL_PACK_CAP - 1] + "…"
    return packed
