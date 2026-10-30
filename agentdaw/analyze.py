"""Measurement.

The Producer cannot hear. It can measure. So instead of asking a language
model to opine about whether a mix is good, this pass computes the numbers
that actually correlate with the complaints an artist actually makes, and
the Producer reports those.

This is the difference between a producer that guesses and a producer that
has instruments.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.signal import lfilter

from .biquad import highpass_coeffs, shelving_high_coeffs


@dataclass
class Measurements:
    peak_dbfs: float = 0.0
    rms_dbfs: float = -120.0
    crest_factor_db: float = 0.0
    dc_offset: float = 0.0
    spectral_centroid_hz: float = 0.0
    spectral_rolloff_85_hz: float = 0.0
    spectral_flatness: float = 0.0
    low_energy_db: float = -120.0
    mid_energy_db: float = -120.0
    high_energy_db: float = -120.0
    integrated_lufs: float = -70.0
    loudness_range_lu: float = 0.0
    stereo_width: float = 0.0
    sample_rate: int = 44100
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _to_db(x: float, floor: float = -120.0) -> float:
    return max(floor, 20.0 * np.log10(max(x, 1e-12)))


def _block_loudness(y: np.ndarray, block: int, hop: int) -> np.ndarray:
    """Per-block loudness in LUFS, per BS.1770-4.

    The channel energies are SUMMED, not averaged. Averaging instead of
    summing biases every stereo measurement by exactly 10*log10(2) =
    3.01 LU low, which is subtle enough to survive a casual sanity check
    and wrong enough to make every reading suspect.
    """
    n_blocks = 1 + (len(y) - block) // hop
    values = np.empty(n_blocks, dtype=np.float64)
    for i in range(n_blocks):
        seg = y[i * hop:i * hop + block]
        z = float(np.mean(np.sum(np.square(seg), axis=1)))
        values[i] = -0.691 + 10.0 * np.log10(max(z, 1e-12))
    return values


def integrated_lufs(signal: np.ndarray, sample_rate: int) -> float:
    """ITU-R BS.1770-4 integrated loudness with K-weighting and gating."""
    if signal.ndim == 1:
        signal = signal[:, None]

    # K-weighting: high-shelf pre-filter, then RLB high-pass.
    b1, a1 = shelving_high_coeffs(1681.974450955533, 3.999843853973347, 0.7071752369554196, sample_rate)
    b2, a2 = highpass_coeffs(38.13547087602444, 0.5003270373238773, sample_rate)

    y = lfilter(b1, a1, signal, axis=0)
    y = lfilter(b2, a2, y, axis=0)

    block = int(0.4 * sample_rate)
    hop = int(0.1 * sample_rate)
    if len(y) < block:
        return -70.0

    powers = _block_loudness(y, block, hop)

    absolute = powers > -70.0
    if not absolute.any():
        return -70.0

    ungated = 10.0 ** ((powers[absolute] + 0.691) / 10.0)
    relative_threshold = (-0.691 + 10.0 * np.log10(ungated.mean())) - 10.0

    keep = absolute & (powers > relative_threshold)
    if not keep.any():
        return -70.0

    z = 10.0 ** ((powers[keep] + 0.691) / 10.0)
    return float(-0.691 + 10.0 * np.log10(z.mean()))


def analyze(signal: np.ndarray, sample_rate: int = 44100) -> Measurements:
    """Measure an audio buffer."""
    if signal.ndim == 1:
        signal = signal[:, None]

    mono = signal.mean(axis=1)
    m = Measurements(sample_rate=sample_rate, duration_s=len(mono) / sample_rate)

    if len(mono) == 0:
        return m

    peak = float(np.max(np.abs(signal)))
    rms = float(np.sqrt(np.mean(np.square(mono))))
    m.peak_dbfs = _to_db(peak)
    m.rms_dbfs = _to_db(rms)
    m.crest_factor_db = m.peak_dbfs - m.rms_dbfs
    m.dc_offset = float(np.mean(mono))

    # Spectral descriptors.
    n = len(mono)
    window = np.hanning(n)
    spectrum = np.abs(np.fft.rfft(mono * window))
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    total = float(spectrum.sum())
    if total > 0:
        m.spectral_centroid_hz = float((spectrum * freqs).sum() / total)
        cumulative = np.cumsum(spectrum) / total
        idx = int(np.searchsorted(cumulative, 0.85))
        m.spectral_rolloff_85_hz = float(freqs[min(idx, len(freqs) - 1)])
        log_spec = np.log(spectrum + 1e-12)
        m.spectral_flatness = float(np.exp(log_spec.mean()) / (spectrum.mean() + 1e-12))

    def band_energy_db(lo: float, hi: float) -> float:
        mask = (freqs >= lo) & (freqs < hi)
        if not mask.any():
            return -120.0
        return _to_db(float(np.sqrt(np.mean(np.square(spectrum[mask])))))

    m.low_energy_db = band_energy_db(20.0, 200.0)
    m.mid_energy_db = band_energy_db(200.0, 2000.0)
    m.high_energy_db = band_energy_db(2000.0, min(sample_rate / 2, 20000.0))

    m.integrated_lufs = integrated_lufs(signal, sample_rate)

    # Loudness range, approximated from the spread of 400 ms block loudness.
    block = int(0.4 * sample_rate)
    hop = int(0.1 * sample_rate)
    if len(mono) >= block:
        b1, a1 = shelving_high_coeffs(1681.974450955533, 3.999843853973347, 0.7071752369554196, sample_rate)
        b2, a2 = highpass_coeffs(38.13547087602444, 0.5003270373238773, sample_rate)
        weighted = lfilter(b2, a2, lfilter(b1, a1, signal, axis=0), axis=0)
        vals = _block_loudness(weighted, block, hop)
        keep = vals > -70.0
        if keep.any():
            m.loudness_range_lu = float(np.percentile(vals[keep], 95) - np.percentile(vals[keep], 10))

    if signal.shape[1] >= 2:
        left, right = signal[:, 0], signal[:, 1]
        corr = float(np.corrcoef(left, right)[0, 1]) if np.std(left) > 0 and np.std(right) > 0 else 0.0
        m.stereo_width = float(np.sqrt(max(0.0, 1.0 - corr)))

    return m


def report_findings(measurements: Measurements, track_name: str = "") -> list[str]:
    """Turn measurements into the kind of sentences a producer would say.

    Deliberately factual. No taste claims, no "this sounds muddy" -- just
    "the low band is 6 dB above the mid band", which the artist can act on.
    """
    out: list[str] = []
    m = measurements
    label = f"{track_name}: " if track_name else ""

    if m.peak_dbfs > -0.5:
        out.append(f"{label}peak is {m.peak_dbfs:.1f} dBFS -- close to clipping.")
    if m.integrated_lufs > -9.0:
        out.append(f"{label}integrated loudness is {m.integrated_lufs:.1f} LUFS, above the -14 streaming target.")
    elif m.integrated_lufs < -24.0:
        out.append(f"{label}integrated loudness is {m.integrated_lufs:.1f} LUFS, very quiet.")

    low_mid = m.low_energy_db - m.mid_energy_db
    if low_mid > 6.0:
        out.append(f"{label}low band sits {low_mid:.1f} dB above the mid band -- possible low-end buildup.")
    elif low_mid < -9.0:
        out.append(f"{label}low band sits {abs(low_mid):.1f} dB below the mid band -- thin bottom end.")

    if m.spectral_centroid_hz > 6000:
        out.append(f"{label}spectral centroid is {m.spectral_centroid_hz:.0f} Hz -- bright and exposed.")
    elif m.spectral_centroid_hz < 900:
        out.append(f"{label}spectral centroid is {m.spectral_centroid_hz:.0f} Hz -- very dark.")

    if m.crest_factor_db < 6.0:
        out.append(f"{label}crest factor is only {m.crest_factor_db:.1f} dB -- heavily limited, little dynamics left.")

    if m.stereo_width < 0.05 and m.duration_s > 0:
        out.append(f"{label}stereo width is {m.stereo_width:.2f} -- effectively mono.")

    return out
