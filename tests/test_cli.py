import argparse
import json

import pytest

from rfscoper.cli import main, parse_hz, parse_range


@pytest.mark.parametrize(
    "text, hz", [("100k", 100_000), ("1M", 1_000_000), ("2.5m", 2_500_000), ("250000", 250_000)]
)
def test_parse_hz(text, hz):
    assert parse_hz(text) == hz


def test_parse_range():
    assert parse_range("88:108") == (88, 108)
    with pytest.raises(argparse.ArgumentTypeError):
        parse_range("88-108")


def test_show_prints_signals_and_saves_plot(tmp_path, csv_factory, capsys):
    csv_factory("room", signals={96.5: (-35.0, 1)})
    assert main(["--dir", str(tmp_path), "show", "room"]) == 0
    out = capsys.readouterr().out
    assert "96.500" in out
    assert (tmp_path / "room.png").exists()


def test_compare_two_finds_new_signal(tmp_path, csv_factory, capsys):
    csv_factory("hallway", signals={96.5: (-35.0, 1)})
    csv_factory("room", signals={96.5: (-35.0, 1), 104.5: (-40.0, 3)}, seed=1)
    assert main(["--dir", str(tmp_path), "compare", "hallway", "room"]) == 0
    out = capsys.readouterr().out
    assert "104.500" in out
    assert (tmp_path / "compare_hallway_vs_room.png").exists()


def test_compare_needs_two_captures(tmp_path, csv_factory, capsys):
    csv_factory("room")
    assert main(["--dir", str(tmp_path), "compare", "room"]) == 1
    assert "baseline" in capsys.readouterr().err


def test_show_json_is_clean_and_complete(tmp_path, csv_factory, capsys):
    csv_factory("room", signals={96.5: (-35.0, 1), 101.5: (-30.0, 5)})
    assert main(["--dir", str(tmp_path), "show", "room", "--json"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)  # stdout must be pure JSON
    assert "Plot saved" in captured.err  # status goes to stderr instead

    by_freq = {s["freq_hz"]: s for s in report["signals"]}
    assert by_freq[96_500_000]["band"] == "FM broadcast"
    assert by_freq[96_500_000]["duty_cycle"] == 1.0  # steady
    assert by_freq[101_500_000]["duty_cycle"] == 0.2  # 2 of 10 sweeps


def test_compare_json(tmp_path, csv_factory, capsys):
    csv_factory("hallway", signals={96.5: (-35.0, 1)})
    csv_factory("room", signals={96.5: (-35.0, 1), 104.5: (-40.0, 3)}, seed=1)
    args = ["--dir", str(tmp_path), "compare", "hallway", "room", "--json", "--no-plot"]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["baseline"] == "hallway"
    new = report["comparisons"][0]["new_signals"]
    assert [s["freq_hz"] for s in new] == [104_500_000]
    assert not (tmp_path / "compare_hallway_vs_room.png").exists()


def test_capture_dry_run_prints_command(tmp_path, capsys):
    args = ["--dir", str(tmp_path), "capture", "room", "--range", "88:108", "--dry-run"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith("hackrf_sweep -f 88:108")
    assert not list(tmp_path.iterdir())  # nothing was run or written


def test_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("rfscoper ")


def test_missing_capture_is_a_clean_error(tmp_path, capsys):
    assert main(["--dir", str(tmp_path), "show", "ghost"]) == 1
    assert "No capture named 'ghost'" in capsys.readouterr().err


def test_list_when_empty(tmp_path, capsys):
    assert main(["--dir", str(tmp_path), "list"]) == 0
    assert "No captures" in capsys.readouterr().out


def test_capture_dry_run_with_duration(tmp_path, capsys):
    args = ["--dir", str(tmp_path), "capture", "fm", "--range", "88:108", "--duration", "20"]
    assert main([*args, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "-N" not in out
    assert "stopped after 20 s" in out


def test_sweeps_and_duration_cannot_be_combined(tmp_path):
    with pytest.raises(SystemExit):
        main(["--dir", str(tmp_path), "capture", "fm", "--sweeps", "5", "--duration", "20"])


def test_weak_steady_signal_reads_as_on(tmp_path, csv_factory, capsys):
    # 12 dB above a -70 dB floor: detectable, but near the threshold. It must still read ~100% on.
    csv_factory("weak", signals={96.5: (-58.0, 1)})
    assert main(["--dir", str(tmp_path), "show", "weak", "--json", "--no-plot"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["signals"][0]["duty_cycle"] >= 0.9


def test_show_identifies_signals(tmp_path, csv_factory, capsys):
    csv_factory("fm", signals={96.5: (-35.0, 1)}, bin_hz=100_000, sweeps=20)
    assert main(["--dir", str(tmp_path), "show", "fm", "--no-plot", "--explain"]) == 0
    out = capsys.readouterr().out
    assert "FM radio station" in out
    assert "on an FM channel (96.5 MHz)" in out  # --explain shows the reasoning


def test_show_json_includes_identification(tmp_path, csv_factory, capsys):
    csv_factory("fm", signals={96.5: (-35.0, 1)}, bin_hz=100_000, sweeps=20)
    assert main(["--dir", str(tmp_path), "show", "fm", "--no-plot", "--json"]) == 0
    signal = json.loads(capsys.readouterr().out)["signals"][0]
    assert signal["likely"][0]["id"] == "fm_broadcast"
    assert signal["likely"][0]["label"] == "likely"
    assert signal["strength"] == "strong"
    assert signal["width_hz"] > 0


def test_catalog_search(capsys):
    assert main(["catalog", "bluetooth"]) == 0
    out = capsys.readouterr().out
    assert "ble_advertising" in out and "bluetooth_hopping" in out
    assert "fm_broadcast" not in out


def test_catalog_json(capsys):
    assert main(["catalog", "--json"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert catalog["ble_advertising"]["channels_mhz"] == [2402, 2426, 2480]


def test_identify_command(capsys):
    assert main(["identify", "2426", "--width", "1M", "--duty", "0.3", "--bin", "250k"]) == 0
    out = capsys.readouterr().out
    assert "Bluetooth Low Energy advertising" in out
    assert "on a BLE channel (2426 MHz)" in out


def _old_ble_capture(directory):
    """A capture recorded by an older rfscoper: 250 kHz asked, 238 kHz delivered."""
    row = "2026-10-14, 08:10:{s:02d}.000000, {low}, {high}, 238095.24, 84, " + ", ".join(
        ["-66.00"] * 21
    )
    lines = [
        row.format(s=s, low=low, high=low + 5_000_000)
        for s in range(3)
        for low in range(2_400_000_000, 2_500_000_000, 5_000_000)
    ]
    (directory / "ble.csv").write_text("\n".join(lines) + "\n")
    old = {
        "name": "ble",
        "created": "2026-10-14T08:10:44",
        "note": "",
        "settings": {
            "start_mhz": 2400,
            "stop_mhz": 2500,
            "bin_hz": 250000,
            "sweeps": 30,
            "lna_db": 24,
            "vga_db": 20,
            "amp": False,
            "duration_s": 60.0,
        },
        "command": "hackrf_sweep -f 2400:2500 -w 250000 -l 24 -g 20 -r captures/ble.csv",
    }
    (directory / "ble.meta.json").write_text(json.dumps(old))


def test_info_shows_requested_radio_and_file(tmp_path, capsys):
    _old_ble_capture(tmp_path)
    assert main(["--dir", str(tmp_path), "info", "ble"]) == 0
    out = capsys.readouterr().out
    assert "250,000.00 Hz" in out  # requested
    assert "250,000.00 Hz  238,095.24 Hz  238,095.24 Hz" in out  # requested, radio, file
    assert "until stopped" in out  # timed: no sweep count was applied
    assert "finished  2026-10-14T08:10:44" in out


def test_info_json(tmp_path, capsys):
    _old_ble_capture(tmp_path)
    assert main(["--dir", str(tmp_path), "info", "ble", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["requested"]["sweeps"] is None
    assert info["radio"]["fft_size"] == info["measured"]["fft_size"] == 84
    assert info["measured"]["complete_sweeps"] == 3
    assert info["warnings"] == []


def test_dry_run_explains_adjustments(tmp_path, capsys):
    args = ["--dir", str(tmp_path), "capture", "x", "--range", "2400:2490", "--bin", "250k"]
    assert main([*args, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "will cover 2400-2500 MHz with 238,095.24 Hz bins (FFT size 84)" in out
    assert "adjusted: range 2400-2490 MHz -> 2400-2500 MHz" in out


def test_closed_pipe_is_not_an_error(tmp_path):
    import subprocess
    import sys

    _old_ble_capture(tmp_path)
    cmd = f"{sys.executable} -m rfscoper --dir {tmp_path} info ble | head -1"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)
    assert "Broken pipe" not in result.stderr
