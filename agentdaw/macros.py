"""Macro -> concrete engine parameters. Pure, and it stays that way.

Every mapping here is a *total function* of the semantic inputs. Nothing is
carried over from a previous state, nothing is re-derived from ambient
context. That discipline matters: a filter's cutoff and its resonance are
paired values, and if one is retained while the other is recomputed you get
the classic bug where a theme flip produces white-on-white or a resonance
that no longer matches the cutoff that produced it.

The rule: a revision produces a new macro vector, and *everything* is
recomputed from it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .spec import LfoSpec, Macros, PatchSpec, ShapeSpec, SpectrumSpec


@dataclass(frozen=True)
class EngineParams:
    """Concrete DSP units. Only this layer is allowed to speak Hz, ms and dB."""

    # filter
    cutoff_hz: float
    resonance: float
    filter_env_octaves: float
    filter_env_attack: float
    filter_env_decay: float
    filter_env_sustain: float

    # modulation
    lfo_rate_hz: float
    lfo_depth: float
    lfo_target: str
    lfo_shape: str

    # drive
    drive_pre: float
    drive_post: float

    # space
    reverb_mix: float
    delay_mix: float
    delay_time_s: float
    delay_feedback: float
    chorus_mix: float
    chorus_rate_hz: float
    chorus_depth_ms: float

    # unison
    unison_voices: int
    detune_cents: float
    width: float

    # envelope (seconds)
    attack_s: float
    decay_s: float
    sustain: float
    release_s: float

    # spectrum
    max_harmonics: int


def _exp_map(value: float, lo: float, hi: float) -> float:
    """Map 0..1 onto a lo..hi range exponentially. Perceptually correct for
    frequency and time; a linear map would put everything in the mud."""
    if hi <= 0 or lo <= 0:
        return lo + (hi - lo) * value
    return lo * (hi / lo) ** value


def resolve(
    macros: Macros,
    spectrum: SpectrumSpec | None = None,
    shape: ShapeSpec | None = None,
    lfo: LfoSpec | None = None,
) -> EngineParams:
    """Resolve semantic inputs into concrete engine parameters."""
    spectrum = spectrum or SpectrumSpec()
    shape = shape or ShapeSpec()
    lfo = lfo or LfoSpec()

    m = macros

    # --- filter -----------------------------------------------------------
    # Cutoff spans 80 Hz to 8 kHz. Brightness 0.5 lands near 800 Hz, which
    # is a sane "middle" for a pad rather than the geometric centre.
    cutoff_hz = _exp_map(m.brightness, 80.0, 8000.0)
    # Resonance rises as the sound gets thinner: a hollow patch is a
    # resonant patch. Capped at 5.1 so it stays musical rather than shrieky.
    resonance = 0.6 + (1.0 - m.body) * 4.5

    # --- modulation -------------------------------------------------------
    # One knob drives both rate and depth so that "movement" reads as a
    # single gesture rather than two unrelated numbers.
    lfo_rate_hz = _exp_map(m.motion, 0.05, 20.0)
    lfo_depth = 0.05 + m.motion * 0.75
    filter_env_octaves = 0.5 + m.motion * 5.0

    # --- drive ------------------------------------------------------------
    drive_pre = 1.0 + m.grit * 7.0
    drive_post = 1.0 / (1.0 + m.grit * 0.5)

    # --- space ------------------------------------------------------------
    reverb_mix = m.space * 0.75
    delay_mix = m.space * 0.35
    delay_time_s = 0.125 + m.space * 0.375  # dotted-eighth territory
    delay_feedback = 0.15 + m.space * 0.3
    chorus_mix = m.space * 0.30
    chorus_rate_hz = _exp_map(m.motion, 0.1, 6.0)
    chorus_depth_ms = 3.0 + m.space * 9.0

    # --- unison -----------------------------------------------------------
    unison_voices = 1 + int(round(m.size * 4.0))
    detune_cents = 3.0 + m.size * 25.0
    width = m.size

    # --- envelope ---------------------------------------------------------
    # "Feel" -> seconds, cubed so that the useful range (0.2-0.5) lands in
    # musically sensible territory instead of everything sounding slow.
    attack_s = 0.002 + shape.attack**3 * 4.0
    decay_s = 0.010 + shape.decay**2.5 * 8.0
    release_s = 0.020 + shape.release**2.5 * 8.0
    sustain = shape.sustain

    # The filter envelope is snappier than the amplitude envelope; that
    # asymmetry is what makes a pad feel like it is "opening".
    filter_env_attack = attack_s * 0.6
    filter_env_decay = max(0.05, decay_s * 0.5)
    filter_env_sustain = 0.35 + sustain * 0.5

    max_harmonics = int(round(4.0 + spectrum.brightness * 44.0))

    return EngineParams(
        cutoff_hz=cutoff_hz,
        resonance=resonance,
        filter_env_octaves=filter_env_octaves,
        filter_env_attack=filter_env_attack,
        filter_env_decay=filter_env_decay,
        filter_env_sustain=filter_env_sustain,
        lfo_rate_hz=lfo_rate_hz,
        lfo_depth=lfo_depth,
        lfo_target=lfo.target,
        lfo_shape=lfo.shape,
        drive_pre=drive_pre,
        drive_post=drive_post,
        reverb_mix=reverb_mix,
        delay_mix=delay_mix,
        delay_time_s=delay_time_s,
        delay_feedback=delay_feedback,
        chorus_mix=chorus_mix,
        chorus_rate_hz=chorus_rate_hz,
        chorus_depth_ms=chorus_depth_ms,
        unison_voices=unison_voices,
        detune_cents=detune_cents,
        width=width,
        attack_s=attack_s,
        decay_s=decay_s,
        sustain=sustain,
        release_s=release_s,
        max_harmonics=max_harmonics,
    )


def resolve_patch(patch: PatchSpec) -> EngineParams:
    return resolve(patch.macros, patch.spectrum, patch.shape, patch.lfo)


def apply_deltas(base: Macros, deltas: dict[str, float]) -> Macros:
    """Apply semantic deltas to a macro vector.

    This is the mechanism behind the retrieval-anchor loop: fetch a
    previously approved patch, describe the change ("darker", "shorter
    tail"), and land on a new macro vector. Still pure -- the result is a
    complete macro vector, never a partially-patched one.
    """
    values = base.model_dump()
    for key, delta in deltas.items():
        if key not in values:
            raise KeyError(f"unknown macro: {key}")
        values[key] = min(1.0, max(0.0, values[key] + delta))
    return Macros(**values)
