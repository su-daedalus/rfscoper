"""Draw captures and comparisons to PNG files."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no screen needed: works over SSH and on a headless Pi
import matplotlib.pyplot as plt
import numpy as np

from rfscoper.analysis import Comparison, Peak, signal_floor, spectrum
from rfscoper.parser import SweepData
from rfscoper.probe import HOT_DB, Hotspot

# Fixed series order: capture 1 is always blue, capture 2 always orange, and so on.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
MAX_SERIES = len(SERIES)
INK = "#0b0b0b"
INK_SOFT = "#52514e"
GRID = "#e4e3de"


def _style(ax) -> None:
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(INK_SOFT)
    ax.tick_params(colors=INK_SOFT, labelsize=9)
    ax.yaxis.label.set_color(INK_SOFT)
    ax.xaxis.label.set_color(INK_SOFT)


def _label_peaks(ax, peaks: list[Peak], labels: list[str] | None = None, limit: int = 6) -> None:
    labels = labels or [""] * len(peaks)
    for p, label in list(zip(peaks, labels, strict=False))[:limit]:
        x = p.freq_hz / 1e6
        ax.plot(x, p.power_db, "o", ms=5, mfc="white", mec=INK, mew=1)
        text = f"{x:.2f}\n{label}" if label else f"{x:.2f}"
        ax.annotate(
            text,
            (x, p.power_db),
            xytext=(0, 7),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8,
            color=INK,
            linespacing=1.1,
        )


def plot_capture(
    data: SweepData,
    name: str,
    out_path: Path,
    stat: str = "peak",
    peaks: list[Peak] | None = None,
    labels: list[str] | None = None,
) -> Path:
    """Spectrum with noise floor on top, waterfall (time vs frequency) below.

    labels, if given, holds a short name per peak (like "BLE") for the plot.
    """
    mhz = data.freqs_hz / 1e6
    spec = spectrum(data, stat)

    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [1, 1.2]}
    )
    top.plot(mhz, spec, color=SERIES[0], linewidth=1.2, label=f"{stat} power")
    top.plot(
        mhz,
        signal_floor(data, stat),
        color=INK_SOFT,
        linewidth=1,
        linestyle="--",
        label="noise floor",
    )
    if peaks:
        _label_peaks(top, peaks, labels)
        top.margins(y=0.18)  # headroom for the labels
    top.set_ylabel("Power (dB)")
    top.set_title(f"{name}  ({data.n_sweeps} sweeps)", color=INK, loc="left", fontsize=11)
    top.legend(frameon=False, fontsize=9, loc="upper right")
    _style(top)

    bottom.imshow(
        data.power_db,
        aspect="auto",
        cmap="Blues",
        interpolation="nearest",
        extent=[mhz[0], mhz[-1], data.n_sweeps, 0],
    )
    bottom.set_xlabel("Frequency (MHz)")
    bottom.set_ylabel("Sweep  (darker = stronger)")
    _style(bottom)
    bottom.grid(False)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return Path(out_path)


def plot_compare(
    series: list[tuple[str, np.ndarray, np.ndarray]],
    out_path: Path,
    stat: str = "peak",
    comparison: Comparison | None = None,
    new: list[Peak] | None = None,
    threshold_db: float = 10.0,
) -> Path:
    """Overlay several captures; with exactly two, add a difference panel."""
    if len(series) > MAX_SERIES:
        raise ValueError(f"Compare at most {MAX_SERIES} captures at a time")

    rows = 2 if comparison is not None else 1
    fig, axes = plt.subplots(rows, 1, figsize=(12, 4 + 3 * (rows - 1)), sharex=True, squeeze=False)
    top = axes[0, 0]
    for color, (name, freqs, spec) in zip(SERIES, series, strict=False):
        top.plot(freqs / 1e6, spec, color=color, linewidth=1.2, label=name)
    top.set_ylabel("Power (dB)")
    top.set_title(f"Comparison ({stat} power)", color=INK, loc="left", fontsize=11)
    top.legend(frameon=False, fontsize=9, loc="upper right")
    _style(top)

    if comparison is not None:
        base_name, other_name = series[0][0], series[1][0]
        ax = axes[1, 0]
        mhz = comparison.freqs_hz / 1e6
        diff = comparison.diff
        ax.fill_between(
            mhz,
            diff,
            0,
            where=diff > 0,
            color=SERIES[1],
            alpha=0.35,
            linewidth=0,
            label=f"stronger in {other_name}",
        )
        ax.fill_between(
            mhz,
            diff,
            0,
            where=diff < 0,
            color=SERIES[0],
            alpha=0.35,
            linewidth=0,
            label=f"stronger in {base_name}",
        )
        ax.plot(mhz, diff, color=INK_SOFT, linewidth=0.8)
        ax.axhline(0, color=INK_SOFT, linewidth=0.8)
        ax.axhline(threshold_db, color=INK_SOFT, linewidth=0.8, linestyle=":")
        for p in (new or [])[:5]:
            x = p.freq_hz / 1e6
            ax.annotate(
                f"{x:.2f}",
                (x, p.margin_db),
                xytext=(0, 6),
                textcoords="offset points",
                ha="center",
                fontsize=8,
                color=INK,
            )
        ax.set_ylabel(f"{other_name} minus {base_name} (dB)")
        ax.legend(frameon=False, fontsize=9, loc="upper right")
        _style(ax)

    axes[-1, 0].set_xlabel("Frequency (MHz)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return Path(out_path)


def plot_probe(results: list[Hotspot], labels: list[str], out_path: Path, name: str) -> Path:
    """Signals (rows) against spots (columns): how much hotter than the reference."""
    shown = results[:20]
    if not shown or not labels:
        raise ValueError("Nothing to plot: the session needs at least one spot and one signal")
    grid = np.array([[h.gains_db.get(lab, np.nan) for lab in labels] for h in shown])

    fig, ax = plt.subplots(figsize=(1.6 + 1.1 * len(labels), 1.2 + 0.42 * len(shown)))
    image = ax.imshow(
        grid, cmap="Oranges", vmin=0, vmax=max(HOT_DB * 2, np.nanmax(grid)), aspect="auto"
    )
    ax.set_xticks(range(len(labels)), labels, rotation=30, ha="right", fontsize=9)
    ax.set_yticks(
        range(len(shown)), [f"{h.peak.center_hz / 1e6:.3f} MHz" for h in shown], fontsize=9
    )
    for r, h in enumerate(shown):
        for c, lab in enumerate(labels):
            gain = h.gains_db.get(lab)
            if gain is None:
                continue
            ax.text(
                c,
                r,
                f"{gain:+.0f}".replace("-0", "0").replace("+0", "0"),
                ha="center",
                va="center",
                fontsize=8,
                color="white" if gain >= HOT_DB else INK,
                fontweight="bold" if gain >= HOT_DB else "normal",
            )
    ax.set_title(f"{name}: dB above the reference", color=INK, loc="left", fontsize=11)
    ax.tick_params(colors=INK_SOFT)
    for side in ax.spines.values():
        side.set_visible(False)
    bar = fig.colorbar(image, ax=ax, fraction=0.04, pad=0.02)
    bar.set_label("dB above reference", color=INK_SOFT, fontsize=9)
    bar.ax.tick_params(colors=INK_SOFT, labelsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return Path(out_path)
