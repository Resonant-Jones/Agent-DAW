"""Tests for the slice-1 pipeline.

These are written to *discriminate*. A test that would still pass against a
broken implementation is worse than no test at all, because it buys false
confidence. So the filter is checked against its measured frequency
response, the vectorized delay line is checked against a naive per-sample
reference, and the loudness meter is checked against a known signal.

If you change the DSP, break something on purpose and confirm this file
notices.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import freqz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentdaw import dsp  # noqa: E402
from agentdaw.analyze import analyze, integrated_lufs  # noqa: E402
from agentdaw.biquad import lowpass_coeffs, sweep_filter  # noqa: E402
from agentdaw.engine import render_preview, render_session  # noqa: E402
from agentdaw.macros import apply_deltas, resolve  # noqa: E402
from agentdaw.midi import read_midi, write_midi  # noqa: E402
from agentdaw.producer import StubProducer  # noqa: E402
from agentdaw.spec import Macros, MidiNote, PatchSpec, SessionSpec, SpectrumSpec  # noqa: E402
from agentdaw.wav import read_wav, write_wav  # noqa: E402
from agentdaw.wavetable import band_limit, build_wavetable, harmonic_amplitudes  # noqa: E402

SR = 44100


# --------------------------------------------------------------------------
# Macros
# --------------------------------------------------------------------------

def test_macro_map_is_pure():
    """Same inputs, same outputs, every time, with no carried state."""
    m = Macros(body=0.3, brightness=0.7, motion=0.2, grit=0.4, space=0.6, size=0.5)
    a = resolve(m)
    b = resolve(m)
    assert a == b


def test_macro_map_has_no_residual_state():
    """Resolving B after A must equal resolving B alone.

    This is the bug class where one half of a paired value is retained
    while the other is re-derived from ambient state.
    """
    a = resolve(Macros(body=0.9, brightness=0.9, motion=0.9, grit=0.9, space=0.9, size=0.9))
    b = resolve(Macros(body=0.1, brightness=0.1, motion=0.1, grit=0.1, space=0.1, size=0.1))
    b_again = resolve(Macros(body=0.1, brightness=0.1, motion=0.1, grit=0.1, space=0.1, size=0.1))
    assert b == b_again
    assert a.cutoff_hz != b.cutoff_hz


def test_brightness_moves_cutoff_monotonically():
    cuts = [resolve(Macros(brightness=v)).cutoff_hz for v in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert cuts == sorted(cuts)
    assert cuts[0] == pytest.approx(80.0, rel=0.01)
    assert cuts[-1] == pytest.approx(8000.0, rel=0.01)


def test_body_and_brightness_are_paired_not_independent():
    """A hollow patch is a resonant patch; a full patch is a clean one."""
    hollow = resolve(Macros(body=0.0, brightness=0.5))
    full = resolve(Macros(body=1.0, brightness=0.5))
    assert hollow.resonance > full.resonance
    assert hollow.cutoff_hz == pytest.approx(full.cutoff_hz)


def test_apply_deltas_clamps_and_produces_complete_vector():
    base = Macros(body=0.5, brightness=0.5, motion=0.5, grit=0.5, space=0.5, size=0.5)
    result = apply_deltas(base, {"brightness": -0.3, "space": 5.0})
    assert result.brightness == pytest.approx(0.2)
    assert result.space == pytest.approx(1.0)
    assert result.body == pytest.approx(0.5)
    with pytest.raises(KeyError):
        apply_deltas(base, {"nonexistent": 0.1})


# --------------------------------------------------------------------------
# Wavetable
# --------------------------------------------------------------------------

def test_wavetable_contains_exactly_the_requested_harmonics():
    """The table must be a single cycle carrying only the requested partials.

    This is the check that actually discriminates. An off-by-one in the
    table period would smear energy across non-harmonic bins; a wrong
    amplitude would show up as a wrong bin magnitude. Both are invisible to
    a "does it sound like something" test.
    """
    amps = np.array([0.0, 1.0, 0.5, 0.25])
    table = build_wavetable(amps, size=1024)
    assert len(table) == 1024

    bins = np.abs(np.fft.rfft(table)) * 2.0 / len(table)

    # The table is peak-normalized, so check the *ratios* between partials,
    # not their absolute level.
    assert bins[2] / bins[1] == pytest.approx(0.5, rel=1e-6)
    assert bins[3] / bins[1] == pytest.approx(0.25, rel=1e-6)

    # Nothing anywhere else.
    assert bins[0] == pytest.approx(0.0, abs=1e-9)
    others = np.delete(bins[:8], [0, 1, 2, 3])
    assert float(others.max()) < 1e-9


def test_wavetable_peak_is_normalized():
    amps = np.array([0.0, 1.0, 0.9, 0.8])
    table = build_wavetable(amps)
    assert np.max(np.abs(table)) == pytest.approx(1.0, rel=1e-9)


def test_band_limit_zeroes_high_harmonics():
    amps = np.array([0.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    limited = band_limit(amps, 2)
    assert limited[1] == 1.0
    assert limited[2] == 1.0
    assert limited[3] == 0.0
    assert limited[4] == 0.0


def test_odd_emphasis_shifts_harmonic_balance():
    flat = SpectrumSpec(brightness=0.5, odd_emphasis=0.0, even_emphasis=0.0)
    odd = SpectrumSpec(brightness=0.5, odd_emphasis=0.8, even_emphasis=0.0)
    a = harmonic_amplitudes(flat)
    b = harmonic_amplitudes(odd)
    assert b[1] > a[1] * 0.9
    assert b[2] < a[2]


def test_explicit_partials_override_derived_spectrum():
    spec = SpectrumSpec(brightness=0.9, partials=[{"n": 1, "amp": 1.0}, {"n": 5, "amp": 0.3}])
    amps = harmonic_amplitudes(spec)
    assert amps[1] == pytest.approx(1.0)
    assert amps[5] == pytest.approx(0.3)
    assert amps[3] == 0.0


# --------------------------------------------------------------------------
# Filter -- checked against its own measured frequency response
# --------------------------------------------------------------------------

def test_lowpass_is_minus_3db_at_its_cutoff():
    """A broken biquad coefficient shows up here immediately."""
    b, a = lowpass_coeffs(1000.0, 0.70710678, SR)
    w, h = freqz(b, a, worN=[2.0 * np.pi * 1000.0 / SR])
    magnitude_db = 20.0 * np.log10(abs(h[0]))
    assert magnitude_db == pytest.approx(-3.0, abs=0.35)


def test_lowpass_passes_low_and_blocks_high():
    b, a = lowpass_coeffs(1000.0, 0.70710678, SR)
    for freq, expect_low in ((100.0, True), (10000.0, False)):
        w, h = freqz(b, a, worN=[2.0 * np.pi * freq / SR])
        magnitude_db = 20.0 * np.log10(abs(h[0]))
        if expect_low:
            assert magnitude_db > -1.0
        else:
            assert magnitude_db < -25.0


def test_sweep_filter_preserves_dc_gain():
    """A sweep that changes cutoff must not change the DC gain."""
    signal = np.ones(SR)
    out = sweep_filter(signal, np.full(100, 1000.0), 0.70710678, SR)
    assert np.mean(out[-1000:]) == pytest.approx(1.0, abs=0.02)


def test_sweep_filter_is_linear():
    """Filtering a+b must equal filtering a plus filtering b."""
    rng = np.random.default_rng(0)
    a = rng.normal(0, 0.2, 4000)
    b = rng.normal(0, 0.2, 4000)
    cuts = np.full(32, 2000.0)
    out_ab = sweep_filter(a + b, cuts, 1.0, SR)
    out_a = sweep_filter(a, cuts, 1.0, SR)
    out_b = sweep_filter(b, cuts, 1.0, SR)
    assert np.allclose(out_ab, out_a + out_b, atol=1e-9)


# --------------------------------------------------------------------------
# Delay line -- compared against a naive per-sample reference
# --------------------------------------------------------------------------

def _naive_feedback_delay(signal: np.ndarray, delay: int, feedback: float) -> np.ndarray:
    """Textbook implementation. Slow, obviously correct."""
    out = np.zeros(len(signal))
    history = [0.0] * delay
    for i, x in enumerate(signal):
        delayed = history.pop(0)
        history.append(x + feedback * delayed)
        out[i] = x + feedback * delayed
    return out


def test_feedback_delay_matches_naive_reference():
    rng = np.random.default_rng(7)
    signal = rng.normal(0, 0.3, 3000)
    delay = 256
    feedback = 0.4

    fast = dsp.feedback_delay(signal, delay, feedback, SR)
    slow = _naive_feedback_delay(signal, delay, feedback)

    # Compare only where the tail has not run past the end.
    n = len(signal) - delay
    assert np.allclose(fast[:n], slow[:n], atol=1e-9)


def test_feedback_delay_actually_delays():
    """The first repeat is the input attenuated by the feedback amount.

    A delay that returns the input at full level is not a feedback delay.
    """
    signal = np.zeros(2000)
    signal[0] = 1.0
    out = dsp.feedback_delay(signal, 100, 0.5, SR)
    assert out[99] == pytest.approx(0.0, abs=1e-9)
    assert out[100] == pytest.approx(0.5, abs=1e-6)
    assert out[200] == pytest.approx(0.25, abs=1e-6)


def test_feedback_delay_rings_out_past_the_end():
    """A delay line shifts, it does not hold.

    An impulse in therefore produces an impulse train at multiples of the
    delay time, with silence between. The echoes that land *after* the
    input ends are the tail, and they must not be trimmed away.
    """
    signal = np.zeros(1000)
    signal[0] = 1.0
    out = dsp.feedback_delay(signal, 100, 0.8, SR)

    assert len(out) > 1000
    assert out[100] == pytest.approx(0.8, abs=1e-6)
    assert out[200] == pytest.approx(0.64, abs=1e-6)
    # Silence between the echoes.
    assert out[150] == pytest.approx(0.0, abs=1e-9)
    # Echoes land past the end of the input -- that is the tail.
    assert out[1000] == pytest.approx(0.8 ** 10, abs=1e-6)
    assert out[1100] == pytest.approx(0.8 ** 11, abs=1e-6)


# --------------------------------------------------------------------------
# Oscillator
# --------------------------------------------------------------------------

def test_oscillator_produces_the_requested_pitch():
    """A 220 Hz oscillator must have its energy at 220 Hz."""
    from agentdaw.wavetable import build_wavetable
    table = build_wavetable(np.array([0.0, 1.0]), size=2048)
    n = SR
    out = dsp.oscillator(table, 220.0, n, SR)
    spectrum = np.abs(np.fft.rfft(out))
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    peak_hz = freqs[int(np.argmax(spectrum))]
    assert peak_hz == pytest.approx(220.0, abs=5.0)


def test_adsr_envelope_shape():
    env = dsp.adsr(1000, attack=100, decay=200, sustain=0.5, release=300)
    assert env[0] == pytest.approx(0.0, abs=1e-9)
    assert env[99] == pytest.approx(1.0, abs=0.02)      # top of the attack
    assert env[299] == pytest.approx(0.5, abs=0.02)    # bottom of the decay
    assert env[650] == pytest.approx(0.5, abs=0.02)    # sustain hold
    assert env[-1] == pytest.approx(0.0, abs=1e-9)     # end of the release


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def test_spectral_centroid_tracks_the_signal():
    for freq in (220.0, 1000.0, 4000.0):
        t = np.arange(SR) / SR
        sine = np.sin(2.0 * np.pi * freq * t)
        m = analyze(sine, SR)
        assert m.spectral_centroid_hz == pytest.approx(freq, rel=0.05)


def test_loudness_meter_responds_to_level_correctly():
    """Halving the amplitude must drop the reading by exactly 6.02 LU.

    This is implementation-independent: it holds for any correct
    implementation of the standard, whatever its filter design.
    """
    t = np.arange(SR * 3) / SR
    sine = np.sin(2.0 * np.pi * 1000.0 * t)
    stereo = np.column_stack([sine, sine])

    loud = integrated_lufs(stereo, SR)
    quiet = integrated_lufs(stereo * 0.5, SR)
    assert loud - quiet == pytest.approx(6.02, abs=0.05)


def test_integrated_lufs_sums_channels_not_averages():
    """One channel at the same level must read 3.01 LU lower than two.

    BS.1770 sums channel energies with G=1.0 each. Averaging instead of
    summing biases every stereo reading by exactly 3.01 LU low, which is
    small enough to look plausible and constant enough to go unnoticed.
    """
    t = np.arange(SR * 3) / SR
    sine = np.sin(2.0 * np.pi * 1000.0 * t)
    stereo = integrated_lufs(np.column_stack([sine, sine]), SR)
    mono = integrated_lufs(sine[:, None], SR)
    assert stereo - mono == pytest.approx(3.01, abs=0.05)


def test_loudness_meter_agrees_with_ffmpeg(tmp_path):
    """Cross-check against an independent implementation.

    This is the test that catches a systematic bias in the meter. If the
    two disagree by more than a fraction of a LU, something is wrong in
    the K-weighting or the gating.
    """
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")

    rng = np.random.default_rng(11)
    t = np.arange(SR * 6) / SR
    signal = 0.3 * np.sin(2.0 * np.pi * 220.0 * t) + 0.15 * rng.normal(0, 1, len(t))
    stereo = np.column_stack([signal, signal * 0.8])

    path = tmp_path / "loudness_probe.wav"
    write_wav(path, stereo, SR, bit_depth=24)

    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    reference = None
    for line in result.stderr.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "I:":
            try:
                reference = float(parts[1])
            except ValueError:
                pass

    if reference is None:
        pytest.skip("ffmpeg did not report an integrated loudness")

    mine = integrated_lufs(stereo, SR)
    assert mine == pytest.approx(reference, abs=0.5), (
        f"meter disagrees with ffmpeg: mine {mine:.2f} vs ffmpeg {reference:.2f}"
    )


def test_measurements_flag_a_low_end_buildup():
    """A 60 Hz-heavy signal must be reported, not silently accepted."""
    sr = SR
    t = np.arange(sr * 2) / sr
    low = np.sin(2.0 * np.pi * 60.0 * t)
    high = np.sin(2.0 * np.pi * 4000.0 * t) * 0.1
    signal = np.column_stack([low + high, low + high])
    m = analyze(signal, sr)
    assert m.low_energy_db > m.high_energy_db + 10


# --------------------------------------------------------------------------
# File formats
# --------------------------------------------------------------------------

def test_wav_round_trip_preserves_samples():
    rng = np.random.default_rng(3)
    # Keep the data inside full scale. The writer clips anything above
    # 1.0, which is correct behaviour but would mask a real bug here.
    data = rng.uniform(-0.9, 0.9, (5000, 2))
    path = write_wav(Path("/tmp/agentdaw_test.wav"), data, SR, bit_depth=24)
    read, sr = read_wav(path)
    assert sr == SR
    assert read.shape == data.shape
    assert np.max(np.abs(read - data)) < 1e-4


def test_wav_clips_out_of_range_input():
    """A writer that silently wraps around instead of clipping is a bug."""
    data = np.array([[2.0, -2.0]])
    path = write_wav(Path("/tmp/agentdaw_clip.wav"), data, SR, bit_depth=24)
    read, _ = read_wav(path)
    assert read[0, 0] == pytest.approx(1.0, abs=1e-4)
    assert read[0, 1] == pytest.approx(-1.0, abs=1e-4)


def test_wav_float_round_trip():
    rng = np.random.default_rng(4)
    data = rng.uniform(-0.9, 0.9, (2000, 2))
    path = write_wav(Path("/tmp/agentdaw_test_float.wav"), data, SR, float_format=True)
    read, sr = read_wav(path)
    assert np.max(np.abs(read - data)) < 1e-6


def test_midi_round_trip_preserves_notes():
    notes = [
        MidiNote(pitch=60, start=0.0, duration=1.5, velocity=0.8),
        MidiNote(pitch=64, start=2.0, duration=0.5, velocity=0.55),
        MidiNote(pitch=67, start=4.25, duration=2.0, velocity=0.9),
    ]
    path = write_midi(Path("/tmp/agentdaw_test.mid"), notes, bpm=120.0, track_name="Test")
    back = read_midi(path)
    assert len(back) == len(notes)
    for original, recovered in zip(notes, back):
        assert recovered.pitch == original.pitch
        assert recovered.start == pytest.approx(original.start, abs=0.01)
        assert recovered.duration == pytest.approx(original.duration, abs=0.01)
        assert recovered.velocity == pytest.approx(original.velocity, abs=0.02)


def test_midi_file_has_valid_header():
    path = write_midi(Path("/tmp/agentdaw_test.mid"), [MidiNote(pitch=60, start=0, duration=1, velocity=0.8)])
    raw = path.read_bytes()
    assert raw[:4] == b"MThd"
    assert int.from_bytes(raw[8:10], "big") == 1  # format 1
    assert raw.count(b"MTrk") >= 2


# --------------------------------------------------------------------------
# Session validation
# --------------------------------------------------------------------------

def test_validation_catches_dangling_patch_reference():
    spec = StubProducer().compose("test")
    spec.tracks[0].patch_id = "does_not_exist"
    errors = spec.validate_references()
    assert any("does_not_exist" in e for e in errors)


def test_validation_catches_bar_count_mismatch():
    spec = StubProducer().compose("test")
    spec.bars = 99
    errors = spec.validate_references()
    assert any("bars" in e for e in errors)


def test_validation_catches_notes_past_the_end():
    spec = StubProducer().compose("test")
    spec.tracks[0].parts[0].notes.append(MidiNote(pitch=60, start=200.0, duration=4.0, velocity=0.5))
    errors = spec.validate_references()
    assert any("past the end" in e for e in errors)


def test_stub_producer_produces_a_valid_spec():
    spec = StubProducer().compose("a warm pad")
    assert spec.validate_references() == []
    assert spec.patches
    assert spec.tracks


class _FakeClient:
    """Stands in for the LLM client so the repair loop can be tested."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[int] = []

    def complete(self, system, prompt, max_tokens=8000, temperature=0.5, thinking_budget=None):
        self.calls.append(max_tokens)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_producer_repairs_an_invalid_spec():
    """A malformed answer is fed back with the error, not silently accepted."""
    from agentdaw.producer import LLMProducer

    valid = StubProducer().compose("a warm pad").model_dump_json()
    client = _FakeClient([
        "not json at all",
        '{"title": "x", "bpm": 120}',   # valid JSON, invalid spec
        valid,
    ])
    producer = LLMProducer(client)
    spec = producer.compose("a warm pad")
    assert spec.validate_references() == []
    assert len(client.calls) == 3


def test_producer_grows_the_token_budget_when_truncated():
    """A reasoning model that thinks itself out of room gets more room."""
    from agentdaw.llm import LLMTruncated
    from agentdaw.producer import LLMProducer

    valid = StubProducer().compose("a warm pad").model_dump_json()
    client = _FakeClient([
        LLMTruncated("no text, thinking_tokens=16000"),
        valid,
    ])
    producer = LLMProducer(client, max_tokens=16000, max_tokens_cap=64000)
    spec = producer.compose("a warm pad")

    assert spec.validate_references() == []
    assert len(client.calls) == 2
    assert client.calls[1] > client.calls[0], "budget did not grow after truncation"


def test_producer_gives_up_rather_than_looping_forever():
    from agentdaw.llm import LLMTruncated
    from agentdaw.producer import LLMProducer

    client = _FakeClient([LLMTruncated("nope")] * 6)
    producer = LLMProducer(client, max_tokens=1000, max_tokens_cap=4000)
    with pytest.raises(Exception):
        producer.compose("a warm pad")


# --------------------------------------------------------------------------
# Constraint enforcement
# --------------------------------------------------------------------------

def test_extract_constraints_picks_up_explicit_values():
    from agentdaw.producer import extract_constraints

    found = extract_constraints(
        "A dark 8-bar intro at 120 BPM in C minor, holding its breath."
    )
    assert found["bpm"] == 120.0
    assert found["bars"] == 8
    assert found["key"] == "C minor"


def test_extract_constraints_ignores_vague_wording():
    from agentdaw.producer import extract_constraints

    assert extract_constraints("something that feels slow and heavy") == {}
    assert extract_constraints("a moody intro") == {}


def test_check_constraints_catches_a_changed_tempo():
    from agentdaw.producer import check_constraints, extract_constraints

    spec = StubProducer().compose("test")
    spec.bpm = 88.0
    violations = check_constraints(spec, extract_constraints("at 120 BPM"))
    assert violations
    assert "120" in violations[0]


def test_check_constraints_catches_a_changed_key():
    from agentdaw.producer import check_constraints, extract_constraints

    spec = StubProducer().compose("test")
    spec.key = "F major"
    spec.mode = "major"
    violations = check_constraints(spec, extract_constraints("in C minor"))
    assert violations


def test_producer_rejects_a_spec_that_contradicts_the_artist():
    """A model that changes an explicit tempo must be sent back to fix it."""
    from agentdaw.producer import LLMProducer

    wrong = StubProducer().compose("test").model_dump()
    wrong["bpm"] = 88.0

    right = StubProducer().compose("test").model_dump()
    right["bpm"] = 120.0

    client = _FakeClient([json.dumps(wrong), json.dumps(right)])
    producer = LLMProducer(client)
    spec = producer.compose("a tense intro at 120 BPM")

    assert spec.bpm == 120.0
    assert len(client.calls) == 2, "the wrong tempo should have been sent back"


# --------------------------------------------------------------------------
# The render pipeline
# --------------------------------------------------------------------------

def test_preview_renders_audible_audio():
    spec = StubProducer().compose("test")
    patch = spec.patches[0]
    track = spec.tracks[0]
    notes = track.parts[0].notes

    audio = render_preview(patch, notes, bpm=spec.bpm, bars=spec.bars, sample_rate=SR)

    assert audio.ndim == 2 and audio.shape[1] == 2
    assert len(audio) > SR  # at least a second of audio
    assert float(np.max(np.abs(audio))) > 0.01, "preview is effectively silent"
    assert not np.any(np.isnan(audio))


def test_preview_render_is_fast_enough_to_be_a_loop():
    """The audition loop is the product. If this regresses, the product dies.

    Budget: an 8-bar preview with drums must land well under the point
    where the artist loses their train of thought.
    """
    import time

    spec = StubProducer().compose("test")
    patch = spec.patches[0]
    notes = spec.tracks[0].parts[0].notes

    started = time.perf_counter()
    render_preview(patch, notes, bpm=spec.bpm, bars=spec.bars, sample_rate=SR)
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"preview render took {elapsed:.2f}s, budget is 5s"


def test_session_render_writes_stems_midi_and_project(tmp_path):
    spec = StubProducer().compose("test")
    report = render_session(spec, tmp_path, sample_rate=SR)

    assert report.mix_path and report.mix_path.exists()
    assert report.project_path and report.project_path.exists()

    for track in report.tracks:
        assert track.stem_path and track.stem_path.exists()
        if track.kind == "midi":
            assert track.midi_path and track.midi_path.exists()

    project_text = report.project_path.read_text()
    assert project_text.startswith("<REAPER_PROJECT")
    assert project_text.rstrip().endswith(">")
    assert "TEMPO" in project_text
    assert "<TRACK" in project_text


def test_session_render_produces_measurements(tmp_path):
    spec = StubProducer().compose("test")
    report = render_session(spec, tmp_path, sample_rate=SR)
    for track in report.tracks:
        assert track.measurements.peak_dbfs > -60.0
        assert track.measurements.integrated_lufs > -70.0


def test_rpp_structure_is_balanced(tmp_path):
    """A malformed .rpp will not open. Check the nesting and the file refs."""
    spec = StubProducer().compose("test")
    report = render_session(spec, tmp_path, sample_rate=SR)
    text = report.project_path.read_text()

    assert text.count("<REAPER_PROJECT") == 1
    assert text.count("<TRACK") == len(spec.tracks)
    assert text.count("<ITEM") == len(spec.tracks)
    assert text.count("<SOURCE") == len(spec.tracks)

    # Every SOURCE must point at a file that exists.
    for line in text.splitlines():
        if "FILE " in line:
            path = line.split('"')[1]
            assert Path(path).exists(), f"dangling file reference: {path}"

    # Angle brackets must balance.
    assert text.count("<") == text.count(">")


def test_rpp_carries_render_settings_when_asked(tmp_path):
    """Render settings are what make a project headlessly renderable."""
    from agentdaw.reaper import write_rpp

    spec = StubProducer().compose("test")
    render_dir = tmp_path / "session"
    render_dir.mkdir(parents=True)
    project = write_rpp(
        tmp_path / "renderable.rpp",
        spec,
        render_dir,
        render_file=str(tmp_path) + "/out/",
        render_pattern="test_render",
        render_channels=2,
    )
    text = project.read_text()
    assert 'RENDER_FILE "' + str(tmp_path) + "/out/" in text
    assert 'RENDER_PATTERN "test_render"' in text
    assert "RENDER_FMT 0 2 0" in text


# --------------------------------------------------------------------------
# REAPER project fidelity
#
# These guard three details that are load-bearing and were all wrong in the
# first version of the writer. They were found by diffing against a project
# that REAPER itself saved on macOS, not by guessing.
# --------------------------------------------------------------------------

def test_rpp_render_bounds_cover_the_whole_project(tmp_path):
    """RENDER_RANGE's first field is the bounds mode.

    Mode 0 means "custom time range". With start and end both 0 that is a
    zero-length range, and REAPER renders a zero-length file while
    reporting success. Mode 1 is "entire project".
    """
    spec = StubProducer().compose("test")
    report = render_session(spec, tmp_path, sample_rate=SR)
    text = report.project_path.read_text()

    assert "RENDER_RANGE 1 0 0 0 1000" in text
    assert "RENDER_RANGE 0 " not in text


def test_rpp_declares_a_master_track(tmp_path):
    """The master is expressed as MASTER* project keys, not a <TRACK> block.

    Without them there is no master bus for the render to land on.
    """
    spec = StubProducer().compose("test")
    report = render_session(spec, tmp_path, sample_rate=SR)
    text = report.project_path.read_text()

    for key in ("MASTER_VOLUME", "MASTER_FX", "MASTER_NCH", "MASTERMUTESOLO"):
        assert f"\n  {key} " in text, f"missing master key: {key}"


def test_rpp_track_volume_is_linear_not_decibels(tmp_path):
    """REAPER stores track volume as a linear multiplier.

    A .rpp written by REAPER holds 0.501187 for -6 dB. Writing the decibel
    value instead means a linear gain of -6.0, which is -15.6 dB and, for
    any negative gain, pulls the track far below where it belongs.
    """
    spec = StubProducer().compose("test")
    report = render_session(spec, tmp_path, sample_rate=SR)
    text = report.project_path.read_text()

    volumes = [
        float(line.split()[1])
        for line in text.splitlines()
        if line.startswith("    VOLPAN ")
    ]
    assert volumes, "no track volumes found"

    for track, volume in zip(spec.tracks, volumes):
        expected = 10.0 ** (track.gain_db / 20.0)
        assert volume == pytest.approx(expected, abs=1e-6)
        assert 0.0 < volume <= 1.0, f"volume {volume} is not a linear gain"


def test_rpp_items_have_a_real_length(tmp_path):
    """A zero-length item plays nothing.

    The first writer emitted LENGTH 0 for every item, which REAPER reads
    as "this item is empty".
    """
    spec = StubProducer().compose("test")
    report = render_session(spec, tmp_path, sample_rate=SR)
    text = report.project_path.read_text()

    lengths = [
        float(line.split()[1])
        for line in text.splitlines()
        if line.strip().startswith("LENGTH ")
    ]
    assert lengths
    for length in lengths:
        assert length > 0.0, "item with zero length plays nothing"


def test_rpp_offers_both_a_stems_view_and_a_sequencing_view(tmp_path):
    """Two projects, two jobs.

    The stems view carries rendered audio so the project opens with sound.
    The sequencing view carries the .mid files so an instrument can be
    dropped on a melodic track.
    """
    spec = StubProducer().compose("test")
    report = render_session(spec, tmp_path, sample_rate=SR)

    assert report.project_path is not None
    assert report.midi_project_path is not None
    assert report.project_path != report.midi_project_path

    stems = report.project_path.read_text()
    sequencing = report.midi_project_path.read_text()

    # Stems view: audio only.
    assert "<SOURCE WAVE" in stems
    assert "<SOURCE MIDI" not in stems

    # Sequencing view: melodic tracks are MIDI, the drum track stays audio.
    assert "<SOURCE MIDI" in sequencing
    assert "<SOURCE WAVE" in sequencing
    assert sequencing.count("<SOURCE MIDI") == sum(
        1 for t in spec.tracks if t.kind == "midi"
    )


def test_rpp_guids_use_the_windows_layout(tmp_path):
    """REAPER writes GUIDs as 8-4-4-4-12 hex, the same shape as Windows."""
    import re

    spec = StubProducer().compose("test")
    report = render_session(spec, tmp_path, sample_rate=SR)
    text = report.project_path.read_text()

    guids = re.findall(r"\{([0-9A-Fa-f-]+)\}", text)
    assert guids
    for guid in guids:
        assert re.fullmatch(
            r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
            guid,
        ), f"malformed GUID: {guid}"


def test_rpp_item_length_matches_the_stem_it_references(tmp_path):
    """The item length must be the length of the audio it points at.

    REAPER uses the item length to decide how much of the source to play.
    If it is shorter than the stem, the tail is cut; if it is longer, the
    extra is silence. Either way the project is not what was rendered.
    """
    from agentdaw.wav import read_wav

    spec = StubProducer().compose("test")
    report = render_session(spec, tmp_path, sample_rate=SR)
    text = report.project_path.read_text()

    # Pull (length, file) pairs out of the item blocks.
    pairs = []
    length = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("LENGTH "):
            length = float(stripped.split()[1])
        elif stripped.startswith("FILE ") and length is not None:
            pairs.append((length, stripped.split('"')[1]))
            length = None

    assert len(pairs) == len(spec.tracks)
    for item_length, source in pairs:
        audio, sample_rate = read_wav(source)
        stem_seconds = len(audio) / sample_rate
        assert item_length == pytest.approx(stem_seconds, abs=0.001)


# --------------------------------------------------------------------------
# The patch library: retrieval anchors and delta revisions
# --------------------------------------------------------------------------

def _library_with_patch(**macro_overrides):
    from agentdaw.library import LibraryEntry, PatchLibrary, Provenance

    macros = {
        "body": 0.6, "brightness": 0.35, "motion": 0.5,
        "grit": 0.15, "space": 0.65, "size": 0.75,
    }
    macros.update(macro_overrides)
    patch = PatchSpec(
        id="warm_pad",
        name="Warm Pad",
        description="Wide warm pad that opens across the form.",
        macros=Macros(**macros),
        seed=101,
    )
    entry = LibraryEntry(
        id="warm_pad",
        name=patch.name,
        description=patch.description,
        patch=patch,
        anchors=["warm pad", "warm", "wide", "spacious"],
        measurements={"spectral_centroid_hz": 702.0, "integrated_lufs": -18.0},
        provenance=Provenance(source="session:test", brief="a warm pad"),
    )
    return PatchLibrary([entry])


def test_library_anchors_are_derived_from_the_sound_not_the_name():
    """An anchor has to describe the sound, or retrieval matches on nothing."""
    from agentdaw.library import describe_patch

    patch = PatchSpec(
        id="p",
        name="Patch",
        description="a thin bright metallic tone",
        macros=Macros(body=0.1, brightness=0.9, motion=0.5, grit=0.8, space=0.1, size=0.2),
    )
    anchors = describe_patch(patch, "Patch", patch.description)

    assert "bright" in anchors
    assert "narrow" in anchors
    assert "gritty" in anchors
    assert "dry" in anchors
    assert "hollow" in anchors


def test_library_retrieval_finds_the_anchor_and_says_which_way_to_move():
    from agentdaw.library import PatchLibrary

    library = _library_with_patch()
    hits = library.retrieve("a brighter, grittier version of that warm pad")

    assert hits, "the warm pad anchor should have matched"
    top = hits[0]
    assert top.entry.id == "warm_pad"
    assert top.score > 0.2
    assert "brightness" in top.suggested_macros
    assert "grit" in top.suggested_macros
    # Brighter means up, and it must stay inside 0..1.
    assert top.suggested_macros["brightness"] > top.entry.macros["brightness"]
    assert 0.0 <= top.suggested_macros["brightness"] <= 1.0
    # The reason must quote a measurement, not an adjective.
    assert any("centroid" in reason for reason in top.reasons)


def test_library_retrieval_reports_a_miss_as_a_miss():
    """A library with nothing related must return nothing, not a false recall."""
    from agentdaw.library import PatchLibrary

    library = _library_with_patch()
    hits = library.retrieve("a growling distorted bass with a screaming solo")
    assert not hits


def test_library_revision_is_a_complete_new_state():
    """The parent is never mutated, and the child is whole."""
    from agentdaw.library import PatchLibrary

    library = _library_with_patch()
    parent = library.get("warm_pad")
    original = parent.macros["brightness"]

    revised_patch = parent.patch.model_copy(deep=True)
    revised_patch.macros.brightness = 0.8
    revised_patch.id = "warm_pad_r1"

    child = library.revise("warm_pad", revised_patch, "make it brighter")

    # The parent still holds what it always held.
    assert library.get("warm_pad").macros["brightness"] == original
    # The child holds every axis, not just the one that moved.
    assert set(child.macros) == set(parent.macros)
    assert child.macros["brightness"] == 0.8
    assert child.provenance.origin == "revision"
    assert child.provenance.parent_id == "warm_pad"
    assert child.provenance.delta == "make it brighter"


def test_library_lineage_reports_the_revision_chain():
    from agentdaw.library import PatchLibrary

    library = _library_with_patch()
    parent = library.get("warm_pad")
    revised = parent.patch.model_copy(deep=True)
    revised.id = "warm_pad_r1"
    child = library.revise("warm_pad", revised, "brighter")

    chain = library.lineage(child.id)
    assert [e.id for e in chain] == ["warm_pad", "warm_pad_r1"]


def test_library_round_trips_through_json(tmp_path):
    from agentdaw.library import PatchLibrary

    library = _library_with_patch()
    path = tmp_path / "library.json"
    library.save(path)

    reloaded = PatchLibrary.load(path)
    assert len(reloaded.entries) == 1
    assert reloaded.get("warm_pad").macros == library.get("warm_pad").macros


def test_service_compose_returns_artifacts_and_measurements(tmp_path):
    """The contract a host application mounts against."""
    from agentdaw import service

    result = service.compose_session(
        "a warm, wide pad over a slow bass groove",
        out_dir=tmp_path,
        producer=StubProducer(),
    )

    assert "spec" in result
    assert result["spec"]["tracks"]

    roles = {a["role"] for a in result["artifacts"]}
    assert "stem" in roles
    assert "mix" in roles
    assert "reaper_project" in roles
    assert "sequence" in roles

    for artifact in result["artifacts"]:
        assert Path(artifact["path"]).exists()
        assert artifact["media_kind"] in ("audio", "other")
        assert artifact["provenance"] == "generated"

    assert result["measurements"]
    for facts in result["measurements"].values():
        assert "integrated_lufs" in facts
        assert "spectral_centroid_hz" in facts


def test_service_anchor_retrieval_reports_a_miss_without_inventing(tmp_path):
    from agentdaw import service

    result = service.retrieve_anchors(
        "a growling distorted bass", library=None
    )
    assert result["count"] == 0
    assert result["hits"] == []


def test_service_approve_promotes_patches_into_the_library(tmp_path):
    from agentdaw import service

    spec = StubProducer().compose("test")
    library_path = tmp_path / "library.json"

    result = service.approve_patches(
        spec,
        measurements={
            "pad": {"spectral_centroid_hz": 702.0, "integrated_lufs": -18.0,
                    "notes": ["not a number"]}
        },
        library_path=library_path,
    )

    assert result["library_size"] == len(spec.patches)
    assert library_path.exists()

    saved = json.loads(library_path.read_text())
    pad = next(e for e in saved["entries"] if e["id"] == "pad_warm")
    assert pad["measurements"]["spectral_centroid_hz"] == 702.0
    # Non-numeric facts are dropped rather than stored as measurements.
    assert "notes" not in pad["measurements"]


def test_producer_prompt_demands_complete_patches():
    """The completeness rule has to reach the model, not just the code."""
    from agentdaw.producer import LLMProducer

    producer = LLMProducer.__new__(LLMProducer)
    prompt = producer._initial_prompt("a warm pad", None)

    assert "COMPLETE" in prompt
    assert "six macros" in prompt


def test_producer_prompt_tells_the_model_to_revise_rather_than_invent():
    from agentdaw.producer import LLMProducer

    producer = LLMProducer.__new__(LLMProducer)
    library = _library_with_patch()
    prompt = producer._initial_prompt("a brighter version of that warm pad", library)

    assert "warm_pad" in prompt
    assert "Prefer" in prompt


def test_codexify_package_imports_without_fastapi():
    """FastAPI is optional; importing agentdaw must not require it."""
    import agentdaw.codexify as codexify

    assert callable(codexify.build_router)


def test_codexify_skill_has_the_frontmatter_codexify_expects():
    """Codexify parses `name` and `description` out of YAML frontmatter."""
    skill = (
        Path(__file__).resolve().parents[1]
        / "agentdaw"
        / "codexify"
        / "skills"
        / "music-producer"
        / "SKILL.md"
    )
    assert skill.exists(), "the music-producer skill should ship with the package"

    raw = skill.read_text(encoding="utf-8")
    assert raw.startswith("---")

    front, _, body = raw.partition("---")[2].partition("---")
    assert "name: music-producer" in front
    assert "description:" in front
    # The body is what the model reads; it has to carry the rules.
    assert "DSP" in body
    assert "measure" in body.lower()


def test_schema_export_is_valid_json(tmp_path):
    from agentdaw.schema import json_schema_bundle

    bundle = json_schema_bundle()
    assert "SessionSpec" in bundle
    assert "PatchSpec" in bundle
    json.dumps(bundle)


# --------------------------------------------------------------------------
# LLM configuration
# --------------------------------------------------------------------------

def test_extract_json_handles_fences_and_prose():
    from agentdaw.llm import extract_json

    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go:\n{"a": 1}\nHope that helps!') == {"a": 1}
    assert extract_json('{"a": {"b": [1, 2]}}') == {"a": {"b": [1, 2]}}


def test_extract_json_rejects_garbage():
    from agentdaw.llm import extract_json

    with pytest.raises(ValueError):
        extract_json("no json here at all")


def test_placeholder_api_key_falls_back_to_oauth(tmp_path, monkeypatch):
    """A config with a placeholder key must not be used as a credential."""
    from agentdaw import llm as llm_module

    auth_dir = tmp_path / "auth" / "prod" / "en" / "mcode-public"
    auth_dir.mkdir(parents=True)
    (auth_dir / "auth.json").write_text(json.dumps({
        "records": {"r1": {"accessToken": "mmoat_real_token_value", "tokenType": "Bearer"}}
    }))

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "provider:\n"
        "  minimax:\n"
        "    npm: '@ai-sdk/anthropic'\n"
        "    models:\n"
        "      Test-Model:\n"
        "        name: Test\n"
        "    options:\n"
        "      apiKey: sk-xxx\n"
        "      baseURL: https://example.invalid/v1\n"
    )

    monkeypatch.setattr(llm_module, "_default_data_dir", lambda: tmp_path)
    config = llm_module.load_llm_config(config_path=config_path)

    assert config.api_key == "mmoat_real_token_value"
    assert config.model == "Test-Model"
    assert config.protocol == "anthropic"
    assert config.credential_loader is not None


def test_auth_style_resolves_by_endpoint():
    """MiniMax wants a bearer token; a first-party Anthropic endpoint wants x-api-key."""
    from agentdaw.llm import LLMClient, LLMConfig

    minimax = LLMClient(LLMConfig(
        base_url="https://agent.minimax.io/mavis/api/v1/llm/v1",
        api_key="tok", model="m", auth_style="auto",
    ))
    assert "Authorization" in minimax._auth_headers()
    assert "x-api-key" not in minimax._auth_headers()

    anthropic = LLMClient(LLMConfig(
        base_url="https://api.anthropic.com", api_key="tok", model="m", auth_style="auto",
    ))
    assert "x-api-key" in anthropic._auth_headers()
    assert "Authorization" not in anthropic._auth_headers()

    explicit = LLMClient(LLMConfig(
        base_url="https://example.invalid", api_key="tok", model="m", auth_style="both",
    ))
    headers = explicit._auth_headers()
    assert "Authorization" in headers and "x-api-key" in headers


def test_environment_overrides_config():
    from agentdaw import llm as llm_module

    monkey = os.environ
    old = {k: monkey.get(k) for k in (
        llm_module.ENV_BASE_URL, llm_module.ENV_API_KEY, llm_module.ENV_MODEL,
    )}
    try:
        monkey[llm_module.ENV_BASE_URL] = "https://custom.invalid/v1"
        monkey[llm_module.ENV_API_KEY] = "custom-key"
        monkey[llm_module.ENV_MODEL] = "custom-model"
        config = llm_module.load_llm_config()
        assert config.base_url == "https://custom.invalid/v1"
        assert config.api_key == "custom-key"
        assert config.model == "custom-model"
        assert config.provider == "env"
    finally:
        for k, v in old.items():
            if v is None:
                monkey.pop(k, None)
            else:
                monkey[k] = v


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_demo_renders_end_to_end(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "agentdaw.cli", "--out", str(tmp_path), "demo"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "mix.wav").exists()
    assert "rendered in" in result.stdout


# --------------------------------------------------------------------------
# Codexify service plugin -- the canonical protocol v1.0
# --------------------------------------------------------------------------

def _plugin_client(tmp_path, monkeypatch):
    """A TestClient over the service plugin, with the LLM stubbed out.

    The stub matters: without it these tests would call the real provider
    and take a minute each, and a test suite that needs the network is a
    test suite that lies about being a test suite.
    """
    from fastapi.testclient import TestClient

    from agentdaw import service as service_module
    from agentdaw.serve import build_app

    monkeypatch.setattr(
        service_module,
        "_producer",
        lambda model=None: (StubProducer(), False),
    )
    return TestClient(build_app(out_root=tmp_path / "out"))


def _envelope(action, payload=None, protocol_version="1.0", capability="music"):
    return {
        "protocol_version": protocol_version,
        "plugin_id": "agentdaw",
        "capability": capability,
        "action": action,
        "input": payload or {},
        "context": {"request_id": "req-1", "thread_id": None, "user_id": "u-1"},
    }


def test_service_plugin_health_advertises_its_capability(tmp_path, monkeypatch):
    client = _plugin_client(tmp_path, monkeypatch)

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["ready"] is True
    assert body["capability"] == "music"
    assert "compose" in body["actions"]
    assert body["protocol_version"] == "1.0"


def test_service_plugin_invokes_compose_through_the_canonical_envelope(
    tmp_path, monkeypatch
):
    """Codexify's facade raises unless `ok` is a boolean and `error` is present.

    See `guardian/core/plugins.py::_normalize_response`. A response that
    omits `error` is not a nicer response; it is a broken plugin.
    """
    client = _plugin_client(tmp_path, monkeypatch)

    response = client.post("/invoke", json=_envelope("compose", {"brief": "a warm pad"}))
    body = response.json()

    assert response.status_code == 200
    assert isinstance(body["ok"], bool)
    assert body["ok"] is True
    assert body["error"] is None
    assert body["output"]["spec"]["title"]
    assert body["output"]["artifacts"]


def test_service_plugin_rejects_an_unsupported_capability(tmp_path, monkeypatch):
    client = _plugin_client(tmp_path, monkeypatch)

    body = client.post(
        "/invoke", json=_envelope("compose", {"brief": "x"}, capability="tts")
    ).json()

    assert body["ok"] is False
    assert body["error"]["code"] == "unsupported_operation"
    assert body["error"]["retryable"] is False


def test_service_plugin_rejects_an_unsupported_action(tmp_path, monkeypatch):
    client = _plugin_client(tmp_path, monkeypatch)

    body = client.post("/invoke", json=_envelope("teleport")).json()

    assert body["ok"] is False
    assert body["error"]["code"] == "unsupported_operation"


def test_service_plugin_rejects_a_wrong_protocol_version(tmp_path, monkeypatch):
    """The envelope's protocol_version is checked, not assumed."""
    client = _plugin_client(tmp_path, monkeypatch)

    body = client.post(
        "/invoke", json=_envelope("compose", {"brief": "x"}, protocol_version="2.0")
    ).json()

    assert body["ok"] is False
    assert body["error"]["code"] == "unsupported_protocol_version"


def test_service_plugin_reports_a_failure_without_crashing(tmp_path, monkeypatch):
    """A bad input must come back as a structured error, not a 500.

    A missing patch is a `not_found`, not an `unsupported_operation`: the
    host asked for a real action with a bad argument, and telling it the
    operation does not exist would send it looking in the wrong place.
    """
    client = _plugin_client(tmp_path, monkeypatch)

    body = client.post("/invoke", json=_envelope("preview", {"spec": {}})).json()

    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"
    assert body["output"] is None


def test_service_plugin_async_compose_returns_a_job_that_finishes(
    tmp_path, monkeypatch
):
    """The facade has a 10s invoke ceiling; compose takes far longer.

    So the envelope has to be able to hand back a job handle, and the job
    has to actually complete.
    """
    client = _plugin_client(tmp_path, monkeypatch)

    body = client.post(
        "/invoke",
        json=_envelope("compose", {"brief": "a warm pad", "mode": "async"}),
    ).json()

    assert body["ok"] is True
    job_id = body["output"]["job_id"]
    assert body["output"]["status"] == "running"

    snapshot = {}
    for _ in range(200):
        snapshot = client.get(f"/jobs/{job_id}").json()
        if snapshot["status"] != "running":
            break
        time.sleep(0.05)

    assert snapshot["status"] == "succeeded", snapshot
    assert snapshot["output"]["spec"]["title"]
    assert snapshot["error"] is None


def test_service_plugin_namespaces_artifacts_by_thread(tmp_path, monkeypatch):
    """The envelope carries a thread_id; artifacts should land under it."""
    client = _plugin_client(tmp_path, monkeypatch)

    envelope = _envelope("compose", {"brief": "a warm pad"})
    envelope["context"]["thread_id"] = "42"
    body = client.post("/invoke", json=envelope).json()

    assert body["ok"] is True
    stems = [a for a in body["output"]["artifacts"] if a["role"] == "stem"]
    assert stems, "compose should produce stems"
    assert all("/42/" in a["path"] for a in stems), stems


def test_approve_writes_a_library_that_did_not_exist_yet(tmp_path, monkeypatch):
    """Approving on a fresh install must actually persist.

    Reading the library and writing it are different questions: the file
    may not exist yet, and conflating the two made approve a silent no-op.
    """
    client = _plugin_client(tmp_path, monkeypatch)

    spec = client.post(
        "/invoke", json=_envelope("compose", {"brief": "a warm pad"})
    ).json()["output"]["spec"]

    library = tmp_path / "fresh-library.json"
    assert not library.exists()

    body = client.post(
        "/invoke",
        json=_envelope(
            "approve", {"spec": spec, "library_path": str(library)}
        ),
    ).json()

    assert body["ok"] is True
    assert library.exists(), "approve must create the library file"
    saved = json.loads(library.read_text(encoding="utf-8"))
    assert saved["entries"], "the approved patches should be in there"


def test_the_router_compose_writes_where_the_host_asked(tmp_path, monkeypatch):
    """The host owns the output directory; the adapter must honour it."""
    client = _plugin_client(tmp_path, monkeypatch)

    out_dir = tmp_path / "host-owned"
    body = client.post(
        "/api/music/compose",
        json={"brief": "a warm pad", "out_dir": str(out_dir)},
    ).json()

    assert body["artifacts"]
    assert all(
        str(out_dir) in a["path"] for a in body["artifacts"] if a
    ), body["artifacts"]
    assert (out_dir / "mix.wav").exists()


def test_the_openapi_schema_gives_every_route_a_request_body():
    """Codexify derives its model-callable tools from this schema.

    So the schema is not documentation, it is the contract the model reads
    before it calls anything. A route whose parameters were demoted to
    query parameters still appears in the schema -- it just looks wrong
    to the model, and every call 422s. Asserting on the schema catches
    that class of bug without needing to know it happened.
    """
    from fastapi import FastAPI

    from agentdaw.codexify.router import build_router

    app = FastAPI()
    app.include_router(build_router())
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})

    def _resolve(body):
        """Follow a $ref into the component schemas."""
        ref = body.get("$ref")
        if not ref:
            return body
        name = ref.rsplit("/", 1)[-1]
        return components.get(name, body)

    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            if method == "get":
                # A GET legitimately carries its arguments in the query
                # string; that is not the bug this test is about.
                continue
            assert "requestBody" in operation, f"{method.upper()} {path} has no body"
            body = _resolve(
                operation["requestBody"]["content"]["application/json"]["schema"]
            )
            assert body.get("type") == "object", f"{method.upper()} {path}"
            assert body.get("properties"), f"{method.upper()} {path} has an empty body"

            # Nothing was demoted into the query string on the way.
            for parameter in operation.get("parameters", []):
                assert parameter["in"] != "query", (
                    f"{method.upper()} {path} exposes {parameter['name']} as a "
                    "query parameter; the request body was lost"
                )


def test_the_skill_says_to_measure_rather_than_opine():
    """The skill is the conduct that goes with the capability."""
    skill = (
        Path(__file__).resolve().parents[1]
        / "agentdaw"
        / "codexify"
        / "skills"
        / "music-producer"
        / "SKILL.md"
    )
    body = skill.read_text(encoding="utf-8").partition("---")[2].partition("---")[2]

    lowered = body.lower()
    assert "lufs" in lowered or "measure" in lowered
    assert "retrieve" in lowered, "the skill should tell the model to retrieve first"


def test_the_plugin_manifest_agrees_with_the_service():
    """Codexify advertises capabilities from the manifest and calls them.

    If the manifest names an action the service refuses, the host is told
    a capability exists and then gets `unsupported_operation` when it
    reaches for it. The manifest and the implementation are two halves of
    one contract, so they are checked against each other.
    """
    from agentdaw.serve import ACTIONS

    manifest_path = (
        Path(__file__).resolve().parents[1]
        / "agentdaw"
        / "codexify"
        / "plugins"
        / "agentdaw"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "1.0"
    assert manifest["id"] == "agentdaw"

    declared = sorted(
        f"{capability['id']}.{action}"
        for capability in manifest["capabilities"]
        for action in capability["actions"]
    )
    assert declared == sorted(f"music.{action}" for action in ACTIONS)

    # Codexify's PluginManifest validates base_url as http/https with a
    # host and no path, query or fragment.
    base_url = manifest["base_url"]
    assert base_url.startswith(("http://", "https://"))
    assert "/" not in base_url[len("http://"):].strip("/")


def test_the_service_rejects_an_action_it_does_not_implement(tmp_path, monkeypatch):
    """The advertised set is closed: an unknown action is refused, not guessed."""
    client = _plugin_client(tmp_path, monkeypatch)

    body = client.post("/invoke", json=_envelope("render_backwards")).json()

    assert body["ok"] is False
    assert body["error"]["code"] == "unsupported_operation"



