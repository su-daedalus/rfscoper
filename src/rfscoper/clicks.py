"""Geiger-counter clicks, generated locally and played through ALSA.

A background thread writes 16-bit mono audio to `aplay` (or `pacat` on
PulseAudio/PipeWire systems). Clicks arrive at random intervals whose average
follows the rate the scope sets, like a real counter near a source: a few per
second near the baseline, a buzz near a strong signal. Nothing is downloaded
and nothing leaves the machine.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

import numpy as np

RATE_HZ = 22050
CHUNK_S = 0.025  # audio written in 25 ms pieces
CLICK_S = 0.004

PLAYERS = (
    ["aplay", "-q", "-f", "S16_LE", "-r", str(RATE_HZ), "-c", "1", "-B", "60000", "-"],
    ["pacat", "--raw", "--format=s16le", f"--rate={RATE_HZ}", "--channels=1", "--latency-msec=60"],
)


def _click_wave(rng: np.random.Generator) -> np.ndarray:
    n = int(RATE_HZ * CLICK_S)
    envelope = np.exp(-np.arange(n) / (n / 5))
    return (rng.normal(0, 0.6, n) * envelope).clip(-1, 1)


class ClickStream:
    """Plays clicks at `rate` per second until stopped; `available` says if it can."""

    def __init__(self) -> None:
        self.rate = 0.0
        self.volume = 0.6
        self.available = any(shutil.which(cmd[0]) for cmd in PLAYERS)
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rng = np.random.default_rng()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return True
        for cmd in PLAYERS:
            if shutil.which(cmd[0]) is None:
                continue
            try:
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except OSError:
                continue
            break
        if self._proc is None or self._proc.stdin is None:
            self.available = False
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="geiger-audio", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()

    def _run(self) -> None:
        chunk_n = int(RATE_HZ * CHUNK_S)
        click = _click_wave(self._rng)
        pending: list[np.ndarray] = []  # tail of a click that spills into the next chunk
        next_click_in = self._wait()
        proc = self._proc
        while not self._stop.is_set() and proc is not None and proc.stdin is not None:
            chunk = np.zeros(chunk_n)
            if pending:
                tail = pending.pop()
                chunk[: len(tail)] += tail[:chunk_n]
                if len(tail) > chunk_n:
                    pending.append(tail[chunk_n:])
            t = 0
            while True:
                t += int(next_click_in * RATE_HZ)
                if t >= chunk_n:
                    next_click_in = (t - chunk_n) / RATE_HZ
                    break
                end = min(chunk_n, t + len(click))
                chunk[t:end] += click[: end - t]
                if end - t < len(click):
                    pending.append(click[end - t :])
                next_click_in = self._wait()
            samples = (chunk.clip(-1, 1) * self.volume * 32767).astype("<i2")
            try:
                proc.stdin.write(samples.tobytes())
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                break
            time.sleep(CHUNK_S * 0.8)

    def _wait(self) -> float:
        """Seconds until the next click: exponential, like radioactive decay."""
        rate = max(self.rate, 1e-6)
        return float(self._rng.exponential(1.0 / rate))
