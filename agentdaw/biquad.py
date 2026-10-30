"""Biquad coefficients (RBJ audio-EQ-cookbook forms) and a block-wise filter.

Why RBJ and not a zero-delay-feedback SVF: the ZDF SVF is prettier, but
its biquad equivalent is fiddly to derive and easy to get subtly wrong.
The RBJ forms are textbook, verifiable, and stable across the whole
operating range we use. We recompute coefficients per block so the cutoff
can be swept by an envelope, and use `scipy.signal.lfilter` with carried
state so each block is exact rather than approximated.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import lfilter


def lowpass_coeffs(fc: float, q: float, sample_rate: float) -> tuple[np.ndarray, np.ndarray]:
    """RBJ lowpass. fc in Hz, q in dB-ish (0.7071 = Butterworth)."""
    fc = float(np.clip(fc, 10.0, sample_rate * 0.49))
    q = float(np.clip(q, 0.05, 30.0))
    w0 = 2.0 * np.pi * fc / sample_rate
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)

    b = np.array([(1.0 - cos_w0) / 2.0, 1.0 - cos_w0, (1.0 - cos_w0) / 2.0])
    a = np.array([1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha])
    return b / a[0], a / a[0]


def highpass_coeffs(fc: float, q: float, sample_rate: float) -> tuple[np.ndarray, np.ndarray]:
    """RBJ highpass."""
    fc = float(np.clip(fc, 10.0, sample_rate * 0.49))
    q = float(np.clip(q, 0.05, 30.0))
    w0 = 2.0 * np.pi * fc / sample_rate
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)

    b = np.array([(1.0 + cos_w0) / 2.0, -(1.0 + cos_w0), (1.0 + cos_w0) / 2.0])
    a = np.array([1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha])
    return b / a[0], a / a[0]


def bandpass_coeffs(fc: float, q: float, sample_rate: float) -> tuple[np.ndarray, np.ndarray]:
    """RBJ bandpass (constant skirt gain)."""
    fc = float(np.clip(fc, 10.0, sample_rate * 0.49))
    q = float(np.clip(q, 0.05, 30.0))
    w0 = 2.0 * np.pi * fc / sample_rate
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)

    b = np.array([alpha, 0.0, -alpha])
    a = np.array([1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha])
    return b / a[0], a / a[0]


def shelving_high_coeffs(fc: float, gain_db: float, q: float, sample_rate: float) -> tuple[np.ndarray, np.ndarray]:
    """RBJ high shelf. Used by the K-weighting stage of the loudness meter."""
    fc = float(np.clip(fc, 10.0, sample_rate * 0.49))
    gain_db = float(np.clip(gain_db, -24.0, 24.0))
    q = float(np.clip(q, 0.05, 30.0))
    a_amp = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * fc / sample_rate
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)

    beta = 2.0 * np.sqrt(a_amp) * alpha
    b = np.array([
        a_amp * ((a_amp + 1.0) + (a_amp - 1.0) * cos_w0 + beta),
        -2.0 * a_amp * ((a_amp - 1.0) + (a_amp + 1.0) * cos_w0),
        a_amp * ((a_amp + 1.0) + (a_amp - 1.0) * cos_w0 - beta),
    ])
    a = np.array([
        (a_amp + 1.0) - (a_amp - 1.0) * cos_w0 + beta,
        2.0 * ((a_amp - 1.0) - (a_amp + 1.0) * cos_w0),
        (a_amp + 1.0) - (a_amp - 1.0) * cos_w0 - beta,
    ])
    return b / a[0], a / a[0]


def apply_filter(
    signal: np.ndarray,
    b: np.ndarray,
    a: np.ndarray,
    zi: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Filter along the last axis, returning the filter state."""
    if zi is None:
        # scipy returns a bare array when no initial state is supplied.
        y = lfilter(b, a, signal, axis=-1)
        return y, np.zeros(max(len(b), len(a)) - 1, dtype=np.float64)
    y, zf = lfilter(b, a, signal, axis=-1, zi=zi)
    return y, zf


def sweep_filter(
    signal: np.ndarray,
    cutoffs: np.ndarray,
    q: float,
    sample_rate: int,
    block: int = 256,
) -> np.ndarray:
    """Filter with a cutoff that varies over time.

    `cutoffs` is one value per block of `block` samples. Coefficients are
    held constant within a block (piecewise-constant), which is exact for
    the filter itself and smooth enough for musical envelopes at this
    block size (256 samples is 5.8 ms at 44.1 kHz).

    Multi-channel input is filtered one channel at a time. SciPy's lfilter
    wants one initial-state vector per *row* for 2D input, which is not
    what a shared filter needs, and filtering the stereo channels
    independently is what preserves the stereo image anyway.
    """
    if signal.ndim == 1:
        return _sweep_1d(signal, cutoffs, q, sample_rate, block)

    flat = signal.T  # (channels, samples)
    out = np.empty_like(flat)
    for channel in range(flat.shape[0]):
        out[channel] = _sweep_1d(flat[channel], cutoffs, q, sample_rate, block)
    return out.T


def _sweep_1d(
    signal: np.ndarray,
    cutoffs: np.ndarray,
    q: float,
    sample_rate: int,
    block: int,
) -> np.ndarray:
    n = len(signal)
    out = np.empty_like(signal)
    zi: np.ndarray | None = None

    n_blocks = (n + block - 1) // block
    for i in range(n_blocks):
        start = i * block
        stop = min(start + block, n)
        fc = float(cutoffs[min(i, len(cutoffs) - 1)])
        b, a = lowpass_coeffs(fc, q, sample_rate)
        chunk, zi = apply_filter(signal[start:stop], b, a, zi)
        out[start:stop] = chunk

    return out
