"""Harmonic spectrum -> wavetable.

The LLM never writes a wave and never writes a filter cutoff. It writes a
*description of harmonic content*, which is a domain where language models
have strong, reliable intuition, and then the engine synthesizes the
table from it.

This is the trick that keeps the system honest: the model says "odd-
weighted, saw-to-square tilt", the engine decides what that means in
partials and how many samples the table needs.
"""

from __future__ import annotations

import numpy as np

from .spec import SpectrumSpec

TABLE_SIZE = 2048


def harmonic_amplitudes(spectrum: SpectrumSpec) -> np.ndarray:
    """Derive per-harmonic amplitudes from a semantic spectrum description.

    Returns an array indexed by harmonic number (index 0 is unused, index 1
    is the fundamental).
    """
    if spectrum.partials:
        amps = np.zeros(max(p.n for p in spectrum.partials) + 1, dtype=np.float64)
        for p in spectrum.partials:
            amps[p.n] = p.amp
        return amps

    k_max = int(round(4.0 + spectrum.brightness * 44.0))
    amps = np.zeros(k_max + 1, dtype=np.float64)

    # Spectral tilt: negative rolls off fast (dark), positive rolls off
    # slowly (bright). Exponent of the 1/k^n family.
    exponent = 1.0 - spectrum.tilt * 0.9

    # Formant: a resonant bump placed somewhere in the series.
    formant_k = 2.0 + spectrum.formant * (k_max - 2)

    for k in range(1, k_max + 1):
        amp = k ** (-exponent)

        # Emphasis is a *balance*, not just a boost: pushing the odds up
        # has to push the evens down, otherwise it is not a shift at all.
        if k % 2 == 1:
            amp *= 1.0 + spectrum.odd_emphasis * 0.9
            amp *= 1.0 - spectrum.even_emphasis * 0.5
        else:
            amp *= 1.0 + spectrum.even_emphasis * 0.9
            amp *= 1.0 - spectrum.odd_emphasis * 0.5
        amp = max(0.0, amp)

        if spectrum.formant > 0.0:
            width = 2.5
            bump = np.exp(-(((k - formant_k) / width) ** 2))
            amp *= 1.0 + spectrum.formant * 1.5 * bump

        amps[k] = amp

    return amps


def band_limit(amps: np.ndarray, max_harmonic: int) -> np.ndarray:
    """Zero out harmonics above a limit (aliasing guard)."""
    out = amps.copy()
    if max_harmonic + 1 < len(out):
        out[max_harmonic + 1 :] = 0.0
    return out


def build_wavetable(amps: np.ndarray, size: int = TABLE_SIZE) -> np.ndarray:
    """Synthesize one cycle of a wavetable from harmonic amplitudes.

    The table is normalized to peak 1.0. The fundamental completes exactly
    `size` samples, so table lookup with a wrapping phase is exactly periodic.
    """
    if size < 8:
        raise ValueError("wavetable size must be >= 8")

    t = np.arange(size, dtype=np.float64) / size
    wave = np.zeros(size, dtype=np.float64)

    for k in range(1, len(amps)):
        if amps[k] == 0.0:
            continue
        wave += amps[k] * np.sin(2.0 * np.pi * k * t)

    peak = float(np.max(np.abs(wave)))
    if peak > 0.0:
        wave /= peak

    return wave.astype(np.float64)


def wavetable_for_midi(
    spectrum: SpectrumSpec,
    midi_note: int,
    sample_rate: int,
    size: int = TABLE_SIZE,
) -> np.ndarray:
    """Build a band-limited table for a specific pitch.

    Harmonics above Nyquist for this pitch are removed, which is what keeps
    a bright patch from turning into aliased hash when it is played high.
    """
    f0 = 440.0 * 2.0 ** ((midi_note - 69) / 12.0)
    max_harmonic = int((sample_rate * 0.5) / max(f0, 1.0)) - 1
    max_harmonic = max(1, min(max_harmonic, len(harmonic_amplitudes(spectrum)) - 1))

    amps = band_limit(harmonic_amplitudes(spectrum), max_harmonic)
    return build_wavetable(amps, size)


def table_spectrum(table: np.ndarray, harmonics: int = 32) -> np.ndarray:
    """Analyze a wavetable back into harmonic amplitudes. Used by the
    measurement pass so we can report what a patch is actually made of."""
    n = len(table)
    spectrum = np.abs(np.fft.rfft(table)) * 2.0 / n
    return spectrum[: harmonics + 1]


def spectral_centroid(table: np.ndarray) -> float:
    """Brightness of a table in Hz-of-harmonic-number terms."""
    spec = table_spectrum(table, 64)
    k = np.arange(len(spec))
    total = float(spec.sum())
    if total <= 0.0:
        return 0.0
    return float((spec * k).sum() / total)
