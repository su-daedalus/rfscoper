"""rfscoper: capture, analyze and compare HackRF spectrum sweeps."""

__version__ = "0.1.0"

from rfscoper.parser import SweepData, load_sweeps

__all__ = ["SweepData", "__version__", "load_sweeps"]
