"""PWA icons painted from the locked face.svg. The drawing is not redrawn."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

NS = "{http://www.w3.org/2000/svg}"
# The idle pose the page shows: outline, screen, antenna, mid eyes, smile, bulb.
IDLE = {"shell", "line", "screen", "mark", "eyes-mid", "mouth-smile", "bulb"}
PAPER = (246, 244, 239, 255)


def _visible(class_attr: str) -> bool:
    classes = set((class_attr or "").split())
    if not classes:
        return True
    return bool(classes & IDLE)


def _color(value: str) -> tuple[int, int, int, int]:
    text = (value or "#1c1b19").strip()
    if text.startswith("#") and len(text) == 7:
        return tuple(int(text[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
    return (28, 27, 25, 255)


def _viewbox(root: ET.Element) -> tuple[float, float]:
    raw = root.attrib.get("viewBox", "0 0 33 39")
    parts = [float(item) for item in raw.split()]
    return parts[2], parts[3]


def face_rects(svg_text: str) -> tuple[tuple[float, float], list[tuple[str, float, float, float, float]]]:
    root = ET.fromstring(svg_text)
    rects: list[tuple[str, float, float, float, float]] = []

    def walk(node: ET.Element, fill: str, show: bool) -> None:
        tag = node.tag
        next_fill = node.attrib.get("fill", fill)
        next_show = show
        if tag == f"{NS}g":
            next_show = show and _visible(node.attrib.get("class", ""))
        if tag == f"{NS}rect" and next_show:
            rects.append(
                (
                    next_fill,
                    float(node.attrib.get("x", "0")),
                    float(node.attrib.get("y", "0")),
                    float(node.attrib.get("width", "0")),
                    float(node.attrib.get("height", "0")),
                )
            )
        for child in list(node):
            walk(child, next_fill, next_show)

    walk(root, "#1c1b19", True)
    return _viewbox(root), rects


def render_face_icon(svg_text: str, size: int) -> Image.Image:
    """Crisp pixels of the locked face, padded so a maskable icon keeps the head."""
    (box_w, box_h), rects = face_rects(svg_text)
    image = Image.new("RGBA", (size, size), PAPER)
    margin = round(size * 0.18)
    inner = size - 2 * margin
    scale = min(inner / box_w, inner / box_h)
    offset_x = margin + (inner - box_w * scale) / 2
    offset_y = margin + (inner - box_h * scale) / 2
    draw = ImageDraw.Draw(image)
    for fill, x, y, w, h in rects:
        box = (
            offset_x + x * scale,
            offset_y + y * scale,
            offset_x + (x + w) * scale,
            offset_y + (y + h) * scale,
        )
        draw.rectangle(box, fill=_color(fill))
    return image


def write_pwa_icons(svg_path: Path, icon_dir: Path) -> None:
    svg = svg_path.read_text(encoding="utf-8")
    icon_dir.mkdir(parents=True, exist_ok=True)
    for size, name in ((180, "apple-touch-icon.png"), (192, "icon-192.png"), (512, "icon-512.png")):
        render_face_icon(svg, size).save(icon_dir / name, format="PNG")
