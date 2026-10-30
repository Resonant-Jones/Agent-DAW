"""Deterministic context drums.

A patch heard in isolation is a different patch than the same patch under a
beat. Judging timbre needs context, so every audition render carries a
simple drum bed. It is deliberately mechanical and repeatable -- this is a
reference surface for listening, not a creative element.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import lfilter

from .dsp import lfo_signal


def _kick(sr: int, rng: np.random.Generator) -> np.ndarray:
    n = int(0.32 * sr)
    t = np.arange(n) / sr
    # pitch envelope from 120 Hz down to 45 Hz
    freq = 45.0 + 95.0 * np.exp(-t / 0.028)
    phase = np.cumsum(freq) / sr
    body = np.sin(2.0 * np.pi * phase) * np.exp(-t / 0.11)
    click = rng.normal(0, 1, n) * np.exp(-t / 0.004) * 0.25
    return body * 0.9 + click


def _snare(sr: int, rng: np.random.Generator) -> np.ndarray:
    n = int(0.22 * sr)
    t = np.arange(n) / sr
    noise = rng.normal(0, 1, n)
    # bandpass around 180 Hz
    noise = lfilter([0.5, 0.0, -0.5], [1.0, 0.0, 0.0], noise)
    noise = lfilter([0.4, 0.0, -0.4], [1.0, 0.0, 0.0], noise)
    body = np.sin(2.0 * np.pi * 185.0 * t) * np.exp(-t / 0.045) * 0.5
    rattle = noise * np.exp(-t / 0.075) * 0.8
    return body + rattle


def _hat(sr: int, rng: np.random.Generator, open_: bool = False) -> np.ndarray:
    n = int((0.14 if open_ else 0.045) * sr)
    t = np.arange(n) / sr
    noise = rng.normal(0, 1, n)
    # crude highpass: difference twice
    noise = np.diff(noise, prepend=0.0)
    noise = np.diff(noise, prepend=0.0)
    return noise * np.exp(-t / (0.02 if not open_ else 0.07)) * 0.5


def render_drums(
    sample_rate: int = 44100,
    bpm: float = 120.0,
    bars: int = 8,
    beats_per_bar: int = 4,
    seed: int = 1234,
    style: str = "straight",
) -> np.ndarray:
    """Render a simple, repeatable drum bed."""
    rng = np.random.default_rng(seed)
    spb = 60.0 / bpm
    total = int(bars * beats_per_bar * spb * sample_rate) + sample_rate
    out = np.zeros(total, dtype=np.float64)

    kick = _kick(sample_rate, rng)
    snare = _snare(sample_rate, rng)
    hat = _hat(sample_rate, rng)
    hat_open = _hat(sample_rate, rng, open_=True)

    def place(buf: np.ndarray, at: int, gain: float) -> None:
        if at + len(buf) > len(out):
            buf = buf[: max(0, len(out) - at)]
        if len(buf):
            out[at:at + len(buf)] += buf * gain

    for bar in range(bars):
        bar_start = bar * beats_per_bar * spb * sample_rate
        for beat in range(beats_per_bar):
            t0 = bar_start + beat * spb * sample_rate

            # kick on 1 and 3, with a light pickup before the turnaround
            if beat in (0, 2):
                place(kick, int(t0), 0.95)
            if bar % 4 == 3 and beat == 3:
                place(kick, int(t0 + spb * 0.75 * sample_rate), 0.55)

            # snare on 2 and 4
            if beat in (1, 3):
                place(snare, int(t0), 0.6)

            # eighth hats, with velocity accents
            for eighth in range(2):
                t8 = t0 + eighth * spb * 0.5 * sample_rate
                accent = 0.5 if eighth == 0 else 0.32
                if bar % 4 == 3 and beat == 3 and eighth == 1:
                    place(hat_open, int(t8), 0.26)
                else:
                    place(hat, int(t8), accent)

    out = out / max(float(np.max(np.abs(out))), 1e-9) * 0.7
    return out
