"""The Producer.

The artist describes a vision; the Producer turns it into a Session Spec
that the engine compiles. This module holds the producer's instructions and
the validation loop around its output.

Two design commitments run through this file:

1. The Producer never writes DSP units. Everything is semantic and
   normalized. The engine owns hertz, milliseconds and decibels.

2. The Producer does not claim taste. It claims measurement. It is asked to
   produce a brief it can be held to, and the measurement pass in
   `analyze.py` is what backs up any claim it makes about the result.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from pydantic import ValidationError

from .llm import LLMClient, LLMError, LLMTruncated, extract_json
from .spec import SessionSpec

# Constraints the artist states outright. These are enforced after the
# fact, because a model told "120 BPM" will sometimes answer 88 BPM and
# will describe it plausibly. The brief says the tempo is locked; the
# check makes that mean something.
_BPM_RE = re.compile(r"\b(\d{2,3})\s*(?:bpm|b\.p\.m)\b", re.IGNORECASE)
_BARS_RE = re.compile(r"\b(\d{1,2})\s*[- ]?bars?\b", re.IGNORECASE)
_KEY_RE = re.compile(r"\b([a-gA-G])(#|b)?\s+(major|minor)\b")


def extract_constraints(vision: str) -> dict:
    """Pull explicit constraints out of the artist's wording.

    Only unambiguous statements count. "around 120" or "maybe 8 bars" are
    not constraints, and treating them as such would make the Producer
    fail on a description that was never precise.
    """
    constraints: dict = {}

    bpm = _BPM_RE.search(vision)
    if bpm:
        value = float(bpm.group(1))
        if 20.0 <= value <= 300.0:
            constraints["bpm"] = value

    bars = _BARS_RE.search(vision)
    if bars:
        value = int(bars.group(1))
        if 1 <= value <= 64:
            constraints["bars"] = value

    key = _KEY_RE.search(vision)
    if key:
        letter = key.group(1).upper()
        accidental = key.group(2) or ""
        constraints["key"] = f"{letter}{accidental} {key.group(3).lower()}"

    return constraints


def check_constraints(spec: SessionSpec, constraints: dict) -> list[str]:
    """Return the constraints the produced spec violates."""
    violations: list[str] = []

    if "bpm" in constraints and abs(spec.bpm - constraints["bpm"]) > 0.01:
        violations.append(
            f"tempo must be {constraints['bpm']:g} BPM, the spec says {spec.bpm:g}"
        )
    if "bars" in constraints and spec.bars != constraints["bars"]:
        violations.append(
            f"arrangement must be {constraints['bars']} bars, the spec says {spec.bars}"
        )
    if "key" in constraints:
        wanted = constraints["key"].lower()
        actual = f"{spec.key.lower()} {spec.mode.lower()}".strip()
        if wanted not in actual and actual not in wanted:
            violations.append(
                f"key must be {constraints['key']}, the spec says {spec.key} {spec.mode}"
            )

    return violations

PRODUCER_SYSTEM = """\
You are the Studio Producer for a record. The artist describes a vision in
plain language; you turn it into a Session Spec that a synthesis engine
compiles into audio. You are the artist, not the audience.

# The one rule that matters

Every number you write is SEMANTIC and normalized to 0.0 - 1.0. You never
write hertz, milliseconds, decibels, ratios, filter Q values, or sample
rates. The engine owns that math and it is very good at it.

If you want a filter around 800 Hz you do not write 800. You set
`brightness` and the engine decides. If you want a long release you set
`release` and the engine decides the seconds. This is not a stylistic
preference -- models that emit raw DSP numbers produce sounds that are
either inaudibly thin or broken, because they cannot reason about the
acoustic consequence of the number they chose.

# Output

Return ONE JSON object. No prose before or after it. No markdown fences.

{
  "version": "1.0",
  "title": "string",
  "bpm": 120.0,
  "key": "C minor",
  "mode": "natural minor",
  "bars": 8,
  "time_signature": [4, 4],
  "brief": {
    "goal": "", "deliverable": "", "tempo": "", "layers": "",
    "midi_audio": "", "length": "", "energy": "", "structure": "",
    "movement": "", "production": "", "variations": "",
    "locked": [], "source": ""
  },
  "sections": [
    {"id": "intro", "bars": 2, "energy": 0.3, "layers": ["drums", "pad"], "note": ""}
  ],
  "patches": [
    {"id": "pad_warm", "name": "Warm Pad", "description": "",
     "macros": {"body": 0.6, "brightness": 0.35, "motion": 0.45,
                "grit": 0.15, "space": 0.6, "size": 0.7},
     "spectrum": {"brightness": 0.45, "odd_emphasis": 0.0, "even_emphasis": 0.0,
                  "tilt": -0.2, "formant": 0.0, "partials": []},
     "shape": {"attack": 0.45, "decay": 0.4, "sustain": 0.8, "release": 0.55},
     "lfo": {"rate": 0.25, "depth": 0.3, "target": "filter", "shape": "sine"},
     "seed": 101}
  ],
  "tracks": [
    {"id": "pad", "name": "Pad", "role": "chords", "kind": "midi",
     "patch_id": "pad_warm", "gain_db": 0.0, "pan": 0.0, "muted": false,
     "parts": [{"id": "pad_1", "track_id": "pad", "notes": [
        {"pitch": 60, "start": 0.0, "duration": 8.0, "velocity": 0.75}
     ]}],
     "audio": null, "generator": {}}
  ]
}

# The brief

Fill the brief FIRST and make it specific. This is the success contract:
it is what the artist checks before you build anything, and it is how they
give surgical corrections ("keep tempo, change the bass"). Put anything
the artist stated as fixed into `locked`.

# Macros

Each patch is described by six semantic axes plus a harmonic description.

macros.body       low-end fullness and resonance. low = hollow, high = solid
macros.brightness spectral brightness. low = dark, high = present
macros.motion     movement over time: LFO rate and depth, filter-envelope travel
macros.grit       saturation
macros.space      reverb, delay and chorus depth
macros.size       stereo width and unison detune

spectrum describes the harmonic content of the oscillator:
  brightness  how many harmonics are present at all
  odd_emphasis  positive favours odd harmonics (hollow), negative favours even
  even_emphasis  positive favours even harmonics
  tilt  negative = darker rolloff, positive = brighter
  formant  a resonant bump placed in the harmonic series
  partials  optional explicit list of {"n": harmonic number, "amp": 0..1}.
             Use this ONLY when the artist is specific about a tone.

# Arrangement

- Sections tile the whole arrangement. The bar counts MUST sum to `bars`.
- `layers` lists the track ids that are playing in that section.
- Energy rises and falls across the form. Not every section is loud.
- Leave space. An arrangement where every track plays in every bar is not
  an arrangement.

# MIDI

- `start` and `duration` are in BEATS, from the start of the arrangement.
- `velocity` is 0.0 - 1.0. Vary it. Real playing is never uniform.
- Do not place every note exactly on the grid. Leave small timing and
  velocity variation -- that is what separates a played part from a
  quantized block.
- Keep the harmony coherent: choose a key and a mode and stay inside them.
  A bassline that wanders outside the scale is the most common failure.
- Give each timbral role its own track, and at most one patch per role
  unless the artist explicitly asked for a sound change.

# Generative audio (optional)

If the artist asked for generated texture, beds, or an element you cannot
synthesize, put it on a track with `"kind": "audio"` and fill `generator`:
{"provider": "stable_audio", "prompt": "...", "duration_s": 30,
 "isolate": true, "seed": 7}
Use `isolate: true` when you want ONE instrument rather than a full
arrangement -- prompt it as an isolated part, not as a song.

# Judgement

You are not the audience. Do not describe the result as beautiful, epic,
or emotional. Describe what you built: tempo, key, form, instrumentation,
what moves and what stays put. Let the artist decide how it feels.
"""

_REVISION_TEMPLATE = """\
Here is the current Session Spec and the artist's correction.

CURRENT SPEC:
{current}

ARTIST CORRECTION:
{feedback}

Produce the complete updated Session Spec as one JSON object. Apply the
correction surgically: leave everything the correction does not touch
exactly as it was, including the `locked` line of the brief. Do not
re-roll parts you were not asked to change.
"""


class Producer(Protocol):
    def compose(self, vision: str, **kwargs) -> SessionSpec: ...


class LLMProducer:
    """A Producer backed by a language model."""

    def __init__(
        self,
        client: LLMClient,
        max_repairs: int = 2,
        max_tokens: int = 16000,
        max_tokens_cap: int = 64000,
        thinking_budget: int = 4000,
    ) -> None:
        self.client = client
        self.max_repairs = max_repairs
        self.max_tokens = max_tokens
        self.max_tokens_cap = max_tokens_cap
        self.thinking_budget = thinking_budget

    def compose(
        self,
        vision: str,
        patch_library: list[dict] | "PatchLibrary" | None = None,
        existing: SessionSpec | None = None,
        feedback: str | None = None,
    ) -> SessionSpec:
        if feedback is not None and existing is not None:
            prompt = _REVISION_TEMPLATE.format(
                current=existing.model_dump_json(indent=2),
                feedback=feedback,
            )
        else:
            prompt = self._initial_prompt(vision, patch_library)

        constraints = extract_constraints(vision) if feedback is None else {}

        last_error: Exception | None = None
        budget = self.max_tokens
        truncation_retries = 0

        for attempt in range(self.max_repairs + 1):
            try:
                text = self.client.complete(
                    PRODUCER_SYSTEM, prompt,
                    max_tokens=budget,
                    thinking_budget=self.thinking_budget,
                )
                data = extract_json(text)
                spec = SessionSpec.model_validate(data)
            except LLMTruncated as exc:
                # Reasoning models share max_tokens between deliberation and
                # answer. When the model spends the whole budget thinking,
                # give it more room rather than asking it to try again the
                # same way.
                last_error = exc
                truncation_retries += 1
                if budget >= self.max_tokens_cap or truncation_retries > 2:
                    raise
                budget = min(budget * 2, self.max_tokens_cap)
                continue
            except (ValidationError, ValueError, LLMError) as exc:
                last_error = exc
                prompt = (
                    f"{prompt}\n\nYour previous answer was rejected:\n{exc}\n\n"
                    "Return the corrected complete JSON object."
                )
                continue

            errors = spec.validate_references()
            errors += check_constraints(spec, constraints)
            if errors:
                last_error = ValueError("; ".join(errors))
                prompt = (
                    f"{prompt}\n\nYour previous answer was rejected:\n"
                    f"{last_error}\n\nReturn the corrected complete JSON object. "
                    "Change only what is wrong; keep everything else identical."
                )
                continue

            return spec

        raise RuntimeError(f"Producer failed to produce a valid spec: {last_error}")

    def _initial_prompt(
        self, vision: str, patch_library: list[dict] | "PatchLibrary" | None
    ) -> str:
        constraints = extract_constraints(vision)

        lines = [f"ARTIST BRIEF:\n{vision.strip()}\n\nProduce the Session Spec."]

        if constraints:
            stated = ", ".join(f"{k}={v}" for k, v in constraints.items())
            lines.append(
                f"\nThe artist stated these explicitly: {stated}. "
                "These are locked. Use them verbatim. Do not reinterpret them."
            )

        lines.append(self._library_prompt(vision, patch_library))

        return "\n".join(lines)

    def _library_prompt(
        self, vision: str, patch_library: list[dict] | "PatchLibrary" | None
    ) -> str:
        """Describe the approved anchors, and how to use them.

        A library that matches nothing is a legitimate outcome, and the
        prompt says so: inventing is allowed, it just has to be recorded
        as new rather than passed off as a recall.
        """
        lines: list[str] = []

        if patch_library is not None:
            from .library import PatchLibrary

            if isinstance(patch_library, PatchLibrary):
                hits = patch_library.retrieve(vision)
                if not hits:
                    lines.append(
                        "\n\nAPPROVED PATCH LIBRARY: nothing in it matches this "
                        "request. Invent a new patch."
                    )
                else:
                    lines.append(
                        "\n\nAPPROVED PATCH LIBRARY (these already exist and work well):"
                    )
                    lines.append(json.dumps([h.summary() for h in hits], indent=2))
                    lines.append(
                        "You may reuse one of these patches verbatim by referencing its "
                        "id, or describe a variation of one by adjusting its macros. "
                        "Prefer reusing an approved anchor over inventing something "
                        "new -- the anchors are known to sound good."
                    )
                    lines.append(
                        "The entries above carry the measurements that were taken of "
                        "them, and any direction the request implies. Where a request "
                        "asks for one of them to move, copy that anchor's spectrum, "
                        "shape and LFO unchanged and move only the axis named."
                    )
            else:
                lines.append(
                    "\n\nAPPROVED PATCH LIBRARY (these already exist and work well):"
                )
                lines.append(json.dumps(list(patch_library), indent=2))
                lines.append(
                    "You may reuse one of these patches verbatim by referencing its id, "
                    "or describe a variation of one by adjusting its macros. Prefer "
                    "reusing an approved anchor over inventing something new -- the "
                    "anchors are known to sound good."
                )

        # This rule is unconditional. It matters most when there is no
        # library to fall back on, because that is exactly when the model
        # is most likely to emit a half-specified patch.
        lines.append(
            "\nEvery patch you emit must be COMPLETE: all six macros, a full "
            "spectrum, a full shape and a full LFO. Never leave one axis "
            "unspecified because you kept it from the anchor -- a revision is "
            "a whole new state, not a half-updated one."
        )

        return "\n".join(lines)


class StubProducer:
    """Deterministic Producer. Used by the tests and for a first run without
    spending tokens. It is not trying to be clever; it produces a valid,
    musically-coherent spec so the pipeline can be exercised end to end."""

    def compose(self, vision: str, **kwargs) -> SessionSpec:
        data = {
            "version": "1.0",
            "title": vision[:48] or "Untitled",
            "bpm": 120.0,
            "key": "C minor",
            "mode": "natural minor",
            "bars": 8,
            "time_signature": [4, 4],
            "brief": {
                "goal": vision[:200],
                "deliverable": "8-bar sketch with pad, bass and drums",
                "tempo": "120 BPM, 4/4",
                "layers": "drums, bass, pad",
                "midi_audio": "MIDI for melodic parts, audio for drums",
                "length": "8 bars",
                "energy": "low to medium",
                "structure": "intro, build, drop, outro",
                "movement": "pad filter opens across the form",
                "production": "wide pad, round bass, dry drums",
                "variations": "none yet",
                "locked": ["tempo", "key"],
            },
            "sections": [
                {"id": "intro", "bars": 2, "energy": 0.25, "layers": ["pad"], "note": ""},
                {"id": "build", "bars": 2, "energy": 0.55, "layers": ["pad", "bass", "drums"], "note": ""},
                {"id": "drop", "bars": 3, "energy": 0.85, "layers": ["pad", "bass", "drums"], "note": ""},
                {"id": "outro", "bars": 1, "energy": 0.3, "layers": ["pad"], "note": ""},
            ],
            "patches": [
                {
                    "id": "pad_warm",
                    "name": "Warm Pad",
                    "description": "Wide warm pad that opens across the form.",
                    "macros": {"body": 0.6, "brightness": 0.35, "motion": 0.5,
                               "grit": 0.15, "space": 0.65, "size": 0.75},
                    "spectrum": {"brightness": 0.45, "odd_emphasis": 0.0,
                                 "even_emphasis": 0.0, "tilt": -0.25,
                                 "formant": 0.0, "partials": []},
                    "shape": {"attack": 0.5, "decay": 0.4, "sustain": 0.8, "release": 0.6},
                    "lfo": {"rate": 0.2, "depth": 0.35, "target": "filter", "shape": "sine"},
                    "seed": 101,
                },
                {
                    "id": "bass_round",
                    "name": "Round Bass",
                    "description": "Simple round bass with a short filter tail.",
                    "macros": {"body": 0.7, "brightness": 0.25, "motion": 0.2,
                               "grit": 0.3, "space": 0.15, "size": 0.2},
                    "spectrum": {"brightness": 0.3, "odd_emphasis": 0.4,
                                 "even_emphasis": 0.0, "tilt": -0.4,
                                 "formant": 0.0, "partials": []},
                    "shape": {"attack": 0.05, "decay": 0.3, "sustain": 0.6, "release": 0.25},
                    "lfo": {"rate": 0.1, "depth": 0.1, "target": "filter", "shape": "sine"},
                    "seed": 202,
                },
            ],
            "tracks": [
                {
                    "id": "pad", "name": "Pad", "role": "chords", "kind": "midi",
                    "patch_id": "pad_warm", "gain_db": -3.0, "pan": 0.0, "muted": False,
                    "parts": [{
                        "id": "pad_1", "track_id": "pad",
                        "notes": [
                            {"pitch": 48, "start": 0.0, "duration": 8.0, "velocity": 0.62},
                            {"pitch": 60, "start": 0.02, "duration": 7.9, "velocity": 0.58},
                            {"pitch": 63, "start": 0.04, "duration": 7.8, "velocity": 0.55},
                            {"pitch": 67, "start": 0.01, "duration": 7.7, "velocity": 0.54},
                        ],
                    }],
                    "audio": None, "generator": {},
                },
                {
                    "id": "bass", "name": "Bass", "role": "bass", "kind": "midi",
                    "patch_id": "bass_round", "gain_db": -4.0, "pan": 0.0, "muted": False,
                    "parts": [{
                        "id": "bass_1", "track_id": "bass",
                        "notes": [
                            {"pitch": 36, "start": 0.0, "duration": 1.9, "velocity": 0.86},
                            {"pitch": 36, "start": 2.0, "duration": 1.9, "velocity": 0.78},
                            {"pitch": 39, "start": 4.0, "duration": 1.9, "velocity": 0.82},
                            {"pitch": 34, "start": 6.0, "duration": 1.9, "velocity": 0.74},
                        ],
                    }],
                    "audio": None, "generator": {},
                },
                {
                    "id": "drums", "name": "Drums", "role": "drums", "kind": "audio",
                    "patch_id": None, "gain_db": -6.0, "pan": 0.0, "muted": False,
                    "parts": [], "audio": None,
                    "generator": {"provider": "local_drums", "prompt": "straight four-to-the-floor kit"},
                },
            ],
        }
        return SessionSpec.model_validate(data)
