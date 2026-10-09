"""Shipped version strings are the package version, and About reads that from the app."""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

import easyagent
from easyagent.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def _json_version(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def _cargo_package_version(path: Path) -> str:
    match = re.search(r'(?m)^version = "([^"]+)"', path.read_text(encoding="utf-8"))
    assert match, path
    return match.group(1)


def _lock_package_version(name: str) -> str:
    text = (ROOT / "desktop" / "Cargo.lock").read_text(encoding="utf-8")
    match = re.search(rf'name = "{name}"\nversion = "([^"]+)"', text)
    assert match, name
    return match.group(1)


def test_version_strings_agree():
    version = easyagent.__version__
    assert version == "0.3.3"
    assert _cargo_package_version(ROOT / "pyproject.toml") == version
    assert _json_version(ROOT / "desktop" / "src-tauri" / "tauri.conf.json") == version
    for rel in ("web/package.json", "desktop/package.json"):
        assert _json_version(ROOT / rel) == version
    for rel in ("web/package-lock.json", "desktop/package-lock.json"):
        lock = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        assert lock["version"] == version
        assert lock["packages"][""]["version"] == version
    for rel in ("desktop/src-tauri/Cargo.toml", "desktop/supervisor/Cargo.toml"):
        assert _cargo_package_version(ROOT / rel) == version
    assert _lock_package_version("easyagent-desktop") == version
    assert _lock_package_version("easyagent-supervisor") == version
    assert f"Version {version}." in (ROOT / "README.md").read_text(encoding="utf-8")

    classic = (ROOT / "easyagent" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "easyagent" / "static" / "app.js").read_text(encoding="utf-8")
    places = (ROOT / "web" / "src" / "screens" / "Places.tsx").read_text(encoding="utf-8")
    assert "Version 0." not in classic
    assert 'id="about-version"' in classic
    assert "about-version" in app_js
    assert "/api/health" in app_js
    assert "Version 0." not in places
    assert "/api/health" in places
    bundled = list((ROOT / "easyagent" / "ui" / "assets").glob("index-*.js"))
    assert bundled
    for asset in bundled:
        text = asset.read_text(encoding="utf-8")
        assert "Version 0.1.0" not in text
        assert "/api/health" in text


def test_health_reports_the_package_version(tmp_path):
    client = TestClient(create_app(tmp_path))
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["version"] == easyagent.__version__
