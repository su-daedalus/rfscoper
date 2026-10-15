"""Import whichever Qt binding is installed: PyQt6 (pip) or PyQt5 (Debian/Raspberry Pi OS).

The scope window is written against this module instead of a binding, so the
same code runs with `pip install rfscoper[app]` (PyQt6, which ships prebuilt
wheels for the Pi's aarch64) and with the `python3-pyqt5` that DragonOS and
Raspberry Pi OS already carry for GNU Radio.

Only the handful of enums the window uses are wrapped. Everything else is the
plain binding API, which is the same in both.
"""

from __future__ import annotations

from typing import Any

BINDING = ""
try:
    from PyQt6 import QtCore, QtGui, QtWidgets

    BINDING = "PyQt6"
except ImportError:  # pragma: no cover - depends on what is installed
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets  # type: ignore[no-redef,import-not-found]

        BINDING = "PyQt5"
    except ImportError:
        QtCore = QtGui = QtWidgets = None  # type: ignore[assignment]

AVAILABLE = bool(BINDING)

INSTALL_HELP = """The scope window needs Qt, which is not installed in this Python environment.

On a Raspberry Pi (or any Debian-based system) either:

  1. Use the system's Qt, which GNU Radio already brought in:
       sudo apt install python3-pyqt5
       deactivate; rm -rf .venv
       python3 -m venv --system-site-packages .venv
       source .venv/bin/activate && pip install -e ".[dev]"

  2. Or install Qt into this environment from prebuilt wheels (about 100 MB):
       pip install -e ".[app]"

Then run `rfscoper scope` again.
"""


def _enum(owner: Any, group: str, member: str) -> Any:
    """`owner.group.member` on PyQt6, `owner.member` on PyQt5."""
    scoped = getattr(owner, group, None)
    if scoped is not None and hasattr(scoped, member):
        return getattr(scoped, member)
    return getattr(owner, member)


def align(*names: str) -> Any:
    flag = _enum(QtCore.Qt, "AlignmentFlag", names[0])
    for name in names[1:]:
        flag = flag | _enum(QtCore.Qt, "AlignmentFlag", name)
    return flag


def pen_style(name: str) -> Any:
    return _enum(QtCore.Qt, "PenStyle", name)


def mouse_button(name: str) -> Any:
    return _enum(QtCore.Qt, "MouseButton", name)


def key(name: str) -> Any:
    return _enum(QtCore.Qt, "Key", name)


def color_role(name: str) -> Any:
    return _enum(QtGui.QPalette, "ColorRole", name)


def image_format(name: str) -> Any:
    return _enum(QtGui.QImage, "Format", name)


def orientation(name: str) -> Any:
    return _enum(QtCore.Qt, "Orientation", name)


def item_flag(name: str) -> Any:
    return _enum(QtCore.Qt, "ItemFlag", name)


def selection_behavior(name: str) -> Any:
    return _enum(QtWidgets.QAbstractItemView, "SelectionBehavior", name)


def edit_trigger(name: str) -> Any:
    return _enum(QtWidgets.QAbstractItemView, "EditTrigger", name)


def resize_mode(name: str) -> Any:
    return _enum(QtWidgets.QHeaderView, "ResizeMode", name)


def message_icon(name: str) -> Any:
    return _enum(QtWidgets.QMessageBox, "Icon", name)


def exec_(obj: Any) -> Any:
    """`exec()` on PyQt6; `exec_()` on older PyQt5."""
    run = getattr(obj, "exec", None) or obj.exec_
    return run()


def mouse_pos(event: Any) -> tuple[float, float]:
    """Cursor position from a mouse event, in both bindings."""
    p = event.position() if hasattr(event, "position") else event.pos()
    return float(p.x()), float(p.y())


def antialiasing() -> Any:
    return _enum(QtGui.QPainter, "RenderHint", "Antialiasing")
