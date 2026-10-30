"""Session and Patch schemas.

THE CONTRACT. Every value in here is *semantic* and normalized to 0..1.
The LLM only ever writes values of this shape. Concrete DSP units
(hertz, milliseconds, decibels, ratios) live exclusively in `macros.resolve`
and `wavetable`, where the engine owns the math.

That separation is deliberate. LLMs have real intuition about harmonic
content, envelope shape, and relative brightness. They have none for
filter cutoffs in Hz or Q factors. Letting the model emit raw DSP numbers
is the single most common way this kind of system breaks -- see the
DAWZY paper (arXiv 2512.03289), where unit conversion was the reported
failure mode for LLM-driven DAW edits.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------
# Patch
# --------------------------------------------------------------------------

class Macros(BaseModel):
    """Six semantic timbre axes. Pure function of these -> concrete params."""

    body: float = Field(0.5, ge=0.0, le=1.0, description="Low-end fullness and resonance. Low = hollow and thin, high = full and solid.")
    brightness: float = Field(0.5, ge=0.0, le=1.0, description="Spectral brightness. Low = dark and muffled, high = present and cutting.")
    motion: float = Field(0.4, ge=0.0, le=1.0, description="Amount of movement over time: LFO rate and depth, plus filter-envelope travel.")
    grit: float = Field(0.2, ge=0.0, le=1.0, description="Saturation and harmonic distortion.")
    space: float = Field(0.4, ge=0.0, le=1.0, description="Reverb, delay and chorus depth.")
    size: float = Field(0.5, ge=0.0, le=1.0, description="Stereo width and unison detune. High = wide and thick, low = mono and focused.")


class Partial(BaseModel):
    n: int = Field(..., ge=1, le=128, description="Harmonic number. 1 is the fundamental.")
    amp: float = Field(..., ge=0.0, le=1.0, description="Relative amplitude of this harmonic.")


class SpectrumSpec(BaseModel):
    """How the oscillator's wavetable is built.

    The engine never sees a wave. It sees a description of the harmonic
    content and synthesizes the table. This is the domain the LLM is
    genuinely good at.
    """

    brightness: float = Field(0.5, ge=0.0, le=1.0, description="How many harmonics are present at all.")
    odd_emphasis: float = Field(0.0, ge=-1.0, le=1.0, description="Positive favors odd harmonics (hollow, clarinet-like). Negative favors even (hollow, nasal).")
    even_emphasis: float = Field(0.0, ge=-1.0, le=1.0, description="Positive favors even harmonics.")
    tilt: float = Field(0.0, ge=-1.0, le=1.0, description="Spectral tilt. Negative = darker (fast rolloff), positive = brighter (slow rolloff).")
    formant: float = Field(0.0, ge=0.0, le=1.0, description="Resonant formant emphasis somewhere in the harmonic series.")
    partials: list[Partial] = Field(default_factory=list, description="Optional explicit partials. When present they override the derived spectrum.")

    @field_validator("partials")
    @classmethod
    def _unique_partials(cls, v: list[Partial]) -> list[Partial]:
        seen: set[int] = set()
        for p in v:
            if p.n in seen:
                raise ValueError(f"duplicate partial {p.n}")
            seen.add(p.n)
        return v


class ShapeSpec(BaseModel):
    """Envelope shape expressed as feel, not seconds."""

    attack: float = Field(0.3, ge=0.0, le=1.0, description="0 = instant, 1 = very slow swell.")
    decay: float = Field(0.4, ge=0.0, le=1.0, description="0 = sharp drop, 1 = long decay.")
    sustain: float = Field(0.7, ge=0.0, le=1.0, description="0 = decaying to silence, 1 = fully held.")
    release: float = Field(0.5, ge=0.0, le=1.0, description="0 = cut off, 1 = long tail.")


class LfoSpec(BaseModel):
    rate: float = Field(0.3, ge=0.0, le=1.0, description="0 = very slow, 1 = fast.")
    depth: float = Field(0.2, ge=0.0, le=1.0, description="How far the LFO pushes its target.")
    target: Literal["filter", "pitch", "amp", "pan"] = "filter"
    shape: Literal["sine", "triangle", "saw"] = "sine"


class PatchSpec(BaseModel):
    """A deployable instrument preset.

    The whole point: this is a portable, editable, re-generatable
    description -- not a rendered blob. Ship the spec alongside the stems
    and the MIDI and the user can retune it anywhere.
    """

    id: str
    name: str
    description: str = ""
    macros: Macros = Field(default_factory=Macros)
    spectrum: SpectrumSpec = Field(default_factory=SpectrumSpec)
    shape: ShapeSpec = Field(default_factory=ShapeSpec)
    lfo: LfoSpec = Field(default_factory=LfoSpec)
    seed: int = Field(0, ge=0, description="Pinned for reproducibility. Changing it yields a deliberate variation, never a silent one.")


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

class Brief(BaseModel):
    """The success contract the Producer posts before any tool runs.

    Forces alignment, and makes the Artist's corrections surgical: you
    edit the `locked` line and everything downstream stays put.
    """

    goal: str = ""
    deliverable: str = ""
    tempo: str = ""
    layers: str = ""
    midi_audio: str = ""
    length: str = ""
    energy: str = ""
    structure: str = ""
    movement: str = ""
    production: str = ""
    variations: str = ""
    locked: list[str] = Field(default_factory=list)
    source: str = ""


class MidiNote(BaseModel):
    pitch: int = Field(..., ge=0, le=127)
    start: float = Field(..., ge=0.0, description="Beats from the start of the part.")
    duration: float = Field(..., ge=0.0)
    velocity: float = Field(0.8, ge=0.0, le=1.0)


class MidiPart(BaseModel):
    id: str
    track_id: str
    notes: list[MidiNote] = Field(default_factory=list)


class ArrangementSection(BaseModel):
    id: str
    bars: int = Field(..., ge=1)
    energy: float = Field(0.5, ge=0.0, le=1.0)
    layers: list[str] = Field(default_factory=list, description="Track ids active in this section.")
    note: str = ""


class TrackSpec(BaseModel):
    id: str
    name: str
    role: Literal["drums", "bass", "chords", "lead", "texture", "fx"] = "texture"
    kind: Literal["midi", "audio"] = "midi"
    patch_id: str | None = None
    gain_db: float = Field(0.0, ge=-48.0, le=12.0)
    pan: float = Field(0.0, ge=-1.0, le=1.0)
    muted: bool = False
    parts: list[MidiPart] = Field(default_factory=list)
    audio: str | None = Field(None, description="Path to a rendered audio stem (generative or rendered).")
    generator: dict = Field(default_factory=dict, description="Generative-audio request: provider, prompt, isolation tags, seed.")


class SessionSpec(BaseModel):
    version: str = "1.0"
    title: str = "Untitled"
    bpm: float = Field(120.0, ge=20.0, le=300.0)
    key: str = "C minor"
    mode: str = "natural minor"
    bars: int = Field(8, ge=1)
    time_signature: tuple[int, int] = (4, 4)
    brief: Brief = Field(default_factory=Brief)
    sections: list[ArrangementSection] = Field(default_factory=list)
    tracks: list[TrackSpec] = Field(default_factory=list)
    patches: list[PatchSpec] = Field(default_factory=list)

    # -- lookups ----------------------------------------------------------

    def patch(self, patch_id: str) -> PatchSpec | None:
        return next((p for p in self.patches if p.id == patch_id), None)

    def track(self, track_id: str) -> TrackSpec | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    def section_start_bar(self, section_id: str) -> int:
        bar = 0
        for s in self.sections:
            if s.id == section_id:
                return bar
            bar += s.bars
        return bar

    def validate_references(self) -> list[str]:
        """Return a list of human-readable reference errors. Empty means valid."""
        errors: list[str] = []
        patch_ids = {p.id for p in self.patches}
        track_ids = {t.id for t in self.tracks}
        seen: set[str] = set()

        if not self.tracks:
            errors.append("session has no tracks")
        if not self.sections:
            errors.append("session has no arrangement sections")

        for t in self.tracks:
            if t.id in seen:
                errors.append(f"duplicate track id: {t.id}")
            seen.add(t.id)
            if t.patch_id and t.patch_id not in patch_ids:
                errors.append(f"track {t.id} references unknown patch {t.patch_id}")
            if t.kind == "midi" and t.parts and not t.patch_id:
                # A MIDI track with notes but no patch renders silence, which
                # looks like a broken engine rather than a missing field.
                errors.append(f"track {t.id} has MIDI notes but no patch")
            for part in t.parts:
                if part.track_id != t.id:
                    errors.append(f"part {part.id} claims track {part.track_id} but lives on {t.id}")
                for n in part.notes:
                    if n.start + n.duration > self.bars * 4 + 1e-6:
                        errors.append(
                            f"note in {part.id} runs past the end of the arrangement "
                            f"({n.start + n.duration:.2f} > {self.bars * 4:.2f} beats)"
                        )

        for s in self.sections:
            for layer in s.layers:
                if layer not in track_ids:
                    errors.append(f"section {s.id} references unknown track {layer}")

        total = sum(s.bars for s in self.sections)
        if self.sections and total != self.bars:
            errors.append(f"sections total {total} bars but session declares {self.bars}")

        return errors


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

def validate_session(spec: SessionSpec) -> list[str]:
    return spec.validate_references()


def json_schema_bundle() -> dict[str, dict]:
    """Export the pydantic models as JSON Schema. This is the published
    contract -- the thing a hosted product would serve to third parties."""
    return {
        "PatchSpec": PatchSpec.model_json_schema(),
        "SessionSpec": SessionSpec.model_json_schema(),
        "Brief": Brief.model_json_schema(),
        "Macros": Macros.model_json_schema(),
    }
