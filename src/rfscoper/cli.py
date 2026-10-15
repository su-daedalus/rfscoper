"""Command-line interface for rfscoper.

    rfscoper capture hallway --range 400:480 --bin 100k --note "baseline"
    rfscoper list
    rfscoper info hallway
    rfscoper show hallway
    rfscoper compare hallway room --explain
    rfscoper catalog bluetooth
    rfscoper identify 2426 --width 1M --duty 0.3
    rfscoper probe start office && rfscoper probe spot office "desk lamp"
    rfscoper probe live --watch 433.92M
    rfscoper scope --demo
    rfscoper scope --range 2400:2500      # live window: spectrum, waterfall, meter

Every analysis command takes --json, which prints machine-readable results
to stdout and sends everything else to stderr, so output can be piped.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from rfscoper import __version__, analysis, bands, capture, identify, plots, probe
from rfscoper.parser import SweepData, load_sweeps

SUFFIXES = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}


# ---------------------------------------------------------------- argument types


def parse_hz(text: str) -> int:
    """'100k' -> 100000, '1M' -> 1000000, '250000' -> 250000."""
    cleaned = text.strip().lower()
    if not cleaned:
        raise argparse.ArgumentTypeError("Empty frequency")
    multiplier = SUFFIXES.get(cleaned[-1], 1)
    number = cleaned[:-1] if cleaned[-1] in SUFFIXES else cleaned
    try:
        value = int(float(number) * multiplier)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Not a frequency: '{text}'") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("Frequency must be positive")
    return value


def parse_range(text: str) -> tuple[int, int]:
    """'88:108' -> (88, 108), in MHz."""
    try:
        start, stop = (int(part) for part in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError("Use START:STOP in MHz, like 88:108") from None
    return start, stop


# ---------------------------------------------------------------- output helpers


def status(message: str, args: argparse.Namespace) -> None:
    """Human-readable progress. Goes to stderr when stdout is carrying JSON."""
    print(message, file=sys.stderr if getattr(args, "json", False) else sys.stdout)


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2))


def describe(peak: analysis.Peak, duty: float | None, bin_hz: float) -> dict[str, Any]:
    """Everything rfscoper can say about one signal, ready for text or JSON."""
    guesses = identify.identify(peak.center_hz, peak.width_hz, bin_hz, duty)
    top = guesses[0] if guesses and guesses[0].score >= identify.POSSIBLE else None
    return {
        "peak": peak,
        "duty": duty,
        "guesses": guesses,
        "top": top,
        "strength": identify.strength(peak.margin_db),
        "hint": identify.proximity_hint(top.kind, peak.margin_db) if top else None,
    }


def signal_json(info: dict[str, Any], margin_key: str) -> dict[str, Any]:
    peak: analysis.Peak = info["peak"]
    result: dict[str, Any] = {
        "freq_hz": peak.freq_hz,
        "center_hz": peak.center_hz,
        "width_hz": peak.width_hz,
        "power_db": round(peak.power_db, 2),
        margin_key: round(peak.margin_db, 2),
    }
    if info["duty"] is not None:
        result["duty_cycle"] = round(info["duty"], 3)
    result["band"] = bands.lookup(peak.center_hz)
    result["strength"] = info["strength"]
    result["hint"] = info["hint"]
    result["likely"] = [
        {
            "id": g.key,
            "name": g.kind.name,
            "label": g.label,
            "score": g.score,
            "reasons": [f"{r.effect} {r.text}" for r in g.reasons],
        }
        for g in info["guesses"][:3]
    ]
    return result


def likely_cell(info: dict[str, Any]) -> str:
    """Short answer for the table: the best guess, or what band it's in."""
    top: identify.Guess | None = info["top"]
    if top is not None:
        return top.kind.name if top.label == "likely" else f"{top.kind.name}?"
    band = bands.lookup(info["peak"].center_hz)
    return f"unknown ({band})" if band else "unknown"


def print_signals(infos: list[dict[str, Any]], margin_header: str) -> None:
    if not infos:
        print("  (none)")
        return
    rows = []
    for info in infos:
        p: analysis.Peak = info["peak"]
        rows.append(
            [
                f"{p.center_hz / 1e6:.3f}",
                identify._mhz(p.width_hz) if p.width_hz else "-",
                f"{p.margin_db:.1f}",
                f"{info['duty']:.0%}" if info["duty"] is not None else "-",
                likely_cell(info),
            ]
        )
    print_table(["MHz", "width", margin_header, "on", "likely"], rows, ">>>><")


def print_explanations(infos: list[dict[str, Any]]) -> None:
    """The reasoning behind each guess, for --explain."""
    marks = {"+": "+", "-": "-", "~": "·"}
    for info in infos:
        p: analysis.Peak = info["peak"]
        parts = [f"{p.center_hz / 1e6:.3f} MHz", f"{identify._mhz(p.width_hz)} wide"]
        parts.append(info["strength"])
        if info["duty"] is not None:
            parts.append(f"on {info['duty']:.0%}")
        print("\n" + " | ".join(parts))
        shown = [g for g in info["guesses"] if g.score >= 0.35][:3] or info["guesses"][:1]
        if not shown:
            print("  no catalog entry covers this frequency")
        for n, guess in enumerate(shown):
            print(f"  {guess.label:<9} {guess.score:.2f}  {guess.kind.name}")
            if n == 0:
                for reason in guess.reasons:
                    print(f"                  {marks[reason.effect]} {reason.text}")
                if guess.kind.note:
                    print(f"                  note: {guess.kind.note}")
        if info["hint"]:
            print(f"  hint: {info['hint']}")


def print_table(headers: list[str], rows: list[list[str]], align: str) -> None:
    """Print columns padded to fit. align has one '<' or '>' per column."""
    widths = [max([len(h), *(len(r[i]) for r in rows)]) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "  " + "  ".join(
            f"{cell:{a}{w}}" for cell, a, w in zip(cells, align, widths, strict=True)
        )

    print(line(headers).rstrip())
    for row in rows:
        print(line(row).rstrip())


# ---------------------------------------------------------------- commands


def confirm(question: str) -> bool:
    """Ask a yes/no question on the terminal; anything but yes (or no terminal) is no."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def pick_name(
    what: str,
    name: str,
    detail: str,
    args: argparse.Namespace,
    exists: Callable[[str], bool] | None = None,
) -> tuple[str, bool]:
    """Name to save under, and whether that replaces the existing one.

    Asks "Overwrite it?". Yes (or --overwrite) replaces the old one; no, or no
    terminal to ask on, keeps it and saves under the next numbered name.
    """
    if args.overwrite or confirm(f"{what} '{name}' already exists{detail}. Overwrite it?"):
        return name, True
    new = capture.free_name(name, args.dir, exists)
    print(f"Keeping the old one. Saving this one as '{new}'.")
    return new, False


def cmd_capture(args: argparse.Namespace) -> int:
    start, stop = args.range
    settings = capture.CaptureSettings(
        start_mhz=start,
        stop_mhz=stop,
        bin_hz=args.bin,
        sweeps=args.sweeps if args.sweeps is not None else 30,
        lna_db=args.lna,
        vga_db=args.vga,
        amp=args.amp,
        duration_s=args.duration,
    )
    settings.validate()
    radio = settings.radio()
    adjustments = settings.adjustments()
    how_long = f"for {args.duration:g} s" if args.duration else f"{settings.sweeps} sweeps"

    if args.dry_run:
        cmd = shlex.join(settings.command(Path(args.dir) / f"{args.name}.csv"))
        print(cmd if args.duration is None else f"{cmd}   # stopped after {args.duration:g} s")
        print(
            f"hackrf_sweep will cover {radio.start_mhz}-{radio.stop_mhz} MHz with "
            f"{radio.bin_hz:,.2f} Hz bins (FFT size {radio.fft_size}), {how_long}"
        )
        for note in adjustments:
            print(f"  adjusted: {note}")
        return 0

    name, overwrite = args.name, False
    existing = Path(args.dir) / f"{name}.csv"
    if existing.exists():
        try:
            when = capture.taken(capture.read_metadata(existing))
        except (OSError, ValueError):
            when = ""
        detail = f" (taken {when[:19].replace('T', ' ')})" if when else ""
        name, overwrite = pick_name("A capture named", name, detail, args)

    for note in adjustments:
        print(f"Note: {note}")
    print(f"Capturing '{name}': {radio.start_mhz}-{radio.stop_mhz} MHz, {how_long}...")
    path = capture.run_capture(name, settings, args.dir, note=args.note, overwrite=overwrite)
    meta = capture.read_metadata(path)
    m = meta["measured"]
    rate = f", {m['sweeps_per_s']:g} sweeps/s" if m["sweeps_per_s"] else ""
    print(
        f"Saved {path}: {m['complete_sweeps']} complete sweeps in {m['duration_s']:g} s{rate}, "
        f"{m['bins']} bins of {m['bin_hz']:,.2f} Hz"
    )
    for warning in meta["warnings"]:
        print(f"Warning: {warning}")
    return 0


def describe_span(data: SweepData) -> str:
    """', 60.0 s (6.6 sweeps/s)' from the first row to the last, or ''."""
    duration = data.duration_s
    if duration is None:
        return ""
    if duration > 0 and data.complete_sweeps:
        return f", {duration:.1f} s ({data.complete_sweeps / duration:.1f} sweeps/s)"
    return f", {duration:.1f} s"


def requested_settings(meta: dict[str, Any]) -> capture.CaptureSettings | None:
    """Rebuild the request from a record, to work out what the radio used."""
    req = meta.get("requested") or {}
    try:
        return capture.CaptureSettings(
            start_mhz=int(req["start_mhz"]),
            stop_mhz=int(req["stop_mhz"]),
            bin_hz=int(req["bin_hz"]),
            sweeps=req.get("sweeps") or 30,
            lna_db=int(req.get("lna_db", 24)),
            vga_db=int(req.get("vga_db", 20)),
            amp=bool(req.get("amp", False)),
            duration_s=req.get("duration_s"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def cmd_list(args: argparse.Namespace) -> int:
    captures = capture.list_captures(args.dir)
    if args.json:
        emit_json(captures)
        return 0
    if not captures:
        print(f"No captures in {args.dir}/ yet. Start with: rfscoper capture NAME")
        return 0

    rows = []
    for meta in captures:
        req = meta.get("requested") or {}
        span = f"{req['start_mhz']}-{req['stop_mhz']}" if "start_mhz" in req else "?"
        if req.get("duration_s"):
            length = f"{req['duration_s']:g} s"
        elif req.get("sweeps"):
            length = f"{req['sweeps']} sweeps"
        else:
            length = "?"
        when = capture.taken(meta)[:19].replace("T", " ")
        rows.append([meta["name"], when, span, length, meta.get("note", "")])
    print_table(["name", "taken", "MHz", "length", "note"], rows, "<<<<<")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Requested settings, what the radio used, and what the file contains."""
    path = capture.resolve(args.name, args.dir)
    meta = capture.read_metadata(path)
    data = load_sweeps(path)
    measured = capture.measure(data)  # always re-read from the file itself
    request = requested_settings(meta)
    radio = asdict(request.radio()) if request else (meta.get("radio") or {})
    warnings = capture.check(radio, measured) if radio else []
    adjustments = request.adjustments() if request else []

    if args.json:
        emit_json({**meta, "radio": radio, "measured": measured, "warnings": warnings})
        return 0

    req = meta.get("requested") or {}

    def hz(value: Any) -> str:
        return f"{value:,.2f} Hz" if isinstance(value, (int, float)) else "-"

    def gains(block: dict[str, Any]) -> str:
        if "lna_db" not in block:
            return "-"
        return f"{block['lna_db']} / {block['vga_db']} dB" + (" + amp" if block.get("amp") else "")

    if req.get("duration_s"):
        asked, radio_len = f"{req['duration_s']:g} s", "until stopped"
    elif req.get("sweeps"):
        asked, radio_len = f"{req['sweeps']} sweeps", f"-N {req['sweeps']}"
    else:
        asked, radio_len = "-", "-"
    got = f"{measured['complete_sweeps']} complete"
    if not measured["last_sweep_complete"]:
        got += " + 1 partial"

    print(f"{meta['name']}  ({path})")
    if meta.get("note"):
        print(f"  note      {meta['note']}")
    print(f"  started   {meta.get('started') or '-'}")
    print(f"  finished  {meta.get('finished') or '-'}")
    print(f"  file      first row {measured['first_row']}, last row {measured['last_row']}")
    print()
    duration = measured["duration_s"]
    rate = f" ({measured['sweeps_per_s']:g}/s)" if measured["sweeps_per_s"] else ""
    rows = [
        [
            "range",
            f"{req['start_mhz']}-{req['stop_mhz']} MHz" if "start_mhz" in req else "-",
            f"{radio['start_mhz']}-{radio['stop_mhz']} MHz" if radio else "-",
            f"{measured['low_edge_hz'] / 1e6:.2f}-{measured['high_edge_hz'] / 1e6:.2f} MHz",
        ],
        ["bin width", hz(req.get("bin_hz")), hz(radio.get("bin_hz")), hz(measured["bin_hz"])],
        ["FFT size", "-", str(radio.get("fft_size", "-")), str(measured["fft_size"])],
        ["length", asked, radio_len, f"{duration:g} s{rate}" if duration is not None else "-"],
        ["sweeps", "-", "-", got],
        ["gains", gains(req), gains(radio), "-"],
        ["data", "-", "-", f"{measured['rows']} rows, {measured['bins']} bins"],
    ]
    print_table(["", "requested", "radio used", "file contains"], rows, "<<<<")
    print()
    if meta.get("command"):
        print(f"  command   {meta['command']}")
    for note in adjustments:
        print(f"  adjusted  {note}")
    for warning in warnings:
        print(f"  warning   {warning}")
    if meta.get("legacy_format"):
        print("  (older record: the radio column is worked out from the request)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = capture.resolve(args.name, args.dir)
    data = load_sweeps(path)
    spec = analysis.spectrum(data, args.stat)
    floor = analysis.signal_floor(data, args.stat)
    peaks = analysis.find_peaks(data.freqs_hz, spec, floor, args.threshold)[: args.top]
    duty = analysis.duty_cycles(data, peaks, args.on_level)
    bin_hz = analysis.bin_width(data.freqs_hz)
    infos = [describe(p, d, bin_hz) for p, d in zip(peaks, duty, strict=True)]

    typical_floor = float(np.nanmedian(floor))
    if args.json:
        emit_json(
            {
                "capture": path.stem,
                "sweeps": data.n_sweeps,
                "bins": data.n_bins,
                "bin_hz": data.bin_hz or round(bin_hz, 2),
                "fft_size": data.fft_size,
                "duration_s": data.duration_s,
                "first_bin_hz": int(data.freqs_hz[0]),
                "last_bin_hz": int(data.freqs_hz[-1]),
                "stat": args.stat,
                "threshold_db": args.threshold,
                "on_level_db": args.on_level,
                "noise_floor_db": round(typical_floor, 2),
                "signals": [signal_json(info, "above_floor_db") for info in infos],
            }
        )
    else:
        mhz = data.freqs_hz / 1e6
        print(
            f"{path.stem}: {data.n_sweeps} sweeps, {data.n_bins} bins of "
            f"{identify._mhz(data.bin_hz or bin_hz)} (FFT {data.fft_size}), "
            f"{mhz[0]:.1f}-{mhz[-1]:.1f} MHz{describe_span(data)}"
        )
        print(f"Typical noise floor: {typical_floor:.1f} dB\n")
        print(f"Signals at least {args.threshold:g} dB above the noise floor ({args.stat}):")
        print_signals(infos, "above floor")
        if args.explain:
            print_explanations(infos)

    if not args.no_plot:
        out = Path(args.out) if args.out else path.with_suffix(".png")
        labels = [info["top"].kind.short if info["top"] else "" for info in infos]
        plots.plot_capture(data, path.stem, out, args.stat, peaks, labels)
        status(f"\nPlot saved to {out}", args)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    if not 2 <= len(args.names) <= plots.MAX_SERIES:
        raise ValueError(f"Compare 2 to {plots.MAX_SERIES} captures (the first is the baseline)")

    paths = [capture.resolve(name, args.dir) for name in args.names]
    captures = [load_sweeps(path) for path in paths]
    series = [
        (path.stem, data.freqs_hz, analysis.spectrum(data, args.stat))
        for path, data in zip(paths, captures, strict=True)
    ]

    base_name, base_freqs, base_spec = series[0]
    bin_hz = analysis.bin_width(base_freqs)
    results = []
    for (name, freqs, spec), data in zip(series[1:], captures[1:], strict=True):
        comparison = analysis.align(base_freqs, base_spec, freqs, spec)
        found = analysis.new_signals(comparison, args.threshold)[: args.top]
        duty = analysis.duty_cycles(data, found, args.on_level)
        infos = [describe(p, d, bin_hz) for p, d in zip(found, duty, strict=True)]
        results.append((name, comparison, found, infos))

    if args.json:
        emit_json(
            {
                "baseline": base_name,
                "stat": args.stat,
                "threshold_db": args.threshold,
                "comparisons": [
                    {
                        "capture": name,
                        "overlap_hz": [int(comp.freqs_hz[0]), int(comp.freqs_hz[-1])],
                        "new_signals": [signal_json(i, "above_baseline_db") for i in infos],
                    }
                    for name, comp, _, infos in results
                ],
            }
        )
    else:
        for name, _, _, infos in results:
            print(f"Stronger in {name} than in {base_name} by {args.threshold:g}+ dB:")
            print_signals(infos, f"vs {base_name}")
            if args.explain:
                print_explanations(infos)
            print()

    if not args.no_plot:
        stem = "_vs_".join(p.stem for p in paths)
        out = Path(args.out) if args.out else paths[0].parent / f"compare_{stem}.png"
        # The difference panel only makes sense for exactly two captures.
        diff_panel: analysis.Comparison | None = None
        diff_peaks: list[analysis.Peak] | None = None
        if len(results) == 1:
            _, diff_panel, diff_peaks, _ = results[0]
        plots.plot_compare(series, out, args.stat, diff_panel, diff_peaks, args.threshold)
        status(f"Plot saved to {out}", args)
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    term = (args.search or "").lower()
    entries = {
        key: kind
        for key, kind in bands.CATALOG.items()
        if not term
        or term in key
        or term in kind.name.lower()
        or term in kind.band.lower()
        or term in kind.note.lower()
    }
    if args.json:
        emit_json(
            {
                key: {
                    "name": kind.name,
                    "band": kind.band,
                    "ranges_mhz": kind.ranges_mhz,
                    "bandwidth_khz": kind.bandwidth_khz,
                    "pattern": kind.pattern,
                    "channels_mhz": kind.channels_mhz,
                    "low_power": kind.low_power,
                    "note": kind.note,
                }
                for key, kind in entries.items()
            }
        )
        return 0
    if not entries:
        print(f"Nothing in the catalog matches '{args.search}'.")
        return 0
    rows = []
    for key, kind in entries.items():
        span = ", ".join(f"{a:g}-{b:g}" for a, b in kind.ranges_mhz)
        low, high = kind.bandwidth_hz
        rows.append([key, span, identify._span(low, high), kind.pattern, kind.name])
    print_table(["id", "MHz", "width", "pattern", "name"], rows, "<<<<<")
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    freq_hz = args.mhz * 1e6
    width = args.width if args.width is not None else args.bin
    peak = analysis.Peak(
        round(freq_hz),
        0.0,
        args.margin,
        round(freq_hz - width / 2),
        round(freq_hz + width / 2),
    )
    info = describe(peak, args.duty, args.bin)
    if args.json:
        emit_json(signal_json(info, "above_floor_db"))
    else:
        print_explanations([info])
    return 0


# ---------------------------------------------------------------- probe


def _probe_settings(args: argparse.Namespace) -> capture.CaptureSettings:
    start, stop = args.range
    return capture.CaptureSettings(
        start_mhz=start,
        stop_mhz=stop,
        bin_hz=args.bin,
        lna_db=args.lna,
        vga_db=args.vga,
        amp=False,
        duration_s=args.duration,
    )


def _capture_into(
    name: str,
    settings: capture.CaptureSettings,
    directory: Path,
    note: str,
    executable: str = "hackrf_sweep",
) -> Path:
    for adjustment in settings.adjustments():
        print(f"Note: {adjustment}")
    how_long = f"{settings.duration_s:g} s"
    print(f"Capturing '{name}' for {how_long}... hold the probe still.")
    path = capture.run_capture(name, settings, directory, note=note, executable=executable)
    m = capture.read_metadata(path)["measured"]
    print(f"  {m['complete_sweeps']} sweeps, {m['bins']} bins")
    return path


def cmd_probe_start(args: argparse.Namespace) -> int:
    directory = probe.session_dir(args.session, args.dir)
    settings = _probe_settings(args)
    settings.validate()
    old: list[Path] = []
    if (directory / probe.SESSION_FILE).exists():
        try:
            spots = len(probe.Session.load(directory).spots)
            detail = f" ({spots} spot{'s' if spots != 1 else ''}, all deleted if you start over)"
        except (OSError, ValueError, KeyError):
            detail = ""
        name, overwrite = pick_name(
            "Probe session",
            args.session,
            detail,
            args,
            lambda n: probe.session_dir(n, args.dir).exists(),
        )
        if overwrite:
            old = [directory]
        else:
            args.session = name
            directory = probe.session_dir(name, args.dir)
    # The old session is only deleted once the new reference has been captured.
    with capture.replacing(old):
        return _start_session(args, directory, settings)


def _start_session(
    args: argparse.Namespace, directory: Path, settings: capture.CaptureSettings
) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    session = probe.Session(
        args.session, directory, settings, created=datetime.now().astimezone().isoformat()
    )
    session.save()
    print(f"Probe session '{args.session}' in {directory}/")
    print("Reference: hold the probe in open air, mid-room, away from everything.")
    _capture_into(
        probe.REFERENCE, settings, directory, args.note or "reference, open air", args.executable
    )
    print(f'\nNext: rfscoper probe spot {args.session} "desk lamp"')
    return 0


def cmd_probe_spot(args: argparse.Namespace) -> int:
    session = probe.Session.load(probe.session_dir(args.session, args.dir))
    if not session.reference_csv.exists():
        raise FileNotFoundError("This session has no reference capture yet")
    csv_name = session.next_spot_file(args.label)
    spot = probe.Spot(len(session.spots) + 1, args.label, csv_name, args.note)
    print(f"Spot {spot.number}: {args.label}. Hold the probe against it.")
    _capture_into(
        Path(csv_name).stem, session.settings, session.directory, args.label, args.executable
    )
    session.spots.append(spot)
    session.save()

    # Quick verdict for this spot alone, so you know whether to look closer.
    reference, spots = probe.load_session_data(session)
    found = probe.hotspots(reference, {args.label: spots[args.label]}, args.threshold)
    hits = [
        (h, h.gains_db[args.label]) for h in found if h.gains_db.get(args.label, 0) >= probe.WARM_DB
    ]
    if hits:
        print(f"  {len(hits)} signal(s) stronger here than at the reference:")
        for h, gain in hits[:5]:
            info = describe(h.peak, None, reference.bin_hz)
            print(f"    {h.peak.center_hz / 1e6:9.3f} MHz  +{gain:.0f} dB  {likely_cell(info)}")
    else:
        print("  nothing stronger here than at the reference")
    return 0


def cmd_probe_report(args: argparse.Namespace) -> int:
    session = probe.Session.load(probe.session_dir(args.session, args.dir))
    reference, spots = probe.load_session_data(session)
    results = probe.hotspots(reference, spots, args.threshold, args.stat)
    bin_hz = reference.bin_hz
    infos = [describe(h.peak, None, bin_hz) for h in results]

    if args.json:
        emit_json(
            {
                **probe.describe_session(session),
                "threshold_db": args.threshold,
                "signals": [
                    {
                        **signal_json(info, "above_floor_db"),
                        "reference_db": round(h.reference_db, 2),
                        "gains_db": h.gains_db,
                        "verdict": h.verdict,
                    }
                    for h, info in zip(results, infos, strict=True)
                ],
            }
        )
        return 0

    print(f"Probe session '{session.name}': reference + {len(session.spots)} spot(s)")
    for spot in session.spots:
        print(f"  {spot.number:2d}. {spot.label}")
    print()
    if not results:
        print("No signals found above the noise floor in any capture.")
        return 0

    labels = [spot.label for spot in session.spots]
    headers = ["MHz", "ref dB", *[f"+{lab[:12]}" for lab in labels], "verdict", "likely"]
    rows = []
    for h, info in zip(results, infos, strict=True):
        gains = [
            (f"{h.gains_db[lab]:+.0f}".replace("-0", "0").replace("+0", "0"))
            if lab in h.gains_db
            else "-"
            for lab in labels
        ]
        rows.append(
            [
                f"{h.peak.center_hz / 1e6:.3f}",
                f"{h.reference_db:.1f}",
                *gains,
                h.verdict,
                likely_cell(info),
            ]
        )
    print_table(headers, rows, ">>" + ">" * len(labels) + "<<")
    hot = [h for h in results if h.hottest and h.hottest[1] >= probe.HOT_DB]
    print()
    if hot:
        print(f"{len(hot)} hot signal(s). Open those objects up and look.")
    else:
        print("No hot spots. Nothing probed is the source of a signal.")
    if args.explain:
        print_explanations(infos)

    if not args.no_plot:
        out = Path(args.out) if args.out else session.directory / "report.png"
        plots.plot_probe(results, labels, out, session.name)
        status(f"\nPlot saved to {out}", args)
    return 0


def cmd_probe_live(args: argparse.Namespace) -> int:
    """Stream hackrf_sweep and print one meter line per sweep."""
    settings = _probe_settings(args)
    settings.duration_s = None
    if args.watch and args.range == (300, 2500):
        # Watching one frequency: sweep only the 20 MHz step around it, so each
        # sweep is fast and hackrf_sweep's 8 KiB output buffer fills many times
        # a second instead of once per wide sweep.
        start = int(args.watch / 1e6) // 20 * 20
        settings.start_mhz, settings.stop_mhz = start, start + 20
    settings.validate()
    cmd = settings.command("-", args.executable, stream=True)  # stdout, until stopped
    if args.dry_run:
        print(shlex.join(cmd))
        return 0

    meter = probe.LiveMeter(args.watch, args.watch_width or 0.0)
    floor: float | None = None
    best_ever = -np.inf
    print("Sweeping. Move the probe slowly; the bar grows as you get closer. Ctrl+C to stop.")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
    )
    started = time.monotonic()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            reading = meter.push(line)
            if reading is None:
                continue
            level = reading.watch_db if reading.watch_db is not None else reading.peak_db
            if floor is None:
                floor = level - 5.0  # the first sweep sets the scale
            best_ever = max(best_ever, level)
            where = (
                f"{args.watch / 1e6:.3f} MHz" if args.watch else f"{reading.peak_hz / 1e6:.3f} MHz"
            )
            print(
                f"{reading.sweep:4d}  {probe.meter_bar(level, floor)}  {level:6.1f} dB  {where}"
                f"  (best {best_ever:.1f})",
                flush=True,
            )
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


def cmd_probe_list(args: argparse.Namespace) -> int:
    base = Path(args.dir)
    sessions = sorted(p for p in base.glob("probe-*") if (p / probe.SESSION_FILE).exists())
    if args.json:
        emit_json([probe.describe_session(probe.Session.load(p)) for p in sessions])
        return 0
    if not sessions:
        print(f"No probe sessions in {base}/. Start one with: rfscoper probe start NAME")
        return 0
    rows = []
    for path in sessions:
        s = probe.Session.load(path)
        req = s.settings
        rows.append(
            [
                s.name,
                s.created[:19].replace("T", " "),
                f"{req.start_mhz}-{req.stop_mhz}",
                str(len(s.spots)),
            ]
        )
    print_table(["session", "created", "MHz", "spots"], rows, "<<<>")
    return 0


# ---------------------------------------------------------------- scope


DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=rfscoper scope
Comment=Live RF spectrum, waterfall and near-field meter (offline)
Exec={exe} scope
Terminal=false
Categories=HamRadio;Science;Utility;
Keywords=SDR;HackRF;spectrum;waterfall;near field;
"""


def cmd_scope(args: argparse.Namespace) -> int:
    from rfscoper import live, qt

    if args.install_launcher:
        exe = shutil.which("rfscoper") or f"{sys.executable} -m rfscoper"
        target = Path.home() / ".local/share/applications/rfscoper-scope.desktop"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DESKTOP_ENTRY.format(exe=exe))
        print(f"Added '{target.name}' to your applications menu ({target}).")
        return 0
    if not qt.AVAILABLE:
        print(qt.INSTALL_HELP, file=sys.stderr)
        return 1

    start, stop = args.range
    settings = capture.CaptureSettings(
        start_mhz=start, stop_mhz=stop, bin_hz=args.bin, lna_db=args.lna, vga_db=args.vga
    )
    settings.validate()
    demo: live.DemoSource | None = None
    if args.demo:
        demo = live.DemoSource()
        source: live.Source = demo
    else:
        if shutil.which(args.executable) is None:
            raise FileNotFoundError(
                f"{args.executable} not found. Install the HackRF tools, "
                "or try the scope without hardware: rfscoper scope --demo"
            )
        source = live.HackRFSource(args.executable)
    scope = live.Scope(settings, source, args.dir)
    scope.state.mode = "near" if args.near else "far"
    if args.watch:
        scope.state.set_watch(float(args.watch))
    for note in settings.adjustments():
        print(f"Note: {note}")

    from rfscoper import app

    return app.run(scope, demo, seconds=args.seconds, screenshot=args.screenshot)


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rfscoper",
        description="Capture, analyze and compare HackRF spectrum sweeps.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--dir",
        type=Path,
        default=capture.DEFAULT_DIR,
        help="folder for captures (default: ~/rfscoper-captures, or $RFSCOPER_DIR)",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("capture", help="run hackrf_sweep and save the result")
    p.add_argument("name", help="name for this capture, like hallway or room")
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="replace a capture with the same name without asking",
    )
    p.add_argument(
        "--range",
        type=parse_range,
        default=(1, 6000),
        metavar="START:STOP",
        help="frequency range in MHz (default: 1:6000)",
    )
    p.add_argument(
        "--bin", type=parse_hz, default=1_000_000, help="bin width, like 100k or 1M (default: 1M)"
    )
    how_long = p.add_mutually_exclusive_group()
    how_long.add_argument("--sweeps", type=int, help="number of sweeps (default: 30)")
    how_long.add_argument(
        "--duration", type=float, metavar="SECONDS", help="capture for this long instead"
    )
    p.add_argument("--lna", type=int, default=24, help="LNA gain in dB, 0-40 (default: 24)")
    p.add_argument("--vga", type=int, default=20, help="VGA gain in dB, 0-62 (default: 20)")
    p.add_argument("--amp", action="store_true", help="turn on the RF amplifier")
    p.add_argument("--note", default="", help="where or why, like 'hallway baseline'")
    p.add_argument("--dry-run", action="store_true", help="print the hackrf_sweep command and exit")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("info", help="how a capture was taken: requested vs. actual")
    p.add_argument("name", help="capture name or path to a CSV file")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("list", help="list saved captures")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="find signals in one capture and plot it")
    show.add_argument("name", help="capture name or path to a CSV file")
    show.set_defaults(func=cmd_show)

    compare = sub.add_parser("compare", help="overlay captures and find new signals")
    compare.add_argument(
        "names", nargs="+", metavar="name", help="2-4 captures; the first one is the baseline"
    )
    compare.set_defaults(func=cmd_compare)

    p = sub.add_parser("catalog", help="list the signal types rfscoper can recognize")
    p.add_argument("search", nargs="?", help="filter by text, like bluetooth or 433")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("identify", help="what could a signal at this frequency be?")
    p.add_argument("mhz", type=float, help="center frequency in MHz")
    p.add_argument("--width", type=parse_hz, help="occupied bandwidth, like 1M (default: one bin)")
    p.add_argument("--duty", type=float, help="share of sweeps it was on, 0-1")
    p.add_argument("--margin", type=float, default=20.0, help="dB above the noise (default: 20)")
    p.add_argument("--bin", type=parse_hz, default=100_000, help="bin width (default: 100k)")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_identify)

    p = sub.add_parser("scope", help="live window: spectrum, waterfall, near-field meter")
    p.add_argument(
        "--range",
        type=parse_range,
        default=(300, 500),
        metavar="START:STOP",
        help="MHz (default: 300:500)",
    )
    p.add_argument("--bin", type=parse_hz, default=200_000, help="bin width (default: 200k)")
    p.add_argument("--lna", type=int, default=16, help="LNA gain dB, 0-40 (default: 16)")
    p.add_argument("--vga", type=int, default=20, help="VGA gain dB, 0-62 (default: 20)")
    p.add_argument("--near", action="store_true", help="start in near-field mode")
    p.add_argument("--watch", type=parse_hz, help="frequency for the meter, like 433.92M")
    p.add_argument("--demo", action="store_true", help="simulated signals, no HackRF needed")
    p.add_argument(
        "--install-launcher",
        action="store_true",
        help="add rfscoper scope to your desktop's applications menu",
    )
    p.add_argument("--seconds", type=float, help=argparse.SUPPRESS)
    p.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--executable", default="hackrf_sweep", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_scope)

    pr = sub.add_parser("probe", help="near-field probing: find where a signal comes from")
    psub = pr.add_subparsers(dest="probe_command", required=True, metavar="ACTION")

    def probe_capture_options(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--range",
            type=parse_range,
            default=(300, 2500),
            metavar="START:STOP",
            help="MHz (default: 300:2500, where small transmitters live)",
        )
        sp.add_argument("--bin", type=parse_hz, default=200_000, help="bin width (default: 200k)")
        sp.add_argument(
            "--lna", type=int, default=8, help="LNA gain dB (default: 8, low: the probe is close)"
        )
        sp.add_argument("--vga", type=int, default=20, help="VGA gain dB (default: 20)")

    sp = psub.add_parser("start", help="create a session and take the reference capture")
    sp.add_argument("session", help="session name, like office")
    sp.add_argument(
        "--overwrite", action="store_true", help="start over if the session exists, without asking"
    )
    probe_capture_options(sp)
    sp.add_argument(
        "--duration", type=float, default=10.0, help="seconds per capture (default: 10)"
    )
    sp.add_argument("--note", default="")
    sp.add_argument("--executable", default="hackrf_sweep", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_probe_start)

    sp = psub.add_parser("spot", help="capture with the probe held against one object")
    sp.add_argument("session")
    sp.add_argument("label", help="what the probe is touching, like 'desk lamp'")
    sp.add_argument("--note", default="")
    sp.add_argument("--threshold", type=float, default=10.0, help="dB (default: 10)")
    sp.add_argument("--executable", default="hackrf_sweep", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_probe_spot)

    sp = psub.add_parser("report", help="which spots are hot, per signal")
    sp.add_argument("session")
    sp.add_argument("--stat", choices=analysis.STATS, default="peak")
    sp.add_argument("--threshold", type=float, default=10.0, help="dB (default: 10)")
    sp.add_argument("--explain", action="store_true")
    sp.add_argument("--out", help="where to save the PNG")
    sp.add_argument("--no-plot", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_probe_report)

    sp = psub.add_parser("live", help="a live signal-strength meter while you move the probe")
    probe_capture_options(sp)
    sp.add_argument("--watch", type=parse_hz, help="track one frequency, like 433.92M")
    sp.add_argument("--watch-width", type=parse_hz, help="width around --watch (default: one bin)")
    sp.add_argument("--seconds", type=float, help="stop after this long (default: until Ctrl+C)")
    sp.add_argument("--executable", default="hackrf_sweep", help=argparse.SUPPRESS)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_probe_live, duration=None)

    sp = psub.add_parser("list", help="list probe sessions")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_probe_list)

    for p in (show, compare):
        p.add_argument("--stat", choices=analysis.STATS, default="peak", help="(default: peak)")
        p.add_argument(
            "--explain", action="store_true", help="show the reasoning behind each guess"
        )
        p.add_argument(
            "--threshold", type=float, default=10.0, help="detection threshold in dB (default: 10)"
        )
        p.add_argument("--top", type=int, default=15, help="max signals to report (default: 15)")
        p.add_argument(
            "--on-level",
            type=float,
            default=6.0,
            metavar="DB",
            help="dB above the noise floor that counts as 'on' for the duty cycle (default: 6)",
        )
        p.add_argument("--out", help="where to save the PNG")
        p.add_argument("--no-plot", action="store_true", help="skip the PNG")
        p.add_argument("--json", action="store_true", help="print JSON")

    return parser


def warn_about_old_captures(directory: Path) -> None:
    """Point out captures still in ./captures, the location before 0.1.0."""
    old = capture.LEGACY_DIR
    if directory != capture.DEFAULT_DIR or not old.is_dir() or old.resolve() == directory.resolve():
        return
    if not any(old.glob("*.csv")) and not any(old.glob("probe-*")):
        return
    print(
        f"Note: captures found in {old.resolve()}/ (the old location, inside the project).\n"
        f"      rfscoper now keeps them in {directory}/, outside the code, so they can\n"
        f"      never be committed. Move them with:\n"
        f"        mkdir -p {directory} && mv {old}/* {directory}/ && rmdir {old}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    warn_about_old_captures(Path(args.dir))
    try:
        result: int = args.func(args)
    except BrokenPipeError:
        # The reader went away early (`rfscoper show x | head`): not an error.
        # Point stdout at /dev/null so Python's exit-time flush stays quiet too.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except (ValueError, OSError, RuntimeError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    return result


if __name__ == "__main__":
    sys.exit(main())
