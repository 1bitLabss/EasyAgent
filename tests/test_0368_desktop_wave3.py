"""Wave 3 of the desktop tool: scroll, drag, set_value, bring-to-front delivery,
and the doctor report.

The ctypes layer and the UIA walk are faked exactly like test_0366/0367 —
no real mouse, keyboard, screen, or COM here.
"""

from types import SimpleNamespace

import pytest
from PIL import Image

from easyagent import browser, desktop, desktop_uia
from easyagent.desktop import (
    clamp_scroll_amount,
    doctor_report,
    judge_desktop,
    perform,
    reset_for_tests,
)
from easyagent.tools import ToolError, ToolRequest, _request_from_call

from tests.test_0367_desktop_elements import BUTTON, EDIT, FakeElement, FakeUIA

# The autouse fixture fakes these; the primitive tests call the real ones.
REAL_SCROLL_AT = desktop.scroll_at
REAL_DRAG_TO = desktop.drag_to
REAL_RAISE_WINDOW = desktop._raise_window


class FakePatternElement(FakeElement):
    """A fake UIA element that carries a ValuePattern (10002)."""

    def __init__(self, *args, value_pattern=True, read_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._value_pattern = value_pattern
        self._read_only = read_only
        self.set_values = []

    def GetCurrentPattern(self, pattern_id):
        assert pattern_id == 10002  # UIA_ValuePatternId
        if not self._value_pattern:
            raise ValueError("no pattern")
        return self

    @property
    def CurrentIsReadOnly(self):
        return self._read_only

    def SetValue(self, value):
        self.set_values.append(value)


class FakeUser32:
    """Records the wheel/drag/raise calls the primitives make."""

    def __init__(self, foreground=0):
        self.cursor = []
        self.sent = []
        self.raised = []
        self.restored = []
        self._foreground = foreground

    def SetCursorPos(self, x, y):
        self.cursor.append((x, y))

    def SendInput(self, count, array, size):
        for i in range(count):
            event = array[i]
            self.sent.append((event.type, event.mi.dwFlags, event.mi.mouseData))
        return count

    def GetForegroundWindow(self):
        return self._foreground

    def IsIconic(self, hwnd):
        return False

    def ShowWindow(self, hwnd, cmd):
        self.restored.append((hwnd, cmd))

    def SetForegroundWindow(self, hwnd):
        self.raised.append(int(hwnd))

    def GetSystemMetrics(self, index):
        return {0: 1920, 1: 1080}.get(index, 0)


def signed(dword):
    return dword - 0x100000000 if dword >= 0x80000000 else dword


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    reset_for_tests()
    monkeypatch.setattr(desktop, "_ensure_interactive", lambda: None)
    monkeypatch.setattr(desktop, "_grab", lambda bbox: Image.new("RGB", (100, 80)))
    scrolls: list = []
    drags: list = []
    clicks: list = []
    monkeypatch.setattr(desktop, "scroll_at", lambda x, y, d, a: scrolls.append((x, y, d, a)))
    monkeypatch.setattr(desktop, "drag_to", lambda x1, y1, x2, y2: drags.append((x1, y1, x2, y2)))
    monkeypatch.setattr(desktop, "click_at", lambda x, y, double=False: clicks.append((x, y, double)))
    monkeypatch.setattr(desktop, "type_text", lambda text: None)
    monkeypatch.setattr(desktop, "press_combo", lambda combo: None)
    monkeypatch.setattr(desktop, "_cursor_pos", lambda: (7, 9))
    monkeypatch.setattr(desktop, "_raise_window", lambda hwnd: False)
    monkeypatch.setattr(browser, "model_has_vision", lambda store, bot_id: True)
    monkeypatch.setattr(desktop.time, "sleep", lambda seconds: None)
    yield SimpleNamespace(scrolls=scrolls, drags=drags, clicks=clicks)


def seeded_elements(monkeypatch, root=None):
    root = root or FakeElement(
        children=[
            FakeElement(BUTTON, "Send", (100, 200, 200, 260)),
            FakeElement(BUTTON, "Handle", (300, 400, 400, 460)),
        ]
    )
    monkeypatch.setattr(desktop_uia, "_get_uia", lambda: FakeUIA(root))
    perform(None, ToolRequest(kind="desktop", action="elements"), "bot")


# ------------------------------------------------------------------ scroll

def test_scroll_amount_clamps_to_one_through_fifty():
    assert clamp_scroll_amount(0) == 1
    assert clamp_scroll_amount(-5) == 1
    assert clamp_scroll_amount(99) == 50
    assert clamp_scroll_amount(None) == 3
    assert clamp_scroll_amount("nonsense") == 3
    assert clamp_scroll_amount("7") == 7


def test_scroll_direction_maps_to_wheel_flags_and_delta(monkeypatch):
    win = FakeUser32()
    monkeypatch.setattr(desktop, "user32", win)
    REAL_SCROLL_AT(5, 6, "down", 2)
    REAL_SCROLL_AT(5, 6, "up", 1)
    REAL_SCROLL_AT(5, 6, "right", 3)
    REAL_SCROLL_AT(5, 6, "left", 1)
    assert win.cursor == [(5, 6)] * 4
    assert [(flags, signed(data)) for (_t, flags, data) in win.sent] == [
        (0x0800, -240),  # MOUSEEVENTF_WHEEL, -120 * 2
        (0x0800, 120),   # up is positive
        (0x1000, 360),   # MOUSEEVENTF_HWHEEL, right is positive
        (0x1000, -120),  # left is negative
    ]


def test_scroll_uses_cursor_spot_and_clamps_in_perform(fake):
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    text, _shot = perform(None, ToolRequest(kind="desktop", action="scroll", command="down|99|"), "bot")
    assert fake.scrolls == [(7, 9, "down", 50)]
    assert "Scrolled down 50 at 7,9" in text and "verify_fresh_state" in text


def test_scroll_at_element_uses_bounds_center_and_invalidates(monkeypatch, fake):
    seeded_elements(monkeypatch)
    text, _shot = perform(
        None, ToolRequest(kind="desktop", action="scroll", command="down|3|element:1"), "bot"
    )
    assert fake.scrolls == [(150, 230, "down", 3)]
    assert "Scrolled down 3 at 150,230" in text
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    assert "no element list yet" in str(caught.value)


# ------------------------------------------------------------------ drag

def test_drag_without_a_pair_is_refused(fake):
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="drag", command="element:1"), "bot")
    assert "A drag needs from/to elements or from/to coordinates." in str(caught.value)
    assert fake.drags == []


def test_drag_element_pair_uses_bounds_centers_and_invalidates(monkeypatch, fake):
    seeded_elements(monkeypatch)
    text, _shot = perform(
        None, ToolRequest(kind="desktop", action="drag", command="element:1|element:2"), "bot"
    )
    assert fake.drags == [(150, 230, 350, 430)]
    # Wave 2 verdict ladder: an element drag on an unchanged fake tree now reads
    # escalate (suspected no-op), not verify_fresh_state.
    assert "Dragged from 150,230 to 350,430" in text and "escalate" in text
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    assert "no element list yet" in str(caught.value)


def test_drag_coordinate_pair_works(fake):
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    perform(None, ToolRequest(kind="desktop", action="drag", command="10,20|30,40"), "bot")
    assert fake.drags == [(10, 20, 30, 40)]


def test_drag_to_primitive_walks_in_steps(monkeypatch):
    win = FakeUser32()
    monkeypatch.setattr(desktop, "user32", win)
    REAL_DRAG_TO(0, 0, 100, 0)
    flags = [f for (_t, f, _d) in win.sent]
    assert flags == [desktop._MOUSEEVENTF_LEFTDOWN, desktop._MOUSEEVENTF_LEFTUP]
    assert win.cursor[0] == (0, 0) and win.cursor[-1] == (100, 0)
    assert len(win.cursor) == 21  # start plus 20 interpolated steps


# ------------------------------------------------------------------ set_value

def seeded_value_tree(monkeypatch):
    target = FakePatternElement(EDIT, "body", (0, 0, 0, 0))
    root = FakeElement(children=[FakePatternElement(BUTTON, "OK", (10, 20, 110, 60)), target])
    monkeypatch.setattr(desktop_uia, "_get_uia", lambda: FakeUIA(root))
    return root, target


def test_set_value_without_an_element_is_refused(fake):
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="set_value", command="5,5", body="hi"), "bot")
    assert "set_value needs an element number from the element list." in str(caught.value)


def test_set_value_types_through_the_value_pattern(monkeypatch, fake):
    root, target = seeded_value_tree(monkeypatch)
    seeded_elements(monkeypatch, root)
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    text, _shot = perform(
        None, ToolRequest(kind="desktop", action="set_value", command="element:2", body="hello"), "bot"
    )
    assert target.set_values == ["hello"]
    # Wave 2 verdict ladder: element set_value on an unchanged fake tree reads escalate.
    assert "Set element 2 to 5 characters" in text and "escalate" in text
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="click", command="element:1"), "bot")
    assert "no element list yet" in str(caught.value)  # snapshot invalidated


def test_set_value_read_only_refuses(monkeypatch, fake):
    root = FakeElement(
        children=[FakePatternElement(EDIT, "locked", (0, 0, 0, 0), read_only=True)]
    )
    monkeypatch.setattr(desktop_uia, "_get_uia", lambda: FakeUIA(root))
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="set_value", command="element:1", body="hi"), "bot")
    assert "Element 1 can't be typed into." in str(caught.value)


def test_set_value_missing_element_names_it(monkeypatch, fake):
    seeded_value_tree(monkeypatch)
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")
    with pytest.raises(ToolError) as caught:
        perform(None, ToolRequest(kind="desktop", action="set_value", command="element:9", body="hi"), "bot")
    assert "Element 9 is not in this window right now" in str(caught.value)


# ------------------------------------------------------------------ bring to front

def test_raise_window_only_when_a_target_window_is_set(monkeypatch, fake):
    raised: list = []
    monkeypatch.setattr(desktop, "_raise_window", lambda hwnd: raised.append(hwnd) or False)
    perform(None, ToolRequest(kind="desktop", action="capture"), "bot")  # whole screen: target 0
    perform(None, ToolRequest(kind="desktop", action="click", command="5,5"), "bot")
    assert raised == [0]
    monkeypatch.setattr(desktop, "_window_rect", lambda hwnd: (0, 0, 800, 600))
    perform(None, ToolRequest(kind="desktop", action="capture", path="123"), "bot")
    perform(None, ToolRequest(kind="desktop", action="click", command="5,5"), "bot")
    assert raised == [0, 123]


def test_raise_window_text_only_when_it_actually_raised(monkeypatch, fake):
    monkeypatch.setattr(desktop, "_window_rect", lambda hwnd: (0, 0, 800, 600))
    perform(None, ToolRequest(kind="desktop", action="capture", path="123"), "bot")
    monkeypatch.setattr(desktop, "_raise_window", lambda hwnd: False)
    text, _shot = perform(None, ToolRequest(kind="desktop", action="click", command="5,5"), "bot")
    assert "Brought the window forward" not in text
    monkeypatch.setattr(desktop, "_raise_window", lambda hwnd: True)
    text, _shot = perform(None, ToolRequest(kind="desktop", action="click", command="6,6"), "bot")
    assert text.startswith("Brought the window forward first.")


def test_raise_window_restores_and_foregrounds(monkeypatch):
    win = FakeUser32(foreground=999)
    monkeypatch.setattr(desktop, "user32", win)
    assert REAL_RAISE_WINDOW(123) is True
    assert win.raised == [123]
    assert REAL_RAISE_WINDOW(999) is False  # already in front
    assert win.raised == [123]
    assert REAL_RAISE_WINDOW(0) is False  # no target


# ------------------------------------------------------------------ doctor

def test_doctor_report_rows_and_capture_failure(monkeypatch):
    monkeypatch.setattr(desktop, "user32", FakeUser32(foreground=0))
    monkeypatch.setattr(desktop_uia, "walk_window", lambda hwnd: [{"index": 1, "role": "Button", "label": "OK", "bounds": (1, 2, 3, 4)}])
    monkeypatch.setattr(desktop, "_grab", lambda bbox: Image.new("RGB", (64, 64)))
    report = doctor_report()
    names = [row["name"] for row in report["checks"]]
    assert names == ["session", "screen", "capture", "elements", "foreground"]
    assert report["ok"] is True
    assert all(row["status"] in {"pass", "fail", "skip"} for row in report["checks"])

    def boom(bbox):
        raise RuntimeError("no display")

    monkeypatch.setattr(desktop, "_grab", boom)
    report = doctor_report()
    assert report["ok"] is False
    capture = next(row for row in report["checks"] if row["name"] == "capture")
    assert capture["status"] == "fail" and "no display" in capture["message"]


def test_doctor_report_skips_elements_without_comtypes(monkeypatch):
    monkeypatch.setattr(desktop, "user32", FakeUser32(foreground=0))

    def no_comtypes(hwnd):
        raise ImportError("no comtypes")

    monkeypatch.setattr(desktop_uia, "walk_window", no_comtypes)
    report = doctor_report()
    row = next(r for r in report["checks"] if r["name"] == "elements")
    assert row["status"] == "skip"
    assert row["hint"] == "Install comtypes to read window controls."


def test_the_doctor_endpoint_answers(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from easyagent.app import create_app

    monkeypatch.setattr(desktop, "user32", FakeUser32(foreground=0))
    monkeypatch.setattr(desktop_uia, "walk_window", lambda hwnd: [])
    client = TestClient(create_app(tmp_path))
    response = client.get("/api/desktop/doctor")
    assert response.status_code == 200
    body = response.json()
    assert [row["name"] for row in body["checks"]] == ["session", "screen", "capture", "elements", "foreground"]


# ------------------------------------------------------------------ judge

def test_judge_asks_for_every_new_input_action():
    for action, extra in (
        ("scroll", {"command": "down|3|"}),
        ("drag", {"command": "element:1|element:2"}),
        ("set_value", {"command": "element:1", "body": "hi"}),
    ):
        verdict = judge_desktop(ToolRequest(kind="desktop", action=action, **extra))
        assert verdict.tier == "ask", action
        assert verdict.rule == "desktop-input", action


# ------------------------------------------------------------------ parser

def test_the_packer_builds_the_new_actions():
    call = _request_from_call({"name": "desktop", "arguments": {"action": "scroll", "direction": "down"}})
    assert (call.action, call.command) == ("scroll", "down|3|")
    call = _request_from_call(
        {"name": "desktop", "arguments": {"action": "scroll", "direction": "up", "amount": 2, "coordinate": "10,20"}}
    )
    assert call.command == "up|2|10,20"
    call = _request_from_call({"name": "desktop", "arguments": {"action": "scroll", "direction": "left", "element": 4}})
    assert call.command == "left|3|element:4"
    call = _request_from_call(
        {"name": "desktop", "arguments": {"action": "drag", "from_element": 1, "to_element": 2}}
    )
    assert call.command == "element:1|element:2"
    call = _request_from_call(
        {"name": "desktop", "arguments": {"action": "drag", "from_coordinate": "1,2", "to_coordinate": "3,4"}}
    )
    assert call.command == "1,2|3,4"
    call = _request_from_call({"name": "desktop", "arguments": {"action": "set_value", "element": 3, "value": "hi"}})
    assert (call.action, call.command, call.body) == ("set_value", "element:3", "hi")


def test_the_packer_clamps_scroll_amount_and_demands_its_fields():
    call = _request_from_call({"name": "desktop", "arguments": {"action": "scroll", "direction": "down", "amount": 0}})
    assert call.command == "down|1|"
    call = _request_from_call({"name": "desktop", "arguments": {"action": "scroll", "direction": "down", "amount": 99}})
    assert call.command == "down|50|"
    with pytest.raises(ToolError) as caught:
        _request_from_call({"name": "desktop", "arguments": {"action": "scroll"}})
    assert "direction" in str(caught.value)
    with pytest.raises(ToolError) as caught:
        _request_from_call({"name": "desktop", "arguments": {"action": "drag", "from_element": 1}})
    assert "A drag needs from/to elements or from/to coordinates." in str(caught.value)
    with pytest.raises(ToolError) as caught:
        _request_from_call({"name": "desktop", "arguments": {"action": "set_value", "value": "hi"}})
    assert "set_value needs an element number" in str(caught.value)
