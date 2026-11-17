"""Wave 2 of the desktop fence tool: read a window's controls through Windows UIA.

comtypes is imported lazily inside functions — this module must import cleanly
without it (CI runs macOS, and the library is only installed on Windows). The
tree walk is budgeted: at most 200 nodes visited, at most 100 elements surfaced.
"""

from __future__ import annotations

_MAX_ELEMENT_LABEL_CHARS = 120
_DEFAULT_MAX_ELEMENTS = 100
_MAX_NODES_VISITED = 200

# UIA control type ids for the controls a bot can actually use.
_CONTROL_TYPE_NAMES = {
    50000: "Button",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50007: "ListItem",
    50010: "MenuBar",
    50011: "MenuItem",
    50013: "RadioButton",
    50015: "Slider",
    50019: "TabItem",
    50024: "TreeItem",
    50030: "Document",
}

_uia = None  # cached CUIAutomation instance; COM is apartment-threaded
_uia_module = None  # the generated UIAutomationCore module, for pattern casts
_VALUE_PATTERN_ID = 10002  # UIA_ValuePatternId


def _get_uia():
    """Create (once) the UIAutomation client. Imports comtypes lazily."""
    global _uia, _uia_module
    if _uia is None:
        import ctypes

        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass
        import comtypes.client

        module = comtypes.client.GetModule("UIAutomationCore.dll")
        _uia = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
        _uia_module = module
    return _uia


def _clip_label(name: str) -> str:
    name = " ".join((name or "").split())
    if len(name) > _MAX_ELEMENT_LABEL_CHARS:
        return name[: _MAX_ELEMENT_LABEL_CHARS - 3] + "..."
    return name


def _surface_bounds(element):
    """walk_window's surface rule for one element's geometry:

    (x, y, w, h) on-screen, (0, 0, 0, 0) when geometry is unknown, None to skip."""
    try:
        rect = element.CurrentBoundingRectangle
        left, top = int(rect.left), int(rect.top)
        width, height = int(rect.right) - left, int(rect.bottom) - top
        if width > 0 and height > 0:
            if int(rect.right) <= 0 or int(rect.bottom) <= 0:
                return None  # fully off-screen: skip it
            return (left, top, width, height)
    except Exception:
        pass
    return (0, 0, 0, 0)  # empty or unreadable rect: geometry unknown, still clickable by index


def _walk_nodes(hwnd: int):
    """Shared tree walk: yield (element, control_type) in tree order, budgeted."""
    uia = _get_uia()
    root = uia.ElementFromHandle(hwnd) if hwnd else uia.GetRootElement()
    walker = uia.ControlViewWalker
    visited = 0
    stack = [root]
    while stack and visited < _MAX_NODES_VISITED:
        element = stack.pop()
        visited += 1
        try:
            control_type = element.CurrentControlType
        except Exception:
            control_type = None
        yield element, control_type
        try:
            sibling = walker.GetNextSiblingElement(element)
            child = walker.GetFirstChildElement(element)
        except Exception:
            sibling = child = None
        if sibling:
            stack.append(sibling)
        if child:
            stack.append(child)


def walk_window(hwnd: int) -> list[dict]:
    """Interactable elements of a window, in tree order.

    Each element: {index (1-based), role, label, bounds (x, y, w, h)}; bounds
    (0, 0, 0, 0) means geometry unknown. If the walk hits the surface cap, the
    last item is a spill marker: {role: 'more', count: N}.
    """
    elements: list[dict] = []
    spill = 0
    for element, control_type in _walk_nodes(hwnd):
        if control_type not in _CONTROL_TYPE_NAMES:
            continue
        bounds = _surface_bounds(element)
        if bounds is None:
            continue
        if len(elements) >= _DEFAULT_MAX_ELEMENTS:
            spill += 1
            continue
        try:
            label = _clip_label(element.CurrentName)
        except Exception:
            label = ""
        elements.append(
            {
                "index": len(elements) + 1,
                "role": _CONTROL_TYPE_NAMES[control_type],
                "label": label,
                "bounds": bounds,
            }
        )
    if spill:
        elements.append({"role": "more", "count": spill})
    return elements


def set_value_by_index(hwnd: int, wanted_index: int, value: str) -> None:
    """Type a value into the element with that walk_window index, via UIA
    ValuePattern — no mouse involved.

    Re-walks the window so the index matches the last element list. Raises
    ValueError with a plain message when the element is gone or cannot be
    typed into; an ImportError from comtypes propagates to the caller."""
    index = 0
    for element, control_type in _walk_nodes(hwnd):
        if control_type not in _CONTROL_TYPE_NAMES:
            continue
        if _surface_bounds(element) is None:
            continue
        index += 1
        if index != wanted_index:
            continue
        try:
            pattern = element.GetCurrentPattern(_VALUE_PATTERN_ID)
        except Exception:
            pattern = None
        if pattern is None:
            raise ValueError(f"Element {wanted_index} can't be typed into.")
        if _uia_module is not None:
            try:
                pattern = pattern.QueryInterface(_uia_module.IUIAutomationValuePattern)
            except Exception:
                raise ValueError(f"Element {wanted_index} can't be typed into.")
        try:
            if pattern.CurrentIsReadOnly:
                raise ValueError(f"Element {wanted_index} can't be typed into.")
            pattern.SetValue(value)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"The value could not be set on element {wanted_index}: {exc}") from exc
        return
    raise ValueError(f"Element {wanted_index} is not in this window right now — run desktop elements again.")
