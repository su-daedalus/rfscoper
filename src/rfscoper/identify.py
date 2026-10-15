"""Guess what a detected signal is, from its shape.

A power sweep cannot decode anything, but it does measure four things that
narrow the possibilities a lot:

1. Frequency: which allocations could hold it (the candidates).
2. Channel: whether it sits exactly where a channelized service would put it.
   BLE advertising, for example, only ever uses 2402, 2426 and 2480 MHz.
3. Width: a 20 MHz Wi-Fi channel, a 2 MHz BLE packet and an 80 MHz carpet of
   hopping Bluetooth look very different.
4. Behavior: steady (a TV station) or intermittent (a key fob).

Each candidate starts from a base score for being in the right place, and
each check adds or removes points. The checks only count when the sweep can
actually measure them: a 200 kHz FM signal can't be measured with 1 MHz bins,
so its width is neither evidence for nor against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rfscoper.bands import SignalType, candidates

BASE = 0.40  # in a plausible place
CHANNEL_HIT, CHANNEL_MISS = 0.25, -0.20
WIDTH_HIT, WIDTH_MISS = 0.20, -0.25
PATTERN_HIT, PATTERN_MISS = 0.15, -0.15
STEADY_DUTY = 0.8  # on in at least this share of sweeps counts as steady

LIKELY, POSSIBLE = 0.75, 0.50


@dataclass(frozen=True)
class Reason:
    effect: Literal["+", "-", "~"]  # for, against, neutral
    text: str


@dataclass(frozen=True)
class Guess:
    key: str
    kind: SignalType
    score: float
    reasons: tuple[Reason, ...]

    @property
    def label(self) -> str:
        if self.score >= LIKELY:
            return "likely"
        if self.score >= POSSIBLE:
            return "possible"
        return "unlikely"


def _mhz(hz: float) -> str:
    """Readable frequency or bandwidth: 2.4 GHz values in MHz, small ones in kHz."""
    if hz >= 1e6:
        return f"{hz / 1e6:.4g} MHz"
    return f"{hz / 1e3:.4g} kHz"


def _article(word: str) -> str:
    """'a BLE', 'an FM': 'an' goes before a vowel sound, including spelled-out letters."""
    vowel_sound = word[:1].upper() in "AEIOU" or (word.isupper() and word[:1] in "FHLMNRSX")
    return f"{'an' if vowel_sound else 'a'} {word}"


def _span(low: float, high: float) -> str:
    """'500 kHz-1.5 MHz', or '30-84 MHz' when both ends share a unit."""
    low_text, high_text = _mhz(low), _mhz(high)
    if low_text.split()[1] == high_text.split()[1]:
        return f"{low_text.split()[0]}-{high_text}"
    return f"{low_text}-{high_text}"


def _check_channel(kind: SignalType, center_hz: float, bin_hz: float) -> tuple[float, Reason]:
    nearest = min(kind.channels_mhz, key=lambda mhz: abs(mhz * 1e6 - center_hz))
    distance = abs(nearest * 1e6 - center_hz)
    tolerance = max(bin_hz, 0.25 * kind.bandwidth_hz[0], 15e3)
    if distance <= tolerance:
        return CHANNEL_HIT, Reason("+", f"on {_article(kind.short)} channel ({nearest:g} MHz)")
    return CHANNEL_MISS, Reason(
        "-", f"off the {kind.short} channels (nearest {nearest:g} MHz, {_mhz(distance)} away)"
    )


def _check_width(kind: SignalType, width_hz: float, bin_hz: float) -> tuple[float, Reason]:
    low, high = kind.bandwidth_hz
    measured = max(width_hz, bin_hz)
    slack = bin_hz  # a run's width is only known to within about one bin
    expected = _span(low, high)
    if measured > high + slack:
        return WIDTH_MISS, Reason("-", f"{_mhz(measured)} wide, too wide (expected {expected})")
    if measured + slack < low:
        return WIDTH_MISS, Reason("-", f"{_mhz(measured)} wide, too narrow (expected {expected})")
    if high <= slack:
        return 0.0, Reason("~", f"narrower than the {_mhz(bin_hz)} bins can measure")
    return WIDTH_HIT, Reason("+", f"about {_mhz(measured)} wide, as expected ({expected})")


def _check_pattern(kind: SignalType, duty: float) -> tuple[float, Reason]:
    steady = duty >= STEADY_DUTY
    on = f"on in {duty:.0%} of sweeps"
    if kind.pattern == "steady":
        if steady:
            return PATTERN_HIT, Reason("+", f"{on}: steady, as expected")
        return PATTERN_MISS, Reason("-", f"{on}, but this is usually steady")
    if not steady:
        return PATTERN_HIT, Reason("+", f"{on}: intermittent, as expected")
    return PATTERN_MISS, Reason("-", f"{on}, but this usually comes and goes")


def identify(
    center_hz: float,
    width_hz: float,
    bin_hz: float,
    duty: float | None = None,
) -> list[Guess]:
    """Rank the catalog entries that could explain a signal, best first."""
    guesses = []
    for key, kind in candidates(center_hz, slack_hz=bin_hz / 2).items():
        score = BASE
        reasons = [Reason("+", f"in the {kind.band} band")]
        checks = []
        if kind.channels_mhz:
            checks.append(_check_channel(kind, center_hz, bin_hz))
        checks.append(_check_width(kind, width_hz, bin_hz))
        if duty is not None and kind.pattern != "any":
            checks.append(_check_pattern(kind, duty))
        for points, reason in checks:
            score += points
            reasons.append(reason)
        guesses.append(Guess(key, kind, round(min(max(score, 0.0), 1.0), 2), tuple(reasons)))

    def specificity(guess: Guess) -> float:
        found = guess.kind.range_containing(center_hz, bin_hz / 2)
        return found[1] - found[0] if found else float("inf")

    return sorted(guesses, key=lambda g: (-g.score, specificity(g)))


def strength(margin_db: float) -> str:
    """How far above the noise a signal stands: weak, moderate or strong."""
    if margin_db >= 30:
        return "strong"
    if margin_db >= 15:
        return "moderate"
    return "weak"


def proximity_hint(kind: SignalType, margin_db: float) -> str | None:
    """What a signal's strength suggests about where it is, if anything.

    Only meaningful for low-power personal devices: a phone or a key fob
    transmits a few milliwatts, so standing far above the noise usually means
    it is close. A TV tower is loud from miles away, so loudness says little.
    """
    level = strength(margin_db)
    if kind.low_power and level == "strong":
        return "strong for a low-power device: probably close by"
    if kind.low_power and level == "weak":
        return "weak for a low-power device: farther away or behind walls"
    if not kind.low_power and level == "strong":
        return "strong: a nearby or high-power transmitter"
    return None
