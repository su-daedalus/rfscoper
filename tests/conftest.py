import numpy as np
import pytest


def make_csv(
    path,
    start_mhz=88,
    stop_mhz=108,
    bin_hz=1_000_000,
    sweeps=10,
    signals=None,
    noise_db=-70.0,
    seed=0,
):
    """Write a fake hackrf_sweep CSV.

    signals maps a frequency in MHz to (power_db, every_n_sweeps): every_n=1
    is a constant transmitter, every_n=5 shows up in one sweep out of five.
    """
    rng = np.random.default_rng(seed)
    signals = signals or {}
    lines = []
    for s in range(sweeps):
        for low in range(start_mhz * 1_000_000, stop_mhz * 1_000_000, 5_000_000):
            values = []
            for i in range(5_000_000 // bin_hz):
                center = low + (i + 0.5) * bin_hz
                db = noise_db + rng.normal(0, 1)
                for mhz, (power, every_n) in signals.items():
                    if abs(center - mhz * 1e6) < bin_hz and s % every_n == 0:
                        db = power
                values.append(f"{db:.1f}")
            lines.append(
                f"2026-10-13, 21:00:{s:02d}, {low}, {low + 5_000_000}, "
                f"{bin_hz:.2f}, {20_000_000 // bin_hz}, " + ", ".join(values)
            )
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def csv_factory(tmp_path):
    def factory(name="capture", **kwargs):
        return make_csv(tmp_path / f"{name}.csv", **kwargs)

    return factory
