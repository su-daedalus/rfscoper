"""Near-field probing: find *where* a signal comes from.

A wide-area sweep says what is transmitting. A near-field probe, a small loop
antenna held a few centimeters from objects, says where. Close to a small
transmitter the field strength climbs steeply with every centimeter, while a
distant cell tower reads the same everywhere in the room. So a signal that is
20-40 dB stronger at one spot than at the reference is coming from that spot.

A probe session is a folder of captures:

    captures/probe-office/
        reference.csv, reference.meta.json      probe held in open air, mid-room
        01-desk-lamp.csv, ...                   probe held against each object
        session.json                            the spots, in order

Each spot is compared against the reference on a shared frequency axis, and
every signal found anywhere in the session gets a gain figure per spot.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from rfscoper import analysis, capture
from rfscoper.parser import SweepData, load_sweeps

SESSION_FILE = "session.json"
REFERENCE = "reference"

# How much stronger than the reference a signal must be at a spot before the
# spot counts as hot. 20 dB is a tenfold voltage ratio: a few centimeters of
# distance from a small transmitter, not a wobble in the measurement.
HOT_DB = 20.0
WARM_DB = 10.0


def slugify(label: str) -> str:
    """'Desk lamp (left)' -> 'desk-lamp-left', for file names."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    if not slug:
        raise ValueError("The spot label needs at least one letter or number")
    return slug


@dataclass
class Spot:
    number: int
    label: str
    csv: str  # file name inside the session folder
    note: str = ""

    @property
    def stem(self) -> str:
        return Path(self.csv).stem


@dataclass
class Session:
    name: str
    directory: Path
    settings: capture.CaptureSettings
    spots: list[Spot] = field(default_factory=list)
    created: str = ""

    @property
    def reference_csv(self) -> Path:
        return self.directory / f"{REFERENCE}.csv"

    def spot_csv(self, spot: Spot) -> Path:
        return self.directory / spot.csv

    def next_spot_file(self, label: str) -> str:
        return f"{len(self.spots) + 1:02d}-{slugify(label)}.csv"

    def save(self) -> None:
        payload = {
            "name": self.name,
            "created": self.created,
            "settings": self.settings.requested(),
            "spots": [spot.__dict__ for spot in self.spots],
        }
        (self.directory / SESSION_FILE).write_text(json.dumps(payload, indent=2) + "\n")

    @classmethod
    def load(cls, directory: Path) -> Session:
        path = Path(directory) / SESSION_FILE
        if not path.exists():
            raise FileNotFoundError(f"No probe session in {directory}/ (missing {SESSION_FILE})")
        raw = json.loads(path.read_text())
        req = raw["settings"]
        settings = capture.CaptureSettings(
            start_mhz=req["start_mhz"],
            stop_mhz=req["stop_mhz"],
            bin_hz=req["bin_hz"],
            sweeps=req.get("sweeps") or 30,
            lna_db=req["lna_db"],
            vga_db=req["vga_db"],
            amp=req["amp"],
            duration_s=req.get("duration_s"),
        )
        spots = [Spot(**spot) for spot in raw.get("spots", [])]
        return cls(raw["name"], Path(directory), settings, spots, raw.get("created", ""))


def session_dir(name: str, captures_dir: Path = capture.DEFAULT_DIR) -> Path:
    return Path(captures_dir) / f"probe-{name}"


# ----------------------------------------------------------------- analysis


@dataclass
class Hotspot:
    """One signal's behavior across the spots."""

    peak: analysis.Peak  # strongest sighting, with where it was found
    found_at: str  # spot label (or "reference") where it was strongest
    gains_db: dict[str, float]  # spot label -> dB above the reference
    reference_db: float  # level in the reference capture

    @property
    def hottest(self) -> tuple[str, float] | None:
        if not self.gains_db:
            return None
        label = max(self.gains_db, key=lambda k: self.gains_db[k])
        return label, self.gains_db[label]

    @property
    def verdict(self) -> str:
        best = self.hottest
        if best is None:
            return "no spots yet"
        label, gain = best
        if gain >= HOT_DB:
            return f"HOT at {label} (+{gain:.0f} dB)"
        if gain >= WARM_DB:
            return f"warm at {label} (+{gain:.0f} dB)"
        return "same everywhere: not from any probed spot"


def _spectrum_on(reference: SweepData, data: SweepData, stat: str) -> np.ndarray:
    """A capture's spectrum interpolated onto the reference's frequency axis."""
    comp = analysis.align(
        reference.freqs_hz,
        analysis.spectrum(reference, stat),
        data.freqs_hz,
        analysis.spectrum(data, stat),
    )
    full = np.full(reference.n_bins, np.nan)
    keep = (reference.freqs_hz >= comp.freqs_hz[0]) & (reference.freqs_hz <= comp.freqs_hz[-1])
    full[keep] = comp.other
    return full


def hotspots(
    reference: SweepData,
    spots: dict[str, SweepData],
    threshold_db: float = 10.0,
    stat: str = "peak",
) -> list[Hotspot]:
    """Every signal seen in the reference or any spot, with its gain per spot.

    Signals are detected on the *maximum across all captures*, so something
    that only shows up when the probe touches one object is still found. Each
    signal's level in each capture is the strongest bin inside its edges.
    """
    freqs = reference.freqs_hz
    ref_spec = analysis.spectrum(reference, stat)
    spot_specs = {label: _spectrum_on(reference, data, stat) for label, data in spots.items()}

    stack = np.vstack([ref_spec, *spot_specs.values()]) if spot_specs else ref_spec[None, :]
    with np.errstate(all="ignore"):
        envelope = np.nanmax(stack, axis=0)
    floor = analysis.estimate_floor(freqs, ref_spec)
    peaks = analysis.find_peaks(freqs, envelope, floor, threshold_db)

    results = []
    for peak in peaks:
        inside = (freqs >= peak.start_hz) & (freqs <= peak.stop_hz)
        with np.errstate(all="ignore"):
            ref_level = float(np.nanmax(ref_spec[inside]))
            levels = {label: float(np.nanmax(spec[inside])) for label, spec in spot_specs.items()}
        gains = {
            label: round(level - ref_level, 1)
            for label, level in levels.items()
            if not np.isnan(level)
        }
        strongest = max(levels, key=lambda k: levels[k], default=REFERENCE)
        found_at = strongest if levels and levels[strongest] > ref_level else REFERENCE
        results.append(Hotspot(peak, found_at, gains, ref_level))

    def rank(h: Hotspot) -> float:
        best = h.hottest
        return -(best[1] if best else -np.inf)

    return sorted(results, key=rank)


def load_session_data(session: Session) -> tuple[SweepData, dict[str, SweepData]]:
    reference = load_sweeps(session.reference_csv)
    spots = {spot.label: load_sweeps(session.spot_csv(spot)) for spot in session.spots}
    return reference, spots


# ------------------------------------------------------------- live meter


@dataclass
class Reading:
    """One sweep's worth of the live meter."""

    sweep: int
    peak_db: float
    peak_hz: int
    watch_db: float | None  # level at the watched frequency, if any


class LiveMeter:
    """Turns a stream of hackrf_sweep rows into one reading per sweep.

    Feed rows with `push`; it returns a Reading each time a sweep completes.
    Used by `rfscoper probe live`, which pipes hackrf_sweep's stdout here.
    """

    def __init__(self, watch_hz: float | None = None, watch_width_hz: float = 0.0) -> None:
        self.watch_hz = watch_hz
        self.watch_width_hz = watch_width_hz
        self.sweep = 0
        self._seen: set[int] = set()
        self._best = (-np.inf, 0)
        self._watch = -np.inf

    def push(self, line: str) -> Reading | None:
        from rfscoper.parser import parse_row

        row = parse_row(line.split(","))
        if row is None:
            return None
        reading = None
        if row.hz_low in self._seen:
            reading = self._finish()
        self._seen.add(row.hz_low)
        for freq, db in zip(row.freqs_hz, row.powers_db, strict=True):
            if db > self._best[0]:
                self._best = (db, freq)
            if self.watch_hz is not None:
                half = max(self.watch_width_hz, row.bin_hz) / 2
                if abs(freq - self.watch_hz) <= half:
                    self._watch = max(self._watch, db)
        return reading

    def _finish(self) -> Reading:
        self.sweep += 1
        reading = Reading(
            self.sweep,
            float(self._best[0]),
            int(self._best[1]),
            None if self.watch_hz is None else float(self._watch),
        )
        self._seen = set()
        self._best = (-np.inf, 0)
        self._watch = -np.inf
        return reading


def meter_bar(db: float, floor_db: float, width: int = 40, span_db: float = 50.0) -> str:
    """A text bar from the floor upward: '########------' style."""
    level = (db - floor_db) / span_db
    filled = round(min(max(level, 0.0), 1.0) * width)
    return "#" * filled + "-" * (width - filled)


def describe_session(session: Session) -> dict[str, Any]:
    return {
        "name": session.name,
        "directory": str(session.directory),
        "created": session.created,
        "settings": session.settings.requested(),
        "reference": session.reference_csv.exists(),
        "spots": [spot.__dict__ for spot in session.spots],
    }
