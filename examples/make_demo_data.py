"""Generate synthetic captures so rfscoper can be tried without a HackRF.

    python examples/make_demo_data.py
    rfscoper compare demo-hallway demo-room
    rfscoper show demo-2g4 --explain
    rfscoper show demo-2g4-headphones --explain

Four captures plus a near-field probe session, laid out exactly like real
hackrf_sweep output (bin widths from
real FFT sizes, fft_size / 4 values per row, realistic timestamps) and recorded
like real captures, but clearly marked as simulated in their notes:

demo-hallway, demo-room (400-480 MHz)
    A steady 200 kHz-wide transmitter at 462.65 MHz and a walkie-talkie on
    FRS channel 10 (467.6625 MHz).
    The room adds an intermittent 433.92 MHz signal, the kind of thing
    `rfscoper compare` is meant to surface.

demo-2g4 (2400-2500 MHz, 60 s)
    Wi-Fi on channel 6, Bluetooth Low Energy advertising on its three
    advertising channels, and a Zigbee device on channel 20.

demo-2g4-headphones (2400-2500 MHz, 60 s)
    The same, plus Classic Bluetooth audio hopping across the whole band.

probe-demo-office/ (300-2500 MHz, 10 s each)
    A reference in open air and four spots. A BLE device hides in the power
    strip and a 315 MHz transmitter in the smoke detector; the lamp and the
    picture frame are clean.
"""

from __future__ import annotations

import argparse
import json
import shlex
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from rfscoper import __version__, load_sweeps
from rfscoper.capture import (
    DEFAULT_DIR,
    SAMPLE_RATE_HZ,
    CaptureSettings,
    check,
    measure,
    meta_path,
)

ROW_HZ = 5_000_000  # hackrf_sweep writes one 5 MHz slice per CSV row


@dataclass
class Emitter:
    """A simulated transmitter."""

    center_mhz: float
    width_mhz: float
    power_db: float
    on_probability: float = 1.0  # chance it is caught in any one sweep
    hop_channels_mhz: tuple[float, ...] = ()  # if set: hops, lighting a few per sweep
    hops_per_sweep: int = 0


def render(
    path: Path,
    settings: CaptureSettings,
    emitters: list[Emitter],
    seed: int,
    t0: datetime,
    sweeps_per_s: float = 6.61,  # what a Raspberry Pi managed over 100 MHz
) -> None:
    """Write a CSV laid out exactly like hackrf_sweep output for these settings."""
    rng = np.random.default_rng(seed)
    radio = settings.radio()
    start_hz, stop_hz = radio.start_mhz * 1_000_000, radio.stop_mhz * 1_000_000
    per_row = radio.fft_size // 4  # hackrf_sweep prints fft_size / 4 values per row
    bin_hz = SAMPLE_RATE_HZ / radio.fft_size
    lows = list(range(start_hz, stop_hz, ROW_HZ))
    centers = np.array([low + (i + 0.5) * bin_hz for low in lows for i in range(per_row)])
    row_s = 1 / (sweeps_per_s * len(lows))

    if settings.duration_s is None:
        n_sweeps, last_rows = settings.sweeps, len(lows)
    else:  # runs until stopped, so the last sweep is usually cut short
        total_rows = int(settings.duration_s / row_s)
        n_sweeps, last_rows = total_rows // len(lows) + 1, total_rows % len(lows)
        if last_rows == 0:
            n_sweeps, last_rows = n_sweeps - 1, len(lows)

    lines = []
    for sweep in range(n_sweeps):
        # Noise floor that rises gently across the band, plus per-bin noise
        power = -72 + 3 * (centers - start_hz) / (stop_hz - start_hz)
        power = power + rng.normal(0, 1.2, centers.size)
        for e in emitters:
            if e.hop_channels_mhz:
                lit = rng.choice(e.hop_channels_mhz, size=e.hops_per_sweep, replace=False)
                spots = [(mhz, e.width_mhz) for mhz in lit]
            elif rng.random() < e.on_probability:
                spots = [(e.center_mhz, e.width_mhz)]
            else:
                spots = []
            for mhz, width in spots:
                # Each bin collects the share of the signal that overlaps it, so
                # edge bins are weaker, as they would be on a real receiver. A
                # signal narrower than a bin still lands whole in the bin that
                # contains it. Overlap is half-open, so a bin on the exact
                # boundary between two channels belongs to only one.
                width_hz = width * 1e6
                low_edge = np.maximum(centers - bin_hz / 2, mhz * 1e6 - width_hz / 2)
                high_edge = np.minimum(centers + bin_hz / 2, mhz * 1e6 + width_hz / 2)
                share = np.clip(high_edge - low_edge, 0, None) / min(width_hz, bin_hz)
                inside = share > 0.02
                level = e.power_db + 10 * np.log10(np.minimum(share[inside], 1.0))
                level = level + rng.normal(0, 0.5, inside.sum())
                power[inside] = np.maximum(power[inside], level)
        for i, low in enumerate(lows):
            if sweep == n_sweeps - 1 and i >= last_rows:
                break
            when = t0 + timedelta(seconds=(sweep * len(lows) + i) * row_s)
            values = ", ".join(f"{v:.2f}" for v in power[i * per_row : (i + 1) * per_row])
            lines.append(
                f"{when:%Y-%m-%d, %H:%M:%S.%f}, {low}, {low + ROW_HZ}, "
                f"{bin_hz:.2f}, {radio.fft_size}, {values}"
            )
    path.write_text("\n".join(lines) + "\n")


UHF_COMMON = [
    Emitter(462.65, 0.2, -48),  # steady and 200 kHz wide: not a walkie-talkie
    Emitter(467.6625, 0.0125, -55, on_probability=0.25),  # FRS channel 10
]
BLE_ADVERTISING = [Emitter(mhz, 1.0, -38, on_probability=0.15) for mhz in (2402, 2426, 2480)]
GHZ_COMMON = [
    Emitter(2437, 18, -50, on_probability=0.7),  # Wi-Fi, channel 6
    *BLE_ADVERTISING,
    Emitter(2450, 2.0, -52, on_probability=0.2),  # Zigbee, channel 20
]
CLASSIC_BT = Emitter(
    0,
    1.0,
    -42,
    hop_channels_mhz=tuple(range(2402, 2481)),  # 79 channels, 1 MHz apart
    # 1600 hops/s, but each 5 MHz slice is only watched for a few ms per sweep,
    # so a given channel is caught in roughly 1 sweep out of 10
    hops_per_sweep=8,
)

SCENARIOS = {
    # name: (what you'd have typed: range and --bin, emitters, note)
    "demo-hallway": (CaptureSettings(400, 480, 100_000), UHF_COMMON, "hallway baseline"),
    "demo-room": (
        CaptureSettings(400, 480, 100_000),
        [*UHF_COMMON, Emitter(433.92, 0.2, -41, 0.33)],
        "room",
    ),
    "demo-2g4": (
        CaptureSettings(2400, 2500, 250_000, duration_s=60),
        GHZ_COMMON,
        "2.4 GHz: Wi-Fi, BLE, Zigbee",
    ),
    "demo-2g4-headphones": (
        CaptureSettings(2400, 2500, 250_000, duration_s=60),
        [*GHZ_COMMON, CLASSIC_BT],
        "2.4 GHz with Bluetooth headphones playing",
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--sweeps", type=int, default=30, help="for the counted captures")
    args = parser.parse_args()
    args.dir.mkdir(parents=True, exist_ok=True)

    for seed, (name, (settings, emitters, note)) in enumerate(SCENARIOS.items(), 1):
        if settings.duration_s is None:
            settings.sweeps = args.sweeps
        write_capture(args.dir / f"{name}.csv", name, settings, emitters, note, seed)

    write_probe_session(args.dir / "probe-demo-office")

    print("\nTry: rfscoper show demo-2g4 --explain")
    print("     rfscoper probe report demo-office")


def write_capture(
    csv_path: Path,
    name: str,
    settings: CaptureSettings,
    emitters: list[Emitter],
    note: str,
    seed: int,
) -> None:
    """Render a capture and record it exactly as `rfscoper capture` would."""
    t0 = datetime(2026, 1, 1, 12, seed % 60, tzinfo=timezone.utc)
    render(csv_path, settings, emitters, seed, t0.replace(tzinfo=None))
    radio = asdict(settings.radio())
    measured = measure(load_sweeps(csv_path))
    t1 = t0 + timedelta(seconds=measured["duration_s"] or 0)
    record = {
        "name": name,
        "note": f"SIMULATED demo data: {note}",
        "started": t0.isoformat(timespec="milliseconds"),
        "finished": t1.isoformat(timespec="milliseconds"),
        "requested": settings.requested(),
        "radio": radio,
        "measured": measured,
        "warnings": check(radio, measured),
        "command": shlex.join(settings.command(csv_path)) + "   # simulated, not run",
        "rfscoper_version": __version__,
    }
    meta_path(csv_path).write_text(json.dumps(record, indent=2) + "\n")
    print(f"Wrote {csv_path}")


# A near-field probe session. The probe is a small loop held against objects,
# so only a transmitter inside the object gets much stronger; everything far
# away (the FM station, the cell tower) reads the same at every spot.
AMBIENT = [
    Emitter(433.92, 0.2, -58, on_probability=0.3),  # a neighbor's weather sensor, far away
    Emitter(462.65, 0.2, -52),  # steady, from somewhere else in the building
    Emitter(2437, 18, -56, on_probability=0.7),  # the house Wi-Fi
]
PROBE_SPOTS = {
    # label: emitters added when the probe touches this object
    "desk lamp": [],
    "power strip": [Emitter(2426, 1.0, -22, on_probability=0.4)],  # BLE, +34 dB over ambient
    "smoke detector": [Emitter(315.0, 0.3, -40, on_probability=0.15)],  # a 315 MHz burst
    "picture frame": [],
}


def write_probe_session(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    settings = CaptureSettings(300, 2500, 200_000, lna_db=8, duration_s=10)
    write_capture(
        directory / "reference.csv", "reference", settings, AMBIENT, "probe reference, open air", 11
    )
    spots = []
    for n, (label, extra) in enumerate(PROBE_SPOTS.items(), 1):
        csv_name = f"{n:02d}-{label.replace(' ', '-')}.csv"
        # BLE that is in the room also reaches the probe faintly at the other spots
        faint = [Emitter(2426, 1.0, -56, on_probability=0.4)]
        write_capture(
            directory / csv_name,
            label,
            settings,
            [*AMBIENT, *faint, *extra],
            f"probe at {label}",
            11 + n,
        )
        spots.append({"number": n, "label": label, "csv": csv_name, "note": ""})
    session = {
        "name": "demo-office",
        "created": "2026-01-01T12:11:00+00:00",
        "settings": settings.requested(),
        "spots": spots,
    }
    (directory / "session.json").write_text(json.dumps(session, indent=2) + "\n")


if __name__ == "__main__":
    main()
