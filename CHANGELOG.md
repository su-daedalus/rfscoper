# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Captures and probe sessions are now saved in `~/rfscoper-captures/`
  (or `$RFSCOPER_DIR`) instead of `./captures/`, so private RF data never
  sits inside the project folder where it could be committed. rfscoper points
  out captures left in the old location and prints the command to move them.
- Reusing a capture or probe session name now asks whether to overwrite it.
  Answering no saves under the next free name (`room-capture2`, ...);
  `--overwrite` replaces without asking. The old capture is kept until the
  new one succeeds, so a failed or interrupted capture loses nothing.

## [0.1.0] - 2026-10-14

First public release.

### Live scope

- `rfscoper scope`: a desktop window (PyQt6 or PyQt5) that streams
  `hackrf_sweep` in real time: spectrum with live, peak-hold, average,
  noise-floor and reference traces; a waterfall; a Geiger-counter meter with
  needle, history strip and optional audio clicks; and a live table of
  identified signals with duty cycle and near-field gain.
- Far-field and near-field modes. Near field takes an averaged open-air
  reference and measures every signal against it; **Follow strongest** lets
  the meter track the loudest signal.
- Tuning: band presets, range, bin width and gains from the toolbar; click to
  watch a frequency, scroll to zoom with an automatic retune.
- Record 30 s to a normal capture, export traces to CSV, save screenshots.
- `--demo` runs the window on a simulated radio with a hidden transmitter
  and a probe-distance slider; `--install-launcher` adds a desktop menu entry.
- Fully offline, and the RF amp is never enabled from the scope.

### Capture

- `rfscoper capture`: runs `hackrf_sweep` and saves the CSV with a
  `.meta.json` record of the settings, the exact command and a note.
- Counted (`--sweeps`) or timed (`--duration SECONDS`) captures. Timed captures
  stop `hackrf_sweep` with SIGINT so the CSV closes cleanly.
- Models `hackrf_sweep`'s own rules (FFT sizes of the form 8k+4, 2445 Hz to
  5 MHz bins, 20 MHz range steps, gain steps of 8 and 2 dB) and says before a
  capture starts what it will adjust.
- Every capture's record keeps what was **requested** (no sweep count for timed
  captures), what the **radio used**, and what the **file contains** (actual bin
  width, FFT size, complete and partial sweeps, time span, sweep rate), plus
  start and finish times with their UTC offset, and flags any disagreement.
- `rfscoper info`: those three views side by side, for new and older captures.
- `--dry-run` prints the `hackrf_sweep` command and what it will really do.
- `rfscoper list`: saved captures with date, range, length and note.

### Analysis

- `rfscoper show`: signals above the noise floor with width, duty cycle and
  strength, plus a spectrum and waterfall plot with labeled signals.
- Noise floor estimated across frequency and across time, so both steady
  stations and band-filling hopping signals are detected.
- `rfscoper compare`: overlays 2 to 4 captures on a shared frequency axis,
  lists what is stronger than in the baseline, and plots the difference.

### Identification

- A catalog of 47 common US signal types (`bands.CATALOG`), each described by
  frequency, channels, bandwidth and behavior.
- Every signal gets a best guess with a confidence label and, with
  `--explain`, the reasoning behind it.
- Tells Bluetooth Low Energy advertising apart from hopping Classic Bluetooth.
- FM stations are recognized with or without HD Radio sidebands (up to ~400 kHz wide).
- Proximity hints for strong low-power devices.
- `rfscoper catalog [SEARCH]` and `rfscoper identify MHZ`.

### Near-field probing

- `rfscoper probe start/spot/report`: a reference capture plus one capture
  per object, and a report of every signal's gain at each spot, with HOT /
  warm / same-everywhere verdicts, identification, and a heatmap.
- `rfscoper probe live`: a live signal-strength meter streamed from
  `hackrf_sweep`, optionally watching one frequency.
- Probe defaults keep the HackRF safe: low LNA gain, amplifier never enabled.
- `docs/near-field-probe.md`: buying or building a loop probe, input limits,
  and the method.

### Project

- `--json` output on `list`, `show`, `compare`, `catalog` and `identify`.
- Library API: `load_sweeps`, `analysis`, `bands`, `identify`.
- Demo data generator with four simulated captures and a probe session,
  laid out exactly like real `hackrf_sweep` output, so the tool can be tried
  without a HackRF.
- Code walkthrough in `docs/how-sweeps-work.md`.
- CI on Python 3.10-3.14 with ruff, mypy and pytest.

[Unreleased]: https://github.com/su-daedalus/rfscoper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/su-daedalus/rfscoper/releases/tag/v0.1.0
