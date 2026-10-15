import json
import stat
import sys

import pytest
from conftest import make_csv

from rfscoper import probe
from rfscoper.cli import main
from rfscoper.parser import load_sweeps


def test_slugify():
    assert probe.slugify("Desk lamp (left)") == "desk-lamp-left"
    with pytest.raises(ValueError):
        probe.slugify("***")


def test_hotspots_find_the_object_with_the_transmitter(tmp_path):
    ambient = {462.65: (-50.0, 1)}  # far away: same everywhere
    reference = load_sweeps(make_csv(tmp_path / "ref.csv", 400, 480, 100_000, 10, ambient))
    lamp = load_sweeps(make_csv(tmp_path / "lamp.csv", 400, 480, 100_000, 10, ambient, seed=1))
    strip = load_sweeps(
        make_csv(
            tmp_path / "strip.csv", 400, 480, 100_000, 10, {**ambient, 433.9: (-25.0, 2)}, seed=2
        )
    )
    results = probe.hotspots(reference, {"lamp": lamp, "power strip": strip})

    by_freq = {round(h.peak.center_hz / 1e5): h for h in results}
    bug = by_freq[4339]
    assert bug.found_at == "power strip"
    assert bug.gains_db["power strip"] > 30
    assert abs(bug.gains_db["lamp"]) < 5
    assert bug.verdict.startswith("HOT at power strip")

    tower = by_freq[4626]
    assert all(abs(g) < 5 for g in tower.gains_db.values())
    assert tower.verdict.startswith("same everywhere")
    assert results[0] is bug  # hottest first


def test_hotspots_with_no_spots(tmp_path):
    reference = load_sweeps(make_csv(tmp_path / "ref.csv", signals={96.5: (-40.0, 1)}))
    (only,) = probe.hotspots(reference, {})
    assert only.gains_db == {} and only.verdict == "no spots yet"


def test_live_meter_one_reading_per_sweep():
    meter = probe.LiveMeter(watch_hz=90_500_000)
    rows = [
        "2026-10-14, 08:00:00, 88000000, 93000000, 1000000.00, 20, -70, -70, -40, -70, -70",
        "2026-10-14, 08:00:00, 93000000, 98000000, 1000000.00, 20, -70, -30, -70, -70, -70",
        "2026-10-14, 08:00:01, 88000000, 93000000, 1000000.00, 20, -70, -70, -20, -70, -70",
    ]
    readings = [meter.push(r) for r in rows]
    assert readings[0] is None and readings[1] is None  # sweep 1 still in progress
    first = readings[2]
    assert first.sweep == 1
    assert (first.peak_db, first.peak_hz) == (-30.0, 94_500_000)
    assert first.watch_db == -40.0  # the 90.5 MHz bin


def test_meter_bar():
    assert probe.meter_bar(-70, -70, width=10) == "----------"
    assert probe.meter_bar(-45, -70, width=10) == "#####-----"
    assert probe.meter_bar(0, -70, width=10) == "##########"


# ------------------------------------------------------------- CLI, end to end


@pytest.fixture
def fake_probe_hackrf(tmp_path):
    """hackrf_sweep stand-in: a 433.9 MHz signal only when PROBE_HOT is set, streams to stdout."""
    script = tmp_path / "fake_hackrf"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys, time, random\n"
        "a = sys.argv; target = a[a.index('-r') + 1]\n"
        "out = sys.stdout if target == '-' else open(target, 'w')\n"
        "lo, hi = (int(x) for x in a[a.index('-f') + 1].split(':'))\n"
        "n = 20_000_000 // int(a[a.index('-w') + 1])\n"
        "while (n + 4) % 8: n += 1\n"
        "hot = os.environ.get('PROBE_HOT') == '1'\n"
        "stop = []\n"
        "signal.signal(signal.SIGINT, lambda *_: stop.append(1))\n"
        "while not stop:\n"
        "    for low in range(lo * 10**6, hi * 10**6, 5_000_000):\n"
        "        if stop: break\n"
        "        bw = 20e6 / n\n"
        "        vals = []\n"
        "        for i in range(n // 4):\n"
        "            f = low + (i + 0.5) * bw\n"
        "            v = -70 + random.gauss(0, 1)\n"
        "            if abs(f - 433.9e6) < bw: v = -25 if hot else -58\n"
        "            vals.append(f'{v:.2f}')\n"
        "        head = f'2026-10-14, 08:00:00.000000, {low}, {low + 5_000_000}, {bw:.2f}, {n}, '\n"
        "        out.write(head + ', '.join(vals) + '\\n')\n"
        "        out.flush()\n"
        "        time.sleep(0.002)\n"
        "if out is not sys.stdout: out.close()\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_probe_session_end_to_end(tmp_path, fake_probe_hackrf, capsys, monkeypatch):
    exe = ["--executable", fake_probe_hackrf]
    common = ["--dir", str(tmp_path), "probe"]
    start = [*common, "start", "office", "--range", "400:480", "--duration", "0.3", *exe]
    assert main(start) == 0
    assert main([*common, "spot", "office", "desk lamp", *exe]) == 0
    out = capsys.readouterr().out
    assert "nothing stronger here" in out

    monkeypatch.setenv("PROBE_HOT", "1")
    assert main([*common, "spot", "office", "power strip", *exe]) == 0
    out = capsys.readouterr().out
    assert "433.900 MHz" in out and "+3" in out  # +30-something dB

    assert main([*common, "report", "office", "--json", "--no-plot"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [s["label"] for s in report["spots"]] == ["desk lamp", "power strip"]
    bug = report["signals"][0]
    assert bug["verdict"].startswith("HOT at power strip")
    assert bug["likely"][0]["id"] == "ism_433"

    assert main([*common, "report", "office", "--out", str(tmp_path / "r.png")]) == 0
    assert (tmp_path / "r.png").exists()
    assert "1 hot signal(s)" in capsys.readouterr().out

    assert main([*common, "list"]) == 0
    assert "office" in capsys.readouterr().out


def test_probe_live_streams_readings(tmp_path, fake_probe_hackrf, capsys, monkeypatch):
    monkeypatch.setenv("PROBE_HOT", "1")
    args = [
        "probe",
        "live",
        "--range",
        "430:440",
        "--watch",
        "433.9M",
        "--seconds",
        "0.5",
        "--executable",
        fake_probe_hackrf,
    ]
    assert main(args) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "433.900 MHz" in ln]
    assert len(lines) >= 2  # several sweeps in half a second
    assert all("#" in ln for ln in lines)  # the bar is up: the signal is strong


def test_probe_live_dry_run(capsys):
    assert main(["probe", "live", "--watch", "433.92M", "--dry-run"]) == 0
    cmd = capsys.readouterr().out
    assert "-f 420:440" in cmd  # narrowed to the 20 MHz step around 433.92
    assert "-r -" in cmd and "-N" not in cmd


def test_spot_requires_a_session(tmp_path, capsys):
    assert main(["--dir", str(tmp_path), "probe", "spot", "ghost", "lamp"]) == 1
    assert "No probe session" in capsys.readouterr().err


def test_probe_start_again_asks_then_numbers_or_replaces(
    tmp_path, fake_probe_hackrf, capsys, monkeypatch
):
    from rfscoper import cli

    exe = ["--executable", fake_probe_hackrf]
    start = ["--dir", str(tmp_path), "probe", "start", "office", "--range", "400:480"]
    start += ["--duration", "0.3", *exe]
    assert main(start) == 0
    assert main(["--dir", str(tmp_path), "probe", "spot", "office", "lamp", *exe]) == 0

    monkeypatch.setattr(cli, "confirm", lambda q: False)  # keep the old session
    assert main(start) == 0
    assert "Saving this one as 'office-capture2'" in capsys.readouterr().out
    assert len(probe.Session.load(probe.session_dir("office", tmp_path)).spots) == 1
    assert probe.session_dir("office-capture2", tmp_path).exists()

    monkeypatch.setattr(cli, "confirm", lambda q: True)  # start over
    assert main(start) == 0
    assert probe.Session.load(probe.session_dir("office", tmp_path)).spots == []
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".replaced")]
