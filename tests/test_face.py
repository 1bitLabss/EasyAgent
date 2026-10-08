"""A bot's face color is stable, a saved color overrides it, and the mascot is SVG."""

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.mascot import BODY, FACE_SMALL, PALETTE, face_color_for, svg_face, svg_mascot


def test_face_color_is_stable_for_an_id_and_not_written_until_chosen(tmp_path):
    first = face_color_for("bot-a")
    assert first == face_color_for("bot-a")
    assert first in PALETTE
    colors = {face_color_for(f"bot-{i}") for i in range(40)}
    assert len(colors) > 1

    with TestClient(create_app(tmp_path)) as client:
        endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
        bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
        assert bot["face_color"] == face_color_for(bot["id"])
        assert bot["face_color_set"] is False
        on_disk = (tmp_path / "bots" / bot["id"] / "bot.json").read_text(encoding="utf-8")
        assert "face_color" not in on_disk

        other = client.post("/api/bots", json={"name": "Bea", "endpoint_id": endpoint["id"]}).json()
        assert other["face_color"] == face_color_for(other["id"])


def test_a_palette_color_overrides_and_a_bad_color_is_refused(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
        bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
        chat = client.post(f"/api/bots/{bot['id']}/chats").json()
        client.post(f"/api/bots/{bot['id']}/chats/{chat['id']}/messages", json={"content": "hi"})
        before = (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_bytes()
        chosen = PALETTE[0]
        if chosen == bot["face_color"]:
            chosen = PALETTE[1]
        patched = client.patch(f"/api/bots/{bot['id']}", json={"face_color": chosen.upper()})
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["face_color"] == chosen
        assert body["face_color_set"] is True
        on_disk = (tmp_path / "bots" / bot["id"] / "bot.json").read_text(encoding="utf-8")
        assert chosen in on_disk
        assert (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_bytes() == before

        refused = client.patch(f"/api/bots/{bot['id']}", json={"face_color": "#ffffff"})
        assert refused.status_code == 400
        assert "not changed" in refused.text
        assert chosen in (tmp_path / "bots" / bot["id"] / "bot.json").read_text(encoding="utf-8")

        cleared = client.patch(f"/api/bots/{bot['id']}", json={"face_color": ""})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["face_color_set"] is False
        assert "face_color" not in (tmp_path / "bots" / bot["id"] / "bot.json").read_text(encoding="utf-8")
        assert (tmp_path / "bots" / bot["id"] / "chats" / f"{chat['id']}.json").read_bytes() == before


def test_mascot_svg_is_crisp_and_the_page_uses_the_tagline(tmp_path):
    for raw in (svg_mascot(), svg_face()):
        root = ET.fromstring(raw)
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert root.attrib.get("shape-rendering") == "crispEdges"
        assert root.find(".//{http://www.w3.org/2000/svg}rect") is not None
    face = svg_face()
    assert "eyes-left" in face and "eyes-up" in face and "eyes-x" in face and "mouth-open" in face
    assert "101" not in face

    with TestClient(create_app(tmp_path)) as client:
        page = client.get("/classic")
        assert page.status_code == 200
        assert "AI agents, made easy." in page.text
        assert 'src="/static/mascot.svg"' in page.text
        mascot = client.get("/static/mascot.svg")
        avatar = client.get("/static/face.svg")
        assert mascot.status_code == 200 and avatar.status_code == 200
        assert "crispEdges" in mascot.text and "<rect" in avatar.text
        script = client.get("/static/app.js?v=41").text
        css = client.get("/static/app.css?v=36").text
        for color in PALETTE:
            assert color in script
        for needle in ("function makeFace", "function faceStateFor", "is-talking", "is-reconnecting", "eyes-left", "eyes-up", "eyes-x", "mouth-open"):
            assert needle in script
        assert "steps(1" in css
        assert "prefers-reduced-motion" in css
        assert ".buddy-face.is-halted" in css


def test_the_approved_mascot_is_locked():
    """The grid and the two reference photos do not change without approval."""
    root = Path(__file__).resolve().parents[1]
    grid = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    small = hashlib.sha256(FACE_SMALL.encode("utf-8")).hexdigest()
    original = hashlib.sha256((root / "assets" / "mascot-original.jpg").read_bytes()).hexdigest()
    banner = hashlib.sha256((root / "assets" / "banner.jpg").read_bytes()).hexdigest()
    assert grid == "294c3931a1e8d7dd0bf49b494e1b36735d2e42b0ae9d89d361518355d9a10f3f"
    assert small == "475fc38119814dcb2065c8517c965b64ae9763dfecabdf9d65e218320d729925"
    assert original == "77c1413ee1f59e5ca909ae88514a77cf7360235b014f4378f9d40ac75da6b8c9"
    assert banner == "46d3aa0b4808e209273f0767a9f52aa2734d3f071d45f072069902f6320da17a"
