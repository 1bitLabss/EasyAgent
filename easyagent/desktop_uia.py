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


def _get_uia():
    """Create (once) the UIAutomation client. Imports comtypes lazily."""
    global _uia
    if _uia is None:
        import ctypes

        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass
        import comtypes.client

        module = comtypes.client.GetModule("UIAutomationCore.dll")
        _uia = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
    return _uia


def _clip_label(name: str) -> str:
    name = " ".join((name or "").split())
    if len(name) > _MAX_ELEMENT_LABEL_CHARS:
        return name[: _MAX_ELEMENT_LABEL_CHARS - 3] + "..."
    return name


def walk_window(hwnd: int) -> list[dict]:
    """Interactable elements of a window, in tree order.

    Each element: {index (1-based), role, label, bounds (x, y, w, h)}; bounds
    (0, 0, 0, 0) means geometry unknown. If the walk hits the surface cap, the
    last item is a spill marker: {role: 'more', count: N}.
    """
    uia = _get_uia()
    root = uia.ElementFromHandle(hwnd) if hwnd else uia.GetRootElement()
    walker = uia.ControlViewWalker
    elements: list[dict] = []
    spill = 0
    visited = 0
    stack = [root]
    while stack and visited < _MAX_NODES_VISITED:
        element = stack.pop()
        visited += 1
        try:
            control_type = element.CurrentControlType
        except Exception:
            control_type = None
        if control_type in _CONTROL_TYPE_NAMES:
            bounds = (0, 0, 0, 0)
            try:
                rect = element.CurrentBoundingRectangle
                left, top = int(rect.left), int(rect.top)
                width, height = int(rect.right) - left, int(rect.bottom) - top
                if width > 0 and height > 0:
                    if int(rect.right) <= 0 or int(rect.bottom) <= 0:
                        bounds = None  # fully off-screen: skip it
                    else:
                        bounds = (left, top, width, height)
                # else: empty rect stays (0, 0, 0, 0) — geometry unknown, still clickable by index
            except Exception:
                bounds = (0, 0, 0, 0)
            if bounds is not None:
                if len(elements) < _DEFAULT_MAX_ELEMENTS:
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
                else:
                    spill += 1
        try:
            sibling = walker.GetNextSiblingElement(element)
            child = walker.GetFirstChildElement(element)
        except Exception:
            sibling = child = None
        if sibling:
            stack.append(sibling)
        if child:
            stack.append(child)
    if spill:
        elements.append({"role": "more", "count": spill})
    return elements
