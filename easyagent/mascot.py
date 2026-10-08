"""The EasyAgent screen-face buddy, and the color of each bot's face.

The drawing is a pixel grid: integer rects, crisp edges, no traced photo.
The full body is the welcome mark. A bot avatar is only the head.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

PALETTE = (
    "#c4532a",
    "#2a6fdb",
    "#1f8a4c",
    "#c43b7a",
    "#b86e12",
    "#5c4d9a",
    "#0e7c86",
    "#8f2d28",
    "#3d6b4f",
    "#a34b2e",
    "#3a4f8a",
    "#6b4a2a",
)

INK = "#1c1b19"
PAPER = "#f6f4ef"
TAGLINE = "AI agents, made easy."


def face_color_for(bot_id: str) -> str:
    """A stable palette color for a bot that has not chosen one."""
    digest = hashlib.sha256(str(bot_id).encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def clean_face_color(value: str | None):
    """A palette hex, or '' when the caller is clearing a saved color."""
    from easyagent.store import StoreError

    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    for color in PALETTE:
        if text == color:
            return color
    raise StoreError("Pick a face color from the row. The bot was not changed.", 400)


def _rects(parts: list[tuple[int, int, int, int]]) -> str:
    return "".join(f'<rect x="{x}" y="{y}" width="{w}" height="{h}"/>' for x, y, w, h in parts)


def _badge() -> tuple[list[tuple[int, int, int, int]], list[tuple[int, int, int, int]]]:
    """Antenna badge. The shell is ink; the '1' is paper cut out of it."""
    shell = [(14, 0, 8, 8), (17, 8, 2, 3)]
    # Interior of the badge, then the digit is punched by not covering it.
    # Drawn as paper first is wrong: shell is a solid block, paper draws the 1.
    one = [
        (16, 2, 1, 1),
        (15, 3, 2, 1),
        (16, 4, 1, 1),
        (16, 5, 1, 1),
        (15, 6, 3, 1),
    ]
    return shell, one


def _head_shell() -> list[tuple[int, int, int, int]]:
    badge, _one = _badge()
    return badge + [
        (10, 11, 16, 1),
        (8, 12, 20, 1),
        (7, 13, 22, 12),
        (8, 25, 20, 1),
        (10, 26, 16, 1),
    ]


def _screen() -> list[tuple[int, int, int, int]]:
    return [
        (11, 14, 14, 1),
        (9, 15, 18, 8),
        (11, 23, 14, 1),
    ]


def _eyes(kind: str) -> list[tuple[int, int, int, int]]:
    if kind == "mid":
        return [(12, 17, 2, 2), (22, 17, 2, 2)]
    if kind == "left":
        return [(10, 17, 2, 2), (20, 17, 2, 2)]
    if kind == "right":
        return [(14, 17, 2, 2), (24, 17, 2, 2)]
    if kind == "up":
        return [(12, 15, 2, 2), (22, 15, 2, 2)]
    if kind == "squint":
        return [(12, 18, 2, 1), (22, 18, 2, 1)]
    if kind == "x":
        return [
            (12, 16, 1, 1), (14, 16, 1, 1), (13, 17, 1, 1), (12, 18, 1, 1), (14, 18, 1, 1),
            (22, 16, 1, 1), (24, 16, 1, 1), (23, 17, 1, 1), (22, 18, 1, 1), (24, 18, 1, 1),
        ]
    return []


def _mouth(kind: str) -> list[tuple[int, int, int, int]]:
    if kind == "smile":
        return [(13, 20, 1, 1), (23, 20, 1, 1), (14, 21, 1, 1), (22, 21, 1, 1), (15, 22, 6, 1)]
    if kind == "flat":
        return [(14, 21, 8, 1)]
    if kind == "open":
        return [(15, 20, 6, 2)]
    return []


def _blink() -> list[tuple[int, int, int, int]]:
    """Paper over the eyes, then a shut line."""
    return [(10, 15, 16, 5)]


def _blink_line() -> list[tuple[int, int, int, int]]:
    return [(12, 18, 2, 1), (22, 18, 2, 1)]


def _body() -> tuple[list[tuple[int, int, int, int]], list[tuple[int, int, int, int]], list[tuple[int, int, int, int]]]:
    """Ink body, paper belly, ink digits. The raised arm is the viewer's left."""
    ink = [
        (15, 27, 6, 2),
        (10, 29, 16, 1),
        (8, 30, 20, 8),
        (10, 38, 16, 1),
        # waving arm
        (5, 24, 4, 3),
        (3, 21, 4, 4),
        (2, 19, 3, 3),
        # hanging arm
        (26, 31, 3, 6),
        (27, 36, 3, 2),
        # legs and feet
        (12, 39, 4, 4),
        (20, 39, 4, 4),
        (10, 43, 7, 2),
        (19, 43, 7, 2),
    ]
    belly = [(11, 32, 14, 5)]
    digits = [
        # 1
        (12, 32, 1, 1), (11, 33, 2, 1), (12, 34, 1, 1), (12, 35, 1, 1), (11, 36, 3, 1),
        # 0
        (16, 32, 3, 1), (16, 33, 1, 1), (18, 33, 1, 1), (16, 34, 1, 1), (18, 34, 1, 1),
        (16, 35, 1, 1), (18, 35, 1, 1), (16, 36, 3, 1),
        # 1
        (21, 32, 1, 1), (20, 33, 2, 1), (21, 34, 1, 1), (21, 35, 1, 1), (20, 36, 3, 1),
    ]
    return ink, belly, digits


def _group(cls: str, fill: str, parts: list[tuple[int, int, int, int]]) -> str:
    if not parts:
        return ""
    return f'<g class="{cls}" fill="{fill}">{_rects(parts)}</g>'


def svg_face() -> str:
    """Head only: antenna, screen, and the frames a run can show."""
    _shell_badge, one = _badge()
    groups = [
        _group("shell", INK, _head_shell()),
        _group("screen", PAPER, _screen()),
        _group("mark", PAPER, one),
        _group("eyes eyes-mid", INK, _eyes("mid")),
        _group("eyes eyes-left", INK, _eyes("left")),
        _group("eyes eyes-right", INK, _eyes("right")),
        _group("eyes eyes-up", INK, _eyes("up")),
        _group("eyes eyes-squint", INK, _eyes("squint")),
        _group("eyes eyes-x", INK, _eyes("x")),
        _group("mouth mouth-smile", INK, _mouth("smile")),
        _group("mouth mouth-flat", INK, _mouth("flat")),
        _group("mouth mouth-open", INK, _mouth("open")),
        _group("blink", PAPER, _blink()),
        _group("blink-line", INK, _blink_line()),
        _group("bulb", "#d0892a", [(21, 2, 2, 2)]),
        _group("thought thought-1", INK, [(31, 8, 2, 2)]),
        _group("thought thought-2", INK, [(33, 12, 2, 2)]),
        _group("thought thought-3", INK, [(31, 16, 2, 2)]),
    ]
    body = "".join(groups)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 30" '
        'shape-rendering="crispEdges" role="img" aria-hidden="true">'
        f"{body}</svg>"
    )


def svg_mascot() -> str:
    """Full-body buddy. Fixed ink on a clear background. Idle face."""
    _shell_badge, one = _badge()
    ink_body, belly, digits = _body()
    groups = [
        _group("shell", INK, _head_shell() + ink_body),
        _group("screen", PAPER, _screen() + belly),
        _group("mark", PAPER, one),
        _group("eyes-mid", INK, _eyes("mid")),
        _group("mouth-smile", INK, _mouth("smile")),
        _group("digits", INK, digits),
    ]
    body = "".join(groups)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 46" '
        'shape-rendering="crispEdges" role="img">'
        "<title>EasyAgent</title>"
        f"{body}</svg>"
    )


def _paint(size: int, parts: list[tuple[str, list[tuple[int, int, int, int]]]], canvas=(36, 46)):
    from PIL import Image

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grid_w, grid_h = canvas
    margin = max(2, size // 16)
    inner = size - margin * 2
    scale = min(inner / grid_w, inner / grid_h)
    offset_x = margin + (inner - grid_w * scale) / 2
    offset_y = margin + (inner - grid_h * scale) / 2
    # Paper tile so the ink reads on a dark dock. The margin stays clear.
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    tile = (
        int(offset_x),
        int(offset_y),
        int(offset_x + grid_w * scale),
        int(offset_y + grid_h * scale),
    )
    paper = tuple(int(PAPER[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
    draw.rectangle(tile, fill=paper)
    for fill, rects in parts:
        color = tuple(int(fill[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
        for x, y, w, h in rects:
            box = (
                int(offset_x + x * scale),
                int(offset_y + y * scale),
                int(offset_x + (x + w) * scale),
                int(offset_y + (y + h) * scale),
            )
            draw.rectangle(box, fill=color)
    return image


def mascot_layers():
    _badge_shell, one = _badge()
    ink_body, belly, digits = _body()
    return [
        (INK, _head_shell() + ink_body),
        (PAPER, _screen() + belly + one),
        (INK, _eyes("mid") + _mouth("smile") + digits),
    ]


def icon_image(size: int):
    return _paint(size, mascot_layers())


def write_icns(path: Path, pngs: list[tuple[str, bytes]]) -> None:
    chunks = []
    for kind, data in pngs:
        chunks.append(kind.encode("ascii") + struct.pack(">I", 8 + len(data)) + data)
    body = b"".join(chunks)
    path.write_bytes(b"icns" + struct.pack(">I", 8 + len(body)) + body)


def write_icons(icon_dir: Path, favicon: Path) -> None:
    from io import BytesIO

    from PIL import Image

    icon_dir.mkdir(parents=True, exist_ok=True)
    sizes = {
        32: icon_dir / "32x32.png",
        128: icon_dir / "128x128.png",
        256: icon_dir / "128x128@2x.png",
        512: icon_dir / "icon.png",
    }
    images = {size: icon_image(size) for size in (16, 32, 48, 64, 128, 256, 512, 1024)}
    for size, path in sizes.items():
        images[size].save(path, format="PNG")
    images[32].save(favicon, format="PNG")
    images[256].save(
        icon_dir / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    named = [
        ("icp4", 16),
        ("icp5", 32),
        ("icp6", 64),
        ("ic07", 128),
        ("ic08", 256),
        ("ic09", 512),
        ("ic10", 1024),
    ]
    pngs = []
    for kind, size in named:
        buf = BytesIO()
        images[size].save(buf, format="PNG")
        pngs.append((kind, buf.getvalue()))
    write_icns(icon_dir / "icon.icns", pngs)


def tray_image(count: int):
    """64px buddy. A positive count adds a badge so 0, 1, 12, and 99+ differ."""
    from PIL import ImageDraw, ImageFont

    image = icon_image(64)
    if count <= 0:
        return image
    label = "99+" if count > 99 else str(int(count))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((34, 36, 62, 62), radius=4, fill=(28, 27, 25, 255))
    font_size = 16 if len(label) > 2 else 18
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), label, font=font)
    x = 34 + (28 - (bbox[2] - bbox[0])) / 2 - bbox[0]
    y = 36 + (26 - (bbox[3] - bbox[1])) / 2 - bbox[1]
    draw.text((x, y), label, fill=(246, 244, 239, 255), font=font)
    return image


def write_assets(root: Path) -> None:
    root = Path(root)
    mascot = svg_mascot()
    face = svg_face()
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "mascot.svg").write_text(mascot, encoding="utf-8")
    static = root / "easyagent" / "static"
    static.mkdir(parents=True, exist_ok=True)
    (static / "mascot.svg").write_text(mascot, encoding="utf-8")
    (static / "face.svg").write_text(face, encoding="utf-8")
    write_icons(root / "desktop" / "src-tauri" / "icons", static / "favicon.png")


def face_frame(state: str, tick: int):
    """One crisp frame of a run state, for the strip and the GIF."""
    from PIL import Image, ImageDraw

    scale = 8
    width, height = 36 * scale, 30 * scale
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    colors = {
        "idle": INK,
        "talking": "#c4532a",
        "waiting": "#d0892a",
        "thinking": "#5c4d9a",
        "tool": "#2a6fdb",
        "halted": "#8f2d28",
    }
    shell = "#c4532a" if state == "talking" else colors.get(state, INK)

    def block(fill, rects):
        color = tuple(int(fill[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
        for x, y, w, h in rects:
            draw.rectangle((x * scale, y * scale, (x + w) * scale, (y + h) * scale), fill=color)

    _badge_shell, one = _badge()
    block(shell, _head_shell())
    block(PAPER, _screen())
    block(PAPER, one)
    eyes = "mid"
    mouth = "smile"
    bulb = False
    thoughts = 0
    if state == "waiting":
        eyes = ("left", "mid", "right", "mid", "blink")[tick % 5]
        mouth = "smile"
    elif state == "thinking":
        eyes = "up"
        mouth = "smile"
        bulb = tick % 2 == 0
        thoughts = (tick % 3) + 1
    elif state == "tool":
        eyes = "squint" if tick % 2 == 0 else "mid"
        mouth = "flat" if tick % 2 == 0 else "open"
        bulb = True
    elif state == "talking":
        eyes = "mid"
        mouth = "open" if tick % 2 == 0 else "smile"
    elif state == "halted":
        eyes = "x"
        mouth = "flat"
    elif state == "idle":
        eyes = "blink" if tick % 6 == 5 else "mid"
        mouth = "smile"
    if eyes == "blink":
        block(PAPER, _blink())
        block(INK, _blink_line())
    else:
        block(INK, _eyes(eyes))
    block(INK, _mouth(mouth))
    if bulb:
        block(colors.get(state, INK), [(21, 2, 2, 2)])
    for index in range(thoughts):
        spots = [(31, 8, 2, 2), (33, 12, 2, 2), (31, 16, 2, 2)]
        block(INK, [spots[index]])
    return image


if __name__ == "__main__":
    write_assets(Path(__file__).resolve().parents[1])
