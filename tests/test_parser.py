import numpy as np
import pytest

from rfscoper.parser import load_sweeps, parse_row

# Two sweeps over 88-98 MHz, each split into two 5 MHz rows with 1 MHz bins.
# The second sweep's rows arrive in reverse order, like real hackrf_sweep output can.
SAMPLE = """\
2026-10-13, 21:04:11, 88000000, 93000000, 1000000.00, 20, -42.1, -38.5, -55.0, -60.2, -61.7
2026-10-13, 21:04:11, 93000000, 98000000, 1000000.00, 20, -70.0, -71.0, -72.0, -73.0, -74.0
2026-10-13, 21:04:12, 93000000, 98000000, 1000000.00, 20, -69.0, -70.0, -71.0, -72.0, -73.0
2026-10-13, 21:04:12, 88000000, 93000000, 1000000.00, 20, -40.0, -39.0, -54.0, -59.0, -60.0
"""


@pytest.fixture
def sample_csv(tmp_path):
    path = tmp_path / "sweep.csv"
    path.write_text(SAMPLE)
    return path


def test_parse_row_bin_centers():
    row = parse_row(SAMPLE.splitlines()[0].split(","))
    assert row.hz_low == 88_000_000
    assert row.freqs_hz[1] == 89_500_000  # 88 MHz + 1.5 x 1 MHz
    assert row.powers_db[1] == -38.5


def test_parse_row_rejects_short_and_bad_rows():
    assert parse_row(["2026-10-13", "21:04:11"]) is None
    assert parse_row(["date", "time", "low", "high", "width", "n", "db"]) is None


def test_load_sweeps_shape_and_order(sample_csv):
    data = load_sweeps(sample_csv)
    assert data.n_sweeps == 2
    assert data.n_bins == 10
    assert list(data.freqs_hz) == sorted(data.freqs_hz)  # frequency axis is sorted
    assert data.freqs_hz[0] == 88_500_000
    assert data.timestamps == ["2026-10-13 21:04:11", "2026-10-13 21:04:12"]


def test_load_sweeps_values(sample_csv):
    data = load_sweeps(sample_csv)
    col = int(np.where(data.freqs_hz == 89_500_000)[0][0])
    assert data.power_db[0, col] == -38.5
    assert data.power_db[1, col] == -39.0


def test_missing_bins_are_nan(tmp_path):
    path = tmp_path / "partial.csv"
    lines = SAMPLE.splitlines()
    # Drop the 93-98 MHz row from the second sweep.
    path.write_text("\n".join([lines[0], lines[1], lines[3]]) + "\n")
    data = load_sweeps(path)
    col = int(np.where(data.freqs_hz == 95_500_000)[0][0])
    assert data.power_db[0, col] == -72.0
    assert np.isnan(data.power_db[1, col])


def test_empty_file_raises(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    with pytest.raises(ValueError):
        load_sweeps(path)


REAL_ROW = (
    "2026-10-14, 08:10:39.335650, 2455000000, 2460000000, 238095.24, 84, "
    "-70.43, -65.33, -61.37, -60.09, -64.85, -69.12, -65.12, -63.81, -63.09, -88.02, "
    "-63.18, -65.57, -71.09, -70.48, -76.19, -67.03, -61.47, -63.67, -74.31, -66.66, -65.90"
)


def test_real_row_matches_hackrf_sweep_rules():
    row = parse_row(REAL_ROW.split(","))
    assert row.fft_size == 84
    assert row.bin_hz == 238095.24
    assert len(row.powers_db) == row.fft_size // 4  # 21 values per row
    assert row.freqs_hz[3] == round(2_455_000_000 + 3.5 * 238095.24)


def test_capture_facts(sample_csv):
    data = load_sweeps(sample_csv)
    assert data.bin_hz == 1_000_000
    assert data.fft_size == 20
    assert data.rows == 4
    assert data.end_timestamp == "2026-10-13 21:04:12"
    assert data.duration_s == 1.0
    assert data.last_sweep_complete
    assert data.complete_sweeps == 2


def test_capture_stopped_mid_sweep(tmp_path):
    lines = SAMPLE.splitlines()
    path = tmp_path / "cut.csv"
    path.write_text("\n".join([*lines, lines[0]]) + "\n")  # a third sweep that only got 1 of 2 rows
    data = load_sweeps(path)
    assert data.n_sweeps == 3
    assert not data.last_sweep_complete
    assert data.complete_sweeps == 2


def test_microsecond_timestamps(tmp_path):
    path = tmp_path / "us.csv"
    path.write_text(REAL_ROW + "\n" + REAL_ROW.replace("08:10:39.335650", "08:10:40.835650") + "\n")
    assert load_sweeps(path).duration_s == 1.5
