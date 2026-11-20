"""Wave 2 of the computer tool: the Hermes contract on top of the Wave 1 desktop
surface — the verdict ladder, screenshot dedup, the 1456 px downscale note,
caps, capture_after, stale-element protection, per-action approval scopes, the
hard blocks that YOLO cannot lift, and the UIA hang fallback.

The whole Win32/UIA layer is faked — no real mouse, keyboard, screen, or
UIAutomation here.
"""

import asyncio
import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from easyagent import browser, desktop, desktop_uia, safety
from easyagent.desktop import canonical_combo, clamp_wait, judge_desktop, perform, reset_for_tests
from easyagent.safety import classify, guard
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest, _request_from_call


def _world(tmp_path, model="llava-7b"):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None, model=model)
    bot = store.add_bot(name="Ann", endpoint_id=endpoint["id"], model=None)
    return store, bot


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    reset_for_tests()
    monkeypatch.setattr(desktop, "_ensure_interactive", lambda: None)
    monkeypatch.setattr(desktop, "list_windows_text", lambda: 'Windows on this computer, frontmost first (1):\n[123] "Notepad" at 0,0 size 800x600')
    clicks: list = []
    rights: list = []
    monkeypatch.setattr(desktop, "_grab", lambda bbox: Image.new("RGB", (200, 150)))
    monkeypatch.setattr(desktop, "click_at", lambda x, y, double=False: clicks.append((x, y, double)))
    monkeypatch.setattr(desktop, "right_click_at", lambda x, y: rights.append((x, y)))
    monkeypatch.setattr(desktop, "type_text", lambda text: None)
    monkeypatch.setattr(desktop, "press_combo", lambda combo: None)
    monkeypatch.setattr(browser, "model_has_vision", lambda store, bot_id: True)
    yield SimpleNamespace(clicks=clicks, rights=rights)


def fake_walk(monkeypatch, trees):
    """Fake desktop_uia.walk_window: pops one tree per call (last one sticks)."""
    calls: list = []

    def walk(hwnd):
        calls.append(hwnd)
        return list(trees.pop(0) if len(trees) > 1 else trees[0])

    monkeypatch.setattr(desktop_uia, "walk_window", walk)
    return calls


def _el(index, label, bounds=(100, 200, 200, 260)):
    return {"index": index, "role": "Button", "label": label, "bounds": bounds}


# ------------------------------------------------------------- verdict ladder

def test_element_action_on_a_changed_tree_reads_done(monkeypatch):
    fake_walk(monkeypatch, [[_el(1, "Send")], [_el(1, "Send"), _el(2, "Sent!")]])
    perform(None, ToolRequest(kind="desktop", action="capture", mode="som"), "bot")
    text, _shot = perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    assert "Clicked element 1" in text and "done:" in text
    assert "verify_fresh_state" not in text and "escalate" not in text


def test_element_action_on_an_unchanged_tree_escalates(monkeypatch):
    fake_walk(monkeypatch, [[_el(1, "Send")]])
    perform(None, ToolRequest(kind="desktop", action="capture", mode="som"), "bot")
    text, _shot = perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    assert "escalate:" in text and "Recommended:" in text
    assert "done:" not in text and "verify_fresh_state" not in text


def test_coordinate_action_and_broken_uia_read_verify_fresh_state(monkeypatch):
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    text, _shot = perform(None, ToolRequest(kind="desktop", action="click", command="5,5"), "bot")
    assert "verify_fresh_state" in text

    def broken(hwnd):
        raise RuntimeError("UIA hung")

    monkeypatch.setattr(desktop_uia, "walk_window", broken)
    with pytest.raises(ToolError):  # som/ax with an unreadable tree say so
        perform(None, ToolRequest(kind="desktop", action="capture", mode="ax"), "bot")
    monkeypatch.setattr(desktop_uia, "walk_window", lambda hwnd: [_el(1, "Send")])
    perform(None, ToolRequest(kind="desktop", action="capture", mode="som"), "bot")
    monkeypatch.setattr(desktop_uia, "walk_window", broken)
    text, _shot = perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    assert "verify_fresh_state" in text


# ------------------------------------------------------------- dedup + caps

def test_identical_frames_omit_the_picture_for_at_most_two_turns(tmp_path):
    store, bot = _world(tmp_path)
    _text, shot = perform(store, ToolRequest(kind="desktop", action="capture"), bot["id"])
    assert shot.startswith("data:image/jpeg")
    for _ in range(2):
        text, shot = perform(store, ToolRequest(kind="desktop", action="capture"), bot["id"])
        assert shot == "" and "screenshot unchanged" in text
    _text, shot = perform(store, ToolRequest(kind="desktop", action="capture"), bot["id"])
    assert shot.startswith("data:image/jpeg")  # streak cap: send it anyway


def test_big_frames_are_downscaled_to_1456_with_a_scale_note(tmp_path, monkeypatch):
    store, bot = _world(tmp_path)
    monkeypatch.setattr(desktop, "_grab", lambda bbox: Image.new("RGB", (3000, 2000), "red"))
    text, shot = perform(store, ToolRequest(kind="desktop", action="capture"), bot["id"])
    assert "downscaled from 3000x2000" in text and "real screen coordinates" in text
    blob = base64.b64decode(shot.split("base64,", 1)[1])
    image = Image.open(io.BytesIO(blob))
    assert max(image.size) == 1456


def test_caps_wait_elements_and_labels():
    assert clamp_wait(900) == 30
    assert desktop_uia._DEFAULT_MAX_ELEMENTS == 100
    assert desktop_uia._MAX_ELEMENT_LABEL_CHARS == 120
    assert desktop_uia._clip_label("x" * 300).__len__() == 120


# ------------------------------------------------------------- capture_after

def test_capture_after_recaptures_the_same_target(tmp_path, monkeypatch):
    store, bot = _world(tmp_path)
    frames = [Image.new("RGB", (200, 150), "red"), Image.new("RGB", (200, 150), "blue")]
    monkeypatch.setattr(desktop, "_grab", lambda bbox: frames.pop(0) if frames else frames[:][0])
    perform(store, ToolRequest(kind="desktop", action="capture"), bot["id"])
    text, shot = perform(
        store, ToolRequest(kind="desktop", action="click", command="5,5", capture_after=True), bot["id"]
    )
    assert "Clicked at 5,5" in text and "Captured the whole screen" in text
    assert shot.startswith("data:image/jpeg")


# ------------------------------------------------------------- stale elements

def test_element_from_a_stale_capture_demands_a_fresh_capture(monkeypatch):
    fake_walk(monkeypatch, [[_el(1, "Send"), _el(2, "Body")]])
    text, _shot = perform(None, ToolRequest(kind="desktop", action="capture", mode="som"), "bot")
    assert "[capture 1]" in text
    perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    assert "no element list yet" in str(caught.value) and "capture again" in str(caught.value)
    fake_walk(monkeypatch, [[_el(1, "Send")]])
    perform(None, ToolRequest(kind="desktop", action="capture", mode="som"), "bot")
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="click", command="element:9"), "bot")
    assert "Capture again" in str(caught.value)


# ------------------------------------------------------------- hard blocks

def test_hyphen_combo_spellings_canonicalize_and_block():
    assert canonical_combo("ctrl-alt-delete") == canonical_combo("ctrl+alt+delete")
    assert canonical_combo("Control - Alt - Delete") == canonical_combo("ctrl+alt+delete")
    for combo in ("ctrl-alt-delete", "Control-Alt-Delete", "ctrl+alt+delete"):
        verdict = judge_desktop(ToolRequest(kind="desktop", action="key", command=combo))
        assert verdict.tier == "block" and verdict.rule == "desktop-blocked", combo


def test_yolo_skips_the_ask_layer_but_not_the_hard_blocks(tmp_path, monkeypatch):
    store, bot = _world(tmp_path)
    monkeypatch.setattr(safety, "_yolo_passes", lambda store, bot_id: True)

    async def run(request):
        return await guard(store, request, bot["id"])

    with pytest.raises(ToolError):
        asyncio.run(run(ToolRequest(kind="desktop", action="key", command="ctrl-alt-delete")))
    # A plain click only needed the ask layer, which YOLO skips.
    request, card = asyncio.run(run(ToolRequest(kind="desktop", action="click", command="5,5")))
    assert card is None


# ------------------------------------------------------------- scopes + strict

def test_approval_scope_is_one_action_class():
    a = judge_desktop(ToolRequest(kind="desktop", action="click", command="10,20"))
    b = judge_desktop(ToolRequest(kind="desktop", action="click", command="999,999"))
    c = judge_desktop(ToolRequest(kind="desktop", action="type", body="hello"))
    assert a.fingerprint == b.fingerprint  # same action class, same scope
    assert a.fingerprint != c.fingerprint  # always-allowing click never allows type


def test_capture_and_list_windows_are_free_even_in_strict(tmp_path):
    store, bot = _world(tmp_path, model="grid")  # add_bot default is strict
    for action in ("capture", "list_windows", "wait"):
        verdict = classify(store, ToolRequest(kind="desktop", action=action), bot["id"])
        assert verdict.tier == "allow", action
    verdict = classify(store, ToolRequest(kind="desktop", action="click", command="5,5"), bot["id"])
    assert verdict.tier == "ask" and verdict.rule == "desktop-input"


# ------------------------------------------------------------- modes + fallback

def test_ax_mode_returns_the_tree_as_text_without_a_picture(monkeypatch):
    fake_walk(monkeypatch, [[_el(1, "Send")]])
    text, shot = perform(None, ToolRequest(kind="desktop", action="capture", mode="ax"), "bot")
    assert "Elements in" in text and "Button 'Send'" in text
    assert shot == ""


def test_picture_blind_capture_returns_the_ax_tree(tmp_path, monkeypatch):
    store, bot = _world(tmp_path, model="grid")
    monkeypatch.setattr(browser, "model_has_vision", lambda store, bot_id: False)
    fake_walk(monkeypatch, [[_el(1, "Send")]])
    text, shot = perform(store, ToolRequest(kind="desktop", action="capture"), bot["id"])
    assert "can't see pictures" in text and "Elements in" in text and "Notepad" in text
    assert shot == ""


def test_uia_walk_over_budget_falls_back_to_pixels_only(monkeypatch):
    fake_walk(monkeypatch, [[_el(1, "Send"), {"role": "truncated"}]])
    text, shot = perform(None, ToolRequest(kind="desktop", action="capture", mode="som"), "bot")
    assert "hit its read budget" in text and "pixels only" in text
    assert shot.startswith("data:image/jpeg")
    assert "Elements in" not in text


def test_truncated_walk_budget_marker_comes_from_the_real_walk(monkeypatch):
    class Rect:
        def __init__(self):
            self.left, self.top, self.right, self.bottom = 0, 0, 10, 10

    class Node:
        CurrentControlType = 50000
        CurrentName = "b"
        CurrentBoundingRectangle = Rect()
        children: list = []
        next = None

    class Walker:
        def GetFirstChildElement(self, e):
            return None

        def GetNextSiblingElement(self, e):
            return Node()

    class UIA:
        ControlViewWalker = Walker()

        def ElementFromHandle(self, hwnd):
            return Node()

        def GetRootElement(self):
            return Node()

    monkeypatch.setattr(desktop_uia, "_get_uia", lambda: UIA())
    walked = desktop_uia.walk_window(1)
    assert any(e.get("role") == "truncated" for e in walked)


# ------------------------------------------------------------- right_click + packer

def test_right_click_packs_judges_and_clicks_the_right_button(fake):
    call = _request_from_call({"name": "computer", "arguments": {"action": "right_click", "coordinate": "640,480"}})
    assert (call.kind, call.action, call.command) == ("desktop", "right_click", "640,480")
    verdict = judge_desktop(call)
    assert verdict.tier == "ask" and verdict.rule == "desktop-input"
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    text, _shot = perform(None, call, "bot")
    assert fake.rights == [(640, 480)]
    assert "Right-clicked at 640,480" in text


def test_capture_mode_packs_and_rejects_nonsense():
    call = _request_from_call({"name": "computer", "arguments": {"action": "capture", "mode": "ax"}})
    assert call.mode == "ax"
    call = _request_from_call({"name": "computer", "arguments": {"action": "capture", "mode": "som"}})
    assert call.mode == "som"
    with pytest.raises(ToolError) as caught:
        _request_from_call({"name": "computer", "arguments": {"action": "capture", "mode": "hologram"}})
    assert "vision, som, or ax" in str(caught.value)
    call = _request_from_call({"name": "computer", "arguments": {"action": "click", "coordinate": "1,2", "capture_after": True}})
    assert call.capture_after is True
