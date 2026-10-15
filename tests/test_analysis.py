import numpy as np
import pytest

from rfscoper import analysis
from rfscoper.parser import load_sweeps


def test_peak_vs_mean_separates_constant_and_intermittent(csv_factory):
    path = csv_factory(signals={96.5: (-35.0, 1), 101.5: (-30.0, 5)})
    data = load_sweeps(path)
    peak = analysis.spectrum(data, "peak")
    mean = analysis.spectrum(data, "mean")
    fm = int(np.flatnonzero(data.freqs_hz == 96_500_000)[0])
    burst = int(np.flatnonzero(data.freqs_hz == 101_500_000)[0])

    assert peak[fm] == pytest.approx(mean[fm])  # always on: no gap
    assert peak[burst] - mean[burst] > 20  # comes and goes: big gap


def test_duty_cycle_measures_how_often_a_signal_is_on(csv_factory):
    data = load_sweeps(csv_factory(signals={96.5: (-35.0, 1), 101.5: (-30.0, 5)}))
    assert analysis.duty_cycle(data, 96_500_000, level_db=-50) == 1.0
    assert analysis.duty_cycle(data, 101_500_000, level_db=-50) == pytest.approx(0.2)
    assert analysis.duty_cycle(data, 90_500_000, level_db=-50) == 0.0  # only noise


def test_unknown_stat_is_rejected(csv_factory):
    with pytest.raises(ValueError):
        analysis.spectrum(load_sweeps(csv_factory()), "median")


def test_noise_floor_ignores_a_strong_signal():
    spec = np.full(50, -70.0)
    spec[25] = -20.0
    floor = analysis.noise_floor(spec, window=11)
    assert floor[25] == pytest.approx(-70.0)


def test_find_peaks_merges_neighboring_bins_into_one_signal():
    freqs = np.arange(10) * 100_000 + 88_000_000
    spec = np.array([-70, -70, -40, -35, -42, -70, -70, -50, -70, -70], dtype=float)
    floor = np.full(10, -70.0)
    peaks = analysis.find_peaks(freqs, spec, floor, threshold_db=10)

    assert [p.freq_hz for p in peaks] == [88_300_000, 88_700_000]  # strongest first
    assert peaks[0].margin_db == pytest.approx(35.0)


def test_align_interpolates_onto_the_first_grid():
    freqs_a = np.array([100, 200, 300, 400])
    freqs_b = np.array([150, 250, 350, 450])
    comp = analysis.align(freqs_a, np.zeros(4), freqs_b, np.array([0.0, 10, 20, 30]))
    assert list(comp.freqs_hz) == [200, 300, 400]  # only the overlap
    assert list(comp.other) == pytest.approx([5.0, 15.0, 25.0])


def test_align_rejects_captures_that_do_not_overlap():
    with pytest.raises(ValueError):
        analysis.align(np.array([1, 2]), np.zeros(2), np.array([5, 6]), np.zeros(2))


def test_new_signals_finds_what_appeared(csv_factory):
    hallway = load_sweeps(csv_factory("hallway", signals={96.5: (-35.0, 1)}))
    room = load_sweeps(csv_factory("room", signals={96.5: (-35.0, 1), 104.5: (-40.0, 3)}, seed=1))
    comp = analysis.align(
        hallway.freqs_hz,
        analysis.spectrum(hallway),
        room.freqs_hz,
        analysis.spectrum(room),
    )
    found = analysis.new_signals(comp, threshold_db=10)
    assert [p.freq_hz for p in found] == [104_500_000]  # the FM station is in both


def test_peaks_report_their_width_and_center():
    freqs = np.arange(20) * 100_000 + 88_000_000
    spec = np.full(20, -70.0)
    spec[5:9] = -40.0  # four bins wide
    spec[7] = -35.0
    peak = analysis.find_peaks(freqs, spec, np.full(20, -70.0), 10)[0]
    assert peak.freq_hz == freqs[7]  # strongest bin
    assert peak.width_hz == 400_000
    assert peak.center_hz == (freqs[5] + freqs[8]) // 2


def test_signal_floor_sees_under_a_hopping_carpet(csv_factory):
    # A signal that visits every bin of the band, but each only now and then,
    # fills the whole band in peak hold. The frequency-based floor alone would
    # take it for noise; the combined floor must not.
    data = load_sweeps(csv_factory(sweeps=30))
    rng = np.random.default_rng(3)
    hop = rng.random(data.power_db.shape) < 0.25
    data.power_db[hop] = -40.0
    peak = analysis.spectrum(data, "peak")

    freq_only = analysis.estimate_floor(data.freqs_hz, peak)
    combined = analysis.signal_floor(data, "peak")
    assert np.median(peak - freq_only) < 5  # blind
    assert np.median(peak - combined) > 20  # sees it


def test_signal_floor_keeps_steady_signals(csv_factory):
    data = load_sweeps(csv_factory(sweeps=30, signals={96.5: (-35.0, 1)}))
    peak = analysis.spectrum(data, "peak")
    floor = analysis.signal_floor(data, "peak")
    found = analysis.find_peaks(data.freqs_hz, peak, floor, 10)
    assert [p.freq_hz for p in found] == [96_500_000]
