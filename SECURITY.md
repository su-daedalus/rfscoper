# Security and privacy

## What rfscoper does and doesn't do

- **Receive only.** rfscoper runs `hackrf_sweep`, which only listens. Nothing in
  this project transmits.
- **Fully offline.** No part of rfscoper opens a network connection: no
  telemetry, no update checks, no map tiles, no web views. The scope window
  works with networking switched off.
- **Never decodes content.** A sweep measures power per frequency. rfscoper
  can't read, record or play what a signal says.
- **Protects the radio.** The live scope and the `probe` commands never enable
  the HackRF's RF amplifier, which keeps the input within its documented limit
  when a probe is held close to a transmitter.

## Your captures are private data

A capture records what was transmitting where you stood, when, plus your
notes about the place (rooms, objects, devices). So rfscoper saves them in
`~/rfscoper-captures/`, **outside the project folder**, where `git add` can't
reach them. As a second line of defense, the `.gitignore` also keeps every
capture, record, plot, probe session and packet capture out of the repository:
`captures/`, `*.csv`, `*.meta.json`, `*.png` (except the README screenshots
in `docs/images/`), `*.pcap` and raw IQ files. Before sharing a capture
anyway, read its `.meta.json` and remove the note if it says where you were.

The screenshots and demo captures in this repository are simulated (see
`examples/make_demo_data.py`) and contain no real measurements.

## Reporting a vulnerability

Please don't open a public issue for a security problem. Use GitHub's
**Report a vulnerability** button on the repository's Security tab, which
reaches the maintainer privately. Include the steps to reproduce it and the
version (`rfscoper --version`).
