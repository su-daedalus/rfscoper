"""Turn a SweepData matrix into spectra, a noise floor, signals and differences."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

from rfscoper.parser import SweepData

STATS = ("peak", "mean")

FLOOR_SPAN_HZ = 60e6  # how much spectrum each noise-floor estimate looks at
FLOOR_PERCENTILE = 25.0  # low percentile: robust even when signals fill much of the window
TIME_SMOOTH_HZ = 4e6  # smoothing for the time-based floor
TIME_PERCENTILE = 10.0  # quietest sweeps of each bin: sees under anything on < ~90% of the time


@dataclass
class Peak:
    """A signal found in a spectrum.

    freq_hz is the strongest bin. start_hz and stop_hz are the edges of the
    run of bins above the threshold, so a wide signal (Wi-Fi) and a narrow one
    (a key fob) can be told apart.

    margin_db is how far it rises above its reference: the noise floor when
    found by find_peaks, or the baseline capture when found by new_signals.
    """

    freq_hz: int
    power_db: float
    margin_db: float
    start_hz: int = 0
    stop_hz: int = 0

    @property
    def width_hz(self) -> int:
        """Occupied bandwidth, as measured at the threshold."""
        return max(self.stop_hz - self.start_hz, 0)

    @property
    def center_hz(self) -> int:
        """Middle of the occupied band. For wide signals this beats freq_hz."""
        if self.width_hz == 0:
            return self.freq_hz
        return (self.start_hz + self.stop_hz) // 2


@dataclass
class Comparison:
    """Two spectra lined up on the same frequency axis."""

    freqs_hz: np.ndarray
    base: np.ndarray
    other: np.ndarray

    @property
    def diff(self) -> np.ndarray:
        """Positive where `other` is stronger than `base`."""
        return self.other - self.base


def bin_width(freqs_hz: np.ndarray) -> float:
    """Spacing between neighboring bins, in Hz."""
    if len(freqs_hz) < 2:
        return 0.0
    return float(np.median(np.diff(freqs_hz)))


def _collapse(func: Callable[..., np.ndarray], matrix: np.ndarray) -> np.ndarray:
    # A column that is all NaN (a bin no sweep ever measured) makes numpy
    # warn; the NaN result is what we want, so silence the warning.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return func(matrix, axis=0)


def spectrum(data: SweepData, stat: str = "peak") -> np.ndarray:
    """Collapse all sweeps into one value per frequency.

    peak: strongest value each bin ever reached (catches brief signals).
    mean: what each bin looks like most of the time.
    """
    if stat == "peak":
        return _collapse(np.nanmax, data.power_db)
    if stat == "mean":
        return _collapse(np.nanmean, data.power_db)
    raise ValueError(f"Unknown stat '{stat}', expected one of {STATS}")


def noise_floor(
    spec: np.ndarray, window: int = 101, percentile: float = FLOOR_PERCENTILE
) -> np.ndarray:
    """Estimate the noise floor around each bin from a rolling low percentile.

    A low percentile ignores the bins that signals occupy, so it follows the
    noise instead of the signals, even in a busy band where signals fill much
    of the window. Rolling (instead of one number for the whole range) lets it
    track the noise level as it drifts across a wide sweep.
    """
    window = max(1, min(window, len(spec)))
    if window % 2 == 0:
        window -= 1  # odd window keeps each bin centered
    half = window // 2
    padded = np.pad(spec, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, window)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanpercentile(windows, percentile, axis=1)


def estimate_floor(freqs_hz: np.ndarray, spec: np.ndarray) -> np.ndarray:
    """noise_floor with a window sized in Hz rather than in bins.

    A fixed number of bins would cover 100 MHz at 1 MHz bins but only 25 MHz
    at 250 kHz bins, and then a single Wi-Fi channel would fill the window.
    """
    step = bin_width(freqs_hz)
    bins = int(FLOOR_SPAN_HZ / step) if step > 0 else len(spec)
    return noise_floor(spec, window=max(31, bins))


def time_floor(data: SweepData, stat: str = "peak") -> np.ndarray:
    """Noise floor of each bin, estimated across time instead of frequency.

    For each bin, look at its quietest sweeps (the 10th percentile over time).
    Anything that transmits less than about 90% of the time leaves some sweeps
    with only noise in that bin, so this sees the noise underneath it. That
    includes hopping signals like Bluetooth, which visit each bin only now
    and then, and a busy Wi-Fi channel.

    The 10th percentile of noise sits about 1.28 standard deviations below
    its median, so that is added back. For peak hold the noise is lifted
    further (the maximum of N noisy readings sits above their median), by the
    expected maximum of N normal draws. The noise's standard deviation comes
    from all bins together, which keeps it steady when one bin is busy.
    """
    power = data.power_db
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        quiet = np.nanpercentile(power, TIME_PERCENTILE, axis=0)
        median = np.nanmedian(power, axis=0)
        sigma = 1.4826 * float(np.nanmedian(np.abs(power - median)))
    lift = -NormalDist().inv_cdf(TIME_PERCENTILE / 100)  # back up to the median
    if stat == "peak":
        count = max(int(np.median(np.sum(~np.isnan(power), axis=0))), 1)
        if count > 1:
            lift += NormalDist().inv_cdf(1 - 0.5 / count)
    # A few bins are busy almost all the time by bad luck (two signals taking
    # turns), and spike the estimate. The floor is noise, which changes slowly
    # with frequency, so a rolling median over a few MHz removes those spikes.
    step = bin_width(data.freqs_hz)
    smooth = max(3, int(TIME_SMOOTH_HZ / step)) if step > 0 else 3
    return noise_floor(quiet + lift * sigma, window=smooth, percentile=50.0)


def signal_floor(data: SweepData, stat: str = "peak") -> np.ndarray:
    """The noise floor rfscoper detects signals against.

    Two estimates, each blind in a different case:

    - Across frequency (estimate_floor) misses wide signals that fill most of
      the band, like hopping Bluetooth, because it takes them for noise.
    - Across time (time_floor) misses steady signals, because a bin that is
      always on has its median at the signal, not the noise.

    Taking the lower of the two at each bin covers both.
    """
    across_freq = estimate_floor(data.freqs_hz, spectrum(data, stat))
    return np.fmin(across_freq, time_floor(data, stat))


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, stop) indexes of each run of consecutive True values."""
    edges = np.diff(np.concatenate(([0], mask.astype(int), [0])))
    starts = np.flatnonzero(edges == 1)
    stops = np.flatnonzero(edges == -1)
    return [(int(a), int(b)) for a, b in zip(starts, stops, strict=True)]


def _edges(freqs_hz: np.ndarray, start: int, stop: int) -> tuple[int, int]:
    """Frequency edges of bins start..stop-1 (bin centers +/- half a bin)."""
    half = bin_width(freqs_hz) / 2
    return round(freqs_hz[start] - half), round(freqs_hz[stop - 1] + half)


def find_peaks(
    freqs_hz: np.ndarray,
    spec: np.ndarray,
    floor: np.ndarray,
    threshold_db: float = 10.0,
) -> list[Peak]:
    """Find signals that rise at least threshold_db above the noise floor.

    Neighboring bins above the threshold are treated as one signal (an FM
    station or a Wi-Fi channel spans several bins). Each is reported at its
    strongest bin, with the edges of the whole run.
    """
    margin = spec - floor
    mask = np.nan_to_num(margin, nan=-np.inf) >= threshold_db
    peaks = []
    for start, stop in _runs(mask):
        i = start + int(np.nanargmax(spec[start:stop]))
        low, high = _edges(freqs_hz, start, stop)
        peaks.append(Peak(int(freqs_hz[i]), float(spec[i]), float(margin[i]), low, high))
    return sorted(peaks, key=lambda p: p.power_db, reverse=True)


def duty_cycle(data: SweepData, freq_hz: int, level_db: float) -> float:
    """Fraction of sweeps in which the bin nearest freq_hz reached level_db.

    Close to 1.0 means a steady transmitter. A low value means something
    that comes and goes, which is often the more interesting kind.
    """
    i = int(np.argmin(np.abs(data.freqs_hz - freq_hz)))
    column = data.power_db[:, i]
    measured = column[~np.isnan(column)]
    if measured.size == 0:
        return 0.0
    return float(np.mean(measured >= level_db))


def duty_cycles(data: SweepData, peaks: list[Peak], on_level_db: float = 6.0) -> list[float]:
    """Duty cycle of each peak: how often it was on_level_db above the noise.

    "The noise" here is the floor of the *mean* spectrum, which is what a bin
    looks like in a single sweep when nothing is transmitting. Using the floor
    of the peak-hold spectrum instead would set the bar a few dB too high,
    since peak hold lifts the noise too.
    """
    mean_floor = signal_floor(data, "mean")
    result = []
    for p in peaks:
        i = int(np.argmin(np.abs(data.freqs_hz - p.freq_hz)))
        result.append(duty_cycle(data, p.freq_hz, float(mean_floor[i]) + on_level_db))
    return result


def align(
    freqs_a: np.ndarray,
    spec_a: np.ndarray,
    freqs_b: np.ndarray,
    spec_b: np.ndarray,
) -> Comparison:
    """Line spectrum B up onto spectrum A's frequency axis.

    Only the overlapping range is kept. B is interpolated onto A's bins,
    so two captures with slightly different bin widths still compare.
    """
    low = max(freqs_a[0], freqs_b[0])
    high = min(freqs_a[-1], freqs_b[-1])
    if low >= high:
        raise ValueError("These captures don't overlap in frequency")

    keep = (freqs_a >= low) & (freqs_a <= high)
    valid_b = ~np.isnan(spec_b)
    if not valid_b.any():
        raise ValueError("The second capture has no valid data")
    other = np.interp(freqs_a[keep], freqs_b[valid_b], spec_b[valid_b])
    return Comparison(freqs_a[keep], spec_a[keep], other)


def new_signals(comparison: Comparison, threshold_db: float = 10.0) -> list[Peak]:
    """Signals at least threshold_db stronger in `other` than in `base`."""
    diff = comparison.diff
    mask = np.nan_to_num(diff, nan=-np.inf) >= threshold_db
    found = []
    for start, stop in _runs(mask):
        i = start + int(np.nanargmax(diff[start:stop]))
        low, high = _edges(comparison.freqs_hz, start, stop)
        found.append(
            Peak(
                int(comparison.freqs_hz[i]),
                float(comparison.other[i]),
                float(diff[i]),
                low,
                high,
            )
        )
    return sorted(found, key=lambda p: p.margin_db, reverse=True)
