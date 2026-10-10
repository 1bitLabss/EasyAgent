"""Tray icon for the unread count. The web UI title is the fallback when this cannot run."""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from typing import Any

from easyagent.store import Store
from easyagent.unread import unread_snapshot

# Windows notification icons have no taskbar-badge API. The number is painted
# onto the icon. pystray needs a logged-in desktop session and, on Windows, pywin32.
WINDOWS_TRAY_LIMIT = (
    "Windows has no taskbar badge for a notification-area icon. "
    "EasyAgent draws the unread number on the icon bitmap with Pillow. "
    "That needs pystray, Pillow, and, on Windows, pywin32, "
    "in a logged-in desktop session. "
    "A Windows service, an SSH session, or a scheduled task that runs while you are logged off "
    "has no notification area, so the icon cannot be created. "
    "The browser tab title still shows the count as (N) EasyAgent."
)

_SHOWN: dict[int, int] = {}


def title_for(count: int) -> str:
    if count <= 0:
        return "EasyAgent"
    return f"({count}) EasyAgent"


def draw_icon(count: int):
    """A 64px buddy. Zero is the face. A positive count paints the number on it."""
    from easyagent.mascot import tray_image

    return tray_image(count)


def apply_count(icon: Any, count: int) -> None:
    icon.icon = draw_icon(count)
    icon.title = title_for(count)


def sync_icon(icon: Any, store: Store) -> int:
    """Read the store and paint the icon when the count changed. Does not restart anything."""
    total = int(unread_snapshot(store)["total"])
    if _SHOWN.get(id(icon)) != total:
        apply_count(icon, total)
        _SHOWN[id(icon)] = total
    return total


def dock_warning(icon: Any) -> str | None:
    """Xorg only. Windows icons do not carry this attribute.

    pystray logs ``Failed to dock icon`` and ``assert self._systray_manager``
    when nothing owns the system tray selection.
    """
    if not hasattr(icon, "_systray_manager"):
        return None
    if icon._systray_manager:
        return None
    return (
        "Failed to dock icon: AssertionError: assert self._systray_manager. "
        "No system tray is running on this display."
    )


def _drop_pystray_modules() -> None:
    for name in list(sys.modules):
        if name == "pystray" or name.startswith("pystray."):
            del sys.modules[name]


def load_pystray():
    """Import pystray. On Linux, a GTK ValueError would hide the Xorg backend.

    ``import pystray`` tries AppIndicator first. Without the GTK 3 typelib that
    raises ``ValueError: Namespace Gtk not available``. pystray only continues
    to the next backend on ImportError, so EasyAgent retries with
    ``PYSTRAY_BACKEND=xorg``.
    """
    try:
        import pystray

        return pystray
    except Exception as first:
        if sys.platform == "win32" or os.environ.get("PYSTRAY_BACKEND"):
            raise first
        _drop_pystray_modules()
        os.environ["PYSTRAY_BACKEND"] = "xorg"
        try:
            import pystray

            return pystray
        except Exception:
            os.environ.pop("PYSTRAY_BACKEND", None)
            _drop_pystray_modules()
            raise first


def _on_windows() -> bool:
    """A Windows desktop has a notification area. Tests patch this, not sys.platform."""
    return sys.platform == "win32"


def _session_block() -> str | None:
    if os.environ.get("EASYAGENT_TRAY", "1").strip() == "0":
        return "EASYAGENT_TRAY=0"
    # A Windows desktop has a notification area and does not set DISPLAY.
    # Linux still needs a display. A headless Linux session, including pytest, skips the tray.
    if _on_windows():
        return None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    return "no DISPLAY or WAYLAND_DISPLAY, so there is no notification area"


def _report(reason: object) -> None:
    print(f"Tray icon is off: {reason}. {WINDOWS_TRAY_LIMIT}", flush=True)


def run_tray(store: Store, port: int, stop: threading.Event | None = None) -> None:
    """Blocking tray loop. Call it on a background thread. Failures are printed, not raised."""
    blocked = _session_block()
    if blocked:
        _report(blocked)
        return
    try:
        pystray = load_pystray()
    except Exception as exc:
        _report(exc)
        return
    try:
        image = draw_icon(0)
    except Exception as exc:
        _report(exc)
        return

    halt = stop or threading.Event()

    def open_app(_icon, _item) -> None:
        webbrowser.open(f"http://127.0.0.1:{port}")

    def watch(icon) -> None:
        icon.visible = True
        painted = False
        warned = False
        try:
            while not halt.is_set():
                # The first paint is allowed to try the tray. After that, a missing
                # systray owner is this display's limit, and repainting only repeats
                # "Failed to dock icon".
                warning = dock_warning(icon) if painted else None
                try:
                    if warning:
                        if not warned:
                            _report(warning)
                            warned = True
                        icon.title = title_for(int(unread_snapshot(store)["total"]))
                    else:
                        sync_icon(icon, store)
                except Exception as exc:
                    _report(exc)
                    return
                painted = True
                halt.wait(2.0)
        finally:
            try:
                icon.stop()
            except Exception:
                pass

    menu = pystray.Menu(pystray.MenuItem("Open EasyAgent", open_app))
    icon = pystray.Icon("easyagent", image, "EasyAgent", menu)
    try:
        icon.run(setup=watch)
    except Exception as exc:
        _report(exc)


def start_tray(store: Store, port: int, stop: threading.Event | None = None) -> threading.Thread | None:
    """Start the icon beside uvicorn. Returns None when the tray is skipped."""
    if _session_block():
        _report(_session_block())
        return None
    thread = threading.Thread(
        target=run_tray,
        args=(store, port, stop),
        name="easyagent-tray",
        daemon=True,
    )
    thread.start()
    return thread
