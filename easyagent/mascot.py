"""The EasyAgent screen-face buddy, and the color of each bot's face.

The full body is the measured trace of assets/mascot-original.jpg: 42 cells
wide and 44 tall, one square cell per sample. '#' is black and '.' is empty.
A '.' that does not touch the outside of the grid is white: the badge, the
line inside the outline, the face screen, or the 101. The face keeps that
head. 'F' is the fill inside the outline and takes the bot's color. 'A' is
the antenna 1, 'E' the eyes, and 'M' the smile.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

PALETTE = (
    "#c4532a",
    "#1f8a4c",
    "#c43b7a",
    "#b86e12",
    "#0e7c86",
    "#3d6b4f",
    "#a34b2e",
    "#3a4f8a",
    "#6b4a2a",
    "#7a4e8a",
    "#2f6f5e",
    "#9a3d62",
)

INK = "#1c1b19"
PAPER = "#f6f4ef"
TAGLINE = "AI agents, made easy."

# Measured 42x44 trace. Do not redraw it. The raised arm is the viewer's left.
BODY = """\
....................######................
...................#..##..#...............
...................#...#..#...............
...................#...#..#...............
...................#..###.#...............
...................#......#...............
....................######................
......................##..................
......................##..................
................###############...........
.............###...............##.........
............#...##############...#........
...........#..##################..#.......
..........#..####################..#......
..........#.######################.#......
..........#.######################.#......
.........#..######################..#.....
.##......#.####................####.#.....
##.#.....#.###..................###.#.....
#.####...#.###..................###.#.....
#.##.#...#.###...###......###...###.#.....
#.###.#..#.###...###......###...###.#.....
.#.###.#.#.###...###......###...###.#.....
..#####.##.###..................###.#.....
...#####.#.###......#....#......###.#.....
...#.#####.###.......####.......###.##....
....#.####.####................####.#.#...
.....#.###.########################.##.#..
......#..#.########################.##.#..
.......###.######.####.####.#######.###.#.
.........#.#####..###.#.##..#######.###.#.
.........#.######.###.#.###.#######.####.#
.........#.######.###.#.###.#######.####.#
.........#.#####...###.###...######.####.#
.........#..######################..#.##.#
.........##.######################.##.##.#
..........#..####################..#.#..#.
..........##......................##..##..
...........########################.......
.............#.###.#......#.###.#.........
............#.####.#......#.####.#........
...........#.#######......#######.#.......
...........#.#######......#######.#.......
...........########........########.......
"""

# Head for drawings up to about 32px. Same outline and squared corners.
FACE_SMALL = """\
....########....
....#..AA..#....
....#...A..#....
....#...A..#....
....#..AAA.#....
....########....
......##........
..############..
.#............#.
.#.FFFFFFFFFF.#.
.#.#........#.#.
.#.#.EE..EE.#.#.
.#.#.EE..EE.#.#.
.#.#..M..M..#.#.
.#.#...MM...#.#.
.#.FFFFFFFFFF.#.
.#............#.
..############..
"""

_INK_CHARS = "#AEMF"


def face_color_for(bot_id: str, taken: list[str] | tuple[str, ...] | None = None) -> str:
    """A stable palette color. Skip colors another bot already has."""
    digest = hashlib.sha256(str(bot_id).encode("utf-8")).digest()
    start = digest[0] % len(PALETTE)
    used = {item for item in (taken or []) if item in PALETTE}
    if len(used) >= len(PALETTE):
        return PALETTE[start]
    for offset in range(len(PALETTE)):
        color = PALETTE[(start + offset) % len(PALETTE)]
        if color not in used:
            return color
    return PALETTE[start]


def colors_for_bots(bots: list[dict]) -> dict[str, str]:
    """One color per bot. A saved color stays. The rest avoid colors already taken."""
    ordered = sorted(bots or [], key=lambda bot: (str(bot.get("created_at") or ""), str(bot.get("id") or "")))
    saved: dict[str, str] = {}
    for bot in ordered:
        raw = bot.get("face_color")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            color = clean_face_color(raw)
        except Exception:
            continue
        if color:
            saved[str(bot.get("id") or "")] = color
    taken = list(saved.values())
    chosen = dict(saved)
    for bot in ordered:
        bot_id = str(bot.get("id") or "")
        if not bot_id or bot_id in chosen:
            continue
        color = face_color_for(bot_id, taken)
        chosen[bot_id] = color
        taken.append(color)
    return chosen


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


def _rows(text: str) -> list[str]:
    return [line for line in text.strip("\n").split("\n")]


def _points(rows: list[str], chars: str) -> list[tuple[int, int]]:
    found = []
    for y, line in enumerate(rows):
        for x, ch in enumerate(line):
            if ch in chars:
                found.append((x, y))
    return found


def _merge(cells: list[tuple[int, int]]) -> list[tuple[int, int, int, int]]:
    """Unit cells into horizontal runs, then stacked runs of the same width."""
    remaining = set(cells)
    runs: list[tuple[int, int, int]] = []
    for y in sorted({point[1] for point in remaining}):
        xs = sorted(x for x, yy in remaining if yy == y)
        if not xs:
            continue
        start = prev = xs[0]
        for x in xs[1:]:
            if x == prev + 1:
                prev = x
                continue
            runs.append((start, y, prev - start + 1))
            start = prev = x
        runs.append((start, y, prev - start + 1))
    runs.sort()
    used = [False] * len(runs)
    rects = []
    for index, (x, y, width) in enumerate(runs):
        if used[index]:
            continue
        height = 1
        used[index] = True
        next_y = y + 1
        while True:
            match = None
            for other, (ox, oy, owidth) in enumerate(runs):
                if not used[other] and ox == x and oy == next_y and owidth == width:
                    match = other
                    break
            if match is None:
                break
            used[match] = True
            height += 1
            next_y += 1
        rects.append((x, y, width, height))
    return rects


def _enclosed(rows: list[str]) -> list[tuple[int, int]]:
    """Empty cells that do not touch the outside of the grid."""
    height = len(rows)
    width = len(rows[0])
    seen = [[False] * width for _ in range(height)]
    holes: list[tuple[int, int]] = []

    def ink(x: int, y: int) -> bool:
        return rows[y][x] in _INK_CHARS

    for y in range(height):
        for x in range(width):
            if ink(x, y) or seen[y][x]:
                continue
            stack = [(x, y)]
            seen[y][x] = True
            cells: list[tuple[int, int]] = []
            touches = False
            while stack:
                cx, cy = stack.pop()
                cells.append((cx, cy))
                if cx in (0, width - 1) or cy in (0, height - 1):
                    touches = True
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < width and 0 <= ny < height and not seen[ny][nx] and not ink(nx, ny):
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            if not touches:
                holes.extend(cells)
    return holes


def _clusters(cells: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """Split cells into groups separated by a horizontal gap."""
    if not cells:
        return []
    xs = sorted({x for x, _ in cells})
    groups_x: list[list[int]] = []
    current = [xs[0]]
    for x in xs[1:]:
        if x > current[-1] + 1:
            groups_x.append(current)
            current = [x]
        else:
            current.append(x)
    groups_x.append(current)
    grouped = []
    for group in groups_x:
        allowed = set(group)
        grouped.append([(x, y) for x, y in cells if x in allowed])
    return grouped


def _shift(cells: list[tuple[int, int]], dx: int, dy: int) -> list[tuple[int, int]]:
    return [(x + dx, y + dy) for x, y in cells]


def _squint(cells: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The bottom row of each eye, so the squares close to a line."""
    line = []
    for group in _clusters(cells):
        bottom = max(y for _, y in group)
        line.extend((x, bottom) for x, y in group if y == bottom)
    return line


def _exes(cells: list[tuple[int, int]]) -> list[tuple[int, int]]:
    marks = []
    for group in _clusters(cells):
        xs = [x for x, _ in group]
        ys = [y for _, y in group]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        marks.extend(((x0, y0), (x1, y0), (x0, y1), (x1, y1)))
        if x1 - x0 >= 2 and y1 - y0 >= 2:
            marks.append(((x0 + x1) // 2, (y0 + y1) // 2))
    return marks


def _mouths(rows: list[str]):
    cells = _points(rows, "M")
    bar_y = max(y for _, y in cells)
    bar = [(x, y) for x, y in cells if y == bar_y]
    xs = [x for x, _ in bar]
    span = range(min(xs), max(xs) + 1)
    flat = [(x, bar_y) for x in span]
    opened = flat + [(x, bar_y - 1) for x in span]
    return cells, flat, opened


def _blink(cells: list[tuple[int, int]]):
    paper: list[tuple[int, int]] = []
    line: list[tuple[int, int]] = []
    for group in _clusters(cells):
        xs = [x for x, _ in group]
        ys = [y for _, y in group]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                paper.append((x, y))
        mid = (y0 + y1) // 2
        for x in range(x0, x1 + 1):
            line.append((x, mid))
    return paper, line


def _bulb(rows: list[str]) -> tuple[int, int, int, int]:
    """A small lamp in the badge, beside the 1, not on top of it."""
    stem = _points(rows, "A")
    holes = set(_enclosed(rows))
    near = []
    for x, y in stem:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if (x + dx, y + dy) in holes:
                near.append((x + dx, y + dy))
    badge = set()
    stack = list(near)
    while stack:
        cell = stack.pop()
        if cell in badge or cell not in holes:
            continue
        badge.add(cell)
        x, y = cell
        stack.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    ax = sum(point[0] for point in stem) / len(stem)
    ay = sum(point[1] for point in stem) / len(stem)
    best = None
    height, width = len(rows), len(rows[0])
    for y in range(height - 1):
        for x in range(width - 1):
            cells = ((x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1))
            if not all(cell in badge for cell in cells):
                continue
            dist = abs(x + 0.5 - ax) + abs(y + 0.5 - ay)
            if best is None or dist < best[0]:
                best = (dist, x, y)
    if best is not None:
        return (best[1], best[2], 2, 2)
    return (min(point[0] for point in stem) + 1, min(point[1] for point in stem) - 1, 2, 2)


def _rects(parts: list[tuple[int, int, int, int]]) -> str:
    return "".join(f'<rect x="{x}" y="{y}" width="{w}" height="{h}"/>' for x, y, w, h in parts)


def _group(cls: str, fill: str, parts: list[tuple[int, int, int, int]]) -> str:
    if not parts:
        return ""
    return f'<g class="{cls}" fill="{fill}">{_rects(parts)}</g>'


def _by_outside(outside, x: int, y: int, width: int, height: int) -> bool:
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if not (0 <= nx < width and 0 <= ny < height) or outside[ny][nx]:
            return True
    return False


def _head_rows() -> list[str]:
    """Antenna and monitor from the traced grid, without the arms or the feet.

    Rows 0-38 and columns 9-36. Row 38 is the monitor's bottom edge. The
    feet start on the next row. Black pixels outside that outline are the
    arms, and they go. The fill inside the outline is 'F'.
    """
    source = _rows(BODY)
    height, width = len(source), len(source[0])
    outside = [[False] * width for _ in range(height)]
    stack = [(x, y) for x in range(width) for y in (0, height - 1)]
    stack += [(x, y) for y in range(height) for x in (0, width - 1)]
    while stack:
        x, y = stack.pop()
        if not (0 <= x < width and 0 <= y < height) or outside[y][x] or source[y][x] == "#":
            continue
        outside[y][x] = True
        stack.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    seen = [[False] * width for _ in range(height)]
    holes = []
    for y in range(height):
        for x in range(width):
            if source[y][x] == "#" or outside[y][x] or seen[y][x]:
                continue
            pile = [(x, y)]
            seen[y][x] = True
            cells = []
            while pile:
                cx, cy = pile.pop()
                cells.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if (
                        0 <= nx < width
                        and 0 <= ny < height
                        and not seen[ny][nx]
                        and source[ny][nx] != "#"
                        and not outside[ny][nx]
                    ):
                        seen[ny][nx] = True
                        pile.append((nx, ny))
            holes.append(cells)
    ring = set(max((cells for cells in holes if min(y for _, y in cells) <= 12 and max(y for _, y in cells) >= 30), key=len))
    seen = [[False] * width for _ in range(height)]
    ink_parts = []
    for y in range(height):
        for x in range(width):
            if source[y][x] != "#" or seen[y][x]:
                continue
            pile = [(x, y)]
            seen[y][x] = True
            cells = []
            while pile:
                cx, cy = pile.pop()
                cells.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < width and 0 <= ny < height and not seen[ny][nx] and source[ny][nx] == "#":
                        seen[ny][nx] = True
                        pile.append((nx, ny))
            ink_parts.append(cells)
    fill = set(max(ink_parts, key=len))
    eyes = set()
    smile = set()
    for cells in ink_parts:
        xs = [x for x, _ in cells]
        ys = [y for _, y in cells]
        if min(xs) >= 17 and max(xs) <= 30 and min(ys) >= 19 and max(ys) <= 23 and len(cells) <= 12:
            eyes.update(cells)
        if min(xs) >= 19 and max(xs) <= 26 and min(ys) >= 23 and max(ys) <= 26 and len(cells) <= 8:
            smile.update(cells)
    antenna = {(x, y) for y in range(1, 5) for x in range(20, 26) if source[y][x] == "#"}

    def touches_ring(x: int, y: int) -> bool:
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if (dx or dy) and (x + dx, y + dy) in ring:
                    return True
        return False

    face = []
    for y in range(0, 39):
        line = []
        for x in range(9, 37):
            if source[y][x] != "#":
                line.append(".")
                continue
            if (x, y) in eyes:
                line.append("E")
            elif (x, y) in smile:
                line.append("M")
            elif (x, y) in antenna:
                line.append("A")
            elif y <= 8 or (touches_ring(x, y) and (x, y) not in fill):
                line.append("#")
            elif (x, y) in fill or not _by_outside(outside, x, y, width, height):
                # Interior black, including the middle of the 0.
                line.append("F")
            else:
                line.append(".")
        face.append("".join(line))
    return face


def _svg_head(rows: list[str]) -> str:
    width = len(rows[0])
    height = len(rows)
    eyes = _points(rows, "E")
    smile, flat, opened = _mouths(rows)
    blink, blink_line = _blink(eyes)
    bulb = _bulb(rows)
    thoughts = [
        (width + 1, 6, 2, 2),
        (width + 2, 10, 2, 2),
        (width + 1, 14, 2, 2),
    ]
    groups = [
        _group("shell", INK, _merge(_points(rows, "F"))),
        _group("line", INK, _merge(_points(rows, "#"))),
        _group("screen", PAPER, _merge(_enclosed(rows))),
        _group("mark", INK, _merge(_points(rows, "A"))),
        _group("eyes eyes-mid", INK, _merge(eyes)),
        _group("eyes eyes-left", INK, _merge(_shift(eyes, -1, 0))),
        _group("eyes eyes-right", INK, _merge(_shift(eyes, 1, 0))),
        _group("eyes eyes-up", INK, _merge(_shift(eyes, 0, -1))),
        _group("eyes eyes-squint", INK, _merge(_squint(eyes))),
        _group("eyes eyes-x", INK, _merge(_exes(eyes))),
        _group("mouth mouth-smile", INK, _merge(smile)),
        _group("mouth mouth-flat", INK, _merge(flat)),
        _group("mouth mouth-open", INK, _merge(opened)),
        _group("blink", PAPER, _merge(blink)),
        _group("blink-line", INK, _merge(blink_line)),
        _group("bulb", "#d0892a", [bulb]),
        _group("thought thought-1", INK, [thoughts[0]]),
        _group("thought thought-2", INK, [thoughts[1]]),
        _group("thought thought-3", INK, [thoughts[2]]),
    ]
    body = "".join(groups)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width + 5} {height}" '
        'shape-rendering="crispEdges" role="img" aria-hidden="true">'
        f"{body}</svg>"
    )


def svg_face() -> str:
    """Head only: antenna, screen, and the frames a run can show."""
    return _svg_head(_head_rows())


def svg_face_small() -> str:
    """The same head, drawn for a sidebar row or a phone header."""
    return _svg_head(_rows(FACE_SMALL))


def svg_mascot() -> str:
    """Full-body buddy. Fixed ink on a clear background. Idle face."""
    rows = _rows(BODY)
    groups = [
        _group("shell", INK, _merge(_points(rows, "#"))),
        _group("screen", PAPER, _merge(_enclosed(rows))),
        _group("mark", INK, _merge(_points(rows, "A"))),
        _group("eyes-mid", INK, _merge(_points(rows, "E"))),
        _group("mouth-smile", INK, _merge(_points(rows, "M"))),
    ]
    body = "".join(groups)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {len(rows[0])} {len(rows)}" '
        'shape-rendering="crispEdges" role="img">'
        "<title>EasyAgent</title>"
        f"{body}</svg>"
    )


def _paint(size: int, parts: list[tuple[str, list[tuple[int, int, int, int]]]], canvas=(45, 45)):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grid_w, grid_h = canvas
    margin = max(2, size // 16)
    inner = size - margin * 2
    scale = min(inner / grid_w, inner / grid_h)
    offset_x = margin + (inner - grid_w * scale) / 2
    offset_y = margin + (inner - grid_h * scale) / 2
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
    rows = _rows(BODY)
    return [
        (INK, _merge(_points(rows, "#"))),
        (PAPER, _merge(_enclosed(rows))),
        (INK, _merge(_points(rows, "AEM"))),
    ]


def icon_image(size: int):
    rows = _rows(BODY)
    return _paint(size, mascot_layers(), canvas=(len(rows[0]), len(rows)))


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
    small = svg_face_small()
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "mascot.svg").write_text(mascot, encoding="utf-8")
    static = root / "easyagent" / "static"
    static.mkdir(parents=True, exist_ok=True)
    (static / "mascot.svg").write_text(mascot, encoding="utf-8")
    (static / "face.svg").write_text(face, encoding="utf-8")
    (static / "face-small.svg").write_text(small, encoding="utf-8")
    write_icons(root / "desktop" / "src-tauri" / "icons", static / "favicon.png")


def _block(draw, scale, fill, rects):
    color = tuple(int(fill[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
    for x, y, w, h in rects:
        draw.rectangle((x * scale, y * scale, (x + w) * scale, (y + h) * scale), fill=color)


def face_frame(state: str, tick: int):
    """One crisp frame of a run state, for the strip and the GIF."""
    from PIL import Image, ImageDraw

    rows = _head_rows()
    scale = 8
    width, height = (len(rows[0]) + 5) * scale, len(rows) * scale
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
    _block(draw, scale, shell, _merge(_points(rows, "F")))
    _block(draw, scale, INK, _merge(_points(rows, "#")))
    _block(draw, scale, PAPER, _merge(_enclosed(rows)))
    _block(draw, scale, INK, _merge(_points(rows, "A")))
    eyes_cells = _points(rows, "E")
    smile, flat, opened = _mouths(rows)
    eyes = "mid"
    mouth = smile
    bulb = False
    thoughts = 0
    if state == "waiting":
        eyes = ("left", "mid", "right", "mid", "blink")[tick % 5]
    elif state == "thinking":
        eyes = "up"
        bulb = tick % 2 == 0
        thoughts = (tick % 3) + 1
    elif state == "tool":
        eyes = "squint" if tick % 2 == 0 else "mid"
        mouth = flat if tick % 2 == 0 else opened
    elif state == "talking":
        mouth = opened if tick % 2 == 0 else smile
    elif state == "halted":
        eyes = "x"
        mouth = flat
    elif state == "idle":
        eyes = "blink" if tick % 6 == 5 else "mid"
    if eyes == "blink":
        paper, line = _blink(eyes_cells)
        _block(draw, scale, PAPER, _merge(paper))
        _block(draw, scale, INK, _merge(line))
    elif eyes == "left":
        _block(draw, scale, INK, _merge(_shift(eyes_cells, -1, 0)))
    elif eyes == "right":
        _block(draw, scale, INK, _merge(_shift(eyes_cells, 1, 0)))
    elif eyes == "up":
        _block(draw, scale, INK, _merge(_shift(eyes_cells, 0, -1)))
    elif eyes == "squint":
        _block(draw, scale, INK, _merge(_squint(eyes_cells)))
    elif eyes == "x":
        _block(draw, scale, INK, _merge(_exes(eyes_cells)))
    else:
        _block(draw, scale, INK, _merge(eyes_cells))
    _block(draw, scale, INK, _merge(mouth))
    if bulb:
        _block(draw, scale, colors.get(state, INK), [_bulb(rows)])
    grid_w = len(rows[0])
    spots = [(grid_w + 1, 6, 2, 2), (grid_w + 2, 10, 2, 2), (grid_w + 1, 14, 2, 2)]
    for index in range(thoughts):
        _block(draw, scale, INK, [spots[index]])
    return image


if __name__ == "__main__":
    write_assets(Path(__file__).resolve().parents[1])
