"""Parse hackrf_sweep CSV output into a time-by-frequency power matrix.

Each hackrf_sweep row looks like:

    date, time, hz_low, hz_high, bin_width_hz, fft_size, dB, dB, dB, ...

hz_high - hz_low is always 5 MHz (a quarter of the 20 MHz sample rate),
bin_width_hz is 20 MHz / fft_size, and each row holds fft_size / 4 dB values.

One full pass over the requested range ("a sweep") is spread across many
rows, and the rows are not guaranteed to arrive in frequency order. This
module groups rows back into sweeps and builds a 2D array where each row
is one sweep and each column is one frequency bin.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np


@dataclass
class SweepRow:
    """One line of hackrf_sweep output."""

    timestamp: str
    hz_low: int
    freqs_hz: list[int]
    powers_db: list[float]
    bin_hz: float = 0.0
    fft_size: int = 0


@dataclass
class SweepData:
    """All sweeps from one capture, aligned on a shared frequency axis."""

    freqs_hz: np.ndarray  # shape (n_bins,)
    power_db: np.ndarray  # shape (n_sweeps, n_bins); NaN where a bin was missing
    timestamps: list[str]  # one per sweep (time of its first row)
    end_timestamp: str = ""  # time of the very last row
    bin_hz: float = 0.0  # bin width hackrf_sweep reported (column 5)
    fft_size: int = 0  # FFT size hackrf_sweep reported (column 6)
    rows: int = 0  # data rows read
    last_sweep_complete: bool = True  # False if the capture stopped mid-sweep

    @property
    def n_sweeps(self) -> int:
        return self.power_db.shape[0]

    @property
    def n_bins(self) -> int:
        return self.power_db.shape[1]

    @property
    def complete_sweeps(self) -> int:
        return self.n_sweeps - (0 if self.last_sweep_complete else 1)

    @property
    def duration_s(self) -> float | None:
        """Seconds from the first row to the last, or None if times can't be read."""
        try:
            first = datetime.fromisoformat(self.timestamps[0])
            last = datetime.fromisoformat(self.end_timestamp or self.timestamps[-1])
        except (ValueError, IndexError):
            return None
        return (last - first).total_seconds()


def parse_row(row: list[str]) -> SweepRow | None:
    """Turn one CSV row into a SweepRow, or None if it isn't a data row."""
    if len(row) < 7:
        return None
    try:
        hz_low = int(float(row[2]))
        bin_width = float(row[4])
        fft_size = int(float(row[5]))
        powers = [float(value) for value in row[6:]]
    except ValueError:
        return None  # header, truncated line, or garbage

    # Center of each bin, rounded to whole Hz so the same bin always
    # gets the same dictionary key (floats like 89500000.00000001 would not).
    freqs = [round(hz_low + (i + 0.5) * bin_width) for i in range(len(powers))]
    timestamp = f"{row[0].strip()} {row[1].strip()}"
    return SweepRow(timestamp, hz_low, freqs, powers, bin_width, fft_size)


def load_sweeps(path: str | Path) -> SweepData:
    """Read a hackrf_sweep CSV file and return it as SweepData.

    A new sweep starts when we see an hz_low we already saw in the
    current sweep: that means the radio has wrapped around to the start.
    """
    sweeps: list[dict[int, float]] = []
    slices_per_sweep: list[int] = []
    timestamps: list[str] = []
    current: dict[int, float] = {}
    seen_lows: set[int] = set()
    bin_widths: Counter[float] = Counter()
    fft_sizes: Counter[int] = Counter()
    rows = 0
    last_time = ""

    with open(path, newline="") as f:
        for raw in csv.reader(f):
            row = parse_row(raw)
            if row is None:
                continue
            rows += 1
            last_time = row.timestamp
            bin_widths[row.bin_hz] += 1
            fft_sizes[row.fft_size] += 1

            if row.hz_low in seen_lows:  # wrapped around: close this sweep
                sweeps.append(current)
                slices_per_sweep.append(len(seen_lows))
                current, seen_lows = {}, set()

            if not current:
                timestamps.append(row.timestamp)
            seen_lows.add(row.hz_low)
            current.update(zip(row.freqs_hz, row.powers_db, strict=True))

    if current:
        sweeps.append(current)
        slices_per_sweep.append(len(seen_lows))
    if not sweeps:
        raise ValueError(f"No sweep data found in {path}")

    all_freqs = sorted(set().union(*sweeps))
    column = {freq: i for i, freq in enumerate(all_freqs)}

    power = np.full((len(sweeps), len(all_freqs)), np.nan)
    for s, sweep in enumerate(sweeps):
        for freq, db in sweep.items():
            power[s, column[freq]] = db

    # A capture stopped mid-sweep (a timed capture, or Ctrl+C) ends with a
    # sweep that visited fewer slices than the others. Its missing bins are
    # already NaN; this flag just records that it happened.
    full = max(slices_per_sweep)
    last_complete = len(sweeps) == 1 or slices_per_sweep[-1] == full

    return SweepData(
        freqs_hz=np.array(all_freqs, dtype=np.int64),
        power_db=power,
        timestamps=timestamps,
        end_timestamp=last_time,
        bin_hz=bin_widths.most_common(1)[0][0],
        fft_size=fft_sizes.most_common(1)[0][0],
        rows=rows,
        last_sweep_complete=last_complete,
    )
