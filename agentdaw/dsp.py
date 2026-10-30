"""The synthesis engine.

Everything here is numpy-vectorized. There are no per-sample Python loops:
the trick that makes that possible is that a feedback delay line can be
written as a gather-then-scatter over a buffer, provided the read distance
is at least one block length. That constraint is enforced, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import fftconvolve, lfilter

from .macros import EngineParams, resolve_patch
from .spec import MidiNote, PatchSpec
from .wavetable import TABLE_SIZE, wavetable_for_midi

BLOCK = 64  # delay-line block size; read distance must exceed this


# --------------------------------------------------------------------------
# Envelopes and modulation
# --------------------------------------------------------------------------

def adsr(
    total: int,
    attack: int,
    decay: int,
    sustain: float,
    release: int,
) -> np.ndarray:
    """Classic ADSR over a buffer of `total` samples.

    Each segment runs to its endpoint exactly: the attack reaches 1.0, the
    decay lands on sustain, and the release reaches 0.0 on the final
    sample. A release that stops one step short leaves a click.
    """
    env = np.zeros(total, dtype=np.float64)
    if total <= 0:
        return env

    a = int(min(max(attack, 0), total))
    if a > 0:
        env[:a] = np.linspace(0.0, 1.0, a)

    d = int(min(max(decay, 0), total - a))
    if d > 0:
        env[a:a + d] = np.linspace(1.0, sustain, d)

    s_start = a + d
    r_start = max(s_start, total - release)
    if r_start > s_start:
        env[s_start:r_start] = sustain

    if r_start < total:
        env[r_start:] = np.linspace(sustain, 0.0, total - r_start)

    return env


def lfo_signal(n: int, rate_hz: float, shape: str, sample_rate: int, phase: float = 0.0) -> np.ndarray:
    """LFO in the range -1..1."""
    t = np.arange(n, dtype=np.float64) / sample_rate + phase
    if shape == "triangle":
        return 2.0 * np.abs(2.0 * ((t * rate_hz) % 1.0) - 1.0) - 1.0
    if shape == "saw":
        return 2.0 * ((t * rate_hz) % 1.0) - 1.0
    return np.sin(2.0 * np.pi * rate_hz * t)


def midi_to_hz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


# --------------------------------------------------------------------------
# Oscillator
# --------------------------------------------------------------------------

def oscillator(
    table: np.ndarray,
    freq_hz: float,
    n: int,
    sample_rate: int,
    detune_cents: float = 0.0,
    phase_offset: float = 0.0,
    vibrato: np.ndarray | None = None,
    vibrato_depth: float = 0.0,
) -> np.ndarray:
    """One wavetable voice, band-limited by the table it is given."""
    if n <= 0:
        return np.zeros(0)

    inc = freq_hz * (2.0 ** (detune_cents / 1200.0)) / sample_rate
    if vibrato is not None and vibrato_depth > 0.0:
        # +/- vibrato_depth percent of pitch
        inc_arr = inc * (2.0 ** (vibrato * vibrato_depth * 0.02))
    else:
        inc_arr = np.full(n, inc, dtype=np.float64)

    phase = np.mod(np.cumsum(inc_arr) + phase_offset, 1.0)

    size = len(table)
    idx = phase * size
    i0 = np.floor(idx).astype(np.int64) % size
    frac = idx - np.floor(idx)
    i1 = (i0 + 1) % size

    return table[i0] * (1.0 - frac) + table[i1] * frac


def unison_voices(
    table: np.ndarray,
    freq_hz: float,
    n: int,
    sample_rate: int,
    params: EngineParams,
    seed: int = 0,
    vibrato: np.ndarray | None = None,
) -> np.ndarray:
    """Sum of detuned unison voices, spread across the stereo field.

    Returns an (n, 2) buffer. The `size` macro controls both how many
    voices there are and how far they are spread, which is what makes a
    wide patch actually wide rather than a mono patch with a reverb on it.
    """
    voices = max(1, params.unison_voices)
    vibrato_depth = params.lfo_depth if params.lfo_target == "pitch" else 0.0

    if voices == 1:
        voice = oscillator(table, freq_hz, n, sample_rate,
                           vibrato=vibrato, vibrato_depth=vibrato_depth)
        return np.column_stack([voice, voice])

    rng = np.random.default_rng(seed)
    out = np.zeros((n, 2), dtype=np.float64)

    for v in range(voices):
        spread = (v / (voices - 1)) * 2.0 - 1.0
        cents = spread * params.detune_cents + float(rng.uniform(-1.5, 1.5))
        voice = oscillator(
            table, freq_hz, n, sample_rate,
            detune_cents=cents,
            phase_offset=float(rng.random()),
            vibrato=vibrato,
            vibrato_depth=vibrato_depth,
        )

        pan = spread * params.width
        angle = (pan + 1.0) * (np.pi / 4.0)
        out[:, 0] += voice * np.cos(angle)
        out[:, 1] += voice * np.sin(angle)

    return out / np.sqrt(voices)


# --------------------------------------------------------------------------
# Delay-line effects (vectorized gather/scatter)
# --------------------------------------------------------------------------

def _read_buffer(buf: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """Linear-interpolated read from a circular buffer at absolute positions."""
    n = len(buf)
    floor_pos = np.floor(pos)
    frac = pos - floor_pos
    i0 = np.mod(floor_pos.astype(np.int64), n)
    i1 = (i0 + 1) % n
    return buf[i0] * (1.0 - frac) + buf[i1] * frac


def feedback_delay(signal: np.ndarray, delay_samples: int, feedback: float, sample_rate: int) -> np.ndarray:
    """Feedback delay with a tail that is flushed past the end of the input.

    The flush is two delay lengths, not one. Flushing only one delay length
    drops the final echo, which is exactly the echo you want to hear when
    the note ends.

    Multi-channel input is delayed independently per channel so the stereo
    image survives.
    """
    delay_samples = int(max(BLOCK + 1, round(delay_samples)))
    tail = delay_samples * 2
    total = len(signal) + tail
    buf_len = int(delay_samples * 2 + 4096)

    was_mono = signal.ndim == 1
    if was_mono:
        signal = signal[:, None]

    ext = np.concatenate([signal, np.zeros((tail, signal.shape[1]))])
    out = np.zeros((total, signal.shape[1]), dtype=np.float64)

    for channel in range(signal.shape[1]):
        buf = np.zeros(buf_len, dtype=np.float64)
        w = 0
        for start in range(0, total, BLOCK):
            chunk = ext[start:start + BLOCK, channel]
            b = len(chunk)
            if b == 0:
                break
            abs_idx = np.arange(w, w + b, dtype=np.float64)
            read = _read_buffer(buf, abs_idx - delay_samples)
            y = chunk + feedback * read
            buf[np.mod(abs_idx.astype(np.int64), buf_len)] = y
            out[start:start + b, channel] = y
            w += b

    return out[:, 0] if was_mono else out


def chorus(signal: np.ndarray, rate_hz: float, depth_ms: float, mix: float, sample_rate: int) -> np.ndarray:
    """Modulated delay chorus.

    Returns the stereo *wet* signal; the caller does the dry/wet blend so
    the original stereo image is preserved rather than being collapsed to
    mono on the way through.
    """
    depth_samples = (depth_ms / 1000.0) * sample_rate
    base_delay = (0.020 + depth_ms / 1000.0) * sample_rate  # guarantees base - depth > BLOCK

    n = len(signal)
    buf_len = int(base_delay + depth_samples + 4096)

    lfo_l = lfo_signal(n, rate_hz, "sine", sample_rate)
    lfo_r = lfo_signal(n, rate_hz, "sine", sample_rate, phase=0.37)

    out = np.zeros((n, 2), dtype=np.float64)
    bufs = [np.zeros(buf_len, dtype=np.float64), np.zeros(buf_len, dtype=np.float64)]

    w = 0
    for start in range(0, n, BLOCK):
        stop = min(start + BLOCK, n)
        b = stop - start
        abs_idx = np.arange(w, w + b, dtype=np.float64)
        for ch, (lfo, buf) in enumerate(zip((lfo_l, lfo_r), bufs)):
            mod = lfo[start:stop] * depth_samples
            read = _read_buffer(buf, abs_idx - base_delay - mod)
            wet = 0.5 * (read + _read_buffer(buf, abs_idx - base_delay * 0.5 - mod))
            buf[np.mod(abs_idx.astype(np.int64), buf_len)] = signal[start:stop]
            out[start:stop, ch] = wet
        w += b

    return out


def make_reverb_ir(sample_rate: int, time_s: float, seed: int, predelay_s: float = 0.02) -> np.ndarray:
    """Synthesize a plausible reverb impulse response: decaying noise with a
    lowpassed tail and a handful of early reflections."""
    n = int(max(0.05, time_s) * sample_rate)
    rng = np.random.default_rng(seed)

    t = np.arange(n, dtype=np.float64) / sample_rate
    ir = rng.normal(0.0, 1.0, n) * np.exp(-t / max(time_s * 0.33, 0.01))

    # one-pole lowpass so the tail darkens the way a real room does
    ir = lfilter([0.25], [1.0, -0.75], ir)

    # early reflections
    for delay_ms, gain in ((11.0, 0.55), (19.0, 0.4), (29.0, 0.32), (43.0, 0.24)):
        d = int(delay_ms / 1000.0 * sample_rate)
        if d < n:
            ir[d] += gain * rng.choice([-1.0, 1.0])

    peak = float(np.max(np.abs(ir)))
    if peak > 0.0:
        ir /= peak

    pre = int(predelay_s * sample_rate)
    return np.concatenate([np.zeros(pre), ir])


def apply_reverb(signal: np.ndarray, mix: float, sample_rate: int, time_s: float, seed: int) -> np.ndarray:
    """Stereo convolution reverb.

    Returns the stereo *wet* signal; the caller blends it with the dry
    signal so the original stereo image survives.
    """
    if mix <= 0.0:
        return np.zeros((len(signal), 2))

    ir_l = make_reverb_ir(sample_rate, time_s, seed)
    ir_r = make_reverb_ir(sample_rate, time_s, seed + 1)

    wet_l = fftconvolve(signal, ir_l)[: len(signal)]
    wet_r = fftconvolve(signal, ir_r)[: len(signal)]

    return np.column_stack([wet_l, wet_r])


# --------------------------------------------------------------------------
# Track rendering
# --------------------------------------------------------------------------

@dataclass
class RenderResult:
    audio: np.ndarray          # (n, 2) float64
    sample_rate: int
    notes_rendered: int
    peak: float


def _note_samples(note: MidiNote, bpm: float, sample_rate: int, beats_per_bar: int) -> tuple[int, int]:
    spb = 60.0 / bpm
    start = int(round(note.start * spb * sample_rate))
    dur = int(round(note.duration * spb * sample_rate))
    return start, max(dur, 1)


def render_track(
    patch: PatchSpec,
    notes: list[MidiNote],
    sample_rate: int = 44100,
    bpm: float = 120.0,
    total_samples: int | None = None,
    beats_per_bar: int = 4,
    seed: int = 0,
) -> RenderResult:
    """Render a patch playing `notes` into a stereo buffer."""
    params = resolve_patch(patch)
    if not notes:
        n = total_samples or sample_rate
        return RenderResult(np.zeros((n, 2)), sample_rate, 0, 0.0)

    spb_samples = int(round((60.0 / bpm) * sample_rate))
    release_samples = int(round(params.release_s * sample_rate))

    # Determine the buffer length up front.
    last_end = 0
    for note in notes:
        start, dur = _note_samples(note, bpm, sample_rate, beats_per_bar)
        last_end = max(last_end, start + dur)
    n_total = max(total_samples or 0, last_end + release_samples)
    n_total += spb_samples  # room for the reverb/delay tail

    out = np.zeros((n_total, 2), dtype=np.float64)

    # Group notes by onset so a chord shares one filter envelope. This is
    # both musically right and several times cheaper than per-note filtering.
    groups: dict[int, list[MidiNote]] = {}
    for note in notes:
        start, _ = _note_samples(note, bpm, sample_rate, beats_per_bar)
        groups.setdefault(start, []).append(note)

    for start, group in sorted(groups.items()):
        group_len = 0
        for note in group:
            _, dur = _note_samples(note, bpm, sample_rate, beats_per_bar)
            group_len = max(group_len, dur)
        group_len += release_samples
        if group_len <= 0:
            continue

        # --- modulation --------------------------------------------------
        vib = None
        if params.lfo_target == "pitch":
            vib = lfo_signal(group_len, params.lfo_rate_hz, params.lfo_shape, sample_rate)
        mod = lfo_signal(group_len, params.lfo_rate_hz, params.lfo_shape, sample_rate)

        # --- amplitude envelope (per note) --------------------------------
        mixed = np.zeros((group_len, 2), dtype=np.float64)
        for note in group:
            n_start, dur = _note_samples(note, bpm, sample_rate, beats_per_bar)
            voice_len = dur + release_samples
            table = wavetable_for_midi(patch.spectrum, note.pitch, sample_rate, TABLE_SIZE)

            attack = int(min(params.attack_s * sample_rate, voice_len * 0.9))
            decay = int(min(params.decay_s * sample_rate, max(voice_len - attack, 1)))
            env = adsr(voice_len, attack, decay, params.sustain, release_samples)

            if params.lfo_target == "amp":
                env = env * np.clip(1.0 + mod * params.lfo_depth * 0.6, 0.0, 1.0)

            voice = unison_voices(
                table, midi_to_hz(note.pitch), voice_len, sample_rate, params,
                seed=seed + note.pitch, vibrato=vib,
            )
            voice = voice * (env * note.velocity)[:, None]

            offset = n_start - start
            if offset < 0:
                voice = voice[-offset:]
                offset = 0
            end = min(offset + len(voice), group_len)
            mixed[offset:end] += voice[: end - offset]

        # --- filter ------------------------------------------------------
        f_attack = int(min(params.filter_env_attack * sample_rate, group_len * 0.9))
        f_decay = int(min(params.filter_env_decay * sample_rate, max(group_len - f_attack, 1)))
        f_env = adsr(group_len, f_attack, f_decay, params.filter_env_sustain, release_samples)

        cutoff_curve = params.cutoff_hz * (2.0 ** (f_env * params.filter_env_octaves))
        if params.lfo_target == "filter":
            cutoff_curve = cutoff_curve * (2.0 ** (mod * params.lfo_depth * 1.5))

        # Downsample the cutoff curve to one value per filter block.
        n_blocks = (group_len + 255) // 256
        block_cuts = np.zeros(n_blocks, dtype=np.float64)
        for i in range(n_blocks):
            lo = i * 256
            hi = min(lo + 256, group_len)
            block_cuts[i] = float(np.mean(cutoff_curve[lo:hi]))

        from .biquad import sweep_filter
        filtered = sweep_filter(mixed, block_cuts, params.resonance, sample_rate, block=256)

        end = min(start + group_len, n_total)
        if end > start:
            out[start:end] += filtered[: end - start]

    # --- bus --------------------------------------------------------------
    # Everything downstream preserves the stereo image. Collapsing to mono
    # here would undo the `size` macro entirely.
    if params.drive_pre != 1.0:
        out = np.tanh(out * params.drive_pre) * params.drive_post

    if params.chorus_mix > 0.0:
        wet = chorus(
            out.mean(axis=1), params.chorus_rate_hz, params.chorus_depth_ms,
            params.chorus_mix, sample_rate,
        )
        out = out * (1.0 - 0.5 * params.chorus_mix) + wet * params.chorus_mix

    if params.delay_mix > 0.0:
        d = feedback_delay(out, int(params.delay_time_s * sample_rate), params.delay_feedback, sample_rate)
        out = out * (1.0 - params.delay_mix) + d[: len(out)] * params.delay_mix

    if params.reverb_mix > 0.0:
        time_s = 0.4 + params.reverb_mix * 4.0
        wet = apply_reverb(out.mean(axis=1), params.reverb_mix, sample_rate, time_s, seed=seed + 99)
        out = out * (1.0 - params.reverb_mix) + wet * params.reverb_mix

    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 0.98:
        out *= 0.98 / peak

    return RenderResult(out, sample_rate, len(notes), peak)
