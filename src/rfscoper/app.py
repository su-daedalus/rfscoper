"""rfscoper scope: the live desktop window.

A native Qt window, fully offline: it runs hackrf_sweep (or the built-in demo
source) through rfscoper.live and draws

    spectrum     live trace, peak hold, average, noise floor, near-field reference
    waterfall    time-frequency history, newest sweep at the top
    meter        Geiger-style gauge and clicks for the watched frequency
    signals      detected signals with their best identification

Click the spectrum or waterfall to watch a frequency; scroll to zoom in or out
(the radio retunes). Near-field mode measures everything against a reference
taken with the probe in open air, the same method as `rfscoper probe`.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import numpy as np

from rfscoper import capture, clicks, live, qt
from rfscoper.qt import QtCore, QtGui, QtWidgets

# ------------------------------------------------------------------ theme

BG = "#0d1117"
PANEL = "#161b22"
GRID = "#263040"
TEXT = "#c9d1d9"
MUTED = "#7d8590"
LIVE = "#3fb6ff"
PEAK = "#ffb347"
AVG = "#7ee787"
FLOOR = "#8b949e"
REF = "#d2a8ff"
WATCH = "#ff5c8a"
HOT = "#ff4d4f"
WARM = "#f5a623"
COOL = "#3fb950"

DB_MIN, DB_MAX = -80.0, -10.0  # default spectrum scale

PRESETS: dict[str, tuple[int, int, int]] = {
    # name: (start MHz, stop MHz, bin Hz)
    "Full 1-6000 MHz": (1, 6000, 1_000_000),
    "FM broadcast": (88, 108, 100_000),
    "Airband + 2 m": (108, 148, 200_000),
    "UHF 300-480 (315/433/FRS)": (300, 480, 200_000),
    "915 MHz ISM": (900, 940, 100_000),
    "Cellular 600-900": (600, 900, 500_000),
    "2.4 GHz (Wi-Fi/BLE)": (2400, 2500, 200_000),
    "5.8 GHz video": (5640, 5960, 1_000_000),
    "Near-field 300-2500": (300, 2500, 200_000),
}


def _colormap() -> np.ndarray:
    """256 RGB32 colors: black -> blue -> cyan -> yellow -> red -> white."""
    stops = [
        (0.00, (0, 0, 0)),
        (0.20, (10, 20, 90)),
        (0.40, (0, 120, 200)),
        (0.60, (40, 220, 160)),
        (0.75, (250, 230, 40)),
        (0.90, (255, 70, 30)),
        (1.00, (255, 255, 255)),
    ]
    xs = np.linspace(0, 1, 256)
    rgb = np.zeros((256, 3))
    for c in range(3):
        rgb[:, c] = np.interp(xs, [s[0] for s in stops], [s[1][c] for s in stops])
    channels = rgb.astype(np.uint32)
    packed = 0xFF000000 | (channels[:, 0] << 16) | (channels[:, 1] << 8) | channels[:, 2]
    return packed.astype(np.uint32)


COLORMAP = _colormap()


def fmt_hz(hz: float | None) -> str:
    if hz is None or (isinstance(hz, float) and math.isnan(hz)):
        return "-"
    if hz >= 1e9:
        return f"{hz / 1e9:.4f} GHz"
    return f"{hz / 1e6:.3f} MHz"


def pen(color: str, width: float = 1.0, dashed: bool = False) -> Any:
    p = QtGui.QPen(QtGui.QColor(color))
    p.setWidthF(width)
    if dashed:
        p.setStyle(qt.pen_style("DashLine"))
    return p


# ------------------------------------------------------------- spectrum


class SpectrumView(QtWidgets.QWidget):
    """Frequency vs power. Click: watch. Wheel: zoom (retunes the radio)."""

    watch_clicked = QtCore.pyqtSignal(float)
    zoom_requested = QtCore.pyqtSignal(float, float)  # center Hz, factor

    LEFT, RIGHT, TOP, BOTTOM = 48, 12, 10, 24

    def __init__(self) -> None:
        super().__init__()
        self.frame: dict[str, Any] | None = None
        self.visible = {"live": True, "peak": True, "avg": True, "floor": True, "ref": True}
        self.db_min, self.db_max = DB_MIN, DB_MAX
        self.auto_scale = True
        self.setMinimumHeight(220)
        self.setMouseTracking(True)
        self.hover: tuple[float, float] | None = None

    # geometry
    def plot_rect(self) -> Any:
        return QtCore.QRectF(
            self.LEFT,
            self.TOP,
            max(10, self.width() - self.LEFT - self.RIGHT),
            max(10, self.height() - self.TOP - self.BOTTOM),
        )

    def hz_at(self, x: float) -> float | None:
        f = self.frame
        if f is None:
            return None
        r = self.plot_rect()
        g = f["grid"]
        frac = min(max((x - r.left()) / r.width(), 0.0), 1.0)
        return g["start_hz"] + frac * (g["stop_hz"] - g["start_hz"])

    def set_frame(self, frame: dict[str, Any]) -> None:
        self.frame = frame
        if self.auto_scale:
            floor = frame.get("floor")
            peak = frame.get("peak")
            if floor is not None and peak is not None and not np.all(np.isnan(peak)):
                lo = float(np.nanpercentile(floor, 10)) - 10
                hi = float(np.nanmax(peak)) + 6
                # move the scale slowly so it doesn't jitter
                self.db_min += 0.2 * (lo - self.db_min)
                self.db_max += 0.2 * (max(hi, lo + 30) - self.db_max)
        self.update()

    # drawing
    def paintEvent(self, _event: Any) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(qt.antialiasing(), True)
        p.fillRect(self.rect(), QtGui.QColor(BG))
        r = self.plot_rect()
        f = self.frame
        p.setFont(QtGui.QFont("monospace", 8))
        # grid + dB labels
        span = self.db_max - self.db_min
        step = 10 if span > 40 else 5
        db = math.ceil(self.db_min / step) * step
        while db <= self.db_max:
            y = self._y(db, r)
            p.setPen(pen(GRID))
            p.drawLine(QtCore.QPointF(r.left(), y), QtCore.QPointF(r.right(), y))
            p.setPen(pen(MUTED))
            p.drawText(
                QtCore.QRectF(0, y - 7, self.LEFT - 4, 14),
                qt.align("AlignRight", "AlignVCenter"),
                f"{db:.0f}",
            )
            db += step
        if f is None:
            p.setPen(pen(MUTED))
            p.drawText(r, qt.align("AlignCenter"), "Waiting for the first sweep...")
            p.end()
            return
        g = f["grid"]
        start, stop = g["start_hz"], g["stop_hz"]
        # frequency labels
        for i in range(7):
            hz = start + (stop - start) * i / 6
            x = r.left() + r.width() * i / 6
            p.setPen(pen(GRID))
            p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
            p.setPen(pen(MUTED))
            align = "AlignLeft" if i == 0 else ("AlignRight" if i == 6 else "AlignHCenter")
            left = x if i == 0 else (x - 120 if i == 6 else x - 60)
            p.drawText(
                QtCore.QRectF(left, r.bottom() + 4, 120, 16),
                qt.align(align, "AlignTop"),
                f"{hz / 1e6:.2f}",
            )
        # traces
        order = [
            ("floor", FLOOR, 1.0, True),
            ("ref", REF, 1.2, True),
            ("avg", AVG, 1.0, False),
            ("peak", PEAK, 1.0, False),
            ("live", LIVE, 1.4, False),
        ]
        for key, color, width, dashed in order:
            values = f.get(key)
            if values is None or not self.visible.get(key, True):
                continue
            self._trace(p, values, r, color, width, dashed)
        # signal markers
        p.setFont(QtGui.QFont("sans", 8))
        for s in f["signals"][:12]:
            x = r.left() + (s["hz"] - start) / (stop - start) * r.width()
            y = self._y(s["db"], r)
            color = HOT if (s.get("gain_db") or 0) >= 20 else TEXT
            p.setPen(pen(color))
            p.drawLine(QtCore.QPointF(x, y - 3), QtCore.QPointF(x, y - 10))
            label = s["short"] or f"{s['hz'] / 1e6:.2f}"
            p.drawText(QtCore.QRectF(x - 40, y - 24, 80, 13), qt.align("AlignCenter"), label)
        # watch marker
        watch = f["status"].get("watch_hz")
        if watch is not None and start <= watch <= stop:
            x = r.left() + (watch - start) / (stop - start) * r.width()
            p.setPen(pen(WATCH, 1.5, dashed=True))
            p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
        # hover readout
        if self.hover is not None:
            hx, hy = self.hover
            hz = self.hz_at(hx)
            if hz is not None and r.contains(QtCore.QPointF(hx, hy)):
                p.setPen(pen(MUTED, 1, dashed=True))
                p.drawLine(QtCore.QPointF(hx, r.top()), QtCore.QPointF(hx, r.bottom()))
                live_vals = f.get("live")
                db_text = ""
                if live_vals is not None:
                    i = int((hz - start) / g["step_hz"])
                    if 0 <= i < len(live_vals) and not math.isnan(live_vals[i]):
                        db_text = f"  {live_vals[i]:.1f} dB"
                p.setPen(pen(TEXT))
                p.drawText(
                    QtCore.QRectF(r.left() + 6, r.top() + 2, 260, 14),
                    qt.align("AlignLeft", "AlignTop"),
                    fmt_hz(hz) + db_text,
                )
        # legend
        x = r.right() - 300
        for name, color in (
            ("live", LIVE),
            ("peak", PEAK),
            ("avg", AVG),
            ("floor", FLOOR),
            ("ref", REF),
        ):
            if not self.visible.get(name, True) or (name == "ref" and f.get("ref") is None):
                continue
            p.setPen(pen(color, 2))
            p.drawLine(QtCore.QPointF(x, r.top() + 9), QtCore.QPointF(x + 14, r.top() + 9))
            p.setPen(pen(TEXT))
            p.drawText(QtCore.QPointF(x + 18, r.top() + 13), name)
            x += 58
        p.end()

    def _y(self, db: float, r: Any) -> float:
        frac = (db - self.db_min) / max(self.db_max - self.db_min, 1e-6)
        return r.bottom() - min(max(frac, 0.0), 1.0) * r.height()

    def _trace(
        self, p: Any, values: np.ndarray, r: Any, color: str, width: float, dashed: bool
    ) -> None:
        n = len(values)
        if n < 2:
            return
        xs = r.left() + (np.arange(n) + 0.5) / n * r.width()
        frac = (values - self.db_min) / max(self.db_max - self.db_min, 1e-6)
        ys = r.bottom() - np.clip(frac, 0, 1) * r.height()
        path = QtGui.QPainterPath()
        started = False
        for x, y, v in zip(xs, ys, values, strict=True):
            if math.isnan(v):
                started = False
                continue
            if not started:
                path.moveTo(float(x), float(y))
                started = True
            else:
                path.lineTo(float(x), float(y))
        p.setPen(pen(color, width, dashed))
        p.drawPath(path)

    # interaction
    def mousePressEvent(self, event: Any) -> None:
        x, _ = qt.mouse_pos(event)
        hz = self.hz_at(x)
        if hz is not None and event.button() == qt.mouse_button("LeftButton"):
            self.watch_clicked.emit(hz)

    def mouseMoveEvent(self, event: Any) -> None:
        self.hover = qt.mouse_pos(event)
        self.update()

    def leaveEvent(self, _event: Any) -> None:
        self.hover = None
        self.update()

    def wheelEvent(self, event: Any) -> None:
        x = event.position().x() if hasattr(event, "position") else event.pos().x()
        hz = self.hz_at(x)
        if hz is None:
            return
        factor = 0.5 if event.angleDelta().y() > 0 else 2.0
        self.zoom_requested.emit(hz, factor)


# ------------------------------------------------------------ waterfall


class WaterfallView(QtWidgets.QWidget):
    """Scrolling time-frequency image, newest sweep at the top."""

    watch_clicked = QtCore.pyqtSignal(float)

    def __init__(self, rows: int = 300) -> None:
        super().__init__()
        self.rows = rows
        self.image: np.ndarray | None = None
        self.last_sweep = -1
        self.grid: dict[str, Any] | None = None
        self.watch_hz: float | None = None
        self.db_min, self.db_max = DB_MIN, DB_MAX
        self.setMinimumHeight(160)

    def reset(self) -> None:
        self.image = None
        self.last_sweep = -1

    def set_frame(self, frame: dict[str, Any], db_min: float, db_max: float) -> None:
        self.db_min, self.db_max = db_min, db_max
        self.watch_hz = frame["status"].get("watch_hz")
        line = frame.get("waterfall")
        if line is None or frame["sweep"] == self.last_sweep:
            self.update()
            return
        n = len(line)
        if self.image is None or self.image.shape[1] != n or self.grid != frame["grid"]:
            self.image = np.zeros((self.rows, n), dtype=np.uint32)
            self.image[:] = COLORMAP[0]
        self.grid = frame["grid"]
        self.last_sweep = frame["sweep"]
        frac = (np.nan_to_num(line, nan=self.db_min) - self.db_min) / max(
            self.db_max - self.db_min, 1e-6
        )
        idx = (np.clip(frac, 0, 1) * 255).astype(np.int32)
        self.image = np.roll(self.image, 1, axis=0)
        self.image[0] = COLORMAP[idx]
        self.update()

    def paintEvent(self, _event: Any) -> None:
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(BG))
        r = QtCore.QRectF(
            SpectrumView.LEFT,
            0,
            max(10, self.width() - SpectrumView.LEFT - SpectrumView.RIGHT),
            self.height(),
        )
        if self.image is not None:
            data = np.ascontiguousarray(self.image)
            h, w = data.shape
            img = QtGui.QImage(data.tobytes(), w, h, w * 4, qt.image_format("Format_RGB32"))
            p.drawImage(r, img)
            if self.watch_hz is not None and self.grid is not None:
                start, stop = self.grid["start_hz"], self.grid["stop_hz"]
                if start <= self.watch_hz <= stop:
                    x = r.left() + (self.watch_hz - start) / (stop - start) * r.width()
                    p.setPen(pen(WATCH, 1, dashed=True))
                    p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
        else:
            p.setPen(pen(MUTED))
            p.drawText(r, qt.align("AlignCenter"), "Waterfall")
        p.end()

    def mousePressEvent(self, event: Any) -> None:
        if self.grid is None:
            return
        x, _ = qt.mouse_pos(event)
        left = SpectrumView.LEFT
        width = max(10, self.width() - SpectrumView.LEFT - SpectrumView.RIGHT)
        frac = min(max((x - left) / width, 0.0), 1.0)
        self.watch_clicked.emit(
            self.grid["start_hz"] + frac * (self.grid["stop_hz"] - self.grid["start_hz"])
        )


# ---------------------------------------------------------------- meter


class MeterView(QtWidgets.QWidget):
    """Geiger-style gauge: dB above the baseline, -10 to +50."""

    LO, HI = -10.0, 50.0

    def __init__(self) -> None:
        super().__init__()
        self.meter: dict[str, Any] = {}
        self.needle = self.LO
        self.history: list[float] = []
        self.setMinimumSize(280, 290)

    def set_meter(self, meter: dict[str, Any]) -> None:
        self.meter = meter
        above = meter.get("above_db")
        target = self.LO if above is None else float(above)
        self.needle += 0.45 * (target - self.needle)  # needle inertia
        self.history.append(target)
        self.history = self.history[-120:]
        self.update()

    def paintEvent(self, _event: Any) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(qt.antialiasing(), True)
        p.fillRect(self.rect(), QtGui.QColor(PANEL))
        w, h = self.width(), self.height()
        spark_h = 46  # the history sparkline sits above the gauge
        radius = max(40.0, min(w * 0.42, h - spark_h - 96))
        cx, cy = w / 2, spark_h + 8 + radius
        # colored arc bands: background / warm / HOT
        bands = [(self.LO, 10, COOL), (10, 20, WARM), (20, self.HI, HOT)]
        for lo, hi, color in bands:
            a0 = self._angle(lo)
            a1 = self._angle(hi)
            pth = QtGui.QPen(QtGui.QColor(color))
            pth.setWidthF(10)
            p.setPen(pth)
            rect = QtCore.QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
            p.drawArc(rect, int(a0 * 16), int((a1 - a0) * 16))
        # ticks
        p.setFont(QtGui.QFont("sans", 8))
        for v in range(int(self.LO), int(self.HI) + 1, 10):
            a = math.radians(self._angle(v))
            x0, y0 = cx + (radius - 14) * math.cos(a), cy - (radius - 14) * math.sin(a)
            x1, y1 = cx + (radius - 24) * math.cos(a), cy - (radius - 24) * math.sin(a)
            p.setPen(pen(TEXT))
            p.drawLine(QtCore.QPointF(x0, y0), QtCore.QPointF(x1, y1))
            tx, ty = cx + (radius - 38) * math.cos(a), cy - (radius - 38) * math.sin(a)
            p.drawText(QtCore.QRectF(tx - 16, ty - 8, 32, 16), qt.align("AlignCenter"), f"{v:+d}")
        # needle
        a = math.radians(self._angle(min(max(self.needle, self.LO), self.HI)))
        p.setPen(pen(WATCH, 3))
        p.drawLine(
            QtCore.QPointF(cx, cy),
            QtCore.QPointF(cx + (radius - 8) * math.cos(a), cy - (radius - 8) * math.sin(a)),
        )
        p.setBrush(QtGui.QColor(WATCH))
        p.drawEllipse(QtCore.QPointF(cx, cy), 5, 5)
        # readouts
        m = self.meter
        above = m.get("above_db")
        verdict = m.get("verdict", "no signal")
        color = HOT if verdict == "HOT" else (WARM if verdict == "warm" else COOL)
        p.setPen(pen(color))
        p.setFont(QtGui.QFont("sans", 20, 700))
        p.drawText(
            QtCore.QRectF(0, cy + 6, w, 30),
            qt.align("AlignCenter"),
            "-- dB" if above is None else f"{above:+.1f} dB",
        )
        p.setFont(QtGui.QFont("sans", 10, 700))
        p.drawText(QtCore.QRectF(0, cy + 36, w, 18), qt.align("AlignCenter"), verdict.upper())
        p.setPen(pen(MUTED))
        p.setFont(QtGui.QFont("sans", 8))
        basis = m.get("basis", "none")
        best = m.get("best_above_db")
        lines = [
            f"{fmt_hz(m.get('hz'))}   vs {basis}",
            f"{m.get('rate', 0):.1f} clicks/s"
            + ("" if best is None else f"   best {best:+.1f} dB"),
        ]
        for row, text in enumerate(lines):
            p.drawText(QtCore.QRectF(0, cy + 56 + row * 14, w, 14), qt.align("AlignCenter"), text)
        # sparkline of the last ~15 s
        if len(self.history) > 1:
            path = QtGui.QPainterPath()
            top = 8.0
            sh = 34
            for i, level in enumerate(self.history):
                x = 10 + i / 119 * (w - 20.0)
                y = (
                    top
                    + sh
                    - (min(max(level, self.LO), self.HI) - self.LO) / (self.HI - self.LO) * sh
                )
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            p.setPen(pen(LIVE, 1.2))
            p.setBrush(QtGui.QBrush())
            p.drawPath(path)
        p.end()

    def _angle(self, value: float) -> float:
        """Degrees: LO at 210 (lower left), HI at -30 (lower right)."""
        frac = (value - self.LO) / (self.HI - self.LO)
        return 210 - 240 * frac


# --------------------------------------------------------------- window


class ScopeWindow(QtWidgets.QMainWindow):
    def __init__(
        self, scope: live.Scope, demo_source: live.DemoSource | None = None, audio: bool = True
    ) -> None:
        super().__init__()
        self.scope = scope
        self.demo = demo_source
        self.audio = clicks.ClickStream()
        self.last_frame_number = -1
        self.last_status_note = ""
        self.setWindowTitle("rfscoper scope")
        self.resize(1400, 860)
        self._style()

        self.spectrum = SpectrumView()
        self.waterfall = WaterfallView()
        self.meter = MeterView()
        self.table = self._make_table()

        split = QtWidgets.QSplitter(qt.orientation("Vertical"))
        split.addWidget(self.spectrum)
        split.addWidget(self.waterfall)
        split.setSizes([480, 300])

        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(6, 6, 6, 6)
        title = QtWidgets.QLabel("GEIGER METER")
        title.setObjectName("section")
        rl.addWidget(title)
        rl.addWidget(self.meter, 3)
        rl.addWidget(self._near_box())
        sig = QtWidgets.QLabel("SIGNALS")
        sig.setObjectName("section")
        rl.addWidget(sig)
        rl.addWidget(self.table, 4)

        main = QtWidgets.QSplitter(qt.orientation("Horizontal"))
        main.addWidget(split)
        main.addWidget(right)
        main.setSizes([1000, 400])

        central = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self._toolbar())
        cl.addWidget(main, 1)
        self.setCentralWidget(central)
        self.status = self.statusBar()

        self.spectrum.watch_clicked.connect(self.watch)
        self.waterfall.watch_clicked.connect(self.watch)
        self.spectrum.zoom_requested.connect(self.zoom)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(int(1000 / live.FRAME_HZ))
        if audio and self.audio.available:
            self.sound_box.setChecked(False)

    # ---------------------------------------------------------- layout

    def _style(self) -> None:
        self.setStyleSheet(
            f"""
            QWidget {{ background: {PANEL}; color: {TEXT}; font-size: 12px; }}
            QMainWindow, QSplitter {{ background: {BG}; }}
            QLabel#section {{ color: {MUTED}; font-weight: bold; letter-spacing: 1px; padding: 4px 2px 0 2px; }}
            QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
                background: #21262d; border: 1px solid #30363d; border-radius: 4px; padding: 3px 8px; }}
            QPushButton:hover {{ border-color: {LIVE}; }}
            QPushButton:checked {{ background: #1f6feb; border-color: #388bfd; }}
            QPushButton#danger:checked {{ background: #b62324; border-color: {HOT}; }}
            QTableWidget {{ background: {BG}; gridline-color: {GRID}; }}
            QHeaderView::section {{ background: #21262d; color: {MUTED}; border: none; padding: 3px; }}
            QStatusBar {{ background: {BG}; color: {MUTED}; }}
            QGroupBox {{ border: 1px solid #30363d; border-radius: 6px; margin-top: 10px; padding: 6px; }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 8px; color: {MUTED}; }}
            """
        )

    def _toolbar(self) -> Any:
        bar = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(8, 6, 8, 6)
        s = self.scope.state.settings

        self.preset = QtWidgets.QComboBox()
        self.preset.addItem("Presets...")
        self.preset.addItems(list(PRESETS))
        self.preset.activated.connect(self.apply_preset)
        lay.addWidget(self.preset)

        def spin(lo: int, hi: int, value: int, suffix: str, step: int = 1) -> Any:
            box = QtWidgets.QSpinBox()
            box.setRange(lo, hi)
            box.setValue(value)
            box.setSuffix(suffix)
            box.setSingleStep(step)
            return box

        self.start_box = spin(0, capture.FREQ_MAX_MHZ - 20, s.start_mhz, " MHz")
        self.stop_box = spin(20, capture.FREQ_MAX_MHZ, s.stop_mhz, " MHz")
        self.bin_box = QtWidgets.QComboBox()
        for label, hz in (
            ("40 kHz", 40_000),
            ("100 kHz", 100_000),
            ("200 kHz", 200_000),
            ("500 kHz", 500_000),
            ("1 MHz", 1_000_000),
            ("2 MHz", 2_000_000),
        ):
            self.bin_box.addItem(label, hz)
        self._select_bin(s.bin_hz)
        self.lna_box = spin(0, 40, s.lna_db, " dB LNA", 8)
        self.vga_box = spin(0, 62, s.vga_db, " dB VGA", 2)
        for label, widget in (
            ("from", self.start_box),
            ("to", self.stop_box),
            ("bins", self.bin_box),
        ):
            lay.addWidget(QtWidgets.QLabel(label))
            lay.addWidget(widget)
        lay.addWidget(self.lna_box)
        lay.addWidget(self.vga_box)
        tune = QtWidgets.QPushButton("Tune")
        tune.clicked.connect(self.apply_tuning)
        lay.addWidget(tune)
        lay.addSpacing(12)

        self.mode_far = QtWidgets.QPushButton("Far field")
        self.mode_near = QtWidgets.QPushButton("Near field")
        for b in (self.mode_far, self.mode_near):
            b.setCheckable(True)
            lay.addWidget(b)
        self.mode_far.setChecked(self.scope.state.mode == "far")
        self.mode_near.setChecked(self.scope.state.mode == "near")
        self.mode_far.clicked.connect(lambda: self.set_mode("far"))
        self.mode_near.clicked.connect(lambda: self.set_mode("near"))
        lay.addSpacing(12)

        for name in ("live", "peak", "avg", "floor", "ref"):
            box = QtWidgets.QCheckBox(name)
            box.setChecked(True)
            box.toggled.connect(lambda on, n=name: self._toggle_trace(n, on))
            lay.addWidget(box)
        reset = QtWidgets.QPushButton("Reset peak")
        reset.clicked.connect(self.scope_state_reset_peak)
        lay.addWidget(reset)
        lay.addStretch(1)

        self.record_button = QtWidgets.QPushButton("● Record 30 s")
        self.record_button.setObjectName("danger")
        self.record_button.setCheckable(True)
        self.record_button.clicked.connect(self.toggle_record)
        lay.addWidget(self.record_button)
        export = QtWidgets.QPushButton("Export")
        export.clicked.connect(self.export)
        lay.addWidget(export)
        shot = QtWidgets.QPushButton("Screenshot")
        shot.clicked.connect(self.screenshot)
        lay.addWidget(shot)
        return bar

    def _near_box(self) -> Any:
        box = QtWidgets.QGroupBox("Near field")
        lay = QtWidgets.QGridLayout(box)
        self.ref_button = QtWidgets.QPushButton("Take reference (open air)")
        self.ref_button.clicked.connect(self.take_reference)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self.clear_reference)
        self.sound_box = QtWidgets.QCheckBox("Clicks (audio)")
        self.sound_box.setEnabled(self.audio.available)
        if not self.audio.available:
            self.sound_box.setToolTip("Install alsa-utils (aplay) for audio clicks")
        self.sound_box.toggled.connect(self.toggle_sound)
        self.watch_label = QtWidgets.QLabel(
            "Watching: strongest signal (click the plot to pick one)"
        )
        self.watch_label.setWordWrap(True)
        unwatch = QtWidgets.QPushButton("Follow strongest")
        unwatch.clicked.connect(lambda: self.watch(None))
        lay.addWidget(self.ref_button, 0, 0, 1, 2)
        lay.addWidget(clear, 0, 2)
        lay.addWidget(self.watch_label, 1, 0, 1, 3)
        lay.addWidget(unwatch, 2, 0)
        lay.addWidget(self.sound_box, 2, 1, 1, 2)
        if self.demo is not None:
            lay.addWidget(
                QtWidgets.QLabel("Demo: probe distance to hidden 433.92 MHz device"), 3, 0, 1, 3
            )
            self.proximity = QtWidgets.QSlider(qt.orientation("Horizontal"))
            self.proximity.setRange(0, 100)
            self.proximity.valueChanged.connect(lambda v: setattr(self.demo, "proximity", v / 100))
            lay.addWidget(QtWidgets.QLabel("far"), 4, 0)
            lay.addWidget(self.proximity, 4, 1)
            lay.addWidget(QtWidgets.QLabel("touching"), 4, 2)
        return box

    def _make_table(self) -> Any:
        table = QtWidgets.QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["MHz", "width", "dB↑", "on", "gain", "likely"])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(qt.selection_behavior("SelectRows"))
        table.setEditTriggers(qt.edit_trigger("NoEditTriggers"))
        header = table.horizontalHeader()
        for col in range(5):
            header.setSectionResizeMode(col, qt.resize_mode("ResizeToContents"))
        header.setSectionResizeMode(5, qt.resize_mode("Stretch"))
        table.cellClicked.connect(lambda row, _col: self._watch_row(row))
        return table

    def _select_bin(self, hz: int) -> None:
        best = min(range(self.bin_box.count()), key=lambda i: abs(self.bin_box.itemData(i) - hz))
        self.bin_box.setCurrentIndex(best)

    # --------------------------------------------------------- actions

    def _toggle_trace(self, name: str, on: bool) -> None:
        self.spectrum.visible[name] = on
        self.spectrum.update()

    def scope_state_reset_peak(self) -> None:
        self.scope.state.reset_peak()

    def watch(self, hz: float | None) -> None:
        state = self.scope.state
        width = None
        if hz is not None:  # snap to a detected signal near the click
            for s in state.signals:
                if abs(s["hz"] - hz) <= max(s["width_hz"], 4 * state.bin_hz):
                    hz, width = float(s["hz"]), float(s["width_hz"]) or None
                    break
        state.set_watch(hz, width)
        self.watch_label.setText(
            "Watching: strongest signal (click the plot to pick one)"
            if hz is None
            else f"Watching {fmt_hz(hz)}"
        )

    def _watch_row(self, row: int) -> None:
        signals = self.scope.state.signals
        if 0 <= row < len(signals):
            self.watch(float(signals[row]["hz"]))

    def zoom(self, center_hz: float, factor: float) -> None:
        s = self.scope.state.settings
        start, stop = live.zoomed(s.start_mhz, s.stop_mhz, center_hz, factor)
        span = stop - start
        bin_hz = s.bin_hz
        # keep about 400-4000 bins on screen
        for candidate in (40_000, 100_000, 200_000, 500_000, 1_000_000, 2_000_000):
            if span * 1e6 / candidate <= 4000:
                bin_hz = candidate
                break
        self.start_box.setValue(start)
        self.stop_box.setValue(stop)
        self._select_bin(bin_hz)
        self.apply_tuning()

    def apply_preset(self, index: int) -> None:
        if index <= 0:
            return
        start, stop, bin_hz = PRESETS[self.preset.itemText(index)]
        self.start_box.setValue(start)
        self.stop_box.setValue(stop)
        self._select_bin(bin_hz)
        self.preset.setCurrentIndex(0)
        self.apply_tuning()

    def apply_tuning(self) -> None:
        start, stop = self.start_box.value(), self.stop_box.value()
        if stop <= start:
            self._note("The stop frequency must be above the start", error=True)
            return
        old = self.scope.state.settings
        settings = capture.CaptureSettings(
            start_mhz=start,
            stop_mhz=stop,
            bin_hz=int(self.bin_box.currentData()),
            lna_db=self.lna_box.value(),
            vga_db=self.vga_box.value(),
            amp=False,  # never: protects the HackRF's input, above all with a probe
            sweeps=old.sweeps,
        )
        try:
            self.scope.retune(settings)
        except ValueError as err:
            self._note(str(err), error=True)
            return
        self.waterfall.reset()
        for note in settings.adjustments():
            self._note(note)

    def set_mode(self, mode: str) -> None:
        self.mode_far.setChecked(mode == "far")
        self.mode_near.setChecked(mode == "near")
        self.scope.state.mode = mode
        if mode == "near" and self.scope.state.ref is None:
            self._note("Near field: hold the probe in open air and press 'Take reference'")
        if mode == "far":
            self.scope.state.clear_reference()

    def take_reference(self) -> None:
        self.scope.state.set_reference(8)
        self.set_mode("near")
        self._note("Taking a reference over the next 8 sweeps: keep the probe in open air")

    def clear_reference(self) -> None:
        self.scope.state.clear_reference()

    def toggle_sound(self, on: bool) -> None:
        if on:
            if not self.audio.start():
                self.sound_box.setChecked(False)
                self._note("No audio player found (install alsa-utils)", error=True)
        else:
            self.audio.stop()

    def toggle_record(self, on: bool) -> None:
        state = self.scope.state
        if not on:
            self.scope.last_recording = state.stop_recording()
            return
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Record", "Capture name:", text=time.strftime("scope-%Y%m%d-%H%M%S")
        )
        if not ok or not name:
            self.record_button.setChecked(False)
            return
        try:
            state.start_recording(
                self.scope.captures_dir,
                name,
                30.0,
                f"recorded from rfscoper scope ({state.mode} field)",
            )
        except (ValueError, OSError, RuntimeError) as err:
            self.record_button.setChecked(False)
            self._note(str(err), error=True)

    def export(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export traces", time.strftime("scope-traces-%Y%m%d-%H%M%S.csv"), "CSV (*.csv)"
        )
        if path:
            self.scope.state.export_csv(Path(path))
            self._note(f"Saved {path}")

    def screenshot(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save screenshot", time.strftime("scope-%Y%m%d-%H%M%S.png"), "PNG (*.png)"
        )
        if path:
            self.grab().save(path)
            self._note(f"Saved {path}")

    def _note(self, text: str, error: bool = False) -> None:
        self.last_status_note = text
        self.status.showMessage(("Error: " if error else "") + text, 8000)

    # --------------------------------------------------------- refresh

    def refresh(self) -> None:
        number, frame = self.scope.frame_number, self.scope.latest
        if frame is None or number == self.last_frame_number:
            return
        self.last_frame_number = number
        self.spectrum.set_frame(frame)
        self.waterfall.set_frame(frame, self.spectrum.db_min, self.spectrum.db_max)
        self.meter.set_meter(frame["meter"])
        self.audio.rate = float(frame["meter"].get("rate") or 0.0)
        self._fill_table(frame["signals"])
        st = frame["status"]
        rec = st.get("recording")
        if rec is None and self.record_button.isChecked():
            self.record_button.setChecked(False)
            last = st.get("last_recording") or {}
            if last.get("path"):
                self._note(f"Recorded {last['path']}: open it with `rfscoper show {last['name']}`")
        if rec is not None:
            self.record_button.setText(f"● {rec['remaining_s']:.0f} s")
        else:
            self.record_button.setText("● Record 30 s")
        if st.get("ref_pending"):
            self.ref_button.setText(f"Taking reference... {st['ref_pending']}")
        else:
            self.ref_button.setText(
                "Retake reference" if st.get("ref") else "Take reference (open air)"
            )
        rate = st.get("sweeps_per_s")
        msg = (
            f"{st['source']}  |  {st['start_mhz']}-{st['stop_mhz']} MHz  |  "
            f"bins {st['bin_hz']:,.0f} Hz (FFT {st['fft_size']})  |  LNA {st['lna_db']} VGA {st['vga_db']}  |  "
            f"sweep {frame['sweep']}"
            + ("" if rate is None else f" @ {rate:.1f}/s")
            + f"  |  {st['mode']} field  |  offline"
        )
        if not self.status.currentMessage() or self.status.currentMessage() == getattr(
            self, "_last_auto", ""
        ):
            self.status.showMessage(msg)
            self._last_auto = msg

    def _fill_table(self, signals: list[dict[str, Any]]) -> None:
        self.table.setRowCount(len(signals))
        for row, s in enumerate(signals):
            gain = s.get("gain_db")
            cells = [
                f"{s['hz'] / 1e6:.3f}",
                fmt_hz(s["width_hz"]).replace(" MHz", "M")
                if s["width_hz"] >= 1e6
                else f"{s['width_hz'] / 1e3:.0f}k",
                f"{s['above_db']:.0f}",
                "-" if s["duty"] is None else f"{s['duty']:.0%}",
                "-" if gain is None else f"{gain:+.0f}",
                (s["likely"] or f"unknown ({s['band'] or '?'})")
                + ("?" if s["label"] == "possible" else ""),
            ]
            for col, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if col == 4 and gain is not None and gain >= 20:
                    item.setForeground(QtGui.QColor(HOT))
                self.table.setItem(row, col, item)

    def closeEvent(self, event: Any) -> None:
        self.timer.stop()
        self.audio.stop()
        self.scope.stop()
        event.accept()


def run(
    scope: live.Scope,
    demo: live.DemoSource | None = None,
    seconds: float | None = None,
    screenshot: Path | None = None,
) -> int:
    """Open the window and block until it is closed."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["rfscoper"])
    app.setApplicationName("rfscoper")
    scope.start()
    window = ScopeWindow(scope, demo)
    window.show()
    if seconds:

        def finish() -> None:
            if screenshot is not None:
                window.grab().save(str(screenshot))
            window.close()

        QtCore.QTimer.singleShot(int(seconds * 1000), finish)
    return int(qt.exec_(app) or 0)
