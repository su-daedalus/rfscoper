# Contributing

Thanks for your interest in rfscoper. Bug reports, feature ideas and pull
requests are all welcome.

## Setup

```bash
git clone https://github.com/su-daedalus/rfscoper.git
cd rfscoper
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The test suite needs no hardware: `hackrf_sweep` is replaced by small fake
scripts in `tests/`.

## Before opening a pull request

```bash
ruff format .      # formatting
ruff check .       # lint
mypy               # type checks
pytest --cov       # tests
```

CI runs the same four commands on Python 3.10 through 3.14.

## Guidelines

- One change per pull request, with a short description of *why*.
- Add or update a test for any behavior change.
- Keep functions small and typed; docstrings explain intent, not syntax.
- Add a line under `[Unreleased]` in `CHANGELOG.md`.
- Never commit captures or other RF data. `.gitignore` blocks the usual
  formats, and the same rule applies to anything it misses.

## Reporting bugs

Include your `rfscoper --version`, the exact command, and the first lines of
the capture's `.meta.json` (it records the settings used). Please do not
attach full captures unless asked; they record what was transmitting around you.
