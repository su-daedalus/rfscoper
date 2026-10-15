"""Use rfscoper as a library: load a capture, find signals, guess what they are.

python examples/make_demo_data.py
python examples/quickstart.py ~/rfscoper-captures/demo-2g4.csv
"""

from __future__ import annotations

import sys

from rfscoper import load_sweeps
from rfscoper.analysis import bin_width, duty_cycles, find_peaks, signal_floor, spectrum
from rfscoper.capture import DEFAULT_DIR
from rfscoper.identify import identify, strength

path = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_DIR / "demo-2g4.csv")
data = load_sweeps(path)  # a matrix: one row per sweep, one column per frequency bin
print(f"{path}: {data.n_sweeps} sweeps x {data.n_bins} bins\n")

peak = spectrum(data, "peak")  # strongest value each bin ever reached
floor = signal_floor(data, "peak")  # the noise underneath, per bin
signals = find_peaks(data.freqs_hz, peak, floor, threshold_db=10)
duty = duty_cycles(data, signals)  # share of sweeps each signal was on
step = bin_width(data.freqs_hz)

for signal, on in zip(signals, duty, strict=True):
    guesses = identify(signal.center_hz, signal.width_hz, step, on)
    best = guesses[0] if guesses else None
    what = f"{best.kind.name} ({best.label})" if best else "unknown"
    print(
        f"{signal.center_hz / 1e6:9.3f} MHz  {signal.width_hz / 1e6:5.1f} MHz wide  "
        f"{strength(signal.margin_db):<8}  on {on:4.0%}  {what}"
    )
