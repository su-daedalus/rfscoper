"""Run hackrf_sweep and store each capture next to an accurate record of it.

Each capture is two files in the captures folder (~/rfscoper-captures by
default, outside the code on purpose: see default_dir):

    hallway.csv         raw hackrf_sweep output
    hallway.meta.json   the record of how it was taken

The record keeps three views of the same capture, because they differ:

    requested   what you asked rfscoper for
    radio       what hackrf_sweep actually used, after applying its own rules
                (it rounds the bin width, the range and the gains)
    measured    what the CSV file actually contains, read back after the capture

The hackrf_sweep rules modeled here come from its source code
(host/hackrf-tools/src/hackrf_sweep.c and host/libhackrf/src/hackrf.c in
github.com/greatscottgadgets/hackrf).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rfscoper import __version__
from rfscoper.parser import SweepData, load_sweeps


def default_dir() -> Path:
    """Where captures live: $RFSCOPER_DIR if set, otherwise ~/rfscoper-captures.

    Deliberately outside the project folder. A capture records what was
    transmitting where you stood and your notes about the place, so it should
    never sit where `git add .` could pick it up and publish it.
    """
    configured = os.environ.get("RFSCOPER_DIR")
    return Path(configured).expanduser() if configured else Path.home() / "rfscoper-captures"


LEGACY_DIR = Path("captures")  # where versions before 0.1.0 saved, relative to the shell
DEFAULT_DIR = default_dir()
NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
META_SUFFIX = ".meta.json"

# hackrf_sweep constants
SAMPLE_RATE_HZ = 20_000_000  # it always samples at 20 MHz
TUNE_STEP_MHZ = 20  # it retunes in 20 MHz steps
FREQ_MAX_MHZ = 7250
MIN_FFT_SIZE, MAX_FFT_SIZE = 4, 8180  # which limits the bin width to 2445 Hz - 5 MHz


def fft_size_for(bin_hz: int) -> int:
    """The FFT size hackrf_sweep picks for a requested bin width.

    It divides the 20 MHz sample rate by the bin width (integer division,
    as in C), checks that result against its limits, and only then rounds
    up until FFT size + 4 is a multiple of 8, which its interleaved sweep
    needs. So the FFT size is always 4, 12, 20, 28 ...
    """
    size = _raw_fft_size(bin_hz)
    while (size + 4) % 8:
        size += 1
    return size


def _raw_fft_size(bin_hz: int) -> int:
    """FFT size before rounding: what hackrf_sweep checks its limits against."""
    return SAMPLE_RATE_HZ // bin_hz


@dataclass(frozen=True)
class RadioSettings:
    """What hackrf_sweep actually runs with, after its own rounding."""

    start_mhz: int
    stop_mhz: int
    fft_size: int
    bin_hz: float
    lna_db: int
    vga_db: int
    amp: bool
    sample_rate_hz: int = SAMPLE_RATE_HZ


@dataclass
class CaptureSettings:
    """What was asked for."""

    start_mhz: int = 1
    stop_mhz: int = 6000
    bin_hz: int = 1_000_000
    sweeps: int = 30
    lna_db: int = 24
    vga_db: int = 20
    amp: bool = False
    duration_s: float | None = None  # when set, capture for this long instead of N sweeps

    def validate(self) -> None:
        if not 0 <= self.start_mhz < self.stop_mhz:
            raise ValueError("The start frequency must be below the stop frequency")
        if self.stop_mhz > FREQ_MAX_MHZ:
            raise ValueError(f"hackrf_sweep can't tune above {FREQ_MAX_MHZ} MHz")
        if self.bin_hz <= 0:
            raise ValueError("The bin width must be positive")
        size = _raw_fft_size(self.bin_hz)  # checked before rounding, as hackrf_sweep does
        if size < MIN_FFT_SIZE:
            raise ValueError("hackrf_sweep needs a bin width of 5 MHz or less")
        if size > MAX_FFT_SIZE:
            raise ValueError("hackrf_sweep needs a bin width of 2445 Hz or more")
        if self.duration_s is not None and self.duration_s <= 0:
            raise ValueError("The duration must be positive")
        if self.duration_s is None and self.sweeps < 1:
            raise ValueError("Take at least one sweep")
        if not 0 <= self.lna_db <= 40:
            raise ValueError("LNA gain must be 0-40 dB")
        if not 0 <= self.vga_db <= 62:
            raise ValueError("VGA gain must be 0-62 dB")

    def radio(self) -> RadioSettings:
        """Apply hackrf_sweep's rules to predict what it will really use."""
        steps = 1 + (self.stop_mhz - self.start_mhz - 1) // TUNE_STEP_MHZ
        size = fft_size_for(self.bin_hz)
        return RadioSettings(
            start_mhz=self.start_mhz,
            stop_mhz=self.start_mhz + steps * TUNE_STEP_MHZ,  # whole 20 MHz steps
            fft_size=size,
            bin_hz=round(SAMPLE_RATE_HZ / size, 2),  # as printed in the CSV
            lna_db=self.lna_db & ~0x07,  # libhackrf rounds down to steps of 8
            vga_db=self.vga_db & ~0x01,  # and to steps of 2
            amp=self.amp,
        )

    def adjustments(self) -> list[str]:
        """Plain-language notes on everything hackrf_sweep will change."""
        radio = self.radio()
        notes = []
        if radio.bin_hz != self.bin_hz:
            notes.append(
                f"bin width {self.bin_hz:,} Hz -> {radio.bin_hz:,.2f} Hz "
                f"(hackrf_sweep uses FFT sizes of the form 8k+4; {radio.fft_size} here)"
            )
        if radio.stop_mhz != self.stop_mhz:
            notes.append(
                f"range {self.start_mhz}-{self.stop_mhz} MHz -> "
                f"{radio.start_mhz}-{radio.stop_mhz} MHz (hackrf_sweep covers whole 20 MHz steps)"
            )
        if radio.lna_db != self.lna_db:
            notes.append(f"LNA gain {self.lna_db} -> {radio.lna_db} dB (steps of 8)")
        if radio.vga_db != self.vga_db:
            notes.append(f"VGA gain {self.vga_db} -> {radio.vga_db} dB (steps of 2)")
        return notes

    def requested(self) -> dict[str, Any]:
        """The request as recorded: only what actually applies."""
        timed = self.duration_s is not None
        return {
            "start_mhz": self.start_mhz,
            "stop_mhz": self.stop_mhz,
            "bin_hz": self.bin_hz,
            "sweeps": None if timed else self.sweeps,
            "duration_s": self.duration_s,
            "lna_db": self.lna_db,
            "vga_db": self.vga_db,
            "amp": self.amp,
        }

    def command(
        self, csv_path: Path | str, executable: str = "hackrf_sweep", stream: bool = False
    ) -> list[str]:
        """The exact hackrf_sweep command these settings produce.

        stream=True leaves out -N so hackrf_sweep runs until it is stopped,
        which is how timed captures and the live scope use it.
        """
        cmd = [executable]
        cmd += ["-f", f"{self.start_mhz}:{self.stop_mhz}"]
        cmd += ["-w", str(self.bin_hz)]
        cmd += ["-l", str(self.lna_db)]
        cmd += ["-g", str(self.vga_db)]
        if self.duration_s is None and not stream:  # otherwise it runs until stopped
            cmd += ["-N", str(self.sweeps)]
        cmd += ["-r", str(csv_path)]
        if self.amp:
            cmd += ["-a", "1"]
        return cmd


def measure(data: SweepData) -> dict[str, Any]:
    """Facts about a capture, read from the CSV itself."""
    half = data.bin_hz / 2
    duration = data.duration_s
    rate = (data.complete_sweeps / duration) if duration else None
    return {
        "sweeps": data.n_sweeps,
        "complete_sweeps": data.complete_sweeps,
        "last_sweep_complete": data.last_sweep_complete,
        "rows": data.rows,
        "bins": data.n_bins,
        "bin_hz": data.bin_hz,
        "fft_size": data.fft_size,
        "low_edge_hz": round(int(data.freqs_hz[0]) - half),
        "high_edge_hz": round(int(data.freqs_hz[-1]) + half),
        "first_row": data.timestamps[0],
        "last_row": data.end_timestamp,
        "duration_s": round(duration, 3) if duration is not None else None,
        "sweeps_per_s": round(rate, 2) if rate is not None else None,
    }


def check(radio: dict[str, Any], measured: dict[str, Any]) -> list[str]:
    """Disagreements between what hackrf_sweep should have done and what it did."""
    warnings = []
    if radio.get("fft_size") and measured["fft_size"] != radio["fft_size"]:
        warnings.append(
            f"expected FFT size {radio['fft_size']}, the file has {measured['fft_size']}"
        )
    if radio.get("start_mhz") is not None:
        low, high = radio["start_mhz"] * 1e6, radio["stop_mhz"] * 1e6
        if measured["low_edge_hz"] > low + 1e6 or measured["high_edge_hz"] < high - 1e6:
            warnings.append(
                f"expected coverage {radio['start_mhz']}-{radio['stop_mhz']} MHz, the file has "
                f"{measured['low_edge_hz'] / 1e6:.1f}-{measured['high_edge_hz'] / 1e6:.1f} MHz"
            )
    if measured["complete_sweeps"] == 0:
        warnings.append("no complete sweep: the capture stopped before covering the range once")
    return warnings


def meta_path(csv_path: Path) -> Path:
    """Where a capture's metadata lives: hallway.csv -> hallway.meta.json."""
    return csv_path.with_name(csv_path.stem + META_SUFFIX)


def now_iso() -> str:
    """Local time with its UTC offset, to the millisecond."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def run_capture(
    name: str,
    settings: CaptureSettings,
    directory: Path = DEFAULT_DIR,
    note: str = "",
    executable: str = "hackrf_sweep",
    overwrite: bool = False,
) -> Path:
    """Run one capture, write its record, and return the path of its CSV file.

    With `overwrite`, an existing capture of the same name is replaced, but
    only once the new one has succeeded: if it fails or is interrupted, the
    old capture is put back untouched.
    """
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("Use only letters, numbers, - and _ in capture names")
    settings.validate()
    if shutil.which(executable) is None:
        raise FileNotFoundError(f"{executable} not found. Is the HackRF software installed?")

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f"{name}.csv"
    if csv_path.exists() and not overwrite:
        raise FileExistsError(
            f"A capture named '{name}' already exists "
            f"(the next free name is {free_name(name, directory)})"
        )
    # The old record and plot go too, so nothing describes the wrong capture.
    with replacing([csv_path, meta_path(csv_path), csv_path.with_suffix(".png")]):
        _capture(csv_path, name, settings, note, executable)
    return csv_path


def _capture(
    csv_path: Path, name: str, settings: CaptureSettings, note: str, executable: str
) -> None:
    cmd = settings.command(csv_path, executable)
    started = now_iso()
    if settings.duration_s is None:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        failed = result.returncode != 0
        stderr = result.stderr
    else:
        stopped_by_us, stderr = _run_for(cmd, settings.duration_s)
        # A timed capture ends with our Ctrl+C, so its exit code says little.
        # What matters is whether it stopped on time and left data behind.
        failed = not stopped_by_us
    finished = now_iso()
    if failed or not csv_path.exists() or csv_path.stat().st_size == 0:
        raise RuntimeError(f"hackrf_sweep failed:\n{stderr.strip()}")

    write_record(csv_path, name, settings, note, shlex.join(cmd), started, finished)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


@contextmanager
def replacing(paths: list[Path]) -> Iterator[None]:
    """Move existing paths aside; delete them if the block succeeds, restore them if not."""
    moved: list[tuple[Path, Path]] = []
    for path in paths:
        if path.exists():
            aside = path.with_name(f".{path.name}.replaced")
            if aside.exists():
                _remove(aside)
            path.rename(aside)
            moved.append((path, aside))
    try:
        yield
    except BaseException:  # including Ctrl+C: put the old files back
        for path, aside in moved:
            if path.exists():
                _remove(path)
            aside.rename(path)
        raise
    for _, aside in moved:
        _remove(aside)


def free_name(
    name: str, directory: Path = DEFAULT_DIR, exists: Callable[[str], bool] | None = None
) -> str:
    """The next unused name in the series: room -> room-capture2, room-capture3, ...

    `exists` says whether a name is taken (by default: a capture with that
    name in `directory`). An existing series is continued, so room-capture2
    is followed by room-capture3, not room-capture2-capture2.
    """
    if exists is None:
        exists = lambda candidate: (Path(directory) / f"{candidate}.csv").exists()  # noqa: E731
    base = re.sub(r"-capture\d+$", "", name)
    n = 2
    while exists(f"{base}-capture{n}"):
        n += 1
    return f"{base}-capture{n}"


def write_record(
    csv_path: Path,
    name: str,
    settings: CaptureSettings,
    note: str,
    command: str,
    started: str,
    finished: str,
) -> dict[str, Any]:
    """Write the .meta.json record for a CSV that hackrf_sweep just produced."""
    radio = asdict(settings.radio())
    measured = measure(load_sweeps(csv_path))
    metadata = {
        "name": name,
        "note": note,
        "started": started,
        "finished": finished,
        "requested": settings.requested(),
        "radio": radio,
        "measured": measured,
        "warnings": check(radio, measured),
        "command": command,
        "rfscoper_version": __version__,
    }
    meta_path(csv_path).write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def _run_for(cmd: list[str], seconds: float) -> tuple[bool, str]:
    """Run cmd for `seconds`, then stop it the way Ctrl+C would.

    Returns (stopped_by_us, stderr). stopped_by_us is False if the program
    exited on its own first, which for hackrf_sweep means an error.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, stderr = proc.communicate(timeout=seconds)
        return False, stderr
    except subprocess.TimeoutExpired:
        # SIGINT, not kill: hackrf_sweep catches it and closes the CSV cleanly.
        proc.send_signal(signal.SIGINT)
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
        return True, stderr
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=10)
        raise


def read_metadata(csv_path: Path) -> dict[str, Any]:
    """A capture's record, normalized so older formats read like the current one.

    Records written before the requested/radio/measured split kept a single
    "settings" block and a "created" time. For a CSV with no record at all,
    only the name is known. Either way, "measured" can always be rebuilt from
    the CSV, which is what `rfscoper info` does.
    """
    for candidate in (meta_path(csv_path), csv_path.with_suffix(".json")):
        if candidate.exists():
            meta: dict[str, Any] = json.loads(candidate.read_text())
            break
    else:
        meta = {"name": csv_path.stem, "note": ""}

    if "requested" not in meta:  # older record
        legacy = dict(meta.get("settings") or {})
        if legacy.get("duration_s") is not None:
            legacy["sweeps"] = None  # it was recorded but never applied
        meta["requested"] = legacy
        meta["legacy_format"] = True
        # Older records wrote "created" after hackrf_sweep exited: it is the
        # finish time, not the start. The start can be read from the CSV.
        if meta.get("created"):
            meta.setdefault("finished", meta["created"])
    meta.setdefault("started", "")
    meta.setdefault("note", "")
    meta["path"] = str(csv_path)
    return meta


def list_captures(directory: Path = DEFAULT_DIR) -> list[dict[str, Any]]:
    """Every capture in the folder, oldest first, with its metadata."""
    captures = [read_metadata(path) for path in Path(directory).glob("*.csv")]
    return sorted(captures, key=lambda meta: (taken(meta), meta["name"]))


def taken(meta: dict[str, Any]) -> str:
    """When a capture was taken: its start time, or its finish time for older records."""
    return str(meta.get("started") or meta.get("finished") or "")


def resolve(name_or_path: str, directory: Path = DEFAULT_DIR) -> Path:
    """Accept either a capture name ('hallway') or a path to a CSV file."""
    path = Path(name_or_path)
    if path.suffix == ".csv" and path.exists():
        return path
    candidate = Path(directory) / f"{name_or_path}.csv"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"No capture named '{name_or_path}' in {directory}/")
