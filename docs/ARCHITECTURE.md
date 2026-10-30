# Architecture

How a sentence becomes audio.

```
  ARTIST (natural language)
        │
        ▼
  PRODUCER  ── brief, plan, delegate, repair invalid specs
        │  emits
        ▼
  SESSION SPEC  (JSON — the contract; semantic values only)
        │
        ├── patches[]  ──▶ macro map ──▶ engine params (Hz, ms, dB)
        │                 spectrum  ──▶ wavetable (harmonic synthesis)
        │                 shape/lfo ──▶ envelopes + modulation
        │
        ├── tracks[]   ──▶ MIDI parts ──▶ .mid  (SMF writer)
        │                 generator{} ──▶ provider (Stable Audio / Lyria)
        │                 audio path  ──▶ .wav stem
        │
        ▼
  ENGINE
        │
        ├── per-track render (osc → filter → env → drive → chorus → delay → reverb)
        ├── mix
        ├── measurement pass (BS.1770, spectral, band, stereo)
        └── REAPER .rpp
```

---

## 1. The spec is the contract

Two documents, both in `agentdaw/spec.py`, both exported as JSON Schema:

- **PatchSpec** — one instrument preset.
- **SessionSpec** — an arrangement: sections, tracks, patches, MIDI parts.

Everything in them is **semantic and normalized to 0..1**. There is no
hertz, no milliseconds, no decibels, no Q value anywhere in the model's
output. This is enforced by the type system: the fields are floats with
`ge=0.0, le=1.0` bounds, so there is no way to express "cutoff 440 Hz"
even by accident.

The schemas are the published interface. A hosted product serves them to
third parties; the engine behind them could be rewritten in Rust or
TypeScript without breaking a single patch.

### PatchSpec

| Field | Meaning |
|---|---|
| `macros.body` | low-end fullness and resonance. low = hollow, high = solid |
| `macros.brightness` | spectral brightness. low = dark, high = present |
| `macros.motion` | movement over time: LFO rate, depth, filter-envelope travel |
| `macros.grit` | saturation |
| `macros.space` | reverb, delay and chorus depth |
| `macros.size` | stereo width and unison detune |
| `spectrum.*` | how the oscillator's wavetable is built |
| `shape.*` | envelope shape as feel, not seconds |
| `lfo.*` | rate, depth, target, shape |
| `seed` | pinned for reproducibility |

### SessionSpec

| Field | Meaning |
|---|---|
| `bpm`, `key`, `mode`, `bars`, `time_signature` | the frame |
| `brief` | the success contract — see below |
| `sections[]` | `id`, `bars`, `energy`, `layers[]`, `note` |
| `tracks[]` | `id`, `name`, `role`, `kind`, `patch_id`, gain/pan, `parts[]`, `audio`, `generator` |
| `patches[]` | the patch library for this session |

### The brief

The Producer fills a `Brief` before it builds anything: `goal`,
`deliverable`, `tempo`, `layers`, `midi_audio`, `length`, `energy`,
`structure`, `movement`, `production`, `variations`, and a `locked` list.

`locked` is what makes corrections surgical. If the artist says "keep the
tempo, change the bass", the tempo is in `locked` and everything else is
free. Without it, every revision risks quietly re-rolling parts that were
already right.

This shape is borrowed from Veena's CoProducer brief, which is a
reasonable design and not a copy.

---

## 2. The macro map is a pure function

`agentdaw/macros.py` turns a macro vector into `EngineParams` — the only
place in the codebase allowed to speak in hertz and milliseconds.

Two properties are non-negotiable:

1. **Totality.** Every output is a function of every input. Nothing is
   carried over from a previous state.
2. **Pairing.** Cutoff and resonance are derived together. Body and
   brightness are linked. Retaining one half while re-deriving the other
   is how a revision produces a filter that no longer matches the sound
   it was tuned for.

`apply_deltas()` supports the retrieval-anchor loop: fetch an approved
patch, apply semantic deltas, recompute everything. The result is always
a complete macro vector, never a partially-patched one.

### The mappings

| Macro | Derived | Mapping |
|---|---|---|
| brightness | cutoff | `80 * (8000/80)^b` Hz — exponential, because a linear map puts everything in the mud |
| body | resonance | `0.6 + (1-body)*4.5` — a hollow patch is a resonant patch |
| motion | lfo rate | `0.05 * (20/0.05)^m` Hz |
| motion | lfo depth | `0.05 + m*0.75` |
| motion | filter env travel | `0.5 + m*5.0` octaves |
| grit | drive | pre-gain `1 + grit*7`, output compensated |
| space | reverb / delay / chorus | `0.75s` / `0.35s` / `0.30s` |
| size | unison voices | `1 + round(size*4)` |
| size | detune | `3 + size*25` cents |
| size | pan spread | voice `v` is panned to `spread * size` |

---

## 3. Wavetable synthesis

`agentdaw/wavetable.py` builds one cycle of a wavetable from harmonic
amplitudes.

`spectrum.brightness` sets how many harmonics exist (4–48).
`spectrum.tilt` sets the rolloff exponent. `odd_emphasis` and
`even_emphasis` shift the balance *both ways* — pushing the odds up
pulls the evens down, because a boost alone is not a shift.

The table is **band-limited per pitch**: `wavetable_for_midi()` rebuilds
it with harmonics above Nyquist removed for that note's frequency. A
bright patch played high does not turn into aliased hash.

Tables are cached per pitch, so a chord of four notes builds four tables
and every note after that is free.

---

## 4. The engine

`agentdaw/dsp.py`. Signal path:

```
oscillator (wavetable, unison, detune, optional vibrato)
    → mix
    → filter (RBJ lowpass, time-varying cutoff, resonance)
    → amp envelope (ADSR)
    → drive (tanh)
    → chorus (modulated delay)
    → delay (feedback delay)
    → reverb (FFT convolution with a synthesized IR)
    → gain / pan
```

### Vectorization, and the constraint behind it

There are no per-sample Python loops. Three techniques make that work:

**Oscillator.** Phase is a `cumsum` of per-sample increments; the table
lookup is manual linear interpolation over an integer index. Fully
vectorized, including per-sample vibrato.

**Filter.** A time-varying cutoff needs a piecewise-constant coefficient
per block. `sweep_filter()` recomputes RBJ coefficients every 256 samples
(5.8 ms) and runs `scipy.signal.lfilter` with carried state, so each
block is *exact* rather than approximated. Multi-channel input is
filtered one channel at a time — SciPy's `lfilter` wants one initial
state per row for 2D input, and filtering channels independently is
what preserves the stereo image anyway.

**Delay line.** A feedback delay is a gather-then-scatter over a
circular buffer: read at `absolute_position - delay`, write the result
at `absolute_position`. This is only valid if the read distance is at
least one block length — otherwise a read can hit a sample written in the
same block. The code enforces it rather than assuming it.

Reverb is convolution against a synthesized impulse response (decaying
noise, one-pole darkened, plus early reflections), computed with
`fftconvolve`.

### Chords share a filter envelope

Notes are grouped by onset. A chord gets one filter sweep and one
envelope, which is musically right and several times cheaper than
filtering each note.

### The audition preview

`render_preview()` renders the patch **under a deterministic drum bed**.
This is not decoration: a patch judged in isolation is a different patch
than the same patch under a beat, and timbre judgment depends on the
context. The drum bed is deliberately mechanical and repeatable — it is a
reference surface for listening, not a creative element.

---

## 5. The measurement pass

`agentdaw/analyze.py`. The Producer cannot hear, so it measures.

| Measurement | How |
|---|---|
| integrated LUFS | ITU-R BS.1770-4: K-weighting (high shelf + RLB high-pass), 400 ms blocks, 100 ms hop, absolute and relative gating |
| loudness range | 10th–95th percentile of gated block loudness |
| spectral centroid / rolloff | FFT of the windowed signal |
| low / mid / high balance | 20–200 / 200–2000 / 2000+ Hz |
| crest factor | peak vs RMS |
| stereo width | `sqrt(1 - corr(L, R))` |

**BS.1770 sums channel energies, it does not average them.** Averaging
biases every stereo reading by exactly 3.01 LU — small enough to look
plausible, constant enough to go unnoticed. The test suite pins the
channel-sum property directly and cross-checks against ffmpeg's
`ebur128` on a real signal.

`report_findings()` turns measurements into sentences a producer would
say, and deliberately not into taste claims:

```
Bass: low band sits 24.1 dB above the mid band -- possible low-end buildup.
Pad:  spectral centroid is 1132 Hz -- bright and exposed.
Drums: integrated loudness is -30.7 LUFS, very quiet.
```

---

## 6. Handoff formats

| Artifact | Writer | Notes |
|---|---|---|
| MIDI parts | `midi.py` | SMF format 1, 480 ticks/beat, tempo + time signature track, meta track name, program change, CC74 carrying macro position |
| Stems | `wav.py` | 24-bit PCM by default, 32-bit float optional, no third-party library |
| Project | `reaper.py` | REAPER `.rpp` — plain text, scriptable, so the handoff carries real project state |
| Library | `library.py` | Approved patches as retrievable anchors: the Artist's words, the macro vector, and the measured facts |
| Host seam | `service.py` | The operations as plain data in and out, so any host can mount them |
| Schema | `schema.py` | JSON Schema exports of both contracts |

The `.rpp` writer is written against the RPP structure and checked
against a project that REAPER 7.62 itself saved on macOS, not against a
guess. That comparison is what turned up the three details that decide
whether a generated project actually opens and renders:

- `RENDER_RANGE`'s first field is the render **bounds mode**. `0` means
  "custom time range" — with start and end both `0` that is a
  zero-length range, and REAPER renders an empty file while reporting
  success. `1` means "entire project".
- Track volume in a `.rpp` is a **linear multiplier**, not decibels.
  REAPER writes `0.501187` for -6 dB; writing `-6.0` there means a linear
  gain of -6.0, which is -15.6 dB.
- The master track is expressed as `MASTER*` project keys, not as a
  `<TRACK>` block. Without them there is no master bus to render into.

Each render writes two projects: one whose tracks carry their rendered
stems (the mix-ready handoff, opens with sound) and one whose melodic
tracks carry their `.mid` (the sequencing view, for dropping a real
instrument on and re-sequencing).

**REAPER has opened one of these projects.** REAPER 7.82 was launched
against a generated `.rpp`; its own config came back naming that file as
`lastproject`, `projecttab1` and `recent01`, which is REAPER's own
account of having parsed and opened it. The render is still outstanding,
blocked by REAPER's evaluation screen — a modal that stops startup dead
and reappears periodically until REAPER is licensed. See the README for
the environment constraints and for `tools/verify_reaper_handoff.py`,
which drives REAPER from inside its own process to close the loop.

---

## 6b. The library, and the host seam

### The library is the Producer's memory

`agentdaw/library.py`. Every patch the artist approves becomes an
**anchor**: named, measured, retrievable. Retrieval ranks on three things
at once -- the artist's own words, the macro vector, and the measured
facts -- and reports which of them matched.

The design rule that matters: **a revision is a complete new state, not
a patch.** `PatchLibrary.revise` never mutates the parent. It writes a
new entry carrying the whole macro vector and the whole spectrum, plus
its parent and the artist's words describing the change. That is the
same rule the engine already follows for macro maps, applied to the
memory of the system rather than to its DSP.

Retrieval is deliberately willing to return nothing. A miss means the
Producer invents, and the result is recorded as born new rather than
passed off as a recall. A library that always returns a hit is a library
that teaches the model to be confident without evidence.

The measured layer is what makes this more than a keyword search. An
anchor records the numbers the analysis pass produced, so "a brighter
version of that pad" is resolved against a centroid in Hz and a macro
position rather than a sentence embedding alone.

### The host seam

`agentdaw/service.py`. Six operations, each taking JSON-ish input and
returning JSON-ish output:

| Operation | In | Out |
|---|---|---|
| `compose_session` | a brief | spec, artifacts, measurements, findings, library hits |
| `revise_session` | a spec + feedback | a complete new spec |
| `preview_patch` | a spec + patch id | measurements + findings for one patch |
| `render_session_artifacts` | a spec | artifacts, measurements, findings |
| `retrieve_anchors` | a query | ranked hits with reasons, or a miss |
| `approve_patches` | a spec + measurements | the library with the session's patches in it |

Nothing here knows about Codexify. The artifacts come back shaped the
way a host registry wants them -- `role`, `path`, `media_kind`,
`mime_type`, `provenance` -- so registering them is a loop, not a
translation.

`agentdaw/codexify/` is the Codexify adapter: a FastAPI router over the
service layer, and the `music-producer` skill that carries the conduct
the model needs (retrieve before inventing, measure don't opine, revise
by delta, keep the artist's stated tempo and key locked). Because
Codexify derives its agent-callable tools from the OpenAPI schema,
mounting the routes is what makes the Producer callable by a model.

The full mapping onto Codexify's object model -- Music Project as a
`Project` row, threads and chats as the ordinary `ChatThread` /
`ChatMessage` pairs, patches in the vector store, stems as `MediaAsset`
rows -- is in **[CODEXIFY-INTEGRATION.md](CODEXIFY-INTEGRATION.md)**.

---

## 7. Generative audio providers

`agentdaw/providers/`. One interface, two adapters.

```python
class AudioProvider(Protocol):
    name: str
    def generate(self, request: GenerationRequest, out_dir: Path) -> GenerationResult: ...
```

**Stable Audio 3.0** is the interesting one, because it can generate an
*isolated part* rather than a finished song. A prompt opening with
`TrackType: Instrument` returns one instrument. That is the difference
between generative audio being a source in a production and generative
audio being the whole production.

It also has audio-to-audio (`init_noise_level`) and inpainting with
numeric mask boundaries — how you revise a generated bed without
regenerating it. Open weights exist on all four models, so self-hosting
is possible when you want unlimited resampling and no per-call cost.

**Lyria 3 / 3 Pro** is the structured one: real form (intro, verse,
chorus, bridge), BPM control, instrumental mode. It returns a single mix
rather than stems, so separate locally if you need them — Ableton Live 12
does that offline for free, and `python-audio-separator` with
BS-RoFormer weights does it from the command line.

Neither adapter has been exercised against a live API. No key is
configured on this machine.

---

## 8. The Producer

`agentdaw/producer.py`. The system prompt teaches the model the schema,
the macro semantics, the arrangement rules, and one hard rule: never
write DSP units. It then loops:

1. Ask for a spec.
2. Parse the JSON out of the response (models wrap it in prose and
   fences regardless of how firmly you ask).
3. Validate against the pydantic schema.
4. Validate the internal references — dangling patch ids, section bar
   counts that do not sum, notes that run past the end, tracks with MIDI
   but no patch.
5. Validate against the constraints the artist stated outright.
6. If anything failed, send the errors back and let it repair.

### Constraint enforcement

The step that matters most is 5. A model told "120 BPM" will sometimes
answer 88 BPM and describe it plausibly. `extract_constraints()` pulls
unambiguous statements out of the vision — tempo, bar count, key — and
`check_constraints()` compares them against the returned spec.

Only unambiguous wording counts. "around 120" or "maybe 8 bars" are not
constraints, and treating them as such would make the Producer fail on a
description that was never precise.

### Reasoning models and the token budget

MiniMax's models require adaptive thinking and reject
`thinking.type="disabled"`. `max_tokens` is therefore shared between
deliberation and answer. Measured on `MiniMax-M3.1-Flash`:

| budget | outcome |
|---|---|
| 4000 | `stop_reason=max_tokens`, 3999 thinking tokens, **no text at all** |
| 8000 | thinking 5152, text produced but the JSON is cut off mid-object |
| 16000 | thinking 8927, complete JSON, ~115 s |
| 16000 + `budget_tokens=4000` | thinking ~3555, complete JSON, ~41 s |

A model that returns no text is a distinct failure from a model that
returns bad JSON, and it needs a different response: more room, not a
retry with the same budget. `LLMTruncated` carries that distinction, and
the Producer grows its budget up to a cap.

The `budget_tokens` cap is the better lever — it cut the round trip from
115 s to 41 s without truncating the answer.

### Credentials

The runtime stores an OAuth access token under
`~/.minimax/auth/<region>/mcode-public/auth.json` and rotates it on its
own schedule. The client reads it fresh, sends it as `Authorization:
Bearer`, and on a 401 reloads it and retries once. A first-party
Anthropic endpoint would need `x-api-key` instead, so the style is
resolved by endpoint and can be overridden with `AGENTDAW_LLM_AUTH_STYLE`.

---

## 9. The product path

The personal tool and the hosted product differ in four places, and the
code is already arranged so they do not leak into each other:

| Concern | Personal | Product |
|---|---|---|
| Credentials | runtime OAuth token, auto-refreshed on 401 | per-tenant keys |
| Patch library | a local folder | a database, with retrieval |
| Rendering | local, in-process | a worker pool, possibly Rust |
| Spec | local JSON | served as JSON Schema, versioned |

**The patch library is the compounding asset.** Every patch the artist
approves becomes a retrievable anchor: stored with its macro vector, an
embedding of its description, and a rendered reference. A later request
retrieves the nearest anchor and asks for *deltas* — "basically that
one, but darker, with a shorter tail" — which turns free-form generation
into controlled editing. That is where language models are reliable and
where artists are fast to judge, and it means the system gets better
with use instead of staying generic.

**The Rust path.** The DSP is the performance-critical part and Python is
an implementation detail. `macros.py`, `wavetable.py`, and the spec
schemas are the boundaries: a Rust engine implements the same two pure
functions and consumes the same JSON, and nothing above them changes.

---

## 10. Deliberately not built yet

- **A DAW interface.** Timeline, mixer, plugin hosting — all commodity.
  The interface is a chat and an audition panel.
- **More than one instrument.** The engine is subtractive only. A
  sample-based or FM path would widen the palette considerably.
- **Multi-agent roles.** A patch designer, an arranger, a mixer and a
  critic as separate agents with separate verifiable jobs is the right
  shape, and it is designed for in the Producer's structure but not
  built.
- **Track-level generative audio integration.** `TrackSpec.generator`
  carries a provider request, but the engine does not yet call the
  provider during a session render.
