import math
import time

import numpy as np
import pytest

from rfscoper import capture, live
from rfscoper.capture import read_metadata
from rfscoper.parser import load_sweeps, parse_row

MHZ = 1_000_000


def settings(start=400, stop=480, bin_hz=100_000, **kw):
    return capture.CaptureSettings(start, stop, bin_hz, duration_s=None, **kw)


def feed(state: live.ScopeState, source: live.DemoSource, sweeps: int) -> None:
    """Push rows until the state has completed `sweeps` more sweeps."""
    target = state.sweeps + sweeps
    for line in source.lines():
        state.push_line(line)
        if state.sweeps >= target:
            break
    source.stop()


# ----------------------------------------------------------------- pieces


def test_click_rate_doubles_every_few_db():
    assert live.click_rate(0) == pytest.approx(1.0)
    assert live.click_rate(15) == pytest.approx(10.0)
    assert live.click_rate(30) == pytest.approx(100.0)
    assert live.click_rate(60) == live.MAX_CLICKS_PER_S  # capped
    assert live.click_rate(-30) == 0.2  # a crackle, never silent
    assert live.click_rate(math.nan) == 0.0


def test_decimate_keeps_peaks_and_trends():
    values = np.full(1000, -70.0)
    values[500] = -20.0
    assert len(live.decimate(values, 100)) == 100
    assert live.decimate(values, 100, "max").max() == -20.0  # the spike survives
    assert live.decimate(values, 100, "mean").max() < -60  # but not in the average
    short = values[:50]
    assert live.decimate(short, 100) is short  # nothing to shrink


def test_demo_source_rows_look_like_hackrf_sweep():
    source = live.DemoSource(row_s=0)
    source.start(settings(2400, 2500, 250_000))
    lines = []
    for line in source.lines():
        lines.append(line)
        if len(lines) == 20:
            break
    source.stop()
    row = parse_row(lines[0].split(","))
    assert row.fft_size == 84 and row.bin_hz == 238095.24  # hackrf_sweep's real rounding
    assert len(row.powers_db) == 21
    assert {parse_row(ln.split(",")).hz_low for ln in lines} == set(
        range(2400 * MHZ, 2500 * MHZ, 5 * MHZ)
    )


# ------------------------------------------------------------------ state


def test_state_grid_is_predicted_from_hackrf_rules():
    state = live.ScopeState(settings(2400, 2500, 250_000))
    assert state.bin_hz == 238095.24
    assert len(state.freqs) == 420
    assert state.freqs[0] == pytest.approx(2400 * MHZ + 238095.24 / 2)


def test_state_assembles_sweeps_and_frames():
    state = live.ScopeState(settings())
    source = live.DemoSource(row_s=0)
    source.start(state.settings)
    feed(state, source, sweeps=3)

    assert state.sweeps == 3
    assert not np.isnan(state.live).any()  # every bin of the grid was filled
    frame = state.frame()
    assert frame["sweep"] == 3
    assert frame["grid"]["n"] == len(frame["live"]) == len(frame["peak"]) <= live.DISPLAY_BINS
    assert frame["floor"] is not None and frame["avg"] is not None
    assert frame["status"]["start_mhz"] == 400 and frame["status"]["fft_size"] == 204


def test_state_detects_and_identifies_signals():
    state = live.ScopeState(settings(400, 480, 100_000))
    source = live.DemoSource(row_s=0)
    source.start(state.settings)
    feed(state, source, sweeps=12)
    frame = state.frame()
    by_mhz = {round(s["hz"] / MHZ, 1): s for s in frame["signals"]}
    assert 462.6 in by_mhz or 462.7 in by_mhz
    frs = by_mhz.get(462.6) or by_mhz[462.7]
    assert frs["duty"] is not None and frs["duty"] > 0.8  # steady in the demo
    assert frs["band"] == "FRS/GMRS"


def test_meter_follows_the_watched_frequency():
    state = live.ScopeState(settings(400, 480, 100_000))
    source = live.DemoSource(row_s=0)
    source.start(state.settings)
    feed(state, source, sweeps=4)
    state.frame()  # computes the floor
    state.watch_hz = 462.65 * MHZ
    reading = state.meter()
    assert reading["hz"] == 462.65 * MHZ
    assert reading["above_db"] > 10 and reading["verdict"] in ("warm", "HOT")
    assert reading["rate"] > 1

    state.watch_hz = 410 * MHZ  # just noise there
    quiet = state.meter()
    assert abs(quiet["above_db"]) < 8 and quiet["verdict"] == "background"


def test_reference_changes_the_baseline():
    state = live.ScopeState(settings(420, 440, 100_000))
    source = live.DemoSource(row_s=0)
    source.start(state.settings)
    state.watch_hz = 433.92 * MHZ
    state.set_reference(3)
    feed(state, source, sweeps=5)
    assert state.ref is not None
    assert state.frame()["meter"]["basis"] == "reference"

    source.proximity = 1.0  # the hidden transmitter is now 40 dB louder
    source2 = live.DemoSource(row_s=0)
    source2.proximity = 1.0
    source2.start(state.settings)
    feed(state, source2, sweeps=6)
    reading = state.frame()["meter"]
    assert reading["best_above_db"] > 25 and reading["verdict"] == "HOT"


def test_recording_from_the_scope_is_a_normal_capture(tmp_path):
    state = live.ScopeState(settings(400, 480, 100_000))
    state.command = "demo"
    source = live.DemoSource(row_s=0)
    source.start(state.settings)
    path = state.start_recording(tmp_path, "scoped", seconds=60, note="from the scope")
    feed(state, source, sweeps=3)
    result = state.stop_recording()

    assert result["path"] == str(path)
    data = load_sweeps(path)
    assert data.complete_sweeps >= 2
    meta = read_metadata(path)
    assert meta["note"] == "from the scope"
    assert meta["requested"]["sweeps"] is None  # it ran until stopped
    assert meta["measured"]["fft_size"] == 204


def test_recording_refuses_to_overwrite(tmp_path):
    (tmp_path / "x.csv").write_text("")
    state = live.ScopeState(settings())
    with pytest.raises(FileExistsError):
        state.start_recording(tmp_path, "x", 1, "")
    with pytest.raises(ValueError):
        state.start_recording(tmp_path, "bad name", 1, "")


# ------------------------------------------------------------------ scope


def test_scope_runs_retunes_and_stops(tmp_path):
    scope = live.Scope(
        settings(400, 480, 100_000), live.DemoSource(row_s=0.001), tmp_path, frame_hz=20
    )
    scope.start()
    number, frame = scope.wait_frame(0, timeout=5)
    deadline = time.time() + 5
    while (frame is None or frame["sweep"] < 2) and time.time() < deadline:
        number, frame = scope.wait_frame(number, timeout=5)
    assert frame is not None and frame["sweep"] >= 2

    scope.state.watch_hz = 433.92 * MHZ
    new = scope.retune(settings(420, 440, 100_000))
    assert new is scope.state
    assert new.watch_hz == 433.92 * MHZ  # carried over, it's inside the new range
    deadline = time.time() + 5
    while time.time() < deadline:
        latest = scope.latest
        if scope.state.sweeps >= 1 and latest and latest["status"]["start_mhz"] == 420:
            break
        time.sleep(0.05)
    assert scope.state.sweeps >= 1
    assert scope.latest is not None and scope.latest["status"]["start_mhz"] == 420
    scope.stop()


# ------------------------------------------------------------- the window


@pytest.fixture(scope="module")
def qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from rfscoper import qt

    if not qt.AVAILABLE:
        pytest.skip("Qt is not installed")
    return qt.QtWidgets.QApplication.instance() or qt.QtWidgets.QApplication([])


def pump(seconds: float) -> None:
    from rfscoper import qt

    loop = qt.QtCore.QEventLoop()
    qt.QtCore.QTimer.singleShot(int(seconds * 1000), loop.quit)
    qt.exec_(loop)


def test_window_draws_frames_and_finds_the_hidden_emitter(qapp, tmp_path):
    from rfscoper import app

    demo = live.DemoSource(row_s=0.001)
    scope = live.Scope(settings(400, 480, 100_000), demo, tmp_path)
    scope.start()
    window = app.ScopeWindow(scope, demo)
    window.show()
    try:
        pump(1.5)
        assert window.last_frame_number > 3
        assert window.table.rowCount() >= 1

        window.take_reference()
        pump(1.5)
        assert scope.state.ref is not None or scope.state.ref_pending

        window.watch(433.92 * MHZ)
        demo.proximity = 1.0  # bring the probe right up to the hidden transmitter
        pump(1.5)
        meter = scope.latest["meter"]
        assert meter["above_db"] > 20
        assert meter["verdict"].startswith("HOT")

        shot = tmp_path / "shot.png"
        assert window.grab().save(str(shot)) and shot.stat().st_size > 10_000
    finally:
        window.close()
