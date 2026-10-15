"""The live engine: stream sweeps, keep a rolling picture, serve snapshots.

    hackrf_sweep -r -  ──►  Source.lines()  ──►  ScopeState.push_line()
                                                       │
                              window  ◄── frames ◄── ScopeState.frame()

A Source yields hackrf_sweep CSV rows as they are produced. HackRFSource runs
the real program with its output on stdout; DemoSource synthesizes rows so
the scope can be tried without hardware. Nothing here touches the network.

ScopeState turns rows into arrays on a fixed frequency grid, predicted from
hackrf_sweep's own rules so the grid is known before the first sweep ends:
the live trace, peak hold, a running average, the noise floor, an optional
near-field reference, and a short history for duty cycles. frame() packs a
downsampled snapshot of all that, plus detected signals and the meter
reading, into a dict the window draws from.
"""

from __future__ import annotations

import math
import signal
import subprocess
import threading
import time
import warnings
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Protocol

import numpy as np

from rfscoper import analysis, bands, capture, identify, probe
from rfscoper.parser import parse_row

FRAME_HZ = 8  # snapshots per second
DISPLAY_BINS = 1000  # points per trace in a snapshot
HISTORY_SWEEPS = 40  # sweeps kept for duty cycles
FLOOR_EVERY_S = 0.5  # how often the noise floor is re-estimated
AVG_ALPHA = 0.25  # weight of the newest sweep in the running average
METER_HOLD_S = 0.75  # the meter shows the strongest reading within this long
MAX_CLICKS_PER_S = 100.0

# ------------------------------------------------------------------ sources


class Source(Protocol):
    """Something that produces hackrf_sweep rows for a given set of settings."""

    name: str

    def start(self, settings: capture.CaptureSettings) -> str:
        """Begin producing rows. Returns the command (or a description) used."""

    def lines(self) -> Iterator[str]: ...

    def stop(self) -> None: ...


def streaming_command(settings: capture.CaptureSettings, executable: str) -> list[str]:
    """The hackrf_sweep command that streams to stdout until stopped (no -N)."""
    streaming = capture.CaptureSettings(**{**settings.__dict__, "duration_s": 1.0})
    return streaming.command("-", executable)


class HackRFSource:
    """Runs hackrf_sweep with its output on stdout and yields its rows."""

    name = "hackrf"

    def __init__(self, executable: str = "hackrf_sweep") -> None:
        self.executable = executable
        self.proc: subprocess.Popen[str] | None = None

    def start(self, settings: capture.CaptureSettings) -> str:
        cmd = streaming_command(settings, self.executable)
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
        )
        return " ".join(cmd)

    def lines(self) -> Iterator[str]:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        yield from proc.stdout

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGINT)  # hackrf_sweep closes cleanly on Ctrl+C
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@dataclass
class DemoEmitter:
    center_mhz: float
    width_mhz: float
    power_db: float
    on_probability: float = 1.0
    hidden: bool = False  # only loud when the simulated probe is close


DEMO_EMITTERS = [
    DemoEmitter(96.5, 0.2, -45),  # FM stations
    DemoEmitter(101.1, 0.4, -48),
    DemoEmitter(162.475, 0.016, -60),  # NOAA weather radio
    DemoEmitter(433.5, 0.2, -62, on_probability=0.08),  # a neighbor's sensor, far away
    DemoEmitter(433.92, 0.2, -72, on_probability=0.5, hidden=True),  # the hidden transmitter
    DemoEmitter(462.65, 0.2, -52),
    DemoEmitter(915.0, 0.5, -58, on_probability=0.1),
    DemoEmitter(2402, 1.0, -50, on_probability=0.3),  # BLE advertising
    DemoEmitter(2426, 1.0, -50, on_probability=0.3),
    DemoEmitter(2480, 1.0, -50, on_probability=0.3),
    DemoEmitter(2437, 18, -56, on_probability=0.7),  # Wi-Fi channel 6
]


class DemoSource:
    """Synthesizes hackrf_sweep rows, timed like a Raspberry Pi (~7.6 ms per row).

    `proximity` (0 to 1) models moving a near-field probe toward a hidden
    433.92 MHz transmitter: at 1.0 it is 40 dB louder than at 0.
    """

    name = "demo"

    def __init__(self, row_s: float = 0.0076, seed: int = 1) -> None:
        self.row_s = row_s
        self.rng = np.random.default_rng(seed)
        self.proximity = 0.0
        self._stop = threading.Event()
        self._settings: capture.CaptureSettings | None = None

    def start(self, settings: capture.CaptureSettings) -> str:
        self._settings = settings
        self._stop.clear()
        return "demo source (simulated, no hardware)"

    def lines(self) -> Iterator[str]:
        assert self._settings is not None
        radio = self._settings.radio()
        per_row = radio.fft_size // 4
        bin_hz = capture.SAMPLE_RATE_HZ / radio.fft_size
        lows = range(radio.start_mhz * 1_000_000, radio.stop_mhz * 1_000_000, 5_000_000)
        start_hz, stop_hz = radio.start_mhz * 1e6, radio.stop_mhz * 1e6
        while not self._stop.is_set():
            for low in lows:
                if self._stop.is_set():
                    return
                centers = low + (np.arange(per_row) + 0.5) * bin_hz
                power = -72 + 3 * (centers - start_hz) / max(stop_hz - start_hz, 1)
                power = power + self.rng.normal(0, 1.2, per_row)
                for e in DEMO_EMITTERS:
                    if abs(e.center_mhz * 1e6 - (low + 2.5e6)) > 2.5e6 + e.width_mhz * 1e6:
                        continue
                    if self.rng.random() >= e.on_probability:
                        continue
                    level = e.power_db + (40 * self.proximity if e.hidden else 0)
                    half = max(e.width_mhz * 1e6, bin_hz) / 2
                    inside = np.abs(centers - e.center_mhz * 1e6) <= half
                    power[inside] = np.maximum(power[inside], level + self.rng.normal(0, 0.5))
                when = datetime.now()
                values = ", ".join(f"{v:.2f}" for v in power)
                yield (
                    f"{when:%Y-%m-%d, %H:%M:%S.%f}, {low}, {low + 5_000_000}, "
                    f"{bin_hz:.2f}, {radio.fft_size}, {values}\n"
                )
                if self.row_s:
                    time.sleep(self.row_s)

    def stop(self) -> None:
        self._stop.set()


# ------------------------------------------------------------- arithmetic


def click_rate(above_db: float) -> float:
    """Geiger-counter clicks per second for a level above the baseline.

    Doubles about every 4.5 dB: 0 dB is one click a second, +15 dB is ten,
    +30 dB is a hundred (the cap). Below the baseline it slows to a crackle
    rather than going silent, so you can hear that it is still alive.
    """
    if math.isnan(above_db):
        return 0.0
    return float(min(MAX_CLICKS_PER_S, max(0.2, 10 ** (above_db / 15))))


def verdict(above_db: float) -> str:
    if math.isnan(above_db):
        return "no signal"
    if above_db >= probe.HOT_DB:
        return "HOT"
    if above_db >= probe.WARM_DB:
        return "warm"
    return "background"


def decimate(values: np.ndarray, n_out: int, how: str = "max") -> np.ndarray:
    """Shrink a trace to at most n_out points, keeping peaks (max) or trends (mean)."""
    n = len(values)
    if n <= n_out:
        return values
    factor = math.ceil(n / n_out)
    padded = np.full(factor * math.ceil(n / factor), np.nan)
    padded[:n] = values
    blocks = padded.reshape(-1, factor)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmax(blocks, axis=1) if how == "max" else np.nanmean(blocks, axis=1)


def zoomed(start_mhz: int, stop_mhz: int, center_hz: float, factor: float) -> tuple[int, int]:
    """A new range `factor` times the width of the old one, centered on center_hz.

    hackrf_sweep covers whole 20 MHz steps, so the result is never narrower
    than 20 MHz and never leaves 0-7250 MHz.
    """
    span = max(20, round((stop_mhz - start_mhz) * factor))
    span = min(span, capture.FREQ_MAX_MHZ)
    start = round(center_hz / 1e6 - span / 2)
    start = max(0, min(start, capture.FREQ_MAX_MHZ - span))
    return start, start + span


# -------------------------------------------------------------------- state


@dataclass
class Recording:
    name: str
    path: Path
    note: str
    started: str
    deadline: float
    file: IO[str]
    rows: int = 0


class ScopeState:
    """Everything the scope knows, on a fixed frequency grid."""

    def __init__(
        self,
        settings: capture.CaptureSettings,
        threshold_db: float = 10.0,
        on_level_db: float = 6.0,
        display_bins: int = DISPLAY_BINS,
        mode: str = "far",
        detect_on: str = "peak",
    ) -> None:
        self.settings = settings
        self.threshold_db = threshold_db
        self.on_level_db = on_level_db
        self.display_bins = display_bins
        self.mode = mode
        self.detect_on = detect_on
        self.watch_hz: float | None = None
        self.watch_width_hz: float | None = None

        self.radio = settings.radio()
        self.start_hz = self.radio.start_mhz * 1e6
        self.bin_hz = float(self.radio.bin_hz)
        n = round((self.radio.stop_mhz - self.radio.start_mhz) * 1e6 / self.bin_hz)
        self.freqs = self.start_hz + (np.arange(n) + 0.5) * self.bin_hz
        self.live = np.full(n, np.nan)
        self.peak = np.full(n, np.nan)
        self.avg: np.ndarray | None = None
        self.floor: np.ndarray | None = None
        self.ref: np.ndarray | None = None
        self.ref_peak = np.full(n, np.nan)  # peak hold since the reference was set
        self.waterfall: np.ndarray | None = None
        self.sweeps = 0
        self.rows = 0
        self.seen_lows: set[int] = set()
        self.sweep_times: deque[float] = deque(maxlen=20)
        self.history: deque[np.ndarray] = deque(maxlen=HISTORY_SWEEPS)
        self.recent: deque[tuple[float, np.ndarray]] = deque()  # (time, sweep) for the meter hold
        self.signals: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self._floor_at = 0.0
        self._ref_pending = 0
        self._ref_sum: np.ndarray | None = None
        self._ref_count = 0
        self.recording: Recording | None = None
        self.source_name = ""
        self.command = ""
        self.retuning = False
        self.started = time.time()

    # ------------------------------------------------------------ input

    def push_line(self, line: str) -> None:
        row = parse_row(line.split(","))
        if row is None:
            return
        with self.lock:
            if self.recording is not None:
                self.recording.file.write(line if line.endswith("\n") else line + "\n")
                self.recording.rows += 1
            if row.hz_low in self.seen_lows:
                self._complete_sweep()
            self.seen_lows.add(row.hz_low)
            first = round((row.hz_low - self.start_hz) / self.bin_hz)
            count = len(row.powers_db)
            if first < 0 or first + count > len(self.live):
                return  # a row from outside the predicted grid: ignore it
            values = np.asarray(row.powers_db)
            self.live[first : first + count] = values
            self.peak[first : first + count] = np.fmax(self.peak[first : first + count], values)
            self.ref_peak[first : first + count] = np.fmax(
                self.ref_peak[first : first + count], values
            )
            self.rows += 1

    def _complete_sweep(self) -> None:
        self.sweeps += 1
        self.sweep_times.append(time.time())
        self.seen_lows = set()
        snapshot = self.live.copy()
        self.waterfall = snapshot
        now = time.time()
        self.recent.append((now, snapshot))
        while self.recent and now - self.recent[0][0] > METER_HOLD_S:
            self.recent.popleft()
        if self.avg is None:
            self.avg = snapshot.copy()
        else:
            have = ~np.isnan(snapshot)
            self.avg[have] = (1 - AVG_ALPHA) * self.avg[have] + AVG_ALPHA * snapshot[have]
        if self.floor is None:
            self._update_floor()  # so duty cycles count from the first sweep
            self._floor_at = 0.0  # provisional: the next frame refines it and finds signals
        if self.floor is not None:
            self.history.append(snapshot >= self.floor + self.on_level_db)
        if self._ref_pending > 0:
            filled = np.nan_to_num(snapshot, nan=-200.0)
            self._ref_sum = filled if self._ref_sum is None else self._ref_sum + filled
            self._ref_count += 1
            self._ref_pending -= 1
            if self._ref_pending == 0:
                self.ref = self._ref_sum / self._ref_count
                self._ref_sum = None
                self.ref_peak = np.full_like(self.ref_peak, np.nan)

    # ---------------------------------------------------------- controls

    def reset_peak(self) -> None:
        with self.lock:
            self.peak = np.full_like(self.peak, np.nan)
            self.history.clear()

    def set_watch(self, hz: float | None, width_hz: float | None = None) -> None:
        with self.lock:
            self.watch_hz = hz
            self.watch_width_hz = width_hz

    def set_reference(self, sweeps: int = 8) -> None:
        """Average the next few sweeps into the near-field reference."""
        with self.lock:
            self._ref_pending = max(1, sweeps)
            self._ref_sum = None
            self._ref_count = 0

    def clear_reference(self) -> None:
        with self.lock:
            self.ref = None
            self._ref_pending = 0

    def start_recording(self, directory: Path, name: str, seconds: float, note: str) -> Path:
        if not capture.NAME_PATTERN.fullmatch(name):
            raise ValueError("Use only letters, numbers, - and _ in capture names")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.csv"
        if path.exists():
            raise FileExistsError(f"A capture named '{name}' already exists")
        with self.lock:
            if self.recording is not None:
                raise RuntimeError(f"Already recording '{self.recording.name}'")
            handle = open(path, "w")  # noqa: SIM115  (closed by stop_recording)
            self.recording = Recording(
                name, path, note, capture.now_iso(), time.monotonic() + seconds, handle
            )
        return path

    def stop_recording(self) -> dict[str, Any] | None:
        """Close the recording and write its record, like `rfscoper capture` would."""
        with self.lock:
            rec, self.recording = self.recording, None
        if rec is None:
            return None
        rec.file.close()
        finished = capture.now_iso()
        if rec.rows == 0:
            rec.path.unlink(missing_ok=True)
            return {"name": rec.name, "error": "no data was received while recording"}
        requested = capture.CaptureSettings(**self.settings.__dict__)
        elapsed = datetime.fromisoformat(finished) - datetime.fromisoformat(rec.started)
        requested.duration_s = round(elapsed.total_seconds(), 1)
        record = capture.write_record(
            rec.path, rec.name, requested, rec.note, self.command, rec.started, finished
        )
        return {"name": rec.name, "path": str(rec.path), "measured": record["measured"]}

    def export_csv(self, path: Path) -> Path:
        """Write the full-resolution traces for analysis elsewhere."""
        with self.lock:
            columns = {
                "hz": self.freqs,
                "live_db": self.live,
                "peak_db": self.peak,
                "avg_db": self.avg,
                "floor_db": self.floor,
                "ref_db": self.ref,
            }
            present = {k: v for k, v in columns.items() if v is not None}
            table = np.column_stack(list(present.values()))
        header = ",".join(present)
        np.savetxt(path, table, delimiter=",", header=header, comments="", fmt="%.2f")
        return Path(path)

    # ---------------------------------------------------------- analysis

    def _update_floor(self) -> None:
        basis = self.avg if self.avg is not None else self.live
        if np.all(np.isnan(basis)):
            return
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            self.floor = analysis.estimate_floor(self.freqs, basis)
        self._floor_at = time.time()

    def _update_signals(self) -> None:
        if self.floor is None:
            return
        if self.detect_on == "peak":
            basis = self.peak
        else:
            basis = self.avg if self.avg is not None else self.live
        peaks = analysis.find_peaks(self.freqs, basis, self.floor, self.threshold_db)[:25]
        history = np.array(self.history) if self.history else None
        found = []
        for p in peaks:
            i = int(np.argmin(np.abs(self.freqs - p.freq_hz)))
            duty = float(history[:, i].mean()) if history is not None else None
            guesses = identify.identify(p.center_hz, p.width_hz, self.bin_hz, duty)
            top = guesses[0] if guesses and guesses[0].score >= identify.POSSIBLE else None
            entry: dict[str, Any] = {
                "hz": p.center_hz,
                "width_hz": p.width_hz,
                "db": round(p.power_db, 1),
                "above_db": round(p.margin_db, 1),
                "duty": None if duty is None else round(duty, 2),
                "band": bands.lookup(p.center_hz),
                "likely": top.kind.name if top else None,
                "short": top.kind.short if top else "",
                "label": top.label if top else "unknown",
                "gain_db": None,
            }
            if self.ref is not None:
                # Gain of the strongest sighting since the reference was set
                # (peak hold), so a bursty signal is not missed between bursts.
                sel = (self.freqs >= p.start_hz) & (self.freqs <= p.stop_hz)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    gain = float(np.nanmax(self.ref_peak[sel]) - np.nanmax(self.ref[sel]))
                entry["gain_db"] = None if math.isnan(gain) else round(gain, 1)
            found.append(entry)
        self.signals = found

    def meter(self) -> dict[str, Any]:
        """Level at the watched frequency (or the strongest bin) above the baseline."""
        empty = {
            "hz": self.watch_hz,
            "db": None,
            "base_db": None,
            "basis": "none",
            "above_db": None,
            "rate": 0.0,
            "verdict": "no signal",
        }
        if self.watch_hz is None:
            if np.all(np.isnan(self.live)):
                return empty
            i = int(np.nanargmax(self.live))
            sel = np.zeros(len(self.live), dtype=bool)
            sel[max(0, i - 1) : i + 2] = True
            hz = float(self.freqs[i])
        else:
            half = max(self.watch_width_hz or 0.0, 3 * self.bin_hz) / 2
            sel = np.abs(self.freqs - self.watch_hz) <= half
            hz = self.watch_hz
            if not sel.any():
                return empty
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            level = float(np.nanmax(self.live[sel]))
            cutoff = time.time() - METER_HOLD_S
            for when, sweep in self.recent:  # hold: the strongest recent reading
                if when >= cutoff:
                    level = float(np.fmax(level, np.nanmax(sweep[sel])))
            if self.ref is not None:
                base, basis = float(np.nanmax(self.ref[sel])), "reference"
            elif self.floor is not None:
                base, basis = float(np.nanmean(self.floor[sel])), "noise floor"
            else:
                base, basis = math.nan, "none"
        above = level - base
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            best = float(np.nanmax(self.ref_peak[sel])) - base
        return {
            "hz": hz,
            "best_above_db": None if math.isnan(best) else round(best, 1),
            "db": None if math.isnan(level) else round(level, 1),
            "base_db": None if math.isnan(base) else round(base, 1),
            "basis": basis,
            "above_db": None if math.isnan(above) else round(above, 1),
            "rate": round(click_rate(above), 2),
            "verdict": verdict(above),
        }

    # ------------------------------------------------------------ output

    def frame(self) -> dict[str, Any]:
        """A snapshot for drawing: downsampled traces, signals, meter, status."""
        with self.lock:
            now = time.time()
            if self.floor is None or now - self._floor_at >= FLOOR_EVERY_S:
                self._update_floor()
                self._update_signals()
            rate = None
            if len(self.sweep_times) >= 2:
                span = self.sweep_times[-1] - self.sweep_times[0]
                rate = (len(self.sweep_times) - 1) / span if span > 0 else None
            n_out = self.display_bins
            factor = max(1, math.ceil(len(self.freqs) / n_out))
            rec = None
            if self.recording is not None:
                rec = {
                    "name": self.recording.name,
                    "remaining_s": max(0.0, round(self.recording.deadline - time.monotonic(), 1)),
                    "rows": self.recording.rows,
                }

            def shrink(values: np.ndarray | None, how: str) -> np.ndarray | None:
                return None if values is None else decimate(values, n_out, how)

            return {
                "t": now,
                "sweep": self.sweeps,
                "rows": self.rows,
                "grid": {
                    "start_hz": self.start_hz,
                    "step_hz": self.bin_hz * factor,
                    "n": math.ceil(len(self.freqs) / factor),
                    "bin_hz": self.bin_hz,
                    "stop_hz": self.start_hz + len(self.freqs) * self.bin_hz,
                },
                "live": shrink(self.live, "max"),
                "peak": shrink(self.peak, "max"),
                "avg": shrink(self.avg, "mean"),
                "floor": shrink(self.floor, "mean"),
                "ref": shrink(self.ref, "max"),
                "waterfall": shrink(self.waterfall, "max"),
                "signals": list(self.signals),
                "meter": self.meter(),
                "status": {
                    "source": self.source_name,
                    "command": self.command,
                    "start_mhz": self.radio.start_mhz,
                    "stop_mhz": self.radio.stop_mhz,
                    "bin_hz": round(self.bin_hz, 2),
                    "fft_size": self.radio.fft_size,
                    "lna_db": self.radio.lna_db,
                    "vga_db": self.radio.vga_db,
                    "sweeps_per_s": None if rate is None else round(rate, 2),
                    "mode": self.mode,
                    "watch_hz": self.watch_hz,
                    "watch_width_hz": self.watch_width_hz,
                    "threshold_db": self.threshold_db,
                    "detect_on": self.detect_on,
                    "ref": self.ref is not None,
                    "ref_pending": self._ref_pending,
                    "retuning": self.retuning,
                    "recording": rec,
                    "uptime_s": round(now - self.started, 1),
                },
            }


# -------------------------------------------------------------------- scope


class Scope:
    """Runs a source into a ScopeState and keeps the latest frame ready."""

    def __init__(
        self,
        settings: capture.CaptureSettings,
        source: Source,
        captures_dir: Path = capture.DEFAULT_DIR,
        frame_hz: float = FRAME_HZ,
    ) -> None:
        self.source = source
        self.captures_dir = Path(captures_dir)
        self.frame_hz = frame_hz
        self.home = (settings.start_mhz, settings.stop_mhz)  # the range to return to
        self.state = ScopeState(settings)
        self.latest: dict[str, Any] | None = None
        self.frame_ready = threading.Condition()
        self.frame_number = 0
        self.last_recording: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._ticker: threading.Thread | None = None
        self._tune_lock = threading.Lock()

    def start(self) -> None:
        self._start_source(self.state)
        self._ticker = threading.Thread(target=self._tick_loop, name="scope-ticker", daemon=True)
        self._ticker.start()

    def _start_source(self, state: ScopeState) -> None:
        state.command = self.source.start(state.settings)
        state.source_name = self.source.name
        self._reader = threading.Thread(
            target=self._read_loop, args=(state,), name="scope-reader", daemon=True
        )
        self._reader.start()

    def _read_loop(self, state: ScopeState) -> None:
        try:
            for line in self.source.lines():
                if self._stop.is_set() or state is not self.state:
                    break
                state.push_line(line)
        except (ValueError, OSError):
            pass

    def _tick_loop(self) -> None:
        period = 1.0 / self.frame_hz
        while not self._stop.is_set():
            started = time.monotonic()
            state = self.state
            rec = state.recording
            if rec is not None and time.monotonic() >= rec.deadline:
                self.last_recording = state.stop_recording()
            frame = state.frame()
            frame["status"]["last_recording"] = self.last_recording
            with self.frame_ready:
                self.latest = frame
                self.frame_number += 1
                self.frame_ready.notify_all()
            time.sleep(max(0.0, period - (time.monotonic() - started)))

    def wait_frame(self, after: int, timeout: float = 5.0) -> tuple[int, dict[str, Any] | None]:
        with self.frame_ready:
            if self.frame_number <= after:
                self.frame_ready.wait(timeout)
            return self.frame_number, self.latest

    def retune(self, settings: capture.CaptureSettings) -> ScopeState:
        """Restart the source with new settings, carrying the view settings over."""
        settings.validate()
        with self._tune_lock:
            old = self.state
            old.retuning = True
            if old.recording is not None:
                self.last_recording = old.stop_recording()
            self.source.stop()
            if self._reader is not None:
                self._reader.join(timeout=5)
            new = ScopeState(
                settings,
                threshold_db=old.threshold_db,
                on_level_db=old.on_level_db,
                display_bins=old.display_bins,
                mode=old.mode,
                detect_on=old.detect_on,
            )
            lo, hi = new.start_hz, new.freqs[-1] + new.bin_hz / 2
            if old.watch_hz is not None and lo <= old.watch_hz <= hi:
                new.watch_hz, new.watch_width_hz = old.watch_hz, old.watch_width_hz
            new.started = old.started
            self.state = new
            self._start_source(new)
            return new

    def stop(self) -> None:
        self._stop.set()
        if self.state.recording is not None:
            self.last_recording = self.state.stop_recording()
        self.source.stop()
        if self._reader is not None:
            self._reader.join(timeout=5)
        if self._ticker is not None:
            self._ticker.join(timeout=2)
