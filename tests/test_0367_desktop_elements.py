"""Wave 2 of the desktop tool: the UIA element tree, SOM overlays, element-index
clicks, and screenshot dedup.

The UIA layer is faked at the comtypes boundary (desktop_uia._get_uia) — no live
desktop, no real UIAutomation here.
"""

from types import SimpleNamespace

import pytest
from PIL import Image

from easyagent import browser, desktop, desktop_uia
from easyagent.desktop import judge_desktop, perform, reset_for_tests
from easyagent.tools import ToolError, ToolRequest, _request_from_call


# ------------------------------------------------------------- fake UIA layer

class FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class FakeElement:
    touched = 0  # counts nodes the walk actually visited

    def __init__(self, control_type=None, name="", rect=(0, 0, 0, 0), children=()):
        self._control_type = control_type
        self._name = name
        self._rect = FakeRect(*rect)
        self.children = list(children)
        self.next = None
        for a, b in zip(self.children, self.children[1:]):  # UIA siblings chain
            a.next = b

    @property
    def CurrentControlType(self):
        FakeElement.touched += 1
        return self._control_type

    @property
    def CurrentName(self):
        return self._name

    @property
    def CurrentBoundingRectangle(self):
        return self._rect


class FakeWalker:
    def GetFirstChildElement(self, element):
        return element.children[0] if element.children else None

    def GetNextSiblingElement(self, element):
        return getattr(element, "next", None)


class FakeUIA:
    def __init__(self, root):
        self.root = root
        self.ControlViewWalker = FakeWalker()

    def ElementFromHandle(self, hwnd):
        return self.root

    def GetRootElement(self):
        return self.root


def chain(count, control_type=50000):
    """A root with `count` button siblings in a chain."""
    root = FakeElement(children=[FakeElement(control_type, f"b{i}", (i * 10, 0, i * 10 + 8, 8)) for i in range(count)])
    kids = root.children
    for a, b in zip(kids, kids[1:]):
        a.next = b
    return root


BUTTON, EDIT = 50000, 50004


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    reset_for_tests()
    FakeElement.touched = 0
    monkeypatch.setattr(desktop, "_ensure_interactive", lambda: None)
    monkeypatch.setattr(desktop, "_grab", lambda bbox: Image.new("RGB", (100, 80)))
    clicks: list = []
    monkeypatch.setattr(desktop, "click_at", lambda x, y, double=False: clicks.append((x, y, double)))
    monkeypatch.setattr(desktop, "type_text", lambda text: None)
    monkeypatch.setattr(desktop, "press_combo", lambda combo: None)
    monkeypatch.setattr(browser, "model_has_vision", lambda store, bot_id: True)
    yield SimpleNamespace(clicks=clicks)


def fake_tree(monkeypatch, root):
    monkeypatch.setattr(desktop_uia, "_get_uia", lambda: FakeUIA(root))


# ------------------------------------------------------------- element listing

def test_element_lines_format_labels_and_geometry(monkeypatch):
    long_name = "x" * 200
    root = FakeElement(
        children=[
            FakeElement(BUTTON, "OK", (10, 20, 110, 60)),
            FakeElement(EDIT, long_name, (0, 0, 0, 0)),
            FakeElement(50006, "a-photo", (5, 5, 55, 45)),  # Image: not interactable
            FakeElement(BUTTON, "hidden-offscreen", (-500, -500, -400, -400)),  # off-screen
        ]
    )
    fake_tree(monkeypatch, root)
    text, shot = perform(None, ToolRequest(kind="desktop", action="elements"), "bot")
    assert "  #1 Button 'OK' @ [10, 20, 100, 40]" in text
    assert "  #2 Edit '" + "x" * 117 + "...' @ bounds-unknown (click by element index)" in text
    assert "a-photo" not in text and "hidden-offscreen" not in text
    assert shot.startswith("data:image/jpeg;base64,")


def test_element_cap_and_spill_note(monkeypatch):
    fake_tree(monkeypatch, chain(105))
    elements = desktop_uia.walk_window(0)
    visible = [e for e in elements if e.get("role") != "more"]
    assert len(visible) == 100
    assert elements[-1] == {"role": "more", "count": 5}
    text, _shot = perform(None, ToolRequest(kind="desktop", action="elements"), "bot")
    assert "... +5 more (narrow with window=)" in text


def test_walk_budget_stops_at_two_hundred_nodes(monkeypatch):
    fake_tree(monkeypatch, chain(500))
    elements = desktop_uia.walk_window(0)
    assert FakeElement.touched <= 200
    assert FakeElement.touched == 200  # the hard cap, not an early stop
    # Wave 2: a walk that exhausts its node budget now also appends a
    # {role: "truncated"} marker so callers can fall back to pixels.
    assert len([e for e in elements if e.get("role") not in ("more", "truncated")]) == 100


# ------------------------------------------------------------- click by index

def seeded_elements(monkeypatch):
    root = FakeElement(
        children=[
            FakeElement(BUTTON, "Send", (100, 200, 200, 260)),
            FakeElement(EDIT, "body", (0, 0, 0, 0)),
        ]
    )
    fake_tree(monkeypatch, root)
    perform(None, ToolRequest(kind="desktop", action="elements"), "bot")


def test_click_by_index_clicks_the_center_of_bounds(monkeypatch, fake):
    seeded_elements(monkeypatch)
    text, _shot = perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    assert fake.clicks == [(150, 230, False)]
    assert "element 1" in text


def test_click_by_index_without_a_snapshot_is_refused(fake):
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    assert "no element list yet" in str(caught.value)
    assert fake.clicks == []


def test_out_of_range_index_names_the_count(monkeypatch, fake):
    seeded_elements(monkeypatch)
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="click", command="element:7"), "bot")
    assert "it has 2" in str(caught.value)
    assert fake.clicks == []


def test_bounds_unknown_element_refuses_an_index_click(monkeypatch, fake):
    seeded_elements(monkeypatch)
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="click", command="element:2"), "bot")
    assert "no position" in str(caught.value)


def test_input_actions_invalidate_the_snapshot(monkeypatch, fake):
    seeded_elements(monkeypatch)
    perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    assert "no element list yet" in str(caught.value)
    seeded_elements(monkeypatch)
    perform(None, ToolRequest(kind="desktop", action="type", body="hi"), "bot")
    with pytest.raises(ToolError):
        perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")


# ------------------------------------------------------------- dedup

def test_identical_screenshots_are_omitted_until_the_streak_cap(monkeypatch):
    still = Image.new("RGB", (100, 80))
    monkeypatch.setattr(desktop, "_grab", lambda bbox: still)
    _text, shot = perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    assert shot.startswith("data:image/jpeg")  # 1st: sent
    text, shot = perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    assert shot == "" and "screenshot unchanged — omitted" in text  # 2nd: omitted
    _text, shot = perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    assert shot == ""  # 3rd: streak still under the cap of 2
    _text, shot = perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    assert shot.startswith("data:image/jpeg")  # streak cap reached: sent anyway
    reset_for_tests()
    _text, shot = perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    assert shot.startswith("data:image/jpeg")  # reset: sent again


def test_a_changed_screen_resets_the_dedup(monkeypatch):
    images = [Image.new("RGB", (100, 80)), Image.new("RGB", (100, 80), "white")]
    monkeypatch.setattr(desktop, "_grab", lambda bbox: images[0])
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    monkeypatch.setattr(desktop, "_grab", lambda bbox: images[1])
    _text, shot = perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    assert shot.startswith("data:image/jpeg")


# ------------------------------------------------------------- SOM overlay

def test_som_overlay_draws_boxes_and_numbers():
    image = Image.new("RGB", (100, 80))
    before = image.tobytes()
    desktop._draw_som(
        image,
        [{"index": 1, "role": "Button", "label": "OK", "bounds": (10, 10, 40, 20)},
         {"index": 2, "role": "Edit", "label": "", "bounds": (0, 0, 0, 0)}],
        (0, 0, 100, 80),
    )
    assert image.tobytes() != before
    assert any(sum(px) < 600 and px[0] > px[1] for px in image.getdata())  # reddish ink


def test_elements_action_overlays_marks_on_the_shot(monkeypatch):
    seeded_elements(monkeypatch)
    monkeypatch.setattr(desktop, "_grab", lambda bbox: Image.new("RGB", (100, 80), "white"))
    drawn: list = []
    monkeypatch.setattr(desktop, "_draw_som", lambda image, elements, bbox: drawn.append((elements, bbox)))
    _text, shot = perform(None, ToolRequest(kind="desktop", action="elements"), "bot")
    assert shot.startswith("data:image/jpeg")
    assert len(drawn) == 1
    assert [e["index"] for e in drawn[0][0]] == [1, 2]


# ------------------------------------------------------------- bounds space

def test_bounds_space_note_fires_past_five_percent_slack():
    image = SimpleNamespace(width=100, height=80)
    far = [{"bounds": (0, 0, 200, 10)}]
    hint = desktop._bounds_hint(far, image)
    assert "native desktop coordinates" in hint and "2.0" in hint
    near = [{"bounds": (0, 0, 104, 10)}]  # 4% over: inside the 5% slack
    assert desktop._bounds_hint(near, image) == ""
    assert desktop._bounds_hint([], image) == ""


# ------------------------------------------------------------- judge + packer

def test_elements_is_free_and_element_clicks_still_ask():
    verdict = judge_desktop(ToolRequest(kind="desktop", action="elements"))
    assert verdict.tier == "allow"
    verdict = judge_desktop(ToolRequest(kind="desktop", action="click", command="element:3"))
    assert verdict.tier == "ask"
    assert verdict.rule == "desktop-input"


def test_the_packer_maps_element_calls():
    call = _request_from_call({"name": "desktop", "arguments": {"action": "elements", "window": "123"}})
    assert (call.kind, call.action, call.path) == ("desktop", "elements", "123")
    call = _request_from_call({"name": "desktop", "arguments": {"action": "click", "element": 4}})
    assert (call.kind, call.action, call.command) == ("desktop", "click", "element:4")
    with pytest.raises(ToolError) as caught:
        _request_from_call({"name": "desktop", "arguments": {"action": "click", "element": "4", "coordinate": "1,2"}})
    assert "not both" in str(caught.value)
    with pytest.raises(ToolError) as caught:
        _request_from_call({"name": "desktop", "arguments": {"action": "teleport"}})
    assert "scroll" in str(caught.value)  # the action list names the wave 3 actions
