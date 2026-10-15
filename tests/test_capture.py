import json
import os
import stat
import sys
import time

import pytest

from rfscoper import capture
from rfscoper.parser import load_sweeps


@pytest.fixture
def fake_hackrf(tmp_path):
    """A stand-in for hackrf_sweep that writes a tiny CSV wherever -r points."""
    script = tmp_path / "fake_hackrf_sweep"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "out = sys.argv[sys.argv.index('-r') + 1]\n"
        "row = '2026-10-13, 21:00:00, 88000000, 93000000, 1000000.00, 20, '\n"
        "row += '-40, -50, -60, -70, -80'\n"
        "open(out, 'w').write(row + '\\n')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_command_matches_hackrf_sweep_flags():
    settings = capture.CaptureSettings(start_mhz=88, stop_mhz=108, bin_hz=100_000, sweeps=5)
    cmd = settings.command("out.csv")
    assert cmd[:3] == ["hackrf_sweep", "-f", "88:108"]
    assert cmd[cmd.index("-w") + 1] == "100000"
    assert cmd[cmd.index("-N") + 1] == "5"
    assert "-a" not in cmd


def test_run_capture_saves_csv_and_metadata(tmp_path, fake_hackrf):
    settings = capture.CaptureSettings(start_mhz=88, stop_mhz=93)
    path = capture.run_capture(
        "hallway", settings, tmp_path / "caps", note="baseline", executable=fake_hackrf
    )
    meta = json.loads(capture.meta_path(path).read_text())
    assert path.exists()
    assert capture.meta_path(path).name == "hallway.meta.json"
    assert meta["note"] == "baseline"
    assert meta["requested"]["start_mhz"] == 88
    assert meta["command"].startswith(fake_hackrf)  # reproducible: the exact command


def test_old_style_metadata_is_still_read(tmp_path):
    (tmp_path / "old.csv").write_text("")
    (tmp_path / "old.json").write_text('{"name": "old", "created": "x", "note": "v0.2"}')
    assert capture.list_captures(tmp_path)[0]["note"] == "v0.2"


def test_run_capture_refuses_to_overwrite_unless_asked(tmp_path, fake_hackrf):
    settings = capture.CaptureSettings(start_mhz=88, stop_mhz=93)
    capture.run_capture("room", settings, tmp_path, executable=fake_hackrf)
    with pytest.raises(FileExistsError):
        capture.run_capture("room", settings, tmp_path, executable=fake_hackrf)


@pytest.mark.parametrize("name", ["../escape", "my room", ""])
def test_bad_names_are_rejected(tmp_path, fake_hackrf, name):
    with pytest.raises(ValueError):
        capture.run_capture(name, capture.CaptureSettings(), tmp_path, executable=fake_hackrf)


def test_missing_hackrf_sweep_gives_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        capture.run_capture(
            "x", capture.CaptureSettings(), tmp_path, executable="definitely-not-installed"
        )


def test_list_and_resolve(tmp_path, fake_hackrf):
    settings = capture.CaptureSettings(start_mhz=88, stop_mhz=93)
    capture.run_capture("hallway", settings, tmp_path, executable=fake_hackrf)
    assert [m["name"] for m in capture.list_captures(tmp_path)] == ["hallway"]
    assert capture.resolve("hallway", tmp_path).name == "hallway.csv"
    with pytest.raises(FileNotFoundError):
        capture.resolve("nope", tmp_path)


@pytest.fixture
def fake_streaming_hackrf(tmp_path):
    """A stand-in that sweeps forever, like hackrf_sweep without -N, until Ctrl+C."""
    script = tmp_path / "fake_streaming_hackrf"
    script.write_text(
        f"#!{sys.executable}\n"
        "import signal, sys, time\n"
        "out = open(sys.argv[sys.argv.index('-r') + 1], 'w')\n"
        "stop = []\n"
        "signal.signal(signal.SIGINT, lambda *_: stop.append(1))\n"
        "while not stop:\n"
        "    for low in (88000000, 93000000):\n"
        "        out.write(f'2026-10-13, 21:00:00, {low}, {low + 5000000}, 1000000.00, 20, '\n"
        "                  '-40, -50, -60, -70, -80\\n')\n"
        "    out.flush()\n"
        "    time.sleep(0.05)\n"
        "out.close()\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_timed_capture_runs_for_the_duration(tmp_path, fake_streaming_hackrf):
    settings = capture.CaptureSettings(start_mhz=88, stop_mhz=98, duration_s=0.5)
    started = time.monotonic()
    path = capture.run_capture("timed", settings, tmp_path, executable=fake_streaming_hackrf)
    elapsed = time.monotonic() - started

    assert 0.5 <= elapsed < 5
    assert load_sweeps(path).n_sweeps > 2  # it kept sweeping until we stopped it
    meta = json.loads(capture.meta_path(path).read_text())
    assert meta["requested"]["duration_s"] == 0.5


def test_timed_capture_has_no_sweep_count():
    cmd = capture.CaptureSettings(duration_s=20).command("out.csv")
    assert "-N" not in cmd


def test_timed_capture_that_exits_early_is_an_error(tmp_path, fake_hackrf):
    # fake_hackrf writes one row and exits on its own: for a timed capture, that's a failure
    settings = capture.CaptureSettings(start_mhz=88, stop_mhz=93, duration_s=5)
    with pytest.raises(RuntimeError):
        capture.run_capture("early", settings, tmp_path, executable=fake_hackrf)


@pytest.mark.parametrize(
    "requested, fft, actual",
    [
        (250_000, 84, 238_095.24),  # your 2.4 GHz capture
        (100_000, 204, 98_039.22),  # your first FM capture: 204 bins over 20 MHz
        (1_000_000, 20, 1_000_000.0),  # exact
        (200_000, 100, 200_000.0),  # exact
        (5_000_000, 4, 5_000_000.0),  # widest allowed
        (2445, 8180, 2444.99),  # narrowest allowed
    ],
)
def test_bin_width_follows_hackrf_sweep(requested, fft, actual):
    radio = capture.CaptureSettings(start_mhz=2400, stop_mhz=2500, bin_hz=requested).radio()
    assert radio.fft_size == fft
    assert (fft + 4) % 8 == 0
    assert radio.bin_hz == actual


@pytest.mark.parametrize("bad", [6_000_000, 2000])
def test_bin_width_limits(bad):
    with pytest.raises(ValueError, match="bin width"):
        capture.CaptureSettings(bin_hz=bad).validate()


@pytest.mark.parametrize(
    "start, stop, covered",
    [(2400, 2500, 2500), (88, 108, 108), (2400, 2490, 2500), (1, 6000, 6001), (433, 435, 453)],
)
def test_range_is_rounded_to_20_mhz_steps(start, stop, covered):
    assert capture.CaptureSettings(start_mhz=start, stop_mhz=stop).radio().stop_mhz == covered


def test_gains_round_down_like_libhackrf():
    radio = capture.CaptureSettings(lna_db=30, vga_db=21).radio()
    assert (radio.lna_db, radio.vga_db) == (24, 20)


def test_adjustments_explain_every_change():
    settings = capture.CaptureSettings(start_mhz=2400, stop_mhz=2490, bin_hz=250_000, lna_db=30)
    notes = " | ".join(settings.adjustments())
    assert "238,095.24 Hz" in notes and "84" in notes
    assert "2400-2500 MHz" in notes
    assert "LNA gain 30 -> 24" in notes
    exact = capture.CaptureSettings(start_mhz=2400, stop_mhz=2500, bin_hz=200_000)
    assert exact.adjustments() == []


def test_timed_capture_records_no_sweep_count(tmp_path, fake_streaming_hackrf):
    settings = capture.CaptureSettings(start_mhz=88, stop_mhz=98, duration_s=0.5)
    path = capture.run_capture("t", settings, tmp_path, executable=fake_streaming_hackrf)
    meta = json.loads(capture.meta_path(path).read_text())
    assert meta["requested"]["sweeps"] is None
    assert meta["requested"]["duration_s"] == 0.5
    assert meta["measured"]["sweeps"] >= 2  # what actually happened
    assert meta["measured"]["bin_hz"] == 1_000_000
    assert meta["started"] < meta["finished"]
    assert meta["started"][-6] in "+-"  # carries its UTC offset


def test_record_flags_disagreement_with_the_file(tmp_path, fake_hackrf):
    # fake_hackrf writes only 88-93 MHz, but hackrf_sweep would cover 88-108
    settings = capture.CaptureSettings(start_mhz=88, stop_mhz=93)
    path = capture.run_capture("x", settings, tmp_path, executable=fake_hackrf)
    meta = json.loads(capture.meta_path(path).read_text())
    assert meta["radio"]["stop_mhz"] == 108
    assert any("coverage" in w for w in meta["warnings"])


def test_old_records_are_normalized(tmp_path):
    (tmp_path / "ble.csv").write_text("")
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
    }
    (tmp_path / "ble.meta.json").write_text(json.dumps(old))
    meta = capture.read_metadata(tmp_path / "ble.csv")
    assert meta["requested"]["sweeps"] is None  # recorded as 30, but never applied
    assert meta["finished"] == "2026-10-14T08:10:44"  # old "created" was the end time
    assert meta["started"] == ""  # unknown from the record; the CSV has it
    assert capture.taken(meta) == "2026-10-14T08:10:44"
    assert meta["legacy_format"]


def test_limits_are_checked_before_rounding():
    # 20,000,000 // 2444 = 8183 > 8180: rejected, even though the bin is "almost" allowed
    with pytest.raises(ValueError, match="2445"):
        capture.CaptureSettings(bin_hz=2444).validate()
    capture.CaptureSettings(bin_hz=2446).validate()  # 8176, rounded up to 8180 afterwards
    assert capture.CaptureSettings(bin_hz=2446).radio().fft_size == 8180


def test_overwrite_replaces_the_capture_and_its_record(tmp_path, fake_hackrf):
    settings = capture.CaptureSettings(start_mhz=88, stop_mhz=93)
    path = capture.run_capture("room", settings, tmp_path, note="first", executable=fake_hackrf)
    path.with_suffix(".png").write_text("old plot")
    capture.run_capture(
        "room", settings, tmp_path, note="second", executable=fake_hackrf, overwrite=True
    )
    assert capture.read_metadata(path)["note"] == "second"
    assert not path.with_suffix(".png").exists()  # the old plot described the old capture
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".replaced")]


def test_failed_overwrite_keeps_the_old_capture(tmp_path, fake_hackrf):
    settings = capture.CaptureSettings(start_mhz=88, stop_mhz=93)
    path = capture.run_capture("room", settings, tmp_path, note="keep me", executable=fake_hackrf)
    before = path.read_text()
    broken = tmp_path / "broken_hackrf"
    broken.write_text(f"#!{sys.executable}\nimport sys\nsys.exit('no HackRF found')\n")
    broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(RuntimeError):
        capture.run_capture("room", settings, tmp_path, executable=str(broken), overwrite=True)
    assert path.read_text() == before
    assert capture.read_metadata(path)["note"] == "keep me"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".replaced")]


def test_free_name_continues_the_series(tmp_path):
    assert capture.free_name("room", tmp_path) == "room-capture2"
    (tmp_path / "room-capture2.csv").write_text("")
    assert capture.free_name("room", tmp_path) == "room-capture3"
    assert capture.free_name("room-capture2", tmp_path) == "room-capture3"


@pytest.fixture
def hackrf_on_path(tmp_path, fake_hackrf, monkeypatch):
    """Put the fake under the real name, so the rfscoper command finds it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hackrf_sweep").symlink_to(fake_hackrf)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def test_cli_asks_and_numbers_the_capture_when_told_no(tmp_path, hackrf_on_path, monkeypatch):
    from rfscoper import cli

    caps = tmp_path / "caps"
    run = ["--dir", str(caps), "capture", "room", "--range", "88:93"]
    assert cli.main([*run, "--note", "first"]) == 0
    asked = []
    monkeypatch.setattr(cli, "confirm", lambda q: asked.append(q) or False)
    assert cli.main([*run, "--note", "second"]) == 0
    assert cli.main([*run, "--note", "third"]) == 0
    assert "already exists" in asked[0] and "Overwrite" in asked[0]
    assert capture.read_metadata(caps / "room.csv")["note"] == "first"  # untouched
    assert capture.read_metadata(caps / "room-capture2.csv")["note"] == "second"
    assert capture.read_metadata(caps / "room-capture3.csv")["note"] == "third"

    monkeypatch.setattr(cli, "confirm", lambda q: True)
    assert cli.main([*run, "--note", "replaced"]) == 0
    assert capture.read_metadata(caps / "room.csv")["note"] == "replaced"
    assert not (caps / "room-capture4.csv").exists()


def test_cli_overwrite_flag_does_not_ask(tmp_path, hackrf_on_path, monkeypatch):
    from rfscoper import cli

    caps = tmp_path / "caps"
    run = ["--dir", str(caps), "capture", "room", "--range", "88:93"]
    assert cli.main(run) == 0
    monkeypatch.setattr(cli, "confirm", lambda q: pytest.fail("should not ask"))
    assert cli.main([*run, "--overwrite", "--note", "new"]) == 0
    assert capture.read_metadata(caps / "room.csv")["note"] == "new"


def test_captures_live_outside_the_project_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("RFSCOPER_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert capture.default_dir() == tmp_path / "rfscoper-captures"
    monkeypatch.setenv("RFSCOPER_DIR", "~/radio")
    assert capture.default_dir() == tmp_path / "radio"


def test_old_captures_folder_gets_a_move_hint(monkeypatch, tmp_path, capsys):
    from rfscoper import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "captures").mkdir()
    (tmp_path / "captures" / "room.csv").write_text("")
    new = tmp_path / "home-captures"
    monkeypatch.setattr(capture, "DEFAULT_DIR", new)
    cli.warn_about_old_captures(new)
    err = capsys.readouterr().err
    assert "old location" in err and f"mv captures/* {new}/" in err

    cli.warn_about_old_captures(tmp_path / "elsewhere")  # an explicit --dir: no hint
    assert capsys.readouterr().err == ""
